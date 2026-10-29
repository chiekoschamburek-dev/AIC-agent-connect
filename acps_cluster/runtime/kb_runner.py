"""知识库执行底座：数据不动、计算动——查询在本机执行，只回结果；字段级脱敏。

分层：本模块是**策略层**（谁能看什么、什么算合法查询），`kb_store.py` 是**存储层**
（SQLite，过滤/排序/分页/聚合下推）。受限 DSL（JSON 条件 + 白名单聚合函数）表达
良定义操作，不需要任意代码执行面——天然无注入面，比沙盒更轻更安全。

分级粒度比评测更细一档：

- 评测：资产级（PEP 准入）——整个 benchmark 要不要放行
- 知识库：资产级（PEP）+ **行级**（where 过滤）+ **字段级**（按调用方角色脱敏）

字段档位（FIELDS 声明表，单一真源：档位与类型都在这张表里，存储列由它生成）：
open（目录信息）/ members（摘要）/ curator（全文与内部笔记）。角色→档位：
provider→curator，consumer→members，auditor/匿名→open。请求了无权字段时
**静默丢弃并在响应 meta 里列明**（透明但不给内容）。

配额语义：查询次数（PEP grant 的 max_uses）+ **单查询行数上限**（防整库导出）。
行数上限保持 25 不变，配套的是 `offset`+`sort`：上限只限"一次拿多少"，不再决定
"最多能拿到多少"——旧实现无 offset，limit≤25 叠加后库超过 25 条的部分根本不可达。
"""

from __future__ import annotations

import os
import random
import re
import time
from pathlib import Path
from typing import Any

from acps_cluster.core.arg_shapes import as_list  # 工具入参数组宽容化（provider 把 array 送成 JSON 字符串）
from acps_cluster.runtime.kb_store import KBStore, StoreError, fingerprint, search_terms

CLUSTER_DIR = Path(__file__).resolve().parents[1]
KB_PATH = CLUSTER_DIR / "ws-kb" / "kb.sqlite3"
KB_PATH_ENV = "ACPS_KB_PATH"  # 演示/测试指向独立库，不碰现网 ws-kb


def default_kb_path() -> Path:
    return Path(os.getenv(KB_PATH_ENV) or KB_PATH)

MAX_ROWS_PER_QUERY = 25
MAX_RECORDS = 50_000
MAX_INGEST_BATCH = 200
_WHERE_OPS = {"==", "!=", ">", ">=", "<", "<=", "contains", "in"}
_AGG_FNS = {"count", "min", "max", "avg", "sum"}
_SAFE_FIELD = re.compile(r"^[a-z][a-z0-9_]*$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,63}$")

# 系统保留键：由 ingest 自己盖章（记录是谁、何时写入），不接受调用方填写，
# 也不出现在任何查询投影里。导出→再导入是资源库的正常回路，所以这里宽容而不是拒绝。
RESERVED_KEYS = ("_provenance",)

# 资源类型：知识库装的不只是论文——集群自己跑出来的产物同样要能被登记和检索
KINDS = ("paper", "dataset", "model", "artifact", "report", "code", "note")

# 字段 → (类型, 档位)。新增字段只改这张表，存储列与校验随之成立。
FIELDS: dict[str, tuple[str, str]] = {
    "id": ("text", "open"),
    "title": ("text", "open"),
    "year": ("int", "open"),
    "venue": ("text", "open"),
    "tags": ("list", "open"),
    "kind": ("text", "open"),
    "source_uri": ("text", "open"),   # 原文出处：DOI / arXiv 链接
    "source": ("text", "open"),       # 元数据来源（openalex / arxiv / 本馆编目）——不是持有方
    "license": ("text", "open"),
    "owner_aic": ("text", "open"),    # 馆藏持有方：KB 自己的机构身份，不是数据来源
    "retrieved_at": ("text", "open"),
    "abstract": ("text", "members"),
    "full_text": ("text", "curator"),
    "internal_note": ("text", "curator"),
}
FIELD_TIERS = {name: tier for name, (kind, tier) in FIELDS.items()}
FIELD_TYPES = {name: kind for name, (kind, tier) in FIELDS.items()}

_TIER_RANK = {"open": 0, "members": 1, "curator": 2}
_ROLE_TIERS = {"provider": "curator", "consumer": "members", "auditor": "open", "?": "open"}

