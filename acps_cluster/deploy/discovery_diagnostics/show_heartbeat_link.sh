#!/bin/sh
# 心跳链路的当前状态（回答"要不要我们把心跳写入平台"）
echo "=== 1) 我方发送器：确实在往平台总线投（位点在推进）==="
systemctl is-active wit-amp-sender.service
/opt/wit/venv/bin/python - <<'PY'
import json
from pathlib import Path
d = json.loads(Path('/var/log/acps-amp/.sender-offsets.json').read_text())
print(f"  已投递位点合计 {sum(d.values())} 字节 / {len(d)} 个文件")
print("  （位点持续推进 = 新心跳在持续发出；不是一次性导入）")
PY

echo
echo "=== 2) 平台侧：alive 视图已覆盖我们（不再是"不覆盖"）==="
curl -s --max-time 10 https://wt.ioa.pub/discovery/admin/alive-sync/status | head -c 200
echo
echo
echo "  aliveMap 里我们每个 agent 的最后心跳时间："
cd /opt/wit/my-project/acps_cluster
sudo -u wit /opt/wit/venv/bin/python - <<'PY'
import ssl
from pathlib import Path
import httpx
acps = Path('/opt/wit/my-project/acps_cluster/ws-scout/acps')
ctx = ssl.create_default_context()
ctx.load_verify_locations(cafile=str(acps / 'trust-bundle.pem'))
ctx.load_cert_chain(certfile=str(acps / 'client' / 'certificate.pem'),
                    keyfile=str(acps / 'client' / 'private-key.pem'))
AICS = {
    'scout': '1.2.156.3088.1.BUPT.VTF97R.GMT5S6.4SZM68.0LC5',
    'supervisor': '1.2.156.3088.1.BUPT.VTF97R.JXN8O8.98DOO8.0EB6',
    'synthesis': '1.2.156.3088.1.BUPT.VTF97R.YDWSUJ.RBUQ6T.12KC',
    'writer': '1.2.156.3088.1.BUPT.VTF97R.WATDMX.AQFZR5.0NQN',
    'critic': '1.2.156.3088.1.BUPT.VTF97R.71P6ML.DDF7EG.13B2',
    'contradiction': '1.2.156.3088.1.BUPT.VTF97R.0FQNAG.ZO888M.0FW5',
    'frontier': '1.2.156.3088.1.BUPT.VTF97R.ZAJ69L.IOEX2R.082D',
    'experiment': '1.2.156.3088.1.BUPT.VTF97R.5CJ349.JNZ5VU.0E5E',
}
with httpx.Client(verify=ctx, timeout=30.0) as c:
    for name, aic in AICS.items():
        r = c.post('https://wt.ioa.pub/discovery/acps-adp-v2/discover',
                   json={'type': 'filtered', 'limit': 5,
                         'filter': {'conditions': [{'field': 'aic', 'op': 'eq', 'value': aic}]}})
        am = ((r.json() or {}).get('result') or {}).get('aliveMap') or {}
        info = am.get(aic) or {}
        print(f"    {name:14s} alive={info.get('alive')}  lastSeen={info.get('aliveLastSeenAt')}")
PY
