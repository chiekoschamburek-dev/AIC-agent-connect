#!/bin/sh
# 重启 amp sender / mirror，让"每轮重扫文件列表"的修复生效（从而纳入 KB 心跳）
echo "=== 1) 重启前状态 ==="
for s in wit-amp-mirror wit-amp-sender; do
  printf '  %-16s %s  启动于 %s\n' "$s" "$(systemctl is-active $s.service 2>/dev/null)" \
    "$(systemctl show -p ActiveEnterTimestamp --value $s.service 2>/dev/null | awk '{print $2,$3}')"
done

echo
echo "=== 2) 重启 ==="
for s in wit-amp-mirror wit-amp-sender; do
  systemctl reset-failed $s.service 2>/dev/null
  systemctl restart $s.service
  sleep 5
  printf '  %-16s → %s\n' "$s" "$(systemctl is-active $s.service 2>/dev/null)"
done

echo
echo "=== 3) 等 60 秒，看 sender 是否投递 KB ==="
sleep 60
journalctl -u wit-amp-sender.service --since "-2min" --no-pager 2>/dev/null | grep -iE 'kb|本轮投递' | tail -12 | sed 's/^/  /'

echo
echo "=== 4) 位点文件是否已包含 kb ==="
/opt/wit/venv/bin/python - <<'PY'
import json
from pathlib import Path
p = Path('/var/log/acps-amp/.sender-offsets.json')
d = json.loads(p.read_text(encoding='utf-8')) if p.is_file() else {}
kb = {k: v for k, v in d.items() if 'kb' in k.lower()}
print(f"  条目数={len(d)}  KB 条目={kb or '（仍无）'}")
PY
