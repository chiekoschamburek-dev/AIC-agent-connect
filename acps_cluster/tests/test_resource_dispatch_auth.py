"""单测：透明补令牌钩子 —— 委派受保护伙伴时自动附令牌，其余伙伴透传。

## 背景（2026-10-11）

CPU/GPU/KB 的干活能力要求持令牌，**连 leader 也不例外**。但 leader 有 13 处委派点、
模型会用通用的 `acps_delegate`，只靠 playbook 提醒"改用 acps_delegate_authenticated"不可靠。

实测确认 `acps_delegate` **不在** ToolManager 里（运行期间由 `AipDelegationHooks` 注入），
所以 `ctx.add_tool` 换不到它；而 `wrap_tool_call` 是 wit 的标准钩子——
`ResourceDispatchAuthHooks` 用它包一层：目标伙伴在受保护名册里 → 签发令牌 →
把 `request` 从纯文本换成 `[TextPart, DataPart(令牌)]`。

## 覆盖

1. 受保护伙伴 → 载荷被换成 Part 列表且含 `delegation-token` DataItem
2. 非受保护伙伴 → **完全透传**（载荷不变）
3. 非 `acps_delegate` 工具 → 透传（不干扰其它工具）
4. 令牌**确实**在出站载荷里（对端要靠它准入——这是设计要求，不是泄漏），
   且 `targetAic`/`targetSkillHint` 与目标一致；同时确认 token 不出现在
   **其它**字段（只有那个 DataItem 携带它）
5. 名册为空（资源 agent 未注册）→ 等价透传
6. 下游 handler 拿到的 arguments 里 `partner_aic` 等原有字段保持不变
"""

from __future__ import annotations

import asyncio
import os
import unittest
import sys
from pathlib import Path
from types import SimpleNamespace

CL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CL))
sys.path.insert(0, str(CL.parent))
os.environ["YANSHU_RUN_TRACE"] = "0"

from acps_cluster.agents import supervisor as s  # noqa: E402

PROTECTED = "1.2.156.3088.1.BUPT.VTF97R.CPUAAA.CCCCCC.DDDD"
OTHER = "1.2.156.3088.1.BUPT.VTF97R.GMT5S6.4SZM68.0LC5"   # scout（白名单内）


class _RT:
    def __init__(self) -> None:
        self.calls: list = []

    async def aip_delegate(self, state, *, partner_aic, request):
        self.calls.append((partner_aic, list(request)))
        return {"task_ref": "t-1", "state": "accepted"}


def _hook(runtime=None) -> s.ResourceDispatchAuthHooks:
    h = s.ResourceDispatchAuthHooks(protected={PROTECTED: ["cpu.run"]})
    h._run = SimpleNamespace(runtime=runtime or _RT(), state=object())
    return h


DEV_KEYS = (CL / "ws-supervisor" / "acps" / "client" / "private-key.pem").is_file()
SKIP = "需要本地 dev CA 私钥；先按 docs/runbook-add-new-agent.md 铸造 dev 身份"


class _Call:
    def __init__(self, name: str, arguments: dict) -> None:
        self.name = name
        self.arguments = arguments
        self.id = "call-1"


def _parts_of(req):
    if isinstance(req, str):
        return [("text", req)]
    out = []
    for p in req or []:
        cls = type(p).__name__
        if cls == "TextPart":
            out.append(("TextPart", getattr(p, "text", "")))
        elif cls == "DataPart":
            out.append(("DataPart", getattr(p, "metadata", {}).get("role")))
        else:
            out.append((cls, ""))
    return out


def _token_item(req):
    for p in req or []:
        if type(p).__name__ == "DataPart":
            return p
    return None


