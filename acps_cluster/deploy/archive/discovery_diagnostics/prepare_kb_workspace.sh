#!/bin/sh
# 预备：把 KB 注册状态迁移到正式工作区 ws-kb（**纯复制，不含任何平台调用**）
# 这样一等平台批准，recover 脚本就能直接续上。
set -u
CL=/opt/wit/my-project/acps_cluster
PY=/opt/wit/venv/bin/python
SRC="${1:-/opt/wit/tmp/probe-kb2}"
DST="$CL/ws-kb"

echo "=== 1) 前置检查 ==="
for f in "$SRC/acps/enrollment.json" "$SRC/acps/acs.draft.json" "$SRC/acps/secrets/registry-token.json"; do
  [ -f "$f" ] && printf '  ✔ %s\n' "$f" || { printf '  ✘ 缺 %s\n' "$f"; exit 2; }
done

echo
echo "=== 2) 迁移到 $DST ==="
mkdir -p "$DST/acps/secrets"
cp -p "$SRC/acps/acs.draft.json" "$DST/acps/acs.draft.json"
cp -p "$SRC/acps/secrets/registry-token.json" "$DST/acps/secrets/registry-token.json"
$PY - "$SRC" "$DST" <<'PY'
import json, sys
from pathlib import Path
src, dst = Path(sys.argv[1]), Path(sys.argv[2])
d = json.loads((src / 'acps' / 'enrollment.json').read_text(encoding='utf-8'))
old = d.get('draftPath')
new = str(dst / 'acps' / 'acs.draft.json')
d['draftPath'] = new
out = dst / 'acps' / 'enrollment.json'
out.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
print(f"  draftPath: {old}")
print(f"          → {new}")
print(f"  已写入 {out}")
# 指纹必须保持不变（决定 wit 是否接受该工作区）
print(f"  acsFingerprint 保持原值: {str(d.get('acsFingerprint'))[:36]}…")
PY
chown -R wit:wit "$DST"

echo
echo "=== 3) 回读校验 ==="
$PY - "$SRC" "$DST" <<'PY'
import json, sys
from pathlib import Path
src, dst = Path(sys.argv[1]), Path(sys.argv[2])
a = json.loads((src / 'acps' / 'enrollment.json').read_text(encoding='utf-8'))
b = json.loads((dst / 'acps' / 'enrollment.json').read_text(encoding='utf-8'))
ok = True
for k in ('agentId', 'registrationId', 'acsFingerprint', 'status'):
    same = a.get(k) == b.get(k)
    ok = ok and same
    print(f"  {k:16s} {'✔ 一致' if same else '✘ 不一致'}  {str(b.get(k))[:40]}")
sa = (src / 'acps' / 'acs.draft.json').read_bytes()
sb = (dst / 'acps' / 'acs.draft.json').read_bytes()
print(f"  acs.draft.json 字节一致: {'✔' if sa == sb else '✘'}（{len(sb)} 字节）")
ta = (src / 'acps' / 'secrets' / 'registry-token.json').read_bytes()
tb = (dst / 'acps' / 'secrets' / 'registry-token.json').read_bytes()
print(f"  registry-token 字节一致: {'✔' if ta == tb else '✘'}")
print()
print("  ✔ 迁移完成且指纹未变——平台批准后可直接续用。" if ok else "  ✘ 迁移有偏差，需人工核。")
PY