# 全文检索**按档位分索引**，每档只索引该档调用方看得见的字段。
# 不能合成一张全字段索引：那样 bm25 得分与命中片段都会带上调用方看不见的正文，
# 等价于把 2026-10-07 封掉的"按受限字段过滤即取值预言机"从过滤面挪到检索面。
SEARCH_LANES: dict[str, list[str]] = {
    "open": ["title", "venue", "tags"],
    "members": ["abstract"],
    "curator": ["full_text", "internal_note"],
}
_TIER_LANES = {"open": ["open"], "members": ["open", "members"],
               "curator": ["open", "members", "curator"]}
# 摘录取"命中档里最高的那一档"的字段：provider 命中全文就给全文片段（它本来就有权看）
_LANE_EXCERPT_FIELDS = {"curator": ("full_text", "internal_note"),
                        "members": ("abstract",),
                        "open": ("title", "venue", "tags")}
MAX_QUERY_CHARS = 200
_EXCERPT_RADIUS = 120
_EXCERPT_MAX = 320

# 幂等导入的去重身份：有出处按出处，没出处按标题+年份
_IDENTITY_BY_URI = ("kind", "source_uri")
_IDENTITY_BY_TITLE = ("kind", "title", "year")


def tier_of_role(role: str | None) -> str:
    return _ROLE_TIERS.get(str(role or "?"), "open")


def visible_fields(role: str | None) -> frozenset[str]:
    limit = _TIER_RANK[tier_of_role(role)]
    return frozenset(n for n, (_, tier) in FIELDS.items() if _TIER_RANK[tier] <= limit)


# ─────────────────── 库的打开与初始化 ───────────────────

_stores: dict[str, KBStore] = {}


def open_store(path: Path | None = None) -> KBStore:
    """按路径复用连接（WAL）——查询密度上来后每条查询都重开库不划算。"""
    target = str(path or default_kb_path())
    store = _stores.get(target)
    if store is None:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        store = KBStore(target, FIELD_TYPES, lanes=SEARCH_LANES)
        _stores[target] = store
    return store


def record_count(path: Path | None = None) -> int:
    return open_store(path).count()


def close_store(path: Path | None = None) -> None:
    """释放连接：备份/换库时用；Windows 上临时目录的删除也要求连接已关。"""
    target = str(path or default_kb_path())
    store = _stores.pop(target, None)
    if store is not None:
        store.close()


_TOPICS = [
    ("图神经网络的知识图谱补全", ["GNN", "KGC"], "TKDE"),
    ("稀疏注意力下的长上下文建模", ["LLM", "attention"], "NeurIPS"),
    ("多智能体系统的通信高效训练", ["MAS", "training"], "ICML"),
    ("基于检索增强的领域问答", ["RAG", "QA"], "ACL"),
    ("图对比学习的自监督预训练", ["GNN", "pretrain"], "ICLR"),
    ("知识图谱嵌入的可解释性分析", ["KGE", "XAI"], "AAAI"),
]


def ensure_seed_kb(*, records: int = 60, seed: int = 7, path: Path | None = None) -> Path:
    """确定性合成库：**只供单测与 K 系列演示**——真实库用 kb_admin.py import 灌真实语料。

    已有记录的库不会被覆盖（否则演示会静默吃掉现网数据）。
    """
    target = Path(path or default_kb_path())
    store = open_store(target)
    if store.count() == 0:
        rng = random.Random(seed)
        rows = []
        for i in range(records):
            topic, tags, venue = _TOPICS[i % len(_TOPICS)]
            rows.append({
                "kind": "paper",
                "title": f"{topic}（研究 {i // len(_TOPICS) + 1} 号）",
                "year": 2019 + (i + seed) % 7,
                "venue": venue,
                "tags": tags,
                "abstract": f"本文研究{topic}。提出方法 M{i}，在标准基准上取得一致改进……（合成摘要 {i}）",
                "full_text": f"<机构订阅全文，仅策展方可下发。合成正文 {i}，共 {800 + i} 词……>",
                "internal_note": f"内部评审分 {round(rng.uniform(1, 5), 1)}；订阅到期 {2027 if i % 2 else 2026} 年",
            })
        store.insert([
            (None, fingerprint(row, identity_keys=_IDENTITY_BY_TITLE), row) for row in rows
        ])
    return target


