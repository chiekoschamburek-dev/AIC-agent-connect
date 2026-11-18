"""Supervisor 入口能力（多入口编排 P1）：`research.submit` / `research.status`。

两个能力都是**非公开**（`public=False`，不进 ACS → 不换 AIC），只对持有授权凭据的
调用方开放（PEP 的 grant / 令牌路径）。它们由入站技能注册表直接执行，**不经过模型**：

- `research.submit`：幂等受理一条根任务 → 建台账 → 拉起后台作业 → 返回根任务号/作业号；
  受理即返回，不代表完成；
- `research.status`：查业务状态与终稿（只对原入口 AIC 开放）。

调用方身份来自 `AipPartnerTransformHooks` 事件里的**已验证** `leader_aic`，不是正文自报的
字段；未验证时 fail-closed（拒绝受理）。平台 taskId 另记在台账里，供 P5 证据关联。

依赖边界：本模块依赖 wit（`@capability` 装饰器）与 supervisor 的 `submit_job` 工具，
所以由 supervisor 在装配时导入；台账/服务层（`root_store` / `root_service`）是纯标准库。
"""

from __future__ import annotations

import os
import re
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable

from wit import capability

from acps_cluster.delegation.skill_scope import current_admitted_skill
from acps_cluster.runtime import root_service, root_store

_CLUSTER = Path(__file__).resolve().parents[1]  # acps_cluster/

#: (已验证调用方 AIC, 平台 taskId)；每个入站请求都要重置，避免跨请求继承
_ENTRY: ContextVar[tuple[str | None, str | None]] = ContextVar(
    "yanshu_root_entry", default=(None, None)
)
#: 本 run 是否是入口受理（research.submit 快速通道）——supervisor 收尾据此保留
#: fastpath 的 JSON 终稿，而不是用代码生成的作业回执把它顶掉。
_ENTRY_RUN: ContextVar[bool] = ContextVar("yanshu_entry_run", default=False)


def mark_entry_run() -> None:
    _ENTRY_RUN.set(True)


def entry_run_active() -> bool:
    return bool(_ENTRY_RUN.get())


def clear_entry_run() -> None:
    _ENTRY_RUN.set(False)


def set_current_caller(aic: str | None, task_id: str | None = None) -> None:
    _ENTRY.set((str(aic) if aic else None, str(task_id) if task_id else None))


def current_caller_aic() -> str | None:
    return _ENTRY.get()[0]


def current_platform_task() -> str | None:
    return _ENTRY.get()[1]


def build_entry_hooks():
    """构造入站身份钩子：把**已验证**的调用方 AIC 与平台 taskId 写进 ContextVar。"""
    from wit import AipPartnerTransformHooks  # noqa: PLC0415

    class _RootEntryHooks(AipPartnerTransformHooks):
        id = "yanshu.root-entry"
        priority = 45

        async def on_partner_request(self, event) -> None:  # noqa: ANN001
            peer = str(getattr(event, "leader_aic", "") or "").strip()
            verified = bool(getattr(event, "peer_verified", False))
            task_id = str(getattr(event, "task_id", "") or "").strip()
            # 每个请求都重置：未验证一律 None（fail-closed），防止上一个请求的身份被继承
            set_current_caller(peer if verified and peer else None, task_id or None)

    return _RootEntryHooks()


# ─────────────────────────── 台账 / 作业句柄 ───────────────────────────


def store_path() -> Path:
    """根任务台账位置：`ACPS_ROOT_STORE` 优先，默认常驻 supervisor 工作区。"""
    override = os.getenv("ACPS_ROOT_STORE", "").strip()
    if override:
        return Path(override)
    return _CLUSTER / "ws-supervisor" / "root-tasks.sqlite3"


def get_store() -> root_store.RootStore:
    return root_store.RootStore(store_path())


def jobs_root() -> Path:
    """作业根目录：与 supervisor `_jobs_root()` 保持同一口径（`__file__` 派生，不用 WORKSPACE）。"""
    from acps_cluster.agents import supervisor  # noqa: PLC0415

    return supervisor._jobs_root()


