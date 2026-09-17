"""Scoped parsing of registered native task transcripts.

The transcript format is intentionally treated as unstable input.  Parsers
retain native identifiers and explicit gaps; they never infer completion from
final prose or estimate tokens from text.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TranscriptPage:
    cursor: int
    lifecycle: tuple[Mapping[str, Any], ...]
    usage: tuple[Mapping[str, Any], ...]
    gaps: tuple[Mapping[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "cursor": self.cursor,
            "lifecycle": [dict(item) for item in self.lifecycle],
            "usage": [dict(item) for item in self.usage],
            "gaps": [dict(item) for item in self.gaps],
        }


LIFECYCLE_TYPES = {
    "task_started",
    "task_complete",
    "task_completed",
    "turn_started",
    "turn_complete",
    "turn_completed",
    "turn_interrupted",
    "interrupted",
    "turn_context",
}


def read_transcript(path: Path, cursor: int = 0) -> TranscriptPage:
    if cursor < 0:
        raise ValueError("transcript cursor cannot be negative")
    try:
        with path.open("rb") as stream:
            stream.seek(cursor)
            data = stream.read()
    except OSError as error:
        return TranscriptPage(
            cursor=cursor,
            lifecycle=(),
            usage=(),
            gaps=({"kind": "transcript_unavailable", "message": str(error)},),
        )
    lifecycle: list[Mapping[str, Any]] = []
    usage: list[Mapping[str, Any]] = []
    gaps: list[Mapping[str, Any]] = []
    consumed = 0
    lines = data.splitlines(keepends=True)
    for index, raw in enumerate(lines, start=1):
        complete = raw.endswith((b"\n", b"\r"))
        if not complete and index == len(lines):
            gaps.append(
                {
                    "kind": "incomplete_trailing_line",
                    "offset": cursor + consumed,
                }
            )
            break
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            gaps.append(
                {
                    "kind": "invalid_json",
                    "offset": cursor + consumed,
                    "message": str(error),
                }
            )
            consumed += len(raw)
            continue
        consumed += len(raw)
        if not isinstance(value, Mapping):
            continue
        event = _event(value)
        event_type = str(event.get("type") or value.get("type") or "")
        if event_type in LIFECYCLE_TYPES:
            lifecycle.append(_lifecycle(event_type, value, event))
        if event_type == "token_usage_record" or "token_usage" in event_type:
            usage.append(_usage(value, event))
    return TranscriptPage(
        cursor=cursor + consumed,
        lifecycle=tuple(lifecycle),
        usage=tuple(usage),
        gaps=tuple(gaps),
    )


def _event(value: Mapping[str, Any]) -> Mapping[str, Any]:
    for key in ("event", "payload", "data"):
        candidate = value.get(key)
        if isinstance(candidate, Mapping):
            return candidate
    return value


def _first(*values: Any) -> Any:
    return next((value for value in values if value is not None), None)


def _lifecycle(
    event_type: str, root: Mapping[str, Any], event: Mapping[str, Any]
) -> Mapping[str, Any]:
    return {
        "type": event_type,
        "event_id": _first(event.get("event_id"), event.get("id"), root.get("id")),
        "task_id": _first(
            event.get("task_id"), event.get("thread_id"), root.get("thread_id")
        ),
        "host_id": _first(event.get("host_id"), root.get("host_id")),
        "turn_id": _first(
            event.get("turn_id"), event.get("turnId"), root.get("turn_id")
        ),
        "response_id": _first(event.get("response_id"), event.get("responseId")),
        "time": _first(
            event.get("time"), event.get("timestamp"), root.get("timestamp")
        ),
    }


def _usage(root: Mapping[str, Any], event: Mapping[str, Any]) -> Mapping[str, Any]:
    counters = event.get("usage")
    values = counters if isinstance(counters, Mapping) else event
    return {
        "event_id": _first(event.get("event_id"), event.get("id"), root.get("id")),
        "task_id": _first(
            event.get("task_id"), event.get("thread_id"), root.get("thread_id")
        ),
        "turn_id": _first(
            event.get("turn_id"), event.get("turnId"), root.get("turn_id")
        ),
        "response_id": _first(event.get("response_id"), event.get("responseId")),
        "model": _first(event.get("model"), root.get("model")),
        "service_tier": _first(event.get("service_tier"), root.get("service_tier")),
        "input_tokens": _counter(values, "input_tokens", "input"),
        "cached_input_tokens": _counter(values, "cached_input_tokens", "cached_input"),
        "cache_write_tokens": _counter(values, "cache_write_tokens", "cache_write"),
        "output_tokens": _counter(values, "output_tokens", "output"),
        "reasoning_tokens": _counter(values, "reasoning_tokens", "reasoning"),
        "cumulative": bool(event.get("cumulative", False)),
    }


def _counter(values: Mapping[str, Any], *names: str) -> int | None:
    for name in names:
        value = values.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None
