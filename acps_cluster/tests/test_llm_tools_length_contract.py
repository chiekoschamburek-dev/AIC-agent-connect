"""`llm_tools.chat` 的长度契约单测（离线：假 client，不发网络请求）。

依据（2026-10-17 真链路）：实验员的代码生成被 max_tokens 切断，`finish_reason=length`
却没人看，截断文本一路走到 JSON 解析才炸——两轮重试都只得到"JSON 不完整"这种
不可行动的反馈。这里固定两件事：max_tokens 要真发出去；截断要成为显式失败。

    python -m unittest acps_cluster.tests.test_llm_tools_length_contract -v
"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

CLUSTER_DIR = Path(__file__).resolve().parents[1]
for _p in (str(CLUSTER_DIR.parent), str(CLUSTER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from acps_cluster.core import llm_tools  # noqa: E402


class _FakeCompletions:
    def __init__(self, *, finish_reason: str, content: str) -> None:
        self.finish_reason = finish_reason
        self.content = content
        self.calls: list[dict] = []

    async def create(self, **params):
        self.calls.append(params)
        message = SimpleNamespace(content=self.content)
        choice = SimpleNamespace(message=message, finish_reason=self.finish_reason)
        return SimpleNamespace(choices=[choice])


class _FakeClient:
    def __init__(self, *, finish_reason: str, content: str) -> None:
        self.completions = _FakeCompletions(finish_reason=finish_reason, content=content)
        self.chat = SimpleNamespace(completions=self.completions)


class LengthContractTest(unittest.TestCase):
    def _run(self, *, finish_reason: str, content: str, **chat_kwargs):
        client = _FakeClient(finish_reason=finish_reason, content=content)
        # 2026-10-19：chat() 改为按目标取 client（_client_for）以支持
        # 「北邮网关主用 → MiniMax 保底」，_instance 已移除。
        # 本测试只关心长度契约，与路由无关：把目标固定成单个假目标，
        # 不读 .env（单测环境本来就没有这些键），否则会因"无可用配置"而失败。
        fake_targets = [("test", "fake-model", "https://example.invalid/v1", "sk-test", "")]
        with mock.patch.object(llm_tools, "_client_for", return_value=client), \
             mock.patch.object(llm_tools, "_route_targets", return_value=fake_targets), \
             mock.patch.object(llm_tools.asyncio, "sleep", _noop), \
             mock.patch.object(llm_tools, "_reset_client", _noop):
            try:
                out = asyncio.run(llm_tools.chat("sys", "user", **chat_kwargs))
                return out, None, client
            except Exception as exc:  # noqa: BLE001
                return None, exc, client

    def test_max_tokens_真的发进请求参数(self):
        _, err, client = self._run(finish_reason="stop", content='{"files":{}}', max_tokens=8000)
        self.assertIsNone(err)
        self.assertEqual(client.completions.calls[0].get("max_tokens"), 8000)

    def test_不传时不发该参数(self):
        _, err, client = self._run(finish_reason="stop", content="ok")
        self.assertIsNone(err)
        self.assertNotIn("max_tokens", client.completions.calls[0])

    def test_strict_length下截断成为失败(self):
        _, err, client = self._run(finish_reason="length", content='{"files":{"main.py": "x',
                                   max_tokens=24000, strict_length=True)
        self.assertIsNotNone(err, "截断必须抛出，否则下游只会看到解析失败")
        self.assertIn("未产出正文", str(err))
        self.assertIn("finish_reason=length", str(err))
        self.assertEqual(len(client.completions.calls), 2, "截断应走既有的重试一次")

    def test_正文为空也算失败并报出长度(self):
        # 推理型模型的真实形态：finish=length、正文 0 字符、thinking 吃掉全部预算
        _, err, _ = self._run(finish_reason="length", content="",
                              max_tokens=24000, strict_length=True)
        self.assertIsNotNone(err)
        self.assertIn("max_tokens=24000", str(err))
        self.assertIn("正文字符=0", str(err))

    def test_默认不因截断报错_散文类调用不受影响(self):
        out, err, _ = self._run(finish_reason="length", content="写了一半的综述")
        self.assertIsNone(err)
        self.assertEqual(out, "写了一半的综述")


async def _noop(*_a, **_k):
    return None


if __name__ == "__main__":
    unittest.main(verbosity=2)
