"""PEP 本地演示（PEP_design.md 阶段二）：S1/S2/S2b/S3/S4/S5 一键复现 + 修复验证。

三个阶段：
- 阶段一 S1：PEP 关，越权直连非公开 skill 被执行（漏洞复现）
- 阶段二 S2/S2b/S3/S4：PEP 开，准入拒绝 10 种、公开能力放行、委托 token 放行
- 阶段三 S5：出站方向——受托方（critic 身份）持 token 经 `runtime.aip_delegate` 反向直连
  scout（§4.4 direct_fetch），并给出「同路径不带 token」的对照

不起 registry/CA，不联网：本地用 `serve_direct_partner` 起一个真实的 wit 直连 mTLS 监听，
用 `AipDirectClient` 持各 agent 的本地证书发真实 TaskCommand，走的是与生产完全相同的
admission 路径（协议身份验证 → AipPartnerAdmissionHooks → 任务状态机）。
出站方向靠手工把 `acps/discovered/` 缓存指向本地端点来替代 ADP 发现（生产由 seed_discovery.py 填）。

    cd yanshu-AIC && python acps_cluster/delegation/demo_pep.py

输出：控制台对比表 + `<out>/report.json` + `<out>/pep-audit.jsonl`
默认输出目录 `acps_cluster/demo_run/pep/`（可用 PEP_DEMO_OUT 覆盖）。
工作区用 ws-scout 的副本，避免污染真实 aip-tasks.sqlite3。

坑（实现期实测）：本包内 `token.py` 与标准库 `token` 同名。以脚本方式运行
（`python acps_cluster/delegation/demo_pep.py`）时 Python 会把脚本所在目录放进 sys.path[0]，
于是 `inspect → tokenize → token` 会导入到本模块并崩在 `inspect.get_annotations`。
生产部署不受影响：systemd 的 `WorkingDirectory=acps_cluster` + `python partner_scout.py`
使 sys.path[0] 是 `acps_cluster`，包内 `token.py` 不在顶层。下面三行把脚本目录从 sys.path 摘掉。
"""

import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))
_sys.path[:] = [p for p in _sys.path if _os.path.abspath(p or _os.getcwd()) != _HERE]
del _os, _sys, _HERE

