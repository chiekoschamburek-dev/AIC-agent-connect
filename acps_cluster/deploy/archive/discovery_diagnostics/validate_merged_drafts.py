"""批量校验 6 份合并后的提交稿（schema / 隐形字符 / 端点 / 与载体的关系）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]          # 仓库根：AIC智能体互联
CL = Path(__file__).resolve().parents[2]            # acps_cluster
SCHEMA = REPO / "ACPs-community/registry-server/app/agent/acsSchema.json"
SNAP = CL / "_platform_snapshot"
INVISIBLE = ("\u200b", "\u200c", "\u200d", "\ufeff", "\u00a0", "\u2060")

import jsonschema  # noqa: E402

schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
KEYS = ("critic", "writer", "synthesis", "frontier", "contradiction", "experiment")
bad = 0
print(f"{'agent':14s} {'schema':7s} {'隐形字符':9s} {'版本':9s} {'描述':6s} {'examples':9s} {'端点保留':8s} 与平台描述一致")
print("-" * 96)
for k in KEYS:
    p = CL / f"acs.{k}.resubmit.json"
    acs = json.loads(p.read_text(encoding="utf-8"))
    carrier = json.loads((SNAP / f"acs.live.{k}.json").read_text(encoding="utf-8"))
    text = p.read_text(encoding="utf-8")

    try:
        jsonschema.validate(instance=acs, schema=schema)
        st = "PASS"
    except jsonschema.ValidationError as exc:
        st = "FAIL"
        bad += 1
        print(f"   schema 错误：{exc.message} @ {list(exc.path)}")

    inv = [f"U+{ord(c):04X}" for c in INVISIBLE if c in text]
    ep_same = acs.get("endPoints") == carrier.get("endPoints")
    desc_keeps_platform = str(acs.get("description", "")).startswith(str(carrier.get("description", ""))[:40])
    nex = sum(len(s.get("examples") or []) for s in acs["skills"])
    print(f"{k:14s} {st:7s} {('无' if not inv else ','.join(inv)):9s} "
          f"{carrier.get('version') + '→' + acs['version']:9s} {len(acs['description']):6d} "
          f"{nex:<9d} {str(ep_same):8s} {desc_keeps_platform}")

    # 端点 / 安全方案复核
    for ep in acs.get("endPoints") or []:
        if not any("mtls" in s for s in (ep.get("security") or [])):
            print(f"   ✘ 端点缺 mtls 引用：{ep.get('url')}")
            bad += 1
print("-" * 96)
print(f"失败项：{bad}")
sys.exit(1 if bad else 0)
