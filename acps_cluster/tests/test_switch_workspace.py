"""换身份工具在本地 dev 工作区上的回归：review 补齐、指纹、路径。"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

CLUSTER_DIR = Path(__file__).resolve().parents[1]
SCRIPT = CLUSTER_DIR / "deploy" / "discovery_diagnostics" / "switch_step4_apply_identity.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("switch_step4_apply_identity", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class SwitchStep4Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_module()

    def test_本地dev空review会被补齐(self):
        enrollment = {"review": {}}
        acs = {"active": True, "aic": "new-aic", "skills": []}
        out = self.mod.build_platform_review(
            enrollment, aic="new-aic", acs=acs, registration_id="rid-1"
        )
        self.assertTrue(out["review"]["metadata"]["active"])
        self.assertEqual(out["review"]["metadata"]["approvalStatus"], "APPROVED")
        self.assertEqual(out["review"]["status"], "approved")
        self.assertEqual(out["review"]["aic"], "new-aic")
        self.assertEqual(out["review"]["authoritativeAcs"], acs)
        self.assertEqual(out["review"]["registrationId"], "rid-1")
        self.assertEqual(out["registrationId"], "rid-1")  # provisioner 读顶层
        self.assertEqual(out["status"], "active")
        self.assertEqual(out["finalAcsSource"], "registry")

    def test_inactive记录标failed(self):
        out = self.mod.build_platform_review(
            {"review": {}}, aic="a", acs={"active": False}
        )
        self.assertFalse(out["review"]["metadata"]["active"])
        self.assertEqual(out["status"], "failed")

    def test_apply_identity写全路径与指纹(self):
        with tempfile.TemporaryDirectory(prefix="ws-idswitch-") as td:
            ws = Path(td)
            (ws / "acps").mkdir()
            enrollment = {
                "identity": {"ref": {"aic": "old-aic"}},
                "review": {},
                "registration": {"kind": "integrated"},
                "failure": "acs_fingerprint_changed_after_submission",
                "candidateDraftPath": "/tmp/old.json",
            }
            out = self.mod.apply_platform_identity(
                enrollment,
                ws=ws,
                aic="new-aic",
                acs={"active": True, "aic": "new-aic", "skills": []},
                acps_config={"certificateKeyAlgorithm": "ed25519",
                             "publicEndpoint": "https://example.test:8470/rpc"},
                acps_config_fingerprint="cfg-fp",
                acs_fingerprint="acs-fp",
                registration_id="rid-2",
            )
        self.assertEqual(out["identity"]["ref"]["aic"], "new-aic")
        self.assertEqual(out["identity"]["ref"]["provenance"], "registry+acme")
        self.assertEqual(out["identity"]["metadata"]["certificateKeyAlgorithm"], "ed25519")
        self.assertEqual(out["acpsConfigFingerprint"], "cfg-fp")
        self.assertEqual(out["acsFingerprint"], "acs-fp")
        self.assertEqual(out["review"]["registrationId"], "rid-2")
        self.assertEqual(out["registrationId"], "rid-2")
        self.assertNotIn("failure", out)
        self.assertNotIn("candidateDraftPath", out)
        cert_path = Path(out["identity"]["certificatePaths"]["clientCertificate"])
        self.assertEqual(cert_path.name, "certificate.pem")
        self.assertEqual(cert_path.parent.name, "client")


if __name__ == "__main__":
    unittest.main()
