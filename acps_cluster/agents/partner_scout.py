"""研枢-Scout-Partner：多源学术信息收集（arXiv + OpenAlex + Semantic Scholar）。

与 arxiv-parnter（演示版，保留不动）的区别：三大数据源、统一 ScholarRecord 结构。
运行：python acps_cluster/agents/partner_scout.py   （常驻，端口默认 8445）

PEP（PEP_design.md 阶段一 1.3）：本 partner 是第一个启用准入策略的伙伴。
- `scholar.deep_fetch` 声明 public=False ⇒ 不在 ACS 里出现（实测 2026-09-28 校验 ACS 草稿
  字节级不变，故不需要重新注册），是 PEP 演示的「非公开能力」样本。
- `ACPS_PEP_ENABLED=0`（默认）时行为与从前完全一致（framework 默认 accept）。
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from wit import AcpsDirect, WitAgent, capability  # noqa: E402

from acps_cluster.core import cluster_common  # noqa: E402
from acps_cluster.core.discovery_metadata import (  # noqa: E402
    agent_discovery_description,
    apply_to_capabilities,
)
from wit.hooks.acps.amp import AmpAccessLoggingHooks  # noqa: E402
from acps_cluster.core.aip_guard import AipPartnerGuardHooks
from capabilities.arxiv import PaperRecord  # noqa: E402
from capabilities.arxiv import fetch_paper as _arxiv_fetch  # noqa: E402
from capabilities.arxiv import search_papers as _arxiv_search  # noqa: E402
from capabilities.scholar import ScholarRecord  # noqa: E402
from capabilities.scholar import openalex_search as _openalex_search  # noqa: E402
from capabilities.scholar import (  # noqa: E402
    semantic_scholar_search as _s2_search,
)
from capabilities.datasets import fetch_dataset  # noqa: E402
from capabilities.papers_memory import (  # noqa: E402
    PaperMemory,
    default_memory,
    memory_enabled,
)
from capabilities.papers_memory import to_records as _memory_to_records  # noqa: E402
from acps_cluster.delegation import build_pep_hooks, capability_visibility, split_pep_hooks  # noqa: E402
from acps_cluster.delegation.bridge import BridgeHooks  # noqa: E402
from acps_cluster.agents.partner_workspace_client import (  # noqa: E402
    WORKSPACE_ACCESS_NOTE,
    WorkspaceAccessBudgetHooks,
    build_workspace_access_capabilities,
)
from acps_cluster.core.workspace_access import enabled as workspace_access_enabled  # noqa: E402
from wit.hooks.acps.delegation import AipDelegationHooks  # noqa: E402

WORKSPACE = Path(__file__).resolve().parents[1] / "ws-scout"

# ── 论文记忆层（2026-10-11）──────────────────────────────────────────────
# 依据：集群历史检索主题重复率 ≈45%（唯一主题 58/105，高频 diffusion×21、kgc×6…），
# 把检索结果存下来可直接省掉这些网络往返。
# 策略：**记忆优先，但要求"量够"** —— 命中（经相关性下限过滤）且数量 ≥
# min(max_results, _MEMORY_SHORT_CIRCUIT_MIN) 才直接返回；否则联网取全并写回记忆库
# （canonical 去重 + 跨源合并）。
# 2026-10-16 现网事故后收紧：原先"任一关键词单命中即短路"，泛词（models/based/score…）
# 把不相关记录判成命中 → 三条路由全部不联网 → 扩散查询返回 agent-memory/医学批次。
# 隔离：记忆层任何异常都退化为纯联网检索，绝不影响检索本身。
# 关闭：ACPS_SCOUT_MEMORY=0；TTL：ACPS_SCOUT_MEMORY_TTL_DAYS（默认 7 天）。
_MEMORY_SHORT_CIRCUIT_MIN = 5    # 记忆命中数达到该值（或 max_results 更小时取 max_results）才短路联网
_MEMORY: PaperMemory | None = None
_LAST_FROM_MEMORY = False
_MEMORY_LOG: list[str] = []      # 最近若干次检索的出处，供 memory.stats 观测


def _memory() -> PaperMemory | None:
    global _MEMORY
    if not memory_enabled():
        return None
    if _MEMORY is None:
        try:
            _MEMORY = default_memory(WORKSPACE)
        except Exception as exc:  # noqa: BLE001  记忆层故障不能影响检索
            print(f"[scout] 记忆层不可用（退化为纯联网检索）: {type(exc).__name__}: {exc}",
                  flush=True)
            return None
    return _MEMORY


async def _search_with_memory(*, kind: str, query: str, max_results: int,
                              fetch, record_cls) -> list:
    """记忆优先（量够才短路）的检索：命中且数量充足直接返回，否则联网并写回记忆库。"""
    global _LAST_FROM_MEMORY
    mem = _memory()
    if mem is not None:
        try:
            rows = mem.lookup(query, limit=max_results)
            need = min(max_results, _MEMORY_SHORT_CIRCUIT_MIN)
            if len(rows) >= need:
                _LAST_FROM_MEMORY = True
                _MEMORY_LOG.append(f"{kind}:hit:{len(rows)}:{query[:60]}")
                del _MEMORY_LOG[:-50]
                return _memory_to_records(rows, record_cls)
            if rows:
                # 命中但量不够：不短路——联网取全（部分命中不能代表"这个主题查过了"）
                _MEMORY_LOG.append(f"{kind}:partial:{len(rows)}/{need}→live:{query[:60]}")
                del _MEMORY_LOG[:-50]
        except Exception as exc:  # noqa: BLE001
            print(f"[scout] 记忆命中查询失败（继续联网）: {type(exc).__name__}: {exc}",
                  flush=True)

    _LAST_FROM_MEMORY = False
    records = await fetch(query=query, max_results=max_results)
    if mem is not None and records:
        try:
            mem.remember(records, query=query)
        except Exception as exc:  # noqa: BLE001
            print(f"[scout] 写回记忆库失败（不影响本次结果）: {type(exc).__name__}: {exc}",
                  flush=True)
    _MEMORY_LOG.append(f"{kind}:miss:{len(records or [])}:{query[:60]}")
    del _MEMORY_LOG[:-50]
    return records


@capability(
    id="arxiv.search",
    description="按关键词检索 arXiv 论文，返回标题、作者、发布时间、摘要与 PDF 链接。",
    public=True,
    tags=["research", "arxiv", "scout"],
)
async def scout_arxiv_search(query: str, max_results: int = 5) -> list[PaperRecord]:
    return await _search_with_memory(
        kind="arxiv", query=query, max_results=max_results,
        fetch=_arxiv_search, record_cls=PaperRecord)


@capability(
    id="openalex.search",
    description="检索 OpenAlex 全球学术库（覆盖期刊/会议/预印本），返回标题、作者、年份、摘要、DOI 与引用数。",
    public=True,
    tags=["research", "openalex", "scout"],
)
async def scout_openalex_search(
    query: str, max_results: int = 5
) -> list[ScholarRecord]:
    return await _search_with_memory(
        kind="openalex", query=query, max_results=max_results,
        fetch=_openalex_search, record_cls=ScholarRecord)


@capability(
    id="semanticscholar.search",
    description="检索 Semantic Scholar（学术引用图谱），返回标题、作者、年份、摘要、引用数与链接。限流时自动退避。",
    public=True,
    tags=["research", "semanticscholar", "scout"],
)
async def scout_s2_search(query: str, max_results: int = 5) -> list[ScholarRecord]:
    return await _search_with_memory(
        kind="s2", query=query, max_results=max_results,
        fetch=_s2_search, record_cls=ScholarRecord)


@capability(
    id="scholar.deep_fetch",
    description="高成本批量文献深取（单次最多 25 条带完整摘要），仅供被授权的伙伴调用。",
    public=False,
    tags=["research", "scout", "delegation"],
)
async def scout_deep_fetch(query: str, max_results: int = 25) -> list[ScholarRecord]:
    return await _openalex_search(query=query, max_results=max(max_results, 1))


@capability(
    id="arxiv.fetch",
    description="按 arXiv ID 取回单篇论文记录（标题/作者/摘要/PDF 链接）。引用核验的取源通道。",
    public=False,  # 取全文有成本，走委托 token
    acps_skill={"visibility": "domain", "domains": ["yanshu-scout"]},
    tags=["research", "arxiv", "scout", "citation"],
)
async def scout_arxiv_fetch(arxiv_id: str) -> dict:
    try:
        record = await _arxiv_fetch(arxiv_id=arxiv_id)
    except LookupError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "paper": {
            "arxiv_id": record.arxiv_id,
            "title": record.title,
            "authors": record.authors,
            "published": record.published,
            "abstract": record.abstract,
            "pdf_url": record.pdf_url,
        },
    }


@capability(
    id="dataset.fetch",
    description=(
        "按关键词在 OpenML 上查找数据集，取回少量样本行（默认 100 行）并以 **CSV 文本**返回，"
        "供下游无网络沙盒中的实验代码直接使用。返回 {dataset:{name,url,rows,columns}, csv, note}。"
    ),
    public=False,  # 不进 ACS（实测新增非公开能力不改 ACS 指纹）→ 服务器 scout 无需重新注册
    tags=["research", "dataset", "scout"],
)
async def scout_dataset_fetch(query: str, max_rows: int = 100) -> dict:
    slice_ = await fetch_dataset(query, max_rows=max(1, min(int(max_rows or 100), 500)))
    if slice_ is None:
        return {"ok": False, "error": f"OpenML 上没找到匹配的数据集（query={query!r}）"}
    return {
        "ok": True,
        "dataset": {
            "source": slice_.source,
            "id": slice_.dataset_id,
            "name": slice_.name,
            "url": slice_.url,
            "rows": slice_.rows,
            "columns": slice_.columns,
        },
        "csv": slice_.csv,
        "note": slice_.note,
    }


@capability(
    id="memory.stats",
    description=(
        "论文记忆库统计：已存论文数、时效内/过期条数、各来源分布、TTL、库文件路径，"
        "以及最近若干次检索的**出处**（hit=走记忆、miss=联网）。只读。"
    ),
    public=False,  # 不改 ACS 投影 → scout 无需重新注册（改 True 会导致 wit 拒绝启动）
    tags=["research", "scout", "memory"],
)
async def scout_memory_stats() -> dict:
    mem = _memory()
    if mem is None:
        return {"enabled": False, "note": "记忆层未启用（ACPS_SCOUT_MEMORY=0）"}
    try:
        st = mem.stats()
    except Exception as exc:  # noqa: BLE001
        return {"enabled": True, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
    st["enabled"] = True
    st["recentAccess"] = list(_MEMORY_LOG[-20:])
    return st


@capability(
    id="memory.search",
    description=(
        "在**已存论文记忆库**里按主题/关键词检索（不联网，毫秒级）。"
        "返回标题、作者、年份、摘要、链接、引用数，以及该记录见过的来源（provenance）。"
        "适合先看库里有没有，避免重复联网检索。只读。"
    ),
    public=False,  # 不改 ACS 投影 → scout 无需重新注册（改 True 会导致 wit 拒绝启动）
    tags=["research", "scout", "memory"],
)
async def scout_memory_search(query: str, max_results: int = 5) -> list:
    """在本地论文记忆库中按主题检索（不联网，毫秒级）。"""
    mem = _memory()
    if mem is None:
        return []
    limit = max(1, min(int(max_results or 5), 25))
    try:
        rows = mem.lookup(query, limit=limit)
    except Exception as exc:  # noqa: BLE001
        print(f"[scout] memory.search 失败: {type(exc).__name__}: {exc}", flush=True)
        return []
    return [
        {
            "title": r.get("title") or "",
            "authors": [a for a in str(r.get("authors") or "").split("; ") if a],
            "year": r.get("year"),
            "abstract": r.get("abstract") or "",
            "url": r.get("url") or "",
            "pdf_url": r.get("pdf_url") or "",
            "citations": r.get("citations"),
            "sources": [s for s in str(r.get("sources") or "").split("; ") if s],
            "from_memory": True,
        }
        for r in rows
    ]


SCOUT_CAPABILITIES = [
    scout_arxiv_search,
    scout_openalex_search,
    scout_s2_search,
    scout_deep_fetch,
    scout_arxiv_fetch,
    scout_dataset_fetch,
    scout_memory_stats,
    scout_memory_search,
]


def build_scout_agent(provider) -> WitAgent:
    """装配「研枢-Scout-Partner」agent：模型、工具与 hooks 的公开构建入口。"""
    ws_enabled = workspace_access_enabled("scout")
    capabilities = list(SCOUT_CAPABILITIES)
    hooks: list = []
    if ws_enabled:
        capabilities.extend(build_workspace_access_capabilities(WORKSPACE / "tokens"))
        hooks.extend([AipDelegationHooks(), BridgeHooks(),
                      WorkspaceAccessBudgetHooks(WORKSPACE / "tokens")])
    public_skills, has_non_public = capability_visibility(capabilities)
    pep_hooks = build_pep_hooks(
        capabilities=capabilities,
        workspace=WORKSPACE,
        public_skills=public_skills,
        has_non_public_skills=has_non_public,
    )
    pep_run_hooks, pep_aip_hooks = split_pep_hooks(pep_hooks)
    hooks.extend(pep_run_hooks)
    if os.getenv("ACPS_PEP_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}:
        print(f"[scout] PEP 已启用: {pep_hooks[0].config.describe()}")
    instructions = (
        "你是研枢平台的对外信息收集员（Scout）。收到检索任务后：优先按主题选择"
        "合适数据源（前沿预印本用 arxiv，全库与引用数用 openalex，引用图谱用 "
        "semanticscholar）。硬性纪律：每个数据源最多调用一次、工具调用总数不超过 3 次；"
        "无论任务要求多少篇，最迟在第 4 步必须直接输出资料清单（每条含标题、作者、"
        "年份、来源、链接与 1 句要点），数量不足也要交，绝不为了凑数反复检索。"
        "不要编造论文。"
    )
    if ws_enabled:
        instructions += "\n" + WORKSPACE_ACCESS_NOTE
    return WitAgent(
        id="yanshu.scout-partner",
        display_name="研枢-Scout-Partner",
        version="1.10.0",
        description=agent_discovery_description('yanshu.scout-partner', "科研信息收集员：跨 arXiv / OpenAlex / Semantic Scholar 三源检索文献，追踪前沿热点与高引工作。"),
        instructions=instructions,
        model=provider,
        capabilities=apply_to_capabilities(capabilities, agent_id="yanshu.scout-partner"),
        hooks=hooks,
        aip_hooks=[AipPartnerGuardHooks(), *pep_aip_hooks],
        amp_hooks=[AmpAccessLoggingHooks()],
        protocols=[AcpsDirect()],
    )


async def main() -> None:
    await cluster_common.serve_partner(
        build_scout_agent,
        workspace=WORKSPACE,
        port_env="ACPS_SCOUT_PORT",
        default_port=8445,
        creds_prefix="SCOUT",
    )


if __name__ == "__main__":
    asyncio.run(main())
