#!/bin/sh
# KB 收尾第 1 步：证书链检查 + PEP 配置 + 端口对齐
CL=/opt/wit/my-project/acps_cluster
WS="$CL/ws-kb"
PY=/opt/wit/venv/bin/python

echo "=== 1) 证书链（新工作区必查：signing.chain 必须 ≥2）==="
$PY - <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, '/opt/wit/my-project'); sys.path.insert(0, '/opt/wit/my-project/acps_cluster')
from delegation.token import load_certificates, load_signing_context
ws = Path('/opt/wit/my-project/acps_cluster/ws-kb/acps')
try:
    n = len(load_certificates(ws / 'trust-bundle.pem'))
    sc = load_signing_context(ws / 'client', trust_bundle=ws / 'trust-bundle.pem')
    print(f"  bundle 证书数={n}  signing.chain={len(sc.chain)}（需 ≥2）")
    print(f"  本机 AIC={sc.issuer_aic}")
    if len(sc.chain) < 2:
        print("  ⚠ 缺中间 CA → 令牌无法验签，需执行 fix_trust_bundle.sh")
except Exception as exc:
    print(f"  读取失败 {type(exc).__name__}: {exc}")
PY

echo
echo "=== 2) 按已批准端点对齐端口（远端 ACS = 8599）==="
CUR=$(grep -E "^ACPS_KB_PORT=" "$CL/deploy/.env.production" | cut -d= -f2)
echo "  当前 ACPS_KB_PORT=$CUR"
if [ "$CUR" != "8599" ]; then
  sed -i.bak-$(date +%Y%m%d-%H%M%S) "s/^ACPS_KB_PORT=.*/ACPS_KB_PORT=8599/" "$CL/deploy/.env.production"
  echo "  已改为 8599（备份 .bak-*）"
else
  echo "  已是 8599"
fi
grep -E "^ACPS_KB_PORT=" "$CL/deploy/.env.production" | sed 's/^/  /'

echo
echo "=== 3) 落 per-agent PEP 加固（env + drop-in）==="
LEADER=1.2.156.3088.1.BUPT.VTF97R.JXN8O8.98DOO8.0EB6
cat > /etc/wit-kb-pep.env <<EOF
# 2026-10-11 KB 资源 agent 使用控制
ACPS_PEP_ENABLED=1
# 承认 leader 的签发资格（否则它签的令牌被判 issuer-not-authorized）
ACPS_PEP_LEADER_AICS=$LEADER
# 关掉"直调免令牌"通道 → leader 也必须持令牌
ACPS_PEP_LEADER_DIRECT=0
# 注意：不要设 ACPS_PEP_MY_AIC——证书已就位，应从 enrollment/证书解析
EOF
mkdir -p /etc/systemd/system/wit-yanshu@kb.service.d
cat > /etc/systemd/system/wit-yanshu@kb.service.d/pep.conf <<'EOF'
[Service]
EnvironmentFile=/etc/wit-kb-pep.env
EOF
systemctl daemon-reload
echo "  ✔ /etc/wit-kb-pep.env + drop-in 已写入并 daemon-reload"
systemctl show -p EnvironmentFiles --value wit-yanshu@kb.service 2>/dev/null | tr ' ' '\n' | sed 's/^/      /'

echo
echo "=== 4) PEP 口径（带 env 复核，服务将会看到的）==="
sudo -u wit YANSHU_RUN_TRACE=0 $PY - <<'PY'
import os, sys
from pathlib import Path
CL = Path('/opt/wit/my-project/acps_cluster')
def load(p):
    if not p.is_file(): return
    for ln in p.read_text(encoding='utf-8').splitlines():
        s = ln.strip()
        if s and not s.startswith('#') and '=' in s:
            k, v = s.split('=', 1)
            os.environ[k.strip()] = v.strip().strip('"').strip("'")
load(CL / 'deploy' / '.env.production')
load(Path('/etc/wit-kb-pep.env'))
sys.path.insert(0, '/opt/wit/my-project'); sys.path.insert(0, str(CL))
from delegation.pep import load_pep_config
cfg = load_pep_config(CL / 'ws-kb')
print(f"  enabled={cfg.enabled} direct_open={cfg.leader_direct_open} "
      f"leader_aics={len(cfg.leader_aics)} my_aic={cfg.my_aic[-24:]}")
print(f"  公开能力={sorted(cfg.public_skills)}  含非公开={cfg.has_non_public_skills}")
PY
