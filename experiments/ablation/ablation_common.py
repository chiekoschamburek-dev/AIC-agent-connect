"""对照实验公共工具：临时铸造一条 Ed25519 证书链，完全自包含。

干净克隆不携带 dev CA 私钥，因此所有需要真实签名的实验在运行时
自造一条 root CA + 若干 leaf 证书（SAN 里带 acps:// AIC）。这与生产
证书同构（Ed25519 + acps SAN + BasicConstraints），验签路径完全相同，
只是信任锚换成临时生成的——所以实验可复现、不依赖任何仓库私钥。

用法：
    ctx = Forge()                       # 造一条临时 root CA
    leader = ctx.leaf("leader")          # 签发一张 leader leaf（带 acps://leader.aic）
    bundle = ctx.trust_bundle            # 写好的 trust-bundle.pem
"""

from __future__ import annotations

import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID

from acps_cluster.delegation import token as tok


class Forge:
    """临时证书铸造厂：root CA + N 张 leaf，全部内存生成。"""

    def __init__(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="ablation-pki-"))
        self.ca_key = ed25519.Ed25519PrivateKey.generate()
        now = datetime.now(timezone.utc)
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ablation-root-ca")])
        self.ca_cert = (
            x509.CertificateBuilder()
            .subject_name(ca_name)
            .issuer_name(ca_name)
            .public_key(self.ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True, content_commitment=False,
                    key_encipherment=False, data_encipherment=False,
                    key_agreement=False, key_cert_sign=True, crl_sign=True,
                    encipher_only=False, decipher_only=False,
                ),
                critical=True,
            )
            .sign(self.ca_key, None)
        )
        self._bundle = self._tmp / "trust-bundle.pem"
        self._bundle.write_bytes(
            self.ca_cert.public_bytes(serialization.Encoding.PEM)
        )
        self._leaves: dict[str, tok.SigningContext] = {}

    @property
    def trust_bundle(self) -> Path:
        return self._bundle

    def leaf(self, label: str, *, aic: str | None = None) -> tok.SigningContext:
        """签发一张 leaf 证书并构造 SigningContext；AIC 形如 acps://<label>.aic。"""
        if label in self._leaves:
            return self._leaves[label]
        aic = aic or f"{label}.aic"
        key = ed25519.Ed25519PrivateKey.generate()
        now = datetime.now(timezone.utc)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, label)])
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(self.ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=365))
            .add_extension(
                x509.SubjectAlternativeName(
                    [x509.UniformResourceIdentifier(f"acps://{aic}")]
                ),
                critical=False,
            )
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True, content_commitment=False,
                    key_encipherment=False, data_encipherment=False,
                    key_agreement=False, key_cert_sign=False, crl_sign=False,
                    encipher_only=False, decipher_only=False,
                ),
                critical=True,
            )
            .sign(self.ca_key, None)
        )
        ctx = tok.SigningContext(private_key=key, chain=(cert, self.ca_cert), issuer_aic=aic)
        self._leaves[label] = ctx
        return ctx


def token_data_item(token: str, *, target_aic: str, skill_id: str) -> dict:
    """构造与生产同构的 delegation token dataItem。"""
    from acps_cluster.delegation.token import token_data_item as _item

    return _item(token, target_aic=target_aic, skill_id=skill_id)


def make_request(*, sender_aic: str, partner_aic: str, skill_id: str | None,
                 data_items: list[dict] | None = None, session_id: str = "session-ablation",
                 peer_verified: bool = True):
    """从 TaskCommand 造 AipPartnerAdmissionRequest（与单测/_request 同构）。"""
    from wit import AipPartnerAdmissionRequest as Req

    cmd_items = list(data_items or [])
    if skill_id and not any(
        (it.get("metadata") or {}).get("skillId") for it in cmd_items
    ):
        cmd_items.insert(0, {"type": "text", "text": "实验请求", "metadata": {"skillId": skill_id}})
    command = {
        "type": "task-command",
        "id": f"msg-{time.time_ns()}",
        "sentAt": datetime.now(timezone.utc).isoformat(),
        "senderRole": "leader",
        "senderId": sender_aic,
        "sessionId": session_id,
        "taskId": f"task-{time.time_ns()}",
        "command": "start",
        "commandParams": {},
        "dataItems": cmd_items,
    }
    return Req.from_command(
        command, leader_aic=sender_aic, partner_aic=partner_aic,
        delivery="rpc", peer_verified=peer_verified,
    )


def percentile(values: list[float], pct: float) -> float:
    """线性插值分位数（pct 取 0..100）。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * pct / 100.0
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def summarize(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "min": 0, "p50": 0, "p95": 0, "p99": 0, "max": 0, "mean": 0}
    return {
        "n": len(values),
        "min": round(min(values), 4),
        "p50": round(percentile(values, 50), 4),
        "p95": round(percentile(values, 95), 4),
        "p99": round(percentile(values, 99), 4),
        "max": round(max(values), 4),
        "mean": round(sum(values) / len(values), 4),
    }
