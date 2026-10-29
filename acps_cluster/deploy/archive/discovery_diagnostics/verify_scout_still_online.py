#!/usr/bin/env python3
"""确认 scout 改代码后仍在平台在线（身份未受影响）。"""

from __future__ import annotations

import json
import ssl
import subprocess
from pathlib import Path

import httpx

CL = Path("/opt/wit/my-project/acps_cluster")
SCOUT = "1.2.156.3088.1.BUPT.VTF97R.GMT5S6.4SZM68.0LC5"
KB = "1.2.156.3088.1.BUPT.VTF97R.GIKJNY.9AL6AY.10UD"

acps = CL / "ws-supervisor" / "acps"
ctx = ssl.create_default_context()
ctx.load_verify_locations(cafile=str(acps / "trust-bundle.pem"))
ctx.load_cert_chain(certfile=str(acps / "client" / "certificate.pem"),
                    keyfile=str(acps / "client" / "private-key.pem"))


def discover(aic: str, label: str) -> None:
    with httpx.Client(verify=ctx, timeout=30) as c:
        r = c.post("https://wt.ioa.pub/discovery/acps-adp-v2/discover",
                   json={"type": "filtered", "limit": 5,
                         "filter": {"conditions": [
                             {"field": "aic", "op": "eq", "value": aic}]}})
        res = (r.json() or {}).get("result") or {}
        acs_map = res.get("acsMap") or {}
        alive = bool((res.get("aliveMap") or {}).get(aic))
        node = acs_map.get(aic) or {}
        skills = [s.get("id") for s in (node.get("skills") or [])]
        print(f"  {label}: HTTP {r.status_code} 收录={len(acs_map)} alive={alive}")
        print(f"      name={node.get('name')}  active={node.get('active')}")
        print(f"      skills={skills}")


print("=== 平台在线情况 ===")
discover(SCOUT, "scout")
discover(KB, "kb   ")

print()
print("=== 服务状态 ===")
for n in ("scout", "supervisor", "kb"):
    out = subprocess.run(["systemctl", "is-active", f"wit-yanshu@{n}.service"],
                         capture_output=True, text=True).stdout.strip()
    print(f"  {n:12s} {out}")
