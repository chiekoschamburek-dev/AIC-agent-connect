"""常驻 supervisor 自愈逻辑的离线单测。

不联网:用临时 sqlite 建最小 aip_tasks 表,验证方向/时间窗/终态过滤
以及 N1 过滤(空状态=发送即失败、带 local_error 的行不算在途);
再验证续跑裁决(计数留痕/锚点窗口/上限标注)与"同 stem 连跑收敛"(2026-10-14 审查补)。
"""

import asyncio
import contextlib
import io
import sqlite3
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

CLUSTER_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = CLUSTER_DIR.parent
for _p in (str(PROJECT_DIR), str(CLUSTER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from acps_cluster.agents import partner_supervisor


class InflightOutboundTest(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="p0-heal-"))
        (self.tmp / "acps").mkdir()
        self.db = self.tmp / "acps" / "aip-tasks.sqlite3"
        con = sqlite3.connect(self.db)
        con.execute(
            "CREATE TABLE aip_tasks (task_ref TEXT, direction TEXT, state TEXT,"
            " created_at TEXT, local_error TEXT)"
        )
        con.commit()
        con.close()

    def _insert(
        self, state: str | None, created_at: str, direction: str = "outbound", local_error: str = ""
    ):
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO aip_tasks (task_ref, direction, state, created_at, local_error)"
            " VALUES (?,?,?,?,?)",
            (f"t-{state}-{created_at[-8:]}", direction, state, created_at, local_error),
        )
        con.commit()
        con.close()

    def test_本轮working计入在途(self):
        self._insert("working", "2026-10-06T12:00:00.000000+00:00")
        self.assertEqual(
            partner_supervisor._inflight_outbound(self.tmp, "2026-10-06T11:00:00.000000+00:00"), 1
        )

    def test_终态不计入(self):
        for s in ("completed", "failed", "rejected", "canceled", "expired"):
            self._insert(s, "2026-10-06T12:00:00.000000+00:00")
        self.assertEqual(
            partner_supervisor._inflight_outbound(self.tmp, "2026-10-06T11:00:00.000000+00:00"), 0
        )

    def test_awaiting_completion不计入在途(self):
        # awaiting-completion = 伙伴已产出等 leader 关单,不是"卡住"
        self._insert("awaiting-completion", "2026-10-06T12:00:00.000000+00:00")
        self.assertEqual(
            partner_supervisor._inflight_outbound(self.tmp, "2026-10-06T11:00:00.000000+00:00"), 0
        )

    def test_时间窗之前的不计入(self):
        self._insert("working", "2026-10-06T10:00:00.000000+00:00")
        self.assertEqual(
            partner_supervisor._inflight_outbound(self.tmp, "2026-10-06T12:00:00.000000+00:00"), 0
        )

    def test_空状态发送即失败不计入(self):
        # N1:state 为空(NULL)=发送即失败(如 ConnectError),重注救不回,不算在途
        self._insert(
            None, "2026-10-14T12:00:00.000000+00:00", local_error='{"type":"ConnectError"}'
        )
        self.assertEqual(
            partner_supervisor._inflight_outbound(self.tmp, "2026-10-14T11:00:00.000000+00:00"), 0
        )

    def test_带本地错误的行不计入(self):
        self._insert(
            "working", "2026-10-14T12:00:00.000000+00:00", local_error='{"type":"ConnectError"}'
        )
        self.assertEqual(
            partner_supervisor._inflight_outbound(self.tmp, "2026-10-14T11:00:00.000000+00:00"), 0
        )

    def test_空状态无错误也不计入(self):
        self._insert(None, "2026-10-14T12:00:00.000000+00:00")
        self.assertEqual(
            partner_supervisor._inflight_outbound(self.tmp, "2026-10-14T11:00:00.000000+00:00"), 0
        )

    def test_入库方向不统计(self):
        self._insert("working", "2026-10-06T12:00:00.000000+00:00", direction="inbound")
        self.assertEqual(
            partner_supervisor._inflight_outbound(self.tmp, "2026-10-06T11:00:00.000000+00:00"), 0
        )

    def test_无库文件按0处理(self):
        self.assertEqual(
            partner_supervisor._inflight_outbound(self.tmp / "nope" / "aip-tasks.sqlite3",
                                                  "2026-10-06T11:00:00.000000+00:00"), 0)


