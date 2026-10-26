"""离线单测：本地模式下终稿 ACS 的维护（cluster_common.ensure_local_acs）。

背景：本地铸造（dev CA）的身份没有注册表回填，`acs.final.json` 要等首次启动才生成；
生成后若代码改了能力，旧实现只看"文件在不在"就再也不更新 → 名册看到的能力陈旧。

    python -m unittest acps_cluster.tests.test_local_acs -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

CLUSTER_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = CLUSTER_DIR.parent
for _p in (str(PROJECT_DIR), str(CLUSTER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from wit import (  # noqa: E402
    AcpsConfig,
    AcpsDirect,
    AgentCertificateRequest,
    AgentProviderProfile,
    WitAgent,
    WitRuntime,
    capability,
)
from wit.acps.provisioner import _fingerprint, _safe_acps_config  # noqa: E402

import cluster_common  # noqa: E402

ACPS = AcpsConfig.custom(
    registry_url="https://localhost:9001",
    ca_url="https://localhost:9003",
    discovery_url="https://localhost:9005",
    public_endpoint="https://localhost:8460/rpc",
    certificate=AgentCertificateRequest(dns=("localhost",)),
    provider=AgentProviderProfile(name="p", organization="o", email="e@e.com", country_code="CN"),
    strict_interop=True,
)


@capability(id="probe.one", description="one", public=True)
async def probe_one(query: str) -> str:
    return query


@capability(id="probe.two", description="two", public=True)
async def probe_two(query: str) -> str:
    return query


def _agent(caps):
    return WitAgent(
        id="probe.agent",
        display_name="probe",
        version="0.0.1",
        description="probe",
        instructions="probe",
        model=None,
        capabilities=list(caps),
        protocols=[AcpsDirect()],
    )


class EnsureLocalAcsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name)
        (self.ws / "acps").mkdir(parents=True)
        (self.ws / "acps" / "enrollment.json").write_text(
            json.dumps(
                {
                    "agentId": "probe.agent",
                    "status": "active",
                    # 模拟"从模板抄来的"注册痕迹（早期铸造脚本的问题）
                    "registrationId": "01a0ce39-7397-725b-8c2c-8f1e873f782d",
                    "review": {"aic": "SCOUT-AIC"},
                    "acsFingerprint": "8037a71ee2ce4d7f",
                    "acpsConfigFingerprint": "279ccc88fa4b7119",
                    "finalAcsSource": "registry",
                    "identity": {"ref": {"aic": "1.2.156.3088.1.1.PROBE.SANDBOX1.RUN01.0000"}},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def _runtime(self, caps):
        return WitRuntime(_agent(caps), workspace=self.ws, acps=ACPS)

    def _final(self):
        return json.loads((self.ws / "acps" / "acs.final.json").read_text(encoding="utf-8"))

    def _enrollment(self):
        return json.loads((self.ws / "acps" / "enrollment.json").read_text(encoding="utf-8"))

    def test_终稿缺失时生成并写入aic与能力(self):
        aic = cluster_common.ensure_local_acs(self._runtime([probe_one]), self.ws)
        self.assertEqual(aic, "1.2.156.3088.1.1.PROBE.SANDBOX1.RUN01.0000")
        final = self._final()
        self.assertEqual(final["aic"], aic)
        self.assertEqual([s["id"] for s in final["skills"]], ["probe.one"])

    def test_skills变化时重新生成(self):
        cluster_common.ensure_local_acs(self._runtime([probe_one]), self.ws)
        self.assertEqual([s["id"] for s in self._final()["skills"]], ["probe.one"])
        # 代码加了能力 → 必须重生成（旧实现只看文件在不在，会漏掉）
        cluster_common.ensure_local_acs(self._runtime([probe_one, probe_two]), self.ws)
        self.assertEqual([s["id"] for s in self._final()["skills"]], ["probe.one", "probe.two"])

    def test_skills未变时不重写(self):
        cluster_common.ensure_local_acs(self._runtime([probe_one]), self.ws)
        before = (self.ws / "acps" / "acs.final.json").read_text(encoding="utf-8")
        cluster_common.ensure_local_acs(self._runtime([probe_one]), self.ws)
        self.assertEqual((self.ws / "acps" / "acs.final.json").read_text(encoding="utf-8"), before)

    def test_回填配置指纹并清掉模板痕迹(self):
        cluster_common.ensure_local_acs(self._runtime([probe_one]), self.ws)
        enrollment = self._enrollment()
        # 配置指纹可精确复现 wit 的算法
        self.assertEqual(enrollment["acpsConfigFingerprint"], _fingerprint(_safe_acps_config(ACPS)))
        # ACS 指纹置 null（复现不出基线，写错值比 null 危险）
        self.assertIsNone(enrollment["acsFingerprint"])
        self.assertEqual(enrollment["finalAcsSource"], "local-dev-ca")
        # 身份与状态保持不动
        self.assertEqual(enrollment["identity"]["ref"]["aic"], "1.2.156.3088.1.1.PROBE.SANDBOX1.RUN01.0000")
        self.assertEqual(enrollment["status"], "active")

    def test_幂等_重复调用结果一致(self):
        cluster_common.ensure_local_acs(self._runtime([probe_one]), self.ws)
        first = self._enrollment()
        cluster_common.ensure_local_acs(self._runtime([probe_one]), self.ws)
        self.assertEqual(self._enrollment()["acpsConfigFingerprint"], first["acpsConfigFingerprint"])

    def test_没有enrollment时不炸(self):
        (self.ws / "acps" / "enrollment.json").unlink()
        self.assertIsNone(cluster_common.ensure_local_acs(self._runtime([probe_one]), self.ws))
        self.assertEqual([s["id"] for s in self._final()["skills"]], ["probe.one"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
