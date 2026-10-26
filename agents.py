"""三个科研 agent 的装配：查阅资料 / 综合资料 / 论文写作。"""

import os
from pathlib import Path

from wit import OpenAIChatProvider, WitAgent

from capabilities.arxiv import fetch_paper, search_papers
from hooks import MaterialContext, RunLogger, ToolRetry

_ENV_PATH = Path(__file__).parent / ".env"


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


def build_provider():
    """构造模型 provider：**北邮网关 deepseek 主用，MiniMax 保底**。

    路由逻辑收在 `acps_cluster/model_router.py`（`llm_tools.chat` 那条直连路径
    也复用它的配置解析，保证两条路径口径一致）。这里只在外面再包一层
    响应级看门狗——它管的是"单次流挂死"，与"换模型"是两件事，互不替代。

    回退开关：`ACPS_MODEL_PRIMARY=none` 可临时禁用主用（例如网关维护期间）。
    """
    try:
        import model_router  # noqa: PLC0415
    except ImportError:  # 集群目录不在 sys.path 时退回旧行为，不因路由缺失而不可用
        provider = OpenAIChatProvider(
            model=os.environ["OPENAI_MODEL"],
            api_key=os.environ["OPENAI_API_KEY"],
            base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            timeout=float(os.getenv("OPENAI_TIMEOUT", "300")),
            temperature=0,
        )
        return _with_response_deadline(provider)

    provider = model_router.build_provider()
    if os.getenv("ACPS_MODEL_LOG_ROUTE", "1").strip().lower() not in {"0", "false", "no", "off"}:
        print(f"[model] 路由: {model_router.active_model()}", flush=True)
    return _with_response_deadline(provider)


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
def _with_response_deadline(provider):  # noqa: ANN001, ANN202
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



def build_search_agent(provider) -> WitAgent:
    return WitAgent(
        id="research.searcher",
        display_name="资料查阅Agent",
        instructions=(
            "你是科研资料调研员。针对用户给定的研究问题，"
            "先用 arxiv_search 从多个关键词角度检索（英文关键词效果更好），"
            "对最相关的论文可用 arxiv_fetch_paper 补充细节。"
            "最终输出一份资料清单：每篇论文包含 arXiv ID、标题、作者、年份，"
            "以及 2-3 句与问题相关的要点概括。不要编造论文。"
        ),
        model=provider,
        capabilities=[search_papers, fetch_paper],
        hooks=[RunLogger(), ToolRetry()],
    )


def build_synthesizer_agent(provider) -> WitAgent:
    return WitAgent(
        id="research.synthesizer",
        display_name="资料综合Agent",
        instructions=(
            "你是文献综合分析员。用户消息是研究问题，系统上下文中附有检索到的资料清单。"
            "请综合这些资料：归纳研究主题与关键发现、比较不同论文的方法与结论、"
            "指出现有研究的共识、分歧与空白，并给出论文结构建议。"
            "只依据提供的资料，标注结论来自哪篇论文（用 arXiv ID）。"
        ),
        model=provider,
        hooks=[RunLogger()],
    )


def build_writer_agent(provider, materials: str) -> WitAgent:
    return WitAgent(
        id="research.writer",
        display_name="论文写作Agent",
        instructions=(
            "你是学术论文写作员。基于系统上下文中提供的研究资料与综合分析，"
            "围绕用户给定的研究问题撰写结构完整的综述论文（Markdown 格式），"
            "包含：标题、摘要、引言、相关工作、主体章节、讨论与展望、参考文献。"
            "引用论文时使用 [arXiv ID] 形式，不得虚构文献。"
            "如果用户消息中包含审稿意见，请逐条修改（标注【阻塞】的意见必须全部修复，"
            "【建议】可酌情采纳），并输出修改后的完整论文（不要只给 diff 或修改说明）。"
        ),
        model=provider,
        hooks=[RunLogger(), MaterialContext("研究资料与综合分析", materials)],
    )


def build_reviewer_agent(provider, source_list: str) -> WitAgent:
    return WitAgent(
        id="research.reviewer",
        display_name="论文审批Agent",
        instructions=(
            "你是严格的论文审稿人。用户会提供一篇论文全文，并在系统上下文中提供"
            "其依据的检索资料清单。请从以下方面审稿：\n"
            "1. 引用真实性：论文中引用的每个 [arXiv ID] 是否都出现在资料清单中"
            "（ID 比较时忽略 v1/v2 等版本后缀），"
            "论点对论文的概括是否与资料摘要一致，有无虚构或张冠李戴；\n"
            "2. 结构完整性：摘要/引言/相关工作/主体/讨论/参考文献是否齐全、层次清晰；\n"
            "3. 内容质量：是否有明显逻辑错误、夸大结论、无依据断言；\n"
            "4. 学术规范：术语、时态、客观表述。\n\n"
            "输出格式（严格遵守）：\n"
            "第一行只写 `VERDICT: APPROVE` 或 `VERDICT: REVISE`；\n"
            "第二行只写 `BLOCKING: <阻塞意见条数>`（无阻塞意见时写 0）；\n"
            "随后给出编号的审稿意见列表，每条正文以【阻塞】或【建议】标签开头，"
            "标签只用于条目标注，不要出现在其它位置；每条指明位置、问题、修改建议。\n\n"
            "分级标准：\n"
            "【阻塞】——引用错误/虚构文献/论点与资料不符、章节缺失或结构混乱、"
            "明显逻辑错误或无依据断言。存在任一【阻塞】意见时第一行必须为 REVISE；\n"
            "【建议】——措辞润色、可选补充、出版信息等不影响结论可靠性的改进。"
            "仅【建议】时才可以 APPROVE，并保留建议供作者参考。"
        ),
        model=provider,
        hooks=[RunLogger(), MaterialContext("检索资料清单（引用核验依据）", source_list)],
    )
