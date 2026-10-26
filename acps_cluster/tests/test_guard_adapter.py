"""guard → supervisor 适配层的离线单测（不联网、只碰临时 SQLite）。

覆盖三条主张：
1. 适配层只读任务库（模式 ro，扫描前后行数/文件不变）；
2. 正常任务零告警，构造的异常载荷/时序能产出可解释告警；
3. 报告与 viz 告警快照按契约落盘，且 ``disposition=advisory-only``（不自动处置）。
"""

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _p in (str(ROOT), str(ROOT / "acps_cluster")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from acps_cluster import guard_adapter  # noqa: E402

BASE = datetime(2026, 10, 20, 8, 0, 0, tzinfo=timezone.utc)
HIGH_ENTROPY = (
    "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8gISIjJCUmJygpKissLS4vMDEyMzQ1"
    "Njc4OTo7PD0+P0BBQkNERUZHSElKS0xNTk9QUVJTVFVWV1hZWltcXV5fYGFiY2RlZmdoaWpr"
    "bG1ub3BxcnN0dXZ3eHl6e3x9fn8="
)


def _iso(seconds: float) -> str:
    return (BASE + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def make_db(path: Path, *, events=(), tasks=(), with_peer_table: bool = True) -> Path:
    con = sqlite3.connect(path)
    try:
        con.execute("""CREATE TABLE aip_task_events (
            task_ref TEXT, event_seq INTEGER, message_id TEXT,
            event_type TEXT, event_data TEXT, created_at TEXT)""")
        for seq, seconds, task_ref, data in events:
            con.execute(
                "INSERT INTO aip_task_events VALUES (?,?,?,?,?,?)",
                (task_ref, seq, f"m{seq}", "task-status-update",
                 json.dumps(data), _iso(seconds)),
            )
        if with_peer_table:
            con.execute("""CREATE TABLE aip_tasks (
                task_ref TEXT, direction TEXT, local_aic TEXT,
                remote_aic TEXT, created_at TEXT)""")
            for index, (task_ref, direction, local, remote, seconds) in enumerate(tasks):
                con.execute("INSERT INTO aip_tasks VALUES (?,?,?,?,?)",
                            (task_ref, direction, local, remote, _iso(seconds)))
        con.commit()
    finally:
        con.close()
    return path


def normal_events(task="task-normal"):
    states = [(0, "accepted"), (2, "working"), (40, "awaiting-completion"), (90, "completed")]
    return [(index, seconds, task, {"status": {"state": state}})
            for index, (seconds, state) in enumerate(states, 1)]


def suspicious_events(task="task-suspicious"):
    """与 guard 的异常样例同构：高熵 base64 + 规律交替 + 终态回退。"""
    return [
        (1, 0, task, {"status": {"state": "accepted"}, "text": HIGH_ENTROPY}),
        (2, 1, task, {"status": {"state": "working"}}),
        (3, 6, task, {"status": {"state": "working"}}),
        (4, 7, task, {"status": {"state": "working"}}),
        (5, 12, task, {"status": {"state": "completed"}}),
        (6, 13, task, {"status": {"state": "working"}}),
        (7, 18, task, {"status": {"state": "completed"}}),
        (8, 19, task, {"status": {"state": "working"}}),
    ]


class GuardAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def test_正常任务零告警(self):
        db = make_db(self.tmp / "tasks.sqlite3", events=normal_events())
        report = guard_adapter.scan(db)
        self.assertEqual([], report.alerts)
        self.assertEqual("LOW", report.highest_level)
        self.assertEqual(1, report.task_count)

    def test_异常任务产出可解释告警(self):
        db = make_db(self.tmp / "tasks.sqlite3", events=suspicious_events())
        report = guard_adapter.scan(db)
        self.assertTrue(report.alerts, "构造的异常样例必须触发告警")
        alert = report.alerts[0]
        self.assertEqual("task", alert.channel)
        self.assertEqual("task-suspicious", alert.subject)
        self.assertIn(alert.level, {"MEDIUM", "HIGH"})
        self.assertTrue(alert.rules)
        self.assertIn("人工复核", alert.suggested_action)

    def test_只读扫描不改任务库(self):
        db = make_db(self.tmp / "tasks.sqlite3", events=suspicious_events(),
                     tasks=[("task-suspicious", "inbound", "local", "remote", 0)])
        before = (db.stat().st_size, db.read_bytes())
        guard_adapter.scan(db)
        after = (db.stat().st_size, db.read_bytes())
        self.assertEqual(before, after)
        self.assertFalse((self.tmp / "tasks.sqlite3-journal").exists())

    def test_缺少aip_tasks表时降级为单任务检测(self):
        db = make_db(self.tmp / "tasks.sqlite3", events=normal_events(),
                     with_peer_table=False)
        report = guard_adapter.scan(db)
        self.assertEqual(0, report.peer_count)
        self.assertTrue(report.warnings)
        self.assertIn("peer timing skipped", report.warnings[0])

    def test_peer_规律间隔升级为告警(self):
        tasks = [(f"task-{i}", "inbound", "local", "remote", i * 60) for i in range(7)]
        db = make_db(self.tmp / "tasks.sqlite3", events=normal_events(), tasks=tasks)
        report = guard_adapter.scan(db)
        peer_alerts = [item for item in report.alerts if item.channel == "peer"]
        self.assertTrue(peer_alerts)
        self.assertIn("peer_over_regular_intervals", peer_alerts[0].rules)

    def test_报告与viz告警落盘且声明仅建议(self):
        db = make_db(self.tmp / "tasks.sqlite3", events=suspicious_events())
        report, report_path, viz_path = guard_adapter.run_once(
            db,
            report_dir=self.tmp / "reports",
            viz_alerts=self.tmp / "viz" / "guard-alerts.json",
        )
        self.assertTrue(report_path.is_file())
        self.assertTrue((self.tmp / "reports" / "latest.json").is_file())
        payload = json.loads(Path(report_path).read_text(encoding="utf-8"))
        self.assertEqual("advisory-only", payload["disposition"])
        self.assertEqual("untouched", payload["pep_semantics"])
        self.assertEqual(len(report.alerts), payload["alerts"].__len__())

        self.assertIsNotNone(viz_path)
        viz = json.loads(Path(viz_path).read_text(encoding="utf-8"))
        self.assertEqual(payload["highest_level"], viz["highest_level"])
        self.assertEqual(payload["alerts"], viz["alerts"])

    def test_viz告警可关闭(self):
        db = make_db(self.tmp / "tasks.sqlite3", events=normal_events())
        _, _, viz_path = guard_adapter.run_once(
            db, report_dir=self.tmp / "reports", viz_alerts="none")
        self.assertIsNone(viz_path)

    def test_告警可写入viz事件流(self):
        class FakeRecorder:
            def __init__(self):
                self.events = []

            def guard_alert(self, **fields):
                self.events.append(fields)

        db = make_db(self.tmp / "tasks.sqlite3", events=suspicious_events())
        report = guard_adapter.scan(db)
        recorder = FakeRecorder()
        written = guard_adapter.attach_recorder(recorder, report)
        self.assertEqual(len(report.alerts), written)
        self.assertEqual(report.alerts[0].subject, recorder.events[0]["subject"])

    def test_viz_live_recorder支持guard_alert事件(self):
        from acps_cluster.core import viz_live

        rec = viz_live.Recorder(
            instruction="guard 告警接线",
            run_id="run-guard-test",
            scenario_dir_override=self.tmp,
            base_url_override="https://viz.example.com",
        )
        rec.guard_alert(channel="task", level="HIGH", score=73,
                        subject="task-x", rules=["encoded_high_entropy_blob"],
                        detail="demo", suggested_action="人工复核")
        alerts = [event for event in rec.events if event["type"] == "guard_alert"]
        self.assertEqual(1, len(alerts))
        self.assertEqual("HIGH", alerts[0]["level"])

    def test_命令行_json输出(self):
        db = make_db(self.tmp / "tasks.sqlite3", events=normal_events())
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = guard_adapter.main([
                "--db", str(db), "--json",
                "--report-dir", str(self.tmp / "reports"),
                "--viz-alerts", "none",
            ])
        self.assertEqual(0, code)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(1, payload["task_count"])

    def test_不存在的库返回2(self):
        code = guard_adapter.main([
            "--db", str(self.tmp / "missing.sqlite3"),
            "--report-dir", str(self.tmp / "reports"),
            "--viz-alerts", "none",
        ])
        self.assertEqual(2, code)


class GuardShimTest(unittest.TestCase):
    def test_兼容路径仍可导入队友模块(self):
        from acps_cluster.covert_event_guard import analyze_task, load_sqlite  # noqa: F401

        self.assertTrue(callable(analyze_task))
        self.assertTrue(callable(load_sqlite))


if __name__ == "__main__":
    unittest.main()
