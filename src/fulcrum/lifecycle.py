"""Explicit assignment and action transitions owned by Python."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from fulcrum.outcomes import validate_outcome
from fulcrum.store import Store, StoreError, utc_now


def accept_finish(
    store: Store,
    *,
    native_thread_id: str,
    outcome_kind: str,
    options: dict[str, Any],
) -> dict[str, Any]:
    """Bind a finish to the caller's current action; exact retries are idempotent."""

    action = store.current_action(native_thread_id)
    payload = validate_outcome(action["kind"], outcome_kind, options)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if action["outcome_kind"] is not None:
        if (
            action["outcome_kind"] == outcome_kind
            and action["outcome_payload"] == encoded
        ):
            return {
                "ok": True,
                "action_id": action["id"],
                "outcome": outcome_kind,
                "reused": True,
            }
        raise StoreError("this action already has a different accepted outcome")
    if action["state"] in {"failed", "canceled"}:
        raise StoreError(f"cannot finish a {action['state']} action")
    timestamp = utc_now()
    store.execute(
        "UPDATE actions SET outcome_kind = ?, outcome_payload = ?, updated_at = ? WHERE id = ?",
        (outcome_kind, encoded, timestamp, action["id"]),
    )
    store.event(
        "outcome_accepted",
        f"accepted {outcome_kind}",
        entity_type="action",
        entity_id=action["id"],
        detail=payload,
    )
    return {
        "ok": True,
        "action_id": action["id"],
        "outcome": outcome_kind,
        "reused": False,
    }


def observe_action_terminal(
    store: Store, action_id: int, *, runtime_state: str = "completed"
) -> dict[str, Any]:
    """Advance only after both the turn and native helpers are terminal."""

    action = store.row("SELECT * FROM actions WHERE id = ?", (action_id,))
    if action is None:
        raise StoreError(f"unknown action {action_id}")
    task = store.row("SELECT * FROM tasks WHERE id = ?", (action["task_id"],))
    if task is None:
        raise StoreError("action task disappeared")
    if not task["last_turn_terminal"] or not task["helpers_terminal"]:
        return {"advanced": False, "reason": "turn or helpers still active"}
    if runtime_state != "completed":
        return _recover_failed_action(store, action, runtime_state)
    if action["outcome_kind"] is None:
        if not action["reminder_sent"]:
            store.execute(
                "UPDATE actions SET state = 'terminal', reminder_sent = 1, updated_at = ? WHERE id = ?",
                (utc_now(), action_id),
            )
            return {"advanced": False, "reminder": True}
        store.execute(
            "UPDATE actions SET state = 'failed', condition = 'finish outcome missing after reminder', updated_at = ? WHERE id = ?",
            (utc_now(), action_id),
        )
        store.execute("DELETE FROM reservations WHERE action_id = ?", (action_id,))
        store.execute(
            "UPDATE tasks SET state = 'idle', updated_at = ? WHERE id = ?",
            (utc_now(), action["task_id"]),
        )
        if action["assignment_id"] is not None:
            _schedule_assignment_retry(
                store,
                int(action["assignment_id"]),
                "finish outcome missing after reminder",
            )
        return {"advanced": False, "condition": "finish outcome missing after reminder"}
    result = _apply_outcome(store, action)
    store.execute("DELETE FROM reservations WHERE action_id = ?", (action_id,))
    store.execute(
        "UPDATE actions SET state = 'processed', updated_at = ? WHERE id = ?",
        (utc_now(), action_id),
    )
    store.execute(
        "UPDATE tasks SET state = 'idle', updated_at = ? WHERE id = ?",
        (utc_now(), action["task_id"]),
    )
    store.event(
        "action_processed",
        f"processed {action['outcome_kind']}",
        entity_type="action",
        entity_id=action_id,
    )
    return {"advanced": True, **result}


