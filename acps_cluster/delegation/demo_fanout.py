"""量化对比：leader 中转 vs 委托隧道（PEP_design.md §5.3 / §2.1）。

同一件"扇出取材料"的事，跑两种拓扑，量三件事：

| 指标 | 中转模式（现状） | 委托隧道（remedy-3） |
|---|---|---|
| 谁去取材料 | leader 自己，逐个取（串行） | 受托方 A，持令牌并发取 |
| 目标数 | N | N |
| 准入依据 | L1 白名单（leader 直接在名单里） | L2 委托令牌（逐目标一枚，aud 绑定） |
| 材料去向 | 经 leader 的模型上下文搬运（Σ 字节进上下文） | 直达 A（leader 上下文 0 字节） |
| leader LLM 回合 | N 次取回 + 1 次 continue = N+1 | 1 次签发（一次签多枚）+ 1 次 continue = 2 |
| 并行度 | 1 | N |

**全部真实**：起 3 个真实的 wit 直连 mTLS 监听（三个不同 AIC 的运行实例，PEP 开启），
调用走与生产相同的 admission 路径；目标伙伴真的睡 S 秒、真的产出 K KB 材料
（用一个**脚本化 provider** 驱动它的业务能力，因此不需要任何 LLM/网络）。
聚合出「材料搬运字节 / 墙钟时延 / 实测目标侧并行度」，并读目标侧审计核对准入路径。

    cd yanshu-AIC && python acps_cluster/delegation/demo_fanout.py

输出：控制台对比表 + `<out>/fanout-report.json` + `<out>/audit-*.jsonl`。
默认输出目录 `acps_cluster/demo_run/fanout/`（可用 FANOUT_DEMO_OUT 覆盖）。

坑：本包内 `token.py` 与标准库 `token` 同名；以脚本方式运行时脚本目录会进 sys.path[0]。
下面三行把脚本目录摘掉（与 demo_pep.py 同因）。
"""

import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))
_sys.path[:] = [p for p in _sys.path if _os.path.abspath(p or _os.getcwd()) != _HERE]
del _os, _sys, _HERE

import asyncio  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
import uuid  # noqa: E402
from pathlib import Path  # noqa: E402

