"""入站产物提交：协议只允许提交一次 —— 兜底判断单测。

现场依据（2026-10-10 23:16–23:39 一次真实任务）：
- 第 1 次 `aip_submit`（资料清单）**成功** → 任务进入 `awaiting-completion`；
- 第 2/3/4 次（初稿、终稿）全部返回 `AipError: This operation is not supported`；
- 结果调用方只拿到资料清单，**终稿被协议拒收**。
结论：必须**最后一次性提交终稿**；兜底要按"是否成功提交"和"提交内容长度"判断。

覆盖：
1. 从未提交 → 兜底提交一次；
2. 提交过但失败（ok=False）→ 仍要兜底；
3. 成功提交且内容足够长 → 不重复提交；
4. 成功提交但只是半成品（比最终文本短很多）→ 兜底补交完整终稿；
5. run 失败 / 空文本 → 不提交。
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

import supervisor as s  # noqa: E402


class _Ctx:
    def __init__(self, cfg) -> None:
        self.config = cfg

    def add_tool(self, tool, *, source: str = "hook"):  # noqa: ANN001, ANN202
        return tool


def _submit_tool(calls: list, *, ok: bool = True) -> Tool:
    async def handler(arguments):  # noqa: ANN001, ANN202
        calls.append(arguments)
        if not ok:
            raise RuntimeError("This operation is not supported")
        return {"ok": True, "state": "awaiting-completion", "result": {"products": arguments.get("products") or []}}

    return Tool(name="aip_submit", description="submit",
                input_schema={"type": "object"}, handler=handler)


def _ev(name: str, args=None):
    return SimpleNamespace(tool_call=SimpleNamespace(name=name, arguments=args or {}), tool=None)


def _resp(name: str, *, ok: bool, text_len: int):
    return SimpleNamespace(
        tool_call=SimpleNamespace(name=name, arguments={}), tool=None,
        response={"status": "success" if ok else "error",
                  "result": {"products": [{"data_items": [{"type": "text", "text": "x" * text_len}]}]}})


def _run_end(text: str, status: str = "completed"):
    return SimpleNamespace(result=SimpleNamespace(final_text=text, status=status, iterations=3))


async def main() -> int:
    hook = s.SupervisorRunHooks()
    failures: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="submit-once-"))
    orig_ws = s.WORKSPACE
    s.WORKSPACE = tmp
    try:
        # 1) 从未提交 → 兜底一次
        calls: list = []
        cfg = RunConfig(max_steps=8, tools=[_submit_tool(calls)])
        await hook.on_run_start(SimpleNamespace(context=_Ctx(cfg)))
        await hook.on_run_end(_run_end("终稿正文"))
        print(f"  [1] 从未提交 → 兜底 {len(calls)} 次（期望 1）")
        if len(calls) != 1:
            failures.append(f"[1] {len(calls)}")

        # 2) 提交过但失败 → 仍兜底（此处只喂事件、不真调 handler，故期望 1 次=兜底那次）
        calls2: list = []
        cfg2 = RunConfig(max_steps=8, tools=[_submit_tool(calls2, ok=False)])
        await hook.on_run_start(SimpleNamespace(context=_Ctx(cfg2)))
        await hook.on_tool_request(_ev("aip_submit"))
        await hook.on_tool_response(_resp("aip_submit", ok=False, text_len=0))
        await hook.on_run_end(_run_end("终稿正文"))
        print(f"  [2] 提交过但失败 → 兜底 {len(calls2)} 次（期望 1：兜底补交）")
        if len(calls2) != 1:
            failures.append(f"[2] {len(calls2)}")

        # 3) 成功提交且内容足够长 → 不重复
        calls3: list = []
        cfg3 = RunConfig(max_steps=8, tools=[_submit_tool(calls3)])
        await hook.on_run_start(SimpleNamespace(context=_Ctx(cfg3)))
        await hook.on_tool_request(_ev("aip_submit"))
        await cfg3.tools[0].handler({"products": [{"id": "final", "data_items": []}]})
        await hook.on_tool_response(_resp("aip_submit", ok=True, text_len=5000))
        await hook.on_run_end(_run_end("短" * 100))   # 最终文本更短
        print(f"  [3] 成功提交(5000字) + 最终文本更短 → 总提交 {len(calls3)} 次（期望 1）")
        if len(calls3) != 1:
            failures.append(f"[3] 重复提交 {len(calls3)}")

        # 4) 成功提交但只是半成品 → 兜底补交完整终稿
        calls4: list = []
        cfg4 = RunConfig(max_steps=8, tools=[_submit_tool(calls4)])
        await hook.on_run_start(SimpleNamespace(context=_Ctx(cfg4)))
        await hook.on_tool_request(_ev("aip_submit"))
        await cfg4.tools[0].handler({"products": [{"id": "materials", "data_items": []}]})
        await hook.on_tool_response(_resp("aip_submit", ok=True, text_len=500))
        await hook.on_run_end(_run_end("终" * 6000))   # 终稿长得多
        print(f"  [4] 成功提交半成品(500字) 但终稿 6000 字 → 总提交 {len(calls4)} 次（期望 2）")
        if len(calls4) != 2:
            failures.append(f"[4] {len(calls4)}")

        # 5) 失败 run / 空文本 → 不提交
        calls5: list = []
        cfg5 = RunConfig(max_steps=8, tools=[_submit_tool(calls5)])
        await hook.on_run_start(SimpleNamespace(context=_Ctx(cfg5)))
        await hook.on_run_end(_run_end("文本", status="failed"))
        await hook.on_run_start(SimpleNamespace(context=_Ctx(cfg5)))
        await hook.on_run_end(_run_end("   "))
        print(f"  [5] 失败/空文本 → 提交 {len(calls5)} 次（期望 0）")
        if calls5:
            failures.append(f"[5] {len(calls5)}")
    finally:
        s.WORKSPACE = orig_ws
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        for f in failures:
            print("  ✘", f)
        return 1
    print("✔ 通过：按『是否成功提交 + 内容长度』判断，既不会漏交终稿也不会无谓重复。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
