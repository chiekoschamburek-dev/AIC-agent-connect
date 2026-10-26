"""第三方算力 grant 文件读取（不经过研枢 leader 签名）。"""

import json
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

CLUSTER = Path(__file__).resolve().parents[1]
for _p in (str(CLUSTER.parent), str(CLUSTER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from acps_cluster.agents import partner_experiment  # noqa: E402


class ExternalGrantTest(unittest.TestCase):
    def test_读取provider签发的grant文件(self):
        tmp = Path(tempfile.mkdtemp()) / "grant.json"
        tmp.write_text(json.dumps({
            "token": "JWS-FROM-PROVIDER",
            "targetAic": "AIC-GPU-PROVIDER",
            "skill": "gpu.run",
        }), encoding="utf-8")
        with mock.patch.dict(os.environ, {"ACPS_COMPUTE_GRANT_FILE": str(tmp)}):
            got = partner_experiment._external_compute_grant()
        self.assertEqual(got["token"], "JWS-FROM-PROVIDER")
        self.assertEqual(got["targetAic"], "AIC-GPU-PROVIDER")
        self.assertEqual(got["skill"], "gpu.run")

    def test_未配置时不读取(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ACPS_COMPUTE_GRANT_FILE", None)
            self.assertEqual(partner_experiment._external_compute_grant(), {})

    def test_损坏文件返回错误而不是崩溃(self):
        tmp = Path(tempfile.mkdtemp()) / "grant.json"
        tmp.write_text("{not-json", encoding="utf-8")
        with mock.patch.dict(os.environ, {"ACPS_COMPUTE_GRANT_FILE": str(tmp)}):
            got = partner_experiment._external_compute_grant()
        self.assertIn("error", got)
        self.assertEqual(got.get("token"), "")

    def test_exp_run优先使用provider_grant而不是leader自签(self):
        tmp = Path(tempfile.mkdtemp()) / "grant.json"
        tmp.write_text(json.dumps({
            "token": "JWS-FROM-PROVIDER",
            "targetAic": "AIC-GPU-PROVIDER",
            "skill": "gpu.run",
        }), encoding="utf-8")
        captured = {}

        async def fake_pipeline(runtime, state, **kwargs):
            captured.update(kwargs)
            return {"ok": True}

        with mock.patch.dict(os.environ, {"ACPS_COMPUTE_GRANT_FILE": str(tmp)}), \
                mock.patch.object(partner_experiment, "experiment_pipeline", fake_pipeline), \
                mock.patch.object(partner_experiment, "current_runtime", return_value=object()), \
                mock.patch.object(partner_experiment, "current_state", return_value=object()), \
                mock.patch.object(partner_experiment, "current_delegation", return_value={}):
            out = asyncio.run(partner_experiment.exp_run("做一次实验"))
        self.assertTrue(out.get("ok"))
        self.assertEqual(captured.get("token"), "JWS-FROM-PROVIDER")
        self.assertEqual(captured.get("compute_aic"), "AIC-GPU-PROVIDER")


if __name__ == "__main__":
    unittest.main()
