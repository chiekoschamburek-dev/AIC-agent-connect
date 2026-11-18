"""伙伴 → Supervisor 的任务上报（互联机制，**不新增 ACS 能力**）。

为什么做成 run-scoped 工具而不是 @capability：
- 平台 ACS 已经锁定，任何新增/改动技能都会触发 `acs_fingerprint_changed_after_submission`；
- "上报"是集群互联机制，不是伙伴的业务特色能力，不应该出现在 ACS/发现里；
- 因此工具只在 run 开始时由 hook 注入（`context.add_tool`），compile/ACS 投影完全看不到它。

伙伴通过 AIP 调用 Supervisor 的 `research.submit`。启用 PEP 时，用
`ACPS_SUPERVISOR_INBOUND_SKILL_ALLOW=<partner AIC>=research.submit` 授权对应伙伴。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from wit import (
    AipPartnerTransformHooks, RunResult, TextPart, Tool, WitHooks,
)
from wit.hooks.acps.delegation import AipDelegationHooks

from acps_cluster.core.aip_guard import _CLOSE, _OPEN

DEFAULT_TOOL_NAME = "report_task_to_supervisor"
_ENV_ENABLED = "ACPS_REPORT_UP"
_ENV_SUPERVISOR_AIC = "ACPS_SUPERVISOR_AIC"
AUTO_SUBMIT_PREFIX = "（本产物由研枢-Supervisor 在任务收尾时自动提交）\n\n"


def _env_seconds(name: str, default: float) -> float:
    """读一个正数秒级配置；写错就退回默认，不能让转发因为配置笔误直接失败。"""
    try:
        return max(0.1, float(os.getenv(name, "") or default))
    except ValueError:
        return default


def _max_forward_question() -> int:
    """与 `root_store._MAX_QUESTION` 对齐：超长文本会被 Supervisor 判 invalid-request。"""
    try:
        return max(200, int(os.getenv("ACPS_FORWARD_MAX_QUESTION", "20000")))
    except ValueError:
        return 20000


def supervisor_aic(cluster_root: Path | str | None = None) -> str:
    """解析 supervisor AIC：env 优先，其次读同集群 ws-supervisor 的已批 ACS。"""
    explicit = os.getenv(_ENV_SUPERVISOR_AIC, "").strip()
    if explicit:
        return explicit
    root = Path(cluster_root) if cluster_root else Path(__file__).resolve().parents[1]
    acs = root / "ws-supervisor" / "acps" / "acs.final.json"
    try:
        data = json.loads(acs.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(data.get("aic") or "").strip() if isinstance(data, dict) else ""


class ReportingDelegationHooks(AipDelegationHooks):
    """Enable WIT's programmatic AIP API without adding outbound tools to the model."""

    async def on_run_start(self, event) -> None:  # noqa: ANN001
        pass

    def context(self, ctx) -> list:  # noqa: ANN001
        return []


def _submit_request(question: str, request_id: str, user_scope: str, **source: str) -> list[TextPart]:
    body = json.dumps({
        "skill": "research.submit",
        "args": {"question": question, "request_id": request_id, "user_scope": user_scope, **source},
    }, ensure_ascii=False)
    # Admission sees the DataItem metadata. The provider sees the text after
    # WIT moves it out of the outer command payload.
    body = body.replace("<", r"\u003c")
    return [TextPart(f"<payload>{body}</payload>", metadata={"skillId": "research.submit"})]


