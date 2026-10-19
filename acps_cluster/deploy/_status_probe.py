"""只读巡检：集群各智能体的服务/身份/心跳/准入/在途 一览（不修改任何东西）。"""
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

CL = Path('/opt/wit/my-project/acps_cluster')
WS = ['scout', 'synthesis', 'writer', 'critic', 'contradiction', 'frontier', 'experiment', 'supervisor']
TERMINAL = ('completed', 'failed', 'rejected', 'canceled', 'cancelled', 'expired')


def sh(*args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception as exc:
        return f'<{type(exc).__name__}>'


def pep_env(pid):
    try:
        raw = Path(f'/proc/{pid}/environ').read_bytes().decode('utf-8', 'replace')
    except Exception:
        return None
    d = dict(kv.split('=', 1) for kv in raw.split('\0') if '=' in kv)
    return d.get('ACPS_PEP_ENABLED'), d.get('ACPS_PEP_LEADER_AICS', '')


def inflight(ws):
    db = CL / f'ws-{ws}' / 'acps' / 'aip-tasks.sqlite3'
    if not db.is_file():
        return None
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    try:
        marks = ','.join('?' * len(TERMINAL))
        rows = con.execute(
            f"SELECT direction, coalesce(state,'<null>'), count(*) FROM aip_tasks"
            f" WHERE lower(coalesce(state,'')) NOT IN ({marks}) GROUP BY 1,2", TERMINAL).fetchall()
    finally:
        con.close()
    return rows


print('=' * 78)
print('① 服务 / 身份 / 心跳 / 准入')
print('=' * 78)
now = datetime.now(timezone.utc)
for ws in WS:
    unit = f'wit-yanshu@{ws}'
    st = sh('systemctl', 'is-active', unit) or '?'
    since = sh('systemctl', 'show', '-p', 'ActiveEnterTimestamp', '--value', unit)
    pid = sh('systemctl', 'show', '-p', 'MainPID', '--value', unit)
    pep = pep_env(pid) if pid and pid != '0' else None
    hb = sorted((CL / f'ws-{ws}' / 'logs').glob('amp_heartbeat_*.jsonl'))
    hb_age = '-'
    aic = '-'
    if hb:
        f = hb[-1]
        aic = f.stem.replace('amp_heartbeat_', '')
        mt = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        hb_age = f'{(now - mt).total_seconds():.0f}s'
    enr = '-'
    try:
        e = json.loads((CL / f'ws-{ws}' / 'acps' / 'enrollment.json').read_text(encoding='utf-8'))
        enr = f"{e.get('status')}{'/' + str(e.get('failure'))[:24] if e.get('failure') else ''}"
    except Exception:
        pass
    pep_s = ('PEP=on leaders=%d' % len(pep[1].split(','))) if pep and pep[0] == '1' else ('PEP=off' if pep else 'PEP=?')
    print(f'  {ws:14s} {st:10s} 起于 {since[:16]:16s} pid={pid:>7s} {pep_s:20s} 心跳 {hb_age:>6s} | {enr}')
    print(f'                 aic={aic}')

print()
print('=' * 78)
print('② 端口监听')
print('=' * 78)
ports = sh('ss', '-tln')
listen = sorted({p for p in [l.split()[3].split(':')[-1] for l in ports.splitlines()[1:] if l.split()] if p.isdigit()})
print('  ', ' '.join(listen))

print()
print('=' * 78)
print('③ 在途任务（非终态）')
print('=' * 78)
for ws in WS:
    rows = inflight(ws)
    if rows is None:
        print(f'  {ws:14s} （无库）')
    elif not rows:
        print(f'  {ws:14s} 干净')
    else:
        parts = ', '.join(f'{d}/{s}×{n}' for d, s, n in rows)
        print(f'  {ws:14s} ⚠ {parts}')

print()
print('=' * 78)
print('④ 名册（服务器侧解析）')
print('=' * 78)
sys.path.insert(0, str(CL))
import cluster_roster as cr  # noqa: E402
for m in cr.load_roster():
    print(f'  {m.key:14s} {str(m.kind):7s} avail={str(m.available):5s} aic={(m.aic or "-")[-22:]:22s} '
          f'caps={len(m.capabilities):2d} ep={m.endpoint or "-"}')
