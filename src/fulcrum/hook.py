"""Bounded Codex lifecycle hooks backed only by local Fulcrum records."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from typing import Any, cast

from fulcrum.config import RuntimePaths, resolve_paths
from fulcrum.context import read_task_context
from fulcrum.records import InterviewRecord, ProgressRecord, load_record
from fulcrum.state import read_record

MAX_CONTEXT_CHARACTERS = 3600
WAIT_THREADS_TOOL_NAMES: frozenset[str] = frozenset(
    {"wait_threads", "mcp__codex_app__wait_threads"}
)
WAIT_THREADS_DENIAL_REASON = (
    "Fulcrum roles must not use wait_threads; use direct handoffs and one-shot "
    "inspection instead."
)
TERMINAL_OR_INACTIVE_PHASES = {
    "canceled",
    "cancelled",
    "complete",
    "completed",
    "debrief",
    "inactive",
    "interview",
    "postmortem",
}
HANDOFF_REMINDER = (
    "Before stopping, check whether your current handoff was sent. If not, "
    "message the responsible agent with the outcome or blocker. If already "
    "sent, do not resend. Update your progress and stop when appropriate."
)

ROLE_REMINDERS = {
    "archon": (
        "Keep strategy and fleet coordination; delegate source investigation "
        "and implementation rather than doing it in this task."
    ),
    "executor": (
        "Work only in your owned scope and report ready-for-review, completion, "
        "or blockers directly to your Overseer. A final response does not send it."
    ),
    "inquisitor": (
        "Review the whole enabled project for major architectural findings, not "
        "only recent changes."
    ),
    "night-watchman": (
        "Observe and report only new, changed, or resolved fleet conditions; do "
        "not become a second scheduler or mutate Archon-owned records."
    ),
    "overseer": (
        "Own review and delivery coordination, communicate decisions to the "
        "Executor, and require Tollgate promotion before completion."
    ),
    "sage": (
        "Use bounded evidence and interviews to find underlying workflow problems "
        "without taking over active delivery."
    ),
}


def _event_task_id(event: Mapping[str, Any]) -> str | None:
    """Use only Codex's actual session identity; never infer from cwd or title."""

    value = event.get("session_id")
    return value if isinstance(value, str) and value.strip() else None


def _is_exactly_registered_role(paths: RuntimePaths, task_id: str) -> bool:
    """Return true only for one resolved role with this exact task identity."""

    try:
        loaded = read_record(paths, "role_run_registry")
    except Exception:
        return False
    if loaded["record_kind"] != "role_run_registry":
        return False
    roles = loaded.get("roles")
    if not isinstance(roles, list):
        return False
    matches = [
        role
        for role in roles
        if isinstance(role, dict)
        and role.get("task_id") == task_id
        and role.get("identity_state") == "resolved"
    ]
    return len(matches) == 1


def _pre_tool_use_response(
    paths: RuntimePaths, event: Mapping[str, Any]
) -> dict[str, Any]:
    tool_name = event.get("tool_name")
    if not isinstance(tool_name, str) or tool_name not in WAIT_THREADS_TOOL_NAMES:
        return {"continue": True}
    task_id = _event_task_id(event)
    if task_id is None or not _is_exactly_registered_role(paths, task_id):
        return {"continue": True}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": WAIT_THREADS_DENIAL_REASON,
        }
    }


def _memory_excerpt(memory: Mapping[str, Any]) -> str | None:
    pieces: list[str] = []
    for label in ("global", "role", "project"):
        value = memory.get(label)
        if not isinstance(value, str):
            continue
        collapsed = " ".join(value.split())
        if collapsed:
            pieces.append(f"{label}: {collapsed[:320]}")
    return " | ".join(pieces)[:800] or None


