"""矛盾侦测的纯逻辑层：数据模型 + 来源校验（不依赖 wit，便于离线单测）。

与 capabilities/scholar.py 同层：只放确定性逻辑，LLM 调用与 ACPs public 包装在
partner_contradiction.py 里。

核心纪律：模型声称"发现矛盾"不算数。每条矛盾必须同时满足——
  1. 引用两个**不同**的来源 id（同篇论文内部前后不一致不是跨论文矛盾）；
  2. 两个 id 都真实出现在本次输入的资料里（防止编造文献来凑矛盾）；
  3. 给出调和假说与可检验预测（否则这条"矛盾"对选题没有价值）。
不满足的一律丢弃并记入 dropped，供 leader 审计。
"""

import re
from dataclasses import dataclass, field

CONTRADICTION_TYPES = (
    "直接冲突",  # A 说 X 有效、B 说 X 无效
    "条件依赖冲突",  # 结论各自成立但边界条件不同（数据集/规模/领域）
    "度量口径冲突",  # 指标定义或评测协议不同导致的表观冲突
    "结论外推冲突",  # 一方把有限条件下的结论外推到更广范围
)

_ARXIV_RE = re.compile(r"\b(\d{4}\.\d{4,5})(v\d+)?\b")
_DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s,\uff0c;\uff1b)\]]+)")
_BRACKET_RE = re.compile(r"[\[【(（]([^\[\]【】()（）]{3,60})[\]】)）]")
# 方括号里只有长得像标识符的才算来源 id。否则中文括号里的说明性文字
# （如"（arXiv:2401.00001，2024）"或"（2024）"）会被当成来源，污染白名单、
# 进而让"来源不存在"这道校验失效——2026-09-26 实测踩过。
_ID_LIKE_RE = re.compile(r"^(?:[A-Za-z][\w.\-]{2,39}|\d{4}\.\d{4,5}(?:v\d+)?)$")
_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")


@dataclass
class ContradictionPair:
    claim_a: str
    source_a: str
    evidence_a: str
    claim_b: str
    source_b: str
    evidence_b: str
    contradiction_type: str
    reconciliation: str  # 调和假说
    testable_prediction: str  # 可检验预测
    confidence: str = "medium"


@dataclass
class ContradictionReport:
    topic: str
    pairs: list[ContradictionPair] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    sources_used: list[str] = field(default_factory=list)


def normalize_id(value: str) -> str:
    """把来源 id 归一成规范形式。

    - 含 arXiv 号时只保留号码本身（"arXiv:2401.00001v2，2024" → "2401.00001"）；
    - 否则去掉 "arXiv:" 前缀与尾随标点。
    这样模型把引用写成 "arXiv:2401.00001，2024" 这种带修饰的形式时，
    仍能归一到与资料抽取结果一致的 id，不会因格式差异被误判为"来源不存在"。
    """
    text = str(value or "").strip()
    match = _ARXIV_RE.search(text)
    if match:
        return match.group(1)
    text = re.sub(r"^arxiv[:\s]*", "", text, flags=re.IGNORECASE)
    return text.strip().strip("，,;；。.")


def _is_id_like(token: str) -> bool:
    """方括号里的 token 是否像来源标识符而非说明性文字。"""
    if _YEAR_RE.match(token) or not _ID_LIKE_RE.match(token):
        return False
    return True


def extract_source_ids(text: str) -> list[str]:
    """从资料文本里抽取来源 id（arXiv / DOI / 方括号标识符），保序去重。"""
    found: list[str] = []
    for match in _ARXIV_RE.finditer(text or ""):
        found.append(match.group(1))
    for match in _DOI_RE.finditer(text or ""):
        found.append(match.group(1))
    for match in _BRACKET_RE.finditer(text or ""):
        token = match.group(1).strip()
        if _is_id_like(token) and not token.startswith("http"):
            found.append(token)
    seen: set[str] = set()
    ordered: list[str] = []
    for item in found:
        norm = normalize_id(item)
        if norm and norm not in seen:
            seen.add(norm)
            ordered.append(norm)
    return ordered


def _dedup_key(pair: ContradictionPair) -> tuple:
    a, b = sorted((normalize_id(pair.source_a), normalize_id(pair.source_b)))
    return (a, b, pair.claim_a.strip().lower()[:40])


def validate_pairs(
    raw_pairs: object, valid_ids: list[str]
) -> tuple[list[ContradictionPair], list[str]]:
    """校验模型给出的候选矛盾对，返回 (采纳的, 丢弃理由列表)。"""
    allowed = {normalize_id(v) for v in valid_ids if str(v).strip()}
    kept: list[ContradictionPair] = []
    dropped: list[str] = []
    seen: set[tuple] = set()

    if not isinstance(raw_pairs, list):
        return [], ["模型未返回 pairs 数组"]

    for i, raw in enumerate(raw_pairs):
        if not isinstance(raw, dict):
            dropped.append(f"#{i} 不是对象")
            continue
        pair = ContradictionPair(
            claim_a=str(raw.get("claim_a") or "").strip(),
            source_a=normalize_id(raw.get("source_a")),
            evidence_a=str(raw.get("evidence_a") or "").strip(),
            claim_b=str(raw.get("claim_b") or "").strip(),
            source_b=normalize_id(raw.get("source_b")),
            evidence_b=str(raw.get("evidence_b") or "").strip(),
            contradiction_type=str(raw.get("contradiction_type") or "").strip(),
            reconciliation=str(raw.get("reconciliation") or "").strip(),
            testable_prediction=str(raw.get("testable_prediction") or "").strip(),
            confidence=str(raw.get("confidence") or "medium").strip().lower(),
        )
        label = f"#{i}({pair.source_a or '?'} vs {pair.source_b or '?'})"

        if not (pair.claim_a and pair.claim_b):
            dropped.append(f"{label} 缺少任一侧的论点表述")
            continue
        if pair.source_a == pair.source_b:
            dropped.append(f"{label} 两侧引的是同一来源，不是跨论文矛盾")
            continue
        if allowed:
            unknown = [s for s in (pair.source_a, pair.source_b) if s not in allowed]
            if unknown:
                dropped.append(f"{label} 引用了资料清单里不存在的来源：{', '.join(unknown)}")
                continue
        if not pair.reconciliation:
            dropped.append(f"{label} 未给出调和假说")
            continue
        if not pair.testable_prediction:
            dropped.append(f"{label} 未给出可检验预测")
            continue
        if pair.contradiction_type not in CONTRADICTION_TYPES:
            pair.contradiction_type = "直接冲突"
        if pair.confidence not in ("high", "medium", "low"):
            pair.confidence = "medium"

        key = _dedup_key(pair)
        if key in seen:
            dropped.append(f"{label} 与前一条重复（同一来源对 + 相同论点）")
            continue
        seen.add(key)
        kept.append(pair)

    return kept, dropped
