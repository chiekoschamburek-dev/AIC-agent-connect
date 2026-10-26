"""P1「提前提交即永久丢终稿」回归测试：提交必须被缓冲并合并成**一次**。

现场依据（2026-10-11 审查 P1）：AIP 任务只接受一次 `aip_submit`，第二次起报
`AipError: This operation is not supported`。模型若中途交了半成品，终稿必然被拒、永久丢失
（10-10 那次 6809 字终稿就是这样丢的）。

修法：`on_run_start` 用 `ctx.add_tool` **同名替换** `aip_submit` 为缓冲版，
`wrap_run` 的 finally 里把全部缓冲**合并成一次**正式提交。

覆盖：
1. 模型多次调用（半成品 + 终稿）→ 正式提交**只有 1 次**，且内容是合并后的完整产物；
2. 同一产物 id 的多次提交 → 取正文更长的那份（终稿赢）；
3. 已成功提交过 → 不再重复提交（协议约束）；
4. run 抛异常 → finally 仍提交（wrap_run 的价值）；
5. 模型从未提交 → 不提交（保持"空文本/未提交不误交"的既有语义）；
6. **`wrap_run` 必须原样返回 `handler()` 的结果**（2026-08-11 生产事故回归：
   曾经在 `finally` 里 `return`，吞掉返回值 → `_run_core` 拿到 None →
   wit 在 run_finalize 读 `.status` 抛 AttributeError → 用户看到
   「研枢-Supervisor 失败 / Agent execution failed.」）；
7. 收尾提交抛异常也不能影响返回值；
8. **awaiting-input 收尾不代交产物、不代报失败**（2026-10-16 现网回归：
   模型用 `aip_request_input` 把任务置为 awaiting-input 后，收尾的 aip_submit 与
   aip_fail 都被协议拒——`AipError: This operation is not supported`；
   现网 trace 实测 aip_submit_auto 与 aip_fail_auto 双双报错）。
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
    """模拟 HookContext：记录 add_tool 的替换。"""

    def __init__(self, cfg) -> None:
        self.config = cfg
        self.added: dict[str, Tool] = {}

    def add_tool(self, tool, *, source: str = "hook"):  # noqa: ANN001, ANN202
        self.added[getattr(tool, "name", "?")] = tool
        return tool


def _orig_tools(calls: list, *, ok: bool = True):
    async def submit_handler(arguments):  # noqa: ANN001, ANN202
        calls.append(arguments)
        if not ok:
            raise RuntimeError("This operation is not supported")
        return {"ok": True, "state": "awaiting-completion"}

    async def fail_handler(arguments):  # noqa: ANN001, ANN202
        calls.append({"_fail": arguments})
        return {"ok": True}

    return [
        Tool(name="aip_submit", description="orig submit",
             input_schema={"type": "object"}, handler=submit_handler),
        Tool(name="aip_fail", description="fail",
             input_schema={"type": "object"}, handler=fail_handler),
    ]


def _text_of(products) -> str:
    out = []
    for p in products or []:
        for d in (p.get("data_items") or p.get("dataItems") or []):
            out.append(str(d.get("text") or ""))
    return "".join(out)


async def main() -> int:
    hook = s.SupervisorRunHooks()
    failures: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="submit-buffer-"))
    orig_ws = s.WORKSPACE
    s.WORKSPACE = tmp
    try:
        # 1+2) 多次调用（半成品 → 终稿）→ 合并成一次，终稿赢
        calls: list = []
        cfg = RunConfig(max_steps=8, tools=_orig_tools(calls))
        ctx = _Ctx(cfg)
        await hook.on_run_start(SimpleNamespace(context=ctx))
        buffering = ctx.added.get("aip_submit")
        print(f"  [0] aip_submit 已被替换为缓冲版: {buffering is not None}")

        async def _body():
            # 模型第一次交半成品
            await buffering.handler({"products": [
                {"id": "final", "name": "资料清单",
                 "data_items": [{"type": "text", "text": "半成品" * 10}]}]})
            # 第二次交终稿
            await buffering.handler({"products": [
                {"id": "final", "name": "综述终稿",
                 "data_items": [{"type": "text", "text": "终稿正文" * 200}]}]})
            return SimpleNamespace(final_text="终稿正文" * 200, status="completed", iterations=5)

        await hook.wrap_run(ctx, _body)
        submits = [c for c in calls if "_fail" not in c]
        print(f"  [1] 模型调用 2 次 → 正式提交 {len(submits)} 次（期望 1）")
        if len(submits) != 1:
            failures.append(f"[1] 提交次数={len(submits)}")
        body = _text_of(submits[0].get("products") if submits else [])
        print(f"  [2] 提交内容长度 {len(body)}，含终稿={'终稿正文' in body}，"
              f"含半成品={'半成品' in body}")
        if "终稿正文" not in body:
            failures.append("[2] 终稿未进入提交内容")
        if "半成品" in body:
            failures.append("[2] 半成品不该覆盖终稿（应取正文更长的那份）")

        # 3) 已成功提交过 → 不再重复
        calls2: list = []
        cfg2 = RunConfig(max_steps=8, tools=_orig_tools(calls2))
        ctx2 = _Ctx(cfg2)
        await hook.on_run_start(SimpleNamespace(context=ctx2))
        b2 = ctx2.added["aip_submit"]

        async def _body2():
            # 模拟"已成功提交过"：直接调**原始** handler，并把它标成成功
            st = hook._SUBMIT_STATE.get()
            if isinstance(st, dict):
                st["ok"] = True
                st["text_len"] = 5000
            await cfg2.tools[0].handler({"products": [{"id": "a", "data_items": [
                {"type": "text", "text": "x" * 5000}]}]})
            # 再走缓冲版（此时 flush 应因 ok=True 而跳过）
            await b2.handler({"products": [{"id": "b", "data_items": [
                {"type": "text", "text": "y" * 10}]}]})
            return SimpleNamespace(final_text="短", status="completed", iterations=2)

        await hook.wrap_run(ctx2, _body2)
        n2 = len([c for c in calls2 if "_fail" not in c])
        print(f"  [3] 已成功提交过 → 总提交 {n2} 次（期望 1，flush 不重复）")
        if n2 != 1:
            failures.append(f"[3] 提交次数={n2}")

        # 4) run 抛异常 → finally 仍提交
        calls3: list = []
        cfg3 = RunConfig(max_steps=8, tools=_orig_tools(calls3))
        ctx3 = _Ctx(cfg3)
        await hook.on_run_start(SimpleNamespace(context=ctx3))
        b3 = ctx3.added["aip_submit"]

        async def _boom():
            await b3.handler({"products": [{"id": "f", "data_items": [
                {"type": "text", "text": "异常前已产出的内容"}]}]})
            raise RuntimeError("run 炸了")

        try:
            await hook.wrap_run(ctx3, _boom)
        except RuntimeError:
            pass
        n3 = len([c for c in calls3 if "_fail" not in c])
        print(f"  [4] run 抛异常 → 仍提交 {n3} 次（期望 1）")
        if n3 != 1:
            failures.append(f"[4] 异常路径未提交：{n3}")

        # 5) 模型从未提交 → 不提交
        calls4: list = []
        cfg4 = RunConfig(max_steps=8, tools=_orig_tools(calls4))
        ctx4 = _Ctx(cfg4)
        await hook.on_run_start(SimpleNamespace(context=ctx4))

        async def _none():
            return SimpleNamespace(final_text="随便", status="completed", iterations=1)

        await hook.wrap_run(ctx4, _none)
        n4 = len([c for c in calls4 if "_fail" not in c])
        print(f"  [5] 从未提交 → 提交 {n4} 次（期望 0）")
        if n4 != 0:
            failures.append(f"[5] 多余提交：{n4}")

        # 6) ★ 生产事故回归：wrap_run 必须原样返回 handler() 的结果
        #    曾经在 finally 里 return → 吞掉返回值 → wit run_finalize 读 .status → AttributeError
        calls5: list = []
        cfg5 = RunConfig(max_steps=8, tools=_orig_tools(calls5))
        ctx5 = _Ctx(cfg5)
        await hook.on_run_start(SimpleNamespace(context=ctx5))
        b5 = ctx5.added["aip_submit"]
        sentinel = SimpleNamespace(final_text="终稿", status="completed", iterations=7)

        async def _body5():
            await b5.handler({"products": [{"id": "f", "data_items": [
                {"type": "text", "text": "内容" * 30}]}]})
            return sentinel

        got5 = await hook.wrap_run(ctx5, _body5)
        ok6 = got5 is sentinel
        print(f"  [6] wrap_run 返回值身份保持: {ok6}"
              f"（期望 True；status={getattr(got5, 'status', '★None★')}）")
        if not ok6:
            failures.append(f"[6] wrap_run 吞掉了返回值：拿到 {type(got5).__name__}"
                            f"（生产表现为 Agent execution failed.）")
        if getattr(got5, "status", None) != "completed":
            failures.append("[6] 返回值的 status 不是 completed")

        # 7) 收尾提交抛异常也不影响返回值
        calls6: list = []
        cfg6 = RunConfig(max_steps=8, tools=_orig_tools(calls6, ok=False))  # 提交必失败
        ctx6 = _Ctx(cfg6)
        await hook.on_run_start(SimpleNamespace(context=ctx6))
        b6 = ctx6.added["aip_submit"]
        sentinel2 = SimpleNamespace(final_text="终稿2", status="completed", iterations=3)

        async def _body6():
            await b6.handler({"products": [{"id": "f", "data_items": [
                {"type": "text", "text": "内容"}]}]})
            return sentinel2

        got6 = await hook.wrap_run(ctx6, _body6)
        print(f"  [7] 收尾提交失败时返回值身份保持: {got6 is sentinel2}（期望 True）")
        if got6 is not sentinel2:
            failures.append("[7] 收尾失败影响了返回值")

        # 8) ★ 2026-10-16 现网回归：run 经 aip_request_input 进入 awaiting-input 后，
        #    收尾不代交产物、也不代报失败（两者都会被协议拒，白报错）
        async def _ask_input():
            await hook.on_tool_response(SimpleNamespace(
                tool_call=SimpleNamespace(name="aip_request_input", arguments={}),
                response={"status": "success",
                          "result": {"ok": True, "state": "awaiting-input"}}))

        calls7: list = []
        cfg7 = RunConfig(max_steps=8, tools=_orig_tools(calls7))
        ctx7 = _Ctx(cfg7)
        await hook.on_run_start(SimpleNamespace(context=ctx7))

        async def _body7():
            await _ask_input()
            return SimpleNamespace(final_text="已向调用方发出信息请求", status="completed", iterations=2)

        await hook.wrap_run(ctx7, _body7)
        await hook.on_run_end(SimpleNamespace(result=SimpleNamespace(
            final_text="已向调用方发出信息请求", status="completed", iterations=2)))
        print(f"  [8] awaiting-input 收尾 → 提交/失败调用 {len(calls7)} 次（期望 0）")
        if len(calls7) != 0:
            failures.append(f"[8] awaiting-input 收尾误触发提交/代报失败：{len(calls7)} 次")

        # 9) 先缓冲过产物、后又请求输入 → flush 也不代交（协议会拒），且全链路无异常
        calls8: list = []
        cfg8 = RunConfig(max_steps=8, tools=_orig_tools(calls8))
        ctx8 = _Ctx(cfg8)
        await hook.on_run_start(SimpleNamespace(context=ctx8))
        b8 = ctx8.added["aip_submit"]

        async def _body8():
            await b8.handler({"products": [{"id": "x", "data_items": [
                {"type": "text", "text": "中途缓冲的内容"}]}]})
            await _ask_input()
            return SimpleNamespace(final_text="等调用方补充输入", status="completed", iterations=2)

        await hook.wrap_run(ctx8, _body8)
        await hook.on_run_end(SimpleNamespace(result=SimpleNamespace(
            final_text="等调用方补充输入", status="completed", iterations=1)))
        print(f"  [9] 先缓冲后请求输入 → 提交/失败调用 {len(calls8)} 次（期望 0）")
        if len(calls8) != 0:
            failures.append(f"[9] 缓冲+请求输入场景不应提交：{len(calls8)} 次")
    finally:
        s.WORKSPACE = orig_ws
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        for f in failures:
            print("  ✘", f)
        return 1
    print("✔ 通过：提交被缓冲并合并为一次，终稿胜出，异常路径也能交付。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
