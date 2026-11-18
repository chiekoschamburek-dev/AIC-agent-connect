"""根任务服务：受理、幂等重放、状态映射与结果取件（纯标准库）。

分工：

- `root_store` 只管台账与状态机；
- 本模块把"根任务"与"后台作业"接起来：受理 = 建根任务 + 拉起作业；查询 = 读作业状态、
  映射成业务状态、按需取终稿；
- **拉起作业的实现由调用方注入**（supervisor 的 `submit_job` 工具），因此本模块不依赖
  wit / supervisor，可离线单测。

状态映射（v1，只映射 `jobs_store` 现有的四个状态）：

    queued → accepted（已受理，等待执行）
    running → running
    done → completed
    failed / interrupted → failed

`needs_input / cancelled / expired` 在台账里已预留，P1 不产生（取消/澄清是 P4）。
"""

from __future__ import annotations

import asyncio
import inspect
import os
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from acps_cluster.runtime import jobs_store
from acps_cluster.runtime.root_store import (
    InvalidTransition,
    RequestConflict,
    RootStore,
    RootTask,
)

JOB_TO_ROOT: dict[str, str] = {
    "queued": "accepted",
    "running": "running",
    "done": "completed",
    "failed": "failed",
    "interrupted": "failed",
}

NEXT_ACTION: dict[str, str] = {
    "accepted": "已受理，等待执行；稍后用 research.status 查询",
    "running": "执行中；稍后用 research.status 查询（不要重复提交）",
    "completed": "已完成；result 字段是终稿（超过一页请用 result_chars/truncated 判断）",
    "failed": "失败；error 字段是原因。需要重试请换一个新的 request_id",
    "cancelled": "已取消",
    "expired": "已过期；结果未在期限内完成",
}

SubmitFn = Callable[[str, str], Mapping[str, Any] | Awaitable[Mapping[str, Any]]]

#: 当前 run 正在受理的根任务号（只在本进程/本请求上下文内有效）。
#: supervisor 的收尾逻辑用它判断"这是入口受理 run"，从而提交 fastpath 的 JSON 终稿，
#: 而不是把它替换成代码生成的作业回执。
_CURRENT_ROOT: ContextVar[str | None] = ContextVar("yanshu_root_task", default=None)


def current_root_id() -> str | None:
    return _CURRENT_ROOT.get()


def _error(code: str, detail: str, **extra: Any) -> dict[str, Any]:
    payload = {"ok": False, "error": code, "detail": detail}
    payload.update(extra)
    return payload


def _submit_payload(root: RootTask, *, replayed: bool, recovered: bool = False) -> dict[str, Any]:
    ok = bool(root.job_id) and root.state in {"accepted", "running", "completed"}
    payload: dict[str, Any] = {
        "ok": ok,
        "root_id": root.root_id,
        "request_id": root.request_id,
        "source_task_id": root.source_task_id,
        "source_caller": root.source_caller,
        "source_session": root.source_session,
        "state": root.state,
        "job_id": root.job_id,
        "mode": root.mode,
        "created_at": root.created_at,
        "replayed": bool(replayed),
        "recovered": bool(recovered),
        "note": "受理回执：任务已受理，不代表已完成；用 research.status 查询进度与结果。",
    }
    if not ok:
        payload["error"] = root.state
        payload["detail"] = root.error
    return payload


def _dispatch_wait_seconds() -> float:
    """并发重试等待派发完成的上限（秒）。

    用户连点/平台重试会在同一根任务上并发调用 `research.submit`；此前第二个请求
    立刻拿到 `dispatch-in-progress`（ok=false），叮当侧表现为“受理失败”。
    这里改为等第一个派发把 job_id 写进台账，再作为重放返回。
    """
    try:
        return max(0.0, float(os.getenv("ACPS_DISPATCH_WAIT_SECONDS", "10")))
    except ValueError:
        return 10.0


async def submit_root(
    store: RootStore,
    submit_fn: SubmitFn,
    *,
    entry_aic: str,
    user_scope: str,
    request_id: str,
    question: str,
    mode: str = "custom",
    source_task_id: str = "",
    source_caller: str = "",
    source_session: str = "",
) -> dict[str, Any]:
    """幂等受理一条根任务：建台账 → 拉起作业 → 绑定 job_id。

    - 同幂等键同内容：直接返回原任务（`replayed=True`），**不再拉起作业**；
    - 同幂等键不同内容：返回 `request-conflict`，不覆盖；
    - 已受理但没绑上作业（崩溃窗口）：重放时补拉起一次（仍受幂等键保护）。
    """
    try:
        root, created = store.submit(
            entry_aic=entry_aic,
            user_scope=user_scope,
            request_id=request_id,
            question=question,
            mode=mode,
            source_task_id=source_task_id,
            source_caller=source_caller,
            source_session=source_session,
        )
    except RequestConflict as exc:
        return _error("request-conflict", str(exc))
    except ValueError as exc:
        return _error("invalid-request", str(exc))

    if root.job_id or root.terminal:
        return _submit_payload(root, replayed=True)

    lock_root = store.path.with_name(store.path.name + ".dispatch")
    lock_root.mkdir(exist_ok=True)
    lock_path = lock_root / root.root_id
    deadline = time.monotonic() + _dispatch_wait_seconds()
    while True:
        with jobs_store.file_lock(lock_path, blocking=False) as locked:
            if locked:
                # A different request may have attached a job before we took the lock.
                latest = store.get(root.root_id)
                if latest is None:
                    return _error("unknown-root", "根任务记录已不存在")
                if latest.job_id or latest.terminal:
                    return _submit_payload(latest, replayed=True)
                return await _dispatch_job(store, latest, submit_fn, replayed=not created)
        # 另一个请求正在派发：等它把 job_id 写进台账，再按重放返回。
        # 超过等待上限才如实报“进行中”，避免把并发重试变成一次失败受理。
        if time.monotonic() >= deadline:
            return _error("dispatch-in-progress", "正在启动作业，请稍后查询或用相同 request_id 重试。",
                          root_id=root.root_id, state="accepted", replayed=True)
        await asyncio.sleep(0.05)


