"""前沿探测的纯逻辑层：数据模型 + 主题校验（不依赖 wit，便于离线单测）。

与 capabilities/scholar.py 同层：scholar 负责取真实引用数字，本模块负责把模型给出的
主题归类**收敛到真实数据上**。

核心纪律：速度数字全部来自 API，不由模型生成；模型只做"把哪些工作归为同一方向、
这个方向值不值得入场"的判断，且必须通过**序号**引用已扫描到的工作（不让模型复述
OpenAlex URL，避免抄错或编造）。引用不足两个真实工作、或序号越界的主题一律丢弃。
"""

from dataclasses import dataclass, field

from .scholar import EmergingWork

ENTRY_WINDOWS = ("可入场", "已过窗口期", "需进一步验证")

HEURISTIC_NOTE = (
    "入场时机是依据引用速度分布给出的启发式判断，不是可验证结论；"
    "速度与主题密度均来自 OpenAlex 真实数据，可按代表工作的 id 复核。"
)


@dataclass
class EmergingTopic:
    topic: str
    why_emerging: str
    entry_window: str  # 取值见 ENTRY_WINDOWS
    representative_ids: list[str]  # 真实 work id（已由代码从序号映射）
    representative_titles: list[str]
    velocity_evidence: str  # 对真实数字的转述


@dataclass
class FrontierReport:
    query: str
    window_months: int
    scanned: int  # 实际扫描到的工作数
    topics: list[EmergingTopic] = field(default_factory=list)
    works: list[EmergingWork] = field(default_factory=list)
    density: list[tuple[str, int]] = field(default_factory=list)  # 主题 -> 窗口内工作数
    dropped: list[str] = field(default_factory=list)
    heuristic_note: str = HEURISTIC_NOTE


def topic_density(
    works: list[EmergingWork], *, top_n: int = 8, per_work: int = 2
) -> list[tuple[str, int]]:
    """统计窗口内工作在各主题上的聚集度，返回按聚集度降序的 (主题, 工作数)。

    这是"新兴"的活动量信号：近 6-12 个月刚发表的论文引用数普遍只有个位数，
    单看引用速度噪声很大（实测 0.15-0.45 引用/月、总引用 1-5 次），
    而"同一主题下短期内聚集了多篇新工作"是更稳健的爆发迹象。
    每篇只取最相关的前 per_work 个主题，避免长尾标签把计数冲淡。
    """
    counts: dict[str, int] = {}
    for work in works:
        for name in work.topics[:per_work]:
            counts[name] = counts.get(name, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n]


def _velocity_span(works: list[EmergingWork]) -> str:
    if not works:
        return "无"
    speeds = [w.citations_per_month for w in works]
    return f"{min(speeds):.2f}-{max(speeds):.2f} 引用/月（{len(works)} 篇）"


def validate_topics(
    raw_topics: object, works: list[EmergingWork]
) -> tuple[list[EmergingTopic], list[str]]:
    """把模型给的主题收敛到真实工作上，返回 (采纳的, 丢弃理由列表)。

    raw_topics 每项形如::

        {"topic": "...", "why_emerging": "...", "entry_window": "可入场",
         "work_index": [1, 4], "velocity_evidence": "..."}

    work_index 是 1-based 序号，指向本次扫描到的工作清单。
    """
    kept: list[EmergingTopic] = []
    dropped: list[str] = []

    if not isinstance(raw_topics, list):
        return [], ["模型未返回 topics 数组"]

    for i, raw in enumerate(raw_topics):
        if not isinstance(raw, dict):
            dropped.append(f"#{i} 不是对象")
            continue
        name = str(raw.get("topic") or "").strip()
        label = f"#{i}({name or '?'})"
        if not name:
            dropped.append(f"{label} 缺少方向名称")
            continue

        indexes = raw.get("work_index")
        if not isinstance(indexes, list):
            dropped.append(f"{label} 未给出 work_index 数组")
            continue
        picked: list[EmergingWork] = []
        for item in indexes:
            try:
                pos = int(item)
            except (TypeError, ValueError):
                continue
            if 1 <= pos <= len(works):
                picked.append(works[pos - 1])
        # 去重（同一序号重复出现）
        unique: list[EmergingWork] = []
        for work in picked:
            if work not in unique:
                unique.append(work)
        if len(unique) < 2:
            dropped.append(
                f"{label} 只引用了 {len(unique)} 篇有效工作，不足以支撑一个'新兴方向'（需 >=2）"
            )
            continue

        why = str(raw.get("why_emerging") or "").strip()
        if not why:
            dropped.append(f"{label} 未说明为何判定为新兴")
            continue

        entry = str(raw.get("entry_window") or "").strip()
        if entry not in ENTRY_WINDOWS:
            entry = "需进一步验证"

        kept.append(
            EmergingTopic(
                topic=name,
                why_emerging=why,
                entry_window=entry,
                representative_ids=[w.native_id for w in unique],
                representative_titles=[w.title for w in unique],
                velocity_evidence=str(
                    raw.get("velocity_evidence") or _velocity_span(unique)
                ),
            )
        )

    return kept, dropped
