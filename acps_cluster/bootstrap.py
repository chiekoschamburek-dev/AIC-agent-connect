"""集群启动引导：`.env` 加载、模型 provider 构建、LLM 调用硬闸。

历史上这三件事放在仓库根的 `agents.py`（同时含早期单 agent 示例）。整合后示例已
移除，只保留运行时必需的部分，供 supervisor、各 partner 与复现/演示脚本共用：

- **导入即加载**仓库根 `.env`（已存在的环境变量优先、不覆盖；空值行跳过）；
- `build_provider()` 统一模型路由（北邮网关 deepseek 主用、MiniMax 保底），
  并给每次模型调用套上响应级硬闸；
- `with_response_deadline()` 把"卡死"变成"可观测的失败"——`OPENAI_TIMEOUT`
  传给 httpx 后只是**读间隔**超时，不是整次调用总时长。

调用方约定：`import bootstrap` 即完成 `.env` 加载（与旧 `import agents` 等价），
再按需调用 `build_provider()`。
"""

from __future__ import annotations

import os
from pathlib import Path

# 仓库根（acps_cluster/ 的上一级），与旧 agents.py 使用同一个 `.env`。
_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def load_env_file(path: Path = _ENV_PATH) -> None:
    """加载 .env 键值对；已存在的环境变量优先，不依赖第三方库。

    空值行（`KEY=`）跳过：写进 os.environ 的空串会让 `os.getenv(KEY, 默认)` 的默认值失效，
    下游 `int(os.getenv("ACPS_*_PORT", "8444"))` 直接 ValueError 崩在启动路上。
    """
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and value and key not in os.environ:
            os.environ[key] = value


load_env_file()


def _model_router():
    """按当前 sys.path 形态取模型路由模块；取不到返回 None（退回单 provider）。

    兼容两种导入姿势：扁平（集群目录在 sys.path，生产/脚本路径）与包式
    （`acps_cluster.model_router`，测试路径）。
    """
    try:
        from acps_cluster import model_router  # noqa: PLC0415
        return model_router
    except ImportError:
        try:
            import model_router  # noqa: PLC0415
            return model_router
        except ImportError:
            return None


def build_provider():
    """构造模型 provider：**北邮网关 deepseek 主用，MiniMax 保底**。

    路由逻辑收在 `acps_cluster/model_router.py`（`llm_tools.chat` 那条直连路径
    也复用它的配置解析，保证两条路径口径一致）。这里只在外面再包一层
    响应级看门狗——它管的是"单次流挂死"，与"换模型"是两件事，互不替代。

    回退开关：`ACPS_MODEL_PRIMARY=none` 可临时禁用主用（例如网关维护期间）。
    """
    router = _model_router()
    if router is None:
        # 集群目录不在 sys.path 时退回旧行为，不因路由缺失而不可用
        from wit import OpenAIChatProvider  # noqa: PLC0415

        provider = OpenAIChatProvider(
            model=os.environ["OPENAI_MODEL"],
            api_key=os.environ["OPENAI_API_KEY"],
            base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            timeout=float(os.getenv("OPENAI_TIMEOUT", "300")),
            temperature=0,
        )
        return with_response_deadline(provider)

    provider = router.build_provider()
    if os.getenv("ACPS_MODEL_LOG_ROUTE", "1").strip().lower() not in {"0", "false", "no", "off"}:
        print(f"[model] 路由: {router.active_model()}", flush=True)
    return with_response_deadline(provider)


# ── 模型调用硬闸（响应级看门狗）─────────────────────────────────────────────
# 背景（2026-10-10 实测）：`OPENAI_TIMEOUT` 传给 httpx 后是**每次读间隔**的超时，不是整次调用的
# 总时长——provider 只要偶尔送一个字节，请求就能无限期挂着。现场表现：
#   · scout 的入站任务在 `working` 状态卡 22 分钟，进程 CPU 0.0%、主线程在 select() 等 I/O；
#   · 任务内部只有 1 条消息 = 它连第一次模型输出都没拿到；
#   · 而同一 provider 用 curl 直测 2.7 秒就返回（说明 provider 正常，是单次流被挂住）。
# 后果：leader 侧 `wait_partner` 永远等不到终态，只能反复超时后放弃。
# 因此这里给**每次模型调用**加一条自己的硬闸：
#   · 首个响应片段超过 ACPS_LLM_FIRST_BYTE_TIMEOUT（默认 120s）→ 中止并抛错；
#   · 整次调用超过 ACPS_LLM_CALL_TIMEOUT（默认 600s）→ 中止并抛错。
# 抛错后 wit 会按自己的错误路径处理（本次调用的工具/任务以失败收尾），
# 于是「卡死」变成「失败」——失败是可观测、可重试、可如实上报的，卡死不是。
def with_response_deadline(provider):  # noqa: ANN001, ANN202
    if os.getenv("ACPS_LLM_DEADLINE", "1").lower() in ("0", "false", "no", "off"):
        return provider
    # 幂等：重复包装会让 _stream 调到自己（无限递归），这里加标记挡住
    if getattr(provider, "_yanshu_deadline_guard", False):
        return provider
    try:
        first_byte = float(os.getenv("ACPS_LLM_FIRST_BYTE_TIMEOUT", "120"))
        total = float(os.getenv("ACPS_LLM_CALL_TIMEOUT", "600"))
    except ValueError:
        return provider

    import asyncio
    import time

    name = type(provider).__name__
    # 关键：先把原方法抓在手里。否则替换后 `provider.stream(...)` 会解析到包装函数自己
    # ——生成器委派给自身，直接 RecursionError（2026-10-10 实测踩到）。
    orig_stream = provider.stream
    orig_complete = provider.complete

    def _too_slow(phase: str, limit: float, seen: int) -> TimeoutError:
        return TimeoutError(
            f"model call stalled: {phase} 超过 {limit:.0f}s（已收到 {seen} 个片段）"
            f" —— provider={name}。已中止本次调用以避免无限挂起；"
            f"可用 ACPS_LLM_FIRST_BYTE_TIMEOUT / ACPS_LLM_CALL_TIMEOUT 调整。"
        )

    async def _complete(request):  # noqa: ANN001, ANN202
        try:
            return await asyncio.wait_for(orig_complete(request), timeout=total)
        except asyncio.TimeoutError as exc:  # noqa: F821
            raise _too_slow("整次调用", total, 0) from exc

    async def _stream(request):  # noqa: ANN001, ANN202
        started = time.monotonic()
        seen = 0
        agen = orig_stream(request).__aiter__()
        while True:
            timeout = first_byte if seen == 0 else max(1.0, total - (time.monotonic() - started))
            try:
                item = await asyncio.wait_for(agen.__anext__(), timeout=timeout)
            except StopAsyncIteration:
                return
            except asyncio.TimeoutError as exc:  # noqa: F821
                raise _too_slow("首个片段" if seen == 0 else "整次调用",
                                first_byte if seen == 0 else total, seen) from exc
            seen += 1
            yield item

    provider.complete = _complete       # type: ignore[method-assign]
    provider.stream = _stream           # type: ignore[method-assign]
    provider._yanshu_deadline_guard = True   # type: ignore[attr-defined]
    return provider


# 兼容旧调用名（脚本/测试曾用 `agents._with_response_deadline`）。
_with_response_deadline = with_response_deadline
