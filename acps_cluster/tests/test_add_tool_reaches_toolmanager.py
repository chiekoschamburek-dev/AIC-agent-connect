"""验证 `HookContext.add_tool()` 是否真的把工具注册进 wit 的 ToolManager。

背景：`config.tools` 在 on_run_start 之前就被读进 ToolManager 快照，所以往里 append 无效
（2026-10-10 入站实测：模型自述"本地无 wait_partner 工具"）。改用 ctx.add_tool 后必须确认它有效。

不依赖模型 provider：直接构造 HookContext + ToolManager 调用官方 API。
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

CL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CL))
sys.path.insert(0, str(CL.parent))
os.environ["YANSHU_RUN_TRACE"] = "0"

from wit import RunConfig, Tool  # noqa: E402
from wit.kernel.capabilities import ToolManager  # noqa: E402
from wit.kernel.hooks import HookContext  # noqa: E402

import supervisor as s  # noqa: E402


async def real_ctx_injection() -> list[str]:
    """用真实的 HookContext/ToolManager 验证 add_tool 生效。"""
    tmp = Path(tempfile.mkdtemp(prefix="addtool-probe-"))
    cfg = RunConfig(max_steps=8)
    tm = ToolManager([])
    try:
        ctx = HookContext(
            runtime=None,          # type: ignore[arg-type]  本探测不用
            compiled=None,         # type: ignore[arg-type]
            state=None,            # type: ignore[arg-type]
            config=cfg,
            workspace=tmp,
            private_state={},
            tool_manager=tm,
        )
    except TypeError as exc:
        return [f"（无法构造 HookContext：{exc}）"]

    hook = s.SupervisorRunHooks()
    from types import SimpleNamespace
    await hook.on_run_start(SimpleNamespace(context=ctx))
    return sorted(tm.lookup().keys())


def main() -> int:
    print("=== 真实 HookContext + ToolManager 注入探测 ===")
    names = asyncio.run(real_ctx_injection())
    print(f"  ToolManager 里的工具：{names}")
    need = {"plan_gate", "review_gate", "product_gate", "wait_partner", "wait_many"}
    missing = sorted(need - set(names))
    if missing:
        print(f"  ✘ 未注册进 ToolManager：{missing}")
        print("  → add_tool 未生效，需要另找通道（wrap_tool_call / ModelRequestEvent.add_tool）")
        return 1
    print("  ✔ 全部注册进 ToolManager（模型可见性有保障）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
