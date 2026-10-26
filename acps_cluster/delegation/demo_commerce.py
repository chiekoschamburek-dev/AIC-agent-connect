"""商业域演示（C 系列）：携带 provider 本地签发的 token 才能调用本地 GPU，其他 agent 一律不可用。

与 demo_domain（合作域）同一套机制、另一种姿态：
- 章程 issuers = **GPU_AIC（provider 本人）**，不是 supervisor；
- `ACPS_PEP_LEADER_AICS` = **空**——L1 白名单关闭，集群 leader 也要持票（provider 主权）；
- 付费在带外（paid_plans 声明），commerce.issue_paid_grant 发卡：max_uses=售卖次数；
- 客户在章程上的签名 = 接受服务条款（ToS 的协议化）。

场景：
  C1 陌生 agent（critic）空手直调 gpu.run → no-credential 拒
  C2 付费客户持 provider 签发的 grant → 放行（真实 mTLS/PEP grant 路径）
  C2b 服务交付：gpu.run 真实沙盒 CUDA 执行（能力直调，证明票真的能换到算力）
  C3 额度用尽（max_uses=3，第 4 次）→ quota-exhausted 拒
  C4 provider 冻结（违约停服）→ 全拒；revision=3 解冻恢复
  C5 计费单：对账报表双签（provider + 客户）
  C6 集群 leader（supervisor）无票直调 → 拒——"集群自己也得买"
  C7 发卡日限额（章程 maxGrantsPerDay=3，滑动 24h）：第 3 张放行、第 4 张拒发

    cd yanshu-AIC && python acps_cluster/delegation/demo_commerce.py
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
from pathlib import Path  # noqa: E402

PROJECT = Path(__file__).resolve().parents[2]
CLUSTER = PROJECT / "acps_cluster"
for _p in (str(PROJECT), str(CLUSTER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


import partner_gpu  # noqa: E402
from delegation import (  # noqa: E402
    audit_window, build_manifest, build_report, load_signing_context, sign_manifest,
    sign_report, verify_manifest, verify_report, write_manifest,
)
from delegation.commerce import load_paid_plans, write_plan_template  # noqa: E402
from delegation.demo_domain import (  # noqa: E402  复用姊妹篇骨架
    CRITIC_WS, EXP_WS, GPU_WS, SUPERVISOR_WS, TRUST, _acps_config, _record, _send, _state, ROWS,
)
from delegation.reconciliation import read_audit  # noqa: E402

GPU_AIC = "1.2.156.3088.1.1.GP5060.SANDBOX1.RUN01.0IGR"
EXP_AIC = "1.2.156.3088.1.1.EXPRMT.LABEXE01.RUN01.1207"
CRITIC_AIC = "1.2.156.3088.1.1.BJ5X2N.QYTBJJ.6GH1CI.12KL"
LEADER_AIC = "1.2.156.3088.1.1.ZV8ZLB.RJ5XH2.GOA7Q6.1672"
DOMAIN = "gpu-shop"
GPU_SKILL = "gpu.run"

PORT_MAIN = 18701
PORT_FROZEN = 18702
PORT_R3 = 18703


def _manifest(*, revision=1, status="active", previous=None):
    """商业章程：provider 定条款（issuer=自己），客户角色 consumer，资产只对 consumer 开。"""
    return build_manifest(
        domain_id=DOMAIN,
        members=[
            {"aic": GPU_AIC, "role": "provider"},
            {"aic": EXP_AIC, "role": "consumer"},  # 客户
        ],
        assets=[{"providerAic": GPU_AIC, "skill": GPU_SKILL, "allowedMembers": ["consumer"]}],
        issuers=[GPU_AIC],  # 签发权归 provider 本人——商业姿态的核心差异
        revision=revision,
        status=status,
        max_grants_per_day=3,  # 演示口径：试用商户日限 3 张（C7 场景用）
        previous=previous,
    )


def _sign_both(manifest):
    """provider 定稿 + 客户签名（签名=接受服务条款）。"""
    manifest = sign_manifest(manifest, load_signing_context(GPU_WS / "acps" / "client", trust_bundle=TRUST))
    manifest = sign_manifest(manifest, load_signing_context(EXP_WS / "acps" / "client", trust_bundle=TRUST))
    return manifest


CUDA_PROOF_MAIN = """\
import json, torch
x = torch.randn(512, 512, device="cuda")
y = float((x @ x).sum())
json.dump({"paid": True, "gpu": torch.cuda.get_device_name(0), "sum": y},
          open("paid_proof.json", "w"))