def _arguments_dict(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return dict(arguments)
    if isinstance(arguments, str):
        try:
            data = json.loads(arguments)
        except ValueError:
            return {}
        return dict(data) if isinstance(data, dict) else {}
    return {}


def make_report_tool(
    runtime: Any,
    state: Any,
    *,
    cluster_root: Path | str | None = None,
) -> Tool:
    """构造 run-scoped 上报工具；handler 捕获本次 run 的 runtime/state。"""

    async def report_task_to_supervisor(arguments: dict | str) -> dict[str, Any]:
        args = _arguments_dict(arguments)
        question = str(args.get("question") or "").strip()
        if not question:
            return {"ok": False, "error": "question-required",
                    "detail": "请给出要上报给 Supervisor 的任务描述（question）。"}
        target = supervisor_aic(cluster_root)
        if not target:
            return {"ok": False, "error": "supervisor-aic-unavailable",
                    "detail": "未配置 ACPS_SUPERVISOR_AIC，且本地找不到 ws-supervisor 的 ACS。"}
        request_id = str(args.get("request_id") or "").strip() or f"report-{uuid.uuid4().hex[:12]}"
        request = _submit_request(
            question[:_max_forward_question()], request_id,
            str(args.get("user_scope") or "partner-report"),
        )
        receipt = await _forward(runtime, state, target, request)
        return {**receipt, "request_id": request_id}

    return Tool(
        name=DEFAULT_TOOL_NAME,
        description=(
            "把当前任务上报给研枢-Supervisor，由它统一编排（创建根任务并返回 job_id）。"
            "用于：本伙伴处理不了、需要跨伙伴调度、或用户要求交给总控继续的请求。"
            "只上报任务描述，不要把整段上下文/密钥塞进去；不要对同一请求重复上报。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "要上报的任务描述（需自包含）"},
                "request_id": {"type": "string", "description": "幂等号；同一请求重复调用请复用"},
                "user_scope": {"type": "string", "description": "用户/会话范围，默认 partner-report"},
            },
            "required": ["question"],
            "additionalProperties": False,
        },
        handler=report_task_to_supervisor,
        metadata={"internal_tool": True, "interconnect": "partner->supervisor"},
    )


class ReportUpHooks(WitHooks):
    """在每次 run 开始时注入上报工具；不参与 compile，因此不改 ACS 投影。"""

    id = "yanshu-report-up"
    priority = 40

    def __init__(self, *, cluster_root: Path | str | None = None) -> None:
        self._cluster_root = cluster_root

    async def on_run_start(self, event) -> None:  # noqa: ANN001
        if os.getenv(_ENV_ENABLED, "1").strip().lower() in {"0", "false", "no", "off"}:
            return
        ctx = event.context
        if not supervisor_aic(self._cluster_root):
            return
        private = getattr(ctx, "private_state", None)
        if isinstance(private, dict):
            if private.get("report_up_added"):
                return
        manager = getattr(ctx, "tool_manager", None)
        if manager is not None and manager.get(DEFAULT_TOOL_NAME) is not None:
            return
        ctx.add_tool(
            make_report_tool(ctx.runtime, ctx.state, cluster_root=self._cluster_root),
            source="hook",
        )
        if isinstance(private, dict):
            private["report_up_added"] = True


__all__ = ["DEFAULT_TOOL_NAME", "ReportUpHooks", "make_report_tool", "supervisor_aic"]

# ── 所有入站任务自动转发 ───────────────────────────────────────────────

_INBOUND: ContextVar[dict[str, Any] | None] = ContextVar(
    "yanshu_auto_forward_inbound", default=None)
_ENV_AUTO = "ACPS_AUTO_FORWARD"


def _parts_text(parts: Any) -> str:
    """把 AIP ContentPart 列表压成文本（TextPart 原样，DataPart 转 JSON）。"""
    out: list[str] = []
    for part in parts or []:
        if isinstance(part, str):
            out.append(part)
            continue
        text = getattr(part, "text", None)
        if isinstance(text, str) and text:
            out.append(text)
            continue
        data = getattr(part, "data", None)
        if data is not None:
            try:
                out.append(json.dumps(data, ensure_ascii=False))
            except (TypeError, ValueError, RecursionError):
                out.append(str(data))
    return "\n".join(out).strip()


