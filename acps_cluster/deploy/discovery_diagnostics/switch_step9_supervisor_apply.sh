#!/bin/sh
# supervisor 换身份（第 2 段）：应用身份 → 搬证书 → 重启（证书已在 -idswitch 副本里签好）
set -e
CL=/opt/wit/my-project/acps_cluster
NEW_AIC=1.2.156.3088.1.BUPT.VTF97R.JXN8O8.98DOO8.0EB6
WS=ws-supervisor

echo "=== 1) 停 supervisor ==="
systemctl stop wit-yanshu@supervisor.service || true
systemctl is-active wit-yanshu@supervisor.service || true

echo
echo "=== 2) 应用身份（enrollment + 权威 ACS）==="
cd "$CL"
sudo -u wit /opt/wit/venv/bin/python deploy/discovery_diagnostics/switch_step4_apply_identity.py \
  --ws "$WS" --aic "$NEW_AIC" --creds-prefix SUPERVISOR

echo
echo "=== 3) 搬证书材料 ==="
for d in client server; do
  cp -p "$CL/$WS-idswitch/acps/$d/certificate.pem" "$CL/$WS/acps/$d/certificate.pem"
done
cp -p "$CL/$WS-idswitch/acps/trust-bundle.pem" "$CL/$WS/acps/trust-bundle.pem"
chown wit:wit "$CL/$WS/acps/client/certificate.pem" "$CL/$WS/acps/server/certificate.pem" \
  "$CL/$WS/acps/trust-bundle.pem"
openssl x509 -in "$CL/$WS/acps/client/certificate.pem" -noout -subject -ext subjectAltName | sed 's/^/  /'

echo
echo "=== 4) 私钥/证书配对 ==="
/opt/wit/venv/bin/python - <<'PY'
import subprocess
from pathlib import Path
ws = Path('/opt/wit/my-project/acps_cluster/ws-supervisor/acps')
for side in ('client', 'server'):
    key = subprocess.run(['openssl','pkey','-in',str(ws/side/'private-key.pem'),'-pubout'],
                         capture_output=True, text=True).stdout
    crt = subprocess.run(['openssl','x509','-in',str(ws/side/'certificate.pem'),'-noout','-pubkey'],
                         capture_output=True, text=True).stdout
    print(f"  {side}: 配对 = {key.strip() == crt.strip()}")
PY

echo
echo "=== 5) 启动 supervisor ==="
systemctl start wit-yanshu@supervisor.service
echo "  （systemd 有 0-300s 随机抖动，等一下再看）"
for i in 1 2 3 4 5 6; do
  sleep 20
  st=$(systemctl is-active wit-yanshu@supervisor.service || true)
  echo "  +$((i*20))s: $st"
  [ "$st" = "active" ] && break
done
journalctl -u wit-yanshu@supervisor.service -n 5 --no-pager | tail -5
