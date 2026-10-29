#!/bin/sh
# 换身份（通用）：把某个工作区切到平台上已注册的新 AIC
# 用法：sh switch_ws_to_aic.sh <ws-name> <new-aic> <creds-prefix> <port>
set -e
WS="$1"; AIC="$2"; PREFIX="$3"; PORT="$4"
CL=/opt/wit/my-project/acps_cluster
INSTANCE="${WS#ws-}"

[ -z "$WS" ] || [ -z "$AIC" ] || [ -z "$PREFIX" ] || [ -z "$PORT" ] && {
  echo "用法：$0 <ws-name> <new-aic> <creds-prefix> <port>"; exit 2; }

echo "======================================================================"
echo "### $WS → $AIC   (实例 $INSTANCE, 端口 $PORT)"

echo
echo "--- 0) 现状"
/opt/wit/venv/bin/python - "$WS" <<'PY'
import json, sys, subprocess
from pathlib import Path
ws = Path('/opt/wit/my-project/acps_cluster') / sys.argv[1] / 'acps'
e = json.loads((ws / 'enrollment.json').read_text(encoding='utf-8'))
print(f"    旧 AIC={e['identity']['ref']['aic']}  status={e.get('status')}")
PY

echo
echo "--- 1) 停服务"
systemctl stop "wit-yanshu@$INSTANCE.service" 2>/dev/null || true

echo
echo "--- 2) 为新 AIC 签发证书（写进 -idswitch 副本）"
cd "$CL"
sudo -u wit TARGET_AIC="$AIC" SOURCE_WS="$CL/$WS" TEST_WS="$CL/$WS-idswitch" \
  PUBLIC_ENDPOINT="https://192.0.2.10:$PORT/rpc" \
  /opt/wit/venv/bin/python deploy/discovery_diagnostics/switch_step2_try_ca_issue.py 2>&1 | tail -12

echo
echo "--- 3) 应用身份（enrollment + 平台权威 ACS）"
sudo -u wit /opt/wit/venv/bin/python deploy/discovery_diagnostics/switch_step4_apply_identity.py \
  --ws "$WS" --aic "$AIC" --creds-prefix "$PREFIX" 2>&1 | tail -14

echo
echo "--- 4) 搬证书材料"
for d in client server; do
  cp -p "$CL/$WS-idswitch/acps/$d/certificate.pem" "$CL/$WS/acps/$d/certificate.pem"
done
cp -p "$CL/$WS-idswitch/acps/trust-bundle.pem" "$CL/$WS/acps/trust-bundle.pem"
chown wit:wit "$CL/$WS/acps/client/certificate.pem" "$CL/$WS/acps/server/certificate.pem" \
  "$CL/$WS/acps/trust-bundle.pem"
openssl x509 -in "$CL/$WS/acps/client/certificate.pem" -noout -subject | sed 's/^/    /'

echo
echo "--- 5) 配对校验"
/opt/wit/venv/bin/python - "$WS" <<'PY'
import subprocess, sys
from pathlib import Path
ws = Path('/opt/wit/my-project/acps_cluster') / sys.argv[1] / 'acps'
for side in ('client', 'server'):
    key = subprocess.run(['openssl','pkey','-in',str(ws/side/'private-key.pem'),'-pubout'],
                         capture_output=True, text=True).stdout
    crt = subprocess.run(['openssl','x509','-in',str(ws/side/'certificate.pem'),'-noout','-pubkey'],
                         capture_output=True, text=True).stdout
    print(f"    {side}: 配对={key.strip() == crt.strip()}")
PY

echo
echo "--- 6) 重启并等待"
systemctl start "wit-yanshu@$INSTANCE.service"
i=0
while [ $i -lt 13 ]; do
  i=$((i+1)); sleep 30
  st=$(systemctl is-active "wit-yanshu@$INSTANCE.service" 2>/dev/null || echo unknown)
  printf '    +%3ds: %s\n' $((i*30)) "$st"
  [ "$st" = "active" ] && break
done
journalctl -u "wit-yanshu@$INSTANCE.service" -n 4 --no-pager | tail -4 | sed 's/^/    /'
