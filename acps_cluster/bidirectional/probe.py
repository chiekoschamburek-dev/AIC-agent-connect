"""出站探针：以本机工作区身份真实发起一次 AIP 任务并产出回执。

为什么单独写一层而不是直接调 `runtime.aip_delegate`：探针要能**在没有模型、没有
wit 运行时**的机器上跑（验证脚本、CI、对端服务器），也要能在离线单测里用假客户端
驱动整个状态机。所以这里只依赖 `acps_sdk.aip.AipRpcClient`（纯客户端 + mTLS），
并且把客户端做成可注入：

    result = await run_probe(cfg)                     # 真实：acps_sdk + mTLS
    result = await run_probe(cfg, client=FakeClient()) # 单测：假客户端

技能指名走 AIP v2 的 `TaskCommand.commandParams`：

    {"skillId": "research.orchestrate", "args": {"task": "..."}}

`AipRpcClient.start_task()` 不暴露 `commandParams`，因此探针自己组装 `TaskCommand`
再走同一条 `_send_request`（私有方法，缺失时退回 `start_task`，只丢技能指名不丢任务）。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence

from .identity import WorkspaceIdentity, peer_endpoint_from_local_workspaces
from .models import SCHEMA_PROBE, ProductSummary, TaskReceipt, normalize_state

TERMINAL_STATES = frozenset({"completed", "failed", "rejected", "canceled", "cancelled", "expired"})

#: 线上格式与 `delegation.token` 一致（不 import 该模块：它依赖 cryptography，
#: 而探针要在没有 crypto 依赖的机器上也能只做连通性验证）
TOKEN_DATA_KEY = "acpsDelegationToken"
TOKEN_ROLE = "delegation-token"


def read_grant_token(path: str | Path) -> str:
    """读 grant 文件：支持 `{"token": "<JWS>"}` 或整份就是 JWS 的文本。"""
    text = Path(path).read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"grant 文件为空：{path}")
    if text.startswith("{"):
        data = json.loads(text)
        token = str((data or {}).get("token") or "").strip()
    else:
        token = text
    if token.count(".") != 2:
        raise ValueError(f"grant 文件里没有 compact JWS：{path}")
    return token


def grant_data_item(token: str, *, target_aic: str, skill_id: str) -> dict[str, Any]:
    """AIP StructuredDataItem：把 grant 交给目标 PEP（与 §3.2 委托令牌同通道）。"""
    return {
        "type": "data",
        "data": {
            TOKEN_DATA_KEY: str(token),
            "targetAic": str(target_aic),
            "targetSkillHint": str(skill_id),
            "usage": "持此域授权向目标伙伴发起任务，勿回传给 leader",
        },
        "metadata": {"role": TOKEN_ROLE},
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _state_of(task: Any) -> str:
    status = getattr(task, "status", None)
    value = getattr(status, "state", None) if status is not None else None
    return normalize_state(getattr(value, "value", value))


def _products_json(task: Any) -> Any:
    products = getattr(task, "products", None)
    if not products:
        return None
    out: list[Any] = []
    for product in products:
        if hasattr(product, "model_dump"):
            out.append(product.model_dump(mode="json"))
        else:
            out.append(product)
    return out


def _product_previews(task: Any, *, limit: int = 3, chars: int = 2000) -> list[dict[str, str]]:
    """产物正文预览（只取 text 型 dataItem）：调用方要从回执里取 root_id/job_id 等。"""
    products = getattr(task, "products", None) or []
    previews: list[dict[str, str]] = []
    for product in list(products)[:max(1, int(limit))]:
        if isinstance(product, Mapping):
            product_id = str(product.get("id") or "")
            name = str(product.get("name") or "")
            items = product.get("dataItems") or product.get("data_items") or []
        else:
            product_id = str(getattr(product, "id", "") or "")
            name = str(getattr(product, "name", "") or "")
            items = getattr(product, "dataItems", None) or []
        texts: list[str] = []
        for item in items:
            if isinstance(item, Mapping):
                text = item.get("text")
            else:
                text = getattr(item, "text", None)
            if text:
                texts.append(str(text))
        previews.append({
            "id": product_id,
            "name": name,
            "text": "\n".join(texts)[: max(0, int(chars))],
        })
    return previews


@dataclass
class ProbeConfig:
    """一次探针的全部输入（不含凭据；凭据从 `identity` 现取）。"""

    identity: WorkspaceIdentity
    peer_aic: str
    peer_endpoint: str = ""
    skill_id: str = ""
    args: Mapping[str, Any] = field(default_factory=dict)
    task_text: str = ""
    answer: str = ""
    grant_file: str = ""
    timeout_s: float = 300.0
    poll_s: float = 2.0
    session_id: str = ""
    task_id: str = ""
    identity_binding: bool = True
    notes: tuple[str, ...] = ()

    def resolved_session_id(self) -> str:
        return self.session_id or f"bidir-{uuid.uuid4()}"

    def resolved_task_id(self) -> str:
        return self.task_id or f"task-{uuid.uuid4()}"

    def resolved_grant(self) -> str:
        return read_grant_token(self.grant_file) if self.grant_file else ""

    def grant_part(self) -> dict[str, Any] | None:
        token = self.resolved_grant()
        if not token:
            return None
        return grant_data_item(token, target_aic=self.peer_aic, skill_id=self.skill_id)

    def resolved_text(self) -> str:
        """任务正文（双通道，任一通道被对端渲染都能识别技能指名）。

        - 显式 `task_text` 优先（自然语言任务）；
        - 否则发 `{"skill": "...", "args": {...}}`：既有 `commandParams`（PEP 认），
          又有正文里的结构化点名（provider 渲染丢失 commandParams 时的兜底）。
        """
        if self.task_text.strip():
            return self.task_text.strip()
        if self.skill_id:
            body: dict[str, Any] = {"skill": self.skill_id}
            if self.args:
                body["args"] = dict(self.args)
            return json.dumps(body, ensure_ascii=False)
        if self.args:
            return json.dumps(dict(self.args), ensure_ascii=False)
        return "双向互调连通性探针"


class LeaderClient(Protocol):
    """探针用到的最小客户端接口（真实实现见 `SdkLeaderClient`）。"""

    async def start(
        self, *, task_id: str, session_id: str, text: str,
        command_params: Mapping[str, Any] | None = None,
        data_items: Sequence[Mapping[str, Any]] | None = None,
    ) -> Any: ...

    async def get(self, *, task_id: str, session_id: str) -> Any: ...

    async def continue_task(self, *, task_id: str, session_id: str, text: str) -> Any: ...

    async def complete(self, *, task_id: str, session_id: str) -> Any: ...

    async def close(self) -> None: ...


class SdkLeaderClient:
    """`acps_sdk.aip.AipRpcClient` 的薄适配层（只补齐 `commandParams`）。"""

    def __init__(
        self,
        *,
        partner_url: str,
        leader_aic: str,
        ssl_context: Any,
        expected_partner_aic: str,
        identity_binding: bool = True,
    ) -> None:
        from acps_sdk.aip.aip_rpc_client import AipRpcClient  # noqa: PLC0415

        self.leader_aic = leader_aic
        self._client = AipRpcClient(
            partner_url=partner_url,
            leader_id=leader_aic,
            ssl_context=ssl_context,
            expected_partner_aic=expected_partner_aic or None,
            identity_binding_enabled=bool(identity_binding and expected_partner_aic),
        )

    def _command(self, command_type: Any, task_id: str, session_id: str,
                 text: str = "", command_params: Mapping[str, Any] | None = None,
                 data_items: Sequence[Mapping[str, Any]] | None = None) -> Any:
        from acps_sdk.aip.aip_base_model import (  # noqa: PLC0415
            StructuredDataItem, TaskCommand, TextDataItem,
        )

        items: list[Any] = [TextDataItem(text=text)] if text else []
        for item in data_items or ():
            if not isinstance(item, Mapping):
                continue
            items.append(StructuredDataItem(
                data=dict(item.get("data") or {}),
                metadata=dict(item.get("metadata") or {}),
            ))
        return TaskCommand(
            id=f"cmd-{uuid.uuid4()}",
            sentAt=_now_iso(),
            senderRole="leader",
            senderId=self.leader_aic,
            dataItems=items or None,
            sessionId=session_id,
            command=command_type,
            taskId=task_id,
            commandParams=dict(command_params) if command_params else None,
        )

    async def _send(self, command: Any) -> Any:
        sender = getattr(self._client, "_send_request", None)
        if sender is None:  # pragma: no cover - SDK 版本差异
            raise RuntimeError("acps_sdk 版本不提供 _send_request，无法带 commandParams 发起任务")
        response = await sender(command)
        result = getattr(response, "result", None)
        if result is None:
            raise RuntimeError("AIP 响应没有 result 字段")
        return result

    async def start(self, *, task_id: str, session_id: str, text: str,
                    command_params: Mapping[str, Any] | None = None,
                    data_items: Sequence[Mapping[str, Any]] | None = None) -> Any:
        if not command_params and not data_items:
            return await self._client.start_task(
                session_id=session_id, user_input=text, task_id=task_id
            )
        from acps_sdk.aip.aip_base_model import TaskCommandType  # noqa: PLC0415

        return await self._send(self._command(
            TaskCommandType.Start, task_id, session_id, text, command_params, data_items
        ))

    async def get(self, *, task_id: str, session_id: str) -> Any:
        return await self._client.get_task(task_id=task_id, session_id=session_id)

    async def continue_task(self, *, task_id: str, session_id: str, text: str) -> Any:
        return await self._client.continue_task(
            task_id=task_id, session_id=session_id, user_input=text
        )

    async def complete(self, *, task_id: str, session_id: str) -> Any:
        return await self._client.complete_task(task_id=task_id, session_id=session_id)

    async def close(self) -> None:
        await self._client.close()


def build_client(cfg: ProbeConfig, *, transport: Any = None) -> LeaderClient:
    """按配置构造真实客户端（mTLS）。`transport` 仅供测试替换 httpx transport。"""
    if not cfg.peer_endpoint:
        raise ValueError("缺少对端端点：请用 --peer-endpoint 指定，或让 discovery/本地工作区解析出端点")
    context = cfg.identity.ssl_context()
    if transport is None:
        return SdkLeaderClient(
            partner_url=cfg.peer_endpoint,
            leader_aic=cfg.identity.aic,
            ssl_context=context,
            expected_partner_aic=cfg.peer_aic,
            identity_binding=cfg.identity_binding,
        )
    from acps_sdk.aip.aip_rpc_client import AipRpcClient  # noqa: PLC0415

    client = AipRpcClient(
        partner_url=cfg.peer_endpoint,
        leader_id=cfg.identity.aic,
        ssl_context=context,
        transport=transport,
        expected_partner_aic=cfg.peer_aic or None,
        identity_binding_enabled=False,
    )
    adapter = SdkLeaderClient.__new__(SdkLeaderClient)
    adapter.leader_aic = cfg.identity.aic
    adapter._client = client
    return adapter


async def run_probe(
    cfg: ProbeConfig,
    *,
    client: LeaderClient | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
    discovery: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """跑一次探针，返回**可 JSON 序列化**的结果（含 `receipt`）。

    状态机与 AIP 一致：

    start → (accepted/working)* → awaiting-input  → continue（有 answer 时）
                                → awaiting-completion → complete
                                → completed / failed / rejected / canceled

    任何异常都收敛成 `ok=false` 的结果，不向上抛（验证脚本要能继续采集证据）。
    """
    owns_client = client is None
    active = client or build_client(cfg)
    task_id = cfg.resolved_task_id()
    session_id = cfg.resolved_session_id()
    started_at = _now_iso()
    t0 = clock()
    states: list[dict[str, Any]] = []
    error = ""
    final_state = ""
    last_task: Any = None
    notes = list(cfg.notes)
    answered = False
    closed = False

    def _record(state: str) -> None:
        if state and (not states or states[-1].get("state") != state):
            states.append({"at": _now_iso(), "state": state, "elapsed_s": round(clock() - t0, 2)})

    command_params: Mapping[str, Any] | None = None
    if cfg.skill_id:
        command_params = {"skillId": cfg.skill_id}
        if cfg.args:
            command_params = {"skillId": cfg.skill_id, "args": dict(cfg.args)}

    try:
        grant_part = cfg.grant_part()
        task = await active.start(
            task_id=task_id,
            session_id=session_id,
            text=cfg.resolved_text(),
            command_params=command_params,
            data_items=[grant_part] if grant_part else None,
        )
        last_task = task
        final_state = _state_of(task)
        _record(final_state)
        while final_state not in TERMINAL_STATES:
            if clock() - t0 > max(1.0, float(cfg.timeout_s)):
                notes.append(f"超时（{cfg.timeout_s:g}s）：最后一个状态 {final_state or '?'}")
                break
            if final_state == "awaiting-input":
                # 对端在等我补料：有 answer 就 continue，没有就如实停下（不空转）
                if not cfg.answer:
                    notes.append("对端请求补充输入（awaiting-input），未提供 --answer，探针就此结束")
                    break
                if answered:
                    notes.append("continue 后对端仍在请求补充输入，探针不再重复补充")
                    break
                answered = True
                last_task = await active.continue_task(
                    task_id=task_id, session_id=session_id, text=str(cfg.answer)
                )
            elif final_state == "awaiting-completion":
                # 产物已交付、等调用方关单：探针代调用方 complete（等价于点"确认产出"）
                if closed:
                    notes.append("complete 后任务仍未进入终态，探针结束（不重复关单）")
                    break
                closed = True
                last_task = await active.complete(task_id=task_id, session_id=session_id)
            else:
                await sleep(max(0.0, float(cfg.poll_s)))
                last_task = await active.get(task_id=task_id, session_id=session_id)
            final_state = _state_of(last_task)
            _record(final_state)
    except Exception as exc:  # noqa: BLE001 - 探针失败要如实落证据，不抛
        error = f"{type(exc).__name__}: {exc}"
    finally:
        if owns_client:
            try:
                await active.close()
            except Exception as exc:  # noqa: BLE001
                notes.append(f"关闭客户端失败：{type(exc).__name__}: {exc}")

    finished_at = _now_iso()
    products = ProductSummary.from_raw(_products_json(last_task))
    previews = _product_previews(last_task)
    receipt = TaskReceipt(
        direction="outbound",
        task_ref=task_id,
        state=final_state,
        local_aic=cfg.identity.aic,
        remote_aic=str(cfg.peer_aic or ""),
        created_at=started_at,
        updated_at=finished_at,
        session_id=session_id,
        products=products,
        events=tuple(states),
        source={
            "kind": "probe",
            "endpoint": cfg.peer_endpoint,
            "skill_id": cfg.skill_id,
            "identity_binding": bool(cfg.identity_binding),
            "grant": bool(cfg.grant_file),
        },
        notes=tuple(notes),
    )
    return {
        "schema": SCHEMA_PROBE,
        "ok": final_state == "completed",
        "leader_aic": cfg.identity.aic,
        "partner_aic": cfg.peer_aic,
        "skill_id": cfg.skill_id,
        "task_id": task_id,
        "session_id": session_id,
        "final_state": final_state,
        "states": states,
        "started_at": started_at,
        "finished_at": finished_at,
        "elapsed_s": round(clock() - t0, 2),
        "error": error,
        "discovery": list(discovery or []),
        "product_previews": previews,
        "receipt": receipt.as_dict(),
    }


def resolve_peer_endpoint(
    identity: WorkspaceIdentity,
    peer_aic: str,
    *,
    explicit: str = "",
    cluster_root: str | Any | None = None,
    discovery: list[dict[str, Any]] | None = None,
) -> str:
    """端点解析顺序：显式参数 → 发现结果 → 本机 `ws-*/acps/acs.final.json`。"""
    if explicit:
        return explicit
    for agent in discovery or ():
        if str(agent.get("aic") or "") == peer_aic and agent.get("endpoint"):
            return str(agent["endpoint"])
    if cluster_root is not None:
        found = peer_endpoint_from_local_workspaces(cluster_root, peer_aic)
        if found:
            return found
    return ""


__all__ = [
    "LeaderClient",
    "ProbeConfig",
    "SdkLeaderClient",
    "build_client",
    "resolve_peer_endpoint",
    "run_probe",
]