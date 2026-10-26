"""研枢-Writer-Partner：论文起草（大纲/全文双模式 + 审稿意见返工）。

对应申请书"Writer Agent"。返工接口：把 Critic 的修改意见放进 feedback 参数重新调用。
运行：python acps_cluster/partner_writer.py   （常驻，端口默认 8447）
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from wit import AcpsDirect, WitAgent, capability  # noqa: E402

from acps_cluster.core import cluster_common  # noqa: E402
from acps_cluster.core.discovery_metadata import (  # noqa: E402
    agent_discovery_description,
    apply_to_capabilities,
)
from acps_cluster.core import llm_tools  # noqa: E402
from acps_cluster.core.math_delimiters import normalize_math_delimiters  # noqa: E402
from wit.hooks.acps.amp import AmpAccessLoggingHooks  # noqa: E402
from acps_cluster.core.aip_guard import AipPartnerGuardHooks
from acps_cluster.delegation import build_pep_hooks, capability_visibility, split_pep_hooks  # noqa: E402
from acps_cluster.delegation.bridge import BridgeHooks  # noqa: E402
from acps_cluster.agents.partner_workspace_client import (  # noqa: E402
    WORKSPACE_ACCESS_NOTE,
    WorkspaceAccessBudgetHooks,
    build_workspace_access_capabilities,
)
from acps_cluster.core.workspace_access import enabled as workspace_access_enabled  # noqa: E402
from wit.hooks.acps.delegation import AipDelegationHooks  # noqa: E402

WORKSPACE = Path(__file__).resolve().parents[1] / "ws-writer"

_OUTLINE_SYSTEM = (
    "你是学术写作助手。根据研究主题与给定资料生成论文大纲（Markdown），"
    "包含题目、摘要要点、各章节标题与每节 2-3 句内容规划；每个论点标注可引用的资料编号。"
    "只依据给定资料组织内容，资料不足之处在对应位置标注[需补充证据]。"
)
_FULL_SYSTEM = (
    "你是学术写作助手。根据研究主题、资料与（可选）大纲，撰写结构完整的中文论文初稿"
    "（Markdown，含摘要/引言/相关工作/方法/实验设计/结论/参考文献）。"
    "要求：所有事实性论断均来自给定资料并以[n]标注引用；不得编造文献与数据；"
    "语言严谨凝练，避免空泛套话。若提供了修改意见（feedback），逐条回应并修订。"
)


@capability(
    id="paper.write",
    description="论文起草：mode=outline 生成大纲，mode=full 输出全文初稿；materials 为编号资料清单，feedback 为上一轮审稿修改意见（返工时传入）。",
    public=True,
    tags=["research", "writing"],
)
async def write_paper(
    topic: str,
    materials: str = "",
    mode: str = "outline",
    feedback: str = "",
    previous_draft: str = "",
) -> str:
    if mode not in ("outline", "full"):
        raise ValueError("mode 只能是 outline 或 full")
    user = f"研究主题：{topic}\n\n资料清单：\n{materials[:12000] or '（无，按[需补充证据]处理）'}"
    if mode == "full" and feedback:
        user += f"\n\n上一轮草稿：\n{previous_draft[:12000]}\n\n审稿修改意见（逐条回应）：\n{feedback[:4000]}"
    draft = await llm_tools.chat(
        system=_FULL_SYSTEM if mode == "full" else _OUTLINE_SYSTEM,
        user=user,
        temperature=0.5,
    )
    # 交付前规范化数学定界符：模型习惯用 LaTeX 的 \[...\] / \(...\)，
    # 而平台渲染器只认 $...$ / $$...$$，否则公式会显示成
    # "\[ \n p_\theta(...) \n = \n \mathcal{N} \n ..." 这种被拆开的样子
    # （2026-10-11 用户反馈）。代码块内不动、幂等。
    return normalize_math_delimiters(draft)


def build_writer_agent(provider) -> WitAgent:
    ws_enabled = workspace_access_enabled("writer")
    capabilities = [write_paper]
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
        print(f"[writer] PEP 已启用: {pep_hooks[0].config.describe()}", flush=True)
    instructions = (
        "你是研枢平台的论文写作员。按任务要求调用 paper.write（先 outline 后 full "
        "或直接 full），把生成的 Markdown 原样返回。"
    )
    if ws_enabled:
        instructions += "\n" + WORKSPACE_ACCESS_NOTE
    return WitAgent(
        id="yanshu.writer-partner",
        display_name="研枢-Writer-Partner",
        version="1.1.0",  # 与平台已批记录对齐（2026-10-14 换身份）
        description=agent_discovery_description('yanshu.writer-partner', "论文写作员：支持大纲与全文双模式起草，可依据审稿意见定向返工。"),
        instructions=instructions,
        model=provider,
        capabilities=apply_to_capabilities(capabilities, agent_id="yanshu.writer-partner"),
        # remedy-3 下发的 delegation token 不得进入模型上下文（cfg=None 纯删除，见 delegation/client.py）
        hooks=hooks,
        aip_hooks=[AipPartnerGuardHooks(), *pep_aip_hooks],
        amp_hooks=[AmpAccessLoggingHooks()],
        protocols=[AcpsDirect()],
    )


async def main() -> None:
    await cluster_common.serve_partner(
        build_writer_agent,
        workspace=WORKSPACE,
        port_env="ACPS_WRITER_PORT",
        default_port=8447,
        creds_prefix="WRITER",
    )


if __name__ == "__main__":
    asyncio.run(main())
