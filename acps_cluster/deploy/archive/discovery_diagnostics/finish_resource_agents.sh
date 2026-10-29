#!/bin/sh
# 审核通过后的一键收尾：证书链检查 → PEP 加固配置 → 启动服务 → 生成 KB 章程
#
# 用法（平台审核通过后执行）：
#   sh deploy/discovery_diagnostics/finish_resource_agents.sh [cpu gpu kb]
# 默认处理全部三个。
set -u
CL=/opt/wit/my-project/acps_cluster
PY=/opt/wit/venv/bin/python
LEADER=1.2.156.3088.1.BUPT.VTF97R.JXN8O8.98DOO8.0EB6
TARGETS="${*:-cpu gpu kb}"

echo "############ 0) 目标: $TARGETS"

echo
echo "############ 1) 先确认三个 agent 已拿到证书（未审核通过会显示 pending）"
for w in $TARGETS; do
  f="$CL/ws-$w/acps/enrollment.json"
  if [ -f "$f" ]; then
    aic=$($PY -c "import json,sys; print(((json.load(open(sys.argv[1])).get('identity') or {}).get('ref') or {}).get('aic') or '（尚未发证）')" "$f" 2>/dev/null)
    printf '  ws-%-5s %s\n' "$w" "$aic"
  else
    printf '  ws-%-5s 【无 enrollment.json（还没提交注册？）】\n' "$w"
  fi
done

echo
echo "############ 2) 证书链检查（新工作区必查：signing.chain 必须 ≥2）"
for w in $TARGETS; do
  b="$CL/ws-$w/acps/trust-bundle.pem"
  c="$CL/ws-$w/acps/client/certificate.pem"
  if [ -f "$b" ] && [ -f "$c" ]; then
    n=$(grep -c 'BEGIN CERT' "$b")
    printf '  ws-%-5s bundle=%s 张\n' "$w" "$n"
  else
    printf '  ws-%-5s 【缺 bundle 或 client 证书】\n' "$w"
  fi
done
echo "  --- 执行修复脚本（幂等：已含中间证书则跳过）"
sh "$CL/deploy/discovery_diagnostics/fix_trust_bundle.sh" 2>&1 | sed 's/^/  /' | tail -30

echo
echo "############ 3) 落 PEP 加固配置（**按 agent 的 drop-in**，不动共享 env）"
# 实测（2026-10-11）：per-agent env 是通过 drop-in 注入的：
#   /etc/systemd/system/wit-yanshu@<agent>.service.d/pep.conf
#     [Service]
#     EnvironmentFile=/etc/wit-<agent>-pep.env
# 而共享 .env.production 里**不能**写 ACPS_PEP_LEADER_DIRECT=0——
# 那会让 leader 连现有 8 个伙伴也被要求令牌，调度链直接断。
for w in $TARGETS; do
  env_f="/etc/wit-$w-pep.env"
  cat > "$env_f" <<EOF
# 2026-10-11 资源 agent 使用控制（审核通过后由脚本写入）
ACPS_PEP_ENABLED=1
# 保留 leader 的**签发资格**（承认它签的令牌）
ACPS_PEP_LEADER_AICS=$LEADER
# 关掉"直调免令牌"通道 → leader 也必须持令牌
ACPS_PEP_LEADER_DIRECT=0
# 首次注册若用了占位 AIC，拿证后必须清掉（否则 aud 校验用错身份）
ACPS_PEP_MY_AIC=
EOF
  drop="/etc/systemd/system/wit-yanshu@$w.service.d"
  mkdir -p "$drop"
  cat > "$drop/pep.conf" <<EOF
[Service]
EnvironmentFile=$env_f
EOF
  printf '  %-42s env ✔\n' "$env_f"
  printf '  %-42s drop-in ✔\n' "$drop/pep.conf"
done
systemctl daemon-reload
echo "  已 daemon-reload"
echo "  --- 对照：scout 的 drop-in 同款写法"
systemctl show -p EnvironmentFiles --value wit-yanshu@scout.service 2>/dev/null | tr ' ' '\n' | sed 's/^/      /'

echo
echo "############ 4) 启动三个服务（unit 模板已存在：wit-yanshu@{cpu,gpu,kb}）"
for w in $TARGETS; do
  systemctl reset-failed "wit-yanshu@$w.service" 2>/dev/null
  systemctl start "wit-yanshu@$w.service"
  printf '  已 start wit-yanshu@%s\n' "$w"
done
echo "  注意：ExecStartPre 有 0-300 秒随机 sleep（错峰），activating 是正常的。"
i=0
while [ $i -lt 24 ]; do
  i=$((i+1)); sleep 30
  line=""; allok=1
  for w in $TARGETS; do
    st=$(systemctl is-active "wit-yanshu@$w.service" 2>/dev/null)
    if [ "$st" = "active" ]; then line="$line $w✔"; else line="$line $w:$st"; allok=0; fi
  done
  printf '  +%3ds:%s\n' $((i*30)) "$line"
  [ $allok -eq 1 ] && break
done

echo
echo "############ 5) 端口"
ss -tlnp 2>/dev/null | grep -E '846[2-4]' | awk '{print "  "$4}' | sort

echo
echo "############ 6) 生成并签名 yanshu-kb 域章程（此时 KB 已有证书，可双签）"
kb_aic=$($PY -c "
import json
d = json.load(open('$CL/ws-kb/acps/enrollment.json'))
print(((d.get('identity') or {}).get('ref') or {}).get('aic') or '')
" 2>/dev/null)
if [ -n "$kb_aic" ]; then
  echo "  KB AIC = $kb_aic"
  cd "$CL" || exit 1
  sudo -u wit $PY deploy/make_kb_domain_charter.py \
      --kb-aic "$kb_aic" --leader-aic "$LEADER" --kb-workspace-name kb 2>&1 | sed 's/^/  /'
else
  echo "  ✘ 取不到 KB AIC（KB 尚未发证）——章程留到发证后再跑："
  echo "     python deploy/make_kb_domain_charter.py --kb-aic <KB AIC> --leader-aic $LEADER --kb-workspace-name kb"
fi

echo
echo "############ 7) 验收：三个 agent 的准入矩阵 + 令牌链"
cd "$CL" || exit 1
sudo -u wit $PY deploy/discovery_diagnostics/e2e_leader_token_admission.py 2>&1 | tail -6 | sed 's/^/  /'