def _page(raw: dict) -> tuple[str | None, int, int]:
    """limit/offset 解析（filter 与 search 共用同一套行数上限口径）。"""
    limit = raw.get("limit")
    try:
        limit = MAX_ROWS_PER_QUERY if limit is None else int(limit)
    except (TypeError, ValueError):
        return "limit 必须是整数", 0, 0
    if limit < 1 or limit > MAX_ROWS_PER_QUERY:
        return f"limit 必须在 1..{MAX_ROWS_PER_QUERY}（单查询行数上限，防整库导出）", 0, 0
    offset = raw.get("offset")
    try:
        offset = 0 if offset is None else int(offset)
    except (TypeError, ValueError):
        return "offset 必须是整数", 0, 0
    if offset < 0:
        return "offset 不能为负", 0, 0
    return None, int(offset), int(limit)


def _window(value: str, terms: list[str]) -> str | None:
    """围绕第一个命中的词取有界片段；不含任何词时返回 None。"""
    low = value.lower()
    hit = -1
    for term in terms:
        pos = low.find(term.lower())
        if pos >= 0 and (hit < 0 or pos < hit):
            hit = pos
    if hit < 0:
        return None
    start = max(0, hit - _EXCERPT_RADIUS)
    end = min(len(value), hit + _EXCERPT_RADIUS)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(value) else ""
    return (prefix + value[start:end].strip() + suffix)[:_EXCERPT_MAX + 8]


def _excerpt(lanes: list[str], record: dict, terms: list[str]) -> str | None:
    """片段只从**命中档**的字段里取（命中档已由角色筛过，越不了权）。

    从高往低找：真正含查询词的字段才当片段来源——否则总是标题先"命中"，
    用户拿到的就是标题复读，检索等于没做。
    """
    for lane in ("curator", "members", "open"):
        if lane not in lanes:
            continue
        for field in _LANE_EXCERPT_FIELDS[lane]:
            value = record.get(field)
            if isinstance(value, list):
                value = " ".join(str(v) for v in value)
            if not isinstance(value, str) or not value.strip():
                continue
            cut = _window(value, terms)
            if cut:
                return cut
    return None


# ─────────────────── 校验 ───────────────────


def _validate_conditions(where: Any, allowed: frozenset[str]) -> str | None:
    if not isinstance(where, list):
        return "where 必须是条件数组（可省略）"
    for i, cond in enumerate(where):
        if not isinstance(cond, dict):
            return f"where[{i}] 必须是对象"
        field, op = cond.get("field"), cond.get("op")
        if not isinstance(field, str) or field not in FIELDS:
            return f"where[{i}].field={field!r} 不在 schema 中"
        # 不能按"看不见值的字段"过滤：投影里被脱敏丢弃了，matched 计数却照样精确——
        # 逐字符 contains + 二分即可把 internal_note 的原文还原出来（2026-10-07 审查修复）
        if field not in allowed:
            return (f"where[{i}].field={field!r} 是受限字段（档位 {FIELD_TIERS.get(field)}，"
                    f"当前角色无权读取其值，也不能据它过滤）")
        if not isinstance(op, str) or op not in _WHERE_OPS:
            return f"where[{i}].op={op!r} 不在白名单 {sorted(_WHERE_OPS)}"
        if "value" not in cond:
            return f"where[{i}] 缺 value"
    return None


# ─────────────────── 查询（filter / compute / get / search 四模式） ───────────────────


