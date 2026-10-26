#!/usr/bin/env python3
"""检查产出正文里的数学公式写法：定界符、空行、以及常见的 Markdown 不兼容形式。"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

CL = Path("/opt/wit/my-project/acps_cluster")


def bodies(text: str) -> None:
    print(f"    总字符 {len(text)}")
    # 各种数学定界符的计数
    pats = {
        r"\[": "display 开（反斜杠方括号）",
        r"\]": "display 闭（反斜杠方括号）",
        r"\$\$": "dollar-double",
        r"\\\(": "inline 开 \\(  ",
        r"\\\)": "inline 闭 \\)  ",
        r"\\begin\{": "环境 begin{",
        r"\\frac|\\mathcal|\\theta|\\sigma|\\mu|\\mathbb": "LaTeX 命令",
    }
    for p, label in pats.items():
        n = len(re.findall(p, text))
        if n:
            print(f"      {label:26s} {n}")

    # 找第一个 display 公式片段，看内部有没有空行
    m = re.search(r"\\\[(.*?)\\\]", text, re.S)
    if m:
        inner = m.group(1)
        blank = len(re.findall(r"\n\s*\n", inner))
        print(f"      → 第一个 \\[...\\] 片段：内部空行 {blank} 处，长度 {len(inner)}")
        print(f"        原文 repr: {inner[:160]!r}")
    m2 = re.search(r"\$\$(.*?)\$\$", text, re.S)
    if m2:
        inner = m2.group(1)
        blank = len(re.findall(r"\n\s*\n", inner))
        print(f"      → 第一个 $$...$$ 片段：内部空行 {blank} 处，长度 {len(inner)}")


for ws in ("supervisor", "writer"):
    db = CL / f"ws-{ws}" / "acps" / "aip-tasks.sqlite3"
    if not db.is_file():
        continue
    print(f"===== ws-{ws}")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("""SELECT task_ref, direction, state, products FROM aip_tasks
                   WHERE products IS NOT NULL AND products != '[]'
                   ORDER BY created_at DESC LIMIT 6""")
    for r in cur.fetchall():
        prods = json.loads(r["products"] or "[]")
        for p in prods:
            items = p.get("dataItems") or p.get("data_items") or []
            for d in items:
                t = str(d.get("text") or "")
                if len(t) < 200:
                    continue
                print(f"  --- {r['task_ref']} [{r['direction']}] {r['state']} "
                      f"product={str(p.get('id'))[:24]}")
                bodies(t)
    con.close()
    print()
