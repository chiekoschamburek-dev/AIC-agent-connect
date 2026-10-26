"""partner 能力内直连 LLM 的小工具（复用 .env 的 OPENAI_* 配置，与 wit runtime 无关）。

网关偶发挂起（TCP 已连接但长时间不回数据，CPU 全闲），这里用 wait_for 硬超时 +
换连接重试兜底，避免伙伴任务被一条卡死的 HTTP 请求永久挂住。
"""

import asyncio
import json
import os
import re

import httpx
from openai import AsyncOpenAI

_client: AsyncOpenAI | None = None
_clients: dict[str, AsyncOpenAI] = {}     # 按 (base_url|api_key) 分桶：主用/保底各一份


def _client_for(base_url: str, api_key: str, proxy: str = "") -> AsyncOpenAI:
    """取该目标专用的 client（连接池不跨目标复用，避免把 A 的故障状态带去 B）。"""
    key = f"{base_url}|{api_key[-8:]}|{proxy}"
    client = _clients.get(key)
    if client is None:
        timeout = float(os.getenv("OPENAI_TIMEOUT", "300"))
        client_kwargs = {}
        if proxy:
            # 校内网关只认校园网源 IP，模型流量要经笔记本反向隧道出去；
            # 只挂在这个 client 上，平台心跳/peer mTLS/arxiv 仍走服务器自己的出口。
            client_kwargs["http_client"] = httpx.AsyncClient(
                proxy=proxy, trust_env=False, timeout=timeout
            )
        client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            max_retries=0,  # 重试由本模块控制（含换连接），避免 SDK 静默 3×300s
            **client_kwargs,
        )
        _clients[key] = client
    return client


def extract_json_object(text: str) -> dict:
    """取模型输出里的第一个 JSON 对象（允许 ```json 围栏与前后说明文字）。

    不能贪心匹配 `\\{.*\\}`：模型输出两个对象（先回显输入再给答案）时，那会框住
    "第一个 { 到最后一个 }"整段，json.loads 报 Extra data → 伙伴任务白失败一次。
    """
    blob = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", blob, re.S)
    if fence:
        blob = fence.group(1)
    decoder = json.JSONDecoder()
    for idx, char in enumerate(blob):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(blob[idx:])
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError(f"模型未返回 JSON: {blob[:200]}")


class LLMOutputError(RuntimeError):
    """模型输出不符合契约（如被截断）；不是连接故障，不能重置进程级 client。"""


def _route_targets():
    """按优先级返回 [(label, model, base_url, api_key, proxy)]：北邮网关在前，MiniMax 保底在后。

    路由配置与 agent 的 provider 共用 `model_router`，避免两套口径漂移。
    **兜底**：model_router 不可用、或它认为"两个目标都没配全"时，退回纯 OPENAI_*
    （旧行为）——这样单测/离线脚本在没有 .env 键时仍能跑，不会因路由改造而不可用。
    """
    try:
        from acps_cluster.core import model_router  # noqa: PLC0415
    except ImportError:
        model_router = None  # type: ignore[assignment]

    if model_router is not None:
        targets = [(t.label, t.model, t.base_url, t.api_key, t.proxy)
                   for t in model_router.ordered_targets()]
        if targets:
            return targets

    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return []
    return [("default", os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
             os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
             api_key, _env_proxy())]


def _env_proxy() -> str:
    return (os.getenv("OPENAI_HTTP_PROXY") or "").strip()


async def _reset_client() -> None:
    """关闭并丢弃全部 client（换连接重试用）。"""
    global _client
    for key, client in list(_clients.items()):
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass
        _clients.pop(key, None)
    if _client is not None:
        try:
            await _client.close()
        except Exception:  # noqa: BLE001
            pass
        _client = None


async def chat(system: str, user: str, *, temperature: float = 0.4,
               max_tokens: int | None = None, strict_length: bool = False) -> str:
    """`strict_length=True` 时把"输出被 max_tokens 截断"当作失败抛出。

    截断的输出对下游是解析失败而非内容偏短：代码生成这类调用需要的是可执行反馈
    （压短规格/换单文件），而不是撞一次 JSON 报错（2026-10-17 真链路实测：codegen
    在 `LOSS_DELTA_TOL` 处被切断，两轮重试都只看到"JSON 不完整"）。

    ## 路由

    **每个目标先试 2 次（换连接重试），失败再轮到下一个目标**：
    北邮网关 deepseek 主用 → MiniMax 保底。两路都挂了才抛错。

    为什么不是"全局切换"：一次失败可能只是这条连接坏了，
    立刻换模型会让同一批任务出现模型不一致的结果；而在单目标内重试，
    既修掉了连接抖动，又保住了结果一致性。
    """
    per_attempt = float(os.getenv("ACPS_LLM_TIMEOUT", "180"))
    targets = _route_targets()
    if not targets:
        raise RuntimeError("没有任何可用模型配置（BUPT_* 与 OPENAI_* 都不完整）")

    last: Exception | None = None
    for target_label, model_name, base_url, api_key, proxy in targets:
        for attempt in range(2):
            try:
                params: dict = {
                    "model": model_name,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": temperature,
                }
                if max_tokens:
                    params["max_tokens"] = int(max_tokens)
                resp = await asyncio.wait_for(
                    _client_for(base_url, api_key, proxy).chat.completions.create(**params),
                    timeout=per_attempt,
                )
                choice = resp.choices[0]
                content = choice.message.content or ""
                reason = getattr(choice, "finish_reason", None)
                if strict_length and (reason == "length" or not content.strip()):
                    # 推理型模型的 thinking 会吃满 completion 预算：finish=length 且正文为空，
                    # 此时下游只会看到"解析不出 JSON"，看不出是预算问题（2026-10-17 实测：
                    # MiniMax-M3.1-Flash 在 8000 下 100% 空正文，24000 才自然结束；
                    # 2026-10-19 复测 deepseek-v4-flash 在 800 下同样空正文）。
                    thinking = len(getattr(choice.message, "reasoning_content", None) or "")
                    raise LLMOutputError(
                        f"模型未产出正文（finish_reason={reason}, max_tokens={int(max_tokens or 0)},"
                        f" 正文字符={len(content)}, thinking字符={thinking}, 模型={model_name}）"
                        f"——thinking 预算挤占正文时请提高 max_tokens 或改用非推理模型")
                return content
            except LLMOutputError as exc:
                # 输出契约失败与连接无关：保留 client 供其他并发调用继续使用，
                # 但仍按既有重试策略再试一次（同一目标内，不换模型）。
                last = exc
                if attempt == 0:
                    await asyncio.sleep(3)
            except Exception as e:  # noqa: BLE001  超时/网络类统一换连接重试
                last = e
                await _reset_client()
                if attempt == 0:
                    await asyncio.sleep(3)
        # 该目标两次都失败：换下一个（保底）
        if len(targets) > 1 and not target_label.endswith("保底"):
            print(f"[llm_tools] {target_label} 不可用（{type(last).__name__}）→ 切下一个目标",
                  flush=True)
    raise RuntimeError(f"LLM 调用失败（{type(last).__name__}）: {str(last)[:200]}")
