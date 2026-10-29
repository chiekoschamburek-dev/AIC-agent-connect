#!/usr/bin/env python3
"""用**正确的** AIC（从各工作区 ACS 读）重做在线判定。

背景：diag_alive_per_agent.py 里的 AIC 是写死的旧值（Synthesis/Writer/Critic/
Contradiction/Frontier/Experiment 全是过期 AIC），所以它给出的"平台不认为在线"
结论是**基于错误 AIC** 的，不能采信。

本次改为：
  1. 从 ws-<agent>/acps/acs.final.json 读**当前注册的** aic；
  2. 用 filtered + aic eq 查平台是否收录该 AIC；
  3. 再看 aliveMap 是否把它标为在线。
"""

from __future__ import annotations

import json
import ssl
from pathlib import Path

import httpx

CL = Path("/opt/wit/my-project/acps_cluster")
ACPS = CL / "ws-scout" / "acps"
D = "https://wt.ioa.pub/discovery/acps-adp-v2/discover"
AGENTS = ["scout", "supervisor", "critic", "writer", "synthesis",
          "contradiction", "frontier", "experiment"]


def ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cafile=str(ACPS / "trust-bundle.pem"))
    ctx.load_cert_chain(certfile=str(ACPS / "client" / "certificate.pem"),
                        keyfile=str(ACPS / "client" / "private-key.pem"))
    return ctx


def aic_of(ws: str) -> str:
    for name in ("acs.final.json", "acs.draft.json"):
        f = CL / f"ws-{ws}" / "acps" / name
        if f.is_file():
            try:
                a = json.loads(f.read_text(encoding="utf-8")).get("aic")
                if a:
                    return str(a)
            except Exception:
                pass
    return ""


def name_of(ws: str) -> str:
    f = CL / f"ws-{ws}" / "acps" / "acs.final.json"
    if f.is_file():
        try:
            return str(json.loads(f.read_text(encoding="utf-8")).get("name") or "")
        except Exception:
            pass
    return ""


print(f"  {'agent':14s} {'注册 AIC（ACS）':46s} {'收录':6s} {'aliveMap':10s} 判定")
print("  " + "-" * 96)
missing = []
with httpx.Client(verify=ssl_ctx(), timeout=40.0) as c:
    for ws in AGENTS:
        aic = aic_of(ws)
        if not aic:
            print(f"  {ws:14s} 【ACS 里无 aic】")
            missing.append(ws)
            continue
        r = c.post(D, json={"type": "filtered", "limit": 5,
                            "filter": {"conditions": [{"field": "aic", "op": "eq", "value": aic}]}})
        result = (r.json() or {}).get("result") or {}
        n = len(result.get("acsMap") or {})
        am = result.get("aliveMap") or {}
        alive = bool(am.get(aic))
        verdict = "✔ 在线" if (n > 0 and alive) else ("△ 已收录但非在线" if n > 0 else "✘ 平台未收录")
        if not (n > 0 and alive):
            missing.append(ws)
        print(f"  {ws:14s} {aic:46s} {str(n):6s} {str(alive):10s} {verdict}")

print(f"\n  非在线/未收录：{missing or '（无）'}")
