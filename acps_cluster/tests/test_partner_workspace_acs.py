"""ACS 中性守卫：给七个研究伙伴加工作区客户端工具不得改变其 ACS 投影。

`ws.list/ws.read/ws.write` 全部 `acps_skill=False`，只作为模型工具存在；
本测试对比 `ACPS_WS_PARTNER_ACCESS` 开/关两种构建的 acs_draft，逐字一致。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

CL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CL))
sys.path.insert(0, str(CL.parent))

from acps_cluster.agents import partner_contradiction  # noqa: E402
from acps_cluster.agents import partner_critic  # noqa: E402
from acps_cluster.agents import partner_experiment  # noqa: E402
from acps_cluster.agents import partner_frontier  # noqa: E402
from acps_cluster.agents import partner_scout  # noqa: E402
from acps_cluster.agents import partner_synthesis  # noqa: E402
from acps_cluster.agents import partner_writer  # noqa: E402
from wit import (  # noqa: E402
    AcpsConfig,
    AgentCertificateRequest,
    AgentProviderProfile,
    WitRuntime,
)

ALL_KEYS = "scout,experiment,synthesis,writer,critic,contradiction,frontier"
PARTNERS = [
    ("scout", partner_scout.build_scout_agent),
    ("experiment", partner_experiment.build_experiment_agent),
    ("synthesis", partner_synthesis.build_synthesis_agent),
    ("writer", partner_writer.build_writer_agent),
    ("critic", partner_critic.build_critic_agent),
    ("contradiction", partner_contradiction.build_contradiction_agent),
    ("frontier", partner_frontier.build_frontier_agent),
]


def _cfg() -> AcpsConfig:
    return AcpsConfig.custom(
        registry_url="https://wt.ioa.pub/registry-server",
        ca_url="https://wt.ioa.pub/ca-server",
        discovery_url="https://wt.ioa.pub/discovery/acps-adp-v2/discover",
        public_endpoint="https://127.0.0.1:8443/rpc",
        certificate=AgentCertificateRequest(ip=("127.0.0.1",)),
        provider=AgentProviderProfile(
            name="wit-research-lab", organization="北京邮电大学",
            email="huaibin2025@outlook.com", country_code="CN"),
        strict_interop=True,
    )


def _draft(build) -> dict:
    with tempfile.TemporaryDirectory(prefix="ws-acs-neutral-") as td:
        runtime = WitRuntime(build(None), workspace=td, acps=_cfg())
        return runtime.acs_draft()


def _stable(value: dict) -> dict:
    value = json.loads(json.dumps(value, ensure_ascii=False))
    for key in ("aic", "active", "lastModifiedTime"):
        value.pop(key, None)
    return value


class PartnerWorkspaceAcsNeutralityTest(unittest.TestCase):
    def test_开关不改变ACS且不含ws技能(self):
        for key, build in PARTNERS:
            with self.subTest(partner=key):
                with mock.patch.dict(os.environ, {"ACPS_WS_PARTNER_ACCESS": ""}):
                    off = _draft(build)
                with mock.patch.dict(os.environ, {"ACPS_WS_PARTNER_ACCESS": ALL_KEYS}):
                    on = _draft(build)
                self.assertEqual(_stable(off), _stable(on),
                                 f"{key}: 工作区工具不应改变 ACS 投影")
                skill_ids = {s.get("id") for s in (on.get("skills") or [])}
                self.assertFalse(skill_ids & {"ws.list", "ws.read", "ws.write"},
                                 f"{key}: ws.* 不应出现在 ACS skills 里")


WS_AIC = "1.2.156.3088.1.BUPT.VTF97R.N9M0PW.ZB1CII.0B3X"


def _ws_acs(aic: str = WS_AIC) -> dict:
    return {
        "aic": aic,
        "name": "研枢-Workspace-Partner",
        "description": "测试用 workspace ACS",
        "version": "1.1.0",
        "active": True,
        "lastModifiedTime": "2026-10-26T00:00:00Z",
        "protocolVersion": "1.0",
        "provider": {"name": "wit-research-lab", "organization": "BUPT"},
        "securitySchemes": {},
        "endPoints": [{"url": "https://127.0.0.1:8470/rpc", "transport": "JSONRPC",
                       "security": [{"mtls": []}]}],
        "capabilities": {},
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": [{"id": "ws.create", "name": "ws.create", "description": "d",
                    "version": "1.1.0", "tags": []}],
        "certificate": {},
    }


class WorkspacePeerSeedTest(unittest.TestCase):
    """伙伴启动时要把 workspace 对端预热进自己的 discovery 缓存。

    2026-10-26 e2e：伙伴的 acps/discovered 是空的（只有 supervisor 跑 seed），
    ws_* 工具全部失败在 "Partner has no discovered Direct JSON-RPC endpoint"。
    """

    def test_合成ACS写进discovered(self):
        from acps_cluster.core import cluster_common
        from wit.acps.discovery import load_discovered_agent

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            acs_dir = root / "ws-workspace" / "acps"
            acs_dir.mkdir(parents=True)
            (acs_dir / "acs.final.json").write_text(
                json.dumps(_ws_acs(), ensure_ascii=False), encoding="utf-8")
            scout = root / "ws-scout"
            scout.mkdir()

            cluster_common.seed_workspace_peer(scout)

            cache = scout / "acps" / "discovered"
            self.assertTrue(list(cache.glob("*")), "未写出 discovery 缓存")
            record = load_discovered_agent(cache, WS_AIC)
            self.assertIsNotNone(record)
            raw = body = json.loads(json.dumps(record, ensure_ascii=False))
            self.assertEqual(raw.get("rawAcs", {}).get("aic"), WS_AIC)
            summary = body.get("normalizedSummary") or {}
            self.assertEqual(summary.get("directEndpointUrl"), "https://127.0.0.1:8470/rpc")

    def test_没有workspace目录时静默跳过(self):
        from acps_cluster.core import cluster_common

        with tempfile.TemporaryDirectory() as td:
            scout = Path(td) / "ws-scout"
            scout.mkdir()
            cluster_common.seed_workspace_peer(scout)   # 不应抛异常
            self.assertFalse((scout / "acps" / "discovered").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
