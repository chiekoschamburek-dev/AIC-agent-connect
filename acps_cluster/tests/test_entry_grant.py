"""入口域章程/grant 的离线端到端测试（临时 CA + 真实验签 + PEP 裁决）。

覆盖：
- 章程双签（prepare → cosign → install）与加载器；
- grant 签发、核验、与章程的资产/成员/配额对齐；
- PEP 端到端：带 grant 的入站 `research.submit` 走 `domain-grant` 放行；
  主体不符 → `grant-subject-mismatch`；配额耗尽 → `quota-exhausted`；
- CLI 四个子命令的参数接线。

需要 cryptography（生产依赖）；缺依赖时整组跳过。
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HAS_CRYPTO = importlib.util.find_spec("cryptography") is not None

if _HAS_CRYPTO:  # pragma: no branch
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.x509.oid import NameOID

from acps_cluster.delegation.domain_manifest import (
    load_domain_policies,
    manifest_body,
    to_policy,
    verify_manifest,
)
from acps_cluster.delegation.pep import PepConfig, evaluate
from acps_cluster.delegation.token import token_data_item
from acps_cluster.tools import entry_grant

SUP_AIC = "1.2.156.3088.1.BUPT.VTF97R.JXN8O8.98DOO8.0EB6"
PEER_AIC = "1.2.156.3088.1.BUPT.BFZ0FH.F2KZXJ.15ZZZG.19CN"
OTHER_AIC = "1.2.156.3088.1.BUPT.OTHER.X"
DOMAIN = "yanshu-entry"


def _cert_pem(cert) -> bytes:  # noqa: ANN001
    return cert.public_bytes(serialization.Encoding.PEM)


def _key_pem(key) -> bytes:  # noqa: ANN001
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def make_ca(root: Path):
    """自签根 CA（ca=True + keyCertSign），作为两侧共同 trust-bundle。"""
    key = ed25519.Ed25519PrivateKey.generate()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Entry Test CA")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=True,
                crl_sign=True, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, None)
    )
    root.mkdir(parents=True, exist_ok=True)
    (root / "ca.pem").write_bytes(_cert_pem(cert))
    return key, cert


def make_workspace(root: Path, aic: str, ca_key, ca_cert) -> Path:
    """写一个最小工作区：acps/client/{certificate,private-key}.pem + trust-bundle.pem。"""
    acps = root / "acps"
    (acps / "client").mkdir(parents=True, exist_ok=True)
    key = ed25519.Ed25519PrivateKey.generate()
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, aic)])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(ca_key, None)
    )
    (acps / "client" / "certificate.pem").write_bytes(_cert_pem(cert))
    (acps / "client" / "private-key.pem").write_bytes(_key_pem(key))
    (acps / "trust-bundle.pem").write_bytes(_cert_pem(ca_cert))
    return root


@unittest.skipUnless(_HAS_CRYPTO, "需要 cryptography")
class EntryGrantTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="entry-grant-"))
        ca_key, ca_cert = make_ca(self.tmp / "ca")
        self.sup_ws = make_workspace(self.tmp / "sup", SUP_AIC, ca_key, ca_cert)
        self.peer_ws = make_workspace(self.tmp / "peer", PEER_AIC, ca_key, ca_cert)
        self.trust = self.sup_ws / "acps" / "trust-bundle.pem"
        self.domains = self.sup_ws / "acps" / "domains"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _signed_charter(self) -> dict:
        manifest = entry_grant.build_entry_manifest(
            domain=DOMAIN, supervisor_aic=SUP_AIC, peer_aic=PEER_AIC,
        )
        signed = entry_grant.sign_manifest(manifest, entry_grant.signing_context(self.sup_ws))
        signed, signers = entry_grant.cosign_manifest(signed, workspace=self.peer_ws)
        self.assertEqual(signers, frozenset({SUP_AIC, PEER_AIC}))
        return signed

    def _grant_document(self, *, max_uses: int = 5, subject: str = PEER_AIC) -> dict:
        manifest = self._signed_charter()
        entry_grant.install_charter(
            manifest, workspace=self.sup_ws, domains_dir=self.domains,
        )
        _grant, document = entry_grant.issue_entry_grant(
            workspace=self.sup_ws,
            subject_aic=subject,
            domain=DOMAIN,
            revision=1,
            provider_aic=SUP_AIC,
            max_uses=max_uses,
            ttl_days=1,
        )
        return document

    # ── 章程 ──────────────────────────────────────────────

    def test_双签章程通过校验且能被加载(self):
        manifest = self._signed_charter()
        body, signers = verify_manifest(manifest, trust_bundle=self.trust)
        self.assertEqual(set(signers), {SUP_AIC, PEER_AIC})
        self.assertEqual(body["domainId"], DOMAIN)
        entry_grant.install_charter(manifest, workspace=self.sup_ws, domains_dir=self.domains)
        policy = load_domain_policies(self.domains, trust_bundle=self.trust)
        self.assertIn(DOMAIN, policy)
        self.assertEqual(policy[DOMAIN].member_role(PEER_AIC), "consumer")

    def test_篡改正文会被签名校验拒绝(self):
        manifest = self._signed_charter()
        tampered = dict(manifest)
        body = manifest_body(manifest)
        body["status"] = "frozen"
        tampered.update({k: v for k, v in body.items()})
        with self.assertRaises(Exception):
            verify_manifest(tampered, trust_bundle=self.trust)

    def test_单签不能安装(self):
        manifest = entry_grant.build_entry_manifest(
            domain=DOMAIN, supervisor_aic=SUP_AIC, peer_aic=PEER_AIC,
        )
        signed = entry_grant.sign_manifest(manifest, entry_grant.signing_context(self.sup_ws))
        with self.assertRaises(Exception):
            entry_grant.install_charter(signed, workspace=self.sup_ws, domains_dir=self.domains)

    def test_额外consumer进章程但不需要签名(self):
        manifest = entry_grant.build_entry_manifest(
            domain=DOMAIN, supervisor_aic=SUP_AIC, peer_aic=PEER_AIC,
            extra_consumers=[OTHER_AIC],
        )
        signed = entry_grant.sign_manifest(manifest, entry_grant.signing_context(self.sup_ws))
        signed, signers = entry_grant.cosign_manifest(signed, workspace=self.peer_ws)
        body, verified_signers = verify_manifest(signed, trust_bundle=self.trust)
        self.assertEqual(set(verified_signers), {SUP_AIC, PEER_AIC})
        policy = to_policy(body, verified_signers)
        self.assertEqual(policy.member_role(OTHER_AIC), "consumer")
        self.assertTrue(policy.asset_allows(
            provider_aic=SUP_AIC, skill_id="research.submit", role="consumer"))
        self.assertEqual(set(signers), {SUP_AIC, PEER_AIC})

    # ── grant ─────────────────────────────────────────────

    def test_签发与核验(self):
        document = self._grant_document()
        summary = entry_grant.check_grant_against_charter(
            document, workspace=self.sup_ws, domains_dir=self.domains,
        )
        self.assertEqual(summary["domain"], DOMAIN)
        self.assertEqual(summary["role"], "consumer")
        self.assertEqual(set(summary["assets"]), {
            f"{SUP_AIC}:research.submit", f"{SUP_AIC}:research.status",
        })

    def test_主体不符的grant不能核验(self):
        document = self._grant_document(subject=OTHER_AIC)
        with self.assertRaises(Exception):
            entry_grant.check_grant_against_charter(
                document, workspace=self.sup_ws, domains_dir=self.domains,
            )

    # ── PEP 端到端 ────────────────────────────────────────

    def _pep_config(self, policy, *, quota_path: Path) -> PepConfig:  # noqa: ANN001
        return PepConfig(
            enabled=True,
            my_aic=SUP_AIC,
            public_skills=frozenset({"research.orchestrate"}),
            has_non_public_skills=True,
            allow_unscoped_public_entry=False,
            domain_skills={},          # 入口技能不声明 visibility=domain（不改 ACS）
            domains={DOMAIN: policy},
            trust_bundle=self.trust,
            quota_path=quota_path,
        )

    @staticmethod
    def _request(*, leader: str, skill: str, token: str):
        from types import SimpleNamespace

        return SimpleNamespace(
            peer_verified=True,
            leader_aic=leader,
            partner_aic=SUP_AIC,
            command_params={"skillId": skill},
            data_items=[token_data_item(token, target_aic=SUP_AIC, skill_id=skill)],
            route=[],
            delivery=None,
        )

    def test_pep带grant放行域名资产(self):
        document = self._grant_document(max_uses=5)
        policies = load_domain_policies(self.domains, trust_bundle=self.trust)
        cfg = self._pep_config(policies[DOMAIN], quota_path=self.tmp / "quota.sqlite3")
        outcome = evaluate(
            self._request(leader=PEER_AIC, skill="research.submit", token=document["token"]),
            cfg,
        )
        self.assertEqual((outcome.decision, outcome.rule), ("allow", "domain-grant"))
        self.assertEqual(outcome.skill_hint, "research.submit")

    def test_pep主体不符拒绝(self):
        document = self._grant_document()
        policies = load_domain_policies(self.domains, trust_bundle=self.trust)
        cfg = self._pep_config(policies[DOMAIN], quota_path=self.tmp / "quota.sqlite3")
        outcome = evaluate(
            self._request(leader=OTHER_AIC, skill="research.submit", token=document["token"]),
            cfg,
        )
        self.assertEqual(outcome.decision, "deny")
        self.assertIn(outcome.rule, {"grant-subject-mismatch", "grant-not-member"})

    def test_pep配额耗尽拒绝(self):
        document = self._grant_document(max_uses=1)
        policies = load_domain_policies(self.domains, trust_bundle=self.trust)
        quota = self.tmp / "quota.sqlite3"
        cfg = self._pep_config(policies[DOMAIN], quota_path=quota)
        first = evaluate(
            self._request(leader=PEER_AIC, skill="research.submit", token=document["token"]),
            cfg,
        )
        self.assertEqual(first.decision, "allow")
        second = evaluate(
            self._request(leader=PEER_AIC, skill="research.submit", token=document["token"]),
            cfg,
        )
        self.assertEqual(second.decision, "deny")
        self.assertEqual(second.rule, "quota-exhausted")

    def test_未被授权的技能拒绝(self):
        document = self._grant_document()
        policies = load_domain_policies(self.domains, trust_bundle=self.trust)
        cfg = self._pep_config(policies[DOMAIN], quota_path=self.tmp / "quota.sqlite3")
        outcome = evaluate(
            self._request(leader=PEER_AIC, skill="research.cancel", token=document["token"]),
            cfg,
        )
        self.assertEqual(outcome.decision, "deny")
        self.assertEqual(outcome.rule, "skill-mismatch")

    # ── CLI ───────────────────────────────────────────────

    def test_cli四步子命令(self):
        draft = self.tmp / "draft.json"
        signed = self.tmp / "signed.json"
        grant_file = self.tmp / "grant.json"
        self.assertEqual(entry_grant.main([
            "prepare", "--supervisor-workspace", str(self.sup_ws),
            "--peer-aic", PEER_AIC, "--out", str(draft),
        ]), 0)
        self.assertEqual(entry_grant.main([
            "cosign", "--in", str(draft), "--peer-workspace", str(self.peer_ws),
            "--out", str(signed),
        ]), 0)
        self.assertEqual(entry_grant.main([
            "install", "--in", str(signed), "--supervisor-workspace", str(self.sup_ws),
        ]), 0)
        self.assertEqual(entry_grant.main([
            "issue", "--charter", str(signed), "--supervisor-workspace", str(self.sup_ws),
            "--subject-aic", PEER_AIC, "--provider-aic", SUP_AIC, "--out", str(grant_file),
        ]), 0)
        self.assertEqual(entry_grant.main([
            "verify", "--grant", str(grant_file), "--workspace", str(self.sup_ws),
        ]), 0)
        document = json.loads(grant_file.read_text(encoding="utf-8"))
        self.assertEqual(document["subject"], PEER_AIC)
        self.assertEqual(document["schema"], entry_grant.GRANT_SCHEMA)

    def test_cli_mint同机一步(self):
        grant_file = self.tmp / "mint-grant.json"
        self.assertEqual(entry_grant.main([
            "mint", "--supervisor-workspace", str(self.sup_ws),
            "--peer-workspace", str(self.peer_ws), "--out", str(grant_file),
        ]), 0)
        self.assertTrue(grant_file.is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)