import asyncio  # noqa: E402
import base64
import copy
import json
import os
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
CLUSTER = PROJECT / "acps_cluster"
for _p in (str(PROJECT), str(CLUSTER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ed25519  # noqa: E402
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
)
from wit.acps.direct import AipDirectClient  # noqa: E402
from wit.acps.discovery import cache_discovered_agent, normalize_discovered_agent  # noqa: E402
from wit.hooks.acps.delegation import AipDelegationHooks  # noqa: E402

import partner_scout  # noqa: E402
from delegation import (  # noqa: E402
    direct_fetch,
    issue_token,
    load_signing_context,
    token_data_item,
)

SCOUT_WS = CLUSTER / "ws-scout"
SUPERVISOR_WS = CLUSTER / "ws-supervisor"
CRITIC_WS = CLUSTER / "ws-critic"

SCOUT_AIC = "1.2.156.3088.1.1.F3BHZ2.2RKLSI.OCPRRD.0PMW"
LEADER_AIC = "1.2.156.3088.1.1.ZV8ZLB.RJ5XH2.GOA7Q6.1672"
CRITIC_AIC = "1.2.156.3088.1.1.BJ5X2N.QYTBJJ.6GH1CI.12KL"
SYNTHESIS_AIC = "1.2.156.3088.1.1.MDY3T9.FWPJR8.N9ZAI3.123Y"

PUBLIC_SKILL = "arxiv.search"
PRIVATE_SKILL = "scholar.deep_fetch"

TRUST_BUNDLE = SCOUT_WS / "acps" / "trust-bundle.pem"
PORT_PEP_OFF = 18445
PORT_PEP_ON = 18446
PORT_OUTBOUND = 18447
PORT_CALLER = 18448

ROWS: list[dict] = []


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _command(
    *,
    sender_aic: str,
    skill: str | None,
    session_id: str,
    task_id: str,
    token: str | None = None,
    target_skill_hint: str | None = None,
    command: str = "start",
) -> dict:
    data_items: list[dict] = []
    if skill:
        data_items.append({"type": "text", "text": "深度检索图神经网络", "metadata": {"skillId": skill}})
    if token:
        data_items.append(
            token_data_item(token, target_aic=SCOUT_AIC, skill_id=target_skill_hint or skill or PRIVATE_SKILL)
        )
    return {
        "type": "task-command",
        "id": f"msg-{uuid.uuid4()}",
        "sentAt": _iso_now(),
        "senderRole": "leader",
        "senderId": sender_aic,
        "sessionId": session_id,
        "taskId": task_id,
        "command": command,
        "commandParams": {},
        "dataItems": data_items,
    }


async def _send(*, caller_ws: Path, caller_aic: str, port: int, command: dict) -> dict:
    client = AipDirectClient(
        client_certificate=caller_ws / "acps" / "client" / "certificate.pem",
        client_private_key=caller_ws / "acps" / "client" / "private-key.pem",
        trust_bundle=TRUST_BUNDLE,
        local_aic=caller_aic,
        strict_interop=True,
    )
    request = {
        "jsonrpc": "2.0",
        "method": "rpc",
        "id": f"req-{uuid.uuid4()}",
        "params": {"command": command},
    }
    try:
        return await client.call(f"https://localhost:{port}/rpc", request, expected_partner_aic=SCOUT_AIC)
    finally:
        await client.aclose()


def _describe(response: dict) -> tuple[str, str]:
    """把 JSON-RPC 响应归一化成 (结论, 人话)。"""
    if "error" in response:
        error = response["error"] or {}
        return f"jsonrpc-error {error.get('code')}", str(error.get("message"))
    result = response.get("result") or {}
    state = (result.get("status") or {}).get("state") or "?"
    items = ((result.get("status") or {}).get("dataItems")) or []
    text = items[0].get("text") if items else ""
    return f"task {state}", str(text or "")


def _record(scenario: str, expect: str, response: dict, note: str = "") -> None:
    outcome, detail = _describe(response)
    ROWS.append(
        {
            "scenario": scenario,
            "expected": expect,
            "outcome": outcome,
            "detail": detail,
            "note": note,
            "allowed": outcome.startswith("task accepted") or outcome.startswith("task working"),
        }
    )


def _record_delegate(scenario: str, expect: str, result: dict, note: str = "") -> None:
    """`runtime.aip_delegate` 返回的是任务快照（不是 JSON-RPC 响应），单独归一化。"""
    state = str(result.get("state") or "?")
    ROWS.append(
        {
            "scenario": scenario,
            "expected": expect,
            "outcome": f"task {state}",
            "detail": str(result.get("summary") or ""),
            "note": note or f"taskRef={result.get('task_ref')}",
            "allowed": state in {"accepted", "working"},
        }
    )


# ─────────────────────────── 签发工具 ───────────────────────────


def _leader_signing():
    return load_signing_context(SUPERVISOR_WS / "acps" / "client", trust_bundle=TRUST_BUNDLE)


def _rogue_signing():
    now = datetime.now(timezone.utc)
    key = ed25519.Ed25519PrivateKey.generate()
    name = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "rogue")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now.replace(year=now.year + 1))
        .add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier("acps://rogue.aic")]), False)
        .sign(key, None)
    )
    from delegation import SigningContext

    return SigningContext(private_key=key, chain=(cert,), issuer_aic="rogue.aic")


def _issue(**kwargs) -> str:
    return issue_token(_leader_signing(), **kwargs).token


def _tamper(token: str) -> str:
    head, payload, signature = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["acps_skill_id"] = "evil.skill"
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"{head}.{forged}.{signature}"


# ─────────────────────────── 服务端 ───────────────────────────


