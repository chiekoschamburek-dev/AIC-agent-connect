#!/usr/bin/env python3
"""演示截图采集：前端（含 AIP 互联面板）关键场景自动实拍。

输出到 docs/aic/evidence/，供技术报告与演示视频引用。
前置：backend(:8000) 与 frontend(:5173) 已启动（tools/systemd/install.sh）。

用法（backend/ 目录）：
  .venv/bin/python scripts/acps/capture_demo.py [--url http://localhost:5173]
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parents[3]
OUT = REPO / "docs" / "aic" / "evidence"

SCENES = [
    # (文件名, 问题或 None=仅首页, 说明)
    ("01_home_aip.png", None, "首页 + AIP 互联状态面板"),
    ("02_db_query.png", "销量前十的曲目有哪些？", "结构化问数：SQL 卡片 + 数据表 + Trace"),
    ("03_doc_qa.png", "员工年假有多少天？", "文档问答：引用列表"),
    ("04_clarify.png", "销售额增长率是多少？", "主动澄清：槽位缺失挂起 + 选项"),
    ("05_formula.png", "Jane 的销售提成是多少？", "公式计算：文档公式 + DB 绑参"),
]


def wait_idle(page, timeout_s: float = 90.0) -> None:
    """等待回复完成：发送按钮从 busy 状态恢复（或超时）。"""
    deadline = time.time() + timeout_s
    time.sleep(2)
    while time.time() < deadline:
        busy = page.locator(".composer.is-busy").count()
        if busy == 0:
            time.sleep(1.5)  # 等待最终渲染
            return
        time.sleep(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:5173")
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    # 优先用系统缓存的 chromium（离线环境），否则回退 playwright 自带下载
    import shutil

    cached = Path.home() / ".cache/ms-playwright/chromium-1234/chrome-linux64/chrome"
    launch_kwargs = {"executable_path": str(cached)} if cached.exists() else {}
    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs)
        page = browser.new_page(viewport={"width": 1680, "height": 1000}, device_scale_factor=2)
        page.goto(args.url, wait_until="networkidle")
        time.sleep(2)

        for filename, question, desc in SCENES:
            if question:
                box = page.locator(".composer textarea, .composer input").first
                box.fill(question)
                box.press("Enter")
                wait_idle(page)
            page.screenshot(path=str(OUT / filename), full_page=False)
            print(f"[ok] {filename}  <- {desc}")

        browser.close()
    print(f"\n已输出 {len(SCENES)} 张到 {OUT}")


if __name__ == "__main__":
    main()
