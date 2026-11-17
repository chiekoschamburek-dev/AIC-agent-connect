"""多入口编排 P1 的离线单测：根任务台账 / 受理服务 / 入口能力 / ACS 不变量。

不联网、不起后台作业：
- `RootStore`：sqlite 幂等键、状态机、绑定作业；
- `root_service`：受理（含重放与崩溃恢复）、状态映射、终稿取件、访问控制；
- `root_entry`（需要 wit）：未验证调用方拒绝、受理-查询闭环、入参面不含注入字段；
- **ACS 不变量**：新增两个非公开能力后，supervisor 的 skills 投影仍只有
  `research.orchestrate`（`acps_skill` 才是投影开关，`public` 只管 PEP 可见性）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from acps_cluster.runtime import jobs_store, root_service, root_store

try:  # pep 依赖 cryptography（CI/生产已装）；本地缺依赖时跳过这组用例，不让整个文件收集失败
    from acps_cluster.delegation.pep import (
        PepConfig,
        evaluate,
        load_pep_config,
        parse_inbound_skill_allow,
    )
    _PEP_IMPORT_ERROR = ""
except Exception as _exc:  # noqa: BLE001 - 缺依赖与导入错误都按"不可用"处理
    PepConfig = evaluate = load_pep_config = parse_inbound_skill_allow = None  # type: ignore[assignment]
    _PEP_IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"

ME = "1.2.156.3088.1.BUPT.VTF97R.JXN8O8.98DOO8.0EB6"
OTHER = "1.2.156.3088.1.BUPT.BFZ0FH.F2KZXJ.15ZZZG.19CN"
QUESTION = "分析这组实验数据，并结合相关论文解释结果"


def _tmpdir(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


class RootStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = _tmpdir("root-store-")
        self.store = root_store.RootStore(self.tmp / "root-tasks.sqlite3")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _submit(self, **kwargs):
        params = {
            "entry_aic": ME,
            "user_scope": "demo",
            "request_id": "req-001",
            "question": QUESTION,
            "mode": "custom",
        }
        params.update(kwargs)
        return self.store.submit(**params)

    def test_新建与幂等重放(self):
        first, created = self._submit()
        self.assertTrue(created)
        self.assertEqual(first.state, "accepted")
        self.assertEqual(first.entry_aic, ME)
        again, created2 = self._submit()
        self.assertFalse(created2)
        self.assertEqual(again.root_id, first.root_id)
        self.assertEqual(again.request_digest, first.request_digest)

    def test_同键不同内容冲突(self):
        self._submit()
        with self.assertRaises(root_store.RequestConflict):
            self._submit(question="换了一个完全不同的任务内容")

    def test_同键不同scope各自成任务(self):
        first, _ = self._submit()
        second, created = self._submit(user_scope="other")
        self.assertTrue(created)
        self.assertNotEqual(first.root_id, second.root_id)

    def test_source_task_id反查与唯一(self):
        first, _ = self._submit(source_task_id="task-1", source_caller=OTHER,
                                source_session="sess-1")
        self.assertEqual(first.source_caller, OTHER)
        self.assertEqual(first.source_session, "sess-1")
        found = self.store.get_by_source_task_id("task-1")
        self.assertIsNotNone(found)
        self.assertEqual(found.root_id, first.root_id)
        with self.assertRaises(root_store.RequestConflict):
            self._submit(request_id="req-002", source_task_id="task-1",
                         question="另一个完全不同的任务内容")

    def test_入参校验(self):
        with self.assertRaises(ValueError):
            self._submit(entry_aic="")
        with self.assertRaises(ValueError):
            self._submit(request_id="坏 id")
        with self.assertRaises(ValueError):
            self._submit(question="太短")
        with self.assertRaises(ValueError):
            self._submit(user_scope="a b")

    def test_绑定作业与拒绝改绑(self):
        task, _ = self._submit()
        bound = self.store.attach_job(task.root_id, "job-1")
        self.assertEqual(bound.job_id, "job-1")
        with self.assertRaises(ValueError):
            self.store.attach_job(task.root_id, "job-2")
        self.assertEqual(self.store.attach_job(task.root_id, "job-1").job_id, "job-1")

    def test_状态迁移(self):
        task, _ = self._submit()
        running = self.store.set_state(task.root_id, "running")
        self.assertEqual(running.state, "running")
        done = self.store.set_state(task.root_id, "completed", result_ref="job-1")
        self.assertEqual(done.state, "completed")
        self.assertTrue(done.terminal)
        with self.assertRaises(root_store.InvalidTransition):
            self.store.set_state(task.root_id, "running")

    def test_记录平台taskId(self):
        task, _ = self._submit()
        updated = self.store.set_platform_task(task.root_id, "task-platform-1")
        self.assertEqual(updated.platform_task_id, "task-platform-1")

    def test_列表按入口过滤(self):
        self._submit()
        self._submit(request_id="req-002", user_scope="other")
        self.assertEqual(len(self.store.list_recent()), 2)
        self.assertEqual(len(self.store.list_recent(entry_aic=ME)), 2)
        self.assertEqual(len(self.store.list_recent(entry_aic="nobody")), 0)


class RootServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = _tmpdir("root-service-")
        self.store = root_store.RootStore(self.tmp / "root-tasks.sqlite3")
        self.jobs_root = self.tmp / "jobs"
        self.jobs_root.mkdir()
        self.calls: list[tuple[str, str]] = []

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _submit_fn(self, ok: bool = True, job_id: str = "job-1"):
        def _fn(question: str, mode: str):
            self.calls.append((question, mode))
            if not ok:
                return {"ok": False, "error": "并发作业已达上限"}
            return {"ok": True, "job_id": job_id}
        return _fn

    def _submit(self, *, submit_fn=None, request_id: str = "req-001", **kwargs):
        return asyncio.run(root_service.submit_root(
            self.store,
            submit_fn or self._submit_fn(),
            entry_aic=ME,
            user_scope="demo",
            request_id=request_id,
            question=QUESTION,
            **kwargs,
        ))

    def _job(self, *, state: str = "running", report: str = "") -> str:
        job_id, job_dir = jobs_store.create_job(self.jobs_root, QUESTION, "custom")
        if state in {"running"}:
            jobs_store.update_status(job_dir, state="running", pid=os.getpid())
        elif state in {"done", "failed"}:
            jobs_store.update_status(job_dir, state=state, exit_code=0)
        if report:
            (job_dir / "report.md").write_text(report, encoding="utf-8")
        return job_id

    def test_受理成功(self):
        payload = self._submit()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["state"], "accepted")
        self.assertEqual(payload["job_id"], "job-1")
        self.assertFalse(payload["replayed"])
        self.assertEqual(len(self.calls), 1)

    def test_受理失败落账(self):
        payload = self._submit(submit_fn=self._submit_fn(ok=False))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "submit-failed")
        root = self.store.get(payload["root_id"])
        self.assertIsNotNone(root)
        self.assertEqual(root.state, "failed")
        self.assertIn("并发作业", root.error)

    def test_source_caller可按原任务查(self):
        payload = self._submit(source_task_id="task-1", source_caller=OTHER)
        status = root_service.status_root(
            self.store, source_task_id="task-1",
            caller_aic=OTHER, jobs_root=self.jobs_root,
        )
        self.assertTrue(status["ok"])
        self.assertEqual(status["root_id"], payload["root_id"])
        denied = root_service.status_root(
            self.store, source_task_id="task-1",
            caller_aic="1.2.156.3088.1.BUPT.0000.AAA00.AAAAAA.0000",
            jobs_root=self.jobs_root,
        )
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["error"], "forbidden")

    def test_重放不再拉起作业(self):
        first = self._submit()
        second = self._submit()
        self.assertTrue(second["replayed"])
        self.assertEqual(first["root_id"], second["root_id"])
        self.assertEqual(len(self.calls), 1, "重放不应再次调用 submit_fn")

    def test_崩溃恢复补拉起(self):
        # 模拟：台账已建、作业没绑上就崩了
        root, created = self.store.submit(
            entry_aic=ME, user_scope="demo", request_id="req-recover",
            question=QUESTION, mode="custom",
        )
        self.assertTrue(created)
        payload = self._submit(request_id="req-recover")
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["replayed"], "同 request_id 属于重放")
        self.assertTrue(payload["recovered"], "崩溃窗口下的重放应补拉起作业")
        self.assertEqual(payload["job_id"], "job-1")
        self.assertEqual(self.store.get(root.root_id).job_id, "job-1")

    def test_状态映射running与completed(self):
        payload = self._submit()
        job_id = self._job(state="running")
        # 手工把根任务改绑到真实作业
        with self.store._session() as con:  # noqa: SLF001 - 测试夹具
            con.execute("UPDATE root_tasks SET job_id=? WHERE root_id=?",
                        (job_id, payload["root_id"]))
        running = root_service.status_root(
            self.store, root_id=payload["root_id"], caller_aic=ME,
            jobs_root=self.jobs_root,
        )
        self.assertEqual(running["state"], "running")
        self.assertEqual(running["job_state"], "running")

        jobs_store.update_status(self.jobs_root / job_id, state="done", exit_code=0)
        (self.jobs_root / job_id / "report.md").write_text("终稿正文", encoding="utf-8")
        done = root_service.status_root(
            self.store, root_id=payload["root_id"], caller_aic=ME,
            jobs_root=self.jobs_root,
        )
        self.assertEqual(done["state"], "completed")
        self.assertTrue(done["report_available"])
        self.assertEqual(done["result"], "终稿正文")

    def test_按job_id反查状态(self):
        payload = self._submit()
        job_id = self._job(state="running")
        with self.store._session() as con:  # noqa: SLF001 - 测试夹具
            con.execute("UPDATE root_tasks SET job_id=? WHERE root_id=?",
                        (job_id, payload["root_id"]))
        status = root_service.status_root(
            self.store, job_id=job_id, caller_aic=ME, jobs_root=self.jobs_root,
        )
        self.assertTrue(status["ok"])
        self.assertEqual(status["root_id"], payload["root_id"])
        self.assertEqual(status["state"], "running")

    def test_受理期间上下文带root_id(self):
        seen = {}

        def fake(question: str, mode: str):
            seen["root"] = root_service.current_root_id()
            return {"ok": True, "job_id": "job-ctx"}

        asyncio.run(root_service.submit_root(
            self.store, fake, entry_aic=ME, user_scope="demo",
            request_id="req-ctx", question=QUESTION,
        ))
        self.assertTrue(seen["root"].startswith("rt-"))
        self.assertIsNone(root_service.current_root_id(), "调用结束后必须复位")

    def test_查询访问控制与未知任务(self):
        payload = self._submit()
        forbidden = root_service.status_root(
            self.store, root_id=payload["root_id"], caller_aic=OTHER,
            jobs_root=self.jobs_root,
        )
        self.assertFalse(forbidden["ok"])
        self.assertEqual(forbidden["error"], "forbidden")
        unknown = root_service.status_root(
            self.store, root_id="rt-nope", caller_aic=ME, jobs_root=self.jobs_root,
        )
        self.assertEqual(unknown["error"], "unknown-root")

    def test_作业丢失如实报告(self):
        payload = self._submit()
        status = root_service.status_root(
            self.store, root_id=payload["root_id"], caller_aic=ME,
            jobs_root=self.jobs_root,
        )
        self.assertEqual(status["job_state"], "")
        self.assertEqual(status["error"], "job-not-found")


@unittest.skipUnless(PepConfig is not None, f"pep 不可导入：{_PEP_IMPORT_ERROR}")
class PepInboundSkillAllowTest(unittest.TestCase):
    """按调用方 × 按技能的入站白名单：只放行指定技能，且不越过域治理。"""

    @staticmethod
    def _request(*, leader: str = ME, skill: str | None = "research.submit"):
        from types import SimpleNamespace

        params = {"skillId": skill} if skill else {}
        return SimpleNamespace(
            peer_verified=True,
            leader_aic=leader,
            partner_aic="supervisor.example",
            command_params=params,
            data_items=[],
            route=[],
            delivery=None,
        )

    def _cfg(self, **kwargs) -> PepConfig:
        base = {
            "enabled": True,
            "my_aic": "supervisor.example",
            "public_skills": frozenset({"research.orchestrate"}),
            "has_non_public_skills": True,
            "allow_unscoped_public_entry": False,
        }
        base.update(kwargs)
        return PepConfig(**base)

    def test_显式技能白名单放行(self):
        cfg = self._cfg(inbound_skill_allow={ME: frozenset({"research.submit"})})
        outcome = evaluate(self._request(), cfg)
        self.assertEqual((outcome.decision, outcome.rule), ("allow", "inbound-skill-allow"))
        self.assertEqual(outcome.skill_hint, "research.submit")

    def test_同调用方的其它技能不放行(self):
        cfg = self._cfg(inbound_skill_allow={ME: frozenset({"research.status"})})
        outcome = evaluate(self._request(), cfg)
        self.assertEqual((outcome.decision, outcome.rule), ("deny", "no-credential"))

    def test_未列入的调用方不放行(self):
        cfg = self._cfg(inbound_skill_allow={OTHER: frozenset({"research.submit"})})
        outcome = evaluate(self._request(), cfg)
        self.assertEqual((outcome.decision, outcome.rule), ("deny", "no-credential"))

    def test_域治理技能不走技能白名单(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "domains-missing"
            cfg = self._cfg(
                inbound_skill_allow={ME: frozenset({"kb.query"})},
                domain_skills={"kb.query": frozenset({"lab"})},
                domains_dir=missing,
            )
            outcome = evaluate(self._request(skill="kb.query"), cfg)
            self.assertEqual((outcome.decision, outcome.rule), ("deny", "domain-store-missing"))

    def test_解析器接受合法项并跳过坏项(self):
        parsed = parse_inbound_skill_allow(
            f"{ME}=research.submit,research.status; bad-entry ;=x;{OTHER}="
        )
        self.assertEqual(parsed[ME], frozenset({"research.submit", "research.status"}))
        self.assertNotIn("", parsed)
        self.assertNotIn(OTHER, parsed)

    def test_环境变量接入配置(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = load_pep_config(
                Path(td),
                public_skills={"research.orchestrate"},
                my_aic="supervisor.example",
                env={
                    "ACPS_SUPERVISOR_INBOUND_SKILL_ALLOW":
                        f"{ME}=research.submit,research.status",
                },
            )
            self.assertEqual(
                cfg.inbound_skill_allow[ME],
                frozenset({"research.submit", "research.status"}),
            )
            self.assertIn("inboundSkillAllow", cfg.describe())


@unittest.skipUnless(importlib.util.find_spec("wit") is not None, "需要 wit 运行时")
class EntryCapabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = _tmpdir("root-entry-")
        self.store = root_store.RootStore(self.tmp / "root.sqlite3")
        self.jobs_root = self.tmp / "jobs"
        self.jobs_root.mkdir()
        from acps_cluster.agents import root_entry  # noqa: PLC0415

        self.mod = root_entry
        root_entry.set_current_caller(None)

    def tearDown(self) -> None:
        self.mod.set_current_caller(None)
        self.mod.clear_entry_run()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_技能快速通道端到端(self):
        from unittest import mock

        from wit import TextPart  # noqa: PLC0415
        from wit.kernel.messages import Message, ModelRequest, ModelResponse  # noqa: PLC0415

        from acps_cluster.bidirectional.skill_entry import SkillFastPathProvider  # noqa: PLC0415

        self.mod.set_current_caller(ME)

        async def fake_submit(question: str, mode: str) -> dict:
            return {"ok": True, "job_id": "job-fast"}

        def _request(skill: str, args: dict) -> ModelRequest:
            payload = {"commandParams": {"skillId": skill, "args": args}}
            text = "<payload>" + json.dumps(payload, ensure_ascii=False) + "</payload>"
            return ModelRequest(messages=[Message(role="user", content=[TextPart(text)])])

        provider = SkillFastPathProvider(
            self.mod.HANDLERS, fallback=None, final_factory=ModelResponse.final,
        )
        env = {"ACPS_ROOT_STORE": str(self.tmp / "fast.sqlite3")}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(self.mod, "default_submit", fake_submit):
            submitted = asyncio.run(provider.complete(_request(
                "research.submit",
                {"question": QUESTION, "request_id": "req-fast", "user_scope": "demo"},
            )))
            receipt = json.loads(submitted.final_text)
            self.assertTrue(receipt["ok"], receipt)
            self.assertEqual(receipt["job_id"], "job-fast")
            self.assertEqual(receipt["state"], "accepted")

            queried = asyncio.run(provider.complete(_request(
                "research.status", {"root_id": receipt["root_id"]},
            )))
            status = json.loads(queried.final_text)
            self.assertTrue(status["ok"], status)
            self.assertEqual(status["job_id"], "job-fast")
            self.assertEqual(status["error"], "job-not-found")

    def test_缺question返回明确错误(self):
        from unittest import mock

        self.mod.set_current_caller(ME)
        with mock.patch.dict(os.environ, {"ACPS_ROOT_STORE": str(self.tmp / "empty.sqlite3")}):
            payload = asyncio.run(self.mod.handle_submit())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "invalid-request")

    def test_default_submit兜底填写作业归属(self):
        from unittest import mock

        from acps_cluster.agents import supervisor  # noqa: PLC0415

        captured: dict[str, object] = {}

        class _Tool:
            async def handler(self, args):  # noqa: ANN001, ANN202
                captured["owner"] = supervisor._JOB_OWNER.get()  # noqa: SLF001
                captured["args"] = args
                return {"ok": True, "job_id": "job-z"}

        self.mod.set_current_caller(ME)
        token = supervisor._JOB_OWNER.set(None)  # noqa: SLF001 - 模拟钩子未写入
        try:
            with mock.patch.object(self.mod, "_submit_tool", lambda: _Tool()):
                result = asyncio.run(self.mod.default_submit("分析这组实验数据", "custom"))
        finally:
            # ContextVar 是进程级：不恢复会污染同进程后续的 submit_job 用例
            supervisor._JOB_OWNER.reset(token)  # noqa: SLF001
        self.assertEqual(result["job_id"], "job-z")
        self.assertEqual(captured["owner"], ME)
        self.assertEqual(captured["args"]["question"], "分析这组实验数据")

    def test_入参面不含注入字段(self):
        import inspect

        for handler in (self.mod.handle_submit, self.mod.handle_status):
            params = set(inspect.signature(handler).parameters)
            self.assertNotIn("store", params)
            self.assertNotIn("submit_fn", params)
            self.assertNotIn("caller_aic", params)

    def test_未验证调用方拒绝(self):
        payload = asyncio.run(self.mod.submit_core(
            QUESTION, "req-1", store=self.store, caller_aic=None,
        ))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "unverified-caller")

    def test_快速通道注册了入口能力(self):
        from unittest import mock
        from acps_cluster.agents import supervisor  # noqa: PLC0415

        wrapped = supervisor._with_inbound_skill_fastpath(object())
        self.assertEqual(set(wrapped.skills), {"research.submit", "research.status"})
        with mock.patch.dict(os.environ, {"ACPS_SUPERVISOR_SKILL_FASTPATH": "1"}):
            enabled = supervisor._with_inbound_skill_fastpath(object())
        self.assertIn("research.orchestrate", enabled.skills)

    def test_受理后查询闭环(self):
        self.mod.set_current_caller(ME)

        async def fake_submit(question: str, mode: str) -> dict:
            return {"ok": True, "job_id": "job-x"}

        payload = asyncio.run(self.mod.submit_core(
            QUESTION, "req-1", store=self.store, submit_fn=fake_submit,
            platform_task_id="task-platform-9",
        ))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["state"], "accepted")
        root = self.store.get(payload["root_id"])
        self.assertEqual(root.entry_aic, ME)
        self.assertEqual(root.platform_task_id, "task-platform-9")

        status = asyncio.run(self.mod.status_core(
            payload["root_id"], store=self.store, jobs_root_path=self.jobs_root,
        ))
        self.assertTrue(status["ok"])
        self.assertEqual(status["job_id"], "job-x")
        self.assertEqual(status["error"], "job-not-found")


@unittest.skipUnless(importlib.util.find_spec("wit") is not None, "需要 wit 运行时")
class AcsInvariantTest(unittest.TestCase):
    def test_新增入口能力不改_supervisor_ACS投影(self):
        from acps_cluster.agents import supervisor  # noqa: PLC0415
        from acps_cluster.tools import p0_checks  # noqa: PLC0415

        agent = supervisor.build_supervisor_agent(None)
        skills = p0_checks._load_make_acs().build_skills(  # noqa: SLF001 - 复用投影函数
            agent, [], str(getattr(agent, "version", "") or "1.2.0")
        )
        self.assertEqual([skill["id"] for skill in skills], ["research.orchestrate"])


if __name__ == "__main__":
    unittest.main(verbosity=2)