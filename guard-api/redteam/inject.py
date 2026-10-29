#!/usr/bin/env python3
"""guard-api 红队注入实验（2026-10-29）

目的：回答"通信守卫到底能不能抓到真实的隐蔽信道"。
做法：在 AIP 事件流上构造四组对照，全部走 guard-api 官方入口
      （load_jsonl -> analyze_events），不改任何检测代码。

四组：
  A plain-exfil        朴素外传：base64 高熵分片，随机间隔，状态单调
  B timing-modulation  时序调制：0/1 交替节拍，明文分片，状态单调
  C evasive            规避版：base64 切段避开正则 + 抖动 + 分片 + 单调状态
  N normal-control     正常对照：模拟真实轮询（等间隔 task-result）

用法：
    python redteam/inject.py              # 跑四组并打印报告
    python redteam/inject.py --live       # 额外回放本机真实任务库
产物：redteam/out/*.jsonl（可复现的事件流存档）
"""

from __future__ import annotations

import argparse
import base64
import glob
import importlib.util
import json
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = HERE / "out"
T0 = datetime(2026, 10, 29, 0, 0, 0, tzinfo=timezone.utc)


def _load_guard():
    """直接从文件加载检测器，避免 fastapi 依赖。"""
    spec = importlib.util.spec_from_file_location(
        "ceg", str(ROOT / "guard_api" / "covert_event_guard.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ceg"] = mod
    spec.loader.exec_module(mod)
    return mod


G = _load_guard()


# ---------------------------------------------------------------- 事件构造


def ev(task_ref: str, at: datetime, state: str, result: dict, seq: int,
       event_type: str = "task-result") -> dict:
    """构造一条与真实 aip_task_events 同构的事件。"""
    return {
        "task_ref": task_ref,
        "created_at": at.isoformat().replace("+00:00", "Z"),
        "event_type": event_type,
        "event_seq": seq,
        "message_id": f"msg-{task_ref}-{seq:04d}",
        "event_data": {
            "type": event_type,
            "status": {"state": state},
            "result": result,
        },
    }


def secret(n: int = 4096, seed: int = 1337) -> bytes:
    """待外传的敏感数据：高熵（模拟密钥材料/加密后的客户名单）。"""
    return random.Random(seed).randbytes(n)


def b64_chunks(data: bytes, size: int = 60) -> list[str]:
    """把 base64 串切成短段——B64 正则要求 {80,}，切到 60 即可绕过。"""
    whole = base64.b64encode(data).decode()
    return [whole[i:i + size] for i in range(0, len(whole), size)]


# ---------------------------------------------------------------- 四组场景


def scene_a() -> list[dict]:
    """A 朴素外传：整段 base64（≥80 字符）+ 高熵 + 随机间隔 + 单调状态。"""
    data = secret(4096)
    task = "rt-A-plain-exfil"
    out, at = [], T0
    for i in range(5):
        chunk = data[i * 800:(i + 1) * 800]
        out.append(ev(task, at, "working",
                      {"blob": base64.b64encode(chunk).decode(), "part": i}, i + 1))
        at += timedelta(seconds=random.Random(11 + i).uniform(3.0, 7.0))
    out.append(ev(task, at, "completed", {"ok": True}, len(out) + 1))
    return out


def scene_b() -> list[dict]:
    """B 时序调制：0/1 交替节拍（2s/6s），明文分片躲开 base64 检测。"""
    data = secret(2048)
    task = "rt-B-timing-modulation"
    out, at = [], T0
    for i in range(10):
        chunk = data[i * 200:(i + 1) * 200]
        # hex 分片：字符集虽在 B64 集合内，但长度 <80，不匹配正则
        out.append(ev(task, at, "working",
                      {"part": i, "hex": chunk.hex()[:60]}, i + 1))
        at += timedelta(seconds=2.0 if i % 2 == 0 else 6.0)
    out.append(ev(task, at, "completed", {"ok": True}, len(out) + 1))
    return out


def scene_c() -> list[dict]:
    """C 规避版：同时绕开三条维度。

    - 载荷维：base64 切成 60 字符段（<80，B64 正则不匹配）；单片远小于 64KB
    - 时序维：间隔加 ±20% 抖动，把 CV 抬到 0.08 以上
    - 状态维：严格单调，不出现 awaiting-input -> working 这类往返
    """
    data = secret(4096)
    chunks = b64_chunks(data, size=60)
    task = "rt-C-evasive"
    rng = random.Random(2026)
    out, at = [], T0
    for i, seg in enumerate(chunks[:12]):
        out.append(ev(task, at, "working", {"part": i, "seg": seg}, i + 1))
        at += timedelta(seconds=4.0 * rng.uniform(0.8, 1.2))
    out.append(ev(task, at, "completed", {"ok": True}, len(out) + 1))
    return out


def scene_n() -> list[dict]:
    """N 正常对照：复刻真实 ws-leader 的 task-result 轮询（5.28s 等间隔）。"""
    task = "rt-N-normal-poll"
    out, at = [], T0
    for i in range(12):
        out.append(ev(task, at, "working",
                      {"poll": i, "elapsed_s": round(i * 5.28, 2)}, i + 1))
        at += timedelta(seconds=5.2808)
    out.append(ev(task, at, "completed", {"ok": True}, len(out) + 1))
    return out


# ---------------------------------------------------------------- 消融变体
# 从 C 档（0 分，完全隐身）出发，逐个"退回"一种规避手法，
# 看哪条规则会复活 —— 用来定位每条规则各自值多少分。


def _c_variant(task: str, *, segment: bool = True, jitter: bool = True,
               small: bool = True, monotone: bool = True) -> list[dict]:
    data = secret(4096)
    if not segment:
        # 退回"不切段"：整块 base64（>=80 字符），分 3 次发送
        step = max(1, len(data) // 3)
        parts = [base64.b64encode(data[i * step:(i + 1) * step]).decode()
                 for i in range(3)]
    else:
        parts = b64_chunks(data, size=60)
    task = f"rt-C-{task}"
    rng = random.Random(2026)
    out, at = [], T0
    for i, seg in enumerate(parts[:12]):
        state = "working" if monotone else ("awaiting-input" if i % 2 else "working")
        out.append(ev(task, at, state, {"part": i, "seg": seg}, i + 1))
        step = 4.0 * rng.uniform(0.8, 1.2) if jitter else 4.0
        at += timedelta(seconds=step)
    out.append(ev(task, at, "completed", {"ok": True}, len(out) + 1))
    return out


def scene_c1() -> list[dict]:
    return _c_variant("no-segment", segment=False)   # 退回：base64 保持长串


def scene_c2() -> list[dict]:
    return _c_variant("no-jitter", jitter=False)     # 退回：恢复等间隔


def scene_c3() -> list[dict]:
    """C3 退回：单片载荷 >64KB。

    用带标点的日志文本填充（非 base64、低熵），单独验证 oversized 这条规则。
    """
    task = "rt-C-no-fragment"
    filler = ("[2026-10-29T00:00:00Z] poll result ok, elapsed 5.28s, "
              "no change observed; ") * 1300
    rng = random.Random(2026)
    out, at = [], T0
    for i in range(6):
        out.append(ev(task, at, "working",
                      {"part": i, "log": filler[:66000]}, i + 1))
        at += timedelta(seconds=4.0 * rng.uniform(0.8, 1.2))
    out.append(ev(task, at, "completed", {"ok": True}, len(out) + 1))
    return out


def scene_c4() -> list[dict]:
    return _c_variant("no-monotone", monotone=False)  # 退回：加入 awaiting-input 往返


ABLATION = [
    ("C1", "消融：不切段（长 base64）", scene_c1),
    ("C2", "消融：不加抖动（等间隔）", scene_c2),
    ("C3", "消融：不分片（>64KB 单片）", scene_c3),
    ("C4", "消融：状态往返（awaiting→working）", scene_c4),
]

SCENES = [
    ("A", "朴素外传：base64 高熵分片", scene_a),
    ("B", "时序调制：0/1 交替节拍", scene_b),
    ("C", "规避版：切段+抖动+分片+单调", scene_c),
    ("N", "正常对照：业务轮询（等间隔）", scene_n),
]


# ---------------------------------------------------------------- 执行与报告


def run_scene(tag: str, desc: str, factory) -> dict:
    events = factory()
    path = OUT / f"{tag.lower()}-{tag}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for item in events:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")

    loaded = G.load_jsonl(path)
    reports = G.analyze_events(loaded)
    rep = reports[0] if reports else None
    return {
        "tag": tag,
        "desc": desc,
        "events": len(loaded),
        "jsonl": str(path.relative_to(ROOT)),
        "score": rep.score if rep else 0,
        "level": rep.level if rep else "N/A",
        "findings": [{"channel": f.channel, "rule": f.rule,
                      "score": f.score, "detail": f.detail}
                     for f in (rep.findings if rep else [])],
    }


def run_live() -> list[dict]:
    """回放本机真实任务库，作为真实流量基线。"""
    rows = []
    for p in sorted(glob.glob(
            str(ROOT.parent / "my-project" / "acps_cluster" / "ws-*"
                / "acps" / "aip-tasks.sqlite3"))):
        try:
            loaded = G.load_sqlite(p, limit=100000)
        except Exception:
            continue
        reports = G.analyze_events(loaded)
        rows.append({
            "workspace": os.path.basename(Path(p).parent.parent.name),
            "events": len(loaded),
            "tasks": len(reports),
            "high": sum(1 for r in reports if r.level == "HIGH"),
            "medium": sum(1 for r in reports if r.level == "MEDIUM"),
            "with_finding": sum(1 for r in reports if r.findings),
            "max_score": max((r.score for r in reports), default=0),
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="额外回放本机真实任务库")
    ap.add_argument("--ablation", action="store_true", help="只跑消融组")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    results = [run_scene(t, d, f) for t, d, f in SCENES]
    ablation = [run_scene(t, d, f) for t, d, f in ABLATION]
    live = run_live() if args.live else []

    if args.ablation:
        print("=" * 78)
        print("消融：从 C 档（0 分）逐个退回一种规避手法，看哪条规则复活")
        print("=" * 78)
        print(f"{'档':<3}{'说明':<34}{'事件':>5}{'得分':>6}  {'级别':<8}复活规则")
        print("-" * 78)
        for r in ablation:
            rules = ",".join(f["rule"] for f in r["findings"]) or "（无）"
            print(f"{r['tag']:<3}{r['desc']:<34}{r['events']:>5}{r['score']:>6}  "
                  f"{r['level']:<8}{rules}")
        print("=" * 78)
        return 0

    if args.json:
        print(json.dumps({"scenes": results, "live": live},
                         ensure_ascii=False, indent=2))
        return 0

    print("=" * 78)
    print("guard-api 红队注入实验  2026-10-29")
    print("入口：guard_api.covert_event_guard.load_jsonl -> analyze_events（未改检测代码）")
    print("=" * 78)
    print(f"{'档':<3}{'说明':<34}{'事件':>5}{'得分':>6}  {'级别':<8}命中规则")
    print("-" * 78)
    for r in results:
        rules = ",".join(f["rule"] for f in r["findings"]) or "-"
        print(f"{r['tag']:<3}{r['desc']:<34}{r['events']:>5}{r['score']:>6}  "
              f"{r['level']:<8}{rules}")
    print("-" * 78)
    for r in results:
        for f in r["findings"]:
            print(f"  [{r['tag']}] {f['channel']}/{f['rule']} +{f['score']}  {f['detail']}")

    if live:
        print()
        print("真实流量基线（本机 workspace 任务库回放）")
        print("-" * 78)
        te = sum(r["events"] for r in live)
        tt = sum(r["tasks"] for r in live)
        tm = sum(r["medium"] for r in live)
        th = sum(r["high"] for r in live)
        print(f"  合计 {te} 事件 / {tt} 任务：HIGH {th} · MEDIUM {tm} · "
              f"最高分 {max(r['max_score'] for r in live)}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
