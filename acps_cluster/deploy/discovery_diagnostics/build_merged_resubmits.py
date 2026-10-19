"""一次性脚本：为 6 个 agent 生成"平台说明书 + 我们的检索面"合并版提交稿。

策略（见 merge_acs_for_resubmit.py 的说明）：
- agent/skill 描述 → **保留平台现网原文**（那是平台侧写的能力说明书，带硬约束与不做清单）；
- agent 描述 → **追加**门面的"典型问法"段（补检索词面，不丢说明书）；
- skill tags → 并集；skill examples → 用我们的（平台现网多为 0 条）；
- version → 抬到平台未占用的号（避开 (name, version) 唯一约束）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

CL = Path(__file__).resolve().parents[2]      # acps_cluster（脚本在 deploy/discovery_diagnostics/ 下）
sys.path.insert(0, str(CL))
sys.path.insert(0, str(CL / "deploy"))
sys.path.insert(0, str(CL.parent))

import discovery_metadata as dm  # noqa: E402

SNAP = CL / "_platform_snapshot"
NAME2ID = {
    "研枢-Critic-Partner": "yanshu.critic-partner",
    "研枢-Writer-Partner": "yanshu.writer-partner",
    "研枢-Synthesis-Partner": "yanshu.synthesis-partner",
    "研枢-Frontier-Partner": "yanshu.frontier-partner",
    "研枢-Contradiction-Partner": "yanshu.contradiction-partner",
    "研枢-Experiment-Partner": "yanshu.experiment-partner",
}
TARGETS = {
    "critic": ("1.11.0", True),
    "writer": ("1.1.0", True),
    "synthesis": ("1.1.0", True),
    "frontier": ("1.1.0", True),
    "contradiction": ("1.1.0", True),
    "experiment": ("1.1.0", True),
}

for key, (version, append_surface) in TARGETS.items():
    carrier_path = SNAP / f"acs.live.{key}.json"
    draft_path = CL / f"acs.{key}.resubmit.json"
    carrier = json.loads(carrier_path.read_text(encoding="utf-8"))
    draft = json.loads(draft_path.read_text(encoding="utf-8"))

    merged = dict(draft)
    merged["version"] = version
    plat_desc = carrier.get("description") or ""
    agent_id = NAME2ID.get(str(carrier.get("name")))
    notes = []
    if plat_desc:
        merged["description"] = plat_desc
        if append_surface and agent_id:
            facade = dm.AGENT_FACADES[agent_id]
            surface = facade.search_surface or facade.description
            merged["description"] = f"{plat_desc}\n{surface}"
            notes.append(f"agent 描述：平台 {len(plat_desc)} 字 + 追加问法段 = {len(merged['description'])} 字")
        else:
            notes.append(f"agent 描述：保留平台 {len(plat_desc)} 字")

    old_skills = {s.get("id"): s for s in (carrier.get("skills") or [])}
    new_skills = []
    for s in (draft.get("skills") or []):
        prev = old_skills.get(s.get("id")) or {}
        item = dict(s)
        if prev.get("description"):
            item["description"] = prev["description"]
            notes.append(f"  {s['id']}: 描述保留平台 {len(prev['description'])} 字")
        tags = list(prev.get("tags") or [])
        for t in (s.get("tags") or []):
            if t not in tags:
                tags.append(t)
        item["tags"] = tags
        if not item.get("examples"):
            item["examples"] = list(prev.get("examples") or [])
        notes.append(f"  {s['id']}: tags {len(prev.get('tags') or [])}→{len(tags)}, "
                     f"examples {len(prev.get('examples') or [])}→{len(item.get('examples') or [])}")
        new_skills.append(item)
    merged["skills"] = new_skills

    out = CL / f"acs.{key}.resubmit.json"
    out.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"### {key}  v{carrier.get('version')} → v{version}")
    for n in notes:
        print(f"    {n}")
    print(f"    → {out.name}\n")
