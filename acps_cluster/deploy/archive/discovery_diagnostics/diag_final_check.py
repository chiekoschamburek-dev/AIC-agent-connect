"""最终复核：模拟平台首页/叮当的两种取数，确认我们的 agent 是否出现。

1) 首页"最新上架 / 热门智能体"面板：type=recent / trending, limit=6（与前端 DISCOVERY_REQUESTS 一致）
2) 叮当典型语义搜索：limit=15，看我们的排名
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

    def walk(n):
        if isinstance(n, dict):
            a = n.get("aic")
            if isinstance(a, str):
                out.append((a, (acs_map.get(a) or {}).get("name") or n.get("name") or ""))
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(result)
    seen, uniq = set(), []
    for a, nm in out:
        if a not in seen:
            seen.add(a)
            uniq.append((a, nm))
    return uniq


with httpx.Client(verify=ssl_ctx(), timeout=40.0) as c:
    print("########## 一、平台首页面板（前端真实参数 limit=6）")
    for tag, body in [("最新上架 recent", {"type": "recent", "limit": 6}),
                      ("热门智能体 trending", {"type": "trending", "limit": 6})]:
        r = c.post(URL, json=body)
        result = (r.json() or {}).get("result") or {}
        rs = rows(result)
        ours = [nm for a, nm in rs if a in OURS]
        print(f"\n--- {tag}: 共 {len(rs)} 张卡片；我们在其中：{ours or '【0 张】'}")
        for i, (a, nm) in enumerate(rs, 1):
            print(f"    {i}. {'★' if a in OURS else ' '} {nm}")

    print("\n\n########## 二、叮当典型搜索（limit=15）")
    for q in ["帮我找一个能做文献综述的智能体", "帮我找一个能写论文的智能体",
              "帮我找一个能审稿的智能体", "帮我找一个能做科研数据分析的智能体"]:
        r = c.post(URL, json={"type": "explicit", "query": q, "limit": 15})
        result = (r.json() or {}).get("result") or {}
        rs = rows(result)
        ours = [(i, nm) for i, (a, nm) in enumerate(rs, 1) if a in OURS]
        print(f"\n--- 「{q}」共 {len(rs)} 个结果")
        print(f"    我们命中 {len(ours)} 个，排名 {[i for i, _ in ours]}")
        for i, (a, nm) in enumerate(rs, 1):
            if i <= 5 or a in OURS:
                print(f"    {i:2d}. {'★' if a in OURS else ' '} {nm}")