class StaleTaskCleanupTest(unittest.TestCase):
    """启动收敛：非终态任务（inbound + outbound）→ canceled；终态一律不动。"""

    def setUp(self):
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="stale-in-"))
        (self.tmp / "acps").mkdir()
        self.db = self.tmp / "acps" / "aip-tasks.sqlite3"
        con = sqlite3.connect(self.db)
        con.execute(
            "CREATE TABLE aip_tasks (task_ref TEXT, direction TEXT, state TEXT,"
            " updated_at TEXT, local_error TEXT, products TEXT)"
        )
        con.commit()
        con.close()

    def _insert(
        self, ref: str, direction: str, state: str, local_error: str = "", products: str = ""
    ) -> None:
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO aip_tasks VALUES (?,?,?,?,?,?)",
            (ref, direction, state, "2026-10-09T00:00:00+00:00", local_error, products),
        )
        con.commit()
        con.close()

    def _states(self) -> dict:
        con = sqlite3.connect(self.db)
        try:
            return dict(con.execute("SELECT task_ref, state FROM aip_tasks").fetchall())
        finally:
            con.close()

    def _quiet(self):
        return lambda *a, **k: None

    def test_非终态inbound被收敛(self):
        self._insert("t1", "inbound", "working")
        self._insert("t2", "inbound", "accepted")
        out = partner_supervisor._cleanup_stale_tasks(self.tmp, logger=self._quiet())
        self.assertEqual(len(out), 2)
        self.assertEqual(self._states(), {"t1": "canceled", "t2": "canceled"})

    def test_inbound已交付不被收敛(self):
        # awaiting-completion = aip_submit 已成功、产物已交付，下一步归调用方。
        # 收敛成 canceled 会把"已交付"改写成"已取消"（2026-10-15 审查：实测 12 条被误标）
        self._insert("d1", "inbound", "awaiting-completion")
        out = partner_supervisor._cleanup_stale_tasks(self.tmp, logger=self._quiet())
        self.assertEqual(out, [])
        self.assertEqual(self._states(), {"d1": "awaiting-completion"})

    def test_inbound等待补充输入不被收敛(self):
        # awaiting-input：等的是调用方（叮当/用户）回话，会话状态已持久化（agent_state）——
        # 重启后对方回话仍可续话；收敛成 canceled 会把"等你回话"改写成"已取消"
        # （2026-10-16 现网：叮当会话任务在 supervisor 重启时被误收敛）
        self._insert("a1", "inbound", "awaiting-input")
        out = partner_supervisor._cleanup_stale_tasks(self.tmp, logger=self._quiet())
        self.assertEqual(out, [])
        self.assertEqual(self._states(), {"a1": "awaiting-input"})

    def test_outbound等待补充输入仍被收敛(self):
        # 出站方向同名状态：向我们要输入的是伙伴，收集它的 run 随重启消失、已无人能答 → canceled
        self._insert("a2", "outbound", "awaiting-input")
        out = partner_supervisor._cleanup_stale_tasks(self.tmp, logger=self._quiet())
        self.assertEqual(len(out), 1)
        self.assertEqual(self._states(), {"a2": "canceled"})

    def test_outbound待收取仍被收敛(self):
        # 同名状态在出站方向：无产物 → 照旧收敛 canceled
        # （收集它的 run 随重启消失、永远收不到，否则账本永远非空、"重启前在途检查"失门）
        self._insert("o2", "outbound", "awaiting-completion")
        out = partner_supervisor._cleanup_stale_tasks(self.tmp, logger=self._quiet())
        self.assertEqual(len(out), 1)
        self.assertEqual(self._states(), {"o2": "canceled"})

    def test_outbound已交付产物被标expired(self):
        # 审查 C：伙伴产物已到达（在库）→ expired，不能把"伙伴已完成"记成"我方取消"
        self._insert("o3", "outbound", "awaiting-completion", products='[{"id":"p"}]')
        out = partner_supervisor._cleanup_stale_tasks(self.tmp, logger=self._quiet())
        self.assertEqual(len(out), 1)
        self.assertEqual(self._states()["o3"], "expired")
        con = sqlite3.connect(self.db)
        try:
            err = con.execute(
                "SELECT local_error FROM aip_tasks WHERE task_ref='o3'"
            ).fetchone()[0]
        finally:
            con.close()
        self.assertIn("ExpiredOnRestart", err)
        self.assertIn("productsBytes", err)

    def test_终态inbound不动(self):
        for i, s in enumerate(("completed", "failed", "canceled", "rejected")):
            self._insert(f"t{i}", "inbound", s)
        out = partner_supervisor._cleanup_stale_tasks(self.tmp, logger=self._quiet())
        self.assertEqual(out, [])
        self.assertEqual(
            self._states(),
            {"t0": "completed", "t1": "failed", "t2": "canceled", "t3": "rejected"},
        )

    def test_非终态outbound同样被收敛(self):
        # 出站委派同样在上一进程里失去收集方：启动时一并收敛（否则账本永远非空、
        # "重启前在途检查"这个门就失效了）
        self._insert("o1", "outbound", "working")
        partner_supervisor._cleanup_stale_tasks(self.tmp, logger=self._quiet())
        self.assertEqual(self._states()["o1"], "canceled")

    def test_收敛留痕写进local_error(self):
        self._insert("t1", "inbound", "working")
        partner_supervisor._cleanup_stale_tasks(self.tmp, logger=self._quiet())
        con = sqlite3.connect(self.db)
        try:
            err = con.execute(
                "SELECT local_error FROM aip_tasks WHERE task_ref='t1'"
            ).fetchone()[0]
        finally:
            con.close()
        self.assertIn("StaleAfterRestart", err)

    def test_原local_error保留进priorLocalError(self):
        # 收敛会整体覆盖 local_error：第一手失败原因（ConnectError）须保留进标记
        # （2026-10-15 审查 D；_inflight_outbound 也依赖 local_error，两处耦合）
        self._insert("e1", "outbound", "working", local_error='{"type":"ConnectError"}')
        partner_supervisor._cleanup_stale_tasks(self.tmp, logger=self._quiet())
        con = sqlite3.connect(self.db)
        try:
            err = con.execute(
                "SELECT local_error FROM aip_tasks WHERE task_ref='e1'"
            ).fetchone()[0]
        finally:
            con.close()
        self.assertIn("StaleAfterRestart", err)
        self.assertIn("priorLocalError", err)
        self.assertIn("ConnectError", err)

    def test_无原错误时不加prior字段(self):
        self._insert("n1", "inbound", "working")
        partner_supervisor._cleanup_stale_tasks(self.tmp, logger=self._quiet())
        con = sqlite3.connect(self.db)
        try:
            err = con.execute(
                "SELECT local_error FROM aip_tasks WHERE task_ref='n1'"
            ).fetchone()[0]
        finally:
            con.close()
        self.assertNotIn("priorLocalError", err)

    def test_无库不报错(self):
        out = partner_supervisor._cleanup_stale_tasks(
            self.tmp / "nope", logger=self._quiet()
        )
        self.assertEqual(out, [])


