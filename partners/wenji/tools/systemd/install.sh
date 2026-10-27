#!/usr/bin/env bash
# 安装/更新问迹 systemd 用户服务（长期值守：AIP serve、AMP 转发、VPS 隧道、引擎栈、流量运行器）
#
# 用法: ./install.sh          # 安装并启动（自动停掉旧的 nohup 进程）
#       ./install.sh status   # 查看全部状态
#
# 一次性前提（需要 sudo，仅一次）：
#   sudo loginctl enable-linger hkr     # 注销/重启后用户服务仍自动运行

set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
UNITS=(wenji-db wenji-backend wenji-frontend wenji-llm-proxy wenji-serve wenji-amp-forwarder wenji-traffic)

install_units() {
  mkdir -p "$UNIT_DIR"
  for name in "${UNITS[@]}"; do
    cp "$REPO/tools/systemd/units/$name.service" "$UNIT_DIR/"
  done
  systemctl --user daemon-reload
  echo "· 已安装 ${#UNITS[@]} 个用户服务"

  echo "· 停止旧的 nohup 进程（若有）"
  for pat in "uvicorn app.main:app" "app.acps.cli serve" "amp_forwarder/forwarder.py" "scripts/acps/traffic.py"; do
    pkill -f "$pat" 2>/dev/null || true
  done
  pkill -f "vite" 2>/dev/null || true
  sleep 2

  echo "· 启动服务"
  for name in "${UNITS[@]}"; do
    systemctl --user enable --now "$name" >/dev/null 2>&1 || true
  done
  sleep 8
  status
}

status() {
  systemctl --user --no-pager --lines=0 status "${UNITS[@]}" 2>/dev/null | grep -E "●|Active:" || true
  echo
  echo "端口："
  ss -ltn | grep -E ":(8000|5173|8443)" || echo "  (无)"
  echo "linger: $(loginctl show-user "$USER" -p Linger 2>/dev/null || echo unknown)"
}

case "${1:-install}" in
  install) install_units ;;
  status)  status ;;
  *) echo "用法: $0 {install|status}"; exit 1 ;;
esac
