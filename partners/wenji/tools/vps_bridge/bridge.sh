#!/usr/bin/env bash
# 问迹 VPS 桥接：把本地 wit 监听 (8443) 经 SSH 反向隧道 + socat 暴露为 VPS 公网端口。
#
# 架构（全程原始 TCP，mTLS 端到端不受影响）：
#   队友/平台 ──TLS+mTLS──► VPS:<PUBLIC_PORT> (socat, 0.0.0.0)
#                              └─► VPS:127.0.0.1:<TUNNEL_PORT> (sshd 反向转发)
#                                      └─► 本机:127.0.0.1:<LOCAL_PORT> (wit serve)
#
# 认证：SSH 密钥（默认 ~/.ssh/wenji_vps）。首次部署密钥：
#   ssh-keygen -t ed25519 -N "" -f ~/.ssh/wenji_vps -C wenji-agent
#   ssh-copy-id -i ~/.ssh/wenji_vps hkr@<VPS_HOST>
#   （authorized_keys 需 600 权限；Alibaba Cloud Linux 另需 restorecon -R ~/.ssh）
#
# 用法：
#   ./bridge.sh up      # VPS 起 socat + 本机起持久隧道（断线自动重连）
#   ./bridge.sh status  # 两侧状态
#   ./bridge.sh down    # 全部拆除
#
# 环境变量：VPS_HOST / VPS_USER / VPS_KEY / PUBLIC_PORT / TUNNEL_PORT / LOCAL_PORT

set -euo pipefail

VPS_HOST="${VPS_HOST:-8.160.176.215}"
VPS_USER="${VPS_USER:-hkr}"
VPS_KEY="${VPS_KEY:-$HOME/.ssh/wenji_vps}"
PUBLIC_PORT="${PUBLIC_PORT:-8601}"
TUNNEL_PORT="${TUNNEL_PORT:-19000}"
LOCAL_PORT="${LOCAL_PORT:-8443}"
KEEPER_PIDFILE=/tmp/wenji-tunnel-keeper.pid
SSH_OPTS="-i $VPS_KEY -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
  -o ServerAliveInterval=20 -o ServerAliveCountMax=6 -o ExitOnForwardFailure=yes"

vps() { ssh $SSH_OPTS "$VPS_USER@$VPS_HOST" "$@"; }

up_relay() {
  echo "· VPS: socat 中继 0.0.0.0:$PUBLIC_PORT -> 127.0.0.1:$TUNNEL_PORT"
  vps "bash -s" <<EOF
if ss -ltn | grep -q ":$PUBLIC_PORT "; then
  echo "  (VPS 端口 $PUBLIC_PORT 已有监听——若非本桥接请先排查)"
else
  nohup socat TCP-LISTEN:$PUBLIC_PORT,fork,reuseaddr TCP:127.0.0.1:$TUNNEL_PORT \
    >/tmp/wenji-socat.log 2>&1 &
  sleep 1
  ss -ltn | grep ":$PUBLIC_PORT " >/dev/null && echo "  socat OK" || { echo "  socat 启动失败"; cat /tmp/wenji-socat.log; exit 1; }
fi
EOF
}

up_tunnel() {
  echo "· 本机: 反向隧道守护循环 VPS:127.0.0.1:$TUNNEL_PORT -> 本机:$LOCAL_PORT"
  if [ -f "$KEEPER_PIDFILE" ] && kill -0 "$(cat "$KEEPER_PIDFILE")" 2>/dev/null; then
    echo "  (守护循环已在运行, pid $(cat "$KEEPER_PIDFILE"))"; return
  fi
  local keeper
  keeper="$(dirname "$(readlink -f "$0")")/tunnel_keeper.sh"
  nohup "$keeper" >/tmp/wenji-tunnel-keeper.log 2>&1 &
  sleep 4
  if [ -f "$KEEPER_PIDFILE" ] && kill -0 "$(cat "$KEEPER_PIDFILE")" 2>/dev/null; then
    echo "  守护循环 OK (pid $(cat "$KEEPER_PIDFILE"), 日志 /tmp/wenji-tunnel-keeper.log)"
  else
    echo "  守护循环启动失败"; cat /tmp/wenji-tunnel-keeper.log; exit 1
  fi
}

cmd_status() {
  echo "== VPS 侧 =="
  vps "ss -ltn | grep -E ':($PUBLIC_PORT|$TUNNEL_PORT) ' || echo '无相关监听'"
  echo "== 本机侧 =="
  ss -ltn | grep -E ":$LOCAL_PORT " || echo "本机 $LOCAL_PORT 未监听（wit serve 未启动时正常）"
  if [ -f "$KEEPER_PIDFILE" ] && kill -0 "$(cat "$KEEPER_PIDFILE")" 2>/dev/null; then
    echo "隧道守护: 运行中 (pid $(cat "$KEEPER_PIDFILE"))"
  else
    echo "隧道守护: 未运行"
  fi
}

cmd_down() {
  echo "· 停止本机隧道守护"
  if [ -f "$KEEPER_PIDFILE" ]; then
    kill "$(cat "$KEEPER_PIDFILE")" 2>/dev/null && echo "  已停" || true
  fi
  echo "· 停止 VPS socat"
  vps "pkill -f 'socat TCP-LISTEN:$PUBLIC_PORT' 2>/dev/null; echo done" || true
}

case "${1:-}" in
  up)     up_relay; up_tunnel; echo; cmd_status ;;
  status) cmd_status ;;
  down)   cmd_down ;;
  *)      echo "用法: $0 {up|status|down}"; exit 1 ;;
esac
