"""研枢集群公共配置：ACPS 配置、凭证、partner 常驻模板。

行为全部由环境变量控制（.env / systemd EnvironmentFile）：
  ACPS_PLATFORM=ioa_pub|custom  ACPS_PUBLIC_HOST  ACPS_CERT_SAN_KIND=dns|ip  ACPS_SERVE_HOST
"""

import asyncio
import contextvars
import json
import os
from pathlib import Path
from typing import Callable

from wit import (
    AcpsConfig,
    AgentCertificateRequest,
    AipPartnerRequestEvent,
    AipPartnerTransformHooks,
    AgentProviderProfile,
    RegistryPasswordCredentials,
    RunConfig,
    WitAgent,
    WitHooks,
    WitRuntime,
)

# ── 框架补丁：伙伴处理外部 AIP 任务时 RunConfig 默认 tool_timeout=60s ──
# 生成类工具（paper.write full / paper.review）单次 LLM 调用常超过 60s，会被误判超时
# 导致任务失败（step_budget_exhausted / Agent produced no AIP Product）。
# 这里把默认抬到 ACPS_TASK_TOOL_TIMEOUT（默认 300s，与 leader 侧 tool_timeout 一致）。
# 上游默认值从 dataclass 字段读而不是写死 60.0：wit 改了默认值时本补丁会跟着适配，
# 不会静默失效（2026-10-07 审查修复）。
_TASK_TOOL_TIMEOUT = float(os.getenv("ACPS_TASK_TOOL_TIMEOUT", "300"))
_orig_post_init = RunConfig.__post_init__
_UPSTREAM_TOOL_TIMEOUT = RunConfig.__dataclass_fields__["tool_timeout_seconds"].default


def _patched_post_init(self):  # type: ignore[no-untyped-def]
    if self.tool_timeout_seconds in (None, _UPSTREAM_TOOL_TIMEOUT):
        object.__setattr__(self, "tool_timeout_seconds", _TASK_TOOL_TIMEOUT)
    _orig_post_init(self)


RunConfig.__post_init__ = _patched_post_init


# ── 对端身份：归因与角色只从 mTLS 已验证的对端 AIC 派生，不接受调用方自报 ──
# 与 aip_guard 的分工：guard 只做内容外壳化并明确"不碰身份"，身份在这里统一收口。
_PEER_AIC = contextvars.ContextVar("yanshu_peer_aic", default=None)


class PeerIdentityHooks(AipPartnerTransformHooks):
    """记录本 inbound 请求的对端 AIC；每个请求先复位，避免继承上一个请求的身份。"""

    id = "yanshu.peer-identity"
    priority = 55  # 晚于外壳化(50)、早于令牌剥离(60)与 PEP

    async def on_partner_request(self, event: AipPartnerRequestEvent) -> None:
        peer = getattr(event, "leader_aic", None)
        _PEER_AIC.set(str(peer) if peer else None)


def current_peer_aic() -> str | None:
    return _PEER_AIC.get()


def set_peer_for_direct_call(peer: str | None) -> None:
    """本机直调/演示用：显式扮演某个已认证对端（wire 路径由 PeerIdentityHooks 写）。"""
    _PEER_AIC.set(str(peer) if peer else None)


def resolve_submitter(claimed: str | None) -> tuple[str, bool]:
    """榜单/溯源归属：有已验证对端就用它；没有（本机直调、离线演示）才用自报名并标记。"""
    peer = _PEER_AIC.get()
    if peer:
        return peer, True
    return (str(claimed or "").strip() or "?"), False


