"""研枢-Citation-Integrity-Partner：草稿逐条引用核验（PEP_design.md 阶段三原计划的落地）。

定位：审稿生态的"考官"——critic 出审稿意见，本伙伴对草稿的**每一条引用**做
机械核验：解析 [n] 标记 → 按参考文献条目取源（arXiv 优先，持委托令牌经桥直连
scout）→ 每条一次小上下文 LLM 比对 → 四档裁决
（supported / misrepresented / fabricated / unverifiable）+ 证据摘录 + 来源分级。
执行层复用 citation_verify.py（核验 LLM ≠ 审稿 LLM 的分离原则在那里落地）。

能力（三档语义）：
- citation.rules  public=True（橱窗）：核验方法论说明——零成本引流
- citation.verify acps_skill=True（保险柜，"可发现但受门槛"，同 gpu.run 先例）：
  提交 draft+references，返回逐条 verdict 表。成本型能力（每条引用一次 LLM 小调用
  + 可能的 scout 深取），凭证闸门放在 PEP 而非可见性——引用核验是通用科研服务，
  不做会员制限定。

令牌阶梯（诚实降级，逐级变弱、从不编造）：
  ① 桥接令牌（leader remedy-3 / continue 下发，ContextVar）→ 第一条真实取源；
  ② 后续来源：一次性 jti 语义下同一令牌必被拒（replay），主动放弃取源 → 降级
     unverified（只用参考文献条目自洽性核验，来源标记 source=unverified）；
  ③ 无任何令牌 → 全部 unverified。
  完整取源需 leader 按来源数批量签发（RFC §5.3 扇出形态，待文件通道支持多枚）。

身份：ws-citation（端口 8468）。注册（平台）：配 ACPS_CITATION_USERNAME/PASSWORD 后
`python acps_cluster/agents/partner_citation.py`；本地演示：`--local`。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from wit import AcpsDirect, WitAgent, WitRuntime, capability  # noqa: E402

from acps_cluster.runtime import citation_verify  # noqa: E402  执行层复用（机械流水线，不是判断）
from acps_cluster.core import cluster_common  # noqa: E402
from acps_cluster.core.discovery_metadata import (  # noqa: E402
    agent_discovery_description,
    apply_to_capabilities,
)
from wit.hooks.acps.amp import AmpAccessLoggingHooks  # noqa: E402
from acps_cluster.core.aip_guard import AipPartnerGuardHooks  # noqa: E402
from acps_cluster.delegation import build_pep_hooks, capability_visibility, split_pep_hooks  # noqa: E402
from acps_cluster.delegation.bridge import BridgeHooks, current_delegation  # noqa: E402
from wit.hooks.acps.delegation import AipDelegationHooks  # noqa: E402

WORKSPACE = Path(__file__).resolve().parents[1] / "ws-citation"
CITATION_PORT_ENV = "ACPS_CITATION_PORT"
CITATION_PORT_DEFAULT = 8468


@capability(
    id="citation.rules",
    description="引用核验方法论说明：四档裁决定义、审稿/核验 LLM 分离原则、来源分级"
                "（arxiv=真实取源比对 / unverified=条目自洽性降级）、提交格式要求。公开只读。",
    public=True,
    tags=["research", "citation", "verification"],
)
async def citation_rules() -> dict:
    return {
        "method": "逐条机械核验：解析 [n] 引用 → 取源 → 每条一次小上下文比对",
        "verdicts": {
            "supported": "论断与来源原文一致",
            "misrepresented": "来源存在但论断曲解/夸大了原文",
            "fabricated": "来源不存在，或论断在来源中无任何依据",
            "unverifiable": "未能取到来源原文（降级模式，不是无害结论）",
        },
        "sourceTiers": {"arxiv": "真实取源比对", "unverified": "条目自洽性降级（未取到原文）"},
        "separation": "核验 LLM 与审稿 LLM 分离（自我复核悖论）；每条引用独立小上下文调用",
        "limits": {"maxCitations": citation_verify.MAX_CITATIONS,
                   "excerptChars": citation_verify._EXCERPT_CHARS},
        "submitFormat": "draft：含 [n] 标记的草稿全文；references：按编号排列的参考文献条目"
                        "（条目中含 arXiv ID 形如 2401.12345 才能真实取源）",
        "tokenLadder": "持委托令牌可真实取源；无令牌自动降级 unverified 并如实标记，不编造比对",
    }


def _single_shot_token_factory():
    """桥接令牌一次性工厂：首枚真实取源，其后返回 None（主动降级，避免必然的 replay 拒绝）。"""
    state = {"used": False}

    def factory():
        if state["used"]:
            return None
        state["used"] = True
        return (current_delegation() or {}).get("token")

    return factory


@capability(
    id="citation.verify",
    description=(
        "草稿逐条引用核验：提交 draft（含 [n] 引用标记）与 references（编号参考文献条目），"
        "返回逐条 verdict 表（supported/misrepresented/fabricated/unverivable 之四档）+"
        "证据摘录 + 来源分级。每条引用独立小上下文比对；核验 LLM 与审稿 LLM 分离。"
        "持委托令牌时对 arXiv 源真实取核，无令牌降级 unverified 并如实标记。"
        "成本型能力（每条一次 LLM 调用 + 可能的 scout 深取）：仅限被授权的伙伴调用。"
    ),
    acps_skill=True,
    tags=["research", "citation", "verification"],
)
async def citation_verify_cap(draft: str, references: str) -> dict:
    return await citation_verify.verify_citations(
        draft, references, token_factory=_single_shot_token_factory()
    )


CITATION_CAPABILITIES = [citation_rules, citation_verify_cap]


def build_citation_agent(provider) -> WitAgent:
    public_skills, has_non_public = capability_visibility(CITATION_CAPABILITIES)
    pep_hooks = build_pep_hooks(
        capabilities=CITATION_CAPABILITIES,
        workspace=WORKSPACE,
        public_skills=public_skills,
        has_non_public_skills=has_non_public,
    )
    pep_run_hooks, pep_aip_hooks = split_pep_hooks(pep_hooks)
    if os.getenv("ACPS_PEP_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}:
        print(f"[citation] PEP 已启用: {pep_hooks[0].config.describe()}", flush=True)
    return WitAgent(
        id="yanshu.citation-integrity",
        display_name="研枢-Citation-Integrity-Partner",
        version="1.0.0",
        description=agent_discovery_description(
            "yanshu.citation-integrity",
            "引用完整性核验：把草稿的每一条引用变成四档裁决与证据摘录，"
            "供审稿与引用对账交叉复核。",
        ),
        instructions=(
            "你是研枢平台的引用核验员。收到核验任务后调用一次 citation.verify"
            "（draft=任务里的草稿全文，references=任务里的参考文献条目），"
            "把返回的 JSON 原样输出（可加 ```json 围栏），不要改写裁决、不要增删字段、"
            "不要把 unverified 说成已核验。方法论问题用 citation.rules 回答。"
        ),
        model=provider,
        capabilities=apply_to_capabilities(CITATION_CAPABILITIES, agent_id="yanshu.citation-integrity"),
        # 桥：capability 内持委托令牌出站直连 scout（remedy-3 通道的消费侧）
        hooks=[AipDelegationHooks(), BridgeHooks(), *pep_run_hooks],
        aip_hooks=[AipPartnerGuardHooks(), *pep_aip_hooks],
        amp_hooks=[AmpAccessLoggingHooks()],
        protocols=[AcpsDirect()],
    )


async def _serve_local() -> None:
    """本地直连模式（不经 registry/CA）。必须有模型才能真正接活。"""
    from acps_cluster.core import bootstrap  # 触发 .env 加载

    port = int(os.getenv(CITATION_PORT_ENV, str(CITATION_PORT_DEFAULT)))
    provider = None if os.getenv("ACPS_CITATION_LOCAL_NO_MODEL") else bootstrap.build_provider()
    agent = build_citation_agent(provider)
    runtime = WitRuntime(agent, workspace=WORKSPACE, acps=cluster_common.build_acps_config(port))
    cluster_common.ensure_local_acs(runtime, WORKSPACE)
    try:
        status = await runtime.serve_direct_partner(host="127.0.0.1", port=port, amp=False)
        aic = cluster_common.ensure_local_acs(runtime, WORKSPACE) or "?"
        print(f"[citation] 本地直连模式 AIC={aic} endpoint=https://localhost:{port}/rpc → {status.status}"
              f"（模型={'无' if provider is None else '已加载'}）", flush=True)
        await asyncio.sleep(int(os.getenv("ACPS_PARTNER_TTL", "864000")))
    finally:
        if provider is not None:
            await provider.aclose()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true", help="本地直连演示模式（不起 registry/CA）")
    args = parser.parse_args()
    if args.local:
        await _serve_local()
        return
    await cluster_common.serve_partner(
        build_citation_agent,
        workspace=WORKSPACE,
        port_env=CITATION_PORT_ENV,
        default_port=CITATION_PORT_DEFAULT,
        creds_prefix="CITATION",
    )


if __name__ == "__main__":
    asyncio.run(main())
