#!/usr/bin/env python3
"""研枢测试入口：一条命令复跑全量 / 无副作用子集 / 仅收集。

背景（2026-10-21 审查）：README 里同时出现过 612 / 622 两个数字，而实际用例数是
661；`unittest discover` 又因为 `tests/` 没有 `__init__.py` 直接报
"Start directory is not importable"。本脚本把"怎么跑、跑出多少"固定成一条命令。

用法：
    python run_all_tests.py              # 全量（需要本地 dev CA 私钥；缺失的用例会 skip）
    python run_all_tests.py --quick      # 无副作用子集（不碰 Docker / 不发网络请求）
    python run_all_tests.py --collect    # 只收集，验证测试可被发现

退出码：0 = 全绿（含 skip）；非 0 = 有失败/错误。
"""

from __future__ import annotations

import argparse
import importlib.util
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TESTS = ROOT / "acps_cluster" / "tests"
# 队友 guard 组件自带测试（经 acps_cluster/covert_event_guard.py shim 原样导入）。
GUARD_TESTS = ROOT / "acps_cluster" / "guard" / "tests"
TEST_DIRS = [TESTS, GUARD_TESTS]

# 无副作用子集：不启动 Docker、不发外部网络请求；仍然需要 dev 私钥才能跑签名用例，
# 缺私钥时这些用例会 skip（不是 fail）。
QUICK_MODULES = [
    "acps_cluster/tests/test_delegation.py",
    "acps_cluster/tests/test_domain.py",
    "acps_cluster/tests/test_domain_hardening.py",
    "acps_cluster/tests/test_visibility.py",
    "acps_cluster/tests/test_entry_hardening.py",
    "acps_cluster/tests/test_p0_security.py",
]


def _git_head() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT, capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _has_pytest() -> bool:
    return importlib.util.find_spec("pytest") is not None


def _run(command: list[str]) -> int:
    print("$ " + " ".join(command))
    return subprocess.call(command, cwd=ROOT)


def main() -> int:
    parser = argparse.ArgumentParser(description="研枢测试入口")
    parser.add_argument("--quick", action="store_true", help="只跑无副作用子集")
    parser.add_argument("--collect", action="store_true", help="只收集测试，不执行")
    args = parser.parse_args()

    print(f"Python {platform.python_version()} | {platform.platform()}")
    print(f"repo   {ROOT} | HEAD {_git_head()}")
    print(f"mode   {'quick' if args.quick else 'full'}{' / collect-only' if args.collect else ''}\n")

    if not TESTS.is_dir():
        print(f"找不到测试目录：{TESTS}", file=sys.stderr)
        return 2

    if _has_pytest():
        command = [sys.executable, "-m", "pytest"]
        command += QUICK_MODULES if args.quick else [str(path) for path in TEST_DIRS]
        command += ["-q"]
        if args.collect:
            command += ["--collect-only"]
        return _run(command)

    print("未安装 pytest，退回 unittest（--collect 需要 pytest）。")
    if args.collect:
        return 2
    modules = QUICK_MODULES if args.quick else sorted(
        str(path.relative_to(ROOT)).replace("/", ".")[:-3]
        for directory in TEST_DIRS
        for path in directory.glob("test_*.py")
    )
    return _run([sys.executable, "-m", "unittest", "-v", *modules])


if __name__ == "__main__":
    raise SystemExit(main())