async def _serve(port: int, *, enabled: bool, audit_path: Path, tmp_root: Path):
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text("", encoding="utf-8")  # 每次运行独立，避免跨运行累积误导
    os.environ["ACPS_PEP_ENABLED"] = "1" if enabled else "0"
    os.environ["ACPS_PEP_LEADER_AICS"] = LEADER_AIC
    os.environ["ACPS_PEP_AUDIT_PATH"] = str(audit_path)
    workspace = tmp_root / f"ws-{port}"
    shutil.copytree(SCOUT_WS / "acps", workspace / "acps")
    runtime = WitRuntime(partner_scout.build_scout_agent(None), workspace=workspace, acps=_acps_config(port))
    status = await runtime.serve_direct_partner(host="127.0.0.1", port=port, amp=False)
    print(f"[demo] scout 监听 https://localhost:{port}/rpc → {status.status} (PEP {'on' if enabled else 'off'})")
    return runtime


# ─────────────────────────── 场景 ───────────────────────────


async def phase_s1(out: Path, tmp_root: Path) -> None:
    """S1 漏洞复现：PEP 关闭 → 非公开 skill 的越权直连被执行。"""
    print("\n──── 阶段一：S1 漏洞复现（ACPS_PEP_ENABLED=0）────")
    audit = out / "audit-pep-off.jsonl"
    runtime = await _serve(PORT_PEP_OFF, enabled=False, audit_path=audit, tmp_root=tmp_root)
    try:
        session_id = f"session-s1-{uuid.uuid4()}"
        response = await _send(
            caller_ws=CRITIC_WS,
            caller_aic=CRITIC_AIC,
            port=PORT_PEP_OFF,
            command=_command(
                sender_aic=CRITIC_AIC,
                skill=PRIVATE_SKILL,
                session_id=session_id,
                task_id=f"task-{uuid.uuid4()}",
            ),
        )
        _record("S1 越权直连非公开 skill（PEP 关）", "task accepted（漏洞成立）", response)
        await _cancel(CRITIC_AIC, session_id, response, PORT_PEP_OFF)
    finally:
        await runtime.stop_serving()
    print(f"[demo] PEP 关闭时审计文件存在: {audit.exists()}（默认 accept，不产生裁决记录）")


async def _cancel(caller_aic: str, session_id: str, response: dict, port: int) -> None:
    task_id = ((response.get("result") or {}).get("taskId")) or ""
    if not task_id:
        return
    await _send(
        caller_ws=CRITIC_WS,
        caller_aic=caller_aic,
        port=port,
        command=_command(
            sender_aic=caller_aic, skill=None, session_id=session_id, task_id=task_id, command="cancel"
        ),
    )


async def phase_s234(out: Path, tmp_root: Path) -> None:
    print("\n──── 阶段二：S2 / S2b / S3 / S4（ACPS_PEP_ENABLED=1）────")
    audit = out / "audit-pep-on.jsonl"
    runtime = await _serve(PORT_PEP_ON, enabled=True, audit_path=audit, tmp_root=tmp_root)
    signing = _leader_signing()
    try:
        # S2：同一越权调用 → 拒绝
        session_id = f"session-s2-{uuid.uuid4()}"
        response = await _send(
            caller_ws=CRITIC_WS,
            caller_aic=CRITIC_AIC,
            port=PORT_PEP_ON,
            command=_command(
                sender_aic=CRITIC_AIC, skill=PRIVATE_SKILL, session_id=session_id, task_id=f"task-{uuid.uuid4()}"
            ),
        )
        _record("S2 同一越权调用（PEP 开）", "task rejected", response)

        # S2b：社区互联线——外部 agent 调公开 skill
        response = await _send(
            caller_ws=CRITIC_WS,
            caller_aic=CRITIC_AIC,
            port=PORT_PEP_ON,
            command=_command(
                sender_aic=CRITIC_AIC, skill=PUBLIC_SKILL, session_id=f"session-s2b-{uuid.uuid4()}",
                task_id=f"task-{uuid.uuid4()}",
            ),
        )
        _record("S2b 外部调公开 skill（L0）", "task accepted", response)
        await _cancel(CRITIC_AIC, (response.get("result") or {}).get("sessionId", ""), response, PORT_PEP_ON)

        # S3：leader 签发 token，受托方持 token 直连
        session_id = f"session-s3-{uuid.uuid4()}"
        issued = issue_token(
            signing, delegate_aic=CRITIC_AIC, target_aic=SCOUT_AIC, skill_id=PRIVATE_SKILL
        )
        response = await _send(
            caller_ws=CRITIC_WS,
            caller_aic=CRITIC_AIC,
            port=PORT_PEP_ON,
            command=_command(
                sender_aic=CRITIC_AIC, skill=PRIVATE_SKILL, session_id=session_id,
                task_id=f"task-{uuid.uuid4()}", token=issued.token,
            ),
        )
        _record(
            "S3 持委托 token 直连（L2）", "task accepted", response,
            note=f"jti={issued.jti[:8]}… delegationId={issued.delegation_id}",
        )
        await _cancel(CRITIC_AIC, session_id, response, PORT_PEP_ON)

        # S4：越界拒绝七种 + 两种补充
        await _s4_cases(signing, issued)
    finally:
        await runtime.stop_serving()


