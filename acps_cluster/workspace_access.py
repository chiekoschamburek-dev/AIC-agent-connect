"""伙伴工作区访问开关（supervisor 与各 partner 共用同一份环境口径）。

`ACPS_WS_PARTNER_ACCESS`：逗号分隔的名册 key。默认七个研究伙伴全部开启；
显式设为空串即关闭。supervisor 用同一份列表决定是否随委派签发 ws.* 令牌，
partner 用它决定是否装配 ws 客户端工具——两侧必须一致，否则会出现
"有令牌没工具"或"有工具没令牌"（后者 fail-closed 返回 awaiting，不会越权）。
"""

from __future__ import annotations

import os

ENV = "ACPS_WS_PARTNER_ACCESS"
DEFAULT_PARTNER_KEYS = (
    "scout",
    "experiment",
    "synthesis",
    "writer",
    "critic",
    "contradiction",
    "frontier",
)


def access_keys() -> frozenset[str]:
    raw = os.getenv(ENV)
    if raw is None:
        return frozenset(DEFAULT_PARTNER_KEYS)
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def enabled(key: str) -> bool:
    return str(key or "").strip() in access_keys()
