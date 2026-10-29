#!/bin/sh
# 清理上一轮测试误写进 ws-supervisor 的章程，并重跑验证（这次写临时目录）
CL=/opt/wit/my-project/acps_cluster
PY=/opt/wit/venv/bin/python
STRAY="$CL/ws-supervisor/acps/domains"

echo "=== 1) 清理误写文件（先备份到 /tmp 以免误删有用的东西）==="
if [ -f "$STRAY/yanshu-kb.r1.json" ]; then
  cp -p "$STRAY/yanshu-kb.r1.json" /tmp/yanshu-kb.r1.json.stray-backup
  rm -f "$STRAY/yanshu-kb.r1.json"
  rmdir "$STRAY" 2>/dev/null && echo "  已删除误写文件并移除空目录（备份在 /tmp/yanshu-kb.r1.json.stray-backup）" \
    || echo "  已删除误写文件（目录非空，保留）"
  ls -la "$STRAY" 2>/dev/null | sed 's/^/  /'
else
  echo "  （无误写文件）"
fi

echo
echo "=== 2) 重跑：这次显式指定 --out-dir 到临时目录 ==="
rm -rf /tmp/kb-charter-test; mkdir -p /tmp/kb-charter-test
cd "$CL" || exit 1
sudo -u wit $PY deploy/make_kb_domain_charter.py \
  --kb-aic 1.2.156.3088.1.BUPT.VTF97R.KBAAAA.KBKBKB.KB01 \
  --leader-aic 1.2.156.3088.1.BUPT.VTF97R.JXN8O8.98DOO8.0EB6 \
  --kb-workspace-name supervisor \
  --leader-workspace-name scout \
  --out-dir /tmp/kb-charter-test 2>&1 | sed 's/^/  /'

echo
echo "=== 3) 独立复核：从临时目录加载并检查策略内容 ==="
sudo -u wit YANSHU_RUN_TRACE=0 $PY - <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, '/opt/wit/my-project')
sys.path.insert(0, '/opt/wit/my-project/acps_cluster')
from delegation.domain_manifest import load_domain_policies
bundle = Path('/opt/wit/my-project/acps_cluster/ws-supervisor/acps/trust-bundle.pem')
pols = load_domain_policies(Path('/tmp/kb-charter-test'), trust_bundle=bundle)
print(f"  加载到域: {list(pols)}")
for did, p in pols.items():
    print(f"  {did}: revision={p.revision} status={p.status} "
          f"members={len(p.members)} issuers={len(p.issuers)} assets={len(p.assets)}")
    for (prov, skill), roles in p.assets.items():
        print(f"      asset {skill} @ {prov[-24:]} → 允许角色 {sorted(roles)}")
    print(f"      members: {[(a[-24:], r) for a, r in p.members.items()]}")
PY

echo
echo "=== 4) 确认现网工作区已干净 ==="
find "$CL" -name 'yanshu-kb*' 2>/dev/null | sed 's/^/  /' || true
echo "  （上面若只剩 /tmp 下的备份即为干净）"