async def _s4_cases(signing, consumed) -> None:
    async def send_with(token: str | None, *, skill=PRIVATE_SKILL, caller=CRITIC_WS, caller_aic=CRITIC_AIC,
                        hint=None):
        session_id = f"session-s4-{uuid.uuid4()}"
        return await _send(
            caller_ws=caller,
            caller_aic=caller_aic,
            port=PORT_PEP_ON,
            command=_command(
                sender_aic=caller_aic, skill=skill, session_id=session_id, task_id=f"task-{uuid.uuid4()}",
                token=token, target_skill_hint=hint,
            ),
        )

    expired = _issue(
        delegate_aic=CRITIC_AIC, target_aic=SCOUT_AIC, skill_id=PRIVATE_SKILL,
        ttl_seconds=10, now=time.time() - 1000,
    )
    _record("S4a 过期 token", "task rejected", await send_with(expired))

    valid = _issue(delegate_aic=CRITIC_AIC, target_aic=SCOUT_AIC, skill_id=PRIVATE_SKILL)
    _record("S4b 篡改 payload", "task rejected", await send_with(_tamper(valid)))

    mismatch_skill = _issue(delegate_aic=CRITIC_AIC, target_aic=SCOUT_AIC, skill_id=PUBLIC_SKILL)
    _record(
        "S4c token 授权的 skill 与请求不符", "task rejected",
        await send_with(mismatch_skill, hint=PUBLIC_SKILL),
    )

    wrong_actor = _issue(delegate_aic=SYNTHESIS_AIC, target_aic=SCOUT_AIC, skill_id=PRIVATE_SKILL)
    _record("S4d act 与调用方证书不符", "task rejected", await send_with(wrong_actor))

    bad_route = _issue(
        delegate_aic=CRITIC_AIC, target_aic=SCOUT_AIC, skill_id=PRIVATE_SKILL,
        allowed_route=[f"agent:{LEADER_AIC}", f"agent:{SYNTHESIS_AIC}", f"agent:{SCOUT_AIC}"],
    )
    _record("S4e acps_allowed_route 不含当前 hop", "task rejected", await send_with(bad_route))

    _record("S4f 重放已消费的 token", "task rejected", await send_with(consumed.token))

    session_mismatch = _issue(
        delegate_aic=CRITIC_AIC, target_aic=SCOUT_AIC, skill_id=PRIVATE_SKILL, session_id="session-OTHER"
    )
    _record("S4g 会话绑定不符（扩展 claim）", "task rejected", await send_with(session_mismatch))

    rogue = issue_token(
        _rogue_signing(), delegate_aic=CRITIC_AIC, target_aic=SCOUT_AIC, skill_id=PRIVATE_SKILL
    )
    _record("S4h 签发者不在 trust-bundle", "task rejected", await send_with(rogue.token))

    # S4j：链可信 ≠ 有权签发——critic 用自己的合法证书自签（2026-09-28 审计 PoC）
    community = load_signing_context(CRITIC_WS / "acps" / "client", trust_bundle=TRUST_BUNDLE)
    self_signed = issue_token(
        community, delegate_aic=CRITIC_AIC, target_aic=SCOUT_AIC, skill_id=PRIVATE_SKILL
    )
    _record("S4j 受信社区成员自签 token", "task rejected", await send_with(self_signed.token))

    cnf_token = _issue(
        delegate_aic=CRITIC_AIC, target_aic=SCOUT_AIC, skill_id=PRIVATE_SKILL,
        delegate_certificate_fingerprint=_fingerprint(CRITIC_WS),
    )
    response = await send_with(cnf_token)
    _record(
        "S4i 带 cnf 的 token（偏差：admission 无法校验证书指纹）", "task accepted + 审计标记",
        response, note="admission 请求不暴露对端证书，cnf 记 cnfUnverifiable=true，不做拒绝",
    )
    await _cancel(CRITIC_AIC, (response.get("result") or {}).get("sessionId", ""), response, PORT_PEP_ON)


