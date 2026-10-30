"""域可见性 + ACS 透传回归：

- 本仓 `delegation.visibility` 的纯谓词语义（与 discovery-server 同形）；
- 策略投影哑读取；
- `make_acs.build_skills` 不再剥掉 visibility/domains，并拦住两种漂移：
  域名缺失、注册版是 domain 但代码静默降级；
- registry AgentSkill schema 接受这两个字段；
- partner_kb 的代码声明能被 PEP 侧 `capability_domain_scopes` 看到。

    python -m unittest acps_cluster.tests.test_visibility -v
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

CLUSTER = Path(__file__).resolve().parents[1]
PROJECT = CLUSTER.parent
for _p in (str(PROJECT), str(CLUSTER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from acps_cluster.core.agent_profile import make_agent_info  # noqa: E402
from acps_cluster.delegation.pep import capability_domain_scopes  # noqa: E402
from acps_cluster.delegation.visibility import (  # noqa: E402
    filter_rows,
    load_policy_projections,
    overfetch_limit,
    skill_visible_to,
    write_policy_projections,
)

MEMBER = "1.2.156.3088.1.1.EXPRMT.LABEXE01.RUN01.1207"
OUTSIDER = "1.2.156.3088.1.1.BJ5X2N.QYTBJJ.6GH1CI.12KL"
PROVIDER = "1.2.156.3088.1.1.GP5060.SANDBOX1.RUN01.0IGR"
DOMAIN = "lab-x-y"
POLICIES = {DOMAIN: {MEMBER: []}}

ACS = {
    "aic": PROVIDER,
    "skills": [
        {"id": "gpu.env", "description": "公开"},
        {"id": "gpu.run", "description": "域内", "visibility": "domain", "domains": [DOMAIN]},
    ],
}
ROWS = [
    {"acs": ACS, "skill_id": "gpu.env", "ranking": 1},
    {"acs": ACS, "skill_id": "gpu.run", "ranking": 2},
]


def _load_script(name: str):
    path = CLUSTER / "deploy" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_test_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VisibilityPredicateTest(unittest.TestCase):
    def test_无字段默认public_域技能按成员裁决(self):
        for caller in (MEMBER, OUTSIDER, None):
            self.assertTrue(skill_visible_to(ACS, "gpu.env", caller, POLICIES))
        self.assertTrue(skill_visible_to(ACS, "gpu.run", MEMBER, POLICIES))
        self.assertFalse(skill_visible_to(ACS, "gpu.run", OUTSIDER, POLICIES))
        self.assertFalse(skill_visible_to(ACS, "gpu.run", None, POLICIES))

    def test_行过滤与响应体脱敏(self):
        member = filter_rows(ROWS, MEMBER, POLICIES)
        outsider = filter_rows(ROWS, OUTSIDER, POLICIES)
        self.assertEqual([r["skill_id"] for r in member], ["gpu.env", "gpu.run"])
        self.assertEqual([r["skill_id"] for r in outsider], ["gpu.env"])
        outsider_skills = {s["id"] for s in outsider[0]["acs"]["skills"]}
        self.assertEqual(outsider_skills, {"gpu.env"})
        # 非变异：原 ACS 不被改写
        self.assertEqual(len(ACS["skills"]), 2)

    def test_域名缺失或类型错误时fail_closed(self):
        bad = {"skills": [{"id": "s", "visibility": "domain", "domains": "lab-x-y"}]}
        self.assertFalse(skill_visible_to(bad, "s", MEMBER, POLICIES))
        empty = {"skills": [{"id": "s", "visibility": "domain", "domains": []}]}
        self.assertFalse(skill_visible_to(empty, "s", MEMBER, POLICIES))

    def test_投影目录哑读取与坏文件跳过(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_policy_projections(
                {"domainId": DOMAIN, "members": {MEMBER: "consumer"}},
                root,
            )
            (root / "broken.json").write_text("{not-json", encoding="utf-8")
            policies = load_policy_projections(root)
            self.assertEqual(set(policies), {DOMAIN})
            self.assertTrue(skill_visible_to(ACS, "gpu.run", MEMBER, policies))
            self.assertFalse(skill_visible_to(ACS, "gpu.run", OUTSIDER, policies))

    def test_超取上限(self):
        self.assertEqual(overfetch_limit(10), 30)
        self.assertEqual(overfetch_limit(50), 50)
        self.assertEqual(overfetch_limit(0), 3)


class _Capability:
    def __init__(self, skill: dict):
        self._skill = skill

    def to_acps_skill(self) -> dict:
        return dict(self._skill)


class _Item:
    def __init__(self, skill: dict):
        self.capability = _Capability(skill)


class _Agent:
    def __init__(self, *skills: dict):
        self.capabilities = [_Item(skill) for skill in skills]


def _skill(**overrides) -> dict:
    base = {
        "id": "kb.query", "name": "查询知识库", "description": "只回分数",
        "version": "1.0.0", "tags": ["kb"],
    }
    base.update(overrides)
    return base


class AcsVisibilityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.make_acs = _load_script("make_acs")
        cls.validate_acs = _load_script("validate_acs")

    def test_visibility与domains不再被SKILL_KEYS剥掉(self):
        agent = _Agent(_skill(visibility="domain", domains=["yanshu-kb"]))
        skills = self.make_acs.build_skills(agent, [], "1.0.0")
        self.assertEqual(skills[0]["visibility"], "domain")
        self.assertEqual(skills[0]["domains"], ["yanshu-kb"])
        self.assertNotIn("visibility", self.make_acs.build_skills(_Agent(_skill()), [], "1.0.0")[0])

    def test_domain可见但缺domains必须报错(self):
        agent = _Agent(_skill(visibility="domain"))
        with self.assertRaises(SystemExit):
            self.make_acs.build_skills(agent, [], "1.0.0")

    def test_注册版domain不能静默降级(self):
        agent = _Agent(_skill())
        carrier = [{"id": "kb.query", "visibility": "domain", "domains": ["yanshu-kb"]}]
        with self.assertRaises(SystemExit):
            self.make_acs.build_skills(agent, carrier, "1.0.0")

    def test_partner_kb声明能被代码侧解析且进入scopes(self):
        from acps_cluster.agents import partner_kb

        declared = self.validate_acs.domain_visibility_from_module("partner_kb")
        self.assertEqual(declared.get("kb.query"), ["yanshu-kb"])
        scopes = capability_domain_scopes(partner_kb.KB_CAPABILITIES)
        self.assertEqual(scopes.get("kb.query"), frozenset({"yanshu-kb"}))

    def test_registry_schema接受visibility字段(self):
        schema_path = PROJECT.parent / "ACPs-community/registry-server/app/agent/acsSchema.json"
        if not schema_path.is_file():
            self.skipTest("registry schema 不在工作区")
        try:
            from jsonschema import Draft202012Validator
            from jsonschema.exceptions import ValidationError
        except ImportError:
            self.skipTest("jsonschema 未安装")
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        agent_skill = schema["$defs"]["AgentSkill"]
        validator = Draft202012Validator(agent_skill)
        validator.validate(_skill(visibility="domain", domains=["yanshu-kb"]))
        with self.assertRaises(ValidationError):
            validator.validate(_skill(visibility="domain"))


class AgentInfoTemplateTest(unittest.TestCase):
    """纯 domain agent 的 public 锚点：agent.info 是普通 public 能力，不是过滤特例。"""

    def _info(self):
        return make_agent_info(
            agent_id="yanshu.example",
            display_name="示例资源 agent",
            summary="公开档案：只说明身份与职责。",
            public_skills=("example.status", "example.health"),
        )

    def test_agent_info是public且非domain(self):
        from acps_cluster.delegation.pep import capability_visibility

        info = self._info()
        public, _ = capability_visibility([info])
        self.assertIn("agent.info", public)
        self.assertNotIn("agent.info", capability_domain_scopes([info]))
        skill = info.capability.to_acps_skill()
        self.assertEqual(skill.get("id"), "agent.info")
        self.assertIsNone(skill.get("visibility"))

    def test_handler只返回公开档案(self):
        result = asyncio.run(self._info()())
        self.assertEqual(result["agentId"], "yanshu.example")
        self.assertEqual(result["displayName"], "示例资源 agent")
        self.assertEqual(result["publicSkills"], ["example.health", "example.status"])
        self.assertEqual(result["profileVisibility"], "public")

    def test_纯domain能力加agent_info后守卫通过(self):
        from acps_cluster.delegation.pep import capability_visibility

        domain_only = SimpleNamespace(
            id="example.domain_only", public=False,
            acps_skill={"visibility": "domain", "domains": ["d"]},
        )
        caps = [domain_only, self._info()]
        public, _ = capability_visibility(caps)
        domain = set(capability_domain_scopes(caps))
        self.assertEqual(public - domain, {"agent.info"})

    def test_validate_acs识别工厂调用(self):
        validate_acs = _load_script("validate_acs")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "fake_agent.py").write_text(
                "from agent_profile import make_agent_info\n"
                "INFO = make_agent_info(agent_id='a', display_name='A', summary='s')\n",
                encoding="utf-8",
            )
            with mock.patch.object(validate_acs, "CLUSTER", root):
                declared, arity = validate_acs.capability_ids_from_module("fake_agent")
        self.assertIn("agent.info", declared)
        self.assertEqual(arity.get("agent.info"), 0)


class AgentDiscoverabilityGuardTest(unittest.TestCase):
    """能力不可见 ≠ agent 不可见：每个 agent 至少保留一个 public 非 domain 能力。

    这是对“无可见能力则整个 agent 消失”这一副作用的守门：现有 agent 都有
    `research.orchestrate` / `gpu.env` / `kb.schema` 之类的公开入口，
    不需要额外加 `agent.info`，也不会改已注册 ACS。将来若新增一个纯 domain agent，
    这个测试会先失败，迫使显式选择“加 public profile 能力”或“接受 agent 隐身”。
    """

    AGENT_MODULES = (
        "supervisor", "partner_scout", "partner_writer", "partner_synthesis",
        "partner_critic", "partner_frontier", "partner_contradiction",
        "partner_experiment", "partner_gpu", "partner_cpu", "partner_kb",
        "partner_hpc", "partner_citation",
    )

    @staticmethod
    def _module_capabilities(module) -> list:
        caps = []
        for value in vars(module).values():
            if (
                getattr(value, "capability", None) is not None
                and getattr(value, "__module__", None) == module.__name__
            ):
                caps.append(value)
            elif isinstance(value, (list, tuple)):
                # 列表是 agent 自己的能力清单；共享工厂（agent_profile.make_agent_info）
                # 产物的 __module__ 不在这里，也要算进来。
                caps.extend(item for item in value if getattr(item, "capability", None) is not None)
        return caps

    def test_each_agent_has_public_non_domain_capability(self):
        from acps_cluster.delegation.pep import (
            capability_domain_scopes,
            capability_visibility,
        )

        missing: list[str] = []
        for module_name in self.AGENT_MODULES:
            module = importlib.import_module(f"acps_cluster.agents.{module_name}")
            capabilities = self._module_capabilities(module)
            public, _ = capability_visibility(capabilities)
            domain = set(capability_domain_scopes(capabilities))
            if not (public - domain):
                missing.append(f"{module_name}(public={sorted(public)}, domain={sorted(domain)})")
        self.assertFalse(
            missing,
            "以下 agent 没有 public 非 domain 能力，会在能力全隐藏时整体消失: "
            + ", ".join(missing),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
