"""从本机任务库提取"一个方向的平台任务"回执（只读）。

数据源自 wit 运行时落盘的 `ws-*/acps/aip-tasks.sqlite3`：

- `aip_tasks`：任务的**当前**快照（`task_ref` / `direction` / `state` / `local_aic` /
  `remote_aic` / `created_at` / `updated_at` / `products` / `local_error`）；
- `aip_task_events`：任务事件流（`event_data` 或 `payload` 里带 `state`），用来还原
  状态时间线（accepted → working → awaiting-completion → completed）。

设计约束：

- **只读**：`mode=ro`，不写库、不改状态；表/列名全部经 `PRAGMA table_info` 探测，
  缺列/缺表按降级处理（早期任务库没有 `aip_tasks`，或没有 `remote_aic`）；
- **不猜**：取不到的值留空串，证据包里显示"未记录"，不补造；
- **不回传正文**：产物只取数量 / 字符数 / sha256（见 `models.ProductSummary`）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import ProductSummary, TaskReceipt, is_terminal, normalize_state

DB_RELATIVE = Path("acps") / "aip-tasks.sqlite3"

#: 选"最佳回执"时的优先级：completed 最硬，其次是其它终态，最后才是 awaiting-completion
_STATE_PRIORITY = {"completed": 0}
_DEFAULT_PRIORITY = 1
_UNCLOSED_PRIORITY = 2


def find_db(workspace: str | Path) -> Path:
    """在工作区里定位任务库；找不到抛 FileNotFoundError（附路径提示）。"""
    ws = Path(workspace)
    candidates = [ws / DB_RELATIVE, ws]
    for candidate in candidates:
        if candidate.is_file() and candidate.suffix == ".sqlite3":
            return candidate
        if candidate.is_dir():
            direct = candidate / "aip-tasks.sqlite3"
            if direct.is_file():
                return direct
    raise FileNotFoundError(
        f"找不到任务库：{ws / DB_RELATIVE}（工作区未初始化或尚未跑过 AIP 任务）"
    )


def connect(db: str | Path) -> sqlite3.Connection:
    """只读连接：`mode=ro`，不创建文件、不加锁写。"""
    path = Path(db).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def columns(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.DatabaseError:
        return set()


def has_table(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _iso_ts(value: Any) -> float | None:
    """ISO-8601 → epoch 秒；解析不了返回 None（用于 since 过滤，不参与排序）。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _state_priority(state: str) -> int:
    normalized = normalize_state(state)
    if normalized in _STATE_PRIORITY:
        return _STATE_PRIORITY[normalized]
    if is_terminal(normalized):
        return _DEFAULT_PRIORITY
    if normalized == "awaiting-completion":
        return _UNCLOSED_PRIORITY
    return _UNCLOSED_PRIORITY + 1


def _select_columns(cols: set[str]) -> list[str]:
    wanted = [
        "task_ref", "direction", "state", "local_aic", "remote_aic",
        "created_at", "updated_at", "session_id", "local_error", "products",
    ]
    return [name for name in wanted if name in cols]


