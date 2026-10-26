"""研枢-Synthesis-Partner：文献精读与细粒度学术要素萃取。

对应申请书"Knowledge Synthesis Agent"：突破粗粒度摘要，按学术 Schema 抽取
核心创新点 / 研究缺口 / 方法论 / 实验配置 四类结构化字段。
运行：python acps_cluster/partner_synthesis.py   （常驻，端口默认 8446）
"""

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wit import AcpsDirect, WitAgent, capability  # noqa: E402

import cluster_common  # noqa: E402
from discovery_metadata import (  # noqa: E402
    agent_discovery_description,
    apply_to_capabilities,
)
import llm_tools  # noqa: E402
from wit.hooks.acps.amp import AmpAccessLoggingHooks  # noqa: E402
from aip_guard import AipPartnerGuardHooks
from delegation import build_pep_hooks, capability_visibility, split_pep_hooks  # noqa: E402
from delegation.bridge import BridgeHooks  # noqa: E402
from partner_workspace_client import (  # noqa: E402
    WORKSPACE_ACCESS_NOTE,
    WorkspaceAccessBudgetHooks,
    build_workspace_access_capabilities,
)
from workspace_access import enabled as workspace_access_enabled  # noqa: E402
from wit.hooks.acps.delegation import AipDelegationHooks  # noqa: E402

WORKSPACE = Path(__file__).parent / "ws-synthesis"


@dataclass
class PaperDigest:
    core_innovation: str
    research_gap: str
    methodology: str
    experiment_setup: str
    keywords: list[str] = field(default_factory=list)


_SCHEMA_KEYS = [
    "core_innovation",
    "research_gap",
    "methodology",
    "experiment_setup",
    "keywords",
]


def _extract_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError(f"模型未返回 JSON: {text[:200]}")
    return json.loads(match.group(0))


# ── digest 缓存（2026-10-16）：同篇论文不重复烧 LLM ──────────────────────────
# 依据：集群检索主题重复率 ≈45%（papers_memory 统计），同一批经典论文被反复精读；
# 精读结果按"论文"缓存即可整篇秒回。键：arXiv ID（去版本号）优先，否则归一化全文哈希。
# 注意：paper.extract 的**注册描述不能动**（在 ACS 里），缓存说明只写在这里与 docstring。
_DIGEST_DB = WORKSPACE / "digests.sqlite3"
_arXiv_ID_RE = re.compile(r"\b(\d{4}\.\d{4,5})(?:v\d+)?\b")


def _digest_cache_enabled() -> bool:
    return os.getenv("ACPS_SYNTHESIS_DIGEST_CACHE", "1").strip().lower() not in {
        "0", "false", "no", "off"}


def _digest_cache_ttl_days() -> float:
    try:
        return float(os.getenv("ACPS_SYNTHESIS_DIGEST_TTL_DAYS", "90") or "90")
    except ValueError:
        return 90.0


def _digest_key(paper_text: str) -> str:
    """缓存键：arXiv ID 仅作前缀，始终叠加全文哈希。

    只按“文本里恰好一个 arXiv ID”做键会让引用该 ID 的另一篇论文/片段命中同一
    摘要，先写者可缓存投毒；全文哈希保证内容不同即键不同（P2-7）。
    """
    ids = {m.group(1) for m in _arXiv_ID_RE.finditer(paper_text or "")}
    normalized = re.sub(r"\s+", " ", (paper_text or "").strip()).lower()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]
    if len(ids) == 1:
        return f"arxiv:{next(iter(ids))}:{digest}"
    return f"txt:{digest}"


def _digest_from_dict(data: dict) -> PaperDigest:
    return PaperDigest(
        core_innovation=str(data.get("core_innovation") or "文中未说明"),
        research_gap=str(data.get("research_gap") or "文中未说明"),
        methodology=str(data.get("methodology") or "文中未说明"),
        experiment_setup=str(data.get("experiment_setup") or "文中未说明"),
        keywords=[str(k) for k in (data.get("keywords") or [])][:6],
    )


def _digest_cache_get(key: str, *, logger=print) -> PaperDigest | None:
    if not _digest_cache_enabled() or not _DIGEST_DB.is_file():
        return None
    try:
        con = sqlite3.connect(_DIGEST_DB, timeout=5)
        try:
            row = con.execute(
                "SELECT digest_json, fetched_at FROM digests WHERE key=?", (key,)).fetchone()
        finally:
            con.close()
        if not row:
            return None
        if time.time() - float(row[1]) > _digest_cache_ttl_days() * 86400.0:
            return None
        return _digest_from_dict(json.loads(row[0]))
    except Exception as exc:  # noqa: BLE001  缓存故障不影响抽取
        logger(f"[synthesis] digest 缓存读取失败（忽略，走 LLM）: {type(exc).__name__}: {exc}",
               flush=True)
        return None


