#!/bin/bash
# 只读盘点服务器：HEAD / Python / 待改文件 sha256 / 服务 / 测试文件。
# 用法（在服务器上）：bash inventory.sh [deploy_files.txt]
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
PY="${PY:-$PROJECT_DIR/.venv/bin/python}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FILES="${1:-$SCRIPT_DIR/deploy_files.txt}"

case "${1:-}" in
  -h|--help) sed -n '2,3p' "$0"; exit 0 ;;
esac

cd "$PROJECT_DIR"
echo "HOST=$(hostname)  PROJECT=$PROJECT_DIR"
echo "HEAD=$(git rev-parse --short HEAD 2>/dev/null || echo NO-GIT)  PY=$("$PY" -V 2>&1)"
echo "WORKTREE=$(git status --porcelain 2>/dev/null | wc -l) 个未提交变更"

echo "== 待改文件 =="
if [ -f "$FILES" ]; then
  while IFS= read -r f; do
    case "$f" in ''|'#'*) continue ;; esac
    if [ -f "$f" ]; then
      printf '%s  %s\n' "$(sha256sum "$f" | cut -c1-16)" "$f"
    else
      printf 'MISSING           %s\n' "$f"
    fi
  done < "$FILES"
else
  echo "  (找不到清单 $FILES)"
fi

echo "== 服务 =="
for p in workspace supervisor scout experiment synthesis writer critic contradiction frontier kb citation; do
  if [ "$p" = workspace ]; then s=wit-workspace.service; else s=wit-yanshu@$p.service; fi
  printf '  %-13s %-9s %s\n' "$p" "$(systemctl is-active "$s" 2>/dev/null || true)" \
    "$(systemctl show "$s" -p ActiveEnterTimestamp --value 2>/dev/null || true)"
done

echo "== 备份目录（最近 5 个）=="
ls -dt /tmp/predeploy-* 2>/dev/null | head -5 || echo "  (none)"
