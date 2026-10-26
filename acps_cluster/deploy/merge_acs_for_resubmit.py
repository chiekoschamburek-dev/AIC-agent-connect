"""把"平台说明书文字"与"我们的检索面"合并成提交稿（而不是用门面覆盖平台文字）。

背景（2026-10-09 实测发现）：
平台现网 ACS 里的 description 是**平台/评审侧写的能力说明书**（含入参上限、参数区间、
代码硬约束、不做清单），我们代码里并没有这些文字（例如 critic 的 176 字
`citation.verify` 描述在 `partner_critic.py` 里不存在）。
直接提交门面文字 = 删掉说明书 = 降级。

因此本脚本按下面的合并策略生成提交稿：

- **描述类（agent.description / skill.description）**：默认**保留平台现网原文**；
- **examples**：用我们的（平台现网为 0 条时是纯增益）；
- **tags**：取并集（保序：平台在前，我们的补充在后）；
- `--append-agent-search-surface`：可选，把门面的"典型问法"段落**追加**到平台描述之后
  （用于补检索词面，同时不丢说明书内容）。

用法：
    python merge_acs_for_resubmit.py --carrier <现网 ACS> --draft <make_acs 产物> \
        --out <合并后提交稿> [--append-agent-search-surface] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import discovery_metadata as dm  # noqa: E402


def merge_skills(carrier_skills: list[dict], draft_skills: list[dict]) -> tuple[list[dict], list[str]]:
    old = {s.get("id"): s for s in carrier_skills}
    out: list[dict] = []
    notes: list[str] = []
    for s in draft_skills:
        sid = s.get("id")
        prev = old.get(sid)
        merged = dict(s)
        if prev:
            # 描述：保留平台原文（说明书），缺失时才用我们的
            if prev.get("description"):
                merged["description"] = prev["description"]
            # tags：并集（平台在前）
            tags = list(prev.get("tags") or [])
            for t in (s.get("tags") or []):
                if t not in tags:
                    tags.append(t)
            merged["tags"] = tags
            # examples：我们的优先；平台有而我们没有时保留平台的
            if not (s.get("examples")):
                merged["examples"] = list(prev.get("examples") or [])
            notes.append(
                f"    {sid}: 描述保留平台原文({len(prev.get('description') or '')}字) / "
                f"tags {len(prev.get('tags') or [])}→{len(tags)} / "
                f"examples {len(prev.get('examples') or [])}→{len(merged.get('examples') or [])}"
            )
        else:
            notes.append(f"    {sid}: 平台新增（用我们的整套门面）")
        out.append(merged)
    return out, notes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--carrier", required=True, help="平台现网 ACS")
    ap.add_argument("--draft", required=True, help="make_acs 生成的提交稿")
    ap.add_argument("--out", required=True)
    ap.add_argument("--append-agent-search-surface", action="store_true",
                    help="把门面的『典型问法』段追加到平台 agent 描述之后")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    carrier = json.loads(Path(args.carrier).read_text(encoding="utf-8"))
    draft = json.loads(Path(args.draft).read_text(encoding="utf-8"))

    # 显示名 → agent 门面 id（facade 文本里没有稳定的键，只能显式映射）
    name_map = {
        "研枢-Critic-Partner": "yanshu.critic-partner",
        "研枢-Writer-Partner": "yanshu.writer-partner",
        "研枢-Synthesis-Partner": "yanshu.synthesis-partner",
        "研枢-Frontier-Partner": "yanshu.frontier-partner",
        "研枢-Contradiction-Partner": "yanshu.contradiction-partner",
        "研枢-Experiment-Partner": "yanshu.experiment-partner",
        "研枢-Scout-Partner": "yanshu.scout-partner",
        "研枢-Supervisor": "yanshu.supervisor",
    }
    agent_id = name_map.get(str(carrier.get("name")))

    merged = dict(draft)
    merged["description"] = carrier.get("description") or draft.get("description")
    print(f"载体：{carrier.get('name')}（{args.carrier}）")
    print(f"  agent 描述：平台 {len(carrier.get('description') or '')} 字 → 保留")
    if args.append_agent_search_surface and agent_id:
        surface = dm.AGENT_FACADES[agent_id].search_surface or dm.AGENT_FACADES[agent_id].description
        merged["description"] = f"{merged['description']}\n{surface}"
        print(f"  已追加检索面 → {len(merged['description'])} 字")

    merged["skills"], notes = merge_skills(carrier.get("skills") or [], draft.get("skills") or [])
    print("  skills：")
    for n in notes:
        print(n)

    text = json.dumps(merged, ensure_ascii=False, indent=2) + "\n"
    if args.dry_run:
        print("\n（--dry-run，未写文件）")
        return 0
    Path(args.out).write_text(text, encoding="utf-8")
    print(f"\n已写出：{args.out}（{len(text)} 字节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
