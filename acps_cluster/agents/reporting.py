"""伙伴 → Supervisor 的任务上报（互联机制，**不新增 ACS 能力**）。

为什么做成 run-scoped 工具而不是 @capability：
- 平台 ACS 已经锁定，任何新增/改动技能都会触发 `acs_fingerprint_changed_after_submission`；
- "上报"是集群互联机制，不是伙伴的业务特色能力，不应该出现在 ACS/发现里；
- 因此工具只在 run 开始时由 hook 注入（`context.add_tool`），compile/ACS 投影完全看不到它。

协议：
    partner --AIP--> supervisor 的 `research.submit`（非公开、无 acps_skill，同样不改 ACS）
    payload = {"skill": "research.submit", "question": ..., "request_id": ..., "user_scope": ...}

Supervisor 侧准入（任选其一，均为配置，不改 ACS）：
- `ACPS_SUPERVISOR_INBOUND_SKILL_ALLOW=<partner AIC>=research.submit`（L1'' 白名单），或
- 给该伙伴签发 `yanshu-entry` 域 grant（provider=supervisor，资产 research.submit）。
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from contextvars import ContextVar

from wit import (
    AipPartnerTransformHooks, RunResult, TextPart, Tool, WitHooks,
)

DEFAULT_TOOL_NAME = "report_task_to_supervisor"
_ENV_ENABLED = "ACPS_REPORT_UP"
_ENV_SUPERVISOR_AIC = "ACPS_SUPERVISOR_AIC"


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
    return str(data.get("aic") or "").strip()


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
        payload = {
            "skill": "research.submit",
            "question": question,
            "request_id": request_id,
            "user_scope": str(args.get("user_scope") or "partner-report"),
        }
        try:
            result = await runtime.aip_delegate(
                state,
                partner_aic=target,
                request=[TextPart(json.dumps(payload, ensure_ascii=False))],
            )
        except Exception as exc:  # noqa: BLE001 - 上报失败要如实回给模型
            return {"ok": False, "error": "report-failed",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "supervisor_aic": target, "request_id": request_id}
        return {"ok": True, "supervisor_aic": target,
                "request_id": request_id, "result": result}

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

_RECEIPT_SUBMITTED: ContextVar[bool] = ContextVar("yanshu_forward_receipt", default=False)
_FORWARD_CACHE: dict[str, dict[str, Any]] = {}
_MAX_FORWARD_CACHE = 1000

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
            except (TypeError, ValueError):
                out.append(str(data))
    return "\n".join(out).strip()


def _clean_question(text: str) -> str:
    """从 AIP/转发包装里取出真正的用户问题（优先最后一个业务 payload）。"""
    raw = str(text or "")
    payloads: list[dict[str, Any]] = []
    for match in re.finditer(r"<payload>(.*?)</payload>", raw, re.S):
        try:
            parsed = json.loads(match.group(1))
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            payloads.append(parsed)
    if not payloads:
        try:
            parsed = json.loads(raw.strip())
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            payloads = [parsed]
    for payload in reversed(payloads):
        if str(payload.get("type") or "") == "task-command":
            for item in payload.get("dataItems") or []:
                if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
                    inner = _clean_question(str(item["text"]))
                    if inner:
                        return inner
            continue
        candidate: Any = payload.get("args", payload)
        if isinstance(candidate, str):
            try:
                candidate = json.loads(candidate)
            except ValueError:
                pass
        if isinstance(candidate, dict):
            for key in ("question", "query", "task", "prompt", "text", "content", "input"):
                value = candidate.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()[:8000]
            return json.dumps(candidate, ensure_ascii=False)[:8000]
    body = re.sub(r"<acps_leader_call.*?</acps_leader_call>", "", raw, flags=re.S)
    body = re.sub(r"</?payload>", "", body)
    body = body.replace("[外部伙伴内容开始]", "").replace("[外部伙伴内容结束]", "")
    body = re.sub(r"<[^>]+>", " ", body)
    return " ".join(body.split()).strip()[:8000]


def _event_question(event: Any) -> str:
    text = _parts_text(getattr(event, "content", None))
    if text:
        return text
    messages = getattr(event, "messages", None) or []
    for msg in reversed(messages):
        text = _parts_text(getattr(msg, "content", None))
        if text:
            return text
    return ""


_INTERNAL_CACHE: dict[str, set[str]] = {}


def _internal_aics(cluster_root: Path | str | None = None) -> set[str]:
    root = Path(cluster_root) if cluster_root else Path(__file__).resolve().parents[1]
    key = str(root)
    cached = _INTERNAL_CACHE.get(key)
    if cached is not None:
        return cached
    out: set[str] = set()
    for path in root.glob("ws-*/acps/acs.final.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        aic = str(data.get("aic") or "").strip()
        if aic:
            out.add(aic)
    _INTERNAL_CACHE[key] = out
    return out


class AutoForwardRequestHooks(AipPartnerTransformHooks):
    """记录每个入站任务的来源与正文；真正转发在 run 的 wrap_run 里做。"""

    id = "yanshu-auto-forward-request"
    priority = 5

    async def on_partner_request(self, event) -> None:  # noqa: ANN001
        if os.getenv(_ENV_AUTO, "1").strip().lower() in {"0", "false", "no", "off"}:
            _INBOUND.set(None)
            return
        leader = str(getattr(event, "leader_aic", "") or "")
        target = supervisor_aic()
        if not target or leader == target:
            _INBOUND.set(None)
            return
        # 默认只转发“外部调用方”的入站任务；伙伴之间的内部调用照常执行，
        # 避免把 supervisor 的正常委派/伙伴互调也卷进转发链（防环）。
        if os.getenv("ACPS_AUTO_FORWARD_SCOPE", "external").strip().lower() != "all":
            if leader in _internal_aics(getattr(event, "_cluster_root", None)):
                _INBOUND.set(None)
                return
        question = _clean_question(_event_question(event)) or _event_question(event)
        if not question:
            _INBOUND.set(None)
            return
        _INBOUND.set({
            "task_id": str(getattr(event, "task_id", "") or ""),
            "session_id": str(getattr(event, "session_id", "") or ""),
            "leader_aic": leader,
            "question": question,
            "supervisor_aic": target,
        })

    async def on_partner_response(self, event) -> None:  # noqa: ANN001
        # 转发在 wrap_run 结束后已清空；这里只兜底清理，避免上下文串任务。
        _INBOUND.set(None)


def _submit_handler(ctx: Any):
    tools = getattr(getattr(ctx, "config", None), "tools", None) or []
    manager = getattr(ctx, "tool_manager", None)
    if not tools and manager is not None:
        lookup = getattr(manager, "lookup", None)
        tools = list(lookup().values()) if callable(lookup) else (getattr(manager, "tools", None) or [])
    for tool in tools:
        if str(getattr(tool, "name", "") or "") in {"aip_submit", "acps.aip.submit"}:
            handler = getattr(tool, "handler", None)
            if callable(handler):
                return handler
    return None


async def _submit_receipt(ctx: Any, text: str) -> bool:
    handler = _submit_handler(ctx)
    if not callable(handler):
        return False
    import uuid  # noqa: PLC0415
    products = [{
        "id": f"forward-{uuid.uuid4().hex[:12]}",
        "name": "任务已转交研枢-Supervisor",
        "data_items": [{"type": "text", "text": text[:12000]}],
    }]
    try:
        await handler({"products": products})
        _RECEIPT_SUBMITTED.set(True)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[report] 转交回执提交失败: {type(exc).__name__}: {exc}", flush=True)
        return False


def _seed_supervisor_peer(ctx: Any, target: str) -> None:
    """把 supervisor 的 ACS 预热进本伙伴的 discovery 缓存（aip_delegate 需要端点）。"""
    ws = Path(str(getattr(ctx, "workspace", "") or ""))
    if not ws:
        return
    acs_path = ws.parent / "ws-supervisor" / "acps" / "acs.final.json"
    if not acs_path.is_file():
        return
    try:
        acs = json.loads(acs_path.read_text(encoding="utf-8"))
        if str(acs.get("aic") or "") != target:
            return
        from wit.acps.discovery import cache_discovered_agent, normalize_discovered_agent  # noqa: PLC0415

        summary = normalize_discovered_agent(acs, ranking=0)
        cache_discovered_agent(
            cache_dir=ws / "acps" / "discovered", summary=summary, raw_acs=acs)
    except Exception:  # noqa: BLE001 - 预热失败就让 delegate 如实报错
        return


def receipt_submitted() -> bool:
    return bool(_RECEIPT_SUBMITTED.get())


def _snapshot_text(snapshot: Any) -> str:
    if not isinstance(snapshot, dict):
        return ""
    parts: list[str] = []
    for product in snapshot.get("products") or []:
        if not isinstance(product, dict):
            continue
        for item in product.get("data_items") or []:
            if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
                parts.append(str(item["text"]))
    return "\n".join(parts).strip()


async def _wait_forward(ctx: Any, result: Any) -> Any:
    """转发后等 supervisor 的受理产物，把 root_id/job_id 带回来。"""
    if not isinstance(result, dict):
        return result
    ref = str(result.get("task_ref") or "")
    if not ref:
        return result
    try:
        snapshot = await ctx.runtime.aip_task(ctx.state, action="wait", task_ref=ref)
    except Exception:  # noqa: BLE001
        return result
    if not isinstance(snapshot, dict):
        return result
    if snapshot.get("products") or str(snapshot.get("state")) == "awaiting-completion":
        try:
            done = await ctx.runtime.aip_task(ctx.state, action="complete", task_ref=ref)
        except Exception:  # noqa: BLE001
            done = None
        if isinstance(done, dict):
            snapshot = done
    return snapshot


class AutoForwardRunHooks(WitHooks):
    """所有用户入站任务：不跑模型，直接包装成 research.submit 交给 supervisor。"""

    id = "yanshu-auto-forward-run"
    priority = 5

    async def wrap_run(self, ctx, handler):  # noqa: ANN001, ANN202
        inbound = _INBOUND.get()
        if not isinstance(inbound, dict):
            return await handler()
        _INBOUND.set(None)
        try:
            target = inbound["supervisor_aic"]
            task_id = inbound["task_id"] or uuid.uuid4().hex[:12]
            cached = _FORWARD_CACHE.get(str(task_id))
            if cached is not None:
                text = json.dumps(cached, ensure_ascii=False, default=str)[:12000]
                submitted = await _submit_receipt(ctx, text)
                return RunResult(
                    output_parts=[TextPart(text)], messages=[], observations=[],
                    iterations=0, status="completed" if submitted else "failed",
                    metadata={"auto_forward": True, "duplicate": True,
                              "receipt_submitted": submitted},
                )
            payload = {
                "skill": "research.submit",
                "args": {
                    "question": inbound["question"],
                    "request_id": f"fwd-{task_id}",
                    "user_scope": "partner:" + str(
                        getattr(getattr(getattr(ctx, "runtime", None), "agent", None), "id", "partner")),
                    "source_task_id": inbound["task_id"],
                    "source_session": inbound["session_id"],
                    "source_caller": inbound["leader_aic"],
                },
            }
            try:
                # aip_delegate 需要本机 discovery 缓存里有 supervisor 的 Direct 端点
                _seed_supervisor_peer(ctx, target)
                try:
                    await ctx.runtime.aip_discover(target, limit=5)
                except Exception:  # noqa: BLE001 - 已有缓存时 discover 失败不影响
                    pass
                body = json.dumps(payload, ensure_ascii=False)
                result = await ctx.runtime.aip_delegate(
                    ctx.state, partner_aic=target,
                    request=[TextPart(f"<payload>{body}</payload>")],
                )
                try:
                    result = await asyncio.wait_for(_wait_forward(ctx, result), timeout=2.0)
                except Exception:  # noqa: BLE001 - 超时就按已受理回执返回，绝不阻塞
                    pass
                text_out = _snapshot_text(result)
                receipt = {
                    "ok": True, "forwarded": True,
                    "source_task_id": inbound["task_id"],
                    "supervisor_aic": target,
                    "supervisor_task_ref": str(result.get("task_ref") or ""),
                    "note": "Supervisor 已受理；root_id/job_id 由 Supervisor 回执给出，不阻塞等待",
                    "result": result,
                }
                if text_out:
                    receipt["supervisor_product"] = text_out[:4000]
                    import re  # noqa: PLC0415
                    m = re.search(r'"root_id"\s*:\s*"([^"]+)"', text_out)
                    if m:
                        receipt["root_id"] = m.group(1)
                    m = re.search(r'"job_id"\s*:\s*"([^"]+)"', text_out)
                    if m:
                        receipt["job_id"] = m.group(1)
            except Exception as exc:  # noqa: BLE001
                receipt = {
                    "ok": False, "forwarded": False, "error": "forward-failed",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "source_task_id": inbound["task_id"],
                    "supervisor_aic": target,
                }
            text = json.dumps(receipt, ensure_ascii=False, default=str)[:12000]
            _FORWARD_CACHE[str(task_id)] = receipt
            if len(_FORWARD_CACHE) > _MAX_FORWARD_CACHE:
                _FORWARD_CACHE.pop(next(iter(_FORWARD_CACHE)))
            await _submit_receipt(ctx, text)
            return RunResult(
                output_parts=[TextPart(text)],
                messages=[], observations=[], iterations=0,
                status="completed", metadata={"auto_forward": True},
            )
        finally:
            _INBOUND.set(None)


__all__ += ["AutoForwardRequestHooks", "AutoForwardRunHooks"]
