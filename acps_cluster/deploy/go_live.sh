#!/usr/bin/env bash
# 研枢集群：一键上线（模型 → 计量 → 转发 → 派发）全链路自检与部署。
#
#   bash /opt/wit/my-project/acps_cluster/deploy/go_live.sh              # 自检 + 试点
#   bash /opt/wit/my-project/acps_cluster/deploy/go_live.sh --dry-run    # 只看会做什么
#   bash /opt/wit/my-project/acps_cluster/deploy/go_live.sh --full       # 试点通过后全量
#   bash /opt/wit/my-project/acps_cluster/deploy/go_live.sh --verify     # 只做验收，不改任何东西
#
# 设计原则（都是踩过的坑换来的）：
#   1. **先自检，再动手**：任何一项阻塞都不往下走，避免"跑到第 5 步才发现第 1 步没做"；
#   2. **先试点，再全量**：默认只重启 writer，确认 access 真的有数据再放开；
#   3. **每步可单独执行**：脚本调用的都是独立命令，失败时照着提示重跑那一条即可；
#   4. **幂等**：重复执行不会重复改（补丁脚本自己判断）。
set -uo pipefail

CL=/opt/wit/my-project/acps_cluster
PY=/opt/wit/venv/bin/python
MODE=preview
FULL=0
VERIFY_ONLY=0
SKIP_MODEL_CALL=0

for arg in "$@"; do
  case "$arg" in
    --dry-run) MODE=dry ;;
    --full)    FULL=1 ;;
    --verify)  VERIFY_ONLY=1 ;;
    --skip-model-call) SKIP_MODEL_CALL=1 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "未知参数: $arg"; exit 2 ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  echo ">> 请用 root 执行（要装 systemd 单元、重启服务）"
  echo "   若只想看现状：sudo -u wit $PY $CL/deploy/preflight_check.py"
  exit 1
fi

cd "$CL" || { echo ">> 集群目录不存在: $CL"; exit 1; }

hr() { printf '%.0s─' $(seq 1 66); echo; }
step() { echo; hr; echo " $1"; hr; }

# ─────────────────────────────────────────────────────────────
# 阶段 0：自检
# ─────────────────────────────────────────────────────────────
step "阶段 0/5  部署前自检"

PREFLIGHT_ARGS=""
[ "$SKIP_MODEL_CALL" -eq 1 ] && PREFLIGHT_ARGS="--skip-model-call"
sudo -u wit "$PY" deploy/preflight_check.py $PREFLIGHT_ARGS
PRE_RC=$?

if [ "$PRE_RC" -eq 1 ]; then
  echo
  echo ">> 有阻塞项。上面每条都写了怎么修；修完重跑本脚本。"
  echo "   常见三项的修法："
  echo "     python3 deploy/patch_amp_access.py          # 挂计量钩子 + 收窄重启清理"
  echo "     /opt/wit/venv/bin/pip install kafka-python  # access 转发器依赖"
  echo "     ACPS_MODEL_PRIMARY=none                     # 网关不可达时改走保底"
  exit 1
fi

if [ "$PRE_RC" -eq 2 ]; then
  echo
  echo ">> 无阻塞项，但有告警（见上）。默认继续；要中断可 Ctrl-C。"
  sleep 3
fi

if [ "$VERIFY_ONLY" -eq 1 ]; then
  step "仅验收模式：自检完成，未做任何修改"
  exit 0
fi

if [ "$MODE" = "dry" ]; then
  step "dry-run：下面这些会真执行（本次不做）"
  cat <<'PLAN'
  1. 打补丁：给 14 个 agent 挂 AmpAccessLoggingHooks；收窄重启清理范围
  2. 装并启动 access 转发器（wit-amp-access-sender）
  3. 重启 writer 试点，等 amp_access_*.jsonl 出现
  4. 派 1 个真实任务给 writer，确认主用模型 + 调用成功
  5. 报告 access 记录与路由统计；提示下一步（--full）
PLAN
  exit 0
fi

# ─────────────────────────────────────────────────────────────
# 阶段 1：备份
# ─────────────────────────────────────────────────────────────
step "阶段 1/5  备份"
STAMP=$(date +%Y%m%d-%H%M%S)
BAK=/opt/wit/yanshu-golive-backup-$STAMP
mkdir -p "$BAK"
cp -a "$CL"/supervisor.py "$CL"/partner_*.py "$BAK"/ 2>/dev/null
cp -a "$CL"/agents.py "$CL"/model_router.py "$CL"/llm_tools.py "$BAK"/ 2>/dev/null
cp -a "$CL"/feed_tasks.py "$BAK"/ 2>/dev/null
mkdir -p "$BAK/deploy"
cp -a "$CL"/deploy/wit-yanshu@.service "$CL"/deploy/run_supervisor.sh \
      "$CL"/deploy/.env.production "$BAK/deploy/" 2>/dev/null
echo "   备份目录: $BAK"
echo "   回滚:     cp -a $BAK/. $CL/ 2>/dev/null; cp -a $BAK/deploy/. $CL/deploy/"