def _recover_failed_action(
    store: Store, action: dict[str, Any], runtime_state: str
) -> dict[str, Any]:
    timestamp = utc_now()
    reason = f"runtime turn {runtime_state}; retained for specific recovery"
    store.execute(
        "UPDATE actions SET state = 'failed', condition = ?, updated_at = ? WHERE id = ?",
        (reason, timestamp, action["id"]),
    )
    store.execute("DELETE FROM reservations WHERE action_id = ?", (action["id"],))
    store.execute(
        "UPDATE tasks SET state = 'idle', updated_at = ? WHERE id = ?",
        (timestamp, action["task_id"]),
    )
    if action["assignment_id"] is not None:
        _schedule_assignment_retry(store, int(action["assignment_id"]), reason)
    return {"advanced": False, "condition": reason}


def _schedule_assignment_retry(store: Store, assignment_id: int, reason: str) -> None:
    assignment = store.row(
        "SELECT stage, retry_count FROM assignments WHERE id = ?", (assignment_id,)
    )
    if assignment is None:
        return
    attempts = int(assignment["retry_count"] or 0) + 1
    due = (
        (
            datetime.now(timezone.utc)
            + timedelta(seconds=5 * (2 ** min(attempts - 1, 6)))
        )
        .isoformat()
        .replace("+00:00", "Z")
    )
    store.execute(
        """UPDATE assignments SET prior_stage = stage, stage = 'recovering',
           retry_count = ?, next_attempt_at = ?, condition = ?, updated_at = ? WHERE id = ?""",
        (attempts, due, reason, utc_now(), assignment_id),
    )


