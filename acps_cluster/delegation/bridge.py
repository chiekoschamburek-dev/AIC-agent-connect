"""capability→runtime 桥：伙伴在 inbound 任务执行中发起出站委托调用的通道（阶段三收尾）。

三个断点、三根线：
1. 受托令牌进了 `event.local_context`（host-only），但 capability 函数签名里没有 local_context
   —— 由 `DelegationTokenScrubHooks` 受托工分支在落 local_context 的同时写入 ContextVar
   （transform 与业务 run 在同一执行链上，asyncio 任务创建时复制上下文，先设后建即贯通）；
2. capability 拿不到 runtime/state（aip_delegate 需要）—— `BridgeHooks.on_run_start` 从
   `event.context`（wit 契约：runtime + state）捕获，同样进 ContextVar；
3. 组装 —— `delegated_fetch()`：能力处理器内一行出站，自动取"本任务收到的令牌 +
   本 run 的 runtime"，JWS 全程不进模型上下文（与句柄方案同一纪律）。

无令牌/无桥时返回结构化错误（对齐 awaiting-input 纪律：报缺什么），不抛异常。
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Mapping

from wit import WitHooks

_DELEGATION: ContextVar[Mapping[str, str] | None] = ContextVar("yanshu_delegation", default=None)
_RUN: ContextVar["RunHandle | None"] = ContextVar("yanshu_run", default=None)


@dataclass(frozen=True)
class RunHandle:
    runtime: Any
    state: Any


def set_delegation(delegation: Mapping[str, str] | None) -> None:
    """由受托工分支（client.py）调用：本任务收到的委托令牌（host-only，绝不进提示词）。"""
    _DELEGATION.set(dict(delegation) if delegation else None)


def current_delegation() -> dict[str, str] | None:
    """本任务收到的委托令牌信息：{token, targetAic, skillId}；无则 None。"""
    value = _DELEGATION.get()
    return dict(value) if value else None


def current_runtime() -> Any | None:
    return _RUN.get().runtime if _RUN.get() else None


def current_state() -> Any | None:
    return _RUN.get().state if _RUN.get() else None


def bridge_ready() -> bool:
    return _RUN.get() is not None


class BridgeHooks(WitHooks):
    """on_run_start 捕获 runtime/state 进 ContextVar（能力处理器经 current_* 取用）。"""

    id = "yanshu.runtime-bridge"
    priority = 90

    async def on_run_start(self, event) -> None:
        ctx = event.context
        _RUN.set(RunHandle(runtime=getattr(ctx, "runtime", None), state=getattr(ctx, "state", None)))


async def delegated_fetch(request: str, *, skill_id: str | None = None,
                          target_aic: str | None = None) -> dict[str, Any]:
    """能力处理器内的一行出站：持本任务收到的委托令牌直连目标伙伴（L2）。

    返回 direct_fetch 的任务快照；缺件（无令牌/无 runtime/无目标）返回
    {"ok": False, "awaiting": {...}} 结构化报缺——不抛异常、不编造。
    """
    from .client import direct_fetch

    delegation = current_delegation()
    handle = _RUN.get()
    need = {
        "need": "delegation.outbound",
        "how": "leader 用 continue 附带委托令牌（remedy-3），或本机挂 BridgeHooks",
        "blocking": True,
    }
    if handle is None or handle.runtime is None or handle.state is None:
        return {"ok": False, "awaiting": {**need, "reason": "桥未就绪（本 run 未挂 BridgeHooks）"}}
    if delegation is None:
        return {"ok": False, "awaiting": {**need, "reason": "本任务未收到委托令牌"}}
    target = target_aic or delegation.get("targetAic") or ""
    if not target:
        return {"ok": False, "awaiting": {**need, "reason": "令牌未携带目标 AIC 且未显式指定"}}
    snapshot = await direct_fetch(
        handle.runtime, handle.state, target_aic=target, request=request,
        token=delegation["token"], skill_id=skill_id or delegation.get("skillId") or "",
    )
    return {"ok": True, **({k: v for k, v in snapshot.items() if k != "ok"} if isinstance(snapshot, dict) else {})}


__all__ = [
    "BridgeHooks",
    "RunHandle",
    "bridge_ready",
    "current_delegation",
    "current_runtime",
    "current_state",
    "delegated_fetch",
    "set_delegation",
]
