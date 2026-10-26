"""模型 provider：北邮网关 deepseek 主用，MiniMax 保底。

## 为什么单独一个模块

集群里所有 agent 都经 `bootstrap.build_provider()` 拿 provider，而部分能力
（`llm_tools.chat`）另走一条 HTTP 直连。要让"主用/保底"对**两条路径都生效**，
逻辑必须收在一处，两处共用同一份配置解析。

## 配置（全在 .env，不写死）

| 变量 | 含义 |
|---|---|
| `BUPT_BASE_URL` | 北邮网关端点，如 `https://myai.bupt.edu.cn/llm-gw/v1` |
| `BUPT_API_KEY` | 网关 key |
| `BUPT_MODEL` | 网关模型名，如 `deepseek-v4-flash` |
| `OPENAI_*` | 保底（MiniMax）那一套，沿用现有变量 |

## 回退语义

**按调用回退，不按启动回退**：主用每失败一次就当场切到保底重试，
而不是"启动时探一次、探到就锁死"。理由是网关抖动是分钟级的
（项目里有过"心跳正常但模型挂起"的实录），启动探测给出的结论很快过期。

判定为"主用不可用"的异常：连接失败、超时、429、5xx、鉴权失败。
**不**回退的情况：模型正常返回但内容不合契约（那是业务问题，
换模型也解决不了，交给调用方的重试/降级逻辑处理）。

## 用法

    from model_router import build_provider, chat, active_model
    provider = build_provider()          # agent 用
    text = await chat(system, user)      # 能力内直连用
"""

from __future__ import annotations

import asyncio
import os
import time
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any, AsyncIterator

import httpx
from wit import OpenAIChatProvider
from wit.kernel.content import ReasoningPart
from wit.providers.base import (
    Message,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ProviderCapabilities,
)

# 这些异常/状态码视为"主用这一路不可用"，值得切保底
_FALLBACK_HTTP_STATUS = {401, 403, 408, 409, 425, 429, 500, 502, 503, 504}


class ProviderUnavailable(RuntimeError):
    """主用 provider 不可用（连接/超时/鉴权/5xx）。调用方据此回退。"""


@dataclass(frozen=True)
class ModelTarget:
    name: str
    base_url: str
    api_key: str
    model: str
    label: str
    proxy: str = ""          # 该目标的出口代理（校内网关只认校园网源 IP）

    @property
    def ready(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def bupt_target() -> ModelTarget:
    return ModelTarget(
        name="bupt",
        base_url=_env("BUPT_BASE_URL"),
        api_key=_env("BUPT_API_KEY"),
        model=_env("BUPT_MODEL"),
        label="北邮网关/" + (_env("BUPT_MODEL") or "?"),
        # 服务器不在校园网内：网关只对校内源 IP 放行，故主用可单独配置出口代理。
        # 笔记本跑 start_campus_egress.sh 把 127.0.0.1:<port> 反向隧道到服务器即可。
        # 只作用于主用；保底（MiniMax）走服务器自己的公网出口，不绕这条隧道。
        proxy=_env("BUPT_HTTP_PROXY") or _env("OPENAI_HTTP_PROXY"),
    )


def fallback_target() -> ModelTarget:
    return ModelTarget(
        name="minimax",
        base_url=_env("OPENAI_BASE_URL"),
        api_key=_env("OPENAI_API_KEY"),
        model=_env("OPENAI_MODEL"),
        label="保底/" + (_env("OPENAI_MODEL") or "?"),
        proxy="",            # 保底是公网服务，不需要校园网出口
    )


def primary_enabled() -> bool:
    """主用开关：ACPS_MODEL_PRIMARY=bupt|none（默认 bupt）。"""
    return _env("ACPS_MODEL_PRIMARY", "bupt").lower() not in {"none", "off", "0", "false"}


def active_model() -> str:
    """当前会被使用的模型名（打日志/排查用）。"""
    if primary_enabled():
        target = bupt_target()
        if target.ready:
            return target.label
    return fallback_target().label


def _is_fallback_worthy(exc: BaseException) -> bool:
    """判断异常是否属于"换一路可能就好了"。"""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
        return True
    # httpx 的传输层错误（ConnectError 等）不属于 ConnectionError/OSError 家族
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, ProviderUnavailable):
        return True
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status in _FALLBACK_HTTP_STATUS
    # openai SDK 的各类 APIError 普遍带 status_code；取不到时按连接类处理
    name = type(exc).__name__
    if name in {"APIConnectionError", "APITimeoutError", "InternalServerError",
                "RateLimitError", "AuthenticationError", "PermissionDeniedError"}:
        return True
    # wit 的 OpenAIChatProvider 会把底层连接错误包装成 RuntimeError 再抛出
    # （如 "OpenAI Chat Completions request failed: ConnectError: …"，
    #   2026-10-24 现网实测：校园网隧道断线时保底因此没接住）。按消息特征识别。
    if isinstance(exc, RuntimeError):
        text = str(exc)
        if any(mark in text for mark in (
                "ConnectError", "Connection error", "All connection attempts failed",
                "APIConnectionError", "APITimeoutError", "timed out")):
            return True
    return False


