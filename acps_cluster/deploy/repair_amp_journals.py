#!/usr/bin/env python3
"""启动前自愈 AMP journal（心跳日志清掉 + 其它日志截掉半截尾行）。

**为什么需要**：WIT 2.1.0 的 `AmpRuntime.start()` 会先对每个要写的日志族做历史校验
（`_AmpJournal.preflight` / `_validate_history`），任何一次硬杀、磁盘写满或并发追加
留下的半行都会让启动抛 `AmpPersistenceError`；而 Supervisor 侧把 AMP 失败当成
"没服务"直接退出，systemd 每 300s 重启一轮，叮当侧表现为周期性掉线
（2026-11-18 审查 P0/P1）。

策略：
- `amp_heartbeat_*.jsonl` 每次启动直接清掉（历史实现如此：心跳是周期重采样，
  清掉不丢业务事实，且能绕开该日志族的启动校验）；
- 其余 `amp_*.jsonl`（system / access / metrics / message / audit）**保留**，
  只把文件末尾不完整的那一行截掉——access 是计量依据，不能整份删。

用法：`python deploy/repair_amp_journals.py ws-supervisor/logs`
（systemd 的 `ExecStartPre` 与 `deploy/run_supervisor.sh` 都会先跑它。）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HEARTBEAT_GLOB = "amp_heartbeat_*.jsonl"
OTHER_GLOB = "amp_*.jsonl"


def truncate_partial_tail(path: Path) -> int:
    """把文件末尾没有换行结尾的半行截掉，返回截掉的字节数（0 = 无需处理）。"""
    try:
        with path.open("r+b") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            if size == 0:
                return 0
            handle.seek(-1, 2)
            if handle.read(1) == b"\n":
                return 0
            # 从尾部向前找最后一个换行；只要半行，不整文件读进内存。
            block = 64 * 1024
            position = size
            newline_at = -1
            while position > 0:
                start = max(0, position - block)
                handle.seek(start)
                chunk = handle.read(position - start)
                index = chunk.rfind(b"\n")
                if index >= 0:
                    newline_at = start + index
                    break
                position = start
            truncated = size - (newline_at + 1)
            handle.truncate(newline_at + 1)
            return truncated
    except OSError as exc:  # noqa: BLE001 - 单个文件修不了也不能挡住启动
        print(f"  [amp-repair] {path.name}: 修复失败 {type(exc).__name__}: {exc}",
              file=sys.stderr, flush=True)
        return 0


def repair_dir(logs_dir: Path) -> tuple[int, int]:
    """执行清理与修复，返回 (删除的心跳文件数, 修复了尾行的文件数)。"""
    if not logs_dir.is_dir():
        return 0, 0
    removed = 0
    for path in sorted(logs_dir.glob(HEARTBEAT_GLOB)):
        try:
            path.unlink()
            removed += 1
        except OSError as exc:  # noqa: BLE001 - 删不掉就交给下面的截尾兜底
            print(f"  [amp-repair] {path.name}: 删除失败 {type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)
    repaired = 0
    for path in sorted(logs_dir.glob(OTHER_GLOB)):
        if path.name.startswith("amp_heartbeat_"):
            continue
        if truncate_partial_tail(path) > 0:
            repaired += 1
            print(f"  [amp-repair] {path.name}: 已截掉不完整的尾行", flush=True)
    return removed, repaired


def main() -> int:
    parser = argparse.ArgumentParser(description="自愈 AMP journal（清心跳、截半截尾行）")
    parser.add_argument("logs_dir", help="实例的 logs 目录，例如 ws-supervisor/logs")
    args = parser.parse_args()
    logs_dir = Path(args.logs_dir)
    removed, repaired = repair_dir(logs_dir)
    if removed or repaired:
        print(f"[amp-repair] {logs_dir}: 清理心跳 {removed} 个，修复尾行 {repaired} 个",
              flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())