# ── 框架补丁：wit 2.1.0 的 discovery 过滤条件平台已废弃 ──
# wit 的 `discovery_filter()` 在 include_unavailable=True（**这是默认值**）时会构造
# `{"conditions":[{"field":"onlyAvailable","op":"eq","value":False}]}` 塞进请求体的 `filter`
# 字段。而 wt.ioa.pub 已整体废弃该字段：实测**任何非空 filter 对象都返回 400**（连
# `{"conditions":[]}` 也拒），只有不带 filter / `filter:null` 才 200；替代是顶层 `extFilter`
# （`{"alive":true|false}` 均 200）。
# 后果：LLM 工具 `acps_discover` 的 schema 里 `include_unavailable` 默认 true → 模型不显式
# 传就必然 400 → **supervisor 发现不了任何 agent**（实测 2026-10-02；
# `runtime.aip_discover` 与 `AcpsDiscoveryClient.discover` 两条路径同错）。
# 修法：让 `discovery_filter` 恒返回 None，请求体不再带 filter。
#   代价：放弃 wit 侧的 include_unavailable 过滤能力，改由平台默认排序决定。可接受——
#   可达性在调用时天然会验证（端点不可用 `aip_delegate` 会立刻失败），且关键调度链路本来
#   就走 `cluster_roster` 名册，discovery 只是 fallback。若平台重新支持 filter，或拿到修好的
#   wit wheel，本补丁可直接删除（"不带 filter"对任何版本都是合法请求）。
# 能这样打：`discover()` 是在**调用时**从模块全局取 `discovery_filter`（不是 import 时捕获），
# 所以在这里重新赋值即生效；实测默认参数调用、真 `aip_discover` 均 200，且发现的 agent
# 会正常落进 `<workspace>/acps/discovered/` 缓存。
def _no_discovery_filter(*, include_unavailable: bool = True, filter: object = None) -> None:
    """替换 `wit.acps.discovery.discovery_filter`：恒返回 None（不带 filter）。"""
    return None


def _patch_discovery_filter() -> None:
    """幂等：重复调用赋的还是同一个函数对象。"""
    import wit.acps.discovery as _discovery  # noqa: PLC0415

    _discovery.discovery_filter = _no_discovery_filter


_patch_discovery_filter()