def _strip_reasoning(request: ModelRequest) -> ModelRequest:
    """跨 provider 回放清洗：剥掉 assistant 消息里的 reasoning 内容。

    reasoning 只能回放给产出它的同一家 Chat 供应商（wit 严格适配器的约束），
    而主用/保底切换会让同一段会话历史先后发给两家模型——不剥就会在切换后
    第一步崩掉整轮（UnsupportedContentError，2026-10-26 现网实测）。
    reasoning 对续答没有作用（各家自产自用），切路时整体剥掉。
    """
    changed = False
    messages: list[Message] = []
    for m in request.messages:
        if m.role == "assistant" and any(isinstance(part, ReasoningPart) for part in m.content):
            messages.append(replace(m, content=[p for p in m.content
                                                if not isinstance(p, ReasoningPart)]))
            changed = True
        else:
            messages.append(m)
    return replace(request, messages=messages) if changed else request


# 本次会话最近一次实际服务的线路（run 上下文隔离）——切路判定与清洗的依据
_LAST_SERVED: ContextVar[str | None] = ContextVar("yanshu_model_last_served", default=None)


class FallbackProvider:
    """主用 + 保底的双路 provider，实现 wit 的 ModelProvider 协议。

    只包一层、不改内部实现：每次 `complete`/`stream` 先走主用，
    失败且可回退时立刻切保底重试**这一次调用**。
    """

    def __init__(self, primary: Any | None, fallback: Any,
                 *, primary_label: str = "", fallback_label: str = "") -> None:
        self._primary = primary
        self._fallback = fallback
        self.primary_label = primary_label or "primary"
        self.fallback_label = fallback_label or "fallback"
        # 统计（排查/报表用；进程内累计）
        self.stats = {"primary_ok": 0, "primary_fail": 0, "fallback_used": 0,
                      "fallback_ok": 0, "fallback_fail": 0}
        self.last_primary_error: str = ""
        self._primary_down_until = 0.0     # 熔断：主用连续失败后短暂跳过

    # ── 协议必需 ──
    @property
    def capabilities(self) -> ProviderCapabilities:
        base = self._primary if self._primary is not None else self._fallback
        return base.capabilities

    def supports_content(self, part, *, role: str):
        base = self._primary if self._primary is not None else self._fallback
        fn = getattr(base, "supports_content", None)
        if callable(fn):
            return fn(part, role=role)
        return True, None

    # ── 熔断：主用连续失败后 60s 内直接走保底，避免每条调用都先等一次超时 ──
    def _primary_skippable(self) -> bool:
        return self._primary is None or time.monotonic() < self._primary_down_until

    def _trip(self) -> None:
        cooldown = float(_env("ACPS_MODEL_PRIMARY_COOLDOWN", "60") or 60)
        self._primary_down_until = time.monotonic() + max(0.0, cooldown)

    def _reset_trip(self) -> None:
        self._primary_down_until = 0.0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not self._primary_skippable():
            last = _LAST_SERVED.get()
            req = _strip_reasoning(request) if last not in (None, "primary") else request
            try:
                response = await self._primary.complete(req)
                _LAST_SERVED.set("primary")
                self.stats["primary_ok"] += 1
                self._reset_trip()
                return response
            except Exception as exc:  # noqa: BLE001
                self.stats["primary_fail"] += 1
                self.last_primary_error = f"{type(exc).__name__}: {str(exc)[:200]}"
                if not _is_fallback_worthy(exc):
                    raise            # 内容/契约类问题：换模型也白搭，原样抛给上层
                self._trip()
                if not _env("ACPS_MODEL_SILENT", ""):
                    print(f"[model] 主用不可用（{self.primary_label}）→ 切保底："
                          f"{self.last_primary_error}", flush=True)
        self.stats["fallback_used"] += 1
        last = _LAST_SERVED.get()
        req = _strip_reasoning(request) if last not in (None, "fallback") else request
        try:
            response = await self._fallback.complete(req)
            _LAST_SERVED.set("fallback")
            self.stats["fallback_ok"] += 1
            return response
        except Exception:
            self.stats["fallback_fail"] += 1
            raise

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        # 流式一旦开始吐字就不能中途换路（会产出半截重复内容），
        # 因此只在"第一个事件之前"回退。
        if not self._primary_skippable():
            try:
                agen = self._primary.stream(request)
                first = await agen.__anext__()
            except StopAsyncIteration:
                self.stats["primary_ok"] += 1
                return
            except Exception as exc:  # noqa: BLE001
                self.stats["primary_fail"] += 1
                self.last_primary_error = f"{type(exc).__name__}: {str(exc)[:200]}"
                if not _is_fallback_worthy(exc):
                    raise
                self._trip()
                if not _env("ACPS_MODEL_SILENT", ""):
                    print(f"[model] 主用流式不可用 → 切保底：{self.last_primary_error}",
                          flush=True)
            else:
                self.stats["primary_ok"] += 1
                self._reset_trip()
                yield first
                async for event in agen:
                    yield event
                return

        self.stats["fallback_used"] += 1
        async for event in self._fallback.stream(request):
            self.stats["fallback_ok"] += 1
            yield event

    async def aclose(self) -> None:
        for provider in (self._primary, self._fallback):
            closer = getattr(provider, "aclose", None)
            if callable(closer):
                try:
                    await closer()
                except Exception:  # noqa: BLE001
                    pass


