"""WIT 包装层使用的模型 Provider（OpenAI 兼容端点）。

优先读取 ``ACPS_LLM_*``；未配置时回落到仓库既有 LLM 配置
（DeepSeek / Qwen / SiliconFlow / OPENAI_*），保证平台侧调用与问迹引擎使用同一套凭据。
"""

from __future__ import annotations

import os

from .settings import _env_str


def _first(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


def resolve_llm_config() -> dict[str, str]:
    """解析 ``base_url`` / ``api_key`` / ``model``，不触发任何网络调用。"""
    provider = _env_str("LLM_PROVIDER", "deepseek").lower()

    if provider == "qwen":
        default_base = _env_str("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        default_key = _first("QWEN_API_KEY")
        default_model = _env_str("QWEN_MODEL", "qwen-plus")
    elif provider == "siliconflow":
        default_base = _env_str("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1")
        default_key = _first("SILICONFLOW_API_KEY")
        default_model = _env_str("LLM_MODEL", "deepseek-ai/DeepSeek-V3")
    else:  # deepseek（主力）
        default_base = _env_str("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
        default_key = _first("DEEPSEEK_API_KEY")
        default_model = _env_str("LLM_MODEL", "deepseek-chat")
    # DeepSeek 官方 base_url 兼容带或不带 /v1；统一补 /v1 以走 OpenAI 兼容面
    if default_base.rstrip("/") == "https://api.deepseek.com":
        default_base = "https://api.deepseek.com/v1"

    return {
        "base_url": _env_str("ACPS_LLM_BASE_URL", default_base),
        "api_key": _env_str("ACPS_LLM_API_KEY", default_key),
        "model": _env_str("ACPS_LLM_MODEL", default_model),
    }


def build_model_provider():
    """构造 WIT 的 ``OpenAIChatProvider``（导入延迟，方便无 wit 环境下单测 bridge）。"""
    import os

    from wit import OpenAIChatProvider

    cfg = resolve_llm_config()
    if not cfg["api_key"]:
        raise RuntimeError(
            "ACPS 模型未配置：请设置 ACPS_LLM_API_KEY 或 DEEPSEEK_API_KEY（.env）"
        )
    kwargs: dict = dict(
        model=cfg["model"],
        api_key=cfg["api_key"],
        base_url=cfg["base_url"],
        temperature=0,
    )
    # 校园网关解析到内网地址，需经反向隧道代理；
    # 其它厂商（DeepSeek 等）直连即可。
    proxy = os.environ.get("ACPS_LLM_PROXY_URL", "").strip()
    if proxy:
        import httpx

        kwargs["transport"] = httpx.AsyncHTTPTransport(proxy=proxy, trust_env=False)
    # 思考型模型可关思考降延迟（vLLM chat_template_kwargs）
    if os.environ.get("ACPS_LLM_ENABLE_THINKING", "0").strip().lower() in {"1", "true", "on"}:
        kwargs["headers"] = {"X-Wenji-Thinking": "on"}
    return OpenAIChatProvider(**kwargs)
