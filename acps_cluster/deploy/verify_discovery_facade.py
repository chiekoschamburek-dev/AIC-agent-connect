"""本地验证：门面是否真的进了 agent 的能力声明（不联网、不提交平台）。

做法：构建每个 partner 的 WitAgent，取出 capabilities，
直接调用 wit 的 `Capability.to_acps_skill()`（ACS 投影函数）看平台将看到什么。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import discovery_metadata as dm  # noqa: E402


def cap_of(item):
    return dm._capability_of(item)


def main() -> int:
    import partner_scout
    import partner_writer
    import partner_synthesis
    import partner_critic
    import partner_frontier
    import partner_contradiction

    builders = {
        "scout": (partner_scout.build_scout_agent, None),
        "writer": (partner_writer.build_writer_agent, None),
        "synthesis": (partner_synthesis.build_synthesis_agent, None),
        "critic": (partner_critic.build_critic_agent, None),
        "frontier": (partner_frontier.build_frontier_agent, None),
        "contradiction": (partner_contradiction.build_contradiction_agent, None),
    }

    for name, (builder, _) in builders.items():
        try:
            agent = builder(None)
        except Exception as exc:  # noqa: BLE001
            print(f"[{name}] 构建失败：{type(exc).__name__}: {exc}")
            continue
        desc = str(getattr(agent, "description", "") or "")
        caps = list(getattr(agent, "capabilities", []) or [])
        print(f"\n===== {name}  描述 {len(desc)} 字")
        print("  " + desc[:150].replace("\n", " ") + ("…" if len(desc) > 150 else ""))
        for item in caps:
            cap = cap_of(item)
            public = getattr(cap, "public", False) or getattr(cap, "acps_skill", False) not in (False, None)
            flag = "公开" if getattr(cap, "public", False) else ("可见" if public else "内部")
            ex = list(getattr(cap, "examples", []) or [])
            print(
                f"    [{flag}] {cap.id:28s} 描述 {len(cap.description):3d} 字  "
                f"tags {len(getattr(cap, 'tags', []) or [])}  examples {len(ex)}"
            )
            if public:
                try:
                    skill = cap.to_acps_skill()
                except Exception as exc:  # noqa: BLE001
                    print(f"        ACS 投影失败：{exc}")
                    continue
                if skill is None:
                    print("        ACS 投影为 None（该能力不进 ACS）")
                    continue
                print(
                    "        ACS: id={id} tags={tags} examples={nex} desc={dlen}字".format(
                        id=skill.get("id"),
                        tags=len(skill.get("tags") or []),
                        nex=len(skill.get("examples") or []),
                        dlen=len(skill.get("description") or ""),
                    )
                )
    print("\n=== 门面统计 ===")
    print(f"agent 门面 {len(dm.AGENT_FACADES)} 个 / 能力门面 {len(dm.CAPABILITY_FACADES)} 个")
    print(json.dumps({k: len(v.description) for k, v in list(dm.AGENT_FACADES.items())}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
