"""Read-only three-channel risk detector for ACPs event audit data."""

from __future__ import annotations
import argparse, base64, json, math, re, sqlite3, statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

B64 = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{80,}={0,2}(?![A-Za-z0-9+/=])")
ORDER = {"accepted": 0, "working": 1, "awaiting-input": 2,
         "awaiting-completion": 2, "completed": 3, "failed": 3,
         "canceled": 3, "rejected": 3}
TERMINAL = {"completed", "failed", "canceled", "rejected"}


@dataclass(frozen=True)
class Event:
    task_ref: str
    event_type: str
    created_at: datetime
    payload: Any
    event_seq: int | None = None
    message_id: str | None = None


@dataclass(frozen=True)
class Finding:
    channel: str
    rule: str
    score: int
    detail: str


@dataclass
class RiskReport:
    task_ref: str
    event_count: int
    score: int
    level: str
    findings: list[Finding] = field(default_factory=list)
    observed_from: str | None = None
    observed_to: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TaskStart:
    task_ref: str
    direction: str
    local_aic: str
    remote_aic: str
    created_at: datetime


@dataclass
class PeerTimingReport:
    direction: str
    local_aic: str
    remote_aic: str
    task_count: int
    session_count: int
    analyzable_sessions: int
    score: int
    level: str
    findings: list[Finding] = field(default_factory=list)
    operational_observations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _time(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, (int, float)):
        result = datetime.fromtimestamp(float(value), tz=timezone.utc)
    else:
        text = str(value or "").strip()
        result = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    return (result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result).astimezone(timezone.utc)


def _payload(value: Any) -> Any:
    if isinstance(value, (dict, list, int, float, bool)) or value is None:
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return str(value)


def event_from_mapping(row: Mapping[str, Any], fallback_seq: int = 0) -> Event:
    timestamp = row.get("created_at", row.get("sentAt", row.get("timestamp")))
    if not timestamp:
        raise ValueError("event is missing created_at/sentAt/timestamp")
    try:
        seq = int(row.get("event_seq", row.get("seq", fallback_seq)))
    except (TypeError, ValueError):
        seq = fallback_seq
    return Event(
        str(row.get("task_ref") or row.get("task_id") or "unknown-task"),
        str(row.get("event_type", row.get("type", "unknown"))),
        _time(timestamp),
        _payload(row.get("event_data", row.get("payload", row.get("data", {})))),
        seq,
        str(row["message_id"]) if row.get("message_id") else None,
    )


def load_jsonl(path: str | Path) -> list[Event]:
    result = []
    with Path(path).open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise ValueError("JSON value is not an object")
                result.append(event_from_mapping(item, number))
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"invalid event at {path}:{number}: {exc}") from exc
    return result


def load_sqlite(path: str | Path, *, task_ref: str | None = None,
                limit: int = 1000) -> list[Event]:
    """Open SQLite in read-only mode; tolerate both task event schemas."""
    db = Path(path).resolve()
    if not db.is_file():
        raise FileNotFoundError(db)
    con = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in con.execute("PRAGMA table_info(aip_task_events)")}
        task_col = "task_ref" if "task_ref" in columns else "task_id" if "task_id" in columns else None
        data_col = "event_data" if "event_data" in columns else "payload" if "payload" in columns else None
        if not task_col or not data_col or "created_at" not in columns:
            raise ValueError(f"unsupported aip_task_events schema: {sorted(columns)}")
        selected = [task_col, "created_at", data_col] + [
            name for name in ("event_seq", "message_id", "event_type") if name in columns
        ]
        query, params = f"SELECT {', '.join(selected)} FROM aip_task_events", []
        if task_ref:
            query += f" WHERE {task_col} = ?"
            params.append(task_ref)
        query += " ORDER BY created_at DESC" + (", event_seq DESC" if "event_seq" in columns else "") + " LIMIT ?"
        params.append(max(1, int(limit)))
        rows = [dict(row) for row in con.execute(query, params)]
    finally:
        con.close()
    rows.reverse()
    return [event_from_mapping(row, n) for n, row in enumerate(rows, 1)]


