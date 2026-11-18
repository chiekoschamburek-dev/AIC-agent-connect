"""伙伴→Supervisor 上报机制的离线单测（不改 ACS、不联网）。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from wit import TextPart, WitAgent, WitRuntime
from wit.acps.aip import content_input_to_data_items
from wit.acps.direct import _task_command
from wit.acps.task_runtime import (
    AipPartnerExecutionContext, AipPartnerExecutor, acps_leader_call_xml,
    sanitize_command_for_prompt,
)
from wit.kernel.runtime import AgentState

from acps_cluster.agents import partner_gpu, reporting
from acps_cluster.core.aip_guard import AipPartnerGuardHooks
from acps_cluster.core.cluster_common import build_local_acps_config
from acps_cluster.core.skill_call import parse_skill_call


class FakeRuntime:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.actions: list[str] = []
        self.receipt = {"ok": True, "root_id": "rt-one", "job_id": "job-one", "state": "accepted"}

    async def aip_discover(self, query, *, limit):  # noqa: ANN001
        return {}

    async def aip_delegate(self, state, *, partner_aic, request):  # noqa: ANN001
        self.calls.append({"state": state, "partner_aic": partner_aic, "request": request})
        return {"state": "working", "task_ref": "task-1"}

    async def aip_task(self, state, *, action, task_ref):  # noqa: ANN001
        self.actions.append(action)
        if action == "complete":
            return {"state": "completed", "task_ref": task_ref}
        if self.receipt is None:
            return {"state": "working", "task_ref": task_ref}
        return {"state": "awaiting-completion", "task_ref": task_ref, "products": [{
            "data_items": [{"type": "text", "text": reporting.AUTO_SUBMIT_PREFIX + json.dumps(self.receipt)}],
        }]}


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
        item, = content_input_to_data_items(call["request"])
        self.assertEqual(item["metadata"]["skillId"], "research.submit")
        command, parts = sanitize_command_for_prompt({"type": "task-command", "dataItems": [item]})
        shell = acps_leader_call_xml(command="start", leader_aic="AIC-USER", partner_aic="AIC-SUP",
                                    session_id="sess-1", task_id="task-1",
                                    payload=json.dumps(command), mode="rpc")
        skill, args = parse_skill_call(shell + "\n" + parts[0].text)
        self.assertEqual(skill, "research.submit")
        self.assertEqual(args, {"question": "帮我做一组实验", "request_id": "req-1", "user_scope": "u1"})

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
        self.ctx = SimpleNamespace(
            runtime=self.runtime, state={"run": "r1"},
        )

    def _event(self, leader="AIC-USER", text="帮我把这个任务交给总控", *, verified=True):
        return SimpleNamespace(
            leader_aic=leader, task_id="task-in-1", session_id="sess-1",
            content=[TextPart(text)], messages=[], peer_verified=verified,
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
        receipt = json.loads(result.final_text)
        self.assertTrue(receipt["accepted"])
        self.assertEqual(receipt["job_id"], "job-one")
        self.assertEqual(receipt["source_task_id"], "task-in-1")
        self.assertEqual(self.runtime.actions, ["wait", "complete"])
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

    def test_业务拒绝保留失败原因(self):
        self.runtime.receipt = {"ok": False, "error": "request-conflict", "detail": "幂等键冲突"}
        with mock.patch.dict("os.environ", {"ACPS_SUPERVISOR_AIC": "AIC-SUP"}):
            result = self._run([], self._event(), reporting.AutoForwardRequestHooks())
        receipt = json.loads(result.final_text)
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["error"], "request-conflict")
        self.assertNotIn("job_id", receipt)

    def test_只有传输任务号时没有受理回执(self):
        self.runtime.receipt = None
        with mock.patch.dict("os.environ", {"ACPS_SUPERVISOR_AIC": "AIC-SUP"}):
            result = self._run([], self._event(), reporting.AutoForwardRequestHooks())
        receipt = json.loads(result.final_text)
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["error"], "acceptance-unconfirmed")
        self.assertEqual(receipt["supervisor_task_ref"], "task-1")
        self.assertEqual(self.runtime.actions, ["wait"])

    def test_转发超时返回未知态而不是失败(self):
        async def slow_delegate(state, *, partner_aic, request):  # noqa: ANN001
            await asyncio.sleep(1)
            return {"state": "working", "task_ref": "task-1"}

        self.runtime.aip_delegate = slow_delegate
        with mock.patch.dict("os.environ", {
                "ACPS_SUPERVISOR_AIC": "AIC-SUP",
                "ACPS_FORWARD_DELEGATE_TIMEOUT": "0.05"}):
            result = self._run([], self._event(), reporting.AutoForwardRequestHooks())
        receipt = json.loads(result.final_text)
        self.assertFalse(receipt["ok"])
        self.assertFalse(receipt["accepted"])
        self.assertEqual(receipt["error"], "acceptance-unconfirmed")
        self.assertNotIn("job_id", receipt)

    def test_未验证调用方不转发(self):
        with mock.patch.dict("os.environ", {"ACPS_SUPERVISOR_AIC": "AIC-SUP"}):
            called: list = []
            self._run(called, self._event(verified=False), reporting.AutoForwardRequestHooks())
        self.assertTrue(called)
        self.assertFalse(self.runtime.calls)

    def test_WIT执行器提交一次原任务产物(self):
        class UnusedModel:
            async def complete(self, request):
                raise AssertionError("自动转发不能调用模型")

        state = AgentState()
        task = {
            "state": "accepted", "taskId": "task-in-1", "taskRef": "task-in-1",
            "sessionId": "sess-1", "products": [],
        }

        def transition(task_ref, target, **fields):
            task.update(state=target, **fields)
            return task

        async def scenario(workspace):
            runtime = WitRuntime(WitAgent(
                id="yanshu.test-partner", instructions="Local AIP test", model=UnusedModel(),
                hooks=[reporting.AutoForwardRunHooks()],
                aip_hooks=[AipPartnerGuardHooks(), reporting.AutoForwardRequestHooks()],
            ), workspace=workspace)
            runtime.aip_delegate = self.runtime.aip_delegate
            runtime.aip_task = self.runtime.aip_task
            runtime.aip_discover = self.runtime.aip_discover
            store = SimpleNamespace(
                get=lambda _: dict(task), load_agent_state=lambda _: state,
                set_local_error=mock.Mock(), save_agent_state=mock.Mock(),
            )
            executor = AipPartnerExecutor(store=store, runtime=runtime, transition=transition)
            command = _task_command(
                command="start", sender_id="AIC-USER", task_id="task-in-1", session_id="sess-1",
                data_items=content_input_to_data_items([TextPart("请把这份研究任务交给总控。")]),
            )
            executor.schedule(
                "task-in-1", command,
                AipPartnerExecutionContext("AIC-USER", "AIC-ENTRY", "direct", "rpc", True),
            )
            await executor.wait()
            store.set_local_error.assert_not_called()

        with tempfile.TemporaryDirectory() as workspace, \
                mock.patch.dict("os.environ", {"ACPS_SUPERVISOR_AIC": "AIC-SUP"}):
            asyncio.run(scenario(workspace))
        self.assertEqual(task["state"], "awaiting-completion")
        product, = task["products"]
        self.assertEqual(json.loads(product["dataItems"][0]["text"])["job_id"], "job-one")

    def test_supervisor派来的任务不转发(self):
        hooks = reporting.AutoForwardRequestHooks()
        with mock.patch.dict("os.environ", {"ACPS_AUTO_FORWARD": "1",
                                            "ACPS_SUPERVISOR_AIC": "AIC-SUP"}):
            called: list = []
            self._run(called, self._event(leader="AIC-SUP"), hooks)
        self.assertTrue(called)
        self.assertFalse(self.runtime.calls)

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
        config = build_local_acps_config(8460)
        original = agent.compile(acps=config).acs_draft
        agent.hooks.extend([reporting.ReportingDelegationHooks(), reporting.AutoForwardRunHooks()])
        agent.aip_hooks.append(reporting.AutoForwardRequestHooks())
        with tempfile.TemporaryDirectory() as workspace:
            runtime = WitRuntime(agent, workspace=workspace, acps=config)
            self.assertEqual(runtime.acs_draft(), original)
            self.assertIsInstance(runtime._aip_delegation_hook(), reporting.ReportingDelegationHooks)
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
