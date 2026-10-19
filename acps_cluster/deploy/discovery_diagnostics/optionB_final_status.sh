#!/bin/sh
# B 上线后的最终状态复核
echo "--- 服务 ---"
systemctl is-active wit-amp-mirror.service wit-amp-sender.service

echo
echo "--- 发送器最近日志 ---"
journalctl -u wit-amp-sender.service -n 8 --no-pager | tail -8

echo
echo "--- 位点推进 ---"
/opt/wit/venv/bin/python - <<'PY'
import json
from pathlib import Path
d = json.loads(Path('/var/log/acps-amp/.sender-offsets.json').read_text())
print(f"  跟踪文件 {len(d)} 个，位点合计 {sum(d.values())} 字节")
for k, v in sorted(d.items()):
    print(f"    {k:38s} {v}")
PY

echo
echo "--- 平台 alive 总盘 ---"
curl -s --max-time 12 https://wt.ioa.pub/discovery/admin/alive-sync/status | head -c 260
echo
