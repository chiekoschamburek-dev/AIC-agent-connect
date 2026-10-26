"""P0-3 缓解单测：按已准入 skill 收窄运行时工具集。

威胁（2026-10-11 审查 P0-3）：L0 放行依据调用方**自报**的 skill（`command_params` /
`data_items[].metadata`），而伙伴把全部能力（含 public=False）都注册进 agent
（public=False 只影响 ACS 可见性）→ 自报一个公开 skillId 就能入场，
再用自然语言驱动非公开高成本能力。

覆盖：
1. `non_public_ids` 能正确区分公开/非公开能力；
2. `resolve_scope`：非公开能力不在准入集合 → 进移除集；在集合 → 保留；
3. 钩子真的从 ToolManager 移除未准入的非公开工具，公开工具与代理工具不动；
4. 令牌授权的 skill 也被计入准入集合（L2 路径取并集）；
5. 未声明非公开能力的伙伴 → 空操作（向后兼容）。
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

CL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CL))
sys.path.insert(0, str(CL.parent))
os.environ["YANSHU_RUN_TRACE"] = "0"

from wit.kernel.capabilities import Capability, ToolManager  # noqa: E402

from delegation import skill_scope  # noqa: E402


def _cap(cid: str, public: bool, metadata: dict | None = None) -> Capability:
    return Capability(id=cid, description=f"{cid} 说明", public=public,
                      metadata=metadata or {})


class _Mgr:
    """最小 ToolManager 替身：只实现 remove_tool 语义。"""

    def __init__(self, names: list[str]) -> None:
        self._names = list(names)
        self.removed: list[str] = []

    def remove_tool(self, name: str):  # noqa: ANN201
        if name in self._names:
            self._names.remove(name)
            self.removed.append(name)
            return {"name": name}
        return None

    @property
    def remaining(self) -> list[str]:
        return list(self._names)


class InternalToolScopeTest(unittest.TestCase):
    """partner 的 ws_* 客户端工具是 internal_tool：不参与非公开能力收窄。

    它们不是对外能力面；令牌校验在工具内部 fail-closed，作用域由 bundle 的
    task_id 固定。若被当成"未准入的非公开能力"移除，伙伴永远看不到工作区工具。
    """

    def test_内部工具不被收窄(self):
        caps = [
            _cap("arxiv.search", True),
            _cap("scholar.deep_fetch", False),
            _cap("ws.read", False, {"internal_tool": True}),
        ]
        self.assertEqual(skill_scope.non_public_ids(caps), {"scholar.deep_fetch"})
        mgr = _Mgr(["arxiv.search", "scholar.deep_fetch", "ws.read"])
        # token_dir 必须指向空目录：钩子会读本任务令牌文件把授权 skill 并入准入集合，
        # 不隔离就会读到工作区里残留的令牌，把测试变成"环境相关"（2026-10-26 实测）。
        with tempfile.TemporaryDirectory() as td:
            hook = skill_scope.SkillScopedToolsHooks(
                non_public=skill_scope.non_public_ids(caps),
                public_skills={"arxiv.search"},
                token_dir=Path(td),
            )
            asyncio.run(hook.on_run_start(
                SimpleNamespace(context=SimpleNamespace(tool_manager=mgr))))
        self.assertIn("ws.read", mgr.remaining, "internal_tool 必须保留")
        self.assertIn("arxiv.search", mgr.remaining)
        self.assertNotIn("scholar.deep_fetch", mgr.remaining)


async def main() -> int:
    failures: list[str] = []
    caps = [
        _cap("arxiv.search", True), _cap("openalex.search", True),
        _cap("scholar.deep_fetch", False), _cap("dataset.fetch", False),
    ]

    # 1) 公开/非公开识别
    npub = skill_scope.non_public_ids(caps)
    print(f"  [1] non_public_ids = {sorted(npub)}（期望 scholar.deep_fetch / dataset.fetch）")
    if npub != {"scholar.deep_fetch", "dataset.fetch"}:
        failures.append(f"[1] {sorted(npub)}")

    # 2) resolve_scope 纯函数
    keeps, drops = skill_scope.resolve_scope(
        non_public=npub, public_skills={"arxiv.search"},
        admitted="arxiv.search", token_skill=None)
    print(f"  [2] 准入 arxiv.search → 保留={sorted(keeps)} 移除={sorted(drops)}")
    if keeps or drops != npub:
        failures.append(f"[2] keeps={sorted(keeps)} drops={sorted(drops)}")
    keeps2, drops2 = skill_scope.resolve_scope(
        non_public=npub, public_skills={"arxiv.search"},
        admitted="arxiv.search", token_skill="dataset.fetch")
    print(f"      准入 arxiv.search + 令牌 dataset.fetch → 保留={sorted(keeps2)} 移除={sorted(drops2)}")
    if keeps2 != {"dataset.fetch"} or drops2 != {"scholar.deep_fetch"}:
        failures.append(f"[2b] keeps={sorted(keeps2)} drops={sorted(drops2)}")

    # 3) 钩子真的移除（自报公开 skill 入场 → 非公开工具消失）
    mgr = _Mgr(["arxiv.search", "openalex.search", "scholar.deep_fetch",
                "dataset.fetch", "aip_submit"])
    hook = skill_scope.SkillScopedToolsHooks(non_public=npub,
                                             public_skills={"arxiv.search", "openalex.search"})
    skill_scope.set_admitted_skill("arxiv.search")
    await hook.on_run_start(SimpleNamespace(context=SimpleNamespace(tool_manager=mgr)))
    print(f"  [3] 移除={sorted(mgr.removed)}  剩余={sorted(mgr.remaining)}")
    if sorted(mgr.removed) != ["dataset.fetch", "scholar.deep_fetch"]:
        failures.append(f"[3] 移除集={sorted(mgr.removed)}")
    if "arxiv.search" not in mgr.remaining or "aip_submit" not in mgr.remaining:
        failures.append("[3] 误删公开能力或代理工具")

    # 4) 无准入 skill（未挂 PEP 的调用）→ 全部非公开都被移除
    mgr2 = _Mgr(["arxiv.search", "scholar.deep_fetch", "dataset.fetch"])
    skill_scope.set_admitted_skill(None)
    await hook.on_run_start(SimpleNamespace(context=SimpleNamespace(tool_manager=mgr2)))
    print(f"  [4] 无准入 skill → 移除={sorted(mgr2.removed)}（期望两个都移除）")
    if sorted(mgr2.removed) != ["dataset.fetch", "scholar.deep_fetch"]:
        failures.append(f"[4] {sorted(mgr2.removed)}")

    # 5) 无能力声明的伙伴 → 空操作
    mgr3 = _Mgr(["paper.extract"])
    empty = skill_scope.SkillScopedToolsHooks(non_public=frozenset(), public_skills={"paper.extract"})
    await empty.on_run_start(SimpleNamespace(context=SimpleNamespace(tool_manager=mgr3)))
    print(f"  [5] 无非公开能力 → 移除={mgr3.removed}（期望空）")
    if mgr3.removed:
        failures.append(f"[5] {mgr3.removed}")

    # 6) 真实 ToolManager 上也能移除（确认 API 存在）
    tm = ToolManager([])
    from wit import Tool
    async def _h(arguments):  # noqa: ANN001, ANN202
        return {}
    tm.add_tool(Tool(name="scholar.deep_fetch", description="d",
                     input_schema={"type": "object"}, handler=_h))
    ok = tm.remove_tool("scholar.deep_fetch") is not None
    print(f"  [6] 真实 ToolManager.remove_tool 可用: {ok}（剩余 {sorted(tm.lookup())}）")
    if not ok:
        failures.append("[6] 真实 ToolManager 不支持移除")

    print()
    if failures:
        for f in failures:
            print("  ✘", f)
        return 1
    print("✔ 通过：未准入的非公开能力被移出工具集，公开能力与代理工具不受影响。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
