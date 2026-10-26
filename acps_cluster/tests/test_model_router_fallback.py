"""model_router._is_fallback_worthy 的分类边界测试（离线，无需网络）。

背景（2026-10-24 现网）：校园网出口隧道断线时，wit 的 OpenAIChatProvider 把底层
httpx ConnectError 包装成 RuntimeError 再抛出，分类器不认识 → 保底没接住，任务
直接 failed（"OpenAI Chat Completions request failed: ConnectError: …"）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

CLUSTER_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = CLUSTER_DIR.parent
for _p in (str(PROJECT_DIR), str(CLUSTER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import httpx  # noqa: E402

from model_router import _is_fallback_worthy, _strip_reasoning  # noqa: E402


def _assistant_with_reasoning(text: str, reasoning: str):
    from wit import TextPart
    from wit.kernel.content import ReasoningPart
    from wit.providers.base import Message

    return Message(role="assistant", content=[ReasoningPart(text=reasoning),
                                              TextPart(text=text)])


class FallbackWorthyTest(unittest.TestCase):
    def test_wit包装的连接类RuntimeError应回退(self):
        exc = RuntimeError(
            "OpenAI Chat Completions request failed: "
            "ConnectError: All connection attempts failed")
        self.assertTrue(_is_fallback_worthy(exc))

    def test_wit包装的超时RuntimeError应回退(self):
        self.assertTrue(_is_fallback_worthy(
            RuntimeError("OpenAI Chat Completions request failed: "
                         "APITimeoutError: Request timed out")))

    def test_裸httpx连接错误应回退(self):
        self.assertTrue(_is_fallback_worthy(httpx.ConnectError("refused")))

    def test_内容契约类RuntimeError不应回退(self):
        # 换一路模型也解决不了的错误（如响应不符合契约），原样抛给上层
        self.assertFalse(_is_fallback_worthy(
            RuntimeError("OpenAI Chat Completions request failed: "
                         "missing field 'usage' in response")))

    def test_业务拒绝类RuntimeError不应回退(self):
        self.assertFalse(_is_fallback_worthy(
            RuntimeError("model returned an invalid verdict")))

    def test_连接类老三条仍然成立(self):
        self.assertTrue(_is_fallback_worthy(ConnectionError("reset")))
        self.assertTrue(_is_fallback_worthy(TimeoutError("t")))


class StripReasoningTest(unittest.TestCase):
    """跨 provider 回放清洗：reasoning 只能回放给产出它的同一家供应商（10-26 实测）。"""

    def test_剥掉assistant的reasoning保留text(self):
        from wit.providers.base import ModelRequest
        from wit import TextPart

        req = ModelRequest(messages=[
            _assistant_with_reasoning("最终答案", "思考过程"),
        ])
        out = _strip_reasoning(req)
        self.assertEqual(len(out.messages[0].content), 1)
        self.assertEqual(out.messages[0].content[0].text, "最终答案")

    def test_无reasoning时原请求原样返回(self):
        from wit import TextPart
        from wit.providers.base import Message, ModelRequest

        req = ModelRequest(messages=[Message(role="user", content=[TextPart(text="问")])])
        self.assertIs(_strip_reasoning(req), req)

    def test_user消息不受影响(self):
        from wit import TextPart
        from wit.kernel.content import ReasoningPart
        from wit.providers.base import Message, ModelRequest

        req = ModelRequest(messages=[
            Message(role="user", content=[TextPart(text="问"), ReasoningPart(text="x")]),
            _assistant_with_reasoning("答", "思考"),
        ])
        out = _strip_reasoning(req)
        self.assertEqual(len(out.messages[0].content), 2)   # user 保留全部
        self.assertEqual(len(out.messages[1].content), 1)   # assistant 只剩 text


if __name__ == "__main__":
    unittest.main()
