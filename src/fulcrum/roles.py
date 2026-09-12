"""Small role identity and progress constructors; no dispatch side effects."""

from __future__ import annotations

from copy import deepcopy
from typing import cast

from fulcrum.records import ProgressRecord, RoleRun, validate_record


def resolve_identity(role: RoleRun, matches: list[tuple[str, str]]) -> RoleRun:
    """Matches must already be scoped by project/run/tag using supported tools."""
    unique = set(matches)
    if len(unique) != 1:
        raise ValueError(
            "identity pending or ambiguous; inspect before retrying creation"
        )
    task_id, host_id = unique.pop()
    if not task_id or not host_id or task_id == role.get("client_thread_id"):
        raise ValueError("a real task/host identity is required")
    if role["task_id"] is not None and (role["task_id"], role["host_id"]) != (
        task_id,
        host_id,
    ):
        raise ValueError("resolved identity cannot be replaced")
    result = deepcopy(role)
    result.update(task_id=task_id, host_id=host_id, identity_state="resolved")
    result.pop("client_thread_id", None)
    return result


def next_role_number(roles: list[RoleRun], role: str) -> int:
    family = {"overseer", "executor"} if role in {"overseer", "executor"} else {role}
    if role not in {"overseer", "executor", "sage", "inquisitor"}:
        raise ValueError("human-created roles do not allocate numbered tags")
    return 1 + max(
        (r["role_number"] or 0 for r in roles if r["role"] in family), default=0
    )


def initialize_progress(
    role: RoleRun, now: str, *, plan_mode: bool = False
) -> ProgressRecord:
    if plan_mode:
        raise ValueError("Plan mode cannot activate a role")
    task = role["task_id"]
    if role["identity_state"] != "resolved" or not task:
        raise ValueError("resolve identity before activation")
    return cast(
        ProgressRecord,
        validate_record(
            {
                "record_kind": "progress",
                "schema_version": 1,
                "writer_id": task,
                "updated_at": now,
                "role_task_id": task,
                "role": role["role"],
                "phase": "queued",
                "phase_started_at": now,
                "expected_next_actor": task,
                "expected_next_action": "Read current assignment and holds",
                "handoff_needed": False,
                "handoff_sent": False,
                "delivery_error": None,
                "owned_resources": [],
            }
        ),
    )


def prepare_handoff(
    progress: ProgressRecord,
    recipient: RoleRun,
    action: str,
    now: str,
    *,
    expected_by: str | None = None,
) -> ProgressRecord:
    if progress["handoff_needed"] and not progress["handoff_sent"]:
        raise ValueError("inspect unresolved delivery before another handoff")
    if recipient["identity_state"] != "resolved" or not recipient["task_id"]:
        raise ValueError("recipient identity must be resolved")
    if not action.strip():
        raise ValueError("handoff requires an action")
    result = deepcopy(progress)
    result.update(
        updated_at=now,
        expected_next_actor=recipient["task_id"],
        expected_next_action=action,
        handoff_needed=True,
        handoff_sent=False,
        delivery_error=None,
        expected_by=expected_by,
    )
    return cast(ProgressRecord, validate_record(result))


def finish_handoff(
    progress: ProgressRecord, now: str, *, delivered: bool, error: str | None = None
) -> ProgressRecord:
    if not progress["handoff_needed"]:
        raise ValueError("record handoff intent before delivery")
    result = deepcopy(progress)
    result.update(
        updated_at=now,
        handoff_sent=delivered,
        delivery_error=(
            None if delivered else error or "delivery uncertain; inspect recipient"
        ),
    )
    return cast(ProgressRecord, validate_record(result))
