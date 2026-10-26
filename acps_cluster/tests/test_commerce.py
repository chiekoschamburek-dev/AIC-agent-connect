"""商业域（commerce.py）离线单测：付费计划校验与发卡规则。

    python -m unittest acps_cluster.tests.test_commerce -v
"""

import sys
import tempfile
import unittest
from pathlib import Path

CLUSTER_DIR = Path(__file__).resolve().parents[1]
for _p in (str(CLUSTER_DIR.parent), str(CLUSTER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from acps_cluster.delegation.commerce import (  # noqa: E402
    PaidPlan, issue_paid_grant, load_paid_plans, write_plan_template,
)
from acps_cluster.delegation.token import load_signing_context  # noqa: E402

GPU_WS = CLUSTER_DIR / "ws-gpu"
TRUST = GPU_WS / "acps" / "trust-bundle.pem"
EXP_AIC = "1.2.156.3088.1.1.EXPRMT.LABEXE01.RUN01.1207"
CRITIC_AIC = "1.2.156.3088.1.1.BJ5X2N.QYTBJJ.6GH1CI.12KL"
GPU_AIC = "1.2.156.3088.1.1.GP5060.SANDBOX1.RUN01.0IGR"

_DEV_KEYS = (GPU_WS / "acps" / "client" / "private-key.pem").is_file()
_SKIP_REASON = "需要本地 dev CA 私钥（ws-gpu）；干净克隆不携带"


@unittest.skipUnless(_DEV_KEYS, _SKIP_REASON)
class CommerceTest(unittest.TestCase):
    def setUp(self):
        self.signing = load_signing_context(GPU_WS / "acps" / "client", trust_bundle=TRUST)
        self.plans = {EXP_AIC: PaidPlan(EXP_AIC, "gpu-starter", 3, "2099-01-01T00:00:00Z", "gpu.run")}

    def test_有计划发卡(self):
        grant, deny = issue_paid_grant(self.signing, self.plans, customer_aic=EXP_AIC,
                                       domain="gpu-shop", domain_revision=1, provider_aic=GPU_AIC)
        self.assertIsNone(deny)
        self.assertEqual(grant.claims["acps_max_uses"], 3)
        self.assertEqual(grant.claims["acps_grant_plan"], "gpu-starter")
        self.assertEqual(grant.claims["acps_domain"], "gpu-shop")

    def test_无计划拒发(self):
        grant, deny = issue_paid_grant(self.signing, self.plans, customer_aic=CRITIC_AIC,
                                       domain="gpu-shop", domain_revision=1, provider_aic=GPU_AIC)
        self.assertIsNone(grant)
        self.assertIn("no-paid-plan", deny)

    def test_过期计划拒发(self):
        expired = {EXP_AIC: PaidPlan(EXP_AIC, "old", 3, "2020-01-01T00:00:00Z", "gpu.run")}
        grant, deny = issue_paid_grant(self.signing, expired, customer_aic=EXP_AIC,
                                       domain="gpu-shop", domain_revision=1, provider_aic=GPU_AIC)
        self.assertIsNone(grant)
        self.assertIn("plan-expired", deny)

    def test_发卡日限额_第N加1张拒发(self):
        import tempfile

        from acps_cluster.delegation.commerce import issue_paid_grant as issue

        with tempfile.TemporaryDirectory() as td:
            quota = Path(td) / "issuance.sqlite3"
            results = []
            for _ in range(4):  # 限额 3：前 3 张放行，第 4 张拒
                grant, deny = issue(self.signing, self.plans, customer_aic=EXP_AIC,
                                    domain="gpu-shop", domain_revision=1, provider_aic=GPU_AIC,
                                    max_grants_per_day=3, quota_path=quota)
                results.append(grant is not None)
            self.assertEqual(results, [True, True, True, False])

    def test_滑动窗口_过期后恢复发卡(self):
        import time as _time
        import tempfile

        from acps_cluster.delegation.quota import record_issue

        with tempfile.TemporaryDirectory() as td:
            quota = Path(td) / "issuance.sqlite3"
            now = _time.time()
            # 25 小时前发过 3 张（已滑出窗口）→ 现在仍可发
            for i in range(3):
                record_issue(quota, domain="gpu-shop", issuer_aic=GPU_AIC,
                             jti=f"old-{i}", now=now - 25 * 3600)
            grant, deny = issue_paid_grant(self.signing, self.plans, customer_aic=EXP_AIC,
                                           domain="gpu-shop", domain_revision=1, provider_aic=GPU_AIC,
                                           max_grants_per_day=3, quota_path=quota, now=now)
            self.assertIsNotNone(grant, deny)

    def test_发卡账本重启持久(self):
        import tempfile

        from acps_cluster.delegation.quota import issues_in_window

        with tempfile.TemporaryDirectory() as td:
            quota = Path(td) / "issuance.sqlite3"
            for i in range(3):
                issue_paid_grant(self.signing, self.plans, customer_aic=EXP_AIC,
                                 domain="gpu-shop", domain_revision=1, provider_aic=GPU_AIC,
                                 max_grants_per_day=5, quota_path=quota)
            self.assertEqual(issues_in_window(quota, domain="gpu-shop", issuer_aic=GPU_AIC), 3)

    def test_模板与加载往返(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_plan_template(Path(td) / "paid_plans.json")
            plans = load_paid_plans(path)
            self.assertIn(EXP_AIC, plans)
            self.assertEqual(plans[EXP_AIC].uses, 3)
            self.assertEqual(load_paid_plans(Path(td) / "nope.json"), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
