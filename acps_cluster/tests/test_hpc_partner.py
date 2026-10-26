"""HPC 网关离线单测：作业校验/限额/背压/状态机（沙盒执行由 demo H 系列覆盖）。

    python -m unittest acps_cluster.tests.test_hpc_partner -v
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

CLUSTER_DIR = Path(__file__).resolve().parents[1]
for _p in (str(CLUSTER_DIR.parent), str(CLUSTER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import hpc_runner  # noqa: E402


def _with_tmp_jobs(fn):
    """把 JOBS_DIR 指到临时目录跑测试（不污染真实 ws-hpc/jobs）。

    JOBS_DIR 必须是临时目录的**子目录**：worker_tick 用 `JOBS_DIR.parent / "runs"` 定位沙盒
    产物根，若 JOBS_DIR 就是临时目录本身，runs 会落到 Temp/runs（沙盒外、跨进程残留），
    于是本测试会读到别的运行留下的文件——假绿（2026-10-07 审查发现）。
    """

    def wrapper(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs"
            jobs.mkdir()
            with mock.patch.object(hpc_runner, "JOBS_DIR", jobs):
                fn(self, Path(td))

    wrapper.__name__ = fn.__name__
    return wrapper


class ValidationTest(unittest.TestCase):
    @_with_tmp_jobs
    def test_合法作业通过(self, tmp):
        spec, error = hpc_runner.validate_job(
            {"name": "gnn-exp", "files": {"main.py": "print(1)"}, "walltimeMinutes": 5}
        )
        self.assertIsNone(error)
        self.assertEqual(spec["walltimeMinutes"], 5)

    @_with_tmp_jobs
    def test_缺main_py拒绝(self, tmp):
        _, error = hpc_runner.validate_job({"name": "x", "files": {"run.py": "1"}})
        self.assertIn("main.py", error)

    @_with_tmp_jobs
    def test_墙钟超限拒绝(self, tmp):
        _, error = hpc_runner.validate_job(
            {"name": "x", "files": {"main.py": "1"}, "walltimeMinutes": 999}
        )
        self.assertIn("墙钟", error)

    @_with_tmp_jobs
    def test_路径穿越拒绝(self, tmp):
        _, error = hpc_runner.validate_job(
            {"name": "x", "files": {"../esc.py": "1", "main.py": "1"}}
        )
        self.assertIsNotNone(error)

    @_with_tmp_jobs
    def test_作业名不合法拒绝(self, tmp):
        _, error = hpc_runner.validate_job({"name": "bad name!", "files": {"main.py": "1"}})
        self.assertIsNotNone(error)


class QueueTest(unittest.TestCase):
    @_with_tmp_jobs
    def test_提交入队并可列出(self, tmp):
        result = hpc_runner.submit({"name": "job-a", "files": {"main.py": "print(1)"}}, submitter="alice")
        self.assertTrue(result["ok"])
        jobs = hpc_runner.list_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["state"], "queued")
        self.assertEqual(jobs[0]["submittedBy"], "alice")

    @_with_tmp_jobs
    def test_队列深度背压(self, tmp):
        for i in range(hpc_runner.MAX_QUEUE_DEPTH):
            r = hpc_runner.submit({"name": f"j{i}", "files": {"main.py": "1"}})
            self.assertTrue(r["ok"])
        r = hpc_runner.submit({"name": "overflow", "files": {"main.py": "1"}})
        self.assertFalse(r["ok"])
        self.assertIn("背压", r["error"])

    @_with_tmp_jobs
    def test_未知作业取结果拒绝(self, tmp):
        self.assertFalse(hpc_runner.result("nope")["ok"])


class WorkerTickTest(unittest.TestCase):
    @_with_tmp_jobs
    def test_worker_tick状态机queued到终态(self, tmp):
        """mock 执行器（不碰 docker）：验证 queued→running→completed 状态机与元数据落盘。"""
        hpc_runner.submit({"name": "sm", "files": {"main.py": "1"}})

        async def fake_sandbox(raw, *, runs_root=None):
            # 与 worker_tick 算出的 runDir 一致：JOBS_DIR.parent/"runs"/f"run-{run_id}"
            run_dir = tmp / "runs" / "run-fake"
            (run_dir / "output").mkdir(parents=True, exist_ok=True)
            (run_dir / "output" / "metrics.json").write_text('{"acc": 0.9}', encoding="utf-8")
            (run_dir / "result.json").write_text('{"stdout": "TRAIN-OK"}', encoding="utf-8")

            class R:
                ok = True
                run_id = "fake"
                duration_s = 0.3
                exit_code = 0
                error = None

            return R()

        with mock.patch("gpu_runner.run_sandbox", side_effect=fake_sandbox):
            processed = asyncio.run(hpc_runner.worker_tick())
        self.assertEqual(processed, 1)
        job_id = hpc_runner.list_jobs()[0]["id"]
        self.assertEqual(hpc_runner.list_jobs()[0]["state"], "completed")
        result = hpc_runner.result(job_id)
        self.assertTrue(result["ok"])
        self.assertIn("TRAIN-OK", result["stdoutHead"])
        self.assertEqual(result["artifacts"][0]["name"], "metrics.json")


class WiringTest(unittest.TestCase):
    def test_能力档位_两橱窗两保险柜(self):
        import partner_hpc
        from delegation.pep import capability_visibility

        ids = [getattr(c, "capability", c).id for c in partner_hpc.HPC_CAPABILITIES]
        self.assertEqual(ids, ["hpc.info", "hpc.jobs", "hpc.submit", "hpc.result"])
        public, has_non = capability_visibility(partner_hpc.HPC_CAPABILITIES)
        self.assertEqual(public, frozenset({"hpc.info", "hpc.jobs"}))
        self.assertTrue(has_non)
        for cap in (partner_hpc.hpc_submit, partner_hpc.hpc_result):
            skill = cap.capability.to_acps_skill()
            self.assertEqual(skill.get("visibility"), "domain")


if __name__ == "__main__":
    unittest.main(verbosity=2)
