"""本地 LLM 反向代理：把 WIT 包装层的 OpenAI 请求转到 BUPT 校园网关（免费额度）。

为什么需要它：

- WIT 的 ``OpenAIChatProvider`` 不接受自定义 HTTP 代理，且 ``myai.bupt.edu.cn`` 解析到
  校园内网（10.3.19.2），公网服务器无法直连；
- 它也不经过引擎的记账层（``app/core/llm.py``），所以此前包装层的 DeepSeek 调用
  在成本账本里显示 ¥0、但实际在扣费（实测 4 天约 ¥64）。

本代理监听 ``127.0.0.1:18999``：

- 把 ``/v1/chat/completions``（含 SSE 流式）转发到 ``BUPT_BASE_URL``，
  经校园网 18888 HTTP CONNECT 隧道出网；
- 自动注入 ``chat_template_kwargs.enable_thinking=false``（vLLM 扩展，显著降延迟）；
- 上游密钥由服务端持有，WIT 侧只需填任意占位 key。

启动：``systemctl --user start wenji-llm-proxy``（见 tools/systemd/units/）。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

# 允许直接以 systemd 启动（不依赖外层 dotenv）：从仓库根 .env 读取 BUPT_* 等配置
try:
    from dotenv import load_dotenv

    _REPO = Path(__file__).resolve().parents[3]
    load_dotenv(_REPO / ".env", override=False)
    load_dotenv(_REPO / ".env.acps", override=False)
except Exception:  # pragma: no cover
    pass

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

UPSTREAM = os.environ.get("BUPT_BASE_URL", "https://myai.bupt.edu.cn/llm-gw/v1").rstrip("/")
API_KEY = os.environ.get("BUPT_API_KEY", "")
PROXY = os.environ.get("BUPT_PROXY_URL", "http://127.0.0.1:18888")
STATS_PATH = Path(os.environ.get("LLM_PROXY_STATS", "var/llm_proxy_stats.json"))

app = FastAPI(title="WenJi LLM Proxy (BUPT)", docs_url=None, redoc_url=None)
_client: httpx.AsyncClient | None = None
_stats = {"requests": 0, "streams": 0, "errors": 0, "started_at": time.time()}


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(proxy=PROXY, timeout=httpx.Timeout(180.0, connect=20.0))
    return _client


def _flush_stats() -> None:
    try:
        STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATS_PATH.write_text(json.dumps(_stats, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "upstream": UPSTREAM, "proxy": PROXY, "stats": _stats}


@app.get("/v1/models")
async def models() -> JSONResponse:
    r = await _get_client().get(f"{UPSTREAM}/models", headers={"Authorization": f"Bearer {API_KEY}"})
    return JSONResponse(r.json(), status_code=r.status_code)


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    # 关思考：显著降低 deepseek-v4-flash / qwen 的时延
    body.setdefault("chat_template_kwargs", {})
    if isinstance(body["chat_template_kwargs"], dict):
        body["chat_template_kwargs"].setdefault("enable_thinking", False)
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

    _stats["requests"] += 1
    if body.get("stream"):
        _stats["streams"] += 1
        req = _get_client().build_request(
            "POST", f"{UPSTREAM}/chat/completions", json=body, headers=headers
        )
        resp = await _get_client().send(req, stream=True)

        async def _gen():
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
            finally:
                await resp.aclose()
                _flush_stats()

        return StreamingResponse(_gen(), media_type="text/event-stream")

    try:
        r = await _get_client().post(f"{UPSTREAM}/chat/completions", json=body, headers=headers)
    except Exception as exc:  # 上游不可达时给出明确错误
        _stats["errors"] += 1
        _flush_stats()
        return JSONResponse({"error": {"message": f"upstream error: {exc}"}}, status_code=502)
    _flush_stats()
    return JSONResponse(r.json(), status_code=r.status_code)
