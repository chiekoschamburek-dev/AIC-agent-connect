"""入口域章程与 grant 的签发/联签/安装工具（多入口编排 P2）。

为什么需要两步：`domain_manifest` 的章程要求**双签**（两位不同签署方），而
supervisor 与入口 Partner 通常不在同一台机器。所以流程拆成：

    supervisor 侧  prepare  → 产出只带 1 个签名的 draft
    入口 Partner 侧 cosign   → 校验 supervisor 签名后补自己的签名（2/2）
    supervisor 侧  install  → 验签 + 写 ws-supervisor/acps/domains/
    supervisor 侧  issue    → 给入口 Partner 签 grant（资产只含 research.submit/status）
    双方各自        verify   → 离线核验 grant 与章程是否对得上

同一台机器上（自证：supervisor ⇄ kb）可以用 `mint` 一次做完。

边界：
- 本工具只读/写 `acps/` 下的证书、章程与 grant 文件；不改平台注册、不改 ACS；
- grant 的 JWS 只写进文件（0600），不打印、不进日志；
- 章程正文一旦签发不可改（签名与正文逐字节绑定）；加资产 = 出 r2 并让上一版签署方连署
  （`--previous` 传上一版文件；本工具不绕过连续性检查）。

用法见 docs/multi-entry-p2-grant.md。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from acps_cluster.delegation.domain_manifest import (
    build_manifest,
    load_domain_policies,
    manifest_body,
    parse_expiry,
    sign_manifest,
    to_policy,
    verify_manifest,
    write_manifest,
)
from acps_cluster.delegation.grant import (
    GRANT_MAX_TTL_SECONDS,
    IssuedGrant,
    issue_grant,
    verify_grant,
)
from acps_cluster.delegation.token import (
    SigningContext,
    TokenError,
    canonical_payload,
    load_signing_context,
    normalize_audience,
    verify_payload_jws,
)

ENTRY_SKILLS: tuple[str, ...] = ("research.submit", "research.status")
DEFAULT_DOMAIN = "yanshu-entry"
GRANT_SCHEMA = "yanshu.entry-grant/1"
DEFAULT_MAX_USES = 200
DEFAULT_TTL_DAYS = 7
DEFAULT_VALID_DAYS = 90


# ─────────────────────────── 基础 ───────────────────────────


def _acps(workspace: str | Path) -> Path:
    return Path(workspace).resolve() / "acps"


def trust_bundle(workspace: str | Path) -> Path:
    path = _acps(workspace) / "trust-bundle.pem"
    if not path.is_file():
        raise FileNotFoundError(f"缺 trust-bundle：{path}")
    return path


def signing_context(workspace: str | Path) -> SigningContext:
    ctx = load_signing_context(_acps(workspace) / "client", trust_bundle=trust_bundle(workspace))
    if not ctx.issuer_aic:
        raise ValueError("证书里没有 AIC")
    return ctx


def workspace_aic(workspace: str | Path) -> str:
    return signing_context(workspace).issuer_aic


def _skills(value: str | Sequence[str]) -> tuple[str, ...]:
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",") if part.strip()]
    else:
        items = [str(part).strip() for part in value if str(part).strip()]
    return tuple(items) or ENTRY_SKILLS


#: 公开别名：给 p2_selftest 等调用方复用（避免 import 私有名）
parse_skills = _skills


def _valid_until(days: int) -> str:
    moment = datetime.now(timezone.utc) + timedelta(days=max(1, int(days)))
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_entry_manifest(
    *,
    domain: str,
    supervisor_aic: str,
    peer_aic: str,
    revision: int = 1,
    valid_days: int = DEFAULT_VALID_DAYS,
    skills: Sequence[str] = ENTRY_SKILLS,
    max_grants_per_day: int = 50,
    previous: Mapping[str, Any] | None = None,
    extra_consumers: Sequence[str] = (),
) -> dict[str, Any]:
    """构造入口域章程正文：supervisor=provider、入口=consumer、资产=两项入口技能。

    `extra_consumers`：**预先列入**、但本次不需要签名的其他入口（consumer）。
    典型用法：同机自证用 kb 双签，同时把问迹 AIC 写进 r1——这样问迹的 grant 不用等 r2。
    """
    if not supervisor_aic or not peer_aic:
        raise ValueError("supervisor_aic / peer_aic 都不能为空")
    if normalize_audience(supervisor_aic) == normalize_audience(peer_aic):
        raise ValueError("supervisor 与入口 Partner 不能是同一个 AIC")
    consumers = [peer_aic]
    for aic in extra_consumers:
        text = str(aic or "").strip()
        if text and normalize_audience(text) not in {normalize_audience(a) for a in consumers}:
            consumers.append(text)
    assets = [
        {
            "providerAic": supervisor_aic,
            "skill": skill,
            "allowedMembers": ["consumer"],
        }
        for skill in _skills(skills)
    ]
    return build_manifest(
        domain_id=domain,
        members=[
            {"aic": supervisor_aic, "role": "provider"},
            *({"aic": aic, "role": "consumer"} for aic in consumers),
        ],
        assets=assets,
        issuers=[supervisor_aic],
        revision=int(revision),
        valid_until=_valid_until(valid_days),
        max_grants_per_day=max_grants_per_day,
        previous=previous,
    )


def verify_signed_so_far(manifest: Mapping[str, Any], *, trust: Path) -> frozenset[str]:
    """校验**已存在的每一条**签名（不要求达到双签法定人数），返回签名者集合。"""
    body = manifest_body(manifest)
    canonical = canonical_payload(body)
    signers: set[str] = set()
    for jws in manifest.get("signatures") or []:
        if not isinstance(jws, str):
            raise ValueError("signatures 里必须都是 JWS 字符串")
        payload, signer = verify_payload_jws(jws, trust_bundle=trust)
        if canonical_payload(payload) != canonical:
            raise ValueError("签名与章程正文不一致（被篡改或张冠李戴）")
        signers.add(signer)
    return frozenset(signers)


def cosign_manifest(
    manifest: Mapping[str, Any],
    *,
    workspace: str | Path,
) -> tuple[dict[str, Any], frozenset[str]]:
    """用 `workspace` 的身份补一条签名；返回 (新章程, 签名者集合)。"""
    trust = trust_bundle(workspace)
    signers = verify_signed_so_far(manifest, trust=trust)
    signing = signing_context(workspace)
    body = manifest_body(manifest)
    member_aics = {normalize_audience(str(m.get("aic") or "")) for m in body.get("members") or []}
    if normalize_audience(signing.issuer_aic) not in member_aics:
        raise ValueError(f"{signing.issuer_aic} 不是该章程的成员，不能联签")
    signed = sign_manifest(manifest, signing)
    new_signers = verify_signed_so_far(signed, trust=trust)
    return signed, new_signers


def load_json(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} 不是 JSON 对象")
    return data


def write_json(path: str | Path, payload: Mapping[str, Any], *, secret: bool = False) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if secret:
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    os.replace(tmp, target)
    return target


def _assert_charter_usable(body: Mapping[str, Any]) -> None:
    """冻结/过期的章程不能用来签 grant（与加载器 fail-closed 口径一致）。"""
    status = str(body.get("status") or "active")
    if status != "active":
        raise ValueError(f"章程状态是 {status!r}，拒绝签发 grant")
    until = parse_expiry(body.get("validUntil"))
    if until is not None and until < time.time():
        raise ValueError("章程已过期（validUntil），拒绝签发 grant")


#: 公开别名（p2_selftest 用）
assert_charter_usable = _assert_charter_usable


def install_charter(manifest: Mapping[str, Any], *, workspace: str | Path,
                    domains_dir: str | Path | None = None) -> tuple[Path, Any]:
    """验双签 → 写入 domains 目录 → 返回 (路径, DomainPolicy)。"""
    trust = trust_bundle(workspace)
    body, signers = verify_manifest(manifest, trust_bundle=trust)
    policy = to_policy(body, signers)
    local_aic = signing_context(workspace).issuer_aic
    if policy.member_role(normalize_audience(local_aic)) != "provider":
        raise ValueError(
            f"本机 {local_aic} 在这份章程里不是 provider——装错机器会让入口技能无法授权"
        )
    target_dir = Path(domains_dir) if domains_dir else _acps(workspace) / "domains"
    path = write_manifest(manifest, target_dir)
    return path, policy


def issue_entry_grant(
    *,
    workspace: str | Path,
    subject_aic: str,
    domain: str,
    revision: int,
    provider_aic: str,
    skills: Sequence[str] = ENTRY_SKILLS,
    max_uses: int = DEFAULT_MAX_USES,
    ttl_days: int = DEFAULT_TTL_DAYS,
) -> tuple[IssuedGrant, dict[str, Any]]:
    """给入口 Partner 签 grant，并返回 (grant, 可投递的 grant 文件内容)。"""
    if ttl_days < 1 or int(ttl_days) * 86400 > GRANT_MAX_TTL_SECONDS:
        raise ValueError(f"ttl_days 必须在 1..{GRANT_MAX_TTL_SECONDS // 86400} 天内")
    signing = signing_context(workspace)
    if normalize_audience(signing.issuer_aic) != normalize_audience(provider_aic):
        raise ValueError(
            f"签发者必须是资产提供方 {provider_aic}（当前证书 AIC={signing.issuer_aic}）"
        )
    assets = [{"providerAic": provider_aic, "skill": skill} for skill in _skills(skills)]
    grant = issue_grant(
        signing,
        domain=domain,
        domain_revision=int(revision),
        subject_aic=subject_aic,
        assets=assets,
        max_uses=int(max_uses),
        ttl_seconds=int(ttl_days) * 86400,
    )
    verify_grant(grant.token, trust_bundle=trust_bundle(workspace))
    document = {
        "schema": GRANT_SCHEMA,
        "domain": domain,
        "revision": int(revision),
        "issuer": normalize_audience(signing.issuer_aic),
        "subject": normalize_audience(subject_aic),
        "jti": grant.jti,
        "expiresAt": grant.expires_at,
        "maxUses": int(grant.claims["acps_max_uses"]),
        "assets": assets,
        "note": "JWS 只用于向 provider 发起任务，勿落日志/勿进模型上下文",
        "token": grant.token,
    }
    return grant, document


def check_grant_against_charter(
    document: Mapping[str, Any], *, workspace: str | Path,
    domains_dir: str | Path | None = None,
) -> dict[str, Any]:
    """离线核对：grant 验签 + 域名/revision/资产/成员角色是否与已装章程一致。"""
    token = str(document.get("token") or "")
    if not token:
        raise ValueError("grant 文件缺 token")
    trust = trust_bundle(workspace)
    claims = verify_grant(token, trust_bundle=trust)
    target_dir = Path(domains_dir) if domains_dir else _acps(workspace) / "domains"
    policies = load_domain_policies(target_dir, trust_bundle=trust)
    domain_id = str(claims.get("acps_domain") or "")
    policy = policies.get(domain_id)
    if policy is None:
        raise ValueError(f"本机没有可验证的域章程：{domain_id}")
    subject = normalize_audience(str(claims.get("sub") or ""))
    role = policy.member_role(subject)
    if role is None:
        raise ValueError(f"{subject} 不是域 {domain_id} 的成员")
    assets = [(str(a.get("providerAic")), str(a.get("skill"))) for a in claims.get("acps_assets") or ()]
    missing = [
        f"{p}:{s}" for p, s in assets
        if not policy.asset_allows(provider_aic=p, skill_id=s, role=role)
    ]
    if missing:
        raise ValueError(f"章程不允许角色 {role} 使用这些资产：{missing}")
    return {
        "domain": domain_id,
        "revision": policy.revision,
        "status": policy.status,
        "subject": subject,
        "role": role,
        "assets": [f"{p}:{s}" for p, s in assets],
        "expiresAt": int(claims["exp"]),
        "maxUses": int(claims["acps_max_uses"]),
        "signers": sorted(policy.signer_aics),
    }


# ─────────────────────────── 子命令 ───────────────────────────


def cmd_prepare(args: argparse.Namespace) -> int:
    signing = signing_context(args.supervisor_workspace)
    previous = load_json(args.previous) if args.previous else None
    manifest = build_entry_manifest(
        domain=args.domain,
        supervisor_aic=signing.issuer_aic,
        peer_aic=args.peer_aic,
        revision=args.revision,
        valid_days=args.valid_days,
        skills=_skills(args.skills),
        max_grants_per_day=args.max_grants_per_day,
        previous=previous,
        extra_consumers=getattr(args, "extra_consumer", []) or [],
    )
    signed = sign_manifest(manifest, signing)
    path = write_json(args.out, signed)
    print(f"[grant] 章程草案（1/2 签名）：{path}")
    print(f"  domain={args.domain} revision={args.revision} "
          f"provider={signing.issuer_aic} consumer={args.peer_aic}")
    print("[grant] 下一步：把该文件交给入口 Partner，在其机器上执行 cosign")
    return 0


def cmd_cosign(args: argparse.Namespace) -> int:
    manifest = load_json(args.infile)
    signed, signers = cosign_manifest(manifest, workspace=args.peer_workspace)
    if len(signers) < 2:
        print(f"[grant] 联签后仍只有 {len(signers)} 个签名：{sorted(signers)}", file=sys.stderr)
        return 1
    path = write_json(args.out, signed)
    print(f"[grant] 已联签（{len(signers)}/2）：{path}")
    print(f"  签名者：{sorted(signers)}")
    print("[grant] 下一步：把该文件交回 supervisor 侧执行 install")
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    manifest = load_json(args.infile)
    path, policy = install_charter(
        manifest, workspace=args.supervisor_workspace, domains_dir=args.domains_dir,
    )
    print(f"[grant] 章程已安装：{path}")
    print(f"  domain={policy.domain_id} revision={policy.revision} status={policy.status} "
          f"signers={sorted(policy.signer_aics)}")
    for (provider, skill), roles in sorted(policy.assets.items()):
        print(f"  asset {provider}:{skill} -> {sorted(roles)}")
    return 0


def cmd_issue(args: argparse.Namespace) -> int:
    manifest = load_json(args.charter)
    trust = trust_bundle(args.supervisor_workspace)
    body, signers = verify_manifest(manifest, trust_bundle=trust)
    _assert_charter_usable(body)
    policy = to_policy(body, signers)
    role = policy.member_role(normalize_audience(args.subject_aic))
    if role is None:
        print(f"[grant] {args.subject_aic} 不是章程成员，拒绝签发", file=sys.stderr)
        return 1
    skills = _skills(args.skills)
    for skill in skills:
        if not policy.asset_allows(
            provider_aic=normalize_audience(args.provider_aic), skill_id=skill, role=role,
        ):
            print(f"[grant] 章程不允许 {role} 使用 {skill}，拒绝签发", file=sys.stderr)
            return 1
    grant, document = issue_entry_grant(
        workspace=args.supervisor_workspace,
        subject_aic=args.subject_aic,
        domain=policy.domain_id,
        revision=policy.revision,
        provider_aic=args.provider_aic,
        skills=skills,
        max_uses=args.max_uses,
        ttl_days=args.ttl_days,
    )
    path = write_json(args.out, document, secret=True)
    print(f"[grant] 已签发：{path}")
    print(f"  domain={policy.domain_id} revision={policy.revision} subject={grant.subject_aic}")
    print(f"  jti={grant.jti} expiresAt={grant.expires_at} maxUses={grant.claims['acps_max_uses']}")
    print(f"  assets={['%s:%s' % (p, s) for p, s in grant.assets]}")
    print("[grant] grant 文件权限 0600；投递后请对端放在工作区外/仅进程可读的位置")
    return 0


def cmd_mint(args: argparse.Namespace) -> int:
    """同一台机器上完成 prepare + cosign + install + issue（自证用）。"""
    supervisor = signing_context(args.supervisor_workspace)
    peer_aic = args.peer_aic or workspace_aic(args.peer_workspace)
    previous = load_json(args.previous) if args.previous else None
    manifest = build_entry_manifest(
        domain=args.domain,
        supervisor_aic=supervisor.issuer_aic,
        peer_aic=peer_aic,
        revision=args.revision,
        valid_days=args.valid_days,
        skills=_skills(args.skills),
        max_grants_per_day=args.max_grants_per_day,
        previous=previous,
        extra_consumers=getattr(args, "extra_consumer", []) or [],
    )
    signed = sign_manifest(manifest, supervisor)
    signed, signers = cosign_manifest(signed, workspace=args.peer_workspace)
    if len(signers) < 2:
        print(f"[grant] 签名不足：{sorted(signers)}", file=sys.stderr)
        return 1
    _assert_charter_usable(manifest_body(signed))
    path, policy = install_charter(
        signed, workspace=args.supervisor_workspace, domains_dir=args.domains_dir,
    )
    grant, document = issue_entry_grant(
        workspace=args.supervisor_workspace,
        subject_aic=peer_aic,
        domain=policy.domain_id,
        revision=policy.revision,
        provider_aic=supervisor.issuer_aic,
        skills=_skills(args.skills),
        max_uses=args.max_uses,
        ttl_days=args.ttl_days,
    )
    grant_path = write_json(args.out, document, secret=True)
    print(f"[grant] 章程已安装：{path}（signers={sorted(signers)}）")
    print(f"[grant] grant 已签发：{grant_path}")
    print(f"  subject={grant.subject_aic} jti={grant.jti} maxUses={grant.claims['acps_max_uses']}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    document = load_json(args.grant)
    summary = check_grant_against_charter(
        document, workspace=args.workspace, domains_dir=args.domains_dir,
    )
    print("[grant] 核验通过")
    for key, value in summary.items():
        print(f"  {key}: {value}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m acps_cluster.tools.entry_grant",
        description="入口域章程双签 + grant 签发/核验（不改 ACS、不改平台注册）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def _common(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--domain", default=DEFAULT_DOMAIN)
        parser.add_argument("--revision", type=int, default=1)
        parser.add_argument("--valid-days", type=int, default=DEFAULT_VALID_DAYS)
        parser.add_argument("--skills", default=",".join(ENTRY_SKILLS))
        parser.add_argument("--max-grants-per-day", type=int, default=50)
        parser.add_argument("--max-uses", type=int, default=DEFAULT_MAX_USES)
        parser.add_argument("--ttl-days", type=int, default=DEFAULT_TTL_DAYS)
        parser.add_argument("--previous", default="", help="上一版已签章程（r>1 时必须）")

    prepare = sub.add_parser("prepare", help="supervisor 侧生成只带 1 个签名的章程草案")
    prepare.add_argument("--supervisor-workspace", required=True)
    prepare.add_argument("--peer-aic", required=True)
    prepare.add_argument("--out", required=True)
    prepare.add_argument("--extra-consumer", action="append", default=[],
                         help="预先列入章程、但本次不签名的其他入口 AIC（可重复）")
    _common(prepare)
    prepare.set_defaults(func=cmd_prepare)

    cosign = sub.add_parser("cosign", help="入口 Partner 侧补第二个签名")
    cosign.add_argument("--in", dest="infile", required=True)
    cosign.add_argument("--peer-workspace", required=True)
    cosign.add_argument("--out", required=True)
    cosign.set_defaults(func=cmd_cosign)

    install = sub.add_parser("install", help="supervisor 侧验双签并写入 domains 目录")
    install.add_argument("--in", dest="infile", required=True)
    install.add_argument("--supervisor-workspace", required=True)
    install.add_argument("--domains-dir", default="")
    install.set_defaults(func=cmd_install)

    issue = sub.add_parser("issue", help="supervisor 侧签发 grant")
    issue.add_argument("--charter", required=True)
    issue.add_argument("--supervisor-workspace", required=True)
    issue.add_argument("--subject-aic", required=True)
    issue.add_argument("--provider-aic", required=True)
    issue.add_argument("--out", required=True)
    _common(issue)
    issue.set_defaults(func=cmd_issue)

    mint = sub.add_parser("mint", help="同机一步完成：双签 + 安装 + 签发")
    mint.add_argument("--supervisor-workspace", required=True)
    mint.add_argument("--peer-workspace", required=True)
    mint.add_argument("--peer-aic", default="")
    mint.add_argument("--out", required=True)
    mint.add_argument("--domains-dir", default="")
    mint.add_argument("--extra-consumer", action="append", default=[],
                      help="预先列入章程、但本次不签名的其他入口 AIC（可重复）")
    _common(mint)
    mint.set_defaults(func=cmd_mint)

    verify = sub.add_parser("verify", help="离线核验 grant 与已装章程是否一致")
    verify.add_argument("--grant", required=True)
    verify.add_argument("--workspace", required=True)
    verify.add_argument("--domains-dir", default="")
    verify.set_defaults(func=cmd_verify)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        return int(args.func(args))
    except (TokenError, ValueError, FileNotFoundError) as exc:
        print(f"[grant] 失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


__all__ = [
    "DEFAULT_DOMAIN",
    "ENTRY_SKILLS",
    "GRANT_SCHEMA",
    "build_entry_manifest",
    "build_parser",
    "check_grant_against_charter",
    "cosign_manifest",
    "install_charter",
    "issue_entry_grant",
    "main",
    "signing_context",
    "trust_bundle",
    "verify_signed_so_far",
    "workspace_aic",
]

if __name__ == "__main__":
    raise SystemExit(main())