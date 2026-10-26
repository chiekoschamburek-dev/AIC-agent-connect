"""对比：平台认为"在线"的 agent vs 我们（不在线）的 ACS 字段差异。

平台公开接口：GET /registry-server/acps-atr-v2/acs/{aic}（无需登录）
只读：只 GET 公开 ACS。
"""

from __future__ import annotations

import json
import os
import ssl
from pathlib import Path

import httpx

CLUSTER = Path(os.getenv("YANSHU_CLUSTER_DIR", "/opt/wit/my-project/acps_cluster"))
BASE = "https://wt.ioa.pub/registry-server/acps-atr-v2/acs/"

# 平台 trending 里出现（=被认为存活）的 agent
THEIRS = {
    "1.2.156.3088.1.BUPT.G4J19Z.24R9KF.UD8CST.0JVQ": "众擎·宠物托管",
    "1.2.156.3088.1.BUPT.L2K8YU.SJKLDA.578Y8P.19DJ": "文献检索与知识综述智能体",
    "1.2.156.3088.1.BUPT.4GKA38.OER277.9WXU5W.0E9H": "科研数据分析智能体",
}
# 我们这组
OURS = {}
for d in sorted(CLUSTER.glob("ws-*")):
    f = d / "acps" / "acs.final.json"
    if f.is_file():
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
            if j.get("aic"):
                OURS[j["aic"]] = j.get("name")
        except Exception:  # noqa: BLE001
            pass


def ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cafile=str(CLUSTER / "ws-scout" / "acps" / "trust-bundle.pem"))
    ctx.load_cert_chain(certfile=str(CLUSTER / "ws-scout" / "acps" / "client" / "certificate.pem"),
                        keyfile=str(CLUSTER / "ws-scout" / "acps" / "client" / "private-key.pem"))
    return ctx


def shape(d: object, prefix: str = "") -> list[str]:
    """收集 ACS 的字段路径（去掉数组下标）。"""
    out: list[str] = []
    if isinstance(d, dict):
        for k, v in d.items():
            out.append(f"{prefix}{k}")
            out.extend(shape(v, f"{prefix}{k}."))
    elif isinstance(d, list) and d:
        out.extend(shape(d[0], f"{prefix}[]."))
    return out


with httpx.Client(verify=ssl_ctx(), timeout=30.0) as c:
    data: dict[str, dict] = {}
    for aic, label in {**THEIRS, **OURS}.items():
        try:
            r = c.get(BASE + aic)
        except Exception as exc:  # noqa: BLE001
            print(f"[跳过] {label}: {type(exc).__name__} {exc}")
            continue
        if r.status_code != 200:
            print(f"[HTTP {r.status_code}] {label} ({aic})  body={r.text[:160]}")
            continue
        try:
            j = r.json()
        except Exception:  # noqa: BLE001
            print(f"[非 JSON] {label}: {r.text[:160]}")
            continue
        data[aic] = j
        print(f"[OK] {label}  顶层字段 {sorted(j.keys())}")

print("\n=== 字段路径对比（只列差异）===")
theirs_paths: set[str] = set()
ours_paths: set[str] = set()
for aic, j in data.items():
    ps = set(shape(j))
    if aic in THEIRS:
        theirs_paths |= ps
    else:
        ours_paths |= ps

print("只有他们有：", sorted(theirs_paths - ours_paths))
print("只有我们有：", sorted(ours_paths - theirs_paths))

print("\n=== 逐个字段值对比（挑关键字段）===")
KEYS = ["active", "version", "protocolVersion", "lastModifiedTime", "endPoints", "capabilities",
        "securitySchemes", "skills", "provider", "certificate", "description", "name"]
for aic, j in data.items():
    who = "他们" if aic in THEIRS else "我们"
    print(f"\n--- [{who}] {j.get('name')} ({aic})")
    for k in KEYS:
        if k in j:
            v = j[k]
            if k == "skills" and isinstance(v, list):
                print(f"    skills: {[s.get('id') for s in v]}")
                if v:
                    print(f"      首个 skill 字段: {sorted(v[0].keys())}")
            elif k == "endPoints":
                print(f"    endPoints: {json.dumps(v, ensure_ascii=False)}")
            elif k == "certificate":
                print(f"    certificate: {json.dumps(v, ensure_ascii=False)[:200]}")
            elif k == "capabilities":
                print(f"    capabilities: {json.dumps(v, ensure_ascii=False)}")
            elif k in ("name", "description", "version", "protocolVersion", "lastModifiedTime", "active"):
                print(f"    {k}: {json.dumps(v, ensure_ascii=False)[:160]}")
