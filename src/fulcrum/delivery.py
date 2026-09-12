"""Review accounting; Tollgate remains the certification authority."""

from __future__ import annotations

from copy import deepcopy
from typing import Literal, cast

from fulcrum.records import AssignmentRecord, Mandate, ReviewEntry, validate_record


def record_review(
    assignment: AssignmentRecord,
    candidate: str,
    source_oid: str,
    now: str,
    *,
    accepted: bool,
    notes: str,
    evidence_complete: bool,
    allowed_replacements: list[str] | None = None,
    archon_decision: str | None = None,
) -> AssignmentRecord:
    """Record a substantive new source review; evidence requests do not count."""
    result = deepcopy(assignment)
    history = result["review_history"]
    if not evidence_complete or any(
        r["candidate_id"] == candidate or r.get("source_oid") == source_oid
        for r in history
    ):
        return result
    if any(r["outcome"] == "escalated" for r in history) and not archon_decision:
        raise ValueError("third rejection requires a recorded Archon decision")
    if not candidate or not source_oid or not notes.strip():
        raise ValueError("review requires exact identities and findings")
    failures = sum(r["outcome"] in {"changes_requested", "escalated"} for r in history)
    outcome: Literal["approved", "changes_requested", "escalated"]
    if accepted:
        outcome = "approved"
    elif failures >= 2:
        outcome = "escalated"
    else:
        outcome = "changes_requested"
    history.append(
        cast(
            ReviewEntry,
            {
                "attempt": len(history) + 1,
                "outcome": outcome,
                "candidate_id": candidate,
                "source_oid": source_oid,
                "at": now,
                "notes": notes
                + (f"\nArchon decision: {archon_decision}" if archon_decision else ""),
            },
        )
    )
    result["updated_at"] = now
    mandate: Mandate | None = None
    if accepted:
        mandate = {
            "candidate_id": candidate,
            "scope": result["scope_reference"],
            "granted_at": now,
            "allowed_replacements": allowed_replacements or [],
        }
    result["mandate"] = mandate
    return cast(AssignmentRecord, validate_record(result))


def authorize_replacement(
    assignment: AssignmentRecord, original: str, replacement: str, reason: str, now: str
) -> AssignmentRecord:
    """Overseer confirms that a concrete replacement is within its mandate."""
    mandate = assignment["mandate"]
    if (
        mandate is None
        or mandate["candidate_id"] != original
        or reason not in mandate.get("allowed_replacements", [])
        or not replacement
        or replacement == original
    ):
        raise ValueError(
            "replacement requires matching explicit mandate and permitted scope"
        )
    result = deepcopy(assignment)
    updated = deepcopy(mandate)
    updated.update(
        candidate_id=replacement, replaces_candidate_id=original, granted_at=now
    )
    result.update(mandate=updated, updated_at=now)
    return cast(AssignmentRecord, validate_record(result))
