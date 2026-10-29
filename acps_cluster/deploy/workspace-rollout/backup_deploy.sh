#!/bin/bash
# 备份 + 覆盖 + 语法检查（失败即停，不启动服务）。
# 用法（在服务器上）：
#   bash backup_deploy.sh --source /tmp/deploy-in --files deploy_files.txt \
#        --name ws-partner --stop "wit-yanshu@supervisor.service"
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
PY="${PY:-$PROJECT_DIR/.venv/bin/python}"
WIT_USER="${WIT_USER:-wit}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SOURCE=""
FILES="$SCRIPT_DIR/deploy_files.txt"
NAME="workspace-rollout"
STOP_UNITS=()
DO_COMPILE=1

while [ $# -gt 0 ]; do
  case "$1" in
    --source) SOURCE="${2:-}"; shift 2 ;;
    --source=*) SOURCE="${1#*=}"; shift ;;
    --files) FILES="${2:-}"; shift 2 ;;
    --files=*) FILES="${1#*=}"; shift ;;
    --name) NAME="${2:-}"; shift 2 ;;
    --name=*) NAME="${1#*=}"; shift ;;
    --stop) read -r -a STOP_UNITS <<< "${2:-}"; shift 2 ;;
    --stop=*) read -r -a STOP_UNITS <<< "${1#*=}"; shift ;;
    --no-compile) DO_COMPILE=0; shift ;;
    -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$SOURCE" ] || [ ! -d "$SOURCE" ]; then
  echo "用法: $0 --source <解包目录> [--files 清单] [--stop \"unit ...\"]" >&2
  exit 2
fi
[ -f "$FILES" ] || { echo "清单不存在: $FILES" >&2; exit 2; }

mapfile -t REQ < <(grep -v '^[[:space:]]*#' "$FILES" | sed '/^[[:space:]]*$/d')
[ "${#REQ[@]}" -gt 0 ] || { echo "清单为空: $FILES" >&2; exit 2; }

cd "$PROJECT_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
BK="/tmp/predeploy-$NAME-$STAMP"
mkdir -p "$BK"

echo "== 1) 停服务 =="
for u in "${STOP_UNITS[@]:-}"; do
  [ -n "$u" ] || continue
  systemctl stop "$u" || true
  echo "  stopped $u -> $(systemctl is-active "$u" 2>/dev/null || true)"
done

echo "== 2) 备份现有文件 -> $BK =="
EXISTING=()
for f in "${REQ[@]}"; do
  if [ -f "$f" ]; then
    EXISTING+=("$f")
  else
    echo "absent-before: $f" >> "$BK/absent.txt"
  fi
done
if [ "${#EXISTING[@]}" -gt 0 ]; then
  tar czf "$BK/files.tar.gz" "${EXISTING[@]}"
fi
echo "BACKUP_DIR=$BK"

echo "== 3) 覆盖 =="
for f in "${REQ[@]}"; do
  src="$SOURCE/$f"
  if [ ! -f "$src" ]; then
    echo "  源文件缺失: $src（先解包 tar 到 --source）" >&2
    exit 1
  fi
  mkdir -p "$(dirname "$f")"
  cp -a "$src" "$f"
done

if id "$WIT_USER" >/dev/null 2>&1; then
  for f in "${REQ[@]}"; do chown "$WIT_USER:$WIT_USER" "$f" 2>/dev/null || true; done
fi

echo "== 4) 语法检查（ast.parse，不写 pyc）=="
if [ "$DO_COMPILE" -eq 1 ]; then
  "$PY" - "${REQ[@]}" <<'PY'
import ast
import sys

bad = 0
for path in sys.argv[1:]:
    if not path.endswith(".py"):
        continue
    try:
        ast.parse(open(path, encoding="utf-8").read(), path)
    except SyntaxError as exc:
        bad = 1
        print(f"  SYNTAX {path}: {exc}")
print("COMPILE_OK" if not bad else "COMPILE_FAILED")
raise SystemExit(1 if bad else 0)
PY
fi

echo "== 5) 新文件哈希 =="
for f in "${REQ[@]}"; do
  [ -f "$f" ] && printf '%s  %s\n' "$(sha256sum "$f" | cut -c1-16)" "$f"
done

echo
echo "完成。下一步：bash restart_units.sh --units \"...\""
echo "回滚：tar xzf $BK/files.tar.gz -C $PROJECT_DIR（再重启对应服务）"
