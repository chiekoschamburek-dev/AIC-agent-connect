#!/bin/bash
# 并行重启（--no-block）+ 等 active + ready 行 + ACS/指纹错误扫描。
# 用法（在服务器上）：
#   bash restart_units.sh --units "wit-workspace.service wit-yanshu@scout.service ..."
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/wit/my-project}"
UNITS=()
TIMEOUT="${TIMEOUT:-600}"

while [ $# -gt 0 ]; do
  case "$1" in
    --units) read -r -a UNITS <<< "${2:-}"; shift 2 ;;
    --units=*) read -r -a UNITS <<< "${1#*=}"; shift ;;
    --timeout) TIMEOUT="${2:-600}"; shift 2 ;;
    --timeout=*) TIMEOUT="${1#*=}"; shift ;;
    -h|--help) sed -n '2,4p' "$0"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

[ "${#UNITS[@]}" -gt 0 ] || { echo "用法: $0 --units \"unit1 unit2\"" >&2; exit 2; }

echo "== 并行重启 ${#UNITS[@]} 个 unit =="
for u in "${UNITS[@]}"; do
  systemctl restart --no-block "$u"
  echo "  restart --no-block $u"
done

echo "== 等 active（上限 ${TIMEOUT}s；ExecStartPre 有 0-300s 随机抖动）=="
elapsed=0
while [ "$elapsed" -lt "$TIMEOUT" ]; do
  all=1
  for u in "${UNITS[@]}"; do
    st="$(systemctl is-active "$u" 2>/dev/null || true)"
    [ "$st" = "active" ] || all=0
    [ "$st" = "failed" ] && echo "  FAILED $u"
  done
  [ "$all" -eq 1 ] && { echo "ALL ACTIVE after ${elapsed}s"; break; }
  sleep 5
  elapsed=$((elapsed + 5))
done

echo "== 状态 =="
for u in "${UNITS[@]}"; do
  printf '  %-32s %-9s %s\n' "$u" "$(systemctl is-active "$u" 2>/dev/null || true)" \
    "$(systemctl show "$u" -p ActiveEnterTimestamp --value 2>/dev/null || true)"
done

echo "== ready 行 =="
for u in "${UNITS[@]}"; do
  line="$(journalctl -u "$u" --since "15 minutes ago" --no-pager 2>/dev/null \
    | grep -E '已上线 AIC|常驻已上线' | tail -1 || true)"
  printf '  %-32s %s\n' "$u" "${line:-(not ready yet)}"
done

echo "== ACS / 指纹 / Traceback 扫描 =="
if journalctl -u 'wit-yanshu@*' -u wit-workspace.service --since "15 minutes ago" --no-pager 2>/dev/null \
  | grep -Ei 'acs_fingerprint_changed_after_submission|拒绝启动|Traceback|CRITICAL'; then
  echo "  ⚠ 上面有命中，按 docs/incident-2026-10-09-code-sync-acs-guard.md 处理" >&2
  exit 1
else
  echo "  (none)"
fi
