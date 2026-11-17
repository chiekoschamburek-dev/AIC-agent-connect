"""P0 前置验证工具的离线单测（不联网、不起作业、不需要证书）。

覆盖三块纯逻辑：
- ACS 投影比较（新增/移除/字段变化、sha256 稳定）；
- 后台作业静态前置检查（env 文件解析、缺件识别）；
- 问迹并发判定（重叠 / 串行 / 时间戳缺失）。
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from acps_cluster.tools import p0_checks


def _skill(sid: str, **extra) -> dict:
    base = {"id": sid, "name": sid, "description": "d", "version": "1.0.0", "tags": []}
    base.update(extra)
    return base


class ProjectionTest(unittest.TestCase):
    def test_sha256稳定_与键序无关(self):
        a = [_skill("a", tags=["x", "y"]), _skill("b")]
        b = [_skill("a", tags=["x", "y"]), _skill("b")]
        self.assertEqual(p0_checks.projection_sha256(a), p0_checks.projection_sha256(b))
        c = [_skill("a", tags=["y", "x"]), _skill("b")]
        self.assertNotEqual(p0_checks.projection_sha256(a), p0_checks.projection_sha256(c))

    def test_diff识别新增移除与字段变化(self):
        base = [_skill("a"), _skill("b")]
        self.assertTrue(p0_checks.diff_skills(base, base)["same"])
        added = p0_checks.diff_skills(base, base + [_skill("c")])
        self.assertEqual(added["added"], ["c"])
        self.assertFalse(added["same"])
        removed = p0_checks.diff_skills(base, [_skill("a")])
        self.assertEqual(removed["removed"], ["b"])
        changed = p0_checks.diff_skills(base, [_skill("a"), _skill("b", description="新描述")])
        self.assertEqual(changed["changed"], ["b"])
        self.assertFalse(changed["same"])


class StaticChecksTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="p0-static-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_空目录报缺件(self):
        checks = {c["id"]: c for c in p0_checks.static_checks(self.tmp)}
        self.assertFalse(checks["env-file"]["ok"])
        self.assertFalse(checks["supervisor-acps"]["ok"])
        self.assertFalse(checks["registry-credentials"]["ok"])

    def test_有配置文件时逐项通过(self):
        deploy = self.tmp / "deploy"
        deploy.mkdir()
        (deploy / ".env.production").write_text(
            "OPENAI_API_KEY=sk-x\n"
            "ACPS_SUPERVISOR_USERNAME=u\n"
            "ACPS_SUPERVISOR_PASSWORD=p\n",
            encoding="utf-8",
        )
        acps = self.tmp / "ws-supervisor" / "acps"
        (acps / "client").mkdir(parents=True)
        (acps / "acs.final.json").write_text("{}", encoding="utf-8")
        (acps / "client" / "certificate.pem").write_text("x", encoding="utf-8")
        (acps / "client" / "private-key.pem").write_text("x", encoding="utf-8")
        checks = {c["id"]: c for c in p0_checks.static_checks(self.tmp)}
        for key in ("env-file", "model-config", "registry-credentials",
                    "supervisor-acps", "supervisor-cert", "job-workspace-root"):
            self.assertTrue(checks[key]["ok"], key)


class JobsRunTest(unittest.TestCase):
    """用假 runner 验证 jobs_run 的创建/跟踪/超时逻辑（不启动真实 supervisor）。"""

    FAKE = """\
import json, os, sys, time
from pathlib import Path
job = Path(sys.argv[1])
status = job / "status.json"

def update(**kw):
    data = json.loads(status.read_text(encoding="utf-8"))
    data.update(kw)
    status.write_text(json.dumps(data), encoding="utf-8")

update(state="running", pid=os.getpid())
mode = sys.argv[2] if len(sys.argv) > 2 else "done"
if mode == "sleep":
    time.sleep(60)
