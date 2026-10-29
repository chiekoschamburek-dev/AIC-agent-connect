"""PEP 审计日志：JSONL 追加写，遵循 AAC §13（不记完整 token，只记 jti 与原因码）。

对外响应永远是通用文案；细节只落盘。写日志失败不得影响准入裁决（best-effort）。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PEP_VERSION = "yanshu.pep.v1"

AUDIT_FILENAME = "pep-audit.jsonl"

# 审计行的"归属"维度一律落齐（2026-10-15 审查：此前只有 domain-grant 路径才写
# domain/skillHint，现网 88 条 allow 全都没有这两个字段 → 对账报表按域过滤后恒为 0）。
# 无归属不是"没有值"，而是一个要显式记录、可被统计到的取值。
NO_DOMAIN = "(no-domain)"
NO_SKILL = "(undeclared)"

# 技能归属的**证据等级**（2026-10-16）：AIP v2 的 TaskCommand 没有能力选择符，
# 大多数 allow 拿不到"被调用的能力"，只能拿到调用方自报或未指名。对账报表必须带上
# 这一档，否则"按技能核算"的数字会被当成与令牌 claim 同级的凭据读。
SKILL_SOURCE_TOKEN_CLAIM = "token-claim"    # 签名令牌/域 grant 里带的，不可伪造
SKILL_SOURCE_SELF_DECLARED = "self-declared"  # 调用方在 params/metadata 里自报的，仅供参考
SKILL_SOURCE_UNSPECIFIED = "unspecified"     # 压根没指名（现网 88 条 allow 里约 72%）
SKILL_SOURCE_LEGACY = "legacy-unknown"       # 修复前落盘的历史行，没有该字段


def default_audit_path(workspace: Path | str) -> Path:
    # 2026-10-06 审查 #6:放 audit/ 而非 logs/——服务模板的 ExecStartPre 会
    # rm -f logs/*.jsonl(绕 AMP bug),不能把委托审计证据一起清掉
    return Path(workspace) / "audit" / AUDIT_FILENAME


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def audit_log(
    path: Path | str,
    *,
    decision: str,
    rule: str,
    caller_aic: str | None = None,
    my_aic: str | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
    delivery: str | None = None,
    jti: str | None = None,
    delegation_id: str | None = None,
    code: int | None = None,
    reason: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """追加一条 PEP/委托审计记录（不阻塞主流程）。"""
    record: dict[str, Any] = {
        "ts": _utc_now(),
        "decision": decision,
        "rule": rule,
        "callerAic": caller_aic,
        "myAic": my_aic,
        "taskId": task_id,
        "sessionId": session_id,
        "delivery": delivery,
        "jti": jti,
        "delegationId": delegation_id,
        "code": code,
        "reason": reason,
        "pepVersion": PEP_VERSION,
    }
    if extra:
        record.update(extra)
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        pass
