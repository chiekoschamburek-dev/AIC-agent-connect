#!/bin/sh
# scout / critic 抢修：停止 → 重建 enrollment（模板法，AIC 用本工作区现网值）→ 重启验证
CL=/opt/wit/my-project/acps_cluster
PY=/opt/wit/venv/bin/python

echo "=== 0) 先停掉两个（避免反复重启写坏状态）==="
for n in scout critic; do
  systemctl stop "wit-yanshu@$n.service" 2>/dev/null
  printf '  %-10s %s\n' "$n" "$(systemctl is-active wit-yanshu@$n.service 2>/dev/null || echo stopped)"
done

echo
echo "=== 1) 确认证书与工作区 AIC 一致（重建前的前提）==="
$PY - <<'PY'
import json, subprocess
from pathlib import Path
CL = Path('/opt/wit/my-project/acps_cluster')
for w in ('ws-scout', 'ws-critic'):
    acps = CL / w / 'acps'
    out = subprocess.run(['openssl', 'x509', '-in', str(acps/'client'/'certificate.pem'),
                          '-noout', '-subject'], capture_output=True, text=True).stdout
    cn = out.strip().replace('subject=CN = ', '').split(',')[0].strip()
    acs = json.loads((acps/'acs.final.json').read_text(encoding='utf-8'))
    print(f"  {w}: 证书CN=…{cn[-22:]}  acs.final.aic=…{(acs.get('aic') or '')[-22:]}  一致={cn == acs.get('aic')}")
    print(f"          注册版本 v{acs.get('version')} 描述 {len(acs.get('description') or '')} 字")
PY

echo
echo "=== 2) 重建 enrollment（模板 ws-writer）==="
for w in scout critic; do
  echo "  --- ws-$w"
  $PY "$CL/deploy/discovery_diagnostics/rebuild_enrollment.py" --ws "ws-$w" --template ws-writer --apply 2>&1 | tail -7
  chown -R wit:wit "$CL/ws-$w/acps"
done

echo
echo "=== 3) 重启并观察 ==="
for n in scout critic; do
  systemctl reset-failed "wit-yanshu@$n.service" 2>/dev/null
  systemctl restart "wit-yanshu@$n.service"
done
i=0
while [ $i -lt 14 ]; do
  i=$((i+1)); sleep 30
  s=$(systemctl is-active wit-yanshu@scout.service 2>/dev/null)
  c=$(systemctl is-active wit-yanshu@critic.service 2>/dev/null)
  printf '  +%3ds: scout=%s critic=%s\n' $((i*30)) "${s:-unknown}" "${c:-unknown}"
  if [ "$s" = "active" ] && [ "$c" = "active" ]; then break; fi
done
echo
for n in scout critic; do
  echo "--- $n"
  journalctl -u "wit-yanshu@$n.service" -n 6 --no-pager | tail -6 | sed 's/^/    /'
done
echo
echo "=== 4) 端口 ==="
ss -tlnp 2>/dev/null | grep -E '8445|8448' | sed 's/^/  /'
