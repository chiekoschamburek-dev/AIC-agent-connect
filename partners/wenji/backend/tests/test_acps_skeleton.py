"""ACPs 接入骨架本地测试：不访问网络、不依赖平台注册。

覆盖：WIT Agent 声明编译、ACS 草稿投影、能力注册表、桥接层纯函数。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("wit")

from app.acps import bridge
from app.acps.capabilities import CAPABILITIES
from app.acps.runtime import AcpsRuntime
from app.acps.settings import AcpsSettings


def _settings(tmp_path: Path, **overrides) -> AcpsSettings:
    base = dict(
        enabled=True,
        profile="custom",
        registry_url="https://registry.example.com",
        ca_url="https://ca.example.com",
        discovery_url="https://discovery.example.com/acps-adp-v2/discover",
        public_endpoint="https://agent.example.com/acps/aip/v1",
        workspace=tmp_path / "wit",
        organization="测试组织",
        delegate_enabled=True,
    )
    base.update(overrides)
    return AcpsSettings(**base)


@pytest.fixture(autouse=True)
def _dummy_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACPS_LLM_API_KEY", "test-key")
    monkeypatch.setenv("ACPS_LLM_BASE_URL", "https://llm.example.com/v1")
    monkeypatch.setenv("ACPS_LLM_MODEL", "test-model")


def test_capability_registry_ids() -> None:
    ids = [c.capability.id for c in CAPABILITIES]
    assert ids == [
        "wenji.ask",
        "wenji.data_query",
        "wenji.doc_qa",
        "wenji.formula_eval",
        "wenji.schema",
    ]


def test_declaration_version_gating(tmp_path: Path) -> None:
    """v1.0.0 = 平台已批准声明（4 技能/rpc）；v1.1.0+ = stream + 公式技能。"""
    from app.acps.agent import build_agent

    v1 = build_agent(_settings(tmp_path, version="1.0.0"))
    draft_v1 = v1.compile(acps=None) if False else None  # compile 需要 acps；改用运行时验证
    rt_v1 = AcpsRuntime(_settings(tmp_path, version="1.0.0"))
    ids_v1 = [s["id"] for s in rt_v1.acs_draft()["skills"]]
    assert "wenji.formula_eval" not in ids_v1
    assert rt_v1.acs_draft()["capabilities"]["streaming"] is False

    rt_v2 = AcpsRuntime(_settings(tmp_path, version="1.1.0"))
    ids_v2 = [s["id"] for s in rt_v2.acs_draft()["skills"]]
    assert "wenji.formula_eval" in ids_v2
    assert rt_v2.acs_draft()["capabilities"]["streaming"] is True


def test_agent_declaration_is_valid(tmp_path: Path) -> None:
    rt = AcpsRuntime(_settings(tmp_path))
    validation = asyncio.run(rt.validate())
    assert validation["valid"] is True, validation.get("diagnostics")
    assert validation["diagnostics"] == []


def test_acs_draft_projection(tmp_path: Path) -> None:
    rt = AcpsRuntime(_settings(tmp_path))
    draft = rt.acs_draft()
    assert draft["name"] == "问迹·可解释问数问答智能体"
    assert draft["provider"]["organization"] == "测试组织"
    # 默认 v1.0.0：与平台已批准声明一致（4 技能，rpc-only）
    skill_ids = [s["id"] for s in draft["skills"]]
    assert set(skill_ids) == {"wenji.ask", "wenji.data_query", "wenji.doc_qa", "wenji.schema"}
    endpoints = [e["url"] for e in draft["endPoints"]]
    assert any(url.endswith("/rpc") for url in endpoints)
    assert any("agent.example.com" in url for url in endpoints)
    assert draft["capabilities"]["streaming"] is False


def test_save_acs_writes_draft(tmp_path: Path) -> None:
    rt = AcpsRuntime(_settings(tmp_path))
    target = tmp_path / "acs.json"
    written = rt.save_acs(target)
    assert Path(written) == target
    assert target.exists()


def test_public_endpoint_falls_back_to_listener(tmp_path: Path) -> None:
    rt = AcpsRuntime(_settings(tmp_path, public_endpoint="", listen_host="0.0.0.0", listen_port=8443))
    assert rt.settings.public_endpoint_or_default() == "https://127.0.0.1:8443/acps/aip/v1"


def test_bridge_trim_and_citations() -> None:
    rows = [[i, f"r{i}"] for i in range(30)]
    assert len(bridge._trim_rows(rows)) == 20
    citations = [
        {"doc": "员工手册.pdf", "page": 3, "snippet": "x" * 300},
        {"doc": "sop.pdf", "page": 1, "snippet": "short"},
    ]
    digest = bridge._citation_digest(citations)
    assert digest[0]["doc"] == "员工手册.pdf"
    assert len(digest[0]["snippet"]) == 200


def test_task_metrics_shape() -> None:
    metrics = bridge.task_metrics()
    assert "loadMetrics" in metrics
    assert metrics["loadMetrics"]["activeTasks"] >= 0
