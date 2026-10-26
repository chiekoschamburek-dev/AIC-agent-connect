#!/bin/sh
# 批准后的一键切换：KB 1.0.0 → 1.0.1（加宽后的门面文案上线）
#
# 为什么需要专门脚本：
#   1. 新注册会发**新的 AIC**，而域章程 yanshu-kb.r1.json 里记着 KB 的旧 AIC（provider），
#      不更新章程的话域路径会失效（provider 对不上）。
#   2. 新工作区的 trust-bundle 默认缺中间 CA → 令牌永远验签失败（KB 上次踩过）。
#   3. 切换过程中 KB 会短暂离线，必须在同一个脚本里完成"换身份→起服务→验在线→验准入"。
set -u
CL=/opt/wit/my-project/acps_cluster
PY=/opt/wit/venv/bin/python
SRC=/opt/wit/tmp/kb-reregister
DST="$CL/ws-kb"
LEADER=1.2.156.3088.1.BUPT.VTF97R.JXN8O8.98DOO8.0EB6
stamp=$(date +%Y%m%d-%H%M%S)

echo
echo "############ 0) 装回「待启用」的代码变更（门面文案 + version 1.0.1）"
# 提交注册后为安全起见，线上代码曾回退到与 1.0.0 注册一致的状态
# （否则 KB 一重启就会因指纹不匹配被拒）。切换时在这里装回。
STAGE=/opt/wit/tmp/kb-101-staged
if [ -f "$STAGE/partner_kb.py.new" ] && [ -f "$STAGE/discovery_metadata.py.new" ]; then
  cp -p "$STAGE/partner_kb.py.new" "$CL/agents/partner_kb.py"
  cp -p "$STAGE/discovery_metadata.py.new" "$CL/core/discovery_metadata.py"
  chown wit:wit "$CL/agents/partner_kb.py" "$CL/core/discovery_metadata.py"
  echo "  ✔ 已从 $STAGE 装回两个文件"
else
  echo "  ⚠ 找不到待启用文件（$STAGE）——请确认代码是否已是新版本"
fi
$PY - <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, '/opt/wit/my-project'); sys.path.insert(0, '/opt/wit/my-project/acps_cluster')
import discovery_metadata as dm
d = dm.agent_discovery_description('yanshu.kb-partner', 'x')
print(f"  投影 description 长度 = {len(d)}（目标 365）  含「查文献」= {'查文献' in d}")
PY
grep -n 'version="1.0' "$CL/agents/partner_kb.py" | sed 's/^/  /'

echo
echo "############ 1) 确认已获批并发证"
$PY - "$SRC" <<'PY'
import json, sys
from pathlib import Path
ws = Path(sys.argv[1])
d = json.loads((ws / 'acps' / 'enrollment.json').read_text(encoding='utf-8'))
r = d.get('review') or {}
m = r.get('metadata') or {}
aic = ((d.get('identity') or {}).get('ref') or {}).get('aic') or r.get('aic')
print(f"  status={d.get('status')}  approvalStatus={m.get('approvalStatus')}")
print(f"  aic={aic!r}")
if not aic:
    print("  ⏸ 尚未发证 → 现在还不能切换（等平台批准后重跑本脚本）")
    raise SystemExit(2)
if (ws / 'acps' / 'client' / 'certificate.pem').is_file():
    print("  ✔ 证书已铸出")
else:
    print("  证书尚未铸出 → 先跑一次 up_until_ready 取证书")
    raise SystemExit(3)
PY
[ $? -ne 0 ] && { echo "  终止：条件不满足"; exit 1; }

echo
echo "############ 2) 备份现有 ws-kb 身份产物"
mkdir -p "/opt/wit/tmp/kb-identity-backup-$stamp"
for f in acps/enrollment.json acps/acs.draft.json acps/acs.final.json acps/trust-bundle.pem; do
  [ -f "$DST/$f" ] && cp -p "$DST/$f" "/opt/wit/tmp/kb-identity-backup-$stamp/$(basename "$f")"
done
echo "  已备份到 /opt/wit/tmp/kb-identity-backup-$stamp"

echo
echo "############ 3) 换成新身份（AIC/证书/终稿）"
for pair in "acps/enrollment.json:acps/enrollment.json" \
            "acps/acs.draft.json:acps/acs.draft.json" \
            "acps/acs.final.json:acps/acs.final.json" \
            "acps/client:acps/client" \
            "acps/server:acps/server" \
            "acps/secrets:acps/secrets"; do
  s="${pair%%:*}"; d="${pair##*:}"
  if [ -e "$SRC/$s" ]; then
    rm -rf "$DST/$d"
    cp -rp "$SRC/$s" "$DST/$d"
    printf '  ✔ %s\n' "$d"
  fi
done
chown -R wit:wit "$DST"

echo
echo "############ 4) 重建 trust-bundle（必须含中间 CA，否则令牌验签必失败）"
sh "$CL/deploy/discovery_diagnostics/fix_trust_bundle.sh" 2>&1 | grep -E 'ws-kb' | sed 's/^/  /'
$PY - <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, '/opt/wit/my-project'); sys.path.insert(0, '/opt/wit/my-project/acps_cluster')
from delegation.token import load_certificates, load_signing_context
ws = Path('/opt/wit/my-project/acps_cluster/ws-kb/acps')
n = len(load_certificates(ws / 'trust-bundle.pem'))
sc = load_signing_context(ws / 'client', trust_bundle=ws / 'trust-bundle.pem')
print(f"  新身份 AIC = {sc.issuer_aic}")
print(f"  bundle={n} 张  signing.chain={len(sc.chain)}（需 ≥2）")
PY

echo
echo "############ 5) 重启 KB 服务"
systemctl reset-failed wit-yanshu@kb.service 2>/dev/null
systemctl restart wit-yanshu@kb.service
i=0
while [ $i -lt 14 ]; do
  i=$((i+1)); sleep 30
  st=$(systemctl is-active wit-yanshu@kb.service 2>/dev/null)
  printf '  +%3ds: %s\n' $((i*30)) "$st"
  [ "$st" = "active" ] && break
done
journalctl -u wit-yanshu@kb.service --since "-9min" --no-pager 2>/dev/null \
  | grep -viE 'systemd\[1\]' | tail -4 | sed 's/^/  /'

echo
echo "############ 6) 用新 AIC 重建域章程（章程里记着 KB 的 provider AIC）"
NEW_AIC=$($PY -c "
import json
from pathlib import Path
d = json.loads(Path('$DST/acps/enrollment.json').read_text(encoding='utf-8'))
print(((d.get('identity') or {}).get('ref') or {}).get('aic') or '')
")
echo "  KB 新 AIC = $NEW_AIC"
cd "$CL" || exit 1
sudo -u wit $PY deploy/make_kb_domain_charter.py --kb-aic "$NEW_AIC" --leader-aic "$LEADER" \
  --kb-workspace-name kb --leader-workspace-name supervisor 2>&1 | tail -6 | sed 's/^/  /'

echo
echo "############ 7) 验收：平台在线 + 准入矩阵"
sudo -u wit $PY deploy/discovery_diagnostics/verify_scout_still_online.py 2>&1 | sed -n '1,8p' | sed 's/^/  /'
sudo -u wit sh deploy/discovery_diagnostics/acceptance_kb_live.sh 2>&1 | tail -14 | sed 's/^/  /'

echo
echo "############ 8) 发现侧复核：'查文献' 这类问法现在能否命中 KB"
sudo -u wit $PY deploy/discovery_diagnostics/kb_quick_probe.py 2>&1 | sed 's/^/  /'
