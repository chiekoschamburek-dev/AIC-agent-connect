#!/usr/bin/env sh
# 核实新注册草案内容 + 把 partner_kb.py 的 version 提到 1.0.1
CL=/opt/wit/my-project/acps_cluster
PY=/opt/wit/venv/bin/python
WS=/opt/wit/tmp/kb-reregister
stamp=$(date +%Y%m%d-%H%M%S)

echo "=== 1) 新注册草案内容核对 ==="
$PY - "$WS" <<'PY'
import json, sys
from pathlib import Path
ws = Path(sys.argv[1])
d = json.loads((ws / 'acps' / 'acs.draft.json').read_text(encoding='utf-8'))
desc = str(d.get('description') or '')
print(f"  name    = {d.get('name')}")
print(f"  version = {d.get('version')}")
print(f"  aic     = {d.get('aic')!r}（空 = 待发证，正常）")
print(f"  endPoints = {[e.get('url') for e in (d.get('endPoints') or [])]}")
print(f"  description 长度 = {len(desc)}")
print(f"    含「查文献」: {'查文献' in desc}")
print(f"    含「找资料」: {'找资料' in desc}")
print(f"    含「统计一下」: {'统计一下' in desc}")
print(f"    含「权限分级」: {'权限分级' in desc}")
for s in (d.get('skills') or []):
    print(f"  skill {s.get('id'):12s} desc={len(str(s.get('description') or '')):3d} 字 "
          f"tags={len(s.get('tags') or [])} examples={len(s.get('examples') or [])}")
PY

echo
echo "=== 2) 把 partner_kb.py 的 version 提到 1.0.1（与草案对齐）==="
cp -p "$CL/partner_kb.py" "$CL/partner_kb.py.pre-version-bump-$stamp"
echo "  已备份 partner_kb.py.pre-version-bump-$stamp"
$PY - "$CL/partner_kb.py" <<'PY'
import sys
from pathlib import Path
p = Path(sys.argv[1])
t = p.read_text(encoding='utf-8')
old = '        version="1.0.0",'
new = '        version="1.0.1",  # 2026-10-11 门面文案加宽后重新注册（见 docs/kb-discovery-widening.md）'
if old not in t:
    print("  ⚠ 未找到 version=\"1.0.0\", 行——请人工确认")
    raise SystemExit(1)
p.write_text(t.replace(old, new, 1), encoding='utf-8')
print('  ✔ version 1.0.0 → 1.0.1')
PY
chown wit:wit "$CL/partner_kb.py"
grep -n 'version="1.0.1"' "$CL/partner_kb.py" | sed 's/^/  /'

echo
echo "=== 3) 确认 KB 服务仍在跑（代码改了但未重启，符合预期）==="
printf '  wit-yanshu@kb: %s\n' "$(systemctl is-active wit-yanshu@kb.service 2>/dev/null)"
echo "  注意：现在**不要**重启 kb 服务——注册未获批前新 version 会让指纹不匹配、拒绝启动。"

echo
echo "=== 4) 平台上的注册状态 ==="
cd "$CL" || exit 1
sudo -u wit $PY - "$WS" <<'PY'
import json, os, sys
from pathlib import Path
import httpx
CL = Path('/opt/wit/my-project/acps_cluster')
for ln in (CL/'deploy'/'.env.production').read_text(encoding='utf-8').splitlines():
    s = ln.strip()
    if s and not s.startswith('#') and '=' in s:
        k, v = s.split('=', 1); os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
ws = Path(sys.argv[1])
tok = json.loads((ws/'acps'/'secrets'/'registry-token.json').read_text(encoding='utf-8'))
r = httpx.get(os.environ['ACPS_REGISTRY_URL'].rstrip('/') + '/api/v1/agent/client',
              headers={'Authorization': 'Bearer ' + tok['accessToken']}, timeout=25)
items = (r.json() or {}).get('items') or []
print(f"  本账号 Agent 共 {len(items)} 条，KB 相关：")
for it in items:
    if 'KB' in str(it.get('name') or ''):
        acs = it.get('acs') or {}
        print(f"    {str(it.get('name'))[:20]:22s} version={str(it.get('version')):8s} "
              f"approval={it.get('approval_status')} active={acs.get('active')} "
              f"aic={(acs.get('aic') or '待发证')[-22:]}")
PY
