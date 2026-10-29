#!/bin/sh
# 修复：把 Agent CA Intermediate 补进各工作区的 trust-bundle.pem
#
# 根因（2026-10-11 实测）：
#   trust-bundle.pem 里只有 2 个自签根，**缺签发我们 leaf 的中间 CA**
#   → build_certificate_chain 补不出中间证书（chain 长度退化为 1）
#   → _verify_chain 的"链尾为锚"与"用 bundle 锚验链尾签名"两个分支都失败
#   → 任何委托令牌都报 `TokenError: chain-untrusted: chain tail … is not anchored`
#
# 修法：把 client/certificate.pem 里的中间证书追加到 trust-bundle.pem（幂等：已含则跳过）。
CL=/opt/wit/my-project/acps_cluster
PY=/opt/wit/venv/bin/python
stamp=$(date +%Y%m%d-%H%M%S)

echo "=== 逐工作区检查并修复 ==="
for d in "$CL"/ws-*; do
  [ -d "$d/acps" ] || continue
  w=$(basename "$d")
  bundle="$d/acps/trust-bundle.pem"
  client="$d/acps/client/certificate.pem"
  [ -f "$bundle" ] || { printf '  %-18s 【无 bundle】\n' "$w"; continue; }
  [ -f "$client" ] || { printf '  %-18s 【无 client 证书】\n' "$w"; continue; }

  need=$($PY - "$bundle" "$client" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, '/opt/wit/my-project')
sys.path.insert(0, '/opt/wit/my-project/acps_cluster')
from delegation.token import load_certificates
bundle, client = Path(sys.argv[1]), Path(sys.argv[2])
have = {c.subject.rfc4514_string() for c in load_certificates(bundle)}
leaf = load_certificates(client)[0]
print("yes" if leaf.issuer.rfc4514_string() not in have else "no")
PY
)
  if [ "$need" = "yes" ]; then
    cp -p "$bundle" "$bundle.pre-intermediate-$stamp"
    cat "$client" >> "$bundle"
    printf '  %-18s ✔ 已补中间证书（原文件已备份）\n' "$w"
  else
    printf '  %-18s 已含中间证书，跳过\n' "$w"
  fi
done

echo
echo "=== 复核：各工作区 bundle 的证书数与链长度 ==="
for d in "$CL"/ws-*; do
  [ -d "$d/acps" ] || continue
  w=$(basename "$d")
  $PY - "$d" "$w" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, '/opt/wit/my-project')
sys.path.insert(0, '/opt/wit/my-project/acps_cluster')
from delegation.token import load_certificates, load_signing_context
d, w = Path(sys.argv[1]), sys.argv[2]
bundle = d / 'acps' / 'trust-bundle.pem'
client = d / 'acps' / 'client'
try:
    n = len(load_certificates(bundle))
    sc = load_signing_context(client, trust_bundle=bundle)
    print(f"  {w:18s} bundle 证书数={n}  signing.chain 长度={len(sc.chain)}")
except Exception as exc:
    print(f"  {w:18s} 读取失败 {type(exc).__name__}: {exc}")
PY
done
