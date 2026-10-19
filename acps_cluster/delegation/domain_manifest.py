"""DomainManifest（域章程）：把"机构间的授权关系"从环境变量升级为协议对象（CDAP 瘦身版）。

定位（见 PEP-result.md 第九轮的边界论证）：
- 承重墙是 grant 画像 + 成员/角色裁决 + 双配额 + 对账；域实体做"薄"——一份**双签的
  策略文件 + 生命周期**，不建服务。MVP 形态：各 partner 工作区 acps/domains/*.json，
  与 trust-bundle 同款本地持有；将来可挪进 registry-server（PDP 只依赖"给我一个策略源"）。

新鲜性（revision，防"签名保完整性不保新鲜性"）：
- 每份章程带 revision + issuedAt；同 domainId 多版本时取最高 revision；
- **revision 提升沿用创建时的签名法定人数**（双签）——修订权 = 创建权，防单方 bump
  驱逐/拉入成员；
- 裁决永远按本地最高 revision；token 携带的 acps_domain_revision 只做审计线索。

生命周期：status = active | frozen。冻结 = 域内急停（该域一切授权链路即时死亡，
含已派生的 task token——PEP 在验签前查域状态，见 pep.py 的 token 分支最外层）。
边界：冻结杀死的是"域派生的授权"，不是 leader 的固有白名单权限（那是 provider 自有
配置，全局急停 = 从 env 白名单摘除，即时生效）。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .token import (
    SigningContext,
    TokenError,
    canonical_payload,
    sign_payload,
    verify_payload_jws,
)

MANIFEST_SIGNATURES_KEY = "signatures"
MIN_SIGNATURES = 2  # 双签：建域与修订同法定人数（修订权 = 创建权）


@dataclass(frozen=True)
class DomainPolicy:
    """PEP 消费的域策略视图（从最高 revision 的 manifest 解析）。"""

    domain_id: str
    revision: int
    status: str  # active | frozen
    members: Mapping[str, str]  # aic -> role（provider/consumer/auditor）
    issuers: frozenset[str]
    assets: Mapping[tuple[str, str], frozenset[str]]  # (provider_aic, skill) -> 允许的角色集合
    max_grants_per_day: int  # 签发侧配额（计数器在 issuer 侧）
    issued_at: str
    signer_aics: frozenset[str]

    def member_role(self, aic: str) -> str | None:
        return self.members.get(aic)

    def asset_allows(self, *, provider_aic: str, skill_id: str, role: str) -> bool:
        allowed = self.assets.get((provider_aic, skill_id))
        return allowed is not None and role in allowed

    def describes(self) -> dict[str, Any]:
        return {
            "domainId": self.domain_id,
            "revision": self.revision,
            "status": self.status,
            "members": dict(self.members),
            "issuers": sorted(self.issuers),
            "assets": [f"{p}:{s}->{sorted(r)}" for (p, s), r in sorted(self.assets.items())],
            "maxGrantsPerDay": self.max_grants_per_day,
            "signers": sorted(self.signer_aics),
        }


def build_manifest(
    *,
    domain_id: str,
    members: Sequence[Mapping[str, str]],
    assets: Sequence[Mapping[str, Any]],
    issuers: Sequence[str],
    revision: int = 1,
    status: str = "active",
    max_grants_per_day: int = 50,
    valid_until: str | None = None,
) -> dict[str, Any]:
    """构造一份待签章程（不含 signatures）。members: [{aic, role}]；
    assets: [{providerAic, skill, allowedMembers(角色列表)}]。"""
    if status not in {"active", "frozen"}:
        raise ValueError(f"status 只能是 active|frozen，收到 {status!r}")
    member_map: dict[str, str] = {}
    for member in members:
        aic, role = str(member.get("aic") or ""), str(member.get("role") or "")
        if not aic or role not in {"provider", "consumer", "auditor"}:
            raise ValueError(f"成员条目不合法: {member!r}")
        member_map[aic] = role
    for issuer in issuers:
        if issuer not in member_map:
            raise ValueError(f"签发者 {issuer} 必须是域成员")
    for asset in assets:
        provider = str(asset.get("providerAic") or "")
        if provider not in member_map or member_map[provider] != "provider":
            raise ValueError(f"资产提供方 {provider} 必须是 provider 角色成员")
    manifest: dict[str, Any] = {
        "domainId": domain_id,
        "revision": int(revision),
        "issuedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": status,
        "validUntil": valid_until,
        "members": [{"aic": aic, "role": role} for aic, role in member_map.items()],
        "assets": [dict(asset) for asset in assets],
        "issuers": list(issuers),
        "maxGrantsPerDay": int(max_grants_per_day),
    }
    return manifest


def sign_manifest(manifest: Mapping[str, Any], signing: SigningContext) -> dict[str, Any]:
    """就地附加一条签名（同一方可覆盖自己的签名），返回更新后的 manifest。"""
    body = {k: v for k, v in manifest.items() if k != MANIFEST_SIGNATURES_KEY}
    jws = sign_payload(signing, body, typ="DOMAIN")
    signatures = [
        old
        for old in (manifest.get(MANIFEST_SIGNATURES_KEY) or [])
        if isinstance(old, str) and _signer_of(old, signing) != signing.issuer_aic
    ]
    signatures.append(jws)
    return {**body, MANIFEST_SIGNATURES_KEY: signatures}


def _signer_of(jws: str, signing: SigningContext) -> str | None:
    """本地自校验快捷路径：用本地链验签自己刚产的 JWS 拿签名者 AIC（失败返回 None）。"""
    try:
        # 本地无 trust-bundle 也能验：直接用 signing.leaf 公钥
        from .token import _parse_token  # noqa: PLC0415

        header, payload, signature, signing_input = _parse_token(jws)
        import base64 as _b64
        from cryptography import x509 as _x509

        leaf = _x509.load_der_x509_certificate(_b64.b64decode(header["x5c"][0]))
        leaf.public_key().verify(signature, signing_input)
        from .token import aic_from_certificate

        return aic_from_certificate(leaf)
    except Exception:  # noqa: BLE001
        return None


def verify_manifest(
    manifest: Mapping[str, Any],
    *,
    trust_bundle: Path | str,
    now: float | None = None,
) -> tuple[dict[str, Any], frozenset[str]]:
    """验证章程：每条签名 = 链+验签+payload 与正文逐字节一致；签名者不重复；达到法定人数。

    返回 (无签名的正文, 签名者 AIC 集合)。任何失败抛 TokenError（fail-closed）。
    """
    body = {k: v for k, v in manifest.items() if k != MANIFEST_SIGNATURES_KEY}
    canonical = canonical_payload(body)
    signers: set[str] = set()
    for jws in manifest.get(MANIFEST_SIGNATURES_KEY) or []:
        if not isinstance(jws, str):
            raise TokenError("manifest-bad-signature", "signature entry must be a JWS string")
        payload, signer = verify_payload_jws(jws, trust_bundle=trust_bundle, now=now)
        if canonical_payload(payload) != canonical:
            raise TokenError("manifest-content-mismatch", "签名对应的正文与当前章程不一致（被篡改或张冠李戴）")
        signers.add(signer)
    if len(signers) < MIN_SIGNATURES:
        raise TokenError(
            "manifest-undercount-signed",
            f"章程需要至少 {MIN_SIGNATURES} 位不同签名者（双签），当前 {len(signers)}",
        )
    # 2026-10-06 审查 #6:签名者必须是域的当事方(members ∪ issuers)——
    # 否则两个自注册身份即可双签一份把自己列为 issuer+资产的章程
    members = body.get("members") or []
    if not isinstance(members, (list, tuple)) or any(not isinstance(m, Mapping) for m in members):
        raise TokenError("manifest-bad-shape", "members must be an array of objects")
    issuers = body.get("issuers") or []
    if not isinstance(issuers, (list, tuple)) or any(not isinstance(i, str) for i in issuers):
        raise TokenError("manifest-bad-shape", "issuers must be an array of strings")
    parties = {str(m.get("aic") or "") for m in members}
    parties |= set(issuers)
    parties.discard("")
    strangers = signers - parties
    if strangers:
        raise TokenError(
            "manifest-signer-not-party",
            f"签名者不是域成员/签发者: {sorted(strangers)}",
        )
    return body, frozenset(signers)


def to_policy(manifest: Mapping[str, Any], signer_aics: frozenset[str]) -> DomainPolicy:
    """把已验证的章程解析成 PDP 视图（成员/资产/签发者索引）。"""
    member_rows = manifest.get("members") or []
    asset_rows = manifest.get("assets") or []
    if not isinstance(member_rows, (list, tuple)) or any(
        not isinstance(m, Mapping) for m in member_rows
    ):
        raise TokenError("manifest-bad-shape", "members must be an array of objects")
    if not isinstance(asset_rows, (list, tuple)) or any(
        not isinstance(a, Mapping) for a in asset_rows
    ):
        raise TokenError("manifest-bad-shape", "assets must be an array of objects")
    issuer_rows = manifest.get("issuers") or []
    if not isinstance(issuer_rows, (list, tuple)) or any(
        not isinstance(i, str) for i in issuer_rows
    ):
        raise TokenError("manifest-bad-shape", "issuers must be an array of strings")

    members = {str(m.get("aic")): str(m.get("role")) for m in member_rows}
    assets: dict[tuple[str, str], frozenset[str]] = {}
    for asset in asset_rows:
        provider = asset.get("providerAic")
        skill = asset.get("skill")
        allowed_raw = asset.get("allowedMembers") or []
        if not isinstance(provider, str) or not isinstance(skill, str):
            raise TokenError("manifest-bad-shape", "asset providerAic/skill must be strings")
        if not isinstance(allowed_raw, (list, tuple)) or any(
            not isinstance(role, str) for role in allowed_raw
        ):
            raise TokenError(
                "manifest-bad-shape",
                f"asset {provider}/{skill} allowedMembers must be an array of strings",
            )
        assets[(provider, skill)] = frozenset(allowed_raw)
    try:
        revision = int(manifest.get("revision") or 1)
        max_grants = int(manifest.get("maxGrantsPerDay") or 50)
    except (TypeError, ValueError) as exc:
        raise TokenError("manifest-bad-shape", f"invalid numeric field: {exc}") from exc
    return DomainPolicy(
        domain_id=str(manifest.get("domainId")),
        revision=revision,
        status=str(manifest.get("status") or "active"),
        members=members,
        issuers=frozenset(issuer_rows),
        assets=assets,
        max_grants_per_day=max_grants,
        issued_at=str(manifest.get("issuedAt") or ""),
        signer_aics=signer_aics,
    )


def _frozen_policy(domain_id: str, revision: int) -> DomainPolicy:
    """毒化域的合成策略:冻结态、无成员、无资产——该域一切 token 被拒(fail-closed)。"""
    return DomainPolicy(domain_id=domain_id, revision=revision, status="frozen",
                        members={}, issuers=frozenset(), assets={},
                        max_grants_per_day=0, issued_at="", signer_aics=frozenset())


def _filename_domain_rev(path: Path) -> tuple[str, int] | None:
    """从 write_manifest 的命名约定 {domainId}.r{rev}.json 解析归属。"""
    import re as _re
    m = _re.fullmatch(r"(.+)\.r(\d+)\.json", path.name)
    return (m.group(1), int(m.group(2))) if m else None


def parse_expiry(value: Any) -> float | None:
    """把"到期时间"解析成 epoch 秒；None 表示不设到期，抛 ValueError 表示写法不认识。

    接受 `…Z`、带偏移的 ISO、unix 秒三种写法（域章程 validUntil 与付费计划 expiresAt 共用）。
    注意时区：Z 串是 UTC，必须显式按 UTC 解释——早先 `strptime(...).timestamp()` 把它按
    **本地时区**读，UTC+8 机器上整域被提前 8 小时判过期（2026-10-07 审查修复）。
    """
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():  # 与 commerce.PLAN_TEMPLATE 同规格：允许 unix 秒
        return float(text)
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        pass
    try:  # 带偏移的 ISO（如 2027-01-01T00:00:00+08:00）
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError as exc:
        raise ValueError(f"无法解析 validUntil={value!r}") from exc


def load_domain_policies(
    directory: Path | str,
    *,
    trust_bundle: Path | str,
    now: float | None = None,
) -> dict[str, DomainPolicy]:
    """加载目录下所有章程：验签 → 按 domainId 取最高 revision（高版本取代低版本）。"""
    root = Path(directory)
    if not root.is_dir():
        return {}
    now_ts = now if now is not None else time.time()
    groups: dict[str, list[tuple[int, Path]]] = {}
    for path in sorted(root.glob("*.json")):
        dr = _filename_domain_rev(path)
        if dr is None:
            continue
        domain_id, rev = dr
        groups.setdefault(domain_id, []).append((rev, path))
    best: dict[str, tuple[int, DomainPolicy]] = {}
    for domain_id, files in groups.items():
        files.sort(key=lambda t: t[0], reverse=True)
        top_rev, top_path = files[0]
        loaded = None
        try:
            manifest = json.loads(top_path.read_text(encoding="utf-8"))
            body, signers = verify_manifest(manifest, trust_bundle=trust_bundle, now=now_ts)
            if body.get("status") == "frozen":
                loaded = _frozen_policy(domain_id, top_rev)
            else:
                policy = to_policy(body, signers)
                try:
                    until = parse_expiry(body.get("validUntil"))
                except ValueError as exc:
                    print(f"[domain] 警告: {domain_id}.r{top_rev} {exc}——按过期毒化处理(fail-closed)",
                          file=sys.stderr, flush=True)
                    raise
                if until is not None and until < now_ts:
                    loaded = _frozen_policy(domain_id, top_rev)  # 过期视同毒化(审计 #4)
                else:
                    loaded = policy
        except Exception as exc:  # noqa: BLE001 — 单域坏章程只毒化该域，不能拖垮整目录
            print(
                f"[domain] 警告: {domain_id}.r{top_rev} 加载失败({type(exc).__name__}: {exc})"
                "——按毒化处理(fail-closed)",
                file=sys.stderr,
                flush=True,
            )
        if loaded is None:
            # 最高 revision 验不过/过期:毒化——**不回落更低 revision 的 active**
            # (否则冻结公告损坏一个字节=急停失效;审查 #3 的核心语义)
            loaded = _frozen_policy(domain_id, top_rev)
        current = best.get(domain_id)
        if current is None or loaded.revision > current[0]:
            best[domain_id] = (loaded.revision, loaded)
    return {domain: policy for domain, (_, policy) in best.items()}


def export_policy_projection(policy: DomainPolicy) -> dict[str, Any]:
    """把已验证的域策略导出为 discovery-server 的**策略投影** JSON（哑格式）。

    签名验证在导出方（持双签章程的一方）完成；发现服务只做成员判定，不重复实现
    密码学——这是 Phase D 的复用边界（服务器侧零新信任逻辑）。
    """
    return {
        "domainId": policy.domain_id,
        "revision": policy.revision,
        "status": policy.status,
        "members": dict(policy.members),
        "assets": [
            {"providerAic": provider, "skill": skill, "allowedMembers": sorted(roles)}
            for (provider, skill), roles in sorted(policy.assets.items())
        ],
        "exportedFrom": "dual-signed charter (verify_manifest)",
    }


_SAFE_DOMAIN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def write_manifest(manifest: Mapping[str, Any], directory: Path | str) -> Path:
    """落盘（同 domainId+revision 覆盖，供冻结/修订分发）。"""
    domain_id = str(manifest.get("domainId") or "")
    if not _SAFE_DOMAIN_ID.fullmatch(domain_id):
        raise ValueError(f"domainId 不合法：{domain_id!r}")
    try:
        revision = int(manifest.get("revision") or 1)
    except (TypeError, ValueError) as exc:
        raise ValueError("revision 必须是整数") from exc
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{domain_id}.r{revision}.json"
    tmp = path.with_suffix(".json.tmp")  # 原子替换:半截文件不能让冻结失效(2026-10-06 审查)
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return path
