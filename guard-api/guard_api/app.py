"""ACPS Event Channel Guard 的 HTTP 接口(FastAPI 应用工厂)。

定位:只读旁路。所有端点只调用检测器的只读入口(SQLite ``mode=ro``),
本服务自身不写任务库、不落盘报告文件;处置边界与 guard_adapter 一致
(``disposition="advisory-only"``)。

用法::

    from guard_api.app import create_app
    app = create_app(db="/path/to/aip-tasks.sqlite3", port_hints=...)

独立运行用 ``python -m guard_api``;进程内挂载用 ``guard_api.mount.start_guard_api``。
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from guard_api import report
from guard_api._version import __version__
from guard_api import covert_event_guard as guard

SERVICE_NAME = "acps-guard-api"

# 单次扫描窗口上限:防止一个请求把超大任务库全部拉进内存。
_MAX_LIMIT = 200_000


class ScanParams(BaseModel):
    """POST /api/guard/scan 的可选请求体(字段与 GET 查询参数一致)。"""

    limit: int | None = Field(default=None, ge=1, le=_MAX_LIMIT)
    min_level: str | None = Field(default=None, pattern="^(LOW|MEDIUM|HIGH)$")
    session_gap: float | None = Field(default=None, gt=0)


class _BearerGuard:
    """可选 Bearer Token 校验:未配置 token 时放行,配置后强制校验。"""

    def __init__(self, token: str | None) -> None:
        self._token = token

    # ⚠️ 不要写成 `Annotated[str | None, Header()]`：本模块启用了
    # `from __future__ import annotations`，FastAPI 会把该参数判定成 query，
    # 导致请求头永远读不到——配置 token 后连正确 token 也会 401（仓库侧修复）。
    async def __call__(self, authorization: str | None = Header(default=None)) -> None:
        if self._token is None:
            return
        if not authorization:
            raise HTTPException(
                status_code=401,
                detail={"code": "unauthorized", "message": "missing Authorization header"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(value.strip(), self._token):
            raise HTTPException(
                status_code=403,
                detail={"code": "forbidden", "message": "invalid bearer token"},
            )


def _db_error(exc: Exception) -> HTTPException:
    """把数据库侧异常映射为 503(服务端数据问题),detail 采用 {code, message}。"""
    if isinstance(exc, FileNotFoundError):
        code = "db_not_found"
    elif isinstance(exc, ValueError):
        code = "unsupported_db"
    else:
        code = "db_error"
    return HTTPException(status_code=503, detail={"code": code, "message": str(exc)})


def create_app(
    db: str | None = None,
    *,
    limit: int = 1000,
    min_level: str = "MEDIUM",
    session_gap: float = 3600.0,
    token: str | None = None,
) -> FastAPI:
    """构造 guard API 应用。

    db 只在启动时确定,不接受请求传入(避免任意本地文件读取);
    limit/min_level/session_gap 是默认值,单次请求可覆盖。
    """
    if min_level not in report.MIN_LEVELS:
        raise ValueError(f"min_level must be one of {report.MIN_LEVELS}, got {min_level!r}")

    app = FastAPI(
        title="ACPS Event Channel Guard API",
        version=__version__,
        description=(
            "只读旁路安全审计接口:从 Storage / Timing / Behavioral 三个维度检测 "
            "AIPs 任务事件中的异常通信模式。报告均为 advisory-only,不构成攻击定论,"
            "不自动处置。"
        ),
    )
    app.state.db = db
    app.state.limit = int(limit)
    app.state.min_level = min_level
    app.state.session_gap = float(session_gap)
    auth = _BearerGuard(token)

    # ---- handlers -------------------------------------------------------

    def _effective(params_limit: int | None, params_min_level: str | None,
                   params_session_gap: float | None) -> tuple[int, str, float]:
        return (
            params_limit if params_limit is not None else app.state.limit,
            params_min_level if params_min_level is not None else app.state.min_level,
            params_session_gap if params_session_gap is not None else app.state.session_gap,
        )

    def _require_db() -> str:
        if not app.state.db:
            raise HTTPException(
                status_code=503,
                detail={"code": "db_not_configured",
                        "message": "no task db configured at startup (--db / ACPS_GUARD_DB)"},
            )
        return app.state.db

    @app.get("/health", tags=["meta"], dependencies=[Depends(auth)])
    def health() -> dict[str, Any]:
        db_path = app.state.db
        return {
            "status": "ok",
            "service": SERVICE_NAME,
            "version": __version__,
            "db": str(db_path) if db_path else None,
            "db_exists": bool(db_path) and Path(db_path).is_file(),
            "disposition": "advisory-only",
        }

    @app.get("/api/guard/scan", tags=["guard"], dependencies=[Depends(auth)],
             summary="全量扫描:单任务三维检测 + peer timing + 告警汇总")
    def scan_get(
        limit: Annotated[int | None, Query(ge=1, le=_MAX_LIMIT)] = None,
        min_level: Annotated[str | None, Query(pattern="^(LOW|MEDIUM|HIGH)$")] = None,
        session_gap: Annotated[float | None, Query(gt=0)] = None,
    ) -> dict[str, Any]:
        return _scan(limit, min_level, session_gap)

    @app.post("/api/guard/scan", tags=["guard"], dependencies=[Depends(auth)],
              summary="全量扫描(请求体传参,等价于 GET)")
    def scan_post(body: ScanParams | None = None) -> dict[str, Any]:
        body = body or ScanParams()
        return _scan(body.limit, body.min_level, body.session_gap)

    @app.get("/api/guard/tasks/{task_ref}", tags=["guard"], dependencies=[Depends(auth)],
             summary="单任务三维检测")
    def task_report(
        task_ref: str,
        limit: Annotated[int | None, Query(ge=1, le=_MAX_LIMIT)] = None,
    ) -> dict[str, Any]:
        db_path = _require_db()
        effective_limit, _, _ = _effective(limit, None, None)
        try:
            events = guard.load_sqlite(db_path, task_ref=task_ref, limit=effective_limit)
        except (FileNotFoundError, ValueError, sqlite3.Error) as exc:
            raise _db_error(exc) from exc
        if not events:
            raise HTTPException(
                status_code=404,
                detail={"code": "task_not_found",
                        "message": f"no events for task_ref={task_ref!r} within limit={effective_limit}"},
            )
        try:
            result = guard.analyze_events(events)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail={"code": "bad_events", "message": str(exc)}
            ) from exc
        return result[0].to_dict()

    @app.get("/api/guard/peer-timing", tags=["guard"], dependencies=[Depends(auth)],
             summary="跨任务 Timing 检测(按 direction/local_aic/remote_aic 聚合)")
    def peer_timing(
        limit: Annotated[int | None, Query(ge=1, le=_MAX_LIMIT)] = None,
        session_gap: Annotated[float | None, Query(gt=0)] = None,
    ) -> list[dict[str, Any]]:
        db_path = _require_db()
        effective_limit, _, effective_gap = _effective(limit, None, session_gap)
        try:
            peer_tasks = guard.load_peer_tasks_sqlite(db_path, limit=effective_limit)
            reports = guard.analyze_peer_timing(peer_tasks, session_gap_seconds=effective_gap)
        except (FileNotFoundError, ValueError, sqlite3.Error) as exc:
            raise _db_error(exc) from exc
        return [item.to_dict() for item in reports]

    @app.post("/api/guard/events", tags=["guard"], dependencies=[Depends(auth)],
              summary="内联事件分析(无需数据库):接受 JSON 数组或 JSONL 文本")
    async def analyze_inline(request: Request) -> list[dict[str, Any]]:
        raw = await request.body()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(
                status_code=400, detail={"code": "invalid_encoding", "message": "body must be UTF-8"}
            ) from exc
        rows = _parse_inline_events(text)
        try:
            events = [guard.event_from_mapping(row, index) for index, row in enumerate(rows, 1)]
            reports = guard.analyze_events(events)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail={"code": "bad_events", "message": str(exc)}
            ) from exc
        return [item.to_dict() for item in reports]

    # ---- shared logic ----------------------------------------------------

    def _scan(limit_override: int | None, min_level_override: str | None,
              session_gap_override: float | None) -> dict[str, Any]:
        db_path = _require_db()
        effective_limit, effective_min_level, effective_gap = _effective(
            limit_override, min_level_override, session_gap_override)
        try:
            result = report.scan(db_path, limit=effective_limit,
                                 min_level=effective_min_level,
                                 session_gap_seconds=effective_gap)
        except (FileNotFoundError, ValueError, sqlite3.Error) as exc:
            raise _db_error(exc) from exc
        return result.to_dict()

    return app


def _parse_inline_events(text: str) -> list[dict[str, Any]]:
    """请求体解析:优先整段 JSON(数组/单对象),否则按 JSONL 逐行解析。"""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if parsed is not None:
        if isinstance(parsed, dict):
            return [parsed]
        if isinstance(parsed, list) and all(isinstance(item, dict) for item in parsed):
            if not parsed:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "empty_body", "message": "no events provided"},
                )
            return parsed
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_body",
                    "message": "JSON body must be an object or an array of objects"},
        )
    rows: list[dict[str, Any]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_jsonl", "message": f"invalid JSON at line {lineno}: {exc}"},
            ) from exc
        if not isinstance(item, dict):
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_jsonl", "message": f"line {lineno} is not a JSON object"},
            )
        rows.append(item)
    if not rows:
        raise HTTPException(
            status_code=400,
            detail={"code": "empty_body", "message": "no events provided"},
        )
    return rows
