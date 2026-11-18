"""job worker 工作区刷新：旧身份材料不能残留到下一次作业。"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

CLUSTER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CLUSTER_DIR.parent))

from acps_cluster.runtime import jobs_runner  # noqa: E402


class PrepareJobWorkspaceTest(unittest.TestCase):
    def test_清理上一轮复制的身份材料并保留发现缓存(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "ws-supervisor" / "acps"
            (src / "domains").mkdir(parents=True)
            (src / "acs.final.json").write_text('{"aic": "new"}', encoding="utf-8")
            (src / "domains" / "active.json").write_text("{}", encoding="utf-8")

            base = root / "ws-supervisor-job"
            stale = base / "acps"
            (stale / "domains").mkdir(parents=True)
            (stale / "domains" / "revoked.json").write_text("{}", encoding="utf-8")
            (stale / "acs.final.json").write_text('{"aic": "old"}', encoding="utf-8")
            (stale / "discovered").mkdir()
            (stale / "discovered" / "cached.json").write_text("{}", encoding="utf-8")

            with mock.patch.object(jobs_runner, "CLUSTER", root):
                jobs_runner._prepare_job_workspace(base)

            self.assertFalse((stale / "domains" / "revoked.json").exists())
            self.assertTrue((stale / "domains" / "active.json").exists())
            self.assertEqual((stale / "acs.final.json").read_text(encoding="utf-8"),
                             '{"aic": "new"}')
            self.assertTrue((stale / "discovered" / "cached.json").exists())

    def test_没有发现缓存时不报错(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "ws-supervisor" / "acps"
            src.mkdir(parents=True)
            (src / "acs.final.json").write_text("{}", encoding="utf-8")
            base = root / "ws-supervisor-job"
            with mock.patch.object(jobs_runner, "CLUSTER", root):
                jobs_runner._prepare_job_workspace(base)
            self.assertTrue((base / "acps" / "acs.final.json").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)