class HealDecisionTest(unittest.TestCase):
    """续跑裁决:计数走留痕文件(单调)、锚点走收件到达时刻、上限归档带标注。"""

    def setUp(self):
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="heal-"))
        (self.tmp / "acps").mkdir()
        self.db = self.tmp / "acps" / "aip-tasks.sqlite3"
        con = sqlite3.connect(self.db)
        con.execute(
            "CREATE TABLE aip_tasks (task_ref TEXT, direction TEXT, state TEXT,"
            " created_at TEXT, local_error TEXT)"
        )
        con.commit()
        con.close()
        self.outbox = self.tmp / "outbox"
        self.outbox.mkdir()

    def _insert(self, state: str, created_at: str) -> None:
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO aip_tasks VALUES (?,?,?,?,?)", ("t1", "outbound", state, created_at, "")
        )
        con.commit()
        con.close()

    def _decide(self, anchor: str = "2026-10-14T00:00:00.000000+00:00"):
        return partner_supervisor._heal_decision(
            workspace=self.tmp, outbox=self.outbox, stem="task", anchor_iso=anchor
        )

    def test_首轮重注(self):
        self._insert("working", "2026-10-14T01:00:00.000000+00:00")
        self.assertEqual(self._decide(), ("reinject", 1, 0))

    def test_计数单调_两轮后续跑到上限归档(self):
        # 留痕文件计数必须单调递增——原先数 outbox 产物文件,同名覆盖导致恒为 1
        self._insert("working", "2026-10-14T01:00:00.000000+00:00")
        self.assertEqual(partner_supervisor._bump_heals(self.outbox, "task"), 1)
        self.assertEqual(partner_supervisor._bump_heals(self.outbox, "task"), 2)
        action, inflight, heals = self._decide()
        self.assertEqual((action, inflight, heals), ("archive", 1, 2))

    def test_无在途委派直接归档(self):
        self.assertEqual(self._decide(), ("archive", 0, 0))

    def test_首轮写锚点_续跑复用(self):
        # 锚点=本任务首轮开始时刻,写进 heal.json;续跑轮传新的 started_iso 也不漂移
        a1 = partner_supervisor._task_anchor(
            self.outbox, "task", "2026-10-14T02:00:00.000000+00:00"
        )
        self.assertEqual(a1, "2026-10-14T02:00:00.000000+00:00")
        a2 = partner_supervisor._task_anchor(
            self.outbox, "task", "2026-10-14T03:00:00.000000+00:00"
        )
        self.assertEqual(a2, "2026-10-14T02:00:00.000000+00:00")

    def test_跨任务不串染(self):
        # 2026-10-14 审查 A:前一任务(锚 T0)的孤儿在 T1 创建;后一任务锚 T2>T1 → 不落进后者窗口
        self._insert("working", "2026-10-14T01:00:00.000000+00:00")
        self.assertEqual(self._decide("2026-10-14T02:00:00.000000+00:00"), ("archive", 0, 0))
        # 而前一任务自己的续跑轮(锚 T0)看得见该孤儿
        self.assertEqual(self._decide("2026-10-14T00:00:00.000000+00:00")[0], "reinject")

    def test_bump保留锚点(self):
        partner_supervisor._task_anchor(
            self.outbox, "task", "2026-10-14T02:00:00.000000+00:00"
        )
        partner_supervisor._bump_heals(self.outbox, "task")
        state = partner_supervisor._heal_state(self.outbox, "task")
        self.assertEqual(state.get("anchor"), "2026-10-14T02:00:00.000000+00:00")
        self.assertEqual(state.get("count"), 1)

    def test_上限归档标注可grep(self):
        (self.outbox / "task.md").write_text("status=ok\n", encoding="utf-8")
        partner_supervisor._annotate_heal(self.outbox, "task", inflight=2, heals=2)
        text = (self.outbox / "task.md").read_text(encoding="utf-8")
        self.assertIn("heal=cap-reached inflight=2 heals=2", text)


