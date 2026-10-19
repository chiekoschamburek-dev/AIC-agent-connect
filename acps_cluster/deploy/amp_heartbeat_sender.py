#!/usr/bin/env python3
"""AMP 心跳转发器（B 方案）：镜像目录 → 平台 Kafka `amp.heartbeat`。

## 与 A 的关系
- A：平台侧 `amp_forwarder` 采集我们的日志目录（等平台施工）；
- **B（本脚本）**：我们侧按官方 Forwarder 的同语义投递 —— 只投新增行、位点持久化、
  消息体原样（不改字段、不重生成 log_id）。

## 实现
用 `kafka-python`（venv 已有 3.0.11）做 producer，不再自研 Kafka 协议。
早先自研版本在 RecordBatch 的 zigzag varint 上有 bug（broker 返回 CORRUPT_MESSAGE），
已弃用；脚本对 kafka-python 缺失会给出明确报错。

## 用法
    # 只看要投什么（不联网、不推进位点）
    python amp_heartbeat_sender.py --mirror-dir /var/log/acps-amp --once --dry-run

    # 从"当前文件末尾"开始正式投递（推荐首次使用：不回灌历史）
    python amp_heartbeat_sender.py --mirror-dir /var/log/acps-amp --init-offsets
    python amp_heartbeat_sender.py --mirror-dir /var/log/acps-amp --send --watch

    # 校验 broker 的投递权限（投入测试 topic，不动正式 topic）
    python amp_heartbeat_sender.py --probe
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

BROKER_DEFAULT = "192.0.2.20:19092"
TOPIC_DEFAULT = "amp.heartbeat"
PROBE_TOPIC = "amp.external.smoke"


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


def read_new_lines(file: Path, start: int) -> list[tuple[str, int]]:
    """返回 [(整行文本, 新偏移)]，只取以换行结尾的完整行。"""
    out: list[tuple[str, int]] = []
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
    return out


def make_producer(broker: str):
    try:
        from kafka import KafkaProducer  # noqa: PLC0415
    except ImportError:
        print("✘ 缺少 kafka-python（应在 /opt/wit/venv 中）：pip install kafka-python", file=sys.stderr)
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
        "log_type": "system",
        "log_id": f"probe-yanshu-b-{int(time.time())}",
        "severity_text": "INFO",
        "severity_number": 9,
        "body": {"event": "external.producer.probe"},
    }
    try:
        meta = producer.send(PROBE_TOPIC, json.dumps(record, ensure_ascii=False).encode("utf-8")).get(timeout=15)
        print(f"✔ 探测成功 topic={meta.topic} partition={meta.partition} offset={meta.offset}")
        return 0
    finally:
        producer.close(timeout=5)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AMP 心跳转发器（B：我们侧直投）")
    ap.add_argument("--mirror-dir", default="/var/log/acps-amp",
                    help="镜像根目录（<dir>/yanshu/amp_heartbeat_*.jsonl）")
    ap.add_argument("--broker", default=BROKER_DEFAULT)
    ap.add_argument("--topic", default=TOPIC_DEFAULT)
    ap.add_argument("--offsets", default=None, help="默认 <mirror-dir>/.sender-offsets.json")
    ap.add_argument("--send", action="store_true", help="真正投递（缺省 dry-run）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--watch", action="store_true", help="常驻循环")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=float, default=15.0)
    ap.add_argument("--max-batch", type=int, default=200, help="每轮每文件最多投递行数")
    ap.add_argument("--send-timeout", type=float, default=15.0,
                    help="单条投递等待 broker 确认的超时（秒）；超时视为失败、位点不推进")
    ap.add_argument("--init-offsets", action="store_true",
                    help="把位点初始化为当前文件末尾后退出（首次上线用，避免回灌历史）")
    ap.add_argument("--probe", action="store_true", help="只向测试 topic 投一条，验证权限后退出")
    args = ap.parse_args(argv)

    if args.probe:
        return probe(args.broker)

    mirror = Path(args.mirror_dir)
    offsets_path = Path(args.offsets) if args.offsets else mirror / ".sender-offsets.json"
    # 文件列表**每轮重扫**（原先只在 while 外 glob 一次）：
    #   · 新增 agent 无需重启 sender 才能投递；
    #   · sender 若早于 mirror 启动（目录还空），下一轮就能捡到文件，不会永久空转。
    #   见 2026-10-11 审查 P1。
    files = sorted(mirror.glob("yanshu/amp_heartbeat_*.jsonl"))

    if args.init_offsets:
        data = {f.name: f.stat().st_size for f in files}
        save_offsets(offsets_path, data)
        print(f"✔ 位点已初始化为当前文件末尾（{len(data)} 个文件）→ {offsets_path}")
        for name, size in data.items():
            print(f"    {name:38s} {size:8d} bytes")
        return 0

    sending = bool(args.send) and not args.dry_run
    offsets = load_offsets(offsets_path)
    print(f"源        : {mirror}/yanshu/amp_heartbeat_*.jsonl（{len(files)} 个文件）")
    print(f"目标      : {args.topic} @ {args.broker}")
    print(f"位点      : {offsets_path}")
    print(f"模式      : {'【真投递】' if sending else '【dry-run，不联网】'}\n")

    producer = make_producer(args.broker) if sending else None
    total = 0
    send_failures = 0
    try:
        while True:
            round_sent = 0
            files = sorted(mirror.glob("yanshu/amp_heartbeat_*.jsonl"))   # 每轮重扫（见上）
            for file in files:
                start = int(offsets.get(file.name, 0))
                try:
                    size = file.stat().st_size
                except OSError:
                    continue                     # 文件刚被 mirror 重建/删掉，下轮再说
                if start > size:                 # 镜像文件被裁剪过
                    start = 0
                count = 0
                for text, new_offset in read_new_lines(file, start):
                    try:
                        record = json.loads(text)
                    except json.JSONDecodeError:
                        offsets[file.name] = new_offset
                        continue
                    if record.get("log_type") != "heartbeat" or not record.get("aic"):
                        offsets[file.name] = new_offset
                        continue
                    if sending:
                        assert producer is not None
                        # 必须拿到 send() 的 Future 并**等它成功**再推进位点。
                        # 原实现丢弃 Future、且不看 flush() 返回值 → broker 拒收时
                        # 心跳被永久跳过，而进程仍 active、日志零报错，
                        # 平台会在 1–2 分钟内把 8 个 agent 全判离线（2026-10-11 审查 P0-5）。
                        # 失败时**不推进位点**，留待下一轮重试。
                        try:
                            meta = producer.send(args.topic, text.encode("utf-8"))
                            meta.get(timeout=args.send_timeout)
                        except Exception as exc:  # noqa: BLE001
                            send_failures += 1
                            if send_failures <= 5 or send_failures % 100 == 0:
                                print(f"  ✘ 投递失败（位点不推进，下轮重试）: "
                                      f"{type(exc).__name__}: {exc}", flush=True)
                            break            # 本轮就此文件停下，保留位点
                    offsets[file.name] = new_offset
                    total += 1
                    round_sent += 1
                    count += 1
                    if count >= args.max_batch:
                        break
                if count:
                    if sending:
                        save_offsets(offsets_path, offsets)
                    print(f"  {file.stem:40s} +{count:4d} 条")
            if sending and round_sent:
                save_offsets(offsets_path, offsets)
            stamp = time.strftime("%H:%M:%S")
            print(f"[{stamp}] 本轮投递 {round_sent} 条（累计 {total}）"
                  f"{'（dry-run）' if not sending else ''}"
                  f"{f' 投递失败累计 {send_failures}' if send_failures else ''}")
            # 投递连续失败要显式告警：静默失败正是 P0-5 的可怕之处
            if send_failures and round_sent == 0:
                print("[sender] ⚠ 本轮零投递且存在失败——位点已保留，将持续重试；"
                      "若长期如此，平台会把 agent 判为离线", flush=True)
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
