#!/usr/bin/env python3
"""事件库清理工具（P1：`aip_task_events` 二次方增长）。

## 问题（2026-10-11 审查 P1，已核实）

`aip_task_events` 单库 **480 MB / 10017 行**，平均每个事件 ~50 KB，最大单任务
739 条事件 = 108 MB。机制：**每次轮询都落一条含全量历史的快照**
（`to_task_result(include_history=True)`，在 wit 里，不在我们代码），
于是单任务体积随轮询线性增长、总量随轮询次数二次方增长。

**归因（实测）**：99.98% 来自**出站**（我方 leader 轮询伙伴）——outbound 9853 条 / 479.8 MB，
inbound 仅 161 条 / 0.2 MB。不是叮当的轮询造成的。

## 本工具做什么

按"保留最近 N 天 + 每任务最多保留 M 条"裁剪事件行，然后 `VACUUM` 回收空间。
**默认 dry-run**，只打印将删除多少；加 `--apply` 才真正执行。

> 注意：这是**删除审计数据**的操作。默认保留窗口设为 7 天，可按需调整；
> 根因在 wit 侧，若要根治需向上游反馈 `include_history` 的默认值。

用法：
    python prune_task_events.py --db <sqlite3> [--keep-days 7] [--keep-per-task 50] [--apply]
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import time
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="aip-tasks.sqlite3 路径")
    ap.add_argument("--keep-days", type=float, default=7.0,
                    help="保留最近 N 天的事件（默认 7）")
    ap.add_argument("--keep-per-task", type=int, default=50,
                    help="每个任务最多保留最近 M 条事件（默认 50）")
    ap.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run）")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.is_file():
        print(f"  ✘ 不存在：{db}")
        return 2

    before = db.stat().st_size
    con = sqlite3.connect(str(db))
    cur = con.cursor()

    cur.execute("SELECT COUNT(*), SUM(LENGTH(CAST(event_data AS TEXT))) FROM aip_task_events")
    n_all, sz_all = cur.fetchone()
    print(f"  库文件 {before/1024/1024:.1f} MB；事件 {n_all} 行 / {(sz_all or 0)/1024/1024:.1f} MB")

    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - args.keep_days * 86400))

    # 策略：① 超过保留窗口的；② 每任务超出 keep_per_task 的较旧部分
    cur.execute("SELECT COUNT(*) FROM aip_task_events WHERE created_at < ?", (cutoff,))
    n_old = cur.fetchone()[0]
    cur.execute("""
        SELECT COUNT(*) FROM (
            SELECT task_ref, ROW_NUMBER() OVER (PARTITION BY task_ref ORDER BY event_seq DESC) rn
            FROM aip_task_events
        ) WHERE rn > ?
    """, (args.keep_per_task,))
    n_excess = cur.fetchone()[0]
    print(f"  将删除：早于 {cutoff} 的 {n_old} 行；每任务超 {args.keep_per_task} 条的 {n_excess} 行")

    if not args.apply:
        print("  （dry-run，未改动）加 --apply 执行")
        con.close()
        return 0

    # 备份（大库备份会占空间，可用 --no-backup 关闭；这里默认备份）
    bak = db.with_suffix(db.suffix + f".pre-prune-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(db, bak)
    print(f"  已备份 → {bak.name}")

    cur.execute("DELETE FROM aip_task_events WHERE created_at < ?", (cutoff,))
    deleted_old = cur.rowcount
    cur.execute("""
        DELETE FROM aip_task_events WHERE rowid IN (
            SELECT rowid FROM (
                SELECT rowid, ROW_NUMBER() OVER (PARTITION BY task_ref ORDER BY event_seq DESC) rn
                FROM aip_task_events
            ) WHERE rn > ?
        )
    """, (args.keep_per_task,))
    deleted_excess = cur.rowcount
    con.commit()
    print(f"  已删除 {deleted_old + deleted_excess} 行")

    con.execute("VACUUM")
    con.commit()
    con.close()
    after = db.stat().st_size
    print(f"  VACUUM 后库文件 {after/1024/1024:.1f} MB（回收 {(before - after)/1024/1024:.1f} MB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
