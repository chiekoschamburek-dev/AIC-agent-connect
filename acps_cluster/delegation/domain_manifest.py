"""DomainManifest（域章程）：把"机构间的授权关系"从环境变量升级为协议对象（CDAP 瘦身版）。

定位（见 PEP-result.md 第九轮的边界论证）：
- 承重墙是 grant 画像 + 成员/角色裁决 + 双配额 + 对账；域实体做"薄"——一份**双签的
  策略文件 + 生命周期**，不建服务。MVP 形态：各 partner 工作区 acps/domains/*.json，
  与 trust-bundle 同款本地持有；将来可挪进 registry-server（PDP 只依赖"给我一个策略源"）。

新鲜性（revision，防"签名保完整性不保新鲜性"）：
- 每份章程带 revision + issuedAt；同 domainId 多版本时取**可验证的最高 revision**；
- **revision 提升沿用创建时的签名法定人数**（双签）——修订权 = 创建权，防单方 bump
  驱逐/拉入成员；
- **修订连续性**：r>1 的章程必须声明 previousRevision/previousDigest，且必须由上一版
  至少 MIN_CONTINUITY_SIGNERS 位签署方连署。只追加一个自建成员再双签，不能接管域。
  签署方自身要退出时，需在该修订里保留其当事方身份（例如转 auditor）完成连署，
  下一版再移除；否则其签名会被 manifest-signer-not-party 拒绝（双签治理的已知代价）。
- 裁决永远按本地最高 revision；token 携带的 acps_domain_revision 只做审计线索。

可用性与防回滚（2026-10-20 审查补齐）：
- 不是所有"高 revision"都能毒化域。**未通过验签/连续性**的高版本只隔离、不生效；
  否则任何一方丢一个坏 r999 文件就能 DoS 整个域。
- 已验签的最高版本写入**高水位状态 + 签名副本**（默认在 domains 目录之外的
  `.domain-state/`），删除/损坏冻结章程文件不会自动回落到旧 active。
- 只有显式调用 `purge_domain_store()`（管理操作）才会清掉高水位。
- 分发修订时要同时带上**上一版**（或整份 `.domain-state`）；新节点只收到一份
  r2 无法证明修订连续性，会拒绝该版本并保持上一已知状态（fail-closed）。

生命周期：status = active | frozen。冻结 = 域内急停（该域一切授权链路即时死亡，
含已派生的 task token——PEP 在验签前查域状态，见 pep.py 的 token 分支最外层）。
边界：冻结杀死的是"域派生的授权"，不是 leader 的固有白名单权限（那是 provider 自有
配置，全局急停 = 从 env 白名单摘除，即时生效）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
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
MIN_CONTINUITY_SIGNERS = MIN_SIGNATURES  # 新版本必须有多少位上一版签署方连署
PREVIOUS_REVISION_KEY = "previousRevision"
PREVIOUS_DIGEST_KEY = "previousDigest"
_STATE_VERSION = 1
_STATE_PATH_ENV = "ACPS_DOMAIN_STATE_PATH"
_STATE_DIR_ENV = "ACPS_DOMAIN_STATE_DIR"

_WARNED: set[str] = set()


def _warn_once(message: str) -> None:
    if message in _WARNED:
        return
    _WARNED.add(message)
    print(f"[domain] 警告: {message}", file=sys.stderr, flush=True)


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


# ─────────────────────────── 章程正文 / 修订链 ───────────────────────────


def manifest_body(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """去掉签名后的正文（签名与正文逐字节绑定，正文本身不带 signatures 键）。"""
    return {k: v for k, v in manifest.items() if k != MANIFEST_SIGNATURES_KEY}


def manifest_digest(manifest: Mapping[str, Any]) -> str:
    """正文摘要（含 domainId/revision/成员/资产），用于修订链绑定上一版内容。"""
    raw = hashlib.sha256(canonical_payload(manifest_body(manifest))).hexdigest()
    return f"sha256:{raw}"


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
    previous: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """构造一份待签章程（不含 signatures）。members: [{aic, role}]；
    assets: [{providerAic, skill, allowedMembers(角色列表)}]。

    `previous`：上一版**已签名章程**。revision>1 时强烈建议传（生成脚本默认强制），
    用来写入 previousRevision/previousDigest；修订连续性由加载器强制执行。
    """
    if status not in {"active", "frozen"}:
        raise ValueError(f"status 只能是 active|frozen，收到 {status!r}")
    try:
        revision = int(revision)
    except (TypeError, ValueError) as exc:
        raise ValueError("revision 必须是整数") from exc
    if revision < 1:
        raise ValueError("revision 必须 ≥ 1")

    previous_revision: int | None = None
    previous_digest: str | None = None
    if previous is not None:
        if revision == 1:
            raise ValueError("初版 revision=1 不能引用上一版")
        previous_revision = _manifest_revision(previous)
        if previous_revision < 1:
            raise ValueError("上一版章程缺合法 revision")
        if revision <= previous_revision:
            raise ValueError(f"新 revision={revision} 必须高于上一版 {previous_revision}")
        previous_digest = manifest_digest(previous)

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
        "revision": revision,
        "issuedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": status,
        "validUntil": valid_until,
        "members": [{"aic": aic, "role": role} for aic, role in member_map.items()],
        "assets": [dict(asset) for asset in assets],
        "issuers": list(issuers),
        "maxGrantsPerDay": int(max_grants_per_day),
        PREVIOUS_REVISION_KEY: previous_revision,
        PREVIOUS_DIGEST_KEY: previous_digest,
    }
    return manifest


def _manifest_revision(manifest: Mapping[str, Any]) -> int:
    try:
        return int(manifest.get("revision") or 0)
    except (TypeError, ValueError) as exc:
        raise TokenError("manifest-bad-shape", "revision must be an integer") from exc


def verify_revision_continuity(
    new_body: Mapping[str, Any],
    new_signers: frozenset[str] | set[str],
    previous_body: Mapping[str, Any],
    previous_signers: frozenset[str] | set[str],
) -> None:
    """校验 r_new 是否合法继承 r_prev：内容摘要绑定 + 上一版签署方连署。"""
    new_rev = _manifest_revision(new_body)
    prev_rev = _manifest_revision(previous_body)
    if new_rev <= prev_rev:
        raise TokenError(
            "manifest-revision-not-newer",
            f"new revision {new_rev} must be > previous {prev_rev}",
        )
    declared_prev = new_body.get(PREVIOUS_REVISION_KEY)
    if declared_prev is None:
        raise TokenError(
            "manifest-missing-continuity",
            f"revision {new_rev} does not declare {PREVIOUS_REVISION_KEY}",
        )
    try:
        declared_prev_int = int(declared_prev)
    except (TypeError, ValueError) as exc:
        raise TokenError("manifest-bad-continuity", "previousRevision must be an integer") from exc
    if declared_prev_int != prev_rev:
        raise TokenError(
            "manifest-previous-revision-mismatch",
            f"declared previousRevision={declared_prev_int}, expected {prev_rev}",
        )
    expected_digest = manifest_digest(previous_body)
    if str(new_body.get(PREVIOUS_DIGEST_KEY) or "") != expected_digest:
        raise TokenError(
            "manifest-previous-digest-mismatch",
            "previousDigest does not match the verified previous charter",
        )
    overlap = frozenset(new_signers) & frozenset(previous_signers)
    if len(overlap) < MIN_CONTINUITY_SIGNERS:
        raise TokenError(
            "manifest-continuity-signers",
            f"need ≥{MIN_CONTINUITY_SIGNERS} previous signers to co-sign revision {new_rev}; "
            f"overlap={len(overlap)}",
        )


def sign_manifest(manifest: Mapping[str, Any], signing: SigningContext) -> dict[str, Any]:
    """就地附加一条签名（同一方可覆盖自己的签名），返回更新后的 manifest。"""
    body = manifest_body(manifest)
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
    这里只做"这一份文件自身"的密码学/结构验证；修订链连续性由
    verify_revision_continuity() 在加载器里按上一版状态执行。
    """
    body = manifest_body(manifest)
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
    for member in members:
        role = str(member.get("role") or "")
        if not str(member.get("aic") or "") or role not in {"provider", "consumer", "auditor"}:
            raise TokenError("manifest-bad-shape", f"成员条目不合法: {dict(member)!r}")
    parties = {str(m.get("aic") or "") for m in members}
    parties |= set(issuers)
    parties.discard("")
    strangers = signers - parties
    if strangers:
        raise TokenError(
            "manifest-signer-not-party",
            f"签名者不是域成员/签发者: {sorted(strangers)}",
        )

    assets = body.get("assets") or []
    if not isinstance(assets, (list, tuple)) or any(not isinstance(a, Mapping) for a in assets):
        raise TokenError("manifest-bad-shape", "assets must be an array of objects")
    for asset in assets:
        provider, skill = asset.get("providerAic"), asset.get("skill")
        allowed = asset.get("allowedMembers") or []
        if not isinstance(provider, str) or not provider or not isinstance(skill, str) or not skill:
            raise TokenError("manifest-bad-shape", "asset providerAic/skill must be non-empty strings")
        if not isinstance(allowed, (list, tuple)) or any(
            not isinstance(role, str) or role not in {"provider", "consumer", "auditor"}
            for role in allowed
        ):
            raise TokenError(
                "manifest-bad-shape",
                f"asset {provider}/{skill} allowedMembers must be roles",
            )
    if str(body.get("status") or "active") not in {"active", "frozen"}:
        raise TokenError("manifest-bad-shape", f"status={body.get('status')!r}")
    if not str(body.get("domainId") or ""):
        raise TokenError("manifest-bad-shape", "domainId must be a non-empty string")
    try:
        int(body.get("maxGrantsPerDay") or 50)
    except (TypeError, ValueError) as exc:
        raise TokenError("manifest-bad-shape", "maxGrantsPerDay must be an integer") from exc

    revision = _manifest_revision(body)
    if revision < 1:
        raise TokenError("manifest-bad-revision", f"revision={revision}")
    previous_revision = body.get(PREVIOUS_REVISION_KEY)
    previous_digest = body.get(PREVIOUS_DIGEST_KEY)
    if previous_revision is not None:
        try:
            previous_revision = int(previous_revision)
        except (TypeError, ValueError) as exc:
            raise TokenError("manifest-bad-continuity", "previousRevision must be an integer") from exc
        if previous_revision < 1 or previous_revision >= revision:
            raise TokenError(
                "manifest-bad-continuity",
                f"previousRevision={previous_revision} must be in 1..{revision - 1}",
            )
    if previous_digest is not None and not isinstance(previous_digest, str):
        raise TokenError("manifest-bad-continuity", "previousDigest must be a string")
    if (previous_revision is None) != (previous_digest is None):
        raise TokenError(
            "manifest-bad-continuity",
            "previousRevision and previousDigest must be provided together",
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
        revision = _manifest_revision(manifest)
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
    m = re.fullmatch(r"(.+)\.r(\d+)\.json", path.name)
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


# ─────────────────────────── 高水位状态 / 签名副本 ───────────────────────────


def domain_state_path(directory: Path | str) -> Path:
    """高水位状态文件路径。默认放在 domains 目录**之外**，防"删章程即回滚"。"""
    explicit = str(os.getenv(_STATE_PATH_ENV) or "").strip()
    if explicit:
        return Path(explicit)
    root = Path(directory).resolve()
    base = str(os.getenv(_STATE_DIR_ENV) or "").strip()
    room = Path(base) if base else root.parent / ".domain-state"
    tag = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
    return room / f"{root.name}-{tag}.json"


def _archive_dir(directory: Path | str) -> Path:
    state = domain_state_path(directory)
    return state.parent / f"{state.stem}.published"


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _read_state(path: Path) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        _warn_once(f"高水位状态 {path} 不可读({type(exc).__name__}: {exc})——按无状态启动")
        return {}
    if not isinstance(data, Mapping) or not isinstance(data.get("domains"), Mapping):
        _warn_once(f"高水位状态 {path} 结构不合法——按无状态启动")
        return {}
    out: dict[str, dict[str, Any]] = {}
    for domain_id, record in data["domains"].items():
        if not isinstance(record, Mapping) or not isinstance(record.get("manifest"), Mapping):
            continue
        try:
            revision = int(record.get("revision"))
        except (TypeError, ValueError):
            continue
        signers_raw = record.get("signers")
        signers = (
            frozenset(str(s) for s in signers_raw)
            if isinstance(signers_raw, (list, tuple))
            else frozenset()
        )
        out[str(domain_id)] = {
            "revision": revision,
            "digest": str(record.get("digest") or ""),
            "signers": signers,
            "manifest": dict(record["manifest"]),
        }
    return out


def _write_state(path: Path, records: Mapping[str, Mapping[str, Any]]) -> None:
    payload = {"version": _STATE_VERSION, "domains": {}}
    for domain_id, record in sorted(records.items()):
        manifest = record.get("manifest")
        if not isinstance(manifest, Mapping):
            continue
        payload["domains"][domain_id] = {
            "revision": int(record["revision"]),
            "digest": str(record.get("digest") or ""),
            "signers": sorted(str(s) for s in (record.get("signers") or ())),
            "manifest": dict(manifest),
        }
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=1))


