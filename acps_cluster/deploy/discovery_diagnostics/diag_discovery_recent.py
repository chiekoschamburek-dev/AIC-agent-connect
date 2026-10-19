"""平台发现诊断 5（服务器侧）：trending/recent 语义与"排名/新鲜度"关系。

只读。
"""

from __future__ import annotations

import json
import os
import ssl
from pathlib import Path

import httpx

CLUSTER = Path(os.getenv("YANSHU_CLUSTER_DIR", "/opt/wit/my-project/acps_cluster"))
ACPS = CLUSTER / "ws-scout" / "acps"
URL = "https://wt.ioa.pub/discovery/acps-adp-v2/discover"


def ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cafile=str(ACPS / "trust-bundle.pem"))
    ctx.load_cert_chain(certfile=str(ACPS / "client" / "certificate.pem"),
                        keyfile=str(ACPS / "client" / "private-key.pem"))
    return ctx


OURS: dict[str, str] = {}
for d in sorted(CLUSTER.glob("ws-*")):
    f = d / "acps" / "acs.final.json"
    if f.is_file():
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
            if j.get("aic"):
                OURS[j["aic"]] = j.get("name")
        except Exception:  # noqa: BLE001
            pass


def rows(result: dict) -> list[tuple[str, str]]:
    acs_map = result.get("acsMap") or {}
    out: list[tuple[str, str]] = []

    def walk(node):
        if isinstance(node, dict):
            a = node.get("aic")
            if isinstance(a, str):
                nm = (acs_map.get(a) or {}).get("name") or node.get("name") or ""
                out.append((a, nm))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(result)
    seen, uniq = set(), []
    for a, nm in out:
        if a not in seen:
            seen.add(a)
            uniq.append((a, nm))
    return uniq


def probe(c, tag, body):
    r = c.post(URL, json=body)
    if r.status_code != 200:
        print(f"\n##### {tag} -> HTTP {r.status_code} {r.text[:200]}")
        return
    result = (r.json() or {}).get("result") or {}
    rs = rows(result)
    ours = [i for i, (a, _) in enumerate(rs, 1) if a in OURS]
    print(f"\n##### {tag} -> 共 {len(rs)} 员；我们命中 {len(ours)} 个 {ours}")
    for i, (a, nm) in enumerate(rs, 1):
        print(f"  {i:2d}{'★' if a in OURS else ' '} {nm}")
    am = result.get("aliveMap")
    if isinstance(am, dict):
        cov = sum(1 for a, _ in rs if a in am)
        print(f"  aliveMap={len(am)} 条；本次结果中被覆盖 {cov}/{len(rs)}；我们被覆盖 "
              f"{sum(1 for a, _ in rs if a in OURS and a in am)}/{len(ours)}")
    print(f"  result 顶层键={sorted(result.keys())}")


with httpx.Client(verify=ssl_ctx(), timeout=40.0) as c:
    probe(c, "type=recent", {"type": "recent", "limit": 20})
    probe(c, "type=trending", {"type": "trending", "limit": 20})
    probe(c, "type=trending limit=50", {"type": "trending", "limit": 50})
    probe(c, "type=recent limit=50", {"type": "recent", "limit": 50})
    probe(c, "type=trending + extFilter alive=true", {"type": "trending", "limit": 50, "extFilter": {"alive": True}})
    probe(c, "type=recent + extFilter alive=true", {"type": "recent", "limit": 50, "extFilter": {"alive": True}})
    probe(c, "type=exploratory 空 query", {"type": "exploratory", "limit": 20})
