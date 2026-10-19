"""多源学术检索实现：OpenAlex / Semantic Scholar（均为免 key 免费 API）。

与 capabilities/arxiv.py 同层：只提供纯函数，ACPS public 包装在各 partner 脚本里。
"""

import asyncio
import os
from dataclasses import dataclass
from datetime import date, timedelta

import httpx

_HEADERS = {"User-Agent": "SciNexus-research/0.1 (mailto:huaibin2025@outlook.com)"}
_MAILTO = os.getenv("ACPS_OA_MAILTO", "huaibin2025@outlook.com")
_MONTH_DAYS = 30.44


@dataclass
class ScholarRecord:
    source: str
    native_id: str
    title: str
    authors: list[str]
    year: int | None
    abstract: str
    url: str
    citations: int | None = None


def _rebuild_abstract(inverted: dict | None) -> str:
    if not inverted:
        return ""
    positions = {pos: word for word, poses in inverted.items() for pos in poses}
    return " ".join(positions[i] for i in sorted(positions))


async def openalex_search(
    query: str, max_results: int = 5, retries: int = 3
) -> list[ScholarRecord]:
    """匿名配额有限，429 时退避重试；mailto 参数进礼貌池可显著提额。"""
    limit = max(1, min(max_results, 20))
    last_error = "unknown"
    async with httpx.AsyncClient(timeout=30.0, headers=_HEADERS) as client:
        for attempt in range(retries):
            resp = await client.get(
                "https://api.openalex.org/works",
                params={"search": query, "per-page": limit, "mailto": _MAILTO},
            )
            if resp.status_code == 429:
                last_error = "rate limited (429)"
                await asyncio.sleep(5 * (attempt + 1))
                continue
            resp.raise_for_status()
            records = []
            for work in resp.json().get("results", []):
                records.append(
                    ScholarRecord(
                        source="openalex",
                        native_id=str(work.get("id") or ""),
                        title=work.get("title") or "",
                        authors=[
                            (a.get("author") or {}).get("display_name") or "?"
                            for a in (work.get("authorships") or [])[:8]
                        ],
                        year=work.get("publication_year"),
                        abstract=_rebuild_abstract(
                            work.get("abstract_inverted_index")
                        ),
                        url=work.get("doi") or str(work.get("id") or ""),
                        citations=work.get("cited_by_count"),
                    )
                )
            return records
    raise RuntimeError(f"OpenAlex 检索失败: {last_error}")


async def semantic_scholar_search(
    query: str, max_results: int = 5, retries: int = 3
) -> list[ScholarRecord]:
    """匿名配额共享、常返回 429，按退避重试；量大时申请免费 key 换 S2_API_KEY。"""
    limit = max(1, min(max_results, 20))
    params = {
        "query": query,
        "fields": "title,year,abstract,authors,externalIds,citationCount,url",
        "limit": limit,
    }
    last_error = "unknown"
    async with httpx.AsyncClient(timeout=30.0, headers=_HEADERS) as client:
        for attempt in range(retries):
            resp = await client.get(
                "https://api.semanticscholar.org/graph/v1/paper/search",
                params=params,
            )
            if resp.status_code == 429:
                last_error = "rate limited (429)"
                await asyncio.sleep(3 * (attempt + 1))
                continue
            resp.raise_for_status()
            records = []
            for paper in resp.json().get("data", []):
                arxiv_id = (paper.get("externalIds") or {}).get("ArXiv")
                records.append(
                    ScholarRecord(
                        source="semanticscholar",
                        native_id=arxiv_id or str(paper.get("paperId") or ""),
                        title=paper.get("title") or "",
                        authors=[
                            a.get("name") or "?" for a in (paper.get("authors") or [])[:8]
                        ],
                        year=paper.get("year"),
                        abstract=paper.get("abstract") or "",
                        url=(
                            f"https://arxiv.org/abs/{arxiv_id}"
                            if arxiv_id
                            else str(paper.get("url") or "")
                        ),
                        citations=paper.get("citationCount"),
                    )
                )
            return records
    raise RuntimeError(f"Semantic Scholar 检索失败: {last_error}")


# ── 前沿速度探测：基于引用图谱计算 citation velocity ──
# 本层只搬运并计算 API 返回的真实数字；"是否算新兴方向""现在入场晚不晚"属于启发式判断，
# 由上层 agent 依据这里给出的原始数字得出，并必须显式标注为启发式。


@dataclass
class EmergingWork:
    """窗口内发表、已积累可观测引用的新工作。"""

    source: str
    native_id: str
    title: str
    authors: list[str]
    published: str  # ISO 日期
    age_months: float
    citations: int
    citations_per_month: float
    recent_citations: int  # 近两个自然年的引用合计（counts_by_year 只有年粒度）
    url: str
    topics: list[str]


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def compute_velocity(
    citations: int, published: str | None, *, today: date | None = None
) -> tuple[float, float] | None:
    """返回 (月龄, 引用/月)；无法计算时返回 None。

    月龄不足 1 个月时以 1 个月作分母，避免刚上线的新论文速度被无限放大；
    age_months 仍如实返回，供调用方判断样本是否可靠。
    """
    published_on = _parse_date(published)
    if published_on is None:
        return None
    age = ((today or date.today()) - published_on).days / _MONTH_DAYS
    if age < 0:  # 日期在未来：数据异常，丢弃
        return None
    return age, citations / max(age, 1.0)