def query(raw: dict, *, role: str | None, path: Path | None = None) -> dict:
    """受限 DSL 查询。filter=分页取行（脱敏+行上限）；compute=聚合下推；
    get=按 id 取单条；search=按档位分索引的全文检索（有界摘录，不下发整段正文）。"""
    store = open_store(path)
    if not isinstance(raw, dict):
        return {"ok": False, "error": "查询规格必须是对象"}
    mode = str(raw.get("mode") or "filter")
    where = raw.get("where") or []
    allowed = visible_fields(role)
    error = _validate_conditions(where, allowed)
    if error:
        return {"ok": False, "error": error}

    if mode == "get":
        record_id = raw.get("id")
        if not isinstance(record_id, str) or not record_id.strip():
            return {"ok": False, "error": "get 模式需要 id"}
        requested = raw.get("fields")
        error = _validate_projection(requested, allowed)
        if error:
            return {"ok": False, "error": error}
        record = store.get(record_id.strip())
        if record is None:
            return {"ok": True, "mode": "get", "found": False, "id": record_id}
        row, dropped = _project(record, requested, allowed)
        return {"ok": True, "mode": "get", "found": True, "row": row, "redactedFields": dropped}

    if mode == "search":
        text = raw.get("query") if isinstance(raw.get("query"), str) else raw.get("q")
        if not isinstance(text, str) or not text.strip():
            return {"ok": False, "error": "search 模式需要 query（字符串）"}
        if len(text) > MAX_QUERY_CHARS:
            return {"ok": False, "error": f"query 长度上限 {MAX_QUERY_CHARS} 字符"}
        requested = raw.get("fields")
        error = _validate_projection(requested, allowed)
        if error:
            return {"ok": False, "error": error}
        page_error, offset, limit = _page(raw)
        if page_error:
            return {"ok": False, "error": page_error}
        _, dropped = _project({}, requested, allowed)

        lanes = _TIER_LANES[tier_of_role(role)]   # 只查角色有权的那几档——档间不合并正文
        terms = search_terms(text)
        try:
            result = store.search(text, lanes, where=where, offset=offset, limit=limit)
        except StoreError as exc:
            return {"ok": False, "error": str(exc)}
        hits = []
        for hit in result["hits"]:
            row, _ = _project(hit["payload"], requested, allowed)
            hits.append({**row, "id": hit["id"],
                         "rank": None if hit["rank"] is None else round(hit["rank"], 4),
                         "matchedIn": hit["lanes"],
                         "excerpt": _excerpt(hit["lanes"], hit["payload"], terms)})
        return {"ok": True, "mode": "search", "query": text, "matched": result["matched"],
                "returned": len(hits), "offset": offset, "searchLanes": lanes,
                "candidatesCapped": result["capped"], "hits": hits, "redactedFields": dropped,
                "note": f"全文检索按档位分索引（本次只查 {'+'.join(lanes)} 档）；excerpt 是命中片段的"
                        f"有界摘录（≤{_EXCERPT_MAX} 字），整段正文请用 get/filter 取行"
                        + ("；候选数触顶，matched 是下限" if result["capped"] else "")}

    if mode == "filter":
        requested = raw.get("fields")
        error = _validate_projection(requested, allowed)
        if error:
            return {"ok": False, "error": error}
        keep = ([f for f in requested if f in allowed] if requested
                else [f for f in FIELDS if f in allowed])
        dropped = sorted(set(requested) - set(keep)) if requested else sorted(set(FIELDS) - set(keep))

        page_error, offset, limit = _page(raw)
        if page_error:
            return {"ok": False, "error": page_error}

        sort = raw.get("sort")
        sort_field, sort_dir = None, "asc"
        if sort is not None:
            if isinstance(sort, str):
                sort = {"field": sort}
            if not isinstance(sort, dict):
                return {"ok": False, "error": 'sort 形如 {"field":"year","dir":"desc"}'}
            sort_field = sort.get("field")
            if not isinstance(sort_field, str) or sort_field not in FIELDS:
                return {"ok": False, "error": f"sort.field={sort_field!r} 不在 schema 中"}
            # 排序键同样是取值通道：按看不见的字段排序会把它的序关系透出来
            if sort_field not in allowed:
                return {"ok": False, "error": f"sort.field={sort_field!r} 是受限字段（角色无权据它排序）"}
            sort_dir = str(sort.get("dir") or "asc")
            if sort_dir.lower() not in ("asc", "desc"):
                return {"ok": False, "error": "sort.dir 只能是 asc 或 desc"}

        try:
            matched, page = store.select(where, sort_field=sort_field, sort_dir=sort_dir,
                                         offset=offset, limit=limit)
        except StoreError as exc:
            return {"ok": False, "error": str(exc)}
        rows = [{f: r.get(f) for f in keep} for r in page]
        has_more = offset + len(rows) < matched
        return {"ok": True, "mode": "filter", "matched": matched, "returned": len(rows),
                "offset": offset, "sort": {"field": sort_field, "dir": sort_dir} if sort_field else None,
                "rows": rows, "redactedFields": dropped, "hasMore": has_more,
                "note": "受限字段已按调用方角色脱敏；matched 是全库命中数，"
                        f"翻页用 offset（下一页 offset={offset + limit}）"}

    if mode == "compute":
        by = raw.get("groupby")
        if by is not None and (not isinstance(by, str) or by not in FIELDS):
            return {"ok": False, "error": f"groupby={by!r} 不在 schema 中"}
        if by is not None and FIELD_TIERS.get(by) != "open" and by not in allowed:
            return {"ok": False, "error": f"groupby={by!r} 是受限字段（分组键即取值，角色 {role} 无权）"}
        aggs = raw.get("aggs") or [{"fn": "count"}]
        if not isinstance(aggs, list) or not 1 <= len(aggs) <= 5:
            return {"ok": False, "error": "aggs 必须是 1..5 个聚合项"}
        for i, agg in enumerate(aggs):
            if not isinstance(agg, dict):
                return {"ok": False, "error": f"aggs[{i}] 必须是对象"}
            fn, field = agg.get("fn"), agg.get("field")
            if not isinstance(fn, str) or fn not in _AGG_FNS:
                return {"ok": False, "error": f"aggs[{i}].fn={fn!r} 不在白名单 {sorted(_AGG_FNS)}"}
            if field is not None and (not isinstance(field, str) or field not in FIELDS):
                return {"ok": False, "error": f"aggs[{i}].field={field!r} 不是合法字段名"}
            if fn != "count" and field is None:
                return {"ok": False, "error": f"aggs[{i}] 缺合法 field"}
            if field is not None and FIELD_TIERS.get(field) != "open" and field not in allowed:
                return {"ok": False, "error": f"aggs[{i}].field={field!r} 是受限字段（角色 {role} 无聚合权）"}
        try:
            matched, grouped = store.aggregate(
                where, by, [(str(a.get("fn") or "count"), a.get("field")) for a in aggs])
        except StoreError as exc:
            return {"ok": False, "error": str(exc)}
        table = []
        for key, values in grouped:
            row: dict[str, Any] = {by: key} if by else {}
            for agg, value in zip(aggs, values):
                fn, field = agg.get("fn") or "count", agg.get("field")
                row[f"{fn}({field})" if field else fn] = value
            table.append(row)
        return {"ok": True, "mode": "compute", "matched": matched, "groups": len(table),
                "table": table, "note": "计算下推：聚合在本机执行，原始行不离开知识库"
                                        f"（分组最多返回 {MAX_ROWS_PER_QUERY} 组）"}

    return {"ok": False, "error": f"未知模式 {mode!r}（filter | compute | get | search）"}


