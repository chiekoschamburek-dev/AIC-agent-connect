"""域级（CDAP 瘦身版）演示：双 provider 合作域八场景，一键复现。

拓扑（双 provider 才能让"域"不沦为仪式——单 provider 的域是 ACL 表）：
  域 lab-x-y = { gpu（算力 provider）, scout（数据 provider）, experiment（consumer）,
                 supervisor（auditor + 域签发者） }，双签章程（supervisor + gpu 各一条）。

场景：
  D1  双边签章程建域（双签名验证 + 分发两 provider）
  D2  域内 consumer 持 grant 直连 gpu.run → 放行（审计 rule=domain-grant）
  D2b 同一枚 grant 直连 scout.dataset.fetch → 放行（双资产一凭证）
  D3a 域外 critic 盗用 grant → 拒（grant-subject-mismatch，主体绑定）
  D3b 误发给域外者的 grant → 拒（grant-not-member，成员表兜底）
  D4  章程冻结 → grant 与**已派生的 task token**都在验签前被拒（domain-frozen）→
      revision=3 解冻恢复。注：MVP 文件形态，冻结生效=对端重载章程（传播延迟已知限制）
  D5  配额：max_uses=2 的 grant 第三次调用 → 拒（quota-exhausted，sqlite 持久计数）
  D6  嵌套：leader 持域 grant（根信封）→ issue 工具按 parent_jti 派生（继承域标识、
      exp≤父、skill∈父资产）→ experiment 持派生 token 直连 → 放行（domain-delegation）
  D7  对账：gpu 审计聚合用量报表 → gpu+leader 双签 → 验签通过
  D8  revision=2 移除 experiment → r1 时代签发的旧 grant → 拒（裁决按本地最高版本）

    cd my-project && python acps_cluster/delegation/demo_domain.py
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
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

PROJECT = Path(__file__).resolve().parents[2]
CLUSTER = PROJECT / "acps_cluster"
for _p in (str(PROJECT), str(CLUSTER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from wit import AcpsConfig, AgentCertificateRequest, AgentProviderProfile, WitRuntime  # noqa: E402
from wit.acps.direct import AipDirectClient  # noqa: E402

import partner_gpu  # noqa: E402
import partner_scout  # noqa: E402
from delegation import (  # noqa: E402
    audit_window,
    build_manifest,
    build_report,
    issue_grant,
    load_signing_context,
    sign_manifest,
    sign_report,
    token_data_item,
    verify_manifest,
    verify_report,
    write_manifest,
)
from delegation.grant import derive_delegation_claims  # noqa: E402
from delegation.issuer_tool import build_issue_token_tool  # noqa: E402
from delegation.token import issue_token  # noqa: E402
from delegation.vault import TokenVault  # noqa: E402

GPU_WS = CLUSTER / "ws-gpu"
SCOUT_WS = CLUSTER / "ws-scout"
EXP_WS = CLUSTER / "ws-experiment"
CRITIC_WS = CLUSTER / "ws-critic"
SUPERVISOR_WS = CLUSTER / "ws-supervisor"
TRUST = GPU_WS / "acps" / "trust-bundle.pem"

GPU_AIC = "1.2.156.3088.1.1.GP5060.SANDBOX1.RUN01.0IGR"
SCOUT_AIC = "1.2.156.3088.1.1.F3BHZ2.2RKLSI.OCPRRD.0PMW"
EXP_AIC = "1.2.156.3088.1.1.EXPRMT.LABEXE01.RUN01.1207"
CRITIC_AIC = "1.2.156.3088.1.1.BJ5X2N.QYTBJJ.6GH1CI.12KL"
LEADER_AIC = "1.2.156.3088.1.1.ZV8ZLB.RJ5XH2.GOA7Q6.1672"

GPU_SKILL = "gpu.run"
DATA_SKILL = "dataset.fetch"
DOMAIN = "lab-x-y"

PORT_GPU = 18660
PORT_SCOUT = 18661
PORT_GPU_FROZEN = 18662
PORT_GPU_R3 = 18663
PORT_GPU_R2 = 18664

ROWS: list[dict] = []


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


def _manifest(*, revision=1, status="active", members=None, previous=None):
    return build_manifest(
        domain_id=DOMAIN,
        members=members
        or [
            {"aic": GPU_AIC, "role": "provider"},
            {"aic": SCOUT_AIC, "role": "provider"},
            {"aic": EXP_AIC, "role": "consumer"},
            {"aic": LEADER_AIC, "role": "auditor"},
        ],
        assets=[
            {"providerAic": GPU_AIC, "skill": GPU_SKILL, "allowedMembers": ["consumer"]},
            {"providerAic": SCOUT_AIC, "skill": DATA_SKILL, "allowedMembers": ["consumer"]},
        ],
        issuers=[LEADER_AIC],
        revision=revision,
        status=status,
        previous=previous,
    )


def _dual_sign(manifest):
    manifest = sign_manifest(manifest, load_signing_context(SUPERVISOR_WS / "acps" / "client", trust_bundle=TRUST))
    manifest = sign_manifest(manifest, load_signing_context(GPU_WS / "acps" / "client", trust_bundle=TRUST))
    return manifest


async def _serve_partner(build_agent, base_ws: Path, port: int, manifests: list[dict], audit: Path, tmp_root: Path):
    """起一个带域章程的 provider（临时工作区副本 + domains 目录 + 独立配额库）。"""
    import os

    os.environ["ACPS_PEP_ENABLED"] = "1"
    os.environ["ACPS_PEP_LEADER_AICS"] = LEADER_AIC
    os.environ["ACPS_PEP_AUDIT_PATH"] = str(audit)
    workspace = tmp_root / f"ws-{port}"
    shutil.copytree(base_ws / "acps", workspace / "acps")
    domains_dir = workspace / "acps" / "domains"
    domains_dir.mkdir(parents=True, exist_ok=True)
    for m in manifests:
        write_manifest(m, domains_dir)
    audit.parent.mkdir(parents=True, exist_ok=True)
    audit.write_text("", encoding="utf-8")
    # build_pep_hooks 读的是各 partner 模块里的真实 WORKSPACE 路径；演示用临时工作区，
    # 经环境变量把域目录与配额库指到本次副本（load_pep_config 原生支持）
    os.environ["ACPS_PEP_DOMAINS_DIR"] = str(domains_dir)
    os.environ["ACPS_PEP_QUOTA_PATH"] = str(workspace / "acps" / "grant-quota.sqlite3")
    runtime = WitRuntime(build_agent(None), workspace=workspace, acps=_acps_config(port))
    status = await runtime.serve_direct_partner(host="127.0.0.1", port=port, amp=False)
    return runtime


async def _send(caller_ws: Path, caller_aic: str, port: int, partner_aic: str, *, skill=None, token=None):
    client = AipDirectClient(
        client_certificate=caller_ws / "acps" / "client" / "certificate.pem",
        client_private_key=caller_ws / "acps" / "client" / "private-key.pem",
        trust_bundle=TRUST, local_aic=caller_aic, strict_interop=True,
    )
    data_items = []
    if skill:
        data_items.append({"type": "text", "text": "取材料", "metadata": {"skillId": skill}})
    if token:
        data_items.append(token_data_item(token, target_aic=partner_aic, skill_id=skill or GPU_SKILL))
    command = {
        "type": "task-command", "id": f"msg-{uuid.uuid4()}",
        "sentAt": datetime.now(timezone.utc).isoformat(),
        "senderRole": "leader", "senderId": caller_aic,
        "sessionId": f"session-d-{uuid.uuid4()}", "taskId": f"task-{uuid.uuid4()}",
        "command": "start", "commandParams": {}, "dataItems": data_items,
    }
    request = {"jsonrpc": "2.0", "method": "rpc", "id": f"req-{uuid.uuid4()}", "params": {"command": command}}
    try:
        return await client.call(f"https://localhost:{port}/rpc", request, expected_partner_aic=partner_aic)
    finally:
        await client.aclose()


def _state(response: dict) -> str:
    if "error" in response:
        return f"jsonrpc-error {(response['error'] or {}).get('code')}"
    return str(((response.get("result") or {}).get("status") or {}).get("state") or "?")


def _record(scenario: str, expect: str, got: str, note: str = ""):
    ROWS.append({"scenario": scenario, "expected": expect, "outcome": got, "note": note})


def _audit_rule(audit: Path) -> list[str]:
    if not audit.is_file():
        return []
    return [
        f"{json.loads(line).get('rule')}({json.loads(line).get('domain', '')})"
        for line in audit.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


async def main() -> None:
    tmp_root = Path(tempfile.mkdtemp(prefix="domain-demo-"))
    out = CLUSTER / "demo_run" / "domain"
    out.mkdir(parents=True, exist_ok=True)
    gpu_audit = out / "gpu-audit.jsonl"
    scout_audit = out / "scout-audit.jsonl"
    leader_signing = load_signing_context(SUPERVISOR_WS / "acps" / "client", trust_bundle=TRUST)

    # ── D1 双边签章程 ──
    print("──── D1 双边签章程建域 ────")
    r1 = _dual_sign(_manifest(revision=1))
    body, signers = verify_manifest(r1, trust_bundle=TRUST)
    d1_ok = signers == frozenset({LEADER_AIC, GPU_AIC})
    _record("D1 双签章程验证", "2 签名方", f"{len(signers)} 签名方 {sorted(signers)[-1][-6:]}…" if d1_ok else "验证失败",
            f"domain={DOMAIN} assets=2 issuers=[leader]")
    r2_frozen = _dual_sign(_manifest(revision=2, status="frozen", previous=r1))
    r3_active = _dual_sign(_manifest(revision=3, status="active", previous=r2_frozen))
    members_without_exp = [
        {"aic": GPU_AIC, "role": "provider"},
        {"aic": SCOUT_AIC, "role": "provider"},
        {"aic": LEADER_AIC, "role": "auditor"},
    ]
    r2_evict = _dual_sign(_manifest(revision=2, members=members_without_exp, previous=r1))

    runtimes = []
    try:
        gpu_rt = await _serve_partner(partner_gpu.build_gpu_agent, GPU_WS, PORT_GPU, [r1], gpu_audit, tmp_root)
        scout_rt = await _serve_partner(partner_scout.build_scout_agent, SCOUT_WS, PORT_SCOUT, [r1], scout_audit, tmp_root)
        gpu_frozen_rt = await _serve_partner(partner_gpu.build_gpu_agent, GPU_WS, PORT_GPU_FROZEN, [r1, r2_frozen], out / "gpu-frozen-audit.jsonl", tmp_root)
        gpu_r3_rt = await _serve_partner(partner_gpu.build_gpu_agent, GPU_WS, PORT_GPU_R3, [r1, r2_frozen, r3_active], out / "gpu-r3-audit.jsonl", tmp_root)
        gpu_r2evict_rt = await _serve_partner(partner_gpu.build_gpu_agent, GPU_WS, PORT_GPU_R2, [r1, r2_evict], out / "gpu-r2evict-audit.jsonl", tmp_root)
        runtimes = [gpu_rt, scout_rt, gpu_frozen_rt, gpu_r3_rt, gpu_r2evict_rt]
        print(f"[demo] gpu:{PORT_GPU} scout:{PORT_SCOUT} frozen:{PORT_GPU_FROZEN} r3:{PORT_GPU_R3} r2evict:{PORT_GPU_R2}")

        # 域内 grant（consumer=experiment，双资产，配额 5）
        grant = issue_grant(
            leader_signing, domain=DOMAIN, domain_revision=1, subject_aic=EXP_AIC,
            assets=[{"providerAic": GPU_AIC, "skill": GPU_SKILL},
                    {"providerAic": SCOUT_AIC, "skill": DATA_SKILL}],
            max_uses=5, ttl_seconds=3600,
        )

        # ── D2 / D2b：域内直连（双资产一凭证） ──
        resp = await _send(EXP_WS, EXP_AIC, PORT_GPU, GPU_AIC, skill=GPU_SKILL, token=grant.token)
        _record("D2 域内 grant 直连 gpu.run", "task accepted", _state(resp))
        resp = await _send(EXP_WS, EXP_AIC, PORT_SCOUT, SCOUT_AIC, skill=DATA_SKILL, token=grant.token)
        _record("D2b 同枚 grant 直连 scout.dataset.fetch", "task accepted", _state(resp))

        # ── D3a / D3b ──
        resp = await _send(CRITIC_WS, CRITIC_AIC, PORT_GPU, GPU_AIC, skill=GPU_SKILL, token=grant.token)
        _record("D3a 域外盗用 grant", "task rejected", _state(resp))
        stolen_grant = issue_grant(
            leader_signing, domain=DOMAIN, domain_revision=1, subject_aic=CRITIC_AIC,
            assets=[{"providerAic": GPU_AIC, "skill": GPU_SKILL}], max_uses=1, ttl_seconds=600,
        )
        resp = await _send(CRITIC_WS, CRITIC_AIC, PORT_GPU, GPU_AIC, skill=GPU_SKILL, token=stolen_grant.token)
        _record("D3b 误发给域外者的 grant", "task rejected", _state(resp))

        # ── D4：冻结杀死 grant 与派生链（验签前） ──
        frozen_grant = issue_grant(
            leader_signing, domain=DOMAIN, domain_revision=2, subject_aic=EXP_AIC,
            assets=[{"providerAic": GPU_AIC, "skill": GPU_SKILL}], max_uses=5, ttl_seconds=3600,
        )
        inherit = derive_delegation_claims(frozen_grant.claims, skill_id=GPU_SKILL)
        derived = issue_token(
            leader_signing, delegate_aic=EXP_AIC, target_aic=GPU_AIC, skill_id=GPU_SKILL,
            ttl_seconds=300, not_after=frozen_grant.expires_at, extra_claims=inherit,
        )
        resp_g = await _send(EXP_WS, EXP_AIC, PORT_GPU_FROZEN, GPU_AIC, skill=GPU_SKILL, token=frozen_grant.token)
        resp_d = await _send(EXP_WS, EXP_AIC, PORT_GPU_FROZEN, GPU_AIC, skill=GPU_SKILL, token=derived.token)
        frozen_audit = out / "gpu-frozen-audit.jsonl"
        rules = _audit_rule(frozen_audit)
        _record("D4 冻结→grant 与派生 token 均死", "2×task rejected",
                f"{_state(resp_g)} + {_state(resp_d)}",
                note=f"audit={rules}")
        resp = await _send(EXP_WS, EXP_AIC, PORT_GPU_R3, GPU_AIC, skill=GPU_SKILL, token=frozen_grant.token)
        _record("D4b revision=3 解冻恢复", "task accepted", _state(resp))

        # ── D5：配额（同 jti，max_uses=2，第三次拒绝） ──
        quota_grant = issue_grant(
            leader_signing, domain=DOMAIN, domain_revision=1, subject_aic=EXP_AIC,
            assets=[{"providerAic": GPU_AIC, "skill": GPU_SKILL}], max_uses=2, ttl_seconds=3600,
        )
        s1 = _state(await _send(EXP_WS, EXP_AIC, PORT_GPU, GPU_AIC, skill=GPU_SKILL, token=quota_grant.token))
        s2 = _state(await _send(EXP_WS, EXP_AIC, PORT_GPU, GPU_AIC, skill=GPU_SKILL, token=quota_grant.token))
        s3 = _state(await _send(EXP_WS, EXP_AIC, PORT_GPU, GPU_AIC, skill=GPU_SKILL, token=quota_grant.token))
        _record("D5 配额 max_uses=2", "accepted,accepted,rejected", f"{s1},{s2},{s3}")

        # ── D6：嵌套（leader 工具真实路径：vault + parent_jti 派生） ──
        root_grant = issue_grant(
            leader_signing, domain=DOMAIN, domain_revision=1, subject_aic=LEADER_AIC,
            assets=[{"providerAic": GPU_AIC, "skill": GPU_SKILL}], max_uses=5, ttl_seconds=1800,
        )
        vault = TokenVault()
        vault.hold_grant(root_grant)
        tool = build_issue_token_tool(
            leader_signing, trust_bundle=TRUST, vault=vault,
        )
        result = await tool.handler({
            "delegate_aic": EXP_AIC, "target_aic": GPU_AIC, "skill_id": GPU_SKILL,
            "parent_jti": root_grant.jti, "ttl_seconds": 300,
        })
        derived_jws = vault.resolve(result["handles"][0]["jti"])
        d6_note = ""
        if result.get("ok") and derived_jws is not None:
            d6_claims = json.loads(__import__("base64").urlsafe_b64decode(
                derived_jws.token.split(".")[1] + "==").decode("utf-8"))
            d6_note = (f"继承域={d6_claims.get('acps_domain')} r{d6_claims.get('acps_domain_revision')} "
                       f"exp≤父={d6_claims['exp'] <= root_grant.expires_at}")
            resp = await _send(EXP_WS, EXP_AIC, PORT_GPU, GPU_AIC, skill=GPU_SKILL, token=derived_jws.token)
            # 注意：root grant 的 subject 是 leader（auditor 角色不被资产许可）——派生链按
            # delegation 画像裁决（act=experiment=consumer 成员），放行
            _record("D6 域 grant→派生→直连（工具真实路径）", "task accepted", _state(resp), note=d6_note)
        else:
            _record("D6 域 grant→派生→直连（工具真实路径）", "task accepted",
                    f"工具失败: {result.get('error')}")

        # ── D7：对账双签 ──
        lines = (out / "gpu-audit.jsonl").read_text(encoding="utf-8").splitlines()
        # 窗口取自本次审计的真实 ts 跨度：报表现在真按窗口过滤，硬编码日期会统计成 0
        report = build_report(lines, domain=DOMAIN, domain_revision=1,
                              window=audit_window(lines),
                              provider_aic=GPU_AIC)
        report = sign_report(report, load_signing_context(GPU_WS / "acps" / "client", trust_bundle=TRUST))
        report = sign_report(report, leader_signing)
        try:
            _, report_signers = verify_report(report, trust_bundle=TRUST,
                                              expected_signers={GPU_AIC, LEADER_AIC})
            ok = report_signers == frozenset({GPU_AIC, LEADER_AIC})
        except Exception as exc:  # noqa: BLE001
            ok, report_signers = False, str(exc)
        (out / "reconciliation.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        _record("D7 对账报表双签", "2 签名方", f"{len(report_signers) if ok else 0} 签名方",
                note=f"totalUses={report['totalUses']} usage={report['usage']}")

        # ── D8：revision 驱逐 ──
        resp = await _send(EXP_WS, EXP_AIC, PORT_GPU_R2, GPU_AIC, skill=GPU_SKILL, token=grant.token)
        _record("D8 r2 移除成员→旧 grant 失效", "task rejected", _state(resp))
    finally:
        for rt in runtimes:
            await rt.stop_serving()
        shutil.rmtree(tmp_root, ignore_errors=True)

    def _verdict(row: dict) -> bool:
        exp, got = row["expected"], row["outcome"]
        if "签名方" in exp:
            return got.startswith(exp)  # D1/D7：前缀即签名的数量结论
        exp2 = exp.replace("2×task rejected", "rejected,rejected").replace("task ", "")
        got2 = got.replace("task ", "")
        return (exp2.count("accepted"), exp2.count("rejected")) == (
            got2.count("accepted"), got2.count("rejected"),
        )

    print("\n══════════════════════ 结果汇总 ══════════════════════")
    for row in ROWS:
        mark = "✅" if _verdict(row) else "❌"
        print(f"{mark} {row['scenario']}")
        print(f"    期望 {row['expected']} / 实际 {row['outcome']}")
        if row["note"]:
            print(f"    备注 {row['note'][:150]}")
    print(f"\n[demo] 通过 {sum(1 for r in ROWS if _verdict(r))}/{len(ROWS)}；审计与对账产物在 {out}")


if __name__ == "__main__":
    asyncio.run(main())
