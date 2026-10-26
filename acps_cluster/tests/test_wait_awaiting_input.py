"""wait_partner / wait_many 对 `awaiting-input` 的提前返回回归（2026-10-16）。

现场：synthesis 收到派单后 3 秒即抛 awaiting-input（在请 leader 补料），但等待循环
只认「有产物 / 终态 / awaiting-completion」——问题被"看不见"，白等 11.7 分钟
（wait_many 5 分钟×2 超时后才靠 list/inspect 发现）。修法：把 awaiting-input 纳入
提前返回集合，返回体里带明确处置指引。
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path

CL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CL))
sys.path.insert(0, str(CL.parent))
os.environ["YANSHU_RUN_TRACE"] = "0"

from acps_cluster.agents import supervisor as s  # noqa: E402


class _FakeRuntime:
    """每个 ref 一个快照序列：取值时弹一个，最后一个保持重复（模拟轮询）。"""

    def __init__(self, by_ref: dict[str, list[dict]]) -> None:
        self._by_ref = {k: list(v) for k, v in by_ref.items()}

    async def aip_task(self, state, action, task_ref):  # noqa: ANN001, ANN002, ANN003
        seq = self._by_ref.get(task_ref) or [{"state": "working", "products": []}]
        return seq.pop(0) if len(seq) > 1 else seq[0]


class WaitAwaitingInputTest(unittest.TestCase):
    def test_wait_partner遇awaiting_input立即返回(self):
        rt = _FakeRuntime({"task_x": [{"state": "awaiting-input", "products": []}]})
        tool = s._make_wait_partner_tool(rt, object(), default_seconds=5, poll_seconds=0.01)
        out = asyncio.run(tool.handler({"task_ref": "task_x"}))
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(out.get("state"), "awaiting-input")
        self.assertIn("补料", str(out.get("note") or ""))

    def test_wait_partner仍正常等待到终态(self):
        rt = _FakeRuntime({"task_y": [
            {"state": "working", "products": []},
            {"state": "completed", "products": [{"id": "p1"}]},
        ]})
        tool = s._make_wait_partner_tool(rt, object(), default_seconds=3, poll_seconds=0.01)
        out = asyncio.run(tool.handler({"task_ref": "task_y"}))
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(out.get("state"), "completed")
        self.assertEqual(out.get("products"), 1)

    def test_wait_many把awaiting_input放进arrived并优先提示(self):
        rt = _FakeRuntime({
            "a": [{"state": "awaiting-input", "products": []}],
            "b": [{"state": "working", "products": []}],
        })
        tool = s._make_wait_many_tool(rt, object(), default_seconds=1, poll_seconds=0.01)
        out = asyncio.run(tool.handler({"task_refs": ["a", "b"], "max_seconds": 1}))
        arrived = out.get("arrived") or []
        self.assertEqual(len(arrived), 1, out)
        self.assertEqual(arrived[0].get("state"), "awaiting-input")
        self.assertIn("b", out.get("pending") or [])
        self.assertIn("补料", str(out.get("note") or ""))

    def test_wait_many全终态时行为不变(self):
        rt = _FakeRuntime({
            "a": [{"state": "completed", "products": [{"id": "p"}]}],
            "b": [{"state": "awaiting-completion", "products": [{"id": "q"}]}],
        })
        tool = s._make_wait_many_tool(rt, object(), default_seconds=1, poll_seconds=0.01)
        out = asyncio.run(tool.handler({"task_refs": ["a", "b"], "max_seconds": 1}))
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(out.get("pending"), [])
        self.assertEqual(len(out.get("arrived") or []), 2)

    def test_超预算时等待返回带强制改道指令(self):
        import time as _t

        s._RUN_STARTED_AT.set(_t.monotonic() - 11 * 60)  # 模拟 run 已进行 11 分钟
        try:
            rt = _FakeRuntime({"t": [
                {"state": "working", "products": []},
                {"state": "completed", "products": [{"id": "p"}]},
            ]})
            tool = s._make_wait_partner_tool(rt, object(), default_seconds=2, poll_seconds=0.01)
            out = asyncio.run(tool.handler({"task_ref": "t"}))
            note = str(out.get("note") or "")
            self.assertIn("已进行 11 分钟", note)
            self.assertIn("立即改道作业化", note)
            self.assertIn("submit_job", note)
        finally:
            s._RUN_STARTED_AT.set(None)

    def test_预算内等待返回只带计时不催改道(self):
        import time as _t

        s._RUN_STARTED_AT.set(_t.monotonic())
        try:
            rt = _FakeRuntime({"t2": [{"state": "completed", "products": [{"id": "p"}]}]})
            tool = s._make_wait_partner_tool(rt, object(), default_seconds=2, poll_seconds=0.01)
            out = asyncio.run(tool.handler({"task_ref": "t2"}))
            note = str(out.get("note") or "")
            self.assertIn("已进行 0 分钟", note)
            self.assertNotIn("立即改道作业化", note)
        finally:
            s._RUN_STARTED_AT.set(None)


class BudgetClockTest(unittest.TestCase):
    """同步预算：默认 8 分钟 + 高频工具也带剩余时间（2026-10-17）。"""

    def test_默认预算8分钟(self):
        from unittest import mock

        from acps_cluster.agents import supervisor as s

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ACPS_SUPERVISOR_SYNC_BUDGET_MINUTES", None)
            self.assertEqual(s._sync_budget_minutes(), 8.0)

    def test_高频工具带改道指令(self):
        import time
        from types import SimpleNamespace

        from acps_cluster.agents import supervisor as s

        s._RUN_STARTED_AT.set(time.monotonic() - 9 * 60)
        try:
            async def handler(tool_call):  # noqa: ANN001
                return {"ok": True}

            out = asyncio.run(s.BudgetClockHooks().wrap_tool_call(
                None, SimpleNamespace(name="plan_gate", arguments={}), handler))
        finally:
            s._RUN_STARTED_AT.set(None)
        self.assertIn("budget_note", out)
        self.assertIn("立即改道作业化", out["budget_note"])
        self.assertIn("已超出", out["budget_note"])

    def test_预算内带剩余秒数(self):
        import time
        from types import SimpleNamespace

        from acps_cluster.agents import supervisor as s

        s._RUN_STARTED_AT.set(time.monotonic() - 60)
        try:
            async def handler(tool_call):  # noqa: ANN001
                return {"ok": True}

            out = asyncio.run(s.BudgetClockHooks().wrap_tool_call(
                None, SimpleNamespace(name="product_gate", arguments={}), handler))
        finally:
            s._RUN_STARTED_AT.set(None)
        self.assertIn("剩余约", out["budget_note"])
        self.assertNotIn("立即改道作业化", out["budget_note"])

    def test_非白名单工具不追加(self):
        from types import SimpleNamespace

        from acps_cluster.agents import supervisor as s

        async def handler(tool_call):  # noqa: ANN001
            return {"ok": True}

        out = asyncio.run(s.BudgetClockHooks().wrap_tool_call(
            None, SimpleNamespace(name="acps_discover", arguments={}), handler))
        self.assertNotIn("budget_note", out)

    def test_作业模式不追加改道提示(self):
        import time
        from unittest import mock

        from acps_cluster.agents import supervisor as s

        s._RUN_STARTED_AT.set(time.monotonic() - 60)
        try:
            with mock.patch.dict(os.environ,
                                 {"ACPS_JOB_REPORT_OUT": "/tmp/job/report.md"}):
                self.assertEqual(s._elapsed_budget_note(), "")
        finally:
            s._RUN_STARTED_AT.set(None)


class JobLifecycleFenceTest(unittest.TestCase):
    """受理后必须收尾 + 超预算必须改道（2026-10-17 现网 follow-up 判红）。"""

    JOB = {"job_id": "job-20261017-113903-df44", "mode": "custom"}

    def _call(self, name, *, elapsed_minutes=0.0, job=None, job_mode=False):
        import time
        from types import SimpleNamespace
        from unittest import mock

        from acps_cluster.agents import supervisor as s

        async def handler(tool_call):  # noqa: ANN001
            return {"ok": True, "handler": "called"}

        async def go():
            env = {"ACPS_JOB_REPORT_OUT": "/tmp/job/report.md"} if job_mode else {}
            with mock.patch.dict(os.environ, env, clear=False):
                if not job_mode:
                    os.environ.pop("ACPS_JOB_REPORT_OUT", None)
                s._JOB_RECEIPTS.set([job] if job else [])
                s._RUN_STARTED_AT.set(time.monotonic() - elapsed_minutes * 60)
                try:
                    return await s.JobLifecycleFenceHooks().wrap_tool_call(
                        None, SimpleNamespace(name=name, arguments={}), handler)
                finally:
                    s._RUN_STARTED_AT.set(None)
                    s._JOB_RECEIPTS.set(None)

        return asyncio.run(go())

    def test_受理后禁止继续调用工具(self):
        out = self._call("acps_delegate_public", job=self.JOB)
        self.assertFalse(out["ok"])
        self.assertIn("已受理后台作业", out["error"])
        self.assertIn(self.JOB["job_id"], out["error"])
        self.assertIn("aip_submit", out["error"])

    def test_受理后aip_submit放行(self):
        out = self._call("aip_submit", job=self.JOB)
        self.assertTrue(out["ok"])
        self.assertEqual(out.get("handler"), "called")

    def test_超预算阻止继续等待(self):
        out = self._call("wait_many", elapsed_minutes=9)
        self.assertFalse(out["ok"])
        self.assertIn("超出预算", out["error"])
        self.assertIn("submit_job", out["error"])

    def test_超预算允许改道与收尾工具(self):
        self.assertTrue(self._call("submit_job", elapsed_minutes=9)["ok"])
        self.assertTrue(self._call("aip_submit", elapsed_minutes=9)["ok"])
        self.assertTrue(self._call("aip_fail", elapsed_minutes=9)["ok"])

    def test_预算内透传(self):
        out = self._call("wait_many", elapsed_minutes=1)
        self.assertTrue(out["ok"])
        self.assertEqual(out.get("handler"), "called")

    def test_job模式不受预算闸影响(self):
        out = self._call("acps_delegate_public", elapsed_minutes=9, job_mode=True)
        self.assertTrue(out["ok"])
        self.assertEqual(out.get("handler"), "called")


if __name__ == "__main__":
    unittest.main()
