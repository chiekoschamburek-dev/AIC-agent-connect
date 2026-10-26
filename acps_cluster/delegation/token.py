"""Delegation Token：AAC §10 的 Ed25519 JWS 实现（无状态可验证）。

对齐 PEP_design.md §3.1 的 claims 结构：
- §10.2 必需字段：iss / sub / aud / exp / iat / jti / scope / act
- §10.3 推荐字段（采用的子集）：acps_subject_type / acps_target_aic / acps_skill_id /
  acps_delegation_id / acps_delegation_mode / acps_allowed_route / acps_chain_depth /
  acps_max_chain_depth / cnf
- 扩展 claims：sessionId / parentTaskId（AAC 无对应字段，草案文档需注明偏差）；
  域派生扩展：acps_parent_jti/exp/max_uses + acps_parent_grant（父 grant JWS，
  验证方重验父签名并交叉核对，不把自报数字当配额依据）

x5c 只带 leaf：中间证书由验证方 trust-bundle 提供（`_verify_chain` 第二分支用 bundle
内的锚验链尾签名），token 缩小 ~0.7K 字符（实测 3234→2548）。**硬依赖：验证方
trust-bundle 必须包含签发中间 CA**（当前集群 bundle = Root + 两个 Intermediate，天然
满足）；若未来收窄为 root-only bundle，签发侧用 `issue_token(include_intermediates=True)`
退回整链。无论哪种形态，验证都无需联络签发者（与 mTLS 同一信任锚）。

实现期对设计文档的三处修正（详见 PEP-result.md「与文档的偏差」）：
1. `x5c` 按 RFC 7515 用 **base64(DER)**，不是文档字面写的 base64(PEM)。
2. `aud` 采用 AAC §6.7 的 canonical 形式 `acps:agent:{aic}`；校验时对 `acps://` /
   `agent:` 前缀宽容，只严格比较 AIC 部分（文档 §3.1 正文用的是无前缀形式）。
3. `x5c` 不带中间证书（2026-09-28 第五轮）：信任判定两种形态等价，都归结为
   「leaf 由 bundle 内某证书签发」；见上段落落的硬依赖声明。
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

TOKEN_ALG = "EdDSA"
TOKEN_TYP = "JWT"
DEFAULT_TTL_SECONDS = 300
# 设计文档 §3.1/§4.2.2 的口径是「建议时效 ≤ 10 分钟」（此前实现误写 900，第四轮统一为 600）
MAX_TTL_SECONDS = 600
CLOCK_SKEW_SECONDS = 60

ROLE_DELEGATION_TOKEN = "delegation-token"
TOKEN_DATA_KEY = "acpsDelegationToken"
TARGET_AIC_KEY = "targetAic"
TARGET_SKILL_KEY = "targetSkillHint"

_AUDIENCE_PREFIXES = ("acps:agent:", "agent:")

_REQUIRED_CLAIMS = ("iss", "sub", "aud", "exp", "iat", "jti", "scope", "act")


class TokenError(Exception):
    """token 校验失败。`reason` 是稳定的原因码，只进审计，不外泄给对端（AAC §12）。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _b64_std_encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def canonical_audience(aic: str) -> str:
    """AAC §6.7 canonical audience。"""
    return f"acps:agent:{aic}"


def normalize_audience(value: str) -> str:
    """把 `acps:agent:X` / `agent:X` / `X` 统一成 `X`（宽容前缀，严格 AIC）。"""
    text = str(value or "").strip()
    for prefix in _AUDIENCE_PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix) :]
    return text


def _agent_uri(aic: str) -> str:
    return f"agent:{aic}"


def aic_from_certificate(certificate: x509.Certificate) -> str:
    """从证书取 AIC：优先 SAN 里的 `acps://` URI，回退 CN（与 wit 严格模式同源）。"""
    try:
        san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        san = None
    if san is not None:
        for uri in san.get_values_for_type(x509.UniformResourceIdentifier):
            if isinstance(uri, str) and uri.startswith("acps://"):
                aic = uri[len("acps://") :].strip("/")
                if aic:
                    return aic
    for attribute in certificate.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME):
        value = attribute.value
        if isinstance(value, str) and value:
            return value
    raise TokenError("certificate-no-aic", "certificate has no acps:// SAN or CN")


def certificate_sha256_fingerprint(certificate: x509.Certificate) -> str:
    """RFC 7515 `x5t#S256`：base64url(sha256(DER))，无 padding。"""
    digest = hashes.Hash(hashes.SHA256())
    digest.update(certificate.public_bytes(serialization.Encoding.DER))
    return _b64u_encode(digest.finalize())


def load_certificates(pem: Path | str | bytes) -> tuple[x509.Certificate, ...]:
    if isinstance(pem, (str, Path)):
        raw = Path(pem).read_bytes()
    else:
        raw = pem
    return tuple(x509.load_pem_x509_certificates(raw))


