"""静态资源节点的确定性路由 provider。

资源 agent（CPU/GPU/eval/KB）的能力执行是纯代码；LLM 只负责"自然语言 → 能力参数"。
本模块把这一步换成结构化 JSON 路由：

- 调用方在 `commandParams` 带 `{"skillId": "gpu.run", "args": {...}}`，或在 dataItems
  文本里放 `{"skill": "gpu.run", "args": {...}}`；
- provider 直接发一次 ToolCall，能力执行后把结果作为最终文本返回；
- 解析失败且 `ACPS_<PREFIX>_LLM_FALLBACK=1` 时，回落到真实 LLM；否则返回用法错误。

这样 AIP 身份/ACS/PEP/令牌/心跳全部保留，资源执行不再经过模型。
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from wit.kernel.messages import Message, ModelRequest, ModelResponse, ToolCall
from wit.providers.base import ProviderCapabilities
from wit.kernel.content import ContentKind

_PAYLOAD_RE = re.compile(r"<payload>(.*?)</payload>", re.S)
_JSON_RE = re.compile(r"\{.*\}", re.S)


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def _parse_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _extract(text: str) -> tuple[str, dict[str, Any]]:
    """从 AIP 信封/文本里取 (skill, args)。解析不出返回 ("", {})。"""
    raw = str(text or "")
    payload: dict[str, Any] = {}
    match = _PAYLOAD_RE.search(raw)
    if match:
        parsed = _parse_json(match.group(1))
        if isinstance(parsed, dict):
            payload = parsed
    if not payload:
        parsed = _parse_json(raw.strip())
        if isinstance(parsed, dict):
            payload = parsed

    params = payload.get("commandParams") if isinstance(payload.get("commandParams"), dict) else {}
    skill = str(params.get("skillId") or params.get("skill") or "").strip()
    args = params.get("args") or params.get("arguments")

    data_items = payload.get("dataItems")
    if isinstance(data_items, list):
        for item in data_items:
            if not isinstance(item, dict):
                continue
            meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            if not skill:
                skill = str(meta.get("skillId") or meta.get("skill") or "").strip()
            if item.get("type") == "text":
                body = _parse_json(item.get("text"))
                if isinstance(body, dict):
                    if not skill:
                        skill = str(body.get("skill") or body.get("skillId") or "").strip()
                    if args is None:
                        args = body.get("args") if "args" in body else body

    if args is None:
        args = payload.get("args") if isinstance(payload.get("args"), dict) else None
    if not isinstance(args, dict):
        args = {}
    return skill, args


@dataclass
class StaticRouterProvider:
    """把结构化请求路由成一次工具调用；解析失败才回落 LLM。"""

    fallback: Any | None = None
    prefix: str = "RESOURCE"

    @property
    def capabilities(self) -> ProviderCapabilities:
        caps = getattr(self.fallback, "capabilities", None)
        if caps is not None:
            return caps
        return ProviderCapabilities(
            provider="static-router",
            api="static",
            input_modalities=frozenset({ContentKind.TEXT}),
            output_modalities=frozenset({ContentKind.TEXT}),
            streaming=False,
            tools=True,
        )

    def supports_content(self, part: Any, *, role: str) -> tuple[bool, str | None]:
        if self.fallback is not None and hasattr(self.fallback, "supports_content"):
            return self.fallback.supports_content(part, role=role)
        return True, None

    async def complete(self, request: ModelRequest) -> ModelResponse:
        messages = list(getattr(request, "messages", None) or ())
        if messages:
            last = messages[-1]
            # 工具执行后的第二跳：把能力结果原样作为最终答复
            if str(getattr(last, "role", "")) == "tool" or getattr(last, "tool_call_id", None):
                text = last.text() if hasattr(last, "text") else ""
                return ModelResponse.final(text or '{"ok": true}')
            # 第一跳：解析最后一轮用户文本
            text = last.text() if hasattr(last, "text") else ""
            skill, args = _extract(text)
            tool = self._pick_tool(request, skill)
            if tool is not None:
                error = self._validate_args(tool, args)
                if error:
                    return ModelResponse.final(error)
                return ModelResponse.tools([
                    ToolCall(id=f"static-{uuid.uuid4().hex[:12]}",
                             name=str(getattr(tool, "name", "") or ""),
                             arguments=args),
                ])
            if self._fallback_enabled():
                return await self.fallback.complete(request)
            return ModelResponse.final(self._usage(skill))
        if self._fallback_enabled():
            return await self.fallback.complete(request)
        return ModelResponse.final(self._usage(""))

    @staticmethod
    def _validate_args(tool: Any, args: Mapping[str, Any]) -> str:
        """按工具 input_schema 做 fail-closed 校验（确定性路由不能靠模型猜参数）。"""
        schema = getattr(tool, "input_schema", None)
        if not isinstance(schema, Mapping):
            return ""
        props = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
        if props:
            unknown = [k for k in args if k not in props]
            if unknown:
                return (f"参数错误：工具 {getattr(tool, 'name', '?')} 不认识 {unknown}；"
                        f"允许字段：{sorted(props)}")
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        missing = [k for k in required if k not in args]
        if missing:
            return (f"参数错误：工具 {getattr(tool, 'name', '?')} 缺少必填字段 {missing}；"
                    f"允许字段：{sorted(props) if props else '未声明'}")
        return ""

    @staticmethod
    def _pick_tool(request: ModelRequest, skill: str) -> Any | None:
        tools = list(getattr(request, "tools", None) or ())
        if not skill:
            return None
        want = _norm(skill)
        for tool in tools:
            if _norm(getattr(tool, "name", "")) == want:
                return tool
        return None

    def _fallback_enabled(self) -> bool:
        raw = os.getenv(f"ACPS_{self.prefix}_LLM_FALLBACK", "1")
        return self.fallback is not None and str(raw).strip().lower() not in {"0", "false", "no", "off"}

    def _usage(self, skill: str) -> str:
        return (
            "静态资源节点：未识别到结构化能力参数。请用 "
            '`commandParams={"skillId":"<skill>","args":{...}}`，或在 dataItems 文本里放 '
            '`{"skill":"<skill>","args":{...}}`。\n'
            f"（请求 skill={skill or '?'}；本节点默认不调用 LLM，可用 "
            f"ACPS_{self.prefix}_LLM_FALLBACK=1 启用自然语言兜底。）"
        )

    async def aclose(self) -> None:
        if self.fallback is not None and hasattr(self.fallback, "aclose"):
            await self.fallback.aclose()


def enabled(prefix: str) -> bool:
    raw = os.getenv(f"ACPS_{prefix}_STATIC_ROUTER", "0")
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def wrap(prefix: str, fallback: Any | None) -> StaticRouterProvider:
    return StaticRouterProvider(fallback=fallback, prefix=prefix)
