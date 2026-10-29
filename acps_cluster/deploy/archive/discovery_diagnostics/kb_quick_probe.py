#!/usr/bin/env python3
"""针对性快测：回答"叮当里为什么找不到 KB"。

只测少量关键查询，每个独立超时，避免整体卡住。
"""

from __future__ import annotations

import ssl
from pathlib import Path

import httpx

CL = Path("/opt/wit/my-project/acps_cluster")
KB = "1.2.156.3088.1.BUPT.VTF97R.GIKJNY.9AL6AY.10UD"
URL = "https://wt.ioa.pub/discovery/acps-adp-v2/discover"
acps = CL / "ws-supervisor" / "acps"
ctx = ssl.create_default_context()
ctx.load_verify_locations(cafile=str(acps / "trust-bundle.pem"))
ctx.load_cert_chain(certfile=str(acps / "client" / "certificate.pem"),
                    keyfile=str(acps / "client" / "private-key.pem"))

CASES = ["知识库", "KB", "研枢-KB-Partner", "查文献", "知识库查询"]


def ask(q: str) -> None:
    try:
        with httpx.Client(verify=ctx, timeout=25) as c:
            r = c.post(URL, json={"type": "semantic", "query": q, "limit": 20})
        if r.status_code != 200:
            print(f"  {q!r:18s} → HTTP {r.status_code}")
            return
        res = (r.json() or {}).get("result") or {}
        m = res.get("acsMap") or {}
        if KB in m:
            rank = list(m).index(KB) + 1
            print(f"  {q!r:18s} → ✔ KB 第 {rank}/{len(m)} 位")
        else:
            names = [str((v or {}).get("name"))[:16] for v in m.values()]
            print(f"  {q!r:18s} → ✘ KB 不在（{len(m)} 个）: {names[:5]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  {q!r:18s} → {type(exc).__name__}")


print("=== 关键问法下 KB 是否出现 ===")
for q in CASES:
    ask(q)
