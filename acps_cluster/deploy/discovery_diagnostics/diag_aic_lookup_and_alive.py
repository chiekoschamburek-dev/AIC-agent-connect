"""两个确定结论的实测：

A. 平台首页"按 AIC 查在线"发出的那个请求（type=filtered + filter.conditions[field=aic]）到底返回什么
B. 我们所有 AIC 在平台 alive 视图里是否被覆盖（aliveMap / 已知存活集合）
"""

from __future__ import annotations

import json
import os
import ssl
from pathlib import Path

import httpx

CLUSTER = Path(os.getenv("YANSHU_CLUSTER_DIR", "/opt/wit/my-project/acps_cluster"))
ACPS = CLUSTER / "ws-scout" / "acps"
DISCOVER = "https://wt.ioa.pub/discovery/acps-adp-v2/discover"

AICS = {
    "Scout 旧(在跑)": "1.2.156.3088.1.BUPT.VTF97R.C6HTRB.5TNX0M.17SZ",
    "Scout 新(注册)": "1.2.156.3088.1.BUPT.VTF97R.GMT5S6.4SZM68.0LC5",
    "Supervisor 旧": "1.2.156.3088.1.BUPT.VTF97R.GGWW9N.KYWRB7.0IC8",
    "Supervisor 新(注册)": "1.2.156.3088.1.BUPT.VTF97R.JXN8O8.98DOO8.0EB6",
}


def ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cafile=str(ACPS / "trust-bundle.pem"))
    ctx.load_cert_chain(certfile=str(ACPS / "client" / "certificate.pem"),
                        keyfile=str(ACPS / "client" / "private-key.pem"))
    return ctx


def acs_names(result: dict) -> list[tuple[str, str]]:
    out = []
    for aic, acs in (result.get("acsMap") or {}).items():
        out.append((aic, acs.get("name") or ""))
    return out


with httpx.Client(verify=ssl_ctx(), timeout=40.0) as c:
    print("=" * 74)
    print("A. 平台首页『按 AIC 查在线』的真实请求（原样复刻前端的 body）")
    for tag, aic in AICS.items():
        body = {"type": "filtered", "limit": 5,
                "filter": {"conditions": [{"field": "aic", "op": "eq", "value": aic}]}}
        r = c.post(DISCOVER, json=body)
        print(f"\n  {tag:22s} HTTP {r.status_code}")
        print(f"      请求体 {json.dumps(body, ensure_ascii=False)[:120]}…")
        print(f"      响应   {r.text[:260]}")

    print()
    print("=" * 74)
    print("B. 我们的 AIC 是否出现在平台的 alive 视图里")
    # aliveMap 只在 recent/trending 响应里注入；把两种都试一遍，再看覆盖
    for tag, body in [("trending limit=50", {"type": "trending", "limit": 50}),
                      ("recent limit=50", {"type": "recent", "limit": 50}),
                      ("trending + extFilter alive=true", {"type": "trending", "limit": 50,
                                                           "extFilter": {"alive": True}})]:
        r = c.post(DISCOVER, json=body)
        result = (r.json() or {}).get("result") or {}
        am = result.get("aliveMap") or {}
        covering = {k: AICS[k] for k in AICS if AICS[k] in am}
        print(f"\n  --- {tag}: HTTP {r.status_code}，aliveMap {len(am)} 条")
        print(f"      覆盖我们的：{covering or '【0 条 —— 平台完全不知道我们】'}")

    print()
    print("=" * 74)
    print("C. 用平台自己的发现结果反查：这些 AIC 在发现索引里的状态")
    for tag, aic in AICS.items():
        r = c.post(DISCOVER, json={"type": "explicit", "query": aic, "limit": 20})
        result = (r.json() or {}).get("result") or {}
        rows = acs_names(result)
        where = next((f"第 {i} 位" for i, (a, _) in enumerate(rows, 1) if a == aic), "未出现")
        print(f"  {tag:22s} 索引中位置={where:8s} 本次共 {len(rows)} 条结果")