@unittest.skipUnless(DEV_KEYS, SKIP)
class WrapToolCallDirectTest(unittest.TestCase):
    """受保护伙伴的委派必须在代码侧直接 aip_delegate。

    2026-10-26 缺陷：把 Part 列表塞回内置 acps_delegate 的 args，内置工具按
    wire-format 校验必然 `request must be of type 'string'` / `Invalid high-level
    content input`，令牌根本没到 partner。修复后 handler 不再被调用。
    """

    def test_受保护伙伴直接发起且载荷含令牌(self):
        rt = _RT()
        h = _hook(rt)
        called = {"handler": 0}

        async def handler(call):
            called["handler"] += 1
            return {"ok": True}

        out = asyncio.run(h.wrap_tool_call(
            None, _Call("acps_delegate", {"partner_aic": PROTECTED, "request": "跑一次 CPU 任务"}),
            handler))
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(called["handler"], 0, "不应再把 Part 列表交给内置工具")
        self.assertEqual(len(rt.calls), 1)
        partner, parts = rt.calls[0]
        self.assertEqual(partner, PROTECTED)
        rendered = _parts_of(parts)
        self.assertIn(("TextPart", "跑一次 CPU 任务"), rendered)
        self.assertIn(("DataPart", "delegation-token"), rendered)
        item = _token_item(parts)
        self.assertIsNotNone(item)
        self.assertEqual(item.data.get("targetAic"), PROTECTED)
        self.assertEqual(item.data.get("targetSkillHint"), "cpu.run")
        self.assertEqual(h.last_skill, "cpu.run")

    def test_非受保护伙伴无bundle时原样透传(self):
        rt = _RT()
        h = _hook(rt)
        h._TASK_SCOPE.set(None)
        seen = {}

        async def handler(call):
            seen["args"] = call.arguments
            return {"ok": True}

        asyncio.run(h.wrap_tool_call(
            None, _Call("acps_delegate", {"partner_aic": OTHER, "request": "检索文献"}), handler))
        self.assertEqual(seen["args"], {"partner_aic": OTHER, "request": "检索文献"})
        self.assertEqual(rt.calls, [])

    def test_authenticated透传给自己的handler(self):
        rt = _RT()
        h = _hook(rt)
        seen = {}

        async def handler(call):
            seen["name"] = call.name
            seen["args"] = dict(call.arguments)
            return {"ok": True}

        asyncio.run(h.wrap_tool_call(
            None, _Call("acps_delegate_authenticated",
                        {"partner_aic": PROTECTED, "skill_id": "cpu.run", "request": "x"}),
            handler))
        self.assertEqual(seen["name"], "acps_delegate_authenticated")
        self.assertEqual(seen["args"]["request"], "x")
        self.assertEqual(rt.calls, [], "authenticated 的令牌由它自己的 handler 签发")

    def test_其他工具完全不受影响(self):
        h = _hook()
        seen = {}

        async def handler(call):
            seen["args"] = call.arguments
            return {"ok": True}

        asyncio.run(h.wrap_tool_call(None, _Call("plan_gate", {"plan": "x"}), handler))
        self.assertEqual(seen["args"], {"plan": "x"})

    def test_空名册透传(self):
        h = s.ResourceDispatchAuthHooks(protected={})
        h._run = SimpleNamespace(runtime=_RT(), state=object())
        h._TASK_SCOPE.set(None)
        seen = {}

        async def handler(call):
            seen["args"] = call.arguments
            return {"ok": True}

        asyncio.run(h.wrap_tool_call(
            None, _Call("acps_delegate", {"partner_aic": PROTECTED, "request": "y"}), handler))
        self.assertEqual(seen["args"]["request"], "y")

    def test_workspace技能从请求文本推断(self):
        h = s.ResourceDispatchAuthHooks(protected={
            PROTECTED: ["ws.create", "ws.read", "ws.write"]})
        self.assertEqual(
            h._skill_for(PROTECTED, {"request": "先调用 ws.create，再调用 ws.read"}),
            "ws.create")
        self.assertEqual(
            h._skill_for(PROTECTED, {"request": "请 ws.read 这个文件"}), "ws.read")
        self.assertEqual(h._skill_for(PROTECTED, {"request": "没有点名"}), "ws.create")


class HookProtocolGuardTest(unittest.TestCase):
    """守卫：supervisor 的 run-hooks 必须满足 wit 协议（都继承 WitHooks）。

    教训（2026-10-12/13 生产故障，平台侧显示 "Agent execution failed."）：
    `ResourceDispatchAuthHooks` 当时**没有基类** → wit 在每次 run 起步取
    `type(hook).wrap_run` → AttributeError → 所有入站调用 **0 步**秒失败，
    连续两天每一条叮当调用都死在这里。本测试若当时存在即可拦住。
    """

    def test_全部run_hook继承WitHooks且有协议方法(self):
        from wit import WitHooks

        agent = s.build_supervisor_agent(None)
        self.assertTrue(agent.hooks, "supervisor 至少应挂一个 run hook")
        for hook in agent.hooks:
            with self.subTest(hook=type(hook).__name__):
                self.assertIsInstance(hook, WitHooks)
                for attr in ("wrap_run", "state", "on_run_start", "wrap_tool_call"):
                    self.assertTrue(
                        hasattr(type(hook), attr),
                        f"{type(hook).__name__} 缺协议方法 {attr}（未继承 WitHooks？）",
                    )

    def test_资源派发钩子自身合规(self):
        from wit import WitHooks

        h = s.ResourceDispatchAuthHooks(protected={})
        self.assertIsInstance(h, WitHooks)
        self.assertTrue(hasattr(type(h), "wrap_run"))


