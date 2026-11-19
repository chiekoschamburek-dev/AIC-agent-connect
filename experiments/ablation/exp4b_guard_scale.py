"""实验④b Guard 规模化评估——用样本分布替代单一案例。

动机：原先只有 1 个正常样本(N) + 3 个攻击样本(A/B/C)，91.8% 止损只是单案例。
本脚本把样本扩到 24 个正常任务 + 16 个异常任务，统计：
  - 正常样本的 LOW/MEDIUM/HIGH 分布与误报率（哪些真实业务模式会被误伤）
  - 异常样本的检出/漏报分布、按 level/score 分桶、各规则命中频次
  - 止损在多种卡死规则下的收益分布（不只是 repeat-loop 一个场景）

沿用 guard-api 红队的同一检测入口（guard_api/covert_event_guard.py），不改检测代码。
全部程序化生成、固定随机种子、可复现。输出 out/exp4b_guard_scale.json。
"""

from __future__ import annotations

import importlib.util
import json
import os
import random
import statistics
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
CLUSTER = ROOT / "acps_cluster"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(CLUSTER))
os.environ.setdefault("OPENAI_API_KEY", "sk-test")

# 与红队脚本完全一致的检测入口
spec = importlib.util.spec_from_file_location(
    "ceg", str(ROOT / "guard-api" / "guard_api" / "covert_event_guard.py"))
G = importlib.util.module_from_spec(spec)
sys.modules["ceg"] = G
spec.loader.exec_module(G)

T0 = datetime(2026, 10, 29, 0, 0, 0, tzinfo=timezone.utc)
RNG = random.Random(20261117)


def ev(task, at, state, result, seq, etype="task-result"):
    return {
        "task_ref": task, "created_at": at.isoformat().replace("+00:00", "Z"),
        "event_type": etype, "event_seq": seq, "message_id": f"msg-{task}-{seq:04d}",
        "event_data": {"type": etype, "status": {"state": state}, "result": result},
    }


def analyze(events: list[dict]) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
        p = fh.name
    loaded = G.load_jsonl(Path(p))
    rep = G.analyze_events(loaded)
    os.unlink(p)
    r = rep[0] if rep else None
    return {
        "score": r.score if r else 0,
        "level": r.level if r else "N/A",
        "rules": [f.rule for f in (r.findings if r else [])],
        "channels": sorted({f.channel for f in (r.findings if r else [])}),
    }


# ═══════════════ 正常任务生成器（贴近真实业务模式）═══════════════

def normal_polling_jitter(task, base=5.0, jitter=0.35, n=10, seed=1):
    """真实轮询：基础间隔 + 显著抖动（CV 远超 0.08，不应命中 timing）。"""
    rng = random.Random(seed)
    out, at = [], T0
    for i in range(n):
        out.append(ev(task, at, "working", {"poll": i, "progress": f"{i}/{n}"}, i + 1))
        at += timedelta(seconds=base * (1 + rng.uniform(-jitter, jitter)))
    out.append(ev(task, at, "completed", {"ok": True}, len(out) + 1))
    return out


def normal_burst(task, seed=2):
    """突发批量：连续快速调用后静止（真实 fan-out），间隔方差极大。"""
    rng = random.Random(seed)
    out, at = [], T0
    for i in range(8):
        out.append(ev(task, at, "working", {"item": i, "val": rng.randint(1, 999)}, i + 1))
        at += timedelta(seconds=rng.uniform(0.2, 1.5))
    at += timedelta(seconds=rng.uniform(30, 120))
    for i in range(4):
        out.append(ev(task, at, "working", {"item": i + 8}, len(out) + 1))
        at += timedelta(seconds=rng.uniform(2, 20))
    out.append(ev(task, at, "completed", {"ok": True}, len(out) + 1))
    return out


def normal_state_flow(task, seed=3):
    """真实业务合法往返：working→awaiting-input（等人补料）→working→completed。

    awaiting-input→working 是业务常态（用户补充输入后继续），但检测器的
    ORDER 把 awaiting-input(2)→working(1) 当作状态倒退 → invalid_state_transition。
    这是"合法往返被误判"的真实误报来源，保留用于量化误报分布。
    """
    rng = random.Random(seed)
    states = ["accepted", "working", "awaiting-input", "working",
              "awaiting-input", "working", "completed"]
    out, at = [], T0
    for i, s in enumerate(states):
        out.append(ev(task, at, s, {"step": i, "note": "正常流程·等待补料后继续"}, i + 1))
        at += timedelta(seconds=rng.uniform(2, 15))
    return out


