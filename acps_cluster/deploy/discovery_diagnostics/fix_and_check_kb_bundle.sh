#!/bin/sh
# 修 KB 的 trust-bundle（补中间 CA）并复核
sh /opt/wit/my-project/acps_cluster/deploy/discovery_diagnostics/fix_trust_bundle.sh 2>&1 \
  | grep -E 'ws-kb|已补|跳过|bundle 证书数' | head -10 | sed 's/^/  /'

echo
echo "=== 针对 ws-kb 的专项复核 ==="
/opt/wit/venv/bin/python - <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, '/opt/wit/my-project')
sys.path.insert(0, '/opt/wit/my-project/acps_cluster')
from delegation.token import load_certificates, load_signing_context

ws = Path('/opt/wit/my-project/acps_cluster/ws-kb/acps')
n = len(load_certificates(ws / 'trust-bundle.pem'))
sc = load_signing_context(ws / 'client', trust_bundle=ws / 'trust-bundle.pem')
print(f"  ws-kb: bundle={n} 张  signing.chain={len(sc.chain)}（需 ≥2）")
print(f"  本机 AIC = {sc.issuer_aic}")
bundle = ws / 'trust-bundle.pem'
for c in load_certificates(bundle):
    print(f"    {c.subject.rfc4514_string()[:70]}")
PY
