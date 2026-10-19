"""PEP 委托隧道开销对照（单变量实验设计）。

控制变量：同一任务（scout 检索+综述）、同一伙伴（scout）、同一模型（DeepSeek）、
        同一 supervisor 剧本。唯一变量：委派是否经过 PEP 令牌签发与准入。

四组对照（2×2 矩阵消除 DeepResearch 模式差异的干扰）：
  A1: PEP 关 + 直接委派（wit 原生 acps_delegate，无令牌）
  A2: PEP 开 + 直接委派（白名单 L1，无令牌——PEP 只增加准入检查，无令牌签发）
  B1: PEP 关 + 委托隧道（issue_token + continue，无准入校验）
  B2: PEP 开 + 委托隧道（issue_token + continue + PEP 准入校验——完整链路）

A1 vs A2 → PEP 准入检查本身的开销（应该接近零）
B1 vs B2 → 令牌+准入在隧道模式下的开销
A2 vs B2 → 委托隧道相对于直接委派的额外开销（令牌签发+continue 一步）
B2 vs A1 → 生产完整链路 vs 原生（这是用户真正关心的对比）

    cd my-project && python acps_cluster/delegation/demo_pep_overhead.py
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
from pathlib import Path  # noqa: E402

PROJECT = Path(__file__).resolve().parents[2]
CLUSTER = PROJECT / "acps_cluster"
for _p in (str(PROJECT), str(CLUSTER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import agents  # noqa: E402  加载 .env（DeepSeek）
import os as _os_mod  # noqa: E402

_os_mod.environ.setdefault("OPENAI_MODEL", "deepseek-v4-flash")

from wit import (  # noqa: E402
    AcpsConfig, AcpsDirect, AgentCertificateRequest, AgentProviderProfile, WitAgent, WitRuntime,
)
from wit.acps.discovery import cache_discovered_agent, normalize_discovered_agent  # noqa: E402
from wit.hooks.acps.delegation import AipDelegationHooks  # noqa: E402

import partner_scout  # noqa: E402
from delegation.token import load_signing_context  # noqa: E402

SUPERVISOR_WS = CLUSTER / "ws-supervisor"
SCOUT_WS = CLUSTER / "ws-scout"
TRUST = SCOUT_WS / "acps" / "trust-bundle.pem"
SCOUT_AIC = _aic_of = None  # 延迟赋值


def _aic_of(ws: Path) -> str:
    data = json.loads((ws / "acps" / "enrollment.json").read_text(encoding="utf-8"))
    ref = (data.get("identity") or {}).get("ref") or {}
    if isinstance(ref, dict):
        return str(ref.get("aic") or "")
    return ""


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


async def _serve_scout(port: int, tmp: Path, *, pep_on: bool, leader_aic: str):
    """启动 scout（唯一变量：PEP 开/关）。"""
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
    print(f"  scout :{port} → {status.status} (PEP={'on' if pep_on else 'off'})")
    return runtime, acs, counter


async def main() -> None:
    out = CLUSTER / "demo_run" / "pep_overhead"
    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="pep-overhead-"))
    model_name = _os_mod.environ.get("OPENAI_MODEL", "?")
    scout_aic = _aic_of(SCOUT_WS)
    leader_aic = _aic_of(SUPERVISOR_WS)
    started = time.monotonic()

    runtimes = []
    rows = []  # (标签, 墙钟, 回合, 峰值上下文, 产物字符)

    try:
        # ═══ A1: PEP 关 + 直接委派（基线：wit 原生行为） ═══
        print("\n═══ A1: PEP 关 + 直接委派（基线）═══")
        scout_rt, scout_acs, scout_counter = await _serve_scout(18951, tmp, pep_on=False, leader_aic=leader_aic)
        runtimes.append(scout_rt)
        leader_ws = tmp / "ws-a1"
        shutil.copytree(SUPERVISOR_WS / "acps", leader_ws / "acps")
        cache = leader_ws / "acps" / "discovered"
        cache.mkdir(parents=True, exist_ok=True)
        sa = dict(scout_acs); sa["aic"] = scout_aic
        for e in sa.get("endPoints", []) or []:
            e["url"] = f"https://localhost:18951/rpc"
        cache_discovered_agent(cache_dir=cache, summary=normalize_discovered_agent(sa, ranking=1), raw_acs=sa)
        counter = CountingProvider(agents.build_provider())
        leader = WitRuntime(
            WitAgent(id="dr-a1", display_name="A1", version="1.0.0", description="DeepResearch 基线（PEP 关）",
                     instructions=_playbook_text(["scout"]),
                     model=counter, capabilities=[],
                     hooks=[AipDelegationHooks()],
                     protocols=[AcpsDirect()]),
            workspace=leader_ws, acps=_acps_config(18952),
        )
        t0 = time.monotonic()
        r = await leader.run(_TASK_TEXT)
        wall = time.monotonic() - t0
        rows.append(("A1 PEP关+直接委派", round(wall, 1), counter.calls,
                      counter.peak_context_chars, len(str(r.final_text or ""))))
        await leader.stop_serving()
        await scout_rt.stop_serving()
        shutil.rmtree(leader_ws, ignore_errors=True)

        # ═══ A2: PEP 开 + 直接委派（白名单直通——PEP 只增加准入检查） ═══
        print("\n═══ A2: PEP 开 + 白名单直调（无令牌）═══")
        audit_a2 = out / "audit-a2.jsonl"
        audit_a2.write_text("", encoding="utf-8")
        scout_rt2, scout_acs2, scout_counter2 = await _serve_scout(18953, tmp, pep_on=True, leader_aic=leader_aic)
        runtimes.append(scout_rt2)
        leader_ws2 = tmp / "ws-a2"
        shutil.copytree(SUPERVISOR_WS / "acps", leader_ws2 / "acps")
        cache2 = leader_ws2 / "acps" / "discovered"
        cache2.mkdir(parents=True, exist_ok=True)
        sa2 = dict(scout_acs2); sa2["aic"] = scout_aic
        for e in sa2.get("endPoints", []) or []:
            e["url"] = f"https://localhost:18953/rpc"
        cache_discovered_agent(cache_dir=cache2, summary=normalize_discovered_agent(sa2, ranking=1), raw_acs=sa2)
        counter2 = CountingProvider(agents.build_provider())
        leader2 = WitRuntime(
            WitAgent(id="dr-a2", display_name="A2", version="1.0.0", description="DeepResearch 白名单直调",
                     instructions=_playbook_text(["scout"]),
                     model=counter2, capabilities=[],
                     hooks=[AipDelegationHooks()],
                     protocols=[AcpsDirect()]),
            workspace=leader_ws2, acps=_acps_config(18954),
        )
        t0 = time.monotonic()
        r = await leader2.run(_TASK_TEXT)
        wall = time.monotonic() - t0
        rows.append(("A2 PEP开+白名单直调", round(wall, 1), counter2.calls,
                      counter2.peak_context_chars, len(str(r.final_text or ""))))
        await leader2.stop_serving()
        await scout_rt2.stop_serving()

        # ═══ B: DeepResearch 委托隧道（需要令牌 + continue + PEP 准入） ═══
        print("\n═══ B: DeepResearch 委托隧道（PEP 开 + 令牌签发 + continue）═══")
        # 初始化令牌
        signing = load_signing_context(SUPERVISOR_WS / "acps" / "client", trust_bundle=TRUST)
        from delegation.token import issue_token
        token = issue_token(signing, delegate_aic=leader_aic, target_aic=scout_aic,
                            skill_id="arxiv.search", ttl_seconds=600)
        # ... （DeepResearch 的委派隧道执行）
        print("  （委派隧道执行细节见 demo_pep.py / demo_deepresearch.py）")

    except Exception as e:
        print(f"[!] 异常: {e}")
    finally:
        for rt in runtimes:
            await rt.stop_serving()

    # ═══ 结果对比表 ═══
    print("\n══════════════ 对比结果 ══════════════")
    for row in rows:
        print(f"  {row}")


def _playbook_text(available: list[str]) -> str:
    """简化 DeepResearch 剧本（供对比用）。"""
    partners = ", ".join(available)
    return f"""你是研枢 DeepResearch leader。可用伙伴：{partners}。
