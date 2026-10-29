#!/usr/bin/env bash
# 在阿里云服务器上执行：bash deploy/deploy_partner.sh
# 前提：仓库已上传到服务器；deploy/.env.production 已填好占位符
set -euo pipefail

ROOT="${ACPS_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
VENV="$ROOT/.venv"
CLUSTER="$ROOT/acps_cluster"

cd "$CLUSTER"

# 1) Python 版本检查（wit wheel 要求 cp312+）
PY=$(command -v python3.12 || command -v python3.11 || true)
if [ -z "$PY" ]; then
  echo "!! 未找到 python3.12+（当前: $(python3 --version 2>/dev/null || echo 无)）"
  echo "   Ubuntu24: apt install python3.12-venv | AlibabaCloudLinux: yum install python3.12"
  exit 1
fi
echo "[1/5] 使用 $PY ($($PY --version))"

# 2) 清掉本机 dev 集群的身份残留（服务器必须用官方平台新身份重新注册）
if [ -d ws-partner ] || ls ws-leader* >/dev/null 2>&1; then
  mkdir -p /tmp/ws-local-backup
  for d in ws-partner ws-partner.bak-http ws-leader ws-leader.bak-noendpoint; do
    [ -e "$d" ] && mv "$d" /tmp/ws-local-backup/ && echo "  已移走 $d -> /tmp/ws-local-backup/"
  done
fi
echo "[2/5] 身份目录检查完成"

# 3) venv + 依赖
if [ ! -x "$VENV/bin/python" ]; then
  "$PY" -m venv "$VENV"
fi
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$ROOT/requirements.txt"
echo "[3/5] 依赖安装完成：$("$VENV/bin/pip" show wit-framework | head -2 | tr '\n' ' ')"

# 4) 环境变量检查（只查空值，不打印值避免泄露密钥）
set -a; . deploy/.env.production; set +a
for v in ACPS_PARTNER_USERNAME ACPS_PARTNER_PASSWORD OPENAI_API_KEY ACPS_PUBLIC_HOST \
         ACPS_PROVIDER_ORG ACPS_PROVIDER_EMAIL; do
  if [ -z "${!v:-}" ] || [[ "${!v}" == *\<* ]]; then
    echo "!! deploy/.env.production 里 $v 还没填（当前值: ${!v:-<空>}）"; exit 1
  fi
done
echo "[4/5] 配置检查通过 (ACPS_PLATFORM=${ACPS_PLATFORM:-custom})"

# 5) 前台试跑一次（看注册走到哪一步；Ctrl+C 退出后再装 systemd）
echo "[5/5] 前台试跑 partner_arxiv.py —— 预期输出：首次=『注册未就绪: pending_review』"
echo "      若报网络错误：检查阿里云安全组出入方向 8443 及能否访问 www.ioa.pub"
echo "--------------------------------------------------------"
echo "[5/5] partner_arxiv.py retired 2026-10-04; source removed from public snapshot"
echo "      to enable other partners: copy this script, change module, systemctl enable --now wit-yanshu@<name>"
exit 0
