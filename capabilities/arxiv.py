"""arXiv 资料查阅 capability：供查阅资料 agent 调用。"""

import asyncio
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import httpx

from wit import capability

_ATOM = "{http://www.w3.org/2005/Atom}"
_API_URL = "https://export.arxiv.org/api/query"
# arXiv API 官方要求至少 3 秒/请求
_MIN_INTERVAL_SECONDS = 3.0

_last_request_at = 0.0
_rate_lock = asyncio.Lock()


@dataclass
class PaperRecord:
    arxiv_id: str
    title: str
    authors: list[str]
    published: str
    abstract: str
    pdf_url: str


async def _get_with_retry(params: dict) -> httpx.Response:
    global _last_request_at
    async with _rate_lock:
        last_error = "unknown"
        for attempt in range(3):
            wait = _MIN_INTERVAL_SECONDS - (time.monotonic() - _last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                    resp = await client.get(
                        _API_URL,
                        params=params,
                        headers={"User-Agent": "research-agent-cluster/0.1"},
                    )
                _last_request_at = time.monotonic()
                if resp.status_code == 200:
                    return resp
                last_error = f"HTTP {resp.status_code}"
                if resp.status_code not in (429, 500, 502, 503):
                    break
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(_MIN_INTERVAL_SECONDS * (attempt + 1))
        raise RuntimeError(f"arXiv API 请求失败: {last_error}")


def _parse_entries(xml_text: str, limit: int) -> list[PaperRecord]:
    root = ET.fromstring(xml_text)
    papers: list[PaperRecord] = []
    for entry in root.findall(f"{_ATOM}entry")[:limit]:
        id_url = (entry.findtext(f"{_ATOM}id") or "").strip()
        pdf_url = ""
        for link in entry.findall(f"{_ATOM}link"):
            if link.get("title") == "pdf":
                pdf_url = link.get("href", "")
        papers.append(
            PaperRecord(
                arxiv_id=id_url.rsplit("/abs/", 1)[-1],
                title=" ".join((entry.findtext(f"{_ATOM}title") or "").split()),
                authors=[
                    (a.findtext(f"{_ATOM}name") or "").strip()
                    for a in entry.findall(f"{_ATOM}author")
                ],
                published=(entry.findtext(f"{_ATOM}published") or "").strip(),
                abstract=" ".join((entry.findtext(f"{_ATOM}summary") or "").split()),
                pdf_url=pdf_url,
            )
        )
    return papers


@capability(
    id="arxiv.search",
    description="按关键词检索 arXiv 论文，返回标题、作者、发布时间、摘要与 PDF 链接列表。",
    tags=["research", "arxiv"],
)
async def search_papers(query: str, max_results: int = 5) -> list[PaperRecord]:
    limit = max(1, min(max_results, 20))
    resp = await _get_with_retry(
        {
            "search_query": f"all:{query}",
            "start": 0,
            "max_results": limit,
            "sortBy": "relevance",
        }
    )
    return _parse_entries(resp.text, limit)


@capability(
    id="arxiv.fetch_paper",
    description="按 arXiv ID（如 2301.00001 或带版本号 2301.00001v2）获取单篇论文的详细元数据与摘要。",
    tags=["research", "arxiv"],
)
async def fetch_paper(arxiv_id: str) -> PaperRecord:
    resp = await _get_with_retry({"id_list": arxiv_id.strip(), "max_results": 1})
    papers = _parse_entries(resp.text, 1)
    if not papers:
        raise LookupError(f"arXiv 上未找到论文: {arxiv_id}")
    return papers[0]
