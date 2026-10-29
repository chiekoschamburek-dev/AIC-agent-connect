"""P0-3 缓解：按「本任务已准入的 skill」收窄运行时工具集。

## 威胁（2026-10-11 审查 P0-3）

`pep.py` 的 L0 放行依据 `requested_skill()`——它只读 `command_params` 与
`data_items[].metadata`，**全是调用方可控字段**（协议里 `TaskCommand` 没有 skillId，
只能靠约定字段），命中 `public_skills` 就在 L1/L2 之前 `allow`。
而伙伴把**全部**能力（含 `public=False` 的）都注册进了 agent——`public=False`
只影响 ACS 可见性，不影响运行时工具集。
于是：任意外部 agent 只要自报一个公开 skillId 就能入场，再用自然语言驱动
非公开的高成本能力（如 scout 的 `scholar.deep_fetch` / `dataset.fetch`）。

## 缓解（协议无法表达"能力选择符"，只能在伙伴侧收窄）

本模块提供一个 run-scoped 钩子，在每次入站 run 开始时把**工具集**收窄为：

    可调用 = 公开能力（public=True，设计上就对外开放）
           ∪ 本任务被准入的 skill（L0 自报命中，或 L2 委托令牌里被授权的 skill）

凡"声明了 `public=False` 但**不在**本任务准入集合里"的能力，直接从
`ToolManager` 移除——模型根本看不见，谈不上被自然语言驱动。

要点：
- **只移除**，不新增；未挂 PEP 的伙伴不受影响（钩子由 `build_pep_hooks` 装配）。
- 只在"本机确实声明了非公开能力"时才动手；全公开的伙伴是空操作。
- 被移除的项会打日志与留痕，便于事后审计。
"""

from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterable

from wit import WitHooks

_ADMITTED_SKILL: ContextVar[str | None] = ContextVar("yanshu_admitted_skill", default=None)

# 代理/内部工具永远保留（不是对外的能力面，例如 leader 侧的本地闸门）
_ALWAYS_KEEP_PREFIXES = ("aip_", "acps_")


def set_admitted_skill(skill: str | None) -> None:
    """由准入钩子在本任务被放行后写入（`pep.py` 的 `build_pep_hook` 调用）。"""
    _ADMITTED_SKILL.set(skill or None)


def current_admitted_skill() -> str | None:
    return _ADMITTED_SKILL.get()


def _capability_of(item: Any) -> Any:
    inner = getattr(item, "capability", None)
    return inner if inner is not None else item


def _identifier(item: Any) -> str:
    cap = _capability_of(item)
    if isinstance(cap, dict):
        return str(cap.get("id") or "")
    return str(getattr(cap, "id", "") or "")


def _is_public(item: Any) -> bool:
    cap = _capability_of(item)
    if isinstance(cap, dict):
        return bool(cap.get("public"))
    return bool(getattr(cap, "public", False))


def _is_internal_tool(item: Any) -> bool:
    """代码级内部工具（如 partner 的 ws_list/ws_read/ws_write）。

    它们不是对外能力面，也不该被 SkillScopedToolsHooks 当"未准入的非公开能力"移除：
    令牌校验在工具内部 fail-closed，作用域由 bundle 的 task_id 固定。
    """
    cap = _capability_of(item)
    meta = cap.get("metadata") if isinstance(cap, dict) else getattr(cap, "metadata", None)
    return isinstance(meta, dict) and bool(meta.get("internal_tool"))


def non_public_ids(capabilities: Iterable[Any] | None) -> frozenset[str]:
    """本机声明里 `public=False` 且**需要按任务准入收窄**的能力 id。"""
    out = set()
    for raw in capabilities or ():
        ident = _identifier(raw)
        if ident and not _is_public(raw) and not _is_internal_tool(raw):
            out.add(ident)
    return frozenset(out)


