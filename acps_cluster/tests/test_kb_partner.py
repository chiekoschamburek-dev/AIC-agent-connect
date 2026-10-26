"""知识库离线单测：DSL 校验/字段级脱敏/行上限与翻页/聚合下推/受控写入与幂等。

    python -m unittest acps_cluster.tests.test_kb_partner -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

CLUSTER_DIR = Path(__file__).resolve().parents[1]
for _p in (str(CLUSTER_DIR.parent), str(CLUSTER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from acps_cluster.runtime import kb_runner  # noqa: E402


def _db(tmp: str) -> Path:
    return Path(tmp) / "kb.sqlite3"


def _stored(path: Path) -> list[dict]:
    """策展视角全量遍历（不经档位投影）——只有 provider 侧的自测才该这么读。"""
    return list(kb_runner.open_store(path).all_records())


class SeedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = _db(self.tmp.name)
        kb_runner.ensure_seed_kb(records=60, seed=7, path=self.path)

    def tearDown(self):
        kb_runner.close_store(self.path)  # Windows：连接不关，临时目录删不掉
        self.tmp.cleanup()

    def test_种子确定性(self):
        again = Path(self.tmp.name) / "kb2.sqlite3"
        kb_runner.ensure_seed_kb(records=60, seed=7, path=again)
        try:
            self.assertEqual(_stored(self.path), _stored(again))
        finally:
            kb_runner.close_store(again)  # 同目录下的第二个库，tearDown 只关 self.path

    def test_种子不被二次覆盖(self):
        kb_runner.ensure_seed_kb(records=5, seed=99, path=self.path)
        self.assertEqual(kb_runner.record_count(self.path), 60)

    def test_种子含受限字段内容(self):
        record = _stored(self.path)[0]
        self.assertIn("full_text", record)      # curator 档存在
        self.assertIn("internal_note", record)  # 内部笔记存在（但不下发，见下）


class FieldTierTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = _db(self.tmp.name)
        kb_runner.ensure_seed_kb(records=30, seed=3, path=self.path)

    def tearDown(self):
        kb_runner.close_store(self.path)  # Windows：连接不关，临时目录删不掉
        self.tmp.cleanup()

    def test_consumer不见curator字段(self):
        result = kb_runner.query(
            {"mode": "filter", "fields": ["id", "title", "full_text", "internal_note"], "limit": 5},
            role="consumer", path=self.path)
        self.assertTrue(result["ok"])
        self.assertIn("full_text", result["redactedFields"])
        self.assertIn("internal_note", result["redactedFields"])
        for row in result["rows"]:
            self.assertNotIn("full_text", row)

    def test_匿名只有open档(self):
        result = kb_runner.query(
            {"mode": "filter", "fields": ["abstract"], "limit": 3}, role=None, path=self.path)
        self.assertIn("abstract", result["redactedFields"])  # members 档对匿名脱敏

    def test_provider策可见全部(self):
        result = kb_runner.query(
            {"mode": "filter", "fields": ["id", "full_text", "internal_note"], "limit": 1},
            role="provider", path=self.path)
        self.assertEqual(result["redactedFields"], [])
        self.assertIn("full_text", result["rows"][0])


class QueryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = _db(self.tmp.name)
        kb_runner.ensure_seed_kb(records=60, seed=7, path=self.path)

    def tearDown(self):
        kb_runner.close_store(self.path)  # Windows：连接不关，临时目录删不掉
        self.tmp.cleanup()

    def test_条件过滤命中(self):
        result = kb_runner.query(
            {"mode": "filter", "where": [{"field": "year", "op": ">=", "value": 2023}], "limit": 25},
            role="consumer", path=self.path)
        self.assertTrue(result["ok"])
        self.assertGreater(result["matched"], 0)
        self.assertLessEqual(result["returned"], kb_runner.MAX_ROWS_PER_QUERY)

    def test_非法op拒绝(self):
        result = kb_runner.query(
            {"mode": "filter", "where": [{"field": "year", "op": "drop table", "value": 1}]},
            role="consumer", path=self.path)
        self.assertFalse(result["ok"])

    def test_受限字段不能当过滤条件(self):
        """投影里被脱敏丢弃的字段，若还能用来过滤，matched 计数就是取值预言机：
        逐字符 contains + 二分可把 internal_note 原文还原出来（2026-10-07 审查修复）。"""
        note = next(r["internal_note"] for r in _stored(self.path)
                    if r.get("internal_note"))
        for frag in (note[:1], note[:4], note):
            result = kb_runner.query(
                {"mode": "filter", "where": [{"field": "internal_note", "op": "contains", "value": frag}],
                 "fields": ["id"], "limit": 25}, role="consumer", path=self.path)
            self.assertFalse(result["ok"], frag)
            self.assertIn("受限字段", result["error"])
        compute = kb_runner.query(
            {"mode": "compute", "where": [{"field": "internal_note", "op": "==", "value": note}],
             "aggs": [{"fn": "count"}]}, role="consumer", path=self.path)
        self.assertFalse(compute["ok"])
        # provider 看得见该字段，过滤照常
        allowed = kb_runner.query(
            {"mode": "filter", "where": [{"field": "internal_note", "op": "contains", "value": note[:4]}],
             "fields": ["id"], "limit": 25}, role="provider", path=self.path)
        self.assertTrue(allowed["ok"], allowed)
        self.assertGreater(allowed["matched"], 0)

    def test_非法字段拒绝(self):
        result = kb_runner.query(
            {"mode": "filter", "where": [{"field": "../escape", "op": "==", "value": 1}]},
            role="consumer", path=self.path)
        self.assertFalse(result["ok"])

    def test_行上限防整库导出(self):
        result = kb_runner.query({"mode": "filter", "limit": 9999}, role="consumer", path=self.path)
        self.assertFalse(result["ok"])
        self.assertIn("行数上限", result["error"])

    def test_compute聚合行不下发(self):
        result = kb_runner.query(
            {"mode": "compute", "groupby": "venue",
             "aggs": [{"fn": "count"}, {"fn": "avg", "field": "year"}]},
            role="consumer", path=self.path)
        self.assertTrue(result["ok"])
        blob = str(result["table"])
        self.assertNotIn("abstract", blob)   # 原始行不出现
        self.assertNotIn("full_text", blob)
        venues = {row["venue"] for row in result["table"]}
        self.assertTrue(venues)  # 聚合表有内容

    def test_无groupby全库总量聚合(self):
        result = kb_runner.query(
            {"mode": "compute", "aggs": [{"fn": "count"}]},
            role="provider", path=self.path)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["table"][0]["count"], kb_runner.record_count(self.path))

    def test_无groupby带where聚合(self):
        where = [{"field": "year", "op": ">=", "value": 2023}]
        result = kb_runner.query(
            {"mode": "compute", "where": where, "aggs": [{"fn": "count"}]},
            role="provider", path=self.path)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["table"][0]["count"], result["matched"])
        self.assertGreater(result["matched"], 0)

    def test_count字段为数组时拒绝而非抛TypeError(self):
        result = kb_runner.query(
            {"mode": "compute", "aggs": [{"fn": "count", "field": ["year"]}]},
            role="consumer", path=self.path)
        self.assertFalse(result["ok"])
        self.assertIn("不是合法字段名", result["error"])

    def test_受限字段的聚合拒绝(self):
        result = kb_runner.query(
            {"mode": "compute", "groupby": "internal_note", "aggs": [{"fn": "count"}]},
            role="consumer", path=self.path)
        self.assertFalse(result["ok"])  # 按受限字段分组=变相读值，拒

    def test_未知模式拒绝(self):
        result = kb_runner.query({"mode": "dump"}, role="provider", path=self.path)
        self.assertFalse(result["ok"])


class IngestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = _db(self.tmp.name)
        kb_runner.ensure_seed_kb(records=10, seed=1, path=self.path)

    def tearDown(self):
        kb_runner.close_store(self.path)  # Windows：连接不关，临时目录删不掉
        self.tmp.cleanup()

    def test_受控写入带溯源(self):
        result = kb_runner.ingest(
            [{"title": "新研究条目", "year": 2025, "tags": ["new"]}],
            submitter="alice", path=self.path)
        self.assertTrue(result["ok"])
        added = _stored(self.path)[-1]
        self.assertEqual(added["_provenance"]["submittedBy"], "alice")
        self.assertTrue(added["id"].startswith("KB"))

    def test_声明过的字段不会静默丢失(self):
        """旧实现 validate 放行 abstract_extra 却在写入时按 FIELD_TIERS 过滤掉它——
        声明表与写表白径不一致就会漏。现在两表同源（FIELDS 单一真源）。"""
        result = kb_runner.ingest(
            [{"title": "带出处的资源条目", "kind": "dataset", "source_uri": "https://example.org/d1",
              "license": "CC-BY-4.0", "owner_aic": "1.2.156.3088.1.X", "retrieved_at": "2026-10-18",
              "abstract": "数据集摘要正文"}],
            submitter="alice", path=self.path)
        self.assertTrue(result["ok"], result)
        stored = kb_runner.open_store(self.path).get(result["added"][0])
        self.assertEqual(stored["source_uri"], "https://example.org/d1")
        self.assertEqual(stored["license"], "CC-BY-4.0")
        self.assertEqual(stored["kind"], "dataset")

    def test_内容指纹幂等(self):
        records = [{"title": "同一篇论文", "year": 2024, "source_uri": "https://doi.org/10.1/x"}]
        first = kb_runner.ingest(records, submitter="a", path=self.path)
        second = kb_runner.ingest(
            [{"title": "  同一篇  论文 ", "year": 2024, "source_uri": "https://DOI.org/10.1/x/"}],
            submitter="b", path=self.path)
        self.assertEqual(len(first["added"]), 1)
        self.assertEqual(second["added"], [])
        self.assertEqual(len(second["duplicates"]), 1)
        self.assertEqual(second["duplicates"][0]["id"], first["added"][0])

    def test_指定外部稳定id(self):
        result = kb_runner.ingest([{"id": "ARXIV:2106.09685", "title": "稳定编号条目", "year": 2021}],
                                  submitter="a", path=self.path)
        self.assertEqual(result["added"], ["ARXIV:2106.09685"])
        self.assertEqual(kb_runner.open_store(self.path).get("ARXIV:2106.09685")["title"], "稳定编号条目")

    def test_导出记录可再导入且原始溯源不被改写(self):
        """_provenance 是系统保留键：导出→再导入是资源库的正常回路，
        若按未知字段拒绝，导出去的一份 JSON 就再也回不来。"""
        first = kb_runner.ingest([{"title": "可回导的条目", "year": 2024}], submitter="alice",
                                 path=self.path)
        stored = kb_runner.open_store(self.path).get(first["added"][0])
        self.assertEqual(stored["_provenance"]["submittedBy"], "alice")
        second = kb_runner.ingest([stored], submitter="kb-migration", path=self.path)
        self.assertEqual(second["added"], [])            # 指纹命中，不产生第二份
        self.assertEqual(second["duplicates"][0]["id"], first["added"][0])
        body = {k: v for k, v in stored.items() if k != "id"}  # 换内容还沿用旧 id 是冲突，不是新记录
        rep = kb_runner.ingest([{**body, "title": "改了标题的同源条目"}],
                               submitter="kb-migration", path=self.path)
        self.assertTrue(rep["ok"], rep)
        again = kb_runner.open_store(self.path).get(rep["added"][0])
        self.assertEqual(again["_provenance"]["submittedBy"], "kb-migration")
        self.assertEqual(again["_provenance"]["originally"]["submittedBy"], "alice")

    def test_沿用已存在的id拒绝(self):
        """改内容却沿旧 id 是更新而非新增——库里现在没有更新通道，明确拒绝而不是静默改。"""
        first = kb_runner.ingest([{"title": "占位条目甲", "year": 2023}], submitter="a", path=self.path)
        out = kb_runner.ingest([{"id": first["added"][0], "title": "换了内容仍用旧 id", "year": 2024}],
                               submitter="b", path=self.path)
        self.assertFalse(out["ok"])
        self.assertIn("已存在", out["error"])

    def test_源缺字段仍可入库(self):
        """真实语料半数字段是 null（源没摘要就没摘要），不能当类型错误拒掉。"""
        out = kb_runner.ingest([{"title": "没有摘要的论文", "year": 2022, "abstract": None,
                                 "venue": None, "source_uri": None}],
                               submitter="openalex", path=self.path)
        self.assertTrue(out["ok"], out)
        stored = kb_runner.open_store(self.path).get(out["added"][0])
        self.assertNotIn("abstract", stored)          # null 不落库，读时按缺字段处理
        self.assertEqual(stored["year"], 2022)        # 有值的照常

    def test_缺title拒绝(self):
        result = kb_runner.ingest([{"year": 2025}], submitter="a", path=self.path)
        self.assertFalse(result["ok"])

    def test_未知字段拒绝(self):
        result = kb_runner.ingest(
            [{"title": "ok title", "year": 2025, "hack": 1}], submitter="a", path=self.path)
        self.assertFalse(result["ok"])


class PagingTest(unittest.TestCase):
    """第 1 步的判据：单查询 25 行的上限只限"一次拿多少"，不再决定"最多能拿到多少"。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = _db(self.tmp.name)
        kb_runner.ensure_seed_kb(records=60, seed=7, path=self.path)

    def tearDown(self):
        kb_runner.close_store(self.path)  # Windows：连接不关，临时目录删不掉
        self.tmp.cleanup()

    def _q(self, **spec):
        return kb_runner.query({"mode": "filter", **spec}, role="consumer", path=self.path)

    def test_offset翻页覆盖全库不重不漏(self):
        seen, offset, pages, has_more = [], 0, 0, True
        while has_more:
            result = self._q(offset=offset, limit=25)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["matched"], 60)
            self.assertLessEqual(result["returned"], kb_runner.MAX_ROWS_PER_QUERY)
            seen.extend(row["id"] for row in result["rows"])
            has_more, offset = result["hasMore"], offset + result["returned"]
            pages += 1
        self.assertEqual(len(seen), 60)
        self.assertEqual(len(set(seen)), 60)   # 无重复即 rowid 兜底排序成立
        self.assertEqual(pages, 3)

    def test_排序翻页仍是同一全序(self):
        first = self._q(sort={"field": "year", "dir": "desc"}, offset=0, limit=25)
        second = self._q(sort={"field": "year", "dir": "desc"}, offset=25, limit=25)
        years = [row["year"] for row in first["rows"] + second["rows"]]
        self.assertEqual(years, sorted(years, reverse=True))
        self.assertEqual(len({r["id"] for r in first["rows"] + second["rows"]}), 50)

    def test_受限字段不能当排序键(self):
        result = self._q(sort={"field": "internal_note", "dir": "asc"}, limit=5)
        self.assertFalse(result["ok"])
        self.assertIn("受限字段", result["error"])

    def test_越界offset返回空页不报错(self):
        result = self._q(offset=1000, limit=25)
        self.assertTrue(result["ok"])
        self.assertEqual(result["returned"], 0)
        self.assertFalse(result["hasMore"])

    def test_负offset拒绝(self):
        self.assertFalse(self._q(offset=-1)["ok"])

    def test_跨类型比较明确拒绝(self):
        """SQL 的类型序会让 'a' > 5 成立，Python 侧是 TypeError→False——
        迁移后不能悄悄换语义，所以这里直接拒。"""
        result = self._q(where=[{"field": "year", "op": ">", "value": "2024"}])
        self.assertFalse(result["ok"])
        self.assertIn("int", result["error"])

    def test_contains转义LIKE通配符(self):
        kb_runner.ingest([{"title": "含 100% 命中率的评测", "year": 2026}],
                         submitter="a", path=self.path)
        hit = self._q(where=[{"field": "title", "op": "contains", "value": "100%"}], limit=5)
        self.assertTrue(hit["ok"])
        self.assertEqual(hit["matched"], 1)          # % 是字面量，不是通配符
        wildcard = self._q(where=[{"field": "title", "op": "contains", "value": "%"}], limit=5)
        self.assertEqual(wildcard["matched"], 1)     # 单字符 % 也只匹配那一条


class GetModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = _db(self.tmp.name)
        kb_runner.ensure_seed_kb(records=6, seed=2, path=self.path)
        self.first_id = kb_runner.query({"mode": "filter", "fields": ["id"], "limit": 1},
                                        role="provider", path=self.path)["rows"][0]["id"]

    def tearDown(self):
        kb_runner.close_store(self.path)  # Windows：连接不关，临时目录删不掉
        self.tmp.cleanup()

    def test_get按档位投影(self):
        consumer = kb_runner.query({"mode": "get", "id": self.first_id}, role="consumer", path=self.path)
        self.assertTrue(consumer["ok"] and consumer["found"])
        self.assertIn("abstract", consumer["row"])           # members 档对 consumer 可见
        self.assertIn("full_text", consumer["redactedFields"])
        self.assertNotIn("full_text", consumer["row"])
        provider = kb_runner.query({"mode": "get", "id": self.first_id}, role="provider", path=self.path)
        self.assertEqual(provider["redactedFields"], [])
        self.assertIn("internal_note", provider["row"])

    def test_get未知id(self):
        result = kb_runner.query({"mode": "get", "id": "KB999999"}, role="consumer", path=self.path)
        self.assertTrue(result["ok"])
        self.assertFalse(result["found"])

    def test_get缺id拒绝(self):
        self.assertFalse(kb_runner.query({"mode": "get"}, role="consumer", path=self.path)["ok"])


