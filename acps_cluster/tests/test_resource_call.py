"""resource_call：把 skill + args_json 组装成静态路由 JSON，再交给带令牌委派。"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

CL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CL))
sys.path.insert(0, str(CL.parent))

from acps_cluster.agents import supervisor  # noqa: E402


class ResourceCallToolTest(unittest.TestCase):
    def _tool(self, captured: dict):
        async def fake_handler(arguments):
            captured.update(arguments)
            return {"ok": True, "task_ref": "task-1"}

        fake = supervisor.Tool(
            name="acps_delegate_authenticated",
            description="fake",
            input_schema=None,
            handler=fake_handler,
        )
        member = SimpleNamespace(key="gpu", name="GPU",
                                 aic="1.2.156.3088.1.BUPT.VTF97R.V0XUC3.9JWIVI.135X",
                                 capabilities=("gpu.run", "gpu.env"))
        with mock.patch.object(supervisor, "_make_authenticated_delegate_tool", return_value=fake), \
                mock.patch.object(supervisor.cluster_roster, "resolve_member", return_value=member):
            return supervisor._make_resource_call_tool([member])

    def test_组装静态JSON并复用令牌委派(self):
        captured: dict = {}
        tool = self._tool(captured)
        out = asyncio.run(tool.handler({
            "partner": "gpu",
            "skill": "gpu.run",
            "args_json": json.dumps({"files": {"main.py": "print(1)"},
                                     "entrypoint": ["python", "main.py"]}),
        }))
        self.assertTrue(out["ok"])
        self.assertEqual(out["payload_mode"], "static-json")
        payload = json.loads(captured["request"])
        self.assertEqual(payload["skill"], "gpu.run")
        self.assertEqual(payload["args"]["entrypoint"], ["python", "main.py"])
        self.assertEqual(captured["skill_id"], "gpu.run")

    def test_未知能力拒绝且不委派(self):
        captured: dict = {}
        tool = self._tool(captured)
        out = asyncio.run(tool.handler({
            "partner": "gpu", "skill": "gpu.nope", "args_json": "{}",
        }))
        self.assertFalse(out["ok"])
        self.assertIn("未声明能力", out["error"])
        self.assertEqual(captured, {})

    def test_非法args_json拒绝(self):
        captured: dict = {}
        tool = self._tool(captured)
        out = asyncio.run(tool.handler({
            "partner": "gpu", "skill": "gpu.run", "args_json": "[1,2]",
        }))
        self.assertFalse(out["ok"])
        self.assertIn("JSON 对象", out["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
