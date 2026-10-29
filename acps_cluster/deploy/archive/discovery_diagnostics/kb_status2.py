#!/usr/bin/env python3
"""KB 注册状态检查（修正版）：令牌与注册状态现已在 ws-kb。

同时回答一个关键前提：**ws-kb 内的已有注册能否被续用**——
若 up_until_ready 认不出它，就会重复提交一份新注册（那反而更麻烦）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx

CL = Path("/opt/wit/my-project/acps_cluster")
for ln in (CL / "deploy" / ".env.production").read_text(encoding="utf-8").splitlines():
    s = ln.strip()
    if s and not s.startswith("#") and "=" in s:
        k, v = s.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

WS = CL / "ws-kb"
tokfile = WS / "acps" / "secrets" / "registry-token.json"
enrfile = WS / "acps" / "enrollment.json"

print("=== 1) ws-kb 注册状态（本地缓存）===")
if not enrfile.is_file():
    print("  ✘ 无 enrollment.json")
    raise SystemExit(2)
d = json.loads(enrfile.read_text(encoding="utf-8"))
r = d.get("review") or {}
m = r.get("metadata") or {}
print(f"  status         = {d.get('status')}")
print(f"  approvalStatus = {m.get('approvalStatus')}")
print(f"  agentId        = {d.get('agentId')}")
print(f"  registrationId = {d.get('registrationId')}")
print(f"  review.aic     = {r.get('aic')}")
print(f"  draftPath      = {d.get('draftPath')}")
print(f"  acsFingerprint = {str(d.get('acsFingerprint'))[:40]}…")

print()
print("=== 2) 续用前提核实 ===")
# wit 解析现有注册时，靠 acpsConfig / acpsConfigFingerprint / agentId / draftPath
acps_cfg = d.get("acpsConfig") or {}
print(f"  acpsConfig 键: {sorted(acps_cfg)[:8]}")
print(f"  acpsConfigFingerprint = {str(d.get('acpsConfigFingerprint'))[:36]}…")
draft = Path(str(d.get("draftPath") or ""))
print(f"  draftPath 指向的文件存在: {draft.is_file()}  ({draft})")
if draft.is_file():
    dj = json.loads(draft.read_text(encoding="utf-8"))
    print(f"    草案里 name={dj.get('name')} aic={dj.get('aic')!r}（空 = 尚未发证，正常）")
print(f"  secrets/registry-token.json 存在: {tokfile.is_file()}")

print()
print("=== 3) registry 实时状态 ===")
if tokfile.is_file():
    tok = json.loads(tokfile.read_text(encoding="utf-8"))
    base = os.environ["ACPS_REGISTRY_URL"].rstrip("/")
    hdr = {"Authorization": "Bearer " + tok["accessToken"]}
    try:
        resp = httpx.get(base + "/api/v1/agent/client", headers=hdr, timeout=25)
        items = (resp.json() or {}).get("items") or []
        kb = next((it for it in items if "KB" in str(it.get("name") or "")), None)
        acs = (kb or {}).get("acs") or {}
        print(f"  本账号 Agent {len(items)} 条；KB: active={acs.get('active')} "
              f"aic={(acs.get('aic') or '（待发证）')}")
        if acs.get("active") and acs.get("aic"):
            print("  ✔ 已批准并发证 → 可执行 recover_kb_registration.sh")
        else:
            print("  ⏸ 仍在待审 —— 需平台方 STAFF/ADMIN 审批")
    except Exception as exc:  # noqa: BLE001
        print(f"  ✘ 查询失败 {type(exc).__name__}: {exc}")
else:
    print("  ✘ 无注册令牌，无法查询")
