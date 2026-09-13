"""Durable workflow primitives shared by every orchestration feature.

The kernel records intent before effects, admits one lease at a time, and makes
progress obligations queryable. Adapters may observe external systems, but they
do not decide workflow state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fulcrum.store import Store, StoreError, utc_now


@dataclass(frozen=True)
class LeaseRequest:
    task_id: int
    assignment_id: int | None
    kind: str
    payload: dict[str, Any]
    project_ids: tuple[str, ...]
    pair_id: int | None = None
    conflict_keys: tuple[str, ...] = ()
    occurrence_id: int | None = None
    check_after: str | None = None


@dataclass(frozen=True)
class LeaseDecision:
    admitted: bool
    action: dict[str, Any] | None = None
    blockers: tuple[str, ...] = ()


def acquire_lease(store: Store, request: LeaseRequest) -> LeaseDecision:
    """Atomically check capacity/conflicts and retain one action plus lease."""

    timestamp = utc_now()
    with store.transaction() as connection:
        blockers = _lease_blockers(connection, request)
        if blockers:
            return LeaseDecision(False, blockers=tuple(blockers))
        action = connection.execute(
            """INSERT INTO actions(
                   task_id, assignment_id, occurrence_id, kind, payload, state,
                   check_after, next_attempt_at, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
            (
                request.task_id,
                request.assignment_id,
                request.occurrence_id,
                request.kind,
                json.dumps(request.payload, sort_keys=True),
                request.check_after,
                timestamp,
                timestamp,
                timestamp,
            ),
        )
        action_id = int(action.lastrowid)
        connection.execute(
            """INSERT INTO reservations(
                   action_id, pair_id, global_slots, project_ids, conflict_keys,
                   state, created_at
               ) VALUES (?, ?, 1, ?, ?, 'reserved', ?)""",
            (
                action_id,
                request.pair_id,
                json.dumps(request.project_ids),
                json.dumps(request.conflict_keys),
                timestamp,
            ),
        )
    retained = store.row("SELECT * FROM actions WHERE id = ?", (action_id,))
    if retained is None:
        raise StoreError("leased action disappeared after commit")
    store.event(
        "lease_acquired",
        f"acquired lease for {request.kind}",
        entity_type="action",
        entity_id=action_id,
        detail={
            "assignment_id": request.assignment_id,
            "projects": request.project_ids,
            "conflict_keys": request.conflict_keys,
        },
    )
    return LeaseDecision(True, action=retained)