def _compact_context(paths: RuntimePaths, event: Mapping[str, Any]) -> dict[str, Any]:
    if event.get("source") != "compact":
        return {"continue": True}
    task_id = _event_task_id(event)
    if task_id is None:
        return {"continue": True}
    context = read_task_context(paths, task_id)
    if not context.get("known"):
        return {"continue": True}

    role = cast(dict[str, Any], context["role"])
    role_name = str(role["role"])
    lines = [
        f"Fulcrum refresher: you are {role_name}.",
        f"Actual task: {task_id}; run: {role['run_id']}; project: {role['project_id']}.",
        ROLE_REMINDERS.get(
            role_name, "Re-read your installed role skill before continuing."
        ),
    ]
    assignments = cast(list[dict[str, Any]], context.get("assignments", []))
    if assignments:
        assignment = assignments[0]
        lines.append(
            "Assignment: "
            f"{assignment['assignment_id']} / bead {assignment['bead_id']} / "
            f"scope {assignment['scope_reference']}."
        )
    holds = cast(list[dict[str, Any]], context.get("holds", []))
    if holds:
        lines.append(
            "Active holds: " + ", ".join(str(item["hold_id"]) for item in holds)
        )
    memory = cast(dict[str, Any], context.get("memory", {}))
    excerpt = _memory_excerpt(memory)
    if excerpt:
        lines.append(f"Short memory: {excerpt}")
    lines.append(
        f"Local sources: {paths.state_root} (assignment/progress/holds); "
        f"{paths.brain_root} (plans/memory/NEWS). Re-read current state before acting."
    )
    additional_context = "\n".join(lines)[:MAX_CONTEXT_CHARACTERS]
    return {
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": additional_context,
        },
    }


def _stop_response(paths: RuntimePaths, event: Mapping[str, Any]) -> dict[str, Any]:
    if event.get("permission_mode") == "plan" or event.get("stop_hook_active") is True:
        return {"continue": True}
    task_id = _event_task_id(event)
    if task_id is None:
        return {"continue": True}
    context = read_task_context(paths, task_id)
    if not context.get("known"):
        return {"continue": True}
    role = cast(dict[str, Any], context["role"])
    if role.get("role") not in {"executor", "overseer"}:
        return {"continue": True}
    interviews_root = paths.state_root / "interviews"
    if interviews_root.is_dir():
        for path in interviews_root.glob("*.json"):
            try:
                candidate = load_record(path)
            except Exception:
                continue
            if candidate["record_kind"] != "interview":
                continue
            interview = cast(InterviewRecord, candidate)
            if (
                interview["subject_task_id"] == task_id
                and interview["completion_state"] == "active"
            ):
                return {"continue": True}
    try:
        loaded = read_record(paths, "progress", task_id)
    except Exception:
        return {"continue": True}
    if loaded["record_kind"] != "progress":
        return {"continue": True}
    progress = cast(ProgressRecord, loaded)
    if progress["role"] != role["role"]:
        return {"continue": True}
    if progress["phase"].strip().lower() in TERMINAL_OR_INACTIVE_PHASES:
        return {"continue": True}
    if not progress["handoff_needed"] or progress["handoff_sent"]:
        return {"continue": True}
    return {"decision": "block", "reason": HANDOFF_REMINDER}


def handle_event(event: Mapping[str, Any], *, paths: RuntimePaths) -> dict[str, Any]:
    """Return the documented response for supported Codex hook events."""

    name = event.get("hook_event_name")
    if name == "PreToolUse":
        return _pre_tool_use_response(paths, event)
    if name == "SessionStart":
        return _compact_context(paths, event)
    if name == "Stop":
        return _stop_response(paths, event)
    return {"continue": True}


def main(argv: Sequence[str] | None = None) -> int:
    """Read one event from stdin and always return a valid, non-failing response."""

    del argv
    response: dict[str, Any] = {"continue": True}
    try:
        event = json.load(sys.stdin)
        if isinstance(event, dict):
            response = handle_event(event, paths=resolve_paths())
    except Exception:
        # Missing or damaged input is advisory, never authority to steer work.
        pass
    json.dump(response, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
