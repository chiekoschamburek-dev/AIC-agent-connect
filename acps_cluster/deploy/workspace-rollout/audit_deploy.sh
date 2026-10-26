#!/bin/bash
# 发布后审计：文件哈希一致性、日志 JWS 泄漏、bundle 权限、镜像残留、服务/心跳。
# 用法（在服务器上）：bash audit_deploy.sh [--expected /tmp/local_sha256.txt]
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/wit/my-project}"
PY="${PY:-/opt/wit/venv/bin/python}"
EXPECTED=""

while [ $# -gt 0 ]; do
  case "$1" in
    --expected) EXPECTED="${2:-}"; shift 2 ;;
    --expected=*) EXPECTED="${1#*=}"; shift ;;
    -h|--help) sed -n '2,3p' "$0"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

cd "$PROJECT_DIR"
fail=0

if [ -n "$EXPECTED" ]; then
  echo "== 1) 文件哈希对比（期望清单 $EXPECTED）=="
  [ -f "$EXPECTED" ] || { echo "  清单不存在: $EXPECTED" >&2; exit 2; }
  while IFS= read -r line; do
    case "$line" in ''|'#'*) continue ;; esac
    # 兼容 `sha256sum` 的 "hash  path" 与单个空格两种形态
    want="${line%%[[:space:]]*}"
    rest="${line#"$want"}"
    path="${rest#"${rest%%[![:space:]]*}"}"
    [ -n "$path" ] || continue
    if [ ! -f "$path" ]; then
      echo "  MISSING $path"; fail=1; continue
    fi
    got="$(sha256sum "$path" | cut -d' ' -f1)"
    if [ "$got" != "$want" ]; then
      echo "  DIFF    $path"
      echo "    want=$want"
      echo "    got =$got"
      fail=1
    fi
  done < "$EXPECTED"
  [ "$fail" -eq 0 ] && echo "  OK（全部一致）"
else
  echo "== 1) 文件哈希：未给 --expected，跳过 =="
fi

# 后续检查的路径都相对于 acps_cluster（ws-*/、ws-workspace/）
cd "$PROJECT_DIR/acps_cluster"

echo "== 2) run-trace 里的 JWS 原文（应为 0）=="
if ! "$PY" - <<'PY'
import re
from pathlib import Path
pat = re.compile(rb"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.")
total = 0
for p in sorted(Path('ws-supervisor/logs/run-trace').glob('*.jsonl')):
    hits = len(pat.findall(p.read_bytes()))
    total += hits
    if hits:
        print(f"  LEAK {p}: {hits}")
print(f"  total={total}")
raise SystemExit(1 if total else 0)
PY
then
  fail=1
fi

echo "== 3) bundle 令牌文件权限（应为 600）=="
bad=0
while IFS= read -r f; do
  [ -n "$f" ] || continue
  mode="$(stat -c '%a' "$f")"
  [ "$mode" = "600" ] || { echo "  BAD   $mode $f"; bad=1; }
done < <(find ws-*/tokens -maxdepth 1 -name 'bundle-*.json' 2>/dev/null || true)
[ "$bad" -eq 0 ] && echo "  OK"

echo "== 4) 历史镜像残留（只应是部署前的旧任务）=="
find ws-workspace/workspaces -path '*/products/supervisor' -type d 2>/dev/null || true

echo "== 5) 服务与心跳 =="
for p in workspace supervisor scout experiment synthesis writer critic contradiction frontier kb citation; do
  if [ "$p" = workspace ]; then s=wit-workspace.service; else s=wit-yanshu@$p.service; fi
  h="$(ls -t ws-$p/logs/amp_heartbeat_*.jsonl 2>/dev/null | head -1 || true)"
  if [ -n "$h" ]; then age=$(( $(date +%s) - $(stat -c %Y "$h") )); else age="?"; fi
  printf '  %-13s %-9s heartbeat=%ss\n' "$p" "$(systemctl is-active "$s" 2>/dev/null || true)" "$age"
done

exit "$fail"
