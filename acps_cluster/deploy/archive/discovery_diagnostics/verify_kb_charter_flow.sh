#!/bin/sh
# 验证 yanshu-kb 章程脚本的完整签名+回读（用两个真实工作区做签名者，
# 因为 ws-kb 尚未注册；KB 注册后把 --kb-workspace-name 换回 kb 即可）。
CL=/opt/wit/my-project/acps_cluster
PY=/opt/wit/venv/bin/python
OUT=/tmp/kb-charter-test

rm -rf "$OUT"; mkdir -p "$OUT"

echo "=== 1) 生成 + 双签 + 回读验签（写到临时目录，不动现网）==="
cd "$CL" || exit 1
sudo -u wit $PY deploy/make_kb_domain_charter.py \
  --kb-aic 1.2.156.3088.1.BUPT.VTF97R.KBAAAA.KBKBKB.KB01 \
  --leader-aic 1.2.156.3088.1.BUPT.VTF97R.JXN8O8.98DOO8.0EB6 \
  --kb-workspace-name supervisor \
  --leader-workspace-name scout \
  2>&1 | sed 's/^/  /'

echo
echo "=== 2) 说明：上面把「KB 签名者」临时用 supervisor、leader 用 scout 代替 ==="
echo "    （ws-kb 尚未注册，注册后应改为 --kb-workspace-name kb）"
echo "    目的：验证「两方不同签名者 → 达法定人数 → 回读验签通过」这条链路。"

echo
echo "=== 3) 独立复核：直接 load 并检查策略内容 ==="
sudo -u wit YANSHU_RUN_TRACE=0 $PY - "$CL" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, '/opt/wit/my-project')
sys.path.insert(0, '/opt/wit/my-project/acps_cluster')
from delegation.domain_manifest import load_domain_policies
CL = Path(sys.argv[1])
bundle = CL / 'ws-supervisor' / 'acps' / 'trust-bundle.pem'
pols = load_domain_policies(Path('/tmp/kb-charter-test'), trust_bundle=bundle)
print(f"  加载到域: {list(pols)}")
for did, p in pols.items():
    print(f"  {did}: revision={p.revision} status={p.status}")
    print(f"      members={len(p.members)} issuers={len(p.issuers)} assets={len(p.assets)}")
    for (prov, skill), roles in p.assets.items():
        print(f"      asset {skill} @ {prov[-24:]} → 角色 {sorted(roles)}")
PY
