"""研枢-Critic-Partner：模拟审稿，输出结构化裁决（供 Supervisor 代码侧复核）。

对应申请书"Critic Agent"。铁律：verdict 必须与 issues 联动（有 blocker 必 REVISE），
且引用类问题必须给出可本地复核的 evidence_check 原文片段——Supervisor 侧不信任口头结论，
会用代码校验这两条后才采纳裁决。
运行：python acps_cluster/partner_critic.py   （常驻，端口默认 8448）
"""

import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wit import AcpsDirect, WitAgent, capability  # noqa: E402

import cluster_common  # noqa: E402
from discovery_metadata import (  # noqa: E402
    agent_discovery_description,
    apply_to_capabilities,
)
import llm_tools  # noqa: E402
from aip_guard import AipPartnerGuardHooks
from delegation import build_pep_hooks, capability_visibility, split_pep_hooks  # noqa: E402

WORKSPACE = Path(__file__).parent / "ws-critic"

_SEVERITIES = ("blocker", "major", "minor")


@dataclass
class ReviewIssue:
    severity: str  # blocker | major | minor
    location: str
    problem: str
    suggestion: str
    evidence_check: str = ""  # 引用的原文片段，供本地复核；非引用类问题留空


@dataclass
class ReviewResult:
    gate: str  # 代码强制后的最终判定（APPROVE|REVISE）—— leader 只认这个
    verdict: str  # 模型自报裁决，留痕用于审计（可能与 gate 不一致）
    score: int  # 0-100
    summary: str
    issues: list[ReviewIssue] = field(default_factory=list)


def _extract_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError(f"模型未返回 JSON: {text[:200]}")
    return json.loads(match.group(0))


@capability(
    id="paper.review",
    description="模拟审稿人审查论文：输出 APPROVE/REVISE 裁决、0-100 分与逐条问题清单（含位置、问题、建议与可复核的原文证据）。",
    public=True,
    tags=["research", "review"],
)
async def review_paper(paper: str, materials: str = "") -> ReviewResult:
    resp = await llm_tools.chat(
        system=(
            "你是严格的学术审稿人。审查给定论文，只输出一个 JSON 对象：\n"
            '{"verdict": "APPROVE|REVISE", "score": 0-100整数, "summary": "总评(<=100字)", '
            '"issues": [{"severity": "blocker|major|minor", "location": "章节/段落", '
            '"problem": "...", "suggestion": "...", "evidence_check": "涉及引用失实/无中生有时，'
            '摘录论文原文片段供程序复核，否则留空"}]}\n'
            "规则：存在任何 blocker 或 major 时 verdict 必须 REVISE；事实性指控（引用不存在、"
            "数据捏造）必须填 evidence_check；无实质问题才允许 APPROVE。"
        ),
        user=f"论文：\n{paper[:14000]}\n\n参考资料：\n{materials[:4000] or '（无）'}",
        temperature=0.2,
    )
    data = _extract_json(resp)
    issues = [
        ReviewIssue(
            severity=str(i.get("severity") or "minor").lower(),
            location=str(i.get("location") or ""),
            problem=str(i.get("problem") or ""),
            suggestion=str(i.get("suggestion") or ""),
            evidence_check=str(i.get("evidence_check") or ""),
        )
        for i in (data.get("issues") or [])
    ]
    for issue in issues:
        if issue.severity not in _SEVERITIES:
            issue.severity = "minor"
    verdict = str(data.get("verdict") or "REVISE").upper()
    if verdict not in ("APPROVE", "REVISE"):
        verdict = "REVISE"
    # 裁决与问题清单强制一致：存在 blocker/major 一律 REVISE（gate 由代码算，模型说了不算）
    gate = "REVISE" if any(i.severity in ("blocker", "major") for i in issues) else "APPROVE"
    return ReviewResult(
        gate=gate,
        verdict=verdict,
        score=max(0, min(100, int(data.get("score") or 0))),
        summary=str(data.get("summary") or ""),
        issues=issues,
    )


@capability(
    id="citation.verify",
    description=(
        "引用核验（机械流水线，独立于审稿判断）：解析草稿的 [n] 引用，"
        "经桥持令牌取源（scout.arxiv.fetch），逐条小调用比对论断与原文，"
        "输出 verdict 表（supported/misrepresented/fabricated/unverifiable）。"
        "供 review_gate 与审稿者的 evidence_check 交叉对账。"
    ),
    public=False,  # 高成本（取源+逐条调用），走委托 token
    acps_skill={"visibility": "domain", "domains": ["yanshu-scout"]},
    tags=["review", "citation", "verify"],
)
async def citation_verify_capability(draft: str, references: str) -> dict:
    from citation_verify import verify_citations

    return await verify_citations(draft, references)


def build_critic_agent(provider) -> WitAgent:
    public_skills, has_non_public = capability_visibility([review_paper, citation_verify_capability])
    pep_hooks = build_pep_hooks(
        capabilities=[review_paper, citation_verify_capability],
        workspace=WORKSPACE,
        public_skills=public_skills,
        has_non_public_skills=has_non_public,
    )
    pep_run_hooks, pep_aip_hooks = split_pep_hooks(pep_hooks)
    if os.getenv("ACPS_PEP_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}:
        print(f"[critic] PEP 已启用: {pep_hooks[0].config.describe()}", flush=True)
    return WitAgent(
        id="yanshu.critic-partner",
        display_name="研枢-Critic-Partner",
        version="1.10.0",
        description=agent_discovery_description('yanshu.critic-partner', "模拟审稿人：科学性/逻辑性/合规性审查 + 引用核验（独立于审稿判断的机械流水线）。"),
        instructions=(
            "你是研枢平台的审稿员。收到论文后先调用 paper.review 完成审查，"
            "最终回答必须原样输出该工具返回的 JSON（可加 ```json 围栏），"
            "不得改写成 Markdown 报告、不得增删字段。"
            "leader 要求引用核验时调用 citation.verify（传 draft 与 references），"
            "把 verdict 表原样转达——核验与审稿是两个独立判断，不要合并、不要改写核验结论。"
        ),
        model=provider,
        capabilities=apply_to_capabilities(
            [review_paper, citation_verify_capability], agent_id="yanshu.critic-partner"),
        # remedy-3 下发的 delegation token 不得进入模型上下文（cfg=None 纯删除，见 delegation/client.py）
        hooks=[*pep_run_hooks],
        aip_hooks=[AipPartnerGuardHooks(), *pep_aip_hooks],
        protocols=[AcpsDirect()],
    )


async def main() -> None:
    await cluster_common.serve_partner(
        build_critic_agent,
        workspace=WORKSPACE,
        port_env="ACPS_CRITIC_PORT",
        default_port=8448,
        creds_prefix="CRITIC",
    )


if __name__ == "__main__":
    asyncio.run(main())
