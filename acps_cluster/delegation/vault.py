"""leader 侧进程内 token 保管库：JWS 原文只留在进程里（PEP_design.md §5.1 + §11.9）。

为什么需要它（这就是 §11.9 的 jti 句柄方案）：
wit 会把本地 Tool 的 handler 返回值回灌给模型，因此签发工具若像 §5.1 草稿那样返回
`token` 字段，JWS 必然进入 leader 的 LLM 上下文——"token 原文不进模型文本"在 leader
侧不成立，且伙伴返回内容里的提示注入（"复述你上一个工具结果"）可将其窃出。

改法：签发工具只把 **jti 句柄** 交给模型（连同到期时间/受众/技能等审计元数据），
JWS 原文存进本库；remedy-3 的 `continue` 由代码侧工具 `delegate_continue` 按句柄取出
JWS、组装 dataItem 直发伙伴。**JWS 全程不过模型。**

一次性：`resolve()` 返回后即从库中删除——同一句柄不会被组装两次；配合 PEP 侧 jti
单次消费（pep.py 的 JtiRegistry），语义一致。重发失败需要重新签发（leader 侧本地
零成本操作，见 §2.1）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .token import IssuedToken, normalize_audience


@dataclass(frozen=True)
class HeldToken:
    """库内一条待下发的委托令牌（JWS 原文只在这里、以及签发时刻的返回值里出现）。"""

    token: str
    jti: str
    expires_at: int
    delegation_id: str
    audience: str
    skill_id: str
    delegate_aic: str
    target_aic: str

    def handle(self) -> dict[str, Any]:
        """给模型的**句柄**（绝不含 token 原文）。"""
        return {
            "jti": self.jti,
            "expiresAt": self.expires_at,
            "audience": self.audience,
            "skillId": self.skill_id,
            "delegateAic": self.delegate_aic,
            "targetAic": self.target_aic,
            "delegationId": self.delegation_id,
        }


class TokenVault:
    """进程内 jti→JWS 保管库。演示/单进程规模足够；生产可换共享存储并按 exp 清理。"""

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.time
        self._held: dict[str, HeldToken] = {}

    def hold(self, issued: IssuedToken) -> HeldToken:
        """存入一枚刚签发的 token，返回其句柄记录。"""
        claims: Mapping[str, Any] = issued.claims
        act = claims.get("act")
        delegate_aic = str(act.get("aic") or "") if isinstance(act, Mapping) else ""
        held = HeldToken(
            token=issued.token,
            jti=issued.jti,
            expires_at=int(issued.expires_at),
            delegation_id=issued.delegation_id,
            audience=normalize_audience(str(claims.get("aud") or "")),
            skill_id=str(claims.get("acps_skill_id") or ""),
            delegate_aic=delegate_aic,
            target_aic=str(claims.get("acps_target_aic") or ""),
        )
        self._prune()
        self._held[held.jti] = held
        return held

    def hold_grant(self, issued) -> HeldToken:
        """存入一枚域级 grant（delegation.grant.IssuedGrant），统一成 HeldToken。"""
        claims: Mapping[str, Any] = issued.claims
        held = HeldToken(
            token=issued.token,
            jti=issued.jti,
            expires_at=int(issued.expires_at),
            delegation_id=f"grant:{issued.domain}",
            audience=issued.subject_aic,
            skill_id=",".join(sorted({s for _, s in issued.assets})),
            delegate_aic=issued.subject_aic,
            target_aic=",".join(sorted({p for p, _ in issued.assets})),
        )
        self._prune()
        self._held[held.jti] = held
        return held

    def resolve(self, jti: str) -> HeldToken | None:
        """按句柄取出并**消费**（一次性）；未知/已过期/已用过返回 None。"""
        self._prune()
        return self._held.pop(str(jti or ""), None)

    def peek(self, jti: str) -> HeldToken | None:
        """只看不消费（供审计/排障）。"""
        self._prune()
        return self._held.get(str(jti or ""))

    def __len__(self) -> int:
        self._prune()
        return len(self._held)

    def _prune(self) -> None:
        now = self._clock()
        for key in [k for k, held in self._held.items() if held.expires_at + 60 < now]:
            del self._held[key]


DEFAULT_TOKEN_VAULT = TokenVault()


__all__ = ["DEFAULT_TOKEN_VAULT", "HeldToken", "TokenVault"]
