"""域硬化回归（2026-10-20）：修订连续性、防回滚、派生绑定/PEP 复核。

覆盖三组此前缺口：
1. 章程改动必须由上一版签署方连署；坏的高 revision 只隔离、不毒化；删除冻结
   章程不会回落旧 active（签名副本 + 高水位）。
2. 域技能不能凭"无 acps_domain 的普通委托令牌"进入；派生令牌必须带父
   jti/exp/max_uses，PEP 复核 exp ≤ 父并继续扣父配额。
3. 访问控制以**已发布章程**为准；代码 visibility 声明用于发现层与域令牌目标域
   约束，不凭空造域（避免把尚未建章程的 scout/hpc 能力锁死）。

    python -m unittest acps_cluster.tests.test_domain_hardening -v
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

CLUSTER_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = CLUSTER_DIR.parent
for _p in (str(PROJECT_DIR), str(CLUSTER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from wit.hooks.acps.admission import AipPartnerAdmissionRequest  # noqa: E402

from acps_cluster.delegation import grant as g  # noqa: E402
from acps_cluster.delegation import token as tok  # noqa: E402
from acps_cluster.delegation.domain_manifest import (  # noqa: E402
    _archive_dir,
    build_manifest,
    load_domain_policies,
    sign_manifest,
    to_policy,
    verify_manifest,
    verify_revision_continuity,
    write_manifest,
)
from acps_cluster.delegation.pep import PepConfig, evaluate  # noqa: E402

LEADER_AIC = "1.2.156.3088.1.1.ZV8ZLB.RJ5XH2.GOA7Q6.1672"
GPU_AIC = "1.2.156.3088.1.1.GP5060.SANDBOX1.RUN01.0IGR"
CRITIC_AIC = "1.2.156.3088.1.1.BJ5X2N.QYTBJJ.6GH1CI.12KL"
EXPERIMENT_AIC = "1.2.156.3088.1.1.EXPRMT.LABEXE01.RUN01.1207"

SUPERVISOR_CLIENT = CLUSTER_DIR / "ws-supervisor" / "acps" / "client"
GPU_CLIENT = CLUSTER_DIR / "ws-gpu" / "acps" / "client"
CRITIC_CLIENT = CLUSTER_DIR / "ws-critic" / "acps" / "client"
EXP_CLIENT = CLUSTER_DIR / "ws-experiment" / "acps" / "client"
TRUST = CLUSTER_DIR / "ws-gpu" / "acps" / "trust-bundle.pem"

DOMAIN = "hardening-domain"
GPU_SKILL = "gpu.run"


def _signing(workspace_client: Path):
    """加载本地 dev CA 私钥；干净克隆不带私钥，返回 None 由 setUpModule 统一跳过。

    2026-10-21 审查：原实现在模块导入期直接读 `private-key.pem`，干净 checkout
    （git archive）会得到 53 个 collection error——比 skip 更糟，连其余 600 多项都跑不了。
    """
    try:
        if not (workspace_client / "private-key.pem").is_file():
            return None
        return tok.load_signing_context(workspace_client, trust_bundle=TRUST)
    except Exception:  # noqa: BLE001 — 缺 trust-bundle/密钥都按"环境不具备"处理
        return None


LEADER = _signing(SUPERVISOR_CLIENT)
GPU = _signing(GPU_CLIENT)
CRITIC = _signing(CRITIC_CLIENT)
EXPERIMENT = _signing(EXP_CLIENT)
_MISSING_SIGNING = [
    name
    for name, ctx in (
        ("ws-supervisor", LEADER),
        ("ws-gpu", GPU),
        ("ws-critic", CRITIC),
        ("ws-experiment", EXPERIMENT),
    )
    if ctx is None
]


def setUpModule() -> None:
    if _MISSING_SIGNING:
        raise unittest.SkipTest(
            "缺少本地 dev CA 私钥/trust-bundle（干净克隆不携带）："
            + ", ".join(_MISSING_SIGNING)
            + "。先按 docs/runbook-add-new-agent.md 铸造身份再跑本模块。"
        )


def _manifest(
    revision: int,
    *,
    status: str = "active",
    previous: dict | None = None,
    extra_members: tuple[str, ...] = (),
) -> dict:
    members = [
        {"aic": GPU_AIC, "role": "provider"},
        {"aic": LEADER_AIC, "role": "consumer"},
        {"aic": EXPERIMENT_AIC, "role": "consumer"},
    ]
    for aic in extra_members:
        if aic not in {m["aic"] for m in members}:
            members.append({"aic": aic, "role": "consumer"})
    return build_manifest(
        domain_id=DOMAIN,
        revision=revision,
        status=status,
        members=members,
        assets=[{"providerAic": GPU_AIC, "skill": GPU_SKILL, "allowedMembers": ["consumer"]}],
        issuers=[LEADER_AIC],
        previous=previous,
    )


def _signed(manifest: dict, *signings) -> dict:
    for signing in signings:
        manifest = sign_manifest(manifest, signing)
    return manifest


def _dual(manifest: dict) -> dict:
    return _signed(manifest, LEADER, GPU)


def _request(caller_aic: str, *, skill: str = GPU_SKILL, token: str | None = None):
    data_items = [{"type": "text", "text": "取材料", "metadata": {"skillId": skill}}]
    if token:
        data_items.append(tok.token_data_item(token, target_aic=GPU_AIC, skill_id=skill))
    command = {
        "type": "task-command", "id": "m1", "sentAt": "2026-10-20T00:00:00Z",
        "senderRole": "leader", "senderId": caller_aic, "sessionId": "session-hardening",
        "taskId": "task-hardening", "command": "start", "commandParams": {},
        "dataItems": data_items,
    }
    return AipPartnerAdmissionRequest.from_command(
        command, leader_aic=caller_aic, partner_aic=GPU_AIC,
        delivery="rpc", peer_verified=True,
    )


class CharterMutationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hardening-"))
        self.domains = self.tmp / "domains"
        self.domains.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _load(self):
        return load_domain_policies(self.domains, trust_bundle=TRUST)[DOMAIN]

    def test_缺少上一版签署方的修订被隔离(self):
        r1 = _dual(_manifest(1))
        write_manifest(r1, self.domains)
        self.assertEqual(self._load().revision, 1)

        rogue = _signed(
            _manifest(2, previous=r1, extra_members=(CRITIC_AIC, EXPERIMENT_AIC)),
            CRITIC, EXPERIMENT,
        )
        write_manifest(rogue, self.domains)
        policy = self._load()
        self.assertEqual((policy.revision, policy.status), (1, "active"))
        with self.assertRaises(tok.TokenError) as ctx:
            verify_revision_continuity(
                verify_manifest(rogue, trust_bundle=TRUST)[0],
                verify_manifest(rogue, trust_bundle=TRUST)[1],
                verify_manifest(r1, trust_bundle=TRUST)[0],
                verify_manifest(r1, trust_bundle=TRUST)[1],
            )
        self.assertEqual(ctx.exception.reason, "manifest-continuity-signers")

    def test_上一版签署方连署的修订生效(self):
        r1 = _dual(_manifest(1))
        write_manifest(r1, self.domains)
        r2 = _dual(_manifest(2, previous=r1))
        write_manifest(r2, self.domains)
        self.assertEqual(self._load().revision, 2)

    def test_坏的高revision不再毒化域(self):
        r1 = _dual(_manifest(1))
        write_manifest(r1, self.domains)
        bad = _manifest(999)  # 未签名
        (self.domains / f"{DOMAIN}.r999.json").write_text(__import__("json").dumps(bad), encoding="utf-8")
        policy = self._load()
        self.assertEqual((policy.revision, policy.status), (1, "active"))

    def test_删除冻结主文件仍保持冻结(self):
        r1 = _dual(_manifest(1))
        r2 = _dual(_manifest(2, status="frozen", previous=r1))
        write_manifest(r1, self.domains)
        write_manifest(r2, self.domains)
        self.assertEqual(self._load().status, "frozen")

        (self.domains / f"{DOMAIN}.r2.json").unlink()
        self.assertEqual(self._load().status, "frozen")

        # 连签名副本目录一起删掉：高水位状态仍应挡住回落
        shutil.rmtree(_archive_dir(self.domains), ignore_errors=True)
        self.assertEqual(self._load().status, "frozen")

    def test_高水位之后的新修订仍可正常解冻(self):
        r1 = _dual(_manifest(1))
        r2 = _dual(_manifest(2, status="frozen", previous=r1))
        r3 = _dual(_manifest(3, status="active", previous=r2))
        write_manifest(r1, self.domains)
        write_manifest(r2, self.domains)
        self.assertEqual(self._load().status, "frozen")  # 先把 r2 写进高水位
        write_manifest(r3, self.domains)
        self.assertEqual(self._load().status, "active")


class DerivedBindingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hardening-pep-"))
        body, signers = verify_manifest(_dual(_manifest(1)), trust_bundle=TRUST)
        self.policy = to_policy(body, signers)
        self.quota = self.tmp / "quota.sqlite3"
        self.cfg = PepConfig(
            enabled=True, my_aic=GPU_AIC,
            leader_aics=frozenset({LEADER_AIC}),
            trust_bundle=TRUST,
            audit_path=self.tmp / "audit.jsonl",
            public_skills=frozenset(), has_non_public_skills=True,
            domains={DOMAIN: self.policy}, quota_path=self.quota,
            # 与 kb 同类：代码声明会员制 + 章程资产命中 → 普通令牌必须被拒
            domain_skills={"gpu.run": frozenset({DOMAIN})},
        )
        self.sc = LEADER

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _grant(self, *, max_uses: int = 5, ttl: int = 3600, subject: str = LEADER_AIC):
        return g.issue_grant(
            self.sc, domain=DOMAIN, domain_revision=1, subject_aic=subject,
            assets=[{"providerAic": GPU_AIC, "skill": GPU_SKILL}],
            max_uses=max_uses, ttl_seconds=ttl,
        )

    def _child(self, parent, *, ttl: int = 300):
        return tok.issue_token(
            self.sc, delegate_aic=EXPERIMENT_AIC, target_aic=GPU_AIC,
            skill_id=GPU_SKILL, ttl_seconds=ttl,
            extra_claims=g.derive_delegation_claims(
                parent.claims, skill_id=GPU_SKILL, parent_token=parent.token,
            ),
        )

    def test_普通令牌不能调用域技能(self):
        plain = tok.issue_token(
            self.sc, delegate_aic=EXPERIMENT_AIC, target_aic=GPU_AIC, skill_id=GPU_SKILL,
        )
        outcome = evaluate(_request(EXPERIMENT_AIC, token=plain.token), self.cfg)
        self.assertEqual((outcome.decision, outcome.rule), ("deny", "domain-required"))

    def test_派生令牌继承父绑定并继续扣父配额(self):
        parent = self._grant(max_uses=1)
        child1 = self._child(parent)
        self.assertEqual(child1.claims["acps_parent_jti"], parent.jti)
        self.assertEqual(child1.claims["acps_parent_exp"], parent.expires_at)
        self.assertEqual(child1.claims["acps_parent_max_uses"], 1)
        first = evaluate(_request(EXPERIMENT_AIC, token=child1.token), self.cfg)
        self.assertEqual((first.decision, first.rule), ("allow", "domain-delegation"))
        self.assertEqual(first.parent_quota_remaining, 0)
        second = evaluate(_request(EXPERIMENT_AIC, token=self._child(parent).token), self.cfg)
        self.assertEqual((second.decision, second.rule), ("deny", "parent-quota-exhausted"))

    def test_伪造父jti与配额没有密码学锚点被拒(self):
        """复现审查 PoC：拿到 leader 私钥后自报 parent_jti/max_uses，连续伪造都拒绝。"""
        parent = self._grant(max_uses=1)
        template = self._child(parent).claims
        for index in range(3):
            claims = dict(template)
            claims.pop("acps_parent_grant", None)
            claims["jti"] = f"forged-parent-{index}-{uuid.uuid4()}"
            claims["acps_parent_jti"] = "attacker-fake-parent"
            claims["acps_parent_max_uses"] = 10**9
            forged = tok.sign_payload(self.sc, claims)
            outcome = evaluate(_request(EXPERIMENT_AIC, token=forged), self.cfg)
            self.assertEqual(
                (outcome.decision, outcome.rule), ("deny", "invalid-parent-binding")
            )

    def test_真实父proof不能配合伪造配额(self):
        parent = self._grant(max_uses=1)
        claims = dict(self._child(parent).claims)
        claims["jti"] = f"forged-quota-{uuid.uuid4()}"
        claims["acps_parent_grant"] = parent.token  # 真 proof
        claims["acps_parent_jti"] = parent.jti
        claims["acps_parent_exp"] = parent.expires_at
        claims["acps_parent_max_uses"] = 10**9     # 假配额
        forged = tok.sign_payload(self.sc, claims)
        outcome = evaluate(_request(EXPERIMENT_AIC, token=forged), self.cfg)
        self.assertEqual((outcome.decision, outcome.rule), ("deny", "invalid-parent-binding"))

    def test_父grant主体必须等于子令牌签发者(self):
        wrong_holder = g.issue_grant(
            self.sc, domain=DOMAIN, domain_revision=1, subject_aic=EXPERIMENT_AIC,
            assets=[{"providerAic": GPU_AIC, "skill": GPU_SKILL}],
            max_uses=5, ttl_seconds=3600,
        )
        child = self._child(wrong_holder)
        outcome = evaluate(_request(EXPERIMENT_AIC, token=child.token), self.cfg)
        self.assertEqual((outcome.decision, outcome.rule), ("deny", "invalid-parent-binding"))

    def test_父grant签发者必须在域issuers(self):
        rogue_parent = g.issue_grant(
            GPU, domain=DOMAIN, domain_revision=1, subject_aic=LEADER_AIC,
            assets=[{"providerAic": GPU_AIC, "skill": GPU_SKILL}],
            max_uses=5, ttl_seconds=3600,
        )
        child = self._child(rogue_parent)
        outcome = evaluate(_request(EXPERIMENT_AIC, token=child.token), self.cfg)
        self.assertEqual((outcome.decision, outcome.rule), ("deny", "parent-issuer-not-in-domain"))

    def test_域令牌不得绕开父grant签发(self):
        with self.assertRaises(tok.TokenError) as ctx:
            tok.issue_token(
                self.sc, delegate_aic=EXPERIMENT_AIC, target_aic=GPU_AIC,
                skill_id=GPU_SKILL,
                extra_claims={"acps_domain": DOMAIN, "acps_domain_revision": 1},
            )
        self.assertEqual(ctx.exception.reason, "domain-token-requires-parent")

    def test_伪造的域令牌缺父绑定被PEP拒(self):
        now = int(time.time())
        claims = {
            "iss": f"agent:{LEADER_AIC}", "sub": f"agent:{LEADER_AIC}",
            "aud": tok.canonical_audience(GPU_AIC), "exp": now + 300, "iat": now,
            "jti": "forged-domain", "scope": f"acps.skill.invoke:{GPU_SKILL}",
            "act": {"sub": f"agent:{EXPERIMENT_AIC}", "aic": EXPERIMENT_AIC,
                    "act": {"sub": f"agent:{LEADER_AIC}", "aic": LEADER_AIC}},
            "acps_subject_type": "agent", "acps_target_aic": GPU_AIC,
            "acps_skill_id": GPU_SKILL, "acps_delegation_id": "dlg-forged",
            "acps_delegation_mode": "fixed",
            "acps_allowed_route": [f"agent:{LEADER_AIC}", f"agent:{EXPERIMENT_AIC}", f"agent:{GPU_AIC}"],
            "acps_chain_depth": 1, "acps_max_chain_depth": 2,
            "acps_domain": DOMAIN, "acps_domain_revision": 1,
        }
        forged = tok.sign_payload(self.sc, claims)
        outcome = evaluate(_request(EXPERIMENT_AIC, token=forged), self.cfg)
        self.assertEqual((outcome.decision, outcome.rule), ("deny", "invalid-parent-binding"))

    def test_子令牌exp自动被父exp收紧(self):
        parent = self._grant(ttl=120)
        child = self._child(parent, ttl=600)
        self.assertLessEqual(child.expires_at, parent.expires_at)
        self.assertEqual(child.claims["acps_parent_exp"], parent.expires_at)

    def test_代码声明域但无本地章程不凭空造域(self):
        # 代码里的 visibility=domain 是发现层元数据；没有章程时若也硬拒普通令牌，
        # 尚未建域的 scout/hpc 能力会被直接锁死。访问控制以章程资产表为准。
        cfg = PepConfig(
            enabled=True, my_aic=GPU_AIC,
            leader_aics=frozenset({LEADER_AIC}),
            trust_bundle=TRUST,
            public_skills=frozenset(), has_non_public_skills=True,
            domains={},
            quota_path=self.quota,
            domain_skills={"gpu.run": frozenset({"some-declared-domain"})},
        )
        plain = tok.issue_token(
            self.sc, delegate_aic=EXPERIMENT_AIC, target_aic=GPU_AIC, skill_id=GPU_SKILL,
        )
        outcome = evaluate(_request(EXPERIMENT_AIC, token=plain.token), cfg)
        self.assertEqual((outcome.decision, outcome.rule), ("allow", "delegation-token"))

    def test_域令牌目标域与代码声明不符被拒(self):
        # 代码声明 gpu.run 只属于 other-domain；令牌却声称 DOMAIN（章程也收录了
        # 该资产）——两把锁冲突时 fail-closed。
        policy = self.policy
        cfg = PepConfig(
            enabled=True, my_aic=GPU_AIC,
            leader_aics=frozenset({LEADER_AIC}),
            trust_bundle=TRUST,
            public_skills=frozenset(), has_non_public_skills=True,
            domains={DOMAIN: policy}, quota_path=self.quota,
            domain_skills={"gpu.run": frozenset({"other-domain"})},
        )
        parent = self._grant()
        child = self._child(parent)
        outcome = evaluate(_request(EXPERIMENT_AIC, token=child.token), cfg)
        self.assertEqual((outcome.decision, outcome.rule), ("deny", "domain-skill-mismatch"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