def resolve_scope(
    *,
    non_public: Iterable[str],
    public_skills: Iterable[str],
    admitted: str | None,
    token_skill: str | None = None,
) -> tuple[frozenset[str], frozenset[str]]:
    """算出 (允许的非公开能力, 需要移除的非公开能力)。纯函数，便于单测。"""
    allowed = {s for s in (admitted, token_skill) if s}
    keeps = frozenset(non_public) & allowed
    drops = frozenset(non_public) - allowed
    # public_skills 只是"设计上对外开放"的声明，收窄只针对非公开能力；
    # 传进来是为了调用方语义清晰（公开能力永不移除）。
    _ = public_skills
    return keeps, drops


class SkillScopedToolsHooks(WitHooks):
    """run-scoped：把不在准入集合里的非公开能力工具从 ToolManager 移除。

    必须注册到 `WitAgent.hooks`（run 生命周期），不能放进 `aip_hooks`——后者只由
    AIP 协议层按 hook 类型分发 admission/transform，`on_run_start` 永远不会被调用
    （2026-10-18 审计 P0-2）。
    """

    id = "yanshu.skill-scope"
    priority = 90          # 早于工具注入（SupervisorRunHooks=70 之后仍可）

    def __init__(self, *, non_public: Iterable[str], public_skills: Iterable[str] = (),
                 token_dir: Path | None = None, audit_path: Path | None = None) -> None:
        self.non_public = frozenset(str(x) for x in non_public)
        self.public_skills = frozenset(str(x) for x in public_skills)
        # 令牌目录必须与写入侧（DelegationTokenScrubHooks 的 _token_dir_for(cfg)）及
        # 业务读取侧（如 partner_experiment 的 WORKSPACE/"tokens"）一致——此前用默认
        # _token_dir()（env/cwd 推断）会"看错抽屉"（2026-10-15 审查 #3 收尾）。
        self.token_dir = token_dir
        self.audit_path = audit_path

    async def on_run_start(self, event) -> None:  # noqa: ANN001
        """run 开始回调：初始化本次运行的钩子状态。"""
        if not self.non_public:
            return
        ctx = getattr(event, "context", None)
        manager = getattr(ctx, "tool_manager", None)
        if manager is None or not hasattr(manager, "remove_tool"):
            return

        token_skill = None
        try:
            # 本任务若带了**有效**的委托令牌，其被授权的 skill 也计入准入集合
            # （L2 路径，签名校验过、不可伪造；与 L0 自报的 admitted 取并集）。
            # peek 只读不消费（consume=False）：令牌要留给真正的业务读取（2026-10-15 审查 #3）。
            from acps_cluster.delegation.client import current_task_id, read_token_file  # noqa: PLC0415
            task_id = current_task_id()
            if task_id is None:
                metadata = getattr(getattr(ctx, "state", None), "metadata", None)
                if isinstance(metadata, dict):
                    task_id = str(
                        metadata.get("taskId") or metadata.get("task_id") or ""
                    ).strip() or None
            tinfo = read_token_file(
                self.token_dir, consume=False, task_id=task_id
            )  # 只读本任务的槽，避免读到上一任务的 latest
            if isinstance(tinfo, dict):
                token_skill = str(tinfo.get("skill_id") or tinfo.get("skillId") or "") or None
        except Exception:  # noqa: BLE001
            pass

        keeps, drops = resolve_scope(
            non_public=self.non_public,
            public_skills=self.public_skills,
            admitted=current_admitted_skill(),
            token_skill=token_skill,
        )
        removed: list[str] = []
        for name in sorted(drops):
            if name.startswith(_ALWAYS_KEEP_PREFIXES):
                continue
            try:
                if manager.remove_tool(name) is not None:
                    removed.append(name)
            except Exception:  # noqa: BLE001
                pass
        if removed:
            print(f"[skill-scope] 已收窄工具集，移除未准入的非公开能力: {removed}"
                  f"（本任务准入={sorted(keeps) or '仅公开'}）", flush=True)
            if self.audit_path is not None:
                try:
                    from acps_cluster.delegation.audit import audit_log  # noqa: PLC0415
                    audit_log(self.audit_path, decision="scope", rule="skill-scoped-tools",
                              extra={"removed": removed, "kept": sorted(keeps)})
                except Exception:  # noqa: BLE001
                    pass