async def _dispatch_job(store: RootStore, root: RootTask, submit_fn: SubmitFn,
                        *, replayed: bool) -> dict[str, Any]:
    token = _CURRENT_ROOT.set(root.root_id)
    try:
        result = submit_fn(root.question, root.mode)
        if inspect.isawaitable(result):
            result = await result
    except Exception as exc:  # noqa: BLE001 - 受理失败必须如实落台账
        detail = f"{type(exc).__name__}: {exc}"
        _mark_failed(store, root.root_id, detail)
        return _error("submit-failed", detail, root_id=root.root_id, state="failed")
    finally:
        _CURRENT_ROOT.reset(token)

    if not isinstance(result, Mapping):
        detail = f"submit_fn 返回非映射类型：{type(result).__name__}"
        _mark_failed(store, root.root_id, detail)
        return _error("submit-failed", detail, root_id=root.root_id, state="failed")

    job_id = str(result.get("job_id") or "").strip()
    if not result.get("ok") or not job_id:
        detail = str(result.get("error") or "作业未受理（submit_fn 未返回 job_id）")
        _mark_failed(store, root.root_id, detail)
        return _error("submit-failed", detail, root_id=root.root_id, state="failed")

    try:
        root = store.attach_job(root.root_id, job_id)
    except (KeyError, ValueError) as exc:
        return _error("attach-failed", f"绑定作业失败：{exc}", root_id=root.root_id,
                      state="accepted")
    return _submit_payload(root, replayed=replayed, recovered=replayed)


def _mark_failed(store: RootStore, root_id: str, detail: str) -> None:
    try:
        store.set_state(root_id, "failed", error=detail[:2000])
    except (KeyError, InvalidTransition, ValueError):
        pass


def _job_state(root: RootTask, jobs_root: Path) -> tuple[str, str, str]:
    """返回 (job_state, report_text, job_error)。找不到作业时 job_state 为空。"""
    if not root.job_id:
        return "", "", ""
    job_dir = jobs_store.find_job(Path(jobs_root), root.job_id)
    if job_dir is None:
        return "", "", "job-not-found"
    status = jobs_store.resolve_liveness(job_dir)
    job_state = str(status.get("state") or "")
    error = ""
    if job_state in {"failed", "interrupted"}:
        error = str(status.get("run_status") or status.get("local_error") or "")
    report = ""
    if job_state == "done":
        text = jobs_store.job_report(job_dir) or ""
        page = max(1, jobs_store.report_page_chars())
        report = text[:page]
    return job_state, report, error


def status_root(
    store: RootStore,
    *,
    root_id: str = "",
    job_id: str = "",
    source_task_id: str = "",
    caller_aic: str,
    jobs_root: str | Path,
) -> dict[str, Any]:
    """查询根任务：业务状态 + 作业状态 + 终稿（截断一页）。

    调用方只拿到作业号（例如受理回执里的 `job-…`）时，可用 `job_id` 反查根任务。
    """
    root = store.get(str(root_id or "").strip()) if str(root_id or "").strip() else None
    if root is None and str(job_id or "").strip():
        root = store.get_by_job_id(str(job_id).strip())
    if root is None and str(source_task_id or "").strip():
        root = store.get_by_source_task_id(source_task_id, caller_aic=caller_aic)
        if root is None and store.get_by_source_task_id(source_task_id) is not None:
            return _error("forbidden", "只有原入口调用方或原任务发起方可以查询该根任务")
    if root is None:
        if root_id:
            detail = f"没有这个根任务：{root_id!r}"
        elif job_id:
            detail = f"没有这个作业对应的根任务：{job_id!r}"
        else:
            detail = f"没有这个原任务对应的根任务：{source_task_id!r}"
        return _error("unknown-root", detail)
    if not caller_aic or caller_aic not in {root.entry_aic, root.source_caller}:
        return _error("forbidden", "只有原入口调用方或原任务发起方可以查询该根任务")

    job_state, report, job_error = ("", "", "")
    if root.job_id:
        job_state, report, job_error = _job_state(root, Path(jobs_root))
        mapped = JOB_TO_ROOT.get(job_state)
        if mapped and mapped != root.state:
            try:
                root = store.set_state(
                    root.root_id,
                    mapped,
                    error=job_error[:2000] if mapped == "failed" else None,
                )
            except (InvalidTransition, KeyError):
                root = store.get(root.root_id) or root

    result_chars = len(report)
    page = max(1, jobs_store.report_page_chars())
    return {
        "ok": True,
        "root_id": root.root_id,
        "request_id": root.request_id,
        "state": root.state,
        "job_id": root.job_id,
        "job_state": job_state,
        "mode": root.mode,
        "created_at": root.created_at,
        "updated_at": root.updated_at,
        "report_available": bool(report),
        "result": report,
        "result_chars": result_chars,
        "truncated": result_chars >= page,
        "error": root.error or job_error,
        "next": NEXT_ACTION.get(root.state, ""),
    }


__all__ = [
    "JOB_TO_ROOT",
    "NEXT_ACTION",
    "SubmitFn",
    "status_root",
    "submit_root",
]
