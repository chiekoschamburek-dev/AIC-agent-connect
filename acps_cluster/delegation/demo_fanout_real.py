"""量化对比（真实 LLM 版，留档用）：leader 中转 vs 委托隧道。

与 demo_fanout.py（脚本化 provider、确定性、可重复）互补的姊妹篇：本版**所有模型位都是
真实 LLM**（目标伙伴的 agent 循环、材料生成、中转 leader、隧道 leader 的签发决策、消费方
汇总），材料是真实生成的内容（约 600 字/份）。因为烧真实 API 且结果带模型方差，定位是
**录播留档材料**——跑一次、存 JSON + Markdown，不进一键演示。

与脚本版的指标对齐，另加两项真实版才有的：
- leader LLM 回合：**实测**（CountingProvider 包真实 provider 计数），脚本版是解析值 N+1/2；
- leader 峰值上下文（字符）：模型最后一次请求的输入消息总字符——中转模式材料真的从
  leader 的上下文里过了一遍，隧道模式为 0 附近（只有句柄元数据）。

拓扑与脚本版一致：3 个目标伙伴（scout/synthesis/writer 身份、各起直连 mTLS、PEP 开），
材料技能 mat.fetch（非公开，L1/L2 门槛）；中转=leader 逐个 acps_delegate 取回并在最终
回答逐字保留三份材料；隧道=leader 调 issue_delegation_token 一次签三枚（真实工具闭环，
JWS 入 vault），消费方（critic 身份）持令牌并发直连取回并做真实汇总。

    cd yanshu-AIC && python acps_cluster/delegation/demo_fanout_real.py
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
import time  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

PROJECT = Path(__file__).resolve().parents[2]
CLUSTER = PROJECT / "acps_cluster"
for _p in (str(PROJECT), str(CLUSTER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import agents  # noqa: E402  加载 .env（llm_tools 与 provider 都依赖）

from wit import (  # noqa: E402
    AcpsDirect, DataPart, TextPart, WitAgent, WitRuntime, capability,
)
from wit.acps.discovery import cache_discovered_agent, normalize_discovered_agent  # noqa: E402
from wit.hooks.acps.delegation import AipDelegationHooks  # noqa: E402

import llm_tools  # noqa: E402
from delegation import (  # noqa: E402
    PepConfig, build_pep_hooks, direct_fetch,
)
from delegation.demo_fanout import (  # noqa: E402  复用姊妹篇的骨架件
    CRITIC_WS, SUPERVISOR_WS, TRUST_BUNDLE, _acps_config, _aic_of, _await_products,
    _peak_concurrency, _product_text,
)
from delegation.leader_tools import DelegationLeaderHooks  # noqa: E402
from delegation.vault import TokenVault  # noqa: E402

SKILL = "mat.fetch"
MODEL_TOOL = "mat_fetch"
TOPICS = {
    "scout": "图神经网络在知识图谱补全中的最新进展",
    "synthesis": "大模型长上下文处理的稀疏注意力方法",
    "writer": "多智能体系统的通信高效训练技术",
}
PORTS = {"scout": 18481, "synthesis": 18482, "writer": 18483}
CALLER_PORT = 18484
LEADER_PORT = 18485
WAIT_TIMEOUT = 360.0

LEADER_AIC = _aic_of(SUPERVISOR_WS)
CRITIC_AIC = _aic_of(CRITIC_WS)
TARGET_WS = {
    "scout": CLUSTER / "ws-scout",
    "synthesis": CLUSTER / "ws-synthesis",
    "writer": CLUSTER / "ws-writer",
}

SPANS: list[dict] = []  # mat_fetch 的真实执行区间（并行度实测）


class CountingProvider:
    """包住真实 provider：计回合数、记最后一次请求的输入消息总字符（≈峰值上下文）。"""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.calls = 0
        self.last_context_chars = 0

    @property
    def capabilities(self):
        return self.inner.capabilities

    def supports_content(self, part, *, role: str):
        return self.inner.supports_content(part, role=role)

    async def complete(self, request):
        self.calls += 1
        total = 0
        for message in getattr(request, "messages", []) or []:
            for part in getattr(message, "content", []) or []:
                total += len(getattr(part, "text", "") or "")
        self.last_context_chars = max(self.last_context_chars, total)
        return await self.inner.complete(request)


@capability(
    id=SKILL,
    description="按主题生成一份约 600 字的结构化中文综述材料（真实 LLM 生成，非公开技能）。",
    public=False,
    acps_skill=True,
    tags=["demo", "fanout-real"],
)
async def mat_fetch(topic: str) -> dict:
    started = time.perf_counter()
    material = await llm_tools.chat(
        "你是资料员。就给定主题写一份约 600 字的中文综述材料：分 3-4 个要点，每点 2-3 句，"
        "内容务实，不编造具体论文与数字。直接输出材料正文，不要寒暄。",
        f"主题：{topic}",
        temperature=0.3,
    )
    ended = time.perf_counter()
    SPANS.append({"start": started, "end": ended})
    return {"topic": topic, "material": material, "bytes": len(material.encode("utf-8")),
            "gen_seconds": round(ended - started, 1)}


def _target_agent(key: str, aic: str, audit: Path, provider) -> WitAgent:
    cfg = PepConfig(
        enabled=True, my_aic=aic, leader_aics=frozenset({LEADER_AIC}),
        trust_bundle=TRUST_BUNDLE, audit_path=audit,
        public_skills=frozenset(), has_non_public_skills=True,
    )
    return WitAgent(
        id=f"yanshu.mat-{key}", display_name=f"研枢-Mat-{key}", version="1.0.0",
        description="演示目标伙伴：按主题生成综述材料（真实 LLM）。",
        instructions=(
            "收到请求后调用 mat_fetch（topic 用任务里给出的主题），拿到材料后"
            "调 aip_submit 把材料**全文**作为文本产物交回，不要摘要、不要截断。"
        ),
        model=provider, capabilities=[mat_fetch],
        aip_hooks=build_pep_hooks(cfg=cfg), protocols=[AcpsDirect()],
    )


async def _serve_target(key: str, aic: str, provider, tmp: Path, out: Path):
    ws = tmp / f"ws-t-{key}"
    shutil.copytree(TARGET_WS[key] / "acps", ws / "acps")
    audit = out / f"real-audit-{key}.jsonl"
    audit.write_text("", encoding="utf-8")
    runtime = WitRuntime(_target_agent(key, aic, audit, provider), workspace=ws,
                         acps=_acps_config(PORTS[key]))
    status = await runtime.serve_direct_partner(host="127.0.0.1", port=PORTS[key], amp=False)
    acs = runtime.acs_draft()
    acs["aic"] = aic
    for endpoint in acs.get("endPoints", []) or []:
        endpoint["url"] = f"https://localhost:{PORTS[key]}/rpc"
    print(f"[real] 目标 {key:9s} :{PORTS[key]} → {status.status}")
    return runtime, acs


def _outbound_runtime(ws: Path, label: str, port: int, tmp: Path, acs_list: list[dict],
                      provider=None, instructions: str = "演示出站 runtime。",
                      extra_hooks=()):
    root = tmp / f"ws-{label}"
    shutil.copytree(ws / "acps", root / "acps")
    cache = root / "acps" / "discovered"
    cache.mkdir(parents=True, exist_ok=True)
    for acs in acs_list:
        cache_discovered_agent(cache_dir=cache, summary=normalize_discovered_agent(acs, ranking=1), raw_acs=acs)
    agent = WitAgent(
        id=f"yanshu.real-{label}", display_name=f"yanshu.real-{label}", version="1.0.0",
        description="真实 LLM 出站/决策 runtime",
        instructions=instructions, model=provider, capabilities=[],
        hooks=[AipDelegationHooks(), *extra_hooks], protocols=[AcpsDirect()],
    )
    return WitRuntime(agent, workspace=root, acps=_acps_config(port))


def _request_parts(topic: str) -> list:
    return [TextPart(f"请就主题生成材料：{topic}"), DataPart(data={"skillId": SKILL})]


async def main() -> None:
    out = CLUSTER / "demo_run" / "fanout" / "real"
    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(__import__("tempfile").mkdtemp(prefix="fanout-real-"))
    aics = {k: _aic_of(ws) for k, ws in TARGET_WS.items()}
    model_name = __import__("os").environ.get("OPENAI_MODEL", "?")
    print(f"[real] 模型={model_name} | 输出 {out}\n")

    runtimes = []
    target_counters: dict[str, CountingProvider] = {}
    try:
        # ── 目标伙伴 ×3（真实 provider） ──
        acs_list = []
        for key, ws in TARGET_WS.items():
            counter = CountingProvider(agents.build_provider())
            target_counters[key] = counter
            runtime, acs = await _serve_target(key, aics[key], counter, tmp, out)
            runtimes.append(runtime)
            acs_list.append(acs)
        targets = [acs["aic"] for acs in acs_list]

        # ══ 模式一：中转（leader 真实 LLM，逐个取回并逐字保留） ══
        roster = "\n".join(f"- {aics[k]} → 主题「{TOPICS[k]}」" for k in TARGET_WS)
        relay_counter = CountingProvider(agents.build_provider())
        relay_leader = _outbound_runtime(
            SUPERVISOR_WS, "relay-leader", LEADER_PORT, tmp, acs_list,
            provider=relay_counter,
            instructions=(
                "你是中转调度者。用 acps_delegate 依次向下列伙伴取材料（必须串行：上一个取回"
                "并阅读完再派下一个），三份全部取回后，在最终回答里按伙伴分三节**逐字完整**"
                "附上三份材料全文（这是硬性要求，缺一节即失败）：\n" + roster
            ),
        )
        SPANS.clear()
        t0 = time.perf_counter()
        relay_result = await relay_leader.run(
            "任务：向三个伙伴各取一份主题材料，并在最终回答中逐字保留三份全文。"
        )
        relay_wall = time.perf_counter() - t0
        final_text = str(relay_result.final_text or "")
        relay = {
            "mode": "relay",
            "wall_seconds": round(relay_wall, 1),
            "leader_llm_turns": relay_counter.calls,
            "leader_peak_context_chars": relay_counter.last_context_chars,
            "leader_answer_bytes": len(final_text.encode("utf-8")),
            "observed_parallelism": _peak_concurrency(SPANS),
            "materials_in_answer": sum(1 for k in TARGET_WS if TOPICS[k][:6] in final_text),
            "target_llm_turns": {k: c.calls for k, c in target_counters.items()},
            "note": "材料经 leader 上下文（工具结果+最终回答逐字保留）",
        }
        print("\n──── 中转模式（真实 LLM）────")
        print("   ", json.dumps(relay, ensure_ascii=False))
        for audit in out.glob("real-audit-*.jsonl"):
            audit.write_text("", encoding="utf-8")
        for key, counter in target_counters.items():
            counter.calls = 0

        # ══ 模式二：委托隧道（leader 真实签发决策 + 消费方真实汇总） ══
        vault = TokenVault()
        tunnel_counter = CountingProvider(agents.build_provider())
        tunnel_leader = _outbound_runtime(
            SUPERVISOR_WS, "tunnel-leader", LEADER_PORT, tmp, acs_list,
            provider=tunnel_counter,
            instructions=(
                "你是调度者。伙伴已报缺材料，授权你签发委托令牌让它自己去取：调用"
                " issue_delegation_token **一次**（delegate_aic=<受托伙伴AIC>，targets=下面三个"
                f"目标 AIC 的数组，skill_id={SKILL}），然后在最终回答里列出返回的全部 jti 句柄。"
                "不要自己取材料。\n受托伙伴 AIC：" + CRITIC_AIC +
                "\n目标伙伴：\n" + "\n".join(f"- {a}" for a in targets),
            ),
            extra_hooks=(DelegationLeaderHooks(workspace=SUPERVISOR_WS, vault=vault),),
        )
        SPANS.clear()
        t0 = time.perf_counter()
        sign_result = await tunnel_leader.run(
            "伙伴报缺材料：请签发三枚委托令牌（一次工具调用）并报告句柄。"
        )
        sign_seconds = time.perf_counter() - t0
        jtis = [k for k in list(vault._held)]  # 签发后 vault 里待下发的句柄
        consumer_counter = CountingProvider(agents.build_provider())
        consumer = _outbound_runtime(
            CRITIC_WS, "consumer", CALLER_PORT, tmp, acs_list,
            provider=consumer_counter,
            instructions="你是材料汇总员：对收到的每份材料各写一段不超过 80 字的要点摘要。",
        )
        from wit import AgentState  # noqa: PLC0415

        state = AgentState()

        async def one(jti: str, aic: str):
            held = vault.resolve(jti)
            snapshot = await direct_fetch(consumer, state, target_aic=aic,
                                          request=f"请就主题生成材料：{TOPICS[next(k for k in aics if aics[k] == aic)]}",
                                          token=held.token, skill_id=SKILL)
            return aic, snapshot

        issued_at = time.perf_counter()
        snapshots = await asyncio.gather(*(one(j, a) for j, a in zip(jtis, targets)))
        texts = []
        for aic, snapshot in snapshots:
            done = await _await_products(consumer, state, str(snapshot.get("task_ref") or ""),
                                         timeout=WAIT_TIMEOUT)
            texts.append(_product_text(done.get("products") or []))
        fetch_wall = time.perf_counter() - issued_at
        summary_result = await consumer.run(
            "三份材料如下，请分别给出不超过 80 字的要点摘要：\n\n"
            + "\n\n---\n\n".join(texts)
        )
        tunnel_wall = sign_seconds + fetch_wall + 0.0  # 汇总与签发/取回合计口径见 note
        summary_at = time.perf_counter()
        tunnel = {
            "mode": "delegation",
            "wall_seconds": round((summary_at - t0), 1),
            "sign_seconds": round(sign_seconds, 1),
            "fetch_seconds": round(fetch_wall, 1),
            "leader_llm_turns": tunnel_counter.calls,
            "leader_peak_context_chars": tunnel_counter.last_context_chars,
            "consumer_llm_turns": consumer_counter.calls,
            "consumer_peak_context_chars": consumer_counter.last_context_chars,
            "observed_parallelism": _peak_concurrency(SPANS),
            "material_bytes_to_consumer": [len(t.encode("utf-8")) for t in texts],
            "tokens_issued": len(jtis),
            "target_llm_turns": {k: c.calls for k, c in target_counters.items()},
            "note": "材料不经 leader；签发一次（真实工具闭环，JWS 入 vault 不过模型）+ 并发直连 + 消费方汇总",
        }
        print("\n──── 委托隧道模式（真实 LLM）────")
        print("   ", json.dumps(tunnel, ensure_ascii=False))
    finally:
        for runtime in runtimes:
            await runtime.stop_serving()
        shutil.rmtree(tmp, ignore_errors=True)

    report = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "model": model_name,
        "topics": TOPICS,
        "note": "真实 LLM 版（录播留档）：模型带方差，数字仅供量级参考；确定性版本见 demo_fanout.py",
        "relay": relay,
        "delegation": tunnel,
        "audit": {
            p.name: [json.loads(line)["rule"] for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
            for p in sorted(out.glob("real-audit-*.jsonl"))
        },
    }
    (out / "fanout-real-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    md = [
        "# 中转 vs 委托隧道：真实 LLM 对比（留档）",
        "",
        f"- 时间：{report['generatedAt']}  模型：{model_name}",
        f"- 与脚本化版本（demo_fanout.py，确定性）互补；本版所有模型位为真实 LLM。",
        "",
        "| 指标 | 中转 | 委托隧道 |",
        "|---|---|---|",
        f"| 墙钟时延 (s) | {relay['wall_seconds']} | {tunnel['wall_seconds']} |",
        f"| 实测并行度 | {relay['observed_parallelism']} | {tunnel['observed_parallelism']} |",
        f"| leader LLM 回合（实测） | {relay['leader_llm_turns']} | {tunnel['leader_llm_turns']} |",
        f"| leader 峰值上下文 (字符) | {relay['leader_peak_context_chars']} | {tunnel['leader_peak_context_chars']} |",
        f"| 材料（字节） | 最终回答 {relay['leader_answer_bytes']} B，含材料 {relay['materials_in_answer']}/3 | 直达消费方 {sum(tunnel['material_bytes_to_consumer'])} B |",
        f"| 消费方回合 | —（材料滞留 leader） | {tunnel['consumer_llm_turns']} |",
        "",
        f"中转模式材料经 leader 上下文搬运（{relay['note']}）；隧道模式 {tunnel['note']}。",
        "目标侧准入审计：", json.dumps(report["audit"], ensure_ascii=False),
    ]
    (out / "fanout-real-结论.md").write_text("\n".join(md), encoding="utf-8")

    print("\n══════════════════════ 对比（真实 LLM） ══════════════════════")
    for name, left, right in [
        ("墙钟时延 (s)", relay["wall_seconds"], tunnel["wall_seconds"]),
        ("实测并行度", relay["observed_parallelism"], tunnel["observed_parallelism"]),
        ("leader LLM 回合（实测）", relay["leader_llm_turns"], tunnel["leader_llm_turns"]),
        ("leader 峰值上下文 (字符)", relay["leader_peak_context_chars"], tunnel["leader_peak_context_chars"]),
        ("材料去向", f"leader 回答 {relay['materials_in_answer']}/3", f"消费方 {sum(tunnel['material_bytes_to_consumer'])}B"),
    ]:
        print(f"{name:<26}{str(left):>16}{str(right):>16}")
    print(f"\n[real] 报告 {out / 'fanout-real-report.json'}\n[real] 结论 {out / 'fanout-real-结论.md'}")


if __name__ == "__main__":
    asyncio.run(main())