(job / "report.md").write_text("p0-ok", encoding="utf-8")
update(state="done", exit_code=0)
"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="p0-jobs-"))
        (self.tmp / "ws-supervisor").mkdir(parents=True)
        self.runner = self.tmp / "fake_runner.py"
        self.runner.write_text(self.FAKE, encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_正常完成路径(self):
        result = p0_checks.jobs_run(
            question="只回复 p0-ok", timeout_s=30, cluster=self.tmp, runner=self.runner,
        )
        self.assertEqual(result["state"], "done")
        self.assertFalse(result["timed_out"])
        self.assertEqual(result["report_bytes"], 5)
        self.assertIn("done", [item["state"] for item in result["timeline"]])

    def test_超时路径会终止进程(self):
        sleeping = self.tmp / "sleep_runner.py"
        sleeping.write_text(
            self.runner.read_text(encoding="utf-8").replace(
                'mode = sys.argv[2] if len(sys.argv) > 2 else "done"', 'mode = "sleep"'),
            encoding="utf-8",
        )
        result = p0_checks.jobs_run(
            question="只回复 p0-ok", timeout_s=3, cluster=self.tmp, runner=sleeping,
        )
        self.assertTrue(result["timed_out"])
        self.assertIn(result["state"], {"running", "interrupted"})


class EnvParseTest(unittest.TestCase):
    def test_解析键值并跳过注释空值(self):
        tmp = Path(tempfile.mkdtemp(prefix="p0-env-"))
        try:
            path = tmp / ".env.production"
            path.write_text(
                "# 注释\nOPENAI_API_KEY='sk-1'\nexport FOO=bar\nEMPTY=\nA=1\n",
                encoding="utf-8",
            )
            values = p0_checks.parse_env_file(path)
            self.assertEqual(values["OPENAI_API_KEY"], "sk-1")
            self.assertEqual(values["A"], "1")
            self.assertNotIn("EMPTY", values)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ConcurrencyTest(unittest.TestCase):
    @staticmethod
    def _run(start: str, end: str, state: str = "completed") -> dict:
        return {"started_at": start, "finished_at": end, "final_state": state}

    def test_短任务在长任务结束前完成_判concurrent(self):
        long_run = self._run("2026-11-17T10:00:00+00:00", "2026-11-17T10:01:00+00:00")
        short_run = self._run("2026-11-17T10:00:02+00:00", "2026-11-17T10:00:05+00:00")
        result = p0_checks.concurrency_verdict(long_run, short_run)
        self.assertEqual(result["verdict"], "concurrent")
        self.assertTrue(result["overlap"])
        self.assertTrue(result["short_finished_first"])

    def test_短任务在长任务结束后才开始_判serialized(self):
        long_run = self._run("2026-11-17T10:00:00+00:00", "2026-11-17T10:01:00+00:00")
        short_run = self._run("2026-11-17T10:01:01+00:00", "2026-11-17T10:01:02+00:00")
        result = p0_checks.concurrency_verdict(long_run, short_run)
        self.assertEqual(result["verdict"], "serialized")
        self.assertFalse(result["overlap"])

    def test_重叠但短任务后完成_判inconclusive(self):
        long_run = self._run("2026-11-17T10:00:00+00:00", "2026-11-17T10:01:00+00:00")
        short_run = self._run("2026-11-17T10:00:50+00:00", "2026-11-17T10:01:30+00:00")
        result = p0_checks.concurrency_verdict(long_run, short_run)
        self.assertEqual(result["verdict"], "inconclusive")

    def test_缺时间戳_判unknown(self):
        result = p0_checks.concurrency_verdict({"started_at": "", "finished_at": ""},
                                               {"started_at": "x", "finished_at": "y"})
        self.assertEqual(result["verdict"], "unknown")


class CliTest(unittest.TestCase):
    def test_子命令解析(self):
        parser = p0_checks.build_parser()
        acs = parser.parse_args(["acs"])
        self.assertEqual(acs.func.__name__, "cmd_acs")
        jobs = parser.parse_args(["jobs", "--static"])
        self.assertEqual(jobs.func.__name__, "cmd_jobs")
        run = parser.parse_args(["jobs", "--run", "--yes", "--timeout", "60"])
        self.assertTrue(run.run and run.yes)
        wenji = parser.parse_args(["wenji", "--workspace", "ws", "--peer-aic", "aic"])
        self.assertEqual(wenji.func.__name__, "cmd_wenji")
        with self.assertRaises(SystemExit):
            parser.parse_args([])  # 必须有子命令


if __name__ == "__main__":
    unittest.main(verbosity=2)