print("PAID-GPU-OK")
"""


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="commerce-"))
    out = CLUSTER / "demo_run" / "commerce"
    out.mkdir(parents=True, exist_ok=True)
    plans_path = write_plan_template(out / "paid_plans.json")
    plans = load_paid_plans(plans_path)
    print(f"[commerce] 域={DOMAIN} | 付费计划 {plans_path}（{len(plans)} 位客户）")

    issuance_quota = out / "issuance-quota.sqlite3"  # 商家控制台的发卡账本（签发侧）
    r1 = _sign_both(_manifest(revision=1))
    body, signers = verify_manifest(r1, trust_bundle=TRUST)
    daily_cap = int(body["maxGrantsPerDay"])
    _record("C0 商业章程（provider 为 issuer + 客户签名=接受条款）", "2 签名方",
            f"{len(signers)} 签名方", "issuers=[provider] members=provider,consumer")
    r2_frozen = _sign_both(_manifest(revision=2, status="frozen", previous=r1))
    r3_active = _sign_both(_manifest(revision=3, status="active", previous=r2_frozen))

    runtimes = []
    try:
        runtimes.append(await _local_serve_with(_manifests_ws(tmp, PORT_MAIN, [r1]), PORT_MAIN, out / "gpu-audit.jsonl"))
        runtimes.append(await _local_serve_with(_manifests_ws(tmp, PORT_FROZEN, [r1, r2_frozen]), PORT_FROZEN, out / "gpu-frozen-audit.jsonl"))
        runtimes.append(await _local_serve_with(_manifests_ws(tmp, PORT_R3, [r1, r2_frozen, r3_active]), PORT_R3, out / "gpu-r3-audit.jsonl"))
        print(f"[commerce] main:{PORT_MAIN} frozen:{PORT_FROZEN} r3:{PORT_R3}")

        from delegation.commerce import issue_paid_grant

        gpu_signing = load_signing_context(GPU_WS / "acps" / "client", trust_bundle=TRUST)

        # C1：陌生 agent 空手
        resp = await _send(CRITIC_WS, CRITIC_AIC, PORT_MAIN, GPU_AIC, skill=GPU_SKILL)
        _record("C1 陌生 agent 空手直调", "task rejected", _state(resp))

        # C1b：未付费发卡被拒
        _, deny = issue_paid_grant(gpu_signing, plans, customer_aic=CRITIC_AIC,
                                   domain=DOMAIN, domain_revision=1, provider_aic=GPU_AIC)
        _record("C1b 未付费客户发卡被拒", "no-paid-plan", deny or "已发卡(异常)")

        # C2：付费客户持 grant（max_uses=3）
        grant, deny2 = issue_paid_grant(gpu_signing, plans, customer_aic=EXP_AIC,
                                        domain=DOMAIN, domain_revision=1, provider_aic=GPU_AIC,
                                        max_grants_per_day=daily_cap, quota_path=issuance_quota)
        assert grant is not None, deny2
        resp = await _send(EXP_WS, EXP_AIC, PORT_MAIN, GPU_AIC, skill=GPU_SKILL, token=grant.token)
        _record("C2 付费客户持 grant 调 gpu.run", "task accepted", _state(resp),
                f"plan={grant.claims.get('acps_grant_plan')} max_uses={grant.claims['acps_max_uses']}")

        # C2b：服务交付——真实沙盒 CUDA 执行
        run = await partner_gpu.gpu_run(files={"main.py": CUDA_PROOF_MAIN}, timeout_seconds=180)
        _record("C2b 票换算力（真实沙盒 CUDA 执行）", "sandbox ok",
                "sandbox ok" if run["ok"] and "PAID-GPU-OK" in run["stdout"] else f"failed: {run['stderr'][:80]}")

        # C3：额度用尽（已用 1，再 2 次后第 4 次拒）
        s2 = _state(await _send(EXP_WS, EXP_AIC, PORT_MAIN, GPU_AIC, skill=GPU_SKILL, token=grant.token))
        s3 = _state(await _send(EXP_WS, EXP_AIC, PORT_MAIN, GPU_AIC, skill=GPU_SKILL, token=grant.token))
        s4 = _state(await _send(EXP_WS, EXP_AIC, PORT_MAIN, GPU_AIC, skill=GPU_SKILL, token=grant.token))
        _record("C3 额度用尽（第 4 次）", "accepted,accepted,rejected", f"{s2},{s3},{s4}")

        # C4：冻结停服 / 解冻
        frozen_grant, _ = issue_paid_grant(gpu_signing, plans, customer_aic=EXP_AIC,
                                           domain=DOMAIN, domain_revision=2, provider_aic=GPU_AIC,
                                           max_grants_per_day=daily_cap, quota_path=issuance_quota)
        resp_f = await _send(EXP_WS, EXP_AIC, PORT_FROZEN, GPU_AIC, skill=GPU_SKILL, token=frozen_grant.token)
        resp_r = await _send(EXP_WS, EXP_AIC, PORT_R3, GPU_AIC, skill=GPU_SKILL, token=frozen_grant.token)
        _record("C4 冻结停服→解冻恢复", "rejected 后 accepted",
                f"{_state(resp_f)} 后 {_state(resp_r)}")

        # C5：计费单（窗口取本次审计的真实 ts 跨度——报表现在真按窗口过滤了）
        lines = read_audit(out / "gpu-audit.jsonl")
        report = build_report(lines, domain=DOMAIN, domain_revision=1,
                              window=audit_window(lines),
                              provider_aic=GPU_AIC)
        report = sign_report(report, gpu_signing)
        report = sign_report(report, load_signing_context(EXP_WS / "acps" / "client", trust_bundle=TRUST))
        try:
            _, rs = verify_report(report, trust_bundle=TRUST, expected_signers={GPU_AIC, EXP_AIC})
            ok5 = rs == frozenset({GPU_AIC, EXP_AIC})
        except Exception as exc:  # noqa: BLE001
            ok5, rs = False, str(exc)
        (out / "billing.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        _record("C5 计费单（用量对账双签）", "2 签名方", f"{len(rs) if ok5 else 0} 签名方",
                f"totalUses={report['totalUses']} usage={report['usage']}")

        # C6：集群 leader 无票直调（白名单空）
        resp = await _send(SUPERVISOR_WS, LEADER_AIC, PORT_MAIN, GPU_AIC, skill=GPU_SKILL)
        _record("C6 集群 leader 无票直调（白名单空）", "task rejected", _state(resp))

        # C7：发卡日限额（此前已发 2 张：C2/C4 各 1；cap=3 → 第 3 张放行、第 4 张拒发）
        g3, d3 = issue_paid_grant(gpu_signing, plans, customer_aic=EXP_AIC,
                                  domain=DOMAIN, domain_revision=1, provider_aic=GPU_AIC,
                                  max_grants_per_day=daily_cap, quota_path=issuance_quota)
        g4, d4 = issue_paid_grant(gpu_signing, plans, customer_aic=EXP_AIC,
                                  domain=DOMAIN, domain_revision=1, provider_aic=GPU_AIC,
                                  max_grants_per_day=daily_cap, quota_path=issuance_quota)
        outcome7 = ("发卡,发卡被拒" if g3 is not None and g4 is None and d4 and "quota-exceeded" in d4
                    else f"异常 g3={'ok' if g3 else 'deny'} g4={d4 or 'ok'}")
        _record("C7 发卡日限额（滑动 24h 第 4 张拒发）", "发卡,发卡被拒", outcome7,
                f"cap={daily_cap}/日 账本={issuance_quota.name}")
    finally:
        for rt in runtimes:
            await rt.stop_serving()
        shutil.rmtree(tmp, ignore_errors=True)

    _summary(out)


def _manifests_ws(tmp: Path, port: int, manifests) -> Path:
    ws = tmp / f"ws-{port}"
    shutil.copytree(GPU_WS / "acps", ws / "acps")
    domains = ws / "acps" / "domains"
    domains.mkdir(parents=True, exist_ok=True)
    for m in manifests:
        write_manifest(m, domains)
    return ws


async def _local_serve_with(ws: Path, port: int, audit: Path):
    import os

    os.environ["ACPS_PEP_ENABLED"] = "1"
    os.environ["ACPS_PEP_LEADER_AICS"] = ""  # 商业姿态：白名单关闭
    os.environ["ACPS_PEP_DOMAINS_DIR"] = str(ws / "acps" / "domains")
    os.environ["ACPS_PEP_QUOTA_PATH"] = str(ws / "acps" / "grant-quota.sqlite3")
    audit.parent.mkdir(parents=True, exist_ok=True)
    audit.write_text("", encoding="utf-8")
    os.environ["ACPS_PEP_AUDIT_PATH"] = str(audit)
    from wit import WitRuntime

    runtime = WitRuntime(partner_gpu.build_gpu_agent(None), workspace=ws, acps=_acps_config(port))
    status = await runtime.serve_direct_partner(host="127.0.0.1", port=port, amp=False)
    print(f"[commerce] :{port} → {status.status}")
    return runtime


def _summary(out: Path) -> None:
    print("\n══════════════════════ 商业域（C 系列）结果 ══════════════════════")
    def _ok(row):
        e, g = row["expected"], row["outcome"]
        if "签名方" in e:
            return g.startswith(e)
        if "发卡" in e:
            return g == e
        want_a, want_r = e.count("accepted"), e.count("rejected")
        if "后" in e:
            parts = g.split(" 后 ")
            return len(parts) == 2 and "rejected" in parts[0] and "accepted" in parts[1]
        return (g.count("accepted"), g.count("rejected")) == (want_a, want_r) or g == e
    passed = 0
    for row in ROWS:
        good = _ok(row)
        passed += good
        print(f"{'✅' if good else '❌'} {row['scenario']}")
        print(f"    期望 {row['expected']} / 实际 {row['outcome']}")
        if row["note"]:
            print(f"    备注 {row['note'][:140]}")
    print(f"\n[commerce] 通过 {passed}/{len(ROWS)}；产物在 {out}")


if __name__ == "__main__":
    asyncio.run(main())