def normal_short(task, seed=4):
    """短任务：事件数 < 6，timing 规则不触发（intervals<5）。"""
    rng = random.Random(seed)
    out, at = [], T0
    for i in range(3):
        out.append(ev(task, at, "working", {"step": i}, i + 1))
        at += timedelta(seconds=rng.uniform(1, 30))
    out.append(ev(task, at, "completed", {"ok": True}, len(out) + 1))
    return out


def normal_regular_poll(task, seed=5):
    """刻意等间隔轮询——这是已知误报源（over_regular_intervals）。单独归类。"""
    out, at = [], T0
    for i in range(12):
        out.append(ev(task, at, "working", {"poll": i, "elapsed": i * 5.28}, i + 1))
        at += timedelta(seconds=5.2808)
    out.append(ev(task, at, "completed", {"ok": True}, len(out) + 1))
    return out


NORMAL = []
for i in range(6):
    NORMAL.append((f"N-jitter-{i}", f"正常·抖动轮询#{i}", normal_polling_jitter(f"N-jitter-{i}", seed=10 + i)))
for i in range(4):
    NORMAL.append((f"N-burst-{i}", f"正常·突发批量#{i}", normal_burst(f"N-burst-{i}", seed=20 + i)))
for i in range(3):
    NORMAL.append((f"N-flow-{i}", f"正常·状态流转#{i}", normal_state_flow(f"N-flow-{i}", seed=30 + i)))
NORMAL.append(("N-short", "正常·短任务", normal_short("N-short", seed=40)))
for i in range(3):
    NORMAL.append((f"N-regular-{i}", f"正常·等间隔轮询#{i}", normal_regular_poll(f"N-regular-{i}", seed=50 + i)))
# 共 17 个正常样本

# ═══════════════ 异常任务生成器（覆盖三通道多变体）═══════════════

import base64


def secret(n, seed):
    return random.Random(seed).randbytes(n)


def exfil_blob(task, seed=1, chunks=5):
    """storage: 高熵 base64 外传。"""
    data = secret(4096, seed)
    out, at = [], T0
    for i in range(chunks):
        c = data[i * 800:(i + 1) * 800]
        out.append(ev(task, at, "working", {"blob": base64.b64encode(c).decode(), "p": i}, i + 1))
        at += timedelta(seconds=random.Random(seed + i).uniform(2, 9))
    out.append(ev(task, at, "completed", {"ok": True}, len(out) + 1))
    return out


def exfil_oversize(task, seed=2, size=70000):
    """storage: 超大载荷。"""
    out, at = [], T0
    for i in range(4):
        out.append(ev(task, at, "working", {"log": "x" * size, "p": i}, i + 1))
        at += timedelta(seconds=random.Random(seed + i).uniform(2, 9))
    out.append(ev(task, at, "completed", {"ok": True}, len(out) + 1))
    return out


def exfil_repeat(task, seed=3):
    """storage: 重复载荷。"""
    out, at = [], T0
    for i in range(8):
        out.append(ev(task, at, "working", {"same": "YmFzZTY0IG9mIHJlcGVhdGVkIGRhdGEg" * 5}, i + 1))
        at += timedelta(seconds=random.Random(seed + i).uniform(2, 9))
    out.append(ev(task, at, "completed", {"ok": True}, len(out) + 1))
    return out


def timing_alternating(task, seed=4):
    """timing: 0/1 交替节拍。"""
    out, at = [], T0
    for i in range(10):
        out.append(ev(task, at, "working", {"p": i, "hex": secret(2048, seed).hex()[:40]}, i + 1))
        at += timedelta(seconds=2.0 if i % 2 == 0 else 6.0)
    out.append(ev(task, at, "completed", {"ok": True}, len(out) + 1))
    return out


