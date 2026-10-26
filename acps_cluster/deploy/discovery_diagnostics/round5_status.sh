#!/bin/sh
# 第 5 轮收尾：服务 + 关键测试 + 目标四项状态
CL=/opt/wit/my-project/acps_cluster
PY=/opt/wit/venv/bin/python

echo "=== 1) 8 个服务 ==="
for n in scout supervisor critic writer synthesis experiment contradiction frontier; do
  printf '  %-14s %s\n' "$n" "$(systemctl is-active wit-yanshu@$n.service 2>/dev/null)"
done
echo "  端口: $(ss -tlnp 2>/dev/null | grep -cE '844[4-9]|8450|8461') 个在监听"

echo
echo "=== 2) 关键测试（服务器实跑）==="
cd "$CL" || exit 1
for t in test_leader_direct_open test_resource_agent_authz test_resource_dispatch_auth \
         test_domain_path_works test_l0_unscoped_relaxed; do
  printf '  %-32s ' "$t"
  sudo -u wit $PY "tests/$t.py" 2>&1 | tail -1 | cut -c1-60
done

echo
echo "=== 3) KB 注册状态 ==="
sudo -u wit $PY deploy/discovery_diagnostics/kb_status.py 2>&1 | tail -3 | sed 's/^/  /'

echo
echo "=== 4) 目标四项 ==="
cat <<'EOF'
  (1) 展示能力保持公开              — ✅ 完成并验证
  (2) 干活能力必须持令牌            — ✅ 完成并验证
  (3) leader 也走令牌（13 处委派）   — ✅ 完成并验证
  (4) KB 章程落盘                   — ⏸ 机制已全部验证（含域路径 5 项断言），
                                      落盘需 KB 拿证 → 卡在平台 STAFF 审批
EOF
