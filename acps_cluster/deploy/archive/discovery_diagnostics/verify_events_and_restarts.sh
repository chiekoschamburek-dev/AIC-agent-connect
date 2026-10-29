#!/bin/sh
# 重启次数（审计被抹频次）+ 事件库分布（验证二次方增长与归因）
CL=/opt/wit/my-project/acps_cluster

echo "=== 服务重启次数（NRestarts）==="
for n in scout supervisor critic writer synthesis experiment contradiction frontier; do
  printf '  %-14s %s\n' "$n" "$(systemctl show -p NRestarts --value wit-yanshu@$n.service 2>/dev/null)"
done
printf '  %-14s %s\n' "amp-sender" "$(systemctl show -p NRestarts --value wit-amp-sender.service 2>/dev/null)"
printf '  %-14s %s\n' "amp-mirror" "$(systemctl show -p NRestarts --value wit-amp-mirror.service 2>/dev/null)"
printf '  manager 启动: %s\n' "$(uptime -s)"

echo
echo "=== aip_task_events 分布（哪些任务贡献了体积）==="
/opt/wit/venv/bin/python - <<'PY'
import sqlite3
from pathlib import Path
p = Path('/opt/wit/my-project/acps_cluster/ws-supervisor/acps/aip-tasks.sqlite3')
con = sqlite3.connect(f'file:{p}?mode=ro', uri=True)
cur = con.cursor()
cur.execute("SELECT COUNT(*), SUM(LENGTH(CAST(payload AS TEXT))) FROM aip_task_events")
n, total = cur.fetchone()
print(f"  事件总数 {n}，payload 合计 {total/1024/1024:.1f} MB")
cur.execute("""SELECT task_id, COUNT(*) AS c, SUM(LENGTH(CAST(payload AS TEXT))) AS sz
               FROM aip_task_events GROUP BY task_id ORDER BY sz DESC LIMIT 6""")
rows = cur.fetchall()
print(f"  {'task_id':40s} {'事件数':>7s} {'体积MB':>8s}")
for tid, c, sz in rows:
    print(f"  {str(tid)[:40]:40s} {c:7d} {(sz or 0)/1024/1024:8.1f}")
print()
print("  说明：单任务事件数越多、体积越大 → 与「每次轮询都落一条含全量历史的快照」一致")
con.close()
PY

echo
echo "=== 出站 vs 入站 事件占比（核对归因）==="
/opt/wit/venv/bin/python - <<'PY'
import sqlite3
from pathlib import Path
for w in ('supervisor', 'scout'):
    p = Path(f'/opt/wit/my-project/acps_cluster/ws-{w}/acps/aip-tasks.sqlite3')
    if not p.is_file():
        continue
    con = sqlite3.connect(f'file:{p}?mode=ro', uri=True)
    cur = con.cursor()
    try:
        cur.execute("""SELECT t.direction, COUNT(*) AS events,
                              SUM(LENGTH(CAST(e.payload AS TEXT))) AS sz
                       FROM aip_task_events e JOIN aip_tasks t ON t.task_id = e.task_id
                       GROUP BY t.direction""")
        print(f"  --- ws-{w}")
        for d, c, sz in cur.fetchall():
            print(f"      {str(d):9s} 事件 {c:6d}  体积 {(sz or 0)/1024/1024:8.1f} MB")
    except Exception as exc:
        print(f"  ws-{w}: 归属查询失败（表结构不同）{exc}")
    con.close()
PY
