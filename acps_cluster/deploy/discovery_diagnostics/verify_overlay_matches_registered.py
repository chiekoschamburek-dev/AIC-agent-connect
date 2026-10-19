"""离线验证：代码投影（经覆盖表）是否与"平台注册文本"逐字一致。

这是部署前的关键闸门——只要这里全绿，服务器上的 wit 指纹保护就不会触发。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

CL = Path(__file__).resolve().parents[2]          # acps_cluster
sys.path.insert(0, str(CL))
sys.path.insert(0, str(CL.parent))

import discovery_metadata as dm  # noqa: E402

SNAP = CL / "_platform_snapshot"
BUILDERS = {
    "scout": ("acs.live.scout.json", "partner_scout", "build_scout_agent"),
    "supervisor": ("acs.live.supervisor.json", "supervisor", "build_supervisor_agent"),
    "critic": ("acs.live.critic.json", "partner_critic", "build_critic_agent"),
    "writer": ("acs.live.writer.json", "partner_writer", "build_writer_agent"),
    "synthesis": ("acs.live.synthesis.json", "partner_synthesis", "build_synthesis_agent"),
    "experiment": ("acs.live.experiment.json", "partner_experiment", "build_experiment_agent"),
    "frontier": ("acs.live.frontier.json", "partner_frontier", "build_frontier_agent"),
    "contradiction": ("acs.live.contradiction.json", "partner_contradiction", "build_contradiction_agent"),
    "citation": ("acs.live.citation.json", "partner_citation", "build_citation_agent"),
}

overlay = dm.registered_overlay()
print(f"覆盖表：agent {len(overlay['agents'])} 个 / skill {len(overlay['skills'])} 个\n")

print(f"{'agent':14s} {'agent 描述':22s} {'skill 描述':24s} {'tags':8s} {'examples':9s}")
print("-" * 84)
bad = 0
for key, (snap, mod_name, builder) in BUILDERS.items():
    reg = json.loads((SNAP / snap).read_text(encoding="utf-8"))
    mod = __import__(mod_name)
    agent = getattr(mod, builder)(None)

    # agent 级
    d_ok = agent.description == (reg.get("description") or "")
    if not d_ok:
        bad += 1
        print(f"  ✘ {key} agent 描述不一致：代码 {len(agent.description)} 字 vs 注册 "
              f"{len(reg.get('description') or '')} 字")

    reg_skills = {s["id"]: s for s in (reg.get("skills") or [])}
    line = [f"{key:14s}", f"{'✔ ' + str(len(agent.description)) + ' 字':22s}"]
    skill_status = []
    for item in agent.capabilities:
        cap = dm._capability_of(item)
        skill = cap.to_acps_skill()
        if skill is None:
            continue
        sid = skill["id"]
        r = reg_skills.get(sid)
        if r is None:
            skill_status.append(f"{sid}:未在注册版")
            bad += 1
            continue
        sd = skill.get("description") == r.get("description")
        st = list(skill.get("tags") or []) == list(r.get("tags") or [])
        se = list(skill.get("examples") or []) == list(r.get("examples") or [])
        if not (sd and st and se):
            bad += 1
            skill_status.append(f"{sid}:" + ",".join(
                [x for x, ok in (("desc", sd), ("tags", st), ("ex", se)) if not ok]) + "≠")
        else:
            skill_status.append(f"{sid}:✔")
    line.append(f"{' '.join(skill_status):24s}")
    line.append(f"{len(reg_skills):<8d}")
    line.append(f"{sum(len(s.get('examples') or []) for s in reg_skills.values()):<9d}")
    print(" ".join(line))

print("-" * 84)
print(f"不一致项：{bad}")
if bad == 0:
    print("→ 代码投影与平台注册文本**逐字一致**，服务器上不会再触发 ACS 指纹保护。")
sys.exit(1 if bad else 0)
