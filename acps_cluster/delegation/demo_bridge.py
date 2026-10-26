"""桥接端到端验证：伙伴在 inbound 任务执行中，capability 经 bridge 一行出站直连取材料。

验证三根线全部贯通（全离线，脚本化 provider，不烧 LLM）：
  ① 受托令牌随任务 dataItems 到达 → 剥离钩子落 local_context + ContextVar（host-only）；
  ② BridgeHooks.on_run_start 捕获 runtime/state；
  ③ 能力处理器内 `delegated_fetch()` 持令牌直连目标伙伴（目标侧 PEP L2 放行）。

拓扑：leader(supervisor) ──DT──► consumer(critic 身份，能力 probe.fetch 走桥)
                                     └──直连──► target(scout 身份，能力 mat.echo)

    cd yanshu-AIC && python acps_cluster/delegation/demo_bridge.py
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
import uuid  # noqa: E402
from pathlib import Path  # noqa: E402

PROJECT = Path(__file__).resolve().parents[2]
CLUSTER = PROJECT / "acps_cluster"
for _p in (str(PROJECT), str(CLUSTER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from wit import (  # noqa: E402
    AcpsDirect, WitAgent, WitRuntime, capability,
)
from wit.acps.direct import AipDirectClient  # noqa: E402
from wit.kernel.capabilities import ToolManager  # noqa: E402
from wit.kernel.messages import ModelResponse, ToolCall  # noqa: E402

from delegation import (  # noqa: E402
    PepConfig, build_pep_hooks, issue_token, load_signing_context, token_data_item,
)
from delegation.bridge import BridgeHooks, delegated_fetch  # noqa: E402
from delegation.demo_fanout import (  # noqa: E402
    CRITIC_WS, SUPERVISOR_WS, TRUST_BUNDLE, _acps_config, _aic_of, _await_products, _product_text,
)
from wit.hooks.acps.delegation import AipDelegationHooks  # noqa: E402

TARGET_WS = CLUSTER / "ws-scout"
PORT_TARGET = 18501
PORT_CONSUMER = 18502

TARGET_SKILL = "mat.echo"
CONSUMER_SKILL = "probe.fetch"
MATERIAL = "桥接验证材料：" + "数据材料块-" * 40


@capability(id=TARGET_SKILL, description="返回一段固定材料（桥接验证用，非公开）。",
            public=False, acps_skill=True, tags=["demo", "bridge"])
async def mat_echo() -> dict:
    return {"material": MATERIAL, "bytes": len(MATERIAL.encode("utf-8"))}


@capability(id=CONSUMER_SKILL, description="经桥持本任务收到的委托令牌直连目标伙伴取材料。",
            public=False, acps_skill=True, tags=["demo", "bridge"])
async def probe_fetch() -> dict:
    result = await delegated_fetch("请返回材料", skill_id=TARGET_SKILL)
    if not result.get("ok"):
        return {"ok": False, "awaiting": result.get("awaiting")}
    snapshot = result.get("task_ref") and result or {}
    done = await _await_products(_runtime_of(), _state_of(), str(result.get("task_ref") or ""), timeout=120)
    material = _product_text(done.get("products") or [])
    return {"ok": True, "material": material, "via": "bridge+delegated_fetch"}


def _runtime_of():
    from delegation.bridge import current_runtime

    return current_runtime()


def _state_of():
    from delegation.bridge import current_state

    return current_state()


class TwoStepProvider:
    """脚本化两步：第一步调业务能力；看到工具结果后 aip_submit 交回（离线，无 LLM）。"""

    def __init__(self, tool_name: str) -> None:
        self.tool = tool_name
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

    async def complete(self, request):
        self.calls += 1
        for message in getattr(request, "messages", []) or []:
            if getattr(message, "role", None) != "tool":
                continue
            text = "".join(getattr(p, "text", "") or "" for p in getattr(message, "content", []) or [])
            return ModelResponse.tools([ToolCall(
                id=f"submit-{uuid.uuid4().hex[:6]}", name="aip_submit",
                arguments={"products": [{"id": f"p-{uuid.uuid4().hex[:6]}", "name": "result",
                                         "data_items": [{"type": "text", "text": text}]}]},
            )])
        return ModelResponse.tools([ToolCall(id=f"call-{uuid.uuid4().hex[:6]}",
                                             name=self.tool, arguments={})])


def _agent(aic: str, audit: Path, tool: str, caps, instructions: str) -> WitAgent:
    cfg = PepConfig(enabled=True, my_aic=aic, leader_aics=frozenset({_aic_of(SUPERVISOR_WS)}),
                    trust_bundle=TRUST_BUNDLE, audit_path=audit,
                    public_skills=frozenset(), has_non_public_skills=True)
    return WitAgent(
        id=f"yanshu.bridge-{tool}", display_name=f"bridge-{tool}", version="1.0.0",
        description="桥接验证伙伴", instructions=instructions,
        model=TwoStepProvider(ToolManager.safe_model_tool_name(tool)),
        capabilities=list(caps),
        hooks=[AipDelegationHooks(), BridgeHooks()],
        aip_hooks=build_pep_hooks(cfg=cfg), protocols=[AcpsDirect()],
    )


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="bridge-"))
    out = CLUSTER / "demo_run" / "bridge"
    out.mkdir(parents=True, exist_ok=True)
    leader_aic, critic_aic, scout_aic = _aic_of(SUPERVISOR_WS), _aic_of(CRITIC_WS), _aic_of(TARGET_WS)
    audit_t, audit_c = out / "audit-target.jsonl", out / "audit-consumer.jsonl"
    audit_t.write_text("", encoding="utf-8"); audit_c.write_text("", encoding="utf-8")

    ws_t, ws_c = tmp / "ws-t", tmp / "ws-c"
    shutil.copytree(TARGET_WS / "acps", ws_t / "acps")
    shutil.copytree(CRITIC_WS / "acps", ws_c / "acps")
    target_rt = WitRuntime(_agent(scout_aic, audit_t, TARGET_SKILL, [mat_echo],
                                  "调用 mat_echo 后 aip_submit 交回材料全文。"),
                           workspace=ws_t, acps=_acps_config(PORT_TARGET))
    # 目标 ACS 供 consumer 的发现缓存解析端点（能力内出站直连必需）——漏播种即
    # "Partner has no discovered Direct JSON-RPC endpoint"
    _target_acs = target_rt.acs_draft()
    _target_acs["aic"] = scout_aic
    for _e in _target_acs.get("endPoints", []) or []:
        _e["url"] = f"https://localhost:{PORT_TARGET}/rpc"
    from wit.acps.discovery import cache_discovered_agent, normalize_discovered_agent

    _cache = ws_c / "acps" / "discovered"
    _cache.mkdir(parents=True, exist_ok=True)
    cache_discovered_agent(cache_dir=_cache, summary=normalize_discovered_agent(_target_acs, ranking=1),
                           raw_acs=_target_acs)
    consumer_rt = WitRuntime(_agent(critic_aic, audit_c, CONSUMER_SKILL, [probe_fetch],
                                    "调用 probe_fetch 后 aip_submit 交回其返回。"),
                             workspace=ws_c, acps=_acps_config(PORT_CONSUMER))
    s1 = await target_rt.serve_direct_partner(host="127.0.0.1", port=PORT_TARGET, amp=False)
    s2 = await consumer_rt.serve_direct_partner(host="127.0.0.1", port=PORT_CONSUMER, amp=False)
    print(f"[bridge] target:{s1.status} consumer:{s2.status}")

    rows = []
    try:
        issued = issue_token(
            load_signing_context(SUPERVISOR_WS / "acps" / "client", trust_bundle=TRUST_BUNDLE),
            delegate_aic=critic_aic, target_aic=scout_aic, skill_id=TARGET_SKILL,
        )
        payload = token_data_item(issued.token, target_aic=scout_aic, skill_id=TARGET_SKILL)
        client = AipDirectClient(
            client_certificate=SUPERVISOR_WS / "acps" / "client" / "certificate.pem",
            client_private_key=SUPERVISOR_WS / "acps" / "client" / "private-key.pem",
            trust_bundle=TRUST_BUNDLE, local_aic=leader_aic, strict_interop=True)
        command = {
            "type": "task-command", "id": f"m-{uuid.uuid4()}", "sentAt": "2026-10-02T00:00:00Z",
            "senderRole": "leader", "senderId": leader_aic,
            "sessionId": f"session-bridge-{uuid.uuid4()}", "taskId": f"task-{uuid.uuid4()}",
            "command": "start", "commandParams": {},
            "dataItems": [
                {"type": "text", "text": "按委托令牌直连目标取回材料", "metadata": {"skillId": CONSUMER_SKILL}},
                payload,
            ],
        }
        request = {"jsonrpc": "2.0", "method": "rpc", "id": f"req-{uuid.uuid4()}", "params": {"command": command}}
        response = await client.call(f"https://localhost:{PORT_CONSUMER}/rpc", request,
                                     expected_partner_aic=critic_aic)
        await client.aclose()
        result = response.get("result") or {}
        session_id = str(result.get("sessionId") or "")
        task_id = str(result.get("taskId") or "")
        rows.append(("B1 持令牌任务被 consumer 接受", "accepted/working",
                     str((result.get("status") or {}).get("state") or "?")))

        # 轮询 get 直到终态（能力内的出站直连 + 材料回传都在服务端异步发生）
        import time as _time

        final = {}
        for _ in range(60):
            _time.sleep(2)
            get_cmd = dict(command, command="get", commandParams={}, dataItems=[],
                           id=f"m-{uuid.uuid4()}")
            response = await client.call(f"https://localhost:{PORT_CONSUMER}/rpc",
                                         {"jsonrpc": "2.0", "method": "rpc", "id": f"req-{uuid.uuid4()}",
                                          "params": {"command": get_cmd}},
                                         expected_partner_aic=critic_aic)
            final = response.get("result") or {}
            if str((final.get("status") or {}).get("state") or "") in {"completed", "failed", "rejected"}:
                break
        await client.aclose()
        state = str((final.get("status") or {}).get("state") or "?")
        blob = json.dumps(final.get("products") or [], ensure_ascii=False)
        rows.append(("B2 consumer 任务产出（能力内出站真实执行）", "awaiting-completion（已产出产物，等 leader 关单）", state))
        rows.append(("B3 材料经桥回传进产物", "包含材料", "是" if MATERIAL[:20] in blob else "否"))

        target_rules = [json.loads(l)["rule"] for l in audit_t.read_text(encoding="utf-8").splitlines() if l.strip()]
        rows.append(("B4 目标侧准入路径", "含 delegation-token", str(target_rules)))
    finally:
        await target_rt.stop_serving()
        await consumer_rt.stop_serving()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n════════════ 桥接验证 ════════════")
    for name, expect, got in rows:
        print(f"  {name}: 期望 {expect} / 实际 {got}")
    print(f"审计：{out}")


if __name__ == "__main__":
    asyncio.run(main())
