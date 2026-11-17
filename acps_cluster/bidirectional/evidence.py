"""双向证据包：把两个方向的回执合成一份可复核的材料。

判定口径（写死在代码里，避免"材料上说得比证据多"）：

- **PASS**：两个方向各有一条平台任务，且都是 `completed`，AIC 对得上、taskId 不同、
  双方都有产物、没有本地错误；
- **INCOMPLETE**：其余情况。`awaiting-completion` 如实记为"已交付未关单"，不算 PASS；
  缺方向、状态没到终态、AIC 对不上都逐条列出原因。

证据包不含 payload 正文与任何凭据，只含：taskId、双方 AIC、方向、终态、时间、
产物数量/字符数/sha256、状态时间线。产物正文由两侧各自留在平台任务里。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import (
    SCHEMA_PACK,
    TaskReceipt,
    direction_label,
)

JSON_NAME = "bidirectional-evidence.json"
MARKDOWN_NAME = "bidirectional-evidence.md"
SUMS_NAME = "SHA256SUMS"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _check(check_id: str, title: str, ok: bool, detail: str, *, required: bool = True) -> dict[str, Any]:
    return {"id": check_id, "title": title, "ok": bool(ok), "required": bool(required),
            "detail": detail}


def run_checks(
    outbound: TaskReceipt | None,
    inbound: TaskReceipt | None,
    *,
    self_aic: str,
    peer_aic: str,
    mirror: Mapping[str, TaskReceipt | None] | None = None,
) -> list[dict[str, Any]]:
    """逐条自检；顺序即证据包的展示顺序。"""
    checks: list[dict[str, Any]] = []
    checks.append(_check(
        "two-directions",
        "两个方向都有任务",
        outbound is not None and inbound is not None,
        f"出站={outbound.task_ref if outbound else '缺失'}；入站={inbound.task_ref if inbound else '缺失'}",
    ))

    def _state(receipt: TaskReceipt | None) -> str:
        return receipt.state if receipt else "缺失"

    both_completed = bool(outbound and inbound and outbound.ok and inbound.ok)
    checks.append(_check(
        "both-completed",
        "两个方向终态都是 completed",
        both_completed,
        f"出站={_state(outbound)}；入站={_state(inbound)}"
        + ("" if both_completed else "（awaiting-completion=已交付未关单，不计 PASS）"),
    ))

    symmetric = bool(
        outbound and inbound
        and outbound.caller_aic and inbound.callee_aic
        and outbound.callee_aic and inbound.caller_aic
        and outbound.caller_aic == inbound.callee_aic
        and outbound.callee_aic == inbound.caller_aic
    )
    detail = (
        f"出站 {outbound.caller_aic or '?'} → {outbound.callee_aic or '?'}；"
        f"入站 {inbound.caller_aic or '?'} → {inbound.callee_aic or '?'}"
        if outbound and inbound else "缺一条回执，无法比对"
    )
    checks.append(_check("aic-symmetry", "双方 AIC 在两个方向上互为对照", symmetric, detail))

    peer_matches = bool(
        outbound and inbound
        and outbound.callee_aic == peer_aic
        and inbound.caller_aic == peer_aic
    )
    checks.append(_check(
        "peer-aic",
        "对端 AIC 与本次验证对象一致",
        peer_matches,
        f"期望对端={peer_aic or '?'}；出站 callee={outbound.callee_aic if outbound else '?'}；"
        f"入站 caller={inbound.caller_aic if inbound else '?'}",
    ))

    distinct = bool(
        outbound and inbound and outbound.task_ref and inbound.task_ref
        and outbound.task_ref != inbound.task_ref
    )
    checks.append(_check(
        "distinct-task-ids",
        "两个方向是两条独立的平台任务",
        distinct,
        f"出站={outbound.task_ref if outbound else '?'}；入站={inbound.task_ref if inbound else '?'}",
    ))

    stamped = bool(
        outbound and inbound and outbound.created_at and inbound.created_at
    )
    checks.append(_check(
        "timestamps",
        "两个方向都有任务创建时间",
        stamped,
        f"出站={outbound.created_at if outbound else '?'}；入站={inbound.created_at if inbound else '?'}",
    ))

    has_products = bool(outbound and inbound and not outbound.products.empty and not inbound.products.empty)
    checks.append(_check(
        "products",
        "两个方向都有产物",
        has_products,
        f"出站 {outbound.products.count if outbound else 0} 个/"
        f"{outbound.products.chars if outbound else 0} 字符；"
        f"入站 {inbound.products.count if inbound else 0} 个/"
        f"{inbound.products.chars if inbound else 0} 字符",
    ))

    clean = bool(outbound and inbound and not outbound.local_error.strip()
                 and not inbound.local_error.strip())
    checks.append(_check(
        "no-local-error",
        "两个方向都没有本地错误记录",
        clean,
        f"出站={outbound.local_error or '无' if outbound else '?'}；"
        f"入站={inbound.local_error or '无' if inbound else '?'}",
    ))

    checks.append(_check(
        "self-aic",
        "出站调用方就是本机身份",
        bool(outbound and self_aic and outbound.caller_aic == self_aic),
        f"期望本机={self_aic or '?'}；出站 caller={outbound.caller_aic if outbound else '?'}",
    ))

    if mirror:
        out_mirror = mirror.get("outbound")
        in_mirror = mirror.get("inbound")
        parts: list[str] = []
        ok = True
        if out_mirror is not None:
            same_task = bool(outbound and out_mirror.task_ref == outbound.task_ref)
            # caller/callee 是绝对身份（不随"哪个库"变化）：两侧必须**一致**
            same_roles = bool(
                outbound
                and out_mirror.caller_aic == outbound.caller_aic
                and out_mirror.callee_aic == outbound.callee_aic
            )
            ok = ok and same_task and same_roles
            parts.append(
                f"出站 mirror: taskId={'一致' if same_task else '不一致'}"
                f"（{out_mirror.task_ref}）、caller/callee={'一致' if same_roles else '不一致'}"
            )
        if in_mirror is not None:
            same_task = bool(inbound and in_mirror.task_ref == inbound.task_ref)
            same_roles = bool(
                inbound
                and in_mirror.caller_aic == inbound.caller_aic
                and in_mirror.callee_aic == inbound.callee_aic
            )
            ok = ok and same_task and same_roles
            parts.append(
                f"入站 mirror: taskId={'一致' if same_task else '不一致'}"
                f"（{in_mirror.task_ref}）、caller/callee={'一致' if same_roles else '不一致'}"
            )
        checked = bool(parts)
        checks.append(_check(
            "peer-ledger",
            "对端任务库的同一条任务与本地互相对账",
            ok if checked else False,
            "；".join(parts) if checked else "给了对端工作区，但没找到对应 taskId 的记录",
            required=False,
        ))
    return checks


def build_pack(
    outbound: TaskReceipt | None,
    inbound: TaskReceipt | None,
    *,
    self_aic: str,
    peer_aic: str,
    self_label: str = "",
    peer_label: str = "",
    workspace: str = "",
    ledger_db: str = "",
    notes: Iterable[str] = (),
    mirror: Mapping[str, TaskReceipt | None] | None = None,
    mirror_workspace: str = "",
) -> dict[str, Any]:
    """组装证据包（纯数据，不落盘）。`verdict` 只由 required 检查决定。"""
    checks = run_checks(
        outbound, inbound, self_aic=self_aic, peer_aic=peer_aic, mirror=mirror,
    )
    required_ok = all(item["ok"] for item in checks if item["required"])
    return {
        "schema": SCHEMA_PACK,
        "generated_at": _now_iso(),
        "verdict": "pass" if required_ok else "incomplete",
        "self": {"label": self_label, "aic": self_aic, "workspace": workspace},
        "peer": {"label": peer_label, "aic": peer_aic, "workspace": mirror_workspace},
        "ledger": {"db": ledger_db},
        "directions": {
            "outbound": outbound.as_dict() if outbound else None,
            "inbound": inbound.as_dict() if inbound else None,
        },
        "mirror": {
            "workspace": mirror_workspace,
            "outbound": mirror["outbound"].as_dict() if mirror and mirror.get("outbound") else None,
            "inbound": mirror["inbound"].as_dict() if mirror and mirror.get("inbound") else None,
        },
        "checks": checks,
        "notes": [str(note) for note in notes],
    }


def _cell(value: Any) -> str:
    text = str(value if value is not None else "")
    return text.replace("|", "\\|").replace("\n", " ") or "—"


def _receipt_section(receipt: TaskReceipt | None, direction: str) -> list[str]:
    if receipt is None:
        return [
            f"## {direction_label(direction)}",
            "",
            "> 未采到该方向的任务回执。",
            "",
        ]
    lines = [
        f"## {direction_label(direction)}",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 平台 taskId | `{_cell(receipt.task_ref)}` |",
        f"| 方向 | `{_cell(receipt.direction)}` |",
        f"| 调用方 AIC（caller） | `{_cell(receipt.caller_aic)}` |",
        f"| 被调方 AIC（callee） | `{_cell(receipt.callee_aic)}` |",
        f"| 终态 | `{_cell(receipt.state)}` |",
        f"| 创建 / 更新 | {_cell(receipt.created_at)} / {_cell(receipt.updated_at)} |",
        f"| 会话 session | `{_cell(receipt.session_id)}` |",
        f"| 产物 | {receipt.products.count} 个 / {receipt.products.chars} 字符 / "
        f"sha256 `{_cell(receipt.products.sha256[:16])}…` |",
        f"| 本地错误 | {_cell(receipt.local_error)} |",
        "",
    ]
    if receipt.events:
        lines += ["状态时间线：", "", "| 时间 | 状态 |", "|---|---|"]
        lines += [f"| {_cell(event.get('at'))} | `{_cell(event.get('state'))}` |"
                  for event in receipt.events]
        lines.append("")
    if receipt.notes:
        lines += [f"> {note}" for note in receipt.notes] + [""]
    return lines


def _mirror_section(pack: Mapping[str, Any]) -> list[str]:
    mirror = pack.get("mirror") or {}
    rows: list[str] = []
    for key in ("outbound", "inbound"):
        item = mirror.get(key)
        if not item:
            continue
        receipt = TaskReceipt.from_dict(item)
        rows.append(
            f"| 对端视角 `{_cell(receipt.direction)}` | `{_cell(receipt.task_ref)}` | "
            f"`{_cell(receipt.caller_aic)}` → `{_cell(receipt.callee_aic)}` | "
            f"`{_cell(receipt.state)}` |"
        )
    if not rows:
        return []
    return [
        "## 对端任务库对账（同一 taskId 在两侧都留痕）",
        "",
        f"对端工作区：`{_cell(mirror.get('workspace'))}`",
        "",
        "| 视角 | taskId | caller → callee | 终态 |",
        "|---|---|---|---|",
        *rows,
        "",
    ]


def render_markdown(pack: Mapping[str, Any]) -> str:
    """把证据包渲染成可直接贴进佐证材料 / 答辩附录的 Markdown。"""
    self_info = pack.get("self") or {}
    peer_info = pack.get("peer") or {}
    directions = pack.get("directions") or {}
    outbound = TaskReceipt.from_dict(directions["outbound"]) if directions.get("outbound") else None
    inbound = TaskReceipt.from_dict(directions["inbound"]) if directions.get("inbound") else None
    verdict = str(pack.get("verdict") or "").upper() or "INCOMPLETE"

    lines = [
        "# 跨智能体双向调用证据包",
        "",
        f"- 生成时间：{_cell(pack.get('generated_at'))}",
        f"- 本机身份：{_cell(self_info.get('label'))}（AIC `{_cell(self_info.get('aic'))}`）",
        f"- 对端身份：{_cell(peer_info.get('label'))}（AIC `{_cell(peer_info.get('aic'))}`）",
        f"- 判定：**{verdict}**",
        "",
        "> 判定口径：两个方向各有一条平台 AIP 任务、终态均为 `completed`、双方 AIC 互相对上、"
        "taskId 不同、双方都有产物。`awaiting-completion` 只算已交付未关单，不计 PASS。",
        "",
    ]
    lines += _receipt_section(outbound, "outbound")
    lines += _receipt_section(inbound, "inbound")
    lines += _mirror_section(pack)

    lines += ["## 自检", "", "| 检查 | 结果 | 说明 |", "|---|---|---|"]
    for item in pack.get("checks") or []:
        mark = "✅" if item.get("ok") else ("❌" if item.get("required", True) else "⚠️")
        lines.append(f"| {_cell(item.get('title'))} | {mark} | {_cell(item.get('detail'))} |")
    lines.append("")

    ledger = (pack.get("ledger") or {}).get("db") or ""
    lines += [
        "## 复核入口",
        "",
        f"- 本机任务库（只读）：`{_cell(ledger)}`",
        "  ```bash",
        f'  sqlite3 "file:{ledger or "acps/aip-tasks.sqlite3"}?mode=ro" \\',
        '    "SELECT task_ref,direction,state,local_aic,remote_aic,created_at '
        'FROM aip_tasks ORDER BY created_at DESC LIMIT 20;"',
        "  ```",
        "- 平台后台：按 taskId 在「智能体访问量 / 互联交互记录」检索并截图（截图需人工完成，"
        "本证据包不含平台后台数据）",
        "- 对端侧回执：由对端用同一份工具 `pack` 生成，或提供平台后台同 taskId 的截图",
        "",
    ]
    notes = pack.get("notes") or []
    if notes:
        lines += ["## 备注", ""] + [f"- {note}" for note in notes] + [""]
    return "\n".join(lines).rstrip() + "\n"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_pack(
    pack: Mapping[str, Any],
    out_dir: str | Path,
    *,
    write_markdown: bool = True,
) -> dict[str, Path]:
    """落盘：JSON + Markdown + `SHA256SUMS`（与 `sha256sum -c` 兼容）。"""
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / JSON_NAME
    json_path.write_text(
        json.dumps(pack, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    written = {"json": json_path}
    if write_markdown:
        md_path = directory / MARKDOWN_NAME
        md_path.write_text(render_markdown(pack), encoding="utf-8")
        written["markdown"] = md_path
    sums_path = directory / SUMS_NAME
    lines = [f"{_sha256(path)}  {path.name}" for path in written.values()]
    sums_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    written["sums"] = sums_path
    return written


def is_pass(pack: Mapping[str, Any]) -> bool:
    return str(pack.get("verdict") or "") == "pass"


__all__ = [
    "JSON_NAME",
    "MARKDOWN_NAME",
    "SUMS_NAME",
    "build_pack",
    "is_pass",
    "render_markdown",
    "run_checks",
    "write_pack",
]