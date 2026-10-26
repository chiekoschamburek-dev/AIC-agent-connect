"""Phase A 干跑：论文复现全链路（不碰 GPU 训练段）。

链路：leader → gpu.env(公开) → scout.scholar.deep_fetch(L1 白名单) →
leader 短名单 → synthesis.paper.extract × N → leader 选型 →
writer.paper.write(复现方案) → critic.paper.review → (REVISE 则返工一轮)。

说明（诚实口径）：
- "leader 的决策"（短名单/最终选型）由本脚本用 llm_tools.chat 直连模型扮演——
  即 supervisor 剧本在 Phase C 将要承担的决策职能；此处先行验证链路与选型逻辑。
- 各伙伴均为真实 wit 实例：直连 mTLS 监听、真实身份、真实 admission 路径；
  scout/gpu 开启 PEP，审计落 demo_run/repro-phaseA/audit-*.jsonl。
- Phase A 不执行训练：experiment/gpu.run 留给 Phase B。

用法（在 my-project 下）：python acps_cluster/tools/repro_phase_a.py
"""

import asyncio
import json
import os
import re
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
from acps_cluster.core import llm_tools  # noqa: E402
from acps_cluster.agents import partner_critic  # noqa: E402
from acps_cluster.agents import partner_gpu  # noqa: E402
from acps_cluster.agents import partner_scout  # noqa: E402
from acps_cluster.agents import partner_synthesis  # noqa: E402
from acps_cluster.agents import partner_writer  # noqa: E402
from acps_cluster.delegation import demo_pep as dp  # noqa: E402  借用 _acps_config / SUPERVISOR_WS（自带 sys.path 卫生）
from wit import AcpsDirect, AgentState, TextPart, WitAgent, WitRuntime  # noqa: E402
from wit.acps.discovery import cache_discovered_agent, normalize_discovered_agent  # noqa: E402
from wit.hooks.acps.delegation import AipDelegationHooks  # noqa: E402

TERMINAL = {"completed", "failed", "canceled", "rejected"}
OUT = CLUSTER / "demo_run" / "repro-phaseA"

# 各伙伴本地端口（避开真实工作区端口段 844x/846x）
PORTS = {"gpu": 18461, "scout": 18462, "synthesis": 18463, "writer": 18464, "critic": 18465}
PORT_CALLER = 18499

SCOUT_QUERY = "convolutional neural network training from scratch small dataset single GPU"

SHORTLIST_SYSTEM = (
    "你是科研团队的调度负责人。给你一份候选论文清单与本地算力约束，"
    "请从清单里挑出最多 3 篇最适合在本地 GPU 上**从零训练复现**的论文。\n"
    "优先级：(1) 模型小、数据集公开且小（MNIST/CIFAR/Cora 级）；(2) 方法描述清晰可独立实现；"
    "(3) 训练量能在 30 分钟内出可判读结果。\n"
    "只输出一个 JSON 对象：{\"shortlist\": [清单序号(1-based)], \"note\": \"一句话理由\"}"
)

SELECT_SYSTEM = (
    "你是科研团队的调度负责人。给你若干份论文四要素萃取与本地算力约束，"
    "请选出**唯一**最适合在本地 GPU 沙盒内从零训练复现的一篇。\n"
    "判定依据：实验配置与约束的匹配度（显存/时长/数据可得性）、方法可独立实现程度、复现结果可判读性。\n"
    "只输出一个 JSON 对象："
    '{"choice": 1-based序号, "title": "选中论文标题", "reasons": "逐条匹配理由", "risks": "复现风险与降规模预案"}'
)

SKILLS = {
    "gpu": ["gpu.env", "gpu.status", "gpu.data.list", "gpu.data.fetch", "gpu.run"],
    "scout": ["arxiv.search", "openalex.search", "semanticscholar.search", "scholar.deep_fetch", "dataset.fetch"],
    "synthesis": ["paper.extract"],
    "writer": ["paper.write"],
    "critic": ["paper.review"],
}


def _now() -> float:
    return time.time()


