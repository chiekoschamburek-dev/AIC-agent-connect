#!/bin/sh
# 确认 KB 心跳是否已被 sender 纳入投递（位点文件 + 稍等一轮再看）
echo "=== 1) 位点文件全部条目 ==="
/opt/wit/venv/bin/python - <<'PY'
import json
from pathlib import Path
p = Path('/var/log/acps-amp/.sender-offsets.json')
d = json.loads(p.read_text(encoding='utf-8')) if p.is_file() else {}
for k, v in sorted(d.items()):
    print(f"  {k:44s} {v}")
print(f"  共 {len(d)} 条")
print(f"  含 kb: {[k for k in d if 'kb' in k.lower()] or '（无）'}")
PY

echo
echo "=== 2) 镜像目录里 KB 文件的大小 ==="
ls -la /var/log/acps-amp/yanshu/amp_heartbeat_kb.jsonl 2>/dev/null | sed 's/^/  /' || echo "  （不存在）"

echo
echo "=== 3) 等 40 秒再看 sender 是否投递 KB ==="
sleep 40
journalctl -u wit-amp-sender.service --since "-1min" --no-pager 2>/dev/null | tail -10 | sed 's/^/  /'
echo
echo "  --- 位点文件里的 kb 条目"
/opt/wit/venv/bin/python -c "
import json
from pathlib import Path
p = Path('/var/log/acps-amp/.sender-offsets.json')
d = json.loads(p.read_text(encoding='utf-8')) if p.is_file() else {}
print('  ', {k: v for k, v in d.items() if 'kb' in k.lower()} or '（仍无）')
print('  条目数:', len(d))
"
