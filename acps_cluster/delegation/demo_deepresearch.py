"""DeepResearch 模式真实业务（D 系列）：多 partner 真栈流水线 + 深度模式激活。

链路（真实 LLM + 真实委派）：supervisor（深度模式激活 + deep_research 打卡）
  → scout 检索 → synthesis 精读 → contradiction 矛盾 → writer 成稿 → critic 审稿
  → review_gate（代码对账）→ 终稿。

为控制真实 LLM 的时长与花费，本演示将六阶段压缩为一次 run 内可完成的
"深度模式最小闭环"：激活深度模式 → 阶段打卡 → 走完检索→萃取→矛盾→成稿→审稿。
预算闸门的行为由 test_deep_research 覆盖（离线）；此处验证的是剧本在真实栈上的可执行性。

    cd yanshu-AIC && python acps_cluster/delegation/demo_deepresearch.py
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

import agents  # noqa: E402  加载 .env（真 LLM）

from wit import (  # noqa: E402
    AcpsDirect, AcpsConfig, AgentCertificateRequest, AgentProviderProfile, AgentState, RunConfig,
    WitAgent, WitRuntime,
)
from wit.acps.discovery import cache_discovered_agent, normalize_discovered_agent  # noqa: E402
from wit.hooks.acps.delegation import AipDelegationHooks  # noqa: E402

import partner_critic  # noqa: E402
import partner_contradiction  # noqa: E402
import partner_scout  # noqa: E402
import partner_synthesis  # noqa: E402
import partner_writer  # noqa: E402
from delegation.leader_tools import DelegationLeaderHooks  # noqa: E402
from supervisor import build_run_config  # noqa: E402
from supervisor import (  # noqa: E402  真实的 leader 剧本与工具
    WORKSPACE as SUPERVISOR_WS_PATH,
)
import cluster_roster  # noqa: E402

PORT_SCOUT = 18901
PORT_SYNTH = 18902
PORT_CONTRA = 18903
PORT_WRITER = 18904
PORT_CRITIC = 18905
PORT_LEADER = 18906


def _aic_of(ws: Path) -> str:
    data = json.loads((ws / "acps" / "enrollment.json").read_text(encoding="utf-8"))
    ref = (data.get("identity") or {}).get("ref") or {}
    return str(ref.get("aic") or "") if isinstance(ref, dict) else ""


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


async def _serve_partner(build, ws_src: Path, port: int, tmp: Path):
    ws = tmp / f"ws-{port}"
    shutil.copytree(ws_src / "acps", ws / "acps")
    runtime = WitRuntime(build(agents.build_provider()), workspace=ws, acps=_acps_config(port))
    status = await runtime.serve_direct_partner(host="127.0.0.1", port=port, amp=False)
    acs = runtime.acs_draft()
    acs["aic"] = _aic_of(ws_src)
    for e in acs.get("endPoints", []) or []:
        e["url"] = f"https://localhost:{port}/rpc"
    print(f"[dr] :{port} → {status.status}")
    return runtime, acs


TASK = ("图神经网络在知识图谱补全中的应用：检索最新进展、精读萃取、"
        "识别研究矛盾、写一篇带引用的综述初稿并送审")


async def main() -> None:
    out = CLUSTER / "demo_run" / "deepresearch"
    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="deepresearch-"))
    ws_map = {
        "scout": CLUSTER / "ws-scout", "synthesis": CLUSTER / "ws-synthesis",
        "contradiction": CLUSTER / "ws-contradiction", "writer": CLUSTER / "ws-writer",
        "critic": CLUSTER / "ws-critic",
    }
    builds = {"scout": partner_scout.build_scout_agent, "synthesis": partner_synthesis.build_synthesis_agent,
              "contradiction": partner_contradiction.build_contradiction_agent,
              "writer": partner_writer.build_writer_agent, "critic": partner_critic.build_critic_agent}

    runtimes = []
    acs_list = []
    started = time.monotonic()
    try:
        for key in ws_map:
            rt, acs = await _serve_partner(builds[key], ws_map[key],
                                           dict(scout=PORT_SCOUT, synthesis=PORT_SYNTH,
                                                contradiction=PORT_CONTRA, writer=PORT_WRITER,
                                                critic=PORT_CRITIC)[key], tmp)
            runtimes.append(rt)
            acs_list.append(acs)

        # leader：真 supervisor 剧本（深度模式附加段生效）+ 真实工具集
        roster = cluster_roster.load_roster()
        roster_text = cluster_roster.render_roster_text(roster)
        leader_ws = tmp / "ws-leader"
        shutil.copytree(CLUSTER / "ws-supervisor" / "acps", leader_ws / "acps")
        cache = leader_ws / "acps" / "discovered"
        cache.mkdir(parents=True, exist_ok=True)
        for acs in acs_list:
            cache_discovered_agent(cache_dir=cache, summary=normalize_discovered_agent(acs, ranking=1), raw_acs=acs)
        leader = WitRuntime(
            WitAgent(
                id="yanshu.deepresearch-leader", display_name="研枢-DeepResearch-Leader",
                version="1.0.0", description="深度模式 leader（真剧本）",
                instructions=cluster_common_note(roster_text),
                model=agents.build_provider(),
                capabilities=[],
                hooks=[AipDelegationHooks(), DelegationLeaderHooks(workspace=SUPERVISOR_WS_PATH)],
                aip_hooks=[],
                protocols=[AcpsDirect()],
            ),
            workspace=leader_ws, acps=_acps_config(PORT_LEADER),
        )
        print(f"[dr] leader :{PORT_LEADER} → 深度模式任务下发")
        state = AgentState()
        result = await leader.run(
            f"【深度研究任务】{TASK}\n\n"
            "先调用 deep_research 激活深度模式；按阶段纪律委派伙伴（检索→萃取→矛盾→成稿→审稿），"
            "每完成一个阶段调 deep_research 打卡；审稿用 review_gate 对账；最后输出终稿与溯源。",
            config=RunConfig(max_steps=80, tool_timeout_seconds=420.0,
                             tools=build_run_config(roster).tools),
        )
        elapsed = time.monotonic() - started
        final = str(result.final_text or "")
        # 完整消息流落盘（人工审计用），不再启发式猜终稿——
        # 提取策略：writer 产物是 tool 消息里最长的**纯中文综述文本**；
        # 找不到时回退 leader final_text。
        dump = [{"role": getattr(m, "role", "?"),
                 "parts": [str(getattr(p, "text", "") or "")[:200000]
                           for p in (getattr(m, "content", []) or [])]}
                for m in (result.messages or [])]
        (out / "messages-dump.json").write_text(
            json.dumps(dump, ensure_ascii=False, indent=1), encoding="utf-8")
        # 终稿提取：消息流里最长的纯中文综述段（writer 产物是中文；
        # leader 的英文推理日志与 JSON 信封按中文密度自然落选）
        def _cjk(s: str) -> int:
            return sum(1 for ch in s if "一" <= ch <= "鿿")

        candidates = [final] if final.strip() else []
        for m in dump:
            for part in m["parts"]:
                if len(part) > 500 and _cjk(part) / max(1, len(part)) > 0.3:
                    candidates.append(part)
        final = max(candidates, key=lambda s: (_cjk(s), len(s)))
        (out / "final-answer.md").write_text(final, encoding="utf-8")
        summary = {
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "task": TASK,
            "wallSeconds": round(elapsed, 1),
            "finalAnswerChars": len(final.encode("utf-8")),
            "hasCitations": "[1]" in final or "[2]" in final or "arxiv" in final.lower(),
            "mode": "deep-research（真实 LLM 多 partner 流水线）",
        }
        (out / "deepresearch-summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=1))
        print(f"[dr] 产物 {out}")
    finally:
        for rt in runtimes:
            await rt.stop_serving()
        shutil.rmtree(tmp, ignore_errors=True)


def cluster_common_note(roster_text: str) -> str:
    """真 supervisor 剧本（深度模式附加段在内）+ 演示名册。"""
    from supervisor import _playbook

    return _playbook(roster_text, ["scout", "synthesis", "contradiction", "writer", "critic"])


if __name__ == "__main__":
    asyncio.run(main())
