#!/usr/bin/env bash
# 问迹演示栈一键管理：DB + 后端 + 前端（+ 可选的 AIP 守护进程状态查看）
# 用法: ./devstack.sh {start|status|stop|restart}

set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB_CONTAINER="${DB_CONTAINER:-mqa-db}"

start() {
  echo "· DB"
  docker start "$DB_CONTAINER" >/dev/null 2>&1 || true
  for i in $(seq 1 15); do
    docker exec "$DB_CONTAINER" pg_isready -U chinook -d chinook >/dev/null 2>&1 && { echo "  ok"; break; }
    sleep 2
  done
  echo "· backend :8000"
  pgrep -f "uvicorn app.main:app" >/dev/null || \
    (cd "$REPO/backend" && nohup .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 \
      > var/uvicorn8000.log 2>&1 &)
  echo "· frontend :5173"
  pgrep -f "vite" >/dev/null || \
    (cd "$REPO/frontend" && nohup pnpm dev > "$REPO/var/vite.log" 2>&1 &)
  sleep 6; status
}

status() {
  echo "== 端口 =="
  for p in 5433 8000 5173 8443; do
    ss -ltn | grep -q ":$p " && echo "  :$p LISTEN" || echo "  :$p ——"
  done
  echo "== HTTP =="
  curl -s -o /dev/null -m 3 -w "  backend  %{http_code}\n" http://localhost:8000/api/health || echo "  backend  down"
  curl -s -o /dev/null -m 3 -w "  frontend %{http_code}\n" http://localhost:5173/ || echo "  frontend down"
  echo "== 守护 =="
  pgrep -af "register --watch --serve" | head -1 || echo "  AIP watcher 未运行"
  pgrep -af "forwarder.py" | head -1 || echo "  AMP forwarder 未运行"
  pgrep -af "ssh.*-R 127.0.0.1:19000" | grep -v sshpass | head -1 || echo "  VPS 隧道未运行（tools/vps_bridge/bridge.sh up）"
  echo "== 注册 =="
  tail -3 "$REPO/var/register_watch.log" 2>/dev/null || echo "  无 watcher 日志"
}

stop() {
  pgrep -f "uvicorn app.main:app" | xargs -r kill
  pgrep -f "vite" | xargs -r kill
  echo "已停 backend/frontend（DB 与 AIP 守护未动）"
}

case "${1:-}" in
  start)   start ;;
  status)  status ;;
  stop)    stop ;;
  restart) stop; start ;;
  *) echo "用法: $0 {start|status|stop|restart}"; exit 1 ;;
esac
