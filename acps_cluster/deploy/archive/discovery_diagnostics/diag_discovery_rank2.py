"""平台发现诊断 4（服务器侧）：分阶段定位发现排名与在线过滤。

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


def flat(result: dict) -> list[tuple[str, str]]:
    """返回 [(aic, name)]，顺序即响应顺序。"""
    out: list[tuple[str, str]] = []

    def walk(node, ctx_name=None):
        if isinstance(node, dict):
            nm = node.get("name") or ctx_name
            a = node.get("aic")
            if isinstance(a, str):
                out.append((a, str(nm or "")))
            for k, v in node.items():
                walk(v, nm if k in ("agents", "skills") else ctx_name)
        elif isinstance(node, list):
            for v in node:
                walk(v, ctx_name)

    walk(result)
    seen, uniq = set(), []
    for a, nm in out:
        if a not in seen:
            seen.add(a)
            uniq.append((a, nm))
    return uniq


RUNS = [
    ("基准：arXiv 论文检索（无 extFilter）", {"type": "explicit", "query": "arXiv 论文检索", "limit": 20}),
    ("加 extFilter.alive=true", {"type": "explicit", "query": "arXiv 论文检索", "limit": 20, "extFilter": {"alive": True}}),
    ("加 extFilter.alive=false", {"type": "explicit", "query": "arXiv 论文检索", "limit": 20, "extFilter": {"alive": False}}),
    ("基准：论文评审（无 extFilter）", {"type": "explicit", "query": "论文评审", "limit": 20}),
    ("论文评审 + alive=true", {"type": "explicit", "query": "论文评审", "limit": 20, "extFilter": {"alive": True}}),
    ("基准：文献检索（无 extFilter）", {"type": "explicit", "query": "文献检索", "limit": 20}),
    ("文献检索 + alive=true", {"type": "explicit", "query": "文献检索", "limit": 20, "extFilter": {"alive": True}}),
]

with httpx.Client(verify=ssl_ctx(), timeout=40.0) as c:
    for tag, body in RUNS:
        r = c.post(URL, json=body)
        if r.status_code != 200:
            print(f"\n##### {tag}\n  HTTP {r.status_code}: {r.text[:200]}")
            continue
        result = (r.json() or {}).get("result") or {}
        rows = flat(result)
        print(f"\n##### {tag}  -> 共 {len(rows)} 员")
        for i, (a, nm) in enumerate(rows, 1):
            mark = "★" if a in OURS else " "
            print(f"  {i:2d}{mark} {nm}")
        ours = [i for i, (a, _) in enumerate(rows, 1) if a in OURS]
        print(f"  >>> 我们在其中：{len(ours)} 个，排名 {ours}")
