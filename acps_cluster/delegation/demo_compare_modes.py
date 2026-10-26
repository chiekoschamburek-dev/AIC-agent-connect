"""双模式真实业务对比（DeepSeek）：DeepResearch 委托隧道 vs wit 原生中转，AutoResearch 自研模式 vs 单 agent。

同一任务分别用"研枢多 partner + 委托隧道"与"wit 原生单 agent 中转"执行，
采集四类指标：墙钟时延 / 模型调用次数 / 上下文搬运字节 / 产物规模。
全部真实 LLM（DeepSeek），无脚本化桩。

    cd my-project && python acps_cluster/delegation/demo_compare_modes.py
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
import time  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

PROJECT = Path(__file__).resolve().parents[2]
CLUSTER = PROJECT / "acps_cluster"
for _p in (str(PROJECT), str(CLUSTER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import agents  # noqa: E402  加载 .env
import os as _os_mod  # noqa: E402

_os_mod.environ.setdefault("OPENAI_MODEL", "deepseek-v4-flash")

from wit import (  # noqa: E402
    AcpsDirect, AgentCertificateRequest, AgentProviderProfile, AcpsConfig, WitAgent, WitRuntime,
)
from wit.acps.discovery import cache_discovered_agent, normalize_discovered_agent  # noqa: E402
from wit.hooks.acps.delegation import AipDelegationHooks  # noqa: E402

import partner_scout  # noqa: E402
import cluster_roster  # noqa: E402
from delegation.demo_domain import TRUST  # noqa: E402
from delegation.demo_fanout import _aic_of  # noqa: E402
from supervisor import SupervisorRunHooks  # noqa: E402

SUPERVISOR_WS = CLUSTER / "ws-supervisor"
SCOUT_WS = CLUSTER / "ws-scout"
CRITIC_WS = CLUSTER / "ws-critic"
TRUST = SCOUT_WS / "acps" / "trust-bundle.pem"

PORT_SCOUT = 18941
PORT_LEADER = 18942

TOPIC = "图神经网络在知识图谱补全中的应用"


def _acps_config(port: int) -> AcpsConfig:
    return AcpsConfig.custom(
        registry_url="https://localhost:9001",
        ca_url="https://localhost:9003",
        discovery_url="https://localhost:9005",
        public_endpoint=f"https://localhost:{port}/rpc",
        certificate=AgentCertificateRequest(dns=("localhost",)),
        provider=AgentProviderProfile(name="yanshu", organization="Wit", email="e@e.com", country_code="CN"),
        strict_interop=True,
    )


class CountingProvider:
    """包住真实 provider：计调用次数、记最大单次输入字符。"""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.calls = 0
        self.peak_context_chars = 0

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
                total += len(str(getattr(part, "text", "") or ""))
        self.peak_context_chars = max(self.peak_context_chars, total)
        return await self.inner.complete(request)


async def _serve_scout(port: int, tmp: Path):
    ws = tmp / f"ws-scout-{port}"
    shutil.copytree(SCOUT_WS / "acps", ws / "acps")
    counter = CountingProvider(agents.build_provider())
    runtime = WitRuntime(partner_scout.build_scout_agent(counter), workspace=ws,
                         acps=_acps_config(port))
    status = await runtime.serve_direct_partner(host="127.0.0.1", port=port, amp=False)
    acs = runtime.acs_draft()
    acs["aic"] = _aic_of(SCOUT_WS)
    for e in acs.get("endPoints", []) or []:
        e["url"] = f"https://localhost:{port}/rpc"
    print(f"[cmp] scout :{port} → {status.status}")
    return runtime, acs, counter


async def main() -> None:
    out = CLUSTER / "demo_run" / "compare"
    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="compare-"))
    model_name = _os_mod.environ.get("OPENAI_MODEL", "?")
    started = time.monotonic()

    results = {}
    runtimes = []
    try:
        scout_rt, scout_acs, scout_counter = await _serve_scout(PORT_SCOUT, tmp)
        runtimes.append(scout_rt)

        # ═══ 模式一：wit 原生中转（单 agent + acps_delegate，材料全过 leader 上下文） ═══
        print(f"\n═══ 模式一：wit 原生中转（{model_name}）═══")
        leader_ws = tmp / "ws-relay"
        shutil.copytree(SUPERVISOR_WS / "acps", leader_ws / "acps")
        cache = leader_ws / "acps" / "discovered"
        cache.mkdir(parents=True, exist_ok=True)
        scout_acs = dict(scout_acs)
        scout_acs["aic"] = _aic_of(SCOUT_WS)
        for e in scout_acs.get("endPoints", []) or []:
            e["url"] = f"https://localhost:{PORT_SCOUT}/rpc"
        cache_discovered_agent(cache_dir=cache, summary=normalize_discovered_agent(scout_acs, ranking=1),
                               raw_acs=scout_acs)
        relay_counter = CountingProvider(agents.build_provider())
        relay_leader = WitRuntime(
            WitAgent(
                id="yanshu.relay-leader", display_name="relay-leader", version="1.0.0",
                description="原生中转 leader",
                instructions=(
                    f"你是研究调度员。任务：就主题「{TOPIC}」产出一份带引用的中文综述（≥800字，"
                    "含3个章节+参考文献列表）。你只有一个信息来源：scout（AIC="
                    f"{_aic_of(SCOUT_WS)}，端点已在发现缓存）。先用 acps_discover 确认它，"
                    "再用 acps_delegate 让它检索该主题的 8-10 篇文献（要求标题+摘要要点+链接），"
                    "取回全部资料后，由你亲自撰写综述终稿（正文中标注引用），最后输出全文。"
                ),
                model=relay_counter, capabilities=[],
                hooks=[AipDelegationHooks()],
                protocols=[AcpsDirect()],
            ),
            workspace=leader_ws, acps=_acps_config(PORT_LEADER),
        )
        t0 = time.monotonic()
        relay_result = await relay_leader.run(
            f"开始执行：主题「{TOPIC}」的带引用综述任务。")
        relay_wall = time.monotonic() - t0
        relay_final = str(relay_result.final_text or "")

        def _longest_cjk(rr):
            best = ""
            for m in rr.messages or []:
                for part in getattr(m, "content", []) or []:
                    text = str(getattr(part, "text", "") or "")
                    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
                    if cjk > 300 and len(text) > len(best):
                        best = text
            return best
        relay_cjk = _longest_cjk(relay_result)
        if len(relay_cjk) > len(relay_final):
            relay_final = relay_cjk
        results["relay"] = {
            "mode": "wit 原生中转（单 leader + acps_delegate，材料过 leader 上下文）",
            "wallSeconds": round(relay_wall, 1),
            "leader_llm_turns": relay_counter.calls,
            "leader_peak_context_chars": relay_counter.peak_context_chars,
            "finalAnswerChars": len(relay_final.encode("utf-8")),
            "finalCjkChars": sum(1 for c in relay_final if "一" <= c <= "鿿"),
            "finalHead": relay_final[:200],
        }
        print(json.dumps(results["relay"], ensure_ascii=False, indent=1))

        # ═══ 模式二：DeepResearch 委托隧道（多 partner + wait_partner + 闸门） ═══
        print(f"\n═══ 模式二：研枢委托隧道（{model_name}）═══")
        # scout 保持同一个（可比性）；leader 换成生产 supervisor 剧本 + 闸门工具
        dr_ws = tmp / "ws-dr-leader"
        shutil.copytree(SUPERVISOR_WS / "acps", dr_ws / "acps")
        dr_cache = dr_ws / "acps" / "discovered"
        dr_cache.mkdir(parents=True, exist_ok=True)
        _scout_full = dict(scout_acs)
        _scout_full["aic"] = _aic_of(SCOUT_WS)
        for _e in _scout_full.get("endPoints", []) or []:
            _e["url"] = f"https://localhost:{PORT_SCOUT}/rpc"
        cache_discovered_agent(cache_dir=dr_cache, summary=normalize_discovered_agent(_scout_full, ranking=1),
                               raw_acs=_scout_full)
        roster = cluster_roster.load_roster()
        roster_text = cluster_roster.render_roster_text(roster)
        dr_counter = CountingProvider(agents.build_provider())
        dr_leader = WitRuntime(
            WitAgent(
                id="yanshu.dr-leader", display_name="dr-leader", version="1.0.0",
                description="深度模式 leader（生产 harness）",
                instructions=_dr_playbook(roster_text),
                model=dr_counter, capabilities=[],
                hooks=[AipDelegationHooks(), SupervisorRunHooks()],
                protocols=[AcpsDirect()],
            ),
            workspace=dr_ws, acps=_acps_config(PORT_LEADER),
        )

        t0 = time.monotonic()
        dr_result = await dr_leader.run(
            f"【深度研究任务】就主题「{TOPIC}」产出一份带引用的中文综述（≥800字，含3个章节"
            "+参考文献列表）。先 deep_research 激活深度模式并按阶段打卡；检索交给 scout；"
            "最后由你汇总成稿并输出全文。"
        )
        dr_wall = time.monotonic() - t0
        dr_final = str(dr_result.final_text or "")
        dr_cjk = _longest_cjk(dr_result)
        if len(dr_cjk) > len(dr_final):
            dr_final = dr_cjk
        results["deepresearch"] = {
            "mode": "研枢委托隧道（生产剧本 + 深度模式 + 闸门）",
            "wallSeconds": round(dr_wall, 1),
            "leader_llm_turns": dr_counter.calls,
            "leader_peak_context_chars": dr_counter.peak_context_chars,
            "finalAnswerChars": len(dr_final.encode("utf-8")),
            "finalCjkChars": sum(1 for c in dr_final if "一" <= c <= "鿿"),
            "finalHead": dr_final[:200],
        }
        print(json.dumps(results["deepresearch"], ensure_ascii=False, indent=1))
    finally:
        for rt in runtimes:
            await rt.stop_serving()
        shutil.rmtree(tmp, ignore_errors=True)

    # ═══ 对比与留档 ═══
    report = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "model": model_name,
        "topic": TOPIC,
        "relay": results["relay"],
        "deepresearch": results["deepresearch"],
        "note": "两模式同一任务同一模型（DeepSeek）；原生命令链 vs 研枢多 partner 委托链",
    }
    (out / "compare-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n══════════════ 对比（同任务·同模型） ══════════════")
    r, d = results["relay"], results["deepresearch"]
    for name, left, right in [
        ("墙钟时延 (s)", r["wallSeconds"], d["wallSeconds"]),
        ("leader LLM 回合", r["leader_llm_turns"], d["leader_llm_turns"]),
        ("leader 峰值上下文 (字符)", r["leader_peak_context_chars"], d["leader_peak_context_chars"]),
        ("产物 (字符)", r["finalAnswerChars"], d["finalAnswerChars"]),
    ]:
        print(f"{name:<28}{str(left):>14}{str(right):>16}")
    print(f"\n[cmp] 报告 {out / 'compare-report.json'}")


def _dr_playbook(roster_text: str) -> str:
    from supervisor import _playbook

    return _playbook(roster_text, ["scout", "writer"])


if __name__ == "__main__":
    asyncio.run(main())
