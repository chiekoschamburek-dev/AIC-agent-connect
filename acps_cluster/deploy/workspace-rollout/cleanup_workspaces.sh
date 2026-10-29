#!/bin/bash
# 按 task_id 精确清理联调产物：工作区目录（gc 墓碑）+ bundle 令牌文件 + 收件箱归档。
# 用法（在服务器上）：bash cleanup_workspaces.sh --task-ids "e2e-2100 e2e-2105" [--with-bundles 1]
# 只删显式列出的 task_id，绝不碰 feed-* 等真实任务。
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
PY="${PY:-$PROJECT_DIR/.venv/bin/python}"
WIT_USER="${WIT_USER:-wit}"

TASK_IDS=()
WITH_BUNDLES=1

while [ $# -gt 0 ]; do
  case "$1" in
    --task-ids) read -r -a TASK_IDS <<< "${2:-}"; shift 2 ;;
    --task-ids=*) read -r -a TASK_IDS <<< "${1#*=}"; shift ;;
    --with-bundles) WITH_BUNDLES="${2:-1}"; shift 2 ;;
    --with-bundles=*) WITH_BUNDLES="${1#*=}"; shift ;;
    -h|--help) sed -n '2,4p' "$0"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

[ "${#TASK_IDS[@]}" -gt 0 ] || { echo "用法: $0 --task-ids \"id1 id2\"" >&2; exit 2; }

cd "$PROJECT_DIR/acps_cluster"

echo "== 1) 回收工作区（gc 墓碑落 .gc-log.jsonl）=="
for t in "${TASK_IDS[@]}"; do
  if [ -d "ws-workspace/workspaces/$t" ]; then
    if id "$WIT_USER" >/dev/null 2>&1; then
      sudo -u "$WIT_USER" "$PY" partner_workspace.py --gc-task "$t" 2>&1 | tail -1
    else
      "$PY" partner_workspace.py --gc-task "$t" 2>&1 | tail -1
    fi
    echo "  gc $t"
  else
    echo "  skip $t（目录不存在）"
  fi
done

if [ "$WITH_BUNDLES" = "1" ]; then
  echo "== 2) 清理 bundle 令牌文件（按 bundle 内的工作区作用域匹配）=="
  "$PY" - "${TASK_IDS[@]}" <<'PY'
import json
import sys
from pathlib import Path

targets = set(sys.argv[1:])
removed = kept = 0
for path in Path('.').glob('ws-*/tokens/bundle-*.json'):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        kept += 1
        continue
    entries = data.get("tokens") or {}
    if any(str((v or {}).get("taskId") or "") in targets for v in entries.values()):
        path.unlink()
        removed += 1
    else:
        kept += 1
print(f"  removed={removed} kept={kept}")
PY
fi

echo "== 3) 收件箱归档 =="
for t in "${TASK_IDS[@]}"; do
  rm -f "ws-supervisor/inbox/done/$t.txt" "ws-supervisor/inbox/done/$t.heal.json"
done

echo "== 4) 结果 =="
left=0
for t in "${TASK_IDS[@]}"; do
  if [ -d "ws-workspace/workspaces/$t" ]; then
    echo "  ⚠ 仍有目录 $t"
    left=1
  fi
done
[ "$left" -eq 0 ] && echo "  测试工作区已清除"
echo "  gc log tail:"
tail -3 ws-workspace/workspaces/.gc-log.jsonl 2>/dev/null || true