@unittest.skipUnless(DEV_KEYS, SKIP)
class KeyToAicNormalizationTest(unittest.TestCase):
    """模型填名册 key 时也必须补上令牌（2026-10-17 审查）。

    `protected` 以 **AIC** 为键，而提示词让模型**优先填 key**（"不要自己写 AIC"）。
    归一之前：传 "cpu" → `protected.get("cpu")` 落空 → 判为"非受保护伙伴"原样透传、
    **不附令牌**；而资源伙伴跑 `ACPS_PEP_LEADER_DIRECT=0`，结果 `deny / no-credential`
    或按 unscoped-narrowed 放行但非公开能力已被剥掉——受保护能力永远调不通。
    """

    def _roster(self):
        from acps_cluster.core import cluster_roster

        return [cluster_roster.RosterMember(
            key="cpu", name="研枢-CPU-Partner", role="算力", module="partner_cpu",
            workspace="ws-cpu", port_env="ACPS_CPU_PORT", default_port=8461,
            declared_capabilities=("cpu.run", "cpu.env"), aic=PROTECTED)]

    def test_resolve_aic_认key与展示名(self):
        h = s.ResourceDispatchAuthHooks(protected={PROTECTED: ["cpu.run"]},
                                       roster=self._roster())
        self.assertEqual(h._resolve_aic("cpu"), PROTECTED)
        self.assertEqual(h._resolve_aic("研枢-CPU-Partner"), PROTECTED)
        self.assertEqual(h._resolve_aic(PROTECTED), PROTECTED, "本来就是 AIC 时应原样返回")

    def test_resolve_aic_名册外原样返回(self):
        h = s.ResourceDispatchAuthHooks(protected={PROTECTED: ["cpu.run"]},
                                       roster=self._roster())
        self.assertEqual(h._resolve_aic("AIC-NOT-IN-ROSTER"), "AIC-NOT-IN-ROSTER")
        self.assertEqual(h._resolve_aic(""), "")

    def test_用key也能命中受保护表(self):
        h = s.ResourceDispatchAuthHooks(protected={PROTECTED: ["cpu.run"]},
                                       roster=self._roster())
        # 归一前：直接拿 key 查表必然落空（这正是 bug 本身）
        self.assertIsNone(h._skill_for("cpu", {}))
        # 归一后：命中
        self.assertEqual(h._skill_for(h._resolve_aic("cpu"), {}), "cpu.run")

    def test_wrap_tool_call_用key时下游拿到AIC(self):
        h = s.ResourceDispatchAuthHooks(protected={PROTECTED: ["cpu.run"]},
                                       roster=self._roster())
        rt = _RT()
        h._run = SimpleNamespace(runtime=rt, state=object())

        async def handler(call):
            raise AssertionError("受保护伙伴不应交给内置工具")

        asyncio.run(h.wrap_tool_call(
            None, _Call("acps_delegate", {"partner_aic": "cpu", "request": "跑一次 CPU 任务"}),
            handler))
        # last_skill 在签发之前就已置位：非空即证明"key → 受保护表"这条路走通了
        self.assertEqual(h.last_skill, "cpu.run",
                         "用名册 key 也必须识别为受保护伙伴并选定能力")
        self.assertEqual(rt.calls[0][0], PROTECTED, "下游委派目标必须是 AIC，不能是名册 key")
        item = _token_item(rt.calls[0][1])
        self.assertIsNotNone(item)
        self.assertEqual(item.data.get("targetAic"), PROTECTED)