_SUBMIT_TOOL: Any = None


def _submit_tool():
    """缓存一个 `submit_job` 工具实例：复用它的并发上限/清理/日志 FD 处理。"""
    global _SUBMIT_TOOL
    if _SUBMIT_TOOL is None:
        from acps_cluster.agents import supervisor  # noqa: PLC0415

        _SUBMIT_TOOL = supervisor._make_submit_job_tool()
    return _SUBMIT_TOOL


async def default_submit(question: str, mode: str) -> dict[str, Any]:
    """默认受理实现：调用 supervisor 的 `submit_job` 工具（受理-作业-取件交路）。

    `submit_job` 用 `_JOB_OWNER` 记录"谁受理的作业"。正常情况下 `JobOwnerHooks` 已经
    把已验证调用方写进去了；这里再兜一层（仅当为空时），避免钩子顺序调整后受理被
    误判成"匿名作业"而 fail-closed。
    """
    handler: Callable | None = getattr(_submit_tool(), "handler", None)
    if handler is None:
        raise RuntimeError("submit_job 工具没有 handler，无法拉起后台作业")

    from acps_cluster.agents import supervisor  # noqa: PLC0415

    token = None
    caller = current_caller_aic()
    if caller and supervisor._JOB_OWNER.get() is None:  # noqa: SLF001 - 同一进程内的受信兜底
        token = supervisor._JOB_OWNER.set(caller)  # noqa: SLF001
    try:
        result = await handler({"question": question, "mode": mode})
    finally:
        if token is not None:
            supervisor._JOB_OWNER.reset(token)  # noqa: SLF001
    return result if isinstance(result, dict) else {"ok": False, "error": "submit_job 返回非字典"}


# ─────────────────────────── 能力实现（纯逻辑） ───────────────────────────


_AIC_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{5,255}$")


def _valid_aic(value: str) -> str:
    text = str(value or "").strip()
    return text if _AIC_RE.fullmatch(text) else ""


async def submit_core(
    question: str,
    request_id: str = "",
    user_scope: str = "default",
    mode: str = "custom",
    source_task_id: str = "",
    source_caller: str = "",
    source_session: str = "",
    *,
    store: root_store.RootStore | None = None,
    submit_fn: Callable | None = None,
    caller_aic: str | None = None,
    platform_task_id: str | None = None,
) -> dict[str, Any]:
    caller = caller_aic if caller_aic is not None else current_caller_aic()
    if not caller:
        return {
            "ok": False,
            "error": "unverified-caller",
            "detail": "受理需要已验证的调用方身份（mTLS + peer_verified）；不接受正文自报。",
        }
    ledger = store or get_store()
    # 根任务归属仍是 mTLS 已验证的调用方；source_caller 只作为来源 metadata，
    # 不能凭它改归属（否则任何伙伴都能伪造 owner）。原发起方查询时按 source_caller 放行。
    payload = await root_service.submit_root(
        ledger,
        submit_fn or default_submit,
        entry_aic=caller,
        user_scope=str(user_scope or "default"),
        request_id=str(request_id or ""),
        question=str(question or ""),
        mode=str(mode or "custom"),
        source_task_id=str(source_task_id or ""),
        source_caller=_valid_aic(source_caller),
        source_session=str(source_session or "")[:200],
    )
    task_id = platform_task_id if platform_task_id is not None else current_platform_task()
    root_id = str(payload.get("root_id") or "")
    if root_id and task_id:
        try:
            ledger.set_platform_task(root_id, task_id)
        except (KeyError, ValueError, OSError):
            pass
    return payload


