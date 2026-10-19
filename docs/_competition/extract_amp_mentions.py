"""从比赛材料 PDF 里找 AMP / 心跳 / 上报 相关的原话。"""

from __future__ import annotations

import re
from pathlib import Path

import fitz  # pymupdf

DIR = Path(r"D:\桌面\project\AIC智能体互联\my-project\docs\_competition")
KEYS = ["AMP", "心跳", "上报", "监控", "heartbeat", "alive", "存活", "在线", "可观测", "观测"]

for pdf in sorted(DIR.glob("*.pdf")):
    doc = fitz.open(pdf)
    print("=" * 78)
    print(f"### {pdf.name}  （{doc.page_count} 页）")
    for i, page in enumerate(doc):
        text = page.get_text()
        hits = [k for k in KEYS if k in text]
        if not hits:
            continue
        print(f"\n--- 第 {i + 1} 页  命中关键词：{hits}")
        # 打印含关键词的段落
        for para in re.split(r"\n\s*\n", text):
            if any(k in para for k in KEYS):
                clean = " ".join(para.split())
                if len(clean) > 8:
                    print(f"    {clean[:400]}")
    doc.close()
