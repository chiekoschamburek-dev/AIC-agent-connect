"""引用核验执行层：机械流水线，不是判断——审稿 LLM 与核验 LLM 必须分离。

四步（第①步零 LLM，第③步每引用一次 focused 小调用）：
① 解析草稿 → 抽取 (论断句, 被引来源) 对（确定性正则，引文标记 [n]）
② 逐条取源：优先 delegated_fetch 持令牌直连 scout（桥已就绪）；无令牌时降级
   本机 llm_tools 仅做"摘要自洽性"核验并标记 source=unverified
③ 每条引用一次小调用：论断 vs 原文摘录 → supported/misrepresented/fabricated/unverifiable
④ 汇成 verdict 表——供 supervisor 的 review_gate 与 critic 的 evidence_check 交叉对账

设计约束（来自架构讨论）：
- 核验 LLM ≠ 审稿 LLM（自我复核悖论：生成 evidence_check 的和核验它的不能是同一次调用）；
- 逐条小上下文：每次调用只有一条论断+一段摘录，不把 N 篇全文塞进一次调用；
- 超时友好：单条独立调用，代码侧循环，不撞 300s 工具超时。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any

MAX_CITATIONS = 30
_EXCERPT_CHARS = 1200

_CITE_PATTERN = re.compile(r"\[(\d{1,2})\]")
_ARXIV_ID_PATTERN = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?", re.IGNORECASE)

VERDICTS = ("supported", "misrepresented", "fabricated", "unverifiable")


@dataclass
class CitationCheck:
    index: int            # 引文编号 [n]
    claim: str            # 含 [n] 的论断句（截选）
    source_ref: str       # 参考文献条目原文（含 arXiv ID 若有）
    verdict: str          # supported / misrepresented / fabricated / unverifiable
    evidence: str         # 支撑该裁决的原文摘录（或"未取到来源"说明）
    source: str           # arxiv（真实取源）/ unverified（降级模式，未取到原文）


def _ref_map(references: str) -> dict[int, str]:
    """参考文献清单 → {编号: 条目}。每行一条，可无显式编号（按序对应）。"""
    ref_map: dict[int, str] = {}
    for i, line in enumerate([line.strip() for line in references.splitlines() if line.strip()], 1):
        explicit = re.match(r"^\[(\d{1,2})\]\s*(.*)", line)
        if explicit:
            ref_map[int(explicit.group(1))] = explicit.group(2)
        else:
            ref_map[i] = line
    return ref_map


def dangling_citations(draft: str, references: str) -> list[tuple[int, str]]:
    """草稿引了但清单里没有的 [n]：返回 [(编号, 论断句)]。

    这类引用过去在 parse_citations 里被静默丢弃，于是"引用不存在的文献"这种
    最典型的编造形态既不进 verdict 表、也不影响 ok=True（2026-10-07 审查修复）。
    """
    if not isinstance(draft, str) or not isinstance(references, str):
        return []
    known = _ref_map(references)
    out: list[tuple[int, str]] = []
    seen: set[int] = set()
    for sentence in re.split(r"(?<=[。．.!?！？])\s*|\n+", draft):
        for match in _CITE_PATTERN.finditer(sentence):
            n = int(match.group(1))
            if n in known or n in seen:
                continue
            seen.add(n)
            out.append((n, sentence.strip()[:500]))
    return out


def parse_citations(draft: str, references: str) -> list[tuple[int, str, str]]:
    """确定性抽取：返回 [(编号, 论断句, 参考文献条目)]。无 LLM。

    约定：草稿用 [n] 引用，references 是编号清单（每行一条，可无显式编号——按序对应）。
    清单里没有的编号不在此返回，见 dangling_citations。
    """
    if not isinstance(draft, str) or not isinstance(references, str):
        return []
    ref_map = _ref_map(references)

    results: list[tuple[int, str, str]] = []
    seen: set[int] = set()
    for sentence in re.split(r"(?<=[。．.!?！？])\s*|\n+", draft):
        for match in _CITE_PATTERN.finditer(sentence):
            n = int(match.group(1))
            if n in seen or n not in ref_map:
                continue
            seen.add(n)
            results.append((n, sentence.strip()[:500], ref_map[n]))
        if len(results) >= MAX_CITATIONS:
            break
    return results


def arxiv_id_of(ref_line: str) -> str | None:
    match = _ARXIV_ID_PATTERN.search(ref_line or "")
    return match.group(1) if match else None


_VERIFY_SYSTEM = """你是引用核验员。给你一条论断和它的被引来源摘录，判定引用是否成立。
只输出一个 JSON 对象：{"verdict": "supported|misrepresented|fabricated|unverifiable",
"evidence": "<支撑裁决的原文关键句（截选，不改写）>"}
判定标准：
- supported：摘录内容确实支撑论断；
- misrepresented：来源存在但说的不是论断的事（断章取义/过度引申）；
- fabricated：论断引用了来源里不存在的内容（来源无此信息）；
- unverifiable：摘录信息不足以判定。"""


async def verify_one(claim: str, excerpt: str, *, source_label: str) -> CitationCheck:
    """单条引用的 focused 核验调用（每次调用只看一条论断+一段摘录）。"""
    import json as _json

    import llm_tools

    raw = await llm_tools.chat(
        _VERIFY_SYSTEM,
        f"论断：{claim}\n\n来源摘录（{source_label}）：\n{excerpt[:_EXCERPT_CHARS]}",
        temperature=0.1,
    )
    verdict, evidence = "unverifiable", ""
    try:
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
        blob = fenced.group(1) if fenced else (re.search(r"\{.*\}", raw, re.S) or re.match(r"$^", "")).group(0) if re.search(r"\{.*\}", raw, re.S) else "{}"
        data = _json.loads(blob)
        verdict = str(data.get("verdict") or "unverifiable")
        evidence = str(data.get("evidence") or "")
    except (ValueError, AttributeError):
        pass
    if verdict not in VERDICTS:
        verdict = "unverifiable"
    return CitationCheck(
        index=-1, claim=claim, source_ref=source_label,
        verdict=verdict, evidence=evidence[:600], source=source_label,
    )


def _target_endpoint(target_aic: str) -> str | None:
    """目标端点：本地开发映射（ws-<vendor> 的 enrollment），未命中返回 None。"""
    import json as _json
    from pathlib import Path as _P

    import os as _os

    override = _os.getenv("ACPS_CITATION_TARGET_ENDPOINT")
    if override:
        return override
    base = _P(__file__).parent
    for ws in base.glob("ws-*"):
        enrollment = ws / "acps" / "enrollment.json"
        if not enrollment.is_file():
            continue
        try:
            data = _json.loads(enrollment.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if str(((data.get("identity") or {}).get("ref") or {}).get("aic") or "") == target_aic:
            return str(((data.get("acpsConfig") or {}).get("publicEndpoint")) or "") or None
    return None


async def fetch_source_via_bridge(arxiv_id: str, *, token: str | None = None) -> dict[str, Any] | None:
    """取源：真实 mTLS wire 请求 scout 的 arxiv.fetch（AipDirectClient 模式）。

    每次取源一枚新 token（一次性语义）+ 一条真实 TaskCommand + 轮询 get 至终态。
    产物里的工具结果信封解包后返回 {ok, paper}。
    """
    import asyncio as _asyncio
    import uuid as _uuid
    from datetime import datetime, timezone as _tz

    from delegation.bridge import current_delegation

    delegation = current_delegation()
    if delegation is None:
        return None
    if delegation.get("skillId") != "arxiv.fetch" and token is None:
        return None
    target_aic = delegation.get("targetAic") or ""
    use_token = token or delegation["token"]
    if not target_aic or not use_token:
        return None

    from wit.acps.direct import AipDirectClient

    from pathlib import Path as _P
    trust = _P(__file__).parent / "ws-scout" / "acps" / "trust-bundle.pem"
    caller_ws = _P(__file__).parent / "ws-critic"

    client = AipDirectClient(
        client_certificate=caller_ws / "acps" / "client" / "certificate.pem",
        client_private_key=caller_ws / "acps" / "client" / "private-key.pem",
        trust_bundle=trust,
        local_aic=delegation.get("callerAic", ""),
        strict_interop=True,
    )
    session_id = f"session-cite-{_uuid.uuid4()}"
    task_id = f"task-cite-{_uuid.uuid4()}"
    from delegation.token import token_data_item

    payload = token_data_item(use_token, target_aic=target_aic, skill_id="arxiv.fetch")
    command = {
        "type": "task-command", "id": f"msg-{_uuid.uuid4()}",
        "sentAt": datetime.now(_tz.utc).isoformat(),
        "senderRole": "leader", "senderId": delegation.get("callerAic", ""),
        "sessionId": session_id, "taskId": task_id,
        "command": "start", "commandParams": {},
        "dataItems": [
            {"type": "text", "text": f"取论文 {arxiv_id}", "metadata": {"skillId": "arxiv.fetch"}},
            payload,
        ],
    }
    try:
        endpoint = _target_endpoint(target_aic)
        if endpoint is None:
            return None
        resp = await client.call(
            endpoint,
            {"jsonrpc": "2.0", "method": "rpc", "id": f"req-{_uuid.uuid4()}",
             "params": {"command": command}},
            expected_partner_aic=target_aic,
        )
        # 轮询 get 直到终态
        for _ in range(30):
            await _asyncio.sleep(2)
            command["command"] = "get"
            command["id"] = f"msg-{_uuid.uuid4()}"
            resp = await client.call(
                endpoint,
                {"jsonrpc": "2.0", "method": "rpc", "id": f"req-{_uuid.uuid4()}",
                 "params": {"command": command}},
                expected_partner_aic=target_aic,
            )
            state = str((((resp or {}).get("result") or {}).get("status") or {}).get("state") or "")
            if state in {"completed", "failed", "rejected", "canceled"}:
                break
        # 从 products 提取工具结果信封
        result = (resp or {}).get("result") or {}

        for product in result.get("products") or []:
            for item in product.get("dataItems") or product.get("data_items") or []:
                if not isinstance(item, dict) or item.get("type") != "text":
                    continue
                try:
                    parsed = json.loads(str(item.get("text") or ""))
                except ValueError:
                    continue
                if isinstance(parsed, dict):
                    if isinstance(parsed.get("result"), dict):
                        return parsed["result"]
                    if "ok" in parsed or "paper" in parsed:
                        return parsed
        return None
    finally:
        await client.aclose()

async def verify_citations(
    draft: str, references: str, *, token_factory=None
) -> dict[str, Any]:
    """完整流水线：解析 → 取源（桥，降级无源）→ 逐条小调用 → verdict 表。

    `token_factory`：每次取源签发一枚**新** task token（一次性语义——同一枚 jti
    第二次准入会被 PEP 判 replay）。缺省退化为 ContextVar 里的令牌（只够取一次源）。
    """
    pairs = parse_citations(draft, references)
    dangling = dangling_citations(draft, references)
    if not pairs and not dangling:
        return {"ok": False, "error": "草稿里没有可解析的 [n] 引用（或 references 为空）"}

    checks: list[CitationCheck] = []
    # 引了清单里不存在的编号＝无源可核，直接判 fabricated（也由此进入 review_gate 的对账）
    for number, claim in dangling:
        checks.append(CitationCheck(
            index=number, claim=claim, source_ref="（参考文献清单里没有这个编号）",
            verdict="fabricated", evidence="无此条目，谈不上支撑或歪曲", source="unverified"))
    fetched: dict[str, dict] = {}
    for n, claim, ref_line in pairs:
        arxiv_id = arxiv_id_of(ref_line)
        excerpt, source_label = None, ref_line
        if arxiv_id and arxiv_id not in fetched:
            token = token_factory() if token_factory else None
            _fetched = await fetch_source_via_bridge(arxiv_id, token=token)
            fetched[arxiv_id] = _fetched or {}
        if arxiv_id:
            paper = fetched.get(arxiv_id) or {}
            record = paper.get("paper") or {}
            abstract = str(record.get("abstract") or "")
            title = str(record.get("title") or "")
            if abstract:
                excerpt, source_label = f"标题：{title}\n摘要：{abstract}", f"arxiv:{arxiv_id}"
        if excerpt is None:
            # 降级：未取到原文（无令牌/非 arXiv 源），只用参考文献条目自洽性核验并标记
                    excerpt, source_label = f"参考文献条目：{ref_line}", "unverified"
        check = await verify_one(claim, excerpt, source_label=source_label)
        check.index = n
        check.source_ref = ref_line
        checks.append(check)

    counts = {v: sum(1 for c in checks if c.verdict == v) for v in VERDICTS}
    return {
        "ok": True,
        "total": len(checks),
        "counts": counts,
        "fabricated": counts["fabricated"],
        "misrepresented": counts["misrepresented"],
        "unverifiedSources": sum(1 for c in checks if c.source == "unverified"),
        "checks": [asdict(c) for c in checks],
        "note": "verdict 表供 review_gate 与审稿者 evidence_check 交叉对账；"
                "source=unverified 表示未取到原文（无令牌或非 arXiv），裁决仅基于条目自洽性",
    }
