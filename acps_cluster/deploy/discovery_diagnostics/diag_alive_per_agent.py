"""判断 aliveMap 覆盖不全是"没上报"还是"查询取数"问题。

思路：
- aliveMap 是按**结果行**注入的（spec §4.2.3：以结果里的 AIC 为键）→ 结果集不含某 agent 时就不会有它；
- 所以用 `filtered + extFilter.alive=true`（只返回存活者）逐个试，能区分两种情况：
  · 能查出来 → 平台确实认为它在线；
  · 查不出 → 平台不认为它在线（才是真没上报）。
"""

from __future__ import annotations

import json
import os
import ssl
from pathlib import Path

import httpx

CLUSTER = Path(os.getenv("YANSHU_CLUSTER_DIR", "/opt/wit/my-project/acps_cluster"))
ACPS = CLUSTER / "ws-scout" / "acps"
D = "https://wt.ioa.pub/discovery/acps-adp-v2/discover"

AICS = {
    "Scout": "1.2.156.3088.1.BUPT.VTF97R.GMT5S6.4SZM68.0LC5",
    "Supervisor": "1.2.156.3088.1.BUPT.VTF97R.JXN8O8.98DOO8.0EB6",
    "Synthesis": "1.2.156.3088.1.BUPT.VTF97R.YDWSUJ.RBUQ6T.12KC",
    "Writer": "1.2.156.3088.1.BUPT.VTF97R.WATDMX.AQFZR5.0NQN",
    "Critic": "1.2.156.3088.1.BUPT.VTF97R.71P6ML.DDF7EG.13B2",
    "Contradiction": "1.2.156.3088.1.BUPT.VTF97R.0FQNAG.ZO888M.0FW5",
    "Frontier": "1.2.156.3088.1.BUPT.VTF97R.ZAJ69L.IOEX2R.082D",
    "Experiment": "1.2.156.3088.1.BUPT.VTF97R.5CJ349.JNZ5VU.0E5E",
}


def ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cafile=str(ACPS / "trust-bundle.pem"))
    ctx.load_cert_chain(certfile=str(ACPS / "client" / "certificate.pem"),
                        keyfile=str(ACPS / "client" / "private-key.pem"))
    return ctx


ctx = ssl_ctx()

with httpx.Client(verify=ctx, timeout=40.0) as c:
    print("逐个按 AIC 查（首页在线查询的原样请求）")
    print(f"  {'agent':14s} {'结果条数':8s} {'aliveMap':10s} 判定")
    for name, aic in AICS.items():
        r = c.post(D, json={"type": "filtered", "limit": 5,
                            "filter": {"conditions": [{"field": "aic", "op": "eq", "value": aic}]}})
        result = (r.json() or {}).get("result") or {}
        n = len(result.get("acsMap") or {})
        am = result.get("aliveMap") or {}
        info = am.get(aic)
        verdict = ("✔ 平台认为在线" if n > 0 else "✘ 平台不认为在线")
        print(f"  {name:14s} {n:<8d} {str(bool(info)):10s} {verdict}")

    print()
    print("用 extFilter.alive=true 拉取（只返回存活者，看我们谁在里面）")
    for limit in (50, 200):
        r = c.post(D, json={"type": "filtered", "limit": limit, "extFilter": {"alive": True}})
        if r.status_code != 200:
            print(f"  limit={limit}: HTTP {r.status_code} {r.text[:120]}")
            continue
        result = (r.json() or {}).get("result") or {}
        am = result.get("aliveMap") or {}
        acs_map = result.get("acsMap") or {}
        ours = {name: (aic in am or aic in acs_map) for name, aic in AICS.items()}
        print(f"  limit={limit}: 返回 {len(acs_map)} 条 / aliveMap {len(am)} 条")
        print(f"      我们命中：{ {k: ('✔' if v else '✘') for k, v in ours.items()} }")
