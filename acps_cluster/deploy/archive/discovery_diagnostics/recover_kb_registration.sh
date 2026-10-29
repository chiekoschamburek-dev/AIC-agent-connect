#!/bin/sh
# 审核通过后：把 KB 注册状态恢复/迁移到正式工作区 ws-kb，并跑通到就绪
#
# 背景（2026-10-11 实测）：
#   · 注册是程序化提交的，但**必须平台侧审核**（status=pending_review）。
#   · 我提交探测时用的工作区是 /opt/wit/tmp/probe-kb2；正式服务用 ws-kb。
#   · 注册绑定的是 (account, agentId)，因此把 draftPath 指向正式工作区的草案后，
#     在 ws-kb 里重跑 up_until_ready 即可续上同一份注册（不会再提一份）。
#
# 用法：sh recover_kb_registration.sh [探测工作区]
set -u
CL=/opt/wit/my-project/acps_cluster
PY=/opt/wit/venv/bin/python
ENVFILE="$CL/deploy/.env.production"
DST="$CL/ws-kb"
# 探测工作区在固化就绪态时被改名为备份 → 自动探测最新那个
SRC="${1:-$(ls -d /opt/wit/tmp/probe-kb2* 2>/dev/null | tail -1)}"

echo "############ 1) 前置检查"
if [ -f "$DST/acps/enrollment.json" ] && [ -f "$DST/acps/acs.draft.json" ]; then
  echo "  ✔ 状态已在正式工作区 $DST（无需探测工作区）"
else
  for f in "$SRC/acps/enrollment.json" "$SRC/acps/acs.draft.json"; do
    [ -f "$f" ] && printf '  ✔ %s\n' "$f" || { printf '  ✘ 缺 %s（且 ws-kb 也无状态）\n' "$f"; exit 2; }
  done
fi
$PY - "$DST" <<'PY'
import json, sys
from pathlib import Path
p = Path(sys.argv[1]) / 'acps' / 'enrollment.json'
d = json.loads(p.read_text(encoding='utf-8'))
r = d.get('review') or {}
m = r.get('metadata') or {}
print(f"  status={d.get('status')}  approvalStatus={m.get('approvalStatus')}")
print(f"  registrationId={d.get('registrationId')}")
print(f"  review.aic={r.get('aic')}")
PY

echo
echo "############ 2) 确保注册状态在正式工作区 $DST（已迁移则跳过）"
if [ -f "$DST/acps/enrollment.json" ] && [ -f "$DST/acps/acs.draft.json" ]; then
  echo "  ✔ ws-kb 已有注册状态（此前已用 prepare_kb_workspace.sh 迁移，指纹未变）——跳过迁移"
  $PY - "$DST" <<'PY'
import json, sys
from pathlib import Path
d = json.loads((Path(sys.argv[1]) / 'acps' / 'enrollment.json').read_text(encoding='utf-8'))
print(f"      status={d.get('status')} agentId={d.get('agentId')}")
print(f"      acsFingerprint={str(d.get('acsFingerprint'))[:36]}…（必须保持不变）")
PY
else
  echo "  ws-kb 无状态 → 从 $SRC 迁移"
  mkdir -p "$DST/acps/secrets"
  cp -p "$SRC/acps/acs.draft.json" "$DST/acps/acs.draft.json"
  cp -p "$SRC/acps/secrets/registry-token.json" "$DST/acps/secrets/registry-token.json"
  $PY - "$SRC" "$DST" <<'PY'
import json, sys
from pathlib import Path
src, dst = Path(sys.argv[1]), Path(sys.argv[2])
d = json.loads((src / 'acps' / 'enrollment.json').read_text(encoding='utf-8'))
old = d.get('draftPath')
d['draftPath'] = str(dst / 'acps' / 'acs.draft.json')
(dst / 'acps' / 'enrollment.json').write_text(
    json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
print(f"  draftPath: {old} → {d['draftPath']}")
PY
  chown -R wit:wit "$DST"
fi

echo
echo "############ 3) 在正式工作区重跑 up_until_ready（最多 120 秒）"
cd "$CL" || exit 1
timeout 120 sudo -u wit YANSHU_RUN_TRACE=0 $PY - "$DST" "$ENVFILE" <<'PY' 2>&1 | tail -12
import asyncio, os, sys
from pathlib import Path
envfile = Path(sys.argv[2])
for ln in envfile.read_text(encoding='utf-8').splitlines():
    s = ln.strip()
    if s and not s.startswith('#') and '=' in s:
        k, v = s.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
os.environ.setdefault('ACPS_PEP_MY_AIC', '1.2.156.3088.1.BUPT.VTF97R.PLACEHLD.PLACEH.PLC1')
sys.path.insert(0, '/opt/wit/my-project'); sys.path.insert(0, '/opt/wit/my-project/acps_cluster')
import bootstrap, cluster_common
from wit import WitRuntime
import partner_kb

async def main():
    provider = bootstrap.build_provider()
    rt = WitRuntime(partner_kb.build_kb_agent(provider), workspace=Path(sys.argv[1]),
                    acps=cluster_common.build_acps_config(8464))
    try:
        res = await rt.up_until_ready(serve=False, credentials=cluster_common.build_credentials("KB"))
        print(f"  ready={res.ready} status={res.status}")
        print(f"  next_action={str(getattr(res,'next_action',None))[:160]}")
        if getattr(res, 'identity', None):
            print(f"  ✔ AIC={res.identity.aic}")
    finally:
        await provider.aclose()

asyncio.run(main())
PY

echo
echo "############ 4) 拿到 AIC 后：完整收尾（证书链/PEP/服务/章程）"
aic=$($PY -c "
import json
from pathlib import Path
p = Path('$DST/acps/enrollment.json')
d = json.loads(p.read_text(encoding='utf-8'))
print(((d.get('identity') or {}).get('ref') or {}).get('aic') or '')
" 2>/dev/null)
if [ -n "$aic" ]; then
  echo "  KB AIC = $aic → 执行 finish_resource_agents.sh kb"
  sh "$CL/deploy/discovery_diagnostics/finish_resource_agents.sh" kb
else
  echo "  仍未发证 → 等平台批准后重跑本脚本。"
fi