def timing_regular(task, seed=5):
    """timing: 严格等间隔（作为隐蔽信道载体，但与正常等间隔轮询同形）。"""
    out, at = [], T0
    for i in range(12):
        out.append(ev(task, at, "working", {"p": i, "v": secret(256, seed).hex()[:20]}, i + 1))
        at += timedelta(seconds=4.0)
    out.append(ev(task, at, "completed", {"ok": True}, len(out) + 1))
    return out


def behavior_state(task, seed=6):
    """behavior: 非法状态倒退。"""
    out, at = [], T0
    states = ["accepted", "working", "completed", "working", "completed", "working"]
    for i, s in enumerate(states):
        out.append(ev(task, at, s, {"p": i}, i + 1))
        at += timedelta(seconds=random.Random(seed + i).uniform(2, 9))
    out.append(ev(task, at, "completed", {"ok": True}, len(out) + 1))
    return out


def behavior_switch(task, seed=7):
    """behavior: 事件类型高频切换。"""
    out, at = [], T0
    types = ["task-result", "task-status", "task-event"]
    for i in range(14):
        out.append(ev(task, at, "working", {"p": i}, i + 1, etype=types[i % 3]))
        at += timedelta(seconds=random.Random(seed + i).uniform(2, 9))
    out.append(ev(task, at, "completed", {"ok": True}, len(out) + 1, etype="task-result"))
    return out


def combined(task, seed=8):
    """组合攻击：高熵 + 交替节拍 + 状态倒退。"""
    data = secret(4096, seed)
    out, at = [], T0
    states = ["working", "working", "completed", "working", "working"]
    for i in range(8):
        c = data[i * 500:(i + 1) * 500]
        out.append(ev(task, at, states[i % len(states)], {"blob": base64.b64encode(c).decode(), "p": i}, i + 1))
        at += timedelta(seconds=2.0 if i % 2 == 0 else 6.0)
    out.append(ev(task, at, "completed", {"ok": True}, len(out) + 1))
    return out


def evasive(task, seed=9):
    """规避版：切段(<80) + 抖动 + 单调状态，绕开三维度（预期漏报）。"""
    data = secret(4096, seed)
    whole = base64.b64encode(data).decode()
    chunks = [whole[i:i + 60] for i in range(0, len(whole), 60)]
    out, at = [], T0
    rng = random.Random(2026)
    for i, seg in enumerate(chunks[:12]):
        out.append(ev(task, at, "working", {"p": i, "seg": seg}, i + 1))
        at += timedelta(seconds=4.0 * rng.uniform(0.8, 1.2))
    out.append(ev(task, at, "completed", {"ok": True}, len(out) + 1))
    return out


ANOMALOUS = [
    ("A-blob-0", "异常·高熵外传#0", exfil_blob("A-blob-0", seed=1)),
    ("A-blob-1", "异常·高熵外传#1", exfil_blob("A-blob-1", seed=11, chunks=7)),
    ("A-over-0", "异常·超大载荷#0", exfil_oversize("A-over-0", seed=2)),
    ("A-repeat-0", "异常·重复载荷#0", exfil_repeat("A-repeat-0", seed=3)),
    ("T-alt-0", "异常·交替节拍#0", timing_alternating("T-alt-0", seed=4)),
    ("T-alt-1", "异常·交替节拍#1", timing_alternating("T-alt-1", seed=41)),
    ("T-reg-0", "异常·严格等间隔载体#0", timing_regular("T-reg-0", seed=5)),
    ("B-state-0", "异常·状态倒退#0", behavior_state("B-state-0", seed=6)),
    ("B-state-1", "异常·状态倒退#1", behavior_state("B-state-1", seed=61)),
    ("B-switch-0", "异常·高频切换#0", behavior_switch("B-switch-0", seed=7)),
    ("C-combo-0", "异常·组合攻击#0", combined("C-combo-0", seed=8)),
    ("C-combo-1", "异常·组合攻击#1", combined("C-combo-1", seed=81)),
    ("E-evasive-0", "异常·规避版#0", evasive("E-evasive-0", seed=9)),
    ("E-evasive-1", "异常·规避版#1", evasive("E-evasive-1", seed=91)),
    ("E-evasive-2", "异常·规避版#2", evasive("E-evasive-2", seed=92)),
    ("A-mixed-0", "异常·高熵+状态倒退", exfil_blob("A-mixed-0", seed=15, chunks=4)),
]
# 16 个异常样本

