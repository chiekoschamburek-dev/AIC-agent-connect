#!/bin/sh
# supervisor 换身份：为新 AIC 签发证书 → 应用身份 → 搬证书 → 重启 → 自检
set -e
CL=/opt/wit/my-project/acps_cluster
NEW_AIC=1.2.156.3088.1.BUPT.VTF97R.JXN8O8.98DOO8.0EB6
WS=ws-supervisor

echo "=== 0) 前置：本地/平台身份现状 ==="
/opt/wit/venv/bin/python - <<'PY'
import json
from pathlib import Path
CL = Path('/opt/wit/my-project/acps_cluster')
for w in ('ws-supervisor',):
    e = json.loads((CL / w / 'acps/enrollment.json').read_text(encoding='utf-8'))
    a = json.loads((CL / w / 'acps/acs.final.json').read_text(encoding='utf-8'))
    print(f"  {w}: enrollment.aic={e['identity']['ref']['aic']}")
    print(f"  {w}: acs.final.aic={a.get('aic')} version={a.get('version')} 描述 {len(a.get('description') or '')} 字")
PY
curl -s --max-time 10 -o /dev/null -w '  平台新 AIC HTTP %{http_code}\n' \
  "https://wt.ioa.pub/registry-server/acps-atr-v2/acs/$NEW_AIC"
curl -s --max-time 10 -o /dev/null -w '  平台旧 AIC HTTP %{http_code}\n' \
  "https://wt.ioa.pub/registry-server/acps-atr-v2/acs/1.2.156.3088.1.BUPT.VTF97R.GGWW9N.KYWRB7.0IC8"

echo
echo "=== 1) 为新 AIC 签发证书（写到 -idswitch 副本）==="
cd "$CL"
sudo -u wit TARGET_AIC="$NEW_AIC" SOURCE_WS="$CL/$WS" TEST_WS="$CL/$WS-idswitch" \
  PUBLIC_ENDPOINT=https://192.0.2.10:8444/rpc \
  /opt/wit/venv/bin/python deploy/discovery_diagnostics/switch_step2_try_ca_issue.py

echo
echo "=== 2) 应用身份到线上工作区 ==="
systemctl stop wit-yanshu@supervisor.service || true
sudo -u wit /opt/wit/venv/bin/python deploy/discovery_diagnostics/switch_step4_apply_identity.py \
  --ws "$WS" --aic "$NEW_AIC" --creds-prefix SUPERVISOR

echo
echo "=== 3) 搬证书材料到线上工作区 ==="
for d in client server; do
  cp -p "$CL/$WS-idswitch/acps/$d/certificate.pem" "$CL/$WS/acps/$d/certificate.pem"
done
cp -p "$CL/$WS-idswitch/acps/trust-bundle.pem" "$CL/$WS/acps/trust-bundle.pem"
chown wit:wit "$CL/$WS/acps/client/certificate.pem" "$CL/$WS/acps/server/certificate.pem" \
  "$CL/$WS/acps/trust-bundle.pem"
openssl x509 -in "$CL/$WS/acps/client/certificate.pem" -noout -subject -ext subjectAltName | sed 's/^/  /'

echo
echo "=== 4) 配对校验 ==="
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
echo "=== 5) 重启 supervisor ==="
systemctl start wit-yanshu@supervisor.service
sleep 20
systemctl is-active wit-yanshu@supervisor.service
journalctl -u wit-yanshu@supervisor.service -n 6 --no-pager | tail -6
