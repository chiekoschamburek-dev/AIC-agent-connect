#!/bin/sh
# 等 scout 起来，然后实测：不重新注册时，平台对 scout 的认知是什么？
# 这直接回答"我还需要重新注册吗"。
CL=/opt/wit/my-project/acps_cluster

echo "=== 1) 等 scout 上线 ==="
i=0
while [ $i -lt 14 ]; do
  i=$((i+1)); sleep 30
  st=$(systemctl is-active wit-yanshu@scout.service 2>/dev/null)
  printf '  +%3ds: %s\n' $((i*30)) "$st"
  [ "$st" = "active" ] && break
done

echo
echo "=== 2) scout 启动日志（看有无指纹/注册告警）==="
journalctl -u wit-yanshu@scout.service --since "-9min" --no-pager 2>/dev/null \
  | grep -viE 'systemd\[1\]' | tail -8 | sed 's/^/  /'

echo
echo "=== 3) 平台对 scout 的认知（关键）==="
cd "$CL" || exit 1
sudo -u wit /opt/wit/venv/bin/python deploy/discovery_diagnostics/verify_scout_still_online.py 2>&1 | sed -n '1,8p' | sed 's/^/  /'

echo
echo "=== 4) 本地 ACS 终稿里是否已含 memory.*（说明本地已投影，但平台还没有）==="
/opt/wit/venv/bin/python -c "
import json
from pathlib import Path
p = Path('$CL/ws-scout/acps/acs.final.json')
d = json.loads(p.read_text(encoding='utf-8'))
ids = [s.get('id') for s in (d.get('skills') or [])]
print('  本地 acs.final.json skills =', ids)
print('  含 memory.* :', [i for i in ids if i and i.startswith('memory.')] or '（否）')
"
