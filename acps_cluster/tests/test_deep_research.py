"""DeepResearch 模式离线单测：阶段框架/预算硬上限/激活语义。

    python -m unittest acps_cluster.tests.test_deep_research -v
"""

import asyncio
import sys
import unittest
from pathlib import Path

CLUSTER_DIR = Path(__file__).resolve().parents[1]
for _p in (str(CLUSTER_DIR.parent), str(CLUSTER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from acps_cluster.agents.supervisor import (  # noqa: E402
    _DELEGATION_DISCIPLINE,
    _DELEGATION_USED,
    _make_deep_research_tool,
    _playbook,
)


class DeepResearchToolTest(unittest.TestCase):
    def setUp(self):
        self.tool = _make_deep_research_tool()

    def test_首次调用激活并返回阶段框架(self):
        r = asyncio.run(self.tool.handler({}))
        self.assertTrue(r["ok"])
        self.assertEqual(r["mode"], "deep-research")
        self.assertEqual(len(r["stages"]), 7)  # 6 阶段 + 收尾
        self.assertIn("广检索", r["currentStage"])

    def test_打卡推进阶段并递减预算(self):
        asyncio.run(self.tool.handler({}))
        _DELEGATION_USED.set(9)  # 模拟本轮已发生 9 次委派
        last = None
        for i in range(3):
            last = asyncio.run(self.tool.handler({"report": f"阶段{i}"}))
        self.assertEqual(last["checkin"], 3)
        self.assertLess(last["budget"]["remaining"], last["budget"]["cap"])

    def test_预算剩余20pct时指示跳过补漏(self):
        asyncio.run(self.tool.handler({}))
        _DELEGATION_USED.set(36)
        last = None
        for i in range(12):  # 12×3=36 > 40×0.8=32
            last = asyncio.run(self.tool.handler({"report": "x"}))
        self.assertLessEqual(last["budget"]["remaining"], 8)
        self.assertIn("收尾", last["note"])

    def test_报告被接收并截断(self):
        asyncio.run(self.tool.handler({}))
        r = asyncio.run(self.tool.handler({"report": "长" * 500}))
        self.assertTrue(r["reportReceived"].endswith("长"))
        self.assertLessEqual(len(r["reportReceived"]), 200)


class PlaybookWiringTest(unittest.TestCase):
    def test_剧本含深度模式附加段(self):
        playbook = _playbook("名册", ["scout", "critic"])
        self.assertIn("深度模式", playbook)
        self.assertIn("deep_research", playbook)
        self.assertIn("阶段6 补漏循环", playbook)
        self.assertIn("citation.verify", playbook)  # 引用核验接入阶段3
        self.assertIn("contradiction.detect", playbook)  # 矛盾复用
        # 委托纪律段仍完整（深度模式是附加，不替换）
        self.assertIn("remedy-3", _DELEGATION_DISCIPLINE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
