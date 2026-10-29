#!/bin/sh
# 精确定位：请求到底有没有到伙伴 / 裁决成了什么
CL=/opt/wit/my-project/acps_cluster
PY=/opt/wit/venv/bin/python

echo "=== 1) 审计路径到底解析成哪里（关键：没有 audit 文件 = 没跑过裁决，或路径不对）==="
cd "$CL" || exit 1
sudo -u wit YANSHU_RUN_TRACE=0 $PY - <<'PY'
import sys, os
sys.path.insert(0, '/opt/wit/my-project')
sys.path.insert(0, '/opt/wit/my-project/acps_cluster')
from pathlib import Path
from delegation.pep import load_pep_config
from delegation.audit import default_audit_path
for w in ('scout', 'critic', 'writer'):
    ws = Path(f'/opt/wit/my-project/acps_cluster/ws-{w}')
    print(f"  ws-{w}: default_audit_path = {default_audit_path(ws)}")
    print(f"          exists = {default_audit_path(ws).exists()}")
print(f"  环境里 ACPS_PEP_AUDIT_PATH = {os.environ.get('ACPS_PEP_AUDIT_PATH')!r}")
PY

echo
echo "=== 2) scout 的全部日志（不限时间，看有没有任何入站痕迹）==="
journalctl -u wit-yanshu@scout.service --no-pager 2>/dev/null | wc -l | sed 's/^/  总日志行数: /'
journalctl -u wit-yanshu@scout.service --no-pager 2>/dev/null | tail -12 | sed 's/^/  /'

echo
echo "=== 3) 直接对 scout 端口做一次 TCP 连接测试 ==="
$PY - <<'PY'
import socket
for name, port in (('scout', 8445), ('supervisor', 8444), ('critic', 8448)):
    s = socket.socket(); s.settimeout(4)
    try:
        s.connect(('127.0.0.1', port))
        print(f"  {name:12s}:{port} TCP 连接成功")
    except Exception as exc:
        print(f"  {name:12s}:{port} 连接失败 {type(exc).__name__}: {exc}")
    finally:
        s.close()
PY

echo
echo "=== 4) 各工作区的 AIC 与常驻状态（从 workspace 配置读）==="
for w in scout supervisor critic writer synthesis contradiction frontier experiment; do
  f="$CL/ws-$w/acps/enrollment.json"
  if [ -f "$f" ]; then
    aic=$($PY -c "import json,sys; print(json.load(open(sys.argv[1])).get('identity','?'))" "$f" 2>/dev/null)
    printf '  ws-%-14s %s\n' "$w" "$aic"
  else
    printf '  ws-%-14s 【无 enrollment.json】\n' "$w"
  fi
done

echo
echo "=== 5) 平台侧：我们的 agent 是否还在 alive 名单 ==="
$PY - <<'PY'
import json, glob
from pathlib import Path
# 从最近的心跳/发现缓存里找
for pat in ('/opt/wit/my-project/acps_cluster/ws-supervisor/logs/*discover*',
            '/opt/wit/my-project/acps_cluster/ws-supervisor/logs/*alive*',
            '/tmp/*discover*', '/tmp/*alive*'):
    for f in glob.glob(pat):
        print(f"  找到 {f}")
PY
ls -la /opt/wit/my-project/acps_cluster/ws-supervisor/logs/ 2>/dev/null | head -20 | sed 's/^/  /'

echo
echo "=== 6) 心跳是否仍在投递（平台判离线的直接原因）==="
journalctl -u wit-amp-sender.service --since "-10min" --no-pager 2>/dev/null | tail -6 | sed 's/^/  /'
echo "  --- mirror 最近"
journalctl -u wit-amp-mirror.service --since "-10min" --no-pager 2>/dev/null | tail -6 | sed 's/^/  /'
echo "  --- 各 workspace 心跳文件新鲜度"
for w in scout critic writer synthesis contradiction frontier experiment supervisor; do
  f="$CL/ws-$w/logs/amp_heartbeat_$w.jsonl"
  if [ -f "$f" ]; then
    printf '  ws-%-14s 最后写入 %s（%s 秒前）\n' "$w" "$(date -r "$f" '+%H:%M:%S')" "$(( $(date +%s) - $(stat -c %Y "$f") ))"
  else
    printf '  ws-%-14s 【无心跳文件】\n' "$w"
  fi
done
