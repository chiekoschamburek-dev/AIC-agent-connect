"""partner 侧工作区客户端：用 supervisor 随任务下发的 ws.* 令牌读写任务工作区。

设计边界（2026-10-26 handoff）：
- 令牌由 supervisor 以 `act=partner, aud=workspace, task_id=<scope>` 签发，随委派
  DataItem 下发；partner 的 `DelegationTokenScrubHooks` 把 ws.* 令牌收进 bundle
  （ContextVar + host 文件），本模块只从 bundle 取——JWS 绝不进模型上下文。
- task_id 固定来自令牌 claim，模型的工具签名里**没有** task_id（改不了作用域）。
- 不 import `partner_workspace` 做本地文件 I/O；一律经 workspace service 的
  ws.list/ws.read/ws.write 协议调用（与 supervisor 委派同一条路）。
- 无令牌/无 runtime/过期 → 结构化 awaiting，不抛异常、不编造文件内容。

模型可见工具名：ws_list / ws_read / ws_write（capability id 为 ws.*，`acps_skill=False`，
不进 ACS；`metadata.internal_tool` 让 SkillScopedToolsHooks 不把它当非公开能力移除）。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Mapping

from wit import WitHooks, capability

from delegation.bridge import (
    current_delegation_bundle,
    current_runtime,
    current_state,
)
from delegation.client import current_task_id, direct_fetch, read_bundle_file

WORKSPACE_ACCESS_NOTE = (
    "本任务若携带工作区令牌（由 supervisor 随任务下发），可用 ws_list/ws_read/ws_write "
    "直接读写任务工作区；这些工具不需要 task_id（作用域由令牌固定）。无令牌时返回 awaiting，"
    "不得编造文件内容；不要用通用委派工具把任务转派给其他伙伴。"
)

_TERMINAL = {"completed", "failed", "rejected", "canceled", "cancelled", "expired"}
_WAIT_STATES = ("awaiting-completion", "awaiting-input")

DEFAULT_TIMEOUT = float(os.getenv("ACPS_WS_CLIENT_TIMEOUT", "180"))
DEFAULT_MAX_WRITE = int(os.getenv("ACPS_WS_CLIENT_MAX_WRITE_BYTES", "262144"))
DEFAULT_MAX_READ = int(os.getenv("ACPS_WS_CLIENT_MAX_READ_BYTES", "262144"))
MIN_STEPS = int(os.getenv("ACPS_WS_PARTNER_MIN_STEPS", "16"))


def _awaiting(reason: str, *, how: str = "") -> dict:
    return {
        "ok": False,
        "awaiting": {
            "need": "workspace.delegation",
            "reason": reason,
            "how": how or ("由 supervisor 先 ws.create/ws.clone_repo 打开任务工作区，"
                           "再委派本任务（代码会自动附工作区令牌）"),
            "blocking": True,
        },
    }


def _bundle(token_dir: Path | str) -> dict[str, dict[str, str]]:
    bundle = current_delegation_bundle()
    if bundle:
        return bundle
    return read_bundle_file(token_dir, task_id=current_task_id())


def _entry(token_dir: Path | str, skill: str) -> tuple[dict[str, str] | None, dict | None]:
    bundle = _bundle(token_dir)
    if not bundle:
        return None, _awaiting("本任务未携带工作区令牌")
    entry = bundle.get(skill)
    if not entry:
        return None, _awaiting(f"本任务的工作区令牌不含 {skill}",
                               how="supervisor 需在 ACPS_WS_PARTNER_ACCESS 中开放该能力并重新委派")
    try:
        exp = int(entry.get("exp") or 0)
    except (TypeError, ValueError):
        exp = 0
    if exp and time.time() > exp + 30:
        return None, _awaiting("工作区令牌已过期，请 supervisor 重新委派")
    if not entry.get("token") or not entry.get("targetAic"):
        return None, _awaiting("工作区令牌缺少 token/targetAic")
    return entry, None


def _request_text(skill: str, task_id: str, payload: Mapping[str, Any]) -> str:
    return (
        f"调用 {skill}：task_id={task_id}。参数（JSON）："
        f"{json.dumps(dict(payload), ensure_ascii=False)}\n"
        f"请原样调用 {skill} 并返回能力的 JSON 结果；task_id 必须原样使用，不要改写。"
    )


def _products_text(products: Any) -> str:
    chunks: list[str] = []
    for product in products or ():
        if not isinstance(product, Mapping):
            continue
        for key in ("data_items", "dataItems"):
            for item in product.get(key) or ():
                if isinstance(item, Mapping) and item.get("type") == "text":
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        chunks.append(text)
    return "\n".join(chunks)


def _json_or_text(text: str) -> Any:
    """优先解析成 JSON 对象；解析不出就原样返回文本（workspace 模型可能写说明）。"""
    blob = str(text or "").strip()
    if not blob:
        return ""
    if blob.startswith("```"):
        try:
            blob = blob.split("```", 2)[1]
            blob = blob.split("\n", 1)[1] if "\n" in blob else blob
        except (IndexError, ValueError):
            pass
    decoder = json.JSONDecoder()
    for idx, char in enumerate(blob):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(blob[idx:])
        except ValueError:
            continue
        if isinstance(value, (dict, list)):
            return value
    return text


async def _wait(runtime: Any, state: Any, snapshot: Mapping[str, Any], *,
                timeout: float) -> dict:
    ref = str(snapshot.get("task_ref") or snapshot.get("taskRef") or "")
    current = dict(snapshot or {})
    if not ref:
        return current
    deadline = time.monotonic() + max(5.0, timeout)
    while True:
        products = current.get("products") or []
        state_name = str(current.get("state") or "")
        if products or state_name in _TERMINAL or state_name in _WAIT_STATES:
            break
        if time.monotonic() >= deadline:
            current["timedOut"] = True
            return current
        await asyncio.sleep(3)
        # wit 的 aip_task 只接受 refresh/continue/complete/cancel；`get` 会抛
        # `AipError: Unsupported task action`，导致三个 ws 工具全部失败
        # （2026-10-26 服务器 e2e 实测）。取当前状态用 `refresh`。
        current = dict(await runtime.aip_task(state, action="refresh", task_ref=ref) or {})
    if str(current.get("state") or "") == "awaiting-completion":
        # AIP 规定 awaiting-completion 必须由调用方 complete 才能取到产物
        current = dict(await runtime.aip_task(state, action="complete", task_ref=ref) or {})
    return current


async def _call(token_dir: Path | str, skill: str, payload: Mapping[str, Any], *,
                timeout: float = DEFAULT_TIMEOUT) -> dict:
    entry, awaiting = _entry(token_dir, skill)
    if entry is None:
        return awaiting or _awaiting("工作区令牌不可用")
    runtime, state = current_runtime(), current_state()
    if runtime is None or state is None:
        return {"ok": False, "error": "bridge-not-ready",
                "reason": "本 run 未挂 BridgeHooks（无法发起 workspace 委派）"}
    task_id = str(entry.get("taskId") or "").strip()
    if not task_id:
        return {"ok": False, "error": "token-scope-missing",
                "reason": "工作区令牌未携带 task_id，无法确定任务作用域"}
    try:
        snapshot = await direct_fetch(
            runtime, state,
            target_aic=str(entry["targetAic"]),
            request=_request_text(skill, task_id, payload),
            token=str(entry["token"]),
            skill_id=skill,
        )
    except Exception as exc:  # noqa: BLE001  网络/协议错误收敛为结构化结果
        return {"ok": False, "error": "workspace-call-failed",
                "reason": f"{type(exc).__name__}: {exc}"[:300]}
    if str((snapshot or {}).get("state") or "") == "rejected":
        return {"ok": False, "error": "workspace-rejected",
                "reason": str((snapshot or {}).get("summary") or "")[:400],
                "taskRef": (snapshot or {}).get("task_ref")}
    final = await _wait(runtime, state, snapshot or {}, timeout=timeout)
    text = _products_text(final.get("products"))
    if final.get("timedOut"):
        return {"ok": False, "error": "workspace-timeout",
                "taskRef": final.get("task_ref") or final.get("taskRef"),
                "reason": f"等待 workspace 任务超过 {int(timeout)}s"}
    state_name = str(final.get("state") or "")
    parsed = _json_or_text(text)
    if state_name in _TERMINAL - {"completed"}:
        return {"ok": False, "error": "workspace-task-failed", "state": state_name,
                "result": parsed, "summary": str(final.get("summary") or "")[:400]}
    summary = str(final.get("summary") or "").strip()
    return {
        "ok": True,
        "skill": skill,
        "taskId": task_id,
        "state": state_name,
        "result": parsed,
        **({"summary": summary[:400]} if summary and not text else {}),
    }


async def ws_list(token_dir: Path | str, *, path: str = "", depth: int = 2) -> dict:
    """列任务工作区目录树（模型工具 ws_list 的实现；不需要 task_id）。"""
    try:
        depth_i = int(depth)
    except (TypeError, ValueError):
        depth_i = 2
    return await _call(token_dir, "ws.list", {"path": str(path or ""), "depth": depth_i})


async def ws_read(token_dir: Path | str, *, path: str, max_bytes: int = 0) -> dict:
    """读任务工作区文件（模型工具 ws_read 的实现；默认 64KB，上限受 workspace 约束）。"""
    try:
        wanted = int(max_bytes or 0)
    except (TypeError, ValueError):
        wanted = 0
    if wanted > DEFAULT_MAX_READ:
        wanted = DEFAULT_MAX_READ
    return await _call(token_dir, "ws.read", {"path": str(path or ""), "max_bytes": wanted})


async def ws_write(token_dir: Path | str, *, path: str, content: str,
                   encoding: str = "text", if_match: str = "") -> dict:
    """写任务工作区（模型工具 ws_write 的实现；repo/ 只读由服务端强制）。"""
    body = str(content or "")
    if len(body.encode("utf-8")) > DEFAULT_MAX_WRITE:
        return {"ok": False, "error": "content-too-large",
                "reason": f"单次写入上限 {DEFAULT_MAX_WRITE} 字节；大文件请由伙伴代码分块写入"}
    return await _call(token_dir, "ws.write", {
        "path": str(path or ""),
        "content": body,
        "encoding": str(encoding or "text"),
        "if_match": str(if_match or ""),
    })


class WorkspaceAccessBudgetHooks(WitHooks):
    """带工作区 bundle 的入站任务抬升步数预算（默认 8 步不够 ws 调用 + 交付）。"""

    id = "yanshu.workspace-access-budget"
    priority = 95

    def __init__(self, token_dir: Path | str) -> None:
        self.token_dir = Path(token_dir)

    async def on_run_start(self, event) -> None:  # noqa: ANN001
        bundle = current_delegation_bundle() or read_bundle_file(
            self.token_dir, task_id=current_task_id())
        if not bundle:
            return
        ctx = getattr(event, "context", None)
        cfg = getattr(ctx, "config", None)
        if cfg is None:
            return
        try:
            cur = int(getattr(cfg, "max_steps", 0) or 0)
        except (TypeError, ValueError):
            cur = 0
        if cur < MIN_STEPS:
            try:
                object.__setattr__(cfg, "max_steps", MIN_STEPS)
            except Exception:  # noqa: BLE001  抬不动不阻断
                pass


def build_workspace_access_capabilities(token_dir: Path | str) -> list:
    """装配 ws_list/ws_read/ws_write 三个模型工具（不进 ACS）。"""
    root = Path(token_dir)

    @capability(
        id="ws.list",
        description=(
            "列本任务工作区的目录树（≤200 项，depth≤8）。需要本任务携带工作区令牌；"
            "无令牌返回 awaiting。适合先看 repo/<仓库名>/ 有哪些文件，再按需 ws_read。"
        ),
        public=False,
        acps_skill=False,
        metadata={"internal_tool": True},
        tags=["workspace", "internal"],
    )
    async def ws_list_tool(path: str = "", depth: int = 2) -> dict:
        return await ws_list(root, path=path, depth=depth)

    @capability(
        id="ws.read",
        description=(
            "读本任务工作区文件（默认 ≤64KB，超限截断）。需要本任务携带工作区令牌；"
            "无令牌返回 awaiting。repo/ 下是克隆仓库，scratch/<你>/ 是自己的草稿，"
            "shared/ 是协作区，products/ 是归档区。"
        ),
        public=False,
        acps_skill=False,
        metadata={"internal_tool": True},
        tags=["workspace", "internal"],
    )
    async def ws_read_tool(path: str, max_bytes: int = 0) -> dict:
        return await ws_read(root, path=path, max_bytes=max_bytes)

    @capability(
        id="ws.write",
        description=(
            "写本任务工作区（repo/ 只读；products/ 服务端版本化；scratch/ 强制 <actor>/ 前缀；"
            "shared/ 支持 if_match 乐观锁）。需要本任务携带工作区令牌；无令牌返回 awaiting。"
            f"单次内容上限 {DEFAULT_MAX_WRITE} 字节。"
        ),
        public=False,
        acps_skill=False,
        metadata={"internal_tool": True},
        tags=["workspace", "internal"],
    )
    async def ws_write_tool(path: str, content: str, encoding: str = "text",
                            if_match: str = "") -> dict:
        return await ws_write(root, path=path, content=content,
                              encoding=encoding, if_match=if_match)

    return [ws_list_tool, ws_read_tool, ws_write_tool]
