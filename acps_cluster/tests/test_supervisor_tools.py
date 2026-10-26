"""离线单测：supervisor 的 wait_many 屏障工具与 max_steps 配置。

不联网、不起服务——runtime 用假对象（aip_task 返回预设快照），
Tool 经 .handler(args) 直接调用（与 test_citation_verify 同款模式）。
"""

import asyncio
import os
import sys
import unittest
from pathlib import Path

CLUSTER_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = CLUSTER_DIR.parent
for _p in (str(PROJECT_DIR), str(CLUSTER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


class _FakeRuntime:
    """按 task_ref 返回预设快照；记录每次 aip_task 调用。"""

    def __init__(self, snapshots: dict[str, dict]):
        self.snapshots = snapshots
        self.calls: list[str] = []

    async def aip_task(self, state, action: str, task_ref: str):
        self.calls.append(task_ref)
        return self.snapshots.get(task_ref, {"state": "working", "products": []})


def _make(refs: list[str], snapshots: dict[str, dict], **kw):
    from acps_cluster.agents import supervisor

    rt = _FakeRuntime(snapshots)
    tool = supervisor._make_wait_many_tool(rt, object(), **kw)
    return tool, rt


class WaitManyTest(unittest.TestCase):
    def test_全部到达_一次返回各自快照(self):
        async def run():
            tool, rt = _make(
                ["t1", "t2"],
                {"t1": {"state": "completed", "products": [1, 2], "summary": "A"},
                 "t2": {"state": "awaiting-completion", "products": [3], "summary": "B"}},
            )
            out = await tool.handler({"task_refs": ["t1", "t2"]})
            return out, rt

        out, rt = asyncio.run(run())
        self.assertTrue(out["ok"])
        self.assertEqual({a["task_ref"] for a in out["arrived"]}, {"t1", "t2"})
        self.assertEqual(out["pending"], [])
        self.assertEqual(out["arrived"][0]["products"], 2)
        self.assertEqual(len(rt.calls), 2)  # 全到达 → 每引用恰好查一次

    def test_超时返回arrived加pending(self):
        async def run():
            tool, rt = _make(
                ["done", "hung"],
                {"done": {"state": "completed", "products": [1]},
                 "hung": {"state": "working", "products": []}},
                default_seconds=0.05, poll_seconds=0.01,
            )
            return await tool.handler({"task_refs": ["done", "hung"]})

        out = asyncio.run(run())
        self.assertFalse(out["ok"])
        self.assertEqual([a["task_ref"] for a in out["arrived"]], ["done"])
        self.assertEqual(out["pending"], ["hung"])
        self.assertIn("remedy", out["note"])

    def test_空refs_校验拒绝(self):
        async def run():
            tool, _ = _make([], {})
            return await tool.handler({"task_refs": []})

        out = asyncio.run(run())
        self.assertFalse(out["ok"])
        self.assertIn("task_refs", out["error"])

    def test_字符串refs与去重(self):
        async def run():
            tool, rt = _make(
                ["t1"],
                {"t1": {"state": "completed", "products": [1]}},
            )
            return await tool.handler({"task_refs": ["t1", "t1", " t1 "]}), rt

        out, rt = asyncio.run(run())
        self.assertTrue(out["ok"])
        self.assertEqual(len(rt.calls), 1)  # 去重后只查一次

    def test_部分轮询后才到达(self):
        async def run():
            from acps_cluster.agents import supervisor

            # 慢到达：前 2 次 working，之后 completed
            class _Slow:
                def __init__(self):
                    self.n = 0

                async def aip_task(self, state, action, task_ref):
                    self.n += 1
                    if self.n <= 2:
                        return {"state": "working", "products": []}
                    return {"state": "completed", "products": [1]}

            slow = _Slow()
            tool = supervisor._make_wait_many_tool(slow, object(), default_seconds=5, poll_seconds=0.01)
            out = await tool.handler({"task_refs": ["x"]})
            return out, slow

        out, slow = asyncio.run(run())
        self.assertTrue(out["ok"])
        self.assertGreaterEqual(slow.n, 3)


class MaxStepsConfigTest(unittest.TestCase):
    def test_默认200(self):
        from acps_cluster.agents import supervisor

        env = "ACPS_SUPERVISOR_MAX_STEPS"
        old = os.environ.pop(env, None)
        try:
            rc = supervisor.build_run_config(supervisor.cluster_roster.load_roster())
            self.assertEqual(rc.max_steps, 200)
        finally:
            if old is not None:
                os.environ[env] = old

    def test_环境变量覆盖(self):
        from acps_cluster.agents import supervisor

        env = "ACPS_SUPERVISOR_MAX_STEPS"
        old = os.environ.get(env)
        os.environ[env] = "150"
        try:
            rc = supervisor.build_run_config(supervisor.cluster_roster.load_roster())
            self.assertEqual(rc.max_steps, 150)
        finally:
            if old is None:
                os.environ.pop(env, None)
            else:
                os.environ[env] = old

    def test_wait_many与wait_partner都已注册(self):
        from acps_cluster.agents import supervisor

        rc = supervisor.build_run_config(supervisor.cluster_roster.load_roster())
        names = {t.name for t in rc.tools}
        self.assertIn("wait_many", names)
        self.assertIn("wait_partner", names)




class WaitBudgetCapTest(unittest.TestCase):
    """回归：等待类工具预算上限必须低于 RunConfig.tool_timeout（2026-10-06 审查发现）。"""

    def test_上限常量低于工具超时(self):
        from acps_cluster.agents import supervisor

        self.assertEqual(supervisor._WAIT_BUDGET_CAP, 400.0)
        self.assertLess(supervisor._WAIT_BUDGET_CAP, 420.0)

    def test_wait_partner传大max_seconds被clamp且优雅返回(self):
        async def run():
            from unittest import mock

            from acps_cluster.agents import supervisor

            class _Never:
                async def aip_task(self, state, action, task_ref):
                    return {"state": "working", "products": []}

            # 钳位语义照验，但不必真等满 400s：把上限临时压到 0.05s
            # （上一版没压，单这一条就占掉整套测试的 400 秒）
            with mock.patch.object(supervisor, "_WAIT_BUDGET_CAP", 0.05):
                tool = supervisor._make_wait_partner_tool(_Never(), object(), poll_seconds=0.01)
                return await tool.handler({"task_ref": "t1", "max_seconds": 500})

        out = asyncio.run(run())
        # 若 clamp 失效(旧 540 > 420),这里会变成 wit 工具超时异常而非优雅返回
        self.assertFalse(out["ok"])
        self.assertTrue(out["still_working"])
        self.assertLess(out["waited_seconds"], 5)
        self.assertIn("wait_partner", out["note"])




class WaitManyGatherTest(unittest.TestCase):
    """gather 化改造的回归:故障隔离、探针时限、取消穿透。"""

    def test_单ref探针异常_不影响其余ref到达(self):
        async def run():
            from acps_cluster.agents import supervisor

            class _Flaky:
                async def aip_task(self, state, action, task_ref):
                    if task_ref == "bad":
                        raise ConnectionError("网关挂死")
                    return {"state": "completed", "products": [1], "summary": "ok"}

            tool = supervisor._make_wait_many_tool(_Flaky(), object(),
                                                   default_seconds=1.0, poll_seconds=0.01)
            return await tool.handler({"task_refs": ["bad", "good"]})

        out = asyncio.run(run())
        self.assertEqual([a["task_ref"] for a in out["arrived"]], ["good"])
        self.assertEqual(out["pending"], [])
        self.assertEqual([e["task_ref"] for e in out["errors"]], ["bad"])
        self.assertFalse(out["ok"])

    def test_挂死探针被时限截断_其余不受拖累(self):
        async def run():
            from acps_cluster.agents import supervisor

            class _Hang:
                async def aip_task(self, state, action, task_ref):
                    if task_ref == "hang":
                        await asyncio.sleep(60)  # 挂死,远超探针时限
                    return {"state": "completed", "products": [1]}

            tool = supervisor._make_wait_many_tool(_Hang(), object(),
                                                   default_seconds=2.0, poll_seconds=0.01)
            return await tool.handler({"task_refs": ["hang", "good"]})

        out = asyncio.run(run())
        self.assertEqual(out["arrived"][0]["task_ref"], "good")
        self.assertEqual(out["pending"], [])
        self.assertEqual([e["task_ref"] for e in out["errors"]], ["hang"])
        self.assertLess(out["waited_seconds"], 30)  # 被单探针时限截断,不被挂死者拖满

    def test_外部取消可穿透wait_many(self):
        async def run():
            from acps_cluster.agents import supervisor

            class _Slow:
                async def aip_task(self, state, action, task_ref):
                    await asyncio.sleep(30)
                    return {"state": "working", "products": []}

            tool = supervisor._make_wait_many_tool(_Slow(), object(),
                                                   default_seconds=60, poll_seconds=0.01)
            task = asyncio.create_task(tool.handler({"task_refs": ["x"]}))
            await asyncio.sleep(0.2)
            task.cancel()
            try:
                await task
                return "no-cancel"
            except asyncio.CancelledError:
                return "cancelled"

        self.assertEqual(asyncio.run(run()), "cancelled")




class LeaderBridgeTest(unittest.TestCase):
    """2026-10-07 审查 🔴 回归:leader 挂上 BridgeHooks 后,wait_partner/wait_many
    桥版工具必须真正可用(此前 _RUN 无人设置,两个工具恒返回'桥未就绪')。"""

    async def _activate_bridge(self, runtime, state):
        from acps_cluster.delegation.bridge import BridgeHooks

        class _Ev:
            pass

        ev = _Ev()
        ev.context = type("C", (), {})()
        ev.context.runtime = runtime
        ev.context.state = state
        await BridgeHooks().on_run_start(ev)
        

    def test_桥激活后wait_many真正执行等待(self):
        async def run():
            from acps_cluster.agents import supervisor
            from acps_cluster.delegation.bridge import current_runtime
            from wit import AgentState

            class _RT:
                async def aip_task(self, state, action, task_ref):
                    return {"state": "completed", "products": [1], "summary": "s"}

            rt, st = _RT(), AgentState()
            self.assertIsNone(current_runtime())  # 激活前:桥未就绪的根因
            await self._activate_bridge(rt, st)
            tool = supervisor._make_wait_many_bridge_tool()
            out = await tool.handler({"task_refs": ["t1"]})
            return out

        out = asyncio.run(run())
        self.assertTrue(out["ok"], f"桥版 wait_many 仍死: {out}")

    def test_桥激活后wait_partner真正执行等待(self):
        async def run():
            from acps_cluster.agents import supervisor
            from acps_cluster.delegation.bridge import current_state
            from wit import AgentState

            class _RT:
                async def aip_task(self, state, action, task_ref):
                    return {"state": "completed", "products": [1]}

            rt, st = _RT(), AgentState()
            self.assertIsNone(current_state())
            await self._activate_bridge(rt, st)
            tool = supervisor._make_wait_partner_bridge_tool()
            return await tool.handler({"task_ref": "t1"})

        out = asyncio.run(run())
        self.assertTrue(out["ok"], f"桥版 wait_partner 仍死: {out}")

    def test_桥未激活时如实报错(self):
        async def run():
            from acps_cluster.agents import supervisor
            from acps_cluster.delegation import bridge

            bridge._RUN.set(None)  # 显式清空,验证失败安全形态
            tool = supervisor._make_wait_many_bridge_tool()
            return await tool.handler({"task_refs": ["x"]})

        out = asyncio.run(run())
        self.assertFalse(out["ok"])
        self.assertIn("桥未就绪", out["error"])



class PublicDelegateToolTest(unittest.TestCase):
    """acps_delegate_public：三入参全标量的公开能力委派工具（2026-10-16）。

    起因：内置 acps_delegate 的 request 是数组入参，模型提供方偶尔丢空 →
    `AipError: Invalid high-level content input`（durationMs=0，请求根本没发出）。
    本工具的 Part 列表在代码侧组装，所以不经过那条会掉件的路。
    """

    GPU_AIC = "AIC-GPU-TEST"
    SCOUT_AIC = "AIC-SCOUT-TEST"

    class _RT:
        def __init__(self, snap=None, raise_exc=None):
            self.calls = []
            self.snap = snap if snap is not None else {"taskRef": "t-1", "state": "accepted"}
            self.raise_exc = raise_exc

        async def aip_delegate(self, state, *, partner_aic, request):
            self.calls.append((partner_aic, request))
            if self.raise_exc is not None:
                raise self.raise_exc
            return dict(self.snap)

    def _roster(self):
        from acps_cluster.core import cluster_roster

        return [
            cluster_roster.RosterMember(
                key="scout", name="Scout", role="检索", module="partner_scout",
                workspace="ws-scout", port_env="ACPS_SCOUT_PORT", default_port=8445,
                declared_capabilities=("arxiv.search",), aic=self.SCOUT_AIC),
            cluster_roster.RosterMember(
                key="gpu", name="GPU", role="算力", module="partner_gpu",
                workspace="ws-gpu", port_env="ACPS_GPU_PORT", default_port=8460,
                declared_capabilities=("gpu.status", "gpu.run"), aic=self.GPU_AIC),
        ]

    def _tool(self, protected=None, snap=None, raise_exc=None):
        """起桥 + 关掉探活/发现缓存，保证只测本工具的标量逻辑（不碰真实工作区文件）。"""
        from acps_cluster.agents import supervisor
        from unittest import mock

        from acps_cluster.delegation.bridge import BridgeHooks

        rt = self._RT(snap=snap, raise_exc=raise_exc)
        tool = supervisor._make_public_delegate_tool(
            self._roster(), protected=(protected if protected is not None
                                       else {self.GPU_AIC: ["gpu.run", "gpu.data.fetch"]}))

        async def go(args):
            class _Ev:
                pass

            ev = _Ev()
            ev.context = type("C", (), {})()
            ev.context.runtime, ev.context.state = rt, object()
            with mock.patch.object(supervisor, "_registered_aic_set", return_value=None), \
                 mock.patch.object(supervisor.cluster_roster, "load_discovered", return_value={}):
                await BridgeHooks().on_run_start(ev)   # 起桥：工具经 current_* 取 runtime/state
                return await tool.handler(args)

        return tool, rt, go

    def _call(self, args, **kw):
        _tool, rt, go = self._tool(**kw)
        return asyncio.run(go(args)), rt

    def test_入参全标量_载荷代码侧组装(self):
        out, rt = self._call({"partner_aic": self.SCOUT_AIC, "skill_id": "arxiv.search",
                             "request": "检索 GNN 补全知识图谱的近三年论文"})
        self.assertTrue(out["ok"], f"公开委派失败: {out}")
        self.assertEqual(out["taskRef"], "t-1")
        partner, request = rt.calls[0]
        self.assertEqual(partner, self.SCOUT_AIC)
        self.assertIsInstance(request, list)          # 模型不需要给出数组
        self.assertEqual(len(request), 1)
        self.assertEqual(request[0].text, "检索 GNN 补全知识图谱的近三年论文")

    def test_key与展示名解析成权威AIC后出站(self):
        for token in ("scout", "Scout"):
            out, rt = self._call({"partner_aic": token, "request": "跑一轮检索"})
            self.assertTrue(out["ok"], f"{token}: {out}")
            self.assertEqual(rt.calls[0][0], self.SCOUT_AIC, "出站必须是解析后的 AIC")
            self.assertEqual(out["partner_aic"], self.SCOUT_AIC)

    def test_受保护能力拒绝并指路(self):
        out, rt = self._call({"partner_aic": self.GPU_AIC, "skill_id": "gpu.run",
                             "request": "跑 3 折训练"})
        self.assertFalse(out["ok"])
        self.assertIn("acps_delegate", out["error"])
        self.assertIn("非公开能力", out["error"])
        self.assertEqual(rt.calls, [], "被拒时不该发出任何请求")

    def test_受保护伙伴缺skill_id时不猜(self):
        out, rt = self._call({"partner_aic": self.GPU_AIC, "request": "帮我跑个训练"})
        self.assertFalse(out["ok"])
        self.assertIn("acps_delegate", out["error"])
        self.assertIn("gpu.status", out["error"], "该当把该伙伴的公开能力一并指出来")
        self.assertIn("公开能力：gpu.status）", out["error"], "公开清单里不该混进受保护能力")
        self.assertEqual(rt.calls, [])

    def test_资源伙伴的公开能力放行(self):
        out, rt = self._call({"partner_aic": self.GPU_AIC, "skill_id": "gpu.status",
                             "request": "看一眼显卡占用"})
        self.assertTrue(out["ok"], f"公开能力应放行: {out}")
        self.assertEqual(rt.calls[0][0], self.GPU_AIC)

    def test_编造伙伴被委派点拦下(self):
        out, rt = self._call({"partner_aic": "AIC-NOT-EXIST", "request": "随便做点什么"})
        self.assertFalse(out["ok"])
        self.assertIn("委派目标校验未通过", out["error"])
        self.assertEqual(rt.calls, [], "闸门在出站之前")

    def test_错派能力被拦下(self):
        out, _rt = self._call({"partner_aic": self.SCOUT_AIC, "skill_id": "kb.query",
                              "request": "查知识库"})
        self.assertFalse(out["ok"])
        self.assertIn("不提供能力 'kb.query'", out["error"])

    def test_缺必填参数如实报错(self):
        out, _rt = self._call({"partner_aic": self.SCOUT_AIC, "request": "   "})
        self.assertFalse(out["ok"])
        self.assertIn("缺少必填参数", out["error"])

    def test_桥未就绪时报缺件不抛异常(self):
        from acps_cluster.agents import supervisor
        from unittest import mock

        from acps_cluster.delegation import bridge

        tool = supervisor._make_public_delegate_tool(self._roster(), protected={})

        async def go():
            bridge._RUN.set(None)   # 新任务上下文里显式清空：验证失败安全形态
            with mock.patch.object(supervisor, "_registered_aic_set", return_value=None), \
                 mock.patch.object(supervisor.cluster_roster, "load_discovered", return_value={}):
                return await tool.handler({"partner_aic": self.SCOUT_AIC, "request": "跑检索"})

        out = asyncio.run(go())
        self.assertFalse(out["ok"])
        self.assertIn("桥未就绪", out["error"])

    def test_传输层异常转成结构化结果(self):
        out, _rt = self._call({"partner_aic": self.SCOUT_AIC, "request": "跑检索"},
                              raise_exc=OSError("connection refused"))
        self.assertFalse(out["ok"])
        self.assertIn("委派失败", out["error"])
        self.assertIn("OSError", out["error"])

    def test_运行配置注册了本工具(self):
        from acps_cluster.agents import supervisor

        cfg = supervisor.build_run_config([])
        self.assertIn("acps_delegate_public", {t.name for t in cfg.tools})


class RosterDiscoveredSwitchTest(unittest.TestCase):
    """名册外 discovered 兜底的开关(ACPS_ROSTER_ALLOW_DISCOVERED,默认开)。"""

    def test_默认开启_显式关闭生效(self):
        import importlib
        import os

        from acps_cluster.core import cluster_roster

        env = "ACPS_ROSTER_ALLOW_DISCOVERED"
        old = os.environ.pop(env, None)
        try:
            self.assertTrue(cluster_roster.allow_discovered())
            os.environ[env] = "0"
            self.assertFalse(cluster_roster.allow_discovered())
            os.environ[env] = "1"
            self.assertTrue(cluster_roster.allow_discovered())
        finally:
            if old is None:
                os.environ.pop(env, None)
            else:
                os.environ[env] = old


class AuthenticatedDelegateAicTest(unittest.TestCase):
    """P1-3（2026-10-17 审查）：带令牌委派必须用**解析后的 AIC**。

    闸门认名册 key / 展示名 / AIC 三种写法，但令牌 audience 与出站目标只能是真 AIC。
    名册提示词恰恰让模型优先填 key，用原始串会签出 `aud="kb"` 的令牌、并以 "kb"
    作为委派目标 → 传输层失败、白烧一轮、目标伙伴一次调用量都记不上。
    """

    KB_AIC = "1.2.156.3088.1.BUPT.TEST00.KB0000.000000.00KB"

    class _RT:
        def __init__(self):
            self.calls = []

        async def aip_delegate(self, state, *, partner_aic, request):
            self.calls.append((partner_aic, request))
            return {"taskRef": "t-auth-1", "state": "accepted"}

    def _roster(self):
        from acps_cluster.core import cluster_roster

        return [cluster_roster.RosterMember(
            key="kb", name="研枢-KB-Partner", role="知识库", module="partner_kb",
            workspace="ws-kb", port_env="ACPS_KB_PORT", default_port=8599,
            declared_capabilities=("kb.schema", "kb.query"), aic=self.KB_AIC)]

    def _call(self, token_used):
        from acps_cluster import delegation
        from acps_cluster.agents import supervisor
        from types import SimpleNamespace
        from unittest import mock

        from acps_cluster.delegation.bridge import BridgeHooks

        rt = self._RT()
        tool = supervisor._make_authenticated_delegate_tool(self._roster())
        issued_with: dict = {}

        def _fake_issuer(_ws):
            async def _h(args):
                issued_with.update(args)
                return {"ok": True, "handles": [{"jti": "J1"}]}
            return SimpleNamespace(handler=_h)

        fake_vault = SimpleNamespace(peek=lambda jti: SimpleNamespace(token="JWS"))

        async def go():
            class _Ev:
                pass

            ev = _Ev()
            ev.context = type("C", (), {})()
            ev.context.runtime, ev.context.state = rt, object()
            with mock.patch.object(supervisor, "_registered_aic_set", return_value=None), \
                 mock.patch.object(supervisor.cluster_roster, "load_discovered", return_value={}), \
                 mock.patch.object(delegation, "build_issue_token_tool_from_workspace", _fake_issuer), \
                 mock.patch.object(delegation, "DEFAULT_TOKEN_VAULT", fake_vault):
                await BridgeHooks().on_run_start(ev)
                return await tool.handler({"partner_aic": token_used,
                                           "skill_id": "kb.query",
                                           "request": "查一下知识库结构"})

        return asyncio.run(go()), rt, issued_with

    def test_key与展示名解析成真AIC后签令牌并出站(self):
        for token in ("kb", "研枢-KB-Partner"):
            out, rt, issued = self._call(token)
            self.assertTrue(out.get("ok"), f"{token}: {out}")
            self.assertEqual(issued.get("target_aic"), self.KB_AIC,
                             f"{token}: 令牌 audience 必须是解析后的真 AIC")
            self.assertEqual(rt.calls[0][0], self.KB_AIC,
                             f"{token}: 出站目标必须是解析后的真 AIC")
            self.assertEqual(out["partner_aic"], self.KB_AIC)

    def test_直接给AIC时行为不变(self):
        out, rt, issued = self._call(self.KB_AIC)
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(issued.get("target_aic"), self.KB_AIC)
        self.assertEqual(rt.calls[0][0], self.KB_AIC)

    def test_闸门拒绝时不签令牌(self):
        # 能力不由该伙伴声明 → 闸门拦下，且**一次签名都不该发生**
        from acps_cluster import delegation
        from acps_cluster.agents import supervisor
        from unittest import mock

        called = {"n": 0}

        def _spy(_ws):
            called["n"] += 1
            raise AssertionError("被拒时不该签发令牌")

        tool = supervisor._make_authenticated_delegate_tool(self._roster())

        async def go():
            with mock.patch.object(supervisor, "_registered_aic_set", return_value=None), \
                 mock.patch.object(supervisor.cluster_roster, "load_discovered", return_value={}), \
                 mock.patch.object(delegation, "build_issue_token_tool_from_workspace", _spy):
                return await tool.handler({"partner_aic": "kb", "skill_id": "gpu.run",
                                           "request": "跑训练"})

        out = asyncio.run(go())
        self.assertFalse(out.get("ok"))
        self.assertIn("委派目标校验未通过", out.get("error") or "")
        self.assertEqual(called["n"], 0)


class BuiltinDelegateGateTest(unittest.TestCase):
    """内置 acps_delegate 也走委派点闸门（2026-10-17 审查）。

    此前闸门只在两个标量工具里，模型改用内置工具即可绕过"目标必须真实存在"。
    内置工具不携带 skill id，所以这里验证目标校验与"拒绝时不交给下游"。
    """

    SCOUT_AIC = "AIC-SCOUT-GATE"

    def _roster(self):
        from acps_cluster.core import cluster_roster

        return [cluster_roster.RosterMember(
            key="scout", name="Scout", role="", module="partner_scout",
            workspace="ws-scout", port_env="ACPS_SCOUT_PORT", default_port=8445,
            declared_capabilities=("arxiv.search",), aic=self.SCOUT_AIC)]

    def _call(self, name, args, *, allowed=True, why="ok"):
        from acps_cluster.agents import supervisor
        from types import SimpleNamespace
        from unittest import mock

        hook = supervisor.DelegationTargetGateHooks(roster=self._roster())
        called: list = []

        async def handler(tool_call):  # noqa: ANN001
            called.append(tool_call)
            return {"ok": True, "taskRef": "t-gate"}

        async def go():
            with mock.patch.object(supervisor, "_check_delegation_target_async",
                                   return_value=(allowed, why)):
                return await hook.wrap_tool_call(
                    None, SimpleNamespace(name=name, arguments=args), handler)

        return asyncio.run(go()), called

    def test_编造伙伴被拦下且不交给下游(self):
        out, called = self._call("acps_delegate",
                                 {"partner_aic": "AIC-NOT-EXIST", "request": "x"},
                                 allowed=False, why="不在名册也不在发现缓存里")
        self.assertFalse(out.get("ok"))
        self.assertIn("委派目标校验未通过", out.get("error") or "")
        self.assertEqual(called, [], "拒绝必须发生在调用下游（签发令牌/出站）之前")

    def test_放行后原样交给下游(self):
        out, called = self._call("acps_delegate",
                                 {"partner_aic": self.SCOUT_AIC, "request": ["x"]})
        self.assertTrue(out.get("ok"))
        self.assertEqual(len(called), 1)

    def test_非委派工具完全透传(self):
        out, called = self._call("plan_gate", {"x": 1}, allowed=False)
        self.assertTrue(out.get("ok"))
        self.assertEqual(len(called), 1)

    def test_优先级早于令牌签发(self):
        from acps_cluster.agents import supervisor

        self.assertLess(supervisor.DelegationTargetGateHooks.priority,
                        supervisor.ResourceDispatchAuthHooks.priority,
                        "拒绝必须先于 ResourceDispatchAuthHooks 签发令牌")


class IntakePolicyTest(unittest.TestCase):
    """开工前 intake：关键参数缺失先问一次，参数齐全/按默认直接开工（2026-10-17）。"""

    def test_提示词包含开工前intake(self):
        from acps_cluster.agents import supervisor

        text = supervisor._INBOUND_INTAKE
        self.assertIn("开工前 intake", text)
        self.assertIn("aip_request_input", text)
        self.assertIn("字数", text)
        self.assertIn("按默认", text)
        self.assertIn("最多澄清一次", text)
        self.assertIn("不要提交产物", text)
        self.assertIn("查作业", text)
        self.assertNotIn("绝不允许再回一句", text,
                         "旧硬禁令会让模型先干一半再让用户改")

    def test_空payload仍走输入请求(self):
        from acps_cluster.agents import supervisor

        text = supervisor._INBOUND_INTAKE
        self.assertIn("完全没有任何用户诉求", text)
        self.assertIn("编排方握手", text)


class JobModeToolRegistrationTest(unittest.TestCase):
    """作业进程内不再注册 submit_job（2026-10-17 审查 P1-2）。

    `jobs_runner` 在启动 supervisor 前设置 ACPS_JOB_REPORT_OUT；构建 run 配置时
    据此判定 job 模式，不给作业 run 再挂 submit_job（防子作业链续命）。
    """

    def _names(self):
        from acps_cluster.agents import supervisor

        return {t.name for t in supervisor.build_run_config([]).tools}

    def test_作业模式不注册submit_job(self):
        from unittest import mock

        with mock.patch.dict(os.environ,
                             {"ACPS_JOB_REPORT_OUT": "/tmp/job/report.md"}):
            names = self._names()
        self.assertNotIn("submit_job", names)
        self.assertIn("check_job", names, "查询作业的能力保留，取件不需要派生作业")

    def test_常驻模式仍注册submit_job(self):
        from unittest import mock

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ACPS_JOB_REPORT_OUT", None)
            names = self._names()
        self.assertIn("submit_job", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
