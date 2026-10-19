#!/bin/sh
# 重跑章程验证：输出到 wit 可写的临时目录
CL=/opt/wit/my-project/acps_cluster
PY=/opt/wit/venv/bin/python
OUT=/opt/wit/tmp/kb-charter-test

echo "=== 0) 准备 wit 可写的输出目录 ==="
rm -rf "$OUT"
mkdir -p "$OUT"
chown -R wit:wit /opt/wit/tmp
ls -ld "$OUT" | sed 's/^/  /'

echo
echo "=== 1) 生成 + 双签 + 回读（显式 --out-dir）==="
cd "$CL" || exit 1
sudo -u wit $PY deploy/make_kb_domain_charter.py \
  --kb-aic 1.2.156.3088.1.BUPT.VTF97R.KBAAAA.KBKBKB.KB01 \
  --leader-aic 1.2.156.3088.1.BUPT.VTF97R.JXN8O8.98DOO8.0EB6 \
  --kb-workspace-name supervisor \
  --leader-workspace-name scout \
  --out-dir "$OUT" 2>&1 | sed 's/^/  /'

echo
echo "=== 2) 独立复核：加载并检查策略 ==="
sudo -u wit YANSHU_RUN_TRACE=0 $PY - "$OUT" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, '/opt/wit/my-project')
sys.path.insert(0, '/opt/wit/my-project/acps_cluster')
from delegation.domain_manifest import load_domain_policies
out = Path(sys.argv[1])
bundle = Path('/opt/wit/my-project/acps_cluster/ws-supervisor/acps/trust-bundle.pem')
pols = load_domain_policies(out, trust_bundle=bundle)
print(f"  加载到域: {list(pols)}")
for did, p in pols.items():
    print(f"  {did}: revision={p.revision} status={p.status} "
          f"members={len(p.members)} issuers={len(p.issuers)} assets={len(p.assets)}")
    for (prov, skill), roles in p.assets.items():
        print(f"      asset {skill} @ {prov[-24:]} → 允许角色 {sorted(roles)}")
    for a, r in p.members.items():
        print(f"      member {a[-24:]} → {r}")
PY
