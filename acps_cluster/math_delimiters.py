"""交付前数学定界符规范化：把 LaTeX 写法换成 Markdown 渲染器认得的形式。

## 问题（2026-10-11 实测）

模型产出用的是 LaTeX 定界符：`\\[ ... \\]`（display）与 `\\( ... \\)`（inline）。
而**主流 Markdown 渲染器（marked / markdown-it / GitHub）默认只认 `$...$` 与 `$$...$$`**，
不认反斜杠写法 → 平台上显示成：

    \\[

    p_\\theta(x_{t-1}\\mid x_t)

    =

    \\mathcal{N}
    ...

即把 `\\[` 当普通文字、并按换行拆开，看起来像"渲染坏了"。

实测：正文里 `\\[`/`\\]` 每篇 40–115 对、`\\(`/`\\)` 14–24 对，
而 `\\[...\\]` 内部**并没有空行**——所以问题**纯在定界符**，不是内容。

## 做法

- `\\[ ... \\]` → `$$ ... $$`
- `\\( ... \\)` → `$ ... $`（inline 两侧去空白，避免 `$ x $` 这种不解析的形式）
- **跳过代码块与行内代码**：` ``` ` 围栏内、以及 `` `...` `` 里一律不动
  （代码里出现 `\\[` 可能是正则或转义，改了会破坏语义）
- 幂等：已规范化的文本再跑一次不变

用法：
    from math_delimiters import normalize_math_delimiters
    text = normalize_math_delimiters(text)
"""

from __future__ import annotations

import re

_FENCE = re.compile(r"^(\s*)(`{3,}|~{3,})")
_INLINE_CODE = re.compile(r"`[^`\n]*`")


def _split_code_segments(text: str) -> list[tuple[bool, str]]:
    """把文本切成 (是否代码, 片段)。围栏代码块与行内代码都算代码。"""
    out: list[tuple[bool, str]] = []
    lines = text.splitlines(keepends=True)
    buf: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0

    def flush(is_code: bool) -> None:
        if buf:
            out.append((is_code, "".join(buf)))
            buf.clear()

    for ln in lines:
        match = _FENCE.match(ln)
        if in_fence:
            buf.append(ln)
            if (
                match
                and match.group(2)[0] == fence_char
                and len(match.group(2)) >= fence_len
            ):
                flush(True)
                in_fence = False
            continue
        if match:
            flush(False)
            marker = match.group(2)
            in_fence = True
            fence_char, fence_len = marker[0], len(marker)
            buf.append(ln)          # 围栏行本身归入代码段
            continue
        buf.append(ln)
    flush(in_fence)

    # 再按行内代码细分非代码段
    result: list[tuple[bool, str]] = []
    for is_code, seg in out:
        if is_code:
            result.append((True, seg))
            continue
        pos = 0
        for m in _INLINE_CODE.finditer(seg):
            if m.start() > pos:
                result.append((False, seg[pos:m.start()]))
            result.append((True, m.group(0)))
            pos = m.end()
        if pos < len(seg):
            result.append((False, seg[pos:]))
    return result


def _convert(seg: str) -> str:
    # display：\[ ... \] → $$ ... $$
    seg = re.sub(r"\\\[(.+?)\\\]", lambda m: "$$" + m.group(1).strip() + "$$", seg, flags=re.S)
    # inline：\( ... \) → $ ... $
    seg = re.sub(r"\\\((.+?)\\\)", lambda m: "$" + m.group(1).strip() + "$", seg, flags=re.S)
    return seg


def normalize_math_delimiters(text: str) -> str:
    """把 LaTeX 定界符换成 Markdown 渲染器认得的 `$`/`$$`，代码段不动。"""
    if not text:
        return text
    parts = _split_code_segments(text)
    return "".join(seg if is_code else _convert(seg) for is_code, seg in parts)


def math_delimiter_stats(text: str) -> dict[str, int]:
    """统计定界符数量（排错与验收用）。"""
    return {
        "bracket_display_open": len(re.findall(r"\\\[", text)),
        "bracket_display_close": len(re.findall(r"\\\]", text)),
        "paren_inline_open": len(re.findall(r"\\\(", text)),
        "paren_inline_close": len(re.findall(r"\\\)", text)),
        "dollar_double": len(re.findall(r"\$\$", text)),
        "dollar_single": len(re.findall(r"(?<!\$)\$(?!\$)", text)),
    }
