"""生产事故回归：缓冲版 aip_submit 的**代理返回**不能被当成"真实提交成功"。

## 事故（2026-10-11 05:42，真实任务 task_c7a3424a17cf）

留痕显示：

```
[05:42:41] → aip_submit（模型提交 3 个产物，15187 字符）
[05:42:41] ★ aip_submit_buffered  {"products": 3, "chars": 15187}   ← 缓冲成功
[05:42:41] ★ aip_submit_result    {"ok": true, "text_len": 0}        ← ★ 误判
[05:42:46] ★ aip_submit_skipped_after_success {"buffered_products": 3}  ← flush 被跳过
[05:42:46] ★ aip_submit_auto      ← 兜底把 945 字摘要提交了
```

缓冲版返回 `{"ok": True, "state": "buffered"}`，`on_tool_response` 只看 `status == "success"`
就置 `state["ok"]=True` → `_flush_buffered_submit` 认为"已提交过"而跳过 →
**15187 字终稿被静默丢弃，兜底的 945 字摘要反而占掉唯一一次提交机会**。

修法：缓冲版返回带 `buffered: True` 判别标记；`on_tool_response` 据此**不**置 ok；
flush 成功后回写 `ok`/`text_len`，兜底不再重复提交。

## 覆盖

1. **完整链路**：模型走缓冲 → 代理返回事件 → wrap_run flush → on_run_end 兜底；
   断言正式提交**恰好一次**，且内容是 **15187 字那批产物**（不是摘要）。
2. 代理返回**不得**污染 `state["ok"]`。
3. flush 成功后兜底不重复提交。
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
        self.added: dict[str, Tool] = {}

    def add_tool(self, tool, *, source: str = "hook"):  # noqa: ANN001, ANN202
        self.added[getattr(tool, "name", "?")] = tool
        return tool


def _tools(calls: list):
    async def submit_handler(arguments):  # noqa: ANN001, ANN202
        calls.append(arguments)
        return {"ok": True, "state": "awaiting-completion"}

    async def fail_handler(arguments):  # noqa: ANN001, ANN202
        calls.append({"_fail": arguments})
        return {"ok": True}

    return [
        Tool(name="aip_submit", description="orig", input_schema={"type": "object"},
             handler=submit_handler),
        Tool(name="aip_fail", description="fail", input_schema={"type": "object"},
             handler=fail_handler),
    ]


def _proxy_ev(resp: dict):
    """模拟缓冲版 aip_submit 的**工具响应事件**（关键：result 带 buffered 标记）。"""
    return SimpleNamespace(
        tool_call=SimpleNamespace(name="aip_submit", arguments={}), tool=None,
        response={"status": "success", "result": resp},
    )


def _text_of(products) -> str:
    out = []
    for p in products or []:
        for d in (p.get("data_items") or p.get("dataItems") or []):
            out.append(str(d.get("text") or ""))
    return "".join(out)


async def main() -> int:
    hook = s.SupervisorRunHooks()
    failures: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="proxy-submit-"))
    orig_ws = s.WORKSPACE
    s.WORKSPACE = tmp
    try:
        calls: list = []
        cfg = RunConfig(max_steps=8, tools=_tools(calls))
        ctx = _Ctx(cfg)
        await hook.on_run_start(SimpleNamespace(context=ctx))
        proxy = ctx.added["aip_submit"]

        # 模型分两次交产物，合计三件（模仿真实：终稿 + 资料清单 + 溯源报告）
        long_text = "综述正文" * 2500          # 10000 字符
        materials = "资料清单" * 500           # 2000 字符
        report = "溯源报告" * 300              # 1200 字符
        total_expected = len(long_text) + len(materials) + len(report)

        st = hook._SUBMIT_STATE.get()
        observed: dict = {}

        async def _body():
            r1 = await proxy.handler({"products": [
                {"id": "final", "name": "综述终稿",
                 "data_items": [{"type": "text", "text": long_text}]},
                {"id": "materials", "name": "资料清单",
                 "data_items": [{"type": "text", "text": materials}]}]})
            # ★ 关键：投递**代理返回**事件（真实 run 里 wit 会发这个事件）
            await hook.on_tool_response(_proxy_ev(r1))
            r2 = await proxy.handler({"products": [
                {"id": "trace", "name": "溯源报告",
                 "data_items": [{"type": "text", "text": report}]}]})
            await hook.on_tool_response(_proxy_ev(r2))
            # 断言点：**代理返回之后、flush 之前**，state["ok"] 必须仍为 False。
            # 若被污染，_flush_buffered_submit 会在 finally 里直接跳过（事故根因）。
            observed["ok_before_flush"] = st.get("ok")
            observed["buf_chars"] = sum(
                len(str(d.get("text") or ""))
                for p in (st.get("buffers") or [])
                for d in (p.get("data_items") or p.get("dataItems") or []))
            return SimpleNamespace(final_text="已完成并提交。\n\n## 交付概览\n约 3000 字综述…",
                                   status="completed", iterations=52)

        result = await hook.wrap_run(ctx, _body)
        submits = [c for c in calls if "_fail" not in c]
        print(f"  [1] 代理返回后、flush 前 state['ok'] = {observed.get('ok_before_flush')}"
              f"（期望 False）；缓冲 {observed.get('buf_chars')} 字符")
        if observed.get("ok_before_flush"):
            failures.append("[1] 代理返回污染了 state['ok'] → flush 会被跳过（事故根因）")
        if observed.get("buf_chars") != total_expected:
            failures.append(f"[1] 缓冲字符数 {observed.get('buf_chars')} != {total_expected}")

        # 【2】正式提交恰好一次
        print(f"  [2] 正式提交次数 = {len(submits)}（期望 1）")
        if len(submits) != 1:
            failures.append(f"[2] 提交次数={len(submits)}")

        # 【3】提交内容必须是那批产物，不是摘要
        body = _text_of(submits[0].get("products") if submits else [])
        print(f"  [3] 提交正文 {len(body)} 字符（期望 {total_expected}），"
              f"含综述正文={'综述正文' in body}，含摘要句={'交付概览' in body}")
        if len(body) != total_expected:
            failures.append(f"[3] 提交正文长度 {len(body)} != {total_expected}")
        if "综述正文" not in body:
            failures.append("[3] 终稿正文未进入提交内容")
        if "交付概览" in body:
            failures.append("[3] 提交的是兜底摘要，不是终稿（事故重现）")

        # 【4】兜底不再重复提交
        await hook.on_run_end(SimpleNamespace(result=result))
        submits2 = [c for c in calls if "_fail" not in c]
        print(f"  [4] on_run_end 后总提交 {len(submits2)} 次（期望 1，兜底不重复）")
        if len(submits2) != 1:
            failures.append(f"[4] 兜底重复提交：{len(submits2)}")
    finally:
        s.WORKSPACE = orig_ws
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        for f in failures:
            print("  ✘", f)
        return 1
    print("✔ 通过：缓冲产物被完整提交，兜底不再抢占唯一一次提交机会。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