def _archive_manifest(manifest: Mapping[str, Any], directory: Path | str) -> None:
    """把已发布章程的签名副本放到 domains 之外（写失败只告警，不阻断主文件）。"""
    body = manifest_body(manifest)
    domain_id = str(body.get("domainId") or "")
    if not _SAFE_DOMAIN_ID.fullmatch(domain_id):
        return
    try:
        revision = _manifest_revision(body)
    except TokenError:
        return
    path = _archive_dir(directory) / f"{domain_id}.r{revision}.json"
    text = json.dumps(manifest, ensure_ascii=False, indent=1)
    try:
        if path.is_file() and path.read_text(encoding="utf-8") == text:
            return
        _atomic_write_text(path, text)
    except OSError as exc:
        _warn_once(f"章程副本归档失败 {path}（{type(exc).__name__}: {exc}）")


def purge_domain_store(directory: Path | str, domain_id: str | None = None) -> None:
    """**显式管理操作**：清空高水位与签名副本（测试/灾难恢复用，不是回滚通道）。

    普通地删除/覆盖 domains/*.json **不会**调用这里；正因如此，删除冻结章程文件
    不会让域回落到旧 active。
    """
    state_path = domain_state_path(directory)
    archive = _archive_dir(directory)
    if domain_id is None:
        try:
            state_path.unlink(missing_ok=True)
        except OSError as exc:
            _warn_once(f"清理高水位失败 {state_path}: {exc}")
        shutil.rmtree(archive, ignore_errors=True)
        return
    records = _read_state(state_path)
    records.pop(domain_id, None)
    try:
        if records:
            _write_state(state_path, records)
        else:
            state_path.unlink(missing_ok=True)
    except OSError as exc:
        _warn_once(f"清理高水位失败 {state_path}: {exc}")
    if archive.is_dir():
        for path in archive.glob(f"{domain_id}.r*.json"):
            try:
                path.unlink()
            except OSError:
                pass


