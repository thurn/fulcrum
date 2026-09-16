"""Process coordination, never workflow storage.

A transition owns fresh reads through writes, not the lifetime of a command.
External effects suspend that lock; operation ownership survives the suspension.
Lock files are never unlinked: replacing an inode would create two lock domains.
"""

from __future__ import annotations

import contextlib
import contextvars
import fcntl
import os
import threading
import time

from fulcrum.timing import record_timing
from pathlib import Path
from typing import Any, Iterator

from fulcrum.contracts import FulcrumError

_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}


class LockLocal(threading.local):
    def __init__(self) -> None:
        self.held: dict[str, list[int]] = {}


_local = LockLocal()


class ProcessLock:
    def __init__(
        self, path: Path, *, blocking: bool = True, shared: bool = False
    ) -> None:
        self.path: Path = path.resolve()
        self.blocking = blocking
        self.shared = shared
        self.key = str(self.path)
        with _guard:
            self.mutex: threading.RLock = (
                threading.RLock()
                if shared
                else _locks.setdefault(self.key, threading.RLock())
            )

    def __enter__(self) -> ProcessLock:
        started = time.monotonic()
        if not self.mutex.acquire(blocking=self.blocking):
            raise FulcrumError(
                "OPERATION_BUSY",
                "operation is already executing",
                exit_code=3,
                retryable=True,
            )
        held = getattr(_local, "held", None)
        if held is None:
            held = _local.held = {}
        if self.key in held:
            held[self.key][1] += 1
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(
                fd,
                (fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX)
                | (0 if self.blocking else fcntl.LOCK_NB),
            )
        except BaseException as error:
            os.close(fd)
            self.mutex.release()
            if isinstance(error, BlockingIOError):
                raise FulcrumError(
                    "OPERATION_BUSY",
                    "operation is already executing",
                    exit_code=3,
                    retryable=True,
                ) from error
            raise
        held[self.key] = [fd, 1]
        record_timing("lock.wait", started)
        return self

    def __exit__(self, *args: Any) -> None:
        held = _local.held
        held[self.key][1] -= 1
        if not held[self.key][1]:
            os.close(held.pop(self.key)[0])
        self.mutex.release()


_active: contextvars.ContextVar[ProcessLock | None] = contextvars.ContextVar[
    ProcessLock | None
]("transition", default=None)


@contextlib.contextmanager
def transition(root: Path) -> Iterator[None]:
    lock = ProcessLock(root / ".fulcrum-locks" / "state")
    with lock:
        token = _active.set(lock)
        try:
            yield
        finally:
            _active.reset(token)


@contextlib.contextmanager
def external_effect() -> Iterator[None]:
    """Never wait on another system while excluding unrelated state changes."""
    lock = _active.get()
    if lock is None:
        yield
        return
    count = _local.held[lock.key][1]
    for _ in range(count):
        lock.__exit__()
    token = _active.set(None)
    try:
        yield
    finally:
        for _ in range(count):
            lock.__enter__()
        _active.reset(token)


def operation_lock(root: Path, request_id: str) -> ProcessLock:
    import uuid

    return ProcessLock(
        root / ".fulcrum-locks" / "operations" / uuid.UUID(request_id).hex,
        blocking=False,
    )


def merge_change(base: Any, desired: Any, current: Any) -> Any:
    """Three-way field changes preserve concurrent, unrelated updates."""
    if desired == base:
        return current
    if current == base or current == desired:
        return desired
    if all(isinstance(item, dict) for item in (base, desired, current)):
        result = dict(current)
        missing = object()
        for key in base.keys() | desired.keys():
            before, after, now = (
                base.get(key, missing),
                desired.get(key, missing),
                current.get(key, missing),
            )
            if before == after:
                continue
            if after is missing:
                if now != before and now is not missing:
                    raise FulcrumError(
                        "STATE_CONFLICT",
                        f"concurrent change to {key}",
                        exit_code=5,
                        retryable=True,
                    )
                result.pop(key, None)
            else:
                result[key] = merge_change(before, after, now)
        return result
    raise FulcrumError(
        "STATE_CONFLICT",
        "state changed during an external effect; inspect before retrying",
        exit_code=5,
        retryable=True,
    )


def coordinated(function: Any) -> Any:
    """One command transition; adapters explicitly yield around external effects."""
    import functools

    @functools.wraps(function)
    def invoke(self: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
        root = request.instance.brain_root
        if root is None or request.request_id is None:
            return function(self, request, *args, **kwargs)
        if (root / ".fulcrum-locks/migration.json").exists():
            raise FulcrumError(
                "MAINTENANCE_IN_PROGRESS",
                "state migration has fenced admission",
                exit_code=3,
                retryable=True,
            )
        with (
            operation_lock(root, request.request_id),
            contextlib.ExitStack() as resources,
        ):
            target = (
                request.arguments.get("bead")
                or request.arguments.get("id")
                or request.thread_id
            )
            if target:
                resources.enter_context(
                    ProcessLock(
                        root
                        / ".fulcrum-locks"
                        / "resources"
                        / str(target).encode().hex(),
                        blocking=False,
                    )
                )
            with ProcessLock(root / ".fulcrum-locks" / "maintenance", shared=True):
                source = os.environ.get("FULCRUM_SOURCE")
                if source and (Path(source) / ".retired-for-state").exists():
                    raise FulcrumError(
                        "SOURCE_RETIRED",
                        "retry with the selected source after state migration",
                        exit_code=3,
                        retryable=True,
                    )
                with transition(root):
                    return function(self, request, *args, **kwargs)

    return invoke


def unlocked(function: Any) -> Any:
    import functools

    @functools.wraps(function)
    def invoke(*args: Any, **kwargs: Any) -> Any:
        with external_effect():
            return function(*args, **kwargs)

    return invoke


def maintenance_operation(function: Any) -> Any:
    """Recovery shares the same exclusion boundary as normal operation claims."""
    import functools

    @functools.wraps(function)
    def invoke(self: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
        root = request.instance.brain_root
        if root is None:
            return function(self, request, *args, **kwargs)
        with ProcessLock(root / ".fulcrum-locks/maintenance", blocking=False):
            with transition(root):
                return function(self, request, *args, **kwargs)

    return invoke
