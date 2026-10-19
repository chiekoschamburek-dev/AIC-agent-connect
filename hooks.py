"""集群通用 hooks：运行日志、工具重试、资料注入。"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from wit import ContextBlock, HookContext, ToolCall, WitHooks

RETRYABLE_TOOL_CODES = {"tool_timeout"}


class RunLogger(WitHooks):
    """每个 agent 的 run 生命周期日志。"""

    id = "research.run-logger"

    async def on_run_start(self, event) -> None:
        print(f"[run.start] agent={event.context.compiled.agent_id}")

    async def on_run_end(self, event) -> None:
        agent_id = event.context.compiled.agent_id
        print(f"[run.end] agent={agent_id} status={event.result.status}")


class ToolRetry(WitHooks):
    """对超时/网络类失败的工具调用做有限重试。"""

    id = "research.tool-retry"
    priority = 10

    def __init__(self, max_retries: int = 2, backoff_seconds: float = 3.0) -> None:
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds

    @staticmethod
    def _is_retryable(observation: dict[str, Any]) -> bool:
        if observation.get("status") not in ("error", "timeout"):
            return False
        error = observation.get("error") or {}
        code = str(error.get("code", ""))
        message = str(error.get("message", "")).lower()
        if code in RETRYABLE_TOOL_CODES:
            return True
        return any(k in message for k in ("arxiv", "http", "timeout", "connection"))

    async def wrap_tool_call(
        self,
        ctx: HookContext,
        tool_call: ToolCall,
        handler: Callable[[ToolCall], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        observation = await handler(tool_call)
        for attempt in range(1, self.max_retries + 1):
            if not self._is_retryable(observation):
                break
            await asyncio.sleep(self.backoff_seconds * attempt)
            print(f"[tool-retry] {tool_call.name} 第 {attempt} 次重试")
            observation = await handler(tool_call)
        return observation


class MaterialContext(WitHooks):
    """把外部资料作为不可信上下文注入系统提示，供综合/写作 agent 使用。"""

    id = "research.material-context"

    def __init__(self, title: str, materials: str) -> None:
        self.title = title
        self.materials = materials

    def context(self, ctx: HookContext) -> list[ContextBlock]:
        return [ContextBlock(title=self.title, value=self.materials, trusted=False)]
