"""商业域"商家控制台"：付费计划（paid_plans）→ GrantToken 发卡（provider 自签发）。

定位（RFC §4 的商业姿态，区别于合作域）：
- 签发权归 **provider 本人**（章程 issuers=[provider AIC]），不是集群 leader；
- 付费在带外（MVP：paid_plans.json 声明"已购计划"；将来换支付回调，接口不变）；
- 发卡规则：客户有未过期计划 → 按计划次数签发 grant（`acps_max_uses` = 售卖次数）；
- 计费底账：PEP 的 sqlite 配额计数 + 对账报表（reconciliation.py），发卡侧只管计划校验。

JWS 纪律与句柄方案同款：本模块产物只交给**代码侧/带外通道**，不进任何模型上下文。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .domain_manifest import parse_expiry
from .grant import IssuedGrant, issue_grant
from .token import SigningContext

PLAN_TEMPLATE = {
    "_说明": "付费计划声明（官网/支付回调的 MVP 替身）。expiresAt 用 ISO 时间或 unix 秒。",
    "plans": [
        {
            "customerAic": "1.2.156.3088.1.1.EXPRMT.LABEXE01.RUN01.1207",
            "plan": "gpu-starter",
            "uses": 3,
            "expiresAt": "2099-01-01T00:00:00Z",
            "skill": "gpu.run",
        }
    ],
}


GRANT_TTL_SECONDS = 7 * 24 * 3600  # 发卡默认时长（实际不超过计划剩余时间）


@dataclass(frozen=True)
class PaidPlan:
    customer_aic: str
    plan: str
    uses: int
    expires_at: str
    skill: str

    def expiry_epoch(self) -> float | None:
        """计划到期时刻；None=不设到期。写法不认识时抛 ValueError。"""
        return parse_expiry(self.expires_at)

    def active(self, *, now: float | None = None) -> bool:
        """到期判定按 epoch 比。

        原先是字符串字典序（`expires_at >= "2026-…Z"`）：PLAN_TEMPLATE 明确允许 unix 秒写法，
        而 "4102444800"（2100 年）与 "946684800"（2000 年）都恒判"有效"，
        "1791381600"（明天到期）恒判"过期"——两个方向都错（2026-10-07 审查修复）。
        """
        checked = now if now is not None else time.time()
        try:
            expiry = self.expiry_epoch()
        except ValueError:
            return False  # 写法不认识 → 当作已过期（fail-closed）
        return expiry is None or expiry >= checked

    def seconds_left(self, *, now: float | None = None) -> float | None:
        expiry = self.expiry_epoch()
        if expiry is None:
            return None
        return expiry - (now if now is not None else time.time())


def load_paid_plans(path: Path | str) -> dict[str, PaidPlan]:
    """读 paid_plans.json → {customerAic: PaidPlan}。文件缺失返回空（没人买过 = 谁都不发卡）。"""
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    plans: dict[str, PaidPlan] = {}
    for item in (data.get("plans") or []):
        if not isinstance(item, Mapping):
            continue
        aic = str(item.get("customerAic") or "").strip()
        if not aic:
            continue
        plans[aic] = PaidPlan(
            customer_aic=aic,
            plan=str(item.get("plan") or "standard"),
            uses=max(1, int(item.get("uses") or 1)),
            expires_at=str(item.get("expiresAt") or ""),
            skill=str(item.get("skill") or "gpu.run"),
        )
    return plans


def issue_paid_grant(
    signing: SigningContext,
    plans: Mapping[str, PaidPlan],
    *,
    customer_aic: str,
    domain: str,
    domain_revision: int,
    provider_aic: str,
    max_grants_per_day: int | None = None,
    quota_path: Path | str | None = None,
    now: float | None = None,
) -> tuple[IssuedGrant | None, str | None]:
    """发卡：客户持有效计划 → 按计划签发 grant（iss=provider，sub=客户，max_uses=计划次数）。

    签发侧配额（RFC §4.7 双轨之一）：传 `max_grants_per_day`（章程值）与 `quota_path`
    （sqlite 账本）时强制执行——滑动 24h 窗口内该 (域, 签发者) 超限即拒发。
    返回 (grant, None) 或 (None, 拒绝原因)。拒绝不签发——商业语义即"没付费/超发卡限额就没有票"。
    """
    plan = plans.get(customer_aic)
    if plan is None:
        return None, "no-paid-plan：该客户没有任何付费记录，不予发卡"
    if not plan.active(now=now):
        return None, f"plan-expired：计划 {plan.plan} 已于 {plan.expires_at} 过期"
    enforce_quota = max_grants_per_day is not None and quota_path is not None
    if enforce_quota:
        from .quota import issues_in_window

        issued = issues_in_window(quota_path, domain=domain, issuer_aic=signing.issuer_aic, now=now)
        if issued >= int(max_grants_per_day):
            return None, (
                f"grant-quota-exceeded：滑动 24h 内已发 {issued} 枚，章程限额 {max_grants_per_day}/日"
            )
    # 凭证时长不超过计划剩余：计划还剩 1 小时却签 7 天 grant，等于免费延长订阅（审查修复）
    ttl = GRANT_TTL_SECONDS
    left = plan.seconds_left(now=now)
    if left is not None:
        ttl = max(60, min(GRANT_TTL_SECONDS, int(left)))
    grant = issue_grant(
        signing,
        domain=domain,
        domain_revision=domain_revision,
        subject_aic=customer_aic,
        assets=[{"providerAic": provider_aic, "skill": plan.skill}],
        max_uses=plan.uses,
        ttl_seconds=ttl,
        plan=plan.plan,
    )
    if enforce_quota:
        from .quota import record_issue

        record_issue(quota_path, domain=domain, issuer_aic=signing.issuer_aic,
                     jti=grant.jti, now=now)
    return grant, None


def write_plan_template(path: Path | str) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text(json.dumps(PLAN_TEMPLATE, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


__all__ = ["PaidPlan", "issue_paid_grant", "load_paid_plans", "write_plan_template"]