# ── 框架补丁：模型调用总时长硬闸 ────────────────────────────────────────────
# 为什么打在 wit 这一层（2026-10-10 实测）：
#   我们曾在 provider 上包装 `stream()`/`complete()`（`bootstrap.with_response_deadline`），
#   但线上仍出现"伙伴进程 CPU 0.0%、主线程在 select() 等 I/O、入站任务卡 9 小时"：
#   卡点在 SDK 发出请求到**首个响应到达之前**这段，且 httpx 的 timeout 是**读间隔**而非
#   总时长，provider 侧包装无法覆盖全部路径。
#   `WitRuntime._call_model_stream` 是 wit 调用模型的**唯一出口**，在这里设总闸最可靠。
# 与既有 `_patch_discovery_filter` 同类做法（monkey patch 上游函数），并保持幂等。
def _patch_model_call_deadline() -> None:
    if os.getenv("ACPS_MODEL_DEADLINE", "1").lower() in ("0", "false", "no", "off"):
        return
    try:
        deadline = float(os.getenv("ACPS_MODEL_CALL_DEADLINE", "900"))
    except ValueError:
        return
    try:
        from wit.kernel.runtime import WitRuntime  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return
    orig = getattr(WitRuntime, "_call_model_stream", None)
    if orig is None or getattr(orig, "_yanshu_deadline", False):
        return

    async def _with_deadline(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        import asyncio  # noqa: PLC0415

        try:
            return await asyncio.wait_for(orig(self, *args, **kwargs), timeout=deadline)
        except asyncio.TimeoutError as exc:  # noqa: F821
            raise TimeoutError(
                f"model call exceeded {deadline:.0f}s total deadline "
                f"(ACPS_MODEL_CALL_DEADLINE) —— 已中止本次模型调用以避免任务无限挂起"
            ) from exc

    _with_deadline._yanshu_deadline = True  # type: ignore[attr-defined]
    WitRuntime._call_model_stream = _with_deadline  # type: ignore[method-assign]


_patch_model_call_deadline()


def _public_host() -> str:
    return os.getenv("ACPS_PUBLIC_HOST", "localhost")


def _patch_ca_csr_san() -> None:
    """给 wit 2.1.0 的 CSR 生成器补 SAN（IP 端点证书缺陷的本地工作区）。

    wit 的 `wit.acps.ca._generate_csr(private_key, common_name)` 只写 CN，不写
    SubjectAlternativeName；`issue_certificate_pair()` 签完证书后再用
    `_validate_public_endpoint()` 校验，IP 端点必然报
    `server_public_endpoint_san_mismatch`（平台 CA 甚至收不到请求）。
    这里按 ACPS_PUBLIC_HOST 自动选 IP/DNS SAN，CSR 是唯一能把它带进 CA 的通道。
    幂等；未安装 cryptography / wit 结构变化时静默跳过。
    """
    try:
        import ipaddress

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519
        from cryptography.x509.oid import NameOID
        from wit.acps import ca as _ca
    except Exception:  # noqa: BLE001
        return
    orig = getattr(_ca, "_generate_csr", None)
    if orig is None or getattr(orig, "_yanshu_san_patch", False):
        return

    def _generate_csr(private_key, common_name):  # type: ignore[no-untyped-def]
        host = _public_host()
        try:
            san = x509.IPAddress(ipaddress.ip_address(host))
        except ValueError:
            try:
                san = x509.DNSName(host)
            except ValueError:
                return orig(private_key, common_name)
        algorithm = None if isinstance(private_key, ed25519.Ed25519PrivateKey) else hashes.SHA256()
        builder = (
            x509.CertificateSigningRequestBuilder()
            .subject_name(x509.Name([
                x509.NameAttribute(NameOID.COMMON_NAME, common_name),
            ]))
            .add_extension(x509.SubjectAlternativeName([san]), critical=False)
        )
        csr = builder.sign(private_key, algorithm)
        return csr.public_bytes(serialization.Encoding.PEM)

    _generate_csr._yanshu_san_patch = True  # type: ignore[attr-defined]
    _ca._generate_csr = _generate_csr  # type: ignore[attr-defined]


_patch_ca_csr_san()


def build_certificate() -> AgentCertificateRequest:
    host = _public_host()
    if os.getenv("ACPS_CERT_SAN_KIND", "dns").lower() == "ip":
        return AgentCertificateRequest(ip=(host,))
    return AgentCertificateRequest(dns=(host,))


def build_provider_profile() -> AgentProviderProfile:
    return AgentProviderProfile(
        name=os.getenv("ACPS_PROVIDER_NAME", "yanshu-lab"),
        organization=os.getenv("ACPS_PROVIDER_ORG", "Wit Research Lab"),
        email=os.getenv("ACPS_PROVIDER_EMAIL", "research@example.com"),
        country_code=os.getenv("ACPS_COUNTRY_CODE", "CN"),
    )


def build_acps_config(port: int) -> AcpsConfig:
    endpoint = f"https://{_public_host()}:{port}/rpc"
    if os.getenv("ACPS_PLATFORM", "custom").lower() == "ioa_pub":
        return AcpsConfig.ioa_pub(
            provider=build_provider_profile(),
            certificate=build_certificate(),
            public_endpoint=endpoint,
        )
    return AcpsConfig.custom(
        registry_url=os.environ["ACPS_REGISTRY_URL"],
        ca_url=os.environ["ACPS_CA_URL"],
        discovery_url=os.environ["ACPS_DISCOVERY_URL"],
        public_endpoint=endpoint,
        certificate=build_certificate(),
        provider=build_provider_profile(),
        strict_interop=True,
    )


def build_local_acps_config(port: int) -> AcpsConfig:
    """本地直连用的固定 localhost 配置；不走 build_acps_config 的平台/环境变量路由。"""
    return AcpsConfig.custom(
        registry_url="https://localhost:9001",
        ca_url="https://localhost:9003",
        discovery_url="https://localhost:9005",
        public_endpoint=f"https://localhost:{port}/rpc",
        certificate=AgentCertificateRequest(dns=("localhost",)),
        provider=AgentProviderProfile(name="yanshu", organization="Wit", email="e@e.com", country_code="CN"),
        strict_interop=True,
    )


def build_credentials(prefix: str) -> RegistryPasswordCredentials:
    return RegistryPasswordCredentials(
        os.environ[f"ACPS_{prefix}_USERNAME"], os.environ[f"ACPS_{prefix}_PASSWORD"]
    )


# ── 本地模式（不经 registry，dev CA 自铸身份）的终稿 ACS 维护 ──
# 背景：本地铸造的身份没有注册表回填，`acs.final.json` 要等 partner 首次启动才生成，
# 而且生成后若代码改了能力（例：gpu 从 3 个能力加到 5 个）就再也不会更新 → 名册看到的
# 能力会陈旧（既不在 ACS、也不在 declared → 不可规划）。
def _read_json_or_none(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _skill_ids(acs: dict | None) -> list[str]:
    return sorted(
        str(s.get("id")) for s in ((acs or {}).get("skills") or []) if isinstance(s, dict) and s.get("id")
    )


def ensure_local_acs(runtime: WitRuntime, workspace: Path | str) -> str | None:
    """本地模式下维护终稿 ACS，并回报本机 AIC。

    **何时重新生成**：终稿缺失，或草稿与终稿的 **skills 集合不一致**（代码改了能力）。

    **生成时顺带处理两个身份字段**：
    - `aic`：本地铸造没有注册表回填，把 `enrollment.json → identity.ref.aic` 写进 ACS
      （指纹不含该字段，写它不影响指纹；名册与 seed_discovery 都读它）。
    - `acsFingerprint` **置 null**：实测用 wit 的 `_fingerprint` / `_final_acs_fingerprint` 在
      draft / final / authoritativeAcs / filled 四种输入、raw 与 normalized 两种形态上共
      **16 种组合都复现不出**已注册 agent 的存档值——基线是提交时刻由 provisioner 在别处算的。
      写一个"看起来合法"的错值比 null 更危险（会触发
      `acs_fingerprint_changed_after_submission`）；留 null = 诚实的"没有基线"，将来真走注册
      时 provisioner 会自己算并覆盖。
    - `acpsConfigFingerprint`：**可以算准**（`_fingerprint(_safe_acps_config(cfg))` 实测与已注册
      agent 存档完全一致），所以重算而不是清空。

    该函数幂等：每次都重写这两个指纹字段（也顺带修复早期铸造脚本抄进来的模板值）。
    """
    acps_dir = Path(workspace) / "acps"
    enrollment_path = acps_dir / "enrollment.json"
    enrollment = _read_json_or_none(enrollment_path)
    aic = ((enrollment or {}).get("identity") or {}).get("ref", {}).get("aic") or None

    draft = runtime.acs_draft()
    new_skills = _skill_ids(draft)
    final = _read_json_or_none(acps_dir / "acs.final.json")

    if final is None or _skill_ids(final) != new_skills:
        if aic:
            draft = {**draft, "aic": aic}
        body = json.dumps(draft, ensure_ascii=False, indent=2)
        (acps_dir / "acs.draft.json").write_text(body, encoding="utf-8")
        (acps_dir / "acs.final.json").write_text(body, encoding="utf-8")
        reason = "终稿缺失" if final is None else "skills 与代码不一致"
        print(f"[local-acs] 重新生成终稿 ACS（{reason}）：{len(new_skills)} 个能力 {new_skills}")
    elif aic and not final.get("aic"):
        final = {**final, "aic": aic}
        (acps_dir / "acs.final.json").write_text(
            json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[local-acs] 已补写终稿 ACS 的 aic={aic}")

    if enrollment is not None:
        from wit.acps.provisioner import _fingerprint, _safe_acps_config  # noqa: PLC0415

        enrollment["acsFingerprint"] = None  # 见上：复现不出，留 null 才诚实
        enrollment["acpsConfigFingerprint"] = _fingerprint(_safe_acps_config(runtime.acps_config))
        enrollment["finalAcsSource"] = "local-dev-ca"
        enrollment_path.write_text(json.dumps(enrollment, ensure_ascii=False, indent=2), encoding="utf-8")

    return str(aic) if aic else None


def seed_workspace_peer(workspace: Path) -> None:
    """把 workspace 的 ACS 预热进本伙伴的 discovery 缓存（伙伴要直连它读写工作区）。

    为什么需要（2026-10-26 workspace e2e 实测）：伙伴的 `acps/discovered/` 是**空的**
    （只有 supervisor 跑 seed_discovery），于是 `runtime.aip_delegate(partner_aic=workspace)`
    解析不到端点，ws_* 工具全部失败在
    `AipError: Partner has no discovered Direct JSON-RPC endpoint`。

    这里只缓存 workspace 一个对端：它的 ACS 就在同机 `ws-workspace/acps/acs.final.json`，
    不需要网络也不需要模型先调 acps_discover。失败绝不阻塞伙伴启动（无 workspace 的
    部署形态下这是预期路径）。
    """
    ws_dir = Path(workspace)
    if ws_dir.name == "ws-workspace":
        return
    acs_path = ws_dir.parent / "ws-workspace" / "acps" / "acs.final.json"
    if not acs_path.is_file():
        return
    try:
        acs = json.loads(acs_path.read_text(encoding="utf-8"))
        from wit.acps.discovery import cache_discovered_agent, normalize_discovered_agent  # noqa: PLC0415

        summary = normalize_discovered_agent(acs, ranking=0)
        path = cache_discovered_agent(
            cache_dir=ws_dir / "acps" / "discovered", summary=summary, raw_acs=acs)
        if path is not None:
            print(f"[discovery] 已预热 workspace 对端（aic={acs.get('aic') or '?'}）", flush=True)
    except Exception as exc:  # noqa: BLE001  预热失败只影响伙伴直连工作区，不阻塞服务
        print(f"[discovery] workspace 对端预热失败（忽略）: {type(exc).__name__}: {exc}",
              flush=True)


async def serve_partner(
    build_agent: Callable[[object], WitAgent],
    *,
    workspace: Path,
    port_env: str,
    default_port: int,
    creds_prefix: str,
    runtime_cls: type[WitRuntime] = WitRuntime,
) -> None:
    """注册→(等审核)→领证书→mTLS 常驻服务；未就绪时以退出码 2 结束，交给 systemd 重试。

    runtime_cls 允许伙伴在 ACS 投影上做最小差异（workspace 的已批 ACS certificate
    为空，需要裁掉代码默认的 altNames），不影响证书请求本身。
    """
    import bootstrap  # noqa: PLC0415  (触发 .env 加载)

    port = int(os.getenv(port_env, str(default_port)))
    provider = bootstrap.build_provider()
    agent = build_agent(provider)
    runtime = runtime_cls(agent, workspace=workspace, acps=build_acps_config(port))
    try:
        result = await runtime.up_until_ready(
            serve=True,
            host=os.getenv("ACPS_SERVE_HOST", "127.0.0.1"),
            port=port,
            credentials=build_credentials(creds_prefix),
        )
        if not result.ready:
            print(f"[{agent.id}] 注册未就绪: {result.status} {result.next_action}")
            raise SystemExit(2)
        aic = result.identity.aic if result.identity else "?"
        print(f"[{agent.id}] 已上线 AIC={aic} endpoint=https://{_public_host()}:{port}/rpc")
        seed_workspace_peer(Path(workspace))
        await asyncio.sleep(int(os.getenv("ACPS_PARTNER_TTL", "864000")))
    finally:
        await provider.aclose()


# ── 伙伴入站任务的产物兜底提交 ──────────────────────────────────────────
# 为什么需要：伙伴的入站 run 里，能力结果要靠伙伴自己的模型调 `aip_submit` 才成为
# AIP 产物。模型跑完却没提交时，任务以 `Agent produced no AIP Product.` 收场——
# **沙箱已经真的跑成功、产物也在盘上，上游却什么都收不到**（2026-10-18 第 9 轮实测：
# run-091cea7de9 exit=0 且已写出 weights.json / predictions.csv，leader 仍报告"无产物"）。
_INBOUND_RESULT: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "yanshu_inbound_capability_result", default=None)


def set_inbound_result(payload: dict) -> None:
    """能力处理器在返回前登记自己的结果，供 run 收尾兜底成交付物。"""
    _INBOUND_RESULT.set(dict(payload or {}))


def _tool_name(event) -> str:
    tc = getattr(event, "tool_call", None)
    name = getattr(tc, "name", None) or getattr(tc, "tool", None) if tc is not None else None
    if not name:
        name = getattr(getattr(event, "tool", None), "name", None)
    return str(name or "")


def _response_ok(event) -> tuple[bool, bool]:
    """返回 (调用成功?, 任务进入 awaiting-input?)。"""
    resp = getattr(event, "response", None)
    r = resp if isinstance(resp, dict) else {}
    result = r.get("result") if isinstance(r.get("result"), dict) else {}
    status_ok = str(r.get("status")) == "success"
    awaiting = str(result.get("state") or "") == "awaiting-input"
    return status_ok, awaiting


class InboundProductFallbackHooks(WitHooks):
    """模型忘了交产物时，用登记过的能力结果补交一次（时序纪律照抄 supervisor 版）。

    协议只接受**一次**成功提交，且任务处于 awaiting-input 时代交会被拒
    （2026-10-16 现网实测：`AipError: This operation is not supported`），
    所以三个条件必须同时成立才补交：从未成功提交、未请求补充输入、结果不是 awaiting。
    """

    id = "yanshu.inbound-product-fallback"
    priority = 70

    _STATE: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
        "yanshu_inbound_submit_state", default=None)

    _SUBMIT_NAMES = ("aip_submit", "acps.aip.submit")
    _INPUT_NAMES = ("aip_request_input", "acps.aip.request_input")

    async def on_run_start(self, event) -> None:
        ctx = getattr(event, "context", None)
        # 真实 HookContext 没有 tools 字段：run 的工具集在 config.tools（RunConfig），
        # tool_manager 是编译期快照。历史实现读 ctx.tools/event.tools，两处恒为 None，
        # 导致兜底提交永远拿不到 aip_submit handler（2026-10-18 P1-1）。
        tools = getattr(getattr(ctx, "config", None), "tools", None) or []
        if not tools:
            manager = getattr(ctx, "tool_manager", None)
            lookup = getattr(manager, "lookup", None)
            if callable(lookup):
                tools = list(lookup().values())
            else:
                tools = getattr(manager, "tools", None) or []
        handler = fail_handler = None
        for tool in tools:
            name = str(getattr(tool, "name", "") or "")
            if name in self._SUBMIT_NAMES and handler is None:
                handler = getattr(tool, "handler", None)
            elif name == "aip_fail" and fail_handler is None:
                fail_handler = getattr(tool, "handler", None)
        _INBOUND_RESULT.set(None)
        self._STATE.set({"handler": handler, "fail_handler": fail_handler,
                         "called": False, "ok": False, "awaiting_input": False})

    async def on_tool_request(self, event) -> None:
        state = self._STATE.get()
        if isinstance(state, dict) and _tool_name(event) in self._SUBMIT_NAMES:
            state["called"] = True

    async def on_tool_response(self, event) -> None:
        state = self._STATE.get()
        if not isinstance(state, dict):
            return
        name = _tool_name(event)
        ok, awaiting = _response_ok(event)
        if name in self._SUBMIT_NAMES and ok:
            state["ok"] = True
        elif name in self._INPUT_NAMES and ok and awaiting:
            state["awaiting_input"] = True

    async def on_run_end(self, event) -> None:
        state, payload = self._STATE.get(), _INBOUND_RESULT.get()
        self._STATE.set(None)
        _INBOUND_RESULT.set(None)
        if not isinstance(state, dict) or state.get("ok") or state.get("awaiting_input"):
            return
        if not isinstance(payload, dict) or "awaiting" in payload:
            return                      # 缺件态交给调用方补料，不能当成交付物
        handler = state.get("handler")
        if not callable(handler):
            return
        text = json.dumps(payload, ensure_ascii=False, default=str)[:12000]
        # id 必填：AIP 产物缺 id 会被判 `Invalid AIP product`（2026-10-26 workspace e2e
        # 实测：兜底提交失败，任务停在 awaiting-completion 且拿不到产物）。
        import uuid  # noqa: PLC0415
        products = [{"id": f"fallback-{uuid.uuid4().hex[:12]}",
                     "name": "能力结果（代码兜底提交）",
                     "data_items": [{"type": "text", "text": text}]}]
        try:
            await handler({"products": products})
            print(f"[partner] 模型未提交产物，已用能力结果兜底成交付物（{len(text)} 字符）",
                  flush=True)
        except Exception as exc:  # noqa: BLE001  收尾绝不能毁掉 run 的返回值
            print(f"[partner] 兜底提交失败: {type(exc).__name__}: {exc}", flush=True)
