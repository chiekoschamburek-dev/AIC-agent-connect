#!/usr/bin/env python3
"""guard → supervisor 适配层：把只读通信风险检测器接进治理层。

定位（与 PEP 互补，二者不替代）
--------------------------------
- **PEP 管"是否准入"（事前）**：任务级点对点委托 + 周期级域授权/对账，
  回答"这一次调用有没有资格"。语义在本适配层中**完全不动**：不签发、不撤销、
  不改令牌、不改域策略、不改 PEP 判定。
- **guard 管"通信过程是否异常"（事中/事后）**：队友的
  ``ACPS Event Channel Guard`` 以 SQLite ``mode=ro`` 只读旁路读取
  ``aip_task_events`` / ``aip_tasks``，从 Storage / Timing / Behavioral 三维
  回答"即使身份与权限都合法，这次通信的行为是否异常"。

本模块只做四件事：
1. 定时调用 guard 的检测入口（``load_sqlite`` / ``analyze_events`` /
   ``load_peer_tasks_sqlite`` / ``analyze_peer_timing``）；
2. 把结果汇总成一份可解释、可留档的风险报告（JSON）；
3. 输出结构化日志（``yanshu.guard`` logger）；
4. 发布 viz 告警（``cluster-viz/public/scenarios/guard-alerts.json``，
   并可在 supervisor 运行期间通过 ``attach_recorder()`` 写进本次 run 的事件流）。

处置边界：本模块**只出建议、不自动处置**。报告里的 ``suggested_action`` 是给人工
复核的提示；是否限速、是否经 PEP 撤销授权/冻结域，必须由人决定并走既有 PEP 流程。
这与 guard README 的"旁路观察 → 风险告警 → 人工复核 → Supervisor 展示 → 必要时再
联动 PEP"演进路径一致。

用法
----
一次性检测（比赛演示/CI 用）::

    python -m acps_cluster.guard_adapter --db /path/to/aip-tasks.sqlite3 --json

周期运行（systemd 常驻）::

    python -m acps_cluster.guard_adapter --db /path/to/aip-tasks.sqlite3 \\
        --interval 300 --report-dir acps_cluster/ws-supervisor/guard

只读保证：本模块对任务库零写入（guard 自身以 ``mode=ro`` 打开），报告与告警只写
``--report-dir`` / ``--viz-alerts`` 指向的本地文件。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from acps_cluster.guard import covert_event_guard as guard

LOG = logging.getLogger("yanshu.guard")

# 与 guard 自身的分级阈值保持一致（单任务：30/60；peer timing：20/40）。
_LEVEL_RANK = {"LOW": 0, "INSUFFICIENT_DATA": 0, "MEDIUM": 1, "HIGH": 2}
_ACTION = {
    "LOW": "observe",
    "INSUFFICIENT_DATA": "observe",
    "MEDIUM": "人工复核；适配层不自动处置",
    "HIGH": "人工复核并评估是否经既有 PEP 流程限速/撤销授权；适配层不自动处置",
}

DEFAULT_REPORT_DIR = Path(__file__).resolve().parent / "ws-supervisor" / "guard"
DEFAULT_VIZ_ALERTS = (
    Path(__file__).resolve().parents[1]
    / "cluster-viz"
    / "public"
    / "scenarios"
    / "guard-alerts.json"
)


@dataclass(frozen=True)
class Alert:
    """一条需要人工关注的风险告警（只含可解释字段，不含载荷原文）。"""

    channel: str          # task | peer
    level: str            # MEDIUM | HIGH
    score: int
    subject: str          # task_ref 或 direction:local->remote
    rules: list[str] = field(default_factory=list)
    detail: str = ""
    suggested_action: str = ""


@dataclass(frozen=True)
class GuardReport:
    """一次检测的完整留档。"""

    generated_at: str
    db: str
    window_limit: int
    task_count: int
    peer_count: int
    alerts: list[Alert] = field(default_factory=list)
    tasks: list[dict[str, Any]] = field(default_factory=list)
    peers: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # 明确写死：本报告不构成攻击定论，也不是自动处置指令。
    disposition: str = "advisory-only"
    pep_semantics: str = "untouched"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        # highest_level 是派生量（不落成字段，避免和 alerts 不一致），但报告消费方
        # （运维脚本、viz 快照）需要它，故在序列化时补上。
        payload["highest_level"] = self.highest_level
        return payload

    @property
    def highest_level(self) -> str:
        if not self.alerts:
            return "LOW"
        return max((alert.level for alert in self.alerts), key=lambda lv: _LEVEL_RANK.get(lv, 0))


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _alert_from_task(report: Any, min_level: str) -> Alert | None:
    level = str(report.level)
    if _LEVEL_RANK.get(level, 0) < _LEVEL_RANK.get(min_level, 1):
        return None
    findings = list(getattr(report, "findings", []) or [])
    return Alert(
        channel="task",
        level=level,
        score=int(report.score),
        subject=str(report.task_ref),
        rules=[str(item.rule) for item in findings],
        detail="; ".join(f"[{item.channel}] {item.rule} (+{item.score}) {item.detail}" for item in findings),
        suggested_action=_ACTION.get(level, _ACTION["MEDIUM"]),
    )


def _alert_from_peer(report: Any, min_level: str) -> Alert | None:
    level = str(report.level)
    if _LEVEL_RANK.get(level, 0) < _LEVEL_RANK.get(min_level, 1):
        return None
    findings = list(getattr(report, "findings", []) or [])
    subject = f"{report.direction}:{report.local_aic}->{report.remote_aic}"
    detail = "; ".join(f"[{item.channel}] {item.rule} (+{item.score}) {item.detail}" for item in findings)
    observations = list(getattr(report, "operational_observations", []) or [])
    if observations:
        detail = (detail + " | " if detail else "") + "运行观察: " + "; ".join(observations)
    return Alert(
        channel="peer",
        level=level,
        score=int(report.score),
        subject=subject,
        rules=[str(item.rule) for item in findings],
        detail=detail,
        suggested_action=_ACTION.get(level, _ACTION["MEDIUM"]),
    )


def _task_summary(report: Any) -> dict[str, Any]:
    return {
        "task_ref": str(report.task_ref),
        "event_count": int(report.event_count),
        "score": int(report.score),
        "level": str(report.level),
        "findings": [
            {"channel": item.channel, "rule": item.rule, "score": item.score, "detail": item.detail}
            for item in (getattr(report, "findings", []) or [])
        ],
    }


def _peer_summary(report: Any) -> dict[str, Any]:
    return {
        "direction": str(report.direction),
        "local_aic": str(report.local_aic),
        "remote_aic": str(report.remote_aic),
        "task_count": int(report.task_count),
        "session_count": int(report.session_count),
        "analyzable_sessions": int(report.analyzable_sessions),
        "score": int(report.score),
        "level": str(report.level),
        "findings": [
            {"channel": item.channel, "rule": item.rule, "score": item.score, "detail": item.detail}
            for item in (getattr(report, "findings", []) or [])
        ],
        "operational_observations": list(getattr(report, "operational_observations", []) or []),
    }


def scan(db: str | Path, *, limit: int = 1000, min_level: str = "MEDIUM") -> GuardReport:
    """调用 guard 检测入口，返回汇总报告（只读，不写任务库）。

    peer timing 是可选增强：早期/精简任务库可能没有 ``aip_tasks`` 表，此时
    记录 warning 并继续，不让单任务检测整体失败。
    """
    db_path = Path(db).expanduser().resolve()
    if not db_path.is_file():
        raise FileNotFoundError(db_path)

    warnings: list[str] = []
    events = guard.load_sqlite(db_path, limit=limit)
    task_reports = guard.analyze_events(events)

    peers: list[Any] = []
    try:
        peer_tasks = guard.load_peer_tasks_sqlite(db_path, limit=limit)
        peers = guard.analyze_peer_timing(peer_tasks)
    except (ValueError, sqlite3.Error) as exc:
        warnings.append(f"peer timing skipped: {exc}")

    alerts = [alert for alert in (_alert_from_task(item, min_level) for item in task_reports) if alert]
    alerts += [alert for alert in (_alert_from_peer(item, min_level) for item in peers) if alert]

    return GuardReport(
        generated_at=_now(),
        db=str(db_path),
        window_limit=int(limit),
        task_count=len(task_reports),
        peer_count=len(peers),
        alerts=alerts,
        tasks=[_task_summary(item) for item in task_reports],
        peers=[_peer_summary(item) for item in peers],
        warnings=warnings,
    )


def log_report(report: GuardReport) -> None:
    """结构化日志：告警逐条、汇总一行。不含载荷原文。"""
    LOG.info(
        "[guard] scan db=%s tasks=%d peers=%d alerts=%d level=%s",
        report.db, report.task_count, report.peer_count,
        len(report.alerts), report.highest_level,
    )
    for alert in report.alerts:
        LOG.warning(
            "[guard] alert channel=%s level=%s score=%d subject=%s rules=%s",
            alert.channel, alert.level, alert.score, alert.subject, ",".join(alert.rules),
        )
    for warning in report.warnings:
        LOG.warning("[guard] warning %s", warning)


def _atomic_write(target: Path, payload: Mapping[str, Any] | str) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
    tmp = target.with_name(f".{target.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)
    return target


def write_report(report: GuardReport, report_dir: str | Path = DEFAULT_REPORT_DIR) -> Path:
    """落盘一份带时间戳的报告，并刷新 ``latest.json``（两者内容一致）。"""
    directory = Path(report_dir)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    payload = report.to_dict()
    path = _atomic_write(directory / f"guard-report-{stamp}.json", payload)
    _atomic_write(directory / "latest.json", payload)
    return path


def publish_viz_alerts(report: GuardReport,
                       target: str | Path = DEFAULT_VIZ_ALERTS) -> Path | None:
    """把告警发布到 viz 的数据目录（前端/桥接层可直接读取）。

    文件保持"最新一次扫描"的快照，只含可解释字段；``--viz-alerts none`` 可关闭。
    """
    if str(target).strip().lower() in {"none", "off", "-"}:
        return None
    payload = {
        "generated_at": report.generated_at,
        "db": report.db,
        "highest_level": report.highest_level,
        "alert_count": len(report.alerts),
        "disposition": report.disposition,
        "alerts": [asdict(alert) for alert in report.alerts],
    }
    return _atomic_write(Path(target), payload)


def attach_recorder(recorder: Any, report: GuardReport) -> int:
    """把告警写进 supervisor 当前 run 的 viz 事件流（若 recorder 支持）。

    调用方在 supervisor 收尾时显式调用；本函数不修改 PEP 状态机，也不阻断任务。
    返回写入的事件条数。
    """
    if recorder is None or not hasattr(recorder, "guard_alert"):
        return 0
    count = 0
    for alert in report.alerts:
        recorder.guard_alert(
            channel=alert.channel,
            level=alert.level,
            score=alert.score,
            subject=alert.subject,
            rules=list(alert.rules),
            detail=alert.detail,
            suggested_action=alert.suggested_action,
        )
        count += 1
    return count


def run_once(
    db: str | Path,
    *,
    limit: int = 1000,
    min_level: str = "MEDIUM",
    report_dir: str | Path = DEFAULT_REPORT_DIR,
    viz_alerts: str | Path = DEFAULT_VIZ_ALERTS,
) -> tuple[GuardReport, Path | None, Path | None]:
    report = scan(db, limit=limit, min_level=min_level)
    log_report(report)
    report_path = write_report(report, report_dir)
    viz_path = publish_viz_alerts(report, viz_alerts)
    return report, report_path, viz_path


def run_forever(
    db: str | Path,
    *,
    interval: float,
    limit: int = 1000,
    min_level: str = "MEDIUM",
    report_dir: str | Path = DEFAULT_REPORT_DIR,
    viz_alerts: str | Path = DEFAULT_VIZ_ALERTS,
) -> None:
    """周期扫描；单轮异常只记日志，不退出（常驻服务不能被一次坏读拖死）。"""
    LOG.info("[guard] periodic mode interval=%ss db=%s", interval, db)
    while True:
        try:
            run_once(db, limit=limit, min_level=min_level,
                     report_dir=report_dir, viz_alerts=viz_alerts)
        except (FileNotFoundError, ValueError, sqlite3.Error) as exc:
            LOG.error("[guard] scan failed: %s", exc)
        time.sleep(max(1.0, float(interval)))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ACPS guard → supervisor 适配层（只读旁路，不自动处置）",
    )
    parser.add_argument("--db", required=True, type=Path, help="任务库路径（SQLite，只读打开）")
    parser.add_argument("--limit", type=int, default=1000, help="每种来源最多读取的事件/任务数")
    parser.add_argument("--min-level", choices=("MEDIUM", "HIGH"), default="MEDIUM",
                        help="低于该等级不产生告警（仍写入报告明细）")
    parser.add_argument("--interval", type=float, default=0,
                        help=">0 时进入周期模式（秒）")
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR,
                        help="报告目录（默认 acps_cluster/ws-supervisor/guard）")
    parser.add_argument("--viz-alerts", default=str(DEFAULT_VIZ_ALERTS),
                        help="viz 告警快照路径；none 表示关闭")
    parser.add_argument("--json", action="store_true", help="把报告打印到 stdout")
    parser.add_argument("--quiet", action="store_true", help="只输出告警日志，不输出 INFO 汇总")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if args.interval and args.interval > 0:
        try:
            run_forever(args.db, interval=args.interval, limit=args.limit,
                        min_level=args.min_level, report_dir=args.report_dir,
                        viz_alerts=args.viz_alerts)
        except KeyboardInterrupt:
            LOG.info("[guard] stopped")
            return 0
        return 0

    try:
        report, report_path, viz_path = run_once(
            args.db, limit=args.limit, min_level=args.min_level,
            report_dir=args.report_dir, viz_alerts=args.viz_alerts,
        )
    except FileNotFoundError:
        LOG.error("[guard] task db not found: %s", args.db)
        return 2
    except (ValueError, sqlite3.Error) as exc:
        LOG.error("[guard] unsupported task db: %s", exc)
        return 2

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(
            f"[guard] level={report.highest_level} alerts={len(report.alerts)} "
            f"tasks={report.task_count} peers={report.peer_count}"
        )
        for alert in report.alerts:
            print(f"  - {alert.level} {alert.channel} {alert.subject} "
                  f"({alert.score}/100) {','.join(alert.rules)}")
        print(f"[guard] report: {report_path}")
        if viz_path is not None:
            print(f"[guard] viz alerts: {viz_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
