"""guard_api 接口测试。

- 临时 SQLite(与 guard 兼容的 aip_task_events / aip_tasks 两表)+ FastAPI TestClient;
- 端点覆盖:health / scan(GET+POST) / tasks / peer-timing / events(内联);
- 挂载模式:用 start_guard_api 真起一个 uvicorn(临时端口),走真实 HTTP 再优雅停止。
"""

from __future__ import annotations

import asyncio
import base64
import httpx
import json
import socket
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from guard_api import create_app, start_guard_api, stop_guard_api

T0 = datetime(2026, 10, 20, 8, 0, 0, tzinfo=timezone.utc)
LOCAL_AIC = "aic-local-supervisor"
PEER_B = "aic-remote-b"
PEER_C = "aic-remote-c"

DDL = """
CREATE TABLE aip_task_events (
    task_ref   TEXT NOT NULL,
    event_seq  INTEGER,
    event_type TEXT,
    message_id TEXT,
    created_at TEXT NOT NULL,
    event_data TEXT
);
CREATE TABLE aip_tasks (
    task_ref   TEXT NOT NULL,
    direction  TEXT,
    local_aic  TEXT,
    remote_aic TEXT,
    created_at TEXT
);
"""


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _high_entropy_b64(size: int = 300_000) -> str:
    """确定性高熵字节流(周期 256 的均匀覆盖,熵 ≈ 8 bit/byte)的 Base64。"""
    return base64.b64encode(bytes((i * 37 + 11) % 256 for i in range(size))).decode()


def _normal_events() -> list[dict]:
    """task-normal-001:正常任务,3 个事件,状态单调 → LOW 0。"""
    states = ["accepted", "working", "completed"]
    return [
        {"task_ref": "task-normal-001", "event_seq": index, "event_type": "task.status",
         "created_at": _iso(T0 + timedelta(seconds=30 * index)),
         "event_data": {"status": {"state": state}, "text": f"step {index}"}}
        for index, state in enumerate(states, 1)
    ]


def _regular_events() -> list[dict]:
    """task-regular-001:7 个等间隔 60s 事件、载荷完全一致。

    timing 过度规律 +20(CV=0)、repeated_payload +10(6 次重复)→ 30 分 MEDIUM。
    (单任务分级阈值是 30/60,单靠 timing 的 20 分仍为 LOW,故叠加重复载荷。)
    """
    payload = {"status": {"state": "working"}, "text": "tick"}
    return [
        {"task_ref": "task-regular-001", "event_seq": index, "event_type": "task.status",
         "created_at": _iso(T0 + timedelta(seconds=60 * index)), "event_data": dict(payload)}
        for index in range(1, 8)
    ]


def _suspicious_events(blob: str) -> list[dict]:
    """task-suspicious-001:终态恢复 ×2(22) + 超大载荷(25) + 高熵 blob(16) → HIGH 63。"""
    big = {"status": {"state": "working"}, "blob": blob}
    plan = [
        (0, {"status": {"state": "completed"}, "text": "done early"}),
        (60, big),
        (120, {"status": {"state": "completed"}, "text": "done again"}),
        (180, {"status": {"state": "working"}, "text": "back to work"}),
        (240, {"status": {"state": "completed"}, "text": "final"}),
    ]
    return [
        {"task_ref": "task-suspicious-001", "event_seq": index, "event_type": "task.status",
         "created_at": _iso(T0 + timedelta(seconds=offset)), "event_data": payload}
        for index, (offset, payload) in enumerate(plan, 1)
    ]


def _peer_rows() -> list[tuple[str, str, str, str, str]]:
    """aip_tasks 行:B 方 7 个任务等间隔 60s(→MEDIUM 20),C 方 3 个(→INSUFFICIENT_DATA)。"""
    rows = []
    for index in range(7):
        rows.append((f"task-peer-b-{index:02d}", "outbound", LOCAL_AIC, PEER_B,
                     _iso(T0 + timedelta(seconds=60 * index))))
    for index in range(3):
        rows.append((f"task-peer-c-{index:02d}", "outbound", LOCAL_AIC, PEER_C,
                     _iso(T0 + timedelta(seconds=60 * index))))
    return rows


