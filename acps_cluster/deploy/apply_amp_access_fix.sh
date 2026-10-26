#!/usr/bin/env bash
# 一键修复"排行榜调用量为 0"的计量断点（在服务器上以 root 执行）。
#
#   bash /opt/wit/my-project/acps_cluster/deploy/apply_amp_access_fix.sh [--dry-run]
#
# 做四件事：
#   1. 给 14 个 agent 模块挂 AmpAccessLoggingHooks（幂等补丁，带 .bak）
#   2. 收窄重启时的日志清理范围（别把 access 证据删了）
#   3. 安装并启动 access 转发器服务（本地 jsonl → Kafka amp.access）
#   4. 打印验证命令
#
# 设计原则：**先试点、后全量**。默认只重启 writer，确认有数据再全量。
set -uo pipefail

CL=/opt/wit/my-project/acps_cluster
PY=/opt/wit/venv/bin/python
DRY=""
[ "${1:-}" = "--dry-run" ] && DRY="--dry-run"

if [ "$(id -u)" -ne 0 ]; then
  echo ">> 请用 root 执行（需要改 systemd 单元并重启服务）"; exit 1
fi

echo "=============================================="
echo " 1/5  备份关键文件"
echo "=============================================="
STAMP=$(date +%Y%m%d-%H%M%S)
BAK=/opt/wit/amp-access-fix-backup-$STAMP
mkdir -p "$BAK"
cp -a "$CL"/supervisor.py "$CL"/partner_*.py "$BAK"/ 2>/dev/null
cp -a "$CL"/deploy/wit-yanshu@.service "$BAK"/ 2>/dev/null
cp -a "$CL"/deploy/run_supervisor.sh "$BAK"/ 2>/dev/null
echo "   备份目录: $BAK"
ls -1 "$BAK" | sed 's/^/     /'

echo
echo "=============================================="
echo " 2/5  挂载 AMP access 钩子（${DRY:-真改}）"
echo "=============================================="
cd "$CL" || exit 1
sudo -u wit "$PY" deploy/patch_amp_access.py $DRY || exit 1

if [ -n "$DRY" ]; then
  echo
  echo ">> dry-run 结束。确认上面列出的改动无误后，去掉 --dry-run 重跑。"
  exit 0
fi

echo
echo "=============================================="
echo " 3/5  安装 access 转发器"
echo "=============================================="
install -m 644 "$CL/deploy/wit-amp-access-sender.service" /etc/systemd/system/
systemctl daemon-reload
# 首次上线：位点置到文件末尾，不回灌历史（历史 access 本来也不存在）
sudo -u wit "$PY" "$CL/deploy/amp_access_sender.py" --init-offsets
systemctl enable --now wit-amp-access-sender.service
sleep 2
systemctl --no-pager --lines=5 status wit-amp-access-sender.service

echo
echo "=============================================="
echo " 4/5  试点重启 writer（先只动一个）"
echo "=============================================="
systemctl restart wit-yanshu@writer
echo "   已重启 writer；抖动 0-300s，等它就绪……"
for i in $(seq 1 60); do
  sleep 5
  if systemctl is-active --quiet wit-yanshu@writer; then
    if ls "$CL"/ws-writer/logs/amp_access_*.jsonl >/dev/null 2>&1; then
      echo "   ✔ access 日志已出现："
      ls -l "$CL"/ws-writer/logs/amp_access_*.jsonl | sed 's/^/     /'
      break
    fi
  fi
  [ "$i" -eq 60 ] && echo "   ⚠ 仍未出现 amp_access 日志，见下面的排查提示"
done

echo
echo "=============================================="
echo " 5/5  下一步（人工确认）"
echo "=============================================="
cat <<'TIP'
  ① 打一次真实调用给 writer（经 mTLS 的 AIP 委派，不是本机脚本），然后：
       tail -1 /opt/wit/my-project/acps_cluster/ws-writer/logs/amp_access_*.jsonl | python3 -m json.tool
     期望看到 log_type=access，且 body 里有 caller / callee。

  ② 看转发器有没有投出去：
       journalctl -u wit-amp-access-sender -n 20 --no-pager

  ③ 确认没问题后全量重启（其余 6 个常驻伙伴）：
       systemctl restart wit-yanshu@{scout,synthesis,critic,contradiction,frontier,experiment}

  ④ 30–60 分钟后查平台榜单（按小时聚合，有延迟）：
       curl -s "https://wt.ioa.pub/internal/matched-stats/top?startAt=$(date +%Y-%m-%dT%H:00:00+08:00)&endAt=$(date -d '+1 hour' +%Y-%m-%dT%H:00:00+08:00)&n=200" \
         | python3 -c "import json,sys; d=json.load(sys.stdin); print([x for x in d['callee'] if 'VTF97R' in x['aic']])"

  回滚：
       # 代码侧（.bak 精确还原）
       cd /opt/wit/my-project/acps_cluster && sudo -u wit /opt/wit/venv/bin/python deploy/patch_amp_access.py --revert
       # 转发器
       systemctl disable --now wit-amp-access-sender.service
       # 全套回滚（含 systemd 单元）
       cp -a /opt/wit/amp-access-fix-backup-*/. /opt/wit/my-project/acps_cluster/deploy/ 2>/dev/null
       systemctl daemon-reload && systemctl restart wit-yanshu@writer
TIP
