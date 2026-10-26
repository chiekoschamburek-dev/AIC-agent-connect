"""伙伴入站产物兜底提交（`InboundProductFallbackHooks`）离线单测。

现场依据（2026-10-18 第 9 轮真链路）：沙箱 `exit=0` 并写出 `weights.json` /
`predictions.csv`，但实验员的模型没有 `aip_submit`，任务以
`Agent produced no AIP Product.` 收场——leader 于是如实报告"无产物"，实测结果
只剩在盘上。覆盖 supervisor 版同款的四条时序纪律（协议只接受一次成功提交、
awaiting-input 时代交会被拒）。

    python -m unittest acps_cluster.tests.test_inbound_product_fallback -v
"""

import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

CLUSTER_DIR = Path(__file__).resolve().parents[1]
for _p in (str(CLUSTER_DIR.parent), str(CLUSTER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import cluster_common  # noqa: E402
from wit import RunConfig  # noqa: E402

RESULT = {"ok": True, "stage": "done", "taskRef": "task_abc",
          "report": {"verdict": "supported", "metrics": {"test_accuracy": 0.8333}}}


def _submit_tool(calls: list):
    async def handler(args):
        calls.append(args)
        return {"status": "success", "result": {"state": "completed"}}
    return SimpleNamespace(name="aip_submit", handler=handler)


def _scenario(*, model_submits: bool, payload: dict | None,
              request_input: bool = False, with_submit_tool: bool = True):
    """跑一遍 on_run_start →（能力登记结果）→ 模型的提交行为 → on_run_end。"""
    calls: list = []

    async def run():
        hook = cluster_common.InboundProductFallbackHooks()
        tools = ([_submit_tool(calls)] if with_submit_tool else []) + [
            SimpleNamespace(name="aip_fail", handler=None)]
        await hook.on_run_start(SimpleNamespace(
            context=SimpleNamespace(runtime=None, state=None,
                                    config=RunConfig(tools=tools), tool_manager=None)))
        if payload is not None:
            cluster_common.set_inbound_result(payload)
        if request_input:
            await hook.on_tool_response(SimpleNamespace(
                tool_call=SimpleNamespace(name="acps.aip.request_input", arguments=None),
                response={"status": "success", "result": {"state": "awaiting-input"}}))
        if model_submits:
            await hook.on_tool_request(SimpleNamespace(
                tool_call=SimpleNamespace(name="aip_submit", arguments={"products": []})))
            await hook.on_tool_response(SimpleNamespace(
                tool_call=SimpleNamespace(name="aip_submit", arguments={"products": []}),
                response={"status": "success", "result": {}}))
        await hook.on_run_end(SimpleNamespace(
            result=SimpleNamespace(status="completed", final_text="模型自述", iterations=6)))
        return list(calls)

    return asyncio.run(run())


class InboundProductFallbackTest(unittest.TestCase):
    def test_模型已提交_兜底不得重复提交(self):
        self.assertEqual(_scenario(model_submits=True, payload=RESULT), [])

    def test_模型没提交_用能力结果补交一次(self):
        calls = _scenario(model_submits=False, payload=RESULT)
        self.assertEqual(len(calls), 1)
        product = calls[0]["products"][0]
        # id 必填：缺 id 会被 AIP 判 Invalid AIP product（2026-10-26 e2e 实测）
        self.assertTrue(str(product.get("id") or "").strip(), product)
        text = product["data_items"][0]["text"]
        self.assertIn("task_abc", text)
        self.assertIn("0.8333", text)
        self.assertEqual(json.loads(text)["report"]["verdict"], "supported")

    def test_缺件态不得当成交付物(self):
        # 能力返回 awaiting（等调用方补料）时补交会被协议拒，且会把等待语义弄丢
        self.assertEqual(_scenario(model_submits=False,
                                   payload={"ok": False, "awaiting": {"need": "delegation.token"}}), [])

    def test_没有能力结果时不提交(self):
        self.assertEqual(_scenario(model_submits=False, payload=None), [])

    def test_已进入awaiting_input_不代交(self):
        self.assertEqual(_scenario(model_submits=False, payload=RESULT, request_input=True), [])

    def test_没有提交工具时静默跳过_不报错(self):
        self.assertEqual(_scenario(model_submits=False, payload=RESULT,
                                   with_submit_tool=False), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
