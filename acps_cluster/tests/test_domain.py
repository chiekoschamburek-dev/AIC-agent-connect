"""域级（CDAP 瘦身版）离线单测：章程、GrantToken、PEP 域路径、配额、对账。

覆盖第九轮三条审查修正的回归：
① 域状态穿透——派生 token 继承域 + 冻结在验签前拒绝（test_冻结杀死派生链 等）；
② 成员/角色裁决——D3a 主体绑定 / D3b 成员表；
③ revision——高版本胜出 + 旧成员被移除后按新章程拒绝。

    python -m unittest acps_cluster.tests.test_domain -v
"""

import sys
import tempfile
import time
import unittest
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
    build_manifest, load_domain_policies, sign_manifest, to_policy, verify_manifest, write_manifest,
)
from acps_cluster.delegation.pep import PepConfig, evaluate  # noqa: E402
from acps_cluster.delegation.quota import QuotaStore  # noqa: E402
from acps_cluster.delegation.audit import NO_DOMAIN, NO_SKILL  # noqa: E402
from acps_cluster.delegation.reconciliation import build_report, sign_report, verify_report  # noqa: E402
from acps_cluster.delegation.token import issue_token, load_signing_context  # noqa: E402

LEADER_AIC = "1.2.156.3088.1.1.ZV8ZLB.RJ5XH2.GOA7Q6.1672"
GPU_AIC = "1.2.156.3088.1.1.GP5060.SANDBOX1.RUN01.0IGR"
SCOUT_AIC = "1.2.156.3088.1.1.F3BHZ2.2RKLSI.OCPRRD.0PMW"
CRITIC_AIC = "1.2.156.3088.1.1.BJ5X2N.QYTBJJ.6GH1CI.12KL"
EXPERIMENT_AIC = "1.2.156.3088.1.1.EXPRMT.LABEXE01.RUN01.1207"

SUPERVISOR_CLIENT = CLUSTER_DIR / "ws-supervisor" / "acps" / "client"
GPU_CLIENT = CLUSTER_DIR / "ws-gpu" / "acps" / "client"
CRITIC_CLIENT = CLUSTER_DIR / "ws-critic" / "acps" / "client"
TRUST_BUNDLE = CLUSTER_DIR / "ws-gpu" / "acps" / "trust-bundle.pem"

GPU_SKILL = "gpu.run"
DATA_SKILL = "dataset.fetch"


def _leader_signing():
    return load_signing_context(SUPERVISOR_CLIENT, trust_bundle=TRUST_BUNDLE)


def _gpu_signing():
    return load_signing_context(GPU_CLIENT, trust_bundle=TRUST_BUNDLE)


def _domain_manifest(*, status="active", revision=1, members=None):
    return build_manifest(
        domain_id="lab-x-y",
        members=members
        or [
            {"aic": GPU_AIC, "role": "provider"},
            {"aic": SCOUT_AIC, "role": "provider"},
            {"aic": EXPERIMENT_AIC, "role": "consumer"},
            {"aic": LEADER_AIC, "role": "auditor"},
        ],
        assets=[
            {"providerAic": GPU_AIC, "skill": GPU_SKILL, "allowedMembers": ["consumer"]},
            {"providerAic": SCOUT_AIC, "skill": DATA_SKILL, "allowedMembers": ["consumer"]},
        ],
        issuers=[LEADER_AIC],
        revision=revision,
        status=status,
    )


def _dual_signed(*, status="active", revision=1, members=None):
    manifest = _domain_manifest(status=status, revision=revision, members=members)
    manifest = sign_manifest(manifest, _leader_signing())
    manifest = sign_manifest(manifest, _gpu_signing())
    return manifest


def _request(caller_aic, *, skill=GPU_SKILL, token=None, session_id="session-domain"):
    data_items = []
    if skill:
        data_items.append({"type": "text", "text": "取材料", "metadata": {"skillId": skill}})
    if token:
        item = tok.token_data_item(token, target_aic=GPU_AIC, skill_id=skill or GPU_SKILL)
        data_items.append(item)
    command = {
        "type": "task-command", "id": "m1", "sentAt": "2026-10-02T00:00:00Z",
        "senderRole": "leader", "senderId": caller_aic, "sessionId": session_id,
        "taskId": "task-d1", "command": "start", "commandParams": {}, "dataItems": data_items,
    }
    return AipPartnerAdmissionRequest.from_command(
        command, leader_aic=caller_aic, partner_aic=GPU_AIC, delivery="rpc", peer_verified=True,
    )


