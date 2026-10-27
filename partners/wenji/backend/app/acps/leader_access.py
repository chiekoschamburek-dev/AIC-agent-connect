"""Leader 侧任务级 AMP Access 记录（与 serve 侧 TaskAccessHooks 配对）。

平台计数口径（梧桐平台使用指南）：

- 平台按 ``correlationId == taskId`` 对 **双方** 的 access 记录做配对，
  配对成功才算 1 次"调用 / 被调用"；
- 只写一侧（仅被调方）时，榜单可能统计不到。

因此本模块在 Leader（流量器 / 自测脚本）侧，按任务写入一条 access：

- ``correlation_id = task_id``
- ``caller.aic = 本机 AIC``、``callee.aic = Partner AIC``
- ``durationMs`` 为任务往返耗时，``response.statusCode`` 按终态映射

写入**独立 journal 文件**（``amp_access_leader_<AIC>.jsonl``）：
wit 对每个 AIC 自己的 journal 做 seal 校验，跨进程写同一文件会报
``journal changed during validation``；独立文件既避免冲突，又能被
``tools/amp_forwarder`` 的通配扫描自动上报。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

_STATE_TO_HTTP = {
    "completed": 200,
    "awaiting-completion": 200,
    "awaiting-input": 200,
    "rejected": 403,
    "canceled": 499,
    "failed": 500,
}


def leader_access_emitter(workspace: Path, aic: str):
    """构造写入独立 journal 的 AccessEmitter（目录不存在则创建）。"""
    from acps_sdk.amp.access_emitter import AccessEmitter

    logs = workspace / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / f"amp_access_leader_{aic}.jsonl"
    return AccessEmitter(path, aic=aic, resource={"service.name": "wenji-aip-leader"})


async def emit_leader_access(
    emitter,
    *,
    task_id: str,
    state: str,
    duration_ms: float,
    partner_aic: str,
    partner_url: str,
    trace_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """写一条 caller 侧任务级 access（失败不影响业务）。"""
    if emitter is None or not task_id:
        return
    try:
        from acps_sdk.amp.models import (
            AccessBody,
            AccessParticipant,
            AccessRequest,
            AccessResponse,
        )

        body = AccessBody(
            request=AccessRequest(method="aip.task", url=partner_url, route="/rpc"),
            response=AccessResponse(statusCode=_STATE_TO_HTTP.get(state, 200)),
            caller=AccessParticipant(aic=emitter_aic(emitter), serviceName="wenji-aip-leader"),
            callee=AccessParticipant(aic=partner_aic, serviceName="wenji-aip-agent"),
            durationMs=float(duration_ms),
        )
        # 注意：AccessEmitter.emit 只接受 trace_* 与 correlation_id（无 attributes）
        await emitter.emit(body, correlation_id=task_id, trace_id=trace_id)
    except Exception:
        pass


def emitter_aic(emitter) -> str:
    """AccessEmitter 未公开 aic 属性，从私有字段读取（SDK 2.2.0）。"""
    return getattr(emitter, "_aic", "") or os.environ.get("ACPS_AIC", "")
