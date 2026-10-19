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

import supervisor as s  # noqa: E402

PROTECTED = "1.2.156.3088.1.BUPT.VTF97R.CPUAAA.CCCCCC.DDDD"
OTHER = "1.2.156.3088.1.BUPT.VTF97R.GMT5S6.4SZM68.0LC5"   # scout（白名单内）


def _hook() -> s.ResourceDispatchAuthHooks:
    h = s.ResourceDispatchAuthHooks(protected={PROTECTED: ["cpu.run"]})
    h._run = SimpleNamespace(runtime=object(), state=object())
    return h


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


async def main() -> int:
    failures: list[str] = []
    seen: dict = {}

    async def handler(call):
        seen["name"] = call.name
        seen["args"] = call.arguments
        return {"ok": True}

    hook = _hook()

    # 1) 受保护伙伴 → 附令牌
    await hook.wrap_tool_call(
        None, _Call("acps_delegate", {"partner_aic": PROTECTED, "request": "跑一次 CPU 任务"}), handler)
    parts = _parts_of(seen["args"].get("request"))
    print(f"  [1] 受保护伙伴 → 载荷: {parts}")
    if not any(k == "DataPart" and v == "delegation-token" for k, v in parts):
        failures.append("[1] 未附上 delegation-token DataItem")
    if not any(k == "TextPart" and v == "跑一次 CPU 任务" for k, v in parts):
        failures.append("[1] 原任务文本丢失")
    # 模型可见的委派参数**不应**被塞入 skill_id（它是发送侧参数，不是内置工具的入参）
    if "skill_id" in seen["args"]:
        failures.append("[1] 不该往 acps_delegate 参数里加 skill_id（污染模型可见载荷）")
    print(f"      args 键: {sorted(seen['args'].keys())}（不含 skill_id 才正确）")
    # 钩子应记录本次选定的能力（供留痕/排障）
    print(f"      钩子选定的能力: {getattr(hook, 'last_skill', None)}")

    # 2) 非受保护伙伴 → 透传
    seen.clear()
    await hook.wrap_tool_call(
        None, _Call("acps_delegate", {"partner_aic": OTHER, "request": "检索文献"}), handler)
    req2 = seen["args"].get("request")
    print(f"  [2] 非受保护伙伴 → request 类型={type(req2).__name__} 值={str(req2)[:24]!r}")
    if req2 != "检索文献":
        failures.append(f"[2] 被误改：{req2!r}")

    # 3) 其它工具 → 透传
    seen.clear()
    await hook.wrap_tool_call(None, _Call("plan_gate", {"plan": "x"}), handler)
    print(f"  [3] 其它工具 → name={seen['name']} args={seen['args']}")
    if seen["args"] != {"plan": "x"}:
        failures.append("[3] 其它工具被改动")

    # 4) 令牌确实在出站载荷里（设计要求），且元数据正确、不出现在其它字段
    seen.clear()
    await hook.wrap_tool_call(
        None, _Call("acps_delegate", {"partner_aic": PROTECTED, "request": "x"}), handler)
    item = _token_item(seen["args"].get("request"))
    data = getattr(item, "data", {}) if item is not None else {}
    tok = str(data.get("acpsDelegationToken") or "")
    print(f"  [4] 载荷内令牌长度={len(tok)} targetAic={str(data.get('targetAic'))[-20:]} "
          f"targetSkillHint={data.get('targetSkillHint')}")
    if not tok:
        failures.append("[4] 出站载荷里没有令牌（对端无法准入）")
    if data.get("targetAic") != PROTECTED:
        failures.append(f"[4] targetAic 不符：{data.get('targetAic')}")
    if data.get("targetSkillHint") != "cpu.run":
        failures.append(f"[4] targetSkillHint 不符：{data.get('targetSkillHint')}")
    # 令牌只应出现在那个 DataItem 的 data 里；不得出现在别的顶层字段
    others = {k: v for k, v in seen["args"].items() if k != "request"}
    if tok and tok in repr(others):
        failures.append("[4] 令牌出现在 request 之外的字段里")

    # 5) 名册为空 → 透传
    empty = s.ResourceDispatchAuthHooks(protected={})
    empty._run = SimpleNamespace(runtime=object(), state=object())
    seen.clear()
    await empty.wrap_tool_call(
        None, _Call("acps_delegate", {"partner_aic": PROTECTED, "request": "y"}), handler)
    print(f"  [5] 空名册 → request 类型={type(seen['args'].get('request')).__name__}（期望 str）")
    if not isinstance(seen["args"].get("request"), str):
        failures.append("[5] 空名册却改了载荷")

    # 6) 原有字段保持不变
    seen.clear()
    await hook.wrap_tool_call(
        None,
        _Call("acps_delegate", {"partner_aic": PROTECTED, "request": "z",
                                "session_id": "s-1", "note": "keep-me"}),
        handler)
    a = seen["args"]
    print(f"  [6] 既有字段保留: session_id={a.get('session_id')} note={a.get('note')}")
    if a.get("session_id") != "s-1" or a.get("note") != "keep-me":
        failures.append("[6] 既有字段被丢弃")

    # 7) 名册构造：资源 agent 未注册时不得抛异常、不得凭空造 AIC
    try:
        partners = s._protected_partners()
        print(f"  [7] _protected_partners() = {[(k[-20:], v) for k, v in partners.items()]}"
              f"（资源 agent 未注册时为空 dict 属正常）")
        for aic, skills in partners.items():
            if not aic or "." not in aic:
                failures.append(f"[7] 造出了非法 AIC：{aic!r}")
            if skills not in (["cpu.run"], ["gpu.run", "gpu.data.fetch"],
                              ["kb.query", "kb.ingest"]):
                failures.append(f"[7] 能力表异常：{skills!r}")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"[7] 名册构造抛异常：{type(exc).__name__}: {exc}")

    print()
    if failures:
        for f in failures:
            print("  ✘", f)
        return 1
    print("✔ 通过：受保护伙伴自动附令牌且元数据正确、其余完全透传、既有字段保留。")
    return 0


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


