"""工作流链接与本次 run Scenario 的离线单测（不联网、不碰真实工作区）。"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

CLUSTER_DIR = Path(__file__).resolve().parents[1]
for _p in (str(CLUSTER_DIR.parent), str(CLUSTER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import viz_live  # noqa: E402


def _member(aic="AIC-SCOUT", key="scout", name="文献检索 scout"):
    return SimpleNamespace(aic=aic, key=key, name=name)


class VizRecorderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.rec = viz_live.Recorder(
            instruction="写一份文献综述",
            roster=[_member()],
            run_id="run-test123",
            scenario_dir_override=self.tmp,
            base_url_override="https://viz.example.com",
        )

    def test_写入本次run的scenario(self):
        self.assertTrue(self.rec.available)
        path = self.tmp / "run-test123.json"
        self.assertTrue(path.is_file())
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["instruction"], "写一份文献综述")
        self.assertIn("events", data)
        self.assertEqual(data["events"][0]["type"], "instruction")

    def test_委派与产物事件(self):
        tc = SimpleNamespace(id="call-1", name="acps_delegate_public",
                             arguments={"partner_aic": "AIC-SCOUT",
                                        "skill_id": "arxiv.search",
                                        "request": "查图注意力网络"})
        self.rec.tool_request("acps_delegate_public", tc.arguments, tc)
        self.rec.tool_response("acps_delegate_public", {"ok": True, "task_ref": "task_abc"}, tc)
        self.rec.tool_request("aip_submit", {"products": [
            {"id": "final", "name": "综述", "data_items": [{"type": "text", "text": "x"}]}
        ]})
        self.rec.finish("完成")
        data = json.loads((self.tmp / "run-test123.json").read_text(encoding="utf-8"))
        kinds = [e["type"] for e in data["events"]]
        self.assertIn("delegate", kinds)
        self.assertIn("product", kinds)
        self.assertIn("done", kinds)
        delegate = next(e for e in data["events"] if e["type"] == "delegate")
        self.assertEqual(delegate["from"], "sup")
        self.assertEqual(delegate["to"], "scout")

    def test_链接产物幂等(self):
        products = []
        self.assertTrue(viz_live.append_workflow_product(products, self.rec))
        self.assertTrue(viz_live.append_workflow_product(products, self.rec))
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]["id"], "workflow-viz")
        text = products[0]["data_items"][0]["text"]
        self.assertIn("https://viz.example.com/?run=run-test123", text)
        self.assertIn("https://viz.example.com/?live=1", text)

    def test_live指针跟随最新run且旧run不覆盖(self):
        rec1 = viz_live.Recorder(
            instruction="第一轮", run_id="run-first",
            scenario_dir_override=self.tmp,
            base_url_override="https://viz.example.com",
        )
        time.sleep(0.02)
        rec2 = viz_live.Recorder(
            instruction="第二轮", run_id="run-second",
            scenario_dir_override=self.tmp,
            base_url_override="https://viz.example.com",
        )
        live = json.loads((self.tmp / "live.json").read_text(encoding="utf-8"))
        self.assertEqual(live["run_id"], "run-second")
        self.assertFalse(live["done"])
        rec1.finish("第一轮完成")
        live = json.loads((self.tmp / "live.json").read_text(encoding="utf-8"))
        self.assertEqual(live["run_id"], "run-second")
        rec2.finish("第二轮完成")
        live = json.loads((self.tmp / "live.json").read_text(encoding="utf-8"))
        self.assertEqual(live["run_id"], "run-second")
        self.assertTrue(live["done"])

    def test_未配置base_url时不写不追加(self):
        rec = viz_live.Recorder(
            instruction="x",
            scenario_dir_override=self.tmp,
            base_url_override="",
        )
        self.assertFalse(rec.available)
        products = []
        self.assertFalse(viz_live.append_workflow_product(products, rec))
        self.assertEqual(products, [])
        self.assertFalse((self.tmp / f"{rec.run_id}.json").exists())


class SupervisorVizLinkTest(unittest.TestCase):
    """supervisor 的真实接线：aip_submit 的参数里必须多出 workflow-viz 产物。"""

    def test_aip_submit参数被追加链接且生成scenario(self):
        tmp = Path(tempfile.mkdtemp())
        env = {
            "ACPS_VIZ_BASE_URL": "https://viz.example.com",
            "ACPS_VIZ_SCENARIO_DIR": str(tmp),
        }
        with mock.patch.dict(os.environ, env, clear=False):
            import asyncio

            from wit import RunConfig
            from supervisor import SupervisorRunHooks

            hook = SupervisorRunHooks()
            ctx = SimpleNamespace(
                config=RunConfig(tools=[]),
                state=SimpleNamespace(messages=[], metadata={"task": "写一份综述"}),
                add_tool=lambda *args, **kwargs: None,
                runtime=None,
                tool_manager=None,
            )
            args = {"products": [{"id": "final", "name": "终稿",
                                  "data_items": [{"type": "text", "text": "正文"}]}]}
            tc = SimpleNamespace(id="c1", name="aip_submit", arguments=args)

            async def _run() -> None:
                await hook.on_run_start(SimpleNamespace(context=ctx))
                await hook.on_tool_request(SimpleNamespace(tool_call=tc))

            asyncio.run(_run())
            ids = [p.get("id") for p in args["products"]]
            self.assertIn("workflow-viz", ids)
            text = args["products"][-1]["data_items"][0]["text"]
            self.assertIn("https://viz.example.com/?run=run-", text)
            self.assertTrue(list(tmp.glob("run-*.json")))


if __name__ == "__main__":
    unittest.main()
