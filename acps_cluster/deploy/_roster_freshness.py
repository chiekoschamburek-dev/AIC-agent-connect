"""只读核对：supervisor 名册里"可调度 agent"是否都是最新的三方对齐。

三方 = ① 名册解析值（ws-<key>/acps/acs.final.json）
       ② 实际在跑的身份（ws-<key>/logs/amp_heartbeat_<AIC>.jsonl 的文件名）
       ③ 平台在册记录（ATR：version / active / 技能数）
"""
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CL = Path('/opt/wit/my-project/acps_cluster')
sys.path.insert(0, str(CL))
import cluster_roster as cr  # noqa: E402

ATR = 'https://wt.ioa.pub/registry-server/acps-atr-v2/acs/'
now = datetime.now(timezone.utc)


def live_aic(key):
    hb = sorted((CL / f'ws-{key}' / 'logs').glob('amp_heartbeat_*.jsonl'))
    if not hb:
        return None, None
    f = hb[-1]
    age = (now - datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)).total_seconds()
    return f.stem.replace('amp_heartbeat_', ''), age


def atr(aic):
    try:
        d = json.loads(urllib.request.urlopen(ATR + aic, timeout=20).read())
        return {'version': d.get('version'), 'active': d.get('active'),
                'skills': len(d.get('skills') or [])}
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode('utf-8', 'replace')[:120]
        except Exception:
            body = ''
        return {'err': f'HTTP {exc.code} {body}'}
    except Exception as exc:
        return {'err': type(exc).__name__}


print(f'{"成员":14s} {"名册aic尾":16s} {"在跑aic尾":16s} {"一致":4s} {"心跳":>6s}  平台记录')
print('-' * 92)
for m in cr.load_roster():
    if not m.available or not m.aic:
        print(f'{m.key:14s} {"-":16s} {"-":16s} —      -      （不可调度/无身份）')
        continue
    la, age = live_aic(m.key)
    # designer 是外部成员：心跳在队友工作区，不按 ws-<key> 找
    if la is None and m.key == 'designer':
        try:
            gjw = sorted(Path('/home/gjw/research-designer/.wit').glob('*/logs/amp_heartbeat_*.jsonl'))
        except (OSError, PermissionError):
            gjw = []
        if gjw:
            la = gjw[-1].stem.replace('amp_heartbeat_', '')
            age = (now - datetime.fromtimestamp(gjw[-1].stat().st_mtime, tz=timezone.utc)).total_seconds()
    same = '✔' if (la and la == m.aic) else ('?' if la is None else '✘')
    ver = atr(m.aic)
    ver_s = (f"v{ver.get('version')} active={ver.get('active')} skills={ver.get('skills')}"
             if 'err' not in ver else f"ATR {ver['err']}")
    print(f'{m.key:14s} ...{m.aic[-16:]:16s} ...{(la or "-")[-16:]:16s} {same:4s} '
          f'{(str(int(age)) + "s") if age is not None else "-":>6s}  {ver_s}')
    if same == '✘':
        print(f'{"":14s}  ⚠ 名册与在跑身份不一致 → supervisor 会派给 {m.aic[-20:]}（可能已停用）')
print()
print('=== 说明 ===')
print('  · 名册值来自 ws-<key>/acps/acs.final.json（supervisor 的 plan_gate 用新鲜名册；')
print('    但它的**提示词**里的名册是上次启动时渲染的文本）')
print('  · “一致=✔”意味着派单目标 = 正在服务的身份')
print('  · gpu 是 dev-CA 身份（平台不认，ATR 会报无效）→ 不走平台属正常')
