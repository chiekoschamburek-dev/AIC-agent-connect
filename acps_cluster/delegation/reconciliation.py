"""对账（双向认账）：把 pep-audit 的 JSONL 聚合成带双方签名的用量报表（CDAP 第⑥步地基）。

语义：provider 侧产出"谁的资产被谁用了多少次"的周期报表；provider 与 consumer 各签一次
（同一套 Ed25519/x5c 机制），任何一方事后无法抵赖用量。MVP 消费 pep-audit 落盘记录；
生产形态换成消费 AMP 的签名 Access 记录（接口不变：输入是记录流，输出是双签报表）。

扯皮仲裁依据：报表记录裁决所依据的 domainRevision（第九轮 revision 规则）。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .audit import (
    NO_DOMAIN,
    NO_SKILL,
    SKILL_SOURCE_LEGACY,
    SKILL_SOURCE_TOKEN_CLAIM,
)
from .token import SigningContext, TokenError, sign_payload, verify_payload_jws

REPORT_TYP = "RECON"
_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"  # 与 audit._utc_now() 同格式：定长 UTC，字典序即时间序


def _record_field(line: str, field: str) -> Any:
    try:
        return json.loads(line.strip()).get(field)
    except (ValueError, AttributeError):
        return None


def _parse_ts(stamp: str) -> datetime | None:
    try:
        return datetime.strptime(stamp, _TS_FMT).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def audit_window(audit_lines: Iterable[str]) -> tuple[str, str]:
    """审计行里实际出现的 ts 跨度（左闭右开后各扩 1 秒）。

    给演示/自检用：报表口径仍以显式 `window` 为准，但窗口该从哪来总得有个事实依据，
    免得再出现"硬编码一个过去的日期、却声称统计了本次运行"那种假数（2026-10-15 审查）。
    """
    stamps = sorted(
        str(ts)
        for ts in (_record_field(line, "ts") for line in audit_lines)
        if ts
    )
    if not stamps:
        return ("", "")
    return (_shift(stamps[0], -1), _shift(stamps[-1], 1))


def _shift(stamp: str, seconds: int) -> str:
    moment = _parse_ts(stamp)
    if moment is None:
        return stamp
    return (moment + timedelta(seconds=seconds)).strftime(_TS_FMT)


def build_report(
    audit_lines: Iterable[str],
    *,
    domain: str,
    domain_revision: int,
    window: tuple[str, str],
    provider_aic: str | Iterable[str],
) -> dict[str, Any]:
    """聚合用量：只统计【窗口内 + 该域 + 本 provider】的 allow 裁决，按 (调用方, 技能) 计数。

    口径（2026-10-15 审查修复；此前 `window` 只是标签、从不按 ts 过滤，
    "换成 2025 年"与"本次运行"两份报表逐字相同，而签名报表是要给对方认账的）：

    - **计数对象 = 准入(allow)裁决次数**，不是能力执行成功次数——与 RFC §4.7
      "acps_max_uses = 单枚 GT 可准入次数" 同口径；准入后执行失败/超时**不回滚**，
      对方若按"成功访问次数"记账必然对不上，这是定义差异而非漏计。
    - **时间窗**：`window[0] <= ts < window[1]`（ts 与窗口同为 `%Y-…T%H:%M:%SZ`
      的 UTC 定长格式，字典序即时间序）。缺 ts 或格式非法的行**一律排除**并计入
      `excluded.noTimestamp`——签名报表宁可少计不猜。
    - **provider 维度**：审计行的 `myAic` 存在时必须落在 `provider_aic` 里，否则排除并计入
      `excluded.otherProvider`（没有该字段的历史行不参与此过滤）。`provider_aic` 可以是
      单个 AIC，也可以是**身份谱系**（换身份前的旧号 + 现役号）——现网 89 行审计里
      有 34 行仍挂在换身份前的旧 `myAic` 上，只传现役号会把那段历史判成别人的用量。
    - 归属缺失不再等于不可见：PEP 侧对无域 allow 落 `NO_DOMAIN`、无技能落 `NO_SKILL`，
      调用方要统计"非域路径的用量"就把 `domain=NO_DOMAIN` 传进来。
    - **技能按证据等级分档**：`skill` 优先取 `skillHint`（校验过的凭据路径才有），
      否则退到调用方自报的 `declaredSkill`，都没有才 `(undeclared)`；每行带
      `skillSource ∈ {token-claim, self-declared, unspecified, legacy-unknown}`，
      汇总另有 `bySkillSource`。**只有 token-claim 那一档可以当对账凭据**——现网
      88 条 allow 里约 72% 是 `unspecified`（AIP v2 的 TaskCommand 没有能力选择符），
      拿总次数当"按技能核算"的账单会让对方以为我们已经证明了用了哪个能力。

    返回体带 `excluded`/`byRule` 两组诊断计数，让"为什么和你的数不一样"当场可读。
    """
    usage: dict[tuple[str, str], int] = {}
    by_rule: dict[str, int] = {}
    by_source: dict[str, int] = {}
    excluded = {
        "outOfWindow": 0, "noTimestamp": 0, "otherDomain": 0,
        "otherProvider": 0, "notAllow": 0, "unparsable": 0,
    }
    win_from, win_to = _parse_ts(window[0]), _parse_ts(window[1])
    provider_set = ({provider_aic} if isinstance(provider_aic, str)
                    else {str(x) for x in provider_aic})
    scanned = 0
    for line in audit_lines:
        line = line.strip()
        if not line:
            continue
        scanned += 1
        try:
            record = json.loads(line)
        except ValueError:
            excluded["unparsable"] += 1
            continue
        if not isinstance(record, dict):  # 台账被截断成裸值/null 之类
            excluded["unparsable"] += 1
            continue
        if record.get("decision") != "allow":
            excluded["notAllow"] += 1
            continue
        stamp = str(record.get("ts") or "")
        moment = _parse_ts(stamp)
        if moment is None:
            excluded["noTimestamp"] += 1
            continue
        # 窗口自身非法时一律算作窗外（fail-closed：签名报表不猜区间）
        if not (win_from and win_to and win_from <= moment < win_to):
            excluded["outOfWindow"] += 1
            continue
        if str(record.get("domain") or NO_DOMAIN) != domain:
            excluded["otherDomain"] += 1
            continue
        mine = str(record.get("myAic") or "")
        if mine and mine not in provider_set:
            excluded["otherProvider"] += 1
            continue
        caller = str(record.get("callerAic") or "?")
        # skillHint 只在校验过的凭据路径上有值（哨兵值等同于没有）；没有时才退到
        # 调用方自报的 declaredSkill——两者的证据等级不同，故每行都要带 skillSource，
        # 免得对方把"自报能力"的计数当成可入账的凭据。
        hint = str(record.get("skillHint") or "")
        if hint == NO_SKILL:
            hint = ""
        skill = hint or str(record.get("declaredSkill") or "") or NO_SKILL
        source = str(record.get("skillSource") or (SKILL_SOURCE_TOKEN_CLAIM if hint
                                                   else SKILL_SOURCE_LEGACY))
        usage[(caller, skill, source)] = usage.get((caller, skill, source), 0) + 1
        by_source[source] = by_source.get(source, 0) + 1
        rule = str(record.get("rule") or "?")
        by_rule[rule] = by_rule.get(rule, 0) + 1
    return {
        "kind": "reconciliation",
        "basis": "allow-admissions（准入计数，非执行成功数；见模块 docstring）",
        "domain": domain,
        "domainRevision": domain_revision,
        "window": {"from": window[0], "to": window[1], "applied": True},
        "providerAic": (provider_aic if isinstance(provider_aic, str)
                        else sorted(provider_set)),
        "providerAics": sorted(provider_set),
        "usage": [
            {"callerAic": caller, "skill": skill, "skillSource": source, "count": count}
            for (caller, skill, source), count in sorted(usage.items())
        ],
        "totalUses": sum(usage.values()),
        "byRule": dict(sorted(by_rule.items())),
        "bySkillSource": dict(sorted(by_source.items())),
        "scanned": scanned,
        "excluded": excluded,
    }


def sign_report(report: Mapping[str, Any], signing: SigningContext) -> dict[str, Any]:
    """附加一条签名（同一方可覆盖自己的旧签名），返回带 signatures 的报表。"""
    body = {k: v for k, v in report.items() if k != "signatures"}
    jws = sign_payload(signing, body, typ=REPORT_TYP)
    signatures = [
        old
        for old in (report.get("signatures") or [])
        if isinstance(old, str) and _local_signer(old) != signing.issuer_aic
    ]
    signatures.append(jws)
    return {**body, "signatures": signatures}


def _local_signer(jws: str) -> str | None:
    try:
        from .token import _parse_token, aic_from_certificate

        header, _, signature, signing_input = _parse_token(jws)
        import base64

        from cryptography import x509

        leaf = x509.load_der_x509_certificate(base64.b64decode(header["x5c"][0]))
        leaf.public_key().verify(signature, signing_input)
        return aic_from_certificate(leaf)
    except Exception:  # noqa: BLE001
        return None


def verify_report(
    report: Mapping[str, Any],
    *,
    trust_bundle: Path | str,
    expected_signers: Iterable[str],
) -> tuple[dict[str, Any], frozenset[str]]:
    """验证双签：每条签名对应正文、签名者覆盖 expected_signers。失败抛 TokenError。"""
    from .token import canonical_payload

    body = {k: v for k, v in report.items() if k != "signatures"}
    canonical = canonical_payload(body)
    signers: set[str] = set()
    for jws in report.get("signatures") or []:
        if not isinstance(jws, str):
            raise TokenError("report-bad-signature", "signature entry must be a JWS string")
        payload, signer = verify_payload_jws(jws, trust_bundle=trust_bundle)
        if canonical_payload(payload) != canonical:
            raise TokenError("report-content-mismatch", "签名对应的正文与当前报表不一致")
        signers.add(signer)
    missing = set(expected_signers) - signers
    if missing:
        raise TokenError(
            "report-undercount-signed",
            f"报表缺少签名方: {sorted(missing)}（双向认账要求双方都签）",
        )
    return body, frozenset(signers)


def read_audit(audit_path: Path | str) -> list[str]:
    path = Path(audit_path)
    if not path.is_file():
        return []
    return path.read_text(encoding="utf-8").splitlines()
