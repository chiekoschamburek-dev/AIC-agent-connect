"""台账日报（只读）：逐工作区列出非终态任务与年龄（2026-10-15 审查 A）。

政策（默认）：`inbound + awaiting-completion` = 产物已交付、待调用方关单，**按设计驻留、
不自动 expire**；超龄（默认 >14 天）打 ⚠ 供人工处置。本脚本**只读不写**：若将来选择
"N 天 → expired" 闸门，须另行评审后加入写路径（当前明确不做）。

建议由 `wit-ledger-report.timer`（每日 09:00）调用；手动运行：
    runuser -u wit -- /opt/wit/venv/bin/python deploy/task_ledger_report.py [--warn-days 14]
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

CL = Path(__file__).resolve().parents[1]
_TERMINAL = ("completed", "failed", "rejected", "canceled", "cancelled", "expired")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--warn-days", type=float, default=14.0)
    args = ap.parse_args()
    now = datetime.now(timezone.utc)
    total = 0
    warned = 0
    print(f"[ledger-report] {now.isoformat()}  非终态台账（⚠ 超 {args.warn_days:g} 天）")
    for db in sorted(CL.glob("ws-*/acps/aip-tasks.sqlite3")):
        ws = db.parts[-3]
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            marks = ",".join("?" * len(_TERMINAL))
            rows = con.execute(
                "SELECT task_ref, direction, state, coalesce(created_at, '') FROM aip_tasks"
                f" WHERE lower(coalesce(state, '')) NOT IN ({marks})",
                _TERMINAL,
            ).fetchall()
            con.close()
        except Exception as exc:  # noqa: BLE001
            print(f"  {ws:14s} 读取失败: {type(exc).__name__}: {exc}")
            continue
        for ref, direction, state, created in rows:
            try:
                age = (now - datetime.fromisoformat(str(created).replace("Z", "+00:00")))
                age_days = age.total_seconds() / 86400
            except Exception:  # noqa: BLE001
                age_days = -1.0
            flag = ""
            if 0 <= age_days and age_days > args.warn_days:
                flag = "  ⚠ 超龄"
                warned += 1
            total += 1
            print(f"  {ws:14s} {str(direction):8s} {str(state):22s} {age_days:6.1f}d  {ref}{flag}")
    print(f"[ledger-report] 合计非终态 {total} 条（超龄 {warned} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
