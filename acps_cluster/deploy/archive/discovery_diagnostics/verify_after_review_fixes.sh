#!/bin/sh
# 独立核查：服务是否就绪 + 本轮修复是否在运行代码里生效
CL=/opt/wit/my-project/acps_cluster
PY=/opt/wit/venv/bin/python

echo "=== 1) 8 个服务 ==="
for n in scout supervisor critic writer synthesis experiment contradiction frontier; do
  pid=$(systemctl show -p MainPID --value wit-yanshu@$n.service 2>/dev/null)
  printf '  %-14s %-9s pid=%s 启动=%s\n' "$n" \
    "$(systemctl is-active wit-yanshu@$n.service 2>/dev/null)" "$pid" \
    "$([ -n "$pid" ] && [ "$pid" != 0 ] && ps -o lstart= -p "$pid" 2>/dev/null | xargs || echo '-')"
done

echo
echo "=== 2) 端口 ==="
ss -tlnp 2>/dev/null | grep -E '844[4-9]|8450|8461' | awk '{print "  "$4}' | sort

echo
echo "=== 3) 运行代码里的修复（导入即生效的检查）==="
cd "$CL" || exit 1
sudo -u wit $PY - <<'PY'
import sys
sys.path.insert(0, '/opt/wit/my-project')
sys.path.insert(0, '/opt/wit/my-project/acps_cluster')
import os
os.environ['YANSHU_RUN_TRACE'] = '0'
import supervisor as s
print(f"  supervisor._WAIT_ROUNDS 是模块级 dict: {isinstance(s._WAIT_ROUNDS, dict)}")
print(f"  SupervisorRunHooks.wrap_run 存在: {hasattr(s.SupervisorRunHooks, 'wrap_run')}")
from delegation import skill_scope
print(f"  delegation.skill_scope 可导入: True")
from delegation import build_pep_hooks
print(f"  build_pep_hooks 接受 capabilities: {'capabilities' in build_pep_hooks.__code__.co_varnames}")
# 抽查一个伙伴的 PEP 钩子数量（应为 3：admission + scrub + scope）
import importlib
for mod_name in ('partner_critic', 'partner_scout'):
    m = importlib.import_module(mod_name)
    b = [n for n in dir(m) if n.startswith('build_') and n.endswith('_agent')][0]
    a = getattr(m, b)(None)
    hooks = getattr(a, 'aip_hooks', None) or []
    ids = [getattr(h, 'id', type(h).__name__) for h in hooks]
    print(f"  {mod_name}: aip_hooks={ids}")
PY

echo
echo "=== 4) 审计路径是否已分散 ==="
for w in scout supervisor critic writer synthesis experiment contradiction frontier; do
  f="$CL/ws-$w/logs/pep-audit.jsonl"
  printf '  ws-%-14s %s\n' "$w" "$([ -f "$f" ] && echo "有 $(wc -l < "$f") 行" || echo '（未生成）')"
done

echo
echo "=== 5) 心跳链路 ==="
for s in wit-amp-mirror wit-amp-sender; do
  printf '  %-16s %s\n' "$s" "$(systemctl is-active $s.service 2>/dev/null)"
done
journalctl -u wit-amp-sender.service --since "-3min" --no-pager 2>/dev/null | tail -3 | sed 's/^/  /'
