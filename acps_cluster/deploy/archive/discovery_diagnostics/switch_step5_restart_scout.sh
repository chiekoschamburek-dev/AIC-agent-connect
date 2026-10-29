#!/bin/sh
# scout 换身份：停服务 → 应用身份 → 起服务 → 验证（全流程一次跑完，中途失败给出回滚提示）
set -e
CL=/opt/wit/my-project/acps_cluster
NEW_AIC=1.2.156.3088.1.BUPT.VTF97R.GMT5S6.4SZM68.0LC5
BK=$(cat /tmp/_idswitch_backup_dir 2>/dev/null || echo /opt/wit/id-switch-backup-unknown)

echo "=== 1) 停 scout ==="
systemctl stop wit-yanshu@scout.service
sleep 2
systemctl is-active wit-yanshu@scout.service || true

echo
echo "=== 2) 应用新身份（写 enrollment/acs）==="
cd "$CL"
sudo -u wit /opt/wit/venv/bin/python deploy/discovery_diagnostics/switch_step4_apply_identity.py \
  --ws ws-scout --aic "$NEW_AIC" --creds-prefix SCOUT

echo
echo "=== 3) 确认工作区身份 ==="
/opt/wit/venv/bin/python - <<'PY'
import json
from pathlib import Path
CL = Path('/opt/wit/my-project/acps_cluster')
e = json.loads((CL / 'ws-scout/acps/enrollment.json').read_text(encoding='utf-8'))
a = json.loads((CL / 'ws-scout/acps/acs.final.json').read_text(encoding='utf-8'))
print('  enrollment identity.ref.aic =', e['identity']['ref']['aic'])
print('  enrollment status           =', e.get('status'))
print('  acs.final aic / version     =', a.get('aic'), '/', a.get('version'))
print('  acs.final 描述长度          =', len(a.get('description') or ''))
PY

echo
echo "=== 4) 启动 scout ==="
systemctl start wit-yanshu@scout.service
sleep 12
systemctl is-active wit-yanshu@scout.service

echo
echo "=== 5) 日志（最近 20 行）==="
journalctl -u wit-yanshu@scout.service -n 20 --no-pager | tail -20

echo
echo "=== 6) 端口与身份自检 ==="
ss -tlnp 2>/dev/null | grep 8445 || echo "  ✘ 8445 未监听"
echo "  回滚命令（如需）：systemctl stop wit-yanshu@scout.service && rm -rf $CL/ws-scout && tar xzf $BK/ws-scout.tar.gz -C $CL && systemctl start wit-yanshu@scout.service"
