"""从 AIP 任务入参文本里解析 `(skillId, args)`（纯标准库，无 wit 依赖）。

AIP v2 的 `TaskCommand` 没有一等公民 `skillId` 字段，平台调用方只能把它放进
`commandParams`（或 dataItem 的 metadata / 结构化文本里）。运行时把整条命令渲染成
一段文本交给 provider，于是"技能点名"要靠这里解析：

- 优先 `<payload>{json}</payload>` 段（wit 的渲染外壳）；
- 其次整段文本就是 JSON；
- `commandParams.skillId` / `skill`、`dataItems[].metadata.skillId`、
  `dataItems[].text` 里的 JSON 体依次兜底；
- 参数取 `args` / `arguments`；没有则用文本 JSON 体本身。

同一份解析同时给两处用：

- `acps_cluster.static_router`：资源 agent 的确定性路由（gpu.run / kb.query …）；
- `acps_cluster.bidirectional.skill_entry`：supervisor 入站技能快速通道
  （`research.orchestrate` 不跑 LLM）。

返回值永远是 `(str, dict)`，解析不出返回 `("", {})`——调用方据此决定"确定性执行"
还是"回落 LLM"，不靠异常。
"""

from __future__ import annotations

import json
import re
from typing import Any

_PAYLOAD_RE = re.compile(r"<payload>(.*?)</payload>", re.S)

#: 与 `delegation.pep._SKILL_HINT_KEYS` 保持同一组键（解析口径一致）
SKILL_HINT_KEYS = ("skillId", "skill_id", "skill", "targetSkillHint")


def _parse_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _skill_of(source: Any) -> str:
    if not isinstance(source, dict):
        return ""
    for key in SKILL_HINT_KEYS:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def unwrap_payload(text: str) -> str:
    """取 `<payload>…</payload>` 里的 JSON 文本；没有外壳则返回原文（去空白）。"""
    raw = str(text or "")
    match = _PAYLOAD_RE.search(raw)
    return match.group(1).strip() if match else raw.strip()


def _first_json_object(text: str) -> dict[str, Any] | None:
    """从任意包裹文本里找第一个**配对完整**的 JSON 对象（跳过字符串里的花括号）。

    为什么需要：入站请求可能被外层包成 `[外部伙伴内容开始] … {json} …`（安全外壳、
    编排话术），整段 `json.loads` 必失败；但技能点名就在里面那段 JSON 里。
    """
    raw = str(text or "")
    start = raw.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(raw)):
            char = raw[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    parsed = _parse_json(raw[start:index + 1])
                    if isinstance(parsed, dict):
                        return parsed
                    break
        start = raw.find("{", start + 1)
    return None


def parse_skill_call(text: Any) -> tuple[str, dict[str, Any]]:
    """从入参文本解析 `(skillId, args)`；解析不出返回 `("", {})`。"""
    raw = str(text or "")
    payload: dict[str, Any] = {}
    candidates: list[dict[str, Any]] = []
    for match in _PAYLOAD_RE.finditer(raw):
        parsed = _parse_json(match.group(1))
        if isinstance(parsed, dict):
            candidates.append(parsed)
    # 取**最后一个**带 skill 的 payload：AIP 命令外壳在前、业务内容在后；
    # 取第一个会把历史/注入内容里的技能名误当成当前调用。
    for candidate in reversed(candidates):
        params = candidate.get("commandParams") if isinstance(candidate.get("commandParams"), dict) else {}
        if _skill_of(params) or _skill_of(candidate):
            payload = candidate
            break
    if not payload and candidates:
        payload = candidates[-1]
    if not payload:
        parsed = _parse_json(raw.strip())
        if isinstance(parsed, dict):
            payload = parsed
    if not payload:
        found = _first_json_object(raw)
        if found:
            payload = found

    params = payload.get("commandParams") if isinstance(payload.get("commandParams"), dict) else {}
    skill = _skill_of(params)
    if not skill:
        # 约定形态：任务正文本身就是 `{"skill": "...", "args": {...}}`
        # （探针与"技能指名"调用方都这么发，见 docs/bidirectional-invocation.md）
        skill = _skill_of(payload)
    args = params.get("args") if "args" in params else params.get("arguments")

    data_items = payload.get("dataItems")
    if isinstance(data_items, list):
        for item in data_items:
            if not isinstance(item, dict):
                continue
            meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            if not skill:
                skill = _skill_of(meta)
            if item.get("type") == "text":
                body = _parse_json(item.get("text"))
                if isinstance(body, dict):
                    if not skill:
                        skill = _skill_of(body)
                    if args is None:
                        args = body.get("args") if "args" in body else body

    if args is None:
        args = payload.get("args") if isinstance(payload.get("args"), dict) else None
        if args is None and skill and _skill_of(payload):
            # Earlier callers sent a flat object alongside the skill name.
            args = {key: value for key, value in payload.items() if key not in SKILL_HINT_KEYS}
    if args is None and not payload:
        # 没有信封、整段就是参数对象（`{"query": "..."}`）
        parsed = _parse_json(unwrap_payload(raw))
        if isinstance(parsed, dict):
            args = parsed
    if not isinstance(args, dict):
        args = {}
    return skill, args


__all__ = ["SKILL_HINT_KEYS", "parse_skill_call", "unwrap_payload"]