class _FakeRun:
    status = "ok"
    iterations = 1
    final_text = "fake"


class _FakeRuntime:
    async def run(self, task, config):  # noqa: ANN001, ANN201
        return _FakeRun()


class HealConvergenceTest(unittest.TestCase):
    """端到端(离线):同 stem 连跑,重注有界,到上限强制归档并留痕(锚点=首轮开始时刻)。"""

    def setUp(self):
        import tempfile

        self.ws = Path(tempfile.mkdtemp(prefix="heal-e2e-"))
        (self.ws / "acps").mkdir()
        self.db = self.ws / "acps" / "aip-tasks.sqlite3"
        con = sqlite3.connect(self.db)
        con.execute(
            "CREATE TABLE aip_tasks (task_ref TEXT, direction TEXT, state TEXT,"
            " created_at TEXT, local_error TEXT)"
        )
        # 持久在途:每轮结束时都算"仍有在途出站委派";远期时间保证落在首轮锚点之后
        con.execute(
            "INSERT INTO aip_tasks VALUES (?,?,?,?,?)",
            ("t1", "outbound", "working", "2099-01-01T00:00:00.000000+00:00", ""),
        )
        con.commit()
        con.close()
        self.inbox = self.ws / "inbox"
        self.outbox = self.ws / "outbox"
        self.done = self.inbox / "done"
        self.inbox.mkdir()
        self.task = self.inbox / "task.txt"
        self.task.write_text("做一个任务", encoding="utf-8")

    def test_同stem连跑三轮收敛归档(self):
        ps = partner_supervisor
        orig = (ps.WORKSPACE, ps.OUTBOX, ps.DONE, ps.supervisor.build_run_config)
        ps.WORKSPACE, ps.OUTBOX, ps.DONE = self.ws, self.outbox, self.done
        ps.supervisor.build_run_config = lambda roster: None  # noqa: ARG005
        try:
            asyncio.run(ps.run_inbox_task(_FakeRuntime(), [], self.task))
            self.assertTrue(self.task.exists(), "第 1 轮后不应归档")
            self.assertTrue(ps._heal_state(self.outbox, "task").get("anchor"), "首轮应写入锚点")
            self.assertEqual(ps._heals_done(self.outbox, "task"), 1)
            asyncio.run(ps.run_inbox_task(_FakeRuntime(), [], self.task))
            self.assertEqual(ps._heals_done(self.outbox, "task"), 2)
            asyncio.run(ps.run_inbox_task(_FakeRuntime(), [], self.task))
        finally:
            (ps.WORKSPACE, ps.OUTBOX, ps.DONE) = orig[:3]
            ps.supervisor.build_run_config = orig[3]
        self.assertFalse(self.task.exists(), "到上限必须归档(有界收敛)")
        self.assertTrue((self.done / "task.txt").is_file())
        self.assertTrue((self.done / "task.heal.json").is_file(), "留痕随归档入 done/")
        text = (self.outbox / "task.md").read_text(encoding="utf-8")
        self.assertIn("heal=cap-reached inflight=1 heals=2", text)


