"""静态路由 provider：结构化 JSON → ToolCall；解析失败才回落 LLM。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

CL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CL))
sys.path.insert(0, str(CL.parent))

from wit import TextPart  # noqa: E402
from wit.kernel.messages import Message, ModelRequest, ModelResponse  # noqa: E402

from acps_cluster import static_router  # noqa: E402


def _request(payload: dict, *, tool="gpu_run"):
    text = f"<payload>{json.dumps(payload, ensure_ascii=False)}</payload>"
    return ModelRequest(
        messages=[Message(role="user", content=[TextPart(text)])],
        tools=[SimpleNamespace(name=tool)],
    )


class _Fallback:
    def __init__(self):
        self.calls = 0

    async def complete(self, request):
        self.calls += 1
        return ModelResponse.final("LLM-FALLBACK")

    async def aclose(self):
        pass


class StaticRouterTest(unittest.TestCase):
    def test_commandParams直接路由到能力(self):
        payload = {
            "type": "task-command",
            "commandParams": {"skillId": "gpu.run",
                              "args": {"files": {"main.py": "print(1)"}, "entrypoint": "main.py"}},
            "dataItems": [{"type": "text", "text": "跑一下", "metadata": {"skillId": "gpu.run"}}],
        }
        provider = static_router.StaticRouterProvider(fallback=None, prefix="GPU")
        out = asyncio.run(provider.complete(_request(payload)))
        calls = out.tool_calls
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "gpu_run")
        self.assertEqual(calls[0].arguments["entrypoint"], "main.py")
        self.assertIn("main.py", calls[0].arguments["files"])

    def test_dataItems文本JSON也可路由(self):
        payload = {
            "type": "task-command",
            "dataItems": [{"type": "text",
                           "text": json.dumps({"skill": "cpu.run", "args": {"files": {}}}),
                           "metadata": {"skillId": "cpu.run"}}],
        }
        provider = static_router.StaticRouterProvider(fallback=None, prefix="CPU")
        out = asyncio.run(provider.complete(_request(payload, tool="cpu_run")))
        self.assertEqual(out.tool_calls[0].name, "cpu_run")

    def test_工具结果作为最终文本(self):
        provider = static_router.StaticRouterProvider(fallback=None, prefix="GPU")
        request = ModelRequest(
            messages=[Message(role="tool", content=[TextPart('{"ok": true}')],
                              tool_call_id="static-1")],
            tools=[SimpleNamespace(name="gpu_run")],
        )
        out = asyncio.run(provider.complete(request))
        self.assertEqual(out.final_text, '{"ok": true}')

    def test_未知参数按schema拒绝(self):
        payload = {
            "type": "task-command",
            "commandParams": {"skillId": "cpu.run", "args": {"files": {}, "bogus": 1}},
        }
        text = f"<payload>{json.dumps(payload)}</payload>"
        request = ModelRequest(
            messages=[Message(role="user", content=[TextPart(text)])],
            tools=[SimpleNamespace(
                name="cpu_run",
                input_schema={"type": "object",
                              "properties": {"files": {"type": "object"}},
                              "required": ["files"]},
            )],
        )
        provider = static_router.StaticRouterProvider(fallback=None, prefix="CPU")
        out = asyncio.run(provider.complete(request))
        self.assertIn("参数错误", out.final_text)
        self.assertIn("bogus", out.final_text)

    def test_解析失败且有兜底才调LLM(self):
        fallback = _Fallback()
        provider = static_router.StaticRouterProvider(fallback=fallback, prefix="GPU")
        request = ModelRequest(
            messages=[Message(role="user", content=[TextPart("帮我跑个实验")])],
            tools=[SimpleNamespace(name="gpu_run")],
        )
        out = asyncio.run(provider.complete(request))
        self.assertEqual(out.final_text, "LLM-FALLBACK")
        self.assertEqual(fallback.calls, 1)

    def test_关闭兜底时返回用法错误(self):
        provider = static_router.StaticRouterProvider(fallback=None, prefix="GPU")
        request = ModelRequest(
            messages=[Message(role="user", content=[TextPart("帮我跑个实验")])],
            tools=[SimpleNamespace(name="gpu_run")],
        )
        with mock.patch.dict(os.environ, {"ACPS_GPU_LLM_FALLBACK": "0"}):
            out = asyncio.run(provider.complete(request))
        self.assertIn("静态资源节点", out.final_text)
        self.assertIn("skillId", out.final_text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