def _candidate_paths(directory: Path) -> list[Path]:
    """主目录 + 签名副本目录的候选章程；主目录优先（同 revision 下先读到它）。"""
    main = [
        p for p in sorted(directory.glob("*.json"))
        if not p.name.startswith(".") and _filename_domain_rev(p) is not None
    ]
    archive = _archive_dir(directory)
    backup: list[Path] = []
    if archive.is_dir():
        backup = [
            p for p in sorted(archive.glob("*.json"))
            if not p.name.startswith(".") and _filename_domain_rev(p) is not None
        ]
    return main + backup


def _read_manifest_file(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TokenError("manifest-unreadable", f"{type(exc).__name__}: {exc}") from exc
    if not isinstance(data, Mapping):
        raise TokenError("manifest-unreadable", "top-level JSON must be an object")
    return dict(data)


def _verify_one(
    manifest: Mapping[str, Any],
    *,
    domain_id: str,
    expected_revision: int,
    trust_bundle: Path | str,
    now: float,
) -> tuple[dict[str, Any], frozenset[str]]:
    body, signers = verify_manifest(manifest, trust_bundle=trust_bundle, now=now)
    if str(body.get("domainId") or "") != domain_id:
        raise TokenError(
            "manifest-domain-mismatch",
            f"file claims domain {domain_id!r}, body has {body.get('domainId')!r}",
        )
    if _manifest_revision(body) != expected_revision:
        raise TokenError(
            "manifest-revision-mismatch",
            f"file name revision {expected_revision}, body has {body.get('revision')}",
        )
    return body, signers


def _record_view(
    record: Mapping[str, Any],
    *,
    domain_id: str,
    trust_bundle: Path | str,
    now: float,
) -> dict[str, Any] | None:
    """把高水位记录还原成可裁决视图；签名坏了就保留 revision/digest/signers 做毒化。"""
    revision = int(record["revision"])
    manifest = record.get("manifest")
    base = {
        "revision": revision,
        "digest": str(record.get("digest") or ""),
        "signers": frozenset(record.get("signers") or ()),
        "body": None,
        "manifest": None,
    }
    if not isinstance(manifest, Mapping):
        return base
    try:
        body, signers = _verify_one(
            manifest, domain_id=domain_id, expected_revision=revision,
            trust_bundle=trust_bundle, now=now,
        )
    except Exception as exc:  # noqa: BLE001
        _warn_once(f"{domain_id}.r{revision} 的高水位副本验签失败({type(exc).__name__}: {exc})——按毒化处理")
        return base
    digest = manifest_digest(body)
    base.update({
        "digest": digest,
        "signers": signers,
        "body": body,
        "manifest": dict(manifest),
    })
    return base


def _extends(candidate: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    """candidate 是否满足 current → candidate 的连续修订条件。"""
    candidate_body = candidate.get("body")
    if not isinstance(candidate_body, Mapping):
        return False
    current_body = current.get("body")
    if isinstance(current_body, Mapping):
        try:
            verify_revision_continuity(
                candidate_body, candidate["signers"], current_body, current["signers"]
            )
            return True
        except TokenError:
            return False
    # 高水位副本损坏时仍允许"声明绑定旧 digest + 旧签署方连署"的恢复路径
    if candidate_body.get(PREVIOUS_REVISION_KEY) != current.get("revision"):
        return False
    if str(candidate_body.get(PREVIOUS_DIGEST_KEY) or "") != str(current.get("digest") or ""):
        return False
    return len(candidate["signers"] & current["signers"]) >= MIN_CONTINUITY_SIGNERS


def _legacy_extends(candidate: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    """迁移期兼容：旧脚本生成的 r>1 没有 previous 字段，但仍要求上一版签署方连署。"""
    if candidate.get("body") is None:
        return False
    body = candidate["body"]
    if body.get(PREVIOUS_REVISION_KEY) is not None:
        return False
    if int(candidate["revision"]) <= int(current["revision"]):
        return False
    return len(candidate["signers"] & current["signers"]) >= MIN_CONTINUITY_SIGNERS


def _choose_with_state(
    domain_id: str,
    state: Mapping[str, Any] | None,
    verified: Mapping[int, list[dict[str, Any]]],
    *,
    trust_bundle: Path | str,
    now: float,
) -> dict[str, Any] | None:
    """有高水位时的选择：只允许从高水位向上延伸，低版本一律视为回滚。"""
    if state is None:
        return None
    current: dict[str, Any] = _record_view(
        state, domain_id=domain_id, trust_bundle=trust_bundle, now=now
    ) or {
        "revision": int(state.get("revision") or 0),
        "digest": str(state.get("digest") or ""),
        "signers": frozenset(state.get("signers") or ()),
        "body": None,
        "manifest": None,
    }
    candidates = [
        candidate
        for revision, items in verified.items()
        for candidate in items
        if revision >= int(current["revision"])
    ]
    # 同一 revision 的副本可用于修复"状态里有 digest 但副本坏了"的情况
    for candidate in candidates:
        if int(candidate["revision"]) == int(current["revision"]):
            if candidate["digest"] == current["digest"] and current.get("body") is None:
                current = candidate
            elif candidate["digest"] != current["digest"]:
                _warn_once(
                    f"{domain_id}.r{candidate['revision']} 与高水位 digest 不一致——已隔离（疑似回滚/分叉）"
                )
    higher = sorted(
        (c for c in candidates if int(c["revision"]) > int(current["revision"])),
        key=lambda c: (int(c["revision"]), str(c["digest"])),
    )
    for candidate in higher:
        # legacy（旧脚本没有 previous 字段）仍要求上一版签署方连署；新脚本文件
        # 还额外要求 previousDigest 绑定上一版正文。迁移期两种都接受，但都不接受
        # "两个完全新的身份自签高 revision"。
        if _extends(candidate, current) or _legacy_extends(candidate, current):
            current = candidate
        else:
            _warn_once(
                f"{domain_id}.r{candidate['revision']} 未通过修订连续性——已隔离"
                "（缺上一版签署方连署/摘要不匹配），不改变当前高水位"
            )
    return current


def _choose_bootstrap(
    domain_id: str,
    verified: Mapping[int, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    """无高水位（首次升级/新部署）：从最低可验证版本开始搭链，坏高版本不毒化。"""
    flat = sorted(verified.values(), key=lambda items: (int(items[0]["revision"]), str(items[0]["digest"])))
    if not flat:
        return None
    # 种子：最低 revision 的合法章程；有 previous 字段但找不到上一版时无法证明连续性，
    # 只能作为"孤儿"跳过（旧脚本的 legacy r2 没有字段，仍可当种子迁移）。
    seed: dict[str, Any] | None = None
    for items in flat:
        for candidate in items:
            body = candidate.get("body") or {}
            if body.get(PREVIOUS_REVISION_KEY) is None:
                seed = candidate
                break
        if seed is not None:
            break
    if seed is None:
        return None
    current = seed
    all_candidates = sorted(
        (c for items in flat for c in items),
        key=lambda c: (int(c["revision"]), str(c["digest"])),
    )
    for candidate in all_candidates:
        if int(candidate["revision"]) <= int(current["revision"]):
            continue
        if _extends(candidate, current) or _legacy_extends(candidate, current):
            current = candidate
        else:
            _warn_once(
                f"{domain_id}.r{candidate['revision']} 未通过修订连续性——已隔离"
                "（至少需要上一版签署方连署）"
            )
    return current


def load_domain_policies(
    directory: Path | str,
    *,
    trust_bundle: Path | str,
    now: float | None = None,
    state_path: Path | str | None = None,
) -> dict[str, DomainPolicy]:
    """加载目录下所有章程：验签 → 修订链/高水位裁决 → 取可验证的最高 revision。

    与旧行为的两处关键差异（2026-10-20）：
    1. **坏的高 revision 不再毒化整个域**：未验签/未通过连续性的候选被隔离，
       继续使用高水位（或首次加载时最高的可验证版本）。只有"完全没有可验证版本"
       才合成 frozen。
    2. **高水位持久化**：已验签版本 + 签名副本放在 domains 之外；删除冻结文件
       不会回落到旧 active。需要显式管理操作时用 purge_domain_store()。
    """
    root = Path(directory)
    if not root.is_dir():
        # 目录缺失不是"没有域"的同义词：调用方若声明了域技能，PEP 会按
        # domain-store-missing fail-closed。这里只留一条去重告警，避免 5s 轮询刷屏。
        _warn_once(f"章程目录 {root} 不存在——按空策略处理；声明域技能的能力将被 PEP 拒绝")
        return {}
    now_ts = float(now if now is not None else time.time())
    state_file = Path(state_path) if state_path is not None else domain_state_path(root)
    state_records = _read_state(state_file)

    groups: dict[str, dict[int, list[Path]]] = {}
    for path in _candidate_paths(root):
        parsed = _filename_domain_rev(path)
        if parsed is None:
            continue
        domain_id, revision = parsed
        groups.setdefault(domain_id, {}).setdefault(revision, []).append(path)

    all_domains = sorted(set(groups) | set(state_records))
    best: dict[str, DomainPolicy] = {}
    next_state = dict(state_records)
    state_changed = False

    for domain_id in all_domains:
        verified: dict[int, list[dict[str, Any]]] = {}
        for revision, paths in sorted(groups.get(domain_id, {}).items()):
            valid: dict[str, dict[str, Any]] = {}
            for path in paths:
                try:
                    manifest = _read_manifest_file(path)
                    body, signers = _verify_one(
                        manifest,
                        domain_id=domain_id,
                        expected_revision=revision,
                        trust_bundle=trust_bundle,
                        now=now_ts,
                    )
                except Exception as exc:  # noqa: BLE001 — 单文件坏只隔离该文件
                    _warn_once(
                        f"{domain_id}.r{revision} 候选 {path.name} 验签失败"
                        f"({type(exc).__name__}: {exc})——已隔离，不毒化域"
                    )
                    continue
                digest = manifest_digest(body)
                valid.setdefault(digest, {
                    "revision": revision,
                    "digest": digest,
                    "signers": signers,
                    "body": body,
                    "manifest": dict(manifest),
                    "path": path,
                })
            if len(valid) > 1:
                _warn_once(
                    f"{domain_id}.r{revision} 存在 {len(valid)} 份内容不同但都验签通过的章程"
                    "——该 revision 已隔离（分叉）"
                )
                continue
            if valid:
                verified[revision] = list(valid.values())

        state_record = state_records.get(domain_id)
        current = (
            _choose_with_state(
                domain_id, state_record, verified,
                trust_bundle=trust_bundle, now=now_ts,
            )
            if state_record is not None
            else _choose_bootstrap(domain_id, verified)
        )

        if current is None:
            # 没有任何可验证章程：若目录里确有候选，合成 frozen（该域本就不该可用）
            if groups.get(domain_id):
                top_rev = max(groups[domain_id])
                _warn_once(f"{domain_id} 没有任何可验证章程——按毒化处理(fail-closed)")
                best[domain_id] = _frozen_policy(domain_id, top_rev)
            elif state_record is not None:
                best[domain_id] = _frozen_policy(domain_id, int(state_record["revision"]))
            continue

        body = current.get("body")
        revision = int(current["revision"])
        if not isinstance(body, Mapping):
            # 高水位元数据在、但签名副本坏了：冻结在已知最高版本，不回落
            _warn_once(f"{domain_id}.r{revision} 高水位副本不可用——按毒化处理(fail-closed)")
            best[domain_id] = _frozen_policy(domain_id, revision)
        else:
            try:
                until = parse_expiry(body.get("validUntil"))
            except ValueError as exc:
                _warn_once(f"{domain_id}.r{revision} {exc}——按过期毒化处理(fail-closed)")
                until = now_ts - 1
            if body.get("status") == "frozen":
                best[domain_id] = _frozen_policy(domain_id, revision)
            elif until is not None and until < now_ts:
                best[domain_id] = _frozen_policy(domain_id, revision)  # 过期视同毒化(审计 #4)
            else:
                try:
                    best[domain_id] = to_policy(body, current["signers"])
                except Exception as exc:  # noqa: BLE001 — 结构异常只毒化该域
                    _warn_once(
                        f"{domain_id}.r{revision} 解析策略失败"
                        f"({type(exc).__name__}: {exc})——按毒化处理(fail-closed)"
                    )
                    best[domain_id] = _frozen_policy(domain_id, revision)

            record = {
                "revision": revision,
                "digest": current["digest"],
                "signers": current["signers"],
                "manifest": current["manifest"],
            }
            old = state_records.get(domain_id)
            if (
                old is None
                or int(old.get("revision") or 0) != revision
                or str(old.get("digest") or "") != str(record["digest"])
                or old.get("manifest") != record["manifest"]
            ):
                next_state[domain_id] = record
                state_changed = True

    if state_changed:
        try:
            _write_state(state_file, next_state)
        except OSError as exc:  # noqa: PERF203
            _warn_once(f"高水位状态落盘失败 {state_file}（{type(exc).__name__}: {exc}）")

    return best


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
    """落盘（同 domainId+revision 覆盖，供冻结/修订分发）。

    同时在 domains 目录之外的 `.domain-state/*.published/` 留一份签名副本：
    主文件被删/损坏时，冻结仍然有效（2026-10-20 防回滚）。
    """
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
    # 原子替换:半截文件不能让冻结失效(2026-10-06 审查)
    _atomic_write_text(path, json.dumps(manifest, ensure_ascii=False, indent=1))
    _archive_manifest(manifest, root)
    return path
