"""引用核验端到端验证（C 系列）：真实链路——真 mTLS 取源 + 真 LLM 逐条判定 + 真 review_gate 对账。

场景：
  C1 scout 的 arxiv.fetch 持令牌直连取回真实论文（真实网络 + PEP L2 准入）
  C2 citation.verify 全流水线（真实 LLM 逐条小调用）→ verdict 表
  C3 review_gate 交叉对账：审稿者 APPROVE 而核验表发现 fabricated → 对账 blocker

    cd yanshu-AIC && python acps_cluster/delegation/demo_citation.py
"""

import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))
_sys.path[:] = [p for p in _sys.path if _os.path.abspath(p or _os.getcwd()) != _HERE]
del _os, _sys, _HERE

import asyncio  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

PROJECT = Path(__file__).resolve().parents[2]
CLUSTER = PROJECT / "acps_cluster"
for _p in (str(PROJECT), str(CLUSTER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import agents  # noqa: E402,F401  加载 .env（OPENAI_API_KEY；OPENAI_MODEL 亦走 .env，勿再写死模型）
import os  # noqa: E402

from wit import (  # noqa: E402
    AcpsDirect, AgentState, WitAgent, WitRuntime, capability,
)
from wit.acps.discovery import cache_discovered_agent, normalize_discovered_agent  # noqa: E402
from wit.hooks.acps.delegation import AipDelegationHooks  # noqa: E402

import citation_verify  # noqa: E402
import partner_scout  # noqa: E402
from delegation import issue_token, load_signing_context  # noqa: E402
from delegation.bridge import RunHandle, set_delegation  # noqa: E402
class TwoStepProvider:
    """脚本化 provider（取源腿零 LLM）：第一步带 arxiv_id 调能力，第二步 aip_submit 交回。"""

    def __init__(self, tool_name: str, arguments: dict) -> None:
        self.tool = tool_name
        self.arguments = arguments
        self.calls = 0

    @property
    def capabilities(self):
        from wit.kernel.content import ContentKind
        from wit.providers.base import ProviderCapabilities

        return ProviderCapabilities(provider="scripted", api="scripted",
                                    input_modalities=frozenset({ContentKind.TEXT}),
                                    output_modalities=frozenset({ContentKind.TEXT}),
                                    streaming=False, tools=True)

    def supports_content(self, part, *, role: str):
        return True, None

    def __init__(self, tool_name: str, arguments: dict) -> None:
        self.tool = tool_name
        self.arguments = arguments
        self.calls = 0
        self.last_call_id: str | None = None  # 本 provider 发出的最近一次 ToolCall id

    async def complete(self, request):
        import uuid as _uuid

        from wit.kernel.messages import ModelResponse, ToolCall

        self.calls += 1
        messages = getattr(request, "messages", []) or []

        # 第 1 步：发调用（arxiv_id 从任务文本解析——见下方 user 消息扫描）
        if self.calls == 1 or (self.last_call_id is None and not self._has_pending_result(messages)):
            args = dict(self.arguments)
            import re as _re
            for message in messages:
                if getattr(message, "role", None) != "user":
                    continue
                for part in getattr(message, "content", []) or []:
                    text = getattr(part, "text", "") or ""
                    m = _re.search(r"取论文 (\d{4}\.\d{4,5})", text)
                    if m:
                        args["arxiv_id"] = m.group(1)
                        break
            call = ToolCall(id=f"call-{_uuid.uuid4().hex[:6]}", name=self.tool, arguments=args)
            self.last_call_id = call.id
            return ModelResponse.tools([call])

        # 第 2 步：消费结果并 submit
        for message in reversed(messages):
            if getattr(message, "role", None) != "tool":
                continue
            call_id = getattr(message, "tool_call_id", None) or ""
            if self.last_call_id and call_id == self.last_call_id:
                text = "".join(getattr(p, "text", "") or "" for p in getattr(message, "content", []) or [])
                self.last_call_id = None
                return ModelResponse.tools([ToolCall(
                    id=f"submit-{_uuid.uuid4().hex[:6]}", name="aip_submit",
                    arguments={"products": [{"id": f"p-{_uuid.uuid4().hex[:6]}", "name": "result",
                                             "data_items": [{"type": "text", "text": text}]}]},
                )])
            break

        # 第 3+ 步：纯文本终止
        return ModelResponse.text("done")

    def _has_pending_result(self, messages) -> bool:
        for message in reversed(messages):
            if getattr(message, "role", None) == "tool":
                return getattr(message, "tool_call_id", None) == self.last_call_id
        return False
from delegation.demo_fanout import _aic_of  # noqa: E402
from delegation.demo_domain import (  # noqa: E402
    CRITIC_WS, SUPERVISOR_WS, TRUST, _acps_config,
)
from delegation.pep import PepConfig  # noqa: E402
from delegation import build_pep_hooks  # noqa: E402
from wit.kernel.capabilities import ToolManager  # noqa: E402

SCOUT_WS = CLUSTER / "ws-scout"
PORT_SCOUT = 18805
PORT_CRITIC_OUT = 18806

FETCH_SKILL = "arxiv.fetch"
# scout 侧 provider 从收到的任务文本解析 arxiv_id（不再用全局待取队列——
# 多步 run/多任务下队列弹出顺序与 wit 的 run 生命周期对不齐，曾致 2/4 取源失败）

# 三条真实引用：一条 supported（符合 GAT 摘要）、一条 misrepresented（曲解 Longformer）、
# 一条 fabricated（无中生有——Attention 论文摘要里没有这个说法）
DRAFT = (
    "图注意力网络（GAT）通过在邻居聚合中引入注意力机制，为邻居分配不同权重，"
    "从而提升了图表示学习的效果[1]。"
    "Longformer 的实验证明了全注意力机制本身就是线性复杂度的[2]。"
    "Transformer 论文指出仅用 5 层编码器就能在所有任务上达到 SOTA[3]。"
)
REFERENCES = (
    "Graph Attention Networks，Petar Veličković et al.，arXiv:1710.10903\n"
    "Longformer: The Long-Document Transformer，Iz Beltagy et al.，arXiv:2004.05150\n"
    "Attention Is All You Need，Vaswani et al.，arXiv:1706.03762"
)


@capability(id=FETCH_SKILL, description="按 arXiv ID 取论文记录（演示目标侧复用真实能力）。",
            public=False, acps_skill=True, tags=["demo", "citation"])
async def _demo_fetch(arxiv_id: str) -> dict:
    return await partner_scout.scout_arxiv_fetch(arxiv_id=arxiv_id)


def _scout_agent(aic: str, audit: Path) -> WitAgent:
    cfg = PepConfig(enabled=True, my_aic=aic, leader_aics=frozenset({_aic_of(SUPERVISOR_WS)}),
                    trust_bundle=TRUST, audit_path=audit,
                    public_skills=frozenset(), has_non_public_skills=True)
    return WitAgent(
        id="yanshu.citation-scout", display_name="citation-scout", version="1.0.0",
        description="引用核验取源目标（真实 arXiv 网络）",
        instructions="调用 arxiv_fetch 后 aip_submit 交回论文记录。",
        model=TwoStepProvider(ToolManager.safe_model_tool_name(FETCH_SKILL), {}),
        capabilities=[_demo_fetch],
        hooks=[],
        aip_hooks=build_pep_hooks(cfg=cfg),
        protocols=[AcpsDirect()],
    )


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="citation-"))
    out = CLUSTER / "demo_run" / "citation"
    out.mkdir(parents=True, exist_ok=True)
    audit = out / "scout-audit.jsonl"
    audit.write_text("", encoding="utf-8")

    scout_aic = _aic_of(SCOUT_WS)
    critic_aic = _aic_of(CRITIC_WS)

    # scout 服务端（真实 mTLS + PEP + 真实 arXiv 网络）
    ws_s = tmp / "ws-scout"
    shutil.copytree(SCOUT_WS / "acps", ws_s / "acps")
    scout_rt = WitRuntime(_scout_agent(scout_aic, audit), workspace=ws_s, acps=_acps_config(PORT_SCOUT))
    await scout_rt.serve_direct_partner(host="127.0.0.1", port=PORT_SCOUT, amp=False)
    acs = scout_rt.acs_draft()
    acs["aic"] = scout_aic
    for e in acs.get("endPoints", []) or []:
        e["url"] = f"https://localhost:{PORT_SCOUT}/rpc"

    # critic 出站 runtime（发现缓存指向本地 scout）
    ws_c = tmp / "ws-critic-out"
    shutil.copytree(CRITIC_WS / "acps", ws_c / "acps")
    cache = ws_c / "acps" / "discovered"
    cache.mkdir(parents=True, exist_ok=True)
    cache_discovered_agent(cache_dir=cache, summary=normalize_discovered_agent(acs, ranking=1), raw_acs=acs)
    critic_rt = WitRuntime(
        WitAgent(id="yanshu.citation-critic-out", display_name="critic-out", version="1.0.0",
                 description="引用核验出站", instructions="演示", model=None, capabilities=[],
                 hooks=[AipDelegationHooks()], protocols=[AcpsDirect()]),
        workspace=ws_c, acps=_acps_config(PORT_CRITIC_OUT),
    )

    rows = []
    try:
        # ① 持令牌直连取源（模拟 scrub hook 落的桥上下文——与 inbound 任务同构）
        issued = issue_token(
            load_signing_context(SUPERVISOR_WS / "acps" / "client", trust_bundle=TRUST),
            delegate_aic=critic_aic, target_aic=scout_aic, skill_id=FETCH_SKILL,
        )
        from delegation import bridge as _bridge

        _bridge._RUN.set(RunHandle(runtime=critic_rt, state=AgentState()))
        os.environ["ACPS_CITATION_TARGET_ENDPOINT"] = f"https://localhost:{PORT_SCOUT}/rpc"
        set_delegation({"token": issued.token, "targetAic": scout_aic,
                        "skillId": FETCH_SKILL, "callerAic": _aic_of(CRITIC_WS)})
        source = await citation_verify.fetch_source_via_bridge("1710.10903")
        ok1 = bool(source and source.get("ok"))
        paper = (source or {}).get("paper") or {}
        rows.append(("C1 持令牌直连取回真实论文（arXiv+PEP L2）", "取到论文",
                     "取到：" + str(paper.get("title", ""))[:60] if ok1 else f"失败 {str(source)[:100]}"))
        (out / "fetched-gat.json").write_text(json.dumps(source, ensure_ascii=False, indent=1), encoding="utf-8")

        # ② citation.verify 全流水线（真实 LLM 逐条判定）
        # 每次取源新签一枚（一次性语义：同一枚 jti 二次准入会被判 replay）
        def _token_factory():
            return issue_token(
                load_signing_context(SUPERVISOR_WS / "acps" / "client", trust_bundle=TRUST),
                delegate_aic=critic_aic, target_aic=scout_aic, skill_id=FETCH_SKILL,
            ).token

        table = await citation_verify.verify_citations(DRAFT, REFERENCES, token_factory=_token_factory)
        (out / "verdict-table.json").write_text(json.dumps(table, ensure_ascii=False, indent=1), encoding="utf-8")
        counts = table.get("counts") or {}
        unverified = table.get("unverifiedSources")
        rows.append(("C2 citation.verify 流水线（真实 LLM 逐条判定）", "3 条 verdict",
                     f"counts={counts} unverifiedSources={unverified}"))

        # ③ review_gate 对账：审稿者 APPROVE（漏报）而核验表发现问题 → blocker
        from supervisor import _make_review_gate_tool

        gate = _make_review_gate_tool()
        result = await gate.handler({
            "raw": json.dumps({"verdict": "APPROVE", "issues": [], "score": 88}),
            "citation_table": table,
        })
        has_conflict = bool(result.get("citationConflicts"))
        rows.append(("C3 review_gate 对账（审稿 APPROVE vs 核验表）",
                     "REVISE+对账blocker" if (counts.get("fabricated") or counts.get("misrepresented")) else "视核验结果",
                     f"gate={result['gate']} conflicts={result.get('citationConflicts')}"))
    finally:
        await scout_rt.stop_serving()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n══════════════ 引用核验端到端（C 系列）══════════════")
    for name, expect, got in rows:
        print(f"  {name}\n    期望 {expect} / 实际 {got[:160]}")
    print(f"\n[citation] 产物在 {out}（fetched-gat.json / verdict-table.json）")


if __name__ == "__main__":
    asyncio.run(main())
