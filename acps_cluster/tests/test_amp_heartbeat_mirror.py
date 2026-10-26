"""mirror 达到上限时原子轮转，不原地裁剪（否则 sender 字节位点失效）。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

CLUSTER_DIR = Path(__file__).resolve().parents[1]
for _p in (str(CLUSTER_DIR.parent), str(CLUSTER_DIR), str(CLUSTER_DIR / "deploy")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import amp_heartbeat_mirror as mirror  # noqa: E402


def _heartbeat_line(aic: str = "1.2.3.4.5") -> str:
    return json.dumps({
        "schema_version": "1.0",
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "aic": aic,
        "log_type": "heartbeat",
        "log_id": "test-log-id",
        "body": {"uptimeSeconds": 1.0},
    }, ensure_ascii=False)


class MirrorRotationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="amp-mirror-")
        self.root = Path(self._tmp.name)
        self.cluster = self.root / "cluster"
        self.mirror_root = self.root / "mirror"
        (self.cluster / "ws-demo" / "logs").mkdir(parents=True)
        (self.mirror_root / "yanshu").mkdir(parents=True)
        self.src = self.cluster / "ws-demo" / "logs" / "amp_heartbeat_1.2.3.4.5.jsonl"
        self.src.write_text(_heartbeat_line() + "\n", encoding="utf-8")
        self.dst = self.mirror_root / "yanshu" / "amp_heartbeat_demo.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_达到上限时原子轮转且当前行落在新文件(self):
        old = b"old-line-1\nold-line-2\n"
        self.dst.write_bytes(old)
        written, stale = mirror.mirror_once(
            self.cluster, self.mirror_root, dry_run=False,
            stale_seconds=300, prune_mb=0.00001,  # 10 bytes，必然触发轮转
        )
        rotated = self.dst.with_name(self.dst.name + ".1")
        self.assertTrue(rotated.is_file())
        self.assertEqual(rotated.read_bytes(), old)
        self.assertTrue(self.dst.is_file())
        self.assertIn("test-log-id", self.dst.read_text(encoding="utf-8"))
        self.assertEqual(written, 1)
        self.assertEqual(stale, 0)

    def test_未到上限不轮转(self):
        old = b"old\n"
        self.dst.write_bytes(old)
        written, _ = mirror.mirror_once(
            self.cluster, self.mirror_root, dry_run=False,
            stale_seconds=300, prune_mb=1.0,
        )
        rotated = self.dst.with_name(self.dst.name + ".1")
        self.assertFalse(rotated.exists())
        self.assertTrue(self.dst.read_bytes().startswith(old))
        self.assertEqual(written, 1)


if __name__ == "__main__":
    unittest.main()