# ─────────────────────────────────────────────────────────────
# 阶段 2：计量补丁
# ─────────────────────────────────────────────────────────────
step "阶段 2/5  挂计量钩子（决定调用是否被计数）"
sudo -u wit "$PY" deploy/patch_amp_access.py || {
  echo ">> 补丁失败，终止（已备份，可回滚）"; exit 1; }

# ─────────────────────────────────────────────────────────────
# 阶段 3：access 转发器
# ─────────────────────────────────────────────────────────────
step "阶段 3/5  安装 access 转发器（本地 jsonl → Kafka amp.access）"
install -m 644 "$CL/deploy/wit-amp-access-sender.service" /etc/systemd/system/
systemctl daemon-reload

if ! systemctl is-active --quiet wit-amp-access-sender; then
  sudo -u wit "$PY" "$CL/deploy/amp_access_sender.py" --init-offsets || true
  systemctl enable --now wit-amp-access-sender.service
fi
sleep 2
systemctl is-active --quiet wit-amp-access-sender \
  && echo "   ✔ 转发器运行中" \
  || { echo "   ⚠ 转发器没起来，看：journalctl -u wit-amp-access-sender -n 30"; }

# ─────────────────────────────────────────────────────────────
# 阶段 4：试点重启 writer + 派一个真实任务
# ─────────────────────────────────────────────────────────────
step "阶段 4/5  试点：重启 writer（只这一个）"
systemctl restart wit-yanshu@writer
echo "   已重启；systemd 有 0-300s 随机抖动，等待就绪（最多 5 分钟）……"

WRITER_READY=0
for _ in $(seq 1 60); do
  sleep 5
  if systemctl is-active --quiet wit-yanshu@writer; then
    if ls "$CL"/ws-writer/logs/amp_*_*.jsonl >/dev/null 2>&1 \
       || ls "$CL"/ws-writer/logs/amp_heartbeat_*.jsonl >/dev/null 2>&1; then
      WRITER_READY=1
      break
    fi
  fi
done
[ "$WRITER_READY" -eq 1 ] && echo "   ✔ writer 已就绪" \
                          || echo "   ⚠ 5 分钟内未见日志，继续但留意下方结果"

echo
echo "   → 派 1 个真实任务给 writer（走主用模型；这一步会真的调模型）"
sudo -u wit "$PY" feed_tasks.py --limit 1 --targets writer --wait 300 --rate 0 \
  --state "$CL/.feeder" 2>&1 | sed 's/^/     /'

echo
echo "   → 检查 writer 侧 access 记录"
ACCESS_FILES=$(ls "$CL"/ws-writer/logs/amp_access_*.jsonl 2>/dev/null | head -1)
if [ -n "$ACCESS_FILES" ]; then
  echo "     ✔ $ACCESS_FILES"
  tail -1 "$ACCESS_FILES" | head -c 400; echo
else
  echo "     ✘ 未生成 amp_access_*.jsonl"
  echo "       可能原因：① 钩子没生效（重启没成功）② 任务没打进去（看上面的派发结果）"
  echo "       排查：grep -n amp_hooks $CL/partner_writer.py"
fi

# ─────────────────────────────────────────────────────────────
# 阶段 5：结论
# ─────────────────────────────────────────────────────────────
step "阶段 5/5  结论"

echo " 模型路由（最近一次启动）:"
journalctl -u wit-yanshu@writer -n 200 --no-pager 2>/dev/null \
  | grep -E '\[model\]' | tail -2 | sed 's/^/   /' || echo "   （无记录）"

echo
echo " 账本:"
sudo -u wit "$PY" feed_tasks.py --stats 2>/dev/null | sed 's/^/   /' || echo "   （无账本）"

echo
if [ "$FULL" -eq 1 ]; then
  echo " 全量重启其余 6 个伙伴（每个有 0-300s 抖动，依次起来）……"
  for name in scout synthesis critic contradiction frontier experiment; do
    echo "   restart wit-yanshu@$name"
    systemctl restart "wit-yanshu@$name"
    sleep 5
  done
  echo "   ✔ 已全部下发重启"
else
  cat <<'NEXT'
 试点已完成。建议按顺序确认后再全量：

   ① 看平台是否开始记录（30–60 分钟后，榜单按小时聚合）
      curl -s "https://wt.ioa.pub/internal/matched-stats/top?startAt=$(date +%Y-%m-%dT%H:00:00+08:00)&endAt=$(date -d '+1 hour' +%Y-%m-%dT%H:00:00+08:00)&n=200" \
        | python3 -c "import json,sys; d=json.load(sys.stdin); print([x for x in d['callee'] if 'VTF97R' in x['aic']])"

   ② 全量重启
      bash deploy/go_live.sh --full

   ③ 开启常驻派发（每 15 分钟 2 个）
      install -m 644 deploy/wit-task-feeder.service /etc/systemd/system/
      systemctl daemon-reload && systemctl enable --now wit-task-feeder

 回滚:
      cp -a /opt/wit/yanshu-golive-backup-*/. /opt/wit/my-project/acps_cluster/
      systemctl disable --now wit-amp-access-sender
      systemctl restart wit-yanshu@writer
NEXT
fi

exit 0
