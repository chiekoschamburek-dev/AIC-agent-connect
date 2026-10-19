#!/usr/bin/env bash
# 笔记本侧常驻脚本：起白名单代理 + 用 SSH 反向隧道把它挂到服务器，断线自动重连。
# 用法：bash start_campus_egress.sh          （前台跑，Ctrl-C 停）
#       变量可覆盖：CAMPUS_EGRESS_SERVER / CAMPUS_EGRESS_PORT / CAMPUS_EGRESS_ALLOWED
set -uo pipefail

SERVER="${CAMPUS_EGRESS_SERVER:-root@192.0.2.10}"
PORT="${CAMPUS_EGRESS_PORT:-18888}"
ALLOWED="${CAMPUS_EGRESS_ALLOWED:-myai.bupt.edu.cn:443}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$DIR/_campus_egress.log"

export CAMPUS_PROXY_HOST=127.0.0.1
export CAMPUS_PROXY_PORT="$PORT"
export CAMPUS_PROXY_ALLOWED="$ALLOWED"

port_open() {
  python -c "import socket,sys; socket.create_connection(('127.0.0.1',int(sys.argv[1])),2).close()" "$1" 2>/dev/null
}

port_open() {
  python -c "import socket,sys; socket.create_connection(('127.0.0.1',int(sys.argv[1])),2).close()" "$1" 2>/dev/null
}

# 代理被杀也要能拉回来，否则隧道虽在，转发的目标端口已死
ensure_proxy() {
  if port_open "$PORT"; then
    return 0
  fi
  nohup python "$DIR/campus_llm_proxy.py" >> "$DIR/_campus_proxy.log" 2>&1 &
  sleep 2
  port_open "$PORT"
}

echo "[tunnel] $SERVER 反向端口 127.0.0.1:$PORT →（本机校园网）"
while true; do
  if ! ensure_proxy; then
    echo "$(date '+%F %T') 代理起不来，15s 后重试" | tee -a "$LOG"
    sleep 15
    continue
  fi
  ssh -N \
    -o ExitOnForwardFailure=yes -o BatchMode=yes -o ConnectTimeout=15 \
    -o ServerAliveInterval=20 -o ServerAliveCountMax=3 \
    -R "127.0.0.1:$PORT:127.0.0.1:$PORT" "$SERVER"
  echo "$(date '+%F %T') 隧道退出 code=$?，15s 后重连" | tee -a "$LOG"
  sleep 15
done