def _fingerprint(ws: Path) -> str:
    from delegation import certificate_sha256_fingerprint

    cert = x509.load_pem_x509_certificates((ws / "acps" / "client" / "certificate.pem").read_bytes())[0]
    return certificate_sha256_fingerprint(cert)


# ─────────────────────────── 出站方向（受托方反向直连） ───────────────────────────


def _caller_runtime(tmp_root: Path, target_port: int):
    """造一个受托方 runtime（critic 身份）+ 把 discovery 缓存指向本地 scout。

    `runtime.aip_delegate` 是 runtime API，不需要 LLM，也不需要 registry：
    只要 `acps/enrollment.json` 是 active、并在 `acps/discovered/` 里有目标端点即可
    （生产里这份缓存由 seed_discovery.py / ADP 发现填充）。
    """
    root = tmp_root / f"ws-{target_port}-caller"
    if not (root / "acps").exists():
        shutil.copytree(CRITIC_WS / "acps", root / "acps")
    cache_dir = root / "acps" / "discovered"
    cache_dir.mkdir(parents=True, exist_ok=True)

    acs = json.loads((SCOUT_WS / "acps" / "acs.final.json").read_text(encoding="utf-8"))
    acs["aic"] = SCOUT_AIC
    for endpoint in acs.get("endPoints", []):
        endpoint["url"] = f"https://localhost:{target_port}/rpc"
    cache_discovered_agent(
        cache_dir=cache_dir,
        summary=normalize_discovered_agent(acs, ranking=1),
        raw_acs=acs,
    )

    agent = WitAgent(
        id="yanshu.critic-partner",
        display_name="研枢-Critic-Partner",
        version="1.0.0",
        description="演示用受托方（critic 身份）",
        instructions="演示用；本阶段只走 runtime.aip_delegate，不跑模型。",
        model=None,
        capabilities=[],
        hooks=[AipDelegationHooks()],
        protocols=[AcpsDirect()],
    )
    return WitRuntime(agent, workspace=root, acps=_acps_config(PORT_CALLER)), AgentState()


