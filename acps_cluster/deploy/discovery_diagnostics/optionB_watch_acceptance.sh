#!/bin/sh
# B 上线后的验收观察：平台 alive 总盘 + 我们的 AIC 是否被覆盖（每 30s 观察一次）
CL=/opt/wit/my-project/acps_cluster
echo "=== 发送器状态 ==="
systemctl is-active wit-amp-sender.service wit-amp-mirror.service

echo
echo "=== 本机：已投递位点（>初始化值说明确实发出去了）==="
/opt/wit/venv/bin/python - <<'PY'
import json
from pathlib import Path
p = Path('/var/log/acps-amp/.sender-offsets.json')
d = json.loads(p.read_text()) if p.is_file() else {}
init = {'contradiction': 8896, 'critic': 8676, 'experiment': 8804, 'frontier': 8739,
        'scout': 8857, 'supervisor': 8765, 'synthesis': 9054, 'writer': 8676}
total = 0
for k, v in sorted(d.items()):
    agent = k.replace('amp_heartbeat_', '').replace('.jsonl', '')
    delta = v - init.get(agent, 0)
    total += max(0, delta)
    print(f"  {agent:16s} 位点 {v:8d}（比初始化 +{delta} 字节）")
print(f"  合计新增约 {total} 字节 ≈ {total // 280} 条心跳")
PY

echo
echo "=== 平台 alive 总盘 ==="
curl -s --max-time 12 https://wt.ioa.pub/discovery/admin/alive-sync/status

echo
echo
echo "=== 等待并复查（每 30s，共 4 次）==="
i=0
while [ $i -lt 4 ]; do
  i=$((i+1))
  sleep 30
  echo "--- +$((i*30))s"
  cd "$CL"
  sudo -u wit /opt/wit/venv/bin/python deploy/discovery_diagnostics/diag_aic_lookup_and_alive.py 2>&1 \
    | sed -n '/B\. 我们的 AIC/,$p' | head -14
done
