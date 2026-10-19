"""配额计数器（sqlite）：使用侧 `acps_max_uses` + 签发侧 `maxGrantsPerDay`（RFC §4.7 双轨）。

使用侧：一用一行（INSERT-only ledger，计数 = COUNT）——比 UPDATE 自增
多一行存储，换来审计友好（每次使用都有时间戳记录）。
注意"原子"的边界：try_consume 走 deferred 事务，首条语句是 SELECT（未持写锁），
同进程内因调用不交错而安全；跨进程并发时读-写之间存在窗口，量级放大到多进程准入
前需改成 `BEGIN IMMEDIATE`（2026-10-07 审查记录，未实测到超配）。
签发侧：`grant_issues` 账本按 (domain, issuer) 计数，**滑动 24 小时窗口**
（issued_at > now-86400），跨重启持久——"签发者一天能发多少枚 grant"的执行。

为什么 sqlite 而不是进程内 dict（第九轮设计定案）：配额一旦承载计量/计费语义，
"进程重启 → 计数清零 → 配额失效"就是语义破产；jti 一次性对 delegation token（≤10min）
可接受内存实现，但 grant 的 max_uses 必须持久 + 原子（并发准入不得漏计）。

表结构：`grant_uses(jti, domain, caller_aic, used_at)` 与 `grant_issues(domain, issuer_aic, jti, issued_at)`。
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

_warned_fresh: set[str] = set()  # 同一进程内每个账本只提醒一次"文件是新建的"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS grant_uses (
    jti TEXT NOT NULL,
    domain TEXT NOT NULL,
    caller_aic TEXT NOT NULL,
    used_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_grant_uses_jti ON grant_uses(jti);
CREATE INDEX IF NOT EXISTS idx_grant_uses_domain ON grant_uses(domain);
CREATE TABLE IF NOT EXISTS grant_issues (
    jti TEXT NOT NULL,
    domain TEXT NOT NULL,
    issuer_aic TEXT NOT NULL,
    issued_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_grant_issues_window ON grant_issues(domain, issuer_aic, issued_at);
"""


class QuotaStore:
    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not self._path.exists()
        # 不开 WAL：单写者演示规模用默认 journal 即可，且 WAL 的 -shm/-wal 在
        # Windows 下会短暂持有句柄、妨碍测试清理临时目录。
        self._conn = sqlite3.connect(str(self._path))
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        if fresh and str(self._path) not in _warned_fresh:
            # 账本文件被删/换路径 = 用量计数静默归零，grant 配额形同重置（审查发现）
            _warned_fresh.add(str(self._path))
            print(f"[quota] 警告: 账本 {self._path} 不存在，已新建——历史用量计数从 0 起",
                  file=sys.stderr, flush=True)

    def try_consume(self, *, jti: str, domain: str, caller_aic: str, max_uses: int, now: float | None = None) -> tuple[bool, int]:
        """原子地记一次使用并检查配额：未超返回 (True, 剩余次数)；已超返回 (False, 0) 且不落行。"""
        with self._conn:  # BEGIN … COMMIT（sqlite 默认 deferred，写锁保证原子）
            used = self._conn.execute(
                "SELECT COUNT(*) FROM grant_uses WHERE jti = ?", (jti,)
            ).fetchone()[0]
            if used >= max_uses:
                return False, 0
            self._conn.execute(
                "INSERT INTO grant_uses (jti, domain, caller_aic, used_at) VALUES (?, ?, ?, ?)",
                (jti, domain, caller_aic, float(now if now is not None else time.time())),
            )
            return True, max_uses - used - 1

    def issues_in_window(self, *, domain: str, issuer_aic: str, now: float | None = None) -> int:
        """滑动 24h 窗口内该 (域, 签发者) 已签发的 grant 数。"""
        cutoff = float(now if now is not None else time.time()) - 86400
        return int(self._conn.execute(
            "SELECT COUNT(*) FROM grant_issues WHERE domain = ? AND issuer_aic = ? AND issued_at > ?",
            (domain, issuer_aic, cutoff),
        ).fetchone()[0])

    def record_issue(self, *, domain: str, issuer_aic: str, jti: str, now: float | None = None) -> None:
        """记一次成功签发（先查后发再记：单写者签发侧无竞态，见 commerce.issue_paid_grant）。"""
        with self._conn:
            self._conn.execute(
                "INSERT INTO grant_issues (jti, domain, issuer_aic, issued_at) VALUES (?, ?, ?, ?)",
                (jti, domain, issuer_aic, float(now if now is not None else time.time())),
            )

    def close(self) -> None:
        self._conn.close()


def default_quota_path(workspace: Path | str) -> Path:
    return Path(workspace) / "acps" / "grant-quota.sqlite3"


def consume(
    path: Path | str,
    *,
    jti: str,
    domain: str,
    caller_aic: str,
    max_uses: int,
    now: float | None = None,
) -> tuple[bool, int]:
    """按需开合的一次性消费（无长持句柄——Windows 下临时目录可清理；开销亚毫秒）。"""
    store = QuotaStore(path)
    try:
        return store.try_consume(jti=jti, domain=domain, caller_aic=caller_aic, max_uses=max_uses, now=now)
    finally:
        store.close()


def issues_in_window(
    path: Path | str, *, domain: str, issuer_aic: str, now: float | None = None
) -> int:
    """按需开合查询签发计数（与 consume 同款，无长持句柄）。"""
    store = QuotaStore(path)
    try:
        return store.issues_in_window(domain=domain, issuer_aic=issuer_aic, now=now)
    finally:
        store.close()


def record_issue(
    path: Path | str, *, domain: str, issuer_aic: str, jti: str, now: float | None = None
) -> None:
    store = QuotaStore(path)
    try:
        store.record_issue(domain=domain, issuer_aic=issuer_aic, jti=jti, now=now)
    finally:
        store.close()
