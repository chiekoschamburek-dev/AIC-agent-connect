"""独立运行入口:``python -m guard_api --db /path/to/aip-tasks.sqlite3``。

与挂载模式(``guard_api.mount``)的差异:这里是前台独立进程,uvicorn 可以
接管 SIGINT/SIGTERM 做 Ctrl+C 优雅退出。
"""

from __future__ import annotations

import argparse
import os

import uvicorn

from guard_api import DEFAULT_HOST, DEFAULT_PORT, report
from guard_api.app import create_app


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m guard_api",
        description="ACPS Event Channel Guard HTTP API(只读旁路,不写任务库)",
    )
    parser.add_argument("--db", default=os.getenv("ACPS_GUARD_DB"),
                        help="任务库路径(SQLite,只读打开);缺省读 ACPS_GUARD_DB")
    parser.add_argument("--host", default=os.getenv("ACPS_GUARD_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int,
                        default=int(os.getenv("ACPS_GUARD_PORT", str(DEFAULT_PORT))))
    parser.add_argument("--limit", type=int,
                        default=int(os.getenv("ACPS_GUARD_LIMIT", "1000")),
                        help="每次扫描的事件/任务窗口上限(默认 1000)")
    parser.add_argument("--min-level", choices=("LOW", "MEDIUM", "HIGH"),
                        default=os.getenv("ACPS_GUARD_MIN_LEVEL", "MEDIUM"),
                        help="低于该等级不产生告警(明细仍返回)")
    parser.add_argument("--session-gap", type=float,
                        default=float(os.getenv("ACPS_GUARD_SESSION_GAP", "3600")),
                        help="peer timing 会话切分间隔(秒,默认 3600)")
    parser.add_argument("--log-level", default=os.getenv("ACPS_GUARD_LOG_LEVEL", "info"),
                        choices=("critical", "error", "warning", "info", "debug", "trace"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not args.db:
        _build_parser().error("--db is required (or set ACPS_GUARD_DB)")
    app = create_app(
        args.db,
        limit=args.limit,
        min_level=args.min_level,
        session_gap=args.session_gap,
        token=os.getenv("ACPS_GUARD_TOKEN"),
    )
    print(
        f"[guard-api] {args.host}:{args.port} db={args.db} "
        f"min_level={args.min_level} (advisory-only, mode=ro)"
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
