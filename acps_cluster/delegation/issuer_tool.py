"""leader 侧本地签发工具（PEP_design.md §5.1 + §11.9 的 jti 句柄方案）。

与 `plan_gate` / `product_gate` 同款：run-scoped 本地代码工具，不改变 ACS。
私钥只在 Supervisor 进程内加载并缓存。

**工具只返回 jti 句柄，不返回 JWS 原文**（2026-10-02 修正 §11.9 的设计矛盾）：
wit 会把 Tool handler 的返回值回灌给模型，若这里返回 `token`，JWS 必然进入 leader 的
LLM 上下文——"token 原文不进模型文本"在 leader 侧不成立，伙伴返回内容里的提示注入
（如"复述你上一个工具结果"）可将其窃出。现在 JWS 存进 `TokenVault`，模型只拿到
`{ok, jti, expiresAt, audience, skillId, delegateAic, targetAic, delegationId}`。
remedy-3 的 `continue` 由 `delegate_continue`（同模块）按句柄取原文、代码侧组装 dataItem，
JWS 全程不过模型。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from wit import Tool

from .token import (
    DEFAULT_TTL_SECONDS,
    MAX_TTL_SECONDS,
    SigningContext,
    TokenError,
    issue_token,
    load_signing_context,
)
from .vault import DEFAULT_TOKEN_VAULT, TokenVault


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    return [part.strip() for part in text.split(",") if part.strip()]


def build_issue_token_tool(
    signing: SigningContext,
    *,
    trust_bundle: Path | str | None = None,
    default_ttl_seconds: int = DEFAULT_TTL_SECONDS,
    tool_name: str = "issue_delegation_token",
    vault: TokenVault | None = None,
    max_targets: int = 8,
) -> Tool:
    """`issue_delegation_token(delegate_aic, target_aic|targets, skill_id, ...)` → 句柄（不含 JWS）。

    支持一次签发多枚（§5.3 平行扇出）：`targets` 给多个目标 AIC，每个目标一枚 token
    （同一 skill），返回 `handles` 数组——leader 一次工具调用即可完成整批签发。
    """
    store = vault if vault is not None else DEFAULT_TOKEN_VAULT

    async def issue_delegation_token(arguments: dict | str) -> dict[str, Any]:
        args = arguments if isinstance(arguments, dict) else {"delegate_aic": str(arguments)}
        delegate_aic = str(args.get("delegate_aic") or "").strip()
        targets = _as_str_list(args.get("targets"))
        single = str(args.get("target_aic") or "").strip()
        if single and single not in targets:
            targets = [single, *targets]
        skill_id = str(args.get("skill_id") or "").strip()
        missing = [
            name
            for name, value in (("delegate_aic", delegate_aic), ("skill_id", skill_id))
            if not value
        ]
        if not targets:
            missing.append("target_aic/targets")
        if missing:
            return {"ok": False, "error": f"缺少必填参数: {', '.join(missing)}"}
        if len(targets) > max_targets:
            return {"ok": False, "error": f"targets 最多 {max_targets} 个，收到 {len(targets)}"}
        ttl_raw = args.get("ttl_seconds") or default_ttl_seconds
        try:
            ttl_seconds = int(ttl_raw)
        except (TypeError, ValueError):
            return {"ok": False, "error": f"ttl_seconds 必须是整数: {ttl_raw!r}"}
        if ttl_seconds <= 0 or ttl_seconds > MAX_TTL_SECONDS:
            return {"ok": False, "error": f"ttl_seconds 必须在 1..{MAX_TTL_SECONDS} 之间"}

        session_id = args.get("session_id")
        parent_task_id = args.get("parent_task_id")
        # 域派生继承（第九轮）：parent_jti 指向 vault 里的一枚域 grant——派生 token
        # 必须继承 acps_domain/revision 且 exp ≤ 父 grant（防"洗白"绕过域冻结，见
        # domain_manifest 模块说明）；skill 必须在父资产集内（收窄规则）。
        parent_jti = str(args.get("parent_jti") or "").strip()
        inherit: dict[str, Any] = {}
        not_after: int | None = None
        if parent_jti:
            from .grant import derive_delegation_claims, verify_grant  # noqa: PLC0415

            parent_jws = store.peek(parent_jti)
            if parent_jws is None:
                return {"ok": False, "error": f"parent_jti {parent_jti} 不在保管库（未签发/已过期/已使用）"}
            if trust_bundle is None:
                return {"ok": False, "error": "派生需要 trust_bundle（构造工具时未提供）"}
            try:
                parent_claims = verify_grant(parent_jws.token, trust_bundle=trust_bundle)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"父 grant 验证失败: {exc}"}
            try:
                inherit = derive_delegation_claims(
                    parent_claims, skill_id=skill_id, parent_token=parent_jws.token,
                )
            except TokenError as exc:
                return {"ok": False, "error": f"派生超出父授权范围: {exc.reason}"}
            not_after = int(parent_claims["exp"])

        handles: list[dict[str, Any]] = []
        for target_aic in targets:
            issued = issue_token(
                signing,
                delegate_aic=delegate_aic,
                target_aic=target_aic,
                skill_id=skill_id,
                session_id=str(session_id) if session_id else None,
                parent_task_id=str(parent_task_id) if parent_task_id else None,
                ttl_seconds=ttl_seconds,
                not_after=not_after,
                extra_claims=inherit or None,
                # ★ 必须带整链（2026-10-11 实测修复）
                # token.py 的默认（只带 leaf）**硬依赖"验证方 trust-bundle 含签发中间 CA"**。
                # 实测本集群各工作区的 trust-bundle **只有 2 个根证书、没有中间 CA**
                # （设计注释写的是"Root + 两个 Intermediate"，与实物不符），
                # 于是 `_verify_chain` 的"链尾为锚"分支永远匹配不上，任何委托令牌都会
                # `TokenError: chain-untrusted: chain tail … is not anchored`。
                # 带整链后链尾 = 自签根，凡 bundle 含该根即可锚定——对
                # "Root only" 与 "Root + Intermediate" 两种 bundle 形态都成立。
                include_intermediates=True,
            )
            handles.append(store.hold(issued).handle())
        # 句柄：绝不含 token 原文
        return {"ok": True, "count": len(handles), "handles": handles,
                **({"domain": inherit.get("acps_domain"), "domainRevision": inherit.get("acps_domain_revision")} if inherit else {})}

    return Tool(
        name=tool_name,
        description=(
            "为等待中的伙伴签发委托令牌（delegation token），让它持令牌直接向目标伙伴发起任务，"
            "不必回到 leader 中转。参数：delegate_aic（受托伙伴的 AIC，通常就是你 continue 的那个等待伙伴）、"
            "target_aic（单个目标伙伴 AIC）或 targets（多个目标 AIC 的数组，一次签发多枚，用于平行扇出）、"
            "skill_id（授权的 skill id）、ttl_seconds（可选，默认 300，上限 600）。"
            "**域管辖的技能必须同时给 parent_jti**（从域 grant 派生）：子令牌会继承域名、"
            "父到期上界与父配额；不带 parent_jti 的令牌会被对端 PEP 以 domain-required 拒绝。"
            "**只返回 jti 句柄数组（不含令牌原文，这是有意的）**。签发后立即用 delegate_continue 把这些 jti "
            "连同给伙伴的说明发出去；**不要把 jti 之外的东西当成令牌，也不要试图构造令牌文本**。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "delegate_aic": {"type": "string", "description": "受托伙伴 AIC"},
                "target_aic": {"type": "string", "description": "目标伙伴 AIC（单个）"},
                "targets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "目标伙伴 AIC 数组（与 target_aic 二选一；一次签发多枚）",
                },
                "skill_id": {"type": "string", "description": "授权的 skill id"},
                "ttl_seconds": {"type": "integer", "description": "有效期秒数，默认 300"},
                "session_id": {"type": "string", "description": "可选：绑定会话（须等于使用时对端的 sessionId）"},
                "parent_task_id": {"type": "string", "description": "可选：任务血缘，仅审计"},
                "parent_jti": {"type": "string", "description": "域技能的父 grant 句柄（必填）：继承域标识、父到期上界与父使用配额；对端 PEP 会复核"},
            },
            "required": ["delegate_aic", "skill_id"],
        },
        handler=issue_delegation_token,
    )


def build_issue_token_tool_from_workspace(
    workspace: Path | str,
    *,
    trust_bundle: Path | str | None = None,
    **kwargs: Any,
) -> Tool:
    """从 `<workspace>/acps/client/` 加载私钥并构建签发工具（进程内一次性）。"""
    root = Path(workspace)
    signing = load_signing_context(
        root / "acps" / "client",
        trust_bundle=trust_bundle or (root / "acps" / "trust-bundle.pem"),
    )
    return build_issue_token_tool(signing, **kwargs)
