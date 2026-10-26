"""partner 侧工作区客户端单测：bundle 取令牌 → 直连 workspace → 等产物。

覆盖：
1. 三个模型工具不进 ACS（acps_skill=False / internal_tool）；
2. 有 bundle 时按 skill 取令牌、task_id 固定来自 bundle，模型不能改作用域；
3. 无 bundle / 过期 / 无 bridge → 结构化 awaiting，不发请求；
4. 文件通道兜底（ContextVar 不复位也能读）；
5. 带 bundle 的入站任务抬升步数预算。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

CL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CL))
sys.path.insert(0, str(CL.parent))
os.environ["YANSHU_RUN_TRACE"] = "0"

from acps_cluster.delegation.bridge import BridgeHooks, set_delegation_bundle  # noqa: E402
from acps_cluster.delegation.client import set_current_task, write_bundle_file  # noqa: E402
from acps_cluster.agents.partner_workspace_client import (  # noqa: E402
    WorkspaceAccessBudgetHooks,
    build_workspace_access_capabilities,
    ws_read,
    ws_write,
)
from acps_cluster.core.workspace_access import enabled as ws_enabled  # noqa: E402

WS_AIC = "1.2.156.3088.1.1.WORKSPACE.TEST"
TASK = "task-ws-1"
PRODUCT_TEXT = json.dumps({"ok": True, "path": "repo/x/README.md", "content": "hello"},
                          ensure_ascii=False)


class _RT:
    def __init__(self, *, initial=None, after_get=None):
        self.calls: list = []
        self.actions: list[str] = []
        self.complete_calls = 0
        self.initial = initial or {"task_ref": "t-1", "state": "awaiting-completion",
                                   "products": []}
        self.after_get = after_get or {"state": "awaiting-completion", "products": []}

    async def aip_delegate(self, state, *, partner_aic, request):
        self.calls.append((partner_aic, list(request)))
        return dict(self.initial)

    async def aip_task(self, state, *, action, task_ref=None, input=None, offset=0):
        self.actions.append(action)
        if action == "complete":
            self.complete_calls += 1
            return {"state": "completed",
                    "products": [{"data_items": [{"type": "text", "text": PRODUCT_TEXT}]}]}
        return dict(self.after_get)


def _entry(skill: str, *, exp: int | None = None, task: str = TASK) -> dict:
    return {
        "token": f"JWS-{skill}",
        "targetAic": WS_AIC,
        "skillId": skill,
        "taskId": task,
        "exp": int(time.time()) + 300 if exp is None else exp,
    }


def _go(rt, coro_fn):
    """在同一个 asyncio 上下文里先挂 BridgeHooks，再执行客户端调用。"""
    async def go():
        await BridgeHooks().on_run_start(SimpleNamespace(
            context=SimpleNamespace(runtime=rt, state=object())))
        return await coro_fn()

    return asyncio.run(go())


class WorkspaceToolsShapeTest(unittest.TestCase):
    def test_三个工具不进ACS且模型名为ws下划线(self):
        with tempfile.TemporaryDirectory() as td:
            caps = build_workspace_access_capabilities(td)
        ids = [c.capability.id for c in caps]
        self.assertEqual(ids, ["ws.list", "ws.read", "ws.write"])
        for cap in (c.capability for c in caps):
            self.assertFalse(cap.public)
            self.assertFalse(cap.acps_skill)
            self.assertTrue(cap.metadata.get("internal_tool"))
            self.assertIsNone(cap.to_acps_skill())
        from wit import ToolManager

        tm = ToolManager()
        tm.add_capabilities([c.capability for c in caps])
        self.assertEqual({t.name for t in tm.tools}, {"ws_list", "ws_read", "ws_write"})

    def test_开关按名册key(self):
        with mock.patch.dict(os.environ, {"ACPS_WS_PARTNER_ACCESS": "scout,writer"}):
            self.assertTrue(ws_enabled("scout"))
            self.assertFalse(ws_enabled("critic"))
        with mock.patch.dict(os.environ, {"ACPS_WS_PARTNER_ACCESS": ""}):
            self.assertFalse(ws_enabled("scout"), "空串 = 全部关闭")


class WorkspaceClientCallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        set_delegation_bundle(None)
        set_current_task(None)

    def test_读文件走bundle且task_id固定(self):
        set_delegation_bundle({"ws.read": _entry("ws.read")})
        rt = _RT()

        async def call():
            return await ws_read(self.dir, path="repo/x/README.md")

        out = self._run(rt, call)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["result"]["content"], "hello")
        self.assertEqual(out["taskId"], TASK)
        self.assertEqual(len(rt.calls), 1)
        partner, parts = rt.calls[0]
        self.assertEqual(partner, WS_AIC)
        self.assertIn(f"task_id={TASK}", parts[0].text)
        self.assertIn("ws.read", parts[0].text)
        self.assertEqual(parts[1].data.get("targetSkillHint"), "ws.read")
        self.assertEqual(parts[1].data.get("acpsDelegationToken"), "JWS-ws.read")
        self.assertEqual(rt.complete_calls, 1, "awaiting-completion 必须由调用方 complete")

    def test_无bundle返回awaiting不发请求(self):
        rt = _RT()

        async def call():
            return await ws_read(self.dir, path="x")

        out = self._run(rt, call)
        self.assertFalse(out["ok"])
        self.assertIn("awaiting", out)
        self.assertEqual(rt.calls, [])

    def test_过期令牌返回awaiting(self):
        set_delegation_bundle({"ws.read": _entry("ws.read", exp=int(time.time()) - 600)})
        rt = _RT()

        async def call():
            return await ws_read(self.dir, path="x")

        out = self._run(rt, call)
        self.assertFalse(out["ok"])
        self.assertIn("过期", out["awaiting"]["reason"])
        self.assertEqual(rt.calls, [])

    def test_文件通道兜底(self):
        set_current_task(TASK)
        write_bundle_file(self.dir, TASK, {"ws.read": _entry("ws.read")})
        rt = _RT()

        async def call():
            return await ws_read(self.dir, path="x")

        out = self._run(rt, call)
        self.assertTrue(out["ok"], out)
        self.assertEqual(len(rt.calls), 1)

    def test_等待用refresh再complete(self):
        set_delegation_bundle({"ws.read": _entry("ws.read")})
        rt = _RT(initial={"task_ref": "t-1", "state": "working", "products": []},
                 after_get={"state": "awaiting-completion", "products": []})

        async def call():
            return await ws_read(self.dir, path="x")

        out = self._run(rt, call)
        self.assertTrue(out["ok"], out)
        self.assertEqual(rt.complete_calls, 1)
        # wit 的 aip_task 只接受 refresh/continue/complete/cancel；`get` 会抛
        # AipError: Unsupported task action（2026-10-26 服务器 e2e 实测）
        self.assertIn("refresh", rt.actions, rt.actions)
        self.assertNotIn("get", rt.actions, rt.actions)

    def test_写入超限直接拒绝(self):
        set_delegation_bundle({"ws.write": _entry("ws.write")})
        rt = _RT()

        async def call():
            return await ws_write(self.dir, path="scratch/x.md", content="x" * 300000)

        out = self._run(rt, call)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "content-too-large")
        self.assertEqual(rt.calls, [])

    def test_无bridge返回结构化错误(self):
        set_delegation_bundle({"ws.read": _entry("ws.read")})

        async def call():
            return await ws_read(self.dir, path="x")

        out = asyncio.run(call())
        self.assertFalse(out["ok"])
        self.assertEqual(out.get("error"), "bridge-not-ready")

    def _run(self, rt, coro_fn):
        return _go(rt, coro_fn)


class BudgetHookTest(unittest.TestCase):
    def test_带bundle抬升步数(self):
        from wit import RunConfig

        cfg = RunConfig()
        set_delegation_bundle({"ws.read": _entry("ws.read")})
        self.addCleanup(set_delegation_bundle, None)
        asyncio.run(WorkspaceAccessBudgetHooks(tempfile.gettempdir()).on_run_start(
            SimpleNamespace(context=SimpleNamespace(config=cfg))))
        self.assertGreaterEqual(cfg.max_steps, 16)

    def test_无bundle不动预算(self):
        from wit import RunConfig

        cfg = RunConfig()
        before = cfg.max_steps
        set_delegation_bundle(None)
        asyncio.run(WorkspaceAccessBudgetHooks(tempfile.gettempdir()).on_run_start(
            SimpleNamespace(context=SimpleNamespace(config=cfg))))
        self.assertEqual(cfg.max_steps, before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
