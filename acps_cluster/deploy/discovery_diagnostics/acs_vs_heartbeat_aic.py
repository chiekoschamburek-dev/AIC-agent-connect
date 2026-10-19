#!/usr/bin/env python3
"""事实收集：ACS 注册的 AIC vs 心跳上报的 AIC（逐工作区对照），并看在线判定是否普遍。

关键区分：
  · ACS 里的 aic  = 平台**注册**的身份（发现/路由用）
  · 心跳里的 aic  = 进程**自称**的身份（在线判定用）
两者不一致 = 平台把"自称身份"判在线，但发现/路由用"注册身份" → 对不上就调不通。
"""

from __future__ import annotations

import json
from pathlib import Path

CL = Path("/opt/wit/my-project/acps_cluster")
AGENTS = ["scout", "supervisor", "critic", "writer", "synthesis",
          "contradiction", "frontier", "experiment"]


def short(aic: str) -> str:
    p = str(aic or "").split(".")
    return ".".join(p[-3:]) if len(p) >= 3 else str(aic or "")


def acs_aic(ws: str) -> str:
    for name in ("acs.final.json", "acs.draft.json"):
        f = CL / f"ws-{ws}" / "acps" / name
        if f.is_file():
            try:
                return str(json.loads(f.read_text(encoding="utf-8")).get("aic") or "")
            except Exception:
                pass
    return ""


def enroll_aic(ws: str) -> str:
    f = CL / f"ws-{ws}" / "acps" / "enrollment.json"
    if not f.is_file():
        return ""
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        return str((d.get("ref") or {}).get("aic") or "")
    except Exception:
        return ""


def hb_aic(ws: str) -> str:
    d = CL / f"ws-{ws}" / "logs"
    for f in sorted(d.glob("amp_heartbeat_*.jsonl")) if d.is_dir() else []:
        lines = [x for x in f.read_text(encoding="utf-8", errors="ignore").splitlines() if x.strip()]
        for ln in reversed(lines[-10:]):
            try:
                a = json.loads(ln).get("aic")
                if a:
                    return str(a)
            except Exception:
                continue
    return ""


print(f"  {'工作区':14s} {'ACS(注册)':24s} {'enrollment':24s} {'心跳(自称)':24s} 一致?")
print("  " + "-" * 96)
mismatch = []
for ws in AGENTS:
    a, e, h = short(acs_aic(ws)), short(enroll_aic(ws)), short(hb_aic(ws))
    same = a and a == h
    if not same:
        mismatch.append(ws)
    print(f"  {ws:14s} {a or '—':24s} {e or '—':24s} {h or '—':24s} {'✔' if same else '✘'}")
print(f"\n  ACS 与心跳不一致的工作区：{mismatch or '（无）'}")
