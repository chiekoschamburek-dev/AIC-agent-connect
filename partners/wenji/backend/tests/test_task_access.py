"""TaskAccessHooks 单元测试：任务登记、稳定态发录、去重、会话隔离（独立 journal 版）。"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("wit")

from app.acps import task_access
from app.acps.task_access import RUNTIME_HOLDER, TaskAccessHooks, _TASK_T0

AIC = "1.2.156.3088.1.BUPT.TESTTEST.TESTTEST.TESTTEST.TEST"


@dataclass
class _Command:
    value: str = "start"


@dataclass
class _State:
    value: str = "awaiting-completion"


@dataclass
class _RequestEvent:
    task_id: str = "task-test-0001"
    command: Any = field(default_factory=_Command)
    session_id: str = "session-x"
    leader_aic: str = "1.2.156...LEADER"
    partner_aic: str = AIC


@dataclass
class _ResponseEvent:
    task_id: str = "task-test-0001"
    state: Any = field(default_factory=_State)
    session_id: str = "session-x"
    leader_aic: str = "1.2.156...LEADER"
    partner_aic: str = AIC
    products: list = field(default_factory=list)


class _FakeRuntime:
    def __init__(self, workspace: Path) -> None:
        self.settings = type("S", (), {"workspace": workspace, "agent_id": "wenji-aip-agent"})()


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _TASK_T0.clear()
    task_access._EMITTER = None
    task_access._EMITTER_PATH = None
    ws = tmp_path / "ws"
    (ws / "acps").mkdir(parents=True)
    (ws / "acps" / "acs.final.json").write_text(json.dumps({"aic": AIC}), encoding="utf-8")
    RUNTIME_HOLDER.set(_FakeRuntime(ws))
    monkeypatch.delenv("ACPS_TASK_ACCESS_ENABLED", raising=False)
    yield ws
    RUNTIME_HOLDER.set(None)
    _TASK_T0.clear()
    task_access._EMITTER = None
    task_access._EMITTER_PATH = None


def _lines(ws: Path) -> list[dict]:
    path = ws / "logs" / f"amp_access_partner_{AIC}.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_request_registers_task_and_session(workspace: Path) -> None:
    hooks = TaskAccessHooks()

    async def scenario() -> None:
        await hooks.on_partner_request(_RequestEvent())
        from app.acps.task_access import CURRENT_TASK_SESSION

        assert CURRENT_TASK_SESSION.get() == "aip-task-test-0001"
        assert "task-test-0001" in _TASK_T0

    asyncio.run(scenario())


def test_working_response_defers_emission(workspace: Path) -> None:
    hooks = TaskAccessHooks()
    asyncio.run(hooks.on_partner_request(_RequestEvent()))
    asyncio.run(hooks.on_partner_response(_ResponseEvent(state=_State("working"))))
    assert _lines(workspace) == []
    assert "task-test-0001" in _TASK_T0


def test_stable_response_writes_one_record_with_task_correlation(workspace: Path) -> None:
    hooks = TaskAccessHooks()
    asyncio.run(hooks.on_partner_request(_RequestEvent()))
    time.sleep(0.01)
    asyncio.run(hooks.on_partner_response(_ResponseEvent()))
    lines = _lines(workspace)
    assert len(lines) == 1
    rec = lines[0]
    assert rec["correlation_id"] == "task-test-0001"
    assert rec["aic"] == AIC
    body = rec["body"]
    assert body["response"]["statusCode"] == 200
    assert body["durationMs"] >= 0
    assert body["caller"]["aic"] == "1.2.156...LEADER"
    assert body["callee"]["aic"] == AIC
    assert rec.get("log_id")

    # 去重：同任务后续响应不再写
    asyncio.run(hooks.on_partner_response(_ResponseEvent(state=_State("completed"))))
    assert len(_lines(workspace)) == 1
    assert "task-test-0001" not in _TASK_T0


def test_failed_state_maps_to_500(workspace: Path) -> None:
    hooks = TaskAccessHooks()
    asyncio.run(hooks.on_partner_request(_RequestEvent()))
    asyncio.run(hooks.on_partner_response(_ResponseEvent(state=_State("failed"))))
    assert _lines(workspace)[0]["body"]["response"]["statusCode"] == 500


def test_continue_request_keeps_session_but_not_retimed(workspace: Path) -> None:
    hooks = TaskAccessHooks()

    async def scenario() -> None:
        await hooks.on_partner_request(_RequestEvent())
        await hooks.on_partner_request(_RequestEvent(command=_Command("continue")))
        from app.acps.task_access import CURRENT_TASK_SESSION

        assert CURRENT_TASK_SESSION.get() == "aip-task-test-0001"
        assert len(_TASK_T0) == 1

    asyncio.run(scenario())


def test_missing_aic_never_raises(workspace: Path) -> None:
    (workspace / "acps" / "acs.final.json").unlink()
    hooks = TaskAccessHooks()
    asyncio.run(hooks.on_partner_request(_RequestEvent()))
    asyncio.run(hooks.on_partner_response(_ResponseEvent()))  # 不抛异常即通过
    assert _lines(workspace) == []