class UnclosedDeliverablesTest(unittest.TestCase):
    """run 收尾代签 aip_complete 的边界（2026-10-16 现网 smoke：模型拿到产物没关单）。"""

    def setUp(self):
        import tempfile

        from acps_cluster.agents import supervisor

        self.supervisor = supervisor
        self.ws = Path(tempfile.mkdtemp(prefix="unclosed-"))
        (self.ws / "acps").mkdir()
        self.db = self.ws / "acps" / "aip-tasks.sqlite3"

    def _ledger(self, rows):
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE aip_tasks (task_ref TEXT, direction TEXT, state TEXT,"
                    " created_at TEXT, products TEXT)")
        con.executemany("INSERT INTO aip_tasks VALUES (?,?,?,?,?)", rows)
        con.commit()
        con.close()

    def _run(self, hook, rows, status, *, runtime, delegated=None, blind_calls=0, gate=True):
        """on_run_start → 铺台账 → （模拟本轮委派 + product_gate）→ on_run_end。

        `delegated`：本轮"亲自派发过"的 task_ref；None = 取 rows 里全部出站行
        （等价于旧行为，便于既有用例继续表达"这些都是本轮派的"）。
        `blind_calls`：额外发起 N 次委派调用但响应里**解析不出 ref**（观测退化路径）。
        `gate`：True=product_gate 通过；False=不通过；None=本轮未调用 product_gate。
        """
        calls: list = []

        class FakeRuntime:
            async def aip_task(self, state, *, action, task_ref):
                calls.append((action, task_ref))
                if isinstance(runtime, Exception):
                    raise runtime
                return {"state": "completed"}

        if delegated is None:
            delegated = [r[0] for r in rows if r[1] == "outbound"]

        async def scenario():
            await hook.on_run_start(SimpleNamespace(context=None))
            self._ledger(rows)
            for ref in delegated:
                await self._delegate_once(hook, {"taskRef": ref, "state": "awaiting-completion"})
            for _ in range(blind_calls):
                await self._delegate_once(hook, {"ok": True})     # 没有 ref 可解析
            if gate is not None:
                await self._gate_once(hook, ok=gate)
            ctx = SimpleNamespace(runtime=FakeRuntime(), state="STATE")
            await hook.on_run_end(SimpleNamespace(result=SimpleNamespace(status=status),
                                                  context=ctx))
            return calls

        return asyncio.run(scenario())

    @staticmethod
    async def _gate_once(hook, *, ok=True):
        """按 wit 的事件形状喂一对 product_gate request/response。"""
        await hook.on_tool_request(SimpleNamespace(
            tool_call=SimpleNamespace(name="product_gate"), tool=None))
        await hook.on_tool_response(SimpleNamespace(
            tool_call=SimpleNamespace(name="product_gate"), response={"ok": ok}))

    @staticmethod
    async def _delegate_once(hook, response):
        """按 wit 的事件形状喂一对 request/response（工具名走内置 acps_delegate）。"""
        await hook.on_tool_request(SimpleNamespace(
            tool_call=SimpleNamespace(name="acps_delegate"), tool=None))
        await hook.on_tool_response(SimpleNamespace(
            tool_call=SimpleNamespace(name="acps_delegate"), response=response))

    def _hook(self):
        return self.supervisor.UnclosedDeliverablesHooks(workspace=self.ws)

    def test_只扫本次run且带产物的出站行(self):
        hook = self._hook()
        rows = [
            ("t-new", "outbound", "awaiting-completion", "2099-01-01T00:00:00.000000+00:00", '["产物快照"]'),
            ("t-noproduct", "outbound", "awaiting-completion", "2099-01-01T00:00:01.000000+00:00", ""),
            ("t-inbound", "inbound", "awaiting-completion", "2099-01-01T00:00:02.000000+00:00", '["产物快照"]'),
            ("t-closed", "outbound", "completed", "2099-01-01T00:00:03.000000+00:00", '["产物快照"]'),
        ]
        calls = self._run(hook, rows, "completed", runtime=None)
        self.assertEqual(calls, [("complete", "t-new")])

    def test_并发run的行不代签(self):
        # 常驻服务与 L2 作业 worker 共用同一个库与 WORKSPACE，而 session_id 是进程/连接级
        # 的（分不开同进程内并发的两个 run）→ 必须再用"本轮亲自派发过"收窄
        hook = self._hook()
        rows = [
            ("task_aaaaaaaaaaaa", "outbound", "awaiting-completion",
             "2099-01-01T00:00:00.000000+00:00", '["产物快照"]'),
            ("task_bbbbbbbbbbbb", "outbound", "awaiting-completion",
             "2099-01-01T00:00:01.000000+00:00", '["产物快照"]'),
        ]
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            calls = self._run(hook, rows, "completed", runtime=None,
                              delegated=["task_aaaaaaaaaaaa"])
        self.assertEqual(calls, [("complete", "task_aaaaaaaaaaaa")])
        self.assertIn("排除 1 条非本轮派发", buf.getvalue())

    def test_本轮零委派时一条都不代签(self):
        hook = self._hook()
        rows = [("task_cccccccccccc", "outbound", "awaiting-completion",
                 "2099-01-01T00:00:00.000000+00:00", '["产物快照"]')]
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            calls = self._run(hook, rows, "completed", runtime=None, delegated=[])
        self.assertEqual(calls, [], "本轮没派过任何委派，扫到的行属于并发 run，不能代签")
        self.assertIn("本轮未发起任何委派", buf.getvalue())

    def test_观测退化时退回时间锚点(self):
        # 有委派、但响应里解析不出 task_ref → 宁可退回锚点，也别让"忘关单"重新变常态
        hook = self._hook()
        rows = [("t-blind", "outbound", "awaiting-completion",
                 "2099-01-01T00:00:00.000000+00:00", '["产物快照"]')]
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            calls = self._run(hook, rows, "completed", runtime=None,
                              delegated=[], blind_calls=1)
        self.assertEqual(calls, [("complete", "t-blind")])
        self.assertIn("观测退化", buf.getvalue())

    def test_正则兜底能从字符串响应里捞出ref(self):
        hook = self._hook()
        rows = [("task_dddddddddddd", "outbound", "awaiting-completion",
                 "2099-01-01T00:00:00.000000+00:00", '["产物快照"]')]

        async def scenario():
            await hook.on_run_start(SimpleNamespace(context=None))
            self._ledger(rows)
            # 响应整体是个字符串（没有结构化 taskRef 字段可读）
            await hook.on_tool_request(SimpleNamespace(
                tool_call=SimpleNamespace(name="acps_delegate_public"), tool=None))
            await hook.on_tool_response(SimpleNamespace(
                tool_call=SimpleNamespace(name="acps_delegate_public"),
                response="{'ok': True, 'taskRef': 'task_dddddddddddd'}"))
            return set((hook._SEEN.get() or {}).get("refs") or ())

        self.assertIn("task_dddddddddddd", asyncio.run(scenario()))

    def test_非委派工具不计入观测(self):
        hook = self._hook()

        async def scenario():
            await hook.on_run_start(SimpleNamespace(context=None))
            await hook.on_tool_request(SimpleNamespace(
                tool_call=SimpleNamespace(name="plan_gate"), tool=None))
            await hook.on_tool_response(SimpleNamespace(
                tool_call=SimpleNamespace(name="plan_gate"),
                response={"taskRef": "task_eeeeeeeeeeee"}))
            seen = hook._SEEN.get() or {}
            return seen.get("calls"), set(seen.get("refs") or ())

        calls, refs = asyncio.run(scenario())
        self.assertEqual(calls, 0)
        self.assertEqual(refs, set())

    def test_历史遗留行不在本次run窗口内不动(self):
        hook = self._hook()
        rows = [("t-old", "outbound", "awaiting-completion",
                 "2000-01-01T00:00:00.000000+00:00", '["产物快照"]')]
        self.assertEqual(self._run(hook, rows, "completed", runtime=None), [])

    def test_run未正常收尾时不代签(self):
        hook = self._hook()
        rows = [("t1", "outbound", "awaiting-completion",
                 "2099-01-01T00:00:00.000000+00:00", '["产物快照"]')]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            calls = self._run(hook, rows, "budget_exhausted", runtime=None)
        self.assertEqual(calls, [])
        self.assertIn("按纪律不代签", out.getvalue())

    def test_product_gate未通过不代签(self):
        # 产物被判无效（空/过短/占位符）时不能替它发 complete
        hook = self._hook()
        rows = [("t1", "outbound", "awaiting-completion",
                 "2099-01-01T00:00:00.000000+00:00", '["产物快照"]')]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            calls = self._run(hook, rows, "completed", runtime=None, gate=False)
        self.assertEqual(calls, [])
        self.assertIn("product_gate 未执行或未通过", out.getvalue())

    def test_本轮未执行product_gate不代签(self):
        # fail-closed：没质检过的产物宁可挂账，也不代签
        hook = self._hook()
        rows = [("t1", "outbound", "awaiting-completion",
                 "2099-01-01T00:00:00.000000+00:00", '["产物快照"]')]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            calls = self._run(hook, rows, "completed", runtime=None, gate=None)
        self.assertEqual(calls, [])
        self.assertIn("product_gate 未执行或未通过", out.getvalue())
        self.assertIn("gate_calls=0", out.getvalue())

    def test_status缺失不代签(self):
        # 原先 "" 被当作正常收尾（fail-open）；拿不到状态就不猜
        hook = self._hook()
        rows = [("t1", "outbound", "awaiting-completion",
                 "2099-01-01T00:00:00.000000+00:00", '["产物快照"]')]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            calls = self._run(hook, rows, "", runtime=None)
        self.assertEqual(calls, [])
        self.assertIn("按纪律不代签", out.getvalue())

    def test_拿不到runtime时不猜(self):
        hook = self._hook()
        rows = [("t1", "outbound", "awaiting-completion",
                 "2099-01-01T00:00:00.000000+00:00", '["产物快照"]')]

        async def scenario():
            await hook.on_run_start(SimpleNamespace(context=None))
            self._ledger(rows)
            await self._delegate_once(hook, {"taskRef": "t1"})
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                await hook.on_run_end(SimpleNamespace(
                    result=SimpleNamespace(status="completed"), context=None))
            return buf.getvalue()

        out = asyncio.run(scenario())
        self.assertIn("unclosed-deliverables 1 条未关单", out)

    def test_补关单异常只记日志不外抛(self):
        hook = self._hook()
        rows = [("t1", "outbound", "awaiting-completion",
                 "2099-01-01T00:00:00.000000+00:00", '["产物快照"]')]
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            self._run(hook, rows, "completed", runtime=RuntimeError("伙伴侧已过期"))
        self.assertIn("补关单失败 t1", buf.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
