"""域路径（L2 域授权）能工作：**有章程 + 域令牌 → 放行；非成员 → 拒绝**。

## 为什么需要这个测试

此前只验证了"**缺**章程时带 `acps_domain` 的令牌被 `deny / domain-unknown`"，
**没有**验证"有章程且调用方是成员时能放行"。第 (4) 项（生成并签名 yanshu-kb 章程）
的**目的**正是让这条路径可用——所以必须把"可用"本身证明出来，
否则章程即使落盘也不知道是否真的生效。

## 域路径的判定链（pep.py 486 行起）

```
token 的 acps_domain claim
  → 本机章程里找该域（找不到 → deny / domain-unknown）
  → 章程是否冻结（frozen → 拒绝）
  → 调用方是否是域成员（member_role）
  → asset_allows(provider=本机, skill=请求的, role=成员角色)
  → 放行
```

## 覆盖

1. 域令牌 + 章程 + **成员**调用方 → 放行（域路径可用）
2. 域令牌 + 章程 + **非成员** → 拒绝（不能靠"有章程"混进来）
3. 域令牌 + 章程 + skill **未被 asset 授权** → 拒绝
4. 域令牌但本机**没有**该域章程 → deny / domain-unknown（对照，既有行为）
5. 章程冻结（frozen）→ 拒绝
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

CL = Path("/opt/wit/my-project/acps_cluster")
sys.path.insert(0, str(CL.parent))
sys.path.insert(0, str(CL))

from delegation.domain_manifest import (  # noqa: E402
    build_manifest,
    load_domain_policies,
    sign_manifest,
    write_manifest,
)
from delegation.grant import derive_delegation_claims, issue_grant  # noqa: E402
from delegation.pep import PepConfig, evaluate  # noqa: E402
from delegation.token import issue_token, load_signing_context  # noqa: E402

WS_SUP = CL / "ws-supervisor"
WS_SCOUT = CL / "ws-scout"
BUNDLE = WS_SUP / "acps" / "trust-bundle.pem"

LEADER = "1.2.156.3088.1.BUPT.VTF97R.JXN8O8.98DOO8.0EB6"    # supervisor
MEMBER = "1.2.156.3088.1.BUPT.VTF97R.GMT5S6.4SZM68.0LC5"    # scout（将作为域成员）
OUTSIDER = "1.2.156.3088.1.BUPT.VTF97R.ZZZZZZ.ZZZZZZ.ZZZZ"  # 域外
KB_AIC = "1.2.156.3088.1.BUPT.VTF97R.KBAAAA.KBKBKB.KB01"    # 占位（KB 尚未注册）
DOMAIN = "yanshu-kb"


def _signing(ws: Path):
    return load_signing_context(ws / "acps" / "client",
                                trust_bundle=ws / "acps" / "trust-bundle.pem")


def _make_charter(out_dir: Path, *, status: str = "active") -> None:
    """用两个真实工作区双签（两方都写进 members，满足 manifest-signer-not-party）。"""
    manifest = build_manifest(
        domain_id=DOMAIN,
        members=[{"aic": MEMBER, "role": "consumer"},   # 占位 provider 用 MEMBER 的域
                 {"aic": LEADER, "role": "provider"}],
        assets=[{"providerAic": LEADER, "skill": "kb.query",
                 "allowedMembers": ["consumer"]}],
        issuers=[MEMBER, LEADER],
        revision=1,
        status=status,
    )
    signed = dict(manifest)
    for ws in (WS_SCOUT, WS_SUP):
        signed = sign_manifest(signed, _signing(ws))
    write_manifest(signed, out_dir)


def _cfg(domains: dict, *, my_aic: str, quota_path=None) -> PepConfig:
    return PepConfig(
        enabled=True, my_aic=my_aic,
        public_skills=frozenset({"kb.schema"}), has_non_public_skills=True,
        leader_aics=frozenset({LEADER}),
        leader_direct_open=False,
        trust_bundle=BUNDLE,
        domains=domains,          # 直接注入已验证的章程（等价于从 domains_dir 加载）
        domains_dir=None,
        quota_path=quota_path,    # 派生委托按父 grant 的 max_uses 扣配额
    )


def _req(caller: str, token: str, skill: str = "kb.query"):
    from delegation.token import token_data_item
    item = token_data_item(token, target_aic=skill, skill_id=skill)
    return SimpleNamespace(
        leader_aic=caller, command_params={"skillId": skill},
        data_items=[{"type": "text", "text": "查询知识库"}, item],
        peer_verified=True, task_id="t", session_id="s", delivery="rpc",
    )


def main() -> int:
    failures: list[str] = []
    sc = _signing(WS_SUP)

    with tempfile.TemporaryDirectory(prefix="kb-domain-") as td:
        out = Path(td)
        _make_charter(out)
        policies = load_domain_policies(out, trust_bundle=BUNDLE)
        pol = policies.get(DOMAIN)
        if pol is None or pol.status != "active" or not pol.members:
            print(f"  ✘ 章程未生效（毒化态）：{pol}")
            return 1
        print(f"  [0] 章程就绪: status={pol.status} members={len(pol.members)} "
              f"assets={len(pol.assets)} issuers={len(pol.issuers)}")
        domains = {DOMAIN: pol}

        # 域令牌：provider=本机(LEADER)。现在必须从一枚父 grant 派生——子令牌会带上
        # parent_jti/exp/max_uses，PEP 才能独立复核并继续扣父配额。
        def issue_dom(caller_delegate: str, *, domain: str = DOMAIN,
                      skill: str = "kb.query") -> str:
            parent = issue_grant(
                sc, domain=domain, domain_revision=1, subject_aic=LEADER,
                assets=[{"providerAic": LEADER, "skill": skill}],
                max_uses=10, ttl_seconds=3600,
            )
            return issue_token(
                sc, delegate_aic=caller_delegate, target_aic=LEADER, skill_id=skill,
                ttl_seconds=300, not_after=parent.expires_at, include_intermediates=True,
                extra_claims=derive_delegation_claims(parent.claims, skill_id=skill),
            ).token

        # 1) 域成员 + 章程 → 放行
        cfg = _cfg(domains, my_aic=LEADER, quota_path=out / "quota.sqlite3")
        out1 = evaluate(_req(MEMBER, issue_dom(MEMBER)), cfg)
        print(f"  [1] 域成员（{MEMBER[-18:]}）+ 章程 → {out1.decision} / {out1.rule}")
        if not out1.allowed:
            failures.append(f"[1] 域路径未放行：{out1.decision} / {out1.rule} / {out1.reason}")

        # 2) 非成员 → 拒绝
        out2 = evaluate(_req(OUTSIDER, issue_dom(OUTSIDER)), cfg)
        print(f"  [2] 域外调用方 → {out2.decision} / {out2.rule}")
        if out2.allowed:
            failures.append("[2] 域外调用方被放行")

        # 3) skill 未被 asset 授权 → 拒绝（父 grant 覆盖它，但章程资产表不放行）
        tok3 = issue_dom(MEMBER, skill="kb.ingest")
        out3 = evaluate(_req(MEMBER, tok3, skill="kb.ingest"), cfg)
        print(f"  [3] 请求未授权 skill（kb.ingest，asset 只允许 kb.query）→ "
              f"{out3.decision} / {out3.rule}")
        if out3.allowed:
            failures.append("[3] 未授权 skill 被放行")

        # 4) 对照：本机没有该域章程 → domain-unknown
        cfg4 = _cfg({}, my_aic=LEADER)
        out4 = evaluate(_req(MEMBER, issue_dom(MEMBER)), cfg4)
        print(f"  [4] 无章程（对照）→ {out4.decision} / {out4.rule}")
        if out4.allowed or out4.rule != "domain-unknown":
            failures.append(f"[4] 期望 domain-unknown，实得 {out4.decision}/{out4.rule}")

        # 5) 章程冻结 → 拒绝
        with tempfile.TemporaryDirectory(prefix="kb-frozen-") as td2:
            out_dir2 = Path(td2)
            _make_charter(out_dir2, status="frozen")
            pols2 = load_domain_policies(out_dir2, trust_bundle=BUNDLE)
            out5 = evaluate(_req(MEMBER, issue_dom(MEMBER)),
                            _cfg(pols2, my_aic=LEADER))
            print(f"  [5] 章程 frozen → {out5.decision} / {out5.rule}")
            if out5.allowed:
                failures.append("[5] 冻结域被放行")

    print()
    if failures:
        for f in failures:
            print("  ✘", f)
        return 1
    print("✔ 通过：域路径可用（成员放行、非成员/未授权 skill/无章程/冻结均拒绝）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
