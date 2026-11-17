"""双向互调证据的数据模型（纯标准库，离线可测）。

一条 `TaskReceipt` = 一个方向的平台任务的可对账快照。它刻意只保留"能被第三方
复核"的字段（taskId、双方 AIC、方向、终态、时间、产物摘要），不携带 payload 原文
（可能含用户数据），也不携带任何凭据。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping

SCHEMA_RECEIPT = "yanshu.bidirectional.receipt/1"
SCHEMA_PROBE = "yanshu.bidirectional.probe/1"
SCHEMA_PACK = "yanshu.bidirectional.evidence/1"

DIRECTIONS = ("outbound", "inbound")

#: 终态：任务不会再变化（`awaiting-completion` 不在其中——它是"产物已交付、等调用方关单"）
TERMINAL_STATES = frozenset(
    {"completed", "failed", "rejected", "canceled", "cancelled", "expired"}
)
#: 唯一算"双向调用成立"的终态。`awaiting-completion` 只算"已交付未关单"，如实降级。
PASS_STATE = "completed"

#: 方向的中文标签（写进 Markdown 证据包）
DIRECTION_LABELS = {
    "outbound": "本机 → 对端（本机为 Leader）",
    "inbound": "对端 → 本机（本机为 Partner）",
}


def normalize_state(raw: Any) -> str:
    """任务状态归一：去空白、转小写；兼容 `Completed` / `completed ` 这类写法。"""
    return str(raw or "").strip().lower()


def is_terminal(state: Any) -> bool:
    return normalize_state(state) in TERMINAL_STATES


def direction_label(direction: str) -> str:
    return DIRECTION_LABELS.get(str(direction), str(direction))


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _walk_text(value: Any) -> int:
    """统计产物里 text 型 DataItem 的字符数（不递归任意字符串，避免把 JSON 结构算进去）。"""
    total = 0
    if isinstance(value, list):
        for item in value:
            total += _walk_text(item)
    elif isinstance(value, Mapping):
        kind = str(value.get("type") or "")
        if kind == "text":
            total += len(str(value.get("text") or ""))
        for key in ("dataItems", "data_items", "products"):
            if key in value:
                total += _walk_text(value[key])
        if kind == "data" and isinstance(value.get("data"), Mapping):
            total += _walk_text(value["data"])
    return total


def _coerce_products(raw: Any) -> Any:
    """把 `products` 列（JSON 文本 / list / dict / None）解析成 Python 结构。"""
    if raw is None or isinstance(raw, (list, dict)):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


@dataclass(frozen=True)
class ProductSummary:
    """产物摘要：只留数量、正文字符数与整体 sha256（用于跨侧对账，不回传正文）。"""

    count: int = 0
    chars: int = 0
    sha256: str = ""

    @property
    def empty(self) -> bool:
        return self.count <= 0 and self.chars <= 0

    @classmethod
    def from_raw(cls, raw: Any) -> "ProductSummary":
        value = _coerce_products(raw)
        if value is None:
            return cls()
        if isinstance(value, Mapping):
            items = value.get("products") if isinstance(value.get("products"), list) else [value]
        elif isinstance(value, list):
            items = value
        else:
            text = str(value)
            return cls(count=1 if text else 0, chars=len(text),
                       sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())
        count = len(items)
        chars = _walk_text(items)
        digest = hashlib.sha256(_canonical_json(items).encode("utf-8")).hexdigest()
        return cls(count=count, chars=chars, sha256=digest)

    def as_dict(self) -> dict[str, Any]:
        return {"count": self.count, "chars": self.chars, "sha256": self.sha256}


@dataclass(frozen=True)
class TaskReceipt:
    """一个方向的平台任务回执（可对账快照）。"""

    direction: str
    task_ref: str
    state: str = ""
    local_aic: str = ""
    remote_aic: str = ""
    created_at: str = ""
    updated_at: str = ""
    session_id: str = ""
    local_error: str = ""
    products: ProductSummary = field(default_factory=ProductSummary)
    events: tuple[dict[str, Any], ...] = ()
    source: Mapping[str, Any] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.direction not in DIRECTIONS:
            raise ValueError(f"direction 必须是 {DIRECTIONS} 之一，收到 {self.direction!r}")

    @property
    def terminal(self) -> bool:
        return is_terminal(self.state)

    @property
    def delivered_unclosed(self) -> bool:
        return normalize_state(self.state) == "awaiting-completion"

    @property
    def ok(self) -> bool:
        return normalize_state(self.state) == PASS_STATE

    @property
    def caller_aic(self) -> str:
        return self.local_aic if self.direction == "outbound" else self.remote_aic

    @property
    def callee_aic(self) -> str:
        return self.remote_aic if self.direction == "outbound" else self.local_aic

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_RECEIPT,
            "direction": self.direction,
            "direction_label": direction_label(self.direction),
            "task_ref": self.task_ref,
            "state": self.state,
            "terminal": self.terminal,
            "caller_aic": self.caller_aic,
            "callee_aic": self.callee_aic,
            "local_aic": self.local_aic,
            "remote_aic": self.remote_aic,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "session_id": self.session_id,
            "local_error": self.local_error,
            "products": self.products.as_dict(),
            "events": [dict(event) for event in self.events],
            "source": dict(self.source),
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "TaskReceipt":
        """从 JSON 回执还原（容忍缺字段：对端回执可能由别的版本生成）。"""
        if not isinstance(payload, Mapping):
            raise TypeError("回执必须是 JSON 对象")
        raw_products = payload.get("products")
        if isinstance(raw_products, Mapping) and {"count", "chars"} <= set(raw_products):
            products = ProductSummary(
                count=int(raw_products.get("count") or 0),
                chars=int(raw_products.get("chars") or 0),
                sha256=str(raw_products.get("sha256") or ""),
            )
        else:
            products = ProductSummary.from_raw(raw_products)
        events = payload.get("events") or ()
        return cls(
            direction=str(payload.get("direction") or ""),
            task_ref=str(payload.get("task_ref") or payload.get("task_id") or ""),
            state=normalize_state(payload.get("state")),
            local_aic=str(payload.get("local_aic") or ""),
            remote_aic=str(payload.get("remote_aic") or ""),
            created_at=str(payload.get("created_at") or ""),
            updated_at=str(payload.get("updated_at") or ""),
            session_id=str(payload.get("session_id") or ""),
            local_error=str(payload.get("local_error") or ""),
            products=products,
            events=tuple(dict(event) for event in events if isinstance(event, Mapping)),
            source=dict(payload.get("source") or {}),
            notes=tuple(str(note) for note in (payload.get("notes") or ())),
        )


__all__ = [
    "DIRECTIONS",
    "DIRECTION_LABELS",
    "PASS_STATE",
    "SCHEMA_PACK",
    "SCHEMA_PROBE",
    "SCHEMA_RECEIPT",
    "TERMINAL_STATES",
    "ProductSummary",
    "TaskReceipt",
    "direction_label",
    "is_terminal",
    "normalize_state",
]