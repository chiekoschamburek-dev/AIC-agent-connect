"""AutoResearch 迭代环离线单测：激活/证据账本/修正链强制/迭代上限/汇总门槛。

    python -m unittest acps_cluster.tests.test_auto_research -v
"""

import asyncio
import json
import sys
import unittest
from pathlib import Path

CLUSTER_DIR = Path(__file__).resolve().parents[1]
for _p in (str(CLUSTER_DIR.parent), str(CLUSTER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from acps_cluster.agents.supervisor import _make_auto_research_tool, _playbook  # noqa: E402


class AutoResearchToolTest(unittest.TestCase):
    def setUp(self):
        self.tool = _make_auto_research_tool()

    def _activate_once(self):
        if not getattr(self, "_activated", False):
            asyncio.run(self.tool.handler({}))  # 先激活（handler 闭包状态跨调用保持）
            self._activated = True

    def _iterate(self, **kw):
        self._activate_once()
        args = {"action": "iterate", "hypothesis": "H", "run_ref": "r1", "verdict": "supported"}
        args.update(kw)
        return asyncio.run(self.tool.handler(args))

    def test_激活返回迭代环框架(self):
        r = asyncio.run(self.tool.handler({}))
        self.assertTrue(r["ok"])
        self.assertEqual(r["mode"], "auto-research")
        self.assertEqual(r["maxIterations"], 2)

    def test_iterate需要hypothesis与run_ref(self):
        asyncio.run(self.tool.handler({}))
        r = asyncio.run(self.tool.handler({"action": "iterate", "verdict": "supported"}))
        self.assertFalse(r["ok"])

    def test_supported入成立池(self):
        r = self._iterate()
        self.assertTrue(r["ok"])
        self.assertEqual(r["supportedPool"], 1)
        self.assertEqual(r["verdict"], "supported")

    def test_refuted无修正时提示必须修正(self):
        r = self._iterate(verdict="refuted")
        self.assertTrue(r["ok"])
        self.assertEqual(r["refutedPool"], 1)

    def test_迭代上限两轮(self):
        self._iterate()
        self._iterate(verdict="refuted", revision_from="H1", revision_to="H2")
        r = self._iterate()
        self.assertFalse(r["ok"])
        self.assertIn("迭代已用尽", r["error"])

    def test_非法iterate不消耗预算且空账本禁止汇总(self):
        self._activate_once()
        # 两次非法调用不占轮次
        self.assertFalse(asyncio.run(self.tool.handler({"action": "iterate"}))["ok"])
        self.assertFalse(asyncio.run(self.tool.handler(
            {"action": "iterate", "hypothesis": "H", "run_ref": "r", "verdict": "bogus"}
        ))["ok"])
        r = self._iterate()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["iteration"], 1)

        empty = _make_auto_research_tool()
        asyncio.run(empty.handler({}))
        summary = asyncio.run(empty.handler({"action": "summarize"}))
        self.assertFalse(summary["ok"])
        self.assertIn("账本为空", summary["error"])

    def test_汇总被refuted阻塞直到登记修正(self):
        self._activate_once()
        self._iterate(verdict="refuted")
        r = asyncio.run(self.tool.handler({"action": "summarize"}))
        self.assertFalse(r["ok"])
        self.assertIn("修正", r["error"])
        # 登记修正后可收尾
        r2 = asyncio.run(self.tool.handler({
            "action": "iterate", "hypothesis": "H(修正)", "run_ref": "r2",
            "verdict": "supported", "revision_from": "H1", "revision_to": "H(修正)"}))
        self.assertTrue(r2["ok"])
        r3 = asyncio.run(self.tool.handler({"action": "summarize"}))
        self.assertTrue(r3["ok"])
        self.assertEqual(len(r3["supported"]), 1)
        self.assertEqual(len(r3["refuted"]), 1)

    def test_修正链入账(self):
        r = self._iterate(verdict="inconclusive", revision_from="H1", revision_to="H2")
        ledger = r["ledger"][-1]
        self.assertEqual(ledger["revision"], {"from": "H1", "to": "H2"})

    def test_全为负结果但已登记修正时迭代用尽仍可收尾(self):
        # 2026-10-17 真链路死锁：两轮 inconclusive 均已带修正链入账后，iterate 说
        # "只能 summarize 收尾"，而 summarize 若仍认 pending_revision 就说"先登记修正"——
        # 两条拒绝互为前提，模型无路可走。修正入账必须解除该门槛。
        self._iterate(verdict="inconclusive", revision_from="H1", revision_to="H2")
        self._iterate(verdict="inconclusive", run_ref="r2", revision_from="H2", revision_to="H3")
        r = asyncio.run(self.tool.handler({"action": "summarize"}))
        self.assertTrue(r["ok"], f"收尾被拒: {r}")
        self.assertEqual(len(r["refuted"]), 2)
        self.assertEqual(len(r["supported"]), 0)

    def test_未登记修正时不得收尾(self):
        # 上面那条不得把门槛整个取消：负结果且没修正链，仍然要拒
        self._iterate(verdict="refuted")
        r = asyncio.run(self.tool.handler({"action": "summarize"}))
        self.assertFalse(r["ok"])
        self.assertIn("修正", r["error"])

    def test_账本跨轮累计(self):
        self._iterate(verdict="inconclusive", revision_from="H1", revision_to="H2")
        r = self._iterate(verdict="refuted", run_ref="r2", revision_from="H2", revision_to="H3")
        self.assertEqual([e["iteration"] for e in r["ledger"]], [1, 2])
        self.assertEqual(r["ledger"][0]["runRef"], "r1")
        self.assertEqual(r["ledger"][1]["revision"], {"from": "H2", "to": "H3"})

    def test_未知action拒绝(self):
        asyncio.run(self.tool.handler({}))
        r = asyncio.run(self.tool.handler({"action": "warp"}))
        self.assertFalse(r["ok"])


class 独立度量裁决Test(unittest.TestCase):
    """有宿主侧独立度量时 verdict 归代码裁决，自述不得绕过判据。"""

    def setUp(self):
        self.tool = _make_auto_research_tool()
        asyncio.run(self.tool.handler({}))          # 激活

    def _iterate(self, **kw):
        args = {"action": "iterate", "hypothesis": "H", "run_ref": "r1", "verdict": "supported"}
        args.update(kw)
        return asyncio.run(self.tool.handler(args))

    def test_度量达标且自述一致_按独立证据入账(self):
        r = self._iterate(host_score=0.9167, threshold=0.85)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["supportedPool"], 1)
        entry = r["ledger"][-1]
        self.assertEqual(entry["verdictSource"], "host-score")
        self.assertEqual(entry["hostScore"], 0.9167)

    def test_自述达标但度量不够_直接拒(self):
        r = self._iterate(host_score=0.8125, threshold=0.85, verdict="supported")
        self.assertFalse(r["ok"])
        self.assertIn("refuted", r["error"])
        self.assertIn("独立证据", r["error"])

    def test_度量不达标且如实报refuted_入被否池(self):
        r = self._iterate(host_score=0.8125, threshold=0.85, verdict="refuted",
                          revision_from="H1", revision_to="H2")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["refutedPool"], 1)

    def test_给了度量却报inconclusive_被拒(self):
        r = self._iterate(host_score=0.9167, threshold=0.85, verdict="inconclusive")
        self.assertFalse(r["ok"])
        self.assertIn("不能是 inconclusive", r["error"])

    def test_度量或阈值非数值_被拒(self):
        self.assertFalse(self._iterate(host_score="高", threshold=0.85)["ok"])


