#!/usr/bin/env python3
"""检查仓库内 Markdown 文档的相对链接是否都能解析（发布前顺手跑）。

用法：
    python check_docs_links.py [repo_root]

默认 repo_root 从脚本位置推导（acps_cluster/deploy/workspace-rollout/ -> my-project）。
跳过 http(s)/mailto 与纯锚点；只报不存在的目标文件。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def check(root: Path) -> int:
    bad = 0
    docs = sorted((root / "docs").glob("*.md"))
    tops = [root / "README.md", root / "project.md", root / "NOTICE"]
    for md in docs + [p for p in tops if p.is_file()]:
        for match in LINK_RE.finditer(md.read_text(encoding="utf-8")):
            target = match.group(1).split("#", 1)[0].strip()
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            if not (md.parent / target).resolve().exists():
                bad += 1
                print(f"BROKEN {md.relative_to(root)} -> {target}")
    print(f"checked={len(docs)} broken={bad}")
    return 0 if bad == 0 else 1


def main() -> int:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[3]
    return check(root)


if __name__ == "__main__":
    raise SystemExit(main())
