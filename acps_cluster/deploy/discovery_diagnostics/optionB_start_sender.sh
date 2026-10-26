#!/bin/sh
# B 方案上线：安装并启动心跳发送器，然后观察投递
set -e
CL=/opt/wit/my-project/acps_cluster

echo "=== 1) 安装 systemd 单元 ==="
install -m 644 "$CL/deploy/wit-amp-sender.service" /etc/systemd/system/wit-amp-sender.service
systemctl daemon-reload
systemctl enable --now wit-amp-sender.service

echo
echo "=== 2) 等 70 秒，让 agent 产生并投递几条心跳 ==="
sleep 70
systemctl is-active wit-amp-sender.service
echo
journalctl -u wit-amp-sender.service -n 18 --no-pager | tail -18

echo
echo "=== 3) 位点推进情况（应大于初始化值）==="
/opt/wit/venv/bin/python - <<'PY'
import json
from pathlib import Path
p = Path('/var/log/acps-amp/.sender-offsets.json')
data = json.loads(p.read_text()) if p.is_file() else {}
for k, v in sorted(data.items()):
    size = (Path('/var/log/acps-amp/yanshu') / k).stat().st_size
    print(f"  {k:38s} 位点 {v:8d} / 文件 {size:8d}  {'✔ 已推进' if v > 0 else '（未推进）'}")
PY
