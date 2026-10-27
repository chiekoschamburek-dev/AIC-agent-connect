#!/usr/bin/env python3
"""AIP 自测 Leader（纯客户端版）：用 ACPs SDK 调用「问迹」自身或指定 Partner。

设计变更（v2，2026-10-19）：
- 不再在自测进程内构造 ``WitRuntime`` —— 那会为同一 AIC 再绑一套 AMP runtime，
  与 serve 进程并发写同一 journal 会触发 wit 的
  ``AMP access append refused a journal changed during validation``（实测）。
- Discovery 直接 POST 平台 REST 接口；委派走 ``AipRpcClient`` + 自身 mTLS 证书。
  于是任务级 access（correlationId=taskId）只在 serve 侧产生一条，计数口径干净。

示例（backend/ 目录）：
    .venv/bin/python scripts/acps/aip_selftest.py --question "销量最高的前五首曲目是什么"
    .venv/bin/python scripts/acps/aip_selftest.py --query "问数" --no-delegate
    .venv/bin/python scripts/acps/aip_selftest.py --question "增长率是多少" --answer "近一年，销售额"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import ssl
import sys
import time
import uuid
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[3]
BACKEND_DIR = REPO_DIR / "backend"
sys.path.insert(0, str(BACKEND_DIR))

TERMINAL = {"awaiting-input", "awaiting-completion", "completed", "failed", "rejected", "canceled"}


def _load_env() -> None:
    from dotenv import load_dotenv

    load_dotenv(REPO_DIR / ".env", override=False)
    load_dotenv(REPO_DIR / ".env.acps", override=False)


def _identity():
    import os

    ws = Path(os.environ.get("ACPS_WORKSPACE", ".wit/wenji-aip-agent"))
    if not ws.is_absolute():
        ws = (BACKEND_DIR / ws).resolve()
    acps = ws / "acps"
    enrollment = json.loads((acps / "enrollment.json").read_text(encoding="utf-8"))
    final_acs = json.loads((acps / "acs.final.json").read_text(encoding="utf-8"))
    cfg = enrollment.get("acpsConfig") or {}
    aic = final_acs.get("aic") or (enrollment.get("review") or {}).get("aic") or ""
    endpoint = str(cfg.get("publicEndpoint") or "").rstrip("/")
    ctx = ssl.create_default_context(cafile=str(acps / "trust-bundle.pem"))
    ctx.load_cert_chain(str(acps / "client/certificate.pem"), str(acps / "client/private-key.pem"))
    return aic, f"{endpoint}/rpc", str(cfg.get("discoveryUrl") or ""), ctx


async def _discover(url: str, query: str, limit: int = 10) -> list[dict]:
    """直接调用 ADP REST 接口（无需 WitRuntime）。"""
    import httpx

    payload = {"type": "explicit", "query": query, "limit": limit}
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
    result = data.get("result") or {}
    acs_map = result.get("acsMap") or {}
    alive = result.get("aliveMap") or {}
    agents = []
    for aic, acs in acs_map.items():
        agents.append({
            "aic": aic,
            "name": acs.get("name"),
            "active": (alive.get(aic) or {}).get("alive", acs.get("active")),
            "skills": [s.get("id") for s in (acs.get("skills") or [])],
        })
    return agents


def _state(task) -> str:
    value = task.status.state
    return str(getattr(value, "value", value))


async def _run(args: argparse.Namespace) -> int:
    from acps_sdk.aip import AipRpcClient

    aic, url, discovery_url, ctx = _identity()
    print(f"[info] 本机 AIC: {aic or '(未激活)'} | endpoint: {url}")

    partner = args.partner_aic or aic
    agents = await _discover(discovery_url, args.query)
    print(f"[discover] '{args.query}' → {len(agents)} 个候选")
    for a in agents:
        mark = "✅" if a["aic"] == partner else "  "
        print(f"  {mark} {a['aic']} | {a['name']} | active={a['active']} | skills={a['skills'][:4]}")
    if not agents:
        print("[warn] 未返回候选（可检查 discovery 是否正常；仍尝试委派）")
    elif not args.partner_aic:
        partner = next((a["aic"] for a in agents if a["active"]), agents[0]["aic"])

    if args.no_delegate:
        print("[info] 仅发现模式")
        return 0

    from app.acps.leader_access import emit_leader_access, leader_access_emitter

    ws = Path(os.environ.get("ACPS_WORKSPACE", ".wit/wenji-aip-agent"))
    ws = ws if ws.is_absolute() else (BACKEND_DIR / ws).resolve()
    emitter = leader_access_emitter(ws, aic)

    session_id = f"selftest-{uuid.uuid4()}"
    client = AipRpcClient(
        partner_url=url,
        leader_id=aic,
        ssl_context=ctx,
        expected_partner_aic=partner,
    )
    t0 = time.monotonic()
    try:
        print(f"[delegate] → {partner}: {args.question}")
        task = await client.start_task(session_id=session_id, user_input=args.question)
        task_id = task.taskId
        state = _state(task)
        print(f"[task] state={state} id={task_id}")

        rounds = 0
        while state not in TERMINAL and rounds < 60:
            await asyncio.sleep(1.0)
            task = await client.get_task(task_id=task_id, session_id=session_id)
            state = _state(task)
            rounds += 1
            if rounds % 5 == 0:
                print(f"[wait] state={state} ({time.monotonic() - t0:.0f}s)")

        if state == "awaiting-input" and args.answer:
            print(f"[continue] 补充: {args.answer}")
            task = await client.continue_task(
                task_id=task_id, session_id=session_id, user_input=args.answer
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
            print("[complete] 已确认产出")

        elapsed = time.monotonic() - t0
        await emit_leader_access(
            emitter,
            task_id=task_id,
            state=state,
            duration_ms=elapsed * 1000,
            partner_aic=partner,
            partner_url=url,
            extra={"selftest": True},
        )
        print(f"[result] state={state} elapsed={elapsed:.1f}s")
        for product in task.products or []:
            for item in product.dataItems or []:
                text = getattr(item, "text", None)
                if text:
                    print(f"--- {product.name or product.id} ---")
                    print(text[:800])
        return 0 if state in {"completed", "awaiting-completion"} else 1
    finally:
        await client.close()


def main() -> int:
    _load_env()
    parser = argparse.ArgumentParser(description="问迹 AIP 自测 Leader（纯客户端）")
    parser.add_argument("--question", default="销量最高的前五首曲目是什么？")
    parser.add_argument("--answer", default="", help="awaiting-input 时的补充输入（触发 continue）")
    parser.add_argument("--query", default="问数 数据库 文档问答")
    parser.add_argument("--partner-aic", default="", help="指定 Partner（默认本机）")
    parser.add_argument("--no-delegate", action="store_true", help="只做发现")
    args = parser.parse_args()
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