def _apply_outcome(store: Store, action: dict[str, Any]) -> dict[str, Any]:
    kind = action["kind"]
    outcome = action["outcome_kind"]
    payload = json.loads(action["outcome_payload"] or "{}")
    assignment_id = action["assignment_id"]
    timestamp = utc_now()
    if kind in {"implement", "correct"} and outcome == "ready_for_review":
        if assignment_id is None:
            raise StoreError("implementation action has no assignment")
        store.execute(
            "UPDATE assignments SET stage = 'review_pending', condition = NULL, updated_at = ? WHERE id = ?",
            (timestamp, assignment_id),
        )
        return {"assignment_id": assignment_id, "stage": "review_pending"}
    if kind == "correct" and outcome == "permitted_repair_complete":
        if assignment_id is None:
            raise StoreError("repair action has no assignment")
        assignment = store.row(
            "SELECT * FROM assignments WHERE id = ?", (assignment_id,)
        )
        if assignment is None:
            raise StoreError("repair assignment disappeared")
        permissions = json.loads(assignment["repair_permissions"] or "[]")
        category = payload.get("repair_category")
        if category not in permissions:
            raise StoreError(
                f"repair category {category!r} is not present in the retained mandate"
            )
        if not assignment["mandate_candidate_id"] or not assignment["mandate_scope"]:
            raise StoreError("covered repair has no retained review mandate")
        if assignment["candidate_id"] == assignment["mandate_candidate_id"]:
            raise StoreError("covered repair did not produce a replacement candidate")
        store.execute(
            "UPDATE assignments SET stage = 'delivering', predecessor_candidate_id = mandate_candidate_id, repair_category = ?, repair_rationale = ?, repair_evidence = ?, condition = NULL, updated_at = ? WHERE id = ?",
            (
                category,
                payload["repair_rationale"],
                payload["evidence"],
                timestamp,
                assignment_id,
            ),
        )
        return {"assignment_id": assignment_id, "stage": "delivering"}
    if kind == "review" and assignment_id is not None:
        if outcome == "approved":
            assignment = store.row(
                "SELECT candidate_id, scope_snapshot FROM assignments WHERE id = ?",
                (assignment_id,),
            )
            if assignment is None or not assignment["candidate_id"]:
                raise StoreError("approval has no exact retained candidate")
            store.execute(
                "UPDATE assignments SET stage = 'delivering', repair_permissions = ?, mandate_candidate_id = ?, mandate_scope = ?, condition = NULL, updated_at = ? WHERE id = ?",
                (
                    json.dumps(payload.get("allow_repair", [])),
                    assignment["candidate_id"],
                    assignment["scope_snapshot"],
                    timestamp,
                    assignment_id,
                ),
            )
            return {"assignment_id": assignment_id, "stage": "delivering"}
        if outcome in {"changes_requested", "incomplete"}:
            assignment = store.row(
                "SELECT review_failures FROM assignments WHERE id = ?", (assignment_id,)
            )
            failures = (
                int(assignment["review_failures"])
                + (1 if outcome == "changes_requested" else 0)
                if assignment
                else 0
            )
            stage = "recovering" if failures >= 3 else "correcting"
            condition = (
                "three substantive review failures require Archon decision"
                if failures >= 3
                else None
            )
            if failures >= 3:
                hold = store.execute(
                    """INSERT INTO holds(scope, target, reason, urgent, release_condition, created_at)
                       VALUES ('assignment', ?, ?, 1, 'Archon decides whether to rescope or cancel', ?)""",
                    (str(assignment_id), condition, timestamp),
                )
                store.execute(
                    """UPDATE assignments SET stage = ?, review_failures = ?, condition = ?,
                       operator_hold_id = ?, next_attempt_at = NULL, updated_at = ? WHERE id = ?""",
                    (
                        stage,
                        failures,
                        condition,
                        hold.lastrowid,
                        timestamp,
                        assignment_id,
                    ),
                )
            else:
                store.execute(
                    "UPDATE assignments SET stage = ?, review_failures = ?, condition = ?, updated_at = ? WHERE id = ?",
                    (stage, failures, condition, timestamp, assignment_id),
                )
            return {
                "assignment_id": assignment_id,
                "stage": stage,
                "review_failures": failures,
            }
    if kind == "interview" and outcome == "blocked":
        interview = store.row(
            "SELECT * FROM interviews WHERE subject_task_id = ? AND state IN ('starting','active') ORDER BY id DESC LIMIT 1",
            (action["task_id"],),
        )
        if interview is not None:
            store.execute(
                "UPDATE interviews SET state = 'failed', answer_json = ?, updated_at = ? WHERE id = ?",
                (action["outcome_payload"], timestamp, interview["id"]),
            )
        return {
            "interview_id": interview["id"] if interview else None,
            "state": "failed",
        }
    if kind == "specialist" and outcome == "blocked":
        store.execute(
            "UPDATE occurrences SET state = 'failed', report_json = ?, updated_at = ? WHERE id = ?",
            (action["outcome_payload"], timestamp, action["occurrence_id"]),
        )
        return {"occurrence_id": action["occurrence_id"], "state": "failed"}
    if outcome in {"blocked", "exception", "checkpointed"}:
        reason = (
            payload.get("reason") or payload.get("evidence") or "action checkpointed"
        )
        if assignment_id is not None:
            if outcome == "checkpointed":
                _schedule_assignment_retry(store, int(assignment_id), reason)
            else:
                hold = store.execute(
                    """INSERT INTO holds(scope, target, reason, urgent, release_condition, created_at)
                       VALUES ('assignment', ?, ?, 1, 'Archon or operator supplies a specific recovery decision', ?)""",
                    (str(assignment_id), reason, timestamp),
                )
                store.execute(
                    """UPDATE assignments SET prior_stage = stage, stage = 'recovering',
                       operator_hold_id = ?, next_attempt_at = NULL, condition = ?, updated_at = ?
                       WHERE id = ?""",
                    (hold.lastrowid, reason, timestamp, assignment_id),
                )
        return {
            "assignment_id": assignment_id,
            "stage": "recovering",
            "condition": reason,
        }
    if kind == "weaver" and outcome in {"intake_complete", "future_plan"}:
        _archive_obligation(store, action["task_id"], timestamp)
        return {"archive_pending": True}
    if kind == "specialist" and outcome == "report":
        occurrence_id = action["occurrence_id"]
        if occurrence_id is None:
            raise StoreError("specialist action has no occurrence")
        store.execute(
            "UPDATE occurrences SET state = 'publishing', report_json = ?, updated_at = ? WHERE id = ?",
            (action["outcome_payload"], timestamp, occurrence_id),
        )
        return {"occurrence_id": occurrence_id, "state": "publishing"}
    if kind == "specialist" and outcome == "evidence_needed":
        occurrence_id = action["occurrence_id"]
        if occurrence_id is None:
            raise StoreError("specialist action has no occurrence")
        if store.row(
            "SELECT 1 FROM interviews WHERE occurrence_id = ?", (occurrence_id,)
        ):
            raise StoreError(
                "a specialist occurrence may request only one interview round"
            )
        occurrence = store.row(
            "SELECT deadline_at FROM occurrences WHERE id = ?", (occurrence_id,)
        )
        deadline = (
            occurrence["deadline_at"]
            if occurrence and occurrence["deadline_at"]
            else (datetime.now(timezone.utc) + timedelta(hours=24))
            .isoformat()
            .replace("+00:00", "Z")
        )
        for request in payload.get("requests", []):
            if not isinstance(request, dict):
                raise StoreError("each interview request must be an object")
            subject = request.get("subject") or request.get("thread_id")
            question = request.get("question") or request.get("request")
            if not isinstance(subject, str) or not isinstance(question, str):
                raise StoreError("each interview requires subject and question")
            subject_task = _resolve_subject(store, subject)
            if subject_task is None:
                raise StoreError(f"unknown interview subject {subject!r}")
            store.execute(
                "INSERT INTO interviews(occurrence_id, subject_task_id, request, prior_archived, state, deadline_at, created_at, updated_at) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)",
                (
                    occurrence_id,
                    subject_task["id"],
                    question.strip(),
                    int(subject_task["archived"]),
                    deadline,
                    timestamp,
                    timestamp,
                ),
            )
        store.execute(
            "UPDATE occurrences SET state = 'collecting', deadline_at = ?, updated_at = ? WHERE id = ?",
            (deadline, timestamp, occurrence_id),
        )
        return {"occurrence_id": occurrence_id, "state": "collecting"}
    if kind == "interview" and outcome == "interview_answer":
        interview = store.row(
            "SELECT * FROM interviews WHERE subject_task_id = ? AND state = 'active'",
            (action["task_id"],),
        )
        if interview is not None:
            store.execute(
                "UPDATE interviews SET state = 'answered', answer_json = ?, updated_at = ? WHERE id = ?",
                (action["outcome_payload"], timestamp, interview["id"]),
            )
        return {
            "interview_id": interview["id"] if interview else None,
            "state": "answered",
        }
    if kind == "archon" and outcome == "decisions":
        result = apply_archon_decisions(store, payload)
        batch = store.row("SELECT id FROM batches WHERE action_id = ?", (action["id"],))
        if batch is not None:
            store.execute(
                "UPDATE batches SET state = 'processed', updated_at = ? WHERE id = ?",
                (timestamp, batch["id"]),
            )
            store.execute(
                "UPDATE updates SET state = 'processed', updated_at = ? WHERE id IN (SELECT update_id FROM batch_updates WHERE batch_id = ?)",
                (timestamp, batch["id"]),
            )
        return result
    return {"outcome": outcome}