def _lease_blockers(connection: Any, request: LeaseRequest) -> list[str]:
    blockers: list[str] = []
    current = connection.execute(
        """SELECT id FROM actions WHERE task_id = ?
           AND state IN ('pending','starting','active','terminal','uncertain')""",
        (request.task_id,),
    ).fetchone()
    if current is not None:
        blockers.append(f"task already owns action {current['id']}")
    if request.pair_id is not None:
        pair = connection.execute(
            "SELECT action_id FROM reservations WHERE pair_id = ?",
            (request.pair_id,),
        ).fetchone()
        if pair is not None:
            blockers.append(f"pair already owns action {pair['action_id']}")

    global_limit_row = connection.execute(
        "SELECT value FROM meta WHERE key = 'global_limit'"
    ).fetchone()
    reservations = connection.execute("SELECT * FROM reservations").fetchall()
    global_usage = sum(int(row["global_slots"]) for row in reservations)
    if global_limit_row is None:
        blockers.append("global capacity is missing")
    elif global_usage >= int(global_limit_row["value"]):
        blockers.append("global capacity is full")

    limits_row = connection.execute(
        "SELECT value FROM meta WHERE key = 'project_limits'"
    ).fetchone()
    limits = json.loads(limits_row["value"]) if limits_row else {}
    project_usage: dict[str, int] = {}
    active_conflicts: set[str] = set()
    for row in reservations:
        for project in json.loads(row["project_ids"]):
            project_usage[project] = project_usage.get(project, 0) + 1
        for key in json.loads(row["conflict_keys"] or "[]"):
            active_conflicts.add(str(key))
    global_hold = connection.execute(
        "SELECT id, reason FROM holds WHERE released_at IS NULL AND scope = 'global' LIMIT 1"
    ).fetchone()
    if global_hold is not None:
        blockers.append(f"hold {global_hold['id']}: {global_hold['reason']}")
    for project_id in request.project_ids:
        project = connection.execute(
            "SELECT enabled, condition FROM projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if project is None or not project["enabled"]:
            blockers.append(
                str(project["condition"] or "project integration is disabled")
                if project is not None
                else f"project {project_id} is missing"
            )
            continue
        limit = limits.get(project_id)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            blockers.append(f"project capacity is missing for {project_id}")
        elif project_usage.get(project_id, 0) >= limit:
            blockers.append(f"project capacity is full for {project_id}")
        hold = connection.execute(
            """SELECT id, reason FROM holds WHERE released_at IS NULL
               AND scope = 'project' AND target = ? LIMIT 1""",
            (project_id,),
        ).fetchone()
        if hold is not None:
            blockers.append(f"hold {hold['id']}: {hold['reason']}")
    if request.assignment_id is not None:
        assignment = connection.execute(
            """SELECT run_id, bead_id FROM assignments WHERE id = ?""",
            (request.assignment_id,),
        ).fetchone()
        if assignment is None:
            blockers.append(f"assignment {request.assignment_id} is missing")
        else:
            run_hold = connection.execute(
                """SELECT id, reason FROM holds WHERE released_at IS NULL
                   AND scope = 'run' AND target = ? LIMIT 1""",
                (str(assignment["run_id"]),),
            ).fetchone()
            if run_hold is not None:
                blockers.append(f"hold {run_hold['id']}: {run_hold['reason']}")
            missing_dependency = connection.execute(
                """SELECT d.dependency_id FROM bead_dependencies d
                   WHERE d.bead_id = ? AND NOT EXISTS (
                     SELECT 1 FROM assignments a
                     WHERE a.bead_id = d.dependency_id AND a.stage = 'completed'
                   ) LIMIT 1""",
                (assignment["bead_id"],),
            ).fetchone()
            if missing_dependency is not None:
                blockers.append(
                    f"dependency {missing_dependency['dependency_id']} is incomplete"
                )
    conflict = sorted(active_conflicts.intersection(request.conflict_keys))
    if conflict:
        blockers.append("conflicting resources are leased: " + ", ".join(conflict))
    return blockers


def schedule_action_retry(
    store: Store,
    action_id: int,
    reason: str,
    *,
    delay_seconds: int = 5,
    maximum_attempts: int = 8,
) -> bool:
    """Make a failed start runnable later, or convert exhaustion to a hold."""

    action = store.row("SELECT * FROM actions WHERE id = ?", (action_id,))
    if action is None:
        raise StoreError(f"unknown action {action_id}")
    attempts = int(action["attempt_count"]) + 1
    timestamp = utc_now()
    if attempts >= maximum_attempts:
        with store.transaction() as connection:
            hold = connection.execute(
                """INSERT INTO holds(scope, target, reason, urgent, release_condition, created_at)
                   VALUES ('action', ?, ?, 1, 'operator resolves exhausted start retries', ?)""",
                (str(action_id), reason, timestamp),
            )
            connection.execute(
                """UPDATE actions SET state = 'failed', attempt_count = ?, operator_hold_id = ?,
                   next_attempt_at = NULL, condition = ?, updated_at = ? WHERE id = ?""",
                (attempts, hold.lastrowid, reason, timestamp, action_id),
            )
            connection.execute(
                "DELETE FROM reservations WHERE action_id = ?", (action_id,)
            )
        store.event(
            "action_held",
            reason,
            entity_type="action",
            entity_id=action_id,
            detail={"attempts": attempts},
        )
        return False
    due = (
        (
            datetime.now(timezone.utc)
            + timedelta(seconds=delay_seconds * (2 ** min(attempts - 1, 6)))
        )
        .isoformat()
        .replace("+00:00", "Z")
    )
    store.transition(
        "action",
        action_id,
        table="actions",
        field="state",
        to_state="pending",
        reason=reason,
        extra={
            "attempt_count": attempts,
            "next_attempt_at": due,
            "condition": reason,
        },
    )
    return True


def release_lease(store: Store, action_id: int, *, reason: str) -> None:
    reservation = store.row(
        "SELECT id FROM reservations WHERE action_id = ?", (action_id,)
    )
    if reservation is None:
        return
    store.execute("DELETE FROM reservations WHERE action_id = ?", (action_id,))
    store.event(
        "lease_released",
        reason,
        entity_type="action",
        entity_id=action_id,
    )


def invariant_violations(store: Store) -> list[str]:
    """Return impossible or deadlocked durable states in stable order."""

    violations: list[str] = []
    dangling = store.rows(
        """SELECT r.action_id, a.state FROM reservations r LEFT JOIN actions a ON a.id = r.action_id
           WHERE a.id IS NULL OR a.state IN ('processed','failed','canceled')"""
    )
    for row in dangling:
        violations.append(
            f"reservation for action {row['action_id']} has no runnable owner"
        )
    for row in (
        store.rows("""SELECT id FROM assignments WHERE stage = 'recovering'
           AND next_attempt_at IS NULL AND operator_hold_id IS NULL""")
        if _has_column(store, "assignments", "next_attempt_at")
        else []
    ):
        violations.append(f"recovering assignment {row['id']} has no retry or hold")
    for row in store.rows("""SELECT id FROM external_operations
           WHERE state = 'uncertain' AND reconciliation_used = 1"""):
        violations.append(
            f"external operation {row['id']} is uncertain after targeted observation"
        )

    global_row = store.row("SELECT value FROM meta WHERE key = 'global_limit'")
    if global_row is not None:
        usage = store.row(
            "SELECT COALESCE(SUM(global_slots), 0) AS usage FROM reservations"
        )
        if usage is not None and int(usage["usage"]) > int(global_row["value"]):
            violations.append("global capacity is exceeded")
    project_row = store.row("SELECT value FROM meta WHERE key = 'project_limits'")
    if project_row is not None:
        limits = json.loads(project_row["value"])
        per_project_usage: dict[str, int] = {}
        for reservation in store.rows("SELECT project_ids FROM reservations"):
            for project in json.loads(reservation["project_ids"]):
                per_project_usage[project] = per_project_usage.get(project, 0) + 1
        for project, count in sorted(per_project_usage.items()):
            limit = limits.get(project)
            if isinstance(limit, int) and count > limit:
                violations.append(f"project capacity is exceeded for {project}")
    return violations


def _has_column(store: Store, table: str, column: str) -> bool:
    return any(
        row["name"] == column for row in store.rows(f"PRAGMA table_info({table})")
    )