def _digest_cache_put(key: str, digest: PaperDigest, *, logger=print) -> None:
    if not _digest_cache_enabled():
        return
    payload = json.dumps({
        "core_innovation": digest.core_innovation,
        "research_gap": digest.research_gap,
        "methodology": digest.methodology,
        "experiment_setup": digest.experiment_setup,
        "keywords": list(digest.keywords),
    }, ensure_ascii=False)
    try:
        con = sqlite3.connect(_DIGEST_DB, timeout=5)
        try:
            con.execute(
                "CREATE TABLE IF NOT EXISTS digests ("
                "key TEXT PRIMARY KEY, digest_json TEXT NOT NULL, fetched_at REAL NOT NULL)")
            con.execute(
                "INSERT INTO digests(key, digest_json, fetched_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET digest_json=excluded.digest_json, "
                "fetched_at=excluded.fetched_at",
                (key, payload, time.time()))
            con.commit()
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001
        logger(f"[synthesis] digest 缓存写入失败（忽略）: {type(exc).__name__}: {exc}",
               flush=True)


@capability(
    id="paper.extract",
    description="输入论文文本（标题+摘要或正文片段），按学术 Schema 抽取核心创新点、研究缺口、方法论与实验配置，输出结构化要素。",
    public=True,
    tags=["research", "synthesis", "schema"],
)
async def extract_digest(paper_text: str) -> PaperDigest:
    key = _digest_key(paper_text)
    cached = _digest_cache_get(key)
    if cached is not None:
        print(f"[synthesis] digest 命中缓存（{key}）——不调 LLM", flush=True)
        return cached
    resp = await llm_tools.chat(
        system=(
            "你是学术文献精读专家。严格依据给定文本抽取要素，文本未提及的字段写"
            '"文中未说明"，禁止编造。只输出一个 JSON 对象，键固定为: '
            + ", ".join(_SCHEMA_KEYS)
            + "，其中 keywords 是 3-6 个中文关键词的数组，其余为不超过 80 字的字符串。"
        ),
        user=paper_text[:12000],
        temperature=0.2,
    )
    digest = _digest_from_dict(_extract_json(resp))
    _digest_cache_put(key, digest)
    return digest


def build_synthesis_agent(provider) -> WitAgent:
    ws_enabled = workspace_access_enabled("synthesis")
    capabilities = [extract_digest]
    hooks: list = []
    if ws_enabled:
        capabilities = [*capabilities, *build_workspace_access_capabilities(WORKSPACE / "tokens")]
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
        print(f"[synthesis] PEP 已启用: {pep_hooks[0].config.describe()}", flush=True)
    instructions = (
        "你是研枢平台的知识综合员。收到论文文本后调用 paper.extract 完成"
        "结构化萃取，并把结果原样返回，不要添加寒暄。"
    )
    if ws_enabled:
        instructions += "\n" + WORKSPACE_ACCESS_NOTE
    return WitAgent(
        id="yanshu.synthesis-partner",
        display_name="研枢-Synthesis-Partner",
        version="1.1.0",  # 与平台已批记录对齐（2026-10-14 换身份）
        description=agent_discovery_description('yanshu.synthesis-partner', "文献精读员：按学术 Schema 将论文拆解为创新点/缺口/方法/实验四要素。"),
        instructions=instructions,
        model=provider,
        capabilities=apply_to_capabilities(capabilities, agent_id="yanshu.synthesis-partner"),
        # remedy-3 下发的 delegation token 不得进入模型上下文（cfg=None 纯删除，见 delegation/client.py）
        hooks=hooks,
        aip_hooks=[AipPartnerGuardHooks(), *pep_aip_hooks],
        amp_hooks=[AmpAccessLoggingHooks()],
        protocols=[AcpsDirect()],
    )


async def main() -> None:
    await cluster_common.serve_partner(
        build_synthesis_agent,
        workspace=WORKSPACE,
        port_env="ACPS_SYNTHESIS_PORT",
        default_port=8446,
        creds_prefix="SYNTHESIS",
    )


if __name__ == "__main__":
    asyncio.run(main())
