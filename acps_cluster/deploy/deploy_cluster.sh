#!/usr/bin/env bash
# 研枢集群第二批部署：scout/synthesis/writer/critic 四个常驻伙伴 + supervisor 运行脚本
# 在阿里云服务器上以 root 执行：bash acps_cluster/deploy/deploy_cluster.sh
# 幂等：可反复执行；首次会提示补 .env.production，填好后再跑一遍即可
set -euo pipefail

ROOT="${ACPS_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
VENV="$ROOT/.venv"
CLUSTER="$ROOT/acps_cluster"
ENVFILE=$CLUSTER/deploy/.env.production

cd "$CLUSTER"

# 1) 身份残留检查：本机 dev 集群的 ws-* 身份在官方平台不可复用，一律移走
moved=""
MOVE_NAMES="scout synthesis writer critic contradiction frontier supervisor"
for d in $MOVE_NAMES; do
  if [ -e "ws-$d" ]; then
    mkdir -p /tmp/ws-local-backup
    mv "ws-$d" /tmp/ws-local-backup/
    moved="$moved ws-$d"
  fi
done
echo "[1/5] 身份目录检查完成${moved:+（已移走:$moved）}"

# 2) venv 与依赖
if [ ! -x "$VENV/bin/python" ]; then
  PY=$(command -v python3.12 || command -v python3.11 || true)
  if [ -z "$PY" ]; then
    echo "!! 未找到 python3.12+，无法创建 venv（当前: $(python3 --version 2>/dev/null || echo 无)）"
    exit 1
  fi
  "$PY" -m venv "$VENV"
fi
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$ROOT/requirements.txt"
echo "[2/5] 依赖检查完成"

# 3) 集群配置：仅当缺失才追加（保护已填好的 arxiv 配置）
# 2026-10-06: judgment key ACPS_SCOUT_USERNAME->ACPS_PEP_ENABLED (old key always exists, append was skipped forever)
if ! grep -q '^ACPS_PEP_ENABLED=' "$ENVFILE"; then
  printf '\n' >> "$ENVFILE"
  cat deploy/env.cluster.append >> "$ENVFILE"
  echo "[3/5] 已把第二批配置追加到 deploy/.env.production 末尾"
else
  echo "[3/5] 第二批配置已存在，跳过追加"
fi

# 4) 配置检查（只看空值与未替换的 <...> 占位符）
set -a
. "$ENVFILE" || { echo "!! .env.production 解析失败：检查是否有未加引号的特殊字符（如 < >）"; exit 1; }
set +a
missing=""
for v in OPENAI_API_KEY ACPS_PUBLIC_HOST ACPS_PROVIDER_ORG ACPS_PROVIDER_EMAIL \
         ACPS_SCOUT_USERNAME ACPS_SCOUT_PASSWORD \
         ACPS_SYNTHESIS_USERNAME ACPS_SYNTHESIS_PASSWORD \
         ACPS_WRITER_USERNAME ACPS_WRITER_PASSWORD \
         ACPS_CRITIC_USERNAME ACPS_CRITIC_PASSWORD \
         ACPS_CONTRADICTION_USERNAME ACPS_CONTRADICTION_PASSWORD \
         ACPS_FRONTIER_USERNAME ACPS_FRONTIER_PASSWORD \
         ACPS_SUPERVISOR_USERNAME ACPS_SUPERVISOR_PASSWORD; do
  if [ -z "${!v:-}" ] || [[ "${!v}" == *"<"* ]]; then missing="$missing $v"; fi
done
if [ -n "$missing" ]; then
  echo "!! 以下配置还没填：$missing"
  echo "   编辑：nano $ENVFILE  （Ctrl+O 保存 / Ctrl+X 退出），把 <...> 替换成真实值后重跑本脚本"
  echo "   5 个 agent 各需一个官方账号：先在 https://www.ioa.pub/registry 注册"
  exit 1
fi
echo "[4/5] 配置检查通过"

# 5) 安装 systemd 模板单元并启动八个平台实例
#    2026-10-06 审查修复:补 supervisor(常驻收件箱形态)与 experiment——
#    此前脚本只装六伙伴,supervisor 实为 10-02 手工 enable,装配脚本不可信重建
#    (cpu/eval/model/kb/citation 是本地资源节点,不经 systemd,不在本清单)
install -m 644 deploy/wit-yanshu@.service /etc/systemd/system/wit-yanshu@.service
systemctl daemon-reload
for name in scout synthesis writer critic contradiction frontier experiment supervisor; do
  systemctl enable --now "wit-yanshu@$name" >/dev/null
  echo "  已启动 wit-yanshu@$name"
done
chown -R wit:wit "$ROOT"
chmod 600 "$ENVFILE"
echo "[5/5] 完成。看日志：journalctl -u wit-yanshu@scout -f"
echo "  首次预期『注册未就绪: pending_review』，每 5 分钟自动重试，审核通过后自动上线"
echo "  supervisor 等它的账号审核通过后，用 wit 用户跑：bash deploy/run_supervisor.sh \"研究任务\""
