#!/usr/bin/env bash
# 反向隧道守护循环（由 bridge.sh up 启动）。
# ssh 断开后等待 5s 重连；PID 写入 /tmp/wenji-tunnel-keeper.pid 供 bridge.sh 管理。

VPS_HOST="${VPS_HOST:-8.160.176.215}"
VPS_USER="${VPS_USER:-hkr}"
VPS_KEY="${VPS_KEY:-$HOME/.ssh/wenji_vps}"
TUNNEL_PORT="${TUNNEL_PORT:-19000}"
LOCAL_PORT="${LOCAL_PORT:-8443}"
PIDFILE=/tmp/wenji-tunnel-keeper.pid

echo $$ > "$PIDFILE"
trap 'rm -f "$PIDFILE"; exit 0' INT TERM

while true; do
  ssh -i "$VPS_KEY" -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
    -o ServerAliveInterval=20 -o ServerAliveCountMax=6 -o ExitOnForwardFailure=yes -N \
    -R "127.0.0.1:$TUNNEL_PORT:127.0.0.1:$LOCAL_PORT" \
    "$VPS_USER@$VPS_HOST"
  echo "$(date '+%F %T') tunnel exited (code=$?), retry in 5s"
  sleep 5
done
