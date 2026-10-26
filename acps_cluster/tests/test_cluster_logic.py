"""离线单测：名册/计划闸门/矛盾校验/前沿主题校验/速度计算。

全部不联网、不依赖 wit（只用标准库与 httpx 层的纯函数），
在 my-project/ 目录下跑：

    python -m unittest discover -s acps_cluster/tests -v
"""

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

CLUSTER_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = CLUSTER_DIR.parent
for _p in (str(PROJECT_DIR), str(CLUSTER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from capabilities.contradiction import (  # noqa: E402
    extract_source_ids,
    normalize_id,
    validate_pairs,
)
from capabilities.frontier import topic_density, validate_topics  # noqa: E402
from capabilities.scholar import EmergingWork, compute_velocity  # noqa: E402
from acps_cluster.core.cluster_roster import (  # noqa: E402
    DiscoveredAgent,
    RosterMember,
    check_delegation_target,
    load_discovered,
    load_roster,
    preferred_provider,
    validate_plan,
)


def _fake_acs(aic: str, port: int, skills: tuple[str, ...] = ()) -> str:
    return json.dumps(
        {
            "aic": aic,
            "active": True,
            "endPoints": [
                {"url": f"https://example.test:{port}/rpc", "transport": "JSONRPC"}
            ],
            "skills": [{"id": s} for s in skills],
        }
    )


def _write_discovered(
    base: Path, *, aic: str, name: str, skills: tuple[str, ...], port: int = 9999
) -> None:
    """伪造一条发现缓存（wit 写的格式：{"normalizedSummary":…, "rawAcs": <ACS>}）。"""
    directory = base / "ws-supervisor" / "acps" / "discovered"
    directory.mkdir(parents=True, exist_ok=True)
    raw = json.loads(_fake_acs(aic, port, skills))
    raw["name"] = name
    (directory / f"{aic}.json").write_text(
        json.dumps({"normalizedSummary": {}, "rawAcs": raw}), encoding="utf-8"
    )


class RosterTest(unittest.TestCase):
    def _fixture(self) -> Path:
        """临时集群：scout/writer 已上线（带 ACS），contradiction/frontier 未上线。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        for workspace, aic, skills in (
            ("ws-scout", "AIC-SCOUT", ("arxiv.search", "openalex.search", "semanticscholar.search")),
            ("ws-writer", "AIC-WRITER", ("paper.write",)),
        ):
            ws = base / workspace / "acps"
            ws.mkdir(parents=True)
            (ws / "acs.final.json").write_text(_fake_acs(aic, 9999, skills), encoding="utf-8")
        return base

    def _roster(self):
        return load_roster(self._fixture())

    def test_注册与未注册成员并存(self):
        roster = self._roster()
        by_key = {m.key: m for m in roster}
        self.assertEqual(by_key["scout"].aic, "AIC-SCOUT")
        self.assertTrue(by_key["scout"].available)
        self.assertIsNone(by_key["contradiction"].aic)
        self.assertFalse(by_key["contradiction"].available)
        self.assertEqual(by_key["scout"].endpoint, "https://example.test:9999/rpc")

    def test_计划通过_按key定位并归一化AIC(self):
        roster = self._roster()
        result = validate_plan(
            {
                "needed_capabilities": ["arxiv.search", "paper.write"],
                "steps": [
                    {"capability": "arxiv.search", "partner": "scout"},
                    {"capability": "paper.write", "partner": "writer"},
                ],
            },
            roster,
        )
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(
            [s["partner_aic"] for s in result["resolved_steps"]], ["AIC-SCOUT", "AIC-WRITER"]
        )

    def test_计划拒绝_编造AIC(self):
        roster = self._roster()
        result = validate_plan(
            {
                "needed_capabilities": ["paper.write"],
                "steps": [{"capability": "paper.write", "partner_aic": "AIC-FAKE"}],
            },
            roster,
        )
        self.assertFalse(result["ok"])
        self.assertTrue(any("不在名册" in e for e in result["errors"]), result["errors"])

    def test_计划拒绝_能力错配(self):
        roster = self._roster()
        result = validate_plan(
            {
                "needed_capabilities": ["paper.write"],
                "steps": [{"capability": "paper.write", "partner": "scout"}],
            },
            roster,
        )
        self.assertFalse(result["ok"])
        self.assertTrue(any("只提供" in e for e in result["errors"]), result["errors"])

    def test_计划拒绝_派给未注册伙伴(self):
        roster = self._roster()
        result = validate_plan(
            {
                "needed_capabilities": ["frontier.detect"],
                "steps": [{"capability": "frontier.detect", "partner": "frontier"}],
            },
            roster,
        )
        self.assertFalse(result["ok"])
        self.assertTrue(any("尚未注册上线" in e for e in result["errors"]), result["errors"])

    def test_计划拒绝_声明能力未被步骤覆盖(self):
        roster = self._roster()
        result = validate_plan(
            {
                "needed_capabilities": ["paper.write", "paper.review"],
                "steps": [{"capability": "paper.write", "partner": "writer"}],
            },
            roster,
        )
        self.assertFalse(result["ok"])
        self.assertTrue(any("没有任何可执行的步骤覆盖" in e for e in result["errors"]))

    def test_计划拒绝_未知能力与空计划(self):
        roster = self._roster()
        unknown = validate_plan(
            {
                "needed_capabilities": ["paper.teleport"],
                "steps": [{"capability": "paper.teleport", "partner": "writer"}],
            },
            roster,
        )
        self.assertFalse(unknown["ok"])
        self.assertTrue(any("里都没有这些能力" in e for e in unknown["errors"]))
        self.assertFalse(validate_plan({}, roster)["ok"])
        self.assertFalse(validate_plan("不是对象", roster)["ok"])

    # ── 2026-10-02 重构新增：能力派生 / 偏好 / 名册外发现兜底 ──

    def test_公开能力从ACS派生并与登记取并集(self):
        base = self._fixture()
        # writer 的 ACS 里多一个**没登记**的能力 → 必须被自动派生出来（这就是"反漂移"）
        ws = base / "ws-writer" / "acps"
        (ws / "acs.final.json").write_text(
            _fake_acs("AIC-WRITER", 9999, ("paper.write", "paper.polish")), encoding="utf-8"
        )
        by_key = {m.key: m for m in load_roster(base)}
        self.assertEqual(by_key["writer"].public_capabilities, ("paper.write", "paper.polish"))
        self.assertIn("paper.polish", by_key["writer"].capabilities)
        # scout：ACS 3 个公开能力 ∪ 登记的非公开能力（deep_fetch / dataset.fetch）
        self.assertEqual(
            by_key["scout"].capabilities,
            (
                "arxiv.search",
                "openalex.search",
                "semanticscholar.search",
                "scholar.deep_fetch",
                "dataset.fetch",
            ),
        )

    def test_未上线成员只保留登记能力(self):
        by_key = {m.key: m for m in self._roster()}
        self.assertFalse(by_key["contradiction"].available)
        self.assertEqual(by_key["contradiction"].public_capabilities, ())
        self.assertEqual(by_key["contradiction"].capabilities, ("contradiction.detect",))
        # 未上线成员不会被选为首选提供者
        roster = self._roster()
        self.assertIsNone(preferred_provider(roster, "gpu.run"))
        self.assertEqual(preferred_provider(roster, "arxiv.search").key, "scout")

    def test_偏好顺序取靠前的可用成员(self):
        from acps_cluster.core.cluster_roster import ROSTER, RosterMember, preferred_provider as pick

        first = RosterMember(
            key="a", name="A", role="", module="", workspace="", port_env="X", default_port=1,
            declared_capabilities=("cap.shared",), aic="AIC-A",
        )
        second = RosterMember(
            key="b", name="B", role="", module="", workspace="", port_env="X", default_port=2,
            declared_capabilities=("cap.shared",), aic="AIC-B",
        )
        self.assertEqual(pick([first, second], "cap.shared").key, "a")
        # 靠前的那个不可用时，退到靠后的
        offline_first = RosterMember(
            key="a", name="A", role="", module="", workspace="", port_env="X", default_port=1,
            declared_capabilities=("cap.shared",),
        )
        self.assertEqual(pick([offline_first, second], "cap.shared").key, "b")
        self.assertTrue(ROSTER)  # 真实名册非空

    def test_发现缓存被解析成结构化能力(self):
        base = self._fixture()
        _write_discovered(base, aic="AIC-OUT", name="社区检索伙伴", skills=("community.search",))
        discovered = load_discovered(base)
        self.assertEqual(set(discovered), {"AIC-OUT"})
        agent = discovered["AIC-OUT"]
        self.assertEqual(agent.skills, ("community.search",))
        self.assertEqual(agent.endpoint, "https://example.test:9999/rpc")

    def test_计划可派给名册外已发现的伙伴(self):
        base = self._fixture()
        _write_discovered(base, aic="AIC-OUT", name="社区检索伙伴", skills=("community.search",))
        roster = load_roster(base)
        result = validate_plan(
            {
                "needed_capabilities": ["community.search"],
                "steps": [
                    {"capability": "community.search", "partner_aic": "AIC-OUT", "purpose": "补充检索"}
                ],
            },
            roster,
            discovered=load_discovered(base),
        )
        self.assertTrue(result["ok"], result["errors"])
        step = result["resolved_steps"][0]
        self.assertEqual(step["partner_aic"], "AIC-OUT")
        self.assertEqual(step["source"], "discovered")

    def test_计划拒绝_名册外伙伴能力不符(self):
        base = self._fixture()
        _write_discovered(base, aic="AIC-OUT", name="社区检索伙伴", skills=("community.search",))
        roster = load_roster(base)
        result = validate_plan(
            {
                "needed_capabilities": ["paper.write"],
                "steps": [{"capability": "paper.write", "partner_aic": "AIC-OUT"}],
            },
            roster,
            discovered=load_discovered(base),
        )
        self.assertFalse(result["ok"])
        self.assertTrue(any("ACS 只声明" in e for e in result["errors"]), result["errors"])

    def test_计划拒绝_名册外且未发现的伙伴(self):
        base = self._fixture()
        roster = load_roster(base)
        # 能力用名册里已有的，才能走到"伙伴是否存在"这一步
        result = validate_plan(
            {
                "needed_capabilities": ["paper.write"],
                "steps": [{"capability": "paper.write", "partner_aic": "AIC-FAKE"}],
            },
            roster,
            discovered=load_discovered(base),
        )
        self.assertFalse(result["ok"])
        self.assertTrue(any("也不在本机发现缓存里" in e for e in result["errors"]), result["errors"])

    def test_本地铸造成员的身份从enrollment回退(self):
        """本地铸造（未过注册表）的成员 ACS 里 aic 为空，身份只在 enrollment.json。

        实测 2026-10-02：ws-gpu（provenance=local-dev-ca）就是这样——只看 ACS 会永远"未上线"。
        """
        base = self._fixture()
        ws = base / "ws-frontier" / "acps"
        ws.mkdir(parents=True)
        (ws / "acs.final.json").write_text(
            _fake_acs("", 8450, ("frontier.detect", "frontier.citation_profile")), encoding="utf-8"
        )
        (ws / "enrollment.json").write_text(
            json.dumps({"identity": {"ref": {"aic": "AIC-FRONTIER-LOCAL", "provenance": "local-dev-ca"}}}),
            encoding="utf-8",
        )
        by_key = {m.key: m for m in load_roster(base)}
        frontier = by_key["frontier"]
        self.assertTrue(frontier.available)
        self.assertEqual(frontier.aic, "AIC-FRONTIER-LOCAL")
        self.assertEqual(frontier.endpoint, "https://example.test:8450/rpc")
        result = validate_plan(
            {
                "needed_capabilities": ["frontier.detect"],
                "steps": [{"capability": "frontier.detect", "partner": "frontier"}],
            },
            load_roster(base),
        )
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["resolved_steps"][0]["partner_aic"], "AIC-FRONTIER-LOCAL")

    def test_没有ACS的成员仍然不可用(self):
        # 只有 enrollment、没有 acs.final.json：不当作"已就绪"（没有终稿 ACS 就不该被派任务）
        base = self._fixture()
        ws = base / "ws-frontier" / "acps"
        ws.mkdir(parents=True)
        (ws / "enrollment.json").write_text(
            json.dumps({"identity": {"ref": {"aic": "AIC-FRONTIER-LOCAL"}}}), encoding="utf-8"
        )
        by_key = {m.key: m for m in load_roster(base)}
        self.assertFalse(by_key["frontier"].available)

    def test_名册内步骤标注来源为roster(self):
        roster = self._roster()
        result = validate_plan(
            {
                "needed_capabilities": ["paper.write"],
                "steps": [{"capability": "paper.write", "partner": "writer"}],
            },
            roster,
        )
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["resolved_steps"][0]["source"], "roster")


class ContradictionTest(unittest.TestCase):
    def test_规范化与抽取来源id(self):
        self.assertEqual(normalize_id("2401.00001v3"), "2401.00001")
        ids = extract_source_ids("见 arXiv:2401.00001v2、[Smith2024] 与 10.1000/xyz")
        self.assertEqual(ids, ["2401.00001", "10.1000/xyz", "Smith2024"])

    def test_中文括号里的说明性文字不算来源(self):
        # 回归：曾把"（arXiv:2401.00001，2024）"整段当成来源，污染白名单，
        # 导致"来源不存在"这道校验失效。
        self.assertEqual(
            extract_source_ids("论文1（arXiv:2401.00001，2024）：核心创新点"), ["2401.00001"]
        )
        self.assertEqual(extract_source_ids("发表于（2024）年的工作"), [])
        self.assertNotIn("2024", extract_source_ids("（2024）"))

    def test_模型带修饰的引用被归一化(self):
        # 模型常把来源写成 "arXiv:2401.00001，2024"，应归一到与资料抽取一致的形式
        self.assertEqual(normalize_id("arXiv:2401.00001，2024"), "2401.00001")
        self.assertEqual(normalize_id("  arXiv:2401.00001v2  "), "2401.00001")
        kept, dropped = validate_pairs(
            [
                {
                    "claim_a": "A",
                    "source_a": "arXiv:2401.00001，2024",
                    "claim_b": "B",
                    "source_b": "arXiv:2403.00003，2025",
                    "reconciliation": "r",
                    "testable_prediction": "t",
                }
            ],
            ["2401.00001", "2402.00002"],
        )
        self.assertEqual(kept, [])
        self.assertTrue(any("不存在" in d for d in dropped), dropped)

    def test_丢弃规则(self):
        valid = ["2401.00001", "2402.00002", "2403.00003"]
        base = {
            "claim_a": "X 有效",
            "source_a": "2401.00001",
            "claim_b": "X 无效",
            "source_b": "2402.00002",
            "reconciliation": "条件不同",
            "testable_prediction": "做 A/B 实验",
        }
        kept, dropped = validate_pairs(
            [
                dict(base),  # 合法
                dict(base, source_b="2401.00001"),  # 同源（归一化后与 source_a 相同）
                dict(base, source_b="9999.99999"),  # 来源不存在
                dict(base, source_b="2403.00003", reconciliation=""),  # 缺调和假说
                {k: v for k, v in dict(base, source_b="2403.00003").items() if k != "testable_prediction"},  # 缺预测
                dict(base),  # 与第一条重复
                "不是对象",
            ],
            valid,
        )
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(dropped), 6)
        self.assertTrue(any("同一来源" in d for d in dropped), dropped)
        self.assertTrue(any("不存在" in d for d in dropped), dropped)
        self.assertTrue(any("调和假说" in d for d in dropped), dropped)
        self.assertTrue(any("可检验预测" in d for d in dropped), dropped)
        self.assertTrue(any("重复" in d for d in dropped), dropped)

    def test_未提供有效来源集合时不额外拦截但在同源处仍拦截(self):
        kept, dropped = validate_pairs(
            [
                {
                    "claim_a": "a",
                    "source_a": "S1",
                    "claim_b": "b",
                    "source_b": "S2",
                    "reconciliation": "r",
                    "testable_prediction": "t",
                }
            ],
            [],
        )
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, [])

    def test_非法矛盾类型与置信度被兜底(self):
        kept, _ = validate_pairs(
            [
                {
                    "claim_a": "a",
                    "source_a": "S1",
                    "claim_b": "b",
                    "source_b": "S2",
                    "reconciliation": "r",
                    "testable_prediction": "t",
                    "contradiction_type": "胡说",
                    "confidence": "超确定",
                }
            ],
            ["S1", "S2"],
        )
        self.assertEqual(kept[0].contradiction_type, "直接冲突")
        self.assertEqual(kept[0].confidence, "medium")


class FrontierTest(unittest.TestCase):
    @staticmethod
    def _work(native_id: str) -> EmergingWork:
        return EmergingWork(
            source="openalex",
            native_id=native_id,
            title=f"T{native_id}",
            authors=[],
            published="2026-05-01",
            age_months=5.0,
            citations=10,
            citations_per_month=2.0,
            recent_citations=10,
            url="u",
            topics=["t"],
        )

    def test_序号映射到真实工作(self):
        works = [self._work("W1"), self._work("W2"), self._work("W3")]
        kept, dropped = validate_topics(
            [
                {"topic": "方向A", "why_emerging": "快", "entry_window": "可入场", "work_index": [1, 3]},
                {"topic": "方向B", "why_emerging": "x", "entry_window": "可入场", "work_index": [1]},
                {"topic": "方向C", "why_emerging": "x", "entry_window": "可入场", "work_index": [1, 99]},
                {"topic": "方向D", "why_emerging": "x", "entry_window": "乱填", "work_index": [2, 3]},
                {"topic": "", "why_emerging": "x", "entry_window": "可入场", "work_index": [1, 2]},
                {"topic": "方向F", "why_emerging": "", "entry_window": "可入场", "work_index": [1, 2]},
            ],
            works,
        )
        self.assertEqual([t.topic for t in kept], ["方向A", "方向D"])
        self.assertEqual(kept[0].representative_ids, ["W1", "W3"])
        self.assertEqual(kept[1].entry_window, "需进一步验证")  # 非法取值兜底
        self.assertEqual(len(dropped), 4)
        self.assertTrue(any("不足 2 篇" in d or "只引用了 1 篇" in d for d in dropped))

    def test_非数组输入被拒且非对象主题被丢弃(self):
        kept, dropped = validate_topics("不是数组", [])
        self.assertEqual(kept, [])
        self.assertEqual(len(dropped), 1)
        kept2, dropped2 = validate_topics(["不是对象"], [self._work("W1")])
        self.assertEqual(kept2, [])
        self.assertTrue(any("不是对象" in d for d in dropped2))

    def test_速度证据缺省时由真实数字兜底(self):
        works = [self._work("W1"), self._work("W2")]
        kept, _ = validate_topics(
            [{"topic": "方向", "why_emerging": "快", "entry_window": "可入场", "work_index": [1, 2]}],
            works,
        )
        self.assertIn("引用/月", kept[0].velocity_evidence)

    def test_主题聚集度按工作数降序(self):
        works = [self._work("W1"), self._work("W2"), self._work("W3")]
        count = topic_density(
            [
                EmergingWork(
                    source="openalex",
                    native_id=w.native_id,
                    title=w.title,
                    authors=[],
                    published=w.published,
                    age_months=1.0,
                    citations=0,
                    citations_per_month=0.0,
                    recent_citations=0,
                    url="u",
                    topics=topics,
                )
                for w, topics in (
                    (works[0], ["A", "B"]),
                    (works[1], ["A"]),
                    (works[2], ["A", "C"]),
                )
            ]
        )
        self.assertEqual(count[0], ("A", 3))
        self.assertEqual(dict(count)["B"], 1)


class VelocityTest(unittest.TestCase):
    def test_正常计算(self):
        age, velocity = compute_velocity(30, "2026-03-26", today=date(2026, 9, 26))
        self.assertAlmostEqual(age, 6.04, places=1)
        self.assertAlmostEqual(velocity, 4.96, places=1)

    def test_月龄不足一个月时分母按1个月(self):
        age, velocity = compute_velocity(5, "2026-09-26", today=date(2026, 9, 26))
        self.assertEqual(age, 0.0)
        self.assertEqual(velocity, 5.0)

    def test_缺日期与未来日期返回None(self):
        self.assertIsNone(compute_velocity(5, None, today=date(2026, 9, 26)))
        self.assertIsNone(compute_velocity(5, "", today=date(2026, 9, 26)))
        self.assertIsNone(compute_velocity(5, "2027-01-01", today=date(2026, 9, 26)))
        self.assertIsNone(compute_velocity(5, "不是日期", today=date(2026, 9, 26)))


class SupervisorPlaybookTest(unittest.TestCase):
    """剧本里的两条纪律（2026-10-02 全链路首跑暴露的缺陷）。"""

    def _playbook(self):
        from acps_cluster.agents import supervisor  # noqa: PLC0415

        return supervisor._playbook("【scout】dataset.fetch ... 【experiment】exp.run", ["scout", "experiment"])

    def test_用代码侧wait_partner而非散文纪律(self):
        text = self._playbook()
        self.assertIn("wait_partner", text)
        self.assertIn("再次调用 wait_partner", text)
        self.assertIn("严禁", text)

    def test_数据集只传名称与URL不贴正文(self):
        text = self._playbook()
        self.assertIn("<name> | <url>", text)
        self.assertIn("绝对不要把 CSV 正文贴过去", text)
        self.assertIn("gpu.data.fetch", text)

    def test_运行配置含三个本地闸门(self):
        from acps_cluster.agents import supervisor  # noqa: PLC0415

        cfg = supervisor.build_run_config([])
        names = {t.name for t in cfg.tools}
        # 第二十一/二十二轮起：模式开关（deep_research/auto_research）与
        # wait_partner（桥版）也走 RunConfig.tools（hooks 通道模型不可见）；
        # 2026-10-05 起新增 wait_many（屏障式批量等待）；
        # 2026-10-11 起新增 acps_delegate_authenticated（受保护伙伴 CPU/GPU/KB 必须持令牌）；
        # 2026-10-16 起新增 acps_delegate_public（公开能力：入参全标量，替代数组入参被
        # 模型提供方丢空后必然失败的内置 acps_delegate）；
        # 2026-10-16 起新增 submit_job / check_job（L2 作业化交路：受理-作业-取件，
        # 复杂长任务不挂前端等待——秒级回执 + 后台跑 + 「查作业」取件）
        self.assertEqual(names, {"plan_gate", "review_gate", "product_gate",
                                 "deep_research", "auto_research", "wait_partner",
                                 "wait_many", "acps_delegate_authenticated",
                                 "acps_delegate_public", "submit_job", "check_job"})
        self.assertEqual(cfg.max_steps, 200)  # 2026-10-05 起默认 200(环境可配 ACPS_SUPERVISOR_MAX_STEPS)
        # wait_partner 的单次预算（360s）必须小于工具超时，否则会被框架打断
        self.assertGreater(cfg.tool_timeout_seconds, 360)


class WaitPartnerToolTest(unittest.TestCase):
    """wait_partner：代码侧阻塞等待，治"leader 在伙伴做完前就收尾"。"""

    class _FakeRuntime:
        def __init__(self, sequence):
            self.sequence = list(sequence)
            self.calls = []

        async def aip_task(self, state, *, action, task_ref=None, offset=0):
            self.calls.append((action, task_ref))
            return self.sequence.pop(0) if self.sequence else {"state": "working", "products": []}

    def _run(self, sequence, args=None, **kwargs):
        import asyncio  # noqa: PLC0415

        from acps_cluster.agents import supervisor  # noqa: PLC0415

        rt = self._FakeRuntime(sequence)
        tool = supervisor._make_wait_partner_tool(rt, object(), poll_seconds=0, **kwargs)
        payload = {"task_ref": "t-1"} if args is None else args
        return asyncio.run(tool.handler(payload)), rt

    def test_一直等到终态(self):
        out, rt = self._run(
            [
                {"state": "working", "products": []},
                {"state": "working", "products": []},
                {"state": "awaiting-completion", "products": [{"id": "p"}], "summary": "数据已取回"},
            ]
        )
        self.assertTrue(out["ok"])
        self.assertEqual(out["products"], 1)
        self.assertEqual(out["state"], "awaiting-completion")
        self.assertIn("数据已取回", out["summary"])
        self.assertEqual(len(rt.calls), 3)          # 内部轮询了 3 次，模型只调 1 次
        # 只断言"轮询的是同一个任务"，不绑定 RPC 动词：`wait` → `get` 是等价实现选择，
        # 2026-10-21 审查发现旧断言会把一次重构变成红灯（上面的 state/products 已覆盖行为）。
        self.assertIn(rt.calls[0][0], {"get", "wait"})
        self.assertEqual({call[1] for call in rt.calls}, {"t-1"})

    def test_超时如实报still_working(self):
        out, _rt = self._run([{"state": "working", "products": []}], args={"task_ref": "t-9", "max_seconds": 0})
        self.assertFalse(out["ok"])
        self.assertTrue(out["still_working"])
        self.assertIn("再次调用 wait_partner", out["note"])

    def test_缺task_ref报错(self):
        out, rt = self._run([], args={})
        self.assertFalse(out["ok"])
        self.assertIn("task_ref", out["error"])
        self.assertEqual(rt.calls, [])


class SupervisorDaemonTest(unittest.TestCase):
    """常驻入口（partner_supervisor.py）的收件箱/出件箱约定。"""

    def test_可导入且约定路径在workspace下(self):
        from acps_cluster.agents import partner_supervisor  # noqa: PLC0415

        ws = partner_supervisor.WORKSPACE
        self.assertEqual(partner_supervisor.INBOX, ws / "inbox")
        self.assertEqual(partner_supervisor.OUTBOX, ws / "outbox")
        self.assertEqual(partner_supervisor.DONE, ws / "inbox" / "done")

    def test_运行配置与一次性入口共用(self):
        from acps_cluster.agents import partner_supervisor  # noqa: PLC0415
        from acps_cluster.agents import supervisor  # noqa: PLC0415

        self.assertIs(partner_supervisor.supervisor.build_run_config, supervisor.build_run_config)


class DelegationTargetGateTest(unittest.TestCase):
    """委派点的标量闸门（plan_gate 的两条硬约束下沉到这里，2026-10-16）。

    全部是标量比较：模型提供方偶尔把 array 参数丢空也拦不住正常委派，但编造伙伴、
    错派能力、派给未注册成员这三类仍必须在委派那一刻被挡下。
    """

    def setUp(self):
        self.online = RosterMember(
            key="scout", name="Scout", role="", module="partner_scout", workspace="ws-scout",
            port_env="ACPS_SCOUT_PORT", default_port=8445,
            declared_capabilities=("arxiv.search",), aic="AIC-SCOUT",
        )
        self.offline = RosterMember(
            key="gpu", name="GPU", role="", module="partner_gpu", workspace="ws-gpu",
            port_env="ACPS_GPU_PORT", default_port=8460, declared_capabilities=("gpu.run",),
        )
        self.roster = [self.online, self.offline]
        self.discovered = {"AIC-OUT": DiscoveredAgent(aic="AIC-OUT", name="社区检索伙伴",
                                                      skills=("community.search",))}

    def _check(self, token, cap=None):
        return check_delegation_target(self.roster, partner_aic=token, capability=cap,
                                       discovered=self.discovered)

    def test_名册内AIC加匹配能力放行(self):
        self.assertEqual(self._check("AIC-SCOUT", "arxiv.search"),
                         (True, "名册成员 scout（AIC=AIC-SCOUT）"))

    def test_key与展示名同样被认(self):
        self.assertTrue(self._check("scout", "arxiv.search")[0])
        self.assertTrue(self._check("Scout", "arxiv.search")[0])

    def test_能力不匹配拒绝(self):
        ok, why = self._check("AIC-SCOUT", "gpu.run")
        self.assertFalse(ok)
        self.assertIn("不提供能力 'gpu.run'", why)

    def test_未注册成员拒绝(self):
        ok, why = self._check("AIC-GPU-NOT-EXIST", "gpu.run")
        self.assertFalse(ok)
        self.assertIn("既不在名册也不在发现缓存", why)
        ok2, why2 = check_delegation_target(self.roster, partner_aic="gpu",
                                            capability="gpu.run", discovered={})
        self.assertFalse(ok2)
        self.assertIn("尚未注册上线", why2)

    def test_编造AIC拒绝并给出补救路径(self):
        ok, why = self._check("1.2.156.3088.1.EVIL.000000.000000.0000", "arxiv.search")
        self.assertFalse(ok)
        self.assertIn("不要编造伙伴", why)
        self.assertIn("acps_discover", why)

    def test_leader自己不可被委派(self):
        # 服务器名册实测：supervisor 是 kind="leader"、available=True、
        # 且 ACS 声明了 research.orchestrate → 展示层会跳过它，闸门若不跳就能自委派成环
        leader = RosterMember(
            key="supervisor", name="研枢-Supervisor", role="编排", module="supervisor",
            workspace="ws-supervisor", port_env="ACPS_SUPERVISOR_PORT", default_port=8444,
            kind="leader", declared_capabilities=("research.orchestrate",), aic="AIC-LEADER",
        )
        roster = self.roster + [leader]
        for token in ("supervisor", "AIC-LEADER", "研枢-Supervisor"):
            ok, why = check_delegation_target(roster, partner_aic=token,
                                              capability="research.orchestrate")
            self.assertFalse(ok, f"{token} 不应被放行")
            self.assertIn("不能委派给自己", why)

    def test_leader拒绝不影响partner(self):
        self.assertTrue(self._check("AIC-SCOUT", "arxiv.search")[0])

    def test_空参数拒绝(self):
        self.assertFalse(self._check("")[0])
        self.assertFalse(self._check("   ")[0])

    def test_发现缓存里的伙伴放行(self):
        self.assertTrue(self._check("AIC-OUT", "community.search")[0])
        ok, why = self._check("AIC-OUT", "not.declared")
        self.assertFalse(ok)
        self.assertIn("缓存声明里没有能力", why)

    def test_硬隔离模式下发现缓存不放行(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"ACPS_ROSTER_ALLOW_DISCOVERED": "0"}):
            ok, why = self._check("AIC-OUT", "community.search")
        self.assertFalse(ok)
        self.assertIn("硬隔离模式", why)


class RouteBLivenessProbeTest(unittest.TestCase):
    """路线 B 成员（不在平台注册集合里）的端点探活（2026-10-16 定案：走探活）。"""

    def setUp(self):
        self.route_b = RosterMember(
            key="gpu", name="GPU", role="", module="partner_gpu", workspace="ws-gpu",
            port_env="ACPS_GPU_PORT", default_port=8460,
            declared_capabilities=("gpu.run",), aic="AIC-GPU-LOCAL",
            endpoint="https://localhost:8460/rpc",
        )
        self.registered = RosterMember(
            key="scout", name="Scout", role="", module="partner_scout", workspace="ws-scout",
            port_env="ACPS_SCOUT_PORT", default_port=8445,
            declared_capabilities=("arxiv.search",), aic="AIC-SCOUT",
            endpoint="https://192.0.2.10:8445/rpc",
        )
        self.roster = [self.route_b, self.registered]

    def _check(self, token, reachable, registered_aics):
        return check_delegation_target(
            self.roster, partner_aic=token, capability="gpu.run" if token == "gpu" else "arxiv.search",
            discovered={}, registered_aics=registered_aics, reachable=reachable,
        )

    def test_路线B端点探不到就拒绝(self):
        ok, why = self._check("gpu", lambda ep: False, set())
        self.assertFalse(ok)
        self.assertIn("端点探活失败", why)
        self.assertIn("https://localhost:8460/rpc", why)
        self.assertIn("不签发令牌", why)

    def test_路线B探得到放行(self):
        self.assertTrue(self._check("gpu", lambda ep: True, set())[0])

    def test_平台注册成员不探活(self):
        # 注册成员的存活由心跳/平台侧反映，且 mTLS 握手自会暴露不可达 → 不该被探活拖慢
        ok, why = self._check("scout", lambda ep: False, {"AIC-SCOUT"})
        self.assertTrue(ok)
        self.assertIn("名册成员 scout", why)

    def test_覆盖表读不到时一律不探(self):
        # registered_aics=None 表示覆盖表不可读；此时不能因为缺配置文件卡住所有委派
        self.assertTrue(self._check("gpu", lambda ep: False, None)[0])

    def test_reachable_不是回调时按不探活处理(self):
        self.assertTrue(self._check("gpu", "不是函数", set())[0])

    def test_探活只用于非注册身份_空集合含义是全部未注册(self):
        # 空集合 ≠ None：它是"读到了覆盖表但里面没有这些身份"，所以两条都要被探
        self.assertFalse(self._check("scout", lambda ep: False, set())[0])

    def test_探活回调收到的是端点字符串(self):
        seen = []
        check_delegation_target(self.roster, partner_aic="gpu", capability="gpu.run",
                                discovered={}, registered_aics=set(),
                                reachable=lambda ep: seen.append(ep) or False)
        self.assertEqual(seen, ["https://localhost:8460/rpc"])


class EndpointReachableHelperTest(unittest.TestCase):
    """探活实现本身：开一个临时监听端口应当判可达，关闭后判不可达。"""

    def test_监听与否的判定(self):
        import socket

        from acps_cluster.agents import supervisor as s

        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            self.assertTrue(s._endpoint_reachable(f"http://127.0.0.1:{port}/rpc"))
        finally:
            srv.close()
        self.assertFalse(s._endpoint_reachable(f"http://127.0.0.1:{port}/rpc", timeout=0.3))

    def test_畸形输入一律判不可达(self):
        from acps_cluster.agents import supervisor as s

        for bad in ("", "不是 URL", "localhost:8460", "https:///rpc"):
            self.assertFalse(s._endpoint_reachable(bad, timeout=0.2), bad)

    def test_首次失败会重试一次(self):
        # 隧道刚重启/瞬时抖动时，单次探活会把可用伙伴误判为不可达
        import socket
        from unittest import mock

        from acps_cluster.agents import supervisor as s

        class _Conn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with mock.patch.object(socket, "create_connection",
                               side_effect=[OSError("refused"), _Conn()]) as cc:
            self.assertTrue(s._endpoint_reachable("http://127.0.0.1:1/rpc", timeout=0.01))
        self.assertEqual(cc.call_count, 2)


if __name__ == "__main__":
    unittest.main()
