#!/usr/bin/env bash
# 等待「问迹-联调客户端」审核通过后，自动把流量运行器切换为独立 AIC 作 Leader。
# 用法（服务器上，后台运行）：
#   nohup bash tools/acps/activate_probe_leader.sh > var/activate_probe.log 2>&1 &
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY="$REPO/backend/.venv/bin/python"
WS="$REPO/backend/.wit/wenji-probe"
export WENJI_REPO="$REPO"

log() { echo "$(date '+%F %T') $*"; }

log "等待探测身份审核（每 2 分钟检查一次）…"
for i in $(seq 1 240); do
  status=$("$PY" - <<'EOF' 2>/dev/null || echo "unknown"
import json, os, sys
from pathlib import Path
sys.path.insert(0, os.path.join(os.environ["WENJI_REPO"], "backend"))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.environ["WENJI_REPO"], ".env"), override=False)
load_dotenv(os.path.join(os.environ["WENJI_REPO"], ".env.acps"), override=False)
import asyncio
from wit import RegistryPasswordCredentials, WitRuntime
sys.path.insert(0, os.path.join(os.environ["WENJI_REPO"], "backend/scripts/acps"))
from register_probe import build_probe_agent, build_probe_config, PROBE_WORKSPACE
rt = WitRuntime(build_probe_agent(), workspace=PROBE_WORKSPACE, acps=build_probe_config())
r = asyncio.run(rt.up(credentials=RegistryPasswordCredentials.from_env()))
print(getattr(r, "status", "unknown"))
EOF
)
  log "status=$status"
  if [ "$status" = "active" ] || [ "$status" = "serving" ]; then
    log "探测身份已激活，切换流量 Leader 身份"
    # 单元里注入 TRAFFIC_LEADER_WORKSPACE（幂等）
    UNIT="$HOME/.config/systemd/user/wenji-traffic.service"
    if ! grep -q "TRAFFIC_LEADER_WORKSPACE" "$UNIT"; then
      sed -i '/^\[Service\]/a Environment=TRAFFIC_LEADER_WORKSPACE=.wit/wenji-probe' "$UNIT"
    fi
    systemctl --user daemon-reload
    systemctl --user restart wenji-traffic
    sleep 15
    log "已重启 wenji-traffic：$(systemctl --user is-active wenji-traffic)"
    "$PY" - <<'EOF'
import json, os
p = os.path.join(os.environ["WENJI_REPO"], "backend/.wit/wenji-probe/acps/acs.final.json")
print("probe AIC:", json.load(open(p)).get("aic"))
EOF
    exit 0
  fi
  sleep 120
done
log "超时未激活，放弃（保留自调用模式）"
exit 1
