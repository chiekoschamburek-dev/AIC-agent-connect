"""P0-1 回归测试：wait_partner 的耐心计数必须**跨工具重建**累计。

原缺陷（2026-10-11 审查 P0-1）：`_wait_rounds` 建在 `_make_wait_partner_tool` 工厂内，
而 `SupervisorRunHooks.on_run_start` 每次 run 都调 `build_run_config()` 重建工具
（内部重建 `wait_partner`）→ 闭包每轮新建 → `rounds` 恒为 1 → `ACPS_WAIT_MAX_ROUNDS`
的 `give_up` 分支永不触发 → 模型被反复告知"必须继续等"，一路烧到 200 步撞顶。

因此本测试**每轮都新建工具实例**（模拟 on_run_start 的重建），验证计数仍在累计。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

CL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CL))
sys.path.insert(0, str(CL.parent))
os.environ["YANSHU_RUN_TRACE"] = "0"
os.environ["ACPS_WAIT_MAX_ROUNDS"] = "3"      # 3 轮就该 give_up

from acps_cluster.agents import supervisor as s  # noqa: E402


class _FakeRuntime:
    """永远返回 still-working（模拟伙伴一直没做完）。"""

    async def aip_task(self, state, *, action: str, task_ref: str):  # noqa: ANN001, ANN202
        return {"task_ref": task_ref, "state": "working", "products": []}


async def main() -> int:
    failures: list[str] = []
    s._WAIT_ROUNDS.clear()
    rt = _FakeRuntime()
    task_ref = "task_p0_1_probe"

    print("=== 每轮都新建工具实例（模拟 on_run_start 的重建）===")
    for round_no in range(1, 5):
        # 关键：重新调工厂 → 若状态在闭包里，计数会从头开始
        tool = s._make_wait_partner_tool(rt, object(), default_seconds=0.05, poll_seconds=0.01)
        out = await tool.handler({"task_ref": task_ref, "max_seconds": 0.05})
        got = out.get("wait_rounds")
        give_up = bool(out.get("give_up"))
        note = str(out.get("note") or "")
        print(f"  第 {round_no} 次调用（新实例）→ wait_rounds={got} give_up={give_up}")
        print(f"      note: {note[:70]}")
        if got != round_no:
            failures.append(f"第 {round_no} 轮计数为 {got}（应为 {round_no}）——状态没跨实例保留")
        if round_no < 3 and give_up:
            failures.append(f"第 {round_no} 轮就 give_up（上限是 3）")
        if round_no >= 3 and not give_up:
            failures.append(f"第 {round_no} 轮应 give_up 但没有")
        if round_no >= 3 and "耐心已耗尽" not in note:
            failures.append(f"第 {round_no} 轮 note 未提示耐心耗尽")

    # 任务到终态后计数应清零
    print("\n=== 任务终态后计数清零 ===")

    class _Done(_FakeRuntime):
        async def aip_task(self, state, *, action: str, task_ref: str):  # noqa: ANN001, ANN202
            return {"task_ref": task_ref, "state": "completed",
                    "products": [{"id": "p", "dataItems": []}]}

    tool = s._make_wait_partner_tool(_Done(), object(), default_seconds=0.05, poll_seconds=0.01)
    out = await tool.handler({"task_ref": task_ref, "max_seconds": 0.05})
    left = s._WAIT_ROUNDS.get(task_ref)
    print(f"  终态返回 ok={out.get('ok')} state={out.get('state')}；计数残留={left}")
    if left is not None:
        failures.append("任务终态后未清零计数")

    print()
    if failures:
        for f in failures:
            print("  ✘", f)
        return 1
    print("✔ 通过：耐心计数跨工具重建累计，达到上限后正确 give_up，终态后清零。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
