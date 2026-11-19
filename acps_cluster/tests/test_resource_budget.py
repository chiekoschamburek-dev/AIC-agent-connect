"""域级资源量配额（GPU 卡时）离线单测：章程解析 → 上下文传递 → 运行时检查与记账。

与 `test_domain.py` 不同，本文件的用例**不需要 dev CA 私钥**——全程用临时目录里的
独立账本与直接构造的 `BudgetScope`，干净克隆即可跑通（PEP 接线用替身 verify 路径，
不涉签名）。
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

CLUSTER_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = CLUSTER_DIR.parent
for _p in (str(PROJECT_DIR), str(CLUSTER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from acps_cluster.delegation import quota, resource_budget  # noqa: E402
from acps_cluster.delegation.domain_manifest import (  # noqa: E402
    DomainPolicy,
    build_manifest,
    to_policy,
)
from acps_cluster.delegation.pep import PepConfig, PepOutcome, pep_decide  # noqa: E402

DOMAIN = "lab-x-y"
CALLER = "1.2.156.3088.1.1.EXPRMT.LABEXE01.RUN01.1207"


def _policy(*, limit: float | None = 1800) -> DomainPolicy:
    body = build_manifest(
        domain_id=DOMAIN,
        members=[
            {"aic": "1.2.156.3088.1.1.GP5060.SANDBOX1.RUN01.0IGR", "role": "provider"},
            {"aic": CALLER, "role": "consumer"},
        ],
        assets=[{"providerAic": "1.2.156.3088.1.1.GP5060.SANDBOX1.RUN01.0IGR",
                 "skill": "gpu.run", "allowedMembers": ["consumer"]}],
        issuers=["1.2.156.3088.1.1.GP5060.SANDBOX1.RUN01.0IGR"],
        resource_quota=(
            {"gpuSecondsPerCallerPerDay": limit} if limit is not None else None
        ),
    )
    return to_policy(body, frozenset({"1.2.156.3088.1.1.GP5060.SANDBOX1.RUN01.0IGR", CALLER}))


class ManifestQuotaTest(unittest.TestCase):
    """章程字段的读写与校验：不写就不带、写了必须合法、解析只认约定的键。"""

    def test_入口章程CLI可写资源配额(self):
        """`entry_grant prepare --gpu-seconds-per-day` 是生产上的写入路径，必须是通的。"""
        from acps_cluster.tools.entry_grant import build_entry_manifest

        with_quota = build_entry_manifest(
            domain="lab-x-y", supervisor_aic="1.2.156.3088.1.1.AAAA.PROV.RUN01.01",
            peer_aic="1.2.156.3088.1.1.BBBB.CONS.RUN01.02", gpu_seconds_per_day=1800,
        )
        self.assertEqual(with_quota.get("resourceQuota"), {"gpuSecondsPerCallerPerDay": 1800.0})
        without = build_entry_manifest(
            domain="lab-x-y", supervisor_aic="1.2.156.3088.1.1.AAAA.PROV.RUN01.01",
            peer_aic="1.2.156.3088.1.1.BBBB.CONS.RUN01.02",
        )
        self.assertNotIn("resourceQuota", without)

    def test_未声明配额时章程不带该字段(self):
        body = build_manifest(
            domain_id="d", members=[{"aic": "A", "role": "provider"}],
            assets=[], issuers=["A"],
        )
        self.assertNotIn("resourceQuota", body)
        self.assertEqual(_policy(limit=None).resource_limits(), {})

    def test_声明配额后解析成资源上限(self):
        self.assertEqual(_policy(limit=1800).resource_limits(), {"gpuSeconds": 1800.0})

    def test_非法配额在建章程时被拒(self):
        with self.assertRaises(ValueError):
            build_manifest(
                domain_id="d", members=[{"aic": "A", "role": "provider"}],
                assets=[], issuers=["A"],
                resource_quota={"gpuSecondsPerCallerPerDay": -1},
            )
        with self.assertRaises(ValueError):
            build_manifest(
                domain_id="d", members=[{"aic": "A", "role": "provider"}],
                assets=[], issuers=["A"],
                resource_quota={"gpuSecondsPerCallerPerDay": "很多"},
            )

    def test_认不出的键被忽略(self):
        body = build_manifest(
            domain_id="d", members=[{"aic": "A", "role": "provider"}],
            assets=[], issuers=["A"],
            resource_quota={"gpuSecondsPerCallerPerDay": 60, "foo": 9, "PerCallerPerDay": 5},
        )
        self.assertEqual(to_policy(body, frozenset({"A"})).resource_limits(), {"gpuSeconds": 60.0})


class BudgetLedgerTest(unittest.TestCase):
    """账本：按 (域, 调用方, 资源) 汇总，只看本 UTC 自然日。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self._tmp.name) / "quota.sqlite3"
        self.addCleanup(self._tmp.cleanup)

    def test_记账后可读回(self):
        quota.record_resource(self.ledger, domain=DOMAIN, caller_aic=CALLER,
                              resource="gpuSeconds", amount=12.5, now=1_700_000_000.0)
        used = quota.resource_used(self.ledger, domain=DOMAIN, caller_aic=CALLER,
                                   resource="gpuSeconds", since=1_699_000_000.0)
        self.assertAlmostEqual(used, 12.5)

    def test_跨域与跨调用方互不串账(self):
        quota.record_resource(self.ledger, domain=DOMAIN, caller_aic=CALLER,
                              resource="gpuSeconds", amount=30, now=1_700_000_000.0)
        other = quota.resource_used(self.ledger, domain="other", caller_aic=CALLER,
                                    resource="gpuSeconds", since=0)
        self.assertEqual(other, 0.0)
        other_caller = quota.resource_used(self.ledger, domain=DOMAIN, caller_aic="someone",
                                           resource="gpuSeconds", since=0)
        self.assertEqual(other_caller, 0.0)

    def test_窗口外用量不计入(self):
        quota.record_resource(self.ledger, domain=DOMAIN, caller_aic=CALLER,
                              resource="gpuSeconds", amount=99, now=1_700_000_000.0)
        self.assertEqual(quota.resource_used(self.ledger, domain=DOMAIN, caller_aic=CALLER,
                                            resource="gpuSeconds", since=1_700_000_001.0), 0.0)


