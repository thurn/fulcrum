"""Scoped parsing of registered native task transcripts.

The transcript format is intentionally treated as unstable input.  Parsers
retain native identifiers and explicit gaps; they never infer completion from
final prose or estimate tokens from text.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TranscriptPage:
    cursor: int
    lifecycle: tuple[Mapping[str, Any], ...]
    usage: tuple[Mapping[str, Any], ...]
    timings: tuple[Mapping[str, Any], ...]
    timing_state: Mapping[str, Any]
    gaps: tuple[Mapping[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "cursor": self.cursor,
            "lifecycle": [dict(item) for item in self.lifecycle],
            "usage": [dict(item) for item in self.usage],
            "timings": [dict(item) for item in self.timings],
            "timing_state": dict(self.timing_state),
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
    "turn_aborted",
    "interrupted",
    "turn_context",
}

TERMINAL_LIFECYCLE_TYPES = {
    "task_complete",
    "task_completed",
    "turn_complete",
    "turn_completed",
    "turn_interrupted",
    "turn_aborted",
    "interrupted",
}


def read_transcript(
    path: Path,
    cursor: int = 0,
    timing_state: Mapping[str, Any] | None = None,
) -> TranscriptPage:
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
            timings=(),
            timing_state=dict(timing_state or {}),
            gaps=({"kind": "transcript_unavailable", "message": str(error)},),
        )
    lifecycle: list[Mapping[str, Any]] = []
    usage: list[Mapping[str, Any]] = []
    timings: list[Mapping[str, Any]] = []
    gaps: list[Mapping[str, Any]] = []
    state = dict(timing_state or {})
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
            observed_lifecycle = _lifecycle(event_type, value, event)
            lifecycle.append(observed_lifecycle)
            if event_type in {"task_started", "turn_started"}:
                _start_response(state, value, event)
        elif event_type.startswith(("task_", "turn_")):
            gaps.append(
                {
                    "kind": "unrecognized_lifecycle_event",
                    "event_type": event_type,
                    "turn_id": _first(
                        event.get("turn_id"),
                        event.get("turnId"),
                        value.get("turn_id"),
                    ),
                    "offset": cursor + consumed - len(raw),
                }
            )
        if event_type in {"custom_tool_call_output", "function_call_output"}:
            _start_response(state, value, event)
        if event_type == "item_completed":
            timing = _item_timing(value, event, state)
            if timing is not None:
                timings.append(timing)
        if event_type == "token_usage_record" or "token_usage" in event_type:
            observed_usage = _usage(value, event)
            usage.append(observed_usage)
            timing = _response_timing(value, event, observed_usage, state)
            if timing is not None:
                timings.append(timing)
    return TranscriptPage(
        cursor=cursor + consumed,
        lifecycle=tuple(lifecycle),
        usage=tuple(usage),
        timings=tuple(timings),
        timing_state=state,
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
        "event_id": _first(
            event.get("event_id"),
            event.get("id"),
            root.get("id"),
            root.get("ordinal"),
        ),
        "task_id": _first(
            event.get("task_id"), event.get("thread_id"), root.get("thread_id")
        ),
        "host_id": _first(event.get("host_id"), root.get("host_id")),
        "turn_id": _first(
            event.get("turn_id"), event.get("turnId"), root.get("turn_id")
        ),
        "response_id": _first(event.get("response_id"), event.get("responseId")),
        "reason": event.get("reason"),
        "model": _first(event.get("model"), root.get("model")),
        "time": _first(
            event.get("time"), event.get("timestamp"), root.get("timestamp")
        ),
    }


def _usage(root: Mapping[str, Any], event: Mapping[str, Any]) -> Mapping[str, Any]:
    counters = event.get("usage")
    values = counters if isinstance(counters, Mapping) else event
    return {
        "event_id": _first(
            event.get("event_id"),
            event.get("id"),
            root.get("id"),
            root.get("ordinal"),
        ),
        "task_id": _first(
            event.get("task_id"), event.get("thread_id"), root.get("thread_id")
        ),
        "turn_id": _first(
            event.get("turn_id"), event.get("turnId"), root.get("turn_id")
        ),
        "response_id": _first(event.get("response_id"), event.get("responseId")),
        "model": _first(event.get("model"), root.get("model")),
        "service_tier": _first(event.get("service_tier"), root.get("service_tier")),
        "time": _first(
            event.get("time"), event.get("timestamp"), root.get("timestamp")
        ),
        "input_tokens": _counter(values, "input_tokens", "input"),
        "cached_input_tokens": _counter(values, "cached_input_tokens", "cached_input"),
        "cache_write_tokens": _counter(
            values,
            "cache_write_input_tokens",
            "cache_write_tokens",
            "cache_write",
        ),
        "output_tokens": _counter(values, "output_tokens", "output"),
        "reasoning_tokens": _counter(
            values,
            "reasoning_output_tokens",
            "reasoning_tokens",
            "reasoning",
        ),
        "cumulative": bool(event.get("cumulative", False)),
    }


def _counter(values: Mapping[str, Any], *names: str) -> int | None:
    for name in names:
        value = values.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def _start_response(
    state: dict[str, Any], root: Mapping[str, Any], event: Mapping[str, Any]
) -> None:
    started_at_ms = _epoch_ms(
        _first(event.get("time"), event.get("timestamp"), root.get("timestamp"))
    )
    if started_at_ms is None:
        return
    state["pending_response_started_at_ms"] = started_at_ms
    state["pending_response_parent_span_id"] = state.get("last_tool_span_id")


def _response_timing(
    root: Mapping[str, Any],
    event: Mapping[str, Any],
    usage: Mapping[str, Any],
    state: dict[str, Any],
) -> Mapping[str, Any] | None:
    started_at_ms = state.get("pending_response_started_at_ms")
    completed_at_ms = _epoch_ms(
        _first(event.get("time"), event.get("timestamp"), root.get("timestamp"))
    )
    response_id = usage.get("response_id")
    if (
        not isinstance(started_at_ms, (int, float))
        or completed_at_ms is None
        or not response_id
    ):
        return None
    correlation_id = _correlation_id(usage.get("task_id"), usage.get("turn_id"))
    span_id = f"{correlation_id}:response:{response_id}"
    timing = {
        "kind": "model_response",
        "item_type": "ModelResponse",
        "task_id": usage.get("task_id"),
        "turn_id": usage.get("turn_id"),
        "response_id": response_id,
        "time": _iso_ms(completed_at_ms),
        "started_at": _iso_ms(float(started_at_ms)),
        "completed_at": _iso_ms(completed_at_ms),
        "duration_ms": max(0.0, completed_at_ms - float(started_at_ms)),
        "span_id": span_id,
        "parent_span_id": state.get("pending_response_parent_span_id"),
        "correlation_id": correlation_id,
        "outcome": "completed",
    }
    state["last_response_span_id"] = span_id
    state.pop("pending_response_started_at_ms", None)
    state.pop("pending_response_parent_span_id", None)
    return timing


def _item_timing(
    root: Mapping[str, Any], event: Mapping[str, Any], state: dict[str, Any]
) -> Mapping[str, Any] | None:
    item = event.get("item")
    if not isinstance(item, Mapping):
        return None
    item_type = str(item.get("type") or "")
    kind = {
        "CommandExecution": "command_execution",
        "McpToolCall": "mcp_tool",
        "FileChange": "file_change",
    }.get(item_type)
    if kind is None:
        return None
    started_at_ms = _number(event.get("started_at_ms"))
    completed_at_ms = _number(event.get("completed_at_ms"))
    if completed_at_ms is None:
        completed_at_ms = _epoch_ms(root.get("timestamp"))
    duration_ms = _native_duration_ms(item)
    if (
        duration_ms is None
        and started_at_ms is not None
        and completed_at_ms is not None
    ):
        duration_ms = max(0.0, completed_at_ms - started_at_ms)
    task_id = _first(
        event.get("task_id"), event.get("thread_id"), root.get("thread_id")
    )
    turn_id = _first(event.get("turn_id"), event.get("turnId"), root.get("turn_id"))
    correlation_id = _correlation_id(task_id, turn_id)
    item_id = _first(item.get("id"), root.get("id"), root.get("ordinal"), "unknown")
    span_id = f"{correlation_id}:item:{item_id}"
    timing = {
        "kind": kind,
        "item_type": item_type,
        "task_id": task_id,
        "turn_id": turn_id,
        "time": _iso_ms(completed_at_ms) if completed_at_ms is not None else None,
        "started_at": _iso_ms(started_at_ms) if started_at_ms is not None else None,
        "completed_at": (
            _iso_ms(completed_at_ms) if completed_at_ms is not None else None
        ),
        "duration_ms": duration_ms,
        "span_id": span_id,
        "parent_span_id": state.get("last_response_span_id"),
        "correlation_id": correlation_id,
        "outcome": str(item.get("status") or "completed"),
    }
    if kind == "mcp_tool":
        timing = {
            **timing,
            "server": item.get("server"),
            "tool": item.get("tool"),
        }
    state["last_tool_span_id"] = span_id
    return timing


def _native_duration_ms(item: Mapping[str, Any]) -> float | None:
    duration = item.get("duration")
    if not isinstance(duration, Mapping):
        return None
    seconds = _number(duration.get("secs"))
    nanos = _number(duration.get("nanos"))
    if seconds is None and nanos is None:
        return None
    return (seconds or 0.0) * 1000.0 + (nanos or 0.0) / 1_000_000.0


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _epoch_ms(value: Any) -> float | None:
    numeric = _number(value)
    if numeric is not None:
        return numeric * 1000.0 if numeric < 10_000_000_000 else numeric
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp() * 1000.0


def _iso_ms(value: float) -> str:
    return (
        datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _correlation_id(task_id: Any, turn_id: Any) -> str:
    return f"{task_id or 'unknown'}:{turn_id or 'unknown'}"