def list_tasks(
    db: str | Path,
    *,
    direction: str | None = None,
    peer_aic: str | None = None,
    since: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """列出任务行（按 created_at 倒序取前 limit 条，返回正序）。

    `peer_aic` 只按 `remote_aic` 过滤；列不存在时**不过滤**并在结果里带
    `notes=["任务库无 remote_aic 列，未按对端过滤"]`，避免把"没过滤"伪装成"已匹配"。
    """
    con = connect(db)
    try:
        if not has_table(con, "aip_tasks"):
            raise ValueError(f"{db} 没有 aip_tasks 表（不是 wit 任务库？）")
        cols = columns(con, "aip_tasks")
        selected = _select_columns(cols)
        if "task_ref" not in selected or "direction" not in selected:
            raise ValueError(f"aip_tasks 缺 task_ref/direction 列：{sorted(cols)}")
        query = f"SELECT {', '.join(selected)} FROM aip_tasks"
        params: list[Any] = []
        filters: list[str] = []
        if direction and "direction" in cols:
            filters.append("lower(coalesce(direction, '')) = ?")
            params.append(str(direction).lower())
        if filters:
            query += " WHERE " + " AND ".join(filters)
        order = " ORDER BY created_at DESC" if "created_at" in cols else ""
        query += order + " LIMIT ?"
        params.append(max(1, int(limit)))
        rows = [dict(row) for row in con.execute(query, params)]
    finally:
        con.close()

    notes: list[str] = []
    if peer_aic and "remote_aic" not in cols:
        notes.append("任务库无 remote_aic 列，未按对端过滤")
    if peer_aic and "remote_aic" in cols:
        rows = [row for row in rows if str(row.get("remote_aic") or "") == str(peer_aic)]
    if since:
        threshold = _iso_ts(since)
        if threshold is not None:
            rows = [row for row in rows if (_iso_ts(row.get("created_at")) or 0.0) >= threshold]
        else:
            notes.append(f"since 参数无法解析，已忽略：{since!r}")
    rows.reverse()
    for row in rows:
        row["notes"] = list(notes)
    return rows


def pick_task(
    db: str | Path,
    *,
    direction: str,
    peer_aic: str | None = None,
    since: str | None = None,
    limit: int = 500,
) -> dict[str, Any] | None:
    """挑一个方向里"最硬"的一条任务：completed 优先，其次其它终态，最后 awaiting-completion。

    同一优先级内取 `created_at` 最新的一条（`list_tasks` 返回正序，所以从尾部找）。
    """
    rows = list_tasks(db, direction=direction, peer_aic=peer_aic, since=since, limit=limit)
    if not rows:
        return None
    scored = [
        (_state_priority(str(row.get("state") or "")), idx, row)
        for idx, row in enumerate(rows)
    ]
    scored.sort(key=lambda item: (item[0], -item[1]))
    return scored[0][2]


def read_events(db: str | Path, task_ref: str, *, limit: int = 200) -> list[dict[str, Any]]:
    """读任务事件时间线（表/列名不固定，缺表返回空列表）。"""
    if not task_ref:
        return []
    con = connect(db)
    try:
        if not has_table(con, "aip_task_events"):
            return []
        cols = columns(con, "aip_task_events")
        task_col = "task_ref" if "task_ref" in cols else "task_id" if "task_id" in cols else None
        data_col = "event_data" if "event_data" in cols else "payload" if "payload" in cols else None
        if task_col is None or "created_at" not in cols:
            return []
        selected = [task_col, "created_at"]
        if data_col:
            selected.append(data_col)
        for name in ("event_type", "event_seq", "message_id"):
            if name in cols:
                selected.append(name)
        order = " ORDER BY created_at DESC" + (", event_seq DESC" if "event_seq" in cols else "")
        rows = [dict(row) for row in con.execute(
            f"SELECT {', '.join(selected)} FROM aip_task_events WHERE {task_col} = ?"
            + order + " LIMIT ?",
            (task_ref, max(1, int(limit))),
        )]
    finally:
        con.close()

    rows.reverse()
    timeline: list[dict[str, Any]] = []
    for row in rows:
        payload = row.get(data_col) if data_col else None
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                payload = None
        state = ""
        if isinstance(payload, Mapping):
            status = payload.get("status")
            if isinstance(status, Mapping) and status.get("state"):
                state = normalize_state(status.get("state"))
            elif payload.get("state"):
                state = normalize_state(payload.get("state"))
        event: dict[str, Any] = {"at": str(row.get("created_at") or "")}
        if row.get("event_type"):
            event["type"] = str(row.get("event_type"))
        if state:
            event["state"] = state
        timeline.append(event)
    return timeline


def receipt_for(
    db: str | Path,
    task_ref: str,
    *,
    direction: str | None = None,
    events_limit: int = 200,
) -> TaskReceipt:
    """按 taskId 取一条完整回执；找不到抛 KeyError。"""
    con = connect(db)
    try:
        if not has_table(con, "aip_tasks"):
            raise ValueError(f"{db} 没有 aip_tasks 表（不是 wit 任务库？）")
        cols = columns(con, "aip_tasks")
        selected = _select_columns(cols)
        row = con.execute(
            f"SELECT {', '.join(selected)} FROM aip_tasks WHERE task_ref = ?", (task_ref,)
        ).fetchone()
    finally:
        con.close()
    if row is None:
        raise KeyError(f"任务库里没有 task_ref={task_ref!r}")
    data = dict(row)
    resolved_direction = str(direction or data.get("direction") or "").lower()
    events = read_events(db, task_ref, limit=events_limit)
    state = normalize_state(data.get("state"))
    notes: list[str] = []
    if state == "awaiting-completion":
        notes.append("产物已交付但调用方未 complete（awaiting-completion 不计入双向成立）")
    if not data.get("remote_aic"):
        notes.append("任务库未记录 remote_aic（无法证明对端身份）")
    return TaskReceipt(
        direction=resolved_direction,
        task_ref=str(data.get("task_ref") or task_ref),
        state=state,
        local_aic=str(data.get("local_aic") or ""),
        remote_aic=str(data.get("remote_aic") or ""),
        created_at=str(data.get("created_at") or ""),
        updated_at=str(data.get("updated_at") or ""),
        session_id=str(data.get("session_id") or ""),
        local_error=str(data.get("local_error") or ""),
        products=ProductSummary.from_raw(data.get("products")),
        events=tuple(events),
        source={"kind": "ledger", "db": str(Path(db).resolve())},
        notes=tuple(notes),
    )


def pair_for(
    db: str | Path,
    peer_aic: str,
    *,
    since: str | None = None,
    out_task: str | None = None,
    in_task: str | None = None,
) -> dict[str, TaskReceipt | None]:
    """取本机与 `peer_aic` 之间两个方向的回执（各一条，取不到为 None）。

    `out_task` / `in_task` 可显式指定 taskId（复现某一次已归档的演示）。
    """
    con = connect(db)
    try:
        cols = columns(con, "aip_tasks") if has_table(con, "aip_tasks") else set()
    finally:
        con.close()

    def _one(direction: str, explicit: str | None) -> TaskReceipt | None:
        if explicit:
            return receipt_for(db, explicit, direction=direction)
        row = pick_task(db, direction=direction, peer_aic=peer_aic, since=since)
        if row is None:
            return None
        return receipt_for(db, str(row.get("task_ref") or ""), direction=direction)

    if "remote_aic" not in cols:
        raise ValueError(
            "任务库没有 remote_aic 列：无法按对端过滤。请升级 wit 或直接用 "
            "--out-task / --in-task 指定两个方向的 taskId。"
        )
    return {
        "outbound": _one("outbound", out_task),
        "inbound": _one("inbound", in_task),
    }


def summarize(db: str | Path, *, peer_aic: str | None = None, limit: int = 1000) -> dict[str, Any]:
    """任务库概况（doctor / 排障用）：按方向 × 状态计数。"""
    rows = list_tasks(db, peer_aic=peer_aic, limit=limit)
    by_direction: dict[str, dict[str, int]] = {}
    for row in rows:
        direction = str(row.get("direction") or "?")
        state = normalize_state(row.get("state")) or "?"
        bucket = by_direction.setdefault(direction, {})
        bucket[state] = bucket.get(state, 0) + 1
    return {"db": str(Path(db).resolve()), "total": len(rows), "by_direction": by_direction}


def iter_peer_aics(db: str | Path, *, limit: int = 2000) -> Iterable[str]:
    """列出任务库里出现过的对端 AIC（供 doctor 提示"你应该 pack 谁"）。"""
    rows = list_tasks(db, limit=limit)
    seen: list[str] = []
    for row in rows:
        peer = str(row.get("remote_aic") or "")
        if peer and peer not in seen:
            seen.append(peer)
    return seen


__all__ = [
    "DB_RELATIVE",
    "columns",
    "connect",
    "find_db",
    "has_table",
    "iter_peer_aics",
    "list_tasks",
    "pair_for",
    "pick_task",
    "read_events",
    "receipt_for",
    "summarize",
]