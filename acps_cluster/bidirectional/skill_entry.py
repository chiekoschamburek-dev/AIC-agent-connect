"""入站技能快速通道：调用方**指名技能**时直接执行能力，不跑 LLM。

为什么需要（双向调用的成本问题）：supervisor 的入站任务是完整 leader run——对方
只想确认"你能不能被我调用、你的能力返回什么"，却要跑一次几十步的编排（模型成本 +
数分钟延迟）。`research.orchestrate` 本身是**纯代码**的只读门面，没有必要经过模型。

机制：把真实 provider 包一层。入站命令里出现

    commandParams = {"skillId": "research.orchestrate", "args": {"task": "..."}}

且技能在登记表里、参数能对上 handler 签名时，直接 `await handler(**args)`，把结果
JSON 作为最终答复返回（run 收尾的 `aip_submit` 兜底会把它交成产物）；其余请求一律
交回原 provider（自然语言任务照旧走模型，行为不变）。

边界：

- **只认结构化指名**：没有 `commandParams`（或 args 对不上签名）时不猜，回落到 LLM；
- **不扩大能力面**：这里只执行代码侧登记的 handler（supervisor 只登记
  `research.orchestrate`），不暴露别的能力，也没有额外凭据；
- **失败不伪装**：handler 抛异常就让 run 失败（`aip_fail` 如实上报），不返回假产物。
"""

from __future__ import annotations

import inspect
import json
import os
from typing import Any, Awaitable, Callable, Mapping

from acps_cluster.core.skill_call import parse_skill_call


def enabled(prefix: str) -> bool:
    """`ACPS_<PREFIX>_SKILL_FASTPATH=1` 时启用（默认关，灰度用）。"""
    raw = os.getenv(f"ACPS_{prefix}_SKILL_FASTPATH", "0")
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _all_texts(request: Any) -> list[str]:
    messages = list(getattr(request, "messages", None) or ())
    return [str(message.text()) for message in messages
            if hasattr(message, "text") and message.text()]


def _last_text(request: Any) -> str:
    texts = _all_texts(request)
    return texts[-1] if texts else ""


def bind_arguments(handler: Callable[..., Any], args: Mapping[str, Any]) -> dict[str, Any] | None:
    """按 handler 签名过滤/校验参数；缺必填或类型不符返回 None（= 不接这一单）。

    **不认识的键直接丢掉**（调用方可能顺手带上 `traceId` 之类的元数据）；handler 显式
    声明 `**kwargs` 时全部保留。严格来自签名的 bind 仍然生效——缺必填就是 None，
    避免把 `{}` 当成合法入参执行一个空计划。
    """
    try:
        signature = inspect.signature(handler)
    except (TypeError, ValueError):
        return None
    accepts_extra = any(
        param.kind is inspect.Parameter.VAR_KEYWORD
        for param in signature.parameters.values()
    )
    filtered = dict(args) if accepts_extra else {
        key: value for key, value in args.items() if key in signature.parameters
    }
    try:
        bound = signature.bind(**filtered)
    except TypeError:
        return None
    bound.apply_defaults()
    return dict(bound.arguments)


class SkillFastPathProvider:
    """把 `commandParams.skillId` 命中的技能直接执行掉；其余请求回落原 provider。"""

    def __init__(
        self,
        handlers: Mapping[str, Callable[..., Awaitable[Any]]],
        *,
        fallback: Any,
        final_factory: Callable[[str], Any],
        prefix: str = "SUPERVISOR",
    ) -> None:
        self._handlers = {str(key): handler for key, handler in handlers.items()}
        self._fallback = fallback
        self._final = final_factory
        self._prefix = prefix

    @property
    def skills(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))

    @property
    def capabilities(self) -> Any:
        caps = getattr(self._fallback, "capabilities", None)
        return caps if caps is not None else None

    def supports_content(self, part: Any, *, role: str) -> tuple[bool, str | None]:
        checker = getattr(self._fallback, "supports_content", None)
        if callable(checker):
            return checker(part, role=role)
        return True, None

    def __getattr__(self, name: str) -> Any:
        """把未显式实现的可选能力（如 `stream`）委托给被包装的 provider。

        本类只覆盖 `complete`——wit 的 run 走 `complete`（静态资源路由同款做法），
        但运行时也可能对 provider 调 `stream`/诊断属性；缺一个方法就让整次 run
        `AttributeError` 不值得，统一委托即可。
        """
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._fallback, name)

    async def complete(self, request: Any) -> Any:
        # 只解析当前入站命令的最后一条消息：历史消息里的技能名不能被重放，
        # 消息内部的多个 <payload> 由 parse_skill_call 取“最后一个带 skill 的”。
        skill, args = parse_skill_call(_last_text(request))
        handler = self._handlers.get(skill)
        if handler is not None and skill:
            bound = bind_arguments(handler, args)
            if bound is not None:
                result = await handler(**bound)
                payload = result if isinstance(result, str) else json.dumps(
                    result, ensure_ascii=False, indent=2
                )
                print(f"[{self._prefix.lower()}] 入站技能快速通道：{skill}（未调用 LLM）", flush=True)
                return self._final(payload)
            print(f"[{self._prefix.lower()}] 技能 {skill} 参数不匹配，回落 LLM：{sorted(args)}",
                  flush=True)
        if self._fallback is not None and hasattr(self._fallback, "complete"):
            return await self._fallback.complete(request)
        return self._final(self._usage(skill))

    async def aclose(self) -> None:
        closer = getattr(self._fallback, "aclose", None)
        if callable(closer):
            await closer()

    def _usage(self, skill: str) -> str:
        listed = "、".join(self.skills) or "（空）"
        return (
            "入站技能快速通道未命中：请在 AIP `commandParams` 里写 "
            '`{"skillId":"<skill>","args":{...}}`。\n'
            f"（收到 skill={skill or '?'}；本节点登记：{listed}）"
        )


__all__ = ["SkillFastPathProvider", "bind_arguments", "enabled"]