"""域级资源量配额（GPU 卡时 / CPU 秒）：PEP 定边，provider 在运行时检查并记账。

## 为什么需要它

任务级令牌只约束"能不能调、能调几次"（`acps_max_uses`），域章程只约束"谁能调、
发多少枚 grant"（`maxGrantsPerDay`）。但科研协作里真正稀缺的是**算力时长**：
一次 `gpu.run` 可以合法地跑 600 秒，十次就是 100 分钟。只数调用次数，等于把
"每人每天最多 1 小时 GPU"这类章程条款停在纸面上。

本模块把该条款落成三段可执行语义：

1. **定边**：`DomainPolicy.resource_limits()` 从章程的 `resourceQuota`
   （键形如 `gpuSecondsPerCallerPerDay`）解析出 `{资源: 每日上限}`；
2. **传递**：PEP 在域授权放行时把 `(域, 调用方, 上限, 账本路径)` 写进 ContextVar
   ——与 `skill_scope` 同一套上下文传递方式，不额外引入进程间通道；
3. **执行**：资源节点（`partner_gpu`）在**真正开跑之前**查余量、跑完后按时长记账
   （`quota.record_resource`）。余量耗尽返回 `quota-exhausted`，不再启动沙盒。

## 边界与取舍

- **只记账、不预付**：余量在开跑前检查，跑完按实测时长入账。所以最后一次调用
  可以超出余量（最多一个 `timeout_seconds`）——这是"先跑后记"的固有代价，
  比"预估时长再扣"更不容易误伤真实任务；需要严格上限时把 timeout 设成预算余量。
- **口径是 UTC 自然日**：与 `maxGrantsPerDay` 的滑动 24 小时窗口不同。自然日
  便于对外解释（"每天多少卡时"），滑动窗口更平滑；两者混用会让人算不清，故分开写。
- **没有域上下文就不记账**：L0/L1 入场、PEP 关闭、或本机未持域章程时，
  ContextVar 为空，本模块全部是空操作——保持既有的"无域即无额外约束"行为。
- 关闭开关 `ACPS_DOMAIN_RESOURCE_QUOTA=off` 可整体停用（排查与压测用），
  默认启用：章程一旦写了额度，就得真的生效。
"""

from __future__ import annotations

import os
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from . import quota

DAY_SECONDS = 86400.0
ENV_TOGGLE = "ACPS_DOMAIN_RESOURCE_QUOTA"


@dataclass(frozen=True)
class BudgetScope:
    """一次域授权携带的资源预算上下文（由 PEP 写入，资源节点读取）。"""

    domain: str
    caller: str
    limits: Mapping[str, float] = field(default_factory=dict)
    ledger: Path | None = None
    revision: int | None = None

    def describes(self) -> dict[str, Any]:
        """给启动日志/审计用（不含任何密钥）。"""
        return {
            "domain": self.domain,
            "caller": self.caller,
            "limits": dict(self.limits),
            "ledger": str(self.ledger) if self.ledger else None,
            "revision": self.revision,
        }


_SCOPE: ContextVar[BudgetScope | None] = ContextVar("yanshu_domain_budget", default=None)


def enabled() -> bool:
    """总开关：默认开。仅显式 `ACPS_DOMAIN_RESOURCE_QUOTA=off/0/false/no` 时关闭。"""
    raw = os.getenv(ENV_TOGGLE, "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def set_scope(scope: BudgetScope | None) -> None:
    """由 `pep.pep_decide` 在裁决结束时调用；拒绝时传 None 清空，避免串任务。"""
    _SCOPE.set(scope)


def current_scope() -> BudgetScope | None:
    return _SCOPE.get()


def clear() -> None:
    _SCOPE.set(None)


def utc_day_start(now: float | None = None) -> float:
    """当前 UTC 自然日的零点（epoch 秒）。"""
    moment = float(now if now is not None else time.time())
    return moment - (moment % DAY_SECONDS)


def _ledger_path(scope: BudgetScope) -> Path | None:
    return Path(scope.ledger) if scope.ledger else None


def usage(resource: str, *, now: float | None = None, scope: BudgetScope | None = None) -> float | None:
    """本 UTC 自然日该 (域, 调用方, 资源) 已用掉的量；无域上下文时返回 None。"""
    current = scope or _SCOPE.get()
    if current is None:
        return None
    path = _ledger_path(current)
    if path is None:
        return None
    return quota.resource_used(
        path,
        domain=current.domain,
        caller_aic=current.caller,
        resource=resource,
        since=utc_day_start(now),
    )


def remaining(resource: str, *, now: float | None = None, scope: BudgetScope | None = None) -> float | None:
    """余量；章程没给该资源定额或没有域上下文时返回 None（= 不受限）。"""
    current = scope or _SCOPE.get()
    if current is None:
        return None
    limit = current.limits.get(resource)
    if limit is None:
        return None
    used = usage(resource, now=now, scope=current) or 0.0
    return float(limit) - float(used)


def check(resource: str, *, now: float | None = None) -> dict[str, Any] | None:
    """开跑前的余量检查：返回拒绝载荷（dict）表示不该开跑，返回 None 表示放行。

    只在"有域上下文 + 章程给了该资源定额 + 已用额度不小于上限"时拒绝；
    其余情况（包括定额为 0 以上但还有余量）一律放行。
    """
    if not enabled():
        return None
    scope = _SCOPE.get()
    if scope is None:
        return None
    left = remaining(resource, now=now, scope=scope)
    if left is None or left > 0:
        return None
    limit = float(scope.limits.get(resource) or 0.0)
    used = usage(resource, now=now, scope=scope) or 0.0
    return {
        "ok": False,
        "error": "quota-exhausted",
        "resource": resource,
        "domain": scope.domain,
        "caller": scope.caller,
        "domainRevision": scope.revision,
        "limit": limit,
        "used": round(used, 3),
        "detail": (
            f"域 {scope.domain} 对调用方 {scope.caller} 的 {resource} 日配额已用尽"
            f"（上限 {limit:g}，已用 {used:.1f}）；按章程需等下一个 UTC 自然日或双签修订额度"
        ),
    }


def charge(resource: str, amount: float, *, now: float | None = None) -> dict[str, Any] | None:
    """跑完后按实测用量记账；无域上下文/账本时是空操作（返回 None）。

    返回记账回执（含 remainingAfter），便于工具层把"这次花了多少、还剩多少"
    一并回给调用方——余量对调用方是可见的，不需要它自己算。
    """
    if not enabled() or amount is None:
        return None
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    scope = _SCOPE.get()
    if scope is None:
        return None
    path = _ledger_path(scope)
    if path is None:
        return None
    quota.record_resource(
        path,
        domain=scope.domain,
        caller_aic=scope.caller,
        resource=resource,
        amount=value,
        now=now,
    )
    left = remaining(resource, now=now, scope=scope)
    return {
        "resource": resource,
        "amount": round(value, 3),
        "domain": scope.domain,
        "caller": scope.caller,
        "remainingAfter": None if left is None else round(max(left, 0.0), 3),
    }