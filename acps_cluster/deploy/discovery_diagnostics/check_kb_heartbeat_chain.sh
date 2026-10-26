#!/bin/sh
# 确认 KB 的心跳进入镜像并已被 sender 投递（否则平台永远判它离线）
echo "=== 1) 镜像侧是否出现 KB ==="
ls -la /var/log/acps-amp/yanshu/ 2>/dev/null | grep -i kb | sed 's/^/  /' || echo "  【镜像里还没有 KB】"

echo
echo "=== 2) sender 最近是否投递 KB ==="
journalctl -u wit-amp-sender.service --since "-8min" --no-pager 2>/dev/null | grep -i kb | tail -5 | sed 's/^/  /' || echo "  （近期日志无 KB）"
journalctl -u wit-amp-sender.service --since "-3min" --no-pager 2>/dev/null | tail -6 | sed 's/^/  /'

echo
echo "=== 3) mirror 最近是否镜像 KB ==="
journalctl -u wit-amp-mirror.service --since "-8min" --no-pager 2>/dev/null | grep -i kb | tail -5 | sed 's/^/  /' || echo "  （近期日志无 KB）"

echo
echo "=== 4) sender 位点文件是否已包含 KB ==="
/opt/wit/venv/bin/python - <<'PY'
import json
from pathlib import Path
p = Path('/var/log/acps-amp/.sender-offsets.json')
if not p.is_file():
    print("  【无位点文件】")
else:
    d = json.loads(p.read_text(encoding='utf-8'))
    kb = [k for k in d if 'kb' in k.lower()]
    print(f"  位点文件条目 {len(d)} 个；KB 相关: {kb or '（无）'}")
    for k in kb:
        f = Path('/var/log/acps-amp/yanshu') / k
        size = f.stat().st_size if f.is_file() else -1
        print(f"    {k}: 位点={d[k]} 文件={size} 未投={size - int(d[k])}")
PY
