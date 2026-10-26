#!/bin/sh
# 流程验证：证明「双签 → 验签 → to_policy 得到 active 策略」这条链路可用。
#
# 约束（verify_manifest 的安全校验）：
#   · 签名者必须是域当事方（members ∪ issuers），否则 manifest-signer-not-party
#   · 至少 2 位**不同**签名者
# 由于 ws-kb 尚未注册（无证书），这里用 **supervisor + scout** 作两个签名者，
# 并把**两者都写进域成员**，以自洽地验证链路。真实章程把 provider 换成 KB 即可。
CL=/opt/wit/my-project/acps_cluster
PY=/opt/wit/venv/bin/python
OUT=/opt/wit/tmp/kb-charter-test2
SUP=1.2.156.3088.1.BUPT.VTF97R.JXN8O8.98DOO8.0EB6   # supervisor
SCOUT=1.2.156.3088.1.BUPT.VTF97R.GMT5S6.4SZM68.0LC5 # scout

rm -rf "$OUT"; mkdir -p "$OUT"; chown wit:wit "$OUT"

cd "$CL" || exit 1
echo "=== 1) 生成 + 双签（两个签名者都在域成员里）==="
sudo -u wit YANSHU_RUN_TRACE=0 $PY - "$OUT" "$SUP" "$SCOUT" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, '/opt/wit/my-project')
sys.path.insert(0, '/opt/wit/my-project/acps_cluster')
from delegation.domain_manifest import (
    MIN_SIGNATURES, build_manifest, load_domain_policies, sign_manifest, write_manifest,
)
from delegation.token import load_signing_context

out, sup, scout = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
CL = Path('/opt/wit/my-project/acps_cluster')

def signing_of(name: str):
    ws = CL / f'ws-{name}'
    return load_signing_context(ws / 'acps' / 'client',
                                trust_bundle=ws / 'acps' / 'trust-bundle.pem')

# 两个签名者都必须是域当事方 → 都写进 members
members = [
    {"aic": sup, "role": "provider"},      # 占位：真实场景这里是 KB 的 AIC
    {"aic": scout, "role": "consumer"},
]
assets = [{"providerAic": sup, "skill": s, "allowedMembers": ["consumer", "auditor"]}
          for s in ("kb.query", "kb.ingest")]
m = build_manifest(domain_id="yanshu-kb", members=members, assets=assets,
                   issuers=[sup, scout], revision=1)
print(f"  法定签名数={MIN_SIGNATURES}  members={len(members)}  assets={len(assets)}")

signed = dict(m)
signers = []
for name in ("supervisor", "scout"):
    sc = signing_of(name)
    if sc.issuer_aic in signers:
        print(f"  ✘ 签名者重复: {sc.issuer_aic[-24:]}")
        raise SystemExit(2)
    signed = sign_manifest(signed, sc)
    signers.append(sc.issuer_aic)
    print(f"  签名者 += {name}({sc.issuer_aic[-24:]})")

path = write_manifest(signed, out)
print(f"  ✔ 已写入 {path}")

print()
print("=== 2) 回读并检查（严格判定：必须 active 且有成员）===")
bundle = CL / 'ws-supervisor' / 'acps' / 'trust-bundle.pem'
pols = load_domain_policies(out, trust_bundle=bundle)
print(f"  加载到域: {list(pols)}")
ok = False
for did, p in pols.items():
    print(f"  {did}: revision={p.revision} status={p.status} "
          f"members={len(p.members)} issuers={len(p.issuers)} assets={len(p.assets)}")
    for (prov, skill), roles in p.assets.items():
        print(f"      asset {skill} @ {prov[-24:]} → 允许角色 {sorted(roles)}")
    for a, r in p.members.items():
        print(f"      member {a[-24:]} → {r}")
    ok = (p.status == "active" and len(p.members) >= 2 and len(p.assets) == 2)
print()
print("  ✔ 流程验证通过：双签章程被验签并解析为 active 策略" if ok
      else "  ✘ 失败：策略未达 active（被毒化）")
raise SystemExit(0 if ok else 1)
PY
