#!/usr/bin/env python3
"""恢复 14 个模块丢失的 AmpAccessLoggingHooks 接线（2026-10-24 计量回归修复）。

背景：10-19 的 amp 计量钩子（amp_hooks=[AmpAccessLoggingHooks()]）当时直接改在
服务器的 partner_*.py / supervisor.py 上；本地此后的 P0 安全整改基于未打补丁的
本地副本，10-23 部署把钩子覆盖丢了 → 伙伴收得到请求、PEP 审计正常，但
amp_access_*.jsonl 停写 → 平台榜单计量清零。

用法：python acps_cluster/deploy/restore_amp_hooks.py   （幂等，跳过已含钩子的文件）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

CLUSTER = Path(__file__).resolve().parents[1]

IMPORT_LINE = "from wit.hooks.acps.amp import AmpAccessLoggingHooks  # noqa: E402"
PARTNER_HOOK_LINE = "        amp_hooks=[AmpAccessLoggingHooks()],"

SUPERVISOR_HOOK_BLOCK = """        # 作业 worker 与常驻 supervisor 的 access journal 已按工作区隔离
        # （ws-supervisor-job），两侧都能安全落账；需要临时关掉 access 记录时设
        # ACPS_SUPERVISOR_NO_AMP_ACCESS=1（2026-10-24 随钩子恢复接线一起回归）。
        amp_hooks=(
            [] if os.getenv("ACPS_SUPERVISOR_NO_AMP_ACCESS", "0").strip().lower()
            in {"1", "true", "yes", "on"}
            else [AmpAccessLoggingHooks()]
        ),
"""

PARTNERS = [
    "partner_citation.py", "partner_contradiction.py", "partner_cpu.py",
    "partner_critic.py", "partner_eval.py", "partner_experiment.py",
    "partner_frontier.py", "partner_gpu.py", "partner_kb.py",
    "partner_model.py", "partner_scout.py", "partner_synthesis.py",
    "partner_writer.py",
]


def add_import(text: str, path: Path) -> str:
    if "AmpAccessLoggingHooks" in text:
        return text
    lines = text.splitlines(keepends=True)
    anchor = None
    for i, line in enumerate(lines):
        if line.startswith("from aip_guard import") or line.startswith("from delegation import"):
            anchor = i
            break
    if anchor is None:
        for i, line in enumerate(lines):
            if line.startswith("from wit import") or line.startswith("from wit.hooks"):
                anchor = i
                break
    if anchor is None:
        raise SystemExit(f"{path.name}: 找不到 import 插入锚点")
    lines.insert(anchor, IMPORT_LINE + "\n")
    return "".join(lines)


def add_partner_hook(text: str, path: Path) -> str:
    if "amp_hooks=" in text:
        return text
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if re.match(r"^\s+aip_hooks=\[[^\]]*\],\s*$", line):
            indent = re.match(r"^(\s+)", line).group(1)  # noqa: W605
            lines.insert(i + 1, f"{indent}amp_hooks=[AmpAccessLoggingHooks()],\n")
            return "".join(lines)
    raise SystemExit(f"{path.name}: 找不到 aip_hooks 单行锚点")


def patch_supervisor(text: str, path: Path) -> str:
    if "amp_hooks=" in text:
        return text
    lines = text.splitlines(keepends=True)
    anchor = None
    for i, line in enumerate(lines):
        if re.match(r"^\s+aip_hooks=\[AipPartnerGuardHooks\(\), \*pep_aip_hooks\],\s*$", line):
            anchor = i
            break
    if anchor is None:
        raise SystemExit(f"{path.name}: 找不到 supervisor aip_hooks 锚点")
    indent = "        "
    block = "".join(indent + l + "\n" for l in SUPERVISOR_HOOK_BLOCK.strip("\n").split("\n"))
    lines.insert(anchor + 1, block)
    return "".join(lines)


def main() -> int:
    failures = []
    for name in PARTNERS:
        p = CLUSTER / name
        text = p.read_text(encoding="utf-8")
        if "AmpAccessLoggingHooks" in text:
            print(f"跳过（已有）: {name}")
            continue
        text = add_partner_hook(add_import(text, p), p)
        p.write_text(text, encoding="utf-8")
        print(f"已补: {name}")
    sup = CLUSTER / "supervisor.py"
    text = sup.read_text(encoding="utf-8")
    if "AmpAccessLoggingHooks" not in text:
        lines = text.splitlines(keepends=True)
        for i, line in enumerate(lines):
            if line.startswith("from wit.hooks.acps.delegation import"):
                lines.insert(i, IMPORT_LINE + "\n")
                break
        else:
            raise SystemExit("supervisor.py: 找不到 delegation import 锚点")
        text = patch_supervisor("".join(lines), sup)
        sup.write_text(text, encoding="utf-8")
        print("已补: supervisor.py")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
