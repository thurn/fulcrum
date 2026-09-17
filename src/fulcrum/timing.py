"""Opt-in, correlated performance spans with negligible disabled overhead.

The timing stream intentionally contains stage names and process metadata only.
It never records requests, paths, descriptions, command arguments, or results.
"""

from __future__ import annotations

import contextvars
from functools import wraps
import inspect
import json
import os
import re
import time
import uuid
from collections.abc import Callable
from types import TracebackType
from typing import Any, TypeVar, cast

F = TypeVar("F", bound=Callable[..., Any])
_active_span: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "fulcrum_timing_span", default=cast(str | None, None)
)
_STAGE_PART: re.Pattern[str] = re.compile(r"[^A-Za-z0-9_.-]+")


def enabled() -> bool:
    return bool(os.environ.get("FULCRUM_TIMING_FILE"))


def stage_name(prefix: str, value: object) -> str:
    """Build a bounded, payload-free stage name from a command or function name."""

    part = _STAGE_PART.sub("_", str(value)).strip("_.-")[:64] or "other"
    return f"{prefix}.{part}"


def _append(row: dict[str, Any]) -> None:
    path = os.environ.get("FULCRUM_TIMING_FILE")
    if not path:
        return
    try:
        # Each row is deliberately small. O_APPEND keeps independent Fulcrum
        # processes from sharing a diagnostic lock on their critical path.
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(
                fd,
                (
                    json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n"
                ).encode("utf-8"),
            )
        finally:
            os.close(fd)
    except OSError:
        # Instrumentation must never change operation behavior.
        pass


def record_completed_span(
    stage: str,
    started: float,
    *,
    span_id: str | None = None,
    parent_span_id: str | None = None,
    outcome: str = "ok",
    exit_code: int | None = None,
) -> None:
    if not enabled():
        return
    row: dict[str, Any] = {
        "schema": 1,
        "trace_id": os.environ.get("FULCRUM_TRACE_ID") or f"process-{os.getpid()}",
        "span_id": span_id or uuid.uuid4().hex,
        "parent_span_id": parent_span_id,
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "stage": stage,
        "started_monotonic": started,
        "duration_ms": (time.monotonic() - started) * 1000,
        "outcome": outcome,
    }
    label = os.environ.get("FULCRUM_TIMING_LABEL")
    sample = os.environ.get("FULCRUM_TIMING_SAMPLE")
    if label:
        row["label"] = label[:128]
    if sample:
        row["sample"] = sample[:32]
    if exit_code is not None:
        row["exit_code"] = exit_code
    _append(row)


def record_timing(stage: str, started: float) -> None:
    """Record a completed leaf stage for callers that already own their clock."""

    record_completed_span(
        stage,
        started,
        parent_span_id=_active_span.get()
        or os.environ.get("FULCRUM_TIMING_PARENT_SPAN"),
    )


class span:
    """A synchronous context manager producing parent-linked timing spans."""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        self.span_id: str | None = None
        self.parent_span_id: str | None = None
        self.started = 0.0
        self.token: contextvars.Token[str | None] | None = None

    def __enter__(self) -> span:
        if not enabled():
            return self
        self.started = time.monotonic()
        self.span_id = uuid.uuid4().hex
        self.parent_span_id = _active_span.get() or os.environ.get(
            "FULCRUM_TIMING_PARENT_SPAN"
        )
        self.token = _active_span.set(self.span_id)
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.span_id is None:
            return
        assert self.token is not None
        _active_span.reset(self.token)
        record_completed_span(
            self.stage,
            self.started,
            span_id=self.span_id,
            parent_span_id=self.parent_span_id,
            outcome="error" if exception_type else "ok",
        )


class timed:
    def __init__(self, stage: str) -> None:
        self.stage = stage

    def __call__(self, function: F) -> F:
        if inspect.iscoroutinefunction(function):

            @wraps(function)
            async def invoke_async(*args: Any, **kwargs: Any) -> Any:
                with span(self.stage):
                    return await function(*args, **kwargs)

            return cast(F, invoke_async)

        @wraps(function)
        def invoke(*args: Any, **kwargs: Any) -> Any:
            with span(self.stage):
                return function(*args, **kwargs)

        return cast(F, invoke)
