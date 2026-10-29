#!/usr/bin/env python3
"""扫描"服务器侧热修"：备份树里有、而当前树里没有的补丁行。

背景（2026-10-23/25 连续两次事故）：服务器上的就地热修（amp 计量钩子、
viz_live 伙伴终态、jobs_runner 作业加固、supervisor 交付目录/轮询/作业目录、
papers_memory 记忆误判、wenji 名册条目）从未回流本地，任何"本地→服务器"
的代码同步都会把它们静默覆盖回旧版。

用法（部署前当门禁跑）：
    python scan_server_only_patches.py --backup /opt/wit/yanshu-predeploy-backup-20261023/changed-files.d/ --current /opt/wit/my-project

输出：每个文件"服务器独有、当前树没有"的、带热修特征的行（实测/现网/热修/
日期标记）。命中不代表一定是补丁（也可能是措辞差异），但每一处都应当人工
确认后再同步。只读，不修改任何文件。
"""
from __future__ import annotations

import argparse
from pathlib import Path

MARKERS = ("实测", "现网", "热修", "2026-10-19", "2026-10-20", "2026-10-21",
           "2026-10-22", "2026-10-23", "2026-10-24", "2026-10-25")


def scan(backup_root: Path, current_root: Path) -> list[tuple[str, int, str]]:
    flagged: list[tuple[str, int, str]] = []
    for f in sorted(backup_root.rglob("*")):
        if f.suffix not in (".py", ".cmd", ".sh", ".ps1") or not f.is_file():
            continue
        rel = f.relative_to(backup_root)
        deployed = current_root / rel
        if not deployed.exists():
            continue
        try:
            old = {l.strip() for l in f.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()}
            new = {l.strip() for l in deployed.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()}
        except OSError:
            continue
        lost = [l for l in old - new if any(m in l for m in MARKERS)]
        if lost:
            flagged.append((str(rel), len(lost), lost[0][:110]))
    return flagged


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backup", required=True, help="部署前备份树（如 changed-files 解包目录）")
    ap.add_argument("--current", required=True, help="当前代码树（通常为 /opt/wit/my-project）")
    args = ap.parse_args()
    flagged = scan(Path(args.backup), Path(args.current))
    if not flagged:
        print("扫描完成：未发现服务器侧独有的补丁行")
        return 0
    print(f"发现 {len(flagged)} 个文件存在服务器独有补丁行——逐个人工确认后再同步：")
    for rel, n, sample in flagged:
        print(f"  {rel}: {n} 行 | 例: {sample}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
