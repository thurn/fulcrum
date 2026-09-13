"""Authoritative emergency-Operative journal primitives.

The control-root journal is the installation-wide fence.  SQLite is deliberately
only a queryable mirror: a missing database must never make an unfinished journal
look complete.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

from fulcrum.store import StoreError

UNFINISHED_OPERATIVE_STATES: Final = frozenset(
    {"acquiring", "active", "closing", "aborted"}
)
OPERATIVE_STATES: Final = UNFINISHED_OPERATIVE_STATES | {"closed"}
OPERATIVE_TRANSITIONS: Final = {
    "acquiring": frozenset({"active"}),
    "active": frozenset({"closing", "aborted"}),
    "closing": frozenset({"closed", "active"}),
    "aborted": frozenset({"acquiring"}),
    "closed": frozenset(),
}


@contextmanager
def authority_gate(path: Path, *, blocking: bool) -> Iterator[None]:
    """Serialize setup mutation with creation of the authoritative takeover fence."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = path.open("a+")
    operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    try:
        try:
            fcntl.flock(handle.fileno(), operation)
        except BlockingIOError as error:
            raise StoreError(
                "Fulcrum authority mutation is already in progress"
            ) from error
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def read_journal(path: Path) -> dict[str, Any] | None:
    """Read and validate the versionless journal without changing it."""

    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StoreError(f"operative journal is unreadable: {error}") from error
    if not isinstance(value, dict):
        raise StoreError("operative journal must contain a JSON object")
    required = {
        "takeover_id": str,
        "state": str,
        "native_thread_id": str,
        "scope": str,
        "prior_dispatch_enabled": bool,
        "completed_effects": list,
        "superseded_thread_ids": list,
        "caller_verification": dict,
        "created_at": str,
        "updated_at": str,
        "next_step": str,
    }
    for key, expected in required.items():
        if not isinstance(value.get(key), expected):
            raise StoreError(f"operative journal has invalid {key}")
    if value["state"] not in OPERATIVE_STATES:
        raise StoreError(f"operative journal has invalid state {value['state']!r}")
    if not value["takeover_id"] or not value["native_thread_id"] or not value["scope"]:
        raise StoreError("operative journal identity and scope must not be empty")
    if not all(isinstance(item, str) for item in value["superseded_thread_ids"]):
        raise StoreError("operative journal superseded thread IDs are invalid")
    if not all(isinstance(item, str) for item in value["completed_effects"]):
        raise StoreError("operative journal completed effects are invalid")
    if value["state"] != "acquiring" or value.get("operative_identity") is not None:
        identity = value.get("operative_identity")
        action = value.get("operative_action")
        if not isinstance(identity, dict) or not isinstance(action, dict):
            raise StoreError(
                "operative journal lacks reconstructable authority identity"
            )
        if (
            not isinstance(identity.get("role_number"), int)
            or not isinstance(identity.get("title"), str)
            or not isinstance(identity.get("model"), str)
            or not isinstance(identity.get("reasoning_effort"), str)
            or not isinstance(action.get("payload"), dict)
            or (
                action.get("native_turn_id") is not None
                and not isinstance(action.get("native_turn_id"), str)
            )
        ):
            raise StoreError("operative journal authority identity is invalid")
    return value


def write_journal(path: Path, journal: dict[str, Any]) -> None:
    """Atomically replace and fsync the journal and containing control root."""

    # Validate the exact value before it can become the authority fence.
    temporary_validation = dict(journal)
    state = temporary_validation.get("state")
    if state not in OPERATIVE_STATES:
        raise StoreError(f"invalid operative state {state!r}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = json.dumps(journal, indent=2, sort_keys=True) + "\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def transition_journal(
    path: Path,
    journal: dict[str, Any],
    target: str,
    *,
    now: str,
    next_step: str,
    completed_effect: str | None = None,
    **changes: Any,
) -> dict[str, Any]:
    """Apply one allowed durable transition and return the saved snapshot."""

    source = str(journal.get("state"))
    if target not in OPERATIVE_TRANSITIONS.get(source, frozenset()):
        raise StoreError(f"invalid operative transition {source!r} -> {target!r}")
    updated = dict(journal)
    updated.update(changes)
    updated["state"] = target
    updated["updated_at"] = now
    updated["next_step"] = next_step
    effects = list(updated.get("completed_effects") or [])
    if completed_effect is not None and completed_effect not in effects:
        effects.append(completed_effect)
    updated["completed_effects"] = effects
    write_journal(path, updated)
    return updated


def journal_is_unfinished(journal: dict[str, Any] | None) -> bool:
    return bool(journal and journal.get("state") in UNFINISHED_OPERATIVE_STATES)
