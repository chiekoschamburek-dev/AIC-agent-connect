"""实验② 持续协作域——热变更对照实验（证明"不是静态配置"）。

核心命题：同一枚 grant 凭证，在运行中改变域章程状态后，下一次准入裁决
立即翻转——冻结即急停、成员移除即撤销、配额用尽即拒、到期即失效。

对照：不启用协作域（无域章程约束，grant 从不校验） vs 启用协作域。
逐项控制变量：唯一变量是"域策略视图 / 章程状态"，令牌与其它请求完全相同。

全部用临时自造 CA + 真实双签章程 + 真实 grant，复刻 test_domain.py 的全链，
不依赖仓库 dev CA 私钥，可复现。输出 out/exp2_domain.json。
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "acps_cluster"))

from ablation_common import Forge  # noqa: E402

from acps_cluster.delegation import grant as g  # noqa: E402
from acps_cluster.delegation import token as tok  # noqa: E402
from acps_cluster.delegation.domain_manifest import (  # noqa: E402
    build_manifest, sign_manifest, verify_manifest, to_policy,
)
from acps_cluster.delegation.pep import PepConfig, evaluate  # noqa: E402
from wit.hooks.acps.admission import AipPartnerAdmissionRequest  # noqa: E402

DOMAIN = "lab-x-y"
GPU_SKILL = "gpu.run"
DATA_SKILL = "dataset.fetch"

LEADER = "leader.aic"       # 域签发者 + auditor
GPU = "gpu.aic"             # provider
SCOUT = "scout.aic"         # provider
EXPERIMENT = "experiment.aic"  # consumer（合法持有者）
CRITIC = "critic.aic"       # 域外第三方


def request(caller_aic: str, *, skill: str | None = GPU_SKILL, token_str: str | None = None,
            session_id: str = "session-domain"):
    data_items = []
    if skill:
        data_items.append({"type": "text", "text": "取材料", "metadata": {"skillId": skill}})
    if token_str:
        data_items.append(tok.token_data_item(token_str, target_aic=GPU, skill_id=skill or GPU_SKILL))
    command = {
        "type": "task-command", "id": "m1",
        "sentAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "senderRole": "leader", "senderId": caller_aic, "sessionId": session_id,
        "taskId": "task-d1", "command": "start", "commandParams": {}, "dataItems": data_items,
    }
    return AipPartnerAdmissionRequest.from_command(
        command, leader_aic=caller_aic, partner_aic=GPU, delivery="rpc", peer_verified=True,
    )


def outcome_of(res) -> str:
    return f"{res.decision}/{res.rule}"


def main() -> int:
    (HERE / "out").mkdir(exist_ok=True)
    forge = Forge()
    leader = forge.leaf("leader", aic=LEADER)
    gpu = forge.leaf("gpu", aic=GPU)
    bundle = forge.trust_bundle

    members_active = [
        {"aic": GPU, "role": "provider"},
        {"aic": SCOUT, "role": "provider"},
        {"aic": EXPERIMENT, "role": "consumer"},
        {"aic": LEADER, "role": "auditor"},
    ]

    def dual_signed(*, status="active", revision=1, members=None, valid_until=None):
        m = build_manifest(
            domain_id=DOMAIN, members=members or members_active,
            assets=[
                {"providerAic": GPU, "skill": GPU_SKILL, "allowedMembers": ["consumer"]},
                {"providerAic": SCOUT, "skill": DATA_SKILL, "allowedMembers": ["consumer"]},
            ],
            issuers=[LEADER], revision=revision, status=status, valid_until=valid_until,
        )
        return sign_manifest(sign_manifest(m, leader), gpu)

    # 初始 active 章程 → 域策略视图
    body, signers = verify_manifest(dual_signed(), trust_bundle=bundle)
    policy_active = to_policy(body, signers)
    tmp = Path(tempfile.mkdtemp(prefix="ablation-domain-"))
    quota_path = tmp / "quota.sqlite3"

    base_cfg = dict(
        enabled=True, my_aic=GPU, leader_aics=frozenset({LEADER}),
        trust_bundle=bundle, audit_path=tmp / "audit.jsonl",
        public_skills=frozenset(), has_non_public_skills=True,
        quota_path=quota_path,
    )
    cfg_active = PepConfig(domains={DOMAIN: policy_active}, **base_cfg)
    # 对照：关闭整个 PEP 治理层（官方 SDK 默认行为 = 直接 accept）
    cfg_off = replace(cfg_active, enabled=False)

    def grant_token(*, subject=EXPERIMENT, max_uses=3, ttl=3600, issuer=None):
        return g.issue_grant(
            issuer or leader, domain=DOMAIN, domain_revision=1, subject_aic=subject,
            assets=[{"providerAic": GPU, "skill": GPU_SKILL},
                    {"providerAic": SCOUT, "skill": DATA_SKILL}],
            max_uses=max_uses, ttl_seconds=ttl,
        ).token

    results = []

    # ── 1. 基线：域内合法 grant 放行 ──
    tok_valid = grant_token()
    from acps_cluster.delegation.pep import pep_decide
    base_domain = outcome_of(evaluate(request(EXPERIMENT, token_str=tok_valid), cfg_active))
    # 官方默认：PEP 关闭 → 同一请求一律 accept（实测 pep_decide 的 enabled=False 回退）
    ctrl_check = pep_decide(request(EXPERIMENT, token_str=tok_valid), cfg_off)
    CTRL = f"accept（官方默认·实测 {str(ctrl_check.state).lower()}）"
    results.append({
        "item": "域内合法 grant（consumer 调 gpu.run）",
        "change": "无（基线）",
        "with_domain": base_domain, "without_domain": CTRL,
        "expected": "allow",
    })

    # ── 2. 冻结：运行中把章程 status 改 frozen（同一枚 token）──
    cfg_frozen = replace(cfg_active, domains={DOMAIN: replace(policy_active, status="frozen")})
    frz = outcome_of(evaluate(request(EXPERIMENT, token_str=tok_valid), cfg_frozen))
    results.append({
        "item": "冻结协作域（章程 active→frozen）",
        "change": "运行中改 status=frozen，token 不变",
        "with_domain": frz, "without_domain": CTRL,
        "expected": "deny/domain-frozen",
    })

    # ── 3. 撤销：revision=2 把 consumer 移出成员表（同一枚 r1 token）──
    v2 = dual_signed(revision=2, members=[
        {"aic": GPU, "role": "provider"},
        {"aic": SCOUT, "role": "provider"},
        {"aic": LEADER, "role": "auditor"},
    ])
    body2, signers2 = verify_manifest(v2, trust_bundle=bundle)
    cfg_v2 = replace(cfg_active, domains={DOMAIN: to_policy(body2, signers2)})
    revoked = outcome_of(evaluate(request(EXPERIMENT, token_str=tok_valid), cfg_v2))
    results.append({
        "item": "撤销成员资格（r1→r2 移除 consumer）",
        "change": "运行中升 revision 移除成员，token 不变",
        "with_domain": revoked, "without_domain": CTRL,
        "expected": "deny/grant-not-member",
    })

    # ── 4. 配额耗尽：max_uses=2，第 3 次调用被拒 ──
    qtoken = grant_token(max_uses=2)
    qpath = tmp / "quota2.sqlite3"
    cfg_q = replace(cfg_active, quota_path=qpath)
    seq = [evaluate(request(EXPERIMENT, token_str=qtoken), cfg_q) for _ in range(3)]
    quota_seq = [outcome_of(r) for r in seq]
    results.append({
        "item": "配额用尽（max_uses=2，连调 3 次）",
        "change": "配额计数器实时扣减",
        "with_domain": " → ".join(quota_seq), "without_domain": CTRL,
        "expected": "allow, allow, deny/quota-exhausted",
    })

    # ── 5. 到期：并发一枚短 TTL grant，用 now 前移模拟到期 ──
    exp_token = grant_token(ttl=60)
    now_future = time.time() + 3600
    expired = outcome_of(evaluate(request(EXPERIMENT, token_str=exp_token), cfg_active, now=now_future))
    results.append({
        "item": "grant 到期失效（TTL=60s，now+3600s）",
        "change": "时间推进到有效期外",
        "with_domain": expired, "without_domain": CTRL,
        "expected": "deny/"  # verify_token 把 expired 归入 invalid-token,
    })

    # ── 6. 越权：域外第三方盗用 grant ──
    stolen = outcome_of(evaluate(request(CRITIC, token_str=tok_valid), cfg_active))
    results.append({
        "item": "域外盗用（critic 持 experiment 的 grant）",
        "change": "调用方换成非成员",
        "with_domain": stolen, "without_domain": CTRL,
        "expected": "deny/grant-subject-mismatch",
    })

    # ── 7. 签发者越权：provider(gpu) 签 grant（不在 issuers）──
    rogue = outcome_of(evaluate(
        request(EXPERIMENT, token_str=grant_token(issuer=gpu)), cfg_active))
    results.append({
        "item": "非授权签发者（gpu 签 grant，issuers 仅 leader）",
        "change": "签发者换成域内 provider",
        "with_domain": rogue, "without_domain": CTRL,
        "expected": "deny/issuer-not-in-domain",
    })

    # ── 8. 未授权 skill：不持域凭证直连受域治理的 gpu.run ──
    no_token = outcome_of(evaluate(request(EXPERIMENT, token_str=None), cfg_active))
    results.append({
        "item": "无域凭证直连受治理技能",
        "change": "去掉 grant",
        "with_domain": no_token, "without_domain": CTRL,
        "expected": "deny/no-credential",
    })

    # 逐项判定：with_domain 是否命中 expected
    def hit(r):
        exp = r["expected"]
        if exp.startswith("allow, allow"):
            return r["with_domain"] == "allow/domain-grant → allow/domain-grant → deny/quota-exhausted"
        return r["with_domain"].startswith(exp)

    hits = sum(1 for r in results if hit(r))
    payload = {
        "experiment": "② 持续协作域热变更对照",
        "control_variable": "域策略视图/章程状态（对照=关闭 PEP 治理层，官方默认 accept）",
        "claim": "同一枚凭证，章程状态运行中改变后下一次裁决立即翻转——证明非静态配置",
        "total": len(results), "matched": hits,
        "cases": results,
    }
    out = HERE / "out" / "exp2_domain.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 84)
    print("实验② 持续协作域热变更（证明非静态配置）")
    print("=" * 84)
    print(f"{'变更项':<36}{'启用协作域':<40}{'官方默认对照'}")
    print("-" * 84)
    for r in results:
        mark = "OK" if hit(r) else "!!"
        print(f"[{mark}] {r['item'][:34]:<36}{r['with_domain'][:38]:<40}{r['without_domain'][:20]}")
        print(f"      变更：{r['change']}  | 期望 {r['expected']}")
    print("-" * 84)
    print(f"命中 {hits}/{len(results)}；明细：{out}")
    return 0 if hits == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
