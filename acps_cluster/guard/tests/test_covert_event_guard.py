import base64
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from acps_cluster.covert_event_guard import Event, analyze_task, load_jsonl, load_sqlite


def event(seq, seconds, payload=None, event_type="task-status-update"):
    return Event("task-demo", event_type,
                 datetime(2026, 10, 20, tzinfo=timezone.utc) + timedelta(seconds=seconds),
                 payload or {"status": {"state": "working"}, "seq": seq}, seq)


class CovertEventGuardTest(unittest.TestCase):
    def test_normal_task_is_low_risk(self):
        report = analyze_task([
            event(1, 0, {"status": {"state": "accepted"}}),
            event(2, .7, {"status": {"state": "working"}}),
            event(3, 8.2, {"status": {"state": "awaiting-completion"}}),
            event(4, 13.9, {"status": {"state": "completed"}})])
        self.assertEqual(("LOW", 0), (report.level, report.score))

    def test_encoded_payload_is_detected(self):
        token = base64.b64encode(bytes(range(128))).decode()
        report = analyze_task([event(1, 0, {"text": token})])
        self.assertIn("encoded_high_entropy_blob", {item.rule for item in report.findings})

    def test_regular_timing_is_detected(self):
        report = analyze_task([event(i, i * 5, {"seq": i}) for i in range(7)])
        self.assertIn("over_regular_intervals", {item.rule for item in report.findings})

    def test_alternating_timing_is_detected(self):
        report = analyze_task([event(i, sec, {"seq": i})
                               for i, sec in enumerate([0, 1, 6, 7, 12, 13, 18, 19])])
        self.assertIn("alternating_interval_pattern", {item.rule for item in report.findings})

    def test_terminal_regression_is_detected(self):
        report = analyze_task([
            event(1, 0, {"status": {"state": "accepted"}}),
            event(2, 1, {"status": {"state": "completed"}}),
            event(3, 2.7, {"status": {"state": "working"}})])
        self.assertIn("invalid_state_transition", {item.rule for item in report.findings})

    def test_jsonl_loader(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False,
                                         encoding="utf-8") as handle:
            path = handle.name
            handle.write(json.dumps({
                "task_ref": "task-jsonl", "event_type": "task-result",
                "created_at": "2026-10-20T00:00:00Z",
                "event_data": {"status": {"state": "accepted"}}}) + "\n")
        try:
            events = load_jsonl(path)
        finally:
            os.unlink(path)
        self.assertEqual("task-jsonl", events[0].task_ref)

    def test_sqlite_loader_is_read_only(self):
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as handle:
            path = handle.name
        try:
            con = sqlite3.connect(path)
            con.execute("""CREATE TABLE aip_task_events (
                task_ref TEXT, event_seq INTEGER, message_id TEXT,
                event_type TEXT, event_data TEXT, created_at TEXT)""")
            con.execute("INSERT INTO aip_task_events VALUES (?,?,?,?,?,?)",
                        ("task-db", 1, "m1", "task-result",
                         json.dumps({"state": "accepted"}), "2026-10-20T00:00:00Z"))
            con.commit()
            con.close()
            self.assertEqual("task-db", load_sqlite(path, task_ref="task-db")[0].task_ref)
            con = sqlite3.connect(path)
            self.assertEqual(1, con.execute("SELECT COUNT(*) FROM aip_task_events").fetchone()[0])
            con.close()
        finally:
            os.unlink(path)


    def test_peer_regular_timing_is_scored(self):
        from acps_cluster.covert_event_guard import TaskStart, analyze_peer_timing
        base = datetime(2026, 10, 20, tzinfo=timezone.utc)
        tasks = [
            TaskStart(f"task-{i}", "inbound", "local", "remote",
                      base + timedelta(seconds=i * 60))
            for i in range(7)
        ]
        report = analyze_peer_timing(tasks)[0]
        self.assertEqual("MEDIUM", report.level)
        self.assertEqual(20, report.score)
        self.assertIn("peer_over_regular_intervals",
                      {finding.rule for finding in report.findings})

    def test_peer_sparse_data_is_not_scored(self):
        from acps_cluster.covert_event_guard import TaskStart, analyze_peer_timing
        base = datetime(2026, 10, 20, tzinfo=timezone.utc)
        tasks = [
            TaskStart(f"task-{i}", "inbound", "local", "remote",
                      base + timedelta(seconds=i * 60))
            for i in range(4)
        ]
        report = analyze_peer_timing(tasks)[0]
        self.assertEqual("INSUFFICIENT_DATA", report.level)
        self.assertEqual(0, report.score)

    def test_peer_four_intervals_are_observed_but_not_scored(self):
        from acps_cluster.covert_event_guard import TaskStart, analyze_peer_timing
        base = datetime(2026, 10, 20, tzinfo=timezone.utc)
        offsets = [0, 20, 100, 140, 300]
        tasks = [
            TaskStart(f"task-{i}", "inbound", "local", "remote",
                      base + timedelta(seconds=value))
            for i, value in enumerate(offsets)
        ]
        report = analyze_peer_timing(tasks)[0]
        self.assertEqual("LOW", report.level)
        self.assertEqual(1, report.analyzable_sessions)
        self.assertTrue(report.operational_observations)

    def test_peer_sqlite_loader(self):
        from acps_cluster.covert_event_guard import load_peer_tasks_sqlite
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as handle:
            path = handle.name
        try:
            con = sqlite3.connect(path)
            con.execute("""CREATE TABLE aip_tasks (
                task_ref TEXT, direction TEXT, local_aic TEXT,
                remote_aic TEXT, created_at TEXT)""")
            con.execute("INSERT INTO aip_tasks VALUES (?,?,?,?,?)",
                        ("task-peer", "inbound", "local", "remote",
                         "2026-10-20T00:00:00Z"))
            con.commit()
            con.close()
            tasks = load_peer_tasks_sqlite(path)
            self.assertEqual(("task-peer", "remote"),
                             (tasks[0].task_ref, tasks[0].remote_aic))
        finally:
            os.unlink(path)


    def test_subsecond_burst_is_operational_only(self):
        from acps_cluster.covert_event_guard import TaskStart, analyze_peer_timing
        base = datetime(2026, 10, 20, tzinfo=timezone.utc)
        tasks = [
            TaskStart(f"burst-{i}", "inbound", "local", "remote",
                      base + timedelta(milliseconds=i * 100))
            for i in range(8)
        ]
        report = analyze_peer_timing(tasks)[0]
        self.assertEqual("INSUFFICIENT_DATA", report.level)
        self.assertEqual(0, report.score)
        self.assertTrue(any("operational_burst" in item
                            for item in report.operational_observations))


if __name__ == "__main__":
    unittest.main()
