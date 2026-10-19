#!/bin/sh
# 核实需要现场数据的主张：P0-5 运行态、P1 审计路径、P1 事件库体积、P1 配置漂移
CL=/opt/wit/my-project/acps_cluster

echo "=== P0-5：sender 运行态（NRestarts / 投递累计）==="
systemctl show -p NRestarts --value wit-amp-sender.service | sed 's/^/  NRestarts=/'
systemctl show -p ActiveEnterTimestamp --value wit-amp-sender.service | sed 's/^/  启动于 /'
journalctl -u wit-amp-sender.service --no-pager 2>/dev/null | grep -c '本轮投递' | sed 's/^/  投递轮次日志行数: /'
journalctl -u wit-amp-sender.service --no-pager 2>/dev/null | tail -1 | sed 's/^/  最近一行: /'

echo
echo "=== P1：PEP 审计路径（是否全部指向 ws-experiment）==="
grep -n 'ACPS_PEP_AUDIT_PATH' "$CL/deploy/.env.production" | sed 's/^/  /'
echo "  --- 各工作区是否有独立审计文件"
for w in scout supervisor critic writer synthesis experiment contradiction frontier; do
  n=$(ls "$CL/ws-$w/logs/"pep-audit.jsonl 2>/dev/null | wc -l)
  printf '      ws-%-14s pep-audit 文件数=%s\n' "$w" "$n"
done
echo "  --- 现存审计内容"
f="$CL/ws-experiment/logs/pep-audit.jsonl"
if [ -f "$f" ]; then
  echo "      行数 $(wc -l < "$f")  大小 $(stat -c %s "$f") B  最早条目："
  head -1 "$f" | cut -c1-160 | sed 's/^/        /'
  echo "      最后条目："
  tail -1 "$f" | cut -c1-160 | sed 's/^/        /'
fi

echo
echo "=== P1：experiment 的 ExecStartPre 是否会删审计文件 ==="
systemctl cat wit-yanshu@experiment.service 2>/dev/null | grep -E 'ExecStartPre|ExecStart=' | sed 's/^/  /'

echo
echo "=== P1：aip_task_events 体积与增长 ==="
for w in supervisor scout; do
  db="$CL/ws-$w/acps/aip-tasks.sqlite3"
  [ -f "$db" ] || continue
  echo "  --- ws-$w"
  ls -la "$db" | awk '{print "      文件 "$5" 字节"}'
  /opt/wit/venv/bin/python - <<PY
import sqlite3
con = sqlite3.connect('file:$db?mode=ro', uri=True)
cur = con.cursor()
try:
    cur.execute("SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM aip_task_events")
    n, mn, mx = cur.fetchone()
    print(f"      aip_task_events: {n} 行，{str(mn)[:10]} → {str(mx)[:10]}")
except Exception as exc:
    print(f"      读取失败 {exc}")
con.close()
PY
done

echo
echo "=== P1：配置漂移（服务器 /opt/wit/my-project/.env 是否还是 LongCat）==="
if [ -f /opt/wit/my-project/.env ]; then
  grep -E '^OPENAI_(MODEL|BASE_URL)=' /opt/wit/my-project/.env | sed 's/^/  服务器顶层 .env: /'
else
  echo "  服务器顶层 .env 不存在"
fi
grep -E '^OPENAI_(MODEL|BASE_URL)=' "$CL/deploy/.env.production" | sed 's/^/  实际生效 .env.production: /'

echo
echo "=== P1：ACPS_PEP_LEADER_AICS 指向哪个 AIC ==="
grep -n 'ACPS_PEP_LEADER_AICS' "$CL/deploy/.env.production" | sed 's/^/  /'
grep -rn 'ACPS_PEP_LEADER_AICS' "$CL/../env.cluster.append" 2>/dev/null | sed 's/^/  env.cluster.append: /' || echo "  （本地 env.cluster.append 不在服务器）"
