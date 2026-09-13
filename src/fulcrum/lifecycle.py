"""Explicit assignment and action transitions owned by Python."""

from __future__ import annotations

import json
import sqlite3
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fulcrum.outcomes import validate_outcome
from fulcrum.store import Store, StoreError, utc_now

OPERATOR_RESOLVABLE_OPERATION_KINDS = {
    "beads_close",
    "beads_create",
    "setup_runtime_smoke",
    "thread_start",
    "tollgate_approve",
    "tollgate_candidate_create",
    "tollgate_worktree_create",
    "turn_start",
}

OPERATOR_OPERATION_RESOLUTIONS = {
    "observed_success",
    "observed_failure",
    "confirmed_unsent",
}

ESCALATION_ITEMS_LIMIT = 5
ESCALATION_TEXT_LIMIT = 320


def _bounded_escalation_text(value: Any) -> str:
    rendered = " ".join(str(value).split())
    if len(rendered) <= ESCALATION_TEXT_LIMIT:
        return rendered
    return rendered[: ESCALATION_TEXT_LIMIT - 1].rstrip() + "…"


def _review_escalation_synthesis(
    store: Store,
    assignment: dict[str, Any],
    current_payload: dict[str, Any],
) -> dict[str, Any]:
    """Build bounded decision evidence while full handoffs remain durable."""

    findings: list[dict[str, str]] = []
    prior = store.rows(
        """SELECT source_action_id, content_json FROM handoffs
           WHERE assignment_id = ? AND kind = 'review_findings'
           ORDER BY id DESC LIMIT ?""",
        (assignment["id"], ESCALATION_ITEMS_LIMIT),
    )
    current_findings = (
        current_payload.get("findings", []) if isinstance(current_payload, dict) else []
    )
    for finding in current_findings[:ESCALATION_ITEMS_LIMIT]:
        if not isinstance(finding, dict):
            continue
        item = {
            key: _bounded_escalation_text(finding.get(key, ""))
            for key in ("problem", "evidence", "required_change")
        }
        if item["problem"]:
            findings.append(item)

    history: list[dict[str, Any]] = []
    for row in prior[:ESCALATION_ITEMS_LIMIT]:
        source = json.loads(row["content_json"])
        historical_findings = (
            source.get("findings", []) if isinstance(source, dict) else []
        )
        history.append(
            {
                "review_action_id": int(row["source_action_id"]),
                "findings": [
                    _bounded_escalation_text(item.get("problem", ""))
                    for item in historical_findings[:ESCALATION_ITEMS_LIMIT]
                    if isinstance(item, dict) and item.get("problem")
                ],
                "classification": "prior review history; resolution status not inferred",
            }
        )

    implementation = store.row(
        """SELECT source_action_id, content_json FROM handoffs
           WHERE assignment_id = ? AND kind = 'implementation_evidence'
           ORDER BY id DESC LIMIT 1""",
        (assignment["id"],),
    )
    changes = "No newer implementation evidence was retained."
    prior_review_action = max(
        (int(row["source_action_id"]) for row in prior), default=0
    )
    if (
        implementation is not None
        and int(implementation["source_action_id"]) > prior_review_action
    ):
        evidence = json.loads(implementation["content_json"])
        if isinstance(evidence, dict):
            changes = _bounded_escalation_text(
                evidence.get("evidence_content") or evidence.get("evidence") or evidence
            )
        else:
            changes = _bounded_escalation_text(evidence)
    recommendations = [
        finding["required_change"]
        for finding in findings
        if finding.get("required_change")
    ]
    return {
        "candidate_revision": {
            "candidate_id": assignment.get("candidate_id"),
            "source": assignment.get("source_oid"),
            "tested": assignment.get("tested_oid"),
        },
        "unresolved_findings": findings,
        "prior_review_history": history,
        "changes_since_prior_attempt": changes,
        "reviewer_recommendation": recommendations[:ESCALATION_ITEMS_LIMIT],
    }


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
    if outcome_kind == "evidence_needed":
        context = json.loads(action["payload"])
        if action["role"] != "sage" or context.get("continuation"):
            raise StoreError("only Sage's initial analysis may request interviews")
        if not payload["requests"]:
            raise StoreError("an interview round requires at least one subject")
        if store.row(
            "SELECT 1 FROM interviews WHERE occurrence_id = ?",
            (action["occurrence_id"],),
        ):
            raise StoreError("only one interview round is allowed")
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
    if action["state"] == "processed":
        return {"advanced": True, "reused": True}
    recoverable_runtime_failure = bool(
        action["state"] == "failed"
        and runtime_state == "completed"
        and action["outcome_kind"] is not None
        and str(action["condition"] or "").startswith("runtime turn ")
    )
    if action["state"] in {"failed", "canceled"} and not recoverable_runtime_failure:
        return {
            "advanced": False,
            "condition": action["condition"] or f"action is {action['state']}",
        }
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
    try:
        with store.transaction() as connection:
            result = _apply_outcome(store, action, connection=connection)
            connection.execute(
                "DELETE FROM reservations WHERE action_id = ?", (action_id,)
            )
            connection.execute(
                "UPDATE actions SET state = 'processed', condition = NULL, updated_at = ? WHERE id = ?",
                (utc_now(), action_id),
            )
            connection.execute(
                "UPDATE tasks SET state = 'idle', updated_at = ? WHERE id = ?",
                (utc_now(), action["task_id"]),
            )
    except (
        StoreError,
        sqlite3.Error,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        return _reject_outcome_application(store, action, str(error))
    store.event(
        "action_processed",
        f"processed {action['outcome_kind']}",
        entity_type="action",
        entity_id=action_id,
    )
    return {"advanced": True, **result}


def _reject_outcome_application(
    store: Store, action: dict[str, Any], reason: str
) -> dict[str, Any]:
    """Retain a bad outcome without poisoning its frozen work forever."""

    timestamp = utc_now()
    condition = f"accepted outcome could not be applied: {reason}"
    with store.transaction() as connection:
        connection.execute(
            "UPDATE actions SET state = 'failed', condition = ?, updated_at = ? WHERE id = ?",
            (condition, timestamp, action["id"]),
        )
        connection.execute(
            "DELETE FROM reservations WHERE action_id = ?", (action["id"],)
        )
        connection.execute(
            "UPDATE tasks SET state = 'idle', updated_at = ? WHERE id = ?",
            (timestamp, action["task_id"]),
        )
        if action["kind"] == "archon":
            batch = connection.execute(
                "SELECT id FROM batches WHERE action_id = ?", (action["id"],)
            ).fetchone()
            if batch is not None:
                connection.execute(
                    "UPDATE batches SET state = 'failed', updated_at = ? WHERE id = ?",
                    (timestamp, batch["id"]),
                )
                connection.execute(
                    """UPDATE updates SET state = 'retained', updated_at = ?
                       WHERE id IN (SELECT update_id FROM batch_updates WHERE batch_id = ?)""",
                    (timestamp, batch["id"]),
                )
        elif action["kind"] == "review" and action["assignment_id"] is not None:
            connection.execute(
                """UPDATE assignments SET stage = 'review_pending', condition = ?,
                   updated_at = ? WHERE id = ?""",
                (condition, timestamp, action["assignment_id"]),
            )
        elif (
            action["kind"] in {"implement", "correct"}
            and action["assignment_id"] is not None
        ):
            assignment = connection.execute(
                "SELECT stage, retry_count FROM assignments WHERE id = ?",
                (action["assignment_id"],),
            ).fetchone()
            if assignment is not None:
                attempts = int(assignment["retry_count"] or 0) + 1
                due = (
                    (
                        datetime.now(timezone.utc)
                        + timedelta(seconds=5 * (2 ** min(attempts - 1, 6)))
                    )
                    .isoformat()
                    .replace("+00:00", "Z")
                )
                connection.execute(
                    """UPDATE assignments SET prior_stage = stage, stage = 'recovering',
                       retry_count = ?, next_attempt_at = ?, condition = ?, updated_at = ?
                       WHERE id = ?""",
                    (
                        attempts,
                        due,
                        condition,
                        timestamp,
                        action["assignment_id"],
                    ),
                )
        elif action["kind"] == "specialist" and action["occurrence_id"] is not None:
            connection.execute(
                "UPDATE occurrences SET state = 'queued', updated_at = ? WHERE id = ?",
                (timestamp, action["occurrence_id"]),
            )
            _archive_obligation(store, int(action["task_id"]), timestamp)
        elif action["kind"] == "interview":
            connection.execute(
                """UPDATE interviews SET state = 'queued', updated_at = ?
                   WHERE occurrence_id = ? AND subject_task_id = ? AND state = 'active'""",
                (timestamp, action["occurrence_id"], action["task_id"]),
            )
    store.event(
        "outcome_application_rejected",
        condition,
        entity_type="action",
        entity_id=action["id"],
    )
    return {"advanced": False, "condition": condition}


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


def _apply_outcome(
    store: Store, action: dict[str, Any], *, connection: Any
) -> dict[str, Any]:
    kind = action["kind"]
    outcome = action["outcome_kind"]
    payload = json.loads(action["outcome_payload"] or "{}")
    assignment_id = action["assignment_id"]
    timestamp = utc_now()
    if kind in {"implement", "correct"} and outcome == "ready_for_review":
        if assignment_id is None:
            raise StoreError("implementation action has no assignment")
        candidate = store.row(
            "SELECT candidate_id, source_oid, tested_oid FROM assignments WHERE id = ?",
            (assignment_id,),
        )
        if (
            candidate is None
            or not candidate["candidate_id"]
            or not candidate["source_oid"]
        ):
            raise StoreError(
                "implementation cannot enter review without an immutable candidate"
            )
        validation_error = _exact_source_validation_error(candidate, payload)
        if validation_error is not None:
            raise StoreError(validation_error)
        store.execute(
            "UPDATE assignments SET stage = 'review_pending', condition = NULL, updated_at = ? WHERE id = ?",
            (timestamp, assignment_id),
        )
        store.execute(
            """INSERT INTO handoffs(assignment_id, source_action_id, kind, content_json, created_at)
               VALUES (?, ?, 'implementation_evidence', ?, ?)""",
            (
                assignment_id,
                action["id"],
                json.dumps(
                    _retain_evidence_content(store, int(assignment_id), payload),
                    sort_keys=True,
                ),
                timestamp,
            ),
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
                    json.dumps(payload.get("repair_permissions", [])),
                    assignment["candidate_id"],
                    assignment["scope_snapshot"],
                    timestamp,
                    assignment_id,
                ),
            )
            return {"assignment_id": assignment_id, "stage": "delivering"}
        if outcome in {"changes_requested", "incomplete"}:
            assignment = store.row(
                """SELECT a.*, r.project_id FROM assignments a
                   JOIN runs r ON r.id = a.run_id WHERE a.id = ?""",
                (assignment_id,),
            )
            if assignment is None:
                raise StoreError("review assignment disappeared")
            failures = int(assignment["review_failures"]) + (
                1 if outcome == "changes_requested" else 0
            )
            stage = "recovering" if failures >= 3 else "correcting"
            condition = (
                "three substantive review failures require Archon decision"
                if failures >= 3
                else None
            )
            if failures >= 3:
                synthesis = _review_escalation_synthesis(store, assignment, payload)
                hold = store.execute(
                    """INSERT INTO holds(scope, target, reason, urgent, release_condition, created_at)
                       VALUES ('assignment', ?, ?, 1,
                               'Archon resolves escalation with retry, rescope, or cancel', ?)""",
                    (str(assignment_id), condition, timestamp),
                )
                hold_id = int(hold.lastrowid)
                store.execute(
                    """UPDATE assignments SET prior_stage = 'correcting', stage = ?,
                       review_failures = ?, condition = ?,
                       operator_hold_id = ?, next_attempt_at = NULL, updated_at = ? WHERE id = ?""",
                    (
                        stage,
                        failures,
                        condition,
                        hold_id,
                        timestamp,
                        assignment_id,
                    ),
                )
                archon = store.row("""SELECT id FROM tasks WHERE role = 'archon'
                       AND state NOT IN ('retired','archived')""")
                if archon is not None:
                    store.execute(
                        """INSERT OR IGNORE INTO updates(
                               recipient_task_id, identity, content, actionable,
                               state, created_at, updated_at
                           ) VALUES (?, ?, ?, 1, 'retained', ?, ?)""",
                        (
                            archon["id"],
                            f"review-escalation:{assignment_id}:{action['id']}",
                            json.dumps(
                                {
                                    "kind": "review_failure_escalation",
                                    "assignment_id": assignment_id,
                                    "bead_id": assignment["bead_id"],
                                    "project": assignment["project_id"],
                                    "action_id": action["id"],
                                    "review_action": outcome,
                                    "review_failures": failures,
                                    "condition": condition,
                                    "hold_id": hold_id,
                                    "required_decision": "resolve_escalation",
                                    "resolutions": ["retry", "rescope", "cancel"],
                                    **synthesis,
                                },
                                sort_keys=True,
                            ),
                            timestamp,
                            timestamp,
                        ),
                    )
            else:
                store.execute(
                    "UPDATE assignments SET stage = ?, review_failures = ?, condition = ?, updated_at = ? WHERE id = ?",
                    (stage, failures, condition, timestamp, assignment_id),
                )
            store.execute(
                """INSERT INTO handoffs(assignment_id, source_action_id, kind, content_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    assignment_id,
                    action["id"],
                    (
                        "review_findings"
                        if outcome == "changes_requested"
                        else "missing_evidence"
                    ),
                    action["outcome_payload"],
                    timestamp,
                ),
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
                archon = store.row("""SELECT id FROM tasks WHERE role = 'archon'
                       AND state NOT IN ('retired','archived')""")
                if archon is not None:
                    store.execute(
                        """INSERT OR IGNORE INTO updates(
                               recipient_task_id, identity, content, actionable,
                               state, created_at, updated_at
                           ) VALUES (?, ?, ?, 1, 'retained', ?, ?)""",
                        (
                            archon["id"],
                            f"blocked:{assignment_id}:{action['id']}",
                            json.dumps(
                                {
                                    "kind": "assignment_blocked",
                                    "assignment_id": assignment_id,
                                    "action_id": action["id"],
                                    "condition": reason,
                                    "hold_id": int(hold.lastrowid),
                                },
                                sort_keys=True,
                            ),
                            timestamp,
                            timestamp,
                        ),
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
        occurrence = store.row(
            "SELECT scope FROM occurrences WHERE id = ?", (occurrence_id,)
        )
        if occurrence is None:
            raise StoreError("specialist occurrence disappeared")
        raw_scope = occurrence["scope"]
        scope = (
            json.loads(raw_scope)
            if raw_scope and str(raw_scope).startswith("{")
            else {}
        )
        allowed_projects = set(scope.get("projects", []))
        identities: set[tuple[str, str]] = set()
        for finding in payload.get("findings", []):
            project = finding["project"]
            if allowed_projects and project not in allowed_projects:
                raise StoreError(
                    f"finding project {project!r} is outside the specialist scope"
                )
            if (
                store.row(
                    "SELECT 1 FROM projects WHERE project_id = ? AND enabled = 1",
                    (project,),
                )
                is None
            ):
                raise StoreError(f"finding project {project!r} is unavailable")
            identity = (project, finding["identity"])
            if identity in identities:
                raise StoreError(f"duplicate finding identity {identity!r}")
            identities.add(identity)
            existing_bead = finding.get("existing_bead_id")
            if (
                existing_bead
                and store.row(
                    "SELECT 1 FROM beads WHERE bead_id = ? AND project_id = ?",
                    (existing_bead, project),
                )
                is None
            ):
                raise StoreError(
                    f"finding names unknown project bead {existing_bead!r}"
                )
        store.execute(
            "UPDATE occurrences SET state = 'publishing', report_json = ?, updated_at = ? WHERE id = ?",
            (action["outcome_payload"], timestamp, occurrence_id),
        )
        store.execute(
            """INSERT INTO obligations(
                   kind, identity, target, state, created_at, updated_at
               ) VALUES ('finding_publication', ?, ?, 'pending', ?, ?)
               ON CONFLICT(kind, identity, target) DO UPDATE SET state = 'pending',
               detail = NULL, next_attempt_at = NULL, updated_at = excluded.updated_at""",
            (str(occurrence_id), str(occurrence_id), timestamp, timestamp),
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
        batch = store.row("SELECT id FROM batches WHERE action_id = ?", (action["id"],))
        required_operation_ids: set[int] = set()
        required_escalations: dict[int, tuple[int, set[str]]] = {}
        proposal_scopes: dict[tuple[str, str], tuple[str, str]] = {}
        if batch is not None:
            expected = {
                int(row["update_id"])
                for row in store.rows(
                    "SELECT update_id FROM batch_updates WHERE batch_id = ?",
                    (batch["id"],),
                )
            }
            handled_items = payload.get("handled_update_ids", [])
            handled = set(handled_items)
            if (
                any(isinstance(item, bool) for item in handled_items)
                or len(handled_items) != len(handled)
                or handled != expected
            ):
                raise StoreError(
                    "Archon must handle every frozen update exactly; "
                    f"expected={sorted(expected)} handled={handled_items}"
                )
            for row in store.rows(
                """SELECT u.id, u.content FROM updates u JOIN batch_updates bu
                   ON bu.update_id = u.id WHERE bu.batch_id = ?""",
                (batch["id"],),
            ):
                content = json.loads(row["content"])
                update_kind = content.get("kind")
                if update_kind == "proposal":
                    bead_id = content.get("bead_id")
                    project = content.get("project")
                    reference = content.get("scope_reference")
                    if not all(
                        isinstance(value, str) and value
                        for value in (bead_id, project, reference)
                    ):
                        raise StoreError(
                            "proposal update has a malformed retained scope reference"
                        )
                    retained_scope = store.row(
                        """SELECT bead_id, project_id, scope_snapshot, update_id
                           FROM scope_references WHERE identity = ?""",
                        (reference,),
                    )
                    if (
                        retained_scope is None
                        or retained_scope["update_id"] != row["id"]
                        or retained_scope["bead_id"] != bead_id
                        or retained_scope["project_id"] != project
                    ):
                        raise StoreError(
                            f"proposal scope reference {reference!r} is missing or stale"
                        )
                    proposal_scopes[(project, bead_id)] = (
                        reference,
                        retained_scope["scope_snapshot"],
                    )
                    continue
                if update_kind == "review_failure_escalation":
                    assignment_id = content.get("assignment_id")
                    hold_id = content.get("hold_id")
                    resolutions = content.get("resolutions")
                    if (
                        not isinstance(assignment_id, int)
                        or isinstance(assignment_id, bool)
                        or not isinstance(hold_id, int)
                        or isinstance(hold_id, bool)
                        or not isinstance(resolutions, list)
                        or not resolutions
                        or not all(isinstance(item, str) for item in resolutions)
                    ):
                        raise StoreError(
                            "review-failure escalation update has invalid resolution context"
                        )
                    unresolved = store.row(
                        """SELECT 1 FROM assignments a JOIN holds h
                           ON h.id = a.operator_hold_id
                           WHERE a.id = ? AND a.stage = 'recovering'
                             AND a.operator_hold_id = ? AND h.released_at IS NULL""",
                        (assignment_id, hold_id),
                    )
                    if unresolved is not None:
                        if assignment_id in required_escalations:
                            raise StoreError(
                                "frozen batch has multiple unresolved review-failure "
                                f"escalations for assignment {assignment_id}"
                            )
                        required_escalations[assignment_id] = (
                            hold_id,
                            set(resolutions),
                        )
                    continue
                if update_kind != "operation_resolution":
                    continue
                operation_id = content.get("operation_id")
                if not isinstance(operation_id, int):
                    raise StoreError(
                        "operation-resolution update has no integer operation_id"
                    )
                operation = store.row(
                    "SELECT state, reconciliation_used FROM external_operations WHERE id = ?",
                    (operation_id,),
                )
                if (
                    operation is not None
                    and operation["state"] == "uncertain"
                    and operation["reconciliation_used"]
                ):
                    required_operation_ids.add(operation_id)
            resolved_decisions: list[dict[str, Any]] = []
            for decision in payload.get("decisions", []):
                if (
                    not isinstance(decision, dict)
                    or decision.get("decision") != "approve"
                ):
                    resolved_decisions.append(decision)
                    continue
                project = decision.get("project")
                beads = decision.get("beads")
                references = decision.get("scope_references")
                if "scope" in decision:
                    raise StoreError(
                        "Archon approval must reference retained scope, not echo scope text"
                    )
                if (
                    not isinstance(project, str)
                    or not isinstance(beads, list)
                    or not beads
                    or not all(isinstance(bead, str) for bead in beads)
                    or not isinstance(references, dict)
                    or set(references) != set(beads)
                    or not all(
                        isinstance(reference, str) and reference
                        for reference in references.values()
                    )
                ):
                    raise StoreError(
                        "Archon approval requires exactly one retained scope reference per bead"
                    )
                stored_scopes: dict[str, str] = {}
                for bead_id in beads:
                    retained = proposal_scopes.get((project, bead_id))
                    if retained is None or references[bead_id] != retained[0]:
                        raise StoreError(
                            f"scope reference for {project}/{bead_id} is missing or stale"
                        )
                    stored_scopes[bead_id] = retained[1]
                resolved_decisions.append(
                    {
                        **{
                            key: value
                            for key, value in decision.items()
                            if key != "scope_references"
                        },
                        "scope": stored_scopes,
                    }
                )
            payload = {**payload, "decisions": resolved_decisions}
            supplied_operation_ids = [
                decision.get("operation_id")
                for decision in payload.get("decisions", [])
                if decision.get("decision") == "resolve_operation"
            ]
            missing = required_operation_ids - set(supplied_operation_ids)
            duplicates = sorted(
                operation_id
                for operation_id in required_operation_ids
                if supplied_operation_ids.count(operation_id) != 1
            )
            if missing or duplicates:
                raise StoreError(
                    "every unresolved operation-resolution update requires exactly "
                    "one matching resolve_operation decision; "
                    f"missing={sorted(missing)} duplicates={duplicates}"
                )
            supplied_escalations: dict[int, list[dict[str, Any]]] = {}
            for decision in payload.get("decisions", []):
                if (
                    isinstance(decision, dict)
                    and decision.get("decision") == "resolve_escalation"
                    and isinstance(decision.get("assignment_id"), int)
                    and not isinstance(decision.get("assignment_id"), bool)
                ):
                    supplied_escalations.setdefault(
                        int(decision["assignment_id"]), []
                    ).append(decision)
            missing_escalations = sorted(
                assignment_id
                for assignment_id in required_escalations
                if not supplied_escalations.get(assignment_id)
            )
            duplicate_escalations = sorted(
                assignment_id
                for assignment_id in required_escalations
                if len(supplied_escalations.get(assignment_id, [])) > 1
            )
            if missing_escalations or duplicate_escalations:
                raise StoreError(
                    "every unresolved review-failure escalation requires exactly "
                    "one matching resolve_escalation decision; "
                    f"missing={missing_escalations} duplicates={duplicate_escalations}"
                )
            for assignment_id, (_, resolutions) in required_escalations.items():
                resolution = supplied_escalations[assignment_id][0].get("resolution")
                if resolution not in resolutions:
                    raise StoreError(
                        f"review-failure escalation for assignment {assignment_id} "
                        f"supports only {sorted(resolutions)}; got {resolution!r}"
                    )
        result = apply_archon_decisions(store, payload, connection=connection)
        unresolved = [
            operation_id
            for operation_id in required_operation_ids
            if store.row(
                "SELECT 1 FROM external_operations WHERE id = ? AND state = 'uncertain'",
                (operation_id,),
            )
            is not None
        ]
        if unresolved:
            raise StoreError(
                f"operation resolution did not transition operations {unresolved}"
            )
        unresolved_escalations = [
            assignment_id
            for assignment_id, (hold_id, _) in required_escalations.items()
            if store.row(
                """SELECT 1 FROM assignments a JOIN holds h
                   ON h.id = a.operator_hold_id
                   WHERE a.id = ? AND a.stage = 'recovering'
                     AND a.operator_hold_id = ? AND h.released_at IS NULL""",
                (assignment_id, hold_id),
            )
            is not None
        ]
        if unresolved_escalations:
            raise StoreError(
                "review escalation resolution did not transition assignments "
                f"{unresolved_escalations}"
            )
        succession = store.row(
            "SELECT value FROM meta WHERE key = 'archon_succession_request'"
        )
        if succession is not None:
            request = json.loads(succession["value"])
            if request.get("task_id") is None:
                request["task_id"] = action["task_id"]
                task = store.row(
                    "SELECT model, reasoning_effort FROM tasks WHERE id = ?",
                    (action["task_id"],),
                )
                if task is None:
                    raise StoreError("retiring Archon task disappeared")
                request["successor_model"] = task["model"]
                request["successor_reasoning_effort"] = task["reasoning_effort"]
                store.execute(
                    "UPDATE meta SET value = ? WHERE key = 'archon_succession_request'",
                    (json.dumps(request, sort_keys=True),),
                )
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
    if kind == "archon" and outcome == "deferred":
        batch = store.row("SELECT id FROM batches WHERE action_id = ?", (action["id"],))
        if batch is None:
            raise StoreError("Archon deferral has no frozen batch")
        reactivation = payload["reactivation"]
        next_check = reactivation.get("next_check_at")
        store.execute(
            """INSERT INTO deferred_batches(batch_id, reactivation_json, next_check_at, created_at)
               VALUES (?, ?, ?, ?)""",
            (
                batch["id"],
                json.dumps(reactivation, sort_keys=True),
                next_check,
                timestamp,
            ),
        )
        return {"batch_id": batch["id"], "state": "deferred"}
    return {"outcome": outcome}


def _retain_evidence_content(
    store: Store, assignment_id: int, payload: dict[str, Any]
) -> dict[str, Any]:
    """Snapshot bounded evidence so a handoff never depends on disappearing context."""

    retained = dict(payload)
    reference = payload.get("evidence")
    if not isinstance(reference, str) or not reference.strip():
        return retained
    assignment = store.row(
        "SELECT worktree_path FROM assignments WHERE id = ?", (assignment_id,)
    )
    path = Path(reference).expanduser()
    if not path.is_absolute() and assignment and assignment["worktree_path"]:
        path = Path(assignment["worktree_path"]) / path
    try:
        if path.stat().st_size > 1_000_000:
            raise OSError("evidence file exceeds 1 MB")
        retained["evidence_content"] = path.read_text(encoding="utf-8")
        retained["evidence_path"] = str(path.resolve(strict=True))
    except (OSError, UnicodeError) as error:
        retained["evidence_read_error"] = str(error)
    return retained


def _exact_source_validation_error(
    candidate: dict[str, Any], payload: dict[str, Any]
) -> str | None:
    """Require controller-produced exact-source evidence for mismatched revisions."""

    source_oid = candidate.get("source_oid")
    tested_oid = candidate.get("tested_oid")
    if not source_oid or not tested_oid or source_oid == tested_oid:
        return None
    artifact = payload.get("exact_source_validation")
    if not isinstance(artifact, dict):
        return (
            "source and tested revisions differ; controller-produced tree comparison "
            "or exact-source validation evidence is required before review"
        )
    if artifact.get("trees_equal") is True:
        if (
            artifact.get("source_revision") == source_oid
            and artifact.get("tested_revision") == tested_oid
        ):
            return None
        return "tree-equivalence evidence does not match the retained revisions"
    valid = (
        artifact.get("required") is True
        and artifact.get("source_revision") == source_oid
        and artifact.get("tested_revision") == tested_oid
        and isinstance(artifact.get("command"), list)
        and bool(artifact["command"])
        and artifact.get("exit_status") == 0
        and artifact.get("source_before") == source_oid
        and artifact.get("source_after") == source_oid
        and artifact.get("source_unchanged") is True
        and artifact.get("worktree_clean_before") is True
        and artifact.get("worktree_clean_after") is True
        and artifact.get("passed") is True
        and isinstance(artifact.get("artifact_path"), str)
    )
    if valid:
        return None
    detail = artifact.get("error") or f"exit status {artifact.get('exit_status')!r}"
    return (
        "source and tested trees differ; exact-source validation must record a "
        "nonempty command, exit status 0, and an unchanged clean source before/after "
        f"the check ({detail})"
    )


def _archive_obligation(store: Store, task_id: int, timestamp: str) -> None:
    task = store.row("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if task is None:
        return
    if task["lineage_number"] is not None and _lineage_has_open_work(
        store.connection, int(task["lineage_number"])
    ):
        return
    store.execute(
        """INSERT INTO obligations(kind, identity, target, state, created_at, updated_at)
           VALUES ('archive', ?, ?, 'pending', ?, ?)
           ON CONFLICT(kind, identity, target) DO UPDATE SET
             state = 'pending', detail = NULL, retry_count = 0,
             next_attempt_at = NULL, operator_hold_id = NULL,
             updated_at = excluded.updated_at
           WHERE obligations.state = 'canceled'""",
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


def _lineage_has_open_work(connection: Any, lineage_number: int) -> bool:
    return (
        connection.execute(
            """SELECT 1 FROM bead_lineages lineage
               WHERE lineage.lineage_number = ? AND (
                 NOT EXISTS (
                   SELECT 1 FROM assignments a WHERE a.bead_id = lineage.bead_id
                 ) OR EXISTS (
                   SELECT 1 FROM assignments a WHERE a.bead_id = lineage.bead_id
                     AND a.stage NOT IN ('completed','canceled')
                 )
               ) LIMIT 1""",
            (lineage_number,),
        ).fetchone()
        is not None
    )


def _schedule_archive(
    connection: Any, task: Any, timestamp: str, *, execute: Any | None = None
) -> None:
    writer = execute or connection.execute
    writer(
        """INSERT INTO obligations(
               kind, identity, target, state, created_at, updated_at
           ) VALUES ('archive', ?, ?, 'pending', ?, ?)
           ON CONFLICT(kind, identity, target) DO UPDATE SET
             state = 'pending', detail = NULL, retry_count = 0,
             next_attempt_at = NULL, operator_hold_id = NULL,
             updated_at = excluded.updated_at
           WHERE obligations.state = 'canceled'""",
        (str(task["id"]), task["native_thread_id"], timestamp, timestamp),
    )


def schedule_run_archival(
    connection: Any,
    run_id: int,
    timestamp: str,
    *,
    execute: Any | None = None,
) -> None:
    run = connection.execute(
        """SELECT executor_task_id, overseer_task_id, lineage_number
           FROM runs WHERE id = ?""",
        (run_id,),
    ).fetchone()
    if run is None:
        return
    if run["lineage_number"] is not None:
        lineage_number = int(run["lineage_number"])
        if _lineage_has_open_work(connection, lineage_number):
            return
        tasks = connection.execute(
            """SELECT * FROM tasks WHERE lineage_number = ?
               AND role IN ('weaver','executor','overseer')
               AND archived = 0 AND state NOT IN ('retired','archived')
               ORDER BY id""",
            (lineage_number,),
        ).fetchall()
        for task in tasks:
            _schedule_archive(connection, task, timestamp, execute=execute)
        return
    for task_id in (run["executor_task_id"], run["overseer_task_id"]):
        if task_id is None:
            continue
        task = connection.execute(
            "SELECT id, native_thread_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if task is not None:
            _schedule_archive(connection, task, timestamp, execute=execute)


def _required_resolution_text(decision: dict[str, Any], name: str) -> str:
    value = decision.get(name)
    if not isinstance(value, str) or not value.strip():
        raise StoreError(f"resolve_operation requires nonempty {name}")
    return value.strip()


def _restore_assignment_after_operation(
    connection: Any,
    assignment: Any,
    *,
    stage: str | None,
    condition: str | None,
    timestamp: str,
) -> None:
    target_stage = stage or str(assignment["prior_stage"] or "queued")
    connection.execute(
        """UPDATE assignments SET stage = ?, prior_stage = NULL,
           operator_hold_id = NULL, next_attempt_at = NULL, condition = ?, updated_at = ?
           WHERE id = ?""",
        (target_stage, condition, timestamp, assignment["id"]),
    )


def _resolve_turn_start(
    connection: Any,
    operation: Any,
    decision: dict[str, Any],
    resolution: str,
    condition: str,
    timestamp: str,
) -> None:
    action = connection.execute(
        "SELECT * FROM actions WHERE id = ?", (int(operation["target"]),)
    ).fetchone()
    if action is None or action["operator_hold_id"] != operation["operator_hold_id"]:
        raise StoreError("uncertain turn start is not attached to its held action")
    if resolution == "observed_success":
        turn_id = _required_resolution_text(decision, "native_id")
        connection.execute(
            """UPDATE actions SET state = 'active', native_turn_id = ?,
               operator_hold_id = NULL, next_attempt_at = NULL, condition = NULL,
               updated_at = ? WHERE id = ?""",
            (turn_id, timestamp, action["id"]),
        )
        connection.execute(
            "UPDATE reservations SET state = 'active' WHERE action_id = ?",
            (action["id"],),
        )
        connection.execute(
            """UPDATE tasks SET state = 'active', runtime_status = 'active',
               last_turn_terminal = 0, archive_eligible_at = NULL,
               archive_idle_turn_id = NULL,
               updated_at = ? WHERE id = ?""",
            (timestamp, action["task_id"]),
        )
        return
    connection.execute(
        """UPDATE actions SET state = 'pending', native_turn_id = NULL,
           operator_hold_id = NULL, next_attempt_at = ?, condition = ?, updated_at = ?
           WHERE id = ?""",
        (timestamp, condition, timestamp, action["id"]),
    )
    connection.execute(
        "UPDATE reservations SET state = 'reserved' WHERE action_id = ?",
        (action["id"],),
    )


def _resolve_worktree_create(
    connection: Any,
    operation: Any,
    decision: dict[str, Any],
    resolution: str,
    condition: str,
    timestamp: str,
) -> None:
    assignment = connection.execute(
        "SELECT * FROM assignments WHERE id = ?", (int(operation["target"]),)
    ).fetchone()
    if (
        assignment is None
        or assignment["operator_hold_id"] != operation["operator_hold_id"]
    ):
        raise StoreError(
            "uncertain worktree creation is not attached to its assignment"
        )
    if resolution == "observed_success":
        result = decision.get("result")
        if not isinstance(result, dict):
            raise StoreError("observed worktree success requires a result object")
        path = result.get("worktree_path")
        if not isinstance(path, str) or not path.strip():
            raise StoreError("observed worktree success requires result.worktree_path")
        connection.execute(
            """UPDATE assignments SET worktree_path = ?, stage = 'preparing',
               prior_stage = NULL, operator_hold_id = NULL, next_attempt_at = NULL,
               condition = NULL, updated_at = ? WHERE id = ?""",
            (path.strip(), timestamp, assignment["id"]),
        )
        return
    connection.execute(
        """UPDATE assignments SET worktree_path = NULL, stage = 'queued',
           prior_stage = NULL, operator_hold_id = NULL, next_attempt_at = NULL,
           condition = ?, updated_at = ? WHERE id = ?""",
        (condition, timestamp, assignment["id"]),
    )


def _resolve_candidate_create(
    connection: Any,
    operation: Any,
    decision: dict[str, Any],
    resolution: str,
    condition: str,
    timestamp: str,
) -> None:
    assignment = connection.execute(
        "SELECT * FROM assignments WHERE id = ?", (int(operation["target"]),)
    ).fetchone()
    if (
        assignment is None
        or assignment["operator_hold_id"] != operation["operator_hold_id"]
    ):
        raise StoreError(
            "uncertain candidate creation is not attached to its assignment"
        )
    if resolution == "observed_success":
        candidate_id = _required_resolution_text(decision, "native_id")
        result = decision.get("result")
        if result is not None and not isinstance(result, dict):
            raise StoreError("observed candidate success result must be an object")
        inputs = json.loads(operation["input_json"])
        source_oid = (result or {}).get("source_oid") or inputs.get("revision")
        if (
            not isinstance(source_oid, str)
            or not source_oid.strip()
            or source_oid == "HEAD"
        ):
            raise StoreError(
                "observed candidate success requires an immutable result.source_oid"
            )
        _restore_assignment_after_operation(
            connection,
            assignment,
            stage=None,
            condition=None,
            timestamp=timestamp,
        )
        connection.execute(
            """UPDATE assignments SET candidate_id = ?, source_oid = ?, tested_oid = ?,
               updated_at = ? WHERE id = ?""",
            (
                candidate_id,
                source_oid.strip(),
                (result or {}).get("tested_oid"),
                timestamp,
                assignment["id"],
            ),
        )
        return
    connection.execute(
        "UPDATE assignments SET candidate_id = NULL, source_oid = NULL, tested_oid = NULL WHERE id = ?",
        (assignment["id"],),
    )
    _restore_assignment_after_operation(
        connection,
        assignment,
        stage=None,
        condition=condition,
        timestamp=timestamp,
    )


def _resolve_assignment_operation(
    connection: Any,
    operation: Any,
    resolution: str,
    condition: str,
    timestamp: str,
) -> None:
    assignment = connection.execute(
        """SELECT * FROM assignments WHERE candidate_id = ?
           AND stage NOT IN ('completed','canceled')""",
        (operation["target"],),
    ).fetchone()
    if operation["kind"] == "beads_close":
        assignment = connection.execute(
            """SELECT * FROM assignments WHERE bead_id = ?
               AND stage NOT IN ('completed','canceled')""",
            (operation["target"],),
        ).fetchone()
    if (
        assignment is None
        or assignment["operator_hold_id"] != operation["operator_hold_id"]
    ):
        raise StoreError(
            f"uncertain {operation['kind']} is not attached to its assignment"
        )
    if operation["kind"] == "tollgate_approve":
        stage = (
            "delivering"
            if resolution in {"observed_success", "confirmed_unsent"}
            else "correcting"
        )
        _restore_assignment_after_operation(
            connection,
            assignment,
            stage=stage,
            condition=None if resolution == "observed_success" else condition,
            timestamp=timestamp,
        )
        return
    if resolution != "observed_success":
        _restore_assignment_after_operation(
            connection,
            assignment,
            stage="delivering",
            condition=condition,
            timestamp=timestamp,
        )
        return
    _restore_assignment_after_operation(
        connection,
        assignment,
        stage="completed",
        condition=None,
        timestamp=timestamp,
    )
    remaining = connection.execute(
        """SELECT 1 FROM assignments WHERE run_id = ?
           AND stage NOT IN ('completed','canceled') LIMIT 1""",
        (assignment["run_id"],),
    ).fetchone()
    if remaining is None:
        connection.execute(
            "UPDATE runs SET state = 'completed', updated_at = ? WHERE id = ?",
            (timestamp, assignment["run_id"]),
        )
        schedule_run_archival(connection, int(assignment["run_id"]), timestamp)


def _resolve_thread_start(
    connection: Any,
    operation: Any,
    decision: dict[str, Any],
    resolution: str,
    condition: str,
    timestamp: str,
) -> None:
    inputs = json.loads(operation["input_json"])
    if resolution == "observed_success":
        thread_id = _required_resolution_text(decision, "native_id")
        task = connection.execute(
            "SELECT * FROM tasks WHERE native_thread_id = ?", (thread_id,)
        ).fetchone()
        if task is None:
            cursor = connection.execute(
                """INSERT INTO tasks(native_thread_id, role, role_number, title,
                   description, project_id, model, reasoning_effort, pair_id, state,
                   runtime_status, lineage_number, lineage_suffix, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'idle', 'unmaterialized', ?, ?, ?, ?)""",
                (
                    thread_id,
                    inputs["role"],
                    inputs.get("role_number"),
                    inputs["title"],
                    inputs["description"],
                    inputs.get("local_project_id"),
                    inputs["model"],
                    inputs["effort"],
                    inputs.get("pair_id"),
                    inputs.get("lineage_number"),
                    inputs.get("lineage_suffix", ""),
                    timestamp,
                    timestamp,
                ),
            )
            task_id = int(cursor.lastrowid)
        else:
            expected = (inputs["role"], inputs.get("pair_id"))
            if (task["role"], task["pair_id"]) != expected:
                raise StoreError("observed thread is already bound to another role")
            task_id = int(task["id"])
        pair_id = inputs.get("pair_id")
        if isinstance(pair_id, int) and inputs["role"] in {"executor", "overseer"}:
            column = f"{inputs['role']}_task_id"
            connection.execute(
                f"UPDATE runs SET {column} = ?, updated_at = ? WHERE id = ?",
                (task_id, timestamp, pair_id),
            )
    pair_id = inputs.get("pair_id")
    if isinstance(pair_id, int):
        assignment = connection.execute(
            """SELECT * FROM assignments WHERE run_id = ?
               AND stage NOT IN ('completed','canceled') ORDER BY id LIMIT 1""",
            (pair_id,),
        ).fetchone()
        if (
            assignment is not None
            and assignment["operator_hold_id"] == operation["operator_hold_id"]
        ):
            _restore_assignment_after_operation(
                connection,
                assignment,
                stage=None,
                condition=None if resolution == "observed_success" else condition,
                timestamp=timestamp,
            )


def _resolve_obligation_operation(
    connection: Any,
    operation: Any,
    decision: dict[str, Any],
    resolution: str,
    condition: str,
    timestamp: str,
) -> None:
    obligation = connection.execute(
        """SELECT * FROM obligations WHERE kind = 'beads_publication' AND identity = ?
           AND state NOT IN ('complete','canceled') ORDER BY id LIMIT 1""",
        (operation["target"],),
    ).fetchone()
    if resolution == "observed_success":
        bead_id = _required_resolution_text(decision, "native_id")
        inputs = json.loads(operation["input_json"])
        connection.execute(
            """INSERT INTO beads(bead_id, intake_key, project_id, title, description,
               activation, executor_model, executor_reasoning_effort, overseer_model,
               overseer_reasoning_effort, model_provenance, plan_id, plan_commit,
               context_json, publication_state, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'complete', ?, ?)""",
            (
                bead_id,
                inputs["intake_key"],
                inputs["project"],
                inputs["title"],
                inputs["description"],
                inputs.get("activation", "pending"),
                inputs.get("executor_model", "gpt-5.6-sol"),
                inputs.get("executor_reasoning_effort", "high"),
                inputs.get("overseer_model", "gpt-5.6-sol"),
                inputs.get("overseer_reasoning_effort", "high"),
                inputs.get("model_provenance", "default"),
                inputs.get("plan_id"),
                inputs.get("plan_commit"),
                json.dumps(inputs.get("context", [])),
                timestamp,
                timestamp,
            ),
        )
        for dependency in inputs.get("dependencies", []):
            connection.execute(
                "INSERT INTO bead_dependencies(bead_id, dependency_id) VALUES (?, ?)",
                (bead_id, dependency),
            )
    if obligation is None:
        return
    if obligation["operator_hold_id"] != operation["operator_hold_id"]:
        raise StoreError(
            f"uncertain {operation['kind']} is not attached to its obligation"
        )
    connection.execute(
        """UPDATE obligations SET state = ?, operator_hold_id = NULL,
           next_attempt_at = ?, detail = ?, updated_at = ? WHERE id = ?""",
        (
            "complete" if resolution == "observed_success" else "failed",
            None if resolution == "observed_success" else timestamp,
            None if resolution == "observed_success" else condition,
            timestamp,
            obligation["id"],
        ),
    )


def _resolve_uncertain_operation(
    connection: Any, operation: Any, decision: dict[str, Any], timestamp: str
) -> dict[str, Any]:
    resolution = decision.get("resolution")
    if resolution not in OPERATOR_OPERATION_RESOLUTIONS:
        raise StoreError(
            "resolve_operation requires observed_success, observed_failure, or confirmed_unsent"
        )
    if operation["kind"] not in OPERATOR_RESOLVABLE_OPERATION_KINDS:
        raise StoreError(
            f"operation kind {operation['kind']!r} has no supported resolution"
        )
    evidence = _required_resolution_text(decision, "evidence")
    retained_result = {
        "operator_resolution": resolution,
        "evidence": evidence,
    }
    if decision.get("native_id") is not None:
        retained_result["native_id"] = decision["native_id"]
    if decision.get("result") is not None:
        retained_result["result"] = decision["result"]
    state = {
        "observed_success": "complete",
        "observed_failure": "failed",
        "confirmed_unsent": "canceled",
    }[resolution]
    if operation["state"] != "uncertain" or not operation["reconciliation_used"]:
        retained = json.loads(operation["result_json"] or "{}")
        if operation["state"] == state and retained == retained_result:
            return {"reused": True}
        raise StoreError(
            f"operation {operation['id']} is not uncertain after targeted observation"
        )
    if operation["operator_hold_id"] is None:
        raise StoreError(f"operation {operation['id']} has no retained resolution hold")
    condition = f"operator {resolution.replace('_', ' ')}: {evidence}"
    if operation["kind"] == "turn_start":
        _resolve_turn_start(
            connection, operation, decision, resolution, condition, timestamp
        )
    elif operation["kind"] == "tollgate_worktree_create":
        _resolve_worktree_create(
            connection, operation, decision, resolution, condition, timestamp
        )
    elif operation["kind"] == "tollgate_candidate_create":
        _resolve_candidate_create(
            connection, operation, decision, resolution, condition, timestamp
        )
    elif operation["kind"] in {"tollgate_approve", "beads_close"}:
        _resolve_assignment_operation(
            connection, operation, resolution, condition, timestamp
        )
    elif operation["kind"] == "thread_start":
        _resolve_thread_start(
            connection, operation, decision, resolution, condition, timestamp
        )
    elif operation["kind"] == "beads_create":
        _resolve_obligation_operation(
            connection, operation, decision, resolution, condition, timestamp
        )
    elif (
        operation["kind"] == "setup_runtime_smoke" and resolution == "observed_success"
    ):
        connection.execute(
            "INSERT INTO meta(key, value) VALUES ('desktop_smoke_check', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (timestamp,),
        )
    hold_id = int(operation["operator_hold_id"])
    dangling = []
    for table in ("assignments", "actions", "obligations"):
        if connection.execute(
            f"SELECT 1 FROM {table} WHERE operator_hold_id = ? LIMIT 1", (hold_id,)
        ).fetchone():
            dangling.append(table)
    if dangling:
        raise StoreError(
            f"operation resolution did not transition held target in {', '.join(dangling)}"
        )
    connection.execute(
        "UPDATE holds SET released_at = ? WHERE id = ? AND released_at IS NULL",
        (timestamp, hold_id),
    )
    connection.execute(
        """UPDATE external_operations SET state = ?, result_json = ?, native_id = COALESCE(?, native_id),
           condition = ?, completed_at = ?, updated_at = ? WHERE id = ?""",
        (
            state,
            json.dumps(retained_result, sort_keys=True),
            decision.get("native_id"),
            None if state == "complete" else condition,
            timestamp if state == "complete" else None,
            timestamp,
            operation["id"],
        ),
    )
    return {"reused": False, "state": state}


def apply_archon_decisions(
    store: Store, payload: dict[str, Any], *, connection: Any | None = None
) -> dict[str, Any]:
    """Apply exact approved runs and policies from a frozen Archon outcome."""

    decisions = payload.get("decisions", [])
    created: list[int] = []
    timestamp = utc_now()
    with (
        store.transaction() if connection is None else nullcontext(connection)
    ) as connection:
        enabled_projects = {
            str(row[0])
            for row in connection.execute(
                "SELECT project_id FROM projects WHERE enabled = 1"
            ).fetchall()
        }
        if "global_limit" in payload:
            supplied_global_limit = payload["global_limit"]
            if (
                not isinstance(supplied_global_limit, int)
                or isinstance(supplied_global_limit, bool)
                or supplied_global_limit <= 0
            ):
                raise StoreError("global capacity must be positive")
            global_limit = supplied_global_limit
            global_usage = int(
                connection.execute(
                    "SELECT COALESCE(SUM(global_slots), 0) FROM reservations"
                ).fetchone()[0]
            )
            if global_limit < global_usage:
                raise StoreError(
                    f"global capacity {global_limit} is below active usage {global_usage}"
                )
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
            if set(limits) != enabled_projects:
                missing = sorted(enabled_projects - set(limits))
                unknown = sorted(set(limits) - enabled_projects)
                raise StoreError(
                    "project capacities must exactly cover enabled projects; "
                    f"missing={missing}; unknown={unknown}"
                )
            usage: dict[str, int] = {}
            for reservation in connection.execute(
                "SELECT project_ids FROM reservations"
            ).fetchall():
                for project_id in json.loads(reservation[0]):
                    project = str(project_id)
                    usage[project] = usage.get(project, 0) + 1
            exceeded = {
                project: count
                for project, count in usage.items()
                if count > limits.get(project, 0)
            }
            if exceeded:
                raise StoreError(
                    "project capacity is below active usage: "
                    + ", ".join(
                        f"{project}={count}>{limits.get(project, 0)}"
                        for project, count in sorted(exceeded.items())
                    )
                )
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
            kind = str(policy["kind"])
            scope = policy.get("scope")
            if kind == "sage" and scope is not None:
                raise StoreError("fleet Sage policy cannot have a project scope")
            if kind == "inquisitor" and (
                not isinstance(scope, str) or scope not in enabled_projects
            ):
                raise StoreError("Inquisitor policy requires an enabled project scope")
            supplied_cadence = policy.get("cadence_seconds", 86400)
            if (
                not isinstance(supplied_cadence, int)
                or isinstance(supplied_cadence, bool)
                or supplied_cadence <= 0
            ):
                raise StoreError("recurring policy cadence must be positive")
            cadence = supplied_cadence
            supplied_anchor = policy.get("anchor_at")
            anchor = (
                str(supplied_anchor)
                if supplied_anchor
                else (datetime.now(timezone.utc) + timedelta(seconds=cadence))
                .isoformat()
                .replace("+00:00", "Z")
            )
            try:
                parsed_anchor = datetime.fromisoformat(anchor.replace("Z", "+00:00"))
            except ValueError as error:
                raise StoreError(
                    "recurring policy anchor must be an ISO timestamp"
                ) from error
            if parsed_anchor.tzinfo is None:
                raise StoreError("recurring policy anchor must include a timezone")
            connection.execute(
                """INSERT INTO policies(kind, scope, cadence_seconds, anchor_at, next_due_at, config_json, active)
                   VALUES (?, ?, ?, ?, ?, ?, 1)
                   ON CONFLICT DO UPDATE SET cadence_seconds = excluded.cadence_seconds,
                   anchor_at = excluded.anchor_at, next_due_at = excluded.next_due_at,
                   config_json = excluded.config_json, active = 1""",
                (
                    kind,
                    scope,
                    cadence,
                    anchor,
                    anchor,
                    json.dumps(policy.get("config", {}), sort_keys=True),
                ),
            )
        applied: list[dict[str, Any]] = []
        for decision in decisions:
            if not isinstance(decision, dict):
                raise StoreError("each Archon decision must be an object")
            kind = decision.get("decision")
            if kind == "approve":
                project = decision.get("project")
                beads = decision.get("beads")
                if (
                    not isinstance(project, str)
                    or project not in enabled_projects
                    or not isinstance(beads, list)
                    or not beads
                    or not all(isinstance(item, str) for item in beads)
                    or len(set(beads)) != len(beads)
                ):
                    raise StoreError(
                        "approved decision requires an enabled project and unique ordered beads"
                    )
                placeholders = ",".join("?" for _ in beads)
                lineage_rows = connection.execute(
                    f"""SELECT bead_id, weaver_task_id, lineage_number
                        FROM bead_lineages WHERE bead_id IN ({placeholders})""",
                    tuple(beads),
                ).fetchall()
                origins = {
                    (int(row["weaver_task_id"]), int(row["lineage_number"]))
                    for row in lineage_rows
                }
                if lineage_rows and (
                    len(lineage_rows) != len(beads) or len(origins) != 1
                ):
                    raise StoreError(
                        "an approved run must contain beads from one Weaver lineage"
                    )
                weaver_task_id, lineage_number = (
                    next(iter(origins)) if origins else (None, None)
                )
                cursor = connection.execute(
                    """INSERT INTO runs(project_id, authority, state, priority,
                           weaver_task_id, lineage_number, created_at, updated_at)
                       VALUES (?, ?, 'approved', ?, ?, ?, ?, ?)""",
                    (
                        project,
                        str(decision.get("authority", "archon")),
                        int(decision.get("priority", 0)),
                        weaver_task_id,
                        lineage_number,
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
                    if not isinstance(scope, str) or not scope.strip():
                        raise StoreError(f"bead {bead_id} has no approved scope")
                    if scope != bead["description"]:
                        raise StoreError(
                            f"approved scope for {bead_id} does not match retained scope"
                        )
                    connection.execute(
                        "INSERT INTO run_beads(run_id, bead_id, position, scope_snapshot) VALUES (?, ?, ?, ?)",
                        (run_id, bead_id, position, scope),
                    )
                    connection.execute(
                        """INSERT INTO assignments(
                               run_id, bead_id, stage, scope_snapshot,
                               weaver_task_id, lineage_number, created_at, updated_at
                           ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?)""",
                        (
                            run_id,
                            bead_id,
                            scope,
                            weaver_task_id,
                            lineage_number,
                            timestamp,
                            timestamp,
                        ),
                    )
                applied.append({"decision": kind, "run_id": run_id})
                continue
            if kind == "hold":
                scope = decision.get("scope")
                target = decision.get("target")
                reason = decision.get("reason")
                release = decision.get("release_condition")
                if scope not in {"global", "project", "run", "assignment"}:
                    raise StoreError("hold requires a supported scope")
                if scope != "global" and (
                    not isinstance(target, (str, int)) or isinstance(target, bool)
                ):
                    raise StoreError("non-global hold requires a target")
                if scope != "global":
                    table, column = {
                        "project": ("projects", "project_id"),
                        "run": ("runs", "id"),
                        "assignment": ("assignments", "id"),
                    }[str(scope)]
                    if (
                        connection.execute(
                            f"SELECT 1 FROM {table} WHERE {column} = ?", (target,)
                        ).fetchone()
                        is None
                    ):
                        raise StoreError(f"hold target {scope}:{target} does not exist")
                if not isinstance(reason, str) or not reason.strip():
                    raise StoreError("hold requires a reason")
                if not isinstance(release, str) or not release.strip():
                    raise StoreError("hold requires a release condition")
                cursor = connection.execute(
                    "INSERT INTO holds(scope, target, reason, urgent, release_condition, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        scope,
                        None if scope == "global" else str(target),
                        reason.strip(),
                        int(bool(decision.get("urgent", False))),
                        release.strip(),
                        timestamp,
                    ),
                )
                if scope == "run":
                    connection.execute(
                        "UPDATE runs SET state = 'held', updated_at = ? WHERE id = ?",
                        (timestamp, target),
                    )
                applied.append({"decision": kind, "hold_id": int(cursor.lastrowid)})
                continue
            if kind == "release_hold":
                hold_id = decision.get("hold_id")
                if not isinstance(hold_id, int) or isinstance(hold_id, bool):
                    raise StoreError("release_hold requires hold_id")
                retained_hold = connection.execute(
                    "SELECT scope, target, released_at FROM holds WHERE id = ?",
                    (hold_id,),
                ).fetchone()
                if retained_hold is None:
                    raise StoreError(f"hold {hold_id} does not exist")
                if retained_hold["released_at"] is not None:
                    applied.append(
                        {"decision": kind, "hold_id": hold_id, "reused": True}
                    )
                    continue
                unresolved_operation = connection.execute(
                    """SELECT id FROM external_operations
                       WHERE operator_hold_id = ? AND state = 'uncertain'
                         AND reconciliation_used = 1""",
                    (hold_id,),
                ).fetchone()
                if unresolved_operation is not None:
                    raise StoreError(
                        f"hold {hold_id} protects unresolved external operation "
                        f"{unresolved_operation['id']}; use resolve_operation"
                    )
                changed = connection.execute(
                    "UPDATE holds SET released_at = ? WHERE id = ? AND released_at IS NULL",
                    (timestamp, hold_id),
                ).rowcount
                if changed != 1:
                    raise StoreError(f"hold {hold_id} changed concurrently")
                if retained_hold is not None and retained_hold["scope"] == "run":
                    connection.execute(
                        """UPDATE runs SET state = CASE WHEN executor_task_id IS NULL
                           THEN 'approved' ELSE 'active' END, updated_at = ?
                           WHERE id = ? AND state = 'held'""",
                        (timestamp, retained_hold["target"]),
                    )
                connection.execute(
                    """UPDATE assignments SET operator_hold_id = NULL,
                       next_attempt_at = COALESCE(next_attempt_at, ?), updated_at = ?
                       WHERE operator_hold_id = ? AND stage = 'recovering'""",
                    (timestamp, timestamp, hold_id),
                )
                applied.append({"decision": kind, "hold_id": hold_id})
                continue
            if kind == "resolve_operation":
                operation_id = decision.get("operation_id")
                if not isinstance(operation_id, int) or isinstance(operation_id, bool):
                    raise StoreError("resolve_operation requires integer operation_id")
                operation = connection.execute(
                    "SELECT * FROM external_operations WHERE id = ?", (operation_id,)
                ).fetchone()
                if operation is None:
                    raise StoreError(f"operation {operation_id} does not exist")
                resolved = _resolve_uncertain_operation(
                    connection, operation, decision, timestamp
                )
                applied.append(
                    {
                        "decision": kind,
                        "operation_id": operation_id,
                        "resolution": decision.get("resolution"),
                        **resolved,
                    }
                )
                continue
            if kind == "cancel_run":
                run_id = decision.get("run_id")
                reason = decision.get("reason")
                if (
                    not isinstance(run_id, int)
                    or isinstance(run_id, bool)
                    or not isinstance(reason, str)
                    or not reason.strip()
                ):
                    raise StoreError("cancel_run requires run_id and reason")
                run = connection.execute(
                    "SELECT state FROM runs WHERE id = ?", (run_id,)
                ).fetchone()
                if run is None or run["state"] in {"completed", "canceled"}:
                    raise StoreError(f"run {run_id} cannot be canceled")
                active = connection.execute(
                    """SELECT 1 FROM actions WHERE assignment_id IN
                       (SELECT id FROM assignments WHERE run_id = ?)
                       AND state IN ('starting','active','terminal','uncertain') LIMIT 1""",
                    (run_id,),
                ).fetchone()
                if active is not None:
                    raise StoreError(f"run {run_id} has an active action")
                connection.execute(
                    "UPDATE assignments SET stage = 'canceled', condition = ?, updated_at = ? WHERE run_id = ? AND stage NOT IN ('completed','canceled')",
                    (reason.strip(), timestamp, run_id),
                )
                connection.execute(
                    "UPDATE runs SET state = 'canceled', updated_at = ? WHERE id = ?",
                    (timestamp, run_id),
                )
                schedule_run_archival(connection, run_id, timestamp)
                applied.append({"decision": kind, "run_id": run_id})
                continue
            if kind == "resolve_escalation":
                assignment_id = decision.get("assignment_id")
                resolution = decision.get("resolution")
                reason = decision.get("reason")
                if (
                    not isinstance(assignment_id, int)
                    or isinstance(assignment_id, bool)
                    or resolution
                    not in {"retry", "rescope", "cancel", "complete_non_code"}
                    or not isinstance(reason, str)
                    or not reason.strip()
                ):
                    raise StoreError(
                        "resolve_escalation requires assignment_id, "
                        "retry|rescope|cancel|complete_non_code, and reason"
                    )
                assignment = connection.execute(
                    "SELECT * FROM assignments WHERE id = ?", (assignment_id,)
                ).fetchone()
                if assignment is None or assignment["stage"] != "recovering":
                    raise StoreError(
                        f"assignment {assignment_id} is not an escalated recovery"
                    )
                if assignment["operator_hold_id"]:
                    connection.execute(
                        "UPDATE holds SET released_at = ? WHERE id = ? AND released_at IS NULL",
                        (timestamp, assignment["operator_hold_id"]),
                    )
                if resolution == "complete_non_code":
                    evidence = decision.get("evidence")
                    if not isinstance(evidence, str) or not evidence.strip():
                        raise StoreError(
                            "complete_non_code resolution requires nonempty evidence"
                        )
                    if any(
                        assignment[field]
                        for field in (
                            "candidate_id",
                            "source_oid",
                            "tested_oid",
                            "mandate_candidate_id",
                        )
                    ):
                        raise StoreError(
                            "complete_non_code resolution requires an assignment "
                            "without a repository candidate"
                        )
                    unresolved_candidate = connection.execute(
                        """SELECT id FROM external_operations
                           WHERE kind = 'tollgate_candidate_create' AND target = ?
                             AND state IN ('intent','sent','uncertain') LIMIT 1""",
                        (str(assignment_id),),
                    ).fetchone()
                    if unresolved_candidate is not None:
                        raise StoreError(
                            "complete_non_code resolution cannot bypass an unresolved "
                            "Tollgate operation"
                        )
                    connection.execute(
                        """UPDATE assignments SET stage = 'delivering',
                           completion_kind = 'non_code', completion_evidence = ?,
                           operator_hold_id = NULL, next_attempt_at = NULL,
                           condition = NULL, updated_at = ? WHERE id = ?""",
                        (evidence.strip(), timestamp, assignment_id),
                    )
                elif resolution == "cancel":
                    connection.execute(
                        "UPDATE assignments SET stage = 'canceled', operator_hold_id = NULL, next_attempt_at = NULL, condition = ?, updated_at = ? WHERE id = ?",
                        (reason.strip(), timestamp, assignment_id),
                    )
                    remaining = connection.execute(
                        """SELECT 1 FROM assignments WHERE run_id = ?
                           AND stage NOT IN ('completed','canceled') LIMIT 1""",
                        (assignment["run_id"],),
                    ).fetchone()
                    if remaining is None:
                        connection.execute(
                            "UPDATE runs SET state = 'canceled', updated_at = ? WHERE id = ?",
                            (timestamp, assignment["run_id"]),
                        )
                        schedule_run_archival(
                            connection, int(assignment["run_id"]), timestamp
                        )
                else:
                    approved_scope = decision.get("scope")
                    if resolution == "rescope":
                        if (
                            not isinstance(approved_scope, str)
                            or not approved_scope.strip()
                        ):
                            raise StoreError("rescope resolution requires exact scope")
                    else:
                        approved_scope = assignment["scope_snapshot"]
                    target_stage = (
                        "correcting"
                        if resolution == "rescope" and assignment["worktree_path"]
                        else (
                            {
                                "queued": "queued",
                                "preparing": "preparing",
                                "implementing": "preparing",
                                "review_pending": "review_pending",
                                "reviewing": "review_pending",
                                "correcting": "correcting",
                                "delivering": "delivering",
                            }.get(str(assignment["prior_stage"]), "queued")
                            if resolution == "retry"
                            else "queued"
                        )
                    )
                    connection.execute(
                        """UPDATE assignments SET stage = ?, scope_snapshot = ?,
                           candidate_id = CASE WHEN ? = 'rescope' THEN NULL ELSE candidate_id END,
                           source_oid = CASE WHEN ? = 'rescope' THEN NULL ELSE source_oid END,
                           tested_oid = CASE WHEN ? = 'rescope' THEN NULL ELSE tested_oid END,
                           mandate_candidate_id = CASE WHEN ? = 'rescope' THEN NULL ELSE mandate_candidate_id END,
                           mandate_scope = CASE WHEN ? = 'rescope' THEN NULL ELSE mandate_scope END,
                           repair_permissions = CASE WHEN ? = 'rescope' THEN '[]' ELSE repair_permissions END,
                           completion_kind = CASE WHEN ? = 'rescope' THEN NULL ELSE completion_kind END,
                           completion_evidence = CASE WHEN ? = 'rescope' THEN NULL ELSE completion_evidence END,
                           operator_hold_id = NULL, next_attempt_at = NULL, condition = ?, updated_at = ?
                           WHERE id = ?""",
                        (
                            target_stage,
                            approved_scope,
                            resolution,
                            resolution,
                            resolution,
                            resolution,
                            resolution,
                            resolution,
                            resolution,
                            resolution,
                            reason.strip(),
                            timestamp,
                            assignment_id,
                        ),
                    )
                applied.append(
                    {
                        "decision": kind,
                        "assignment_id": assignment_id,
                        "resolution": resolution,
                    }
                )
                continue
            if kind == "set_priority":
                run_id = decision.get("run_id")
                priority = decision.get("priority")
                if (
                    not isinstance(run_id, int)
                    or isinstance(run_id, bool)
                    or not isinstance(priority, int)
                    or isinstance(priority, bool)
                ):
                    raise StoreError(
                        "set_priority requires integer run_id and priority"
                    )
                if (
                    connection.execute(
                        "SELECT 1 FROM runs WHERE id = ?", (run_id,)
                    ).fetchone()
                    is None
                ):
                    raise StoreError(f"unknown run {run_id}")
                connection.execute(
                    "UPDATE runs SET priority = ?, updated_at = ? WHERE id = ?",
                    (priority, timestamp, run_id),
                )
                applied.append({"decision": kind, "run_id": run_id})
                continue
            if kind == "suspend_policy":
                policy_kind = decision.get("kind")
                scope = decision.get("scope")
                changed = connection.execute(
                    "UPDATE policies SET active = 0 WHERE kind = ? AND scope IS ? AND active = 1",
                    (policy_kind, scope),
                ).rowcount
                if policy_kind not in {"sage", "inquisitor"} or changed != 1:
                    raise StoreError("suspend_policy names no active policy")
                applied.append({"decision": kind, "kind": policy_kind, "scope": scope})
                continue
            if kind == "request_specialist":
                specialist = decision.get("kind")
                projects = decision.get("projects", [])
                if specialist not in {"sage", "inquisitor"}:
                    raise StoreError("request_specialist requires sage or inquisitor")
                if not isinstance(projects, list) or not all(
                    isinstance(project, str) and project in enabled_projects
                    for project in projects
                ):
                    raise StoreError("specialist projects must all be enabled")
                if specialist == "inquisitor" and not projects:
                    raise StoreError("Inquisitor requires at least one project")
                scope = json.dumps(
                    {"global": not projects, "projects": projects}, sort_keys=True
                )
                cursor = connection.execute(
                    """INSERT INTO occurrences(kind, scope, authority, prompt, state, created_at, updated_at)
                       VALUES (?, ?, 'archon', ?, 'queued', ?, ?)""",
                    (specialist, scope, decision.get("prompt"), timestamp, timestamp),
                )
                applied.append(
                    {"decision": kind, "occurrence_id": int(cursor.lastrowid)}
                )
                continue
            if kind == "set_models":
                bead_id = decision.get("bead_id")
                values = [
                    decision.get("executor_model"),
                    decision.get("executor_reasoning_effort"),
                    decision.get("overseer_model"),
                    decision.get("overseer_reasoning_effort"),
                ]
                rationale = decision.get("rationale")
                if (
                    not isinstance(bead_id, str)
                    or not all(
                        isinstance(value, str) and value.strip() for value in values
                    )
                    or not isinstance(rationale, str)
                    or not rationale.strip()
                ):
                    raise StoreError(
                        "set_models requires bead_id, four model settings, and rationale"
                    )
                provisioned = connection.execute(
                    "SELECT 1 FROM assignments WHERE bead_id = ? AND (executor_task_id IS NOT NULL OR overseer_task_id IS NOT NULL)",
                    (bead_id,),
                ).fetchone()
                if provisioned is not None:
                    raise StoreError("models cannot change after pair provisioning")
                changed = connection.execute(
                    """UPDATE beads SET executor_model = ?, executor_reasoning_effort = ?,
                       overseer_model = ?, overseer_reasoning_effort = ?,
                       model_provenance = 'archon', updated_at = ? WHERE bead_id = ?""",
                    (*values, timestamp, bead_id),
                ).rowcount
                if changed != 1:
                    raise StoreError(f"unknown bead {bead_id}")
                connection.execute(
                    """INSERT INTO model_decisions(bead_id, rationale, created_at)
                       VALUES (?, ?, ?) ON CONFLICT(bead_id) DO UPDATE SET
                       rationale = excluded.rationale, created_at = excluded.created_at""",
                    (bead_id, rationale.strip(), timestamp),
                )
                applied.append({"decision": kind, "bead_id": bead_id})
                continue
            if kind == "retire_archon":
                reason = decision.get("reason")
                if not isinstance(reason, str) or not reason.strip():
                    raise StoreError("retire_archon requires a reason")
                existing = connection.execute(
                    "SELECT value FROM meta WHERE key = 'archon_succession_request'"
                ).fetchone()
                if existing is not None:
                    raise StoreError("an Archon succession is already pending")
                connection.execute(
                    "INSERT INTO meta(key, value) VALUES ('archon_succession_request', ?)",
                    (
                        json.dumps(
                            {
                                "task_id": None,
                                "reason": reason,
                                "requested_at": timestamp,
                            },
                            sort_keys=True,
                        ),
                    ),
                )
                applied.append({"decision": kind})
                continue
            raise StoreError(f"unsupported Archon decision: {kind!r}")
    return {"created_runs": created, "applied_decisions": applied}
