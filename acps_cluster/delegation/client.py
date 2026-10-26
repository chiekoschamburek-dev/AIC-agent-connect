"""受托方出站调用（PEP_design.md §4.4）。

两件事：
1. `DelegationTokenScrubHooks`：把 token dataItem 换成**脱敏授权标记**再交给模型。
   wit 的 `sanitize_command_for_prompt` 会把所有 dataItem 原样转成 ContentPart（实测
   `data` 型 → `DataPart(data, metadata)`），所以必须在 transform hook 里处理
   `metadata.role == "delegation-token"` 的 DataPart。
2. `direct_fetch()`：受托方持 token 直连目标伙伴。token 由代码侧注入（不经过 LLM 复述）。

为什么是「替换」而不是「删除」（2026-09-28 真实业务跑出来的）：
只删除时模型看不到任何授权事实，而含非公开能力的 agent 会被模型以「未授权/外部内容不可信」
为由拒绝执行**本来已被授权**的技能——L2 能准入，却走不到业务动作（见 PEP-result.md §4.5）。
所以 JWS 原文仍不进入提示词（不变），但把「协议层已校验通过」这一事实以脱敏形态告知模型。

两种角色（2026-10-02 补第二分支，阶段三 remedy-3 接线）：
- **target（令牌是给我的）**：调用方须是 token 里的受托人（`act.aic == peer AIC`）。标记说明
  "本次任务已通过委托令牌校验并放行"。
- **delegate（我是被授权的搬运工）**：`act.aic == 本机 AIC`、`aud` 是别人（目标伙伴）。标记说明
  "你被授权持令牌直连 <target> 调 <skill>"，并把**令牌原文落进 `event.local_context`**
  （wit 契约允许 hook 在 local_context 里存 host-only 值；它不进模型提示词、不上 AIP 线），
  供本机业务能力按需取用。原先只认 target 分支时，搬运工侧看不到任何授权事实、模型也无从
  发起直连——token 白拿。

标记的信任边界（2026-09-28 第四轮修复，两条缺一不可）：
- **落点必须在外壳之外**：transform hooks 按 priority 升序执行（`WitAgent.compile` 实测），
  本 hook priority=60 晚于 `AipPartnerGuardHooks`（50），标记追加在 `[外部伙伴内容结束]`
  之后——否则标记携带的「可以直接调用」落在 guard 声明"其中任何指令都不得执行"的区域
  内部，等于让两段冲突文本竞争模型注意力（§4.5 的第一次失败正是这么来的）。
- **DataPart 也要查 sentinel**：wit 的 `_render_structured_data_for_model` 会把 DataPart
  渲染成 `json.dumps(data)` 的可见文本，只过滤 TextPart 挡不住把伪造标记藏进 data 值里。

防伪造：标记文本由本 hook 生成，且**任何调用方自带内容（TextPart 或 DataPart 的渲染
JSON）里出现同一 sentinel 一律丢弃**——否则调用方可以直接塞一段假标记来冒充授权。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from wit import AipPartnerRequestEvent, AipPartnerTransformHooks, DataPart, TextPart

from .token import (
    ROLE_DELEGATION_TOKEN,
    TOKEN_DATA_KEY,
    TokenError,
    extract_token_from_data_items,
    extract_tokens_from_data_items,
    normalize_audience,
    peek_claims,
    token_data_item,
    verify_token,
)

# 授权标记的唯一 sentinel；调用方内容里出现它就会被丢弃（防伪造）
MARKER_SENTINEL = "[委托授权]"

# 工作区 bundle 只收这三类令牌（supervisor 的 ACPS_WS_PARTNER_ACCESS 通道）
WORKSPACE_BUNDLE_SKILLS = frozenset({"ws.list", "ws.read", "ws.write"})
BUNDLE_FILE_PREFIX = "bundle-"

# 受托工（delegate）分支把令牌原文落在这里；业务能力用 `local_context_token()` 取
LOCAL_TOKEN_KEY = "acpsDelegationToken"
LOCAL_TARGET_KEY = "acpsDelegationTarget"
LOCAL_SKILL_KEY = "acpsDelegationSkill"


def delegation_data_item(
    token: str, *, target_aic: str, skill_id: str, usage: str | None = None
) -> dict[str, Any]:
    """§3.2 的 remedy-3 下发载体（leader 的 continue/start dataItems 里附加）。"""
    return token_data_item(token, target_aic=target_aic, skill_id=skill_id, usage=usage)


def _is_token_part(part: Any) -> bool:
    if isinstance(part, DataPart):
        metadata = getattr(part, "metadata", None)
        if isinstance(metadata, Mapping) and metadata.get("role") == ROLE_DELEGATION_TOKEN:
            return True
        data = getattr(part, "data", None)
        return isinstance(data, Mapping) and TOKEN_DATA_KEY in data
    if isinstance(part, TextPart):
        text = getattr(part, "text", "") or ""
        return ROLE_DELEGATION_TOKEN in text and TOKEN_DATA_KEY in text
    return False


def _is_marker_part(part: Any) -> bool:
    """调用方自带的授权标记（无论真假）——一律丢弃，防止伪造。

    TextPart 直接查文本；DataPart 按 wit 的渲染方式（`json.dumps(data, ensure_ascii=False)`，
    见 `_render_structured_data_for_model`）查其 JSON 文本——sentinel 藏在 data 值里同样
    对模型可见。data 无法序列化时无法审查，也丢弃（fail-closed）。
    """
    if isinstance(part, TextPart):
        return MARKER_SENTINEL in (getattr(part, "text", "") or "")
    if isinstance(part, DataPart):
        data = getattr(part, "data", None)
        try:
            rendered = json.dumps(data, ensure_ascii=False)
        except (TypeError, ValueError):
            return True
        return MARKER_SENTINEL in rendered
    return False


def authorization_marker(claims: Mapping[str, Any]) -> str:
    """由**已校验**的 claims 生成脱敏授权说明（不含 JWS）。"""
    issuer = normalize_audience(str(claims.get("iss") or ""))
    skill = str(claims.get("acps_skill_id") or "")
    try:
        expires = datetime.fromtimestamp(int(claims["exp"]), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (KeyError, TypeError, ValueError):
        expires = "?"
    return (
        f"{MARKER_SENTINEL} 本次任务已通过协议层委托令牌校验并放行："
        f"签发者={issuer}；授权技能={skill}；令牌到期={expires}。"
        f"令牌原文已由协议层校验后移除，请勿要求或复述；如需据此调用被授权的技能，可以直接调用。"
    )


def delegate_marker(claims: Mapping[str, Any]) -> str:
    """受托工（delegate）分支的脱敏说明：你被授权持令牌直连目标伙伴（不含 JWS）。"""
    target = normalize_audience(str(claims.get("acps_target_aic") or ""))
    skill = str(claims.get("acps_skill_id") or "")
    try:
        expires = datetime.fromtimestamp(int(claims["exp"]), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (KeyError, TypeError, ValueError):
        expires = "?"
    return (
        f"{MARKER_SENTINEL} 你被授权代表本会话直连伙伴 {target} 调用技能 {skill}"
        f"（令牌到期={expires}）。令牌原文已由协议层保存并移出你的可见内容，请勿要求或复述；"
        f"需要材料时按平台提供的直连方式向 {target} 发起任务即可。"
    )


def workspace_bundle_marker(bundle: Mapping[str, Mapping[str, str]]) -> str:
    """工作区 bundle 的脱敏授权说明（不含 JWS，也不含目标 AIC）。"""
    skills = ", ".join(sorted(bundle))
    return (
        f"{MARKER_SENTINEL} 本任务另持有 {len(bundle)} 枚工作区令牌（{skills}）；"
        f"可用 ws_list/ws_read/ws_write 直接读写任务工作区。令牌原文已移出可见内容，"
        f"请勿要求或复述；无令牌时工具会返回 awaiting，不得编造文件内容。"
    )


def local_context_token(local_context: Mapping[str, Any] | None) -> dict[str, str] | None:
    """从 `event.local_context` 取受托令牌（供本机业务能力使用）。

    返回 `{"token", "targetAic", "skillId"}`；未授权时 None。token 只在此处交给**本机代码**，
    不会进入模型提示词。
    """
    if not isinstance(local_context, Mapping):
        return None
    token = local_context.get(LOCAL_TOKEN_KEY)
    if not isinstance(token, str) or not token:
        return None
    return {
        "token": token,
        "targetAic": str(local_context.get(LOCAL_TARGET_KEY) or ""),
        "skillId": str(local_context.get(LOCAL_SKILL_KEY) or ""),
    }


class DelegationTokenScrubHooks(AipPartnerTransformHooks):
    """把 token dataItem 换成脱敏授权标记（JWS 原文绝不进入模型可见内容）。

    `cfg` 为 None（或 PEP 总开关关闭）时退化为纯删除（不含授权事实），保留给没有
    PEP 配置的调用方——admission 未运行时不给「已放行」的表述。

    priority=60：必须晚于 `AipPartnerGuardHooks`（50）执行，标记才落在「不可信外部
    内容」外壳之外（wit 的 transform hooks 按 priority 升序执行，见模块说明）。
    """

    id = "yanshu.delegation-token-scrub"
    priority = 60

    def __init__(self, cfg: Any | None = None) -> None:
        self._cfg = cfg

    def _role_for_token(
        self, token: str, event: AipPartnerRequestEvent, cfg: Any | None
    ) -> tuple[str, Mapping[str, Any]] | None:
        """返回 (角色, claims)：target=令牌给我的；delegate=我是被授权的搬运工。否则 None。"""
        if cfg is None or cfg.trust_bundle is None or not token:
            return None
        try:
            # 先取 aud 才能驱动 verify_token 的 aud 闸门；权威的 audience 校验在目标伙伴的
            # PEP（见 token.peek_claims 的说明），这里不做也不该做 aud 归属判断。
            expected_aud = normalize_audience(str(peek_claims(token).get("aud") or ""))
            claims = verify_token(token, trust_bundle=cfg.trust_bundle, expected_aud=expected_aud)
        except (TokenError, OSError):  # trust bundle 读不到时不影响请求处理
            return None
        if normalize_audience(str(claims.get("iss") or "")) not in cfg.leader_aics:
            return None
        act = claims.get("act")
        act_aic = normalize_audience(str(act.get("aic") or "")) if isinstance(act, Mapping) else ""
        my_aic = normalize_audience(str(getattr(cfg, "my_aic", "") or ""))
        audience = normalize_audience(str(claims.get("aud") or ""))
        if audience == my_aic:
            # 令牌是给我的：协议层已校验调用方是受托人（act.aic == 对端 AIC，主体级绑定）
            peer = getattr(event, "leader_aic", None)
            if not act_aic or not peer or act_aic != str(peer):
                return None
            return ("target", claims)
        if act_aic and act_aic == my_aic:
            # 我是被授权的受托人，令牌的目标是别人：我是搬运工，要把原文留给本机代码。
            # 要求令牌由**其签发者本人**送达（否则任何社区成员都能把别人的令牌塞给我）。
            issuer = normalize_audience(str(claims.get("iss") or ""))
            peer = normalize_audience(str(getattr(event, "leader_aic", None) or ""))
            if not peer or peer != issuer:
                return None
            return ("delegate", claims)
        return None

    @staticmethod
    def _primary_of(verified: list[tuple[str, Mapping[str, Any], str]]):
        """选本任务的主令牌：先 target（非 ws），再任意 target，最后非 ws 的 delegate。

        ws.* 令牌只进工作区 bundle，绝不成为 `current_delegation()`——否则会覆写
        experiment→gpu 等既有出站链路的凭据（2026-10-26 多令牌改造）。
        """
        for role, claims, token in verified:
            if role == "target" and str(claims.get("acps_skill_id") or "") not in WORKSPACE_BUNDLE_SKILLS:
                return role, claims, token
        for role, claims, token in verified:
            if role == "target":
                return role, claims, token
        for role, claims, token in verified:
            if role == "delegate" and str(claims.get("acps_skill_id") or "") not in WORKSPACE_BUNDLE_SKILLS:
                return role, claims, token
        return None

    @staticmethod
    def _workspace_bundle_of(verified: list[tuple[str, Mapping[str, Any], str]]) -> dict:
        """把本任务收到的 ws.* 令牌按 skillId 收成 bundle（同 skill 取第一枚）。"""
        bundle: dict[str, dict] = {}
        for _role, claims, token in verified:
            skill = str(claims.get("acps_skill_id") or "")
            if skill not in WORKSPACE_BUNDLE_SKILLS or skill in bundle:
                continue
            try:
                exp = int(claims.get("exp") or 0)
            except (TypeError, ValueError):
                exp = 0
            bundle[skill] = {
                "token": token,
                "targetAic": normalize_audience(
                    str(claims.get("acps_target_aic") or claims.get("aud") or "")),
                "skillId": skill,
                "taskId": str(claims.get("task_id") or ""),
                "exp": exp,
            }
        return bundle

    async def on_partner_request(self, event: AipPartnerRequestEvent) -> None:
        from .bridge import set_delegation, set_delegation_bundle

        # 每个 inbound 请求从"无令牌"起算：不在受托分支就复位。此前只有拿到令牌时才 set，
        # 若 wit 复用同一上下文处理下一个请求，上一个任务的 bearer 令牌会被继承（跨租户越权，
        # 与 kb 的 peer 复位同一纪律；2026-10-07 审查修复）
        set_delegation(None)
        # bundle 文件**不**随每个请求删除：remedy-3/awaiting-input 的 continue 不带
        # bundle，删掉会让任务中途失去工作区访问。ContextVar 每次复位，客户端回退读文件。
        set_delegation_bundle(None)
        task_id = str(getattr(event, "task_id", "") or "") or None
        session_id = str(getattr(event, "session_id", "") or "") or None
        set_current_task(task_id, session_id)
        cfg = self._cfg if getattr(self._cfg, "enabled", False) else None
        try:
            clear_task_token(_token_dir_for(cfg), task_id)
        except OSError:
            pass
        if not event.content:
            return
        tokens = extract_tokens_from_data_items(getattr(event, "data_items", ()) or ())
        verified: list[tuple[str, Mapping[str, Any], str]] = []
        for token in tokens:
            role = self._role_for_token(token, event, cfg)
            if role is not None:
                verified.append((role[0], role[1], token))
        kept = [
            part
            for part in event.content
            if not _is_token_part(part) and not _is_marker_part(part)
        ]
        primary = self._primary_of(verified)
        if primary is not None:
            role, claims, token = primary
            skill_id = str(claims.get("acps_skill_id") or "")
            target_aic = normalize_audience(
                str(claims.get("acps_target_aic")
                    or (claims.get("aud") if role == "target" else "") or ""))
            local_context = getattr(event, "local_context", None)
            if role == "delegate" and isinstance(local_context, dict):
                local_context[LOCAL_TOKEN_KEY] = token
                local_context[LOCAL_TARGET_KEY] = target_aic
                local_context[LOCAL_SKILL_KEY] = skill_id
            # 桥接（阶段三收尾）：业务 run 的能力处理器经 bridge.delegated_fetch()
            # 一行出站——transform 与 run 同链，ContextVar 先设后建即贯通
            set_delegation({"token": token, "targetAic": target_aic, "skillId": skill_id})
            # host 侧令牌文件（跨 run/跨线程可靠通道；host-only，不进任何模型上下文）：
            # capability 侧经 read_token_file() 读取——ContextVar 在跨 run 场景不可靠
            try:
                _write_token_file(token, target_aic, skill_id,
                                  directory=_token_dir_for(cfg),
                                  exp=claims.get("exp"),
                                  delegation_id=str(claims.get("acps_delegation_id") or ""),
                                  task_id=task_id, session_id=session_id)
            except OSError:
                pass
            kept.append(TextPart(delegate_marker(claims) if role == "delegate"
                                 else authorization_marker(claims)))
        bundle = self._workspace_bundle_of(verified)
        if bundle:
            set_delegation_bundle(bundle)
            try:
                write_bundle_file(_token_dir_for(cfg), task_id, bundle)
            except OSError:
                pass
            kept.append(TextPart(workspace_bundle_marker(bundle)))
        if len(kept) != len(event.content) or verified:
            event.content[:] = kept


def strip_delegation_data_items(data_items: Iterable[Any]) -> list[Any]:
    """从 dataItems 里剔除 token 项（给不走 transform hook 的代码路径用）。"""
    return [
        item
        for item in data_items
        if not (
            isinstance(item, Mapping)
            and isinstance(item.get("metadata"), Mapping)
            and item["metadata"].get("role") == ROLE_DELEGATION_TOKEN
        )
    ]


async def direct_fetch(
    runtime: Any,
    state: Any,
    *,
    target_aic: str,
    request: str,
    token: str,
    skill_id: str,
) -> dict[str, Any]:
    """受托方持 token 直连目标伙伴（复用 wit 的 `runtime.aip_delegate`）。

    `DataPart` 会被 wit 转成 StructuredDataItem 并带 metadata，目标伙伴的 PEP 据此取 token。
    token 只出现在这里（代码侧）；对端的模型只会看到脱敏授权标记，拿不到 JWS 原文。
    """
    payload = delegation_data_item(token, target_aic=target_aic, skill_id=skill_id)
    return await runtime.aip_delegate(
        state,
        partner_aic=target_aic,
        request=[
            TextPart(request),
            DataPart(data=dict(payload["data"]), metadata=dict(payload["metadata"])),
        ],
    )


def trust_bundle_of(workspace: Path | str) -> Path:
    return Path(workspace) / "acps" / "trust-bundle.pem"


def client_directory_of(workspace: Path | str) -> Path:
    return Path(workspace) / "acps" / "client"


__all__ = [
    "BUNDLE_FILE_PREFIX",
    "LOCAL_SKILL_KEY",
    "LOCAL_TARGET_KEY",
    "LOCAL_TOKEN_KEY",
    "MARKER_SENTINEL",
    "WORKSPACE_BUNDLE_SKILLS",
    "DelegationTokenScrubHooks",
    "authorization_marker",
    "client_directory_of",
    "delegate_marker",
    "delegation_data_item",
    "direct_fetch",
    "local_context_token",
    "read_bundle_file",
    "strip_delegation_data_items",
    "trust_bundle_of",
    "workspace_bundle_marker",
    "write_bundle_file",
]


# ─────────── host 侧令牌文件（跨 run/跨线程可靠通道） ───────────

_TOKEN_FILE_DIR = Path(os.getenv("ACPS_TOKEN_FILE_DIR", "")) if os.getenv("ACPS_TOKEN_FILE_DIR") else None


def _token_dir_for(cfg: Any | None) -> Path:
    """cfg.workspace（PepConfig）→ <workspace>/tokens；无 cfg 用环境/默认。"""
    ws = getattr(cfg, "workspace", None) if cfg is not None else None
    if ws:
        return Path(ws) / "tokens"
    return _token_dir()


def _token_dir() -> Path:
    """令牌文件目录：环境变量优先；缺省 ws-*/tokens 推断失败时用 cwd/tokens。"""
    if _TOKEN_FILE_DIR is not None and str(_TOKEN_FILE_DIR):
        return _TOKEN_FILE_DIR
    return Path(os.getenv("ACPS_TOKEN_DIR_DEFAULT", "tokens"))


_CURRENT_TASK: ContextVar[tuple[str | None, str | None]] = ContextVar(
    "yanshu_current_inbound_task", default=(None, None)
)


def set_current_task(task_id: str | None, session_id: str | None = None) -> None:
    _CURRENT_TASK.set((str(task_id or "") or None, str(session_id or "") or None))


def current_task_id() -> str | None:
    return _CURRENT_TASK.get()[0]


def current_session_id() -> str | None:
    """本任务所属会话（与 current_task_id 同源；未在 AIP 任务上下文时为 None）。"""
    return _CURRENT_TASK.get()[1]


def _task_token_path(directory: Path, task_id: str) -> Path:
    digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:24]
    return directory / f"task-{digest}.json"


def clear_task_token(directory: Path | str | None, task_id: str | None) -> None:
    if not task_id:
        return
    d = Path(directory) if directory else _token_dir()
    _task_token_path(d, str(task_id)).unlink(missing_ok=True)


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


def _write_token_file(token: str, target_aic: str, skill_id: str, *,
                      directory: Path | None = None, exp: int | None = None,
                      delegation_id: str | None = None,
                      task_id: str | None = None,
                      session_id: str | None = None) -> Path:
    """写令牌文件：任务级槽 + latest 兼容槽，读取侧按 taskId 校验。"""
    d = Path(directory) if directory else _token_dir()
    d.mkdir(parents=True, exist_ok=True)
    payload = {"token": token, "targetAic": target_aic, "skillId": skill_id,
               "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "exp": int(exp) if exp else None,
               "delegationId": delegation_id or "",
               "taskId": task_id or None,
               "sessionId": session_id or None}
    payload = {k: v for k, v in payload.items() if v is not None}
    text = json.dumps(payload, ensure_ascii=False, indent=1)
    path = _task_token_path(d, task_id) if task_id else d / "latest.json"
    _write_atomic(path, text)
    if task_id:
        _write_atomic(d / "latest.json", text)
    return path


def read_token_file(directory: Path | str | None = None, *, now: float | None = None,
                    consume: bool = True, task_id: str | None = None,
                    session_id: str | None = None) -> dict[str, str] | None:
    """capability 侧读取 host 令牌文件(默认一次性:读到即删;过期即拒)。

    task_id 给定时只读该任务的槽，**绝不回退 latest**——否则任务 B 会读到任务 A
    留下的 bearer 令牌（2026-10-18 P1-13）。未给 task_id 的旧调用方仍读 latest。

    consume=False：只读不删——准入侧 peek 专用（skill_scope 只确认本任务是否
    被令牌授权了某 skill）。
    """
    import time as _time

    d = Path(directory) if directory else _token_dir()
    path = _task_token_path(d, task_id) if task_id else d / "latest.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if task_id is None and data.get("taskId"):
        # 没有任务上下文时不得消费任务级令牌（latest 只是旧格式兼容槽）。
        return None
    if task_id and str(data.get("taskId") or "") != str(task_id):
        if consume:
            path.unlink(missing_ok=True)
        return None
    if session_id and data.get("sessionId") and str(data["sessionId"]) != str(session_id):
        if consume:
            path.unlink(missing_ok=True)
        return None
    if consume:
        path.unlink(missing_ok=True)  # 一次性消费(读后即删,防跨任务复用)
    token = data.get("token")
    if not isinstance(token, str) or not token:
        return None
    exp = data.get("exp")
    now_ts = now if now is not None else _time.time()
    if isinstance(exp, (int, float)) and now_ts > float(exp):
        return None  # 过期即拒(调用方按无令牌走 awaiting/降级,不编造)
    return {"token": token, "targetAic": str(data.get("targetAic") or ""),
            "skillId": str(data.get("skillId") or "")}


# ─────────── 工作区 token bundle（按 skillId 索引的 host 侧通道） ───────────


def _bundle_path(directory: Path, task_id: str) -> Path:
    digest = hashlib.sha256(str(task_id).encode("utf-8")).hexdigest()[:24]
    return directory / f"{BUNDLE_FILE_PREFIX}{digest}.json"


def write_bundle_file(directory: Path | str | None, task_id: str | None,
                      entries: Mapping[str, Mapping[str, Any]]) -> Path | None:
    """把 bundle 落成 host-only 文件（0600）。task_id 缺失时不落盘（只用 ContextVar）。"""
    if not task_id or not entries:
        return None
    d = Path(directory) if directory else _token_dir()
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "taskId": str(task_id),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tokens": {str(k): dict(v) for k, v in entries.items()},
    }
    path = _bundle_path(d, str(task_id))
    _write_atomic(path, json.dumps(payload, ensure_ascii=False, indent=1))
    return path


def read_bundle_file(directory: Path | str | None = None, *, task_id: str | None = None,
                     now: float | None = None) -> dict[str, dict[str, str]]:
    """读取本任务的 bundle（只读不删；过期条目剔除）。

    与 `read_token_file` 的一次性语义不同：bundle 会被多次 ws 调用复用，
    因此不能消费。task_id 必填——绝不回退 latest，避免跨任务读到别人的工作区授权。
    """
    if not task_id:
        return {}
    d = Path(directory) if directory else _token_dir()
    path = _bundle_path(d, str(task_id))
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or str(data.get("taskId") or "") != str(task_id):
        return {}
    raw = data.get("tokens")
    if not isinstance(raw, dict):
        return {}
    now_ts = now if now is not None else time.time()
    out: dict[str, dict[str, str]] = {}
    for skill, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        token = str(entry.get("token") or "")
        if not token:
            continue
        try:
            exp = int(entry.get("exp") or 0)
        except (TypeError, ValueError):
            exp = 0
        if exp and now_ts > exp:
            continue
        out[str(skill)] = {
            "token": token,
            "targetAic": str(entry.get("targetAic") or ""),
            "skillId": str(entry.get("skillId") or skill),
            "taskId": str(entry.get("taskId") or ""),
            "exp": exp,
        }
    return out
