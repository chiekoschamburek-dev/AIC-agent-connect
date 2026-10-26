#!/bin/sh
# 复核：arxiv-partner 退役前后 vs 现在在跑的 scout/supervisor（只读）
CL=/opt/wit/my-project/acps_cluster
echo "=== 1) 当前我们的在线状态（aliveMap / 按 AIC 查）==="
cd "$CL"
sudo -u wit /opt/wit/venv/bin/python - <<'PY'
import json, ssl
from pathlib import Path
import httpx
CL = Path('/opt/wit/my-project/acps_cluster')
acps = CL / 'ws-scout' / 'acps'
ctx = ssl.create_default_context()
ctx.load_verify_locations(cafile=str(acps / 'trust-bundle.pem'))
ctx.load_cert_chain(certfile=str(acps / 'client' / 'certificate.pem'),
                    keyfile=str(acps / 'client' / 'private-key.pem'))
AICS = {
    'Scout': '1.2.156.3088.1.BUPT.VTF97R.GMT5S6.4SZM68.0LC5',
    'Supervisor': '1.2.156.3088.1.BUPT.VTF97R.JXN8O8.98DOO8.0EB6',
    'Synthesis': '1.2.156.3088.1.BUPT.VTF97R.YDWSUJ.RBUQ6T.12KC',
    'Writer': '1.2.156.3088.1.BUPT.VTF97R.WATDMX.AQFZR5.0NQN',
    'Critic': '1.2.156.3088.1.BUPT.VTF97R.71P6ML.DDF7EG.13B2',
    'Contradiction': '1.2.156.3088.1.BUPT.VTF97R.0FQNAG.ZO888M.0FW5',
    'Frontier': '1.2.156.3088.1.BUPT.VTF97R.ZAJ69L.IOEX2R.082D',
    'Experiment': '1.2.156.3088.1.BUPT.VTF97R.5CJ349.JNZ5VU.0E5E',
    'arXiv(退役)': '1.2.156.3088.1.BUPT.VTF97R.Q1EZOG.SSJTOF.0T4Q',
}
D = 'https://wt.ioa.pub/discovery/acps-adp-v2/discover'
with httpx.Client(verify=ctx, timeout=40.0) as c:
    r = c.post(D, json={'type': 'recent', 'limit': 50})
    am = ((r.json() or {}).get('result') or {}).get('aliveMap') or {}
    print(f"  recent 取数 aliveMap 共 {len(am)} 条\n")
    print(f"  {'agent':14s} {'平台认为在线':12s} {'aliveLastSeenAt':32s} 按AIC查")
    for name, aic in AICS.items():
        info = am.get(aic)
        r2 = c.post(D, json={'type': 'filtered', 'limit': 5,
                             'filter': {'conditions': [{'field': 'aic', 'op': 'eq', 'value': aic}]}})
        n = len((((r2.json() or {}).get('result') or {}).get('acsMap') or {}))
        alive = '✔ 在线' if info else '✘ 不在线'
        seen = (info or {}).get('aliveLastSeenAt', '—') if info else '—'
        print(f"  {name:14s} {alive:12s} {str(seen):32s} {'✔ 查得到' if n else '✘ 0 条'}")
PY

echo
echo "=== 2) 发送器仍在投递？（位点是否继续推进）==="
systemctl is-active wit-amp-mirror.service wit-amp-sender.service
/opt/wit/venv/bin/python - <<'PY'
import json
from pathlib import Path
p = Path('/var/log/acps-amp/.sender-offsets.json')
d = json.loads(p.read_text()) if p.is_file() else {}
print(f"  位点合计 {sum(d.values())} 字节 / {len(d)} 个文件")
PY

echo
echo "=== 3) 平台 alive 总盘 ==="
curl -s --max-time 12 https://wt.ioa.pub/discovery/admin/alive-sync/status | head -c 150
echo
