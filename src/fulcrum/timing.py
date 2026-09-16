"""Opt-in process-local timings. Never record request contents or affect outcomes."""

from __future__ import annotations

from functools import wraps
import json
import os
import time
from collections.abc import Callable
from typing import Any, TypeVar, cast

F = TypeVar("F", bound=Callable[..., Any])


def record_timing(stage: str, started: float) -> None:
    path = os.environ.get("FULCRUM_TIMING_FILE")
    if not path:
        return
    row = {
        "pid": os.getpid(),
        "stage": stage,
        "started_monotonic": started,
        "duration_ms": (time.monotonic() - started) * 1000,
    }
    try:
        # One append per span allows a detached worker and its client to share
        # the file without a diagnostic lock on the command's critical path.
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (json.dumps(row) + "\n").encode())
        finally:
            os.close(fd)
    except OSError:
        pass


class timed:
    def __init__(self, stage: str) -> None:
        self.stage = stage

    def __call__(self, function: F) -> F:
        @wraps(function)
        def invoke(*args: Any, **kwargs: Any) -> Any:
            if not os.environ.get("FULCRUM_TIMING_FILE"):
                return function(*args, **kwargs)
            started = time.monotonic()
            try:
                return function(*args, **kwargs)
            finally:
                record_timing(self.stage, started)

        return cast(F, invoke)