async def status_core(
    root_id: str = "",
    *,
    job_id: str = "",
    source_task_id: str = "",
    store: root_store.RootStore | None = None,
    jobs_root_path: str | Path | None = None,
    caller_aic: str | None = None,
    platform_task_id: str | None = None,
) -> dict[str, Any]:
    caller = caller_aic if caller_aic is not None else current_caller_aic()
    if not caller:
        return {
            "ok": False,
            "error": "unverified-caller",
            "detail": "查询需要已验证的调用方身份。",
        }
    ledger = store or get_store()
    jobs_dir = Path(jobs_root_path) if jobs_root_path is not None else jobs_root()
    payload = root_service.status_root(
        ledger,
        root_id=str(root_id or ""),
        job_id=str(job_id or ""),
        source_task_id=str(source_task_id or ""),
        caller_aic=caller,
        jobs_root=jobs_dir,
    )
    return payload


def _allows_entry_skill(skill: str) -> bool:
    pep_enabled = os.getenv("ACPS_PEP_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
    return not pep_enabled or current_admitted_skill() == skill


#: 入站注册表用的薄包装：只暴露领域参数。
async def handle_submit(
    question: str = "",
    request_id: str = "",
    user_scope: str = "default",
    mode: str = "custom",
    source_task_id: str = "",
    source_caller: str = "",
    source_session: str = "",
) -> dict:
    mark_entry_run()
    if not _allows_entry_skill("research.submit"):
        return {"ok": False, "error": "forbidden", "detail": "当前任务未获得 research.submit 授权。"}
    return await submit_core(question, request_id, user_scope, mode, source_task_id, source_caller, source_session)


async def handle_status(root_id: str = "", job_id: str = "", source_task_id: str = "") -> dict:
    mark_entry_run()
    if not _allows_entry_skill("research.status"):
        return {"ok": False, "error": "forbidden", "detail": "当前任务未获得 research.status 授权。"}
    return await status_core(root_id, job_id=job_id, source_task_id=source_task_id)


# ─────────────────────────── AIP 能力声明（非公开） ───────────────────────────


@capability(
    id="research.submit",
    description=(
        "受理入口 Partner 提交的科研需求：按（调用方 AIC + user_scope + request_id）幂等受理，"
        "返回根任务号与作业号；受理即返回，不代表任务完成。需要有效授权凭据。"
    ),
    public=False,
    model_callable=False,
    version="1.0.0",
    tags=["入口", "编排", "受理"],
    # 不设 acps_skill：非公开能力不进 ACS（设了就会投影进平台声明、换指纹/换 AIC）
)
async def research_submit(
    question: str = "",
    request_id: str = "",
    user_scope: str = "default",
    mode: str = "custom",
    source_task_id: str = "",
    source_caller: str = "",
    source_session: str = "",
) -> dict:
    """受理一条根任务（幂等）。"""
    return await handle_submit(question, request_id, user_scope, mode, source_task_id, source_caller, source_session)


@capability(
    id="research.status",
    description=(
        "查询根任务的业务状态与终稿（只对原入口调用方开放；root_id / job_id / source_task_id 三选一）。"
        "返回 state / job_state / result / truncated；查询不触发新的编排。"
    ),
    public=False,
    model_callable=False,
    version="1.0.0",
    tags=["入口", "查询", "状态"],
    # 同上：不设 acps_skill
)
async def research_status(root_id: str = "", job_id: str = "", source_task_id: str = "") -> dict:
    """查询根任务状态与结果（root_id / job_id / source_task_id 三选一）。"""
    return await handle_status(root_id, job_id, source_task_id)


#: 入站技能注册表（supervisor 的快速通道直接执行，不跑模型）
HANDLERS: dict[str, Callable[..., Any]] = {
    "research.submit": handle_submit,
    "research.status": handle_status,
}


__all__ = [
    "HANDLERS",
    "clear_entry_run",
    "entry_run_active",
    "mark_entry_run",
    "build_entry_hooks",
    "current_caller_aic",
    "current_platform_task",
    "default_submit",
    "get_store",
    "handle_status",
    "handle_submit",
    "jobs_root",
    "research_status",
    "research_submit",
    "set_current_caller",
    "status_core",
    "store_path",
    "submit_core",
]
