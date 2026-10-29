"""知识库存储层：SQLite 单文件库 + 过滤/排序/分页/聚合/全文检索下推。

策略层（kb_runner）管"谁能看哪些字段"，本模块只管"怎么把行取出来"。两道闸各自成立：
本模块只认建库时登记过的列名，未登记的字段直接拒——即使策略层漏了，这里也不会把
任意字符串拼进 SQL（列名取自白名单，值一律走参数绑定）。

检索按**档位分索引**（`lanes`）：open 列一张、members 列一张、curator 列一张。
不能把它们合成一张全字段索引——那样 `bm25` 得分和命中片段都会带着调用方看不见的
内容，等于把 2026-10-07 封掉的"按受限字段过滤即取值预言机"从过滤面挪到了检索面。

分词选 `trigram`：unicode61 会把整句中文切成一个 token（实测 "注意力" / "稀疏" 对
"稀疏注意力下的长上下文建模" 均 0 命中），trigram 对中英文一律按 3 字符滑窗，
中英混排的语料才查得动。代价是 trigram 最短 3 字符——2 字中文词（"稀疏""检索"）
MATCH 不成立，用 LIKE 扫列兜底（见 `search`）。

四点不显然的实现口径：
- 相等用 `IS`/`IS NOT` 而非 `=`/`!=`：`col != 2024` 在 col 为 NULL 时结果是 NULL（行被排除），
  而 Python 侧 `None != 2024` 是 True。真实语料里缺字段是常态，用 `=` 会让迁移后的
  命中数与旧实现不一致。
- `contains` 走 `LIKE ... ESCAPE`，先转义 `%`/`_`：调用方查询串里带百分号时不能变成通配符。
- 排序末位固定补 `rowid`：没有全序键时 offset 分页会跳过或重复行（分页正确性的前提）。
- 检索词逐个加双引号再 AND：裸拼会被 FTS5 当查询语法（`OR`/`*`/括号）解释。
"""

from __future__ import annotations

import hashlib
import json
import re
try:  # 服务器 Python 自带 SQLite 3.26（不支持生成列 / FTS5 trigram）
    import pysqlite3 as sqlite3  # type: ignore[import-not-found]
except ImportError:  # 本地/CI 用标准库（新版 SQLite）
    import sqlite3
import time
from pathlib import Path
from typing import Any, Iterator

_SAFE_FIELD = re.compile(r"^[a-z][a-z0-9_]*$")

# 字段声明 → SQLite 生成列类型。list 存 JSON 文本，contains 走 json_each。
_SQL_TYPE = {"text": "TEXT", "int": "INTEGER", "real": "REAL", "list": "TEXT"}

MAX_PAGE_SIZE = 25
MAX_IN_VALUES = 50
_LIKE_ESCAPES = str.maketrans({"%": "\\%", "_": "\\_", "\\": "\\\\"})

MIN_TRIGRAM = 3          # trigram 分词的最短可 MATCH 片段
MAX_SEARCH_TERMS = 8     # 再多词对检索质量没有意义，只放大扫描面
CANDIDATES_PER_LANE = 200
# 切词时一并剥掉 FTS5 的查询语法字符——留着它们，用户的一个引号就能把检索词
# 变成查询表达式（NEAR/OR/括号），那是语法注入面而不是检索需求。
# `_` 不剥：long_context 这类下划线词形在文献里常见。
_TERM_SPLIT = re.compile("[\\s,;、，；：:/\\-()（）\\[\\]{}\"'*^|+?!#%&=~<>`]+")


