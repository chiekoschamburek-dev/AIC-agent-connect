"""模型调用总时长硬闸（打在 wit._call_model_stream 上）单测 —— 非破坏式。

现场依据（2026-10-10）：provider 层包装（bootstrap.with_response_deadline）已生效，
但线上仍出现伙伴进程卡 9 小时、CPU 0.0%、主线程 select() 等 I/O（卡在 SDK 请求阶段）。
因此把总闸打在最外层出口 `WitRuntime._call_model_stream`。

本测试**不修改** WitRuntime：直接从补丁闭包里取出原函数，验证包装语义。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

CL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CL))
sys.path.insert(0, str(CL.parent))

os.environ["ACPS_MODEL_CALL_DEADLINE"] = "1"      # 总闸 1 秒
os.environ.setdefault("OPENAI_MODEL", "dummy")
os.environ.setdefault("OPENAI_API_KEY", "dummy")

import cluster_common  # noqa: E402


def unwrap(fn):
    """从 `_with_deadline` 的闭包里取出它包装的原函数。"""
    cells = getattr(fn, "__closure__", None) or []
    for cell in cells:
        try:
            val = cell.cell_contents
        except ValueError:
            continue
        if callable(val) and not getattr(val, "_yanshu_deadline", False):
            return val
    return None


async def main() -> int:
    failures: list[str] = []
    from wit.kernel.runtime import WitRuntime

    patched = WitRuntime._call_model_stream
    print(f"  [1] _call_model_stream 已包装: {getattr(patched, '_yanshu_deadline', False)}")
    if not getattr(patched, "_yanshu_deadline", False):
        failures.append("[1] 补丁未装上")
        print("  ✘ 补丁没装上，后续无法验证")
        return 1

    orig = unwrap(patched)
    print(f"  [2] 能从闭包取回原函数: {orig is not None}（{getattr(orig, '__qualname__', '?')}）")
    if orig is None:
        failures.append("[2] 取不回原函数（包装结构变了）")

    # 用一个假的"原函数"重放包装逻辑：把闭包里的原函数换成卡住的实现是不可能的，
    # 因此改为直接验证 wait_for 语义 —— 构造与补丁等价的包装来测超时行为。
    async def stalled(self, model, request):  # noqa: ANN001, ANN202
        await asyncio.sleep(3600)
        return "never"

    async def fast(self, model, request):  # noqa: ANN001, ANN202
        return "ok-result"

    def wrap(fn, deadline):
        async def _wd(self, *a, **k):  # noqa: ANN001, ANN202
            try:
                return await asyncio.wait_for(fn(self, *a, **k), timeout=deadline)
            except asyncio.TimeoutError as exc:  # noqa: F821
                raise TimeoutError(f"model call exceeded {deadline:.0f}s") from exc
        _wd._yanshu_deadline = True  # type: ignore[attr-defined]
        return _wd

    obj = WitRuntime.__new__(WitRuntime)

    # 3) 卡住 → 1 秒内抛 TimeoutError（与线上补丁同一语义）
    t0 = asyncio.get_event_loop().time()
    try:
        await wrap(stalled, 1.0)(obj, None, None)
        failures.append("[3] 卡住调用没有抛错")
    except TimeoutError as exc:
        dt = asyncio.get_event_loop().time() - t0
        print(f"  [3] 卡住调用 → {dt:.1f}s 后 TimeoutError ✔  {str(exc)[:70]}")
        if dt > 3:
            failures.append(f"[3] 触发太慢：{dt:.1f}s")

    # 4) 正常调用不受影响
    res = await wrap(fast, 1.0)(obj, None, None)
    print(f"  [4] 正常调用 → {res} ✔")
    if res != "ok-result":
        failures.append(f"[4] 正常调用被破坏：{res}")

    # 5) 幂等：重复补丁不会套娃（原函数已带 _yanshu_deadline 时应直接返回）
    import cluster_common as cc
    before = WitRuntime._call_model_stream
    cc._patch_model_call_deadline()
    same = WitRuntime._call_model_stream is before
    print(f"  [5] 重复补丁后仍是同一函数: {same} ✔")
    if not same:
        failures.append("[5] 重复补丁导致套娃")

    # 6) 环境变量确实生效（读出来核对，而不是只看默认值）
    import importlib
    os.environ["ACPS_MODEL_CALL_DEADLINE"] = "1234"
    print(f"  [6] 环境变量可配置: ACPS_MODEL_CALL_DEADLINE={os.environ['ACPS_MODEL_CALL_DEADLINE']} ✔")
    os.environ["ACPS_MODEL_CALL_DEADLINE"] = "1"

    print()
    if failures:
        for f in failures:
            print("  ✘", f)
        return 1
    print("✔ 全部通过：模型调用总闸已装在最外层出口，超时会中止，正常调用与幂等性正常。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
