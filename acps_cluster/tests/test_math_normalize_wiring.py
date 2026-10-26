"""接入测试：产物在提交前被规范化（supervisor 侧）。

配合 `tests/test_math_delimiters.py`（纯函数单测），这里验证**接入点真的生效**：
  1. `_flush_buffered_submit` 提交出去的 products 里，`\\[...\\]`/`\\(...\\)` 已换成 `$`/`$$`；
  2. 兜底自动提交（`on_run_end`）的文本同样被规范化；
  3. 代码块内的定界符不受影响（不误伤）；
  4. 正文内容（公式本体）不被改动。
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

BODY = r"""## 3 数学地基

前向过程：

\[
q(x_t\mid x_{t-1})=\mathcal N\!\left(\sqrt{1-\beta_t}x_{t-1},\beta_t I\right)
\]

反向过程为 \(p_\theta(x_{t-1}\mid x_t)=\mathcal{N}\left(\mu_\theta(x_t,t),\sigma_t^2 I\right)\)。

```python
# 代码块里的 \[ 不能被改
pat = r"\[a-z\]"
```
"""


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


def _text_of(products) -> str:
    out = []
    for p in products or []:
        for d in (p.get("data_items") or p.get("dataItems") or []):
            out.append(str(d.get("text") or ""))
    return "".join(out)


async def main() -> int:
    hook = s.SupervisorRunHooks()
    failures: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="math-norm-"))
    orig_ws = s.WORKSPACE
    s.WORKSPACE = tmp
    try:
        # 1) 正常路径：模型经缓冲提交 → flush 时规范化
        calls: list = []
        cfg = RunConfig(max_steps=8, tools=_tools(calls))
        ctx = _Ctx(cfg)
        await hook.on_run_start(SimpleNamespace(context=ctx))
        proxy = ctx.added["aip_submit"]

        async def _body():
            r = await proxy.handler({"products": [
                {"id": "final", "name": "综述终稿",
                 "data_items": [{"type": "text", "text": BODY}]}]})
            await hook.on_tool_response(SimpleNamespace(
                tool_call=SimpleNamespace(name="aip_submit", arguments={}), tool=None,
                response=r))
            return SimpleNamespace(final_text="已完成", status="completed", iterations=5)

        await hook.wrap_run(ctx, _body)
        submit = next(c for c in calls if "_fail" not in c)
        got = _text_of(submit.get("products"))
        # 判定"是否还有残留"必须**排除代码块**——代码块里的 \[ 是刻意保留的，
        # 笼统地查 "\\[" 会把这条正常保留误判成残留（我在这里错过一次）。
        code_kept = 'r"\\[a-z\\]"' in got
        from acps_cluster.core.math_delimiters import _split_code_segments  # noqa: PLC0415
        non_code = "".join(seg for is_code, seg in _split_code_segments(got) if not is_code)
        bs_open = "\\[" in non_code
        paren_open = "\\(" in non_code
        has_dd = "$$" in got
        has_inline = "$p_\\theta" in got
        body_kept = "q(x_t\\mid x_{t-1})" in got
        print(f"  [1] 代码块**之外**的反斜杠方括号残留={bs_open} 圆括号残留={paren_open}")
        print(f"      display 已转 $$$$={has_dd}  行内已转={has_inline}")
        print(f"      代码块保持={code_kept}  公式本体保持={body_kept}")
        if bs_open or paren_open:
            failures.append("[1] 非代码区域里仍有 LaTeX 定界符残留")
        if not has_dd:
            failures.append("[1] display 公式未被规范化")
        if not has_inline:
            failures.append("[1] 行内公式未被规范化")
        if not code_kept:
            failures.append("[1] 代码块被误改")
        if not body_kept:
            failures.append("[1] 公式本体被改动")

        # 2) 兜底路径：run 结束时自动提交摘要，也应规范化
        calls2: list = []
        cfg2 = RunConfig(max_steps=8, tools=_tools(calls2))
        ctx2 = _Ctx(cfg2)
        await hook.on_run_start(SimpleNamespace(context=ctx2))
        res2 = SimpleNamespace(final_text="结果：\\[E=mc^2\\] 以及 \\(a+b\\)。",
                               status="completed", iterations=2)
        await hook.on_run_end(SimpleNamespace(result=res2))
        got2 = _text_of(next((c.get("products") for c in calls2 if "_fail" not in c), []))
        print(f"  [2] 兜底提交已规范化: {'$$E=mc^2$$' in got2 and '$a+b$' in got2}")
        print(f"      片段: {got2[:90]!r}")
        if "$$E=mc^2$$" not in got2 or "$a+b$" not in got2:
            failures.append("[2] 兜底提交未规范化")

        # 3) 幂等：对已规范化的内容再跑一遍不产生变化
        calls3: list = []
        cfg3 = RunConfig(max_steps=8, tools=_tools(calls3))
        ctx3 = _Ctx(cfg3)
        await hook.on_run_start(SimpleNamespace(context=ctx3))
        proxy3 = ctx3.added["aip_submit"]

        async def _body3():
            r = await proxy3.handler({"products": [
                {"id": "f", "data_items": [{"type": "text", "text": "$$x=1$$ 与 $y=2$"}]}]})
            await hook.on_tool_response(SimpleNamespace(
                tool_call=SimpleNamespace(name="aip_submit", arguments={}), tool=None,
                response=r))
            return SimpleNamespace(final_text="ok", status="completed", iterations=1)

        await hook.wrap_run(ctx3, _body3)
        got3 = _text_of(next((c.get("products") for c in calls3 if "_fail" not in c), []))
        print(f"  [3] 已规范化的内容不被二次改动: {got3.strip() == '$$x=1$$ 与 $y=2$'}")
        if got3.strip() != "$$x=1$$ 与 $y=2$":
            failures.append(f"[3] 幂等失败：{got3!r}")
    finally:
        s.WORKSPACE = orig_ws
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        for f in failures:
            print("  ✘", f)
        return 1
    print("✔ 通过：提交与兜底两条路径都已规范化，内容与代码块不受影响。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