def _archive_obligation(store: Store, task_id: int, timestamp: str) -> None:
    task = store.row("SELECT native_thread_id FROM tasks WHERE id = ?", (task_id,))
    if task is None:
        return
    store.execute(
        """INSERT INTO obligations(kind, identity, target, state, created_at, updated_at)
           VALUES ('archive', ?, ?, 'pending', ?, ?)
           ON CONFLICT(kind, identity, target) DO NOTHING""",
        (str(task_id), task["native_thread_id"], timestamp, timestamp),
    )


def _resolve_subject(store: Store, subject: str) -> dict[str, Any] | None:
    exact = store.row(
        "SELECT * FROM tasks WHERE native_thread_id = ? OR title = ?",
        (subject, subject),
    )
    if exact is not None:
        return exact
    code = subject.strip().upper()
    return store.row("SELECT * FROM tasks WHERE title LIKE ?", (f"%[{code}]%",))


def apply_archon_decisions(store: Store, payload: dict[str, Any]) -> dict[str, Any]:
    """Apply exact approved runs and policies from a frozen Archon outcome."""

    decisions = payload.get("decisions", [])
    created: list[int] = []
    timestamp = utc_now()
    with store.transaction() as connection:
        if "global_limit" in payload:
            global_limit = int(payload["global_limit"])
            if global_limit <= 0:
                raise StoreError("global capacity must be positive")
            connection.execute(
                "INSERT INTO meta(key, value) VALUES ('global_limit', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(global_limit),),
            )
        if "project_limits" in payload:
            limits = payload["project_limits"]
            if not isinstance(limits, dict) or not all(
                isinstance(project, str)
                and isinstance(limit, int)
                and not isinstance(limit, bool)
                and limit > 0
                for project, limit in limits.items()
            ):
                raise StoreError("project capacities must be positive integer mappings")
            connection.execute(
                "INSERT INTO meta(key, value) VALUES ('project_limits', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (json.dumps(payload["project_limits"], sort_keys=True),),
            )
        for policy in payload.get("recurring_policies", []):
            if not isinstance(policy, dict) or policy.get("kind") not in {
                "sage",
                "inquisitor",
            }:
                raise StoreError("recurring policies require a sage or inquisitor kind")
            cadence = int(policy.get("cadence_seconds", 86400))
            if cadence <= 0:
                raise StoreError("recurring policy cadence must be positive")
            anchor = str(policy.get("anchor_at") or timestamp)
            connection.execute(
                """INSERT INTO policies(kind, scope, cadence_seconds, anchor_at, next_due_at, config_json, active)
                   VALUES (?, ?, ?, ?, ?, ?, 1)
                   ON CONFLICT(kind, scope) DO UPDATE SET cadence_seconds = excluded.cadence_seconds,
                   anchor_at = excluded.anchor_at, next_due_at = excluded.next_due_at,
                   config_json = excluded.config_json, active = 1""",
                (
                    policy["kind"],
                    policy.get("scope"),
                    cadence,
                    anchor,
                    anchor,
                    json.dumps(policy.get("config", {}), sort_keys=True),
                ),
            )
        for decision in decisions:
            if not isinstance(decision, dict) or decision.get("decision") != "approve":
                continue
            project = decision.get("project")
            beads = decision.get("beads")
            if not isinstance(project, str) or not isinstance(beads, list) or not beads:
                raise StoreError("approved decision requires project and ordered beads")
            cursor = connection.execute(
                "INSERT INTO runs(project_id, authority, state, created_at, updated_at) VALUES (?, ?, 'approved', ?, ?)",
                (
                    project,
                    str(decision.get("authority", "archon")),
                    timestamp,
                    timestamp,
                ),
            )
            run_id = int(cursor.lastrowid)
            created.append(run_id)
            scopes = decision.get("scope", {})
            for position, bead_id in enumerate(beads):
                bead = connection.execute(
                    "SELECT description, activation, publication_state FROM beads WHERE bead_id = ? AND project_id = ?",
                    (bead_id, project),
                ).fetchone()
                if (
                    bead is None
                    or bead["activation"] != "pending"
                    or bead["publication_state"] != "complete"
                ):
                    raise StoreError(f"bead {bead_id} is not eligible for approval")
                scope = (
                    scopes.get(bead_id, bead["description"])
                    if isinstance(scopes, dict)
                    else bead["description"]
                )
                connection.execute(
                    "INSERT INTO run_beads(run_id, bead_id, position, scope_snapshot) VALUES (?, ?, ?, ?)",
                    (run_id, bead_id, position, scope),
                )
                connection.execute(
                    "INSERT INTO assignments(run_id, bead_id, stage, scope_snapshot, created_at, updated_at) VALUES (?, ?, 'queued', ?, ?, ?)",
                    (run_id, bead_id, scope, timestamp, timestamp),
                )
    return {"created_runs": created}