# ═══════════════ 执行检测 ═══════════════

def run_set(samples, truth):
    rows = []
    for tag, desc, events in samples:
        r = analyze(events)
        r.update({"tag": tag, "desc": desc, "truth": truth})
        rows.append(r)
    return rows


normal_rows = run_set(NORMAL, "normal")
anom_rows = run_set(ANOMALOUS, "anomalous")


def level_dist(rows):
    d = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for r in rows:
        d[r["level"]] = d.get(r["level"], 0) + 1
    return d


# 误报定义：正常样本得分 > 0（任何 finding 都算误报；LOW 级也算）
fp = [r for r in normal_rows if r["score"] > 0]
fp_low = [r for r in normal_rows if r["score"] > 0 and r["level"] == "LOW"]
fp_medium_plus = [r for r in normal_rows if r["level"] in ("MEDIUM", "HIGH")]
# 检出定义：异常样本得分 > 0；漏报 = 0 分
tp = [r for r in anom_rows if r["score"] > 0]
fn = [r for r in anom_rows if r["score"] == 0]
# 有意义检出（MEDIUM+）
tp_meaningful = [r for r in anom_rows if r["level"] in ("MEDIUM", "HIGH")]

from collections import Counter
rule_freq = Counter(rule for r in anom_rows for rule in r["rules"])
normal_rule_freq = Counter(rule for r in normal_rows for rule in r["rules"])

payload = {
    "experiment": "④b Guard 规模化评估（样本分布替代单点）",
    "detector_entry": "guard_api.covert_event_guard（与红队同一入口，未改检测代码）",
    "seed": 20261117,
    "sample_sizes": {"normal": len(normal_rows), "anomalous": len(anom_rows)},
    "normal": {
        "level_distribution": level_dist(normal_rows),
        "score_distribution": {
            "min": min(r["score"] for r in normal_rows),
            "p50": statistics.median([r["score"] for r in normal_rows]),
            "p95": sorted([r["score"] for r in normal_rows])[int(0.95 * (len(normal_rows) - 1))],
            "max": max(r["score"] for r in normal_rows),
        },
        "false_positive_any": len(fp),
        "false_positive_rate_any_pct": round(len(fp) / len(normal_rows) * 100, 1),
        "false_positive_low": len(fp_low),
        "false_positive_rate_low_pct": round(len(fp_low) / len(normal_rows) * 100, 1),
        "false_positive_medium_plus": len(fp_medium_plus),
        "false_positive_rate_medium_plus_pct": round(len(fp_medium_plus) / len(normal_rows) * 100, 1),
        "rule_frequency": dict(normal_rule_freq),
        "false_positive_by_rule": {
            rule: sum(1 for r in fp if rule in r["rules"]) for rule in normal_rule_freq
        },
        "rows": normal_rows,
    },
    "anomalous": {
        "level_distribution": level_dist(anom_rows),
        "detected_any": len(tp),
        "detection_rate_any_pct": round(len(tp) / len(anom_rows) * 100, 1),
        "missed": len(fn),
        "miss_rate_pct": round(len(fn) / len(anom_rows) * 100, 1),
        "missed_tags": [r["tag"] for r in fn],
        "meaningful_medium_plus": len(tp_meaningful),
        "meaningful_rate_pct": round(len(tp_meaningful) / len(anom_rows) * 100, 1),
        "rule_frequency": dict(rule_freq),
        "rows": anom_rows,
    },
}

# ═══════════════ 止损效果分布（多规则 × 多预算）══════════════
from acps_cluster.agents import supervisor


