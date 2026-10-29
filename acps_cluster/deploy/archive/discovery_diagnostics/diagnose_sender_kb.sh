#!/bin/sh
# 诊断 sender 为何没投递 KB 心跳
echo "=== 1) sender 服务状态与最近日志 ==="
printf '  active: %s\n' "$(systemctl is-active wit-amp-sender.service 2>/dev/null)"
printf '  NRestarts: %s\n' "$(systemctl show -p NRestarts --value wit-amp-sender.service 2>/dev/null)"
printf '  启动于: %s\n' "$(systemctl show -p ActiveEnterTimestamp --value wit-amp-sender.service 2>/dev/null)"
echo "  --- 最近 12 行"
journalctl -u wit-amp-sender.service --no-pager -n 12 2>/dev/null | sed 's/^/      /'

echo
echo "=== 2) sender 进程在做什么 ==="
p=$(systemctl show -p MainPID --value wit-amp-sender.service 2>/dev/null)
echo "  MainPID=$p"
[ -n "$p" ] && [ "$p" != 0 ] && ps -o pid,etime,stat,wchan:20,cmd -p "$p" 2>/dev/null | sed 's/^/      /'

echo
echo "=== 3) sender 的启动参数（看 mirror-dir 与 glob 是否覆盖 kb）==="
systemctl cat wit-amp-sender.service 2>/dev/null | grep -E 'ExecStart' | sed 's/^/  /'

echo
echo "=== 4) 直接验证 glob 表达式能否匹配 kb 文件 ==="
/opt/wit/venv/bin/python - <<'PY'
from pathlib import Path
m = Path('/var/log/acps-amp')
hits = sorted(m.glob('yanshu/amp_heartbeat_*.jsonl'))
print(f"  glob 命中 {len(hits)} 个：")
for h in hits:
    print(f"    {h.name}  ({h.stat().st_size} 字节)")
PY

echo
echo "=== 5) 位点文件的时间戳（看 sender 是否在写）==="
ls -la /var/log/acps-amp/.sender-offsets.json | sed 's/^/  /'