def _extract_tool_result(text: str) -> str:
    """伙伴产物多为工具调用的 JSON 包装（含 toolCallId/result 字段），剥出 result 便于下游阅读。"""
    try:
        data = llm_tools.extract_json_object(text or "")
    except Exception:  # noqa: BLE001  不是 JSON 包装就原样用
        return text or ""
    result = data.get("result")
    if isinstance(result, str) and result.strip():
        return result
    if isinstance(result, (dict, list)):
        return json.dumps(result, ensure_ascii=False)
    return text or ""


def _parse_records(text: str) -> list[dict]:
    """解析 scout 输出里的候选记录（兼容两种形态：Markdown 编号清单 / ScholarRecord repr）。

    实测 scout 并不原样回显工具 JSON，而是自主多源检索后输出 Markdown 资料清单——
    所以 Markdown 形态才是主路径，repr 正则只作兜底。
    """
    records: list[dict] = []
    # 形态一：Markdown 编号清单（**N. 标题** + 后续 "- 字段：值" 行，直到下一个编号）
    for m in re.finditer(
        r"\*\*(\d+)\.\s*(.+?)\*\*\s*\n((?:(?!\*\*\d+\.).)*?)(?=\*\*\d+\.|\Z)", text or "", re.S
    ):
        idx, title, body = int(m.group(1)), m.group(2).strip(), m.group(3).strip()
        if not title:
            continue
        year_m = re.search(r"年份[:：]\s*(\d{4})", body)
        records.append(
            {
                "index": idx,
                "title": title,
                "abstract": body[:2200],  # 含作者/年份/链接/摘要等行，整体交给下游
                "year": year_m.group(1) if year_m else None,
            }
        )
    if records:
        return records
    # 形态二：ScholarRecord repr（按字段名正则切）
    for chunk in (text or "").split("ScholarRecord(")[1:]:
        seg = chunk[:6000]

        def _field(name: str, end_pat: str) -> str:
            fm = re.search(rf"{name}='(.*?)', {end_pat}", seg, re.S)
            return fm.group(1) if fm else ""

        title = _field("title", r"authors=")
        if title:
            year_m = re.search(r"year=(\d{4}|None)", seg)
            records.append(
                {
                    "index": len(records) + 1,
                    "title": title,
                    "abstract": _field("abstract", r"url="),
                    "year": year_m.group(1) if year_m else None,
                }
            )
    return records


