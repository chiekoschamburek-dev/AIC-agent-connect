"""数学定界符规范化单测（真实报文回归）。

依据（2026-10-11 实测）：正文里 `\\[...\\]` 每篇 40–115 对、`\\(...\\)` 14–24 对，
而 `\\[...\\]` 内部没有空行 → 渲染问题纯由"平台渲染器不认反斜杠定界符"造成。
"""

from __future__ import annotations

import sys
from pathlib import Path

CL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CL))
sys.path.insert(0, str(CL.parent))

from acps_cluster.core.math_delimiters import (  # noqa: E402
    math_delimiter_stats,
    normalize_math_delimiters,
)

# 用户报告的真实片段（源文本本身无空行；看到的空行是渲染器拆开的观感）
REAL = r"""前向过程由下式给出：

\[
q(x_t\mid x_{t-1})
=\mathcal N\!\left(\sqrt{1-\beta_t}x_{t-1},\beta_t I\right),
\qquad
\bar\alpha_t=\prod_{s=1}^{t}(1-\beta_s),
\]

反向过程则为 \(p_\theta(x_{t-1}\mid x_t)=\mathcal{N}\left(\mu_\theta(x_t,t),\sigma_t^2 I\right)\)。
"""

BS_OPEN = "\\["
BS_CLOSE = "\\]"
PAREN_OPEN = "\\("
PAREN_CLOSE = "\\)"
NEWLINE = "\n"


def main() -> int:
    failures: list[str] = []

    out = normalize_math_delimiters(REAL)
    st = math_delimiter_stats(out)
    print(f"  [1] 规范化后统计: {st}")
    if st["bracket_display_open"] or st["bracket_display_close"]:
        failures.append("[1] 仍有反斜杠方括号残留")
    if st["paren_inline_open"] or st["paren_inline_close"]:
        failures.append("[1] 仍有反斜杠圆括号残留")
    if st["dollar_double"] != 2:
        failures.append(f"[1] $$ 数量 {st['dollar_double']}（期望 2）")
    if st["dollar_single"] < 2:
        failures.append(f"[1] 行内 $ 数量 {st['dollar_single']}（期望 ≥2）")

    # [2] 公式内容不得被改动
    probe = "q(x_t\\mid x_{t-1})"
    print(f"  [2] 公式内容保持: {probe in REAL and probe in out}")
    if not (probe in REAL and probe in out):
        failures.append("[2] 公式内容被改动")

    # [3] $$ 必须紧贴公式。判定用**邻接字符**，不要用 "$$\n" 这种子串搜索——
    #     块级公式的闭 $$ 后面本来就该换段、开 $$ 前面本来就该有段落分隔，
    #     子串搜索会把这两种正常情况误判为错误（我在这里错过两次）。
    pairs = [i for i in range(len(out)) if out.startswith("$$", i)]
    ok3 = True
    print(f"  [3] $$ 出现 {len(pairs)} 次（应为 2：一开一闭）")
    for idx, i in enumerate(pairs):
        before = out[i - 1] if i else ""
        after = out[i + 2] if i + 2 < len(out) else ""
        role = "开" if idx % 2 == 0 else "闭"
        # 开定界符：后面必须紧跟公式首字符（非空白）；闭定界符：前面必须是公式末字符（非换行）
        if role == "开" and (after in ("", NEWLINE, " ", "\t")):
            ok3 = False
        if role == "闭" and (before in ("", NEWLINE, " ", "\t")):
            ok3 = False
        print(f"      {role} $$ @ {i}: 前={before!r} 后={after!r}")
    print(f"      邻接检查通过: {ok3}")
    if not ok3:
        failures.append("[3] $$ 与公式之间存在空白")
    if len(pairs) != 2:
        failures.append(f"[3] $$ 数量 {len(pairs)}（期望 2）")

    # [4] 幂等
    again = normalize_math_delimiters(out)
    print(f"  [4] 幂等: {again == out}")
    if again != out:
        failures.append("[4] 非幂等")

    # [5] 围栏代码块内不得改（正则/转义里出现 \[ 是合法的）
    code = "```python\npattern = r\"\\[a-z\\]\"\n```\n\n公式：\\[a=b\\]\n"
    out5 = normalize_math_delimiters(code)
    code_kept = 'r"\\[a-z\\]"' in out5
    outside_done = "$$a=b$$" in out5
    print(f"  [5] 围栏代码块保持: {code_kept}   代码块外已转换: {outside_done}")
    if not code_kept:
        failures.append("[5] 围栏代码块内的反斜杠方括号被误改")
    if not outside_done:
        failures.append("[5] 代码块外的公式未被转换")

    # [6] 行内代码里的不得改，真实行内公式要转
    inline = "用 `" + PAREN_OPEN + "x" + PAREN_CLOSE + "` 表示写法，真公式是 " + \
             PAREN_OPEN + "y" + PAREN_CLOSE + "。"
    out6 = normalize_math_delimiters(inline)
    inline_kept = ("`" + PAREN_OPEN + "x" + PAREN_CLOSE + "`") in out6
    inline_done = "$y$" in out6
    print(f"  [6] 行内代码保持: {inline_kept}   真实行内公式已转换: {inline_done}")
    if not inline_kept:
        failures.append("[6] 行内代码被误改")
    if not inline_done:
        failures.append("[6] 真实行内公式未转换")

    # [7] 空文本与无公式文本原样返回
    plain = "没有任何公式的一段话。"
    ok7 = normalize_math_delimiters("") == "" and normalize_math_delimiters(plain) == plain
    print(f"  [7] 空文本与无公式文本原样返回: {ok7}")
    if not ok7:
        failures.append("[7] 无关文本被改动")

    print()
    if failures:
        for f in failures:
            print("  ✘", f)
        return 1
    print("✔ 通过：定界符规范化正确、内容不变、代码段不动、幂等。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
