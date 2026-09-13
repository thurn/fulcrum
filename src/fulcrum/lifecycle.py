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
                "UPDATE actions SET state = 'processed', updated_at = ? WHERE id = ?",
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
            "SELECT candidate_id, source_oid FROM assignments WHERE id = ?",
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
        if batch is not None:
            expected = {
                int(row["update_id"])
                for row in store.rows(
                    "SELECT update_id FROM batch_updates WHERE batch_id = ?",
                    (batch["id"],),
                )
            }
            handled = set(payload.get("handled_update_ids", []))
            if handled != expected:
                raise StoreError(
                    "Archon must handle every frozen update exactly; "
                    f"expected={sorted(expected)} handled={sorted(handled)}"
                )
        result = apply_archon_decisions(store, payload, connection=connection)
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


def _archive_run_pair(connection: Any, run_id: int, timestamp: str) -> None:
    run = connection.execute(
        "SELECT executor_task_id, overseer_task_id FROM runs WHERE id = ?", (run_id,)
    ).fetchone()
    if run is None:
        return
    for task_id in (run["executor_task_id"], run["overseer_task_id"]):
        if task_id is None:
            continue
        task = connection.execute(
            "SELECT native_thread_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if task is not None:
            connection.execute(
                """INSERT OR IGNORE INTO obligations(
                       kind, identity, target, state, created_at, updated_at
                   ) VALUES ('archive', ?, ?, 'pending', ?, ?)""",
                (str(task_id), task["native_thread_id"], timestamp, timestamp),
            )


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
            anchor = str(policy.get("anchor_at") or timestamp)
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
                cursor = connection.execute(
                    "INSERT INTO runs(project_id, authority, state, priority, created_at, updated_at) VALUES (?, ?, 'approved', ?, ?, ?)",
                    (
                        project,
                        str(decision.get("authority", "archon")),
                        int(decision.get("priority", 0)),
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
                    connection.execute(
                        "INSERT INTO run_beads(run_id, bead_id, position, scope_snapshot) VALUES (?, ?, ?, ?)",
                        (run_id, bead_id, position, scope),
                    )
                    connection.execute(
                        "INSERT INTO assignments(run_id, bead_id, stage, scope_snapshot, created_at, updated_at) VALUES (?, ?, 'queued', ?, ?, ?)",
                        (run_id, bead_id, scope, timestamp, timestamp),
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
                    "SELECT scope, target FROM holds WHERE id = ? AND released_at IS NULL",
                    (hold_id,),
                ).fetchone()
                changed = connection.execute(
                    "UPDATE holds SET released_at = ? WHERE id = ? AND released_at IS NULL",
                    (timestamp, hold_id),
                ).rowcount
                if changed != 1:
                    raise StoreError(f"hold {hold_id} is missing or already released")
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
                _archive_run_pair(connection, run_id, timestamp)
                applied.append({"decision": kind, "run_id": run_id})
                continue
            if kind == "resolve_escalation":
                assignment_id = decision.get("assignment_id")
                resolution = decision.get("resolution")
                reason = decision.get("reason")
                if (
                    not isinstance(assignment_id, int)
                    or isinstance(assignment_id, bool)
                    or resolution not in {"retry", "rescope", "cancel"}
                    or not isinstance(reason, str)
                    or not reason.strip()
                ):
                    raise StoreError(
                        "resolve_escalation requires assignment_id, retry|rescope|cancel, and reason"
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
                if resolution == "cancel":
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
                        _archive_run_pair(
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
