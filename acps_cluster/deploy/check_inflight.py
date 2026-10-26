"""重启前的在途任务检查（默认只读；可选按年龄门限兜底收敛）。

为什么：重启会打死在途任务——2026-10-10 实测两例：
  · 重启 supervisor 让一条 inbound 任务以 `PartnerRestarted` 失败；
  · scout 在 01:34 被重启，让一条跑了 30 分钟的委派报废。
重启任何常驻 agent **之前**先跑本脚本。

用法：
    python acps_cluster/deploy/check_inflight.py                  # 只查 supervisor（只读）
    python acps_cluster/deploy/check_inflight.py --all            # 查 8 个常驻工作区（只读）
    python acps_cluster/deploy/check_inflight.py --all --clean 60 # 兜底：把「60 分钟前就不再
        # 更新」的非终态任务收敛为 canceled（local_error 记 previousState，可回溯）。
        # 年龄门限最小 10 分钟：新鲜任务一律不碰，所以不可能吃掉正在跑的东西。

退出码：仍有非终态任务（含刚被清理前的残留之外的"新鲜"在途）= 1；干净 = 0。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

CLUSTER = Path(__file__).resolve().parents[1]
TERMINAL = ("completed", "failed", "rejected", "canceled", "cancelled", "expired")
DEFAULT_WORKSPACES = ("supervisor",)
ALL_WORKSPACES = (
    "scout",
    "synthesis",
    "writer",
    "critic",
    "contradiction",
    "frontier",
    "experiment",
    "supervisor",
)


def _db_path(workspace: str) -> Path:
    return CLUSTER / f"ws-{workspace}" / "acps" / "aip-tasks.sqlite3"


def scan(workspace: str) -> list[tuple[str, str, str, str]] | None:
    """返回该工作区里未到终态的任务；库不存在返回 None。"""
    db = _db_path(workspace)
    if not db.is_file():
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        marks = ",".join("?" * len(TERMINAL))
        return con.execute(
            "SELECT task_ref, direction, coalesce(state, '<null>'), coalesce(updated_at, '')"
            f" FROM aip_tasks WHERE lower(coalesce(state, '')) NOT IN ({marks})"
            " ORDER BY updated_at",
            TERMINAL,
        ).fetchall()
    finally:
        con.close()


def _parse_utc(value: str) -> datetime | None:
    try:
        ts = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def clean_workspace(workspace: str, older_minutes: int) -> list[str]:
    """把「older_minutes 分钟前就不再更新」的非终态任务收敛为 canceled。

    日期解析不出来的条目**不动**（宁漏不误伤）；留痕写进 local_error。
    """
    db = _db_path(workspace)
    if not db.is_file():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=older_minutes)
    con = sqlite3.connect(db, timeout=10)
    try:
        marks = ",".join("?" * len(TERMINAL))
        rows = con.execute(
            "SELECT task_ref, direction, coalesce(state, ''), coalesce(updated_at, '')"
            f" FROM aip_tasks WHERE lower(coalesce(state, '')) NOT IN ({marks})",
            TERMINAL,
        ).fetchall()
        eligible = [
            (ref, direction, state)
            for ref, direction, state, updated in rows
            if (ts := _parse_utc(updated)) is not None and ts < cutoff
        ]
        if not eligible:
            return []
        now_iso = datetime.now(timezone.utc).isoformat()
        con.executemany(
            "UPDATE aip_tasks SET state='canceled', updated_at=?, local_error=?"
            " WHERE task_ref=?",
            [
                (
                    now_iso,
                    json.dumps(
                        {
                            "type": "StaleAfterCleanup",
                            "message": f"超过 {older_minutes} 分钟未更新且非终态，运维按要求收敛",
                            "direction": direction,
                            "previousState": state or None,
                        },
                        ensure_ascii=False,
                    ),
                    ref,
                )
                for ref, direction, state in eligible
            ],
        )
        con.commit()
        return [ref for ref, _, _ in eligible]
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="重启前的在途任务检查（默认只读）")
    ap.add_argument("--all", action="store_true", help="检查全部 8 个常驻工作区")
    ap.add_argument(
        "--clean",
        type=int,
        metavar="MINUTES",
        default=0,
        help="兜底收敛：把 MINUTES 分钟前就不再更新的非终态任务标记为 canceled（>=10）",
    )
    args = ap.parse_args(argv)

    if args.clean and args.clean < 10:
        print("--clean 的年龄门限至少 10 分钟（防止误伤在跑任务）", file=sys.stderr)
        return 2

    workspaces = ALL_WORKSPACES if args.all else DEFAULT_WORKSPACES
    stale_total = 0
    for ws in workspaces:
        try:
            if args.clean:
                cleaned = clean_workspace(ws, args.clean)
                if cleaned:
                    print(f"  {ws:14s} 已收敛 {len(cleaned)} 条陈旧任务（> {args.clean} 分钟未更新）")
            rows = scan(ws)
        except sqlite3.Error as exc:
            print(f"  {ws:14s} 读取失败（跳过）: {type(exc).__name__}: {exc}")
            continue
        if rows is None:
            continue
        if not rows:
            print(f"  {ws:14s} 干净（无在途任务）")
            continue
        total = len(rows)
        stale_total += total
        print(f"  {ws:14s} ⚠ 在途 {total} 条：")
        for ref, direction, state, updated in rows:
            print(f"      {ref}  {direction:8s} {state:22s} updated={updated[:19]}")
    print()
    if stale_total:
        print(
            f"结论：仍有 {stale_total} 条未到终态 —— 先等它们结束，或显式取消后再重启；"
            f"\n      若要兜底，可用 --clean {max(args.clean, 30)}（只收敛超过该时长未更新的）。"
        )
        return 1
    print("结论：无在途任务，可以重启。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
