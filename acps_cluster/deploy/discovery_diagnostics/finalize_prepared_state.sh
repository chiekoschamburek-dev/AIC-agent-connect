#!/bin/sh
# 清理探测残留 + 固化"待审批就绪态"
CL=/opt/wit/my-project/acps_cluster
PY=/opt/wit/venv/bin/python

echo "=== 1) 探测工作区的处置 ==="
echo "  注册状态已正式落在 ws-kb（指纹未变）。探测工作区保留为备份，但改名以免被误当成活状态。"
if [ -d /opt/wit/tmp/probe-kb2 ]; then
  mv /opt/wit/tmp/probe-kb2 /opt/wit/tmp/probe-kb2.backup-$(date +%Y%m%d-%H%M%S) 2>/dev/null \
    && echo "  已改名（保留为备份）" || echo "  改名跳过"
fi
ls -d /opt/wit/tmp/probe-kb* 2>/dev/null | sed 's/^/    /'
[ -d /opt/wit/tmp/probe-kb ] && rmdir /opt/wit/tmp/probe-kb 2>/dev/null && echo "  已移除空目录 probe-kb"

echo
echo "=== 2) 待审批就绪态（ws-kb）==="
for rel in acps/enrollment.json acps/acs.draft.json acps/secrets/registry-token.json; do
  f="$CL/ws-kb/$rel"
  [ -f "$f" ] && printf '  ✔ %-34s %s 字节\n' "$rel" "$(stat -c %s "$f")" || printf '  ✘ %-34s 缺失\n' "$rel"
done
$PY - <<'PY'
import json
from pathlib import Path
p = Path('/opt/wit/my-project/acps_cluster/ws-kb/acps/enrollment.json')
d = json.loads(p.read_text(encoding='utf-8'))
r = d.get('review') or {}
print(f"  status={d.get('status')}  approvalStatus={(r.get('metadata') or {}).get('approvalStatus')}")
print(f"  agentId={d.get('agentId')}  registrationId={d.get('registrationId')}")
print(f"  acsFingerprint={str(d.get('acsFingerprint'))[:36]}…（批准后必须保持此值）")
PY

echo
echo "=== 3) 未提交的（按你的决定）==="
for w in cpu gpu kb; do
  if [ -d "$CL/ws-$w/acps" ]; then
    printf '  ws-%-5s 已有 acps/（%s）\n' "$w" "$(ls -1 "$CL/ws-$w/acps" | tr '\n' ' ')"
  else
    printf '  ws-%-5s 尚未创建（注册未提交）\n' "$w"
  fi
done

echo
echo "=== 4) 一切就绪后的一条命令 ==="
cat <<'EOF'
  # 平台批准 KB 后：
  sh deploy/discovery_diagnostics/recover_kb_registration.sh
  # 它会：续上注册 → 拿证 → 证书链检查 → 落 per-agent drop-in → 启动服务 → 生成并签名 KB 章程
EOF
