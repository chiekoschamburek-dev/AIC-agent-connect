#!/usr/bin/env python3
"""仓库事实核验：把对外材料里引用的数字，全部在本地算一遍，一条命令给出。

用法：

    python acps_cluster/tools/repo_facts.py            # 人读表格
    python acps_cluster/tools/repo_facts.py --json     # 机器读（写进 docs/verification.md）

为什么要这个脚本
----------------
答辩材料里的"受控文件 N 个""核心模块 docstring X/X""公开函数 Y/Y""Python 占比 Z%"
都属于**可被评委当场复核**的数字。它们的定义（算哪些文件、排除哪些目录、什么算"公开函数"）
如果只写在文档里，换个时间点就会漂移。本脚本把定义固化成代码：材料引用哪个数字，
就跑这个脚本取，而不是手抄。

口径（与材料一致）
------------------
- **受控文件**：`git ls-files` 的行数——即"入仓、可被任何人 `git clone` 后看到的文件"。
  运行时目录（ws-*）、私钥、数据集不入库，因此不计入。
- **核心模块**：`acps_cluster/{agents,core,runtime,delegation}` + `capabilities` 下的 `.py`
  （不含 `__pycache__`）。这是"我们自己写、且承载机制"的代码面。
- **公开函数 docstring**：核心模块里不以 `_` 开头的函数；"长函数"= 跨行 ≥20 行。
  材料只引用**长公开函数**的完整率（短小 getter 的 docstring 不构成质量信号）。
- **语言占比**：按**代码文件字节数**统计（`.py/.sh/.ts/.tsx/.js/.vue/.css/.html` 等），
  不含 Markdown、JSON、图片、PDF、wheel 这些非代码文件——与 GitHub 语言条的算法近似
  （GitHub 的 linguist 会额外剔除 vendored 路径与生成文件，数值允许 ±1pt 差异）。
"""

from __future__ import annotations

import argparse
import ast
import json
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORE_DIRS = ("acps_cluster/agents", "acps_cluster/core", "acps_cluster/runtime",
             "acps_cluster/delegation", "capabilities")
CODE_SUFFIXES = {".py", ".sh", ".ts", ".tsx", ".js", ".jsx", ".vue", ".css",
                 ".html", ".scss", ".mjs", ".cjs", ".service", ".timer"}
LONG_FUNCTION_LINES = 20


def _git(*args: str) -> str:
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)
    return out.stdout


def tracked_files() -> list[Path]:
    lines = [line.strip() for line in _git("ls-files").splitlines() if line.strip()]
    return [ROOT / line for line in lines]


def commit() -> dict[str, str]:
    return {
        "head": _git("rev-parse", "HEAD").strip() or "unknown",
        "short": _git("rev-parse", "--short", "HEAD").strip() or "unknown",
        "date": _git("log", "-1", "--format=%cI").strip() or "unknown",
    }


def python_scale(files: list[Path]) -> dict[str, int]:
    """生产代码 / 测试代码行数（物理行，含空行与注释——与"代码规模"口径一致）。"""
    prod = test = prod_files = test_files = 0
    for path in files:
        if path.suffix != ".py" or not path.is_file():
            continue
        try:
            lines = len(path.read_text(encoding="utf-8", errors="ignore").splitlines())
        except OSError:
            continue
        if path.name.startswith("test_") or "/tests/" in path.as_posix():
            test += lines
            test_files += 1
        else:
            prod += lines
            prod_files += 1
    return {"production_lines": prod, "test_lines": test,
            "production_files": prod_files, "test_files": test_files}


def docstring_coverage() -> dict[str, object]:
    """核心模块的模块级 docstring 与（长）公开函数 docstring 完整率。"""
    module_total = module_doc = 0
    long_total = long_doc = 0
    long_missing: list[str] = []
    for name in CORE_DIRS:
        for path in sorted((ROOT / name).rglob("*.py")):
            if "__pycache__" in path.as_posix():
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            except SyntaxError:
                continue
            module_total += 1
            module_doc += bool(ast.get_docstring(tree))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if node.name.startswith("_"):
                    continue
                span = (node.end_lineno or node.lineno) - node.lineno
                if span < LONG_FUNCTION_LINES:
                    continue
                long_total += 1
                has = bool(ast.get_docstring(node))
                long_doc += has
                if not has:
                    long_missing.append(
                        f"{path.relative_to(ROOT).as_posix()}:{node.lineno} {node.name}"
                    )
    return {
        "core_modules": module_total,
        "core_modules_with_docstring": module_doc,
        "long_public_functions": long_total,
        "long_public_functions_with_docstring": long_doc,
        "long_public_functions_missing": long_missing,
    }


def language_share(files: list[Path]) -> dict[str, float]:
    """代码文件字节数占比（只统计 CODE_SUFFIXES）。"""
    buckets: dict[str, int] = {}
    for path in files:
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix not in CODE_SUFFIXES:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        buckets[suffix] = buckets.get(suffix, 0) + size
    total = sum(buckets.values()) or 1
    return {ext: round(size / total, 4) for ext, size in
            sorted(buckets.items(), key=lambda item: item[1], reverse=True)}


def collect() -> dict[str, object]:
    files = tracked_files()
    info = commit()
    python = python_scale(files)
    docs = docstring_coverage()
    languages = language_share(files)
    return {
        "commit": info,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "tracked_files": len(files),
        **python,
        **docs,
        "python_share": languages.get(".py", 0.0),
        "languages": languages,
        "test_command": ("python -m pytest acps_cluster/tests acps_cluster/guard/tests "
                         "guard-api/tests -q"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="仓库事实核验（对外数字的唯一取值来源）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    facts = collect()
    if args.json:
        print(json.dumps(facts, ensure_ascii=False, indent=2))
        return 0

    print(f"仓库      {ROOT}")
    print(f"提交      {facts['commit']['short']}（{facts['commit']['date']}）")
    print(f"解释器    Python {facts['python']} | {facts['platform']}")
    print()
    print(f"受控文件           {facts['tracked_files']} 个（git ls-files）")
    print(f"生产 Python        {facts['production_lines']} 行 / {facts['production_files']} 文件")
    print(f"测试 Python        {facts['test_lines']} 行 / {facts['test_files']} 文件")
    print()
    print(f"核心模块           {facts['core_modules_with_docstring']}/{facts['core_modules']}"
          " 有模块 docstring")
    print(f"长公开函数         {facts['long_public_functions_with_docstring']}/"
          f"{facts['long_public_functions']} 有 docstring（≥{LONG_FUNCTION_LINES} 行）")
    if facts["long_public_functions_missing"]:
        print("缺失：")
        for item in facts["long_public_functions_missing"]:
            print(f"  - {item}")
    print()
    print("代码占比（按字节）")
    for ext, share in list(facts["languages"].items())[:6]:
        print(f"  {ext:<10} {share:6.1%}")
    print()
    print(f"测试复跑           {facts['test_command']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())