"""方案 B 单测：L0 放宽「未指名 skill」的入场，能力面仍靠运行时收窄。

## 背景（2026-10-11 实测）

平台侧调用**不会**稳定携带 skillId（AIP v2 的 `TaskCommand` 里没有这个字段），
所以"未指名 skill"是常态。而旧规则 `unscoped_entry_open()` 要求"本机**全部**能力都公开"，
于是只要有一个非公开能力（scout 有 3 个），常态调用就会被
`deny rule=no-credential code=-32008` 拒掉。

实测对照：
  · 叮当 → supervisor：`allow rule=public-skill-unscoped`（唯一能力即公开）
  · 叮当 → scout     ：`deny  rule=no-credential`（有非公开能力）
差别只在"本机有没有非公开能力"，与调用方是否滥用无关。

## 修法（方案 B）

新增 `unscoped_public_entry_open()`：只要求"确有公开能力"，
未指名 skill 时放行，记 `rule=public-skill-unscoped-narrowed`。
**能力面照旧收紧**：`skill_scope` 在"无准入 skill"时移除所有 `public=False` 的工具。

## 覆盖

1. 有非公开能力 + 未指名 skill → **允许**（本次修复的核心）
2. 完全没有公开能力 + 未指名 skill → **仍拒绝**（fail-closed 保留）
3. 指名公开 skill → 仍 `public-skill`
4. 指名非公开 skill → 仍落到 L1/L2（不放行）
5. 严格口径 `unscoped_entry_open()` 语义不变（向后兼容）
6. 放宽路径下，运行时收窄确实移除了非公开工具（能力面没放宽）
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

CL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CL))
sys.path.insert(0, str(CL.parent))
os.environ["YANSHU_RUN_TRACE"] = "0"

from acps_cluster.delegation import skill_scope  # noqa: E402
from acps_cluster.delegation.pep import PepConfig, evaluate  # noqa: E402


def _cfg(*, public=("arxiv.search", "openalex.search"), has_non_public=True) -> PepConfig:
    return PepConfig(
        enabled=True,
        my_aic="1.2.156.3088.1.BUPT.VTF97R.GMT5S6.4SZM68.0LC5",
        public_skills=frozenset(public),
        has_non_public_skills=has_non_public,
        leader_aics=frozenset({"1.2.156.3088.1.BUPT.VTF97R.JXN8O8.98DOO8.0EB6"}),
    )


def _req(*, params=None, items=None, caller="1.2.156.3088.1.0001.00001.EZTK03.JISTB6.0P3V"):
    return SimpleNamespace(
        leader_aic=caller,
        command_params=params or {},
        data_items=items or [],
        peer_verified=True,
        task_id="t1",
        session_id="s1",
        delivery="rpc",
    )


class _Mgr:
    def __init__(self, names):
        self._n = list(names)
        self.removed = []

    def remove_tool(self, name):  # noqa: ANN201
        if name in self._n:
            self._n.remove(name)
            self.removed.append(name)
            return {"name": name}
        return None

    @property
    def remaining(self):  # noqa: ANN201
        return list(self._n)


async def main() -> int:
    failures: list[str] = []

    # 1) ★ 修复核心：有非公开能力 + 未指名 skill → 允许
    out = evaluate(_req(), _cfg())
    print(f"  [1] scout 场景（有非公开能力、未指名 skill）→ {out.decision} / {out.rule}")
    if not out.allowed or out.rule != "public-skill-unscoped-narrowed":
        failures.append(f"[1] 期望 allow/public-skill-unscoped-narrowed，实得 {out.decision}/{out.rule}")

    # 2) 完全没有公开能力 → 仍拒绝（fail-closed）
    out2 = evaluate(_req(), _cfg(public=(), has_non_public=True))
    print(f"  [2] 无任何公开能力、未指名 skill → {out2.decision} / {out2.rule}")
    if out2.allowed:
        failures.append("[2] 无公开能力的 agent 不该放行")

    # 3) 指名公开 skill → public-skill（原行为不变）
    out3 = evaluate(_req(params={"skillId": "arxiv.search"}), _cfg())
    print(f"  [3] 指名公开 skill arxiv.search → {out3.decision} / {out3.rule}")
    if not out3.allowed or out3.rule != "public-skill":
        failures.append(f"[3] 实得 {out3.decision}/{out3.rule}")

    # 4) 指名非公开 skill → 不放行
    out4 = evaluate(_req(params={"skillId": "scholar.deep_fetch"}), _cfg())
    print(f"  [4] 指名非公开 skill scholar.deep_fetch → {out4.decision} / {out4.rule}")
    if out4.allowed:
        failures.append(f"[4] 非公开 skill 不该放行（实得 {out4.rule}）")

    # 5) 严格口径语义不变
    c = _cfg()
    print(f"  [5] unscoped_entry_open={c.unscoped_entry_open()}（期望 False）"
          f"  unscoped_public_entry_open={c.unscoped_public_entry_open()}（期望 True）")
    if c.unscoped_entry_open():
        failures.append("[5] 严格口径被改动")
    if not c.unscoped_public_entry_open():
        failures.append("[5] 宽松口径未生效")

    # 6) ★ 关键：放宽入场后，运行时收窄确实移除了非公开工具
    caps = [SimpleNamespace(id="arxiv.search", public=True),
            SimpleNamespace(id="scholar.deep_fetch", public=False),
            SimpleNamespace(id="arxiv.fetch", public=False),
            SimpleNamespace(id="dataset.fetch", public=False)]
    mgr = _Mgr(["arxiv.search", "openalex.search", "scholar.deep_fetch",
                "arxiv.fetch", "dataset.fetch", "aip_submit"])
    hook = skill_scope.SkillScopedToolsHooks(
        non_public=skill_scope.non_public_ids(caps), public_skills={"arxiv.search"})
    skill_scope.set_admitted_skill(out.skill_hint)      # 放宽路径下 skill_hint 为 None
    await hook.on_run_start(SimpleNamespace(context=SimpleNamespace(tool_manager=mgr)))
    print(f"  [6] 放宽入场后，非公开工具被移除={sorted(mgr.removed)}")
    print(f"      模型仍可见={sorted(mgr.remaining)}")
    if sorted(mgr.removed) != ["arxiv.fetch", "dataset.fetch", "scholar.deep_fetch"]:
        failures.append(f"[6] 非公开工具未被移除：{sorted(mgr.removed)}")
    if "scholar.deep_fetch" in mgr.remaining:
        failures.append("[6] 非公开能力仍对模型可见 → 能力面被放宽了")

    print()
    if failures:
        for f in failures:
            print("  ✘", f)
        return 1
    print("✔ 通过：入场放宽（未指名 skill 也能进），能力面照旧收紧（非公开工具不可见）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
