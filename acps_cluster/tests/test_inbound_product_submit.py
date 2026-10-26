"""入站产物提交兜底（aip_submit）单测。

现场依据（2026-10-10）：叮当实测跑了 67 步、产出 6809 字综述，但 leader 从未调用
`aip_submit`，任务停在 `awaiting-completion`，调用方拿不到结果
（对端 2365 次 get 轮询后放弃）。

覆盖：
1. 模型调了 aip_submit → 兜底**不得**重复提交；
2. 模型没调 → run 正常结束时用最终文本自动提交一次，且内容为完整交付物；
3. run 失败/空文本 → 不提交；
4. 没有 aip_submit 工具（非入站 run）→ 不提交、不报错。
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

CL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CL))
sys.path.insert(0, str(CL.parent))
os.environ["YANSHU_RUN_TRACE"] = "0"

from wit import RunConfig, Tool  # noqa: E402

from acps_cluster.agents import supervisor as s  # noqa: E402


class _Ctx:
    def __init__(self, cfg) -> None:
        self.config = cfg
        self.added: list[str] = []

    def add_tool(self, tool, *, source: str = "hook"):  # noqa: ANN001, ANN202
        self.added.append(getattr(tool, "name", "?"))
        return tool


def _submit_tool(calls: list) -> Tool:
    async def handler(arguments):  # noqa: ANN001, ANN202
        calls.append(arguments)
        return {"ok": True, "state": "completed"}

    return Tool(name="aip_submit", description="submit", input_schema={"type": "object"}, handler=handler)


def _fail_tool(calls: list) -> Tool:
    async def handler(arguments):  # noqa: ANN001, ANN202
        calls.append(arguments)
        return {"ok": True}

    return Tool(name="aip_fail", description="fail", input_schema={"type": "object"}, handler=handler)


def _run_end(text: str, status: str = "completed"):
    return SimpleNamespace(result=SimpleNamespace(final_text=text, status=status, iterations=3))


async def main() -> int:
    hook = s.SupervisorRunHooks()
    failures: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="submit-guard-"))
    orig_ws = s.WORKSPACE
    s.WORKSPACE = tmp

    try:
        # 1) 模型自己提交过 → 兜底不重复
        #    （需要同时给出"提交成功 + 内容足够长"的响应事件，兜底才不会补交）
        calls: list = []
        cfg = RunConfig(max_steps=8, tools=[_submit_tool(calls)])
        ev = SimpleNamespace(context=_Ctx(cfg))
        await hook.on_run_start(ev)
        await hook.on_tool_request(SimpleNamespace(
            tool_call=SimpleNamespace(name="aip_submit", arguments={"products": []}), tool=None))
        await cfg.tools[0].handler({"products": [{"id": "x", "data_items": []}]})
        await hook.on_tool_response(SimpleNamespace(
            tool_call=SimpleNamespace(name="aip_submit", arguments={}), tool=None,
            response={"status": "success",
                      "result": {"products": [{"data_items": [{"type": "text", "text": "x" * 500}]}]}}))
        await hook.on_run_end(_run_end("最终答案正文"))
        print(f"  [1] 模型已提交 → 总提交次数 {len(calls)}（期望 1，不重复）")
        if len(calls) != 1:
            failures.append(f"[1] 重复提交：{len(calls)} 次")

        # 2) 模型没提交 → 自动兜底一次，内容为完整最终文本
        calls2: list = []
        cfg2 = RunConfig(max_steps=8, tools=[_submit_tool(calls2)])
        await hook.on_run_start(SimpleNamespace(context=_Ctx(cfg2)))
        body = "综述终稿：" + "内容" * 500          # 1000+ 字符，验证不被截断
        await hook.on_run_end(_run_end(body))
        print(f"  [2] 模型未提交 → 自动提交 {len(calls2)} 次（期望 1）")
        if len(calls2) != 1:
            failures.append(f"[2] 兜底未触发：{len(calls2)} 次")
        else:
            prods = calls2[0].get("products") or []
            text = ""
            if prods:
                items = prods[0].get("data_items") or prods[0].get("dataItems") or []
                text = str(items[0].get("text") if items else "")
            print(f"      提交内容长度 {len(text)}（原文 {len(body)}），含前缀="
                  f"{text.startswith(hook._AUTO_SUBMIT_PREFIX)}")
            if body not in text:
                failures.append("[2] 提交内容不是完整交付物")
            if not prods or not prods[0].get("id"):
                failures.append("[2] product 缺 id")

        # 3) run 失败 → 不提交
        calls3: list = []
        cfg3 = RunConfig(max_steps=8, tools=[_submit_tool(calls3)])
        await hook.on_run_start(SimpleNamespace(context=_Ctx(cfg3)))
        await hook.on_run_end(_run_end("有文本但失败", status="failed"))
        print(f"  [3] run 失败 → 提交 {len(calls3)} 次（期望 0）")
        if calls3:
            failures.append("[3] 失败任务不该提交")

        # 4) 空文本 → 不提交
        calls4: list = []
        cfg4 = RunConfig(max_steps=8, tools=[_submit_tool(calls4)])
        await hook.on_run_start(SimpleNamespace(context=_Ctx(cfg4)))
        await hook.on_run_end(_run_end("   \n  "))
        print(f"  [4] 空文本 → 提交 {len(calls4)} 次（期望 0）")
        if calls4:
            failures.append("[4] 空文本不该提交")

        # 4b) 空产出 + 提供 aip_fail → 如实标记失败、不提交（2026-10-15 审查：空产出静默）
        callsS: list = []
        callsF: list = []
        cfg4b = RunConfig(max_steps=8, tools=[_submit_tool(callsS), _fail_tool(callsF)])
        await hook.on_run_start(SimpleNamespace(context=_Ctx(cfg4b)))
        await hook.on_run_end(_run_end("", status="budget_exhausted"))
        print(f"  [4b] 空产出+撞顶 → 提交 {len(callsS)} 次（期望 0），fail {len(callsF)} 次（期望 1）")
        if callsS:
            failures.append("[4b] 空产出不该提交")
        if len(callsF) != 1:
            failures.append(f"[4b] 空产出应如实标记失败，实得 {len(callsF)}")
        else:
            reason = str((callsF[0] or {}).get("reason") or "")
            if "空产出" not in reason:
                failures.append(f"[4b] fail 原因未说明空产出：{reason[:60]}")

        # 4c) 模型已成功提交过 + 终稿为空 → 不补交、也不误报失败
        callsS2: list = []
        callsF2: list = []
        cfg4c = RunConfig(max_steps=8, tools=[_submit_tool(callsS2), _fail_tool(callsF2)])
        await hook.on_run_start(SimpleNamespace(context=_Ctx(cfg4c)))
        await hook.on_tool_request(SimpleNamespace(
            tool_call=SimpleNamespace(name="aip_submit", arguments={"products": []}), tool=None))
        await cfg4c.tools[0].handler({"products": [{"id": "x", "data_items": []}]})
        await hook.on_tool_response(SimpleNamespace(
            tool_call=SimpleNamespace(name="aip_submit", arguments={}), tool=None,
            response={"status": "success",
                      "result": {"products": [{"data_items": [{"type": "text", "text": "y" * 300}]}]}}))
        await hook.on_run_end(_run_end(""))
        print(f"  [4c] 已提交+空终稿 → 提交 {len(callsS2)} 次（期望 1），fail {len(callsF2)} 次（期望 0）")
        if len(callsS2) != 1 or callsF2:
            failures.append(f"[4c] 已交付的 run 不应再动：submit={len(callsS2)} fail={len(callsF2)}")

        # 5) 非入站（没有 aip_submit 工具）→ 不提交、不报错
        cfg5 = RunConfig(max_steps=8, tools=[])
        await hook.on_run_start(SimpleNamespace(context=_Ctx(cfg5)))
        await hook.on_run_end(_run_end("普通 run 的文本"))
        print("  [5] 无 aip_submit 工具 → 正常结束，无异常 ✔")

        # 6) 模型提交过但抛错 → 兜底仍应尝试？（当前策略：called=True 就不兜底）
        calls6: list = []
        async def boom(arguments):  # noqa: ANN001, ANN202
            calls6.append(arguments)
            raise RuntimeError("submit failed")
        cfg6 = RunConfig(max_steps=8, tools=[Tool(name="aip_submit", description="s",
                                                 input_schema={"type": "object"}, handler=boom)])
        await hook.on_run_start(SimpleNamespace(context=_Ctx(cfg6)))
        try:
            await cfg6.tools[0].handler({"products": []})
        except RuntimeError:
            pass
        await hook.on_run_end(_run_end("正文"))
        print(f"  [6] 模型提交抛错 → 总提交 {len(calls6)} 次（当前策略记为已调用，不重复）")
    finally:
        s.WORKSPACE = orig_ws
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        for f in failures:
            print("  ✘", f)
        return 1
    print("✔ 全部通过：漏提交会被自动兜底，已提交/失败/空文本不会被误提交。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
