#!/bin/sh
# 本轮收尾核验：关键设施与测试在服务器上是否齐备
CL=/opt/wit/my-project/acps_cluster
PY=/opt/wit/venv/bin/python

echo "=== 1) 关键测试文件是否齐备 ==="
for t in test_leader_direct_open test_resource_agent_authz test_resource_dispatch_auth \
         test_math_delimiters test_math_normalize_wiring test_resource_dispatch_auth; do
  f="$CL/tests/$t.py"
  [ -f "$f" ] && printf '  ✔ %s\n' "$t" || printf '  ✘ 缺 %s\n' "$t"
done

echo
echo "=== 2) 逐个跑（只看结论）==="
cd "$CL" || exit 1
for t in test_leader_direct_open test_resource_agent_authz test_resource_dispatch_auth; do
  printf '  %-32s ' "$t"
  sudo -u wit $PY "tests/$t.py" 2>&1 | tail -1
done

echo
echo "=== 3) 文档是否就位 ==="
for d in resource-agents-authz-acceptance.md math-rendering-fix.md review-fixes-2026-10-11.md agent-aic-roster.md; do
  f="/opt/wit/my-project/docs/$d"
  [ -f "$f" ] && printf '  ✔ %s (%s 字节)\n' "$d" "$(stat -c %s "$f")" || printf '  ✘ 缺 %s\n' "$d"
done

echo
echo "=== 4) 收尾脚本就位 ==="
for s in finish_resource_agents.sh recover_kb_registration.sh fix_trust_bundle.sh \
         e2e_leader_token_admission.py make_kb_domain_charter.py; do
  f="$CL/deploy/discovery_diagnostics/$s"
  [ -f "$f" ] || f="$CL/deploy/$s"
  [ -f "$f" ] && printf '  ✔ %s\n' "$(basename "$f")" || printf '  ✘ 缺 %s\n' "$s"
done

echo
echo "=== 5) 服务与端口 ==="
for n in scout supervisor critic writer synthesis experiment contradiction frontier; do
  printf '  %-14s %s\n' "$n" "$(systemctl is-active wit-yanshu@$n.service 2>/dev/null)"
done
ss -tlnp 2>/dev/null | grep -E '844[4-9]|8450|8461' | awk '{print "  "$4}' | sort