def make_db(path: Path) -> None:
    events = _normal_events() + _regular_events() + _suspicious_events(_high_entropy_b64())
    con = sqlite3.connect(path)
    try:
        con.executescript(DDL)
        con.executemany(
            "INSERT INTO aip_task_events VALUES (?, ?, ?, ?, ?, ?)",
            [(row["task_ref"], row["event_seq"], row["event_type"], None,
              row["created_at"], json.dumps(row["event_data"])) for row in events],
        )
        con.executemany("INSERT INTO aip_tasks VALUES (?, ?, ?, ?, ?)", _peer_rows())
        con.commit()
    finally:
        con.close()


class GuardApiTestCase(unittest.TestCase):
    """TestClient 覆盖全部端点与错误映射。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls._tmp.name) / "aip-tasks.sqlite3"
        make_db(cls.db_path)
        cls.client = TestClient(create_app(str(cls.db_path)))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        cls._tmp.cleanup()

    # ---- /health --------------------------------------------------------

    def test_health(self) -> None:
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["service"], "acps-guard-api")
        self.assertEqual(body["status"], "ok")
        self.assertTrue(body["db_exists"])
        self.assertEqual(body["disposition"], "advisory-only")

    # ---- /api/guard/scan --------------------------------------------------

    def test_scan_full_report(self) -> None:
        resp = self.client.get("/api/guard/scan")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["disposition"], "advisory-only")
        self.assertEqual(body["pep_semantics"], "untouched")
        self.assertEqual(body["task_count"], 3)   # normal / regular / suspicious
        self.assertEqual(body["peer_count"], 2)   # peer-b / peer-c
        self.assertEqual(body["highest_level"], "HIGH")
        subjects = {alert["subject"] for alert in body["alerts"]}
        self.assertIn("task-suspicious-001", subjects)
        self.assertIn(f"outbound:{LOCAL_AIC}->{PEER_B}", subjects)

    def test_scan_min_level_filter(self) -> None:
        resp = self.client.get("/api/guard/scan", params={"min_level": "HIGH"})
        self.assertEqual(resp.status_code, 200)
        alerts = resp.json()["alerts"]
        self.assertTrue(alerts)
        self.assertTrue(all(alert["level"] == "HIGH" for alert in alerts))

    def test_scan_post_body_params(self) -> None:
        resp = self.client.post("/api/guard/scan", json={"min_level": "HIGH", "limit": 100})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["window_limit"], 100)
        self.assertTrue(all(alert["level"] == "HIGH" for alert in body["alerts"]))

    def test_scan_invalid_min_level_rejected(self) -> None:
        resp = self.client.get("/api/guard/scan", params={"min_level": "CRITICAL"})
        self.assertEqual(resp.status_code, 422)

    # ---- /api/guard/tasks/{task_ref} -------------------------------------

    def test_task_suspicious_is_high(self) -> None:
        resp = self.client.get("/api/guard/tasks/task-suspicious-001")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["level"], "HIGH")
        self.assertEqual(body["event_count"], 5)
        rules = {finding["rule"] for finding in body["findings"]}
        self.assertIn("oversized_payload", rules)
        self.assertIn("encoded_high_entropy_blob", rules)
        self.assertIn("invalid_state_transition", rules)
        self.assertTrue(body["observed_from"] and body["observed_to"])

    def test_task_normal_is_low(self) -> None:
        resp = self.client.get("/api/guard/tasks/task-normal-001")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["level"], "LOW")
        self.assertEqual(body["score"], 0)

    def test_task_regular_timing_medium(self) -> None:
        resp = self.client.get("/api/guard/tasks/task-regular-001")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["level"], "MEDIUM")
        self.assertIn("over_regular_intervals",
                      {finding["rule"] for finding in body["findings"]})

    def test_task_not_found_is_404(self) -> None:
        resp = self.client.get("/api/guard/tasks/task-unknown")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["detail"]["code"], "task_not_found")

    # ---- /api/guard/peer-timing -------------------------------------------

    def test_peer_timing_reports(self) -> None:
        resp = self.client.get("/api/guard/peer-timing")
        self.assertEqual(resp.status_code, 200)
        peers = {item["remote_aic"]: item for item in resp.json()}
        self.assertEqual(set(peers), {PEER_B, PEER_C})
        scored = peers[PEER_B]
        self.assertEqual(scored["level"], "MEDIUM")
        self.assertEqual(scored["score"], 20)
        self.assertEqual(scored["task_count"], 7)
        self.assertEqual(scored["analyzable_sessions"], 1)
        self.assertIn("peer_over_regular_intervals",
                      {finding["rule"] for finding in scored["findings"]})
        sparse = peers[PEER_C]
        self.assertEqual(sparse["level"], "INSUFFICIENT_DATA")
        self.assertEqual(sparse["score"], 0)

    def test_peer_timing_session_gap_splits_sessions(self) -> None:
        resp = self.client.get("/api/guard/peer-timing", params={"session_gap": 30})
        self.assertEqual(resp.status_code, 200)
        scored = [item for item in resp.json() if item["remote_aic"] == PEER_B]
        self.assertEqual(scored[0]["session_count"], 7)
        self.assertEqual(scored[0]["level"], "INSUFFICIENT_DATA")

    # ---- /api/guard/events(内联,无库)-------------------------------------

    def test_events_jsonl_suspicious(self) -> None:
        lines = "\n".join(json.dumps(item) for item in _suspicious_events(_high_entropy_b64()))
        resp = self.client.post("/api/guard/events", content=lines,
                                headers={"Content-Type": "application/x-ndjson"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["level"], "HIGH")

    def test_events_json_array(self) -> None:
        resp = self.client.post("/api/guard/events", json=_normal_events())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["level"], "LOW")

    def test_events_rejects_bad_line(self) -> None:
        resp = self.client.post("/api/guard/events", content="{not json}\n",
                                headers={"Content-Type": "application/x-ndjson"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["detail"]["code"], "invalid_jsonl")

    def test_events_rejects_missing_timestamp(self) -> None:
        resp = self.client.post("/api/guard/events", json=[{"task_ref": "t1"}])
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["detail"]["code"], "bad_events")

    def test_events_rejects_empty_body(self) -> None:
        resp = self.client.post("/api/guard/events", json=[])
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["detail"]["code"], "empty_body")


class GuardApiConfigTestCase(unittest.TestCase):
    """db 未配置/缺失、Bearer Token 的行为。"""

    def test_scan_without_db_is_503(self) -> None:
        with TestClient(create_app(None)) as client:
            health = client.get("/health")
            self.assertEqual(health.status_code, 200)
            self.assertFalse(health.json()["db_exists"])
            resp = client.get("/api/guard/scan")
            self.assertEqual(resp.status_code, 503)
            self.assertEqual(resp.json()["detail"]["code"], "db_not_configured")

    def test_scan_missing_db_file_is_503(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "absent.sqlite3")
            with TestClient(create_app(missing)) as client:
                resp = client.get("/api/guard/scan")
                self.assertEqual(resp.status_code, 503)
                self.assertEqual(resp.json()["detail"]["code"], "db_not_found")

    def test_bearer_token_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "aip-tasks.sqlite3"
            make_db(db_path)
            with TestClient(create_app(str(db_path), token="s3cret")) as client:
                self.assertEqual(client.get("/health").status_code, 401)
                self.assertEqual(
                    client.get("/health", headers={"Authorization": "Bearer wrong"}).status_code, 403)
                self.assertEqual(
                    client.get("/health", headers={"Authorization": "Bearer s3cret"}).status_code, 200)
                secured = client.get("/api/guard/scan",
                                     headers={"Authorization": "Bearer s3cret"})
                self.assertEqual(secured.status_code, 200)


class MountModeTestCase(unittest.IsolatedAsyncioTestCase):
    """挂载模式:在事件循环里真起一个 uvicorn,走真实 HTTP,再优雅停止。"""

    async def test_mount_lifecycle_and_http(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "aip-tasks.sqlite3"
            make_db(db_path)
            port = _free_port()
            server, task = await start_guard_api(str(db_path), port=port, log_level="error")
            try:
                for _ in range(100):
                    if server.started:
                        break
                    await asyncio.sleep(0.05)
                self.assertTrue(server.started, "uvicorn did not start in time")

                base = f"http://127.0.0.1:{port}"
                async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
                    health = await client.get(f"{base}/health")
                    self.assertEqual(health.status_code, 200)
                    self.assertTrue(health.json()["db_exists"])
                    task_resp = await client.get(f"{base}/api/guard/tasks/task-normal-001")
                    self.assertEqual(task_resp.status_code, 200)
                    self.assertEqual(task_resp.json()["level"], "LOW")
            finally:
                await stop_guard_api(server, task)
            self.assertTrue(task.done())
            self.assertFalse(task.cancelled())


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


if __name__ == "__main__":
    unittest.main()