@dataclass(frozen=True)
class SigningContext:
    """签发方上下文：私钥 + 证书链（leaf 在首位）+ 自身 AIC。"""

    private_key: ed25519.Ed25519PrivateKey
    chain: tuple[x509.Certificate, ...]
    issuer_aic: str

    @property
    def leaf(self) -> x509.Certificate:
        return self.chain[0]


def build_certificate_chain(
    leaf: x509.Certificate, trust_bundle: Path | str
) -> tuple[x509.Certificate, ...]:
    """按 issuer→subject 关系从 trust-bundle 补齐中间证书。

    默认不进 x5c（只带 leaf，见模块说明）；仅当 `issue_token(include_intermediates=True)`
    时使用，且**不含信任锚**（RFC 7515：信任锚由验证方本地持有）。
    """
    pool = {cert.subject.rfc4514_string(): cert for cert in load_certificates(trust_bundle)}
    chain = [leaf]
    seen = {leaf.subject.rfc4514_string()}
    current = leaf
    while current.issuer != current.subject:
        issuer = pool.get(current.issuer.rfc4514_string())
        if issuer is None or issuer.subject.rfc4514_string() in seen:
            break
        if issuer.issuer == issuer.subject:
            # 自签根 = 信任锚，不进 x5c
            break
        chain.append(issuer)
        seen.add(issuer.subject.rfc4514_string())
        current = issuer
    return tuple(chain)


def load_signing_context(
    client_dir: Path | str,
    *,
    trust_bundle: Path | str | None = None,
    private_key_name: str = "private-key.pem",
    certificate_name: str = "certificate.pem",
) -> SigningContext:
    """从 `<workspace>/acps/client/` 加载 Ed25519 私钥与客户端证书。"""
    directory = Path(client_dir)
    key_bytes = (directory / private_key_name).read_bytes()
    private_key = serialization.load_pem_private_key(key_bytes, password=None)
    if not isinstance(private_key, ed25519.Ed25519PrivateKey):
        raise TokenError("unsupported-key", f"expected Ed25519 private key, got {type(private_key).__name__}")
    leaf = load_certificates(directory / certificate_name)[0]
    chain = build_certificate_chain(leaf, trust_bundle) if trust_bundle else (leaf,)
    if len(chain) == 1 and trust_bundle:
        # trust-bundle 只有根 CA（本集群实况）时，build_certificate_chain 补不出
        # 中间证书 → x5c 只有叶子 → 验证方（同样只有根）永远 chain-untrusted。
        # 允许签发方目录里放一份 intermediate.pem（平台中间 CA，从服务端 TLS
        # 链提取），主题必须正是叶子的 issuer 才采纳（2026-10-26 现网修复）。
        for extra_name in ("intermediate.pem", "chain.pem", "intermediate-ca.pem"):
            extra_path = directory / extra_name
            if not extra_path.is_file():
                continue
            try:
                for cert in load_certificates(extra_path):
                    if (cert.subject == leaf.issuer
                            and all(c.subject != leaf.issuer for c in chain)):
                        chain = (*chain, cert)
                        break
            except Exception:  # noqa: BLE001  坏文件不阻断签发
                continue
            if len(chain) > 1:
                break
    return SigningContext(private_key=private_key, chain=chain, issuer_aic=aic_from_certificate(leaf))


@dataclass(frozen=True)
class IssuedToken:
    token: str
    claims: dict[str, Any]
    expires_at: int

    @property
    def jti(self) -> str:
        return str(self.claims["jti"])

    @property
    def delegation_id(self) -> str:
        return str(self.claims["acps_delegation_id"])

