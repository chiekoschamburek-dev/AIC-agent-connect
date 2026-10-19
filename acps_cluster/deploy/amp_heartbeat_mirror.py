#!/usr/bin/env python3
"""把各 agent 的心跳镜像成一个"平台 Forwarder 可直接尾随"的目录。

## 为什么需要它（选项 A）
官方 AMP Forwarder（`acps-infra` 的 `amp_forwarder` role / Fluent Bit）**只能在它自己读得到的日志目录里 tail**。
我们的 agent 跑在自己的主机上，日志散在 `ws-*/logs/` 里、文件名还带 AIC，
所以要先镜像成一个稳定的目录 + 固定文件名，平台侧只需加一段 tail 配置：

    [INPUT]
        Name             tail
        Path             <ROOT>/yanshu/amp_heartbeat_*.jsonl
        Tag              amp.heartbeat
        Parser           amp_audit_json
        Refresh_Interval 2
        Read_From_Head   False
        DB               /var/lib/fluent-bit/yanshu-hb.db

    [OUTPUT]
        Name    kafka
        Match   amp.heartbeat
        Brokers <redpanda>:19092
        Topics  amp.heartbeat

## 这个脚本做什么
- 读 `ws-*/logs/amp_heartbeat_*.jsonl`（只读，不碰原文件）；
- 按 `<ROOT>/yanshu/amp_heartbeat_<agent>.jsonl` 追加写入（**只写新增行**，位置记在 `<ROOT>/.mirror-offsets.json`）；
- 每行原样搬运（不改字段、不重新生成 `log_id`——AMP 要求 logId 源端不可变）；
- `--stale-seconds` 内的**新鲜行**才镜像（默认 300s，避免把历史/退役 agent 的旧心跳灌进平台）；
- `--prune-mb` 控制单文件上限，避免长期跑爆盘。

用法：
    python amp_heartbeat_mirror.py --root /var/log/acps --once        # 跑一轮
    python amp_heartbeat_mirror.py --root /var/log/acps --watch       # 常驻（每 15s 一轮）
    python amp_heartbeat_mirror.py --root /var/log/acps --once --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

CLUSTER_DEFAULT = "/opt/wit/my-project/acps_cluster"


def parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def load_offsets(path: Path) -> dict[str, int]:
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def save_offsets(path: Path, data: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def prune(path: Path, keep_bytes: int) -> None:
    """文件超过上限时，保留尾部 keep_bytes（按行边界裁剪）。"""
    if keep_bytes <= 0 or not path.is_file() or path.stat().st_size <= keep_bytes:
        return
    data = path.read_bytes()
    tail = data[-keep_bytes:]
    nl = tail.find(b"\n")
    tail = tail[nl + 1:] if nl >= 0 else tail
    path.write_bytes(tail)


def mirror_once(cluster: Path, root: Path, *, dry_run: bool, stale_seconds: float,
                prune_mb: float) -> tuple[int, int]:
    out_dir = root / "yanshu"
    offsets_path = root / ".mirror-offsets.json"
    offsets = load_offsets(offsets_path)
    now = datetime.now(timezone.utc)
    written_total = 0
    skipped_stale = 0
    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    for src in sorted(cluster.glob("ws-*/logs/amp_heartbeat_*.jsonl")):
        agent = src.parent.parent.name.removeprefix("ws-")
        dst = out_dir / f"amp_heartbeat_{agent}.jsonl"
        # 每文件容错（2026-10-11 审查 P1）：`src.stat()` / `src.open()` 都可能在竞态下抛
        # FileNotFoundError——各单元 ExecStartPre 会 `rm -f ws-%i/logs/*.jsonl`，
        # 正好删掉本循环正在 glob 的文件。原先没有 per-file try，
        # 一次竞态就会掀掉整个 mirror（好在 RestartSec=30 会拉起，但会丢一轮）。
        try:
            start = int(offsets.get(src.name, 0))
            if start > src.stat().st_size:  # 原文件被轮转过（systemd 启动前会清）
                start = 0
            written = 0
            with src.open("rb") as fh:
                fh.seek(start)
                buf = b""
                pos = start
                while True:
                    chunk = fh.read(65536)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        pos += len(line) + 1
                        text = line.decode("utf-8", "ignore").strip()
                        if not text:
                            continue
                        try:
                            record = json.loads(text)
                        except json.JSONDecodeError:
                            continue
                        ts = parse_ts(str(record.get("timestamp") or ""))
                        if stale_seconds > 0 and ts is not None:
                            age = (now - ts).total_seconds()
                            if age > stale_seconds:
                                skipped_stale += 1
                                continue
                        # 只镜像心跳类型、且必须有 aic（AMP 最小要求）
                        if record.get("log_type") != "heartbeat" or not record.get("aic"):
                            continue
                        if not dry_run:
                            with dst.open("a", encoding="utf-8") as out:
                                out.write(text + "\n")
                        written += 1
                offsets[src.name] = pos
        except OSError as exc:
            print(f"  {agent:16s} 跳过（文件竞态: {type(exc).__name__}）", flush=True)
            continue
        if written:
            written_total += written
            print(f"  {agent:16s} → {dst.name:38s} +{written:4d} 行")
            if not dry_run:
                prune(dst, int(prune_mb * 1024 * 1024))

    if not dry_run:
        save_offsets(offsets_path, offsets)
    return written_total, skipped_stale


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="心跳镜像到平台 Forwarder 可读目录（选项 A）")
    ap.add_argument("--cluster", default=CLUSTER_DEFAULT, help="acps_cluster 目录（各 ws-* 的父目录）")
    ap.add_argument("--root", required=True, help="输出根目录（平台 Forwarder 尾随的位置）")
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    ap.add_argument("--watch", action="store_true", help="常驻，按 --interval 循环")
    ap.add_argument("--interval", type=float, default=15.0)
    ap.add_argument("--stale-seconds", type=float, default=300.0,
                    help="只镜像该时间窗内的新鲜心跳（<=0 表示不过滤）")
    ap.add_argument("--prune-mb", type=float, default=8.0, help="单个镜像文件上限（MB）")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写盘")
    args = ap.parse_args(argv)

    cluster = Path(args.cluster)
    root = Path(args.root)
    if not cluster.is_dir():
        print(f"✘ 集群目录不存在：{cluster}", file=sys.stderr)
        return 1

    print(f"源        : {cluster}/ws-*/logs/amp_heartbeat_*.jsonl")
    print(f"镜像目标  : {root}/yanshu/amp_heartbeat_<agent>.jsonl")
    print(f"新鲜度窗口: {args.stale_seconds}s   单文件上限: {args.prune_mb}MB   "
          f"模式: {'dry-run' if args.dry_run else ('watch' if args.watch else 'once')}\n")

    rounds = 0
    try:
        while True:
            rounds += 1
            written, stale = mirror_once(cluster, root, dry_run=args.dry_run,
                                         stale_seconds=args.stale_seconds, prune_mb=args.prune_mb)
            stamp = time.strftime("%H:%M:%S")
            print(f"[{stamp}] 第 {rounds} 轮：镜像 {written} 行，跳过过期 {stale} 行"
                  f"{'（dry-run）' if args.dry_run else ''}")
            if args.once or args.dry_run:
                break
            if not args.watch:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n收到中断，退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
