#!/usr/bin/env python3
"""统计入站 start 命令的载荷形态：占位符 vs 真实内容，按发送方分组。

要回答的问题：
  1. "DAG 指定的上游产物摘要：/ 叮当已完成协作规划并开始按 DAG 派发。" 这个占位符
     出现频率多高？只出现在叮当发来的任务里，还是我们自己的委派里也有？
  2. 叮当发来的包，除了"用户原话 + 占位符"，还有没有别的字段能拿到节点任务？
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path

CL = Path("/opt/wit/my-project/acps_cluster")
PLACEHOLDER = "叮当已完成协作规划并开始按 DAG 派发"
OURL = "1.2.156.3088.1.BUPT.VTF97R.JXN8O8.98DOO8.0EB6"   # 我们自己的 leader

rows: list[dict] = []
for ws_dir in sorted(CL.glob("ws-*")):
    db = ws_dir / "acps" / "aip-tasks.sqlite3"
    if not db.is_file():
        continue
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        for r in con.execute("""SELECT task_ref, direction, state, remote_aic, created_at,
                                       command_history
                                FROM aip_tasks WHERE direction='inbound'
                                ORDER BY created_at DESC LIMIT 40"""):
            ch = json.loads(r["command_history"] or "[]")
            start = next((c for c in ch if (c.get("command") or "") == "start"), None)
            if not start:
                continue
            items = start.get("dataItems") or []
            text = "".join(str(i.get("text") or "") for i in items if isinstance(i, dict))
            rows.append({
                "ws": ws_dir.name.removeprefix("ws-"),
                "task": r["task_ref"],
                "state": r["state"],
                "sender": str(start.get("senderId") or ""),
                "keys": sorted(start.keys()),
                "len": len(text),
                "placeholder": PLACEHOLDER in text,
                "text": text,
            })
    finally:
        con.close()

print(f"  共检查入站 start 命令 {len(rows)} 条\n")

by_sender = Counter()
ph_by_sender = Counter()
for r in rows:
    who = "我们自己(leader)" if r["sender"] == OURL else ("叮当" if "EZTK03" in r["sender"] else "其他")
    by_sender[who] += 1
    if r["placeholder"]:
        ph_by_sender[who] += 1

print("  按发送方统计（含占位符的比例）：")
for who, n in by_sender.most_common():
    ph = ph_by_sender[who]
    print(f"    {who:16s} {n:3d} 条，其中带占位符 {ph:3d} 条 ({ph/n*100:.0f}%)")

print("\n  叮当发来的包：payload 长度分布")
dj = [r for r in rows if "EZTK03" in r["sender"]]
if dj:
    for r in sorted(dj, key=lambda x: x["len"])[:6]:
        print(f"    {r['ws']:12s} {r['task']}  {r['len']:5d} 字符  state={r['state']}")
    print(f"    … 最长 {max(x['len'] for x in dj)} 字符")
    print(f"    占位符出现率 {sum(1 for x in dj if x['placeholder'])}/{len(dj)}")
    print(f"    start 命令字段（全体一致？）: {dj[0]['keys']}")
    extra = set()
    for x in dj:
        extra |= set(x["keys"])
    print(f"    出现过的所有字段: {sorted(extra)}")

print("\n  对照：我们自己委派的包（应含完整指令）")
ours = [r for r in rows if r["sender"] == OURL]
for r in sorted(ours, key=lambda x: -x["len"])[:3]:
    print(f"    {r['ws']:12s} {r['task']}  {r['len']:5d} 字符  占位符={r['placeholder']}")