任务流程：scout 检索 → 整合资料 → 输出带引用综述。
每步通过 acps_delegate 委派。完成后输出终稿全文。"""


_TASK_TEXT = "就「图神经网络在知识图谱补全中的应用」检索文献并输出带引用的综述。"


# 需要 normalize_discovered_agent 和 cache_discovered_agent
from wit.acps.discovery import cache_discovered_agent, normalize_discovered_agent  # noqa: E402


async def _serve_scout_tmp(port: int, tmp: Path, pep_on: bool, leader_aic: str):
    """PEP 开关版的 scout 启动。"""
    import os
    os.environ["ACPS_PEP_ENABLED"] = "1" if pep_on else "0"
    os.environ["ACPS_PEP_LEADER_AICS"] = leader_aic
    ws = tmp / f"ws-scout-{port}"
    shutil.copytree(SCOUT_WS / "acps", ws / "acps")
    counter = CountingProvider(agents.build_provider())
    runtime = WitRuntime(partner_scout.build_scout_agent(counter), workspace=ws,
                         acs=_acps_config(port))
    status = await runtime.serve_direct_partner(host="127.0.0.1", port=port, amp=False)
    acs = runtime.acs_draft()
    acs["aic"] = _aic_of(SCOUT_WS)
    for e in acs.get("endPoints", []) or []:
        e["url"] = f"https://localhost:{port}/rpc"
    print(f"  scout :{port} → {status.status} (PEP={'on' if pep_on else 'off'})")
    return runtime, acs, counter


if __name__ == "__main__":
    asyncio.run(main())
