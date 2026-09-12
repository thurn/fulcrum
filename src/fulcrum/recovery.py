"""Durable escalation facts and bounded operational recovery decisions."""

from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Literal, cast

from fulcrum.records import (
    EmergencyAuthorization,
    Escalation,
    ExecutorEvidenceRecord,
    FailureEvent,
    ProgressRecord,
    RecoveryAttempt,
    ReviewEntry,
    SuspendedInvestigation,
    validate_record,
)
from fulcrum.resources import utc_text


def build_escalation(
    progress: ProgressRecord,
    *,
    boundary: str,
    error: str,
    evidence: list[str],
    attempts: list[RecoveryAttempt],
    retained_work: list[str],
    untried_recovery: list[str],
    requested_decision: str,
    expected_next_actor: str,
    now: str,
    delivery_error: str | None = None,
) -> ProgressRecord:
    """Attach the complete evidence-bearing escalation envelope to owned progress."""

    required = [boundary, error, requested_decision, expected_next_actor]
    if (
        any(not value.strip() for value in required)
        or not evidence
        or not retained_work
    ):
        raise ValueError(
            "escalation requires boundary, evidence, retained work, and decision"
        )
    escalation: Escalation = {
        "boundary": boundary,
        "error": error,
        "evidence": evidence,
        "attempts": attempts,
        "retained_work": retained_work,
        "untried_recovery": untried_recovery,
        "requested_decision": requested_decision,
        "expected_next_actor": expected_next_actor,
    }
    result = deepcopy(progress)
    result.update(
        updated_at=now,
        expected_next_actor=expected_next_actor,
        expected_next_action=requested_decision,
        handoff_needed=True,
        handoff_sent=False,
        delivery_error=delivery_error,
        escalation=escalation,
    )
    return cast(ProgressRecord, validate_record(result))


def record_candidate_failure(
    evidence: ExecutorEvidenceRecord,
    *,
    boundary: str,
    error_evidence: str,
    now: datetime,
) -> ExecutorEvidenceRecord:
    """Start one bounded diagnosis window while retaining prior failure history."""

    if not boundary or not error_evidence:
        raise ValueError("candidate failure requires boundary and evidence")
    event: FailureEvent = {
        "boundary": boundary,
        "occurred_at": utc_text(now),
        "evidence": error_evidence,
        "unchanged_retry_used": False,
        "diagnosis_deadline": utc_text(now + timedelta(minutes=15)),
    }
    result = deepcopy(evidence)
    result.setdefault("failure_history", []).append(event)
    result["updated_at"] = utc_text(now)
    return cast(ExecutorEvidenceRecord, validate_record(result))


def candidate_failure_action(
    failure: FailureEvent,
    *,
    now: datetime,
    diagnose_complete: bool,
    retry_hypothesis: str | None = None,
) -> Literal[
    "tg_diagnose", "retry_once", "focused_diagnosis", "repair_rollback_or_escalate"
]:
    """Enforce diagnosis first, one hypothesized retry, then a fifteen-minute bound."""

    if not diagnose_complete:
        return "tg_diagnose"
    if not failure["unchanged_retry_used"] and retry_hypothesis:
        return "retry_once"
    deadline_text = failure["diagnosis_deadline"]
    if deadline_text is None:
        return "repair_rollback_or_escalate"
    deadline = datetime.fromisoformat(deadline_text.replace("Z", "+00:00"))
    if now.astimezone(timezone.utc) < deadline:
        return "focused_diagnosis"
    return "repair_rollback_or_escalate"


def mark_unchanged_retry(
    evidence: ExecutorEvidenceRecord,
    *,
    boundary: str,
    hypothesis: str,
    now: str,
) -> ExecutorEvidenceRecord:
    """Consume the single unchanged retry only when its hypothesis is stated."""

    if not hypothesis.strip():
        raise ValueError("unchanged retry requires a stated hypothesis")
    result = deepcopy(evidence)
    matches = [
        event
        for event in result.get("failure_history", [])
        if event["boundary"] == boundary
    ]
    if not matches:
        raise ValueError(f"no retained failure for boundary {boundary!r}")
    event = matches[-1]
    if event["unchanged_retry_used"]:
        raise ValueError("unchanged retry already used at this boundary")
    event["unchanged_retry_used"] = True
    event["evidence"] += f"; retry hypothesis: {hypothesis}"
    retry_time = datetime.fromisoformat(now.replace("Z", "+00:00"))
    event["diagnosis_deadline"] = utc_text(retry_time + timedelta(minutes=15))
    result["updated_at"] = now
    return cast(ExecutorEvidenceRecord, validate_record(result))


