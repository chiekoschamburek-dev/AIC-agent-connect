#!/usr/bin/env bash
# 全库事件裁剪（2026-10-15 审查 #2）：对每个 ws-*/acps/aip-tasks.sqlite3 执行
# prune_task_events（默认 keep-days 7 / 每任务 50 条，含 VACUUM）。
# 只裁**事件流**（aip_task_events）；aip_tasks 行与产物列不动。
# 建议由 wit-task-prune.timer（周日 04:00）调用；手动演练：bash deploy/prune_task_events_all.sh --dry-run
set -uo pipefail

CL=/opt/wit/my-project/acps_cluster
PY=/opt/wit/venv/bin/python
KEEP_DAYS="${KEEP_DAYS:-7}"
KEEP_PER_TASK="${KEEP_PER_TASK:-50}"
APPLY="--apply"
[ "${1:-}" = "--dry-run" ] && APPLY=""

cd "$CL" || exit 1
rc=0
for db in ws-*/acps/aip-tasks.sqlite3; do
  echo "== $db =="
  "$PY" deploy/prune_task_events.py --db "$PWD/$db" \
      --keep-days "$KEEP_DAYS" --keep-per-task "$KEEP_PER_TASK" $APPLY || rc=1
done
echo "[prune-all] 完成（keep-days=$KEEP_DAYS keep-per-task=$KEEP_PER_TASK apply='${APPLY:-no}')"
exit "$rc"