def _sign(signing: SigningContext, header: dict[str, Any], claims: dict[str, Any]) -> str:
    header_segment = _b64u_encode(json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    payload_segment = _b64u_encode(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    signature = signing.private_key.sign(signing_input)
    return f"{header_segment}.{payload_segment}.{_b64u_encode(signature)}"


def issue_token(
    signing: SigningContext,
    *,
    delegate_aic: str,
    target_aic: str,
    skill_id: str,
    session_id: str | None = None,
    parent_task_id: str | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    not_after: int | None = None,
    delegation_id: str | None = None,
    allowed_route: Sequence[str] | None = None,
    chain_depth: int = 1,
    max_chain_depth: int = 2,
    scope: str | None = None,
    delegate_certificate_fingerprint: str | None = None,
    include_intermediates: bool = False,
    now: float | None = None,
    extra_claims: Mapping[str, Any] | None = None,
) -> IssuedToken:
    """签发一枚 leader→delegate 的固定链路 token（leader 兼任 issuer，未引入 STS）。

    `allowed_route` 缺省为 [leader, delegate, target]（AAC §10.6 固定链路写法：`agent:{AIC}`）。
    `not_after`：绝对到期上界（域级派生继承用——派生 token 的 exp 不得超过父 grant 的 exp，
    见 domain_manifest/grant 的嵌套设计）。

    域声明只能来自父 grant：`extra_claims` 里出现 `acps_domain` 时，必须同时带
    `acps_parent_jti` / `acps_parent_exp` / `acps_parent_max_uses` 以及**父 grant
    的 JWS 原文** `acps_parent_grant`（由 derive_delegation_claims(..., parent_token=...)
    生成）。父 exp 会**自动收紧**本令牌的 exp；验证方会重验父签名并交叉核对
    jti/域/主体/资产/exp/max_uses，自报数字不再能伪造配额。
    """
    if ttl_seconds <= 0:
        raise TokenError("invalid-ttl", "ttl_seconds must be positive")
    if ttl_seconds > MAX_TTL_SECONDS:
        raise TokenError("invalid-ttl", f"ttl_seconds must be <= {MAX_TTL_SECONDS}")

    # scope 不接受自造值：必须与 skill_id 派生值一致（verify_token 侧有对称校验）。
    # 显式传入不一致值直接报错，而不是让 scope 变成装饰性 claim。
    derived_scope = f"acps.skill.invoke:{skill_id}"
    if scope is not None and scope != derived_scope:
        raise TokenError("inconsistent-scope", f"scope must be {derived_scope!r} (derived from skill_id)")

    issued_at = int(now if now is not None else time.time())
    extra = dict(extra_claims or {})

    domain = str(extra.get("acps_domain") or "").strip()
    if domain:
        try:
            domain_revision = int(extra.get("acps_domain_revision"))
        except (TypeError, ValueError) as exc:
            raise TokenError("missing-claim", "acps_domain_revision must accompany acps_domain") from exc
        extra["acps_domain"] = domain
        extra["acps_domain_revision"] = domain_revision

    parent_jti = str(extra.get("acps_parent_jti") or "").strip()
    has_parent_fields = bool(
        parent_jti
        or "acps_parent_exp" in extra
        or "acps_parent_max_uses" in extra
        or "acps_parent_revision" in extra
        or "acps_parent_issuer" in extra
        or "acps_parent_grant" in extra
    )
    parent_exp: int | None = None
    if has_parent_fields:
        if not domain:
            raise TokenError("parent-without-domain", "parent binding claims require acps_domain")
        if not parent_jti:
            raise TokenError("incomplete-parent-binding", "acps_parent_jti is required")
        try:
            parent_exp = int(extra["acps_parent_exp"])
            parent_max_uses = int(extra["acps_parent_max_uses"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TokenError(
                "incomplete-parent-binding",
                "acps_parent_exp/acps_parent_max_uses are required",
            ) from exc
        if parent_max_uses < 1:
            raise TokenError("invalid-parent-quota", "acps_parent_max_uses must be >= 1")
        if parent_exp <= issued_at:
            raise TokenError("parent-expired", f"parent exp={parent_exp}")
        if "acps_parent_revision" in extra:
            try:
                parent_revision = int(extra["acps_parent_revision"])
            except (TypeError, ValueError) as exc:
                raise TokenError("incomplete-parent-binding", "acps_parent_revision must be an integer") from exc
            if parent_revision != int(extra["acps_domain_revision"]):
                raise TokenError(
                    "parent-revision-mismatch",
                    "acps_parent_revision != acps_domain_revision",
                )
            extra["acps_parent_revision"] = parent_revision
        parent_grant = str(extra.get("acps_parent_grant") or "").strip()
        if not parent_grant:
            raise TokenError(
                "missing-parent-proof",
                "域令牌必须嵌入父 grant JWS（acps_parent_grant）",
            )
        if parent_grant.count(".") != 2:
            raise TokenError("invalid-parent-proof", "acps_parent_grant 必须是 compact JWS")
        extra["acps_parent_grant"] = parent_grant
        extra["acps_parent_jti"] = parent_jti
        extra["acps_parent_exp"] = parent_exp
        extra["acps_parent_max_uses"] = parent_max_uses
    elif domain:
        raise TokenError(
            "domain-token-requires-parent",
            "域作用域委托令牌必须由域 grant 派生（缺 acps_parent_jti）",
        )

    expires_at = issued_at + int(ttl_seconds)
    if not_after is not None:
        expires_at = min(expires_at, int(not_after))
    if parent_exp is not None:
        expires_at = min(expires_at, parent_exp)
    leader_aic = signing.issuer_aic
    route = list(allowed_route) if allowed_route else [
        _agent_uri(leader_aic),
        _agent_uri(delegate_aic),
        _agent_uri(target_aic),
    ]

    claims: dict[str, Any] = {
        # ── §10.2 必需字段 ──
        "iss": _agent_uri(leader_aic),
        "sub": _agent_uri(leader_aic),
        "aud": canonical_audience(target_aic),
        "exp": expires_at,
        "iat": issued_at,
        "jti": str(uuid.uuid4()),
        "scope": derived_scope,
        "act": {
            "sub": _agent_uri(delegate_aic),
            "aic": delegate_aic,
            "act": {"sub": _agent_uri(leader_aic), "aic": leader_aic},
        },
        # ── §10.3 推荐字段（采用的子集）──
        "acps_subject_type": "agent",
        "acps_target_aic": target_aic,
        "acps_skill_id": skill_id,
        "acps_delegation_id": delegation_id or f"dlg-{uuid.uuid4()}",
        "acps_delegation_mode": "fixed",
        "acps_allowed_route": route,
        "acps_chain_depth": int(chain_depth),
        "acps_max_chain_depth": int(max_chain_depth),
    }
    if session_id:
        claims["sessionId"] = session_id
    if parent_task_id:
        claims["parentTaskId"] = parent_task_id
    if delegate_certificate_fingerprint:
        claims["cnf"] = {"x5t#S256": delegate_certificate_fingerprint}
    if extra:
        overridden = sorted(set(extra) & set(claims))
        if overridden:
            raise TokenError(
                "reserved-claim",
                f"extra_claims 不能覆盖已生成的 claim: {overridden}",
            )
        claims.update(extra)

    # x5c 默认只带 leaf（体积约减半）：中间证书由验证方 trust-bundle 提供，硬依赖见模块说明；
    # include_intermediates=True 退回整链（兼容未来 root-only bundle 的逃生门）。
    x5c_chain = signing.chain if include_intermediates else (signing.leaf,)
    header = {
        "alg": TOKEN_ALG,
        "typ": TOKEN_TYP,
        "x5c": [_b64_std_encode(cert.public_bytes(serialization.Encoding.DER)) for cert in x5c_chain],
    }
    return IssuedToken(token=_sign(signing, header, claims), claims=claims, expires_at=expires_at)


def _parse_token(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    if not isinstance(token, str) or token.count(".") != 2:
        raise TokenError("malformed", "expected three JWS segments")
    header_segment, payload_segment, signature_segment = token.split(".")
    try:
        header = json.loads(_b64u_decode(header_segment))
        claims = json.loads(_b64u_decode(payload_segment))
        signature = _b64u_decode(signature_segment)
    except Exception as exc:  # noqa: BLE001
        raise TokenError("malformed", f"{type(exc).__name__}") from exc
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise TokenError("malformed", "header/payload must be JSON objects")
    return header, claims, signature, f"{header_segment}.{payload_segment}".encode("ascii")


def _verify_certificate_signature(issuer: x509.Certificate, subject: x509.Certificate) -> None:
    public_key = issuer.public_key()
    if isinstance(public_key, ed25519.Ed25519PublicKey):
        public_key.verify(subject.signature, subject.tbs_certificate_bytes)
    elif isinstance(public_key, ec.EllipticCurvePublicKey):
        public_key.verify(
            subject.signature,
            subject.tbs_certificate_bytes,
            ec.ECDSA(subject.signature_hash_algorithm),
        )
    elif isinstance(public_key, rsa.RSAPublicKey):
        public_key.verify(
            subject.signature,
            subject.tbs_certificate_bytes,
            padding.PKCS1v15(),
            subject.signature_hash_algorithm,
        )
    else:
        raise TokenError("unsupported-key", type(public_key).__name__)


def _basic_constraints(cert: x509.Certificate) -> x509.BasicConstraints | None:
    try:
        return cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    except x509.ExtensionNotFound:
        return None


def _require_issuer_ca(cert: x509.Certificate, *, below_ca: int) -> None:
    """签发者必须是 CA；约束 path_length 与 KeyUsage.keyCertSign。"""
    constraints = _basic_constraints(cert)
    if constraints is None or not constraints.ca:
        raise TokenError(
            "certificate-not-ca",
            f"{cert.subject.rfc4514_string()} signed another certificate but ca=False",
        )
    if constraints.path_length is not None and below_ca > constraints.path_length:
        raise TokenError(
            "path-length-exceeded",
            f"{cert.subject.rfc4514_string()} path_length={constraints.path_length}, "
            f"chain has {below_ca} CA certificate(s) below it",
        )
    try:
        usage = cert.extensions.get_extension_for_class(x509.KeyUsage).value
    except x509.ExtensionNotFound:
        return
    if not usage.key_cert_sign:
        raise TokenError(
            "certificate-key-usage",
            f"{cert.subject.rfc4514_string()} lacks keyCertSign",
        )


def _require_leaf(cert: x509.Certificate) -> None:
    constraints = _basic_constraints(cert)
    if constraints is not None and constraints.ca:
        raise TokenError(
            "certificate-is-ca",
            f"x5c leaf {cert.subject.rfc4514_string()} is a CA certificate",
        )


def _verify_chain(
    chain: Sequence[x509.Certificate], anchors: Sequence[x509.Certificate], *, now: float
) -> None:
    """用本地 trust-bundle 验链：leaf → … → 命中信任锚。

    除逐级签名外，还执行 RFC 5280 的基本约束：leaf 必须 ca=False；所有签发者必须
    ca=True、可选 KeyUsage 必须含 keyCertSign，并满足 path_length。缺了这些检查，
    任意一张普通叶子证书都能签出"下级 CA"并伪造 leader 令牌（2026-10-18 审计 P0-1）。
    """
    if not chain:
        raise TokenError("missing-x5c", "empty x5c")
    anchor_der = {a.public_bytes(serialization.Encoding.DER) for a in anchors}

    for index, cert in enumerate(chain):
        not_before = cert.not_valid_before_utc.timestamp()
        not_after = cert.not_valid_after_utc.timestamp()
        if now < not_before or now > not_after:
            raise TokenError("certificate-expired", cert.subject.rfc4514_string())

    _require_leaf(chain[0])
    # chain[1..] 都是链内签发者；下标 i 的 CA 下方有 i-1 张链内 CA。
    for index in range(1, len(chain)):
        _require_issuer_ca(chain[index], below_ca=index - 1)

    for index in range(len(chain) - 1):
        _verify_certificate_signature(chain[index + 1], chain[index])

    last = chain[-1]
    if last.public_bytes(serialization.Encoding.DER) in anchor_der:
        return
    # 链尾不是锚：尝试用 bundle 里的锚验证链尾签名（锚本身可能未随 x5c 下发）。
    below_ca = len(chain) - 1
    for anchor in anchors:
        if anchor.subject != last.issuer:
            continue
        _require_issuer_ca(anchor, below_ca=below_ca)
        try:
            _verify_certificate_signature(anchor, last)
            return
        except InvalidSignature:
            continue
    raise TokenError("chain-untrusted", f"chain tail {last.subject.rfc4514_string()} is not anchored")


def peek_claims(token: str) -> dict[str, Any]:
    """只解析 payload、**不验签**（malformed 仍抛 TokenError）。

    唯一用途：受托方（令牌搬运工）要先知道 `aud` 才能驱动 `verify_token(expected_aud=...)`。
    这一处的 aud 比较是循环的，但**不构成放宽**——令牌的 audience 权威校验发生在目标伙伴的
    PEP（pep.py 用本机 AIC 作为 expected_aud）；搬运工只负责"签名有效 + 签发者是白名单
    leader + act.aic 是我"，aud 指向谁对搬运工不重要。
    """
    _, claims, _, _ = _parse_token(token)
    return claims


def verify_token(
    token: str,
    *,
    trust_bundle: Path | str,
    expected_aud: str,
    now: float | None = None,
    clock_skew_seconds: int = CLOCK_SKEW_SECONDS,
    max_ttl_seconds: int | None = MAX_TTL_SECONDS,
) -> dict[str, Any]:
    """验证 token 并返回 claims。任何失败抛 TokenError（fail-closed）。"""
    checked_at = float(now if now is not None else time.time())
    header, claims, signature, signing_input = _parse_token(token)

    if header.get("alg") != TOKEN_ALG:
        raise TokenError("unsupported-alg", str(header.get("alg")))

    x5c = header.get("x5c")
    if not isinstance(x5c, list) or not x5c:
        raise TokenError("missing-x5c", "x5c must be a non-empty array")
    try:
        chain = tuple(x509.load_der_x509_certificate(base64.b64decode(entry)) for entry in x5c)
    except Exception as exc:  # noqa: BLE001
        raise TokenError("bad-x5c", f"{type(exc).__name__}") from exc

    anchors = load_certificates(trust_bundle)
    _verify_chain(chain, anchors, now=checked_at)

    leaf = chain[0]
    leaf_key = leaf.public_key()
    # 叶子公钥必须是 Ed25519（header.alg=EdDSA）。不做类型检查的话，EC/RSA 叶子会在
    # verify() 上抛 TypeError 而非 TokenError——wit 的 admission 分发无 try/except，
    # 异常会裸穿到 RPC 层（任务不建档、无审计）。当前集群 CA 只签 Ed25519，此检查是
    # 防御性的（见 PEP-result.md 第四轮缺陷 4）。
    if not isinstance(leaf_key, ed25519.Ed25519PublicKey):
        raise TokenError(
            "unsupported-key", f"x5c leaf public key must be Ed25519, got {type(leaf_key).__name__}"
        )
    try:
        leaf_key.verify(signature, signing_input)
    except InvalidSignature as exc:
        raise TokenError("bad-signature", "x5c leaf does not match token signature") from exc

    for name in _REQUIRED_CLAIMS:
        if name not in claims:
            raise TokenError("missing-claim", name)

    # 签名只证明"某个受信证书持有者签的"；iss 必须与叶子证书 AIC 一致，
    # 否则社区内任何 agent 都能签出 iss=leader 的 token（2026-09-28 审计 PoC）。
    try:
        leaf_aic = aic_from_certificate(leaf)
    except TokenError as exc:
        raise TokenError("bad-signature", f"x5c leaf has no AIC: {exc.detail}") from exc
    if normalize_audience(str(claims.get("iss") or "")) != leaf_aic:
        raise TokenError("issuer-mismatch", "iss does not match x5c leaf certificate AIC")

    try:
        expires_at = int(claims["exp"])
        issued_at = int(claims["iat"])
    except (TypeError, ValueError) as exc:
        raise TokenError("malformed", "exp/iat must be integers") from exc

    if checked_at > expires_at + clock_skew_seconds:
        raise TokenError("expired", f"exp={expires_at}")
    if issued_at > checked_at + clock_skew_seconds:
        raise TokenError("not-yet-valid", f"iat={issued_at}")
    if max_ttl_seconds is not None and expires_at - issued_at > max_ttl_seconds:
        raise TokenError("ttl-too-long", f"ttl={expires_at - issued_at}")

    if normalize_audience(str(claims["aud"])) != normalize_audience(expected_aud):
        raise TokenError("audience-mismatch", str(claims["aud"]))

    # claims 间一致性（§5.5.1 的加固项）：目标与授权对象不得与裁决依据脱节。
    # - acps_target_aic 必须与 aud 指同一个伙伴（否则 aud 校验通过但授权对象另有其人）；
    # - scope 必须与 acps_skill_id 派生值一致（否则 scope 成了装饰性 claim）。
    target_aic = claims.get("acps_target_aic")
    if isinstance(target_aic, str) and target_aic:
        if normalize_audience(target_aic) != normalize_audience(str(claims["aud"])):
            raise TokenError("target-mismatch", "acps_target_aic does not match aud")
    scope_claim = claims.get("scope")
    skill_claim = claims.get("acps_skill_id")
    if isinstance(scope_claim, str) and scope_claim and isinstance(skill_claim, str) and skill_claim:
        if scope_claim != f"acps.skill.invoke:{skill_claim}":
            raise TokenError("scope-mismatch", "scope does not match acps_skill_id")

    act = claims.get("act")
    if not isinstance(act, Mapping) or not act.get("aic"):
        raise TokenError("missing-claim", "act.aic")

    # 域作用域委托令牌必须能证明自己是从父 grant 派生的：父 jti/exp/配额随令牌
    # 一起签名，验证方不必回查签发侧即可拒绝"超父期"与"无域声明的洗白令牌"。
    token_domain = str(claims.get("acps_domain") or "").strip()
    parent_jti = str(claims.get("acps_parent_jti") or "").strip()
    if token_domain and not parent_jti:
        raise TokenError("missing-claim", "domain token requires acps_parent_jti")
    if parent_jti and not token_domain:
        raise TokenError("parent-without-domain", "acps_parent_jti requires acps_domain")
    if parent_jti:
        try:
            parent_exp = int(claims["acps_parent_exp"])
            parent_max_uses = int(claims["acps_parent_max_uses"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TokenError(
                "missing-claim",
                "acps_parent_exp/acps_parent_max_uses must be present and integer",
            ) from exc
        if parent_max_uses < 1:
            raise TokenError("invalid-parent-quota", "acps_parent_max_uses must be >= 1")
        if expires_at > parent_exp:
            raise TokenError(
                "parent-exp-exceeded",
                f"token exp={expires_at} exceeds parent exp={parent_exp}",
            )
        parent_revision = claims.get("acps_parent_revision")
        if parent_revision is not None:
            domain_revision = claims.get("acps_domain_revision")
            if domain_revision is None:
                raise TokenError("missing-claim", "acps_parent_revision requires acps_domain_revision")
            try:
                if int(parent_revision) != int(domain_revision):
                    raise TokenError(
                        "parent-revision-mismatch",
                        "acps_parent_revision != acps_domain_revision",
                    )
            except (TypeError, ValueError) as exc:
                raise TokenError("malformed", "domain revision claims must be integers") from exc

        # 父绑定必须是密码学锚点，而不是子令牌里的自报数字：验证嵌入的父 grant JWS，
        # 再把 jti/域/主体/资产/exp/max_uses 逐项对齐。否则任何持有 leader 私钥的代码
        # 都能伪造 parent_jti 与 1e9 配额，PEP 的"独立复核"就只是装饰。
        parent_grant = claims.get("acps_parent_grant")
        if not isinstance(parent_grant, str) or parent_grant.count(".") != 2:
            raise TokenError("missing-claim", "domain token requires acps_parent_grant")
        from .grant import verify_grant  # noqa: PLC0415

        try:
            parent_claims = verify_grant(
                parent_grant,
                trust_bundle=trust_bundle,
                now=checked_at,
                clock_skew_seconds=clock_skew_seconds,
            )
        except TokenError as exc:
            raise TokenError("parent-grant-invalid", exc.reason) from exc
        try:
            pg_jti = str(parent_claims["jti"])
            pg_domain = str(parent_claims["acps_domain"])
            pg_exp = int(parent_claims["exp"])
            pg_max_uses = int(parent_claims["acps_max_uses"])
            pg_sub = normalize_audience(str(parent_claims["sub"]))
            pg_aud = normalize_audience(str(parent_claims["aud"]))
            pg_revision = int(parent_claims.get("acps_domain_revision") or 1)
            child_revision = int(claims.get("acps_domain_revision") or 1)
        except (KeyError, TypeError, ValueError) as exc:
            raise TokenError("parent-grant-mismatch", f"父 grant 字段不完整: {exc}") from exc
        if pg_jti != parent_jti:
            raise TokenError("parent-grant-mismatch", "acps_parent_jti != parent grant jti")
        if pg_domain != token_domain:
            raise TokenError("parent-grant-mismatch", "acps_domain != parent grant domain")
        if pg_exp != parent_exp:
            raise TokenError("parent-grant-mismatch", "acps_parent_exp != parent grant exp")
        if pg_max_uses != parent_max_uses:
            raise TokenError(
                "parent-grant-mismatch",
                "acps_parent_max_uses != parent grant max_uses",
            )
        if pg_sub != normalize_audience(str(claims["iss"])):
            raise TokenError(
                "parent-subject-mismatch",
                "父 grant sub != 子令牌 iss（签发者不是 grant 持有者）",
            )
        if pg_aud != pg_sub:
            raise TokenError("parent-audience-mismatch", "父 grant aud != sub（未绑定持有者）")
        if pg_revision != child_revision:
            raise TokenError("parent-revision-mismatch", "父 grant revision != 子令牌 revision")
        skill_claim = str(claims.get("acps_skill_id") or "")
        if not skill_claim:
            raise TokenError("missing-claim", "acps_skill_id")
        parent_assets = {
            (normalize_audience(str(asset.get("providerAic") or "")), str(asset.get("skill") or ""))
            for asset in parent_claims.get("acps_assets") or ()
            if isinstance(asset, Mapping)
        }
        if (normalize_audience(expected_aud), skill_claim) not in parent_assets:
            raise TokenError(
                "parent-asset-mismatch",
                f"父 grant 资产集不覆盖 ({expected_aud}, {skill_claim})",
            )

    return claims


def token_data_item(
    token: str,
    *,
    target_aic: str,
    skill_id: str,
    usage: str | None = None,
) -> dict[str, Any]:
    """§3.2 约定的点对点传递通道：`metadata.role == "delegation-token"` 的 StructuredDataItem。

    该 dataItem 由 `DelegationTokenScrubHooks` 在进模型提示词之前剥离，防提示注入窃取 token。
    """
    return {
        "type": "data",
        "data": {
            TOKEN_DATA_KEY: token,
            TARGET_AIC_KEY: target_aic,
            TARGET_SKILL_KEY: skill_id,
            "usage": usage or "持此令牌直接向目标伙伴发起任务，勿回传给 leader",
        },
        "metadata": {"role": ROLE_DELEGATION_TOKEN},
    }


def is_delegation_token_data_item(item: Any) -> bool:
    if not isinstance(item, Mapping):
        return False
    metadata = item.get("metadata")
    if isinstance(metadata, Mapping) and metadata.get("role") == ROLE_DELEGATION_TOKEN:
        return True
    data = item.get("data")
    return isinstance(data, Mapping) and TOKEN_DATA_KEY in data


def extract_tokens_from_data_items(data_items: Iterable[Any] | None) -> list[str]:
    """按 §3.2 约定取出**全部** token（一次委派可携带多枚：任务令牌 + 工作区 bundle）。"""
    out: list[str] = []
    for item in data_items or ():
        if not is_delegation_token_data_item(item):
            continue
        data = item.get("data") if isinstance(item, Mapping) else None
        if isinstance(data, Mapping):
            token = data.get(TOKEN_DATA_KEY)
            if isinstance(token, str) and token:
                out.append(token)
    return out


def extract_token_from_data_items(data_items: Iterable[Any] | None) -> str | None:
    """按 §3.2 约定查找 token；找不到返回 None（调用方据此走「缺凭据」分支）。"""
    tokens = extract_tokens_from_data_items(data_items)
    return tokens[0] if tokens else None


def extract_token_for_audience(
    data_items: Iterable[Any] | None, my_aic: str, *,
    accept: Any | None = None,
) -> str | None:
    """取**发给本机**的 token（aud == my_aic）；不是给本机的令牌按类型处理。

    为什么需要它（2026-10-26 workspace bundle）：一次委派会同时携带任务令牌
    （aud=partner）与工作区令牌（aud=workspace）。PEP 若拿第一枚就把工作区令牌
    当成本机凭据，会验出 audience-mismatch / act-peer-mismatch，把合法的公开
    委派误判成非法凭据。凭据选择必须先按 audience 归位，再交给 verify_token。

    规则：
    - `accept(claims)` 为真的令牌（域 grant）直接返回：grant 的 aud 语义不同，
      其主体绑定由 `_evaluate_grant` 自己校验；
    - aud == 本机 → 返回；
    - 非本机的 **ws.* 工作区 bundle** → 只是本机要替别人保管的令牌，忽略；
    - 非本机的其它令牌（任务令牌发错对象）→ 返回，让 verify_token 照旧报
      `invalid-token`，不把"凭据错投"降级成"无凭据"；
    - 只有 malformed 令牌时返回它，保持 invalid-token 语义。
    """
    want = normalize_audience(my_aic)
    tokens = extract_tokens_from_data_items(data_items)
    if not tokens or not want:
        return None
    malformed: list[str] = []
    foreign: list[str] = []
    for token in tokens:
        try:
            claims = peek_claims(token)
        except TokenError:
            malformed.append(token)
            continue
        if callable(accept) and accept(claims):
            return token
        if normalize_audience(str(claims.get("aud") or "")) == want:
            return token
        skill = str(claims.get("acps_skill_id") or "")
        if skill.startswith("ws."):
            continue
        foreign.append(token)
    if foreign:
        return foreign[0]
    return malformed[0] if malformed else None


# ─────────────────── 通用签名/验签底层（域章程与 GrantToken 共用） ───────────────────


def canonical_payload(payload: Mapping[str, Any]) -> bytes:
    """规范字节串：多签与验签都以它为准（sort_keys + 紧凑分隔符 + UTF-8）。"""
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8")


def sign_payload(signing: SigningContext, payload: Mapping[str, Any], *, typ: str = "JWT") -> str:
    """对任意 JSON payload 签一条 JWS（header.x5c 只带 leaf，验签方走 trust-bundle）。"""
    header = {
        "alg": TOKEN_ALG,
        "typ": typ,
        "x5c": [_b64_std_encode(signing.leaf.public_bytes(serialization.Encoding.DER))],
    }
    header_segment = _b64u_encode(json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    payload_segment = _b64u_encode(canonical_payload(payload))
    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    return f"{header_segment}.{payload_segment}.{_b64u_encode(signing.private_key.sign(signing_input))}"


def verify_payload_jws(
    jws: str,
    *,
    trust_bundle: Path | str,
    now: float | None = None,
) -> tuple[dict[str, Any], str]:
    """验一条 sign_payload 产的 JWS：链 → 叶子公钥类型 → 签名 → 有效期。

    返回 (payload, 签名者叶子 AIC)。任何失败抛 TokenError。不做 claims 语义校验
    （那是 grant/manifest 各自画像的事）——这是 grant.py / domain_manifest.py 的共用底层。
    """
    checked_at = float(now if now is not None else time.time())
    header, payload, signature, signing_input = _parse_token(jws)
    if header.get("alg") != TOKEN_ALG:
        raise TokenError("unsupported-alg", str(header.get("alg")))
    x5c = header.get("x5c")
    if not isinstance(x5c, list) or not x5c:
        raise TokenError("missing-x5c", "x5c must be a non-empty array")
    try:
        chain = tuple(x509.load_der_x509_certificate(base64.b64decode(entry)) for entry in x5c)
    except Exception as exc:  # noqa: BLE001
        raise TokenError("bad-x5c", f"{type(exc).__name__}") from exc

    anchors = load_certificates(trust_bundle)
    _verify_chain(chain, anchors, now=checked_at)
    leaf = chain[0]
    leaf_key = leaf.public_key()
    if not isinstance(leaf_key, ed25519.Ed25519PublicKey):
        raise TokenError("unsupported-key", f"x5c leaf must be Ed25519, got {type(leaf_key).__name__}")
    try:
        leaf_key.verify(signature, signing_input)
    except InvalidSignature as exc:
        raise TokenError("bad-signature", "x5c leaf does not match payload signature") from exc
    if not isinstance(payload, dict):
        raise TokenError("malformed", "payload must be a JSON object")
    return payload, aic_from_certificate(leaf)
