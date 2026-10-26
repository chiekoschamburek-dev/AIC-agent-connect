"""研枢-Frontier-Partner：速度前沿探测（为选题服务，不为写作服务）。

与 Scout 的分工：Scout 回答"这个主题上有什么文献"，Frontier 回答"什么方向正在爆发、
现在进场晚不晚"。做法是用 OpenAlex 引用图谱算 citation velocity（总引用 / 月龄），
在近 6-12 个月的窗口里找增速最高的新工作，再聚合成方向清单。

运行：python acps_cluster/partner_frontier.py   （常驻，端口默认 8450）
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wit import AcpsDirect, WitAgent, capability  # noqa: E402

import cluster_common  # noqa: E402
from discovery_metadata import (  # noqa: E402
    agent_discovery_description,
    apply_to_capabilities,
)
import llm_tools  # noqa: E402
from wit.hooks.acps.amp import AmpAccessLoggingHooks  # noqa: E402
from aip_guard import AipPartnerGuardHooks
from delegation import build_pep_hooks, capability_visibility, split_pep_hooks  # noqa: E402
from delegation.bridge import BridgeHooks  # noqa: E402
from partner_workspace_client import (  # noqa: E402
    WORKSPACE_ACCESS_NOTE,
    WorkspaceAccessBudgetHooks,
    build_workspace_access_capabilities,
)
from workspace_access import enabled as workspace_access_enabled  # noqa: E402
from wit.hooks.acps.delegation import AipDelegationHooks  # noqa: E402
from capabilities.frontier import (  # noqa: E402
    ENTRY_WINDOWS,
    FrontierReport,
    topic_density,
    validate_topics,
)
from capabilities.scholar import openalex_emerging, s2_citation_profile  # noqa: E402

WORKSPACE = Path(__file__).parent / "ws-frontier"

_SYSTEM = (
    "你是科研选题顾问，任务是识别**正在爆发的新兴方向**。输入是对某主题近一段时间内"
    "新发表工作的扫描结果（含每篇的引用速度、总引用、月龄、主题标签），"
    "以及一份系统统计的「主题聚集度」（同一主题下窗口内出现了多少篇新工作）。\n"
    "两项指标都由系统用真实引用数据算好，**你不得自行生成或修改任何数字**。\n"
    "判定'新兴'的依据可以是其中之一或两者：\n"
    "  (a) 主题聚集度高——短期内同一方向集中冒出多篇新工作，说明有活动量；\n"
    "  (b) 代表工作引用速度相对较高——说明已有牵引力。\n"
    "注意：近一年发表的论文引用数普遍只有个位数，速度绝对值小是正常的，"
    "不要把'速度小'直接等同于'不新兴'，要优先看聚集度与相对排名。\n"
    "只输出一个 JSON 对象：\n"
    '{"topics": [{"topic": "方向名称（简短、可检索）",\n'
    '  "why_emerging": "为何判定它在爆发，必须引用清单里的真实数字（速度或聚集度）作为依据",\n'
    f'  "entry_window": "{"|".join(ENTRY_WINDOWS)}",\n'
    '  "work_index": [1, 4],\n'
    '  "velocity_evidence": "把代表工作的速度/引用/月龄如实转述"}]}\n'
    "硬性要求：\n"
    "1. 每个方向的 work_index 至少 2 个序号，且必须是清单里真实存在的序号，禁止编造；\n"
    "2. 只依据清单里给出的数字推理，不要把清单外的信息写进 why_emerging 或 velocity_evidence；\n"
    "3. entry_window 只能取上面三个值之一：有活动量但尚未被大量引用、值得现在切入的判'可入场'；"
    "已被大量引用且月龄明显偏大的判'已过窗口期'；证据不足以判断的判'需进一步验证'；\n"
    "4. 尽量给出 2-4 个方向；只有在清单里确实找不到任何聚集迹象时才返回 {\"topics\": []}。"
)


@capability(
    id="frontier.detect",
    description=(
        "速度前沿探测：按关键词扫描近 window_months 个月内发表的工作，用引用速度"
        "（总引用/月龄）识别正在爆发的方向，输出方向清单与入场时机（启发式）。"
        "速度与主题聚集度数字均由系统用 OpenAlex 真实数据统计；入场时机是启发式判断，报告中会显式标注。"
    ),
    public=True,
    tags=["research", "frontier", "trend", "topic-selection"],
)
async def detect_frontier(
    topic: str, window_months: int = 12, max_results: int = 20
) -> FrontierReport:
    try:
        window = max(1, min(int(window_months), 36))
    except (TypeError, ValueError):
        window = 12
    try:
        max_results = max(1, min(int(max_results), 50))
    except (TypeError, ValueError):
        max_results = 20
    works = await openalex_emerging(
        topic, window_months=window, max_results=max_results
    )
    if not works:
        return FrontierReport(
            query=topic,
            window_months=window,
            scanned=0,
            dropped=["时间窗内没有匹配到含有效发表日期的工作"],
        )

    listing = "\n".join(
        f"{i}. [{w.published}] {w.title} | 速度 {w.citations_per_month} 引用/月"
        f" | 总引用 {w.citations} | 月龄 {w.age_months} | 主题：{', '.join(w.topics[:2]) or '无'}"
        for i, w in enumerate(works, 1)
    )
    density = topic_density(works)
    density_text = "、".join(f"{name}（{count} 篇）" for name, count in density) or "（无）"
    resp = await llm_tools.chat(
        system=_SYSTEM,
        user=(
            f"研究主题：{topic}\n时间窗：近 {window} 个月\n\n"
            f"主题聚集度（窗口内新工作数，降序）：{density_text}\n\n"
            f"工作清单：\n{listing}"
        ),
        temperature=0.3,
    )
    data = llm_tools.extract_json_object(resp)
    topics, dropped = validate_topics(data.get("topics"), works)
    return FrontierReport(
        query=topic,
        window_months=window,
        scanned=len(works),
        topics=topics,
        works=works,
        density=density,
        dropped=dropped,
    )


@capability(
    id="frontier.citation_profile",
    description=(
        "取单篇论文的施引年份分布（Semantic Scholar 引用图谱），用于复核某个方向的引用是否在加速。"
        "paper_id 支持 S2 paperId 或 arXiv:<id>。注意一次最多取回 1000 条，被截断时 capped=true。"
    ),
    public=True,
    tags=["research", "frontier", "citation", "semanticscholar"],
)
async def citation_profile(paper_id: str) -> dict:
    return await s2_citation_profile(paper_id)


def build_frontier_agent(provider) -> WitAgent:
    ws_enabled = workspace_access_enabled("frontier")
    capabilities = [detect_frontier, citation_profile]
    hooks: list = []
    if ws_enabled:
        capabilities = [*capabilities, *build_workspace_access_capabilities(WORKSPACE / "tokens")]
        hooks.extend([AipDelegationHooks(), BridgeHooks(),
                      WorkspaceAccessBudgetHooks(WORKSPACE / "tokens")])
    public_skills, has_non_public = capability_visibility(capabilities)
    pep_hooks = build_pep_hooks(
        capabilities=capabilities,
        workspace=WORKSPACE,
        public_skills=public_skills,
        has_non_public_skills=has_non_public,
    )
    pep_run_hooks, pep_aip_hooks = split_pep_hooks(pep_hooks)
    hooks.extend(pep_run_hooks)
    if os.getenv("ACPS_PEP_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}:
        print(f"[frontier] PEP 已启用: {pep_hooks[0].config.describe()}", flush=True)
    instructions = (
        "你是研枢平台的前沿探测员。收到选题任务后调用一次 frontier.detect，"
        "需要复核单篇引用走势时可再调用 frontier.citation_profile（最多 2 次）。"
        "最终把工具返回的 JSON 原样输出（可加 ```json 围栏），不要改写、不要补充。"
    )
    if ws_enabled:
        instructions += "\n" + WORKSPACE_ACCESS_NOTE
    return WitAgent(
        id="yanshu.frontier-partner",
        display_name="研枢-Frontier-Partner",
        version="1.0.0",
        description=agent_discovery_description('yanshu.frontier-partner', "速度前沿探测：按引用速度识别近 6-12 个月爆发的新兴方向，输出选题建议。"),
        instructions=instructions,
        model=provider,
        capabilities=apply_to_capabilities(capabilities, agent_id="yanshu.frontier-partner"),
        # remedy-3 下发的 delegation token 不得进入模型上下文（cfg=None 纯删除，见 delegation/client.py）
        hooks=hooks,
        aip_hooks=[AipPartnerGuardHooks(), *pep_aip_hooks],
        amp_hooks=[AmpAccessLoggingHooks()],
        protocols=[AcpsDirect()],
    )


async def main() -> None:
    await cluster_common.serve_partner(
        build_frontier_agent,
        workspace=WORKSPACE,
        port_env="ACPS_FRONTIER_PORT",
        default_port=8450,
        creds_prefix="FRONTIER",
    )


if __name__ == "__main__":
    asyncio.run(main())
