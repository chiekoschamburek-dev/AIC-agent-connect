"""受理回执的代码兜底（2026-10-17）：模型忘交/漏作业号时，收尾 flush 自动补交。

背景：P1「回执必达没有代码保证」——submit_job 成功后完全靠模型记得 aip_submit；
模型忘了（或回执漏了作业号）用户就拿不到取件凭据。现在 submit_job 成功即把作业号
记进 run 状态，`_flush_buffered_submit` 发现缓冲产物里没有该作业号时，自动追加一条
"受理回执"产物；幂等、只在尚未成功提交时触发。
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

CL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CL))
sys.path.insert(0, str(CL.parent))
os.environ["YANSHU_RUN_TRACE"] = "0"

from wit import RunConfig, Tool  # noqa: E402

from acps_cluster.agents import supervisor as s  # noqa: E402

JOB = "job-20261017-113903-df44"


class _Ctx:
    def __init__(self, cfg) -> None:
        self.config = cfg
        self.added: dict[str, Tool] = {}

    def add_tool(self, tool, *, source: str = "hook"):  # noqa: ANN001, ANN202
        self.added[getattr(tool, "name", "?")] = tool
        return tool


def _orig_tools(calls: list):
    async def submit_handler(arguments):  # noqa: ANN001, ANN202
        calls.append(arguments)
        return {"ok": True, "state": "awaiting-completion"}

    return [Tool(name="aip_submit", description="orig",
                 input_schema={"type": "object"}, handler=submit_handler)]


def _text_of(products) -> str:
    return "".join(
        str(d.get("text") or "")
        for p in products or []
        for d in (p.get("data_items") or p.get("dataItems") or [])
    )


class JobReceiptFlushTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="job-receipt-"))
        # 交付目录 2026-10-25 起改为运行时函数 _deliver_dir()（作业 worker 的
        # WORKSPACE 会被换掉），测试 patch 函数而非模块常量。
        self._deliver = mock.patch.object(s, "_deliver_dir", lambda: self.tmp / "deliver")
        self._deliver.start()

    def tearDown(self) -> None:
        self._deliver.stop()
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *, receipts, buffered=None, already_ok=False):
        hook = s.SupervisorRunHooks()
        calls: list = []
        cfg = RunConfig(max_steps=8, tools=_orig_tools(calls))
        ctx = _Ctx(cfg)

        async def body():
            return SimpleNamespace(final_text="done", status="completed", iterations=3)

        async def scenario():
            await hook.on_run_start(SimpleNamespace(context=ctx))
            state = hook._SUBMIT_STATE.get()
            if isinstance(state, dict) and already_ok:
                state["ok"] = True
            s._JOB_RECEIPTS.set([dict(r) for r in receipts])
            buffering = ctx.added["aip_submit"]
            for product in (buffered or []):
                await buffering.handler({"products": [product]})
            await hook.wrap_run(ctx, body)
            return calls, (hook._SUBMIT_STATE.get() or {})

        # on_run_end 会把 _SUBMIT_STATE 置空；flush 后的状态在 wrap_run 里，
        # 这里只需断言实际提交内容，状态用不到。返回 calls 即可。
        return asyncio.run(scenario())[0]

    def _receipt(self):
        return {"job_id": JOB, "mode": "deep_research",
                "created_at": "2026-10-17T03:39:03+00:00"}

    def test_没有任何产物也自动补回执(self):
        calls = self._run(receipts=[self._receipt()])
        self.assertEqual(len(calls), 1, calls)
        text = _text_of(calls[0].get("products") or [])
        self.assertIn(JOB, text)
        self.assertIn("受理回执", text)
        self.assertIn("查作业", text)
        self.assertIn("不代表任务已完成", text)

    def test_产物不含作业号时追加回执(self):
        calls = self._run(
            receipts=[self._receipt()],
            buffered=[{"id": "final", "name": "报告",
                       "data_items": [{"type": "text", "text": "正文内容"}]}],
        )
        self.assertEqual(len(calls), 1, calls)
        products = calls[0].get("products") or []
        self.assertEqual(len(products), 2, products)
        text = _text_of(products)
        self.assertIn("正文内容", text)
        self.assertIn(JOB, text)

    def test_产物已含作业号不重复补(self):
        calls = self._run(
            receipts=[self._receipt()],
            buffered=[{"id": "final",
                       "data_items": [{"type": "text", "text": f"已受理 {JOB}"}]}],
        )
        products = calls[0].get("products") or []
        self.assertEqual(len(products), 1, products)
        self.assertNotIn("代码自动补交", _text_of(products))

    def test_已成功提交时不再补(self):
        calls = self._run(receipts=[self._receipt()], already_ok=True)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
