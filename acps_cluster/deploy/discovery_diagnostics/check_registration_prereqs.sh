#!/bin/sh
# 核实 KB / CPU / GPU 的注册前置：账号、registry 可达性、workspace 状态
CL=/opt/wit/my-project/acps_cluster
echo "=== 1) .env.production 里的 registry 与各 agent 账号 ==="
grep -nE 'ACPS_REGISTRY_URL|ACPS_(KB|CPU|GPU)_(USERNAME|PASSWORD)' "$CL/deploy/.env.production" 2>/dev/null \
  | sed -E 's/(PASSWORD=).*/\1***/' | sed 's/^/  /' || echo "  （无匹配）"

echo
echo "=== 2) 对照：已有 agent 的账号变量名（以 SCOUT 为例）==="
grep -nE 'ACPS_[A-Z]+_USERNAME' "$CL/deploy/.env.production" 2>/dev/null | sed -E 's/=(.*)/=\1/' | sed 's/^/  /'

echo
echo "=== 3) registry 是否可达 ==="
/opt/wit/venv/bin/python - <<'PY'
import os
from pathlib import Path
env = {}
for ln in (Path('/opt/wit/my-project/acps_cluster/deploy/.env.production')).read_text(encoding='utf-8').splitlines():
    ln = ln.strip()
    if ln and not ln.startswith('#') and '=' in ln:
        k, v = ln.split('=', 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
base = env.get('ACPS_REGISTRY_URL') or env.get('ACPS_CA_BASE_URL') or ''
print(f"  ACPS_REGISTRY_URL = {base!r}")
if base:
    import httpx
    for path in ('/api/v1/auth/login', '/'):
        try:
            r = httpx.get(base.rstrip('/') + path, timeout=10, follow_redirects=True)
            print(f"  GET {path} → HTTP {r.status_code}")
        except Exception as exc:
            print(f"  GET {path} → 失败 {type(exc).__name__}: {exc}")
PY

echo
echo "=== 4) 新工作区当前状态 ==="
for w in cpu gpu kb; do
  d="$CL/ws-$w"
  if [ -d "$d/acps" ]; then
    printf '  ws-%-5s 有 acps/: %s\n' "$w" "$(ls -1 "$d/acps" | tr '\n' ' ')"
  else
    printf '  ws-%-5s 【无工作区】\n' "$w"
  fi
done

echo
echo "=== 5) systemd 是否已有 cpu/gpu/kb 的 unit 实例定义 ==="
for n in cpu gpu kb; do
  printf '  wit-yanshu@%s: %s\n' "$n" "$(systemctl list-unit-files 'wit-yanshu@*' 2>/dev/null | grep -c "wit-yanshu@.service" | sed 's/^1$/模板存在（实例按需启动）/')"
done
