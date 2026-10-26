"""冒烟：把名册里从未真实运行过的 contradiction / frontier 两个伙伴各跑一次。

模式与 delegation/real_business.py 相同：不起 registry/CA，
`WitRuntime.serve_direct_partner` 起真实直连 mTLS 监听（与生产相同的
身份提取 → 准入 → 任务状态机路径），调用方（critic 身份）经
`runtime.aip_delegate` 下发任务、`aip_task(wait)` 轮询到终态后关单，
产物落 demo_run/smoke-cfp/<agent>/result.json。

前置：ws-contradiction / ws-frontier 已由 deploy/mint_identity.py 铸造身份。

用法（在 my-project 下）：
    python acps_cluster/smoke_contradiction_frontier.py contradiction
    python acps_cluster/smoke_contradiction_frontier.py frontier
"""

import asyncio
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
CLUSTER = PROJECT / "acps_cluster"
for _p in (str(PROJECT), str(CLUSTER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from acps_cluster.core import bootstrap  # noqa: E402  导入即加载 .env
from acps_cluster.agents import partner_contradiction  # noqa: E402
from acps_cluster.agents import partner_frontier  # noqa: E402
from acps_cluster.delegation import demo_pep as dp  # noqa: E402  借用 _acps_config / CRITIC_WS（导入时自带 sys.path 卫生）
from wit import AcpsDirect, AgentState, TextPart, WitAgent, WitRuntime  # noqa: E402
from wit.acps.discovery import cache_discovered_agent, normalize_discovered_agent  # noqa: E402
from wit.hooks.acps.delegation import AipDelegationHooks  # noqa: E402

TERMINAL = {"completed", "failed", "canceled", "rejected"}

FRONTIER_TASK = (
    "研究选题任务：请调用 frontier.detect，对主题 'LLM-based multi-agent collaboration' "
    "做速度前沿探测，时间窗 12 个月，最多 20 篇；完成后把工具返回的 JSON 原样输出。"
)

# 三篇均为真实论文；四要素为演示用简写萃取，矛盾点（涌现之争）真实存在
CONTRADICTION_DIGESTS = """\
[source: arxiv:2201.11903] Chain-of-Thought Prompting Elicits Reasoning in Large Language Models (Wei et al., 2022)
- 创新点：提出思维链（few-shot CoT）提示，用中间推理步骤示范显著提升大模型的多步推理表现。
- 研究缺口：此前 few-shot 提示只示范输入-输出对，模型缺乏"如何推理"的示范。
- 方法论：在 GSM8K 等多个基准上给 8 条思维链示例做 few-shot；系统消融模型规模与示例子集。
- 实验配置：PaLM 540B / GPT-3 175B 等大模型；核心结论：推理增益随模型规模涌现，约 100B 参数以下小模型几乎无增益。

[source: arxiv:2304.15004] Are Emergent Abilities of Large Language Models a Mirage? (Schaeffer et al., 2023)
- 创新点：论证"涌现能力"可能是评测指标造成的测量假象，而非模型能力真实相变。
- 研究缺口：此前涌现结论几乎全部基于 exact-match 类不连续指标，未系统检验指标选择的影响。
- 方法论：对同一批模型与任务换用连续指标（token 级对数似然等）重新分析，并给出指标选择的元分析。
- 实验配置：在多项此前声称涌现的任务上，连续指标下性能随规模平滑、可预测地提升；结论：涌现是"研究者的度量选择"而非模型属性。

[source: arxiv:2205.11916] Large Language Models are Zero-Shot Reasoners (Kojima et al., 2022)
- 创新点：零样本思维链——只在提示里加"Let's think step by step"即可引出逐步推理，无需任何示例。
- 研究缺口：few-shot CoT 依赖人工编写推理示范，成本高且对示范质量敏感。
- 方法论：零样本两段式提示（先自生成推理再提取答案），在多套推理基准上与标准零样本对比。
- 实验配置：GPT-3 系（text-davinci-002 等）验证；结论：无需示例也能引出推理，但对模型规模有要求。
"""

CONTRADICTION_TASK = (
    "研究主题：大语言模型推理能力随规模涌现是否真实\n"
    "请调用 contradiction.detect 完成分析：topic 取上面的研究主题，digests 用下面「论文萃取资料」全文，"
    "source_ids 可留空由系统自动抽取；完成后把工具返回的 JSON 原样输出。\n\n"
    "论文萃取资料：\n" + CONTRADICTION_DIGESTS
)


def _enrollment(ws: Path) -> dict:
    data = json.loads((ws / "acps" / "enrollment.json").read_text(encoding="utf-8"))
    if data.get("status") != "active":
        raise SystemExit(f"{ws} enrollment 状态为 {data.get('status')!r}，非 active")
    return data


def _partner_ws(kind: str) -> Path:
    ws = CLUSTER / f"ws-{kind}"
    if not (ws / "acps" / "enrollment.json").exists():
        raise SystemExit(f"{ws} 不存在；先运行 deploy/mint_identity.py 铸造身份")
    return ws


def _partner_aic(ws: Path) -> str:
    return _enrollment(ws)["identity"]["ref"]["aic"]


def _partner_port(ws: Path) -> int:
    endpoint = _enrollment(ws)["acpsConfig"]["publicEndpoint"]
    return int(endpoint.rsplit(":", 1)[1].split("/")[0])


def _acs_for(kind: str, aic: str, port: int) -> dict:
    """为调用方 discovery 缓存合成 ACS（以 ws-synthesis 终稿为骨架，改写身份与能力）。"""
    template = json.loads(
        (CLUSTER / "ws-synthesis" / "acps" / "acs.final.json").read_text(encoding="utf-8")
    )
    skills = (
        [{"id": "contradiction.detect"}]
        if kind == "contradiction"
        else [{"id": "frontier.detect"}, {"id": "frontier.citation_profile"}]
    )
    template.update(
        aic=aic,
        name=f"研枢-{kind.capitalize()}-Partner",
        skills=skills,
        endPoints=[
            {"transport": "JSONRPC", "url": f"https://localhost:{port}/rpc", "security": [{"mtls": []}]}
        ],
    )
    return template


def _caller_runtime(tmp_root: Path, kind: str, aic: str, port: int):
    """critic 身份的调用方 runtime + 把 discovery 缓存指向本地目标伙伴（同 demo_pep._caller_runtime）。"""
    root = tmp_root / f"ws-caller-{kind}"
    shutil.copytree(dp.CRITIC_WS / "acps", root / "acps")
    cache_dir = root / "acps" / "discovered"
    cache_dir.mkdir(parents=True, exist_ok=True)
    acs = _acs_for(kind, aic, port)
    cache_discovered_agent(
        cache_dir=cache_dir,
        summary=normalize_discovered_agent(acs, ranking=1),
        raw_acs=acs,
    )
    agent = WitAgent(
        id="yanshu.smoke-caller",
        display_name="研枢-Smoke-Caller",
        version="1.0.0",
        description="冒烟调用方（critic 身份）",
        instructions="冒烟专用；只走 runtime.aip_delegate，不跑模型。",
        model=None,
        capabilities=[],
        hooks=[AipDelegationHooks()],
        protocols=[AcpsDirect()],
    )
    return WitRuntime(agent, workspace=root, acps=dp._acps_config(18500)), AgentState()


async def _run_once(kind: str) -> None:
    ws = _partner_ws(kind)
    aic = _partner_aic(ws)
    port = _partner_port(ws)
    build = (
        partner_contradiction.build_contradiction_agent
        if kind == "contradiction"
        else partner_frontier.build_frontier_agent
    )
    task = CONTRADICTION_TASK if kind == "contradiction" else FRONTIER_TASK
    out = CLUSTER / "demo_run" / "smoke-cfp" / kind
    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f"smoke-{kind}-"))

    provider = bootstrap.build_provider()
    started = time.time()
    print(f"[{kind}] AIC={aic} | 监听端口={port} | 工作区={ws.name}")
    partner_rt = WitRuntime(build(provider), workspace=ws, acps=dp._acps_config(port))
    status = await partner_rt.serve_direct_partner(host="127.0.0.1", port=port, amp=False)
    print(f"[{kind}] 直连 mTLS 监听 https://localhost:{port}/rpc → {status.status}")
    try:
        caller, state = _caller_runtime(tmp, kind, aic, port)
        result = await caller.aip_delegate(state, partner_aic=aic, request=[TextPart(task)])
        task_ref = result.get("task_ref")
        print(f"[{kind}] 委派受理 state={result.get('state')} taskRef={task_ref}")

        for _ in range(90):
            if result.get("state") in TERMINAL or result.get("state") == "awaiting-completion" or not task_ref:
                break
            await asyncio.sleep(5)
            result = await caller.aip_task(state, action="wait", task_ref=task_ref)
            print(
                f"  [poll] state={result.get('state')} "
                f"products={len(result.get('products') or [])} elapsed={time.time() - started:.0f}s"
            )

        if result.get("state") == "awaiting-completion" and task_ref:
            closed = await caller.aip_task(state, action="complete", task_ref=task_ref)
            print(f"[{kind}] leader 已关单 → {closed.get('state')}")
            result = closed if closed.get("products") else result

        elapsed = time.time() - started
        print(f"\n[{kind}] 终态={result.get('state')} 用时 {elapsed:.1f}s")
        for product in result.get("products") or []:
            print(f"== Product: {product.get('name')} ({product.get('id')})")
            for item in product.get("data_items") or product.get("dataItems") or []:
                blob = item.get("text") or json.dumps(item.get("data"), ensure_ascii=False)
                print(blob[:6000])
        (out / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n[done] 产物写入 {out}")
    finally:
        await partner_rt.stop_serving()
        await provider.aclose()
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    kind = sys.argv[1] if len(sys.argv) > 1 else ""
    if kind not in {"contradiction", "frontier"}:
        raise SystemExit(
            "用法: python acps_cluster/smoke_contradiction_frontier.py contradiction|frontier"
        )
    asyncio.run(_run_once(kind))


if __name__ == "__main__":
    main()
