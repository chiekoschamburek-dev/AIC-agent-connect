#!/usr/bin/env python3
"""知识库运维入口：建库、迁移旧 JSON、把真实文献语料灌进库、修元数据。

导入一律走 `kb_runner.ingest()`——不绕策略层的字段白名单与溯源；重复执行同一条命令
不再产生第二份记录（内容指纹幂等）。

本库是**独立机构的馆藏**：`owner_aic` 写本馆自己的身份（读 ws-kb/acps/enrollment.json，
或 ACPS_KB_HOLDER_AIC / --holder 覆盖），`source` 写元数据来源，`source_uri` 写原文出处。

    python acps_cluster/kb_admin.py stats
    python acps_cluster/kb_admin.py migrate acps_cluster/ws-kb/kb.json
    python acps_cluster/kb_admin.py import --source openalex --query "graph neural network knowledge graph" --limit 200
    python acps_cluster/kb_admin.py import --source arxiv   --query "abs:attention AND abs:long context" --limit 100
    python acps_cluster/kb_admin.py fix-owner      # 历史记录的持有方归位（幂等）
    python acps_cluster/kb_admin.py reindex        # 绕过 ingest 直接改过库之后跑
    python acps_cluster/kb_admin.py grant --subject <消费方 AIC> --skill kb.query   # 本馆自签授权
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import kb_runner  # noqa: E402

KB_WORKSPACE = HERE / "ws-kb"
ENROLLMENT = KB_WORKSPACE / "acps" / "enrollment.json"
HOLDER_ENV = "ACPS_KB_HOLDER_AIC"
SOURCE_BY_ID_PREFIX = {"OA:": "openalex", "ARXIV:": "arxiv"}


def holder_aic() -> str:
    """馆藏持有方 = KB 自己的机构身份，**不是**元数据来源。

    来源记在 `source`（openalex/arxiv/…），出处记在 `source_uri`（DOI/arXiv 链接）。
    这三个字段混成一谈的话，"谁的馆藏"就查不出来了——本库是独立机构的库，
    持有方必须是它自己。
    """
    env = os.getenv(HOLDER_ENV)
    if env and env.strip():
        return env.strip()
    try:
        enrollment = json.loads(ENROLLMENT.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"读不到 KB 身份 {ENROLLMENT}（{exc}）；请用 --holder 或 {HOLDER_ENV} 指定")
    aic = ((enrollment.get("identity") or {}).get("ref") or {}).get("aic")
    if not aic:
        raise SystemExit(f"{ENROLLMENT} 里没有 identity.ref.aic；请用 --holder 或 {HOLDER_ENV} 指定")
    return str(aic)


def source_of(record: dict) -> str:
    """按编号前缀认来源；认不出就是本馆自己编目的。"""
    return SOURCE_BY_ID_PREFIX.get(str(record.get("id", "")).split(":")[0] + ":", "local-catalog")

UA = "yanshu-kb-admin/1.0 (research cluster)"
HTTP_TIMEOUT = 30
MAX_TITLE = 300
MAX_ABSTRACT = 6000


def _clean(value: Any, limit: int = MAX_ABSTRACT) -> str | None:
    if not isinstance(value, str):
        return None
    text = re.sub(r"\s+", " ", value).strip()
    return text[:limit] or None


def _http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read().decode("utf-8", "replace")


def _abstract_from_inverted_index(inverted: dict | None) -> str | None:
    """OpenAlex 只回倒排索引（词 → 位置数组），按位置拼回原文。"""
    if not inverted:
        return None
    positions: list[tuple[int, str]] = []
    for word, idxs in inverted.items():
        for i in idxs or []:
            positions.append((int(i), word))
    if not positions:
        return None
    positions.sort(key=lambda p: p[0])
    return _clean(" ".join(w for _, w in positions))


def fetch_openalex(query: str, limit: int, *, mailto: str | None = None, holder: str = "?") -> list[dict]:
    """真实文献元数据 + 摘要（OpenAlex 无需密钥；mailto 进礼貌池更稳）。"""
    records: list[dict] = []
    cursor = "*"
    retrieved = time.strftime("%Y-%m-%d")  # 本机取回日期：源里没有 retrieved_date 这个字段
    select = ("id,doi,title,display_name,publication_year,primary_location,"
              "open_access,abstract_inverted_index,topics")
    while len(records) < limit:
        params = {
            "search": query, "per-page": str(min(200, limit - len(records))),
            "select": select, "cursor": cursor,
        }
        if mailto:
            params["mailto"] = mailto
        body = _http_json("https://api.openalex.org/works?" + urllib.parse.urlencode(params))
        results = body.get("results") or []
        if not results:
            break
        cursor = body.get("meta", {}).get("next_cursor") or "*"
        for work in results:
            openalex_id = str(work.get("id") or "").rstrip("/").split("/")[-1]
            if not openalex_id:
                continue
            location = work.get("primary_location") or {}
            source = location.get("source") or {}
            topics = [t.get("display_name") for t in (work.get("topics") or []) if t.get("display_name")]
            open_access = work.get("open_access") or {}
            title = _clean(work.get("title") or work.get("display_name"), MAX_TITLE)
            if not title:
                continue
            year = work.get("publication_year")
            records.append({
                "id": f"OA:{openalex_id}",
                "kind": "paper",
                "title": title,
                "year": year if isinstance(year, int) else None,
                "venue": _clean(source.get("display_name"), 200),
                "tags": [t[:64] for t in topics[:8]],
                "abstract": _abstract_from_inverted_index(work.get("abstract_inverted_index")),
                "source_uri": _clean(work.get("doi") or work.get("id"), 300),
                "license": _clean(open_access.get("license"), 100),
                "source": "openalex",
                "owner_aic": holder,
                "retrieved_at": retrieved,
            })
        if cursor == "*":
            break
    return records[:limit]


_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV_NS = "{http://arxiv.org/schemas/atom}"


def fetch_arxiv(query: str, limit: int, *, holder: str = "?") -> list[dict]:
    """arXiv Atom feed：预印本标题 + 摘要 + 分类。search_query 用 arXiv 语法（all:/abs:/ti:）。"""
    url = ("http://export.arxiv.org/api/query?search_query=" + urllib.parse.quote(query)
           + f"&start=0&max_results={min(limit, 200)}")
    root = ET.fromstring(_http_text(url))
    records = []
    for entry in root.findall(f"{_ATOM}entry"):
        raw_id = (entry.findtext(f"{_ATOM}id") or "").strip()
        arxiv_id = re.sub(r"^https?://arxiv\.org/abs/", "", raw_id)
        title = _clean(entry.findtext(f"{_ATOM}title"), MAX_TITLE)
        if not arxiv_id or not title:
            continue
        published = entry.findtext(f"{_ATOM}published") or ""
        year = int(published[:4]) if published[:4].isdigit() else None
        cats = [c.get("term") for c in entry.findall(f"{_ATOM}category") if c.get("term")]
        primary = entry.find(f"{_ARXIV_NS}primary_category")
        records.append({
            "id": f"ARXIV:{arxiv_id.split('v')[0]}",
            "kind": "paper",
            "title": title,
            "year": year,
            "venue": "arXiv",
            "tags": cats[:8],
            "abstract": _clean(entry.findtext(f"{_ATOM}summary")),
            "source_uri": raw_id or f"https://arxiv.org/abs/{arxiv_id}",
            "license": _clean((entry.findtext(f"{_ARXIV_NS}license")) or None, 200),
            "source": "arxiv",
            "owner_aic": holder,
            "retrieved_at": time.strftime("%Y-%m-%d"),
        })
    return records[:limit]


SOURCES = {"openalex": fetch_openalex, "arxiv": fetch_arxiv}


def _ingest_in_batches(records: list[dict], *, submitter: str, batch: int) -> dict:
    added, duplicates, errors = [], [], []
    for i in range(0, len(records), batch):
        out = kb_runner.ingest(records[i:i + batch], submitter=submitter)
        if not out.get("ok"):
            errors.append(out.get("error"))
            continue
        added.extend(out.get("added") or [])
        duplicates.extend(out.get("duplicates") or [])
    return {"added": len(added), "duplicated": len(duplicates),
            "total": kb_runner.record_count(), "errors": errors[:3]}


def cmd_import(args: argparse.Namespace) -> int:
    holder = args.holder or holder_aic()
    kwargs: dict[str, Any] = {"holder": holder}
    if args.source == "openalex":
        kwargs["mailto"] = args.mailto
    print(f"[import] {args.source} ← {args.query!r} 目标 {args.limit} 条（持有方 {holder}）")
    records = SOURCES[args.source](args.query, args.limit, **kwargs)
    print(f"[import] 取回 {len(records)} 条（含摘要 {sum(1 for r in records if r.get('abstract'))} 条）")
    if not records:
        return 1
    out = _ingest_in_batches(records, submitter=args.submitter, batch=args.batch)
    print(f"[import] 入库 新增={out['added']} 幂等跳过={out['duplicated']} 库总量={out['total']}")
    for err in out["errors"]:
        print(f"[import] 批失败：{err}")
    return 0 if out["added"] or out["duplicated"] else 1


def cmd_migrate(args: argparse.Namespace) -> int:
    """旧 kb.json → SQLite：保留原 id 与全部字段（含 curator 档，档位由策略层管）。"""
    src = Path(args.json_path)
    payload = json.loads(src.read_text(encoding="utf-8"))
    records = payload.get("records") or []
    if not records:
        print("[migrate] 旧库没有记录")
        return 1
    for record in records:
        record.setdefault("kind", "paper")
    out = _ingest_in_batches(records, submitter="yanshu.kb-migration", batch=args.batch)
    print(f"[migrate] {src.name} 记录 {len(records)} 条 → 新增={out['added']} "
          f"跳过={out['duplicated']} 库总量={out['total']}")
    for err in out["errors"]:
        print(f"[migrate] 批失败：{err}")
    print("[migrate] 旧库没有持有方/来源元数据，接着跑 `fix-owner` 归位")
    return 0 if out["added"] else 1


def cmd_fix_owner(args: argparse.Namespace) -> int:
    """把历史记录的持有方改成本馆，来源另记 source（幂等，可反复跑）。

    早期导入把 `owner_aic` 填成了 openalex/arxiv——那是**来源**不是**持有方**，
    等于在自家元数据里把"谁的馆藏"写丢了。持有方只能是本馆自己。
    """
    holder = args.holder or holder_aic()
    store = kb_runner.open_store()
    changed = correct = 0
    for rowid, record in list(store.iter_rows()):
        source = record.get("source") or source_of(record)
        if record.get("owner_aic") == holder and record.get("source") == source:
            correct += 1
            continue
        record["owner_aic"] = holder
        record["source"] = source
        store.replace_payload(rowid, record)
        changed += 1
    store.commit()
    print(f"[fix-owner] 持有方 → {holder}；改写 {changed} 条，本已正确 {correct} 条，库 {store.count()} 条")
    return 0


def cmd_grant(args: argparse.Namespace) -> int:
    """资源方（本馆）给消费方签发域 grant——B3 之后授权从这里出。

    章程 `issuers` 只含本馆，所以 leader 自己签的令牌会被判 `issuer-not-in-domain`；
    消费方要用得先来申请，本馆签完把令牌**带外**递过去（令牌是凭据，不进模型上下文）。
    """
    from delegation import issue_grant, load_signing_context
    from delegation.domain_manifest import load_domain_policies
    from delegation.token import TokenError

    workspace = Path(args.workspace)
    acps = workspace / "acps"
    policies = load_domain_policies(acps / "domains", trust_bundle=acps / "trust-bundle.pem")
    policy = policies.get(args.domain)
    if policy is None:
        print(f"[grant] 本机没有域 {args.domain!r} 的已验证章程（读到：{sorted(policies) or '无'}）")
        return 1
    holder = holder_aic()
    if holder not in policy.issuers:
        print(f"[grant] 本馆 {holder[-24:]} 不在域 {args.domain} 的 issuers 里，"
              f"签出来的令牌会被判 issuer-not-in-domain；issuers={sorted(i[-24:] for i in policy.issuers)}")
        return 1
    offered = sorted({skill for (provider, skill) in policy.assets if provider == holder})
    wanted = list(args.skill or offered)
    unknown = [s for s in wanted if s not in offered]
    if unknown:
        print(f"[grant] 章程没把这些资产给到本馆：{unknown}；可签发={offered}")
        return 1

    signing = load_signing_context(acps / "client", trust_bundle=acps / "trust-bundle.pem")
    try:
        grant = issue_grant(
            signing, domain=args.domain, domain_revision=policy.revision, subject_aic=args.subject,
            assets=[{"providerAic": holder, "skill": s} for s in wanted],
            max_uses=args.max_uses, ttl_seconds=args.ttl,
        )
    except TokenError as exc:
        print(f"[grant] 签发失败 {exc.reason}: {exc.detail}")
        return 1

    # 令牌落在 <工作区>/acps/grants/：acps/ 是本集群既有的私有物料目录（client 私钥、
    # secrets 都在里面），跟着它的排除约定走；单独放 ws-kb/grants/ 会被 git 看见。
    out_dir = acps / "grants"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{grant.jti}.token"
    path.write_text(grant.token, encoding="utf-8")
    os.chmod(path, 0o600)  # Windows 上只落 readonly 位；真正的保护是 .gitignore + 不带外递
    print(f"[grant] 已签 subject={args.subject[-24:]} skills={wanted} "
          f"max_uses={args.max_uses} 有效={args.ttl}s revision={policy.revision}")
    print(f"[grant] 令牌落盘 {path}（带外递给消费方，不要贴进模型上下文、心跳或提交）")
    return 0


def cmd_stats(_: argparse.Namespace) -> int:
    store = kb_runner.open_store()
    print(f"[stats] 库路径 {store.path}（{kb_runner.record_count()} 条）")
    if store.drift["columns"] or store.drift["lanes"]:
        print(f"[stats] 开库时补齐：新增列 {store.drift['columns']}（VIRTUAL，不可建索引）；"
              f"重建索引档 {store.drift['lanes']}")
    kinds: dict[str, int] = {}
    with_abstract = 0
    for record in store.all_records():
        kinds[str(record.get("kind"))] = kinds.get(str(record.get("kind")), 0) + 1
        if record.get("abstract"):
            with_abstract += 1
    print(f"[stats] 类型分布 {kinds}；带摘要 {with_abstract}")
    # 索引健康只能靠真查一次：外部内容表的 COUNT 读正文表，索引空了也照样报满数
    probe = kb_runner.query({"mode": "search", "query": "network", "limit": 1}, role="provider")
    print(f"[stats] 全文索引探针 'network' → matched={probe.get('matched')} "
          f"档位={probe.get('searchLanes')} 索引档={sorted(store.lanes)}")
    return 0


def cmd_reindex(args: argparse.Namespace) -> int:
    """重建倒排：绕过 ingest 直接改过库、或索引规格变化后跑这个（幂等）。"""
    store = kb_runner.open_store()
    lanes = store.reindex()
    print(f"[reindex] 重建档位 {lanes}（trigram 分词，按档位分索引）")
    for term in args.probe:
        out = kb_runner.query({"mode": "search", "query": term, "limit": 3}, role="provider")
        print(f"[reindex] 探针 {term!r} → {out.get('matched')} 命中")
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    """合成库：只给单测与 K 系列演示用（ACPS_KB_PATH 指到临时库再执行）。"""
    kb_runner.ensure_seed_kb(records=args.records, seed=args.seed)
    print(f"[seed] 库内 {kb_runner.record_count()} 条（合成）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="知识库建库/迁移/真实语料导入")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("import", help="从 OpenAlex/arXiv 拉真实文献入库")
    p.add_argument("--source", choices=sorted(SOURCES), required=True)
    p.add_argument("--query", required=True)
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--batch", type=int, default=kb_runner.MAX_INGEST_BATCH)
    p.add_argument("--mailto", default=None, help="OpenAlex 礼貌池联系邮箱")
    p.add_argument("--submitter", default="yanshu.kb-admin")
    p.add_argument("--holder", default=None, help="馆藏持有方 AIC（默认读本馆 enrollment 或 ACPS_KB_HOLDER_AIC）")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("migrate", help="旧 kb.json 迁入 SQLite 库")
    p.add_argument("json_path")
    p.add_argument("--batch", type=int, default=kb_runner.MAX_INGEST_BATCH)
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("fix-owner", help="历史记录持有方改成本馆、来源另记 source（幂等）")
    p.add_argument("--holder", default=None)
    p.set_defaults(func=cmd_fix_owner)

    p = sub.add_parser("grant", help="本馆给消费方签发域 grant（令牌落盘 0600，带外递）")
    p.add_argument("--domain", default="yanshu-kb")
    p.add_argument("--subject", required=True, help="消费方 AIC（grant 只能由它本人使用）")
    p.add_argument("--skill", action="append", default=[], help="可多次；默认取章程里本馆提供的全部资产")
    p.add_argument("--max-uses", type=int, default=20)
    p.add_argument("--ttl", type=int, default=3600)
    p.add_argument("--workspace", default=str(KB_WORKSPACE))
    p.set_defaults(func=cmd_grant)

    p = sub.add_parser("stats")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("reindex", help="重建全文倒排（按档位分索引，幂等）")
    p.add_argument("--probe", nargs="*", default=["network", "知识图谱"],
                   help="重建后用来验真命的查询词")
    p.set_defaults(func=cmd_reindex)

    p = sub.add_parser("seed", help="写入合成记录（仅演示/测试）")
    p.add_argument("--records", type=int, default=60)
    p.add_argument("--seed", type=int, default=7)
    p.set_defaults(func=cmd_seed)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
