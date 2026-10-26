"""入口收口与域存储 fail-closed 回归（2026-10-21 安全审查）。

覆盖两处"组件正确、组合失效"的问题：

1. supervisor 的唯一能力是 public，旧逻辑 `unscoped_entry_open()` 会让**任何**
   未指名 skill 的入站请求走 L0；进入 run 后 leader 会用自身身份为 gpu/cpu
   代签令牌。现在默认关闭未指名入场，完整编排只认显式白名单。
2. 代码声明了域技能、但章程目录被删/挂空时，旧逻辑把 `policy_domains` 当空集，
   `required_domains` 为空 → 普通令牌直接放行。现在按 `domain-store-missing` 拒绝。

    python -m unittest acps_cluster.tests.test_entry_hardening -v
"""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

CLUSTER_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = CLUSTER_DIR.parent
for _p in (str(PROJECT_DIR), str(CLUSTER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from acps_cluster.delegation.pep import PepConfig, evaluate  # noqa: E402


def _request(*, leader="anonymous.example", skill=None, peer_verified=True):
    params = {"skillId": skill} if skill else {}
    return SimpleNamespace(
        peer_verified=peer_verified,
        leader_aic=leader,
        partner_aic="supervisor.example",
        command_params=params,
        data_items=[],
        route=[],
        delivery=None,
    )


class SupervisorEntryTest(unittest.TestCase):
    """supervisor 这类"入场即整链执行"的 agent：公开只读能力开放，整链执行收口。"""

    def _cfg(self, **kwargs):
        base = {
            "enabled": True,
            "my_aic": "supervisor.example",
            "public_skills": frozenset({"research.orchestrate"}),
            "has_non_public_skills": False,
            "allow_unscoped_public_entry": False,
        }
        base.update(kwargs)
        return PepConfig(**base)

    def test_未指名任务默认被拒(self):
        outcome = evaluate(_request(), self._cfg())
        self.assertEqual((outcome.decision, outcome.rule), ("deny", "no-credential"))

    def test_入站白名单放行整链(self):
        outcome = evaluate(
            _request(leader="platform.example"),
            self._cfg(inbound_allow_aics=frozenset({"platform.example"})),
        )
        self.assertEqual((outcome.decision, outcome.rule), ("allow", "inbound-allowlist"))

    def test_公开只读能力仍按名放行(self):
        outcome = evaluate(_request(skill="research.orchestrate"), self._cfg())
        self.assertEqual((outcome.decision, outcome.rule), ("allow", "public-skill"))

    def test_传输层未验证一律拒(self):
        outcome = evaluate(_request(peer_verified=False), self._cfg())
        self.assertEqual((outcome.decision, outcome.rule), ("deny", "peer-not-verified"))


class DomainStoreFailClosedTest(unittest.TestCase):
    def test_声明了域但目录缺失_拒(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = PepConfig(
                enabled=True,
                my_aic="eval.example",
                domain_skills={"eval.submit": frozenset({"lab"})},
                domains_dir=Path(td) / "domains-not-there",
                public_skills=frozenset({"agent.info"}),
            )
            outcome = evaluate(_request(skill="eval.submit"), cfg)
            self.assertEqual(
                (outcome.decision, outcome.rule), ("deny", "domain-store-missing")
            )

    def test_目录存在但为空_仍需令牌(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = PepConfig(
                enabled=True,
                my_aic="eval.example",
                domain_skills={"eval.submit": frozenset({"lab"})},
                domains_dir=Path(td),
                public_skills=frozenset({"agent.info"}),
            )
            outcome = evaluate(_request(skill="eval.submit"), cfg)
            # 空目录 = 没有可验证章程，不是"目录丢了"；仍要求令牌，不能静默放行。
            self.assertEqual((outcome.decision, outcome.rule), ("deny", "no-credential"))

    def test_公开与域声明重叠时域优先(self):
        """M4：误把域技能标成 public 时，L0 不得绕过冻结/成员制/配额。"""
        with tempfile.TemporaryDirectory() as td:
            cfg = PepConfig(
                enabled=True,
                my_aic="eval.example",
                public_skills=frozenset({"eval.submit"}),
                domain_skills={"eval.submit": frozenset({"lab"})},
                domains_dir=Path(td),
            )
            outcome = evaluate(_request(skill="eval.submit"), cfg)
            self.assertEqual((outcome.decision, outcome.rule), ("deny", "no-credential"))


if __name__ == "__main__":
    unittest.main()