def suspend_for_investigation(
    evidence: ExecutorEvidenceRecord,
    *,
    investigation_task_id: str,
    project_id: str,
    reason: str,
    evidence_reference: str,
    review_history: list[ReviewEntry],
    now: str,
) -> ExecutorEvidenceRecord:
    """Push the current assignment evidence before a fresh owned investigation."""

    suspended: SuspendedInvestigation = {
        "task_id": investigation_task_id,
        "project_id": project_id,
        "reason": reason,
        "evidence_reference": evidence_reference,
        "worktree_path": evidence["worktree_path"],
        "source_oid": evidence["source_oid"],
        "candidate_id": evidence["candidate_id"],
        "review_history": deepcopy(review_history),
        "failure_history": deepcopy(evidence.get("failure_history", [])),
    }
    result = deepcopy(evidence)
    result["suspended_investigations"].append(suspended)
    result["updated_at"] = now
    return cast(ExecutorEvidenceRecord, validate_record(result))


def resume_suspended_investigation(
    evidence: ExecutorEvidenceRecord, *, now: str
) -> tuple[ExecutorEvidenceRecord, SuspendedInvestigation]:
    """Pop only the newest investigation so nested work resumes in LIFO order."""

    result = deepcopy(evidence)
    if not result["suspended_investigations"]:
        raise ValueError("no suspended investigation to resume")
    suspended = result["suspended_investigations"].pop()
    result["updated_at"] = now
    return cast(ExecutorEvidenceRecord, validate_record(result)), suspended


def investigation_project(original_project: str, affected_project: str) -> str:
    """Scope infrastructure repair to the project that owns the suspected fault."""

    return (
        original_project if original_project == affected_project else affected_project
    )


def unavailable_task_action(
    *,
    original_available: bool,
    candidate_promoted: bool | None,
    dirty_work_disposition_known: bool,
    retained_commit: str | None,
) -> Literal[
    "resume_original",
    "reconcile_promoted_candidate",
    "inspect_and_preserve_dirty_work",
    "fresh_worktree_from_retained_commit",
    "record_disposition_then_replace",
]:
    """Choose recovery without transferring ownership of an old worktree."""

    if original_available:
        return "resume_original"
    if candidate_promoted is True:
        return "reconcile_promoted_candidate"
    if not dirty_work_disposition_known:
        return "inspect_and_preserve_dirty_work"
    if retained_commit:
        return "fresh_worktree_from_retained_commit"
    return "record_disposition_then_replace"


def completion_recovery(evidence: ExecutorEvidenceRecord) -> str | None:
    """Route promoted delivery obligations without asking for implementation redo."""

    if evidence["push_state"] == "failed":
        return "retry_source_push"
    if evidence["cleanup_state"] == "failed":
        return "reconcile_owned_cleanup"
    return None


def emergency_authorization(
    *,
    outage_evidence: str,
    attempted_recovery: list[str],
    scope: str,
    repair_owner: str,
    permitted_runtime_changes: list[str],
    certified_release_oid: str,
) -> EmergencyAuthorization:
    """Record the narrow provisional path only after operational recovery failed."""

    if not outage_evidence or not attempted_recovery or not repair_owner:
        raise ValueError(
            "emergency repair requires outage and attempted recovery evidence"
        )
    if not scope.startswith("tollgate:"):
        raise ValueError("emergency repair scope must be owned by Tollgate")
    if not re.fullmatch(r"[0-9a-f]{40,64}", certified_release_oid):
        raise ValueError("emergency repair requires an exact certified release OID")
    return {
        "outage_evidence": outage_evidence,
        "attempted_recovery": attempted_recovery,
        "scope": scope,
        "repair_owner": repair_owner,
        "permitted_runtime_changes": permitted_runtime_changes,
        "certified_release_oid": certified_release_oid,
        "provisional": True,
    }


def emergency_next_step(
    *,
    service_restored: bool,
    provisional_reviewed: bool,
    normally_certified: bool,
    installed_version_matches: bool,
) -> Literal[
    "review_provisional_repair",
    "install_reviewed_provisional_repair",
    "certify_normally",
    "reconcile_installed_version",
    "cleanup_emergency_resources",
]:
    """Keep provisional repair distinct from normal certification and cleanup."""

    if not provisional_reviewed:
        return "review_provisional_repair"
    if not service_restored:
        return "install_reviewed_provisional_repair"
    if not normally_certified:
        return "certify_normally"
    if not installed_version_matches:
        return "reconcile_installed_version"
    return "cleanup_emergency_resources"
