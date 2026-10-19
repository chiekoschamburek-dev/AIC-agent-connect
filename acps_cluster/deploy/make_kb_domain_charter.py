#!/usr/bin/env python3
"""生成并签名 `yanshu-kb` 域章程（KB 资源 agent 的域级授权，**资源方自签口径**）。

## 背景

`partner_kb.py` 的 `kb.query` / `kb.ingest` 声明了
`acps_skill={"visibility": "domain", "domains": ["yanshu-kb"]}`。

**注意（2026-10-11 实测）**：该本机声明**本身不参与准入裁决**——`pep.py` 的
`token_domain` 取自**令牌的 `acps_domain` claim**。因此：
  · 普通委托令牌调 KB → 不碰域校验 → 走的是 leader 白名单那条路；
  · 走**域级授权**（grant 或 grant 派生令牌）才必须有章程，否则带 `acps_domain`
    的令牌被判 `deny / domain-unknown`。

## B3：谁有权签发（2026-10-18 改）

`issuers` 默认**只含本馆自己**。理由是资源库是独立机构的家底：
`pep._evaluate_grant` 对 grant 只看章程 issuers（注释原话"grant 的权力来源是章程不是
编排关系"），所以 leader 自签的 grant 会被判 `issuer-not-in-domain`——K11 有实证。
消费方要用，得先来申请，由本馆用 `kb_admin.py grant` 签发。

⚠️ **现网要彻底切过来还差一步**：delegation 那条路是
`allowed_issuers = cfg.leader_aics | set(domain_policy.issuers)`——只要
`ACPS_PEP_LEADER_AICS` 里还写着 supervisor，它自签的**委托令牌**（非 grant）仍然有效。
配套改动：章程 r2（本馆自签）+ `ACPS_PEP_LEADER_AICS=` 置空 + `ACPS_PEP_LEADER_DIRECT=0`
（现网已置 0）。置空之后 grant 通道照常工作，因为它只比对 `policy.issuers`。

## 章程 schema（来自 delegation/domain_manifest.py）

- `members`: `[{aic, role}]`，role ∈ {provider, consumer, auditor}
- `assets`: `[{providerAic, skill, allowedMembers: [角色...]}]`，providerAic 必须是 provider 成员
  —— 本脚本把 `kb.query` 给 consumer、`kb.ingest` 只留 provider（外馆不能往本馆库里写）
- `issuers`: 必须**都是域成员**
- `MIN_SIGNATURES = 2` → **至少两方签名**，且签名者必须是域当事方（members ∪ issuers）；
  第二签从消费方工作区出，语义是"消费方确认条款"，不是"上级批准"

## 用法

```sh
# 本馆自签（默认），消费方两位：实验 agent 与 supervisor
python make_kb_domain_charter.py     --kb-aic <KB 的 AIC>     --consumer-aic <实验 agent AIC> --consumer-aic <supervisor AIC>     --sign-with kb --sign-with supervisor     --root /opt/wit/my-project/acps_cluster --revision 2

# 多馆共建一个库时才加共签签发方
    --issuer-aic <本馆 AIC> --issuer-aic <兄弟馆 AIC>
```
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

CL = Path(os.getenv("ACPS_CLUSTER_ROOT") or "/opt/wit/my-project/acps_cluster")
sys.path.insert(0, str(CL.parent))
sys.path.insert(0, str(CL))

from delegation.domain_manifest import (  # noqa: E402
    MIN_SIGNATURES,
    build_manifest,
    load_domain_policies,
    sign_manifest,
    write_manifest,
)
from delegation.token import load_signing_context  # noqa: E402

DOMAIN_ID = "yanshu-kb"
# 策展写入是本馆特权；对外只放查询
KB_READ_SKILL = "kb.query"
KB_WRITE_SKILL = "kb.ingest"


def signing_for(name: str, root: Path = CL):
    """从 <root>/ws-<name>/acps/client/ 加载签名上下文（bundle 用同工作区的）。"""
    ws = root / f"ws-{name}"
    return load_signing_context(
        ws / "acps" / "client", trust_bundle=ws / "acps" / "trust-bundle.pem"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb-aic", required=True, help="KB 资源 agent 的 AIC（provider，也是默认唯一签发方）")
    ap.add_argument("--consumer-aic", action="append", default=[],
                    help="消费方 AIC（可多次）；至少要一位，双签的第二方就从这些工作区里出")
    ap.add_argument("--leader-aic", default=None,
                    help="旧参数名，等价于再给一个 --consumer-aic（现网 runbook 还在用它）。"
                         "注意 B3 之后它**不再自动成为签发方**")
    ap.add_argument("--issuer-aic", action="append", default=[],
                    help="域签发方（可多次）。默认只有本馆自己=资源方自主授权；"
                         "多馆共建一个库时才显式加共签签发方")
    ap.add_argument("--sign-with", action="append", default=[],
                    help="签名用的工作区名（可多次）；默认 kb + supervisor。"
                         "签名者必须是域当事方，所以这里的工作区身份要出现在 --consumer-aic 里")
    ap.add_argument("--root", default=str(CL),
                    help="集群根（默认取 ACPS_CLUSTER_ROOT，否则 /opt/wit/my-project/acps_cluster）")
    ap.add_argument("--kb-workspace-name", default="kb", help="KB 工作区名（默认 kb）")
    ap.add_argument("--out-dir", default=None,
                    help="章程输出目录（默认 <ws-kb>/acps/domains）。"
                         "测试时指向临时目录，避免污染现网工作区——这是本脚本第一版的一个坑："
                         "把 --kb-workspace-name 当输出目录会直接写进别人家工作区。")
    ap.add_argument("--revision", type=int, default=1)
    ap.add_argument("--valid-until", default=None, help="可选，格式如 2027-12-31T23:59:59Z")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不落盘")
    args = ap.parse_args()
    root = Path(args.root)

    consumers = list(args.consumer_aic)
    if args.leader_aic:
        consumers.append(args.leader_aic)
    if not consumers:
        print("  ✘ 至少要一位消费方（--consumer-aic，或旧名 --leader-aic）："
              f"双签要求 {MIN_SIGNATURES} 位域当事方，第二方就得从消费方里出")
        return 2
    members = [{"aic": args.kb_aic, "role": "provider"}]
    for aic in consumers:
        if aic and aic not in {m["aic"] for m in members}:
            members.append({"aic": aic, "role": "consumer"})

    # 资产：查询面向消费方；策展写入只给本馆（provider）——外馆不能往别人家库里写
    assets = [{"providerAic": args.kb_aic, "skill": KB_READ_SKILL, "allowedMembers": ["consumer"]},
              {"providerAic": args.kb_aic, "skill": KB_WRITE_SKILL, "allowedMembers": ["provider"]}]

    # B3：**资源方自己签发**。签发者必须是域成员（本馆是 provider，天然满足）。
    # leader 从"批准别人进来的人"变成"申请进来的 consumer"——
    # pep._evaluate_grant 对 grant 只认章程 issuers（"grant 的权力来源是章程不是编排关系"），
    # 所以 issuers 里没有 leader，它自签的令牌就会被判 issuer-not-in-domain（K11 实证）。
    issuers = list(args.issuer_aic) or [args.kb_aic]
    for issuer in issuers:
        if issuer not in {m["aic"] for m in members}:
            print(f"  ✘ 签发者 {issuer[-24:]} 不是域成员，章程验不过")
            return 2

    manifest = build_manifest(
        domain_id=DOMAIN_ID, members=members, assets=assets, issuers=issuers,
        revision=args.revision, status="active", valid_until=args.valid_until,
    )
    print(f"  domain_id   = {DOMAIN_ID}  revision={args.revision}")
    print(f"  members     = {[(m['aic'][-24:], m['role']) for m in members]}")
    print(f"  assets      = {[(a['skill'], a['allowedMembers']) for a in assets]}")
    print(f"  issuers     = {[i[-24:] for i in issuers]}"
          f"{'（资源方自签）' if issuers == [args.kb_aic] else '（多签发方）'}")
    print(f"  法定签名数  = {MIN_SIGNATURES}")

    # 签名者必须是域当事方（members ∪ issuers），且必须互不相同
    # 签名者必须是域当事方（members ∪ issuers）且互不相同。默认：本馆 + leader 的工作区
    # （leader 现在是 consumer 成员，它的签名含义是"消费方确认条款"，不再是"我批准你"）
    sign_workspaces = list(args.sign_with) or [args.kb_workspace_name, "supervisor"]
    party_aics = {m["aic"] for m in members} | set(issuers)
    signed = dict(manifest)
    signers = []
    seen_aics: set[str] = set()
    for ws_name in sign_workspaces:
        try:
            sc = signing_for(ws_name, root)
        except Exception as exc:  # noqa: BLE001
            print(f"  ✘ 加载 ws-{ws_name} 的签名上下文失败：{type(exc).__name__}: {exc}")
            print(f"     （KB 尚未注册时这是预期的——注册后 ws-{ws_name}/acps/client/ 才有证书）")
            return 2
        if sc.issuer_aic not in party_aics:
            print(f"  ✘ ws-{ws_name} 的身份 {sc.issuer_aic[-24:]} 不是域当事方（members ∪ issuers）。"
                  f"verify_manifest 会判 manifest-signer-not-party，章程被毒化成 frozen 而不是报错")
            return 2
        # 签名者不能重复：verify_manifest 要求"签名者不重复"且达法定人数。
        # 若两方指向同一工作区（例如调试时把 --kb-workspace-name 也写成 supervisor），
        # 会得到同一方签两次 → 验签时判失败，所以这里先拦住并给出清晰提示。
        if sc.issuer_aic in seen_aics:
            print(f"  ✘ 签名者重复：{sc.issuer_aic[-24:]} 已经签过。"
                  f"章程要求 {MIN_SIGNATURES} 个**不同**工作区签名；"
                  f"请用 --sign-with 指定 KB 自己的工作区。")
            return 2
        seen_aics.add(sc.issuer_aic)
        signed = sign_manifest(signed, sc)
        signers.append(f"{ws_name}({sc.issuer_aic[-24:]})")
    print(f"  已签名者    = {signers}")
    if len(seen_aics) < MIN_SIGNATURES:
        print(f"  ✘ 签名者不足 {MIN_SIGNATURES} 个（当前 {len(seen_aics)}）")
        return 2

    out_dir = (Path(args.out_dir) if args.out_dir
               else root / f"ws-{args.kb_workspace_name}" / "acps" / "domains")
    if args.dry_run:
        print(f"  （dry-run）将写入 {out_dir}")
        print("  --- 章程摘要")
        print(json.dumps({k: v for k, v in signed.items() if k != "signatures"},
                         ensure_ascii=False, indent=2)[:900])
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    path = write_manifest(signed, out_dir)
    print(f"  ✔ 已写入 {path}")

    # 立刻回读验签（fail-closed：验不过就等于没生效）。
    # bundle 用 **KB 工作区**的（域策略由 KB 侧加载）；测试时若 KB 不存在则退回首个签名工作区。
    bundle = root / f"ws-{args.kb_workspace_name}" / "acps" / "trust-bundle.pem"
    if not bundle.is_file():
        bundle = root / f"ws-{sign_workspaces[0]}" / "acps" / "trust-bundle.pem"
        print(f"  （提示：ws-{args.kb_workspace_name} 无 bundle，回读改用 {bundle.parent.parent.name} 的）")
    policies = load_domain_policies(out_dir, trust_bundle=bundle)
    pol = policies.get(DOMAIN_ID)
    if pol is None:
        print(f"  ✘ 回读失败：{out_dir} 下没有 {DOMAIN_ID} 章程")
        return 3
    # ⚠️ 判定必须严格：`load_domain_policies` 在**验签失败时不会返回 None**，
    # 而是"毒化"——合成一个 status=frozen、无成员无资产的策略（fail-closed），
    # 于是只看 `pol is not None` 会把"验签失败"误报成"通过"（本脚本第一版就踩了这个坑）。
    if pol.status != "active" or not pol.members:
        print(f"  ✘ 回读为**毒化态**（验签失败或已冻结）：status={pol.status} "
              f"members={len(pol.members)} assets={len(pol.assets)}")
        print("     常见原因：① 签名者不是域成员/签发者（verify_manifest 的 "
              "manifest-signer-not-party）；② trust-bundle 缺中间 CA；③ 签名数不足 2。")
        return 3
    print(f"  ✔ 回读验签通过：revision={pol.revision} status={pol.status} "
          f"members={len(pol.members)} assets={len(pol.assets)} issuers={len(pol.issuers)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
