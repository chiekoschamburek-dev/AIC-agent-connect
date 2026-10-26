#!/bin/sh
# 核实服务器上文档与诊断脚本的真实位置（不是"缺失"，可能只是路径不同）
CL=/opt/wit/my-project/acps_cluster
echo "=== 1) 全盘找这些文件 ==="
for f in math-rendering-fix.md review-fixes-2026-10-11.md agent-aic-roster.md \
         fix_trust_bundle.sh e2e_leader_token_admission.py inbound-result-delivery-aip-submit.md; do
  hits=$(find /opt/wit/my-project -name "$f" 2>/dev/null | head -3)
  if [ -n "$hits" ]; then
    printf '  ✔ %s\n' "$f"
    echo "$hits" | sed 's/^/      /'
  else
    printf '  ✘ %s（服务器上没有）\n' "$f"
  fi
done

echo
echo "=== 2) discovery_diagnostics 目录内容（前 25 个）==="
ls -1 "$CL/deploy/discovery_diagnostics" 2>/dev/null | head -25 | sed 's/^/  /'
echo "  ... 共 $(ls -1 "$CL/deploy/discovery_diagnostics" 2>/dev/null | wc -l) 个文件"

echo
echo "=== 3) docs 目录是否存在 ==="
ls -la /opt/wit/my-project/docs 2>/dev/null | head -12 | sed 's/^/  /' || echo "  【/opt/wit/my-project/docs 不存在】"
