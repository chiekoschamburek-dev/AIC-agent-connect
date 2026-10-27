#!/usr/bin/env python3
"""AMP NDJSON journal → 梧桐平台 Kafka 转发器（可靠版 v2）。

针对 v1 的缺陷（2026-10-19 复盘）：
1. ``send()`` fire-and-forget，不检查 future → 超时/失败静默丢日志；
2. 送完立刻推进 offset，失败也不回退 → 丢数不可恢复；
3. 多 journal 文件只取第一个；
4. 无落盘补偿。

v2 行为：
- 优先 confluent-kafka（``acks=all`` + 幂等 + 重试），否则 kafka-python（``acks=all`` + ``send().get()``）；
- **每个文件逐条确认后才推进 offset**；任一失败立即停止该文件并保留 offset，下一轮重试；
- 失败记录落盘到 ``.amp-forwarder-dlq.jsonl``（含原 topic），下轮优先重投；
- ``--replay``：从 offset 0 重放全部历史（平台以 ``logId`` 幂等去重，规范 5.1.3）。

平台口径（不变）：明文 TCP、无 TLS/SASL、topic ``amp.<type>``；
access 的 ``correlationId`` 必须是 taskId，message 需 messageId+eventType。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

LOG_TYPES = ("heartbeat", "metrics", "access", "message", "system", "audit")
TOPIC_MAP = {t: os.environ.get(f"AMP_KAFKA_TOPIC_{t.upper()}", f"amp.{t}") for t in LOG_TYPES}


def find_journals(root: Path) -> dict[str, list[Path]]:
    """发现六类 journal（可多文件：amp_access_<AIC> / amp_access_partner_* / amp_access_leader_*）。"""
    found: dict[str, list[Path]] = {}
    if not root.exists():
        return found
    for log_type in LOG_TYPES:
        candidates = sorted(root.rglob(f"amp_{log_type}*.jsonl"))
        if candidates:
            found[log_type] = candidates
    return found


def load_state(path: Path) -> dict[str, int]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(path: Path, state: dict[str, int]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


class Producer:
    """统一封装：逐条确认送达，失败抛异常（由调用方保留 offset）。"""

    def __init__(self, bootstrap: str) -> None:
        self.bootstrap = bootstrap
        self.kind = ""
        self._p = None
        try:
            from confluent_kafka import Producer as CProducer  # type: ignore

            self._p = CProducer(
                {
                    "bootstrap.servers": bootstrap,
                    "acks": "all",
                    "enable.idempotence": True,
                    "retries": 10,
                    "retry.backoff.ms": 300,
                    "message.timeout.ms": 30000,
                    "linger.ms": 5,
                }
            )
            self.kind = "confluent"
            return
        except ImportError:
            pass
        try:
            from kafka import KafkaProducer  # type: ignore

            self._p = KafkaProducer(
                bootstrap_servers=bootstrap,
                acks="all",
                retries=10,
                linger_ms=5,
                max_block_ms=30000,
            )
            self.kind = "kafka-python"
            return
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "未安装 Kafka 客户端：pip install confluent-kafka（推荐）或 kafka-python"
            ) from exc

    def send_sync(self, topic: str, payload: bytes, timeout: float = 30.0) -> None:
        if self.kind == "confluent":
            from confluent_kafka import KafkaException  # type: ignore

            errors: list[str] = []

            def _cb(err, _msg) -> None:  # 回调在 poll/flush 时触发
                if err is not None:
                    errors.append(str(err))

            self._p.produce(topic, payload, callback=_cb)
            self._p.flush(timeout)
            if errors:
                raise RuntimeError(f"produce failed: {errors[0]}")
        else:
            future = self._p.send(topic, payload)
            future.get(timeout=timeout)  # 阻塞直到 broker ack；失败抛异常


def _dlq_append(path: Path, topic: str, record: str) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"topic": topic, "payload": record}, ensure_ascii=False) + "\n")


def _dlq_drain(path: Path, producer: Producer) -> int:
    """重投上次失败的记录；全部成功才清空 DLQ。"""
    if not path.exists():
        return 0
    pending: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            pending.append(json.loads(line))
    if not pending:
        return 0
    remaining: list[dict] = []
    for item in pending:
        try:
            producer.send_sync(item["topic"], item["payload"].encode("utf-8"))
        except Exception:
            remaining.append(item)
    path.write_text(
        "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in remaining), encoding="utf-8"
    )
    return len(pending) - len(remaining)


def forward_once(
    journals: dict[str, list[Path]],
    state: dict[str, int],
    *,
    producer: Producer | None,
    dlq: Path,
    dry_run: bool,
    max_per_file: int = 5000,
) -> tuple[int, int]:
    """返回 (成功条数, 失败条数)；失败时保留 offset 以便重试。"""
    sent = failed = 0
    for log_type, paths in journals.items():
        for path in paths:
            offset = state.get(str(path), 0)
            topic = TOPIC_MAP[log_type]
            count = 0
            with path.open("r", encoding="utf-8") as fh:
                fh.seek(offset)
                for line in fh:
                    if count >= max_per_file:
                        break
                    record = line.strip()
                    if not record:
                        offset += len(line.encode("utf-8"))
                        continue
                    try:
                        json.loads(record)  # 畸形行直接跳过
                    except json.JSONDecodeError:
                        offset += len(line.encode("utf-8"))
                        continue
                    if dry_run:
                        print(f"[dry-run] {path.name} -> {topic}")
                    else:
                        assert producer is not None
                        try:
                            producer.send_sync(topic, record.encode("utf-8"))
                        except Exception as exc:
                            failed += 1
                            _dlq_append(dlq, topic, record)
                            print(f"[warn] {path.name} 第 {count} 条发送失败（保留 offset 重试）: {exc}",
                                  file=sys.stderr)
                            # 不推进 offset，本轮该文件停止
                            state[str(path)] = offset
                            break
                    offset += len(line.encode("utf-8"))
                    count += 1
                    sent += 1
            state[str(path)] = offset
            if failed:
                break
    return sent, failed


def main() -> int:
    parser = argparse.ArgumentParser(description="AMP journal → 平台 Kafka 转发器（可靠版）")
    parser.add_argument("--journal-dir", default=".wit/wenji-aip-agent")
    parser.add_argument("--state", default=".amp-forwarder-state.json")
    parser.add_argument("--dlq", default=".amp-forwarder-dlq.jsonl")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--replay", action="store_true", help="从 offset 0 重放全部历史（logId 幂等）")
    parser.add_argument("--only", default="", help="只处理文件名匹配该子串的 journal（如 amp_access_partner）")
    args = parser.parse_args()

    root = Path(args.journal_dir)
    state_path = Path(args.state)
    dlq = Path(args.dlq)
    state = load_state(state_path)
    if args.replay:
        print("[replay] 将从 journal 起点全量重放（平台按 logId 幂等去重）")
        # 只重置将要处理的文件（配合 --only 精确回放）
        for log_type, paths in find_journals(root).items():
            for p in paths:
                if not args.only or args.only in p.name:
                    state.pop(str(p), None)

    producer: Producer | None = None
    if not args.dry_run:
        try:
            producer = Producer(os.environ.get("AMP_KAFKA_BOOTSTRAP", "wt.ioa.pub:19092"))
            print(f"[boot] producer={producer.kind} broker={producer.bootstrap}", flush=True)
        except RuntimeError as exc:
            print(f"[fatal] {exc}", file=sys.stderr)
            return 2

    while True:
        if producer is not None:
            drained = _dlq_drain(dlq, producer)
            if drained:
                print(f"[dlq] 重投成功 {drained} 条", flush=True)
        journals = find_journals(root)
        if args.only:
            journals = {k: [p for p in v if args.only in p.name] for k, v in journals.items()}
            journals = {k: v for k, v in journals.items() if v}
        if not journals:
            print(f"[warn] 未在 {root} 发现 amp_*.jsonl", file=sys.stderr)
        else:
            sent, failed = forward_once(
                journals, state, producer=producer, dlq=dlq, dry_run=args.dry_run
            )
            if sent:
                print(f"[ok] 送达 {sent} 条" + (f"，失败 {failed} 条（已入 DLQ）" if failed else ""), flush=True)
        save_state(state_path, state)
        if args.once:
            break
        time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