def _topics_of(work: dict) -> list[str]:
    """优先取 OpenAlex 新版 topics/primary_topic，回退到旧版 concepts。"""
    names: list[str] = []
    primary = work.get("primary_topic") or {}
    if primary.get("display_name"):
        names.append(str(primary["display_name"]))
    for key in ("topics", "concepts"):
        for entry in (work.get(key) or [])[:4]:
            name = entry.get("display_name")
            if name and name not in names:
                names.append(str(name))
    return names[:5]


async def openalex_emerging(
    topic: str,
    *,
    window_months: int = 12,
    max_results: int = 20,
    retries: int = 3,
    today: date | None = None,
) -> list[EmergingWork]:
    """拉取时间窗内发表、按总引用排序的 OpenAlex 工作并计算 citation velocity。

    用 title_and_abstract.search 而非全文 search：后者的相关度排序会把十多年前的
    高引综述顶到前面（实测 "GNN 知识图谱补全" 会返回 2017 年医学影像综述），
    与"新兴方向"目标相反。这里限定时间窗 + 题录匹配，再在代码侧按速度重排。

    counts_by_year 是自然年粒度，无法精确切到"近 12 个月"，因此 recent_citations
    的语义是"近两个自然年"，转述给用户时不能改写成"近一年"。
    """
    now = today or date.today()
    limit = max(1, min(max_results, 50))
    since = now - timedelta(days=int(window_months * _MONTH_DAYS))
    last_error = "unknown"
    async with httpx.AsyncClient(timeout=30.0, headers=_HEADERS) as client:
        for attempt in range(retries):
            resp = await client.get(
                "https://api.openalex.org/works",
                params={
                    "filter": (
                        f"from_publication_date:{since.isoformat()},"
                        f"to_publication_date:{now.isoformat()},"
                        f"title_and_abstract.search:{topic}"
                    ),
                    "sort": "cited_by_count:desc",
                    "per-page": limit,
                    "mailto": _MAILTO,
                },
            )
            if resp.status_code == 429:
                last_error = "rate limited (429)"
                await asyncio.sleep(5 * (attempt + 1))
                continue
            resp.raise_for_status()
            works: list[EmergingWork] = []
            for work in resp.json().get("results", []):
                published = work.get("publication_date") or ""
                computed = compute_velocity(
                    int(work.get("cited_by_count") or 0), published, today=now
                )
                if computed is None:
                    continue
                age, velocity = computed
                works.append(
                    EmergingWork(
                        source="openalex",
                        native_id=str(work.get("id") or ""),
                        title=work.get("title") or "",
                        authors=[
                            (a.get("author") or {}).get("display_name") or "?"
                            for a in (work.get("authorships") or [])[:8]
                        ],
                        published=published[:10],
                        age_months=round(age, 2),
                        citations=int(work.get("cited_by_count") or 0),
                        citations_per_month=round(velocity, 2),
                        recent_citations=sum(
                            int(entry.get("cited_by_count") or 0)
                            for entry in (work.get("counts_by_year") or [])
                            if int(entry.get("year") or 0) >= now.year - 1
                        ),
                        url=work.get("doi") or str(work.get("id") or ""),
                        topics=_topics_of(work),
                    )
                )
            works.sort(key=lambda w: w.citations_per_month, reverse=True)
            return works
    raise RuntimeError(f"OpenAlex 前沿检索失败: {last_error}")


async def s2_citation_profile(paper_id: str, *, retries: int = 3, limit: int = 1000) -> dict:
    """取单篇论文的施引年份分布（S2 引用图谱）。

    paper_id 支持 S2 paperId 或 `arXiv:2401.00001` 形式。匿名配额常 429，按退避重试。

    注意：S2 该端点一次最多返回 limit 条施引记录，没有总数字段。因此
    `fetched` 是"本次取回的施引记录条数"，不是论文的真实引用总数；被截断时
    `capped=True` 且早年年份可能偏少，不能当作完整引用曲线使用。
    """
    last_error = "unknown"
    async with httpx.AsyncClient(timeout=30.0, headers=_HEADERS) as client:
        for attempt in range(retries):
            resp = await client.get(
                f"https://api.semanticscholar.org/graph/v1/paper/{paper_id}/citations",
                params={"fields": "year", "limit": limit},
            )
            if resp.status_code == 429:
                last_error = "rate limited (429)"
                await asyncio.sleep(3 * (attempt + 1))
                continue
            resp.raise_for_status()
            by_year: dict[int, int] = {}
            for entry in resp.json().get("data", []):
                year = (entry.get("citingPaper") or {}).get("year")
                if isinstance(year, int):
                    by_year[year] = by_year.get(year, 0) + 1
            fetched = sum(by_year.values())
            return {
                "by_year": dict(sorted(by_year.items())),
                "fetched": fetched,
                "capped": fetched >= limit,
            }
    raise RuntimeError(f"S2 引文分布获取失败: {last_error}")
