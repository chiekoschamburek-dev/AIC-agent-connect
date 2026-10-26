#!/bin/sh
# KB 重新注册（门面文案加宽后）：
#   · 在**临时工作区**提交，避免动 ws-kb 的证书/终稿 → 在跑的 KB 服务不受影响
#   · 沿用同一个 publicEndpoint（8599）避免 KB 上次"端口不一致"的坑
#   · agent version 提到 1.0.1，明确表示这是新版本
#   · 提交后停在 pending_review（需平台方 STAFF 审批）
set -u
CL=/opt/wit/my-project/acps_cluster
PY=/opt/wit/venv/bin/python
WS=/opt/wit/tmp/kb-reregister
ENVFILE="$CL/deploy/.env.production"
PORT=8599

echo "############ 0) 前置：确认文案改动已生效"
$PY - <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, '/opt/wit/my-project'); sys.path.insert(0, '/opt/wit/my-project/acps_cluster')
import discovery_metadata as dm
d = dm.agent_discovery_description('yanshu.kb-partner', 'x')
print(f"  新 agent 描述长度 = {len(d)}（原 209）")
print(f"  含「查文献」: {'查文献' in d}   含「找资料」: {'找资料' in d}")
PY

echo
echo "############ 1) 准备临时工作区 $WS"
rm -rf "$WS"; mkdir -p "$WS"; chown wit:wit "$WS"
echo "  ✔ 已创建（KB 服务继续用 ws-kb，不受影响）"

echo
echo "############ 2) 提交注册（version 1.0.1，端点沿用 $PORT）"
cd "$CL" || exit 1
timeout 180 sudo -u wit YANSHU_RUN_TRACE=0 $PY - "$WS" "$ENVFILE" "$PORT" <<'PY' 2>&1 | tail -25
import asyncio, os, sys
from pathlib import Path

ws, envfile, port = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
for ln in envfile.read_text(encoding='utf-8').splitlines():
    s = ln.strip()
    if s and not s.startswith('#') and '=' in s:
        k, v = s.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
# 首次注册尚无证书 → 占位 AIC 让 PEP 启动期校验通过（已知缺陷的绕法）
os.environ.setdefault('ACPS_PEP_MY_AIC', '1.2.156.3088.1.BUPT.VTF97R.PLACEHLD.PLACEH.PLC1')

sys.path.insert(0, '/opt/wit/my-project'); sys.path.insert(0, '/opt/wit/my-project/acps_cluster')
import bootstrap, cluster_common, partner_kb
from wit import WitAgent

# version 提到 1.0.1：明确这是新版本（平台按 agentId+version 管理）
def build(provider):
    base = partner_kb.build_kb_agent(provider)
    try:
        import dataclasses
        return dataclasses.replace(base, version="1.0.1")
    except Exception as exc:
        print(f"  ⚠ 无法改 version（{type(exc).__name__}: {exc}），沿用原 version")
        return base

async def main():
    import wit as _wit
    provider = bootstrap.build_provider()
    rt = _wit.WitRuntime(build(provider), workspace=ws,
                         acps=cluster_common.build_acps_config(port))
    try:
        res = await rt.up_until_ready(serve=False,
                                      credentials=cluster_common.build_credentials("KB"))
        print(f"  ready       = {res.ready}")
        print(f"  status      = {res.status}")
        print(f"  next_action = {str(getattr(res, 'next_action', None))[:190]}")
        print(f"  registrationId = {getattr(res, 'registration_id', None)}")
        if getattr(res, 'identity', None):
            print(f"  ✔ AIC = {res.identity.aic}")
    except SystemExit as exc:
        print(f"  SystemExit code={exc.code}")
    except Exception as exc:
        print(f"  {type(exc).__name__}: {str(exc)[:300]}")
    finally:
        await provider.aclose()

asyncio.run(main())
PY

echo
echo "############ 3) 临时工作区产物"
find "$WS" -maxdepth 3 -type f 2>/dev/null | sed 's/^/  /'
$PY - "$WS" <<'PY' 2>/dev/null
import json, sys
from pathlib import Path
p = Path(sys.argv[1]) / 'acps' / 'enrollment.json'
if p.is_file():
    d = json.loads(p.read_text(encoding='utf-8'))
    r = d.get('review') or {}
    print(f"  status={d.get('status')}  registrationId={d.get('registrationId')}")
    print(f"  approvalStatus={(r.get('metadata') or {}).get('approvalStatus')}")
    print(f"  acsFingerprint={str(d.get('acsFingerprint'))[:36]}…")
    print(f"  publicEndpoint={(d.get('acpsConfig') or {}).get('publicEndpoint')}")
PY

echo
echo "############ 4) 确认线上 KB 未受影响"
printf '  wit-yanshu@kb: %s\n' "$(systemctl is-active wit-yanshu@kb.service 2>/dev/null)"
ss -tlnp 2>/dev/null | grep 8599 | awk '{print "  监听: "$4}'
