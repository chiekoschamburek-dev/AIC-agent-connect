"""guard 报告层:把检测器的原始结果汇总成可解释、可留档的 GuardReport。

从 yanshu-AIC ``acps_cluster/guard_adapter.py`` 移植,只保留"扫描→报告"部分:
- 保留 ``disposition="advisory-only"``、``pep_semantics="untouched"`` 契约字段;
- 去掉报告落盘 / viz 告警发布 / 周期循环(HTTP API 只读不写,留档仍由
  guard_adapter 的 CLI 模式承担)。
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from guard_api import covert_event_guard as guard

# 与 guard 自身的分级阈值保持一致(单任务:30/60;peer timing:20/40)。
_LEVEL_RANK = {"LOW": 0, "INSUFFICIENT_DATA": 0, "MEDIUM": 1, "HIGH": 2}
_ACTION = {
    "LOW": "observe",
    "INSUFFICIENT_DATA": "observe",
    "MEDIUM": "人工复核;适配层不自动处置",
    "HIGH": "人工复核并评估是否经既有 PEP 流程限速/撤销授权;适配层不自动处置",
}

MIN_LEVELS = ("LOW", "MEDIUM", "HIGH")


@dataclass(frozen=True)
class Alert:
    """一条需要人工关注的风险告警(只含可解释字段,不含载荷原文)。"""

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
    # 明确写死:本报告不构成攻击定论,也不是自动处置指令。
    disposition: str = "advisory-only"
    pep_semantics: str = "untouched"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        # highest_level 是派生量(不落成字段,避免和 alerts 不一致),但报告消费方
        # (运维脚本、viz 快照)需要它,故在序列化时补上。
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


def scan(db: str | Path, *, limit: int = 1000, min_level: str = "MEDIUM",
         session_gap_seconds: float = 3600.0) -> GuardReport:
    """调用 guard 检测入口,返回汇总报告(只读,不写任务库)。

    peer timing 是可选增强:早期/精简任务库可能没有 ``aip_tasks`` 表,此时
    记录 warning 并继续,不让单任务检测整体失败。
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
        peers = guard.analyze_peer_timing(peer_tasks, session_gap_seconds=session_gap_seconds)
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
