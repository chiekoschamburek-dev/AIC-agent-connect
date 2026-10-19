"""L2 作业化交路（受理-作业-取件）回归：jobs_store 生命周期 + submit_job/check_job 工具。

背景：前端等待窗口 <20 分钟，复杂任务必须"秒级受理回执 + 后台跑 + 取件"。见 supervisor.py
`_make_submit_job_tool` 的注释与 jobs_store/jobs_runner 模块。
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

CL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CL))
sys.path.insert(0, str(CL.parent))
os.environ["YANSHU_RUN_TRACE"] = "0"

import jobs_store as js  # noqa: E402
import supervisor as s  # noqa: E402


class JobsStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="jobs-store-")
        self.root = Path(self._tmp.name) / "jobs"
        self.root.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_创建与完整生命周期(self):
        job_id, job_dir = js.create_job(self.root, "写一篇完整的深度研究报告", mode="deep_research")
        st = js.read_status(job_dir)
        self.assertEqual(st["state"], "queued")
        self.assertEqual(st["mode"], "deep_research")
        self.assertTrue((job_dir / "request.txt").read_text(encoding="utf-8").startswith("写一篇"))

        js.update_status(job_dir, state="running", started_at=js.now_iso(), pid=os.getpid())
        st = js.resolve_liveness(job_dir)
        self.assertEqual(st["state"], "running", "活 pid 不应被判 interrupted")

        (job_dir / "report.md").write_text("# 终稿\n内容内容", encoding="utf-8")
        js.update_status(job_dir, state="done", finished_at=js.now_iso())
        self.assertIn("终稿", js.job_report(job_dir) or "")
        self.assertGreaterEqual(js.elapsed_seconds(js.read_status(job_dir)) or 0, 0)

    def test_终态写入与死亡判定串行_不会覆盖done(self):
        _, job_dir = js.create_job(self.root, "任务文本占位")
        js.update_status(job_dir, state="running", pid=999_999_999, started_at=js.now_iso())
        results: list[dict] = []

        def _liveness() -> None:
            results.append(js.update_status(
                job_dir, expected_states=js._ACTIVE_STATES, state="interrupted",
                finished_at=js.now_iso(), reason="process-exited"))

        with js._status_lock(job_dir):
            worker = threading.Thread(target=_liveness)
            worker.start()
            time.sleep(0.1)  # 让 worker 先卡在锁上
            # 模拟作业进程在同一把锁内写完终态
            st = js.read_status(job_dir)
            st.update(state="done", finished_at=js.now_iso())
            js._write_status_unlocked(job_dir, st)
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]["state"], "done")
        self.assertEqual(js.read_status(job_dir)["state"], "done")

    def test_死pid判中断(self):
        _, job_dir = js.create_job(self.root, "任务文本占位")
        js.update_status(job_dir, state="running", pid=999_999_999, started_at=js.now_iso())
        st = js.resolve_liveness(job_dir)
        self.assertEqual(st["state"], "interrupted")

    def test_前缀查找与列表(self):
        job_id, _ = js.create_job(self.root, "任务文本占位")
        (self.root / "not-a-job").mkdir()
        found = js.find_job(self.root, job_id[:12])
        self.assertIsNotNone(found)
        self.assertEqual(found.name, job_id)
        self.assertIsNone(js.find_job(self.root, "job-不存在"))
        jobs = js.list_jobs(self.root)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["job_id"], job_id)

    def _make_job_dir(self, name: str, state: str = "done", *, pid=None,
                      started: str | None = None) -> Path:
        d = self.root / name
        d.mkdir()
        (d / "request.txt").write_text("task", encoding="utf-8")
        js._write_status(d, {
            "id": name, "mode": "custom", "state": state,
            "created_at": started or js.now_iso(), "started_at": started,
            "finished_at": None, "pid": pid, "exit_code": None, "question_head": "task",
        })
        return d

    def test_前缀歧义不取错作业(self):
        a = self._make_job_dir("job-20261017-010101-aaaa")
        b = self._make_job_dir("job-20261017-010101-aaab")
        self.assertIsNone(js.find_job(self.root, "job-20261017-010101-aaa"),
                          "前缀命中多个时必须拒绝猜")
        self.assertEqual(js.find_job(self.root, "job-20261017-010101-aaaa"), a)
        self.assertEqual(set(js.match_jobs(self.root, "job-20261017-010101-aaa")), {a, b})

    def test_作业号白名单拒绝绝对路径与点穿越(self):
        # 2026-10-17 审查 P1-1：job_ref 是模型参数，绝不能直接拼路径
        victim = self.root.parent / "ws-victim" / "jobs" / "job-20261017-000000-dead"
        victim.mkdir(parents=True)
        js._write_status(victim, {"id": "victim", "state": "done",
                                  "created_at": js.now_iso()})
        (victim / "report.md").write_text("受害工作区终稿", encoding="utf-8")
        for ref in (
            str(victim),                                      # 绝对路径
            "../ws-victim/jobs/job-20261017-000000-dead",     # 相对穿越
            "../../ws-victim/jobs/job-20261017-000000-dead",  # 更深的穿越
            "job-20261017-000000-dead/../../x",               # 混在合法前缀后
            "job_20261017-000000-dead",                       # 下划线不在白名单
            "job 20261017",                                   # 空格不在白名单
        ):
            self.assertEqual(js.match_jobs(self.root, ref), [], ref)
            self.assertIsNone(js.find_job(self.root, ref), ref)

    def test_ttl超时判中断(self):
        d = self._make_job_dir("job-20261017-020202-cccc", state="running",
                               pid=os.getpid(), started="2000-01-01T00:00:00+00:00")
        st = js.resolve_liveness(d)
        self.assertEqual(st["state"], "interrupted")
        self.assertEqual(st.get("reason"), "ttl-exceeded")

    def test_活pid且未超ttl保持running(self):
        d = self._make_job_dir("job-20261017-020202-dddd", state="running",
                               pid=os.getpid(), started=js.now_iso())
        self.assertEqual(js.resolve_liveness(d)["state"], "running")

    def test_pid_alive当前进程为真(self):
        # Windows 回归：旧实现 os.kill(pid, 0) 会 TerminateProcess 把进程杀掉
        self.assertTrue(js.pid_alive(os.getpid()))
        self.assertFalse(js.pid_alive(999_999_999))

    def test_报告文件存在即视为可取件(self):
        d = self._make_job_dir("job-20261017-030303-eeee", state="failed")
        (d / "report.md").write_text("终稿内容", encoding="utf-8")
        self.assertTrue(js.report_present(d))
        self.assertIn("终稿内容", js.job_report(d) or "")

    def test_prune只删过老终态并保留最近N(self):
        old = "2000-01-01T00:00:00+00:00"
        d1 = self._make_job_dir("job-20000101-000000-aaaa", state="done", started=old)
        js.update_status(d1, finished_at=old)
        d2 = self._make_job_dir("job-20000101-000000-bbbb", state="done", started=old)
        js.update_status(d2, finished_at=old)
        active = self._make_job_dir("job-20000101-000000-cccc", state="running",
                                    pid=os.getpid(), started=js.now_iso())
        removed = js.prune_jobs(self.root, keep_days=7, keep_last=1)
        self.assertEqual(removed, 1)
        self.assertFalse(d1.exists(), "最老的终态作业应被清理")
        self.assertTrue(d2.exists(), "最近 keep_last 个终态作业必须保留")
        self.assertTrue(active.exists(), "在跑的作业不能被清理")


class _FakePopen:
    last: dict = {}

    def __init__(self, args, **kwargs):  # noqa: ANN001
        _FakePopen.last = {"args": args, **kwargs}
        self.pid = 777_001
        out = kwargs.get("stdout")
        if out is not None and not out.closed:
            out.close()


class _NoClosePopen:
    """故意**不**替父进程关 stdout——复刻真实 `subprocess.Popen` 的行为。

    `_FakePopen` 会顺手关掉 stdout，所以它抓不到 FD 泄漏（2026-10-17 审查 P1-6）：
    子进程 dup 走 fd 之后，父进程那份仍需自己 close。
    """

    last: dict = {}

    def __init__(self, args, **kwargs):  # noqa: ANN001
        _NoClosePopen.last = {"args": args, **kwargs}
        self.pid = 777_002
        self.returncode = None


class JobToolsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="job-tools-")
        self.ws = Path(self._tmp.name)
        self._ws_patch = mock.patch.object(s, "WORKSPACE", self.ws)
        self._ws_patch.start()

    def tearDown(self) -> None:
        self._ws_patch.stop()
        self._tmp.cleanup()

    def test_submit_job_创建作业并启动进程(self):
        tool = s._make_submit_job_tool()
        with mock.patch.object(s.subprocess, "Popen", _FakePopen):
            out = asyncio.run(tool.handler({"question": "对扩散模型做一小时级深度研究", "mode": "deep_research"}))
        self.assertTrue(out.get("ok"), out)
        job_dir = self.ws / "jobs" / out["job_id"]
        self.assertTrue((job_dir / "request.txt").is_file())
        st = js.read_status(job_dir)
        self.assertEqual(st["pid"], 777_001)
        self.assertEqual(st["mode"], "deep_research")
        self.assertIn("受理回执", out.get("note") or "")
        self.assertEqual(_FakePopen.last["args"][1], "jobs_runner.py")

    def test_submit_job_成功路径关闭日志FD(self):
        # 常驻服务里每提交一个作业泄漏一个 FD，攒到 RLIMIT_NOFILE 后证书/sqlite/socket
        # 全部开始失败——所以成功路径也必须关掉父进程那份 fd
        tool = s._make_submit_job_tool()
        with mock.patch.object(s.subprocess, "Popen", _NoClosePopen):
            out = asyncio.run(tool.handler({"question": "对扩散模型做一小时级深度研究"}))
        self.assertTrue(out.get("ok"), out)
        logf = _NoClosePopen.last.get("stdout")
        self.assertIsNotNone(logf, "应当把日志文件作为 stdout 传给子进程")
        self.assertTrue(logf.closed, "Popen 之后父进程必须 close 自己的 fd")

    def test_submit_job_启动失败也关闭日志FD(self):
        captured = {}

        def _boom(*a, **k):  # noqa: ANN002
            captured["stdout"] = k.get("stdout")
            raise OSError("fork 失败")

        tool = s._make_submit_job_tool()
        with mock.patch.object(s.subprocess, "Popen", _boom):
            out = asyncio.run(tool.handler({"question": "对扩散模型做一小时级深度研究"}))
        self.assertFalse(out.get("ok"))
        self.assertIn("启动作业进程失败", out.get("error") or "")
        self.assertIsNotNone(captured.get("stdout"))
        self.assertTrue(captured["stdout"].closed, "失败路径同样不能漏 fd")

    def test_submit_job_拒绝过短任务(self):
        tool = s._make_submit_job_tool()
        out = asyncio.run(tool.handler({"question": "短"}))
        self.assertFalse(out.get("ok"))

    def test_check_job_完成取件返回report(self):
        root = self.ws / "jobs"
        root.mkdir()
        job_id, job_dir = js.create_job(root, "任务文本占位")
        (job_dir / "report.md").write_text("终稿正文内容", encoding="utf-8")
        js.update_status(job_dir, state="done", finished_at=js.now_iso())
        tool = s._make_check_job_tool()
        out = asyncio.run(tool.handler({"job_ref": job_id[:10]}))
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(out.get("state"), "done")
        self.assertIn("终稿正文内容", out.get("report") or "")
        self.assertIn("product_gate", out.get("note") or "")

    def test_check_job_运行中与死pid(self):
        root = self.ws / "jobs"
        root.mkdir()
        id1, d1 = js.create_job(root, "任务文本占位")
        js.update_status(d1, state="running", pid=os.getpid(), started_at=js.now_iso())
        id2, d2 = js.create_job(root, "任务文本占位")
        js.update_status(d2, state="running", pid=999_999_999, started_at=js.now_iso())
        tool = s._make_check_job_tool()
        out1 = asyncio.run(tool.handler({"job_ref": id1}))
        self.assertEqual(out1.get("state"), "running")
        self.assertIn("仍在跑", out1.get("note") or "")
        out2 = asyncio.run(tool.handler({"job_ref": id2}))
        self.assertEqual(out2.get("state"), "interrupted")

    def test_check_job_不存在与列表(self):
        (self.ws / "jobs").mkdir()
        tool = s._make_check_job_tool()
        out = asyncio.run(tool.handler({"job_ref": "job-不存在"}))
        self.assertFalse(out.get("ok"))
        out2 = asyncio.run(tool.handler({"job_ref": "list"}))
        self.assertTrue(out2.get("ok"))
        self.assertIn("jobs", out2)

    def test_check_job状态异常但终稿存在也返回report(self):
        # 状态写入失败不该让已落盘的终稿永久取不到（原先只在 state=done 时读）
        root = self.ws / "jobs"
        root.mkdir()
        job_id, job_dir = js.create_job(root, "任务文本占位")
        (job_dir / "report.md").write_text("终稿正文内容", encoding="utf-8")
        tool = s._make_check_job_tool()
        out = asyncio.run(tool.handler({"job_ref": job_id}))
        self.assertTrue(out.get("ok"), out)
        self.assertNotEqual(out.get("state"), "done")
        self.assertIn("终稿正文内容", out.get("report") or "")

    def test_check_job前缀歧义时要求更长作业号(self):
        root = self.ws / "jobs"
        root.mkdir()
        for name in ("job-20261017-040404-aaaa", "job-20261017-040404-aaab"):
            d = root / name
            d.mkdir()
            js._write_status(d, {"id": name, "state": "done", "created_at": js.now_iso()})
        tool = s._make_check_job_tool()
        out = asyncio.run(tool.handler({"job_ref": "job-20261017-040404-aaa"}))
        self.assertFalse(out.get("ok"))
        self.assertIn("命中 2 个作业", out.get("error", ""))
        self.assertIn("更长的作业号", out.get("error", ""))

    def test_并发上限拒绝新作业(self):
        root = self.ws / "jobs"
        root.mkdir()
        _, d = js.create_job(root, "已有一个在跑的作业")
        js.update_status(d, state="running", pid=os.getpid(), started_at=js.now_iso())
        tool = s._make_submit_job_tool()
        with mock.patch.dict(os.environ, {"ACPS_JOB_MAX_CONCURRENT": "1"}), \
             mock.patch.object(s.subprocess, "Popen", _FakePopen):
            out = asyncio.run(tool.handler({"question": "再来一个长任务跑后台"}))
        self.assertFalse(out.get("ok"), out)
        self.assertIn("并发作业已达上限 1", out.get("error", ""))

    def test_submit_job在作业进程内拒绝(self):
        # 2026-10-17 审查 P1-2：作业里不能再派生作业（防链式续命）
        tool = s._make_submit_job_tool()
        with mock.patch.dict(os.environ,
                             {"ACPS_JOB_REPORT_OUT": str(self.ws / "report.md")}):
            out = asyncio.run(tool.handler({"question": "再来一个长任务跑后台"}))
        self.assertFalse(out.get("ok"), out)
        self.assertIn("作业进程内", out.get("error", ""))
        self.assertFalse((self.ws / "jobs").exists(), "被拒时不应创建作业目录")

    def test_check_job拒绝绝对路径与穿越(self):
        root = self.ws / "jobs"
        root.mkdir()
        victim = self.ws / "ws-victim" / "jobs" / "job-20261017-000000-dead"
        victim.mkdir(parents=True)
        js._write_status(victim, {"id": "victim", "state": "done",
                                  "created_at": js.now_iso()})
        (victim / "report.md").write_text("受害工作区终稿", encoding="utf-8")
        tool = s._make_check_job_tool()
        for ref in (str(victim), "../ws-victim/jobs/job-20261017-000000-dead"):
            out = asyncio.run(tool.handler({"job_ref": ref}))
            self.assertFalse(out.get("ok"), ref)
            self.assertNotIn("report", out, ref)

    def test_check_job分页取件与截断标记(self):
        root = self.ws / "jobs"
        root.mkdir()
        job_id, job_dir = js.create_job(root, "任务文本占位")
        (job_dir / "report.md").write_text("".join(str(i % 10) for i in range(25)),
                                           encoding="utf-8")
        js.update_status(job_dir, state="done", finished_at=js.now_iso(),
                         run_status="completed", iterations=3)
        tool = s._make_check_job_tool()
        with mock.patch.dict(os.environ, {"ACPS_JOB_REPORT_PAGE_CHARS": "10"}):
            out1 = asyncio.run(tool.handler({"job_ref": job_id}))
            out2 = asyncio.run(tool.handler(
                {"job_ref": job_id, "offset": out1["report_next_offset"]}))
            out3 = asyncio.run(tool.handler({"job_ref": job_id, "offset": 20}))
            out4 = asyncio.run(tool.handler({"job_ref": job_id, "offset": 999}))
        self.assertTrue(out1["report_truncated"])
        self.assertEqual(out1["report_total_chars"], 25)
        self.assertEqual(len(out1["report"]), 10)
        self.assertEqual(out1["report_next_offset"], 10)
        self.assertIn("未取完", out1["note"])
        self.assertEqual(out1["run_status"], "completed")
        self.assertEqual(out1["iterations"], 3)
        self.assertEqual(out2["report_offset"], 10)
        self.assertEqual(len(out2["report"]), 10)
        self.assertTrue(out2["report_truncated"])
        self.assertEqual(len(out3["report"]), 5)
        self.assertFalse(out3["report_truncated"])
        self.assertEqual(out4["report"], "")
        self.assertEqual(out4["report_offset"], 25)
        self.assertFalse(out4["report_truncated"])

    def test_check_job运行中返回代码时间字段(self):
        root = self.ws / "jobs"
        root.mkdir()
        job_id, job_dir = js.create_job(root, "任务文本占位")
        js.update_status(job_dir, state="running", pid=os.getpid(),
                         started_at=js.now_iso())
        tool = s._make_check_job_tool()
        out = asyncio.run(tool.handler({"job_ref": job_id}))
        self.assertEqual(out["state"], "running")
        self.assertIn("suggested_poll_seconds", out)
        self.assertIn("ttl_remaining_seconds", out)
        self.assertIn("不是预计完成时间", out["note"])

    def test_submit_job成功后记录回执状态(self):
        tool = s._make_submit_job_tool()

        async def go():
            with mock.patch.object(s.subprocess, "Popen", _FakePopen):
                out = await tool.handler({"question": "对扩散模型做一小时级深度研究",
                                          "mode": "deep_research"})
            return out, list(s._JOB_RECEIPTS.get() or [])

        out, receipts = asyncio.run(go())
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(receipts[-1]["job_id"], out["job_id"])
        self.assertEqual(receipts[-1]["mode"], "deep_research")


class JobRunStateTest(unittest.TestCase):
    def test_done仅限成功收尾且终稿存在(self):
        import jobs_runner

        self.assertEqual(jobs_runner.final_state(True, "completed"), "done")
        self.assertEqual(jobs_runner.final_state(True, "success"), "done")
        self.assertEqual(jobs_runner.final_state(True, "budget_exhausted"), "failed")
        self.assertEqual(jobs_runner.final_state(True, ""), "failed")
        self.assertEqual(jobs_runner.final_state(False, "completed"), "failed")


class AtomicReportWriteTest(unittest.TestCase):
    def test_原子写不留临时文件(self):
        with tempfile.TemporaryDirectory(prefix="atomic-") as tmp:
            p = Path(tmp) / "report.md"
            s._atomic_write_text(p, "完整终稿")
            self.assertEqual(p.read_text(encoding="utf-8"), "完整终稿")
            self.assertEqual(list(Path(tmp).glob("report.md.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
