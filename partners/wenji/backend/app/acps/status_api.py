"""``GET /api/acps/status`` — AIP 互联状态（前端面板 / 演示 / 巡检用，只读）。

数据来源（全部只读，无副作用）：
- ``<workspace>/acps/enrollment.json``  AIC / 状态 / 公开端点
- ``<workspace>/acps/acs.final.json``    已批准 ACS（技能清单）
- ``<workspace>/logs/amp_*.jsonl``       AMP journal（心跳时点 / 任务级 access 统计）

设计约束：引擎进程与 wit serve 是两个进程，本端点不访问网络、不读进程内存，
只做文件级聚合；workspace 缺失（未注册/未启用 ACPs）时返回 ``enabled=false``。
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from .settings import AcpsSettings

router = APIRouter(prefix="/api/acps", tags=["acps"])


@dataclass(frozen=True)
class _Snapshot:
    enabled: bool
    payload: dict[str, Any]


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _journal_stats(logs_dir: Path) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "last_heartbeat_at": None,
        "task_total": 0,
        "task_p50_ms": None,
        "task_p95_ms": None,
        "rpc_total": 0,
        "last_task_at": None,
    }
    if not logs_dir.is_dir():
        return stats
    durations: list[float] = []
    last_task_ts = ""
    for path in logs_dir.glob("amp_access_*.jsonl"):
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    stats["rpc_total"] += 1
                    cid = str(rec.get("correlation_id") or "")
                    if cid.startswith("task-"):
                        stats["task_total"] += 1
                        dm = (rec.get("body") or {}).get("durationMs")
                        if isinstance(dm, (int, float)):
                            durations.append(float(dm))
                    ts = str(rec.get("timestamp") or "")
                    if cid.startswith("task-") and ts > last_task_ts:
                        last_task_ts = ts
        except OSError:
            continue
    for path in logs_dir.glob("amp_heartbeat_*.jsonl"):
        try:
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            if lines:
                rec = json.loads(lines[-1])
                ts = str(rec.get("timestamp") or "")
                if ts > str(stats["last_heartbeat_at"] or ""):
                    stats["last_heartbeat_at"] = ts
        except (OSError, json.JSONDecodeError, IndexError):
            continue
    if durations:
        stats["task_p50_ms"] = round(statistics.median(durations), 1)
        stats["task_p95_ms"] = round(sorted(durations)[max(0, int(len(durations) * 0.95) - 1)], 1)
    stats["last_task_at"] = last_task_ts or None
    return stats


def _snapshot() -> _Snapshot:
    settings = AcpsSettings.from_env()
    workspace = Path(settings.workspace)
    acps_dir = workspace / "acps"
    if not acps_dir.is_dir():
        return _Snapshot(False, {"reason": "workspace 不存在（未注册或未启用 ACPs）"})

    enrollment = _read_json(acps_dir / "enrollment.json") or {}
    final_acs = _read_json(acps_dir / "acs.final.json") or {}
    config = enrollment.get("acpsConfig", {}) or {}

    payload: dict[str, Any] = {
        "agent_id": settings.agent_id,
        "display_name": settings.display_name,
        "aic": (final_acs.get("aic") or enrollment.get("review", {}).get("aic") or ""),
        "status": enrollment.get("status"),
        "public_endpoint": config.get("publicEndpoint"),
        "registry_url": config.get("registryUrl"),
        "discovery_url": config.get("discoveryUrl"),
        "skills": [
            {"id": s.get("id"), "name": s.get("name"), "tags": s.get("tags", [])}
            for s in (final_acs.get("skills") or [])
        ],
        "listen": f"{settings.listen_host}:{settings.listen_port}",
        "stats": _journal_stats(workspace / "logs"),
    }
    return _Snapshot(True, payload)


@router.get("/status")
def acps_status() -> dict[str, Any]:
    snap = _snapshot()
    return {"enabled": snap.enabled, **snap.payload}