class StoreError(ValueError):
    """存储层拒绝（未知字段/未知算子/类型不符）——不放进 SQL 里猜。"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _fts_lane_ddl(lane: str, cols: list[str]) -> str:
    """一张档位索引 + 三条同步触发器（外部内容表不存正文，只存倒排）。

    触发器读的是生成列——正文只写进 records.payload 一处，索引跟着走，
    第 4 步加"改记录/删记录"时不会再出现库里和索引两个版本。
    """
    table = f"fts_{lane}"
    collist = ", ".join(cols)
    new_vals = ", ".join(f"new.{c}" for c in cols)
    old_vals = ", ".join(f"old.{c}" for c in cols)
    return (
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING fts5("
        f"{collist}, content='records', content_rowid='rowid', tokenize='trigram');\n"
        f"CREATE TRIGGER IF NOT EXISTS {table}_ai AFTER INSERT ON records BEGIN\n"
        f"  INSERT INTO {table}(rowid, {collist}) VALUES(new.rowid, {new_vals}); END;\n"
        f"CREATE TRIGGER IF NOT EXISTS {table}_ad AFTER DELETE ON records BEGIN\n"
        f"  INSERT INTO {table}({table}, rowid, {collist}) VALUES('delete', old.rowid, {old_vals}); END;\n"
        f"CREATE TRIGGER IF NOT EXISTS {table}_au AFTER UPDATE ON records BEGIN\n"
        f"  INSERT INTO {table}({table}, rowid, {collist}) VALUES('delete', old.rowid, {old_vals});\n"
        f"  INSERT INTO {table}(rowid, {collist}) VALUES(new.rowid, {new_vals}); END;\n"
    )


def _ddl(fields: dict[str, str]) -> str:
    cols = [
        "rowid INTEGER PRIMARY KEY AUTOINCREMENT",
        "id TEXT NOT NULL UNIQUE",
        "hash TEXT NOT NULL UNIQUE",
        "payload TEXT NOT NULL CHECK (json_valid(payload))",
        "created_at TEXT NOT NULL",
        "updated_at TEXT NOT NULL",
    ]
    for name, kind in fields.items():
        if not _SAFE_FIELD.match(name):
            raise StoreError(f"字段名 {name!r} 不合法")
        if name == "id":  # 主键列已存在，不再建生成列
            continue
        if kind not in _SQL_TYPE:
            raise StoreError(f"字段 {name!r} 类型 {kind!r} 未知")
        cols.append(
            f"{name} {_SQL_TYPE[kind]} GENERATED ALWAYS AS (json_extract(payload, '$.{name}')) STORED"
        )
    return (
        "CREATE TABLE IF NOT EXISTS records (\n  " + ",\n  ".join(cols) + "\n);\n"
        "CREATE INDEX IF NOT EXISTS records_created ON records(created_at);\n"
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);\n"
    )


def _lanes_ddl(lanes: dict[str, list[str]], fields: dict[str, str]) -> str:
    ddl = ""
    for lane, lane_cols in lanes.items():
        if not _SAFE_FIELD.match(lane):
            raise StoreError(f"索引档 {lane!r} 不合法")
        for col in lane_cols:
            if col not in fields:
                raise StoreError(f"索引档 {lane} 引用了未登记字段 {col!r}")
            if fields[col] not in ("text", "list"):
                raise StoreError(f"索引档 {lane} 的字段 {col} 不是文本/数组，不能全文索引")
        ddl += _fts_lane_ddl(lane, lane_cols)
    return ddl


def search_terms(text: str) -> list[str]:
    """切词：空白与常见标点分界，去空，截到 MAX_SEARCH_TERMS。"""
    return [t for t in _TERM_SPLIT.split(str(text or "").strip()) if t][:MAX_SEARCH_TERMS]


class KBStore:
    """一个库一个实例。`fields` 是 {字段名: 类型}，`lanes` 是 {档位: 可全文检索字段}，
    两者都由策略层声明——列与索引由此生成，单一真源。"""

    def __init__(self, path: Path | str, fields: dict[str, str],
                 *, lanes: dict[str, list[str]] | None = None):
        self.path = Path(path)
        self.fields = dict(fields)
        self.lanes = {k: list(v) for k, v in (lanes or {}).items()}
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.drift: dict[str, list[str]] = {"columns": [], "lanes": []}
        self.conn.executescript(_ddl(fields))
        self.conn.commit()
        # 顺序要紧：CREATE TRIGGER 时就会校验 NEW.<列> 是否存在，
        # 所以先按声明表补齐老库缺的生成列，再建索引档与触发器。
        self._heal_schema()
        self.conn.executescript(_lanes_ddl(self.lanes, fields))
        self.conn.commit()
        self._heal_indexes()

    def _heal_schema(self) -> None:
        """声明表加了字段 → 老库补列。

        `CREATE TABLE IF NOT EXISTS` 对**已存在**的表不生效，所以新增字段不会自动出现，
        查询会直接 `no such column`（现网库用新代码打开必踩）。**删字段不回收列**：那些列
        被既有触发器引着，SQLite 不许 drop，留着只是惰性数据；真要清库另走重建。

        补的是 **VIRTUAL** 生成列：SQLite 明确禁止 `ALTER TABLE ADD COLUMN … STORED`
        （实测报 cannot add a STORED column）。代价是 VIRTUAL 列不能建 B-tree 索引，
        过滤/排序按行算 json_extract——检索主路径走 FTS 倒排，不受影响。新建的库仍是
        STORED 并可索引，所以这条只影响"老库升上来"的那批列。
        """
        # 必须用 table_xinfo：`table_info` 根本不列生成列（实测 3.50 如此），按它判断会
        # 把每个字段都当成缺失，于是 duplicate column name。xinfo 里 hidden 的取值
        # 各版本不一致（STORED 在本机报 3），所以不按 hidden 过滤——出现的列就是存在的列。
        present = {row["name"] for row in self.conn.execute("PRAGMA table_xinfo(records)")}
        for name, kind in self.fields.items():
            if name == "id" or name in present:
                continue
            if not _SAFE_FIELD.match(name) or kind not in _SQL_TYPE:
                raise StoreError(f"字段 {name!r} 类型 {kind!r} 不合法，不能补列")
            self.conn.execute(
                f"ALTER TABLE records ADD COLUMN {name} {_SQL_TYPE[kind]} "
                f"GENERATED ALWAYS AS (json_extract(payload, '$.{name}')) VIRTUAL")
            self.drift["columns"].append(name)
        if self.drift["columns"]:
            self.conn.commit()

    def _lane_columns(self, lane: str) -> set[str] | None:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (f"fts_{lane}",)).fetchone()
        if row is None:
            return None
        return {r["name"] for r in self.conn.execute(f"PRAGMA table_info(fts_{lane})")}

    def _recreate_lane(self, lane: str) -> None:
        """索引档的列集合变了只能重建：FTS5 虚表不支持改列。"""
        table = f"fts_{lane}"
        self.conn.executescript(
            f"DROP TRIGGER IF EXISTS {table}_ai;\n"
            f"DROP TRIGGER IF EXISTS {table}_ad;\n"
            f"DROP TRIGGER IF EXISTS {table}_au;\n"
            f"DROP TABLE IF EXISTS {table};\n" + _fts_lane_ddl(lane, self.lanes[lane]))
        self.conn.commit()
        self.drift["lanes"].append(lane)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "KBStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ─────────────────── 索引维护 ───────────────────

    def _index_spec(self) -> str:
        """索引规格指纹：档位、列、分词器任一变化就必须重建。"""
        payload = json.dumps({"lanes": self.lanes, "tokenizer": "trigram"},
                             sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def _heal_indexes(self) -> None:
        """索引档列不一致先重建，再按规格标记决定是否 rebuild。

        不能用 `SELECT COUNT(*) FROM fts_x` 判断索引空不空——外部内容表的 COUNT 读的是
        **正文表**，索引全空时照样返回 1101（实测踩过：补索引逻辑恒不触发，检索全 0 命中）。
        """
        if not self.lanes:
            return
        for lane, cols in self.lanes.items():
            if self._lane_columns(lane) != set(cols):
                self._recreate_lane(lane)
        spec = self._index_spec()
        row = self.conn.execute("SELECT value FROM meta WHERE key='fts_spec'").fetchone()
        if row is not None and row[0] == spec and not self.drift["lanes"]:
            return
        self.reindex()
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES('fts_spec',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (spec,))
        self.conn.commit()

    def reindex(self) -> list[str]:
        """从 records 表重建全部倒排（幂等，随时可跑；绕过触发器的写入后必须跑这个）。"""
        for lane in self.lanes:
            self.conn.execute(f"INSERT INTO fts_{lane}(fts_{lane}) VALUES('rebuild')")
        self.conn.commit()
        spec = self._index_spec()
        self.conn.execute("INSERT INTO meta(key,value) VALUES('fts_spec',?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (spec,))
        self.conn.commit()
        return list(self.lanes)

    # ─────────────────── 取数 ───────────────────

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM records").fetchone()[0])

    def get(self, record_id: str) -> dict | None:
        row = self.conn.execute("SELECT payload FROM records WHERE id = ?", (record_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def all_records(self) -> Iterator[dict]:
        """全量遍历：供策展/迁移/自测用，不经过档位投影——调用方必须是 provider 侧。"""
        for row in self.conn.execute("SELECT payload FROM records ORDER BY rowid"):
            yield json.loads(row["payload"])

    def iter_rows(self) -> Iterator[tuple[int, dict]]:
        """带 rowid 的全量遍历：策展批改要按 rowid 写回，外部内容索引才跟得上。"""
        for row in self.conn.execute("SELECT rowid, payload FROM records ORDER BY rowid"):
            yield int(row["rowid"]), json.loads(row["payload"])

    def replace_payload(self, rowid: int, record: dict) -> None:
        """按 rowid 换整条记录；三档倒排由 AFTER UPDATE 触发器一并换掉。

        只给策展/迁移用——对外没有 update 通道（改内容不是"再写入一次"，指纹会把它当新记录）。
        """
        self.conn.execute(
            "UPDATE records SET payload = ?, updated_at = ? WHERE rowid = ?",
            (json.dumps(record, ensure_ascii=False, sort_keys=True), _now(), int(rowid)))

    def commit(self) -> None:
        self.conn.commit()

    # ─────────────────── 谓词编译 ───────────────────

    def _column(self, field: Any) -> str:
        if not isinstance(field, str) or field not in self.fields:
            raise StoreError(f"字段 {field!r} 不在库的列白名单里")
        return field

    def _compile(self, cond: dict, alias: str = "") -> tuple[str, list[Any]]:
        col = self._column(cond.get("field"))
        ref = f"{alias}{col}"
        op, value = cond.get("op"), cond.get("value")
        kind = self.fields[col]
        if op == "==":
            return f"{ref} IS ?", [value]
        if op == "!=":
            return f"{ref} IS NOT ?", [value]
        if op in (">", ">=", "<", "<="):
            # 跨类型比较在 SQL 里按类型序成立（'a' > 5 为真），Python 侧是 TypeError→False。
            # 与其静默改语义，不如在这里拒绝：调用方拿到明确文案而不是意外结果。
            want = {"int": int, "real": (int, float), "text": str}.get(kind)
            if want is not None and not isinstance(value, want):
                raise StoreError(f"字段 {col} 是 {kind} 型，{op} 的 value 必须是 {kind} 型")
            return f"{ref} {op} ?", [value]
        if op == "contains":
            if kind == "list":
                return f"EXISTS(SELECT 1 FROM json_each({ref}) WHERE value IS ?)", [value]
            if kind != "text":
                raise StoreError(f"字段 {col} 不支持 contains")
            return f"lower(IFNULL({ref},'')) LIKE '%'||lower(?)||'%' ESCAPE '\\'", [
                str(value).translate(_LIKE_ESCAPES)
            ]
        if op == "in":
            values = value if isinstance(value, list) else None
            if not values or len(values) > MAX_IN_VALUES:
                raise StoreError(f"in 的 value 必须是 1..{MAX_IN_VALUES} 个元素的数组")
            return "(" + " OR ".join([f"{ref} IS ?"] * len(values)) + ")", list(values)
        raise StoreError(f"算子 {op!r} 不支持")

    def _where(self, where: list[dict], alias: str = "") -> tuple[list[str], list[Any]]:
        conds, params = [], []
        for cond in where:
            sql, p = self._compile(cond, alias)
            conds.append(f"({sql})")
            params.extend(p)
        return conds, params

    @staticmethod
    def _filter_sql(conds: list[str], *, keyword: str = "WHERE") -> str:
        """检索路径已经有 `MATCH ?` 占着 WHERE，条件得用 AND 接上去——
        统一成 WHERE 会拼出双 WHERE。"""
        return f" {keyword} " + " AND ".join(conds) if conds else ""

    # ─────────────────── 查询（分页 + 稳定排序） ───────────────────

    def select(self, where: list[dict], *, sort_field: str | None = None,
               sort_dir: str = "asc", offset: int = 0,
               limit: int = MAX_PAGE_SIZE) -> tuple[int, list[dict]]:
        """返回 (全库命中数, 本页行)。offset 让 matched 之外的行也可达——旧实现只有
        matched[:limit]，limit 上限 25 时 matched=62 的后 37 条永远取不到。"""
        conds, params = self._where(where)
        clause = self._filter_sql(conds)
        matched = int(self.conn.execute(
            f"SELECT COUNT(*) FROM records{clause}", params).fetchone()[0])
        if sort_field is not None:
            col = self._column(sort_field)
            direction = "DESC" if str(sort_dir).lower().startswith("desc") else "ASC"
            order = f" ORDER BY {col} {direction} NULLS LAST, rowid ASC"
        else:
            order = " ORDER BY rowid ASC"
        rows = self.conn.execute(
            f"SELECT payload FROM records{clause}{order} LIMIT ? OFFSET ?",
            [*params, int(limit), int(offset)],
        ).fetchall()
        return matched, [json.loads(r["payload"]) for r in rows]

    def aggregate(self, where: list[dict], groupby: str | None,
                  aggs: list[tuple[str, str | None]]) -> tuple[int, list[tuple[Any, list[Any]]]]:
        """聚合下推：count/min/max/avg/sum 全在库内完成，行不出存储层（更不出机器）。"""
        conds, params = self._where(where)
        clause = self._filter_sql(conds)
        key = self._column(groupby) if groupby else None
        selects = []
        for fn, field in aggs:
            if fn == "count":
                selects.append("COUNT(*)")
                continue
            col = self._column(field)
            if self.fields[col] not in ("int", "real"):
                raise StoreError(f"字段 {col} 不是数值型，不能 {fn}")
            selects.append(f"{fn.upper()}({col})")
        grouped = (
            f"SELECT {key} AS gkey, {', '.join(selects)} FROM records{clause}"
            f" GROUP BY {key} ORDER BY gkey NULLS LAST LIMIT ?"
            if key else
            f"SELECT NULL AS gkey, {', '.join(selects)} FROM records{clause} LIMIT 1"
        )
        # 无 groupby 时 SQL 里是字面量 LIMIT 1，没有占位符；不能再绑定 MAX_PAGE_SIZE。
        bind = [*params, MAX_PAGE_SIZE] if key else list(params)
        raw = self.conn.execute(grouped, bind).fetchall()
        matched = int(self.conn.execute(
            f"SELECT COUNT(*) FROM records{clause}", params).fetchone()[0])
        table = []
        for row in raw:
            values = []
            for i, (fn, _) in enumerate(aggs, start=1):
                v = row[i]
                values.append(None if v is None else (round(v, 2) if fn == "avg" else v))
            table.append((row[0], values))
        return matched, table

    # ─────────────────── 全文检索（按档位分索引） ───────────────────

    def _lane_terms(self, text: str) -> tuple[list[str], list[str]]:
        """切词 → (可 MATCH 的词, 全部词)。trigram 下限之下的词只能走 LIKE。"""
        terms = search_terms(text)
        return [t for t in terms if len(t) >= MIN_TRIGRAM], terms

    def _search_lane(self, lane: str, match: str, conds: list[str], params: list[Any]) -> list[sqlite3.Row]:
        table = f"fts_{lane}"
        extra = self._filter_sql(conds, keyword="AND")
        return self.conn.execute(
            f"SELECT rec.id AS id, bm25({table}) AS rank, rec.payload AS payload "
            f"FROM {table} JOIN records rec ON rec.rowid = {table}.rowid "
            f"WHERE {table} MATCH ?{extra} ORDER BY rank LIMIT ?",
            [match, *params, CANDIDATES_PER_LANE],
        ).fetchall()

    def _like_lane(self, lane: str, term: str, conds: list[str], params: list[Any]) -> list[sqlite3.Row]:
        """trigram 下限（3 字符）之下的短词：只能扫该档的列。

        短词在中文里很常见（"稀疏""检索""图算"），拒绝它们等于中文用户查不动——
        代价是无索引扫描，所以同样受 CANDIDATES_PER_LANE 截断。
        """
        cols = self.lanes[lane]
        like = "(" + " OR ".join(f"lower(IFNULL(rec.{c},'')) LIKE ? ESCAPE '\\'" for c in cols) + ")"
        tail = self._filter_sql(conds, keyword="AND")
        pattern = f"%{term.translate(_LIKE_ESCAPES).lower()}%"
        return self.conn.execute(
            f"SELECT rec.id AS id, NULL AS rank, rec.payload AS payload FROM records rec"
            f" WHERE {like}{tail} ORDER BY rec.rowid LIMIT ?",
            [pattern] * len(cols) + [*params, CANDIDATES_PER_LANE],
        ).fetchall()

    def search(self, text: str, lanes: list[str], *, where: list[dict] | None = None,
               offset: int = 0, limit: int = MAX_PAGE_SIZE) -> dict:
        """返回 {matched, capped, offset, hits:[{id,rank,lanes,payload}]}。

        只查传进来的档位——档位由策略层按角色决定，这里不看角色。AND 无果退 OR 是
        为了"多词检索"的召回；两档以上命中同一条时取最好（最小）的 bm25 分。
        bm25 分跨表不完全可比（各表词长分布不同），所以只做同一档内的排序与档间
        的粗略合并，不宣称全局相关度。
        """
        where = where or []
        conds, params = self._where(where, alias="rec.")
        matchable, all_terms = self._lane_terms(text)
        merged: dict[str, dict] = {}
        capped = False
        for lane in lanes:
            if lane not in self.lanes:
                raise StoreError(f"检索档 {lane!r} 未建索引")
            if matchable:
                quoted = [f'"{t}"' for t in matchable]
                rows = self._search_lane(lane, " ".join(quoted), conds, params)
                if not rows and len(quoted) > 1:      # 多词 AND 过严时退 OR 保召回
                    rows = self._search_lane(lane, " OR ".join(quoted), conds, params)
            else:
                short = (all_terms[0] if all_terms else str(text or "")).strip()
                rows = self._like_lane(lane, short, conds, params) if short else []
            capped = capped or len(rows) >= CANDIDATES_PER_LANE
            for row in rows:
                hit = merged.setdefault(row["id"], {"id": row["id"], "rank": None,
                                                    "lanes": [], "payload": None})
                hit["lanes"].append(lane)
                if hit["payload"] is None:
                    hit["payload"] = json.loads(row["payload"])
                if row["rank"] is not None and (hit["rank"] is None or row["rank"] < hit["rank"]):
                    hit["rank"] = row["rank"]
        for hit in merged.values():
            hit["lanes"] = sorted(set(hit["lanes"]))
        ordered = sorted(merged.values(),
                         key=lambda h: (h["rank"] is None, h["rank"] or 0.0, str(h["id"])))
        return {"matched": len(ordered), "capped": capped, "offset": offset,
                "hits": ordered[offset:offset + int(limit)]}

    # ─────────────────── 写入 ───────────────────

    def _next_id(self) -> str:
        row = self.conn.execute("SELECT value FROM meta WHERE key='seq'").fetchone()
        seq = int(row["value"]) + 1 if row else 1
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES('seq',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(seq),),
        )
        return f"KB{seq:06d}"

    def insert(self, prepared: list[tuple[str | None, str, dict]]) -> tuple[list[str], list[dict]]:
        """prepared = [(指定 id 或 None, 内容指纹, 记录)]；单事务写入。

        指纹命中即跳过——重复导入同一批文献不再产生第二份记录（幂等）。
        id 由调用方给定时用于外部稳定编号（如 arXiv id）。
        """
        added, duplicates = [], []
        ts = _now()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            for want_id, digest, record in prepared:
                row = self.conn.execute("SELECT id FROM records WHERE hash=?", (digest,)).fetchone()
                if row:
                    duplicates.append({"incoming": record.get("title"), "id": row["id"]})
                    continue
                rid = want_id or self._next_id()
                if self.conn.execute("SELECT 1 FROM records WHERE id=?", (rid,)).fetchone():
                    raise StoreError(f"id {rid!r} 已存在")
                record = {**record, "id": rid}
                self.conn.execute(
                    "INSERT INTO records(id,hash,payload,created_at,updated_at) VALUES(?,?,?,?,?)",
                    (rid, digest, json.dumps(record, ensure_ascii=False, sort_keys=True), ts, ts),
                )
                added.append(rid)
            self.conn.commit()
        except sqlite3.IntegrityError as exc:
            self.conn.rollback()
            raise StoreError(f"写入冲突：{exc}") from exc
        except Exception:
            self.conn.rollback()
            raise
        return added, duplicates


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def normalize_uri(value: str) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"^https?://", "", text)
    return text.rstrip("/")


def fingerprint(record: dict, *, identity_keys: tuple[str, ...]) -> str:
    """内容指纹：按 identity_keys 取值归一化后哈希，供幂等导入。

    不做归一化的话，同一篇论文标题多一个空格就存两份——真实语料导入必然发生。
    """
    parts = []
    for key in identity_keys:
        value = record.get(key)
        if isinstance(value, str):
            value = normalize_uri(value) if key.endswith("_uri") else normalize_text(value)
        parts.append(f"{key}={value}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
