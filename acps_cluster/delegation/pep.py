"""PEP：AIP Partner 侧准入策略（AipPartnerAdmissionHooks 子类 + 三级策略表）。

校验顺序逐条对齐 AAC §11.3（详见 PEP_design.md §4.2）：
  ①传输层身份（框架）→ ②协议身份绑定（框架）→ ③token（issuer/签名 → audience/expiry/scope
  → revocation【显式偏差】→ jti/replay → act 绑定 → cnf【本实现无通道】→ allowed_route 对齐
  → 会话绑定【扩展】）→ ④资源动作（skillId → 链深）→ ⑤⑥内嵌 PDP → ⑦allow 进 handler → ⑧deny 记审计。
任一失败即 fail-closed（AAC §5）。

三级准入策略（PEP_design.md §4.2.1）：
  L0 公开能力   请求的 skill 在本机 capability 声明中 public=True（或未指名 skill 且本机存在公开能力）
  L1 白名单     调用方 AIC 在 ACPS_PEP_LEADER_AICS
  L2 委托 token 有效 delegation token，且 skill == acps_skill_id

实现期发现（详见 PEP-result.md）：
1. **ACS 不带 `public` 字段**——`@capability(public=True)` 不会进入 ACS skills（实测 ws-scout
   acs.final.json 无任何 `public` 字样）。因此 L0 依据只能是**本机 capability 声明**
   （`public_skill_ids()`），不能像文档 §4.2.1 写的那样"查本机 ACS"。
2. **admission 请求不暴露对端证书**——`AipPartnerAdmissionRequest` 只有 leader_aic/partner_aic/
   delivery/peer_verified，拿不到 mTLS client certificate 指纹，故文档 §4.2.2 建议的
   `cnf.x5t#S256` 证书级绑定在 admission 点**无法校验**；本实现以 `act.aic == peer AIC`
   做主体级绑定，`cnf` 若存在只记审计标记。
3. **AIP v2 的 TaskCommand 没有 skillId 字段**——skill 只能靠调用方自报（dataItem/commandParams
   里的 skillId 约定）。因此 skill 维度的授权只约束"诚实调用方"，真正的边界仍是白名单与 token。
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from wit.hooks.acps.admission import (
    AipPartnerAdmissionDecision,
    AipPartnerAdmissionHooks,
    AipPartnerAdmissionRequest,
)

from .audit import (
    NO_DOMAIN,
    NO_SKILL,
    PEP_VERSION,
    SKILL_SOURCE_SELF_DECLARED,
    SKILL_SOURCE_TOKEN_CLAIM,
    SKILL_SOURCE_UNSPECIFIED,
    audit_log,
    default_audit_path,
)
from .token import (
    CLOCK_SKEW_SECONDS,
    MAX_TTL_SECONDS,
    TokenError,
    _parse_token,
    extract_token_from_data_items,
    normalize_audience,
    verify_token,
)

# AAC §12 错误码（本实现把「意图码」记进审计；对端拿到的码由框架决定，见 PEP-result.md）
CODE_AUTHENTICATION_REQUIRED = -32008
CODE_AUTHORIZATION_FAILED = -32009
CODE_ACCESS_TOKEN_INVALID = -32010

# AAC §12 对外通用文案（不泄露策略细节）
MSG_AUTHENTICATION_REQUIRED = "Authentication required"
MSG_AUTHORIZATION_FAILED = "Authorization failed"
MSG_INVALID_ACCESS_TOKEN = "Invalid access token"

_SKILL_HINT_KEYS = ("skillId", "skill_id", "skill", "targetSkillHint")


@dataclass(frozen=True)
class PepConfig:
    """准入策略配置。`my_aic` 缺失时 PEP 拒绝启动（aud 校验必须有本机身份）。

    域级扩展（第九轮）：`domains` 是本地持有的域章程策略视图（最高 revision），
    `quota` 是使用侧配额计数器（sqlite，重启不清零）。两者缺省为空 = 纯 task 级行为，
    完全向后兼容。
    """

    enabled: bool = False
    my_aic: str = ""
    leader_aics: frozenset[str] = frozenset()
    leader_direct_open: bool = True
    trust_bundle: Path | None = None
    audit_path: Path | None = None
    public_skills: frozenset[str] = frozenset()
    has_non_public_skills: bool = False
    # 本机代码声明的域可见技能：skill_id -> 允许的域名集合。
    # ① 带 acps_domain 的令牌必须落在声明集合内（代码没声明时退回章程集合）；
    # ② 普通令牌的 domain-required 需要**声明与章程资产同时命中**：代码表达了
    # "这是会员制"的意图，章程才是真正生效的域；二者缺一不硬拒，避免把尚未
    # 建章程的 scout/hpc 或仅作为额外通道的 gpu.run 误锁死。
    domain_skills: Mapping[str, frozenset[str]] = field(default_factory=dict)
    unscoped_public_open: bool = True
    require_session_binding: bool = False
    max_token_ttl_seconds: int = MAX_TTL_SECONDS
    clock_skew_seconds: int = CLOCK_SKEW_SECONDS
    domains: Mapping[str, Any] = field(default_factory=dict)  # domain_id -> DomainPolicy
    domains_dir: Path | None = None  # 章程目录：决策时按 TTL 重读，冻结/升版不必等重启
    quota_path: Path | None = None  # 使用侧配额 sqlite（grant 路径必需；按需开合，无长持句柄）
    workspace: Path | None = None  # 本 partner 工作区（令牌文件等 host 通道根目录）

    def unscoped_entry_open(self) -> bool:
        """未指名 skill 的请求是否按 L0 放行（**严格**口径）。

        只有当本机**全部** capability 都声明为公开（且确有公开能力）时才放开——否则本机同时
        提供非公开能力，未指名 skill 的请求无法证明只碰公开能力，必须走 L1/L2（fail-closed）。
        """
        return bool(self.unscoped_public_open and self.public_skills and not self.has_non_public_skills)

    def unscoped_public_entry_open(self) -> bool:
        """未指名 skill 的请求是否放行到「**仅公开能力**」的收窄口径（宽松口径）。

        为什么需要它（2026-10-11 实测）：平台侧调用**不会**稳定携带 skillId——
        AIP v2 的 `TaskCommand` 里根本没有这个字段，我们只能从 `command_params`/
        `data_items[].metadata` 这类"约定字段"里猜。于是"未指名 skill"是**常态**。
        而只要本机有一个非公开能力，`unscoped_entry_open()` 就为假 → 常态调用被
        `no-credential` 拒绝。实测：叮当调 scout 得到
        `decision=deny rule=no-credential code=-32008`，而调 supervisor（唯一能力即公开）
        却成功——差别只在"本机有没有非公开能力"，这与调用方是否滥用完全无关。

        放宽的边界：**入场放宽、能力面照旧收紧**。
        运行时由 `skill_scope.SkillScopedToolsHooks` 兜底——没有准入 skill 时，
        所有 `public=False` 的工具会被**移出 ToolManager**，模型根本看不见，
        所以"未指名 skill 入场"并不能换来任何非公开能力。

        仍要求"确有公开能力"（`public_skills` 非空）：完全没有公开入口的 agent
        不该被无凭据调用（保持 fail-closed）。
        """
        return bool(self.unscoped_public_open and self.public_skills)

    def describe(self) -> dict[str, Any]:
        """给启动日志用（不含任何密钥）。"""
        return {
            "enabled": self.enabled,
            "myAic": self.my_aic,
            "leaderAics": sorted(self.leader_aics),
            "leaderDirectOpen": self.leader_direct_open,
            "publicSkills": sorted(self.public_skills),
            "hasNonPublicSkills": self.has_non_public_skills,
            "domainSkills": {k: sorted(v) for k, v in sorted(self.domain_skills.items())},
            "unscopedEntryOpen": self.unscoped_entry_open(),
            "unscopedPublicEntryOpen": self.unscoped_public_entry_open(),
            "requireSessionBinding": self.require_session_binding,
            "jtiMaxUses": JtiRegistry.max_uses(),
            "trustBundle": str(self.trust_bundle) if self.trust_bundle else None,
            "auditPath": str(self.audit_path) if self.audit_path else None,
            "domains": {d: p.describes() for d, p in self.domains.items()} if self.domains else {},
            "pepVersion": PEP_VERSION,
        }


@dataclass(frozen=True)
class PepOutcome:
    """一次准入裁决的完整结果：`decision` 给框架，其余进审计/单测断言。"""

    decision: str
    rule: str
    code: int | None = None
    message: str | None = None
    reason: str | None = None
    jti: str | None = None
    delegation_id: str | None = None
    cnf_unverifiable: bool = False
    domain: str | None = None
    domain_revision: int | None = None
    skill_hint: str | None = None
    parent_jti: str | None = None
    parent_quota_remaining: int | None = None
    # skill_hint 的证据等级：token-claim（签名凭据）/ self-declared（L1 请求自报）
    skill_source: str | None = None

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"

    def as_decision(self) -> AipPartnerAdmissionDecision:
        if self.allowed:
            return AipPartnerAdmissionDecision.accept()
        return AipPartnerAdmissionDecision.reject(self.message or MSG_AUTHORIZATION_FAILED)


class JtiRegistry:
    """进程内 jti 使用登记（防重放）。演示规模足够；生产可换 sqlite。

    默认严格一次性（`ACPS_PEP_JTI_MAX_USES` 未设即 1）。调大只放宽**同一作用域**
    （同一已验证调用方 + 同一能力）的有界复用：伙伴流水线内部的重试会重放同一枚
    令牌，严格一次性把合法重试判成 replay（2026-10-17 真链路实测：1 allow 之后
    `jti already used`，整轮自研环再无凭据可用）。
    跨作用域一律拒：一枚 jti 只要在别的调用方/别的能力下出现过就直接拒——窃取者
    换上下文重放的语义与默认一致，不受该开关影响。
    """

    def __init__(self) -> None:
        self._scopes: dict[str, str] = {}              # jti -> 首次登记的作用域
        self._uses: dict[tuple[str, str], int] = {}    # (jti, scope) -> 已用次数
        self._expiries: dict[str, int] = {}

    @staticmethod
    def max_uses() -> int:
        try:
            return max(1, int(os.getenv("ACPS_PEP_JTI_MAX_USES", "1")))
        except ValueError:
            return 1

    def claim(self, jti: str, *, expires_at: int, now: float, scope: str = "") -> bool:
        self._prune(now)
        first = self._scopes.get(jti)
        if first is not None and first != scope:
            return False
        used = self._uses.get((jti, scope), 0)
        if used >= self.max_uses():
            return False
        self._scopes[jti] = scope
        self._uses[(jti, scope)] = used + 1
        self._expiries[jti] = int(expires_at)
        return True

    def __len__(self) -> int:
        return len(self._expiries)

    def _prune(self, now: float) -> None:
        for jti in [k for k, exp in self._expiries.items() if exp + 60 < now]:
            self._expiries.pop(jti, None)
            self._scopes.pop(jti, None)
            for key in [s for s in self._uses if s[0] == jti]:
                self._uses.pop(key, None)


DEFAULT_JTI_REGISTRY = JtiRegistry()


# ─────────────────────────── 配置解析 ───────────────────────────


def _env_flag(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _my_aic_from_workspace(workspace: Path) -> str:
    enrollment = workspace / "acps" / "enrollment.json"
    if enrollment.exists():
        import json

        try:
            data = json.loads(enrollment.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        aic = ((data.get("identity") or {}).get("ref") or {}).get("aic")
        if aic:
            return str(aic)
    certificate = workspace / "acps" / "client" / "certificate.pem"
    if certificate.exists():
        from .token import aic_from_certificate, load_certificates

        return aic_from_certificate(load_certificates(certificate)[0])
    return ""


def load_pep_config(
    workspace: Path | str,
    *,
    public_skills: Iterable[str] = (),
    has_non_public_skills: bool = False,
    domain_skills: Mapping[str, Iterable[str]] | None = None,
    my_aic: str | None = None,
    env: Mapping[str, str] | None = None,
) -> PepConfig:
    """环境变量注入配置（沿用 cluster_common 惯例），见 PEP_design.md §4.3。

    启用时做两项 fail-fast 检查（PepConfig docstring 承诺的「my_aic 缺失拒绝启动」在此落实）：
    - `my_aic` 解析不出来 → raise（否则所有 L2 静默拒绝、审计全是误导性的 audience-mismatch）；
    - 白名单为空 → 告警不阻断（纯公开能力的 partner 合法地不需要 leader）。
    """
    env = env if env is not None else os.environ
    workspace_path = Path(workspace)
    leaders = frozenset(
        part.strip() for part in (env.get("ACPS_PEP_LEADER_AICS") or "").split(",") if part.strip()
    )
    trust_bundle_env = env.get("ACPS_PEP_TRUST_BUNDLE")
    trust_bundle = Path(trust_bundle_env) if trust_bundle_env else workspace_path / "acps" / "trust-bundle.pem"
    audit_env = env.get("ACPS_PEP_AUDIT_PATH")
    max_ttl_env = env.get("ACPS_PEP_MAX_TOKEN_TTL")
    from .domain_manifest import load_domain_policies
    from .quota import default_quota_path

    domains_dir = Path(env.get("ACPS_PEP_DOMAINS_DIR") or (workspace_path / "acps" / "domains"))
    domains = load_domain_policies(domains_dir, trust_bundle=trust_bundle) if trust_bundle else {}
    quota_env = env.get("ACPS_PEP_QUOTA_PATH")
    cfg = PepConfig(
        enabled=_env_flag(env, "ACPS_PEP_ENABLED", False),
        my_aic=(my_aic or env.get("ACPS_PEP_MY_AIC") or _my_aic_from_workspace(workspace_path)),
        leader_aics=leaders,
        trust_bundle=trust_bundle,
        audit_path=Path(audit_env) if audit_env else default_audit_path(workspace_path),
        public_skills=frozenset(str(s) for s in public_skills),
        has_non_public_skills=has_non_public_skills,
        domain_skills={
            str(skill): frozenset(str(d) for d in domains if str(d).strip())
            for skill, domains in (domain_skills or {}).items()
            if str(skill).strip()
        },
        unscoped_public_open=_env_flag(env, "ACPS_PEP_UNSCOPED_PUBLIC", True),
        # 资源类 agent 用 `ACPS_PEP_LEADER_DIRECT=0` 关掉"直调免令牌"通道，
        # 同时保留 `ACPS_PEP_LEADER_AICS`（承认 leader 的**签发资格**）——
        # 这样 leader 也必须持令牌，但它签的令牌仍然有效。默认 True（向后兼容）。
        leader_direct_open=_env_flag(env, "ACPS_PEP_LEADER_DIRECT", True),
        require_session_binding=_env_flag(env, "ACPS_PEP_REQUIRE_SESSION_BINDING", False),
        max_token_ttl_seconds=int(max_ttl_env) if max_ttl_env else MAX_TTL_SECONDS,
        domains=domains,
        domains_dir=domains_dir,
        quota_path=Path(quota_env) if quota_env else default_quota_path(workspace_path),
        workspace=workspace_path,
    )
    _validate_config(cfg)
    if cfg.enabled and domains:
        print(f"[pep] 已加载 {len(domains)} 个域章程: "
              + "; ".join(f"{d}(r{p.revision},{p.status})" for d, p in sorted(domains.items())), flush=True)
    return cfg


_DOMAINS_TTL_SECONDS = 5.0  # 章程重读节流（每次决策都全量验签太贵，冻结传播延迟压到秒级）
_domains_cache: dict[str, tuple[float, Mapping[str, Any]]] = {}


def current_domains(cfg: PepConfig) -> Mapping[str, Any]:
    """决策时点的域策略：按目录短 TTL 重读盘。

    此前只在启动时快照一次（`config_from_env` 里 load 完就冻在 cfg.domains），于是"冻结即急停"
    实际要等进程重启才生效，与文档承诺不符（2026-10-07 审查修复）。没有 domains_dir 的调用方
    （单测直接构造 PepConfig）沿用其内存快照，行为不变。
    """
    if cfg.domains_dir is None or cfg.trust_bundle is None:
        return cfg.domains
    key = str(cfg.domains_dir)
    mono = time.monotonic()
    hit = _domains_cache.get(key)
    if hit is not None and hit[0] > mono:
        return hit[1]
    from .domain_manifest import load_domain_policies

    policies = load_domain_policies(cfg.domains_dir, trust_bundle=cfg.trust_bundle)
    _domains_cache[key] = (mono + _DOMAINS_TTL_SECONDS, policies)
    return policies


def domains_governing_skill(
    cfg: PepConfig,
    policies: Mapping[str, Any],
    skill_id: str | None,
) -> frozenset[str]:
    """一个 skill 在本机被**章程资产表**划入哪些域（访问控制的权威来源）。

    代码里的 `visibility=domain` 主要是发现层元数据；在没有章程时若也据此硬拒，
    会把尚未建域的 scout/hpc 能力直接锁死（属于未部署状态，不是安全边界）。
    """
    if not skill_id:
        return frozenset()
    governing: set[str] = set()
    for policy in policies.values():
        if (cfg.my_aic, str(skill_id)) in getattr(policy, "assets", {}):
            governing.add(str(policy.domain_id))
    return frozenset(governing)


def _validate_config(cfg: PepConfig) -> None:
    """启用态的启动期检查：aud 校验必须有本机身份；白名单为空只告警。"""
    if not cfg.enabled:
        return
    if not cfg.my_aic:
        raise ValueError(
            "ACPS_PEP_ENABLED=1 但无法解析本机 AIC（enrollment.json 与 client/certificate.pem 均缺失），"
            "aud 校验无法进行——PEP 拒绝启动（fail-fast，见 PepConfig docstring）"
        )
    if not cfg.leader_aics:
        print(
            "[pep] 警告: ACPS_PEP_LEADER_AICS 为空——L1 白名单与 L2 委托 token 均不可用，"
            "仅 L0 公开能力可放行；若本机存在非公开能力，它们将被全部锁死",
            file=sys.stderr,
        )
    declared_domains = {d for domains in cfg.domain_skills.values() for d in domains}
    missing = sorted(declared_domains - set(cfg.domains))
    if missing:
        print(
            f"[pep] 警告: 代码声明了域可见技能，但本地没有章程: {missing}——"
            "带 acps_domain 的令牌会被拒(domain-unknown)；普通令牌仍走旧路径。"
            "要启用域访问控制，请发布章程（代码声明本身不凭空造域）",
            file=sys.stderr,
        )
    uncovered: list[str] = []
    for skill, domains in cfg.domain_skills.items():
        present = [d for d in domains if d in cfg.domains]
        if not present:
            continue  # 缺章程已由上面的警告覆盖
        if not any(
            (cfg.my_aic, skill) in getattr(cfg.domains.get(domain), "assets", {})
            for domain in present
        ):
            uncovered.append(skill)
    uncovered.sort()
    if uncovered:
        print(
            f"[pep] 警告: 代码声明为域可见、但章程资产表未收录: {uncovered}——"
            "普通令牌仍可调用；补齐章程资产后 domain-required 才会生效",
            file=sys.stderr,
        )


def _capability_of(item: Any) -> Any:
    """`@capability(...)` 装饰后返回的是函数，真正的 `Capability` 挂在 `.capability` 上。"""
    inner = getattr(item, "capability", None)
    return inner if inner is not None else item


def capability_visibility(capabilities: Iterable[Any] | None) -> tuple[frozenset[str], bool]:
    """从本机 capability 声明取 (public=True 的 skill id, 是否存在非公开 capability)。

    ACS 里没有 `public` 字段（实测），所以 L0 只能依据本机声明。
    """
    public_ids: set[str] = set()
    has_non_public = False
    for raw in capabilities or ():
        capability = _capability_of(raw)
        public = getattr(capability, "public", None)
        identifier = getattr(capability, "id", None)
        if isinstance(capability, Mapping):
            public = capability.get("public")
            identifier = capability.get("id")
        if not identifier:
            continue
        if public:
            public_ids.add(str(identifier))
        else:
            has_non_public = True
    return frozenset(public_ids), has_non_public


def capability_domain_scopes(
    capabilities: Iterable[Any] | None,
) -> dict[str, frozenset[str]]:
    """从本机 capability 声明取 `acps_skill.visibility == "domain"` 的技能及其域。

    用途是发现层元数据 + **域令牌的目标域约束**。注意：代码声明本身不构成访问控制；
    普通令牌是否必须带域，以**本地已发布章程**是否把该资产纳入管辖为准。
    """
    scopes: dict[str, frozenset[str]] = {}
    for raw in capabilities or ():
        capability = _capability_of(raw)
        identifier = getattr(capability, "id", None)
        skill = getattr(capability, "acps_skill", None)
        if isinstance(capability, Mapping):
            identifier = capability.get("id")
            skill = capability.get("acps_skill")
        if not identifier or not isinstance(skill, Mapping):
            continue
        if str(skill.get("visibility") or "").strip() != "domain":
            continue
        raw_domains = skill.get("domains") or ()
        if not isinstance(raw_domains, (list, tuple, set, frozenset)):
            raise TypeError(f"capability {identifier!r} 的 domains 必须是数组")
        domains = frozenset(str(d).strip() for d in raw_domains if str(d).strip())
        if not domains:
            # 声明了会员制却没说哪个域：fail-fast，不能静默退回 public/白名单路径
            raise ValueError(f"capability {identifier!r} 声明 visibility=domain 但没有 domains")
        scopes[str(identifier)] = domains
    return scopes


# 预留公共 API（delegation 包 __all__ 导出；文档 §4.2.1 的"代码侧收集公开技能"语义）：
# 当前运行路径不调用（PEP 走配置里的 public_skills），保留供外部消费者/未来变体使用。
def public_skill_ids(capabilities: Iterable[Any] | None) -> frozenset[str]:
    """从本机 capability 声明取 public=True 的 skill id（ACS 里没有这个字段）。"""
    return capability_visibility(capabilities)[0]


# ─────────────────────────── 请求解析 ───────────────────────────


def _hint_from_mapping(source: Any) -> str | None:
    if not isinstance(source, Mapping):
        return None
    for key in _SKILL_HINT_KEYS:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def requested_skill(request: AipPartnerAdmissionRequest) -> str | None:
    """调用方自报的 skill（AIP v2 命令里没有 skillId，只能靠约定字段）。"""
    hint = _hint_from_mapping(request.command_params)
    if hint:
        return hint
    for item in request.data_items or ():
        if not isinstance(item, Mapping):
            continue
        hint = _hint_from_mapping(item.get("metadata")) or _hint_from_mapping(item.get("data"))
        if hint:
            return hint
    return None


def _is_adjacent_in_route(route: Iterable[Any], hop: tuple[str, str]) -> bool:
    aics = [normalize_audience(str(entry)) for entry in route]
    return any((aics[i], aics[i + 1]) == hop for i in range(len(aics) - 1))


def _trace(cfg: PepConfig, request: AipPartnerAdmissionRequest, outcome: PepOutcome, *, extra=None) -> None:
    if cfg.audit_path is None:
        return
    extra = dict(extra or {})
    # 归属维度恒落齐：allow 若不走域（public-skill-unscoped / leader-allowlist 等），
    # 也要留下可统计的哨兵值，否则按域聚合的用量报表会把真实访问静默漏掉
    extra.setdefault("domain", outcome.domain or NO_DOMAIN)
    if outcome.domain_revision is not None:
        extra.setdefault("domainRevision", outcome.domain_revision)
    extra.setdefault("skillHint", outcome.skill_hint or NO_SKILL)
    if outcome.parent_jti:
        extra.setdefault("parentJti", outcome.parent_jti)
    if outcome.parent_quota_remaining is not None:
        extra.setdefault("parentQuotaRemaining", outcome.parent_quota_remaining)
    # 技能归属的**证据等级**（2026-10-16）：`skillHint` 只在校验过的凭据（域 grant / 委托
    # 令牌）路径上有值，且它同时被 skill_scope 用来做能力收窄——所以这里只加审计字段、
    # 不改它的语义。`declaredSkill` 是调用方自报的能力，配上 `skillSource` 才能把
    # "按技能核算"分成三档：token-claim（可当凭据）/ self-declared（仅供参考）/
    # unspecified（没指名——现网 88 条 allow 里约 72% 是这一档）。
    declared = requested_skill(request)
    extra.setdefault("declaredSkill", declared or NO_SKILL)
    extra.setdefault(
        "skillSource",
        outcome.skill_source
        or (
            SKILL_SOURCE_TOKEN_CLAIM
            if outcome.skill_hint
            else (SKILL_SOURCE_SELF_DECLARED if declared else SKILL_SOURCE_UNSPECIFIED)
        ),
    )
    audit_log(
        cfg.audit_path,
        decision=outcome.decision,
        rule=outcome.rule,
        caller_aic=request.leader_aic,
        my_aic=cfg.my_aic,
        task_id=request.task_id,
        session_id=request.session_id,
        delivery=request.delivery,
        jti=outcome.jti,
        delegation_id=outcome.delegation_id,
        code=outcome.code,
        reason=outcome.reason,
        extra=extra,
    )


# ─────────────────────────── 裁决 ───────────────────────────


def evaluate(
    request: AipPartnerAdmissionRequest,
    cfg: PepConfig,
    *,
    jti_registry: JtiRegistry | None = None,
    now: float | None = None,
) -> PepOutcome:
    """纯裁决函数（不依赖 wit 运行期），供单测与 hook 共用。"""
    import time as _time

    checked_at = float(now if now is not None else _time.time())
    registry = jti_registry if jti_registry is not None else DEFAULT_JTI_REGISTRY

    # 前置：传输层身份必须已验证（fail-closed）。直连严格模式下框架恒传 True；防的是
    # 未来其他 delivery（notification/group 等）允许 senderId 回退时，L1 白名单与
    # act 绑定建立在未验证身份上（admission 契约有 peer_verified 字段，不能默认它成立）。
    if not getattr(request, "peer_verified", False):
        return PepOutcome(
            "deny",
            "peer-not-verified",
            code=CODE_AUTHENTICATION_REQUIRED,
            message=MSG_AUTHENTICATION_REQUIRED,
        )

    # 策略 0（L0）：公开能力免 token——保住社区互联线
    skill = requested_skill(request)
    if skill is None:
        if cfg.unscoped_entry_open():
            return PepOutcome("allow", "public-skill-unscoped")
    elif skill in cfg.public_skills:
        return PepOutcome("allow", "public-skill")

    # 策略 1（L1）：白名单 leader **直调**放行
    #
    # ⚠️ `leader_direct_open` 存在的理由（2026-10-11 实测发现的耦合）：
    # `leader_aics` 原本同时承担两个职责——① L1 直调白名单；② 令牌**签发者**白名单
    # （见下方 `allowed_issuers = cfg.leader_aics`）。
    # 于是"关掉白名单让 leader 也必须带令牌"会**连带剥夺 leader 的签发资格**，
    # 它签的令牌会被判 `deny / issuer-not-authorized`（实测：
    # `reason="token issuer is not an authorized leader"`）。
    # 拆开之后：资源类 agent 可以保留 `leader_aics={supervisor}`（承认其签发资格）
    # 同时设 `leader_direct_open=False`（不给直调免令牌的通道），
    # 于是"leader 也必须持令牌"与"leader 签的令牌有效"得以同时成立。
    if cfg.leader_direct_open and request.leader_aic and request.leader_aic in cfg.leader_aics:
        # 带上调用方指明的 skill：技能作用域（P0-3）按 `outcome.skill_hint` 绑定工具面，
        # 不带的化 L1 直调的伙伴会只剩公开能力——白名单 leader 委派 `eval.submit` 时
        # 对端 agent 因此"看不见"该能力，只好多交一句"无法提交"（2026-10-18 第 17 轮实测）。
        # 这不放宽 L1 语义：该 leader 本就允许直调本机，作用域仍限制在这一个被指明的能力上。
        return PepOutcome(
            "allow",
            "leader-allowlist",
            skill_hint=skill or None,
            skill_source=SKILL_SOURCE_SELF_DECLARED if skill else SKILL_SOURCE_UNSPECIFIED,
        )

    # ── §11.3 步骤 3：token 校验 ──
    token = extract_token_from_data_items(request.data_items)
    if token is None:
        # 策略 0'（L0'·兜底）：无任何凭据、且未指名 skill 时，放宽到
        # 「仅公开能力」的收窄口径——**入场放宽、能力面照旧收紧**。
        #
        # 为什么放在这里而不是并进 L0：L0 的严格口径要求"本机全部能力都公开"，
        # 只要有一个非公开能力（scout 有 3 个）就为假，而"未指名 skill"又是
        # **平台侧常态**（AIP v2 的 TaskCommand 里没有 skillId 字段，只能从
        # command_params/data_items[].metadata 这些约定字段猜）。
        # 实测 2026-10-11：叮当调 scout → `deny rule=no-credential code=-32008`；
        # 调 supervisor（唯一能力即公开）→ `allow public-skill-unscoped`。
        # 差别只在"本机有没有非公开能力"，与调用方是否滥用无关。
        #
        # ⚠️ 必须排在 L1/L2 **之后**：否则白名单 leader 会被记成公开入场
        # （审计规则失真）、带凭据的调用会绕过凭据链（测试
        # `test_L1白名单leader直调` / `test_裸令牌无skillId_...` 抓到过这个回归）。
        # 语义上它是"没有凭据可用时的最后退路"，不是"优先于凭据的通道"。
        if skill is None and cfg.unscoped_public_entry_open():
            return PepOutcome("allow", "public-skill-unscoped-narrowed")
        return PepOutcome(
            "deny", "no-credential", code=CODE_AUTHENTICATION_REQUIRED, message=MSG_AUTHENTICATION_REQUIRED
        )

    if cfg.trust_bundle is None or not Path(cfg.trust_bundle).exists():
        return PepOutcome(
            "deny",
            "pep-misconfigured",
            code=CODE_AUTHORIZATION_FAILED,
            message=MSG_AUTHORIZATION_FAILED,
            reason="trust bundle missing",
        )

    # ── 域路径预检（第九轮；token 分支最外层，**验签之前**）──
    # 携带 acps_domain 的 token（grant 画像或从 grant 派生的 delegation），其所属域必须
    # 已知且未冻结。冻结是急停：不做密码学开销（DoS 友好）；基于未验签 claim 分支是安全的
    # ——伪造 claim 只会把方向推向"更拒"，两条后续路径（grant/delegation）都会完整验签，
    # 不会 fail-open。这是"域状态穿透"修复的执行侧一半（派生继承是签发侧另一半）。
    try:
        _, pre_claims, _, _ = _parse_token(token)
        pre_claims = pre_claims if isinstance(pre_claims, dict) else {}
    except TokenError:
        pre_claims = {}
    policies = current_domains(cfg)
    token_domain = str(pre_claims.get("acps_domain") or "")
    domain_policy = policies.get(token_domain) if token_domain else None
    if token_domain:
        if domain_policy is None:
            return PepOutcome(
                "deny",
                "domain-unknown",
                code=CODE_ACCESS_TOKEN_INVALID,
                message=MSG_INVALID_ACCESS_TOKEN,
                reason=f"unknown/unloadable domain {token_domain} (pre-signature)",
                domain=token_domain,
            )
        if domain_policy.status != "active":
            return PepOutcome(
                "deny",
                "domain-frozen",
                code=CODE_AUTHORIZATION_FAILED,
                message=MSG_AUTHORIZATION_FAILED,
                reason=f"domain {token_domain} frozen at revision {domain_policy.revision} (pre-signature)",
                domain=token_domain,
                domain_revision=domain_policy.revision,
            )

    # 域级 grant 画像（长期凭证、无 act 链、配额制）走独立裁决；delegation 原路径不变
    from .grant import is_grant_payload

    if is_grant_payload(pre_claims):
        return _evaluate_grant(request, cfg, token, domain_policy, skill, checked_at=checked_at)

    try:
        # 3a issuer/signature（x5c 链 + trust-bundle 验签）
        # 3b canonical audience / expiry / ttl
        # 3c revocation：显式偏差——无 CRL/introspection 通道，以自包含 token + ≤10min exp 替代
        claims = verify_token(
            token,
            trust_bundle=cfg.trust_bundle,
            expected_aud=cfg.my_aic,
            now=checked_at,
            clock_skew_seconds=cfg.clock_skew_seconds,
            max_ttl_seconds=cfg.max_token_ttl_seconds,
        )
    except TokenError as exc:
        parent_related = (
            exc.reason in {
                "parent-exp-exceeded", "parent-without-domain",
                "invalid-parent-quota", "parent-revision-mismatch",
                "parent-grant-invalid", "parent-grant-mismatch",
                "parent-subject-mismatch", "parent-audience-mismatch",
                "parent-asset-mismatch", "missing-parent-proof",
                "invalid-parent-proof",
            }
            or (exc.reason == "missing-claim" and "parent" in str(exc.detail).lower())
            or (exc.reason == "missing-claim" and "domain token" in str(exc.detail).lower())
        )
        return PepOutcome(
            "deny",
            "invalid-parent-binding" if parent_related else "invalid-token",
            code=CODE_ACCESS_TOKEN_INVALID,
            message=MSG_INVALID_ACCESS_TOKEN,
            reason=exc.reason,
        )

    # 验签后的域归属以签名 claims 为准（预检用的是同一串，但这里不依赖预检结论）。
    token_domain = str(claims.get("acps_domain") or "")
    domain_policy = policies.get(token_domain) if token_domain else None
    if token_domain and domain_policy is None:
        return PepOutcome(
            "deny", "domain-unknown", code=CODE_ACCESS_TOKEN_INVALID,
            message=MSG_INVALID_ACCESS_TOKEN,
            reason=f"unknown/unloadable domain {token_domain} (post-signature)",
            domain=token_domain,
        )
    if token_domain and domain_policy.status != "active":
        return PepOutcome(
            "deny", "domain-frozen", code=CODE_AUTHORIZATION_FAILED,
            message=MSG_AUTHORIZATION_FAILED,
            reason=f"domain {token_domain} frozen at revision {domain_policy.revision}",
            domain=token_domain, domain_revision=domain_policy.revision,
        )

    # 父绑定的密码学复核：verify_token 已经验过嵌入的父 grant，这里再取一次已验证
    # claims，用于检查 parent iss ∈ 域 issuers，并以**父 grant 自己签过的**配额/到期
    # 上界扣减——不信任子令牌里的 acps_parent_* 自报值。
    parent_claims_verified: dict[str, Any] | None = None
    if token_domain:
        from .grant import verify_grant  # noqa: PLC0415

        parent_grant = str(claims.get("acps_parent_grant") or "")
        try:
            parent_claims_verified = verify_grant(
                parent_grant,
                trust_bundle=cfg.trust_bundle,
                now=checked_at,
                clock_skew_seconds=cfg.clock_skew_seconds,
            )
        except TokenError as exc:
            return PepOutcome(
                "deny", "invalid-parent-binding", code=CODE_ACCESS_TOKEN_INVALID,
                message=MSG_INVALID_ACCESS_TOKEN, reason=exc.reason,
                jti=str(claims.get("jti") or "") or None,
                domain=token_domain, domain_revision=domain_policy.revision,
            )
        parent_issuer = normalize_audience(str(parent_claims_verified.get("iss") or ""))
        if parent_issuer not in domain_policy.issuers:
            return PepOutcome(
                "deny", "parent-issuer-not-in-domain", code=CODE_ACCESS_TOKEN_INVALID,
                message=MSG_INVALID_ACCESS_TOKEN,
                reason=f"parent grant issuer {parent_issuer} not in domain issuers",
                jti=str(claims.get("jti") or "") or None,
                domain=token_domain, domain_revision=domain_policy.revision,
                parent_jti=str(parent_claims_verified.get("jti") or "") or None,
            )

    token_skill = str(claims.get("acps_skill_id") or "")
    skill_key = token_skill or skill or ""
    policy_domains = domains_governing_skill(cfg, policies, skill_key)
    declared = frozenset(cfg.domain_skills.get(skill_key, ()))
    # 只有"代码声明了会员制"且"章程真的把该资产纳入管辖"时才强制域令牌。
    # 后者是访问控制权威，前者防止把一条演示章程当成对所有调用方式的封锁；
    # 二者同时命中才说明这个部署确实在运营一个域（eval.submit / kb.query 等）。
    required_domains = declared & policy_domains
    if required_domains and not token_domain:
        # 关键绕过修复：普通令牌不能"省掉 parent_jti"进入已生效的域——
        # 否则冻结、成员表、配额全部失效。
        return PepOutcome(
            "deny", "domain-required", code=CODE_AUTHORIZATION_FAILED,
            message=MSG_AUTHORIZATION_FAILED,
            reason=f"skill {skill_key} is domain-governed ({sorted(required_domains)}); "
                   "a domain-scoped token derived from a grant is required",
            jti=str(claims.get("jti") or "") or None,
            skill_hint=skill_key or None,
            skill_source=SKILL_SOURCE_TOKEN_CLAIM if token_skill else None,
        )
    # 已经声称域身份的令牌，还要对得上代码声明的目标域；代码没声明时退回章程集合。
    if token_domain:
        allowed_domains = declared or policy_domains
        if allowed_domains and token_domain not in allowed_domains:
            return PepOutcome(
                "deny", "domain-skill-mismatch", code=CODE_AUTHORIZATION_FAILED,
                message=MSG_AUTHORIZATION_FAILED,
                reason=f"skill {skill_key} belongs to {sorted(allowed_domains)}, "
                       f"token domain={token_domain}",
                jti=str(claims.get("jti") or "") or None,
                skill_hint=skill_key or None,
            )

    # 3a 补充：签发者授权——受信证书 ≠ 有权签发。社区内任何 agent 都持有效证书，
    # 必须限定只有白名单 leader（或未来 STS）签发的 token 才被接受（2026-09-28 审计 PoC：
    # critic 自签 token 可通过全部其余校验）。域派生 token 额外接受域签发者（继承链：
    # leader 从域 grant 派生，leader 本身应在域 issuers 中；∪ 白名单保持单机退化兼容）。
    issuer_aic = normalize_audience(str(claims.get("iss") or ""))
    allowed_issuers = cfg.leader_aics
    if token_domain and domain_policy is not None:
        allowed_issuers = cfg.leader_aics | set(domain_policy.issuers)
    if issuer_aic not in allowed_issuers:
        return PepOutcome(
            "deny",
            "issuer-not-authorized",
            code=CODE_ACCESS_TOKEN_INVALID,
            message=MSG_INVALID_ACCESS_TOKEN,
            reason="token issuer is not an authorized leader",
        )

    # 3a'' 域派生 delegation 的成员/资产一致性：peer（act 受托人）必须是域成员且角色
    #     被允许调用 (本机, acps_skill_id) 这项资产。防"非成员拿着域内派生 token 硬闯"（D3b
    #     的 delegation 投影）；skill 维度仍受下方 4a 约束（声明式），此处按域章程裁决。
    token_skill = str(claims.get("acps_skill_id") or "")   # 域校验与 replay 作用域都要用
    if token_domain and domain_policy is not None:
        peer_role = domain_policy.member_role(request.leader_aic)
        if peer_role is None:
            return PepOutcome(
                "deny",
                "domain-not-member",
                code=CODE_AUTHORIZATION_FAILED,
                message=MSG_AUTHORIZATION_FAILED,
                reason=f"caller not a member of domain {token_domain}",
            )
        if not domain_policy.asset_allows(
            provider_aic=cfg.my_aic, skill_id=token_skill or (skill or ""), role=peer_role
        ):
            return PepOutcome(
                "deny",
                "domain-asset-not-allowed",
                code=CODE_AUTHORIZATION_FAILED,
                message=MSG_AUTHORIZATION_FAILED,
                reason=f"asset ({cfg.my_aic}, {token_skill or skill}) not allowed for role {peer_role!r}",
            )

    jti = str(claims.get("jti") or "")
    delegation_id = str(claims.get("acps_delegation_id") or "") or None

    # 3e act 与 peer AIC 绑定（受托人 == 实际调用方；主体级绑定，见模块说明）
    act = claims["act"]
    if str(act.get("aic") or "") != request.leader_aic:
        return PepOutcome(
            "deny",
            "act-peer-mismatch",
            code=CODE_AUTHORIZATION_FAILED,
            message=MSG_AUTHORIZATION_FAILED,
            reason="act.aic != peer aic",
            jti=jti,
            delegation_id=delegation_id,
        )

    # 3f cnf 与 mTLS client certificate 绑定——本实现无通道（admission 请求不含对端证书），
    #    以 3e 的主体级绑定等价替代；cnf 存在时只记审计标记。
    cnf_unverifiable = "cnf" in claims

    # 3g 固定链路模式：当前 hop == route 中的相邻位置
    if claims.get("acps_delegation_mode") == "fixed":
        hop = (request.leader_aic, cfg.my_aic)
        if not _is_adjacent_in_route(claims.get("acps_allowed_route") or (), hop):
            return PepOutcome(
                "deny",
                "route-not-allowed",
                code=CODE_AUTHORIZATION_FAILED,
                message=MSG_AUTHORIZATION_FAILED,
                reason="current hop not in acps_allowed_route",
                jti=jti,
                delegation_id=delegation_id,
            )

    # 3h 会话绑定（本设计扩展 claim，非 §11.3 条目）
    claim_session = claims.get("sessionId")
    if claim_session:
        if str(claim_session) != request.session_id:
            return PepOutcome(
                "deny",
                "session-mismatch",
                code=CODE_AUTHORIZATION_FAILED,
                message=MSG_AUTHORIZATION_FAILED,
                reason="sessionId claim != request session",
                jti=jti,
                delegation_id=delegation_id,
            )
    elif cfg.require_session_binding:
        return PepOutcome(
            "deny",
            "session-binding-missing",
            code=CODE_AUTHORIZATION_FAILED,
            message=MSG_AUTHORIZATION_FAILED,
            reason="token has no sessionId claim",
            jti=jti,
            delegation_id=delegation_id,
        )

    # ── §11.3 步骤 4：解析资源与动作 ──
    # 4a skill 粒度强制(fail-closed,2026-10-06 审查修复):此前"调用方不写 skillId
    #    就跳过校验"——令牌授予 skill_x,请求不带 skillId 即可调任意能力。现在:
    #    请求未指名 skill → 拒(token-skill-unconfirmed,令牌授予与请求不一致无法确认);
    #    请求已指名但不等于令牌授予 → 拒(skill-mismatch,原语义保留)。
    #    调用方修复方式:请求 dataItems 带上 metadata.skillId(与令牌签发一致)。
    token_skill = str(claims.get("acps_skill_id") or "")
    if skill is None:
        return PepOutcome(
            "deny",
            "token-skill-unconfirmed",
            code=CODE_AUTHORIZATION_FAILED,
            message=MSG_AUTHORIZATION_FAILED,
            reason=f"request carries no skillId; token grants {token_skill or 'unscoped'}",
            jti=jti,
            delegation_id=delegation_id,
        )
    if skill != token_skill:
        return PepOutcome(
            "deny",
            "skill-mismatch",
            code=CODE_AUTHORIZATION_FAILED,
            message=MSG_AUTHORIZATION_FAILED,
            reason=f"requested {skill}, token grants {token_skill}",
            jti=jti,
            delegation_id=delegation_id,
        )

    # 4b 链深（当前版本 max=2 且 A 不得转委托：chain_depth == max 即拒）
    if int(claims.get("acps_chain_depth", 0)) >= int(claims.get("acps_max_chain_depth", 2)):
        return PepOutcome(
            "deny",
            "chain-depth-exceeded",
            code=CODE_AUTHORIZATION_FAILED,
            message=MSG_AUTHORIZATION_FAILED,
            reason="chain depth reached max",
            jti=jti,
            delegation_id=delegation_id,
        )

    # 所有确定性校验通过后才消费 jti：错误调用方/错误 session 的请求先占 jti 会让
    # 合法持有者被误判 replay（2026-10-18 P1-12）。
    scope = f"{request.leader_aic or ''}|{token_skill or skill or ''}"
    if not registry.claim(jti, expires_at=int(claims["exp"]), now=checked_at, scope=scope):
        return PepOutcome(
            "deny",
            "replay",
            code=CODE_ACCESS_TOKEN_INVALID,
            message=MSG_INVALID_ACCESS_TOKEN,
            reason="jti already used",
            jti=jti,
            delegation_id=delegation_id,
        )

    # 派生委托也消耗父 grant 的使用配额（签发侧已强制把 parent_jti/max_uses 写进子令牌）。
    # 否则 grant 的 acps_max_uses 只对"直接出示 grant"生效，leader 可以无限派生绕过。
    parent_jti = None
    parent_remaining = None
    if token_domain:
        if parent_claims_verified is None:
            return PepOutcome(
                "deny", "invalid-parent-binding", code=CODE_ACCESS_TOKEN_INVALID,
                message=MSG_INVALID_ACCESS_TOKEN,
                reason="verified parent grant missing",
                jti=jti, delegation_id=delegation_id, domain=token_domain,
                domain_revision=domain_policy.revision if domain_policy else None,
            )
        parent_jti = str(parent_claims_verified.get("jti") or "")
        try:
            parent_exp = int(parent_claims_verified.get("exp") or 0)
            parent_max_uses = int(parent_claims_verified.get("acps_max_uses") or 0)
            child_parent_jti = str(claims.get("acps_parent_jti") or "")
            child_parent_exp = int(claims.get("acps_parent_exp") or 0)
            child_parent_max = int(claims.get("acps_parent_max_uses") or 0)
        except (TypeError, ValueError):
            return PepOutcome(
                "deny", "invalid-parent-binding", code=CODE_ACCESS_TOKEN_INVALID,
                message=MSG_INVALID_ACCESS_TOKEN,
                reason="parent exp/max_uses missing or malformed",
                jti=jti, delegation_id=delegation_id, domain=token_domain,
                domain_revision=domain_policy.revision if domain_policy else None,
            )
        if (
            not parent_jti
            or parent_max_uses < 1
            or child_parent_jti != parent_jti
            or child_parent_exp != parent_exp
            or child_parent_max != parent_max_uses
        ):
            return PepOutcome(
                "deny", "invalid-parent-binding", code=CODE_ACCESS_TOKEN_INVALID,
                message=MSG_INVALID_ACCESS_TOKEN,
                reason="child parent claims do not match the verified parent grant",
                jti=jti, delegation_id=delegation_id, domain=token_domain,
                domain_revision=domain_policy.revision if domain_policy else None,
                parent_jti=parent_jti or None,
            )
        if int(claims["exp"]) > parent_exp:
            return PepOutcome(
                "deny", "parent-exp-exceeded", code=CODE_ACCESS_TOKEN_INVALID,
                message=MSG_INVALID_ACCESS_TOKEN,
                reason=f"child exp={claims['exp']} > parent exp={parent_exp}",
                jti=jti, delegation_id=delegation_id, domain=token_domain,
                domain_revision=domain_policy.revision if domain_policy else None,
                parent_jti=parent_jti,
            )
        if cfg.quota_path is None:
            return PepOutcome(
                "deny", "pep-misconfigured", code=CODE_AUTHORIZATION_FAILED,
                message=MSG_AUTHORIZATION_FAILED,
                reason="quota store missing (derived domain tokens consume parent quota)",
                jti=jti, delegation_id=delegation_id, domain=token_domain,
                domain_revision=domain_policy.revision if domain_policy else None,
                parent_jti=parent_jti,
            )
        from .quota import consume as quota_consume

        ok, parent_remaining = quota_consume(
            cfg.quota_path,
            jti=parent_jti, domain=token_domain, caller_aic=request.leader_aic,
            max_uses=parent_max_uses, now=checked_at,
        )
        if not ok:
            return PepOutcome(
                "deny", "parent-quota-exhausted", code=CODE_AUTHORIZATION_FAILED,
                message=MSG_AUTHORIZATION_FAILED,
                reason=f"parent grant jti={parent_jti[:8]}… max_uses={parent_max_uses} exhausted",
                jti=jti, delegation_id=delegation_id, domain=token_domain,
                domain_revision=domain_policy.revision if domain_policy else None,
                parent_jti=parent_jti, parent_quota_remaining=0,
            )

    return PepOutcome(
        "allow",
        "domain-delegation" if token_domain else "delegation-token",
        jti=jti,
        delegation_id=delegation_id,
        cnf_unverifiable=cnf_unverifiable,
        domain=token_domain or None,
        domain_revision=(int(claims.get("acps_domain_revision")) if token_domain and claims.get("acps_domain_revision") else None),
        skill_hint=skill or (str(claims.get("acps_skill_id") or "") or None),
        parent_jti=parent_jti,
        parent_quota_remaining=parent_remaining,
    )



def _evaluate_grant(
    request: AipPartnerAdmissionRequest,
    cfg: PepConfig,
    token: str,
    policy: Any,
    skill: str | None,
    *,
    checked_at: float,
) -> PepOutcome:
    """域级 grant 画像裁决（第九轮）：验签 → 签发者∈域 → 主体绑定 → 成员/角色/资产
    → 配额（sqlite 原子）→ allow。D3a/D3b 的执行点。
    """
    from .grant import verify_grant

    try:
        claims = verify_grant(
            token, trust_bundle=cfg.trust_bundle, now=checked_at,
            clock_skew_seconds=cfg.clock_skew_seconds,
        )
    except TokenError as exc:
        return PepOutcome(
            "deny", "invalid-token", code=CODE_ACCESS_TOKEN_INVALID,
            message=MSG_INVALID_ACCESS_TOKEN, reason=exc.reason,
            domain=policy.domain_id if policy else None,
        )

    jti = str(claims.get("jti") or "")
    domain_id = policy.domain_id

    # 域签发者（iss ∈ 域 issuers；单机白名单不适用——grant 的权力来源是章程不是编排关系）
    issuer_aic = normalize_audience(str(claims.get("iss") or ""))
    if issuer_aic not in policy.issuers:
        return PepOutcome(
            "deny", "issuer-not-in-domain", code=CODE_ACCESS_TOKEN_INVALID,
            message=MSG_INVALID_ACCESS_TOKEN,
            reason="grant issuer not in domain issuers",
            jti=jti, domain=domain_id, domain_revision=policy.revision,
        )

    # 主体绑定（D3a 盗用）：aud=持有者，只能由 sub 本人使用
    subject_aic = normalize_audience(str(claims.get("sub") or ""))
    if subject_aic != request.leader_aic:
        return PepOutcome(
            "deny", "grant-subject-mismatch", code=CODE_AUTHORIZATION_FAILED,
            message=MSG_AUTHORIZATION_FAILED, reason="grant sub != peer aic",
            jti=jti, domain=domain_id, domain_revision=policy.revision,
        )

    # 成员/角色（D3b 非成员）：caller 必须是域成员
    role = policy.member_role(request.leader_aic)
    if role is None:
        return PepOutcome(
            "deny", "grant-not-member", code=CODE_AUTHORIZATION_FAILED,
            message=MSG_AUTHORIZATION_FAILED,
            reason=f"caller not a member of domain {domain_id}",
            jti=jti, domain=domain_id, domain_revision=policy.revision,
        )

    # 资产解析：grant 在本机（provider）覆盖的资产；声明了 skill 则必须命中
    grant_assets_here = {s for (p, s) in ((str(a.get("providerAic")), str(a.get("skill"))) for a in claims.get("acps_assets") or ()) if p == cfg.my_aic}
    if skill is not None:
        if skill not in grant_assets_here:
            return PepOutcome(
                "deny", "skill-mismatch", code=CODE_AUTHORIZATION_FAILED,
                message=MSG_AUTHORIZATION_FAILED,
                reason=f"requested {skill}, grant covers {sorted(grant_assets_here)} at this provider",
                jti=jti, domain=domain_id, domain_revision=policy.revision,
            )
        resolved_skill = skill
    elif len(grant_assets_here) == 1:
        resolved_skill = next(iter(grant_assets_here))
    else:
        return PepOutcome(
            "deny", "grant-skill-required", code=CODE_AUTHORIZATION_FAILED,
            message=MSG_AUTHORIZATION_FAILED,
            reason=f"grant covers {sorted(grant_assets_here)} at this provider; declare skillId",
            jti=jti, domain=domain_id, domain_revision=policy.revision,
        )

    # 代码里的 domain visibility 声明是第二把锁：即使某个域章程把该资产列进来了，
    # 若技能声明只属于别的域，也不接受这一域的 grant。
    declared_domains = cfg.domain_skills.get(resolved_skill)
    if declared_domains and domain_id not in declared_domains:
        return PepOutcome(
            "deny", "grant-domain-mismatch", code=CODE_AUTHORIZATION_FAILED,
            message=MSG_AUTHORIZATION_FAILED,
            reason=f"skill {resolved_skill} is declared for domains {sorted(declared_domains)}, "
                   f"grant domain={domain_id}",
            jti=jti, domain=domain_id, domain_revision=policy.revision,
        )

    # 域章程对该资产的角色许可（grant 说"给了什么"，章程说"这个角色配不配用"）
    if not policy.asset_allows(provider_aic=cfg.my_aic, skill_id=resolved_skill, role=role):
        return PepOutcome(
            "deny", "grant-role-not-allowed", code=CODE_AUTHORIZATION_FAILED,
            message=MSG_AUTHORIZATION_FAILED,
            reason=f"role {role!r} not allowed on asset ({cfg.my_aic}, {resolved_skill})",
            jti=jti, domain=domain_id, domain_revision=policy.revision,
        )

    # 使用侧配额（sqlite 原子，按需开合；无存储则 fail-closed）
    if cfg.quota_path is None:
        return PepOutcome(
            "deny", "pep-misconfigured", code=CODE_AUTHORIZATION_FAILED,
            message=MSG_AUTHORIZATION_FAILED,
            reason="quota store missing (grant path requires persistent counter)",
            jti=jti, domain=domain_id, domain_revision=policy.revision,
        )
    from .quota import consume as quota_consume

    ok, _ = quota_consume(
        cfg.quota_path,
        jti=jti, domain=domain_id, caller_aic=request.leader_aic,
        max_uses=int(claims["acps_max_uses"]), now=checked_at,
    )
    if not ok:
        return PepOutcome(
            "deny", "quota-exhausted", code=CODE_AUTHORIZATION_FAILED,
            message=MSG_AUTHORIZATION_FAILED,
            reason=f"acps_max_uses={claims['acps_max_uses']} exhausted",
            jti=jti, domain=domain_id, domain_revision=policy.revision,
        )

    return PepOutcome(
        "allow", "domain-grant", jti=jti,
        domain=domain_id, domain_revision=policy.revision, skill_hint=resolved_skill,
    )


def pep_decide(
    request: AipPartnerAdmissionRequest,
    cfg: PepConfig,
    *,
    jti_registry: JtiRegistry | None = None,
    now: float | None = None,
) -> AipPartnerAdmissionDecision:
    """admission hook 的裁决入口：总开关关闭时回退 wit 默认 accept 行为。"""
    if not cfg.enabled:
        return AipPartnerAdmissionDecision.accept()
    try:
        outcome = evaluate(request, cfg, jti_registry=jti_registry, now=now)
    except Exception as exc:  # noqa: BLE001
        # 兜底不变量：任何未预期异常收敛为「拒绝 + 审计 + §12 通用文案」，绝不裸穿到
        # RPC 层（wit 的 admission 分发无 try/except，异常会让任务不建档、无审计行，
        # 对端拿到内部错误——见 PEP-result.md 第四轮缺陷 4）。细节只进审计。
        outcome = PepOutcome(
            "deny",
            "pep-internal-error",
            code=CODE_AUTHORIZATION_FAILED,
            message=MSG_AUTHORIZATION_FAILED,
            reason=f"{type(exc).__name__}: {exc}"[:200],
        )
    _trace(cfg, request, outcome, extra={"cnfUnverifiable": True} if outcome.cnf_unverifiable else None)
    # 把"本任务被准入的 skill"记进 ContextVar，供能力收窄钩子（P0-3 缓解）使用：
    # 未被准入的非公开能力会在 run 开始时被移出工具集。
    try:
        from . import skill_scope  # noqa: PLC0415

        skill_scope.set_admitted_skill(outcome.skill_hint if outcome.allowed else None)
    except Exception:  # noqa: BLE001
        pass
    return outcome.as_decision()


class _PepAdmissionHooks(AipPartnerAdmissionHooks):
    id = "yanshu.pep"
    priority = 100
    config: PepConfig | None = None


def build_pep_hook(
    *,
    cfg: PepConfig | None = None,
    workspace: Path | str | None = None,
    public_skills: Iterable[str] = (),
    has_non_public_skills: bool = False,
    domain_skills: Mapping[str, Iterable[str]] | None = None,
    jti_registry: JtiRegistry | None = None,
    env: Mapping[str, str] | None = None,
    priority: int = 100,
) -> AipPartnerAdmissionHooks:
    """§4.3 的注册方式：`WitAgent(..., aip_hooks=[build_pep_hook()])`。"""
    if cfg is None:
        if workspace is None:
            raise ValueError("build_pep_hook requires either cfg or workspace")
        cfg = load_pep_config(
            workspace,
            public_skills=public_skills,
            has_non_public_skills=has_non_public_skills,
            domain_skills=domain_skills,
            env=env,
        )
    else:
        _validate_config(cfg)
    registry = jti_registry if jti_registry is not None else DEFAULT_JTI_REGISTRY

    async def handler(request: AipPartnerAdmissionRequest) -> AipPartnerAdmissionDecision:
        return pep_decide(request, cfg, jti_registry=registry)

    hook_type = type(
        "_ConfiguredPepHooks", (_PepAdmissionHooks,), {"priority": priority, "config": cfg}
    )
    return hook_type(handler)
