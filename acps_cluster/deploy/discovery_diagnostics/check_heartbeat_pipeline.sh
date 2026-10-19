#!/bin/sh
# 查心跳投递链路：sender 偏移 vs 镜像文件实际大小
echo "=== 1) sender 偏移文件 ==="
ls -la /var/log/acps-amp/.sender-offsets.json 2>/dev/null
/opt/wit/venv/bin/python - <<'PY'
import json
from pathlib import Path
p = Path('/var/log/acps-amp/.sender-offsets.json')
if p.is_file():
    d = json.loads(p.read_text(encoding='utf-8'))
    print(f"  记录 {len(d)} 个文件")
    for k, v in sorted(d.items()):
        print(f"    {k:44s} → offset {v}")
else:
    print("  【偏移文件不存在】")
PY

echo
echo "=== 2) 镜像目录各文件实际大小 ==="
ls -la /var/log/acps-amp/yanshu/*.jsonl | awk '{printf "    %-46s %s B\n", $9, $5}'

echo
echo "=== 3) 逐文件比对：偏移 vs 大小（偏移 > 大小 = 卡死）==="
/opt/wit/venv/bin/python - <<'PY'
import json
from pathlib import Path
MIR = Path('/var/log/acps-amp/yanshu')
off_p = Path('/var/log/acps-amp/.sender-offsets.json')
offs = json.loads(off_p.read_text(encoding='utf-8')) if off_p.is_file() else {}
for f in sorted(MIR.glob('compound_*.jsonl')) or sorted(MIR.glob('*.jsonl')):
    if f.is_symlink():
        continue
    size = f.stat().st_size
    o = offs.get(str(f)) or offs.get(f.name)
    if o is None:
        # 试试其它键形式
        for k, v in offs.items():
            if k.endswith(f.name):
                o = v
                break
    flag = ''
    if o is not None:
        if o > size:
            flag = '  ✘ 偏移>大小（会卡住/丢数据）'
        elif o == size:
            flag = '  （已追平）'
    print(f"    {f.name:46s} size={size:8d}  offset={o}{flag}")
PY

echo
echo "=== 4) sender / mirror 服务状态与最近日志 ==="
for s in wit-amp-mirror wit-amp-sender; do
  printf '  %-16s %s  enabled=%s\n' "$s" "$(systemctl is-active $s.service 2>/dev/null)" "$(systemctl is-enabled $s.service 2>/dev/null)"
done
echo "  --- sender 最近 12 行"
journalctl -u wit-amp-sender.service -n 12 --no-pager | tail -12 | sed 's/^/      /'
echo "  --- mirror 最近 6 行"
journalctl -u wit-amp-mirror.service -n 6 --no-pager | tail -6 | sed 's/^/      /'
