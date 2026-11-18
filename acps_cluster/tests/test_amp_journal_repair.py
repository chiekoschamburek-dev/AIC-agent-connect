"""AMP journal 启动前自愈（清心跳 + 截半截尾行）的离线单测。

背景：WIT 的 `AmpRuntime.start()` 会校验各日志族历史，硬杀留下的半行会让启动抛
`AmpPersistenceError`；Supervisor 若把它当“没服务”退出，systemd 每 300s 重启一轮，
叮当侧就是周期性掉线（2026-11-18 审查 P0/P1）。本脚本负责在启动前把这类文件修好。
"""

import sys
import tempfile
import unittest
from pathlib import Path

CLUSTER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CLUSTER_DIR / "deploy"))

import repair_amp_journals  # noqa: E402


class RepairAmpJournalsTest(unittest.TestCase):
    def test_清掉心跳日志(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            heartbeat = logs / "amp_heartbeat_AIC.jsonl"
            heartbeat.write_text('{"log_type":"heartbeat"}\n', encoding="utf-8")
            removed, repaired = repair_amp_journals.repair_dir(logs)
            self.assertEqual(removed, 1)
            self.assertEqual(repaired, 0)
            self.assertFalse(heartbeat.exists())

    def test_只截掉不完整的尾行(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            system = logs / "amp_system_AIC.jsonl"
            complete = '{"log_type":"system","log_id":"1"}\n'
            system.write_text(complete + '{"log_type":"system","log_id":"2"', encoding="utf-8")
            removed, repaired = repair_amp_journals.repair_dir(logs)
            self.assertEqual(removed, 0)
            self.assertEqual(repaired, 1)
            self.assertEqual(system.read_text(encoding="utf-8"), complete)

    def test_完整文件不动(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            access = logs / "amp_access_AIC.jsonl"
            content = '{"log_type":"access","log_id":"1"}\n' * 3
            access.write_text(content, encoding="utf-8")
            removed, repaired = repair_amp_journals.repair_dir(logs)
            self.assertEqual((removed, repaired), (0, 0))
            self.assertEqual(access.read_text(encoding="utf-8"), content)

    def test_目录不存在不抛异常(self):
        self.assertEqual(repair_amp_journals.repair_dir(Path("no-such-logs-dir")), (0, 0))

    def test_空文件不报修复(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            (logs / "amp_system_AIC.jsonl").write_bytes(b"")
            self.assertEqual(repair_amp_journals.repair_dir(logs), (0, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)