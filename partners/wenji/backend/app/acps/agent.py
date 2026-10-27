"""WIT Agent 声明：问迹 = AIP Partner（能力提供方）+ 可选 Leader（发现/委派）。

- Partner 侧：对梧桐平台暴露 ``wenji.*`` 能力，使用 ``AcpsDirect(modes=("rpc",))``；
- Leader 侧（``ACPS_DELEGATE_ENABLED=1``，默认开）：通过 ``AipDelegationHooks``
  注入 ``acps_discover / acps_delegate / acps_task`` 工具，把不属于本地能力
  的子任务委派给平台上队友的智能体；
- AMP：``AmpAccessLoggingHooks`` 记录入站/出站访问（平台按 task 统计），
  ``AmpMetricsHooks`` 上报进程内任务负载。
"""

from __future__ import annotations

from .capabilities import CAPABILITIES
from .provider import build_model_provider
from .settings import AcpsSettings


def collect_amp_metrics(context):
    """AMP 周期 metrics collector（由 WIT 按周期调用）。"""
    from .bridge import task_metrics

    return task_metrics()

INSTRUCTIONS = """\
你是「问迹」——一个可解释的问数/问答智能体，通过 AIP 国标对外提供能力。

【本地能力，优先使用】
- wenji_ask(question)：综合问数/问答。问题可能同时涉及业务数据库与知识库文档，
  内核会自动规划、执行并给出带引用、可追溯的答案。收到问数/问答任务时，
  直接调用它并原样传入用户问题，不要自行拆解或改写问题。
- wenji_data_query(question)：只做结构化问数，返回 SQL 与数据行。
- wenji_doc_qa(question)：只做知识库文档问答，返回带文档名/页码的引用。
- wenji_formula_eval(question)：文档公式计算（如销售提成），返回计算步骤与引用。
- wenji_schema()：查看业务数据库表结构。

【协作能力，仅当本地能力无法满足时启用】
- acps_discover：用自然语言描述需求，发现平台上其它智能体；
- acps_delegate：把子任务委派给选中的智能体；
- acps_task：用 action="wait" 等待结果，或 inspect/continue/complete 任务。

【规则】
1. 问数、文档问答、跨源问题一律先用本地 wenji_ask 解决。
2. 只有明显超出本地能力范围的子任务（例如队友智能体负责的其它领域），
   才先 acps_discover 再 acps_delegate；委派后先 wait，拿回结果再汇总。
3. 最终答复保持简洁（3~6 句），并标明数据来源（数据库 / 文档名）。
4. 无法完成或证据不足时如实说明，不要编造。
"""


def build_agent(settings: AcpsSettings):
    """按配置构造 ``WitAgent``（本函数不访问网络）。"""
    from wit import (
        AcpsDirect,
        AipDelegationHooks,
        AmpAccessLoggingHooks,
        AmpMetricsHooks,
        WitAgent,
    )

    from .task_access import TaskAccessHooks

    hooks: list = []
    if settings.delegate_enabled:
        hooks.append(AipDelegationHooks(delivery=settings.delegate_delivery))

    # 平台计数口径：任务级 access（correlation_id=taskId），见 task_access.py
    aip_hooks: list = [TaskAccessHooks()]

    # 声明与已批准 ACS 的一致性：wit 要求 draft == final 才能启动服务。
    # v1.0.0 = 平台已批准的声明（4 技能，rpc）；v1.1.0 起 = stream 模式 + 公式技能。
    # 升级流程：改 ACPS_AGENT_VERSION=1.1.0 → register 提交新草稿 → 平台批准 → 重启 serve。
    advanced = tuple(int(x) for x in settings.version.split(".") if x.isdigit()) >= (1, 1)
    modes = ("rpc", "stream") if advanced else ("rpc",)
    capabilities = list(CAPABILITIES) if advanced else [
        c for c in CAPABILITIES if c.capability.id != "wenji.formula_eval"
    ]

    return WitAgent(
        id=settings.agent_id,
        display_name=settings.display_name,
        description=settings.description,
        version=settings.version,
        instructions=INSTRUCTIONS,
        model=build_model_provider(),
        capabilities=capabilities,
        protocols=[AcpsDirect(modes=modes)],
        hooks=hooks,
        aip_hooks=aip_hooks,
        # AMP 接入（按《在 Agent 中接入 AMP 可观测性》教程）：
        # - AmpAccessLoggingHooks：Direct transport 边界的 access 记录（每 RPC 一条）；
        # - AmpMetricsHooks：周期指标（真实并发/成功率/P50-P95）；
        # 任务级 access（correlationId=taskId，平台配对计数）由 aip_hooks 的
        # TaskAccessHooks 补发（serve 侧），leader 侧见 leader_access.py —— 双侧齐全。
        amp_hooks=[
            AmpAccessLoggingHooks(),
            AmpMetricsHooks(collect_amp_metrics),
        ],
        metadata={
            "product": "问迹 WenJi",
            "domain": ["NL2SQL", "RAG", "跨源多跳", "可解释性"],
            "trace_api": "/api/trace/{turn_id}",
        },
    )
