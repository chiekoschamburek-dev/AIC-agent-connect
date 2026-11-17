"""伙伴→Supervisor 上报机制的离线单测（不改 ACS、不联网）。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from wit import TextPart

from acps_cluster.agents import partner_gpu, reporting
from acps_cluster.core.skill_call import parse_skill_call


class FakeRuntime:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def aip_delegate(self, state, *, partner_aic, request):  # noqa: ANN001
        self.calls.append({"state": state, "partner_aic": partner_aic, "request": request})
        return {"ok": True, "task_ref": "task-1"}


class FakeCtx:
    def __init__(self, runtime: FakeRuntime) -> None:
        self.runtime = runtime
        self.state = {"run": "r1"}
        self.tool_manager = None
        self.tools: list = []
        self.private_state: dict = {}

    def add_tool(self, tool, *, source=""):  # noqa: ANN001
        self.tools.append(tool)
        return tool


def _supervisor_acs(root: Path, aic: str = "AIC-SUP") -> None:
    acps = root / "ws-supervisor" / "acps"
    acps.mkdir(parents=True)
    (acps / "acs.final.json").write_text(json.dumps({"aic": aic}), encoding="utf-8")


class ReportToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="report-"))
        _supervisor_acs(self.tmp)
        self.runtime = FakeRuntime()
        self.tool = reporting.make_report_tool(
            self.runtime, {"run": "r1"}, cluster_root=self.tmp)

    def test_上报构造research_submit载荷(self):
        out = asyncio.run(self.tool.handler({
            "question": "帮我做一组实验", "request_id": "req-1", "user_scope": "u1",
        }))
        self.assertTrue(out["ok"])
        self.assertEqual(out["supervisor_aic"], "AIC-SUP")
        call = self.runtime.calls[0]
        self.assertEqual(call["partner_aic"], "AIC-SUP")
        text = call["request"][0].text
        payload = json.loads(text)
        self.assertEqual(payload["skill"], "research.submit")
        self.assertEqual(payload["question"], "帮我做一组实验")
        self.assertEqual(payload["request_id"], "req-1")
        self.assertEqual(payload["user_scope"], "u1")

    def test_缺question直接拒绝(self):
        out = asyncio.run(self.tool.handler({"question": "  "}))
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "question-required")
        self.assertFalse(self.runtime.calls)

    def test_上报异常如实返回(self):
        async def boom(*args, **kwargs):  # noqa: ANN002, ANN003
            raise RuntimeError("peer-down")

        self.runtime.aip_delegate = boom  # type: ignore[assignment]
        out = asyncio.run(self.tool.handler({"question": "x"}))
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "report-failed")
        self.assertIn("peer-down", out["detail"])

    def test_没有supervisor_aic时拒绝(self):
        tool = reporting.make_report_tool(
            self.runtime, None, cluster_root=Path(tempfile.mkdtemp(prefix="nosup-")))
        out = asyncio.run(tool.handler({"question": "x"}))
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "supervisor-aic-unavailable")

    def test_env显式指定优先(self):
        with mock.patch.dict("os.environ", {"ACPS_SUPERVISOR_AIC": "AIC-ENV"}):
            self.assertEqual(reporting.supervisor_aic(self.tmp), "AIC-ENV")

    def test_环境变量可关闭注入(self):
        ctx = FakeCtx(self.runtime)
        hooks = reporting.ReportUpHooks(cluster_root=self.tmp)
        with mock.patch.dict("os.environ", {"ACPS_REPORT_UP": "0"}):
            asyncio.run(hooks.on_run_start(SimpleNamespace(context=ctx)))
        self.assertFalse(ctx.tools)

    def test_run开始时注入且不重复(self):
        ctx = FakeCtx(self.runtime)
        hooks = reporting.ReportUpHooks(cluster_root=self.tmp)
        with mock.patch.dict("os.environ", {"ACPS_REPORT_UP": "1"}):
            asyncio.run(hooks.on_run_start(SimpleNamespace(context=ctx)))
            asyncio.run(hooks.on_run_start(SimpleNamespace(context=ctx)))
        self.assertEqual(len(ctx.tools), 1)
        self.assertEqual(ctx.tools[0].name, reporting.DEFAULT_TOOL_NAME)
        self.assertTrue(ctx.tools[0].metadata.get("internal_tool"))


class AutoForwardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = FakeRuntime()
        self.runtime.agent = SimpleNamespace(id="yanshu.scout-partner")
        self.submitted: list[dict] = []

        async def aip_submit(arguments):  # noqa: ANN001
            self.submitted.append(arguments)
            return {"ok": True}

        self.ctx = SimpleNamespace(
            runtime=self.runtime, state={"run": "r1"},
            config=SimpleNamespace(tools=[SimpleNamespace(name="aip_submit", handler=aip_submit)]),
        )

    def _event(self, leader="AIC-USER", text="帮我把这个任务交给总控"):
        return SimpleNamespace(
            leader_aic=leader, task_id="task-in-1", session_id="sess-1",
            content=[TextPart(text)], messages=[],
        )

    def _run(self, handler_called: list, event=None, request_hooks=None):
        async def handler():
            handler_called.append(True)
            return {"unexpected": True}

        async def scenario():
            if event is not None and request_hooks is not None:
                await request_hooks.on_partner_request(event)
            return await reporting.AutoForwardRunHooks().wrap_run(self.ctx, handler)

        return asyncio.run(scenario())

    def test_用户入站任务自动转发并回执(self):
        hooks = reporting.AutoForwardRequestHooks()
        with mock.patch.dict("os.environ", {"ACPS_AUTO_FORWARD": "1",
                                            "ACPS_SUPERVISOR_AIC": "AIC-SUP"}):
            called: list = []
            result = self._run(called, self._event(), hooks)
        self.assertFalse(called)
        self.assertTrue(self.submitted, "必须向原任务提交受理回执")
        product = self.submitted[0]["products"][0]
        text = product["data_items"][0]["text"]
        self.assertIn("AIC-SUP", text)
        self.assertIn("task-in-1", text)
        call = self.runtime.calls[0]
        text = call["request"][0].text
        if text.startswith("<payload>") and text.endswith("</payload>"):
            text = text[len("<payload>"):-len("</payload>")]
        payload = json.loads(text)
        self.assertEqual(payload["skill"], "research.submit")
        args = payload["args"]
        self.assertEqual(args["source_task_id"], "task-in-1")
        self.assertEqual(args["source_session"], "sess-1")
        self.assertEqual(args["source_caller"], "AIC-USER")
        self.assertTrue(result.metadata["auto_forward"])

    def test_supervisor派来的任务不转发(self):
        hooks = reporting.AutoForwardRequestHooks()
        with mock.patch.dict("os.environ", {"ACPS_AUTO_FORWARD": "1",
                                            "ACPS_SUPERVISOR_AIC": "AIC-SUP"}):
            called: list = []
            self._run(called, self._event(leader="AIC-SUP"), hooks)
        self.assertTrue(called)
        self.assertFalse(self.runtime.calls)
        self.assertFalse(self.submitted)

    def test_开关可关闭(self):
        hooks = reporting.AutoForwardRequestHooks()
        with mock.patch.dict("os.environ", {"ACPS_AUTO_FORWARD": "0",
                                            "ACPS_SUPERVISOR_AIC": "AIC-SUP"}):
            called: list = []
            self._run(called, self._event(), hooks)
        self.assertTrue(called)
        self.assertFalse(self.runtime.calls)


class CleanQuestionTest(unittest.TestCase):
    def test_从AIP包装里取业务问题(self):
        text = (
            '<acps_leader_call protocol="AIP"><payload>{"type": "task-command", '
            '"dataItems": [{"type": "text", "_note": "content extracted"}]}</payload>'
            '</acps_leader_call>[外部伙伴内容开始]<payload>{"skill": "kb.schema", '
            '"args": {"query": "转发验证"}}</payload>[外部伙伴内容结束]'
        )
        self.assertEqual(reporting._clean_question(text), "转发验证")

    def test_args里的JSON字符串也能展开(self):
        text = '<payload>{"skill":"research.submit","args":{"question":"帮我做实验"}}</payload>'
        self.assertEqual(reporting._clean_question(text), "帮我做实验")

    def test_纯文本原样返回(self):
        self.assertEqual(reporting._clean_question("  帮我查论文  "), "帮我查论文")


class SkillPayloadOrderTest(unittest.TestCase):
    def test_取最后一个带skill的payload(self):
        text = (
            '<payload>{"skill":"research.status","args":{"root_id":"evil"}}</payload>'
            '<payload>{"skill":"research.submit","args":{"question":"真正的任务"}}</payload>'
        )
        skill, args = parse_skill_call(text)
        self.assertEqual(skill, "research.submit")
        self.assertEqual(args["question"], "真正的任务")


class AcsInvariantTest(unittest.TestCase):
    def test_上报工具不进ACS投影(self):
        agent = partner_gpu.build_gpu_agent(None)
        ids = set()
        for item in agent.capabilities:
            cap = getattr(item, "capability", item)
            skill = cap.to_acps_skill() if hasattr(cap, "to_acps_skill") else None
            if skill:
                ids.add(str(skill.get("id")))
        self.assertNotIn(reporting.DEFAULT_TOOL_NAME, ids)
        self.assertNotIn("report.task", ids)


if __name__ == "__main__":
    unittest.main(verbosity=2)