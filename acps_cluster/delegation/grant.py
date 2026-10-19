"""GrantToken（域级授权凭证）：task 级 delegation 之上的"根授权信封"画像。

两画像一内核（共享 token.py 的 Ed25519/x5c/验签底层，claim 形态不同）：
- delegation（已有）：task 级、act 链、≤10min、jti 一次性——"这一次行为"；
- grant（本模块）：域级、**无 act 链**（直接授予）、天/月级、`acps_max_uses` 配额
  （使用侧计数，sqlite 原子自增）——"这一种资格"（有范围的持续许可，非 agent 整把钥匙）。

受众模型：grant 的 `aud` = **持有者自身**（canonical(subject)）——"这枚凭证只能由 sub
本人使用"，主体级绑定（D3a 盗用拒绝）。资产范围用 `acps_assets` 表达（可覆盖域内多个
provider 的资产——双资产一凭证）。PEP 侧对 grant 走独立的 verify 入口（不复用
verify_token 的 aud==本机 检查，那是对 delegation 画像的语义）。

嵌套（D6）：leader 持 grant 为伙伴派生 task token 时，**必须继承** `acps_domain` /
`acps_domain_revision`，且 exp ≤ 父 grant 的 exp、skill ∈ 父资产集——否则冻结会被
"洗白"绕过（派生不带域 claim → 掉回白名单路径），这是域状态穿透修复的签发侧一半。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .token import (
    SigningContext,
    TokenError,
    _agent_uri,
    canonical_audience,
    sign_payload,
    verify_payload_jws,
)

GRANT_SCOPE_PREFIX = "acps.domain.grant:"
GRANT_MAX_TTL_SECONDS = 30 * 24 * 3600  # 订阅级上限 30 天（域章程的 validUntil 另有约束）
DEFAULT_GRANT_TTL_SECONDS = 7 * 24 * 3600
DEFAULT_MAX_USES = 1

_ASSET_KEYS = ("providerAic", "skill")


@dataclass(frozen=True)
class IssuedGrant:
    token: str  # JWS 原文（只交给代码侧/落 local_context，不进模型上下文）
    claims: dict[str, Any]

    @property
    def jti(self) -> str:
        return str(self.claims["jti"])

    @property
    def expires_at(self) -> int:
        return int(self.claims["exp"])

    @property
    def domain(self) -> str:
        return str(self.claims["acps_domain"])

    @property
    def subject_aic(self) -> str:
        return str(self.claims["sub"]).removeprefix("agent:")

    @property
    def assets(self) -> tuple[tuple[str, str], ...]:
        return tuple((str(a.get("providerAic")), str(a.get("skill"))) for a in self.claims.get("acps_assets") or ())

    def handle(self) -> dict[str, Any]:
        """给模型/审计的句柄（无 JWS 原文）。"""
        return {
            "kind": "grant",
            "jti": self.jti,
            "domain": self.domain,
            "revision": self.claims.get("acps_domain_revision"),
            "subject": self.subject_aic,
            "assets": [f"{p}:{s}" for p, s in self.assets],
            "maxUses": self.claims.get("acps_max_uses"),
            "expiresAt": self.expires_at,
        }


def issue_grant(
    signing: SigningContext,
    *,
    domain: str,
    domain_revision: int,
    subject_aic: str,
    assets: Sequence[Mapping[str, str]],
    max_uses: int = DEFAULT_MAX_USES,
    ttl_seconds: int = DEFAULT_GRANT_TTL_SECONDS,
    plan: str | None = None,
    not_after: int | None = None,
    now: float | None = None,
) -> IssuedGrant:
    """签发一枚域级 grant。assets: [{providerAic, skill}]（应为域章程内资产）。"""
    if ttl_seconds <= 0 or ttl_seconds > GRANT_MAX_TTL_SECONDS:
        raise TokenError("invalid-ttl", f"grant ttl 必须在 1..{GRANT_MAX_TTL_SECONDS}s 内")
    if max_uses < 1:
        raise TokenError("invalid-max-uses", "acps_max_uses 必须 ≥ 1")
    if not assets:
        raise TokenError("empty-assets", "grant 至少授权一项资产")
    normalized_assets = []
    for asset in assets:
        if any(not str(asset.get(k) or "") for k in _ASSET_KEYS):
            raise TokenError("bad-asset", f"资产条目缺字段: {asset!r}")
        normalized_assets.append({k: str(asset[k]) for k in _ASSET_KEYS})

    issued_at = int(now if now is not None else time.time())
    expires_at = min(issued_at + int(ttl_seconds), int(not_after)) if not_after else issued_at + int(ttl_seconds)
    claims: dict[str, Any] = {
        "iss": _agent_uri(signing.issuer_aic),
        "sub": _agent_uri(subject_aic),
        "aud": canonical_audience(subject_aic),  # 受众=持有者：只能由 sub 本人使用（D3a）
        "exp": expires_at,
        "iat": issued_at,
        "jti": str(uuid.uuid4()),
        "scope": f"{GRANT_SCOPE_PREFIX}{domain}",
        "acps_subject_type": "agent",
        "acps_domain": domain,
        "acps_domain_revision": int(domain_revision),
        "acps_assets": normalized_assets,
        "acps_max_uses": int(max_uses),
    }
    if plan:
        claims["acps_grant_plan"] = str(plan)
    return IssuedGrant(token=sign_payload(signing, claims, typ="GRANT"), claims=claims)


def is_grant_payload(claims: Mapping[str, Any]) -> bool:
    return str(claims.get("scope") or "").startswith(GRANT_SCOPE_PREFIX)


def verify_grant(
    token: str,
    *,
    trust_bundle: Path | str,
    now: float | None = None,
    clock_skew_seconds: int = 60,
) -> dict[str, Any]:
    """grant 画像的验签入口：链/签名/有效期/必需 claims/画像一致性。

    语义裁决（主体绑定/成员角色/配额/域状态）在 pep.py 的域路径里做——这里只管密码学
    与结构有效性。任何失败抛 TokenError。
    """
    claims, signer = verify_payload_jws(token, trust_bundle=trust_bundle, now=now)
    checked_at = float(now if now is not None else time.time())
    if not is_grant_payload(claims):
        raise TokenError("not-a-grant", "scope 不属于 grant 画像")
    for name in ("iss", "sub", "aud", "exp", "iat", "jti", "acps_domain", "acps_assets", "acps_max_uses"):
        if name not in claims:
            raise TokenError("missing-claim", name)
    # 签名者与签发者一致（同 delegation 的修复一：防"别人替 iss 签"）
    from .token import normalize_audience

    if normalize_audience(str(claims.get("iss") or "")) != signer:
        raise TokenError("issuer-mismatch", "iss does not match x5c leaf certificate AIC")
    try:
        expires_at, issued_at = int(claims["exp"]), int(claims["iat"])
    except (TypeError, ValueError) as exc:
        raise TokenError("malformed", "exp/iat must be integers") from exc
    if checked_at > expires_at + clock_skew_seconds:
        raise TokenError("expired", f"exp={expires_at}")
    if issued_at > checked_at + clock_skew_seconds:
        raise TokenError("not-yet-valid", f"iat={issued_at}")
    if expires_at - issued_at > GRANT_MAX_TTL_SECONDS + clock_skew_seconds:
        raise TokenError("ttl-too-long", f"ttl={expires_at - issued_at}")
    if int(claims.get("acps_max_uses") or 0) < 1:
        raise TokenError("invalid-max-uses", "acps_max_uses 必须 ≥ 1")
    return claims


def derive_delegation_claims(grant_claims: Mapping[str, Any], *, skill_id: str) -> dict[str, Any]:
    """从父 grant 提取派生 task token 必须继承的 claims（签发侧防"洗白"）。"""
    assets = {(str(a.get("providerAic")), str(a.get("skill"))) for a in grant_claims.get("acps_assets") or ()}
    if skill_id not in {skill for _, skill in assets}:
        raise TokenError(
            "derive-out-of-scope",
            f"派生 skill {skill_id!r} 不在父 grant 资产集内（收窄规则：子授权 ≤ 父授权）",
        )
    return {
        "acps_domain": str(grant_claims["acps_domain"]),
        "acps_domain_revision": int(grant_claims.get("acps_domain_revision") or 1),
    }
