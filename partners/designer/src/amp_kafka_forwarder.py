#!/usr/bin/env python3
"""Continuously forward new WIT AMP JSONL records to Kafka with checkpoints."""

import argparse
import json
import os
from pathlib import Path
import signal
import sys
import time

from kafka import KafkaProducer


RUNNING = True


def stop(_signum, _frame):
    global RUNNING
    RUNNING = False


def save_state(path: Path, state: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def new_producer(broker: str) -> KafkaProducer:
    return KafkaProducer(
        bootstrap_servers=[broker],
        security_protocol="PLAINTEXT",
        acks=1,
        retries=5,
        retry_backoff_ms=1000,
        max_block_ms=15000,
        request_timeout_ms=15000,
    )


def discover(log_dir: Path, aic: str) -> dict[str, tuple[Path, str]]:
    expected = {
        "heartbeat": (log_dir / f"amp_heartbeat_{aic}.jsonl", "amp.heartbeat"),
        "access": (log_dir / f"amp_access_{aic}.jsonl", "amp.access"),
    }
    return expected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log-dir", required=True, type=Path)
    parser.add_argument("--aic", required=True)
    parser.add_argument("--broker", default="49.232.42.120:19092")
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument(
        "--initial-position", choices=("end", "beginning"), default="end",
        help="Used only when a file has no checkpoint. 'end' avoids replaying history.",
    )
    args = parser.parse_args()

    args.log_dir = args.log_dir.resolve()
    args.state = args.state.resolve()
    args.state.parent.mkdir(parents=True, exist_ok=True)
    try:
        state = json.loads(args.state.read_text(encoding="utf-8"))
    except FileNotFoundError:
        state = {}
    except (OSError, ValueError) as exc:
        print(f"Invalid state file: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    producer = None
    while RUNNING:
        made_progress = False
        for name, (path, topic) in discover(args.log_dir, args.aic).items():
            if not path.is_file():
                continue
            stat = path.stat()
            entry = state.get(name)
            if not isinstance(entry, dict):
                offset = stat.st_size if args.initial_position == "end" else 0
                state[name] = {"offset": offset, "device": stat.st_dev, "inode": stat.st_ino}
                save_state(args.state, state)
                print(f"Initialized {name} at byte {offset}", flush=True)
                continue

            rotated = entry.get("device") != stat.st_dev or entry.get("inode") != stat.st_ino
            truncated = int(entry.get("offset", 0)) > stat.st_size
            if rotated or truncated:
                entry = {"offset": 0, "device": stat.st_dev, "inode": stat.st_ino}
                state[name] = entry
                save_state(args.state, state)

            offset = int(entry.get("offset", 0))
            with path.open("rb") as stream:
                stream.seek(offset)
                while RUNNING:
                    line_start = stream.tell()
                    line = stream.readline()
                    if not line:
                        break
                    if not line.endswith(b"\n"):
                        stream.seek(line_start)
                        break
                    payload = line.strip()
                    if not payload:
                        entry["offset"] = stream.tell()
                        save_state(args.state, state)
                        continue
                    try:
                        record = json.loads(payload)
                    except ValueError as exc:
                        print(f"Invalid JSON at {path}:{line_start}: {exc}", file=sys.stderr, flush=True)
                        return 3
                    try:
                        if producer is None:
                            producer = new_producer(args.broker)
                        metadata = producer.send(topic, value=payload).get(timeout=20)
                    except Exception as exc:
                        print(f"Kafka send failed for {topic}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
                        if producer is not None:
                            producer.close(timeout=1)
                            producer = None
                        time.sleep(5)
                        break
                    entry["offset"] = stream.tell()
                    entry["device"] = stat.st_dev
                    entry["inode"] = stat.st_ino
                    save_state(args.state, state)
                    made_progress = True
                    print(
                        f"sent topic={topic} partition={metadata.partition} offset={metadata.offset} "
                        f"log_id={record.get('log_id')}", flush=True
                    )
        if not made_progress:
            time.sleep(max(0.2, args.poll_seconds))

    if producer is not None:
        producer.flush(timeout=10)
        producer.close(timeout=5)
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    raise SystemExit(main())