class ScaleTest(unittest.TestCase):
    """第 2 步的判据：真实规模（≥1k 条）存得下、取得出。"""

    def test_千条级入库与检索(self):
        tmp = tempfile.TemporaryDirectory()
        path = _db(tmp.name)
        self.addCleanup(tmp.cleanup)
        self.addCleanup(kb_runner.close_store, path)  # addCleanup 是 LIFO：先关连接再删目录
        kb_runner.open_store(path)
        for i in range(0, 1200, 200):
            out = kb_runner.ingest(
                [{"title": f"集群产物记录 {i + j}", "kind": "artifact", "year": 2020 + (i + j) % 6,
                  "tags": ["artifact"], "source_uri": f"https://cluster.local/artifacts/{i + j}"}
                 for j in range(200)],
                submitter="experiment-runner", path=path)
            self.assertTrue(out["ok"], out)
        self.assertEqual(kb_runner.record_count(path), 1200)
        result = kb_runner.query(
            {"mode": "filter", "where": [{"field": "kind", "op": "==", "value": "artifact"}],
             "sort": {"field": "year", "dir": "desc"}, "offset": 1175, "limit": 25,
             "fields": ["id", "title", "year"]}, role="consumer", path=path)
        self.assertEqual(result["matched"], 1200)
        self.assertEqual(result["returned"], 25)      # 尾部页也拿得到（旧实现做不到）
        self.assertFalse(result["hasMore"])


