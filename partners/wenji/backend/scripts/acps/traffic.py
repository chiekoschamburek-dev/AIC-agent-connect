#!/usr/bin/env python3
"""AIP 流量运行器（纯客户端版，多工作并发）。

设计变更（v3，2026-10-19）：
- 不再在流量进程内构造 WitRuntime —— 那会为同一 AIC 绑定第二套 AMP runtime，
  与 serve 进程并发写同一 journal 会触发 wit 的 `AMP access append refused`。
- 改为用 ACPs SDK 的 ``AipRpcClient`` + 自身 mTLS 客户端证书，扮演真实外部 Leader
  调用公网端点；任务级 access（correlationId=taskId）仍由 serve 侧 TaskAccessHooks 记录。

成本（实测，BUPT deepseek-v4-flash 免费额度）：单任务 ≈ 0 元。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import ssl
import sys
import time
from datetime import date
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[3]
BACKEND_DIR = REPO_DIR / "backend"
sys.path.insert(0, str(BACKEND_DIR))

# 类别权重：单表问数最快（吞吐优先），跨源最慢占少数
CASE_FILES = [
    ("eval/cases/nl2sql_single_table.jsonl", 6),
    ("eval/cases/nl2sql_multi_table.jsonl", 3),
    ("eval/cases/rag_corpus_w5.jsonl", 2),
    ("eval/cases/cross_source.jsonl", 1),
    ("eval/cases/clarify_questions.jsonl", 1),
]

TERMINAL = {"awaiting-input", "awaiting-completion", "completed", "failed", "rejected", "canceled"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


WORKERS = max(1, int(_env_float("TRAFFIC_WORKERS", 3)))
INTERVAL_MIN = _env_float("TRAFFIC_INTERVAL_MIN", 5.0)
JITTER_MIN = _env_float("TRAFFIC_JITTER_MIN", 2.0)
DAILY_MAX = int(_env_float("TRAFFIC_DAILY_MAX", 1000))
LOG_PATH = Path(os.environ.get("TRAFFIC_LOG") or (REPO_DIR / "var/traffic.log"))

_today = {"date": "", "n": 0}


def log(message: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%F %T')} {message}"
    print(line, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def count_today() -> int:
    today = date.today().isoformat()
    if _today["date"] != today:
        _today.update(date=today, n=0)
        if LOG_PATH.exists():
            with LOG_PATH.open("r", encoding="utf-8") as fh:
                _today["n"] = sum(1 for line in fh if line.startswith(today) and "[ok]" in line)
    return _today["n"]


def bump_today() -> None:
    count_today()
    _today["n"] += 1


def load_questions() -> list[str]:
    """按类别权重展开题库（权重越高，取样概率越大）。"""
    questions: list[str] = []
    for name, weight in CASE_FILES:
        path = BACKEND_DIR / name
        if not path.exists():
            continue
        bucket: list[str] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            q = str(record.get("question") or "").strip()
            if q:
                bucket.append(q)
        questions.extend(bucket * max(1, weight))
    return questions


def _workspace() -> Path:
    ws = Path(os.environ.get("ACPS_WORKSPACE", ".wit/wenji-aip-agent"))
    return ws if ws.is_absolute() else (BACKEND_DIR / ws).resolve()


def load_identity():
    """读取证书 / AIC / 端点。

    - Partner 固定为问迹（ACPS_WORKSPACE）；
    - Leader 默认用问迹自身证书（自调用）；设置 ``TRAFFIC_LEADER_WORKSPACE``
      可改用独立身份（如 .wit/wenji-probe），从而形成真实跨 AIC 调用。
    """
    partner_ws = _workspace()
    partner_acps = partner_ws / "acps"
    partner_enr = json.loads((partner_acps / "enrollment.json").read_text(encoding="utf-8"))
    partner_final = json.loads((partner_acps / "acs.final.json").read_text(encoding="utf-8"))
    partner_aic = partner_final.get("aic") or (partner_enr.get("review") or {}).get("aic") or ""
    endpoint = (partner_enr.get("acpsConfig") or {}).get("publicEndpoint") or ""
    if not endpoint:
        raise SystemExit("[fatal] 问迹 enrollment 中无 publicEndpoint")

    leader_ws = Path(os.environ.get("TRAFFIC_LEADER_WORKSPACE", "") or partner_ws)
    if not leader_ws.is_absolute():
        leader_ws = (BACKEND_DIR / leader_ws).resolve()
    acps = leader_ws / "acps"
    client_cert = acps / "client/certificate.pem"
    client_key = acps / "client/private-key.pem"
    ca = acps / "trust-bundle.pem"
    for p in (client_cert, client_key, ca):
        if not p.exists():
            raise SystemExit(f"[fatal] 缺少证书材料: {p}")
    leader_enr = json.loads((acps / "enrollment.json").read_text(encoding="utf-8"))
    leader_final = json.loads((acps / "acs.final.json").read_text(encoding="utf-8"))
    leader_aic = leader_final.get("aic") or (leader_enr.get("review") or {}).get("aic") or ""

    ctx = ssl.create_default_context(cafile=str(ca))
    ctx.load_cert_chain(certfile=str(client_cert), keyfile=str(client_key))
    return leader_aic, f"{endpoint.rstrip('/')}/rpc", ctx, partner_aic, partner_ws


async def run_task(client, question: str) -> tuple[str, float, str]:
    """start → 轮询/等待 → complete，返回 (state, elapsed_s)。"""
    import uuid

    t0 = time.monotonic()
    session_id = f"traffic-session-{uuid.uuid4()}"
    task = await client.start_task(session_id=session_id, user_input=question)
    task_id = task.taskId
    rounds = 0
    state = _state(task)
    # BUPT 内核单任务 20~60s；轮询上限 240s，超时视为失败（不误记为 ok）
    while state not in TERMINAL and rounds < 240:
        await asyncio.sleep(1.0)
        task = await client.get_task(task_id=task_id, session_id=session_id)
        state = _state(task)
        rounds += 1
    if state == "awaiting-completion":
        task = await client.complete_task(task_id=task_id, session_id=session_id)
        state = _state(task)  # complete 后刷新（本应是 completed）
    if state == "awaiting-input":
        task = await client.continue_task(
            task_id=task_id, session_id=session_id, user_input="按最常见口径（近一年、销售额）"
        )
        state = _state(task)
        while state not in TERMINAL and rounds < 120:
            await asyncio.sleep(1.0)
            task = await client.get_task(task_id=task_id, session_id=session_id)
            state = _state(task)
            rounds += 1
        if state == "awaiting-completion":
            task = await client.complete_task(task_id=task_id, session_id=session_id)
            state = _state(task)
    if state not in TERMINAL:
        raise TimeoutError(f"任务 {task_id} 超过 {rounds}s 仍未到终态（state={state}）")
    return state, time.monotonic() - t0, task_id


def _state(task) -> str:
    """TaskState 枚举 -> 字符串（兼容 str 枚举与纯字符串实现）。"""
    value = task.status.state
    return str(getattr(value, "value", value))


async def worker(aic: str, url: str, ctx, questions: list[str], wid: int,
                 partner_aic: str = "", partner_ws: Path | None = None) -> None:
    from acps_sdk.aip import AipRpcClient

    from app.acps.leader_access import emit_leader_access, leader_access_emitter

    emitter = leader_access_emitter(partner_ws or _workspace(), aic)

    client = AipRpcClient(
        partner_url=url,
        leader_id=aic,
        ssl_context=ctx,
        expected_partner_aic=partner_aic or aic,
    )
    last = ""
    failures = 0
    try:
        while True:
            if count_today() >= DAILY_MAX:
                await asyncio.sleep(600)
                continue
            question = random.choice(questions)
            if len(questions) > 1:
                while question == last:
                    question = random.choice(questions)
            last = question
            try:
                state, elapsed, task_id = await run_task(client, question)
                failures = 0
                bump_today()
                await emit_leader_access(
                    emitter,
                    task_id=task_id,
                    state=state,
                    duration_ms=elapsed * 1000,
                    partner_aic=partner_aic or aic,
                    partner_url=url,
                    extra={"traffic.worker": wid, "traffic.question": question[:120]},
                )
                log(f"[ok] w{wid} state={state} {elapsed:.1f}s q={question[:40]}")
            except Exception as exc:
                failures += 1
                log(f"[fail#{failures}] w{wid} {type(exc).__name__}: {str(exc)[:100]} q={question[:40]}")
                if failures >= 8:
                    log(f"[fatal] w{wid} 连续失败 8 次，本工作退出")
                    return
                await asyncio.sleep(min(300, 30 * failures))
            sleep_min = max(1.0, INTERVAL_MIN + random.uniform(-JITTER_MIN, JITTER_MIN))
            await asyncio.sleep(sleep_min * 60)
    finally:
        await client.close()


async def main() -> None:
    from dotenv import load_dotenv

    load_dotenv(REPO_DIR / ".env", override=False)
    load_dotenv(REPO_DIR / ".env.acps", override=False)

    questions = load_questions()
    if not questions:
        log("[fatal] 题库为空")
        return
    aic, url, ctx, partner_aic, partner_ws = load_identity()
    log(f"[boot] 题库 {len(questions)} 条 · {WORKERS} worker × {INTERVAL_MIN}±{JITTER_MIN} 分钟 · 日上限 {DAILY_MAX}")
    log(f"[boot] leader={aic or '(空)'} -> partner={partner_aic or '(空)'} endpoint={url}")

    workers = [
        asyncio.create_task(worker(aic, url, ctx, questions, i, partner_aic, partner_ws))
        for i in range(WORKERS)
    ]
    done, pending = await asyncio.wait(workers, return_when=asyncio.FIRST_EXCEPTION)
    for task in pending:
        task.cancel()
    log("[stop] 运行器退出")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("[stop] 手动停止")
