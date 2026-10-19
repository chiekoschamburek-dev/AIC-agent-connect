"""AIP 委托授权层（PEP）：对 AIP 的二次开发，落地 AAC 第 10 册定义但 SDK 未实现的委托语义。

模块划分（对齐 PEP_design.md §4.1）：
- token.py        签发 issue_token() / 验证 verify_token()（纯函数，无状态）
- vault.py        leader 侧进程内 jti→JWS 保管库（§11.9 的 jti 句柄方案）
- pep.py          AipPartnerAdmissionHooks 子类 + 三级策略（L0/L1/L2）+ jti 防重放
- audit.py        审计日志（JSONL 追加写，遵循 AAC §13 不记完整 token）
- client.py       受托方出站调用（delegation dataItem 构造、token 剥离、direct_fetch）
- issuer_tool.py  leader 侧本地签发工具（只回 jti 句柄）
- leader_tools.py leader 侧 run-scoped 挂载（签发 + delegate_continue 代码侧组装）

不改 wit / acps_sdk 源码：全部通过 wit 官方扩展点 `WitAgent.aip_hooks` / `WitAgent.hooks` 交付。
"""

from pathlib import Path

from .token import (
    DEFAULT_TTL_SECONDS,
    MAX_TTL_SECONDS,
    ROLE_DELEGATION_TOKEN,
    TOKEN_DATA_KEY,
    SigningContext,
    TokenError,
    canonical_audience,
    certificate_sha256_fingerprint,
    extract_token_from_data_items,
    is_delegation_token_data_item,
    issue_token,
    load_signing_context,
    normalize_audience,
    peek_claims,
    token_data_item,
    verify_token,
)
from .vault import DEFAULT_TOKEN_VAULT, HeldToken, TokenVault
from .audit import audit_log
from .pep import (
    PEP_VERSION,
    JtiRegistry,
    PepConfig,
    PepOutcome,
    build_pep_hook,
    capability_visibility,
    evaluate,
    load_pep_config,
    public_skill_ids,
    requested_skill,
)
from .client import (
    LOCAL_SKILL_KEY,
    LOCAL_TARGET_KEY,
    LOCAL_TOKEN_KEY,
    MARKER_SENTINEL,
    DelegationTokenScrubHooks,
    authorization_marker,
    delegate_marker,
    delegation_data_item,
    direct_fetch,
    local_context_token,
)
from .issuer_tool import build_issue_token_tool, build_issue_token_tool_from_workspace
from .bridge import (
    BridgeHooks,
    bridge_ready,
    current_delegation,
    current_runtime,
    current_state,
    delegated_fetch,
)
from .domain_manifest import (
    DomainPolicy,
    build_manifest,
    export_policy_projection,
    load_domain_policies,
    sign_manifest,
    to_policy,
    verify_manifest,
    write_manifest,
)
from .grant import (
    IssuedGrant,
    derive_delegation_claims,
    issue_grant,
    verify_grant,
)
from .quota import QuotaStore, consume as quota_consume
from .reconciliation import audit_window, build_report, sign_report, verify_report
from .leader_tools import DelegationLeaderHooks, build_delegate_continue_tool


def build_pep_hooks(**kwargs) -> list:
    """§4.3 的一句话注册：准入钩子 + token 替换钩子 + **能力收窄钩子**。

    替换钩子拿到同一个 PepConfig，才能在校验通过后把「已授权」这一事实以脱敏标记告知模型
    （只删除会让模型以"未授权"为由拒绝执行被授权的技能，见 client.py 模块说明）。

    第三个钩子（P0-3 缓解）按本任务被准入的 skill 收窄运行时工具集：
    `public=False` 的能力若不在准入集合里，就从 ToolManager 移除，
    使"自报公开 skillId 入场 → 自然语言驱动非公开能力"这条路走不通。
    可选参数 `capabilities`（本机能力声明）用于推导哪些 id 是非公开的；
    不传则**不装配**该钩子（保持既有行为，向后兼容）。
    """
    capabilities = kwargs.pop("capabilities", None)
    admission = build_pep_hook(**kwargs)
    hooks: list = [admission, DelegationTokenScrubHooks(getattr(admission, "config", None))]
    if capabilities is not None:
        from . import skill_scope  # noqa: PLC0415

        cap_list = list(capabilities or ())
        ws = kwargs.get("workspace")
        hooks.append(
            skill_scope.SkillScopedToolsHooks(
                non_public=skill_scope.non_public_ids(cap_list),
                public_skills=capability_visibility(cap_list)[0],
                # 令牌 peek 目录与写入侧(_token_dir_for(cfg))及业务读取侧同源（2026-10-15 审查 #3）
                token_dir=(Path(ws) / "tokens") if ws else None,
                audit_path=getattr(getattr(admission, "config", None), "audit_path", None),
            )
        )
    return hooks


def split_pep_hooks(hooks) -> tuple[list, list]:
    """把 build_pep_hooks 的返回值拆成 (run 生命周期 hooks, AIP 协议层 hooks)。

    SkillScopedToolsHooks 靠 `on_run_start` 收窄工具集，只有注册到
    `WitAgent.hooks` 才会被 wit 调度；放进 `aip_hooks` 会成为死代码（P0-2）。
    """
    from .skill_scope import SkillScopedToolsHooks  # noqa: PLC0415

    run_hooks = [hook for hook in hooks if isinstance(hook, SkillScopedToolsHooks)]
    aip_hooks = [hook for hook in hooks if not isinstance(hook, SkillScopedToolsHooks)]
    return run_hooks, aip_hooks


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "DEFAULT_TOKEN_VAULT",
    "LOCAL_SKILL_KEY",
    "LOCAL_TARGET_KEY",
    "LOCAL_TOKEN_KEY",
    "MARKER_SENTINEL",
    "MAX_TTL_SECONDS",
    "PEP_VERSION",
    "ROLE_DELEGATION_TOKEN",
    "TOKEN_DATA_KEY",
    "DelegationLeaderHooks",
    "DelegationTokenScrubHooks",
    "HeldToken",
    "JtiRegistry",
    "PepConfig",
    "PepOutcome",
    "SigningContext",
    "TokenError",
    "TokenVault",
    "audit_log",
    "authorization_marker",
    "build_delegate_continue_tool",
    "BridgeHooks",
    "bridge_ready",
    "current_delegation",
    "current_runtime",
    "current_state",
    "delegated_fetch",
    "build_issue_token_tool",
    "build_issue_token_tool_from_workspace",
    "DomainPolicy",
    "build_manifest",
    "audit_window",
    "build_report",
    "derive_delegation_claims",
    "issue_grant",
    "export_policy_projection",
    "load_domain_policies",
    "sign_manifest",
    "sign_report",
    "to_policy",
    "verify_grant",
    "verify_manifest",
    "verify_report",
    "write_manifest",
    "build_pep_hook",
    "build_pep_hooks",
    "split_pep_hooks",
    "canonical_audience",
    "capability_visibility",
    "certificate_sha256_fingerprint",
    "delegate_marker",
    "delegation_data_item",
    "direct_fetch",
    "evaluate",
    "extract_token_from_data_items",
    "is_delegation_token_data_item",
    "issue_token",
    "load_pep_config",
    "load_signing_context",
    "local_context_token",
    "normalize_audience",
    "peek_claims",
    "public_skill_ids",
    "requested_skill",
    "token_data_item",
    "verify_token",
    "IssuedGrant",
    "QuotaStore",
    "quota_consume",
]
