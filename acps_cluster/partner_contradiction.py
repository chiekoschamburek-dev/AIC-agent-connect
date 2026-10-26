"""研枢-Contradiction-Partner：跨论文矛盾侦测与假说生成。

对应申请书里 Synthesis 的自然升级：Synthesis 只做单篇四要素萃取，本 agent 消费**多份**
四要素做冲突挖掘，输出可检验的新假说——流水线因此从"写综述"升级为"产生新研究问题"。

不做的事：不检索、不下载全文、不评价单篇质量（那是 Scout / Synthesis / Critic 的职责）。

运行：python acps_cluster/partner_contradiction.py   （常驻，端口默认 8449）
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
from capabilities.contradiction import (  # noqa: E402
    CONTRADICTION_TYPES,
    ContradictionReport,
    extract_source_ids,
    normalize_id,
    validate_pairs,
)

WORKSPACE = Path(__file__).parent / "ws-contradiction"

_SYSTEM = (
    "你是科研矛盾侦测专家。输入是多篇论文的结构化萃取（每篇含来源 id 与四要素），"
    "你的任务是找出**跨论文**的实质冲突，并把它转化成可检验的研究假说。\n"
    "只输出一个 JSON 对象：\n"
    '{"pairs": [{"claim_a": "论文A的主张", "source_a": "A的来源id",\n'
    '  "evidence_a": "A支撑该主张的原文要点", "claim_b": "论文B的主张", "source_b": "B的来源id",\n'
    '  "evidence_b": "B支撑该主张的原文要点",\n'
    f'  "contradiction_type": "{"|".join(CONTRADICTION_TYPES)}",\n'
    '  "reconciliation": "调和假说：什么条件下两者可以同时成立",\n'
    '  "testable_prediction": "由此可检验的预测：做什么实验/观察什么数据能判定",\n'
    '  "confidence": "high|medium|low"}]}\n'
    "硬性要求：\n"
    "1. source_a 与 source_b 必须不同，且都必须是输入中真实出现过的来源 id，禁止虚构；\n"
    "2. 只报实质冲突（同一问题上的相反结论），不要把'研究方向不同'或'互补'当成矛盾；\n"
    "3. 每条必须给出调和假说与可检验预测，给不出就不要报；\n"
    "4. 宁缺毋滥：没有真实矛盾就返回 {\"pairs\": []}。"
)


@capability(
    id="contradiction.detect",
    description=(
        "跨论文矛盾侦测：输入多篇论文的四要素萃取（含来源 id），输出结构化矛盾对——"
        "双方论点、证据与来源，矛盾类型，以及可检验的调和假说。"
        "代码侧强制：引用同一来源、来源不存在、缺调和假说或缺可检验预测的候选一律丢弃。"
    ),
    public=True,
    tags=["research", "contradiction", "hypothesis"],
)
async def detect_contradictions(
    topic: str, digests: str, source_ids: str = ""
) -> ContradictionReport:
    """source_ids 可用逗号/顿号分隔显式给出；留空时从 digests 里自动抽取。"""
    valid_ids = [
        normalize_id(token)
        for token in (source_ids or "").replace("、", ",").replace(" ", ",").split(",")
        if token.strip()
    ]
    if not valid_ids:
        valid_ids = extract_source_ids(digests)

    resp = await llm_tools.chat(
        system=_SYSTEM,
        user=f"研究主题：{topic}\n\n本次可引用的来源 id：{', '.join(valid_ids) or '（未能识别，禁止编造）'}"
        f"\n\n论文萃取资料：\n{digests[:14000]}",
        temperature=0.2,
    )
    data = llm_tools.extract_json_object(resp)
    pairs, dropped = validate_pairs(data.get("pairs"), valid_ids)
    return ContradictionReport(
        topic=str(topic),
        pairs=pairs,
        dropped=dropped,
        sources_used=sorted({p.source_a for p in pairs} | {p.source_b for p in pairs}),
    )


def build_contradiction_agent(provider) -> WitAgent:
    ws_enabled = workspace_access_enabled("contradiction")
    capabilities = [detect_contradictions]
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
        print(f"[contradiction] PEP 已启用: {pep_hooks[0].config.describe()}", flush=True)
    instructions = (
        "你是研枢平台的矛盾侦测员。收到资料后调用一次 contradiction.detect 完成分析，"
        "然后把该工具返回的 JSON 原样输出（可加 ```json 围栏），"
        "不要改写成 Markdown 报告、不要增删字段、不要补充自己的判断。"
    )
    if ws_enabled:
        instructions += "\n" + WORKSPACE_ACCESS_NOTE
    return WitAgent(
        id="yanshu.contradiction-partner",
        display_name="研枢-Contradiction-Partner",
        version="1.0.0",
        description=agent_discovery_description('yanshu.contradiction-partner', "矛盾侦测与假说生成：跨论文比对，输出矛盾对、调和假说与可检验预测。"),
        instructions=instructions,
        model=provider,
        capabilities=apply_to_capabilities(capabilities, agent_id="yanshu.contradiction-partner"),
        # remedy-3 下发的 delegation token 不得进入模型上下文（cfg=None 纯删除，见 delegation/client.py）
        hooks=hooks,
        aip_hooks=[AipPartnerGuardHooks(), *pep_aip_hooks],
        amp_hooks=[AmpAccessLoggingHooks()],
        protocols=[AcpsDirect()],
    )


async def main() -> None:
    await cluster_common.serve_partner(
        build_contradiction_agent,
        workspace=WORKSPACE,
        port_env="ACPS_CONTRADICTION_PORT",
        default_port=8449,
        creds_prefix="CONTRADICTION",
    )


if __name__ == "__main__":
    asyncio.run(main())
