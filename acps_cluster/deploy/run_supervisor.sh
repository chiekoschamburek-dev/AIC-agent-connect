#!/usr/bin/env bash
# 用 wit 用户手动跑一次 Supervisor：注册→(等审核)→上线→执行任务→退出（不常驻）
#   bash /opt/wit/my-project/acps_cluster/deploy/run_supervisor.sh "研究任务文本"
# 输出同时写入 ws-supervisor/supervisor_run.log
set -uo pipefail

# 必须以 wit 用户运行：root 跑会把 ws-supervisor 写成 root 属主，之后 wit 跑不了（EPERM chmod）
if [ "$(id -un)" != "wit" ]; then
  echo ">> 请改用 wit 用户运行（当前是 $(id -un)）："
  echo "   runuser -u wit -- bash $0 \"任务文本\""
  exit 1
fi

CLUSTER=/opt/wit/my-project/acps_cluster
cd "$CLUSTER"

set -a
. deploy/.env.production
set +a

mkdir -p ws-supervisor/logs
rm -f ws-supervisor/logs/*.jsonl   # 重启时 AMP 心跳 journal 校验会失败，先清掉

# 预热 discovery 缓存：平台的发现接口与 wit 2.1.0 客户端不兼容（onlyAvailable 字段已移除 → 400），
# 不预热的话委派会报 "Partner has no discovered Direct JSON-RPC endpoint"
/opt/wit/venv/bin/python deploy/seed_discovery.py || echo ">> 缓存预热失败或不完整（rc≠0 = 有伙伴缺项，见上方 ⚠ 缺 N 项）：委派这些伙伴会解析不到端点"

TASK="${1:-调研 GNN 在知识图谱补全中的最新进展，写一篇带引用的 mini survey 初稿并送审}"
LOG=ws-supervisor/supervisor_run.log

/opt/wit/venv/bin/python supervisor.py "$TASK" 2>&1 | tee "$LOG"
status="${PIPESTATUS[0]}"

if grep -q "注册未就绪" "$LOG"; then
  echo ""
  echo ">> 注册还没通过（官方审核中）：等审核通过后再执行同一条命令即可，本次任务没有执行。"
fi
exit "$status"
