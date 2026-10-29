#!/usr/bin/env python3
"""AMP Access 转发器：本地 `amp_access_*.jsonl` → 平台 Kafka `amp.access`。

## 为什么需要

`amp_heartbeat_sender.py` 只转 `log_type == "heartbeat"`（见其第 180 行），
access 记录会被 `continue` 跳过并**推进位点**——等于永久丢弃。
而平台排行榜口径是 `count = accessCount + messageCount`，access 进不去，
榜单就永远是 0（见 `docs/incident-2026-10-19-ranking-zero.md`）。

本脚本是 access 专用转发器，与心跳转发器**并行、互不影响**：
心跳链路是生产命脉（停了 1–2 分钟全集群被判离线），不能为了 access 去动它。

## 与心跳转发器的相同点

- 只投**新增行**，位点持久化，失败不推进位点（下轮重试）；
- 消息体**原样投递**（不改字段、不重生 log_id）——log_id 是平台幂等键；
- `kafka-python` producer，acks=1，send 后 `get(timeout)` 确认。

## 不同点

- 扫 `amp_access_*.jsonl`（每个 agent 一个文件，文件名里是该 agent 的 AIC）；
- 默认 topic = `amp.access`；
- 校验 `log_type == "access"` 且 body 里有 caller/callee，格式不对的跳过并计数。

## 用法

    # 1) 只看要投什么（不联网）
    python3 amp_access_sender.py --source-dir ../../../acps_cluster --once --dry-run

    # 2) 从当前文件末尾开始投（首次上线用，不回灌历史）
    python3 amp_access_sender.py --init-offsets

    # 3) 正式常驻
    python3 amp_access_sender.py --send --watch

    # 4) 校验 broker 写入权限（投入测试 topic，不动正式 topic）
    python3 amp_access_sender.py --probe

## 目录约定

`--source-dir` 是**集群根目录**（含 `ws-*/logs/`），脚本自己 glob
`ws-*/logs/amp_access_*.jsonl`。位点默认存在 `--state-dir`（默认
`<source-dir>/.amp-access-sender/`），按文件名分键。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

BROKER_DEFAULT = "192.0.2.20:19092"
TOPIC_DEFAULT = "amp.access"
PROBE_TOPIC = "amp.external.smoke"

CLUSTER_DEFAULT = Path(__file__).resolve().parents[1]


# ── 位点 ──────────────────────────────────────────────────────────────────────

def load_offsets(path: Path) -> dict[str, int]:
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return {str(k): int(v) for k, v in data.items()}
        except (OSError, ValueError):
            return {}
    return {}


def save_offsets(path: Path, data: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ── 读取 ──────────────────────────────────────────────────────────────────────

def read_new_lines(file: Path, start: int) -> list[tuple[str, int]]:
    """返回 [(整行文本, 新偏移)]，只取以换行结尾的完整行（半行留到下轮）。"""
    out: list[tuple[str, int]] = []
    try:
        with file.open("rb") as fh:
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
                    if text:
                        out.append((text, pos))
    except OSError:
        return []
    return out


def discover(source: Path) -> list[Path]:
    """每轮重扫：新增 agent 无需重启本进程。"""
    return sorted(source.glob("ws-*/logs/amp_access_*.jsonl"))


def offset_key(file: Path, source: Path, offsets: dict[str, int]) -> str:
    """位点键：主工作区沿用旧 basename，独立 job workspace 用相对路径。

    旧版本 offsets 以 basename 为键（历史上每个 AIC 只有一个 access 文件）。
    job worker 使用 `ws-supervisor-job/logs/` 后，两个文件会同名；若继续用
    basename 会共用位点、漏投/重投。因此：
    - 主工作区 `ws-supervisor/...` 继续读旧 basename 键；
    - 其他（`ws-supervisor-job/...`）使用相对路径键；
    - 新键优先（迁移后）。
    """
    rel = str(file.relative_to(source))
    if rel in offsets:
        return rel
    if rel.startswith("ws-supervisor/") and file.name in offsets:
        return file.name
    return rel


def valid_access(record: dict) -> bool:
    """最小校验：平台侧 AccessWriter 需要 caller/callee 与 log_type。"""
    if record.get("log_type") != "access":
        return False
    if not record.get("aic"):
        return False
    body = record.get("body")
    return isinstance(body, dict) and bool(body.get("caller") or body.get("callee"))


# ── Kafka ─────────────────────────────────────────────────────────────────────

def make_producer(broker: str):
    try:
        from kafka import KafkaProducer  # noqa: PLC0415
    except ImportError:
        print("✘ 缺少 kafka-python（当前虚拟环境）：pip install kafka-python",
              file=sys.stderr)
        raise SystemExit(3)
    return KafkaProducer(
        bootstrap_servers=broker,
        acks=1,
        request_timeout_ms=15000,
        max_block_ms=15000,
        retries=3,
        linger_ms=200,
    )


def probe(broker: str) -> int:
    producer = make_producer(broker)
    record = {
        "schema_version": "1.0",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
        "aic": "1.2.156.3088.1.BUPT.VTF97R.GMT5S6.4SZM68.0LC5",
        "log_type": "access",
        "log_id": f"probe-access-{int(time.time())}",
        "body": {
            "durationMs": 1.0,
            "request": {"method": "POST", "route": "/rpc"},
            "response": {"statusCode": 200},
            "caller": {"aic": "1.2.156.3088.1.BUPT.VTF97R.JXN8O8.98DOO8.0EB6"},
            "callee": {"aic": "1.2.156.3088.1.BUPT.VTF97R.GMT5S6.4SZM68.0LC5"},
        },
        "resource": {"service.name": "yanshu.access-probe"},
    }
    try:
        meta = producer.send(PROBE_TOPIC, json.dumps(record, ensure_ascii=False).encode("utf-8")).get(timeout=15)
        print(f"✔ 探测成功 topic={meta.topic} partition={meta.partition} offset={meta.offset}")
        return 0
    finally:
        producer.close(timeout=5)


# ── 主循环 ────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AMP Access 转发器（本地 jsonl → Kafka amp.access）")
    ap.add_argument("--source-dir", default=str(CLUSTER_DEFAULT),
                    help="集群根目录（含 ws-*/logs/），默认脚本上一级")
    ap.add_argument("--state-dir", default=None,
                    help="位点目录，默认 <source-dir>/.amp-access-sender")
    ap.add_argument("--broker", default=BROKER_DEFAULT)
    ap.add_argument("--topic", default=TOPIC_DEFAULT)
    ap.add_argument("--send", action="store_true", help="真正投递（缺省 dry-run）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--watch", action="store_true", help="常驻循环")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=float, default=15.0)
    ap.add_argument("--max-batch", type=int, default=500, help="每轮每文件最多投递行数")
    ap.add_argument("--send-timeout", type=float, default=15.0,
                    help="单条投递等待 broker 确认的超时（秒）；超时视为失败、位点不推进")
    ap.add_argument("--init-offsets", action="store_true",
                    help="把位点初始化为当前文件末尾后退出（首次上线用，避免回灌历史）")
    ap.add_argument("--probe", action="store_true", help="只向测试 topic 投一条，验证权限后退出")
    args = ap.parse_args(argv)

    if args.probe:
        return probe(args.broker)

    source = Path(args.source_dir).resolve()
    state = Path(args.state_dir) if args.state_dir else source / ".amp-access-sender"
    offsets_path = state / "offsets.json"

    files = discover(source)
    if args.init_offsets:
        data = {f.name: f.stat().st_size for f in files}
        save_offsets(offsets_path, data)
        print(f"✔ 位点已初始化为当前文件末尾（{len(data)} 个文件）→ {offsets_path}")
        for name, size in sorted(data.items()):
            print(f"    {name:70s} {size:8d} bytes")
        return 0

    sending = bool(args.send) and not args.dry_run
    offsets = load_offsets(offsets_path)
    print(f"源        : {source}/ws-*/logs/amp_access_*.jsonl（{len(files)} 个文件）")
    print(f"目标      : {args.topic} @ {args.broker}")
    print(f"位点      : {offsets_path}")
    print(f"模式      : {'【真投递】' if sending else '【dry-run，不联网】'}\n")

    producer = make_producer(args.broker) if sending else None
    total = sent_total = skipped = failures = 0
    try:
        while True:
            round_sent = 0
            for file in discover(source):         # 每轮重扫
                key = offset_key(file, source, offsets)
                start = int(offsets.get(key, 0))
                try:
                    size = file.stat().st_size
                except OSError:
                    continue
                if start > size:                  # 文件被裁剪/重建
                    start = 0
                count = 0
                for text, new_offset in read_new_lines(file, start):
                    try:
                        record = json.loads(text.lstrip("\ufeff"))
                    except json.JSONDecodeError:
                        offsets[key] = new_offset
                        skipped += 1
                        continue
                    if not valid_access(record):
                        offsets[key] = new_offset
                        skipped += 1
                        continue
                    total += 1
                    if sending:
                        assert producer is not None
                        try:
                            meta = producer.send(args.topic, text.encode("utf-8"))
                            meta.get(timeout=args.send_timeout)
                        except Exception as exc:  # noqa: BLE001
                            failures += 1
                            if failures <= 5 or failures % 100 == 0:
                                print(f"  ✘ 投递失败（位点不推进，下轮重试）: "
                                      f"{type(exc).__name__}: {exc}", flush=True)
                            break             # 本轮就此文件停下，保留位点
                        sent_total += 1
                    offsets[key] = new_offset
                    round_sent += 1
                    count += 1
                    if count >= args.max_batch:
                        break
                if count:
                    if sending:
                        save_offsets(offsets_path, offsets)
                    print(f"  {file.stem[:70]:72s} +{count:4d} 条")
            if sending and round_sent:
                save_offsets(offsets_path, offsets)
            stamp = time.strftime("%H:%M:%S")
            print(f"[{stamp}] 本轮 {round_sent} 条（累计命中 {total}，已投 {sent_total}，"
                  f"跳过 {skipped}）{'（dry-run）' if not sending else ''}"
                  f"{f'  失败累计 {failures}' if failures else ''}")
            if sending and failures and round_sent == 0:
                print("[access-sender] ⚠ 本轮零投递且存在失败——位点已保留，将持续重试",
                      flush=True)
            if args.once or args.dry_run or not args.watch:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n收到中断，退出")
    finally:
        if producer is not None:
            producer.close(timeout=5)
        if sending:
            save_offsets(offsets_path, offsets)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