# ── 直连路径（llm_tools.chat 用）──────────────────────────────────────────────

async def chat_via(target: ModelTarget, system: str, user: str, *,
                   temperature: float, max_tokens: int | None,
                   timeout: float) -> tuple[str, dict]:
    """一次 chat completion。返回 (正文, 诊断信息)。失败抛异常由调用方决定回退。"""
    payload: dict[str, Any] = {
        "model": target.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
    }
    if max_tokens:
        payload["max_tokens"] = int(max_tokens)
    headers = {"Authorization": f"Bearer {target.api_key}"}
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        resp = await client.post(target.base_url.rstrip("/") + "/chat/completions",
                                 headers=headers, json=payload)
    if resp.status_code in _FALLBACK_HTTP_STATUS:
        raise ProviderUnavailable(f"HTTP {resp.status_code}: {resp.text[:180]}")
    resp.raise_for_status()
    data = resp.json()
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = message.get("content") or ""
    diag = {
        "model": target.model,
        "finish_reason": choice.get("finish_reason"),
        "reasoning_chars": len(message.get("reasoning_content") or ""),
        "usage": data.get("usage") or {},
    }
    return content, diag


def ordered_targets() -> list[ModelTarget]:
    """按优先级返回可用目标：主用在前，保底在后。"""
    targets: list[ModelTarget] = []
    if primary_enabled():
        primary = bupt_target()
        if primary.ready:
            targets.append(primary)
    fallback = fallback_target()
    if fallback.ready:
        targets.append(fallback)
    return targets


def _make_provider(target: ModelTarget, timeout: float):
    """按目标构造 OpenAIChatProvider；带代理的目标走专用 http client。"""
    kwargs: dict[str, Any] = {
        "model": target.model,
        "api_key": target.api_key,
        "base_url": target.base_url,
        "timeout": timeout,
        "temperature": 0,
    }
    if target.proxy:
        kwargs["transport"] = httpx.AsyncHTTPTransport(proxy=target.proxy, trust_env=False)
    return OpenAIChatProvider(**kwargs)


def build_provider():
    """构造 wit 用的 provider：主用 + 保底。

    **保底总是保留**，即使主用此刻可用——网关抖动是分钟级的，
    等真出问题再切换已经太晚。
    """
    timeout = float(_env("OPENAI_TIMEOUT", "300") or 300)
    fallback = fallback_target()
    if not fallback.ready:
        # 连保底都没有：只能用主用；主用也没有就按老行为炸出来，别静默降级
        primary = bupt_target()
        if not primary.ready:
            raise RuntimeError("没有任何可用模型配置：BUPT_* 与 OPENAI_* 都不完整")
        return _make_provider(primary, timeout)

    fallback_obj = _make_provider(fallback, timeout)
    if primary_enabled():
        primary = bupt_target()
        if primary.ready:
            return FallbackProvider(
                _make_provider(primary, timeout), fallback_obj,
                primary_label=primary.label, fallback_label=fallback.label,
            )
    return fallback_obj