async def phase_outbound(out: Path, tmp_root: Path) -> None:
    """S5 出站方向：受托方持 token 经 `runtime.aip_delegate` 直连目标伙伴（§4.4 direct_fetch）。"""
    print("\n──── 阶段三：S5 出站直连（critic 持 token 反向调用 scout）────")
    audit = out / "audit-pep-outbound.jsonl"
    runtime = await _serve(PORT_OUTBOUND, enabled=True, audit_path=audit, tmp_root=tmp_root)
    try:
        caller, state = _caller_runtime(tmp_root, PORT_OUTBOUND)
        issued = issue_token(
            _leader_signing(),
            delegate_aic=CRITIC_AIC,
            target_aic=SCOUT_AIC,
            skill_id=PRIVATE_SKILL,
        )
        try:
            result = await direct_fetch(
                caller,
                state,
                target_aic=SCOUT_AIC,
                request="深度检索图神经网络",
                token=issued.token,
                skill_id=PRIVATE_SKILL,
            )
            _record_delegate(
                "S5a 受托方持 token 反向直连", "task accepted", result,
                note=f"jti={issued.jti[:8]}… delegationId={issued.delegation_id}",
            )
        except Exception as exc:  # noqa: BLE001
            _record("S5a 受托方持 token 反向直连", "task accepted",
                    {"error": {"code": type(exc).__name__, "message": str(exc)}})

        # 对照：同一条出站路径不带 token（skill 提示照给）
        try:
            result = await caller.aip_delegate(
                state,
                partner_aic=SCOUT_AIC,
                request=[TextPart("深度检索图神经网络"), DataPart(data={"skillId": PRIVATE_SKILL})],
            )
            _record_delegate("S5b 同路径不带 token（对照）", "task rejected", result)
        except Exception as exc:  # noqa: BLE001
            _record("S5b 同路径不带 token（对照）", "task rejected",
                    {"error": {"code": type(exc).__name__, "message": str(exc)}})
    finally:
        await runtime.stop_serving()

    if audit.exists():
        print("──── 出站审计（audit-pep-outbound.jsonl）────")
        for line in audit.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            print(
                f"  {record['decision']:5s} {record['rule']:20s} code={record['code']} "
                f"caller={record['callerAic'].split('.')[-1]} jti={(record['jti'] or '-')[:8]}"
            )


# ─────────────────────────── 主流程 ───────────────────────────


async def main() -> None:
    out = Path(os.getenv("PEP_DEMO_OUT", str(CLUSTER / "demo_run" / "pep")))
    out.mkdir(parents=True, exist_ok=True)
    tmp_root = Path(os.getenv("TEMP", "/tmp")) / f"pep-demo-{uuid.uuid4().hex[:8]}"
    tmp_root.mkdir(parents=True, exist_ok=True)
    print(f"[demo] 输出目录 {out}\n[demo] 临时工作区 {tmp_root}")

    await phase_s1(out, tmp_root)
    await phase_s234(out, tmp_root)
    await phase_outbound(out, tmp_root)

    print("\n══════════════════════ 结果汇总 ══════════════════════")
    for row in ROWS:
        mark = "✅" if _verdict(row) else "❌"
        print(f"{mark} {row['scenario']}")
        print(f"    期望 {row['expected']} / 实际 {row['outcome']}  {row['detail'][:70]}")
        if row["note"]:
            print(f"    备注 {row['note']}")

    audit = out / "audit-pep-on.jsonl"
    if audit.exists():
        print("\n──── PEP 审计（audit-pep-on.jsonl）────")
        for line in audit.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            print(
                f"  {record['decision']:5s} {record['rule']:22s} code={record['code']} "
                f"caller={record['callerAic'].split('.')[-1]} jti={(record['jti'] or '-')[:8]} "
                f"reason={record['reason']}"
            )

    report = {
        "generatedAt": _iso_now(),
        "scoutAic": SCOUT_AIC,
        "cases": copy.deepcopy(ROWS),
        "passed": sum(1 for row in ROWS if _verdict(row)),
        "total": len(ROWS),
    }
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[demo] {report['passed']}/{report['total']} 场景符合预期；明细写入 {out / 'report.json'}")
    shutil.rmtree(tmp_root, ignore_errors=True)


def _verdict(row: dict) -> bool:
    expected = row["expected"]
    outcome = row["outcome"]
    if expected.startswith("task accepted"):
        return outcome in {"task accepted", "task working"}
    if expected.startswith("task rejected"):
        return outcome == "task rejected"
    return True


if __name__ == "__main__":
    asyncio.run(main())
