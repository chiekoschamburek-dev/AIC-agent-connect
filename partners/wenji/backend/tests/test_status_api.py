"""status_api 单元测试：journal 统计与 workspace 快照（不依赖真实注册）。"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.acps import status_api


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_journal_stats_empty(tmp_path: Path) -> None:
    stats = status_api._journal_stats(tmp_path / "logs")
    assert stats["task_total"] == 0
    assert stats["last_heartbeat_at"] is None


def test_journal_stats_counts_tasks_and_heartbeat(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir(parents=True)
    aic = "1.2.156...X"
    access = logs / f"amp_access_{aic}.jsonl"
    records = [
        {"correlation_id": "rpc-1", "timestamp": "2026-10-15T01:00:00Z", "body": {"durationMs": 100}},
        {"correlation_id": "task-a", "timestamp": "2026-10-15T02:00:00Z", "body": {"durationMs": 1000, "response": {"statusCode": 200}}},
        {"correlation_id": "task-b", "timestamp": "2026-10-15T03:00:00Z", "body": {"durationMs": 3000, "response": {"statusCode": 200}}},
    ]
    access.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    hb = logs / f"amp_heartbeat_{aic}.jsonl"
    hb.write_text(json.dumps({"timestamp": "2026-10-15T03:30:00Z"}) + "\n", encoding="utf-8")

    stats = status_api._journal_stats(tmp_path / "logs")
    assert stats["rpc_total"] == 3
    assert stats["task_total"] == 2
    assert stats["task_p50_ms"] == 2000.0
    assert stats["last_heartbeat_at"] == "2026-10-15T03:30:00Z"
    assert stats["last_task_at"] == "2026-10-15T03:00:00Z"


def test_status_endpoint_reports_disabled_without_workspace(tmp_path: Path, monkeypatch) -> None:
    from app.acps.settings import AcpsSettings

    settings = AcpsSettings(workspace=tmp_path / "nope")
    monkeypatch.setattr(status_api, "_snapshot", lambda: status_api._Snapshot(False, {"reason": "x"}))
    from app.main import app

    client = TestClient(app)
    resp = client.get("/api/acps/status")
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False


def test_snapshot_reads_workspace(tmp_path: Path, monkeypatch) -> None:
    acps = tmp_path / "acps"
    _write(acps / "enrollment.json", {
        "status": "active",
        "acpsConfig": {"publicEndpoint": "https://8.160.176.215:8601",
                       "registryUrl": "https://wt.ioa.pub/registry-server",
                       "discoveryUrl": "https://wt.ioa.pub/discovery/acps-adp-v2/discover"},
    })
    _write(acps / "acs.final.json", {
        "aic": "1.2.156...X",
        "skills": [{"id": "wenji.ask", "name": "ask", "tags": ["问数"]}],
    })

    from app.acps.settings import AcpsSettings

    monkeypatch.setattr(
        status_api.AcpsSettings, "from_env",
        classmethod(lambda cls: AcpsSettings(workspace=tmp_path)),
    )
    snap = status_api._snapshot()
    assert snap.enabled is True
    assert snap.payload["aic"] == "1.2.156...X"
    assert snap.payload["status"] == "active"
    assert snap.payload["skills"][0]["id"] == "wenji.ask"
    assert snap.payload["public_endpoint"] == "https://8.160.176.215:8601"
