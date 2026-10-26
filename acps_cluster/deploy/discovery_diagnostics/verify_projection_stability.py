"""部署前验证：投影是否稳定（enroll 与 serve 两阶段指纹一致）。

关键：wit 在 enroll 阶段与 serve 阶段的投影必须算出**同一个指纹**，
否则会误报"ACS 已变"。历史 bug：serve 阶段会带上 skill.version，enroll 不带。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

CL = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(CL))
sys.path.insert(0, str(CL.parent))

import discovery_metadata as dm  # noqa: E402
from wit.acps.provisioner import _fingerprint  # type: ignore  # noqa: E402

CASES = {
    "contradiction": ("partner_contradiction", "acs.live.contradiction.json"),
    "frontier": ("partner_frontier", "acs.live.frontier.json"),
    "critic": ("partner_critic", "acs.live.critic.json"),
    "scout": ("partner_scout", "acs.live.scout.json"),
    "supervisor": ("supervisor", "acs.live.supervisor.json"),
    "writer": ("partner_writer", "acs.live.writer.json"),
    "synthesis": ("partner_synthesis", "acs.live.synthesis.json"),
    "experiment": ("partner_experiment", "acs.live.experiment.json"),
    "citation": ("partner_citation", "acs.live.citation.json"),
    "workspace": ("partner_workspace", "acs.live.workspace.json"),
}

print(f"{'agent':15s} {'skill version':16s} {'描述':6s} {'tags':5s} {'ex':4s}  与注册版一致")
print("-" * 78)
bad = 0
for key, (mod_name, snap) in CASES.items():
    reg = json.loads((CL / "_platform_snapshot" / snap).read_text(encoding="utf-8"))
    reg_skills = {s["id"]: s for s in (reg.get("skills") or [])}
    mod = __import__(mod_name)
    builder = [n for n in dir(mod) if n.startswith("build_") and n.endswith("_agent")][0]
    agent = getattr(mod, builder)(None)

    ok_agent = agent.description == (reg.get("description") or "")
    for item in agent.capabilities:
        s = item.capability.to_acps_skill()
        if not s:
            continue
        r = reg_skills.get(s["id"]) or {}
        same = all(s.get(f) == r.get(f) for f in
                   ("description", "version", "tags", "examples", "inputModes", "outputModes", "name"))
        if not same or not ok_agent:
            bad += 1
        print(f"{key:15s} {s['id'][:16]:16s} {len(s.get('description') or ''):6d} "
              f"{len(s.get('tags') or []):5d} {len(s.get('examples') or []):4d}  "
              f"{'✔' if same else '✘'}  version={s.get('version')!r}")

print("-" * 78)
print(f"不一致项：{bad}")

# 真正的稳定性不变量：投影里的每个 skill 都必须带 version。
# 历史 bug 是 enroll 阶段投影无 version、serve 阶段有 version；修复后两阶段都由
# apply_to_capabilities 注入同一个 version。若这里出现缺 version 的 skill，两阶段
# 就会再次漂移。注意：不能拿"含 version vs 去掉 version 的指纹"当自检——_fingerprint
# 对整个 draft 做哈希，version 参与哈希，两者本来就必然不同（那是恒红的原因）。
print("\n=== 投影稳定性自检（contradiction）===")
mod = __import__("partner_contradiction")
agent = mod.build_contradiction_agent(None)
base = json.loads((CL / "_platform_snapshot" / "acs.live.contradiction.json").read_text(encoding="utf-8"))
proj = dict(base)
proj["description"] = agent.description
proj["skills"] = [s for item in agent.capabilities
                  if (s := item.capability.to_acps_skill()) is not None]
missing = [str(s.get("id")) for s in proj["skills"] if not s.get("version")]
if missing:
    bad += 1
    print(f"  ✘ 这些 skill 缺 version，enroll/serve 投影会漂移：{', '.join(missing)}")
else:
    print(f"  ✔ 投影的 {len(proj['skills'])} 个 skill 均带 version："
          f"enroll/serve 投影同源（稳定）")
    print(f"     指纹：{_fingerprint(proj)}")
sys.exit(1 if bad else 0)
