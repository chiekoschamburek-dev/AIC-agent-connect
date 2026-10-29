#!/usr/bin/env python3
"""可复用的注册状态检查：本账号在 registry 上的所有 Agent + KB 待审状态。

用法：sudo -u wit /opt/wit/venv/bin/python deploy/discovery_diagnostics/kb_status.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx

CL = Path("/opt/wit/my-project/acps_cluster")
sys.path.insert(0, str(CL.parent))
sys.path.insert(0, str(CL))

for ln in (CL / "deploy" / ".env.production").read_text(encoding="utf-8").splitlines():
    s = ln.strip()
    if s and not s.startswith("#") and "=" in s:
        k, v = s.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

tokfile = Path("/opt/wit/tmp/probe-kb2/acps/secrets/registry-token.json")
enrfile = Path("/opt/wit/tmp/probe-kb2/acps/enrollment.json")
if not tokfile.is_file():
    print("  ✘ 找不到注册令牌（探测工作区可能已清理）")
    raise SystemExit(2)

tok = json.loads(tokfile.read_text(encoding="utf-8"))
base = os.environ["ACPS_REGISTRY_URL"].rstrip("/")
hdr = {"Authorization": "Bearer " + tok["accessToken"]}

print("=== 本地 enrollment 缓存 ===")
if enrfile.is_file():
    d = json.loads(enrfile.read_text(encoding="utf-8"))
    r = d.get("review") or {}
    m = r.get("metadata") or {}
    print(f"  status         = {d.get('status')}")
    print(f"  approvalStatus = {m.get('approvalStatus')}")
    print(f"  review.aic     = {r.get('aic')}")
    print(f"  identity.aic   = {((d.get('identity') or {}).get('ref') or {}).get('aic') or '（未发证）'}")

print()
print("=== registry 实时状态（权威）===")
try:
    resp = httpx.get(base + "/api/v1/agent/client", headers=hdr, timeout=25)
    print(f"  GET /api/v1/agent/client → HTTP {resp.status_code}")
    items = (resp.json() or {}).get("items") or []
    print(f"  本账号 Agent 共 {len(items)} 条：")
    for it in items:
        acs = it.get("acs") or {}
        nm = str(it.get("name") or "")
        mark = "  ← KB" if "KB" in nm else ""
        print(f"    {nm:28s} active={str(acs.get('active')):5s} "
              f"aic={(acs.get('aic') or '（待发证）')[:46]}{mark}")
except Exception as exc:  # noqa: BLE001
    print(f"  ✘ {type(exc).__name__}: {exc}")

print()
print("=== 结论 ===")
try:
    kb = next((it for it in items if "KB" in str(it.get("name") or "")), None)
    acs = (kb or {}).get("acs") or {}
    if acs.get("active") and acs.get("aic"):
        print(f"  ✔ KB 已批准并发证：AIC={acs['aic']}")
        print("     → 可执行：sh deploy/discovery_diagnostics/recover_kb_registration.sh")
    else:
        print("  ⏸ KB 仍在待审（active=false / aic 空）——需平台方 STAFF/ADMIN 审批。")
except Exception as exc:  # noqa: BLE001
    print(f"  无法判定：{type(exc).__name__}: {exc}")