class BudgetScopeTest(unittest.TestCase):
    """运行时检查与记账：没有域上下文一律放行（向后兼容），有上下文才生效。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self._tmp.name) / "quota.sqlite3"
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(resource_budget.clear)

    def _scope(self, limit: float = 100.0) -> resource_budget.BudgetScope:
        return resource_budget.BudgetScope(
            domain=DOMAIN, caller=CALLER, limits={"gpuSeconds": limit},
            ledger=self.ledger, revision=1,
        )

    def test_无域上下文时不受限(self):
        resource_budget.clear()
        self.assertIsNone(resource_budget.check("gpuSeconds"))
        self.assertIsNone(resource_budget.charge("gpuSeconds", 10))
        self.assertIsNone(resource_budget.remaining("gpuSeconds"))

    def test_未声明该资源时不受限(self):
        resource_budget.set_scope(self._scope())
        self.assertIsNone(resource_budget.check("cpuSeconds"))
        self.assertIsNone(resource_budget.remaining("cpuSeconds"))

    def test_余量按记账递减_用尽后拒绝(self):
        resource_budget.set_scope(self._scope(limit=100))
        self.assertIsNone(resource_budget.check("gpuSeconds"))
        receipt = resource_budget.charge("gpuSeconds", 60)
        self.assertEqual(receipt["amount"], 60)
        self.assertAlmostEqual(receipt["remainingAfter"], 40)
        self.assertIsNone(resource_budget.check("gpuSeconds"))
        resource_budget.charge("gpuSeconds", 40)
        refusal = resource_budget.check("gpuSeconds")
        self.assertIsNotNone(refusal)
        self.assertEqual(refusal["error"], "quota-exhausted")
        self.assertEqual(refusal["limit"], 100)
        self.assertIn("已用", refusal["detail"])

    def test_总开关关闭时不生效(self):
        resource_budget.set_scope(self._scope(limit=1))
        resource_budget.charge("gpuSeconds", 5)
        with mock.patch.dict("os.environ", {resource_budget.ENV_TOGGLE: "off"}):
            self.assertFalse(resource_budget.enabled())
            self.assertIsNone(resource_budget.check("gpuSeconds"))
            self.assertIsNone(resource_budget.charge("gpuSeconds", 5))


class GpuRunEnforcementTest(unittest.TestCase):
    """资源节点侧：用尽不开跑、跑完按实测时长记账（替身 runner，不碰 Docker）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self._tmp.name) / "quota.sqlite3"
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(resource_budget.clear)
        from acps_cluster.agents import partner_gpu

        self.module = partner_gpu
        self.calls = []

    def _fake_runner(self, duration: float):
        module = self.module

        async def run_sandbox(raw):  # noqa: ANN001, ANN202
            self.calls.append(raw)
            return SimpleNamespace(
                ok=True, run_id="run-1", exit_code=0, timed_out=False,
                duration_s=duration, stdout="0.97", stderr="", artifacts=[], error=None,
            )

        return mock.patch.object(module.gpu_runner, "run_sandbox", run_sandbox)

    def test_预算用尽时不开跑(self):
        resource_budget.set_scope(resource_budget.BudgetScope(
            domain=DOMAIN, caller=CALLER, limits={"gpuSeconds": 10}, ledger=self.ledger,
        ))
        quota.record_resource(self.ledger, domain=DOMAIN, caller_aic=CALLER,
                              resource="gpuSeconds", amount=10)
        with self._fake_runner(1.0):
            out = asyncio.run(self.module.gpu_run(files={"main.py": "print(1)"}))
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "quota-exhausted")
        self.assertEqual(self.calls, [], "预算用尽时不得启动沙盒")

    def test_跑完按实测时长记账并回传回执(self):
        resource_budget.set_scope(resource_budget.BudgetScope(
            domain=DOMAIN, caller=CALLER, limits={"gpuSeconds": 100}, ledger=self.ledger,
        ))
        with self._fake_runner(30.0):
            out = asyncio.run(self.module.gpu_run(files={"main.py": "print(1)"}))
        self.assertTrue(out["ok"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(out["resourceQuota"]["amount"], 30.0)
        self.assertAlmostEqual(out["resourceQuota"]["remainingAfter"], 70.0)
        self.assertAlmostEqual(
            quota.resource_used(self.ledger, domain=DOMAIN, caller_aic=CALLER,
                                resource="gpuSeconds", since=0),
            30.0,
        )

    def test_无域上下文时不留回执(self):
        resource_budget.clear()
        with self._fake_runner(5.0):
            out = asyncio.run(self.module.gpu_run(files={"main.py": "print(1)"}))
        self.assertTrue(out["ok"])
        self.assertNotIn("resourceQuota", out)


class PepWiringTest(unittest.TestCase):
    """PEP 接线：域授权放行才把预算上下文交出去，非域/拒绝一律清空。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self._tmp.name) / "quota.sqlite3"
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(resource_budget.clear)

    def _cfg(self) -> PepConfig:
        return PepConfig(enabled=True, my_aic="1.2.156.3088.1.1.GP5060.SANDBOX1.RUN01.0IGR",
                         quota_path=self.ledger)

    def _decide(self, outcome: PepOutcome) -> None:
        with mock.patch("acps_cluster.delegation.pep.evaluate", return_value=outcome):
            pep_decide(SimpleNamespace(), self._cfg())

    def test_域授权放行时写入预算上下文(self):
        self._decide(PepOutcome(
            "allow", "domain-grant", domain=DOMAIN, domain_revision=3,
            skill_hint="gpu.run", resource_limits={"gpuSeconds": 1800},
        ))
        scope = resource_budget.current_scope()
        self.assertIsNotNone(scope)
        self.assertEqual(scope.domain, DOMAIN)
        self.assertEqual(scope.caller, "")
        self.assertEqual(scope.limits, {"gpuSeconds": 1800})
        self.assertEqual(scope.ledger, self.ledger)
        self.assertEqual(scope.revision, 3)

    def test_拒绝时清空预算上下文(self):
        resource_budget.set_scope(resource_budget.BudgetScope(domain="old", caller="x"))
        self._decide(PepOutcome("deny", "no-credential"))
        self.assertIsNone(resource_budget.current_scope())

    def test_非域放行不携带预算(self):
        resource_budget.set_scope(resource_budget.BudgetScope(domain="old", caller="x"))
        self._decide(PepOutcome("allow", "public-skill-unscoped", skill_hint="arxiv.search"))
        self.assertIsNone(resource_budget.current_scope())


if __name__ == "__main__":
    unittest.main()