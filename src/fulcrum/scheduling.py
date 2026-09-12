"""Capacity, hold, dependency, cadence, and dispatch calculations."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from fulcrum.store import Store, utc_now


def capacity(store: Store) -> dict[str, Any]:
    global_row = store.row("SELECT value FROM meta WHERE key = 'global_limit'")
    project_row = store.row("SELECT value FROM meta WHERE key = 'project_limits'")
    reservations = store.rows("SELECT * FROM reservations")
    project_usage: dict[str, int] = {}
    for reservation in reservations:
        for project in json.loads(reservation["project_ids"]):
            project_usage[project] = project_usage.get(project, 0) + 1
    return {
        "global_limit": int(global_row["value"]) if global_row else None,
        "project_limits": json.loads(project_row["value"]) if project_row else {},
        "global_usage": sum(int(row["global_slots"]) for row in reservations),
        "project_usage": project_usage,
    }


def assignment_blockers(store: Store, assignment: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    enabled = store.row(
        "SELECT enabled, condition FROM projects WHERE project_id = ?",
        (assignment["project_id"],),
    )
    if enabled is None or not enabled["enabled"]:
        blockers.append(
            enabled["condition"]
            if enabled and enabled["condition"]
            else "project integration is disabled"
        )
    for hold in store.rows("SELECT * FROM holds WHERE released_at IS NULL"):
        if (
            hold["scope"] == "global"
            or (
                hold["scope"] == "project"
                and hold["target"] == assignment["project_id"]
            )
            or (hold["scope"] == "run" and hold["target"] == str(assignment["run_id"]))
        ):
            blockers.append(f"hold {hold['id']}: {hold['reason']}")
    dependencies = store.rows(
        "SELECT dependency_id FROM bead_dependencies WHERE bead_id = ?",
        (assignment["bead_id"],),
    )
    for dependency in dependencies:
        completed = store.row(
            "SELECT 1 FROM assignments WHERE bead_id = ? AND stage = 'completed'",
            (dependency["dependency_id"],),
        )
        if completed is None:
            blockers.append(f"dependency {dependency['dependency_id']} is incomplete")
    limits = capacity(store)
    if limits["global_limit"] is None:
        blockers.append("Archon has not established a global capacity limit")
    elif limits["global_usage"] >= limits["global_limit"]:
        blockers.append("global capacity is full")
    project_limit = limits["project_limits"].get(assignment["project_id"])
    if project_limit is None:
        blockers.append(
            f"Archon has not established capacity for {assignment['project_id']}"
        )
    elif limits["project_usage"].get(assignment["project_id"], 0) >= project_limit:
        blockers.append(f"project capacity is full for {assignment['project_id']}")
    return blockers


def ready_assignments(store: Store) -> list[dict[str, Any]]:
    enabled = store.row("SELECT value FROM meta WHERE key = 'dispatch_enabled'")
    if enabled is None or enabled["value"] != "1":
        return []
    assignments = store.rows(
        """SELECT a.*, r.project_id, rb.position FROM assignments a JOIN runs r ON r.id = a.run_id
           JOIN run_beads rb ON rb.run_id = a.run_id AND rb.bead_id = a.bead_id
           WHERE r.state IN ('approved','active') AND a.stage IN ('queued','preparing','review_pending','correcting')
           ORDER BY CASE WHEN a.stage = 'queued' THEN 1 ELSE 0 END, r.id, rb.position, a.id"""
    )
    return [
        assignment
        for assignment in assignments
        if not assignment_blockers(store, assignment)
    ]


def can_start_task(task: dict[str, Any], *, unresolved_start: bool = False) -> bool:
    return bool(
        task["last_turn_terminal"]
        and task["runtime_status"] == "idle"
        and task["helpers_terminal"]
        and not unresolved_start
        and task["state"] not in {"retired", "archived", "uncertain"}
    )


def next_cadence(anchor: datetime, cadence_seconds: int, now: datetime) -> datetime:
    """Return the first cadence point strictly after now without catch-up fanout."""

    if cadence_seconds <= 0:
        raise ValueError("cadence must be positive")
    if anchor.tzinfo is None or now.tzinfo is None:
        raise ValueError("cadence timestamps must be timezone-aware")
    if anchor > now:
        return anchor
    elapsed = (now - anchor).total_seconds()
    steps = int(elapsed // cadence_seconds) + 1
    return anchor + timedelta(seconds=steps * cadence_seconds)


def create_due_occurrences(store: Store, *, now: datetime | None = None) -> list[int]:
    current = now or datetime.now(timezone.utc)
    created: list[int] = []
    for policy in store.rows(
        "SELECT * FROM policies WHERE active = 1 AND next_due_at IS NOT NULL"
    ):
        due = datetime.fromisoformat(policy["next_due_at"].replace("Z", "+00:00"))
        if due > current:
            continue
        existing = store.row(
            "SELECT id FROM occurrences WHERE policy_id = ? AND state NOT IN ('complete','skipped')",
            (policy["id"],),
        )
        if existing is not None:
            continue
        timestamp = utc_now()
        cursor = store.execute(
            "INSERT INTO occurrences(policy_id, kind, scope, authority, state, created_at, updated_at) VALUES (?, ?, ?, 'recurring-policy', 'queued', ?, ?)",
            (policy["id"], policy["kind"], policy["scope"], timestamp, timestamp),
        )
        created.append(int(cursor.lastrowid))
    return created