@unittest.skipUnless(DEV_KEYS, SKIP)
class WorkspaceBundleDispatchTest(unittest.TestCase):
    """委派研究伙伴时随任务附 ws.list/ws.read/ws.write 三枚令牌。

    2026-10-26：bundle 令牌必须 delegate=partner / target=workspace / task_id 作用域；
    投递项不带 targetSkillHint（否则会被对端 PEP 当成本请求的 skill）。
    """

    WS_AIC = "1.2.156.3088.1.BUPT.VTF97R.WSAAAA.WSWWWW.WSWWWW"
    EXP_AIC = "1.2.156.3088.1.BUPT.VTF97R.EXPAAA.EXPWWW.EXPWWW"
    SCOUT_AIC = OTHER

    class _Issuer:
        def __init__(self):
            self.calls: list = []

        async def handler(self, args):
            self.calls.append(dict(args))
            return {"ok": True, "handles": [{"jti": f"j-{args['skill_id']}"}]}

    class _Vault:
        @staticmethod
        def peek(jti):
            return SimpleNamespace(token=f"JWS-{jti[2:]}")

    def _roster(self):
        from acps_cluster.core import cluster_roster

        return [
            cluster_roster.RosterMember(
                key="scout", name="研枢-Scout", role="检索", module="partner_scout",
                workspace="ws-scout", port_env="ACPS_SCOUT_PORT", default_port=8445,
                declared_capabilities=("arxiv.search",), aic=self.SCOUT_AIC),
            cluster_roster.RosterMember(
                key="experiment", name="研枢-Experiment", role="实验", module="partner_experiment",
                workspace="ws-experiment", port_env="ACPS_EXP_PORT", default_port=8461,
                declared_capabilities=("exp.run",), aic=self.EXP_AIC),
        ]

    def _hook(self, rt):
        h = s.ResourceDispatchAuthHooks(
            protected={
                self.EXP_AIC: ["exp.run"],
                self.WS_AIC: ["ws.list", "ws.read", "ws.write"],
            },
            roster=self._roster(),
        )
        h._run = SimpleNamespace(runtime=rt, state=object())
        h._TASK_SCOPE.set("task-ws-1")
        return h

    def _run(self, hook, call, handler):
        from unittest import mock
        from acps_cluster import delegation

        issuer = self._Issuer()
        with mock.patch.object(delegation, "build_issue_token_tool_from_workspace",
                               lambda _ws: issuer), \
             mock.patch.object(delegation, "DEFAULT_TOKEN_VAULT", self._Vault()):
            out = asyncio.run(hook.wrap_tool_call(None, call, handler))
        return out, issuer

    def test_公开伙伴也随任务附工作区bundle(self):
        rt = _RT()
        h = self._hook(rt)

        async def handler(call):
            raise AssertionError("有 bundle 时应在代码侧直发")

        out, issuer = self._run(
            h, _Call("acps_delegate", {"partner_aic": self.SCOUT_AIC, "request": "读仓库"}),
            handler)
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(out.get("workspace_access"), ["ws.list", "ws.read", "ws.write"])
        self.assertEqual(len(rt.calls), 1)
        partner, parts = rt.calls[0]
        self.assertEqual(partner, self.SCOUT_AIC)
        token_items = [p for p in parts if type(p).__name__ == "DataPart"]
        self.assertEqual(len(token_items), 3, "公开伙伴只附 3 枚 ws 令牌，不附任务令牌")
        for item in token_items:
            self.assertEqual(item.data.get("targetAic"), self.WS_AIC)
            self.assertEqual(item.data.get("targetSkillHint"), "", "投递项不得带 skill hint")
            self.assertTrue(str(item.data.get("acpsDelegationToken") or "").startswith("JWS-ws."))
        self.assertEqual({c["skill_id"] for c in issuer.calls},
                         {"ws.list", "ws.read", "ws.write"})
        for call in issuer.calls:
            self.assertEqual(call["delegate_aic"], self.SCOUT_AIC)
            self.assertEqual(call["target_aic"], self.WS_AIC)
            self.assertEqual(call["task_id"], "task-ws-1")

    def test_受保护伙伴主令牌在前bundle在后(self):
        rt = _RT()
        h = self._hook(rt)

        async def handler(call):
            raise AssertionError("受保护伙伴不应交给内置工具")

        out, issuer = self._run(
            h, _Call("acps_delegate", {"partner_aic": "experiment", "request": "跑实验"}), handler)
        self.assertTrue(out.get("ok"), out)
        partner, parts = rt.calls[0]
        self.assertEqual(partner, self.EXP_AIC)
        token_items = [p for p in parts if type(p).__name__ == "DataPart"]
        self.assertEqual(len(token_items), 4, "1 枚任务令牌 + 3 枚工作区令牌")
        primary = token_items[0]
        self.assertEqual(primary.data.get("targetAic"), self.EXP_AIC)
        self.assertEqual(primary.data.get("targetSkillHint"), "exp.run")
        for item in token_items[1:]:
            self.assertEqual(item.data.get("targetAic"), self.WS_AIC)
            self.assertEqual(item.data.get("targetSkillHint"), "")
        targets = {c["target_aic"] for c in issuer.calls}
        self.assertEqual(targets, {self.EXP_AIC, self.WS_AIC})


if __name__ == "__main__":
    unittest.main(verbosity=2)
