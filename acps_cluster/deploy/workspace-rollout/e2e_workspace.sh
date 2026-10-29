#!/bin/bash
# 走 supervisor 收件箱跑一次真实工作区 E2E（建工作区 → 写 shared → 伙伴读回并写 products）。
# 用法（在服务器上）：
#   bash e2e_workspace.sh --task-id e2e-2100 [--partner scout] [--marker "联调标记"] \
#        [--create 1] [--timeout 600]
# 说明：收件箱 run 不挂 aip_submit，所以以 run-trace 的 run_end 作为完成信号。
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
PY="${PY:-$PROJECT_DIR/.venv/bin/python}"
WIT_USER="${WIT_USER:-wit}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

TASK_ID=""
PARTNER="scout"
MARKER=""
CREATE=1
TIMEOUT=600

while [ $# -gt 0 ]; do
  case "$1" in
    --task-id) TASK_ID="${2:-}"; shift 2 ;;
    --task-id=*) TASK_ID="${1#*=}"; shift ;;
    --partner) PARTNER="${2:-}"; shift 2 ;;
    --partner=*) PARTNER="${1#*=}"; shift ;;
    --marker) MARKER="${2:-}"; shift 2 ;;
    --marker=*) MARKER="${1#*=}"; shift ;;
    --create) CREATE="${2:-1}"; shift 2 ;;
    --create=*) CREATE="${1#*=}"; shift ;;
    --timeout) TIMEOUT="${2:-600}"; shift 2 ;;
    --timeout=*) TIMEOUT="${1#*=}"; shift ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

[ -n "$TASK_ID" ] || { echo "用法: $0 --task-id <id> [--partner key]" >&2; exit 2; }
[[ "$TASK_ID" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ ]] || {
  echo "task-id 只允许 [A-Za-z0-9][A-Za-z0-9_-]{0,63}" >&2; exit 2; }
[[ "$PARTNER" =~ ^[a-z][a-z0-9_-]{0,31}$ ]] || {
  echo "partner 只允许名册 key（小写字母开头）" >&2; exit 2; }
[ -n "$MARKER" ] || MARKER="联调标记 $TASK_ID"

cd "$PROJECT_DIR/acps_cluster"
INBOX="ws-supervisor/inbox"
mkdir -p "$INBOX"
TRACE="$(ls -t ws-supervisor/logs/run-trace/run-trace-*.jsonl 2>/dev/null | head -1 || true)"
[ -n "$TRACE" ] || { echo "找不到 run-trace（supervisor 日志目录为空？）" >&2; exit 2; }
BEFORE="$(wc -l < "$TRACE")"

case "$CREATE" in
  1) CREATE_STEP="第 1 步：acps_delegate(partner_aic=\"workspace\")，request 文本：
调用 ws.create：task_id=__TASK_ID__。为任务作用域 __TASK_ID__ 创建工作区并返回分区清单。
（__TASK_ID__ 是工作区作用域，不是 AIP 任务号；这段文本里只出现 ws.create。）" ;;
  *) CREATE_STEP="第 1 步：跳过建工作区（--create 0，假定 __TASK_ID__ 已存在）。" ;;
esac

cat > "$INBOX/$TASK_ID.txt" <<TASK
工作区联调任务，请按步骤执行，每步都等到终态：

$CREATE_STEP

第 2 步：acps_delegate(partner_aic="workspace")，request 文本：
调用 ws.write：task_id=__TASK_ID__，path=shared/__TASK_ID__.md，content=__MARKER__，encoding=text。
（__TASK_ID__ 是工作区作用域；这段文本里只出现 ws.write。）

第 3 步：acps_delegate(partner_aic="__PARTNER__")，request 文本：
任务 task_id=__TASK_ID__。请用 ws_read 读 shared/__TASK_ID__.md，再用 ws_write 把原文写到 products/__PARTNER__/__TASK_ID__.md，并在产物里原样报告你读到的内容。工作区工具 ws_list/ws_read/ws_write 已随任务附令牌；若报错请如实报告错误原文。

第 4 步：用 aip_submit 交付：各步摘要 + 伙伴读到的原文。

约束：不要用 acps_delegate_authenticated；不要 submit_job；不要编造内容。
TASK

sed -i "s|__TASK_ID__|$TASK_ID|g; s|__PARTNER__|$PARTNER|g; s|__MARKER__|$MARKER|g" "$INBOX/$TASK_ID.txt"
if id "$WIT_USER" >/dev/null 2>&1; then chown "$WIT_USER:$WIT_USER" "$INBOX/$TASK_ID.txt"; fi

echo "已投递 $INBOX/$TASK_ID.txt（等待 run_end，最多 ${TIMEOUT}s）"
elapsed=0
while [ "$elapsed" -lt "$TIMEOUT" ]; do
  if tail -n "+$((BEFORE + 1))" "$TRACE" 2>/dev/null | grep -q '"run_end"'; then
    echo "RUN_END 出现（约 ${elapsed}s）"
    break
  fi
  sleep 5
  elapsed=$((elapsed + 5))
done
[ "$elapsed" -lt "$TIMEOUT" ] || echo "⚠ 超时未见 run_end（继续打印已有证据）"

echo "== trace 摘要 =="
"$PY" "$SCRIPT_DIR/parse_trace.py" "$TRACE" "$BEFORE" || true

echo "== 工作区 manifest =="
MANIFEST="ws-workspace/workspaces/$TASK_ID/manifest.jsonl"
if [ -f "$MANIFEST" ]; then
  cat "$MANIFEST"
  echo "== 验收 =="
  if grep -q "\"actor\": \"$PARTNER\"" "$MANIFEST"; then
    echo "  PASS  manifest 出现 actor=$PARTNER"
  else
    echo "  FAIL  manifest 无 actor=$PARTNER（伙伴是否真的调用了 ws_write？）"
  fi
else
  echo "  (无 manifest：任务可能未执行到 workspace)"
fi