def _clean_question(text: str, *, unwrap_command: bool = True) -> str:
    """从 AIP/转发包装里取出真正的用户问题（优先最后一个业务 payload）。"""
    raw = str(text or "")
    payloads: list[dict[str, Any]] = []
    for match in re.finditer(r"<payload>(.*?)</payload>", raw, re.S):
        try:
            parsed = json.loads(match.group(1))
        except (TypeError, ValueError, RecursionError):
            continue
        if isinstance(parsed, dict):
            payloads.append(parsed)
    if not payloads:
        try:
            parsed = json.loads(raw.strip())
        except (TypeError, ValueError, RecursionError):
            parsed = None
        if isinstance(parsed, dict):
            payloads = [parsed]
    for payload in reversed(payloads):
        if str(payload.get("type") or "") == "task-command":
            items = payload.get("dataItems")
            if not unwrap_command or not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
                    inner = _clean_question(str(item["text"]), unwrap_command=False)
                    if inner:
                        return inner
            continue
        candidate: Any = payload.get("args", payload)
        if isinstance(candidate, str):
            try:
                candidate = json.loads(candidate)
            except (ValueError, RecursionError):
                pass
        if isinstance(candidate, dict):
            for key in ("question", "query", "task", "prompt", "text", "content", "input"):
                value = candidate.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return json.dumps(candidate, ensure_ascii=False)
    body = re.sub(r"<acps_leader_call.*?</acps_leader_call>", "", raw, flags=re.S)
    body = re.sub(r"</?payload>", "", body)
    return body.replace(_OPEN, "").replace("[外部伙伴内容开始]", "").replace(_CLOSE, "").strip()


_INTERNAL_CACHE: dict[str, tuple[float, frozenset[str]]] = {}
_INTERNAL_CACHE_TTL = 30.0