class Trace:
    """全链路执行轨迹：每步记录输入摘要/输出/状态/耗时，最终落 result.json。"""

    def __init__(self) -> None:
        self.steps: list[dict] = []
        self.t0 = _now()
        self.saved = False

    def add(self, name: str, detail: str, output: str, state: str = "-", elapsed: float = 0.0) -> None:
        self.steps.append(
            {
                "step": name,
                "detail": detail,
                "state": state,
                "elapsed_s": round(elapsed, 1),
                "total_s": round(_now() - self.t0, 1),
                "output_chars": len(output or ""),
                "output_head": (output or "")[:1200],
            }
        )
        print(f"[{name}] state={state} 用时={elapsed:.1f}s 输出={len(output or '')}字", flush=True)

    def save(self) -> None:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "result.json").write_text(
            json.dumps(
                {"total_s": round(_now() - self.t0, 1), "steps": self.steps}, ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
        self.saved = True


def _enrollment_aic(ws_name: str) -> str:
    data = json.loads((CLUSTER / ws_name / "acps" / "enrollment.json").read_text(encoding="utf-8"))
    return data["identity"]["ref"]["aic"]


def _acs_entry(aic: str, name: str, skills: list[str], port: int) -> dict:
    template = json.loads(
        (CLUSTER / "ws-synthesis" / "acps" / "acs.final.json").read_text(encoding="utf-8")
    )
    template.update(
        aic=aic,
        name=name,
        skills=[{"id": s} for s in skills],
        endPoints=[
            {"transport": "JSONRPC", "url": f"https://localhost:{port}/rpc", "security": [{"mtls": []}]}
        ],
    )
    return template


def _serve_partners(tmp: Path):
    """起 5 个真实伙伴（直连 mTLS）；scout/gpu 开 PEP，审计各落一份（env 在构建时读取，按序设置）。"""
    builders = {
        "gpu": partner_gpu.build_gpu_agent,
        "scout": partner_scout.build_scout_agent,
        "synthesis": partner_synthesis.build_synthesis_agent,
        "writer": partner_writer.build_writer_agent,
        "critic": partner_critic.build_critic_agent,
    }
    runtimes: dict[str, WitRuntime] = {}
    providers: dict[str, object] = {}
    for kind in ("gpu", "scout"):  # PEP 开启的两个伙伴，审计文件各一份
        audit = OUT / f"audit-{kind}.jsonl"
        audit.parent.mkdir(parents=True, exist_ok=True)
        audit.write_text("", encoding="utf-8")
        os.environ["ACPS_PEP_ENABLED"] = "1"
        os.environ["ACPS_PEP_LEADER_AICS"] = dp.LEADER_AIC
        os.environ["ACPS_PEP_AUDIT_PATH"] = str(audit)
        root = tmp / f"ws-{kind}"
        shutil.copytree(CLUSTER / f"ws-{kind}" / "acps", root / "acps")
        providers[kind] = bootstrap.build_provider()
        runtimes[kind] = WitRuntime(
            builders[kind](providers[kind]), workspace=root, acps=dp._acps_config(PORTS[kind])
        )
    os.environ["ACPS_PEP_ENABLED"] = "0"  # 其余伙伴关闭 PEP（本链路只有 leader 直调公开/L1 能力）
    for kind in ("synthesis", "writer", "critic"):
        root = tmp / f"ws-{kind}"
        shutil.copytree(CLUSTER / f"ws-{kind}" / "acps", root / "acps")
        providers[kind] = bootstrap.build_provider()
        runtimes[kind] = WitRuntime(
            builders[kind](providers[kind]), workspace=root, acps=dp._acps_config(PORTS[kind])
        )
    return runtimes, providers


async def _start_all(runtimes: dict[str, WitRuntime], trace: Trace) -> None:
    for kind, rt in runtimes.items():
        status = await rt.serve_direct_partner(host="127.0.0.1", port=PORTS[kind], amp=False)
        trace.add(f"serve-{kind}", f"端口 {PORTS[kind]}", f"status={status.status}")


def _make_caller(tmp: Path, aics: dict[str, str]):
    root = tmp / "ws-caller"
    shutil.copytree(dp.SUPERVISOR_WS / "acps", root / "acps")
    cache_dir = root / "acps" / "discovered"
    cache_dir.mkdir(parents=True, exist_ok=True)
    for kind, port in PORTS.items():
        acs = _acs_entry(aics[kind], f"研枢-{kind.capitalize()}-Partner", SKILLS[kind], port)
        cache_discovered_agent(
            cache_dir=cache_dir, summary=normalize_discovered_agent(acs, ranking=1), raw_acs=acs
        )
    agent = WitAgent(
        id="yanshu.phasea-leader",
        display_name="研枢-PhaseA-Leader",
        version="1.0.0",
        description="Phase A 干跑编排者（supervisor 身份）",
        instructions="Phase A 干跑专用；决策用代码侧 llm_tools，伙伴交互走 runtime.aip_delegate。",
        model=None,
        capabilities=[],
        hooks=[AipDelegationHooks()],
        protocols=[AcpsDirect()],
    )
    return WitRuntime(agent, workspace=root, acps=dp._acps_config(PORT_CALLER)), AgentState()


async def _delegate(
    caller, state, partner_aic: str, request: str, *, name: str, trace: Trace, max_polls: int = 60
) -> str:
    """委派一次任务并等待终态，返回伙伴产物文本（已剥工具包装）。"""
    t0 = _now()
    result = await caller.aip_delegate(state, partner_aic=partner_aic, request=[TextPart(request)])
    task_ref = result.get("task_ref")
    for _ in range(max_polls):
        if result.get("state") in TERMINAL or result.get("state") == "awaiting-completion" or not task_ref:
            break
        await asyncio.sleep(5)
        result = await caller.aip_task(state, action="wait", task_ref=task_ref)
    if result.get("state") == "awaiting-completion" and task_ref:
        result = await caller.aip_task(state, action="complete", task_ref=task_ref)
    elapsed = _now() - t0
    text = ""
    for product in result.get("products") or []:
        for item in product.get("data_items") or product.get("dataItems") or []:
            text += (item.get("text") or "") + "\n"
    state_str = str(result.get("state"))
    trace.add(name, f"taskRef={task_ref}", text, state=state_str, elapsed=elapsed)
    if state_str not in {"completed", "awaiting-completion"}:
        raise RuntimeError(f"[{name}] 伙伴任务未完成：state={state_str} ref={task_ref}")
    return _extract_tool_result(text)


def _verdict(review_text: str) -> str:
    m = re.search(r"verdict['\"]?\s*[:=]\s*['\"]?(APPROVE|REVISE)", review_text or "", re.I)
    return m.group(1).upper() if m else "UNKNOWN"


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    trace = Trace()
    tmp = Path(tempfile.mkdtemp(prefix="repro-a-"))
    print(f"[setup] 输出目录 {OUT}", flush=True)
    runtimes: dict[str, WitRuntime] = {}
    providers: dict[str, object] = {}
    try:
        runtimes, providers = _serve_partners(tmp)
        await _start_all(runtimes, trace)
        aics = {k: _enrollment_aic(f"ws-{k}") for k in PORTS}

        caller, state = _make_caller(tmp, aics)
        print(f"[setup] leader={dp.LEADER_AIC} 发现缓存已预热 {len(PORTS)} 个伙伴", flush=True)

        # S1 算力约束：gpu.env（公开能力，L0）
        gpu_env = await _delegate(
            caller, state, aics["gpu"],
            "请调用 gpu.env 查询本节点算力环境，并把工具返回的 JSON 原样输出，不要增删字段、"
            "不要调用 gpu.run。",
            name="S1-gpu.env", trace=trace,
        )

        # S2 文献检索：scholar.deep_fetch（非公开能力，leader 走 L1 白名单）
        records = await _delegate(
            caller, state, aics["scout"],
            f"请调用 scholar.deep_fetch 检索候选文献：query=\"{SCOUT_QUERY}\"，max_results=8，"
            "并把工具返回的 JSON 原样输出。",
            name="S2-deep_fetch", trace=trace,
        )

        # S3 leader 短名单（Phase C 将由 supervisor 剧本承担）
        parsed = _parse_records(records)
        by_idx = {r["index"]: r for r in parsed}
        listing = "\n".join(
            f"{r['index']}. [{r.get('year')}] {r['title']}\n   摘要：{r['abstract'][:220]}"
            for r in parsed
        )
        if not listing:
            listing = records[:8000]  # 解析失败降级：整段交给模型
        short = llm_tools.extract_json_object(
            await llm_tools.chat(
                system=SHORTLIST_SYSTEM,
                user=f"本地算力约束：\n{gpu_env[:3000]}\n\n候选论文清单：\n{listing[:12000]}",
                temperature=0.2,
            )
        )
        picks = [int(i) for i in short.get("shortlist", []) if int(i) in by_idx][:3]
        trace.add("S3-shortlist", f"解析出 {len(parsed)} 条记录 → 短名单 {picks}",
                  json.dumps(short, ensure_ascii=False))

        # S4 逐篇四要素（synthesis × N）
        digests: list[str] = []
        for idx, pick in enumerate(picks, 1):
            r = by_idx[pick]
            paper_text = f"标题：{r['title']}\n年份：{r.get('year')}\n{r['abstract'][:5000]}"
            digest = await _delegate(
                caller, state, aics["synthesis"],
                "请调用 paper.extract 对下面这篇论文做四要素萃取，并把工具返回的 JSON 原样输出。"
                f"\n\n{paper_text}",
                name=f"S4-extract-{idx}", trace=trace,
            )
            digests.append(f"【候选 {idx}】{r['title']}\n{digest}")
        if not digests:
            # 降级：解析失败时把检索原文整体交给模型选型（Phase A 容错路径）
            digests = [f"【检索原文（未结构化）】\n{records[:8000]}"]

        if not digests:
            # 降级：解析失败时把检索原文整体交给模型选型（Phase A 容错路径）
            digests = [f"【检索原文（未结构化）】\n{records[:8000]}"]

        # S5 leader 最终选型
        selection = llm_tools.extract_json_object(
            await llm_tools.chat(
                system=SELECT_SYSTEM,
                user=(
                    f"本地算力约束：\n{gpu_env[:3000]}\n\n候选四要素萃取：\n"
                    + ("\n\n".join(digests))[:12000]
                ),
                temperature=0.2,
            )
        )
        chosen_title = str(selection.get("title") or "")
        trace.add(
            "S5-select", f"选中：{chosen_title[:60]}",
            json.dumps(
                {"choice": selection.get("choice"), "reasons": selection.get("reasons"),
                 "risks": selection.get("risks")}, ensure_ascii=False
            ),
        )

        # S6 writer 起草复现方案
        plan = await _delegate(
            caller, state, aics["writer"],
            "请调用 paper.write（mode=full）起草一份**论文复现实验方案**（不是论文本身），"
            "内容须含：目标与复现范围、模型与数据集选型、训练流程、评估指标、"
            "算力匹配与降规模预案、时间预估。把生成的 Markdown 原样返回。\n"
            f"topic=论文复现方案：{chosen_title}\n"
            f"materials=算力约束：\n{gpu_env[:2500]}\n\n选中理由与风险："
            f"{json.dumps(selection, ensure_ascii=False)[:2000]}\n\n"
            f"论文四要素：\n{digests[0][:6000] if digests else '（无）'}",
            name="S6-write-plan", trace=trace,
        )

        # S7 critic 审方案（最多 2 轮：初审 + 1 次返工）
        verdict_text = ""
        for round_no in (1, 2):
            review = await _delegate(
                caller, state, aics["critic"],
                "请调用 paper.review 审查下面这份复现实验方案（重点：可行性、严谨性、"
                "算力匹配是否属实），并把工具返回的 JSON 原样输出。\n\n"
                f"参考资料（算力约束）：\n{gpu_env[:2500]}\n\n待审方案：\n{plan[:12000]}",
                name=f"S7-review-r{round_no}", trace=trace,
            )
            verdict_text = review
            if _verdict(review) != "REVISE" or round_no == 2:
                break
            plan = await _delegate(
                caller, state, aics["writer"],
                "请调用 paper.write（mode=full）按审稿意见修订复现实验方案，逐条回应问题，"
                "把修订后的 Markdown 原样返回。\n"
                f"topic=论文复现方案（修订版）：{chosen_title}\n"
                f"materials=算力约束：\n{gpu_env[:2000]}\n\n审稿意见：\n{review[:3500]}\n\n"
                f"上一版方案：\n{plan[:9000]}",
                name=f"S7-rework-r{round_no}", trace=trace,
            )

        # 落盘
        (OUT / "report.md").write_text(plan, encoding="utf-8")
        (OUT / "review.md").write_text(verdict_text, encoding="utf-8")
        (OUT / "gpu-env.txt").write_text(gpu_env, encoding="utf-8")
        (OUT / "records.txt").write_text(records, encoding="utf-8")
        (OUT / "selection.json").write_text(
            json.dumps(selection, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n[done] 最终裁决={_verdict(verdict_text)}，产物在 {OUT}", flush=True)
    finally:
        for rt in runtimes.values():
            try:
                await rt.stop_serving()
            except Exception:  # noqa: BLE001
                pass
        for p in providers.values():
            try:
                await p.aclose()
            except Exception:  # noqa: BLE001
                pass
        if not trace.saved:
            trace.save()  # 中途异常也要留下已执行步骤的轨迹
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
