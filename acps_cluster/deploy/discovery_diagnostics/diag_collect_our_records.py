"""把平台索引里属于我们的记录**全部**捞出来（多问法汇合去重），并逐条查其 ACS 与存活状态。

只读：发现接口 + 公开 ACS 接口。
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
ACS_BASE = "https://wt.ioa.pub/registry-server/acps-atr-v2/acs/"

PROBE_QUERIES = [
    "研枢 科研 智能体", "文献检索", "论文写作 综述", "论文审稿 评审", "文献精读 信息抽取",
    "选题 前沿 趋势", "矛盾 假说 研究空白", "实验执行 GPU 沙箱", "引用核验",
    "arXiv 检索", "科研数据分析", "论文格式检查",
]


def ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cafile=str(ACPS / "trust-bundle.pem"))
    ctx.load_cert_chain(certfile=str(ACPS / "client" / "certificate.pem"),
                        keyfile=str(ACPS / "client" / "private-key.pem"))
    return ctx


def local_map() -> dict[str, dict]:
    out = {}
    for ws in sorted(CLUSTER.glob("ws-*")):
        f = ws / "acps" / "acs.final.json"
        if not f.is_file():
            continue
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if j.get("aic"):
            out[j["aic"]] = {
                "ws": ws.name,
                "name": j.get("name"),
                "version": j.get("version"),
                "dlen": len(j.get("description") or ""),
                "nex": sum(len(s.get("examples") or []) for s in (j.get("skills") or [])),
                "endpoints": [e.get("url") for e in (j.get("endPoints") or [])],
            }
    return out


with httpx.Client(verify=ssl_ctx(), timeout=40.0) as c:
    seen: dict[str, dict] = {}
    alive_hits: dict[str, dict] = {}
    for q in PROBE_QUERIES:
        r = c.post(DISCOVER, json={"type": "explicit", "query": q, "limit": 20})
        if r.status_code != 200:
            continue
        result = (r.json() or {}).get("result") or {}
        acs_map = result.get("acsMap") or {}
        alive = result.get("aliveMap") or {}

        def walk(node):
            if isinstance(node, dict):
                a = node.get("aic")
                if isinstance(a, str) and a not in seen:
                    acs = acs_map.get(a) or {}
                    if "研枢" in (acs.get("name") or "") or "arXiv 检索" in (acs.get("name") or ""):
                        seen[a] = {
                            "name": acs.get("name"),
                            "dlen": len(acs.get("description") or ""),
                            "nex": sum(len(s.get("examples") or []) for s in (acs.get("skills") or [])),
                            "skills": [s.get("id") for s in (acs.get("skills") or [])],
                            "endpoints": [e.get("url") for e in (acs.get("endPoints") or [])],
                        }
                        if a in alive:
                            alive_hits[a] = alive[a]
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(result)

    loc = local_map()
    print(f"平台上属于我们的记录：{len(seen)} 条\n")
    for aic, v in sorted(seen.items(), key=lambda kv: (kv[1]["name"] or "")):
        mark = "本地在跑" if aic in loc else "★本地没有这条（新注册/旧遗留）"
        print(f"--- {v['name']}   [{mark}]")
        print(f"    aic        {aic}")
        print(f"    描述 {v['dlen']} 字 / examples {v['nex']} 条 / skills {v['skills']}")
        print(f"    端点       {v['endpoints']}")
        print(f"    存活       {alive_hits.get(aic, '（aliveMap 未覆盖）')}")
        if aic in loc:
            l = loc[aic]
            print(f"    本地工作区 {l['ws']}：描述 {l['dlen']} 字 / examples {l['nex']} / 端点 {l['endpoints']}")

    print("\n=== 本地有、但索引里没捞到的成员（可能名字不匹配或已停用）===")
    for aic, l in loc.items():
        if aic not in seen:
            print(f"  {l['name']:32s} {aic}  （{l['ws']}）")
