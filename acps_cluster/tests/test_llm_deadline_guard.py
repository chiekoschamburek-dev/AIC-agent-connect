"""模型调用硬闸（响应级看门狗）单测：卡住的流必须变成"可观测的失败"。

现场依据（2026-10-10）：scout 的入站任务卡在 `working` 22 分钟、进程 CPU 0.0%、
主线程在 select() 等 I/O，而同 provider 用 curl 直测 2.7 秒即返回
→ httpx 的 timeout 是"读间隔"而非"总时长"，需要我们自己设总闸。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

CL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CL))
sys.path.insert(0, str(CL.parent))

os.environ["ACPS_LLM_FIRST_BYTE_TIMEOUT"] = "1"     # 测试里把硬闸调到 1 秒
os.environ["ACPS_LLM_CALL_TIMEOUT"] = "3"
os.environ.setdefault("OPENAI_MODEL", "dummy")
os.environ.setdefault("OPENAI_API_KEY", "dummy")

from acps_cluster.core import bootstrap  # noqa: E402


class _StallFirstByte:
    """首个片段永远不来。"""
    async def complete(self, request):  # noqa: ANN001, ANN202
        await asyncio.sleep(3600)

    async def stream(self, request):  # noqa: ANN001, ANN202
        await asyncio.sleep(3600)
        yield "never"


class _StallMidStream:
    """先给两个片段，然后永远停住（模拟"偶尔送字节"把读间隔超时拖过去）。"""
    async def complete(self, request):  # noqa: ANN001, ANN202
        await asyncio.sleep(3600)

    async def stream(self, request):  # noqa: ANN001, ANN202
        yield "chunk-1"
        yield "chunk-2"
        await asyncio.sleep(3600)
        yield "never"


class _Healthy:
    async def complete(self, request):  # noqa: ANN001, ANN202
        return {"ok": True}

    async def stream(self, request):  # noqa: ANN001, ANN202
        for i in range(3):
            yield f"chunk-{i}"


async def main() -> int:
    failures: list[str] = []

    # 1) 首片段不来 → 必须在 ~1s 内抛错
    p1 = bootstrap.with_response_deadline(_StallFirstByte())
    t0 = asyncio.get_event_loop().time()
    try:
        async for _ in p1.stream(None):
            pass
        failures.append("[1] 卡住的流没有抛错")
    except TimeoutError as exc:
        dt = asyncio.get_event_loop().time() - t0
        print(f"  [1] 首片段不来 → {dt:.1f}s 后抛 TimeoutError ✔")
        print(f"      {str(exc)[:120]}")
        if dt > 3:
            failures.append(f"[1] 触发太慢：{dt:.1f}s")

    # 2) 中途停住 → 也必须在总闸内抛错
    p2 = bootstrap.with_response_deadline(_StallMidStream())
    got = 0
    t0 = asyncio.get_event_loop().time()
    try:
        async for _ in p2.stream(None):
            got += 1
        failures.append("[2] 中途卡住没有抛错")
    except TimeoutError:
        dt = asyncio.get_event_loop().time() - t0
        print(f"  [2] 中途卡住（已收 {got} 片段）→ {dt:.1f}s 后抛 TimeoutError ✔")
        if got != 2:
            failures.append(f"[2] 已收片段数异常：{got}")

    # 3) 健康流不受影响（片段齐全、不抛错）
    p3 = bootstrap.with_response_deadline(_Healthy())
    items = [x async for x in p3.stream(None)]
    print(f"  [3] 健康流 → 收到 {len(items)} 个片段 {items} ✔")
    if items != ["chunk-0", "chunk-1", "chunk-2"]:
        failures.append(f"[3] 健康流被破坏：{items}")

    # 4) complete() 也有总闸
    p4 = bootstrap.with_response_deadline(_StallFirstByte())
    try:
        await asyncio.wait_for(p4.complete(None), timeout=5)
        failures.append("[4] complete 卡住没有抛错")
    except TimeoutError:
        print("  [4] complete 卡住 → 抛 TimeoutError ✔")

    # 5) 开关可关
    os.environ["ACPS_LLM_DEADLINE"] = "0"
    raw = _StallFirstByte()
    same = bootstrap.with_response_deadline(raw)
    print(f"  [5] ACPS_LLM_DEADLINE=0 → 原样返回: {same is raw} ✔")
    if same is not raw:
        failures.append("[5] 关闭开关无效")
    os.environ.pop("ACPS_LLM_DEADLINE", None)

    print()
    if failures:
        for f in failures:
            print("  ✘", f)
        return 1
    print("✔ 全部通过：卡住的模型调用会在硬闸内变成可观测的失败，健康调用不受影响。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