def _internal_aics(cluster_root: Path | str | None = None) -> set[str]:
    """本集群内部成员的 AIC 集合（用来跳过伙伴之间的内部调用）。

    结果按 cluster_root 缓存 30s：每个入站请求都重扫 overlay + 所有
    `ws-*/acps/acs.final.json` + roster 的 enrollment.json（约 2N 次文件读）成本太高，
    而成员身份在 30s 内不会变化；换身份/新增成员重新拉起实例即可。
    """
    root = Path(cluster_root) if cluster_root else Path(__file__).resolve().parents[1]
    try:
        key = str(root.resolve())
    except OSError:
        key = str(root)
    cached = _INTERNAL_CACHE.get(key)
    now = time.monotonic()
    if cached is not None and now - cached[0] < _INTERNAL_CACHE_TTL:
        return set(cached[1])

    from acps_cluster.core.discovery_metadata import registered_overlay

    out: set[str] = set()
    for agent in registered_overlay().get("agents", {}).values():
        if isinstance(agent, dict) and agent.get("aic"):
            out.add(str(agent["aic"]))
    for path in root.glob("ws-*/acps/acs.final.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        aic = str(data.get("aic") or "").strip() if isinstance(data, dict) else ""
        if aic:
            out.add(aic)
    from acps_cluster.core.cluster_roster import ROSTER, _enrollment_aic

    for member in ROSTER:
        aic = _enrollment_aic(member.workspace, root)
        if aic:
            out.add(aic)
    _INTERNAL_CACHE[key] = (now, frozenset(out))
    return out


class AutoForwardRequestHooks(AipPartnerTransformHooks):
    """记录每个入站任务的来源与正文；真正转发在 run 的 wrap_run 里做。"""

    id = "yanshu-auto-forward-request"
    # Read the same business content that remains after credential stripping.
    priority = 90

    def __init__(self, cluster_root: Path | None = None) -> None:
        self._cluster_root = cluster_root

    async def on_partner_request(self, event) -> None:  # noqa: ANN001
        _INBOUND.set(None)
        if os.getenv(_ENV_AUTO, "1").strip().lower() in {"0", "false", "no", "off"}:
            return
        leader = str(getattr(event, "leader_aic", "") or "").strip()
        task_id = str(getattr(event, "task_id", "") or "").strip()
        target = supervisor_aic(self._cluster_root)
        if not getattr(event, "peer_verified", False) or not leader or not task_id or not target or leader == target:
            return
        # 默认只转发“外部调用方”的入站任务；伙伴之间的内部调用照常执行，
        # 避免把 supervisor 的正常委派/伙伴互调也卷进转发链（防环）。
        if os.getenv("ACPS_AUTO_FORWARD_SCOPE", "external").strip().lower() != "all":
            if leader in _internal_aics(self._cluster_root):
                return
        question = _clean_question(_parts_text(getattr(event, "content", None)))
        if not question:
            return
        _INBOUND.set({
            "task_id": task_id,
            "session_id": str(getattr(event, "session_id", "") or ""),
            "leader_aic": leader,
            "question": question[:_max_forward_question()],
            "supervisor_aic": target,
        })

    async def on_partner_response(self, event) -> None:  # noqa: ANN001
        # 转发在 wrap_run 结束后已清空；这里只兜底清理，避免上下文串任务。
        _INBOUND.set(None)


def _seed_supervisor_peer(runtime: Any, target: str) -> bool:
    """把 supervisor 的 ACS 预热进本伙伴的 discovery 缓存（aip_delegate 需要端点）。"""
    ws = getattr(runtime, "workspace", None)
    if not ws:
        return False
    ws = Path(ws)
    acs_path = ws.parent / "ws-supervisor" / "acps" / "acs.final.json"
    if not acs_path.is_file():
        return False
    try:
        acs = json.loads(acs_path.read_text(encoding="utf-8"))
        if str(acs.get("aic") or "") != target:
            return False
        from wit.acps.discovery import (  # noqa: PLC0415
            cache_discovered_agent, extract_endpoint_url, normalize_discovered_agent,
        )

        if not extract_endpoint_url(acs):
            return False
        summary = normalize_discovered_agent(acs, ranking=0)
        path = cache_discovered_agent(
            cache_dir=ws / "acps" / "discovered", summary=summary, raw_acs=acs)
        return path is not None
    except Exception:  # noqa: BLE001 - 预热失败就让 delegate 如实报错
        return False


def _business_receipt(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    for product in snapshot.get("products") or []:
        if not isinstance(product, dict):
            continue
        for item in product.get("data_items") or product.get("dataItems") or []:
            if not isinstance(item, dict) or item.get("type") != "text":
                continue
            text = item.get("text")
            if not isinstance(text, str):
                continue
            try:
                data = json.loads(text.removeprefix(AUTO_SUBMIT_PREFIX))
            except (ValueError, RecursionError):
                continue
            if isinstance(data, dict) and isinstance(data.get("ok"), bool):
                return data
    return None


_FAILED_STATES = frozenset({"failed", "rejected", "canceled", "cancelled", "expired"})


async def _forward(runtime: Any, state: Any, target: str, request: list[TextPart]) -> dict[str, Any]:
    base = {"supervisor_aic": target}
    if not _seed_supervisor_peer(runtime, target):
        try:
            await asyncio.wait_for(runtime.aip_discover(target, limit=5), timeout=2)
        except Exception:  # noqa: BLE001 - existing discovery cache can still be usable
            pass
    try:
        snapshot = await asyncio.wait_for(
            runtime.aip_delegate(state, partner_aic=target, request=request),
            timeout=_env_seconds("ACPS_FORWARD_DELEGATE_TIMEOUT", 10.0),
        )
    except TimeoutError:
        # 超时 ≠ 没发出去：请求可能已被 Supervisor 受理（只是回执没赶上）。
        # 如实返回“未知态”，让调用方用同一个 request_id 重试或凭 root/source 查询，
        # 不要把它报成 report-failed（2026-11-18 审查 P1）。
        return {**base, "ok": False, "forwarded": False, "accepted": False,
                "error": "acceptance-unconfirmed",
                "detail": "转发在等待窗口内未返回；Supervisor 可能已受理。"
                          "请用相同 request_id 重试，或用 research.status 查询。"}
    except Exception as exc:  # noqa: BLE001
        return {**base, "ok": False, "forwarded": False, "error": "report-failed",
                "detail": f"{type(exc).__name__}: {exc}"[:1000]}
    if not isinstance(snapshot, dict):
        return {**base, "ok": False, "forwarded": False, "error": "invalid-forward-response"}
    ref = str(snapshot.get("task_ref") or "").strip()
    base.update(forwarded=bool(ref), supervisor_task_ref=ref)
    if ref and not snapshot.get("products") and snapshot.get("state") not in _FAILED_STATES:
        try:
            update = await asyncio.wait_for(
                runtime.aip_task(state, action="wait", task_ref=ref),
                timeout=_env_seconds("ACPS_FORWARD_WAIT_TIMEOUT", 5.0),
            )
            if isinstance(update, dict):
                snapshot = update
        except Exception:  # noqa: BLE001 - preserve the dispatch reference if wait fails
            pass
    if ref and snapshot.get("state") == "awaiting-completion":
        try:
            await asyncio.wait_for(
                runtime.aip_task(state, action="complete", task_ref=ref),
                timeout=_env_seconds("ACPS_FORWARD_COMPLETE_TIMEOUT", 2.0),
            )
        except Exception as exc:  # noqa: BLE001 - the receipt exists even if closeout fails
            print(f"[report] 转发任务 {ref} 未能关单: {type(exc).__name__}", flush=True)

    task_state = str(snapshot.get("state") or "")
    base["supervisor_state"] = task_state
    if task_state in _FAILED_STATES or snapshot.get("ok") is False:
        return {**base, "ok": False, "error": "forward-rejected",
                "detail": str(snapshot.get("error") or task_state)[:1000]}
    receipt = _business_receipt(snapshot)
    if receipt is not None:
        for key in ("root_id", "job_id", "state"):
            if isinstance(receipt.get(key), str) and receipt[key]:
                base[key] = receipt[key][:256]
        if not receipt["ok"]:
            return {**base, "ok": False, "error": str(receipt.get("error") or "submit-rejected")[:256],
                    "detail": str(receipt.get("detail") or "")[:1000]}
        if base.get("root_id") and base.get("job_id"):
            return {**base, "ok": True, "accepted": True,
                    "note": "Supervisor 已受理。可用 research.status 查询进度与结果。"}
    detail = ("已发往 Supervisor，尚未确认受理。" if ref else "未获得 Supervisor 任务引用。")
    return {**base, "ok": False, "accepted": False,
            "error": "acceptance-unconfirmed", "detail": detail}


class AutoForwardRunHooks(WitHooks):
    """所有用户入站任务：不跑模型，直接包装成 research.submit 交给 supervisor。"""

    id = "yanshu-auto-forward-run"
    priority = 5

    async def wrap_run(self, ctx, handler):  # noqa: ANN001, ANN202
        inbound = _INBOUND.get()
        if not isinstance(inbound, dict):
            return await handler()
        _INBOUND.set(None)
        task_id = inbound["task_id"]
        partner_id = getattr(getattr(ctx.runtime, "agent", None), "id", "partner")
        request = _submit_request(
            inbound["question"], f"fwd-{task_id}", f"partner:{partner_id}",
            source_task_id=task_id, source_session=inbound["session_id"],
            source_caller=inbound["leader_aic"],
        )
        receipt = await _forward(ctx.runtime, ctx.state, inbound["supervisor_aic"], request)
        receipt["source_task_id"] = task_id
        # WIT's inbound executor persists output_parts as the AIP product.
        return RunResult(
            output_parts=[TextPart(json.dumps(receipt, ensure_ascii=False))],
            messages=[], observations=[], iterations=0, state=ctx.state,
            status="completed", metadata={"auto_forward": True},
        )


__all__ += ["AutoForwardRequestHooks", "AutoForwardRunHooks"]