def scan_case(rule, trigger_at, budget=195):
    """模拟某条规则在 trigger_at 轮触发，对比 observe vs enforce 的调用消耗。"""
    supervisor._RUN_GUARD_ABORT.set(None)
    guard = supervisor.RunGuardHooks()
    st = {"started": time.monotonic(), "last_activity": time.monotonic(),
          "last_progress": time.monotonic(), "progress_states": {},
          "steps": 0, "errors": 0, "repeat": {}, "waits": {}, "abort": None, "submitted": False}
    with mock.patch.object(supervisor.RunGuardHooks, "_audit", lambda *a, **k: None), \
         mock.patch.object(supervisor, "_run_guard_mode", return_value="enforce"):
        supervisor.RunGuardHooks._STATE.set(st)
        hit_at = None
        for i in range(1, budget + 1):
            st["steps"] = i
            if rule == "repeat-loop":
                st["repeat"] = {"call|{}": i}
            elif rule == "error-streak":
                st["errors"] = i
            elif rule == "partner-not-finishing":
                st["waits"] = {"task_1": i}
            with mock.patch.dict(os.environ, {
                "ACPS_GUARD_REPEAT_LIMIT": str(trigger_at),
                "ACPS_GUARD_ERROR_LIMIT": str(trigger_at),
                "ACPS_GUARD_WAIT_LIMIT": str(trigger_at),
            }):
                guard._check(st)
            if st.get("abort"):
                hit_at = i
                break
    supervisor._RUN_GUARD_ABORT.set(None)
    supervisor.RunGuardHooks._STATE.set(None)
    consumed = hit_at if hit_at else budget
    return {"rule": rule, "trigger_at": trigger_at, "budget": budget,
            "enforce_consumed": consumed, "observe_consumed": budget,
            "saved": budget - consumed, "saved_pct": round((budget - consumed) / budget * 100, 1)}


stop_cases = []
for rule in ("repeat-loop", "error-streak", "partner-not-finishing"):
    for trigger in (8, 16, 32):
        stop_cases.append(scan_case(rule, trigger, budget=195))
saved_pcts = [c["saved_pct"] for c in stop_cases]
payload["stop_loss_distribution"] = {
    "scenarios": len(stop_cases),
    "saved_pct_min": min(saved_pcts),
    "saved_pct_p50": statistics.median(saved_pcts),
    "saved_pct_max": max(saved_pcts),
    "cases": stop_cases,
}

out = HERE / "out" / "exp4b_guard_scale.json"
out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

# ═══════════════ 打印 ═══════════════
print("=" * 84)
print("实验④b Guard 规模化评估（样本分布）")
print("=" * 84)
print(f"样本：正常 {len(normal_rows)} 个 / 异常 {len(anom_rows)} 个")
print("-" * 84)
print("【正常样本·误报分布】")
print(f"  level 分布：{payload['normal']['level_distribution']}")
print(f"  score 分布：min={payload['normal']['score_distribution']['min']} "
      f"p50={payload['normal']['score_distribution']['p50']} "
      f"p95={payload['normal']['score_distribution']['p95']} "
      f"max={payload['normal']['score_distribution']['max']}")
print(f"  误报(任意得分>0)：{len(fp)}/{len(normal_rows)} = {payload['normal']['false_positive_rate_any_pct']}%")
print(f"    其中 LOW 级：{len(fp_low)}；MEDIUM+：{len(fp_medium_plus)} "
      f"({payload['normal']['false_positive_rate_medium_plus_pct']}%)")
print(f"  触发规则频次：{dict(normal_rule_freq)}")
print("-" * 84)
print("【异常样本·检出/漏报分布】")
print(f"  level 分布：{payload['anomalous']['level_distribution']}")
print(f"  检出(得分>0)：{len(tp)}/{len(anom_rows)} = {payload['anomalous']['detection_rate_any_pct']}%")
print(f"  漏报(0分)：{len(fn)}/{len(anom_rows)} = {payload['anomalous']['miss_rate_pct']}% → {[r['tag'] for r in fn]}")
print(f"  有意义检出(MEDIUM+)：{len(tp_meaningful)}/{len(anom_rows)} = {payload['anomalous']['meaningful_rate_pct']}%")
print(f"  规则命中频次：{dict(rule_freq)}")
print("-" * 84)
print("【止损收益分布】")
print(f"  {len(stop_cases)} 场景：min={min(saved_pcts)}% p50={statistics.median(saved_pcts)}% max={max(saved_pcts)}%")
for c in stop_cases:
    print(f"    {c['rule']:<24} 触发@{c['trigger_at']:<3} 省 {c['saved']}/{c['budget']} = {c['saved_pct']}%")
print("=" * 84)
print(f"明细：{out}")