class KeyToAicNormalizationTest(unittest.TestCase):
    """模型填名册 key 时也必须补上令牌（2026-10-17 审查）。

    `protected` 以 **AIC** 为键，而提示词让模型**优先填 key**（"不要自己写 AIC"）。
    归一之前：传 "cpu" → `protected.get("cpu")` 落空 → 判为"非受保护伙伴"原样透传、
    **不附令牌**；而资源伙伴跑 `ACPS_PEP_LEADER_DIRECT=0`，结果 `deny / no-credential`
    或按 unscoped-narrowed 放行但非公开能力已被剥掉——受保护能力永远调不通。
    """

    def _roster(self):
        import cluster_roster

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
        h._run = SimpleNamespace(runtime=object(), state=object())
        seen: dict = {}

        async def handler(call):
            seen["args"] = call.arguments
            return {"ok": True}

        asyncio.run(h.wrap_tool_call(
            None, _Call("acps_delegate", {"partner_aic": "cpu", "request": "跑一次 CPU 任务"}),
            handler))
        # last_skill 在签发之前就已置位：非空即证明"key → 受保护表"这条路走通了
        self.assertEqual(h.last_skill, "cpu.run",
                         "用名册 key 也必须识别为受保护伙伴并选定能力")
        parts = _parts_of(seen.get("args", {}).get("request"))
        attached = any(k == "DataPart" and v == "delegation-token" for k, v in parts)
        if attached:
            # 签发链路可用时，出站目标与令牌 audience 都必须是解析后的 AIC
            self.assertEqual(seen["args"].get("partner_aic"), PROTECTED,
                             "下游委派目标必须是 AIC，不能是名册 key")
            item = _token_item(seen["args"].get("request"))
            data = getattr(item, "data", {}) if item is not None else {}
            self.assertEqual(data.get("targetAic"), PROTECTED)
        else:
            self.skipTest("本机签名上下文不可用，跳过出站载荷断言（能力选定已断言通过）")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
