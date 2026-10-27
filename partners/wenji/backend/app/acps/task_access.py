"""任务级 AMP Access 记录（serve 侧，写独立 journal）。

为什么不再用 ``runtime.amp.emit_access``：

- wit 对每个 AIC 的 journal 做 seal/历史校验，追加前会重新校验全文；
  当同进程存在多个 AmpRuntime 实例（serve 的 + 我们钩子懒取句柄时可能新建的），
  两把进程内锁不互斥，实测 6 小时内 524 次
  ``AMP access append refused a journal changed during validation`` —— 任务级记录被拒，
  平台按 taskId 的配对统计因此缺失。
- 改为用 SDK 的 ``AccessEmitter`` 写**独立文件** ``amp_access_partner_<AIC>.jsonl``：
  不与 wit 自有 journal 争锁，也不参与其 seal 校验；转发器按通配扫描，自动上报。

平台计数口径保持不变：``correlationId = taskId``，一个任务恰好一条；
caller/callee 字段完整（caller=Leader AIC，callee=本机 AIC）。
"""

from __future__ import annotations

import contextvars
import json
import time
from pathlib import Path
from typing import Any

# 当前 AIP 任务的内核会话标识（capabilities 层读取；非 AIP 上下文回退 "aip"）
CURRENT_TASK_SESSION = contextvars.ContextVar("wenji_aip_task_session", default="aip")

try:  # wit 仅在接入层可用时导入；缺失时本模块退化为空实现
    from wit import AipPartnerTransformHooks

    _WIT_AVAILABLE = True
except ImportError:  # pragma: no cover
    _WIT_AVAILABLE = False


class _RuntimeHolder:
    """同进程内的运行时晚期绑定（保留给需要 runtime 的扩展）。"""

    def __init__(self) -> None:
        self._runtime: Any = None

    def set(self, runtime: Any) -> None:
        self._runtime = runtime

    def get(self) -> Any:
        return self._runtime


RUNTIME_HOLDER = _RuntimeHolder()

# 任务起点（monotonic），用于响应时计算真实时长；上限防泄漏
_TASK_T0: dict[str, float] = {}
_TASK_T0_MAX = 512

_STABLE_STATES = {
    "awaiting-input",
    "awaiting-completion",
    "completed",
    "failed",
    "rejected",
    "canceled",
}

_STATE_TO_HTTP = {
    "failed": 500,
    "rejected": 403,
    "canceled": 499,
}

# 独立 journal（forwarder 的 amp_access_*.jsonl 通配会扫到）
_EMITTER = None
_EMITTER_PATH: Path | None = None


def _enabled() -> bool:
    import os

    return os.environ.get("ACPS_TASK_ACCESS_ENABLED", "1").strip().lower() not in {
        "0",
        "false",
        "off",
    }


def _load_aic(workspace: Path) -> str:
    """从 workspace 的最终 ACS 读取真实 AIC（AMP 记录的 aic 字段必须是 AIC）。"""
    try:
        final = json.loads((workspace / "acps" / "acs.final.json").read_text(encoding="utf-8"))
        return str(final.get("aic") or "")
    except (OSError, json.JSONDecodeError):
        return ""


def _emitter(workspace: Path, aic: str):
    """惰性构造 partner 侧 AccessEmitter（独立文件）。"""
    global _EMITTER, _EMITTER_PATH
    if _EMITTER is not None:
        return _EMITTER
    from acps_sdk.amp.access_emitter import AccessEmitter

    logs = workspace / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / f"amp_access_partner_{aic}.jsonl"
    _EMITTER = AccessEmitter(path, aic=aic, resource={"service.name": "wenji-aip-agent"})
    _EMITTER_PATH = path
    return _EMITTER


if _WIT_AVAILABLE:

    class TaskAccessHooks(AipPartnerTransformHooks):
        """按平台口径补发任务级 access（correlation_id=taskId），注册于 aip_hooks。"""

        async def on_partner_request(self, event: Any) -> None:  # type: ignore[override]
            task_id = str(getattr(event, "task_id", "") or "")
            if task_id:
                CURRENT_TASK_SESSION.set(f"aip-{task_id}")
            if not _enabled() or not task_id:
                return
            command = getattr(event, "command", None)
            command = getattr(command, "value", command)  # start | continue
            if str(command) != "start":
                return
            if len(_TASK_T0) >= _TASK_T0_MAX:
                oldest = min(_TASK_T0, key=_TASK_T0.get)  # type: ignore[arg-type]
                _TASK_T0.pop(oldest, None)
            _TASK_T0[task_id] = time.monotonic()

        async def on_partner_response(self, event: Any) -> None:  # type: ignore[override]
            if not _enabled():
                return
            task_id = str(getattr(event, "task_id", "") or "")
            t0 = _TASK_T0.pop(task_id, None)
            if t0 is None:
                return
            state = getattr(getattr(event, "state", None), "value", None) or str(
                getattr(event, "state", "")
            )
            if state not in _STABLE_STATES:
                _TASK_T0[task_id] = t0  # working 等中间态：等首个稳定态
                return
            runtime = RUNTIME_HOLDER.get()
            if runtime is None:
                return
            try:
                from acps_sdk.amp.models import (
                    AccessBody,
                    AccessParticipant,
                    AccessRequest,
                    AccessResponse,
                )

                ws = Path(runtime.settings.workspace)
                aic = _load_aic(ws)
                if not aic:
                    return
                emitter = _emitter(ws, aic)
                body = AccessBody(
                    request=AccessRequest(method="aip.task", url="acps.direct.rpc", route="/rpc"),
                    response=AccessResponse(statusCode=_STATE_TO_HTTP.get(state, 200)),
                    caller=AccessParticipant(aic=event.leader_aic),
                    callee=AccessParticipant(aic=event.partner_aic, serviceName="wenji-aip-agent"),
                    durationMs=float(int((time.monotonic() - t0) * 1000)),
                )
                await emitter.emit(body, correlation_id=task_id)
            except Exception as exc:  # 日志失败绝不影响任务主流程
                import sys

                print(f"[task_access] emit 失败：{type(exc).__name__}: {exc}", file=sys.stderr)

else:  # pragma: no cover

    class TaskAccessHooks:  # type: ignore[no-redef]
        """wit 不可用时的空实现。"""

        async def on_partner_request(self, event: Any) -> None:  # type: ignore[override]
            return None

        async def on_partner_response(self, event: Any) -> None:  # type: ignore[override]
            return None
