"""scout 论文记忆层：canonical 去重 + provenance + 关键词索引。

## 为什么（2026-10-11 实测依据）

对集群历史任务统计：检索类指令 281 条 → 真正独立的检索主题 105 条 → **唯一主题仅 58 个**，
跨任务主题重复率 ≈ **45%**（高频：diffusion×21、models×19、kgc×6…）。
即接近一半的检索是同一批主题反复查——把结果存下来能直接省掉这些网络往返。

## 设计要点（都是踩过/想清楚的坑）

1. **canonical 论文身份**：同一篇论文在 arXiv / OpenAlex / S2 里 ID 不同，
   按源各自存会"越滚越胖但知识不涨"。本模块的键优先级：
   `arXiv ID` → `(source, native_id)` → `标题+首作者指纹`；
   并且**额外用标题指纹做跨源合并**（低风险：只在标题归一化后完全相同且年份不冲突时合并）。
   > 注：`ScholarRecord` 目前**没有 DOI 字段**（只有 source/native_id），所以 DOI 位先留空，
   > 未来加字段时补进 `_canonical_of` 即可。
2. **一篇论文一条记录**（不是"每次查询的原始响应"），provenance 累积各来源与命中查询。
3. **命中口径按归一化主题/关键词**，不按查询原文——历史数据证明措辞每次略不同，按原文几乎全 miss。
4. **时效**：存 `fetched_at`，超过 TTL 的记录不再作为命中（需要重新联网刷新）。
5. **关键词索引**：用 FTS5（若不可用则退回 LIKE），避免全表扫描。

存储位置：`<workspace>/memory/papers.sqlite3`（默认 scouts 的 ws-scout）。
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

DEFAULT_TTL_DAYS = 7.0
_MAX_ROWS_SCAN = 4000

_STOP = {
    "the", "a", "an", "of", "and", "or", "for", "on", "in", "to", "with", "by", "at",
    "is", "are", "be", "as", "from", "that", "this", "it", "we", "our", "using", "use",
    # 泛词（2026-10-16 现网标定）：这些词在几乎任何论文摘要里都出现，只靠它们命中
    # 会把不相关记录判成"记忆命中"（实测 agent-memory/医学记录靠 models/based/score
    # 命中扩散查询）——从匹配词表剔除，让其不参与命中判定与 FTS 候选。
    "model", "models", "based", "score",
    "请", "帮", "我", "的", "了", "与", "和", "关于", "一下", "检索", "搜索", "论文", "文献",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    key             TEXT PRIMARY KEY,          -- canonical key
    title           TEXT NOT NULL,
    title_norm      TEXT NOT NULL,             -- 归一化标题（跨源比对用）
    authors         TEXT NOT NULL DEFAULT '',  -- '; ' 连接
    year            INTEGER,
    abstract        TEXT NOT NULL DEFAULT '',
    url             TEXT NOT NULL DEFAULT '',
    pdf_url         TEXT NOT NULL DEFAULT '',
    citations       INTEGER,
    arxiv_id        TEXT,
    first_author    TEXT NOT NULL DEFAULT '',
    sources         TEXT NOT NULL DEFAULT '',  -- '; ' 连接，provenance
    source_ids      TEXT NOT NULL DEFAULT '',  -- 'source:native_id' 列表
    queries         TEXT NOT NULL DEFAULT '',  -- 命中过它的查询（归一化）
    fetched_at      REAL NOT NULL,
    refreshed_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_papers_title_norm ON papers(title_norm);
CREATE INDEX IF NOT EXISTS idx_papers_refreshed  ON papers(refreshed_at);
CREATE INDEX IF NOT EXISTS idx_papers_arxiv      ON papers(arxiv_id);

CREATE TABLE IF NOT EXISTS memory_meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""


# ─────────────────────────── 归一化与 canonical 键 ───────────────────────────

def normalize_title(title: str) -> str:
    """标题归一化：小写、去标点、压空白。用于跨源同一性判断与关键词匹配。"""
    t = (title or "").lower()
    t = re.sub(r"[^\w\u4e00-\u9fff]+", " ", t, flags=re.UNICODE)
    return re.sub(r"\s+", " ", t).strip()


def title_fingerprint(title: str, first_author: str = "") -> str:
    """标题+首作者的稳定指纹（标题为空时退化用作者）。"""
    basis = normalize_title(title) or (first_author or "").strip().lower()
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def keywords(query: str, limit: int = 12) -> list[str]:
    """查询关键词（去掉停用词），用于命中判断与索引。"""
    words = []
    for w in normalize_title(query).split():
        if len(w) < 2 or w in _STOP:
            continue
        if w not in words:
            words.append(w)
        if len(words) >= limit:
            break
    return words


def _arxiv_id_in(text: str) -> str:
    """从任意文本里抽 arXiv ID（含 2508.17434 与 cs/0701001 两种形态）。"""
    if not text:
        return ""
    m = re.search(r"(\d{4}\.\d{4,5})(v\d+)?", text)
    if m:
        return m.group(1)
    m = re.search(r"\b([a-z\-]+(?:\.[A-Z]{2})?/\d{7})\b", text, re.I)
    return m.group(1) if m else ""


def record_fields(rec: Any) -> dict[str, Any]:
    """把 PaperRecord / ScholarRecord 归一成统一字典（两种 dataclass 都能吃）。"""
    def g(name: str, default: Any = None) -> Any:
        if isinstance(rec, Mapping):
            return rec.get(name, default)
        return getattr(rec, name, default)

    authors = g("authors", []) or []
    if isinstance(authors, str):
        authors = [a for a in re.split(r"[;,]", authors) if a.strip()]
    authors = [str(a).strip() for a in authors if str(a).strip()]

    title = str(g("title", "") or "")
    url = str(g("url", "") or "")
    pdf = str(g("pdf_url", "") or "")
    source = str(g("source", "") or "")
    native = str(g("native_id", "") or "")
    arxiv = str(g("arxiv_id", "") or "") or _arxiv_id_in(url) or _arxiv_id_in(pdf)
    if not arxiv and source.lower() == "arxiv":
        arxiv = _arxiv_id_in(native) or native
    # arXiv 的 PaperRecord 没有 source/native_id 字段 → 补出来
    if not source and arxiv:
        source, native = "arxiv", arxiv

    year = g("year", None)
    if year is None and g("published"):
        m = re.search(r"(\d{4})", str(g("published")))
        year = int(m.group(1)) if m else None

    return {
        "title": title,
        "title_norm": normalize_title(title),
        "authors": authors,
        "first_author": (authors[0] if authors else ""),
        "year": int(year) if isinstance(year, int) else None,
        "abstract": str(g("abstract", "") or ""),
        "url": url or (f"https://arxiv.org/abs/{arxiv}" if arxiv else ""),
        "pdf_url": pdf,
        "citations": g("citations", None),
        "arxiv_id": arxiv or None,
        "source": source or "unknown",
        "native_id": native or (arxiv or ""),
    }


def canonical_key(fields: Mapping[str, Any]) -> str:
    """canonical 键：arXiv ID → (source, native_id) → 标题+首作者指纹。"""
    if fields.get("arxiv_id"):
        return f"arxiv:{fields['arxiv_id']}"
    src, nid = fields.get("source"), fields.get("native_id")
    if src and nid:
        return f"{src}:{nid}"
    return f"fp:{title_fingerprint(str(fields.get('title') or ''), str(fields.get('first_author') or ''))}"


# ─────────────────────────────── 存储 ───────────────────────────────

class PaperMemory:
    """论文记忆库（SQLite）。线程内单例使用即可。"""

    def __init__(self, db_path: Path | str, *, ttl_days: float | None = None) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl_days = float(
            ttl_days if ttl_days is not None
            else os.getenv("ACPS_SCOUT_MEMORY_TTL_DAYS", str(DEFAULT_TTL_DAYS))
        )
        self._con = sqlite3.connect(str(self.db_path), timeout=15.0)
        self._con.row_factory = sqlite3.Row
        self._con.executescript(SCHEMA)
        self._con.commit()
        self._fts = self._init_fts()

    # -- FTS5 可选（不可用则退回 LIKE） --------------------------------------
    def _init_fts(self) -> bool:
        try:
            self._con.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts "
                "USING fts5(key UNINDEXED, title, abstract, tokenize='unicode61')"
            )
            self._con.commit()
            return True
        except sqlite3.OperationalError:
            return False

    def close(self) -> None:
        try:
            self._con.close()
        except Exception:  # noqa: BLE001
            pass

    # -- 写入 ---------------------------------------------------------------
    def remember(self, records: Iterable[Any], *, query: str = "") -> dict[str, int]:
        """把一批检索结果存进记忆库（canonical 去重 + 跨源合并 + provenance 累积）。"""
        now = time.time()
        q_norm = normalize_title(query)[:200]
        added = merged = 0
        for rec in records or ():
            f = record_fields(rec)
            if not f["title"]:
                continue
            key = canonical_key(f)
            # 跨源合并：同标题指纹且年份不冲突 → 复用已有记录
            existing = self._find_by_title(f["title_norm"], f["year"], f["arxiv_id"])
            if existing is not None:
                key = str(existing["key"])
            row = self._get(key)
            if row is None:
                self._insert(key, f, q_norm, now)
                added += 1
            else:
                self._merge(row, f, q_norm, now)
                merged += 1
        self._con.commit()
        return {"added": added, "merged": merged}

    def _insert(self, key: str, f: Mapping[str, Any], q_norm: str, now: float) -> None:
        self._con.execute(
            "INSERT INTO papers (key, title, title_norm, authors, year, abstract, url, pdf_url,"
            " citations, arxiv_id, first_author, sources, source_ids, queries, fetched_at,"
            " refreshed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, f["title"], f["title_norm"], "; ".join(f["authors"]), f["year"],
             f["abstract"], f["url"], f["pdf_url"], f["citations"], f["arxiv_id"],
             f["first_author"], f["source"],
             f"{f['source']}:{f['native_id']}" if f["native_id"] else f["source"],
             q_norm, now, now),
        )
        if self._fts:
            self._con.execute(
                "INSERT INTO papers_fts (key, title, abstract) VALUES (?,?,?)",
                (key, f["title"], f["abstract"]),
            )

    def _merge(self, row: sqlite3.Row, f: Mapping[str, Any], q_norm: str, now: float) -> None:
        """合并：补全缺失字段、累积 provenance、刷新时间戳。"""
        sources = {s for s in (row["sources"] or "").split("; ") if s}
        sources.add(f["source"])
        src_ids = {s for s in (row["source_ids"] or "").split("; ") if s}
        if f["native_id"]:
            src_ids.add(f"{f['source']}:{f['native_id']}")
        queries = {q for q in (row["queries"] or "").split("; ") if q}
        if q_norm:
            queries.add(q_norm)

        def pick(new: Any, old: Any) -> Any:
            """新值非空就用新值；旧值更"长"（信息更多）时保留旧值。"""
            if new in (None, "", 0):
                return old
            if old in (None, ""):
                return new
            if isinstance(new, str) and isinstance(old, str) and len(old) > len(new):
                return old
            return new

        self._con.execute(
            "UPDATE papers SET abstract=?, url=?, pdf_url=?, citations=?, year=?,"
            " arxiv_id=COALESCE(arxiv_id, ?), sources=?, source_ids=?, queries=?,"
            " refreshed_at=? WHERE key=?",
            (pick(f["abstract"], row["abstract"]), pick(f["url"], row["url"]),
             pick(f["pdf_url"], row["pdf_url"]), pick(f["citations"], row["citations"]),
             pick(f["year"], row["year"]), f["arxiv_id"],
             "; ".join(sorted(sources)), "; ".join(sorted(src_ids)),
             "; ".join(sorted(queries))[:2000], now, row["key"]),
        )
        if self._fts:
            self._con.execute("DELETE FROM papers_fts WHERE key=?", (row["key"],))
            self._con.execute(
                "INSERT INTO papers_fts (key, title, abstract) VALUES (?,?,?)",
                (row["key"], row["title"], pick(f["abstract"], row["abstract"])),
            )

    # -- 查询 ---------------------------------------------------------------
    def _get(self, key: str) -> sqlite3.Row | None:
        cur = self._con.execute("SELECT * FROM papers WHERE key=?", (key,))
        return cur.fetchone()

    def _find_by_title(self, title_norm: str, year: int | None,
                       arxiv_id: str | None) -> sqlite3.Row | None:
        if not title_norm:
            return None
        if arxiv_id:
            cur = self._con.execute("SELECT * FROM papers WHERE arxiv_id=?", (arxiv_id,))
            hit = cur.fetchone()
            if hit is not None:
                return hit
        cur = self._con.execute("SELECT * FROM papers WHERE title_norm=?", (title_norm,))
        for cand in cur.fetchall():
            cy = cand["year"]
            # 年份不冲突才合并（同标题不同年可能是不同论文）
            if year is None or cy is None or int(cy) == int(year):
                return cand
        return None

    def lookup(self, query: str, *, limit: int = 10,
               max_age_days: float | None = None) -> list[dict[str, Any]]:
        """按归一化关键词查记忆库；只返回 TTL 内（未过期）的记录。"""
        words = keywords(query)
        if not words:
            return []
        ttl = self.ttl_days if max_age_days is None else float(max_age_days)
        cutoff = time.time() - ttl * 86400.0

        rows: list[sqlite3.Row] = []
        if self._fts:
            expr = " OR ".join(f'"{w}"' for w in words)
            try:
                cur = self._con.execute(
                    "SELECT p.* FROM papers_fts f JOIN papers p ON p.key = f.key "
                    "WHERE papers_fts MATCH ? AND p.refreshed_at >= ? LIMIT ?",
                    (expr, cutoff, _MAX_ROWS_SCAN),
                )
                rows = cur.fetchall()
            except sqlite3.OperationalError:
                rows = []
        if not rows:
            like = " OR ".join(["title LIKE ? OR abstract LIKE ?"] * len(words))
            params: list[Any] = []
            for w in words:
                params.extend([f"%{w}%", f"%{w}%"])
            params.extend([cutoff, _MAX_ROWS_SCAN])
            cur = self._con.execute(
                f"SELECT * FROM papers WHERE ({like}) AND refreshed_at >= ? LIMIT ?", params)
            rows = cur.fetchall()

        # 相关性下限（2026-10-16 现网事故）：泛词（models/based/score…）单命中不能算
        # "记忆命中"，否则弱相关记录会短路联网检索——扩散查询曾被 18 条 agent-memory/
        # 医学记录以 1–5 个泛词命中截胡，三条检索路由全不联网。
        # 短查询（≤2 词）必须**全部**命中：泛词（text/sql/models）单命中就会短路
        # 联网检索，把 agent-memory/医学影像这类无关记录当命中
        # （2026-10-20 实测：text-to-SQL 被 "text" 一词截胡；2026-10-25 自服务器
        #   备份找回——10-23 部署曾把这段覆盖回旧版）。
        min_hits = len(words) if len(words) <= 2 else max(2, (len(words) + 3) // 4)
        scored: list[tuple[float, sqlite3.Row]] = []
        for r in rows:
            hay = f"{r['title_norm']} {str(r['abstract']).lower()}"
            hits = sum(1 for w in words if w in hay)
            if hits < min_hits:
                continue
            age_days = max(0.0, (time.time() - float(r["refreshed_at"])) / 86400.0)
            score = hits * 10.0 + min(3.0, float(r["citations"] or 0) / 100.0) - age_days * 0.05
            scored.append((score, r))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [dict(r) for _, r in scored[:limit]]

    def stats(self) -> dict[str, Any]:
        cur = self._con.execute(
            "SELECT COUNT(*) n, MIN(fetched_at) first, MAX(refreshed_at) last FROM papers")
        row = cur.fetchone()
        n = int(row["n"] or 0)
        by_source: dict[str, int] = {}
        for r in self._con.execute("SELECT sources FROM papers"):
            for s in str(r["sources"] or "").split("; "):
                if s:
                    by_source[s] = by_source.get(s, 0) + 1
        ttl = self.ttl_days
        fresh = self._con.execute(
            "SELECT COUNT(*) n FROM papers WHERE refreshed_at >= ?",
            (time.time() - ttl * 86400.0,)).fetchone()["n"]
        return {
            "papers": n,
            "fresh": int(fresh or 0),
            "stale": n - int(fresh or 0),
            "sources": by_source,
            "ttlDays": ttl,
            "firstFetchedAt": row["first"],
            "lastRefreshedAt": row["last"],
            "path": str(self.db_path),
            "fts": self._fts,
        }

    def all_papers(self, *, limit: int = 20, offset: int = 0,
                   fresh_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM papers"
        params: list[Any] = []
        if fresh_only:
            sql += " WHERE refreshed_at >= ?"
            params.append(time.time() - self.ttl_days * 86400.0)
        sql += " ORDER BY refreshed_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return [dict(r) for r in self._con.execute(sql, params).fetchall()]


_DEFAULT: PaperMemory | None = None
_DEFAULT_PATH: Path | None = None


def default_memory(workspace: Path | str) -> PaperMemory:
    """按工作区取默认记忆库（进程内缓存）。"""
    global _DEFAULT, _DEFAULT_PATH
    path = Path(workspace) / "memory" / "papers.sqlite3"
    if _DEFAULT is None or _DEFAULT_PATH != path:
        if _DEFAULT is not None:
            _DEFAULT.close()
        _DEFAULT = PaperMemory(path)
        _DEFAULT_PATH = path
    return _DEFAULT


def memory_enabled() -> bool:
    """记忆层总开关（默认开）。`ACPS_SCOUT_MEMORY=0` 可关闭。"""
    return os.getenv("ACPS_SCOUT_MEMORY", "1").strip().lower() not in ("0", "false", "no", "off")


def to_records(rows: Iterable[Mapping[str, Any]], record_cls: type) -> list[Any]:
    """把记忆库行还原成 dataclass 记录（字段不匹配的部分自动忽略）。"""
    field_names = {f.name for f in dataclasses.fields(record_cls)} if dataclasses.is_dataclass(record_cls) else set()
    out: list[Any] = []
    for r in rows or ():
        kwargs: dict[str, Any] = {}
        for name in field_names:
            if name == "arxiv_id":
                val = r.get("arxiv_id") or ""
            elif name == "native_id":
                val = (r.get("source_ids") or "").split(":")[-1] or r.get("url") or ""
            elif name == "source":
                val = (r.get("sources") or "memory").split("; ")[0]
            elif name == "title":
                val = r.get("title") or ""
            elif name == "authors":
                val = [a for a in str(r.get("authors") or "").split("; ") if a]
            elif name == "published":
                val = str(r.get("year") or "")
            elif name == "year":
                val = r.get("year")
            elif name == "abstract":
                val = r.get("abstract") or ""
            elif name == "url":
                val = r.get("url") or ""
            elif name == "pdf_url":
                val = r.get("pdf_url") or ""
            elif name == "citations":
                val = r.get("citations")
            else:
                continue
            kwargs[name] = val
        try:
            out.append(record_cls(**kwargs))
        except TypeError:
            continue
    return out
