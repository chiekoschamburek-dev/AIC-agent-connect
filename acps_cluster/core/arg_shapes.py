"""工具入参的数组宽容化：模型提供方有时把 array 参数序列化成 JSON 字符串。

起因（2026-10-16 生产）：现网 MiniMax-M3.1-Flash-Preview 在一次会话里把 `plan_gate` 的
`needed_capabilities` / `steps` 送成了非数组，`cluster_roster.validate_plan` 的严格
`isinstance(x, list)` 连报两次"必须是非空数组"，leader 一步都走不动；同一会话里
`acps_delegate` 的 `request` 数组形态也在 wit 投影层被拒（那条我们改不了，已另行上报）。

规则刻意保守：**只**把"能 `json.loads` 成 list 的字符串"还原成 list，其余原样返回，
让各校验函数照常给出自己的拒绝文案。宽容的是**形态**，不是内容——不把 `"随便写点啥"`
当成一条记录，也不把 `{...}` 对象串当数组。
"""

from __future__ import annotations

import json
from typing import Any


def as_list(value: Any) -> Any:
    """array 形态的参数值：已是 list 就原样返回；JSON 字符串数组还原成 list；其余不动。"""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = json.loads(text)
            except ValueError:
                return value
            if isinstance(parsed, list):
                return parsed
    return value
