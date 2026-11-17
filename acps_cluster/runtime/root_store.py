"""根任务台账（多入口编排 P1）：SQLite + 幂等键 + 应用层状态机。

一条根任务 = 一次"入口 Partner 提交的科研需求"。它和平台 AIP taskId 不是一回事：

- `root_id`：业务根任务号（本模块生成，`rt-…`），跨多次平台调用保持不变；
- `job_id`：后台作业号（`jobs_store` 生成），是当前执行的载体；
- 平台 taskId：入口那一次 AIP 调用的标识，由调用方/平台侧记录，不进本表主键。

幂等键 = `(entry_aic, user_scope, request_id)`。同键同内容返回原记录；同键不同内容
抛 `RequestConflict`（不覆盖）。`entry_aic` 必须是**已验证的调用方身份**，不接受正文自报。

纯标准库（sqlite3），不依赖 wit，便于离线单测。
"""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

STATES = ("accepted", "running", "completed", "failed", "cancelled", "expired")
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "expired"})
#: 允许的状态迁移（同态更新另算：只刷新时间/字段）
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "accepted": frozenset({"running", "completed", "failed", "cancelled", "expired"}),
    "running": frozenset({"completed", "failed", "cancelled", "expired"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "expired": frozenset(),
}

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SCOPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_MAX_QUESTION = 20000


class RequestConflict(ValueError):
    """幂等键相同但内容不同：不覆盖原任务。"""


class InvalidTransition(ValueError):
    """不允许的状态迁移。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_root_id(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).astimezone().strftime("%Y%m%d-%H%M%S")
    return f"rt-{stamp}-{secrets.token_hex(2)}"


def request_digest(question: str, mode: str) -> str:
    """幂等内容指纹：只对"用户可感知的请求内容"取哈希。"""
    payload = f"{str(question).strip()}\0{str(mode).strip()}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_request_id(value: str) -> str:
    text = str(value or "").strip()
    if not _REQUEST_ID_RE.fullmatch(text):
        raise ValueError(
            "request_id 非法：需 1-128 位，首字符为字母/数字，仅允许字母数字与 . _ : -"
        )
    return text


def _validate_scope(value: str) -> str:
    text = str(value or "").strip() or "default"
    if not _SCOPE_RE.fullmatch(text):
        raise ValueError("user_scope 非法：需 1-128 位，仅允许字母数字与 . _ : @ -")
    return text


@dataclass(frozen=True)
class RootTask:
    root_id: str
    request_id: str
    source_task_id: str
    source_caller: str
    source_session: str
    entry_aic: str
    user_scope: str
    request_digest: str
    state: str
    question: str
    mode: str
    job_id: str = ""
    platform_task_id: str = ""
    error: str = ""
    result_ref: str = ""
    created_at: str = ""
    updated_at: str = ""

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def as_dict(self) -> dict:
        return {
            "root_id": self.root_id,
            "request_id": self.request_id,
            "source_task_id": self.source_task_id,
            "source_caller": self.source_caller,
            "source_session": self.source_session,
            "entry_aic": self.entry_aic,
            "user_scope": self.user_scope,
            "state": self.state,
            "mode": self.mode,
            "job_id": self.job_id,
            "platform_task_id": self.platform_task_id,
            "error": self.error,
            "result_ref": self.result_ref,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class RootStore:
    """根任务台账。每次操作独立连接，WAL + busy_timeout，写路径用 BEGIN IMMEDIATE。"""

    def __init__(self, db: str | Path) -> None:
        self.path = Path(db)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._session() as con:
            self._ensure_schema(con)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=5000")
        return con

    @contextmanager
    def _session(self):
        """短连接：sqlite3 的 `with con:` 只提交/回滚、不关闭，这里显式关闭。"""
        con = self._connect()
        try:
            yield con
        finally:
            con.close()

    @staticmethod
    def _ensure_schema(con: sqlite3.Connection) -> None:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(
            """CREATE TABLE IF NOT EXISTS root_tasks (
                   root_id TEXT PRIMARY KEY,
                   request_id TEXT NOT NULL,
                   source_task_id TEXT NOT NULL DEFAULT '',
                   source_caller TEXT NOT NULL DEFAULT '',
                   source_session TEXT NOT NULL DEFAULT '',
                   entry_aic TEXT NOT NULL,
                   user_scope TEXT NOT NULL,
                   request_digest TEXT NOT NULL,
                   state TEXT NOT NULL,
                   question TEXT NOT NULL,
                   mode TEXT NOT NULL,
                   job_id TEXT NOT NULL DEFAULT '',
                   platform_task_id TEXT NOT NULL DEFAULT '',
                   error TEXT NOT NULL DEFAULT '',
                   result_ref TEXT NOT NULL DEFAULT '',
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL
               )"""
        )
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_root_request"
            " ON root_tasks(entry_aic, user_scope, request_id)"
        )
        con.execute("CREATE INDEX IF NOT EXISTS ix_root_state ON root_tasks(state)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_root_job ON root_tasks(job_id)")
        cols = {row[1] for row in con.execute("PRAGMA table_info(root_tasks)")}
        if "source_task_id" not in cols:
            con.execute("ALTER TABLE root_tasks ADD COLUMN source_task_id TEXT NOT NULL DEFAULT ''")
        for column in ("source_caller", "source_session"):
            if column not in cols:
                con.execute(f"ALTER TABLE root_tasks ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        con.execute("CREATE INDEX IF NOT EXISTS ix_root_source ON root_tasks(source_task_id)")
        try:
            con.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_root_source ON root_tasks(source_task_id)"
                " WHERE source_task_id <> ''")
        except sqlite3.IntegrityError:
            print("[root] 警告: source_task_id 存在重复历史记录，唯一索引未建立", flush=True)
        con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @staticmethod
    def _row(row: sqlite3.Row | None) -> RootTask | None:
        if row is None:
            return None
        keys = row.keys()
        return RootTask(
            root_id=str(row["root_id"]),
            request_id=str(row["request_id"]),
            source_task_id=str(row["source_task_id"] if "source_task_id" in keys else ""),
            source_caller=str(row["source_caller"] if "source_caller" in keys else ""),
            source_session=str(row["source_session"] if "source_session" in keys else ""),
            entry_aic=str(row["entry_aic"]),
            user_scope=str(row["user_scope"]),
            request_digest=str(row["request_digest"]),
            state=str(row["state"]),
            question=str(row["question"]),
            mode=str(row["mode"]),
            job_id=str(row["job_id"] if "job_id" in keys else ""),
            platform_task_id=str(row["platform_task_id"] if "platform_task_id" in keys else ""),
            error=str(row["error"] if "error" in keys else ""),
            result_ref=str(row["result_ref"] if "result_ref" in keys else ""),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def get(self, root_id: str) -> RootTask | None:
        with self._session() as con:
            row = con.execute(
                "SELECT * FROM root_tasks WHERE root_id = ?", (str(root_id),)
            ).fetchone()
        return self._row(row)

    def get_by_job_id(self, job_id: str) -> RootTask | None:
        """按后台作业号反查根任务（调用方只拿到 job_id 时的兜底查询入口）。"""
        job = str(job_id or "").strip()
        if not job:
            return None
        with self._session() as con:
            row = con.execute(
                "SELECT * FROM root_tasks WHERE job_id=? ORDER BY created_at DESC LIMIT 1",
                (job,),
            ).fetchone()
        return self._row(row)

    def get_by_source_task_id(self, source_task_id: str) -> RootTask | None:
        """按原始入站 taskId 反查根任务（入口转发后的对账入口）。"""
        source = str(source_task_id or "").strip()
        if not source:
            return None
        with self._session() as con:
            row = con.execute(
                "SELECT * FROM root_tasks WHERE source_task_id=? ORDER BY created_at DESC LIMIT 1",
                (source,),
            ).fetchone()
        return self._row(row)

    def submit(
        self,
        *,
        entry_aic: str,
        user_scope: str,
        request_id: str,
        question: str,
        mode: str = "custom",
        source_task_id: str = "",
        source_caller: str = "",
        source_session: str = "",
    ) -> tuple[RootTask, bool]:
        """幂等受理：返回 (根任务, 是否新建)。同键不同内容抛 `RequestConflict`。"""
        aic = str(entry_aic or "").strip()
        if not aic:
            raise ValueError("entry_aic 不能为空（必须是已验证的调用方身份）")
        scope = _validate_scope(user_scope)
        rid = _validate_request_id(request_id)
        text = str(question or "").strip()
        if len(text) < 8:
            raise ValueError("question 太短：请提交完整任务文本（至少 8 个字符）")
        if len(text) > _MAX_QUESTION:
            raise ValueError(f"question 太长：上限 {_MAX_QUESTION} 字符")
        mode_text = str(mode or "custom").strip() or "custom"
        if len(mode_text) > 32:
            raise ValueError("mode 太长（上限 32 字符）")
        digest = request_digest(text, mode_text)

        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT * FROM root_tasks WHERE entry_aic=? AND user_scope=? AND request_id=?",
                (aic, scope, rid),
            ).fetchone()
            if row is not None:
                con.execute("COMMIT")
                existing = self._row(row)
                assert existing is not None
                if existing.request_digest != digest:
                    raise RequestConflict(
                        "同一幂等键（entry_aic+user_scope+request_id）已存在不同内容；"
                        "不覆盖原任务。要提交新内容请换一个 request_id。"
                    )
                return existing, False
            now = _now_iso()
            root_id = new_root_id()
            con.execute(
                "INSERT INTO root_tasks(root_id, request_id, source_task_id, source_caller,"
                " source_session, entry_aic, user_scope, request_digest, state, question, mode,"
                " created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (root_id, rid, str(source_task_id or ""), str(source_caller or ""),
                 str(source_session or ""), aic, scope, digest, "accepted", text, mode_text, now, now),
            )
            con.execute("COMMIT")
            created = self.get(root_id)
            assert created is not None
            return created, True
        except sqlite3.IntegrityError:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            row = con.execute(
                "SELECT * FROM root_tasks WHERE entry_aic=? AND user_scope=? AND request_id=?",
                (aic, scope, rid),
            ).fetchone()
            existing = self._row(row)
            if existing is None and str(source_task_id or "").strip():
                row = con.execute(
                    "SELECT * FROM root_tasks WHERE source_task_id=? ORDER BY created_at DESC LIMIT 1",
                    (str(source_task_id).strip(),),
                ).fetchone()
                existing = self._row(row)
            if existing is None:
                raise
            if existing.request_digest != digest:
                raise RequestConflict("同一幂等键/原任务并发写入不同内容") from None
            return existing, False
        except Exception:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            con.close()

    def attach_job(self, root_id: str, job_id: str) -> RootTask:
        job = str(job_id or "").strip()
        if not job:
            raise ValueError("job_id 不能为空")
        task = self.get(root_id)
        if task is None:
            raise KeyError(f"根任务不存在：{root_id}")
        if task.job_id and task.job_id != job:
            raise ValueError(f"根任务已绑定作业 {task.job_id}，拒绝改绑 {job}")
        with self._session() as con:
            con.execute(
                "UPDATE root_tasks SET job_id=?, updated_at=? WHERE root_id=?",
                (job, _now_iso(), task.root_id),
            )
        updated = self.get(task.root_id)
        assert updated is not None
        return updated

    def set_platform_task(self, root_id: str, task_id: str) -> RootTask:
        """记录入口那次平台 AIP taskId（P5 证据关联用；不参与幂等）。"""
        task = self.get(root_id)
        if task is None:
            raise KeyError(f"根任务不存在：{root_id}")
        value = str(task_id or "").strip()
        if not value:
            return task
        with self._session() as con:
            con.execute(
                "UPDATE root_tasks SET platform_task_id=?, updated_at=? WHERE root_id=?",
                (value, _now_iso(), task.root_id),
            )
        updated = self.get(task.root_id)
        assert updated is not None
        return updated

    def set_state(
        self,
        root_id: str,
        state: str,
        *,
        error: str | None = None,
        result_ref: str | None = None,
    ) -> RootTask:
        target = str(state or "").strip().lower()
        if target not in STATES:
            raise ValueError(f"未知状态：{state!r}")
        task = self.get(root_id)
        if task is None:
            raise KeyError(f"根任务不存在：{root_id}")
        if target != task.state and target not in ALLOWED_TRANSITIONS.get(task.state, frozenset()):
            raise InvalidTransition(f"不允许的状态迁移：{task.state} → {target}")
        updates = ["state=?", "updated_at=?"]
        params: list[object] = [target, _now_iso()]
        if error is not None:
            updates.append("error=?")
            params.append(str(error))
        if result_ref is not None:
            updates.append("result_ref=?")
            params.append(str(result_ref))
        params.append(task.root_id)
        with self._session() as con:
            con.execute(f"UPDATE root_tasks SET {', '.join(updates)} WHERE root_id=?", params)
        updated = self.get(task.root_id)
        assert updated is not None
        return updated

    def list_recent(self, *, limit: int = 20, entry_aic: str = "") -> list[RootTask]:
        query = "SELECT * FROM root_tasks"
        params: list[object] = []
        if entry_aic:
            query += " WHERE entry_aic=?"
            params.append(str(entry_aic))
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._session() as con:
            rows = con.execute(query, params).fetchall()
        return [task for task in (self._row(row) for row in rows) if task is not None]


__all__ = [
    "ALLOWED_TRANSITIONS",
    "InvalidTransition",
    "RequestConflict",
    "RootStore",
    "RootTask",
    "SCHEMA_VERSION",
    "STATES",
    "TERMINAL_STATES",
    "new_root_id",
    "request_digest",
]