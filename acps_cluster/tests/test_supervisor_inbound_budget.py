"""验证 SupervisorRunHooks 的入站预算抬升与工具补挂（不需要模型 provider）。

覆盖：
1. wit 入站默认 `RunConfig(max_steps=8)` → 被抬到 `ACPS_SUPERVISOR_MAX_STEPS`（默认 200）；
2. 调用方要求更大预算时**不下调**；
3. 工具补挂：空 tools 列表会补全；已有同名工具不重复补（避免 wit "Duplicate model tool name"）。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

CL = Path(__file__).resolve().parents[1]      # acps_cluster（测试在 tests/ 下）
sys.path.insert(0, str(CL))
sys.path.insert(0, str(CL.parent))

# 本测试直接调用 hook、事件是假的：必须关掉留痕，否则会把假 run 写进真实留痕文件
# （2026-10-10 实测：在服务器上跑本测试污染了 ws-supervisor/logs/run-trace/）。
os.environ["YANSHU_RUN_TRACE"] = "0"

from wit import RunConfig  # noqa: E402
from wit.kernel.runtime import RunStartEvent  # noqa: E402

from acps_cluster.agents import supervisor  # noqa: E402


class _Ctx:
    """模拟 wit 的 HookContext：记录 add_tool 调用（官方工具注入通道）。"""

    def __init__(self, cfg) -> None:
        self.config = cfg
        self.added: list[str] = []

    def add_tool(self, tool, *, source: str = "hook"):  # noqa: ANN001, ANN202
        self.added.append(getattr(tool, "name", "?"))
        return tool


def _event(cfg) -> SimpleNamespace:
    return SimpleNamespace(context=_Ctx(cfg))


async def main() -> int:
    hook = supervisor.SupervisorRunHooks()
    failures: list[str] = []

    # 双保险：即使留痕被误开，也写到临时目录而不是真实工作区
    import tempfile
    _tmp: Path | None = None
    _orig_ws = supervisor.WORKSPACE
    try:
        _tmp = Path(tempfile.mkdtemp(prefix="run-trace-selftest-"))
        supervisor.WORKSPACE = _tmp
    except Exception:  # noqa: BLE001
        _tmp = None

    # ── 1) 入站默认 8 步 → 抬到默认 200 ────────────────────────────────────
    os.environ.pop("ACPS_SUPERVISOR_INBOUND_MIN_STEPS", None)
    os.environ.pop("ACPS_SUPERVISOR_MAX_STEPS", None)
    cfg = RunConfig(max_steps=8)
    ev1 = _event(cfg)
    await hook.on_run_start(ev1)
    print(f"  [1] 入站默认 8 步 → max_steps={cfg.max_steps}  (期望 200)")
    if cfg.max_steps != 200:
        failures.append(f"[1] 未抬升：{cfg.max_steps}")
    tool_names = [t.name for t in cfg.tools]
    print(f"      config.tools 补挂：{tool_names}")
    print(f"      ctx.add_tool 注入：{ev1.context.added}   ← 模型可见性靠这条")
    for need in ("plan_gate", "product_gate", "review_gate", "wait_partner", "wait_many"):
        if need not in tool_names:
            failures.append(f"[1] config.tools 缺 {need}")
        if need not in ev1.context.added:
            failures.append(f"[1] ctx.add_tool 未注入 {need}")

    # ── 2) 调用方要求更大预算 → 不下调 ────────────────────────────────────
    cfg2 = RunConfig(max_steps=500)
    await hook.on_run_start(_event(cfg2))
    print(f"  [2] 调用方 500 步 → max_steps={cfg2.max_steps}  (期望 500，不下调)")
    if cfg2.max_steps != 500:
        failures.append(f"[2] 被下调：{cfg2.max_steps}")

    # ── 3) 已有同名工具 → 不重复 ──────────────────────────────────────────
    base = supervisor.build_run_config(supervisor.cluster_roster.load_roster())
    cfg3 = RunConfig(max_steps=8, tools=list(base.tools))
    before = len(cfg3.tools)
    await hook.on_run_start(_event(cfg3))
    names = [t.name for t in cfg3.tools]
    dup = {n for n in names if names.count(n) > 1}
    print(f"  [3] 已带全套工具({before}) → 补挂后 {len(names)} 个，重复={dup or '无'}")
    if dup:
        failures.append(f"[3] 出现重名：{dup}")

    # ── 4) 自定义预算生效 ────────────────────────────────────────────────
    os.environ["ACPS_SUPERVISOR_INBOUND_MIN_STEPS"] = "77"
    cfg4 = RunConfig(max_steps=8)
    await hook.on_run_start(_event(cfg4))
    print(f"  [4] ACPS_SUPERVISOR_INBOUND_MIN_STEPS=77 → max_steps={cfg4.max_steps}  (期望 77)")
    if cfg4.max_steps != 77:
        failures.append(f"[4] 未生效：{cfg4.max_steps}")
    os.environ.pop("ACPS_SUPERVISOR_INBOUND_MIN_STEPS", None)
    supervisor.WORKSPACE = _orig_ws
    if _tmp is not None:
        import shutil
        shutil.rmtree(_tmp, ignore_errors=True)

    print()
    if failures:
        print("失败项：")
        for f in failures:
            print("  ✘", f)
        return 1
    print("✔ 全部通过：入站预算已抬升到与出站同量级，三类工具均已补挂，无重名。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
