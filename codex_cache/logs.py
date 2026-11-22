"""Read Codex usage events without retaining conversation text."""

from __future__ import annotations

import codecs
import json
import os
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

MARKERS = (
    b'"session_meta"', b'"turn_context"', b'"token_usage_record"', b'"token_count"',
    b'"compacted"', b'"turn.completed"', b'"turn.failed"',
)
TEXT_MARKERS = tuple(marker.decode("ascii") for marker in MARKERS)


@dataclass(frozen=True)
class Sample:
    input_tokens: int
    cached: int
    output: int | None
    cache_write: int | None
    phase: str
    scope: str
    model: str
    version: str
    at: datetime | None
    response_id: str | None

    @property
    def fresh(self) -> int:
        return self.input_tokens - self.cached


def mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def identifier(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def label(value: object) -> str:
    text = identifier(value)
    if text and len(text) <= 80 and all(
        ch.isascii() and (ch.isalnum() or ch in "._-") for ch in text
    ):
        return text
    return "unknown"


def count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def date_time(value: object) -> datetime | None:
    if isinstance(value, str):
        try:
            timestamp = datetime.fromisoformat(value)
            return timestamp if timestamp.tzinfo is not None else None
        except ValueError:
            pass
    return None


def read_sample(
    usage: dict, scope: str, model: str, version: str,
    at: datetime | None, response_id: str | None, notes: Counter,
) -> Sample | None:
    inputs = count(usage.get("input_tokens"))
    cached = count(usage.get("cached_input_tokens"))
    if inputs is None or cached is None or cached > inputs:
        notes["invalid_or_missing_usage"] += 1
        return None
    if inputs == 0:
        return None
    output = count(usage.get("output_tokens"))
    writes = count(usage.get("cache_write_input_tokens"))
    if writes is not None and writes > inputs - cached:
        notes["invalid_cache_write_count"] += 1
        writes = None
    return Sample(inputs, cached, output, writes, "", scope, model, version, at, response_id)


def log_events(path: Path, notes: Counter):
    with path.open("rb") as stream:
        prefix = stream.read(3)
        stream.seek(0)
        if prefix.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            # Windows PowerShell 5.1 can redirect native output as UTF-16.
            with codecs.getreader("utf-16")(stream, errors="replace") as text:
                lines = (line for line in text if any(marker in line for marker in TEXT_MARKERS))
                yield from decode_events(lines, notes)
        else:
            lines = (line for line in stream if any(marker in line for marker in MARKERS))
            yield from decode_events(lines, notes)


def decode_events(lines, notes: Counter):
    for line in lines:
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            notes["invalid_usage_related_lines"] += 1
            continue
        if isinstance(event, dict):
            yield event


def phase_of(sample: Sample, previous: Sample | None, compacted: bool) -> str:
    if compacted:
        return "after_compaction"
    if previous is None:
        return "first_observed"
    if sample.model != previous.model and "unknown" not in (sample.model, previous.model):
        return "model_changed"
    if sample.at and previous.at and sample.at >= previous.at:
        return "after_idle" if (sample.at - previous.at).total_seconds() >= 1800 else "warm_follow_up"
    return "follow_up_unknown_gap"


def read_log(path: Path, workspace: Path | None, notes: Counter) -> list[Sample]:
    """Prefer per-response usage records over legacy usage events within a turn."""
    selected = []
    modern = []
    legacy = []
    last_total = None
    previous = None
    window = previous_window = 0
    model = version = "unknown"
    allowed = workspace is None

    def flush():
        nonlocal previous, previous_window
        for item, window_at_sample in modern or legacy:
            sample = replace(item, phase=phase_of(item, previous, window_at_sample != previous_window))
            selected.append(sample)
            previous, previous_window = sample, window_at_sample
        modern.clear()
        legacy.clear()

    for event in log_events(path, notes):
        kind = event.get("type")
        payload = mapping(event.get("payload"))
        if kind == "session_meta":
            version = label(payload.get("cli_version"))
            model = label(payload.get("model"))
            cwd = identifier(payload.get("cwd"))
            if workspace is not None:
                allowed = bool(cwd and os.path.normcase(os.path.normpath(cwd)) == os.path.normcase(str(workspace)))
        if not allowed:
            continue
        if kind == "turn_context":
            flush()
            model = label(payload.get("model"))
            continue
        if kind == "compacted":
            window += 1
            continue
        if kind == "turn.failed":
            notes["failed_cli_turns"] += 1
            continue

        at = date_time(event.get("timestamp"))
        if kind == "turn.completed":
            sample = read_sample(mapping(event.get("usage")), "turn", model, version, at, None, notes)
            if sample:
                selected.append(replace(sample, phase="cli_turn"))
        elif kind == "token_usage_record":
            sample = read_sample(
                mapping(payload.get("usage")), "request", model, version, at,
                identifier(payload.get("response_id")), notes,
            )
            if sample:
                modern.append((sample, window))
        elif kind == "event_msg" and payload.get("type") == "token_count":
            info = mapping(payload.get("info"))
            total = mapping(info.get("total_token_usage"))
            usage = mapping(info.get("last_token_usage"))
            if not total or total == last_total:
                continue
            last_total = total
            if usage:
                sample = read_sample(usage, "request", model, version, at, None, notes)
                if sample:
                    legacy.append((sample, window))

    flush()
    return selected


def collect(
    paths: list[Path], notes: Counter, workspace: Path | None = None,
    since: datetime | None = None,
) -> list[Sample]:
    samples = []
    seen_files = set()
    seen_responses = set()
    for path in paths:
        try:
            resolved = path.resolve()
            if resolved in seen_files:
                continue
            seen_files.add(resolved)
            records = read_log(resolved, workspace, notes)
        except (OSError, ValueError):
            notes["unreadable_files"] += 1
            continue
        for record in records:
            if since and (record.at is None or record.at < since):
                if record.at is None:
                    notes["missing_timestamp"] += 1
                continue
            if record.response_id:
                if record.response_id in seen_responses:
                    notes["duplicate_responses"] += 1
                    continue
                seen_responses.add(record.response_id)
            samples.append(record)
    return samples


def recent_rollouts(home: Path, since: datetime) -> list[Path]:
    paths = []
    cutoff = since.timestamp()
    for path in (home / "sessions").rglob("rollout-*.jsonl"):
        try:
            if path.is_file() and path.stat().st_mtime >= cutoff:
                paths.append(path)
        except OSError:
            continue
    return sorted(paths)
