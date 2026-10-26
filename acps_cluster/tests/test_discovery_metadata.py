"""发现门面单测：登记完整性 + 只改描述类字段 + 描述质量下限。

跑法（my-project 目录下，本机已装 wit/acps_sdk）：
    python -m unittest acps_cluster.tests.test_discovery_metadata -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

CLUSTER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CLUSTER))
sys.path.insert(0, str(CLUSTER.parent))

from acps_cluster.core import discovery_metadata as dm  # noqa: E402


def _caps(module, const: str) -> list:
    return list(getattr(module, const))


class TestFacadeRegistry(unittest.TestCase):
    """登记表本身的完整性。"""

    def test_每个agent门面都有实质描述(self):
        for agent_id, facade in dm.AGENT_FACADES.items():
            with self.subTest(agent=agent_id):
                text = facade.description or facade.search_surface
                self.assertTrue(text, f"{agent_id} 门面为空")
                self.assertGreaterEqual(len(text), 120, f"{agent_id} 门面过短，语义匹配信号不足")

    def test_每个能力门面都有描述与关键词(self):
        for cid, facade in dm.CAPABILITY_FACADES.items():
            with self.subTest(skill=cid):
                self.assertGreaterEqual(len(facade.description), 40, f"{cid} 描述过短")
                self.assertGreaterEqual(len(facade.tags), 3, f"{cid} 关键词太少（建议中英文各若干）")

    def test_公开能力都有用户问法示例(self):
        # 只对"公开可见"的能力要求 examples：非公开能力不进 ACS，写了也不被检索
        from acps_cluster.agents import partner_scout  # noqa: PLC0415
        from acps_cluster.agents import partner_writer  # noqa: PLC0415
        from acps_cluster.agents import partner_critic  # noqa: PLC0415
        from acps_cluster.agents import partner_synthesis  # noqa: PLC0415
        from acps_cluster.agents import partner_frontier  # noqa: PLC0415
        from acps_cluster.agents import partner_contradiction  # noqa: PLC0415

        writer_caps = (
            _caps(partner_writer, "WRITER_CAPABILITIES")
            if hasattr(partner_writer, "WRITER_CAPABILITIES")
            else [partner_writer.write_paper]
        )
        public_caps = [
            *_caps(partner_scout, "SCOUT_CAPABILITIES"),
            *writer_caps,
            *[partner_critic.review_paper, partner_critic.citation_verify_capability],
            *[partner_synthesis.extract_digest],
            *[partner_frontier.detect_frontier, partner_frontier.citation_profile],
            *[partner_contradiction.detect_contradictions],
        ]
        for item in public_caps:
            cap = dm._capability_of(item)
            facade = dm.CAPABILITY_FACADES.get(cap.id)
            with self.subTest(skill=cap.id):
                self.assertIsNotNone(facade, f"{cap.id} 未登记门面")
                if getattr(cap, "public", False) or getattr(cap, "acps_skill", False) not in (False, None):
                    self.assertTrue(facade.examples, f"{cap.id} 是平台可见能力，但没有用户问法示例")


class TestApply(unittest.TestCase):
    """apply_to_capabilities 的契约：只动描述类字段，不动身份与可见性。"""

    def setUp(self):
        from acps_cluster.agents import partner_scout  # noqa: PLC0415

        self.original = _caps(partner_scout, "SCOUT_CAPABILITIES")

    def test_不改id与可见性(self):
        enriched = dm.apply_to_capabilities(self.original)
        self.assertEqual(len(enriched), len(self.original))
        for before, after in zip(self.original, enriched):
            cap_b, cap_a = dm._capability_of(before), dm._capability_of(after)
            self.assertEqual(cap_b.id, cap_a.id)
            self.assertEqual(bool(cap_b.public), bool(cap_a.public))
            self.assertEqual(cap_b.acps_skill, cap_a.acps_skill)
            self.assertEqual(cap_b.input_schema, cap_a.input_schema)

    def test_已登记能力描述被增强(self):
        enriched = dm.apply_to_capabilities(self.original)
        by_id = {dm._capability_of(c).id: dm._capability_of(c) for c in enriched}
        arxiv = by_id["arxiv.search"]
        self.assertIn("arXiv", arxiv.description)
        self.assertGreaterEqual(len(arxiv.tags), 5)
        self.assertTrue(arxiv.examples)

    def test_非公开能力保持未登记也能原样透传(self):
        # dataset.fetch 未登记门面：不应被改动、也不应报错
        enriched = dm.apply_to_capabilities(self.original)
        by_id = {dm._capability_of(c).id: dm._capability_of(c) for c in enriched}
        original_by_id = {dm._capability_of(c).id: dm._capability_of(c) for c in self.original}
        if "dataset.fetch" in by_id:
            self.assertEqual(by_id["dataset.fetch"].description, original_by_id["dataset.fetch"].description)

    def test_返回的仍是可调用对象(self):
        enriched = dm.apply_to_capabilities(self.original)
        for item in enriched:
            self.assertTrue(callable(item), "wit 需要函数本体来取 handler")

    def test_幂等(self):
        once = dm.apply_to_capabilities(self.original)
        twice = dm.apply_to_capabilities(once)
        for a, b in zip(once, twice):
            self.assertEqual(dm._capability_of(a).description, dm._capability_of(b).description)
            self.assertEqual(list(dm._capability_of(a).tags), list(dm._capability_of(b).tags))


class TestAgentDescription(unittest.TestCase):
    def test_未登记agent原样返回(self):
        self.assertEqual(dm.agent_discovery_description("no.such.agent", "原始描述"), "原始描述")

    def test_已注册agent逐字返回注册文本(self):
        """已注册的 agent 必须**逐字**返回注册文本（这是唯一的不变量）。

        为什么：wit 有 ACS 指纹保护——代码投影必须与平台注册版逐字一致，
        否则 agent 拒绝启动（`acs_fingerprint_changed_after_submission`）。
        覆盖表（registered_acs_overlay.json）就是为此存在，
        见 docs/acs-fingerprint-guard-and-deploy-runbook.md。

        注意：注册文本**本身可能就包含**检索面（supervisor 当初就是"原文+追加检索面"
        注册上去的），所以不能断言"不含检索面"——只断言"不改写注册文本"。
        """
        registered = dm.registered_agent_description("yanshu.supervisor")
        self.assertTrue(registered, "覆盖表里应当有 supervisor 的注册文本")
        out = dm.agent_discovery_description("yanshu.supervisor", "任意默认描述（不应被采用）")
        self.assertEqual(out, registered, "已注册 agent 必须返回注册文本本身，不得改写")
        self.assertNotIn("任意默认描述", out, "不得采用调用方传入的默认描述")

    def test_未注册agent仍可用门面并追加检索面(self):
        """覆盖表未命中时，门面照常生效（本地开发 / 新 agent 首次注册前）。"""
        saved = dm._OVERLAY_CACHE
        try:
            dm._OVERLAY_CACHE = {"agents": {}, "skills": {}, "skillsByAgent": {}}   # 模拟"未注册"
            default = "原有长描述：调度语义与代码闸门说明。"
            out = dm.agent_discovery_description("yanshu.supervisor", default)
            self.assertTrue(out.startswith(default))
            self.assertIn("【找智能体时看这里】", out)
            self.assertIn("文献综述", out)
        finally:
            dm._OVERLAY_CACHE = saved


class TestSkillDisambiguation(unittest.TestCase):
    """跨 agent 同名技能的注册文本消歧（critic 与 citation 都声明 citation.verify）。"""

    def test_同名技能按agent命名空间取各自注册文本(self):
        saved = dm._OVERLAY_CACHE
        try:
            dm._OVERLAY_CACHE = {
                "agents": {},
                "skills": {"citation.verify": {"description": "扁平版（首个写入者=critic）"}},
                "skillsByAgent": {
                    "yanshu.critic-partner": {"citation.verify": {"description": "critic 注册版"}},
                    "yanshu.citation-integrity": {"citation.verify": {"description": "citation 注册版"}},
                },
            }
            self.assertEqual(
                dm.registered_skill("citation.verify", "yanshu.critic-partner")["description"],
                "critic 注册版")
            self.assertEqual(
                dm.registered_skill("citation.verify", "yanshu.citation-integrity")["description"],
                "citation 注册版")
            self.assertEqual(dm.registered_skill("citation.verify")["description"],
                             "扁平版（首个写入者=critic）", "不传 agent_id 退回扁平表")
            self.assertEqual(dm.registered_skill("citation.verify", "no.such.agent")["description"],
                             "扁平版（首个写入者=critic）", "命名空间未命中退回扁平表")
        finally:
            dm._OVERLAY_CACHE = saved

    def test_命名空间录入的字段经apply生效(self):
        saved = dm._OVERLAY_CACHE
        try:
            dm._OVERLAY_CACHE = {
                "agents": {},
                "skills": {},
                "skillsByAgent": {"yanshu.scout-partner": {
                    "arxiv.search": {"description": "scout 注册版描述", "tags": ["注册tag"],
                                     "examples": ["注册例"], "version": "9.9.9",
                                     "name": "注册技能名", "inputModes": ["application/x-注册"]},
                }},
            }
            from acps_cluster.agents import partner_scout  # noqa: PLC0415
            caps = _caps(partner_scout, "SCOUT_CAPABILITIES")
            enriched = dm.apply_to_capabilities(caps, agent_id="yanshu.scout-partner")
            by_id = {dm._capability_of(c).id: dm._capability_of(c) for c in enriched}
            self.assertEqual(by_id["arxiv.search"].description, "scout 注册版描述")
            self.assertEqual(by_id["arxiv.search"].version, "9.9.9")
            skill = by_id["arxiv.search"].to_acps_skill()
            self.assertEqual(skill["name"], "注册技能名", "注册名走 acps_skill 字典覆写")
            self.assertEqual(skill["inputModes"], ["application/x-注册"])
            # 同列表里的其它能力（注册表未命中）不受影响
            self.assertNotEqual(by_id["openalex.search"].description, "scout 注册版描述")
        finally:
            dm._OVERLAY_CACHE = saved


if __name__ == "__main__":
    unittest.main()
