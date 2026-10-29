"""RunGuardHooks：运行中守卫的规则与中止行为（离线，不联网）。"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

CL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CL))
sys.path.insert(0, str(CL.parent))
os.environ.setdefault("OPENAI_API_KEY", "sk-test")

from acps_cluster.agents import supervisor  # noqa: E402


def _state(*, started_ago=0.0, last_activity_ago=0.0, last_progress_ago=None, **kw):
    now = time.monotonic()
    st = {
        "started": now - started_ago,
        "last_activity": now - last_activity_ago,
        "last_progress": now - (started_ago if last_progress_ago is None else last_progress_ago),
        "progress_states": {},
        "steps": 0,
        "errors": 0,
        "repeat": {},
        "waits": {},
        "abort": None,
        "submitted": False,
    }
    st.update(kw)
    return st


class RunGuardRuleTest(unittest.TestCase):
    def setUp(self):
        supervisor._RUN_GUARD_ABORT.set(None)
        self.guard = supervisor.RunGuardHooks()
        self._patch = mock.patch.object(supervisor.RunGuardHooks, "_audit", lambda *a, **k: None)
        self._patch.start()
        self._trace = mock.patch.object(
            supervisor.SupervisorRunHooks, "_trace_write", lambda *a, **k: None)
        self._trace.start()

    def tearDown(self):
        self._patch.stop()
        self._trace.stop()
        supervisor._RUN_GUARD_ABORT.set(None)

    def test_墙钟超限触发中止(self):
        st = _state(started_ago=100)
        with mock.patch.dict(os.environ, {"ACPS_GUARD_WALL_SECONDS": "10"}), \
             mock.patch.object(supervisor, "_run_guard_mode", return_value="enforce"):
            supervisor.RunGuardHooks._STATE.set(st)
            self.guard._check(st)
        self.assertEqual(st["abort"]["rule"], "wall-clock")
        self.assertEqual(supervisor._RUN_GUARD_ABORT.get()["rule"], "wall-clock")

    def test_无活动超限触发中止(self):
        st = _state(last_activity_ago=100)
        with mock.patch.dict(os.environ, {"ACPS_GUARD_IDLE_SECONDS": "10"}), \
             mock.patch.object(supervisor, "_run_guard_mode", return_value="enforce"):
            self.guard._check(st)
        self.assertEqual(st["abort"]["rule"], "no-activity")

    def test_重复调用超限触发中止(self):
        st = _state(repeat={"acps_delegate_public|{}": 12})
        with mock.patch.dict(os.environ, {"ACPS_GUARD_REPEAT_LIMIT": "12"}), \
             mock.patch.object(supervisor, "_run_guard_mode", return_value="enforce"):
            self.guard._check(st)
        self.assertEqual(st["abort"]["rule"], "repeat-loop")

    def test_进度停滞触发中止(self):
        st = _state(last_progress_ago=100)
        with mock.patch.dict(os.environ, {"ACPS_GUARD_PROGRESS_SECONDS": "10"}), \
             mock.patch.object(supervisor, "_run_guard_mode", return_value="enforce"):
            self.guard._check(st)
        self.assertEqual(st["abort"]["rule"], "progress-stall")
    def test_写入成功算进度(self):
        import time as _time

        st = _state(last_progress_ago=100, progress_states={})
        old = st["last_progress"]
        supervisor.RunGuardHooks._STATE.set(st)
        event = SimpleNamespace(
            tool_call=SimpleNamespace(name="ws.write", arguments={}),
            response={"status": "success", "result": {}},
        )
        asyncio.run(supervisor.RunGuardHooks().on_tool_response(event))
        self.assertGreater(st["last_progress"], old)

    def test_等待状态变化算进度(self):
        st = _state(last_progress_ago=100, progress_states={"task_1": "working"})
        old = st["last_progress"]
        supervisor.RunGuardHooks._STATE.set(st)
        event = SimpleNamespace(
            tool_call=SimpleNamespace(name="wait_partner", arguments={"task_ref": "task_1"}),
            response={"status": "success", "result": {"state": "awaiting-completion"}},
        )
        asyncio.run(supervisor.RunGuardHooks().on_tool_response(event))
        self.assertGreater(st["last_progress"], old)
        self.assertEqual(st["progress_states"]["task_1"], "awaiting-completion")

    def test_观察模式不设置中止标记(self):
        st = _state(started_ago=100)
        with mock.patch.dict(os.environ, {"ACPS_GUARD_WALL_SECONDS": "10"}), \
             mock.patch.object(supervisor, "_run_guard_mode", return_value="observe"):
            self.guard._check(st)
        self.assertTrue(st["abort"])                      # 记录异常
        self.assertIsNone(supervisor._RUN_GUARD_ABORT.get())  # 但不阻断


class RunGuardWrapTest(unittest.TestCase):
    def setUp(self):
        self.guard = supervisor.RunGuardHooks()
        supervisor.RunGuardHooks._STATE.set(_state())

    def tearDown(self):
        supervisor._RUN_GUARD_ABORT.set(None)
        supervisor.RunGuardHooks._STATE.set(None)

    def test_enforce下只放行aip_fail(self):
        called = []

        async def handler(tc):
            called.append(tc.name)
            return {"ok": True}

        async def run():
            supervisor._RUN_GUARD_ABORT.set({"rule": "wall-clock", "reason": "too long"})
            with mock.patch.object(supervisor, "_run_guard_mode", return_value="enforce"):
                blocked = await self.guard.wrap_tool_call(
                    None, SimpleNamespace(name="acps_delegate_public"), handler)
                allowed = await self.guard.wrap_tool_call(
                    None, SimpleNamespace(name="aip_fail"), handler)
            return blocked, allowed

        blocked, allowed = asyncio.run(run())
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["error"], "run-guard-abort")
        self.assertEqual(blocked["rule"], "wall-clock")
        self.assertTrue(allowed["ok"])
        self.assertEqual(called, ["aip_fail"])

    def test_observe不阻断工具(self):
        async def handler(tc):
            return {"ok": True}

        async def run():
            supervisor._RUN_GUARD_ABORT.set({"rule": "x", "reason": "y"})
            with mock.patch.object(supervisor, "_run_guard_mode", return_value="observe"):
                return await self.guard.wrap_tool_call(
                    None, SimpleNamespace(name="acps_delegate_public"), handler)

        self.assertTrue(asyncio.run(run())["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