class DomainManifestTest(unittest.TestCase):
    def test_双签章程验证通过(self):
        body, signers = verify_manifest(_dual_signed(), trust_bundle=TRUST_BUNDLE)
        self.assertEqual(body["domainId"], "lab-x-y")
        self.assertEqual(signers, frozenset({LEADER_AIC, GPU_AIC}))

    def test_单签不足法定人数(self):
        manifest = sign_manifest(_domain_manifest(), _leader_signing())
        with self.assertRaises(tok.TokenError) as ctx:
            verify_manifest(manifest, trust_bundle=TRUST_BUNDLE)
        self.assertEqual(ctx.exception.reason, "manifest-undercount-signed")

    def test_签名后篡改正文被拒(self):
        manifest = _dual_signed()
        manifest["status"] = "frozen"  # 只改正文不动签名
        with self.assertRaises(tok.TokenError) as ctx:
            verify_manifest(manifest, trust_bundle=TRUST_BUNDLE)
        self.assertEqual(ctx.exception.reason, "manifest-content-mismatch")

    def test_加载取最高revision(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_manifest(_dual_signed(revision=1), root)
            write_manifest(_dual_signed(revision=3), root)
            write_manifest(_dual_signed(revision=2), root)
            policies = load_domain_policies(root, trust_bundle=TRUST_BUNDLE)
            self.assertEqual(policies["lab-x-y"].revision, 3)

    def test_验证不过的章程不加载(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_manifest(_domain_manifest(), root)  # 未签名
            policies = load_domain_policies(root, trust_bundle=TRUST_BUNDLE)
            # 2026-10-06 审查 #3:最高 revision 验不过 → 毒化为 frozen(不回落旧 active)
            self.assertEqual(policies["lab-x-y"].status, "frozen")


class GrantTokenTest(unittest.TestCase):
    def setUp(self):
        self.signing = _leader_signing()
        self.manifest = _dual_signed()

    def test_签发验证通过且claims齐备(self):
        issued = g.issue_grant(
            self.signing, domain="lab-x-y", domain_revision=1, subject_aic=EXPERIMENT_AIC,
            assets=[{"providerAic": GPU_AIC, "skill": GPU_SKILL},
                    {"providerAic": SCOUT_AIC, "skill": DATA_SKILL}],
            max_uses=5, ttl_seconds=3600,
        )
        claims = g.verify_grant(issued.token, trust_bundle=TRUST_BUNDLE)
        self.assertEqual(claims["acps_domain"], "lab-x-y")
        self.assertEqual(tok.normalize_audience(claims["aud"]), EXPERIMENT_AIC)
        self.assertEqual(len(claims["acps_assets"]), 2)
        self.assertTrue(issued.handle()["jti"])
        self.assertNotIn(issued.token, str(issued.handle()))

    def test_超长TTL被拒(self):
        with self.assertRaises(tok.TokenError):
            g.issue_grant(self.signing, domain="d", domain_revision=1, subject_aic=EXPERIMENT_AIC,
                          assets=[{"providerAic": GPU_AIC, "skill": GPU_SKILL}],
                          ttl_seconds=g.GRANT_MAX_TTL_SECONDS + 1)

    def test_派生继承校验_越界skill被拒(self):
        issued = g.issue_grant(
            self.signing, domain="lab-x-y", domain_revision=1, subject_aic=EXPERIMENT_AIC,
            assets=[{"providerAic": GPU_AIC, "skill": GPU_SKILL}], max_uses=5,
        )
        with self.assertRaises(tok.TokenError) as ctx:
            g.derive_delegation_claims(
                issued.claims, skill_id="other.skill", parent_token=issued.token,
            )
        self.assertEqual(ctx.exception.reason, "derive-out-of-scope")


class PepDomainPathTest(unittest.TestCase):
    """PEP 域路径全链：预检（冻结/未知域）→ grant 裁决（D3a/D3b/角色/配额）→ 派生链。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        body, signers = verify_manifest(_dual_signed(), trust_bundle=TRUST_BUNDLE)
        self.policy = to_policy(body, signers)
        self.quota_path = Path(self.tmp.name) / "quota.sqlite3"
        self.cfg = PepConfig(
            enabled=True, my_aic=GPU_AIC, leader_aics=frozenset({LEADER_AIC}),
            trust_bundle=TRUST_BUNDLE, audit_path=Path(self.tmp.name) / "audit.jsonl",
            public_skills=frozenset(), has_non_public_skills=True,
            domains={"lab-x-y": self.policy}, quota_path=self.quota_path,
        )
        self.signing = _leader_signing()

    def tearDown(self):
        self.tmp.cleanup()

    def _grant_token(self, *, subject=EXPERIMENT_AIC, max_uses=3, assets=None):
        issued = g.issue_grant(
            self.signing, domain="lab-x-y", domain_revision=1, subject_aic=subject,
            assets=assets or [{"providerAic": GPU_AIC, "skill": GPU_SKILL},
                              {"providerAic": SCOUT_AIC, "skill": DATA_SKILL}],
            max_uses=max_uses, ttl_seconds=3600,
        )
        return issued.token

    def test_D2_域内grant调用放行并记审计(self):
        outcome = evaluate(_request(EXPERIMENT_AIC, token=self._grant_token()), self.cfg)
        self.assertEqual((outcome.decision, outcome.rule), ("allow", "domain-grant"))
        self.assertEqual(outcome.domain, "lab-x-y")
        self.assertEqual(outcome.skill_hint, GPU_SKILL)

    def test_D2b_同枚grant可跨provider资产(self):
        # gpu 侧只裁 (GPU_AIC, gpu.run)；scout 侧同理（这里以 gpu 视角验证资产命中）
        outcome = evaluate(_request(EXPERIMENT_AIC, skill=GPU_SKILL, token=self._grant_token()), self.cfg)
        self.assertTrue(outcome.allowed)

    def test_D3a_域外盗用被主体绑定拒绝(self):
        outcome = evaluate(_request(CRITIC_AIC, token=self._grant_token()), self.cfg)
        self.assertEqual((outcome.decision, outcome.rule), ("deny", "grant-subject-mismatch"))

    def test_D3b_误发给非成员的grant被成员表拒绝(self):
        # issuer 失误：grant 签给了域外的 critic（签名有效、主体绑定也过）——成员表兜底
        token = self._grant_token(subject=CRITIC_AIC)
        outcome = evaluate(_request(CRITIC_AIC, token=token), self.cfg)
        self.assertEqual((outcome.decision, outcome.rule), ("deny", "grant-not-member"))

    def test_角色不被资产许可拒绝(self):
        # auditor 角色的成员拿到 grant（成员表过），但资产只允许 consumer。
        # 注意不能用 leader 测——leader 在 L1 白名单里，白名单先于 token 分支放行
        # （域冻结不剥夺 provider 自有白名单关系，这是第九轮声明的语义边界）。
        from dataclasses import replace

        members = [
            {"aic": GPU_AIC, "role": "provider"},
            {"aic": SCOUT_AIC, "role": "provider"},
            {"aic": EXPERIMENT_AIC, "role": "consumer"},
            {"aic": CRITIC_AIC, "role": "auditor"},
            {"aic": LEADER_AIC, "role": "auditor"},
        ]
        manifest = _dual_signed(members=members)
        body, signers = verify_manifest(manifest, trust_bundle=TRUST_BUNDLE)
        cfg = replace(self.cfg, domains={"lab-x-y": to_policy(body, signers)})
        token = self._grant_token(subject=CRITIC_AIC)  # critic：域内 auditor，非白名单
        outcome = evaluate(_request(CRITIC_AIC, token=token), cfg)
        self.assertEqual((outcome.decision, outcome.rule), ("deny", "grant-role-not-allowed"))

    def test_D4_冻结在验签前拒绝grant(self):
        frozen = dict(self.cfg.domains)
        from dataclasses import replace

        frozen_policy = replace(self.policy, status="frozen")
        frozen_cfg = replace(self.cfg, domains={"lab-x-y": frozen_policy})
        outcome = evaluate(_request(EXPERIMENT_AIC, token=self._grant_token()), frozen_cfg)
        self.assertEqual((outcome.decision, outcome.rule), ("deny", "domain-frozen"))
        self.assertIn("pre-signature", outcome.reason)

    def test_D4prime_冻结杀死已派生的task_token(self):
        """域状态穿透回归（第九轮审查①）：grant→派生链活跃时冻结，派生 token 立即死。"""
        grant = g.issue_grant(
            self.signing, domain="lab-x-y", domain_revision=1, subject_aic=LEADER_AIC,
            assets=[{"providerAic": GPU_AIC, "skill": GPU_SKILL}], max_uses=5, ttl_seconds=3600,
        )
        inherit = g.derive_delegation_claims(
            grant.claims, skill_id=GPU_SKILL, parent_token=grant.token,
        )
        derived = issue_token(
            self.signing, delegate_aic=EXPERIMENT_AIC, target_aic=GPU_AIC, skill_id=GPU_SKILL,
            ttl_seconds=300, not_after=grant.expires_at, extra_claims=inherit,
        )
        # 活跃时：派生 token 放行（domain-delegation）
        outcome = evaluate(_request(EXPERIMENT_AIC, token=derived.token), self.cfg)
        self.assertEqual((outcome.decision, outcome.rule), ("allow", "domain-delegation"))
        # 冻结后：同一枚派生 token 在验签前被拒
        from dataclasses import replace

        frozen_cfg = replace(self.cfg, domains={"lab-x-y": replace(self.policy, status="frozen")})
        outcome = evaluate(_request(EXPERIMENT_AIC, token=derived.token), frozen_cfg)
        self.assertEqual((outcome.decision, outcome.rule), ("deny", "domain-frozen"))

    def test_派生token的exp不超过父grant(self):
        grant = g.issue_grant(
            self.signing, domain="lab-x-y", domain_revision=1, subject_aic=LEADER_AIC,
            assets=[{"providerAic": GPU_AIC, "skill": GPU_SKILL}], max_uses=5, ttl_seconds=120,
        )
        inherit = g.derive_delegation_claims(
            grant.claims, skill_id=GPU_SKILL, parent_token=grant.token,
        )
        derived = issue_token(
            self.signing, delegate_aic=EXPERIMENT_AIC, target_aic=GPU_AIC, skill_id=GPU_SKILL,
            ttl_seconds=600, not_after=grant.expires_at, extra_claims=inherit,
        )
        self.assertLessEqual(derived.expires_at, grant.expires_at)

    def test_D5_配额用尽拒绝(self):
        token = self._grant_token(max_uses=2)
        self.assertTrue(evaluate(_request(EXPERIMENT_AIC, token=token), self.cfg).allowed)
        self.assertTrue(evaluate(_request(EXPERIMENT_AIC, token=token), self.cfg).allowed)
        outcome = evaluate(_request(EXPERIMENT_AIC, token=token), self.cfg)
        self.assertEqual((outcome.decision, outcome.rule), ("deny", "quota-exhausted"))

    def test_未知域拒绝(self):
        issued = g.issue_grant(
            self.signing, domain="no-such-domain", domain_revision=1, subject_aic=EXPERIMENT_AIC,
            assets=[{"providerAic": GPU_AIC, "skill": GPU_SKILL}], max_uses=1,
        )
        outcome = evaluate(_request(EXPERIMENT_AIC, token=issued.token), self.cfg)
        self.assertEqual((outcome.decision, outcome.rule), ("deny", "domain-unknown"))

    def test_grant签发者不在域issuers拒绝(self):
        rogue = g.issue_grant(
            _gpu_signing(),  # gpu 是 provider 成员但不在 issuers
            domain="lab-x-y", domain_revision=1, subject_aic=EXPERIMENT_AIC,
            assets=[{"providerAic": GPU_AIC, "skill": GPU_SKILL}], max_uses=1,
        )
        outcome = evaluate(_request(EXPERIMENT_AIC, token=rogue.token), self.cfg)
        self.assertEqual((outcome.decision, outcome.rule), ("deny", "issuer-not-in-domain"))

    def test_D8_revision_旧成员被移除后按新章程拒绝(self):
        # revision=2 移除 experiment —— 裁决按本地最高 revision，旧 grant（r1 签发）失效
        v2 = _dual_signed(
            revision=2,
            members=[
                {"aic": GPU_AIC, "role": "provider"},
                {"aic": SCOUT_AIC, "role": "provider"},
                {"aic": LEADER_AIC, "role": "auditor"},
            ],
        )
        body, signers = verify_manifest(v2, trust_bundle=TRUST_BUNDLE)
        cfg_v2 = PepConfig(**{**self.cfg.__dict__, "domains": {"lab-x-y": to_policy(body, signers)}})
        token = self._grant_token()  # r1 时代签给 experiment
        outcome = evaluate(_request(EXPERIMENT_AIC, token=token), cfg_v2)
        self.assertEqual((outcome.decision, outcome.rule), ("deny", "grant-not-member"))

    def test_无配额存储时grant路径fail_closed(self):
        from dataclasses import replace

        cfg = replace(self.cfg, quota_path=None)
        outcome = evaluate(_request(EXPERIMENT_AIC, token=self._grant_token()), cfg)
        self.assertEqual((outcome.decision, outcome.rule), ("deny", "pep-misconfigured"))


class QuotaStoreTest(unittest.TestCase):
    def test_持久性_重开存储计数保留(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "q.sqlite3"
            store = QuotaStore(path)
            try:
                # 配额 2：用满两次
                self.assertEqual(store.try_consume(jti="j", domain="d", caller_aic="a", max_uses=2), (True, 1))
                self.assertEqual(store.try_consume(jti="j", domain="d", caller_aic="a", max_uses=2), (True, 0))
            finally:
                store.close()
            reopened = QuotaStore(path)  # 模拟进程重启：计数必须保留（不能清零）
            try:
                self.assertEqual(reopened.try_consume(jti="j", domain="d", caller_aic="a", max_uses=2), (False, 0))
            finally:
                reopened.close()


class ReconciliationTest(unittest.TestCase):
    # 审计行的 ts 与 myAic 是报表口径的必需字段（2026-10-15 审查：fixture 此前既不写
    # ts 也不写 myAic，于是"窗口不生效""provider 不过滤"两个缺陷在单测里隐形）
    W0 = "2026-10-02T00:00:00Z"
    W1 = "2026-10-03T00:00:00Z"

    @staticmethod
    def _line(ts, *, decision="allow", rule="domain-grant", domain="lab-x-y",
              skill=GPU_SKILL, caller=EXPERIMENT_AIC, mine=GPU_AIC):
        return ('{"ts":"%s","decision":"%s","rule":"%s","callerAic":"%s",'
                '"domain":"%s","skillHint":"%s","myAic":"%s"}'
                % (ts, decision, rule, caller, domain, skill, mine))

    def test_报表聚合与双签验证(self):
        with tempfile.TemporaryDirectory() as td:
            audit = Path(td) / "audit.jsonl"
            lines = [self._line(f"2026-10-02T0{i}:00:00Z",
                                skill=(DATA_SKILL if i == 3 else GPU_SKILL))
                     for i in (1, 2, 3)]
            lines.append(self._line("2026-10-02T04:00:00Z", decision="deny",
                                    rule="quota-exhausted"))
            lines.append(self._line("2026-10-02T05:00:00Z", domain="other"))
            audit.write_text("\n".join(lines), encoding="utf-8")

            report = build_report(lines, domain="lab-x-y", domain_revision=2,
                                  window=(self.W0, self.W1), provider_aic=GPU_AIC)
            self.assertEqual(report["totalUses"], 3)
            usage = {(u["callerAic"], u["skill"]): u["count"] for u in report["usage"]}
            self.assertEqual(usage[(EXPERIMENT_AIC, GPU_SKILL)], 2)
            self.assertEqual(usage[(EXPERIMENT_AIC, DATA_SKILL)], 1)
            self.assertEqual(report["excluded"], {"outOfWindow": 0, "noTimestamp": 0,
                                                 "otherDomain": 1, "otherProvider": 0,
                                                 "notAllow": 1, "unparsable": 0})

            signed = sign_report(report, _gpu_signing())
            signed = sign_report(signed, _leader_signing())
            body, signers = verify_report(signed, trust_bundle=TRUST_BUNDLE,
                                          expected_signers={GPU_AIC, LEADER_AIC})
            self.assertEqual(body["domainRevision"], 2)
            self.assertTrue(body["window"]["applied"])

    def test_窗口按ts真实生效(self):
        lines = [self._line("2026-10-02T01:00:00Z"), self._line("2026-10-05T01:00:00Z")]
        inside = build_report(lines, domain="lab-x-y", domain_revision=1,
                              window=(self.W0, self.W1), provider_aic=GPU_AIC)
        outside = build_report(lines, domain="lab-x-y", domain_revision=1,
                               window=("2025-01-01T00:00:00Z", "2025-01-02T00:00:00Z"),
                               provider_aic=GPU_AIC)
        self.assertEqual((inside["totalUses"], outside["totalUses"]), (1, 0))
        self.assertEqual(outside["excluded"]["outOfWindow"], 2)
        # 窗口自身非法 → 全部算窗外，不猜区间
        bogus = build_report(lines, domain="lab-x-y", domain_revision=1,
                             window=("a", "b"), provider_aic=GPU_AIC)
        self.assertEqual(bogus["totalUses"], 0)

    def test_缺ts与异构provider的行不静默计入(self):
        lines = [
            '{"decision":"allow","domain":"lab-x-y","skillHint":"gpu.run","callerAic":"c"}',
            self._line("2026-10-02T02:00:00Z", mine="SOME-OTHER-PARTNER"),
            self._line("2026-10-02T03:00:00Z"),
        ]
        report = build_report(lines, domain="lab-x-y", domain_revision=1,
                              window=(self.W0, self.W1), provider_aic=GPU_AIC)
        self.assertEqual(report["totalUses"], 1)
        self.assertEqual(report["excluded"]["noTimestamp"], 1)
        self.assertEqual(report["excluded"]["otherProvider"], 1)
        self.assertEqual(report["scanned"], 3)

    def test_无域allow落哨兵可见(self):
        # PEP 侧对非域路径也落 domain/skillHint 哨兵，否则按域聚合会把真实访问漏光
        lines = [self._line("2026-10-02T06:00:00Z", domain=NO_DOMAIN,
                            rule="public-skill-unscoped", skill=NO_SKILL)]
        report = build_report(lines, domain=NO_DOMAIN, domain_revision=1,
                              window=(self.W0, self.W1), provider_aic=GPU_AIC)
        self.assertEqual(report["totalUses"], 1)
        self.assertEqual(report["byRule"], {"public-skill-unscoped": 1})
        self.assertEqual(report["usage"][0]["skill"], NO_SKILL)

    def test_技能按证据等级分档(self):
        # 三条同样"用了一次 kb.query"的行，证据等级完全不同：凭据 / 自报 / 未指名
        lines = [
            self._line("2026-10-02T10:00:00Z", skill="kb.query", rule="domain-grant"),
            ('{"ts":"2026-10-02T10:01:00Z","decision":"allow","rule":"public-skill",'
             '"callerAic":"%s","domain":"lab-x-y","skillHint":"(undeclared)",'
             '"declaredSkill":"kb.query","skillSource":"self-declared","myAic":"%s"}'
             % (EXPERIMENT_AIC, GPU_AIC)),
            ('{"ts":"2026-10-02T10:02:00Z","decision":"allow",'
             '"rule":"public-skill-unscoped","callerAic":"%s","domain":"lab-x-y",'
             '"skillHint":"(undeclared)","declaredSkill":"(undeclared)",'
             '"skillSource":"unspecified","myAic":"%s"}' % (EXPERIMENT_AIC, GPU_AIC)),
        ]
        rep = build_report(lines, domain="lab-x-y", domain_revision=1,
                           window=(self.W0, self.W1), provider_aic=GPU_AIC)
        self.assertEqual(rep["totalUses"], 3)
        rows = {(r["skill"], r["skillSource"]): r["count"] for r in rep["usage"]}
        self.assertEqual(rows[("kb.query", "token-claim")], 1)
        self.assertEqual(rows[("kb.query", "self-declared")], 1)
        self.assertEqual(rows[(NO_SKILL, "unspecified")], 1)
        self.assertEqual(rep["bySkillSource"],
                         {"token-claim": 1, "self-declared": 1, "unspecified": 1})

    def test_历史行没有skillSource时按hint推断(self):
        legacy_hint = ('{"ts":"2026-10-02T11:00:00Z","decision":"allow","rule":"domain-grant",'
                       '"callerAic":"%s","domain":"lab-x-y","skillHint":"kb.query","myAic":"%s"}'
                       % (EXPERIMENT_AIC, GPU_AIC))
        legacy_bare = ('{"ts":"2026-10-02T11:01:00Z","decision":"allow","rule":"leader-allowlist",'
                       '"callerAic":"%s","domain":"lab-x-y","myAic":"%s"}'
                       % (EXPERIMENT_AIC, GPU_AIC))
        rep = build_report([legacy_hint, legacy_bare], domain=NO_DOMAIN, domain_revision=1,
                           window=(self.W0, self.W1), provider_aic=GPU_AIC)
        self.assertEqual(rep["totalUses"], 0)          # 两行都属 lab-x-y 域
        rep2 = build_report([legacy_hint, legacy_bare], domain="lab-x-y", domain_revision=1,
                            window=(self.W0, self.W1), provider_aic=GPU_AIC)
        rows = {(r["skill"], r["skillSource"]): r["count"] for r in rep2["usage"]}
        # 修复前只有凭据路径会写 skillHint → 有 hint 的历史行推断为 token-claim，
        # 完全没有归属信息的历史行标 legacy-unknown
        self.assertEqual(rows[("kb.query", "token-claim")], 1)
        self.assertEqual(rows[(NO_SKILL, "legacy-unknown")], 1)

    def test_历史审计行缺字段也归入无域桶(self):
        # 现网 2026-10-15 之前落盘的 88 条 allow 根本没有 domain/skillHint 两个键，
        # 修复后仍要能被统计到（否则"改了新代码但历史台账永久不可对账"）
        legacy = ('{"ts":"2026-10-02T07:00:00Z","decision":"allow",'
                  '"rule":"leader-allowlist","callerAic":"%s","myAic":"%s"}'
                  % (EXPERIMENT_AIC, GPU_AIC))
        report = build_report([legacy], domain=NO_DOMAIN, domain_revision=1,
                              window=(self.W0, self.W1), provider_aic=GPU_AIC)
        self.assertEqual(report["totalUses"], 1)
        self.assertEqual(report["usage"][0]["skill"], NO_SKILL)

    def test_provider可给身份谱系(self):
        # 换身份前的审计行 myAic 是旧号；只传现役号会把那段历史判成别人的用量
        old_line = self._line("2026-10-02T08:00:00Z", mine="1.2.156.3088.1.BUPT.OLD.AAAA.0001")
        new_line = self._line("2026-10-02T09:00:00Z", mine=GPU_AIC)
        only_new = build_report([old_line, new_line], domain="lab-x-y", domain_revision=1,
                                window=(self.W0, self.W1), provider_aic=GPU_AIC)
        self.assertEqual(only_new["totalUses"], 1)
        self.assertEqual(only_new["excluded"]["otherProvider"], 1)
        lineage = build_report([old_line, new_line], domain="lab-x-y", domain_revision=1,
                               window=(self.W0, self.W1),
                               provider_aic=[GPU_AIC, "1.2.156.3088.1.BUPT.OLD.AAAA.0001"])
        self.assertEqual(lineage["totalUses"], 2)
        self.assertEqual(lineage["excluded"]["otherProvider"], 0)
        self.assertEqual(len(lineage["providerAics"]), 2)

    def test_缺一方签名拒绝(self):
        report = build_report([], domain="d", domain_revision=1,
                              window=("a", "b"), provider_aic=GPU_AIC)
        signed = sign_report(report, _gpu_signing())
        with self.assertRaises(tok.TokenError) as ctx:
            verify_report(signed, trust_bundle=TRUST_BUNDLE, expected_signers={GPU_AIC, LEADER_AIC})
        self.assertEqual(ctx.exception.reason, "report-undercount-signed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