class 账本落盘Test(unittest.TestCase):
    """`ACPS_AR_LEDGER` 设了才落盘；落盘是旁路，不参与裁决。"""

    def test_逐条写入jsonl并带schema版本(self):
        import os
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ledger.v1.jsonl"
            with mock.patch.dict(os.environ, {"ACPS_AR_LEDGER": str(path)}):
                tool = _make_auto_research_tool()
                asyncio.run(tool.handler({}))
                asyncio.run(tool.handler({"action": "iterate", "hypothesis": "H1",
                                          "run_ref": "r1", "verdict": "supported",
                                          "host_score": 0.9, "threshold": 0.85}))
                asyncio.run(tool.handler({"action": "summarize"}))
            lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
            self.assertEqual([l["event"] for l in lines], ["iterate", "summarize"])
            self.assertEqual({l["schema"] for l in lines}, {"ar-ledger.v1"})
            self.assertEqual(lines[0]["verdictSource"], "host-score")
            self.assertEqual(len(lines[1]["supported"]), 1)

    def test_未设环境变量时不落盘(self):
        import os
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "should-not-exist.jsonl"
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("ACPS_AR_LEDGER", None)
                tool = _make_auto_research_tool()
                asyncio.run(tool.handler({}))
                asyncio.run(tool.handler({"action": "iterate", "hypothesis": "H",
                                          "run_ref": "r", "verdict": "supported"}))
            self.assertFalse(path.exists())


class PlaybookWiringTest(unittest.TestCase):
    def test_剧本含自研模式段(self):
        playbook = _playbook("名册", ["frontier", "experiment", "critic", "writer"])
        self.assertIn("自研模式", playbook)
        self.assertIn("auto_research", playbook)
        self.assertIn("可证伪假设", playbook)
        self.assertIn("负结果也是结果", playbook)  # 负结果如实入账纪律
        self.assertIn("frontier.detect", playbook)  # 迭代环起点
        self.assertIn("host_score", playbook)      # 独立度量优先于自述


if __name__ == "__main__":
    unittest.main(verbosity=2)