def _text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _entropy(data: bytes) -> float:
    counts, size = Counter(data), len(data)
    return -sum((n / size) * math.log2(n / size) for n in counts.values()) if size else 0.0


def _state(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    status = payload.get("status")
    if isinstance(status, dict) and status.get("state"):
        return str(status["state"])
    return str(payload["state"]) if payload.get("state") else None


def _storage(events: Sequence[Event]) -> list[Finding]:
    out, texts = [], [_text(event.payload) for event in events]
    largest = max((len(text.encode()) for text in texts), default=0)
    if largest > 64_000:
        out.append(Finding("storage", "oversized_payload", 25 if largest > 256_000 else 12,
                           f"largest payload is {largest} bytes"))
    blobs, high = 0, 0.0
    for text in texts:
        for match in B64.finditer(text):
            try:
                decoded = base64.b64decode(match.group(), validate=True)
            except (ValueError, base64.binascii.Error):
                continue
            entropy = _entropy(decoded)
            high = max(high, entropy)
            blobs += int(len(decoded) >= 60 and entropy >= 4.5)
    if blobs:
        out.append(Finding("storage", "encoded_high_entropy_blob", min(30, 16 + 4 * (blobs - 1)),
                           f"found {blobs} encoded blob(s), max entropy {high:.2f} bits/byte"))
    duplicates = sum(n - 1 for n in Counter(texts).values() if n > 1)
    if len(events) >= 6 and duplicates >= max(3, len(events) // 2):
        out.append(Finding("storage", "repeated_payload", 10, f"{duplicates} repeated payloads"))
    return out


def _timing(events: Sequence[Event]) -> list[Finding]:
    ordered = sorted(events, key=lambda x: x.created_at)
    intervals = [(b.created_at - a.created_at).total_seconds()
                 for a, b in zip(ordered, ordered[1:])
                 if (b.created_at - a.created_at).total_seconds() > 0]
    if len(intervals) < 5:
        return []
    out, mean = [], statistics.fmean(intervals)
    cv = statistics.pstdev(intervals) / mean if mean else 0
    if mean >= .2 and cv <= .08:
        out.append(Finding("timing", "over_regular_intervals", 20 if cv <= .03 else 10,
                           f"{len(intervals)} intervals have CV {cv:.3f}"))
    if len(intervals) >= 6:
        even, odd = intervals[::2], intervals[1::2]
        em, om = statistics.fmean(even), statistics.fmean(odd)
        separation = abs(em - om) / mean if mean else 0
        within = statistics.fmean([
            statistics.pstdev(even) / em if len(even) > 1 and em else 0,
            statistics.pstdev(odd) / om if len(odd) > 1 and om else 0])
        if separation >= .35 and within <= .12:
            out.append(Finding("timing", "alternating_interval_pattern", 25,
                               f"alternating groups differ by {separation:.0%}"))
    return out


def _behavior(events: Sequence[Event]) -> list[Finding]:
    ordered = sorted(events, key=lambda x: (x.created_at, x.event_seq or 0))
    states = [state for state in (_state(event.payload) for event in ordered) if state]
    bad = []
    for before, after in zip(states, states[1:]):
        if ((before in TERMINAL and after != before) or
                (before in ORDER and after in ORDER and ORDER[after] < ORDER[before])):
            bad.append(f"{before}->{after}")
    out = []
    if bad:
        out.append(Finding("behavior", "invalid_state_transition",
                           min(30, 18 + 4 * (len(bad) - 1)), ", ".join(bad[:4])))
    types = [event.event_type for event in ordered]
    switches = sum(a != b for a, b in zip(types, types[1:]))
    if len(types) >= 10 and switches / (len(types) - 1) >= .9 and len(set(types)) <= 3:
        out.append(Finding("behavior", "repetitive_action_switching", 15,
                           f"event type changes on {switches}/{len(types)-1} transitions"))
    return out


def analyze_task(events: Sequence[Event]) -> RiskReport:
    if not events:
        raise ValueError("at least one event is required")
    if len({event.task_ref for event in events}) != 1:
        raise ValueError("analyze_task expects exactly one task")
    ordered = sorted(events, key=lambda x: x.created_at)
    findings = _storage(ordered) + _timing(ordered) + _behavior(ordered)
    score = min(100, sum(item.score for item in findings))
    level = "HIGH" if score >= 60 else "MEDIUM" if score >= 30 else "LOW"
    return RiskReport(ordered[0].task_ref, len(ordered), score, level, findings,
                      ordered[0].created_at.isoformat(), ordered[-1].created_at.isoformat())


def analyze_events(events: Iterable[Event]) -> list[RiskReport]:
    grouped: dict[str, list[Event]] = defaultdict(list)
    for event in events:
        grouped[event.task_ref].append(event)
    return sorted((analyze_task(items) for items in grouped.values()),
                  key=lambda report: (-report.score, report.task_ref))


def load_peer_tasks_sqlite(path: str | Path, *, limit: int = 1000) -> list[TaskStart]:
    """Read task starts and peer identities from aip_tasks in read-only mode."""
    db = Path(path).resolve()
    if not db.is_file():
        raise FileNotFoundError(db)
    con = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    try:
        columns = {row[1] for row in con.execute("PRAGMA table_info(aip_tasks)")}
        required = {"task_ref", "direction", "local_aic", "remote_aic", "created_at"}
        if not required.issubset(columns):
            raise ValueError(f"unsupported aip_tasks schema; missing: {sorted(required - columns)}")
        rows = con.execute(
            """SELECT task_ref, direction, local_aic, remote_aic, created_at
               FROM aip_tasks
               WHERE created_at IS NOT NULL
                 AND local_aic IS NOT NULL
                 AND remote_aic IS NOT NULL
               ORDER BY created_at DESC
               LIMIT ?""",
            (max(1, int(limit)),),
        ).fetchall()
    finally:
        con.close()
    rows.reverse()
    return [
        TaskStart(str(task_ref), str(direction), str(local_aic),
                  str(remote_aic), _time(created_at))
        for task_ref, direction, local_aic, remote_aic, created_at in rows
    ]


def _split_sessions(tasks: Sequence[TaskStart], gap_seconds: float) -> list[list[TaskStart]]:
    ordered = sorted(tasks, key=lambda item: item.created_at)
    if not ordered:
        return []
    sessions = [[ordered[0]]]
    for task in ordered[1:]:
        gap = (task.created_at - sessions[-1][-1].created_at).total_seconds()
        if gap > gap_seconds:
            sessions.append([task])
        else:
            sessions[-1].append(task)
    return sessions


def analyze_peer_timing(
    tasks: Iterable[TaskStart],
    *,
    session_gap_seconds: float = 3600.0,
    min_observation_intervals: int = 4,
    min_scored_intervals: int = 6,
    regular_cv_threshold: float = 0.10,
) -> list[PeerTimingReport]:
    """Analyze cross-task timing per direction/local/remote peer tuple.

    Sessions with 4-5 intervals are observable but never scored.  At least six
    intervals are required before timing regularity contributes risk.
    """
    grouped: dict[tuple[str, str, str], list[TaskStart]] = defaultdict(list)
    for task in tasks:
        grouped[(task.direction, task.local_aic, task.remote_aic)].append(task)

    reports: list[PeerTimingReport] = []
    for (direction, local_aic, remote_aic), peer_tasks in grouped.items():
        sessions = _split_sessions(peer_tasks, session_gap_seconds)
        findings: list[Finding] = []
        observations: list[str] = []
        analyzable = 0
        has_observable_session = False

        for index, session in enumerate(sessions, 1):
            intervals = [
                (right.created_at - left.created_at).total_seconds()
                for left, right in zip(session, session[1:])
                if right.created_at > left.created_at
            ]
            burst_count = sum(interval < 1.0 for interval in intervals)
            if burst_count:
                observations.append(
                    f"session {index}: {burst_count} sub-second interval(s); "
                    "classified as operational_burst, not security risk"
                )
            analysis_intervals = [value for value in intervals if value >= 1.0]
            if len(analysis_intervals) < min_observation_intervals:
                continue
            has_observable_session = True
            analyzable += 1
            if len(analysis_intervals) < min_scored_intervals:
                observations.append(
                    f"session {index}: {len(analysis_intervals)} intervals observed "
                    f"but not scored (minimum {min_scored_intervals})"
                )
                continue

            mean = statistics.fmean(analysis_intervals)
            cv = statistics.pstdev(analysis_intervals) / mean if mean else 0.0
            if mean >= 0.2 and cv <= regular_cv_threshold:
                findings.append(Finding(
                    "timing", "peer_over_regular_intervals", 20,
                    f"session {index}: {len(analysis_intervals)} intervals, CV {cv:.3f}",
                ))

            even, odd = analysis_intervals[::2], analysis_intervals[1::2]
            if len(even) >= 3 and len(odd) >= 3:
                even_mean, odd_mean = statistics.fmean(even), statistics.fmean(odd)
                separation = abs(even_mean - odd_mean) / mean if mean else 0.0
                within = statistics.fmean([
                    statistics.pstdev(even) / even_mean if even_mean else 0.0,
                    statistics.pstdev(odd) / odd_mean if odd_mean else 0.0,
                ])
                if separation >= 0.35 and within <= 0.12:
                    findings.append(Finding(
                        "timing", "peer_alternating_interval_pattern", 25,
                        f"session {index}: alternating groups differ by "
                        f"{separation:.0%}, within-group CV {within:.3f}",
                    ))

        score = min(100, sum(item.score for item in findings))
        if not has_observable_session:
            level = "INSUFFICIENT_DATA"
        else:
            level = "HIGH" if score >= 40 else "MEDIUM" if score >= 20 else "LOW"
        reports.append(PeerTimingReport(
            direction, local_aic, remote_aic, len(peer_tasks), len(sessions),
            analyzable, score, level, findings, observations,
        ))

    return sorted(reports, key=lambda item: (-item.score, -item.task_count, item.remote_aic))


def _print_peer_report(report: PeerTimingReport) -> None:
    print(f"Peer: {report.remote_aic}")
    print(f"Direction: {report.direction}")
    print(f"Timing risk: {report.level} ({report.score}/100)")
    print(
        f"Tasks: {report.task_count}; sessions: {report.session_count}; "
        f"analyzable sessions: {report.analyzable_sessions}"
    )
    print("Findings:" if report.findings else "Findings: none")
    for item in report.findings:
        print(f"  - {item.rule} (+{item.score}): {item.detail}")
    if report.operational_observations:
        print("Operational observations:")
        for observation in report.operational_observations:
            print(f"  - {observation}")
    print()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only event-channel risk detector")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path)
    source.add_argument("--db", type=Path)
    parser.add_argument("--task-ref")
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument(
        "--peer-timing", action="store_true",
        help="analyze cross-task timing grouped by direction/local_aic/remote_aic",
    )
    parser.add_argument("--session-gap", type=float, default=3600.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.peer_timing:
        if not args.db:
            parser.error("--peer-timing requires --db")
        reports = analyze_peer_timing(
            load_peer_tasks_sqlite(args.db, limit=args.limit),
            session_gap_seconds=args.session_gap,
        )
        if args.json:
            print(json.dumps([report.to_dict() for report in reports],
                             ensure_ascii=False, indent=2))
        else:
            for report in reports:
                _print_peer_report(report)
        return 0

    events = load_jsonl(args.input) if args.input else load_sqlite(
        args.db, task_ref=args.task_ref, limit=args.limit)
    reports = analyze_events(events)
    if args.json:
        print(json.dumps([report.to_dict() for report in reports], ensure_ascii=False, indent=2))
    else:
        for report in reports:
            print(f"Task: {report.task_ref}\nRisk: {report.level} ({report.score}/100)\nEvents: {report.event_count}")
            print("Findings:" if report.findings else "Findings: none")
            for item in report.findings:
                print(f"  - [{item.channel}] {item.rule} (+{item.score}): {item.detail}")
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
