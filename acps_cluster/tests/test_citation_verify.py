"""引用核验离线单测：解析/arXiv ID/对账冲突（LLM 调用与取源在真实链路验证）。

    python -m unittest acps_cluster.tests.test_citation_verify -v
"""

import asyncio
import sys
import unittest
from pathlib import Path

CLUSTER_DIR = Path(__file__).resolve().parents[1]
for _p in (str(CLUSTER_DIR.parent), str(CLUSTER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from acps_cluster.runtime.citation_verify import arxiv_id_of, parse_citations  # noqa: E402


class ParseTest(unittest.TestCase):
    def test_基本抽取_句级引用配参考条目(self):
        draft = (
            "图注意力网络在归纳任务上表现优异[1]。"
            "稀疏注意力将复杂度降到线性[2]。"
            "两者可组合使用[3]。"
        )
        refs = "GAT（Graph Attention Network），arXiv:1710.10903\nLongformer，arXiv:2004.05150\n组合方法的综述"
        pairs = parse_citations(draft, refs)
        self.assertEqual([n for n, _, _ in pairs], [1, 2, 3])
        self.assertIn("1710.10903", pairs[0][2])
        self.assertIn("图注意力", pairs[0][1])

    def test_显式编号的参考表(self):
        pairs = parse_citations("A 方法有效[2]。", "[1] 第一条\n[2] 第二条 arXiv:2401.00001")
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0][0], 2)
        self.assertIn("2401.00001", pairs[0][2])

    def test_同一引用只取一次(self):
        pairs = parse_citations("X[1]。又提 X[1]。", "ref one")
        self.assertEqual(len(pairs), 1)

    def test_越界引用忽略(self):
        pairs = parse_citations("越界[9]。", "only one ref")
        self.assertEqual(pairs, [])

    def test_空输入返回空(self):
        self.assertEqual(parse_citations("", ""), [])
        self.assertEqual(parse_citations("无引用", "refs"), [])


class ArxivIdTest(unittest.TestCase):
    def test_标准格式(self):
        self.assertEqual(arxiv_id_of("GAT paper, arXiv:1710.10903v2"), "1710.10903")
        self.assertEqual(arxiv_id_of("see 2401.12345"), "2401.12345")

    def test_无ID返回None(self):
        self.assertIsNone(arxiv_id_of("普通期刊论文，无编号"))
        self.assertIsNone(arxiv_id_of(""))


class ReviewGateCrossCheckTest(unittest.TestCase):
    """第三步的对账逻辑：三种冲突形态。"""

    def setUp(self):
        from acps_cluster.agents.supervisor import _make_review_gate_tool

        self.tool = _make_review_gate_tool()

    def _run(self, raw, table=None):
        args = {"raw": raw}
        if table is not None:
            args["citation_table"] = table
        return asyncio.run(self.tool.handler(args))

    def test_核验发现fabricated而审稿者未报_对账blocker(self):
        raw = '{"verdict": "APPROVE", "issues": [], "score": 90}'
        table = {"ok": True, "checks": [
            {"index": 1, "verdict": "fabricated"}, {"index": 2, "verdict": "supported"},
        ]}
        result = self._run(raw, table)
        self.assertEqual(result["gate"], "REVISE")
        self.assertTrue(result["citationConflicts"])
        self.assertIn("未报", result["citationConflicts"][0])
        self.assertIn("对账", result["feedback"])

    def test_审稿者对supported引用报blocker且无证据_疑似编造(self):
        raw = ('{"verdict": "REVISE", "issues": [{"severity": "blocker", '
               '"location": "引用[2]处", "problem": "断章取义", "suggestion": "修正"}]}')
        table = {"ok": True, "checks": [{"index": 2, "verdict": "supported"}]}
        result = self._run(raw, table)
        self.assertTrue(any("编造" in c or "supported" in c for c in result["citationConflicts"]))

    def test_对账一致时不加冲突(self):
        raw = ('{"verdict": "REVISE", "issues": [{"severity": "major", '
               '"location": "引用[1]处", "problem": "misrep", "suggestion": "修"}]}')
        table = {"ok": True, "checks": [{"index": 1, "verdict": "misrepresented"}]}
        result = self._run(raw, table)
        self.assertEqual(result["citationConflicts"], [])
        self.assertEqual(result["gate"], "REVISE")  # major 本身已 REVISE
        self.assertEqual(result["blockers"], 0)

    def test_不传表时行为与从前一致(self):
        raw = '{"verdict": "APPROVE", "issues": []}'
        result = self._run(raw)
        self.assertEqual(result["gate"], "APPROVE")
        self.assertEqual(result["citationConflicts"], [])

    def test_审稿者报blocker且给了证据_不判编造(self):
        raw = ('{"verdict": "REVISE", "issues": [{"severity": "blocker", '
               '"location": "引用[2]", "problem": "曲解", "suggestion": "修", '
               '"evidence_check": "原文说……"}]}')
        table = {"ok": True, "checks": [{"index": 2, "verdict": "supported"}]}
        result = self._run(raw, table)
        self.assertEqual(result["citationConflicts"], [])  # 有 evidence_check 就不判编造


class WiringTest(unittest.TestCase):
    def test_critic与scout能力声明就位(self):
        from acps_cluster.agents import partner_critic
        from acps_cluster.agents import partner_scout
        from acps_cluster.delegation.pep import capability_visibility

        critic_ids = [getattr(c, "capability", c).id for c in [partner_critic.review_paper, partner_critic.citation_verify_capability]]
        self.assertEqual(critic_ids, ["paper.review", "citation.verify"])
        scout_ids = [getattr(c, "capability", c).id for c in partner_scout.SCOUT_CAPABILITIES]
        self.assertIn("arxiv.fetch", scout_ids)

        # 档位：citation.verify 与 arxiv.fetch 均非公开（走委托 token）
        _, critic_has_non = capability_visibility([partner_critic.citation_verify_capability])
        self.assertTrue(critic_has_non)
        skill = partner_scout.scout_arxiv_fetch.capability.to_acps_skill()
        self.assertEqual(skill.get("visibility"), "domain")

    def test_verify_one的verdict白名单收敛(self):
        from acps_cluster.runtime.citation_verify import VERDICTS, verify_one

        self.assertEqual(set(VERDICTS), {"supported", "misrepresented", "fabricated", "unverifiable"})
        # verify_one 依赖 llm_tools（真 LLM），离线只验证导入面
        self.assertTrue(callable(verify_one))


if __name__ == "__main__":
    unittest.main(verbosity=2)
