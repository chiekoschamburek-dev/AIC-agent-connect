"""检查 aliveMap 对不同查询的覆盖：我们的 agent 是否被平台认为"存活"。

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


def aics_of(result: dict) -> list[str]:
    out: list[str] = []

    def walk(n):
        if isinstance(n, dict):
            a = n.get("aic")
            if isinstance(a, str):
                out.append(a)
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(result)
    return list(dict.fromkeys(out))


with httpx.Client(verify=ssl_ctx(), timeout=40.0) as c:
    for tag, body in [
        ("explicit arXiv 论文检索", {"type": "explicit", "query": "arXiv 论文检索", "limit": 20}),
        ("explicit 研枢", {"type": "explicit", "query": "研枢", "limit": 20}),
        ("explicit 论文评审", {"type": "explicit", "query": "论文评审", "limit": 20}),
    ]:
        r = c.post(URL, json=body)
        result = (r.json() or {}).get("result") or {}
        am = result.get("aliveMap")
        got = aics_of(result)
        ours = [a for a in got if a in OURS]
        print(f"\n### {tag}: 共{len(got)}员，我们{len(ours)}个")
        if not isinstance(am, dict):
            print("    aliveMap 缺失")
            continue
        true_n = sum(1 for v in am.values() if isinstance(v, dict) and v.get("alive"))
        print(f"    aliveMap {len(am)} 条（alive=true {true_n}）；本次结果被覆盖 "
              f"{sum(1 for a in got if a in am)}/{len(got)}")
        for a in ours:
            print(f"    我们 {OURS[a]}: 条目={'有' if a in am else '【缺失】'} {am.get(a)}")
        missing_all = [a for a in got if a not in am]
        print(f"    结果中未被 aliveMap 覆盖的：{len(missing_all)} 个")
        for a in missing_all[:12]:
            nm = OURS.get(a) or "?"
            print(f"       - {a}  {nm}")