class SearchTest(unittest.TestCase):
    """第 3 步的判据：能问出答案，且检索面不成新的泄露通道。

    库内合成记录：摘要含"合成摘要"（members 档）、全文含"机构订阅全文"、
    内部笔记含"内部评审分"（均 curator 档）。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = _db(self.tmp.name)
        kb_runner.ensure_seed_kb(records=60, seed=7, path=self.path)

    def tearDown(self):
        kb_runner.close_store(self.path)  # Windows：连接不关，临时目录删不掉
        self.tmp.cleanup()

    def _s(self, query, role="consumer", **spec):
        return kb_runner.query({"mode": "search", "query": query, **spec}, role=role, path=self.path)

    def test_英文多词命中且按相关度排序(self):
        result = self._s("attention")
        self.assertTrue(result["ok"])
        self.assertGreater(result["matched"], 0)
        ranks = [h["rank"] for h in result["hits"] if h["rank"] is not None]
        self.assertEqual(ranks, sorted(ranks))          # bm25 越小越相关

    def test_中文三字命中(self):
        self.assertGreater(self._s("注意力")["matched"], 0)

    def test_中文短词走扫列兜底(self):
        """trigram 最短 3 字符，"稀疏"这类 2 字中文词必须还能命中，否则中文用户查不动。"""
        result = self._s("稀疏")
        self.assertGreater(result["matched"], 0)
        self.assertTrue(all(h["rank"] is None for h in result["hits"]))  # 无倒排得分

    def test_档位隔离_同一查询consumer搜不到curator内容(self):
        """这是分索引要守住的不变式：越权内容根本不进调用方那条检索通道，
        所以命中数、相关度、摘录三处都无从透出它。"""
        consumer = self._s("机构订阅全文")
        self.assertEqual(consumer["matched"], 0)
        auditor = self._s("内部评审分", role="auditor")
        self.assertEqual(auditor["matched"], 0)
        provider = self._s("机构订阅全文", role="provider")
        self.assertEqual(provider["matched"], 60)
        self.assertIn("curator", provider["hits"][0]["matchedIn"])
        self.assertIn("机构订阅全文", provider["hits"][0]["excerpt"])

    def test_档位隔离_consumer能搜到摘要而auditor不能(self):
        consumer = self._s("合成摘要")
        self.assertGreater(consumer["matched"], 0)
        self.assertEqual(self._s("合成摘要", role="auditor")["matched"], 0)

    def test_摘录不越权(self):
        """consumer 检索时，摘录只能来自 open/members 档字段——内部笔记的字样不得出现。"""
        for hit in self._s("研究", limit=25)["hits"]:
            self.assertNotIn("内部评审分", hit["excerpt"] or "")
            self.assertNotIn("机构订阅全文", hit["excerpt"] or "")

    def test_摘录长度有界(self):
        self.assertLessEqual(max(len(h["excerpt"] or "") for h in self._s("合成摘要")["hits"]),
                             kb_runner._EXCERPT_MAX + 8)

    def test_检索可叠加条件过滤(self):
        plain = self._s("注意力")
        narrowed = self._s("注意力", where=[{"field": "year", "op": ">=", "value": 2024}])
        self.assertTrue(narrowed["ok"])
        self.assertLessEqual(narrowed["matched"], plain["matched"])
        for hit in narrowed["hits"]:
            self.assertGreaterEqual(hit["year"], 2024)

    def test_检索翻页(self):
        first, second = self._s("注意力", limit=5, offset=0), self._s("注意力", limit=5, offset=5)
        self.assertEqual(first["matched"], second["matched"])
        self.assertFalse({h["id"] for h in first["hits"]} & {h["id"] for h in second["hits"]})

    def test_写入后立刻可搜(self):
        """倒排靠触发器同步：ingest 之后不需要 reindex。"""
        out = kb_runner.ingest([{"title": "触发器同步验证条目", "year": 2026,
                                 "abstract": "这段文字专门用来验证写完就能搜到"}],
                               submitter="a", path=self.path)
        self.assertTrue(out["ok"])
        self.assertEqual(self._s("触发器同步验证")["matched"], 1)
        self.assertEqual(self._s("专门用来验证")["matched"], 1)

    def test_空查询与超长拒绝(self):
        self.assertFalse(self._s("   ")["ok"])
        self.assertFalse(self._s("阿" * 201)["ok"])

    def test_行数上限同样作用于检索(self):
        self.assertFalse(self._s("注意力", limit=9999)["ok"])

    def test_受限字段不能参与检索的过滤条件(self):
        result = self._s("注意力", where=[{"field": "internal_note", "op": "contains", "value": "分"}])
        self.assertFalse(result["ok"])
        self.assertIn("受限字段", result["error"])

    def test_检索词里的FTS语法字符不被当语法(self):
        """裸拼会把 NEAR/OR/括号当查询语法；逐个加引号后它就是字面量。"""
        result = self._s('attention OR "知识"')
        self.assertTrue(result["ok"])


class SchemaHealTest(unittest.TestCase):
    """声明表加字段后，**已存在的库**必须自动补齐——否则现网库一开就 `no such column`。"""

    OLD_FIELDS = {"id": "text", "title": "text", "year": "int", "tags": "list",
                  "abstract": "text", "full_text": "text", "internal_note": "text"}
    OLD_LANES = {"open": ["title"], "members": ["abstract"], "curator": ["full_text", "internal_note"]}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = _db(self.tmp.name)

    def tearDown(self):
        kb_runner.close_store(self.path)
        self.tmp.cleanup()

    def _make_legacy(self, rows: list[dict]):
        from acps_cluster.runtime.kb_store import KBStore
        legacy = KBStore(self.path, self.OLD_FIELDS, lanes=self.OLD_LANES)
        # 显式 id 走元组第一位（record 里的 id 会被存储层覆盖成自动号）
        legacy.insert([(row.get("id"), f"legacy{i}", row) for i, row in enumerate(rows, start=1)])
        legacy.close()

    def test_老库缺列自动补齐且老数据仍可读(self):
        self._make_legacy([
            {"id": "L1", "title": "稀疏注意力下的长上下文建模", "year": 2024, "tags": ["LLM"],
             "abstract": "讲稀疏注意力的摘要", "full_text": "订阅全文", "internal_note": "内部评审分 4.2"},
            {"id": "L2", "title": "知识图谱补全的图网络方法", "year": 2023, "tags": ["GNN"],
             "abstract": "讲知识图谱的摘要", "full_text": "另一份全文", "internal_note": "内部评审分 3.1"},
        ])
        store = kb_runner.open_store(self.path)
        expected = [f for f in kb_runner.FIELDS if f not in self.OLD_FIELDS and f != "id"]
        self.assertEqual(sorted(store.drift["columns"]), sorted(expected))
        self.assertIn("source", store.drift["columns"])   # 新加的字段也是补出来的，不是重建库
        # 补齐的列对老记录是 NULL，不能因此查不动
        result = kb_runner.query({"mode": "filter", "fields": ["id", "title", "venue", "kind"],
                                  "sort": {"field": "year", "dir": "desc"}},
                                 role="consumer", path=self.path)
        self.assertTrue(result["ok"], result)
        self.assertEqual([r["id"] for r in result["rows"]], ["L1", "L2"])
        self.assertIsNone(result["rows"][0]["venue"])

    def test_索引档列变化后重建并可搜(self):
        """旧 open 档只索引 title；新声明加了 venue/tags → 档必须重建，否则列集合不对就查不出。"""
        self._make_legacy([{"id": "L1", "title": "条目甲", "year": 2024, "tags": ["GNN"],
                            "abstract": "摘要有图神经网络", "full_text": "全文", "internal_note": "笔记"}])
        store = kb_runner.open_store(self.path)
        self.assertEqual(store.drift["lanes"], ["open"])      # open 档列集合变了 → 重建
        self.assertEqual(sorted(store._lane_columns("open")), ["tags", "title", "venue"])
        hit = kb_runner.query({"mode": "search", "query": "图神经网络"}, role="consumer", path=self.path)
        self.assertTrue(hit["ok"])
        self.assertEqual(hit["matched"], 1)                    # 重建后摘要照样搜得到

    def test_重复打开不再报漂移(self):
        self._make_legacy([{"id": "L1", "title": "条目甲", "year": 2024, "abstract": "摘要",
                            "full_text": "全文", "internal_note": "笔记"}])
        kb_runner.open_store(self.path)
        kb_runner.close_store(self.path)          # 必须真重开：open_store 有连接缓存
        second = kb_runner.open_store(self.path)
        self.assertEqual(second.drift, {"columns": [], "lanes": []})


class HolderTest(unittest.TestCase):
    """馆藏独立性：`owner_aic` 是本馆身份，`source` 才是数据来源，`source_uri` 是原文出处。

    早期导入把这三件事混在 owner_aic 一个字段里（填了 openalex/arxiv），
    等于自家元数据里把"这是谁的馆藏"写丢了。
    """

    HOLDER = "1.2.156.3088.1.1.KBTEST.HOLD01.RUN01.0001"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = _db(self.tmp.name)
        self._env = {k: os.environ.get(k) for k in ("ACPS_KB_PATH", "ACPS_KB_HOLDER_AIC")}
        os.environ["ACPS_KB_PATH"] = str(self.path)
        os.environ["ACPS_KB_HOLDER_AIC"] = self.HOLDER
        kb_runner.ensure_seed_kb(records=8, seed=1, path=self.path)

    def tearDown(self):
        kb_runner.close_store(self.path)
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def _fix(self):
        import argparse

        from acps_cluster.runtime import kb_admin
        kb_admin.cmd_fix_owner(argparse.Namespace(holder=None))
        return kb_runner.open_store(self.path)

    def test_归位后持有方是本馆(self):
        store = self._fix()
        rows = list(store.all_records())
        self.assertTrue(all(r["owner_aic"] == self.HOLDER for r in rows))
        self.assertTrue(all(r["source"] == "local-catalog" for r in rows))  # 本馆自己编目的

    def test_归位幂等(self):
        self._fix()
        before = list(kb_runner.open_store(self.path).all_records())
        self._fix()
        self.assertEqual(before, list(kb_runner.open_store(self.path).all_records()))

    def test_按编号前缀认来源(self):
        kb_runner.ingest([
            {"id": "OA:W123", "title": "外来编目的论文甲", "year": 2024, "owner_aic": "openalex"},
            {"id": "ARXIV:2401.00009", "title": "外来编目的论文乙", "year": 2024, "owner_aic": "arxiv"},
        ], submitter="yanshu.kb-admin", path=self.path)
        store = self._fix()
        by_id = {r["id"]: r for r in store.all_records()}
        self.assertEqual(by_id["OA:W123"]["source"], "openalex")
        self.assertEqual(by_id["ARXIV:2401.00009"]["source"], "arxiv")
        self.assertEqual(by_id["OA:W123"]["owner_aic"], self.HOLDER)

    def test_改写后倒排仍同步(self):
        """UPDATE 走 AFTER UPDATE 触发器——正文没动但索引必须跟着换，不能两个版本。"""
        self._fix()
        hit = kb_runner.query({"mode": "search", "query": "合成摘要"}, role="consumer", path=self.path)
        self.assertTrue(hit["ok"])
        self.assertEqual(hit["matched"], 8)

    def test_source可过滤但不进全文索引(self):
        """来源是目录维度，该按条件筛；进倒排的话搜 "arxiv" 会把一堆无关文献顶上来。"""
        self.assertNotIn("source", kb_runner.SEARCH_LANES["open"])
        self._fix()  # 归位之后 source 才有值（老记录本来就没这个字段）
        filtered = kb_runner.query(
            {"mode": "filter", "where": [{"field": "source", "op": "==", "value": "local-catalog"}],
             "fields": ["id", "source"]}, role="consumer", path=self.path)
        self.assertEqual(filtered["matched"], 8)
        searched = kb_runner.query({"mode": "search", "query": "local-catalog"},
                                   role="provider", path=self.path)
        self.assertEqual(searched["matched"], 0)


class WiringTest(unittest.TestCase):
    def test_能力档位_一橱窗两保险柜(self):
        from acps_cluster.agents import partner_kb
        from acps_cluster.delegation.pep import capability_visibility

        ids = [getattr(c, "capability", c).id for c in partner_kb.KB_CAPABILITIES]
        self.assertEqual(ids, ["kb.schema", "kb.query", "kb.ingest"])
        public, has_non = capability_visibility(partner_kb.KB_CAPABILITIES)
        self.assertEqual(public, frozenset({"kb.schema"}))
        self.assertTrue(has_non)
        for cap in (partner_kb.kb_query, partner_kb.kb_ingest):
            skill = cap.capability.to_acps_skill()
            self.assertEqual(skill.get("visibility"), "domain")
            self.assertIn("yanshu-kb", skill.get("domains", []))


if __name__ == "__main__":
    unittest.main(verbosity=2)