def _validate_projection(requested: Any, allowed: frozenset[str]) -> str | None:
    if requested is None:
        return None
    if not isinstance(requested, list):
        return "fields 必须是数组"
    for f in requested:
        if not isinstance(f, str) or f not in FIELDS:
            return f"fields 含未知字段 {f!r}（以 kb.schema 为准）"
    return None


def _project(record: dict, requested: Any, allowed: frozenset[str]) -> tuple[dict, list[str]]:
    """调用方须已用 _validate_projection 过一遍——这里只做档位投影。

    未指定 fields 时也把"因档位而没给"的字段列进 redactedFields：不给清单等于让调用方
    以为库里没有该字段，透明度的意义正在于此（与 filter 模式同口径）。
    """
    keep = [f for f in requested if f in allowed] if requested else [f for f in FIELDS if f in allowed]
    asked = set(requested) if requested else set(FIELDS)
    return {f: record.get(f) for f in keep}, sorted(asked - set(keep))


# ─────────────────── 受控写入（ingest） ───────────────────


def _field_type_error(name: str, value: Any) -> str | None:
    if value is None:
        return None  # 缺字段是真实语料的常态（源没摘要就没摘要），不是类型错误
    kind = FIELD_TYPES[name]
    if kind == "text" and not isinstance(value, str):
        return f"{name} 须为字符串"
    if kind == "int" and (not isinstance(value, int) or isinstance(value, bool)):
        return f"{name} 须为整数"
    if kind == "real" and not isinstance(value, (int, float)):
        return f"{name} 须为数值"
    if kind == "list":
        if not isinstance(value, list):
            return f"{name} 须为数组"
        if name == "tags":
            if len(value) > 8 or not all(isinstance(t, str) and len(t) <= 64 for t in value):
                return f"{name} 须为 ≤8 个短标签的数组"
    return None


