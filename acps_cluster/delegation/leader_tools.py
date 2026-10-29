"""leader 侧 run-scoped 工具挂载（PEP_design.md §5.2 的 remedy-3 接线）。

`DelegationLeaderHooks(WitHooks)` 在 `on_run_start` 里用 `HookContext.add_tool()` 挂两件本地工具：

1. `issue_delegation_token`：签发令牌，**只把 jti 句柄回给模型**（见 issuer_tool.py 的说明）；
2. `delegate_continue`：拿模型给的 `jti` + 等待中的 `task_ref`，**由代码侧**从 `TokenVault`
   取出 JWS、组装 `metadata.role == "delegation-token"` 的 dataItem，再调
   `runtime.aip_task(action="continue")` 发出去。**JWS 全程不过模型。**

为什么要走 WitHooks 而不是像 plan_gate 那样直接塞进 `RunConfig.tools`：
`aip_task`/`aip_delegate` 需要当次运行的 `AgentState`，而 `RunConfig.tools` 是在 `run()` 之前
构造的、拿不到 state。`on_run_start(event)` 的 `event.context` 同时给出 `runtime` 与 `state`，
且 `add_tool` 支持同名覆盖，天然是 per-run 挂载点。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from wit import DataPart, RunStartEvent, TextPart, Tool, WitHooks

from .client import delegation_data_item
from .issuer_tool import build_issue_token_tool
from .token import DEFAULT_TTL_SECONDS, TokenError, load_signing_context
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


def build_delegate_continue_tool(
    runtime: Any,
    state: Any,
    *,
    vault: TokenVault | None = None,
    tool_name: str = "delegate_continue",
) -> Tool:
    """把等待中的伙伴任务 continue 下去，并附上按 jti 取出的委托令牌（可一次附多枚）。"""
    store = vault if vault is not None else DEFAULT_TOKEN_VAULT

    async def delegate_continue(arguments: dict | str) -> dict[str, Any]:
        """向运行中的伙伴任务注入令牌并继续执行。"""
        args = arguments if isinstance(arguments, dict) else {"task_ref": str(arguments)}
        task_ref = str(args.get("task_ref") or "").strip()
        jtis = _as_str_list(args.get("jtis")) or _as_str_list(args.get("jti"))
        text = str(args.get("text") or "").strip()
        if not task_ref:
            return {"ok": False, "error": "缺少 task_ref（等待中的伙伴任务引用）"}
        if not jtis:
            return {"ok": False, "error": "缺少 jti（issue_delegation_token 返回的句柄）"}

        expected_delegate = str(args.get("delegate_aic") or "").strip()
        held_list = []
        for jti in jtis:
            held = store.peek(jti)
            if held is None:
                return {
                    "ok": False,
                    "error": (
                        f"jti {jti} 无效或已过期/已使用（句柄一次性）。"
                        "若确需重发，请重新调用 issue_delegation_token 签发。"
                    ),
                }
            if expected_delegate and expected_delegate != held.delegate_aic:
                return {
                    "ok": False,
                    "error": f"delegate_aic 与句柄不符：句柄受托方是 {held.delegate_aic}，收到 {expected_delegate}",
                }
            held_list.append(held)

        input_parts: list[Any] = [
            TextPart(
                text
                or (
                    "你的缺口可由随信的委托令牌覆盖：请持令牌直接向你需要的目标伙伴发起任务取回材料"
                    "（令牌原文已由协议层保存、你的模型看不到），不要回传给 leader。"
                )
            )
        ]
        delegated_to: list[str] = []
        for held in held_list:
            store.resolve(held.jti)
            payload = delegation_data_item(held.token, target_aic=held.target_aic, skill_id=held.skill_id)
            input_parts.append(DataPart(data=dict(payload["data"]), metadata=dict(payload["metadata"])))
            delegated_to.append(held.target_aic)
        try:
            result = await runtime.aip_task(
                state, action="continue", task_ref=task_ref, input=input_parts
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": f"continue 下发失败: {type(exc).__name__}: {exc}",
                "hint": "这些 jti 已消费；请重新调用 issue_delegation_token 签发后再发。",
            }
        snapshot = result if isinstance(result, dict) else {}
        task_state = str(snapshot.get("state") or "")
        # 不抛异常 ≠ 已下发：jti 在 try 之前就被 resolve 烧掉，若伙伴侧任务是失败态，
        # 一律报 ok=True 会让 leader 以为委托已送出而继续等产物（审查修复）
        failed = task_state in {"failed", "rejected", "canceled", "cancelled", "expired"}
        return {
            "ok": not failed,
            "action": "continue",
            "taskRef": task_ref,
            "state": task_state,
            "summary": str(snapshot.get("summary") or ""),
            "count": len(held_list),
            "delegatedTo": delegated_to,
            **({"error": f"continue 后任务态为 {task_state}；这 {len(held_list)} 枚 jti 已消费，"
                         "需重新签发才能再发"} if failed else {}),
        }

    return Tool(
        name=tool_name,
        description=(
            "把已签发的委托令牌下发给一个正在等待输入的伙伴：参数 task_ref（等待中的伙伴任务引用）、"
            "jti（issue_delegation_token 返回的句柄，可传数组一次附多枚）、text（给伙伴的说明）、"
            "delegate_aic（可选，与句柄交叉校验）。令牌原文由代码侧按 jti 取出并注入，"
            "**你永远看不到也不需要看到令牌文本**。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_ref": {"type": "string", "description": "等待中的伙伴任务引用"},
                "jti": {"type": "string", "description": "issue_delegation_token 返回的句柄"},
                "jtis": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "句柄数组（与 jti 二选一；一次附多枚令牌）",
                },
                "text": {"type": "string", "description": "给等待伙伴的说明（令牌会由代码侧附加）"},
                "delegate_aic": {"type": "string", "description": "可选：受托伙伴 AIC，用于交叉校验"},
            },
            "required": ["task_ref"],
        },
        handler=delegate_continue,
    )


class DelegationLeaderHooks(WitHooks):
    """把签发工具与 continue 工具挂到 leader 的每一次 run 上（无需改 ACS）。"""

    id = "yanshu.delegation-leader"
    priority = 80

    def __init__(
        self,
        *,
        workspace: Path | str | None = None,
        trust_bundle: Path | str | None = None,
        signing: Any | None = None,
        vault: TokenVault | None = None,
        default_ttl_seconds: int = DEFAULT_TTL_SECONDS,
        mount_issuer: bool = True,
        mount_continue: bool = True,
    ) -> None:
        self._workspace = Path(workspace) if workspace is not None else None
        self._trust_bundle = Path(trust_bundle) if trust_bundle is not None else None
        self._signing = signing
        self._vault = vault if vault is not None else DEFAULT_TOKEN_VAULT
        self._default_ttl_seconds = default_ttl_seconds
        self._mount_issuer = mount_issuer
        self._mount_continue = mount_continue

    @property
    def vault(self) -> TokenVault:
        return self._vault

    def _signing_context(self) -> Any:
        if self._signing is None:
            if self._workspace is None:
                raise ValueError("DelegationLeaderHooks 需要 signing 或 workspace 之一")
            self._signing = load_signing_context(
                self._workspace / "acps" / "client",
                trust_bundle=self._trust_bundle or (self._workspace / "acps" / "trust-bundle.pem"),
            )
        return self._signing

    async def on_run_start(self, event: RunStartEvent) -> None:
        """run 开始回调：初始化本次运行的钩子状态。"""
        ctx = event.context
        if self._mount_issuer:
            try:
                signing = self._signing_context()
            except (OSError, TokenError, ValueError) as exc:
                print(
                    f"[delegation-leader] 未挂载 issue_delegation_token（本机签名上下文不可用: "
                    f"{type(exc).__name__}: {exc}）；remedy-3 将退化为 leader 中转",
                    flush=True,
                )
            else:
                trust_bundle = self._trust_bundle
                if trust_bundle is None and self._workspace is not None:
                    trust_bundle = self._workspace / "acps" / "trust-bundle.pem"
                ctx.add_tool(
                    build_issue_token_tool(
                        signing,
                        trust_bundle=trust_bundle,
                        vault=self._vault,
                        default_ttl_seconds=self._default_ttl_seconds,
                    ),
                    source="delegation",
                )
        if self._mount_continue:
            ctx.add_tool(
                build_delegate_continue_tool(ctx.runtime, ctx.state, vault=self._vault),
                source="delegation",
            )


__all__ = ["DelegationLeaderHooks", "build_delegate_continue_tool"]