PROJECT = Path(__file__).resolve().parents[2]
CLUSTER = PROJECT / "acps_cluster"
for _p in (str(PROJECT), str(CLUSTER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from wit import (  # noqa: E402
    AcpsConfig,
    AcpsDirect,
    AgentCertificateRequest,
    AgentProviderProfile,
    AgentState,
    DataPart,
    TextPart,
    WitAgent,
    WitRuntime,
    capability,
)
from wit.acps.discovery import cache_discovered_agent, normalize_discovered_agent  # noqa: E402
from wit.hooks.acps.delegation import AipDelegationHooks  # noqa: E402
from wit.kernel.capabilities import ToolManager  # noqa: E402
from wit.kernel.content import ContentKind  # noqa: E402
from wit.kernel.messages import ModelResponse, ToolCall  # noqa: E402
from wit.providers.base import ProviderCapabilities  # noqa: E402

from delegation import (  # noqa: E402
    PepConfig,
    build_pep_hooks,
    direct_fetch,
    issue_token,
    load_signing_context,
)

SUPERVISOR_WS = CLUSTER / "ws-supervisor"
CRITIC_WS = CLUSTER / "ws-critic"
TRUST_BUNDLE = SUPERVISOR_WS / "acps" / "trust-bundle.pem"

# 三个目标伙伴（复用现有本地身份：各自的 AIC 不同 ⇒ aud 逐目标绑定，与 §5.3 一致）
TARGET_WORKSPACES = {
    "scout": CLUSTER / "ws-scout",
    "synthesis": CLUSTER / "ws-synthesis",
    "writer": CLUSTER / "ws-writer",
}
TARGET_PORTS = {"scout": 18471, "synthesis": 18472, "writer": 18473}
CALLER_PORT = 18474  # 受托方 A（critic 身份）的出站 runtime
LEADER_PORT = 18475  # leader 的出站 runtime

SKILL = "noisy.fetch"
MODEL_TOOL = ToolManager.safe_model_tool_name(SKILL)  # noisy_fetch
DELAY_SECONDS = 1.2
PAYLOAD_KB = 64
WAIT_TIMEOUT = 60.0

# 目标侧真实执行区间（模块级，供并行度实测）
SPANS: list[dict] = []


def _aic_of(workspace: Path) -> str:
    data = json.loads((workspace / "acps" / "enrollment.json").read_text(encoding="utf-8"))
    return str(((data.get("identity") or {}).get("ref") or {}).get("aic") or "")


LEADER_AIC = _aic_of(SUPERVISOR_WS)
CRITIC_AIC = _aic_of(CRITIC_WS)


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


# ───────────────────────── 目标伙伴（真睡真产出，无 LLM） ─────────────────────────


@capability(
    id=SKILL,
    description="演示用非公开技能：睡指定秒数后返回一块指定大小的材料。",
    public=False,       # 不公开 ⇒ L0 不放行，必须 L1/L2
    acps_skill=True,    # 进 ACS（可被发现），但对 PEP 仍是非公开
    tags=["demo", "fanout"],
)
async def noisy_fetch(delay_seconds: float = DELAY_SECONDS, payload_kb: int = PAYLOAD_KB) -> dict:
    started = time.perf_counter()
    await asyncio.sleep(float(delay_seconds))
    ended = time.perf_counter()
    SPANS.append({"start": started, "end": ended, "keys": (int(delay_seconds * 1000), int(payload_kb))})
    size = max(1, int(payload_kb)) * 1024
    return {"payload": "M" * size, "bytes": size, "elapsed": round(ended - started, 3)}


class ScriptedProvider:
    """脚本化 provider：不调 LLM，按消息历史决定下一步工具调用。

    步骤 1：调 noisy_fetch（业务动作，真睡真产出）
    步骤 2：看到工具结果后调 aip_submit，把材料作为 Product 交回
    """

    def __init__(self, *, delay: float, payload_kb: int) -> None:
        self.delay = float(delay)
        self.payload_kb = int(payload_kb)
        self.calls = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider="scripted",
            api="scripted",
            input_modalities=frozenset({ContentKind.TEXT}),
            output_modalities=frozenset({ContentKind.TEXT}),
            streaming=False,
            tools=True,
        )

    def supports_content(self, part, *, role: str) -> tuple[bool, str | None]:
        return True, None

    async def complete(self, request):
        self.calls += 1
        for message in request.messages:
            if getattr(message, "role", None) != "tool":
                continue
            payload = self._payload_of(message)
            if payload is None:
                continue
            return ModelResponse.tools(
                [
                    ToolCall(
                        id=f"submit-{uuid.uuid4().hex[:8]}",
                        name="aip_submit",
                        arguments={
                            "products": [
                                {
                                    "id": f"prod-{uuid.uuid4().hex[:8]}",
                                    "name": "noisy material",
                                    "data_items": [{"type": "text", "text": payload}],
                                }
                            ]
                        },
                    )
                ]
            )
        return ModelResponse.tools(
            [
                ToolCall(
                    id=f"fetch-{uuid.uuid4().hex[:8]}",
                    name=MODEL_TOOL,
                    arguments={"delay_seconds": self.delay, "payload_kb": self.payload_kb},
                )
            ]
        )

    @staticmethod
    def _payload_of(message) -> str | None:
        for part in getattr(message, "content", []) or []:
            text = getattr(part, "text", None)
            if not text:
                continue
            try:
                data = json.loads(text)
            except (TypeError, ValueError):
                continue
            result = data.get("result") if isinstance(data, dict) else None
            if isinstance(result, dict) and isinstance(result.get("payload"), str):
                return result["payload"]
        return None


def _build_target_agent(key: str, aic: str, audit: Path) -> WitAgent:
    cfg = PepConfig(
        enabled=True,
        my_aic=aic,
        leader_aics=frozenset({LEADER_AIC}),
        trust_bundle=TRUST_BUNDLE,
        audit_path=audit,
        public_skills=frozenset(),          # 无公开能力
        has_non_public_skills=True,         # ⇒ 未指名 skill 的请求一律 fail-closed
    )
    return WitAgent(
        id=f"yanshu.noisy-{key}",
        display_name=f"研枢-Noisy-{key}",
        version="1.0.0",
        description="演示用目标伙伴：按需睡 S 秒后交回 K KB 材料（非公开技能）。",
        instructions="收到请求后调用 noisy_fetch 取材料，再调 aip_submit 交回。",
        model=ScriptedProvider(delay=DELAY_SECONDS, payload_kb=PAYLOAD_KB),
        capabilities=[noisy_fetch],
        aip_hooks=build_pep_hooks(cfg=cfg),
        protocols=[AcpsDirect()],
    )


async def _serve_target(key: str, aic: str, tmp_root: Path, out: Path):
    port = TARGET_PORTS[key]
    workspaces_root = tmp_root / f"ws-target-{key}"
    if not (workspaces_root / "acps").exists():
        shutil.copytree(TARGET_WORKSPACES[key] / "acps", workspaces_root / "acps")
    audit = out / f"audit-{key}.jsonl"
    audit.write_text("", encoding="utf-8")
    runtime = WitRuntime(
        _build_target_agent(key, aic, audit),
        workspace=workspaces_root,
        acps=_acps_config(port),
    )
    status = await runtime.serve_direct_partner(host="127.0.0.1", port=port, amp=False)
    acs = runtime.acs_draft()
    acs["aic"] = aic
    for endpoint in acs.get("endPoints", []) or []:
        endpoint["url"] = f"https://localhost:{port}/rpc"
    skills = {s.get("id") for s in acs.get("skills", []) or []}
    print(f"[fanout] 目标 {key:9s} https://localhost:{port}/rpc  PEP=on  ACS skills={sorted(skills)}")
    return runtime, acs, status


def _identity_runtime(ws: Path, port: int, tmp_root: Path, acs_list: list[dict], label: str):
    """建一个出站 runtime（A 或 leader）+ 把每个目标的端点塞进发现缓存。"""
    root = tmp_root / f"ws-{label}"
    if not (root / "acps").exists():
        shutil.copytree(ws / "acps", root / "acps")
    cache_dir = root / "acps" / "discovered"
    cache_dir.mkdir(parents=True, exist_ok=True)
    for acs in acs_list:
        cache_discovered_agent(
            cache_dir=cache_dir,
            summary=normalize_discovered_agent(acs, ranking=1),
            raw_acs=acs,
        )
    agent = WitAgent(
        id=f"yanshu.{label}",
        display_name=f"yanshu.{label}",
        version="1.0.0",
        description="演示用出站 runtime",
        instructions="演示用；只走 runtime.aip_delegate / aip_task，不跑模型。",
        model=None,
        capabilities=[],
        hooks=[AipDelegationHooks()],
        protocols=[AcpsDirect()],
    )
    return WitRuntime(agent, workspace=root, acps=_acps_config(port)), AgentState()


# ───────────────────────── 取材料（发送 + 等待产物） ─────────────────────────


def _product_text(products: list[dict]) -> str:
    chunks: list[str] = []
    for product in products or []:
        for item in product.get("dataItems") or product.get("data_items") or []:
            if isinstance(item, dict) and item.get("type") == "text":
                chunks.append(str(item.get("text") or ""))
    return "".join(chunks)


async def _await_products(runtime, state, task_ref: str, *, timeout: float = WAIT_TIMEOUT) -> dict:
    deadline = time.monotonic() + timeout
    snapshot: dict = {}
    while time.monotonic() < deadline:
        snapshot = await runtime.aip_task(state, action="wait", task_ref=task_ref)
        if not isinstance(snapshot, dict):
            snapshot = {}
        products = snapshot.get("products") or []
        if products or str(snapshot.get("state") or "") in {
            "completed", "failed", "rejected", "cancelled", "expired",
        }:
            return snapshot
    raise TimeoutError(f"等待产物超时（task_ref={task_ref}, last_state={snapshot.get('state')}）")


def _peak_concurrency(spans: list[dict]) -> int:
    events: list[tuple[float, int]] = []
    for span in spans:
        events.append((span["start"], 1))
        events.append((span["end"], -1))
    events.sort()
    peak = current = 0
    for _, delta in events:
        current += delta
        peak = max(peak, current)
    return peak


# ───────────────────────── 两种拓扑 ─────────────────────────


def _request_body() -> list:
    return [TextPart("请提供一份材料"), DataPart(data={"skillId": SKILL})]


async def _relay_leg(leader_rt, leader_state, target_aics: list[str]) -> dict:
    """中转：leader 自己逐个取（L1 白名单），材料全部经 leader 上下文。"""
    SPANS.clear()
    t0 = time.perf_counter()
    materials: list[str] = []
    states: list[str] = []
    for aic in target_aics:
        snapshot = await leader_rt.aip_delegate(
            leader_state, partner_aic=aic, request=_request_body()
        )
        task_ref = str(snapshot.get("task_ref") or "")
        done = await _await_products(leader_rt, leader_state, task_ref)
        states.append(str(done.get("state") or ""))
        materials.append(_product_text(done.get("products") or []))
    elapsed = time.perf_counter() - t0
    return {
        "mode": "relay",
        "wall_seconds": round(elapsed, 3),
        "targets": len(target_aics),
        "leader_context_bytes": sum(len(m) for m in materials),
        "leader_llm_turns": len(target_aics) + 1,
        "observed_parallelism": _peak_concurrency(SPANS),
        "target_states": states,
        "material_bytes": [len(m) for m in materials],
    }


async def _delegation_leg(a_rt, a_state, signing, target_aics: list[str], leader_ai: dict) -> dict:
    """委托：leader 一次签发 N 枚 token（本地），A 持 token 并发取（L2），材料直达 A。"""
    SPANS.clear()
    t0 = time.perf_counter()
    issued = [
        issue_token(signing, delegate_aic=CRITIC_AIC, target_aic=aic, skill_id=SKILL)
        for aic in target_aics
    ]
    leader_ai["issue_seconds"] = round(time.perf_counter() - t0, 6)
    issued_at = time.perf_counter()

    async def one(token, aic):
        snapshot = await direct_fetch(
            a_rt, a_state, target_aic=aic, request="请提供一份材料", token=token, skill_id=SKILL
        )
        return aic, snapshot

    snapshots = await asyncio.gather(*(one(tok.token, aic) for tok, aic in zip(issued, target_aics)))

    async def wait(aic, snapshot):
        task_ref = str(snapshot.get("task_ref") or "")
        done = await _await_products(a_rt, a_state, task_ref)
        return aic, str(done.get("state") or ""), _product_text(done.get("products") or [])

    finished = await asyncio.gather(*(wait(aic, snap) for aic, snap in snapshots))
    elapsed = time.perf_counter() - issued_at
    return {
        "mode": "delegation",
        "wall_seconds": round(elapsed, 3),
        "targets": len(target_aics),
        "leader_context_bytes": 0,          # 材料不经 leader
        "leader_llm_turns": 2,              # 1 次签发（一次签多枚）+ 1 次 continue
        "observed_parallelism": _peak_concurrency(SPANS),
        "sign_seconds": leader_ai["issue_seconds"],
        "target_states": [state for _, state, _ in finished],
        "material_bytes": [len(text) for _, _, text in finished],
        "jtis": [tok.jti[:8] for tok in issued],
    }


# ───────────────────────── 审计核对 ─────────────────────────


def _read_audit(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _audit_summary(path: Path) -> dict:
    records = _read_audit(path)
    return {
        "total": len(records),
        "rules": {r["rule"]: sum(1 for x in records if x["rule"] == r["rule"]) for r in records},
        "decisions": {r["decision"]: sum(1 for x in records if x["decision"] == r["decision"]) for r in records},
    }


async def main() -> None:
    out = Path(os.getenv("FANOUT_DEMO_OUT", str(CLUSTER / "demo_run" / "fanout")))
    out.mkdir(parents=True, exist_ok=True)
    tmp_root = Path(os.getenv("TEMP", "/tmp")) / f"fanout-{uuid.uuid4().hex[:8]}"
    tmp_root.mkdir(parents=True, exist_ok=True)
    print(f"[fanout] 输出目录 {out}\n[fanout] 临时工作区 {tmp_root}")
    print(f"[fanout] 目标 {len(TARGET_WORKSPACES)} 个 | 每个睡 {DELAY_SECONDS}s、产出 {PAYLOAD_KB} KB\n")

    aics = {key: _aic_of(ws) for key, ws in TARGET_WORKSPACES.items()}
    for key, aic in aics.items():
        print(f"[fanout] {key:9s} AIC={aic}")
    print()

    runtimes = []
    targets: list[str] = []
    acs_list: list[dict] = []
    try:
        for key, aic in aics.items():
            runtime, acs, _status = await _serve_target(key, aic, tmp_root, out)
            runtimes.append(runtime)
            targets.append(acs["aic"])
            acs_list.append(acs)

        leader_rt, leader_state = _identity_runtime(
            SUPERVISOR_WS, LEADER_PORT, tmp_root, acs_list, "leader-out"
        )
        a_rt, a_state = _identity_runtime(CRITIC_WS, CALLER_PORT, tmp_root, acs_list, "critic-out")
        signing = load_signing_context(SUPERVISOR_WS / "acps" / "client", trust_bundle=TRUST_BUNDLE)

        print("\n──── 中转模式（leader 逐个取，L1 白名单）────")
        relay = await _relay_leg(leader_rt, leader_state, targets)
        print("   ", relay)
        relay_audit = {key: _audit_summary(out / f"audit-{key}.jsonl") for key in aics}

        for key in aics:
            (out / f"audit-{key}.jsonl").write_text("", encoding="utf-8")

        print("\n──── 委托隧道模式（A 持 N 枚 token 并发取，L2）────")
        leader_ai: dict = {}
        delegated = await _delegation_leg(a_rt, a_state, signing, targets, leader_ai)
        print("   ", delegated)
        delegation_audit = {key: _audit_summary(out / f"audit-{key}.jsonl") for key in aics}
    finally:
        for runtime in runtimes:
            await runtime.stop_serving()

    report = {
        "config": {
            "targets": targets,
            "delay_seconds": DELAY_SECONDS,
            "payload_kb": PAYLOAD_KB,
            "skill": SKILL,
            "delegate_aic": CRITIC_AIC,
            "leader_aic": LEADER_AIC,
        },
        "relay": relay,
        "delegation": delegated,
        "audit": {"relay": relay_audit, "delegation": delegation_audit},
    }
    (out / "fanout-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("\n══════════════════════ 对比 ══════════════════════")
    header = f"{'指标':<22}{'中转':>14}{'委托隧道':>14}"
    print(header)
    print("-" * len(header))
    rows = [
        ("墙钟时延 (s)", relay["wall_seconds"], delegated["wall_seconds"]),
        ("实测并行度", relay["observed_parallelism"], delegated["observed_parallelism"]),
        ("leader 上下文搬运 (B)", relay["leader_context_bytes"], delegated["leader_context_bytes"]),
        ("leader LLM 回合", relay["leader_llm_turns"], delegated["leader_llm_turns"]),
        ("材料总字节 (A 收到)", sum(relay["material_bytes"]), sum(delegated["material_bytes"])),
    ]
    for name, left, right in rows:
        print(f"{name:<22}{left:>14}{right:>14}")
    print("\n准入路径（目标侧审计）")
    for key in aics:
        print(f"  {key:9s} 中转 {relay_audit[key]['rules']} / 委托 {delegation_audit[key]['rules']}")
        print(f"           中转 {relay_audit[key]['decisions']} / 委托 {delegation_audit[key]['decisions']}")

    speedup = (relay["wall_seconds"] / delegated["wall_seconds"]) if delegated["wall_seconds"] else float("nan")
    print(f"\n时延加速 {speedup:.2f}x（{len(targets)} 个目标、每个 {DELAY_SECONDS}s）")
    print(f"报告 {out / 'fanout-report.json'}")


if __name__ == "__main__":
    asyncio.run(main())