def validate_ingest(records: Any) -> str | None:
    """校验知识库写入请求的字段、类型与规模。"""
    records = as_list(records)
    if not isinstance(records, list) or not records:
        return "records 必须是非空数组"
    if len(records) > MAX_INGEST_BATCH:
        return f"单批上限 {MAX_INGEST_BATCH} 条"
    for i, r in enumerate(records):
        if not isinstance(r, dict):
            return f"records[{i}] 必须是对象"
        title = r.get("title")
        if not isinstance(title, str) or not 3 <= len(title) <= 300:
            return f"records[{i}].title 长度须在 3..300"
        kind = r.get("kind")
        if kind is not None and kind not in KINDS:
            return f"records[{i}].kind={kind!r} 不在 {list(KINDS)}"
        rid = r.get("id")
        if rid is not None and not _SAFE_ID.match(str(rid)):
            return f"records[{i}].id={rid!r} 不合法（字母开头，≤64 字符，可含 . _ : -）"
        for field, value in r.items():
            if field in RESERVED_KEYS:
                continue
            if not _SAFE_FIELD.match(str(field)) or field not in FIELDS:
                return f"records[{i}] 含未知字段 {field!r}（以 kb.schema 为准）"
            error = _field_type_error(field, value)
            if error:
                return f"records[{i}].{error}"
    return None


def ingest(records: Any, *, submitter: str, path: Path | None = None) -> dict:
    """受控写入：校验 + 溯源（提交者/时间）+ 幂等去重 + 容量上限。每次写入都是审计事件。"""
    error = validate_ingest(records)
    if error:
        return {"ok": False, "error": error}
    records = as_list(records)
    store = open_store(path)
    existing = store.count()
    rows = list(records)
    if existing + len(rows) > MAX_RECORDS:
        return {"ok": False, "error": f"库容量上限 {MAX_RECORDS}（现 {existing}）"}
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    prepared = []
    for r in rows:
        incoming = r.get("_provenance")
        provenance = {"submittedBy": submitter, "ts": ts}
        if isinstance(incoming, dict) and incoming.get("submittedBy"):
            # 原始提交人不覆盖：迁移/再导入后仍要说得清"谁第一次把它交进来"——
            # 溯源是审计件，不能因为换了只手写入就改写历史。
            provenance["originally"] = {k: incoming[k] for k in ("submittedBy", "ts") if k in incoming}
        record = {
            # 空值不入库：真实语料半数字段缺失，存一堆 null 既占位又让投影噪音过大
            **{k: v for k, v in r.items() if k in FIELDS and k != "id" and v is not None},
            "_provenance": provenance,
        }
        keys = _IDENTITY_BY_URI if record.get("source_uri") else _IDENTITY_BY_TITLE
        prepared.append((r.get("id"), fingerprint(record, identity_keys=keys), record))
    try:
        added, duplicates = store.insert(prepared)
    except StoreError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "added": added, "duplicates": duplicates,
            "total": store.count(), "provenance": {"submittedBy": submitter, "ts": ts}}


def schema_card() -> dict:
    """公开橱窗：字段/类型/档位/规则——只有结构，没有内容。"""
    return {
        "fields": [{"name": f, "type": FIELD_TYPES[f], "tier": t} for f, t in FIELD_TIERS.items()],
        "kinds": list(KINDS),
        "tiers": {"open": "所有成员", "members": "consumer+", "curator": "仅 provider 侧"},
        "whereOps": sorted(_WHERE_OPS),
        "aggFns": sorted(_AGG_FNS),
        "modes": ["filter", "compute", "get", "search"],
        "search": {
            "lanes": SEARCH_LANES,
            "laneByRole": _TIER_LANES,
            "note": "全文检索按档位分索引：角色看不见的字段根本不进它那条检索通道，"
                    "得分与摘录都不含越权内容；trigram 分词（中英文一律 3 字符滑窗），"
                    "短于 3 字符的词用扫列兜底",
        },
        "limits": {"maxRowsPerQuery": MAX_ROWS_PER_QUERY, "maxIngestBatch": MAX_INGEST_BATCH,
                   "maxRecords": MAX_RECORDS, "maxQueryChars": MAX_QUERY_CHARS,
                   "excerptMaxChars": _EXCERPT_MAX},
        "note": "计算下推：compute 聚合在本机执行，原始行不下发；受限字段按角色脱敏；"
                "filter 支持 offset 翻页与 sort 排序，get 按 id 取单条，"
                "search 返回有界摘录（整段正文仍需 get/filter 且按档位放行）",
    }


# 扩展注记（不在本期实现）：累计行数账本（跨查询分页翻库的计量）接入 quota.sqlite，
# 与 max_uses（查询次数）构成双轨配额——语义同 RFC §4.7 的"次数/容量"之分。
# offset 上线后这条从"理论风险"变成"必要项"：翻页现在真的能翻到底。
