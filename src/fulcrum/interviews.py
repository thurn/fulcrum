"""Bounded Sage interview decisions; Codex actions stay with the role."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from datetime import datetime, timedelta
from typing import cast

from fulcrum.records import InterviewRecord, validate_record
from fulcrum.resources import utc_text


def begin_interview(
    sage: str, run: str, subject: str, host: str, *, prior_archived: bool, now: datetime
) -> InterviewRecord:
    """Persist this record BEFORE any unarchive or interview request."""
    if now.tzinfo is None:
        raise ValueError("interview time must be timezone-aware")
    if not all((sage, run, subject, host)) or sage == subject:
        raise ValueError(
            "interview requires distinct resolved Sage and subject identities"
        )
    identity = hashlib.sha256(f"{sage}\0{run}\0{host}\0{subject}".encode()).hexdigest()[
        :24
    ]
    return cast(
        InterviewRecord,
        validate_record(
            {
                "record_kind": "interview",
                "schema_version": 1,
                "writer_id": sage,
                "updated_at": utc_text(now),
                "sage_task_id": sage,
                "run_id": run,
                "interview_id": identity,
                "subject_task_id": subject,
                "subject_host_id": host,
                "prior_archived": prior_archived,
                "reminder_state": "not_due",
                "completion_state": "active",
                "reopen_state": "not_requested",
                "archive_restore_state": "pending" if prior_archived else "not_needed",
                "reminder_due_at": utc_text(now + timedelta(hours=1)),
                "finish_due_at": utc_text(now + timedelta(hours=2)),
                "reminder_attempted": False,
                "response_reference": None,
            }
        ),
    )


def interview_action(record: InterviewRecord, now: datetime) -> str:
    """One later-patrol reminder, then bounded completion/restoration obligations."""
    if now.tzinfo is None:
        raise ValueError("interview time must be timezone-aware")
    if record["completion_state"] != "active":
        return "none"
    required = (
        "finish_due_at",
        "reminder_due_at",
        "reopen_state",
        "archive_restore_state",
    )
    if any(key not in record for key in required):
        return "inspect_legacy"
    finished = bool(record.get("response_reference")) or now >= datetime.fromisoformat(
        record["finish_due_at"].replace("Z", "+00:00")
    )
    if finished:
        if record["archive_restore_state"] == "pending":
            if record["reopen_state"] == "uncertain":
                return "inspect_archive"
            if record["reopen_state"] == "confirmed":
                return "restore_archive"
        return "complete"
    if record["prior_archived"] and record["reopen_state"] == "not_requested":
        return "unarchive"
    if record["reopen_state"] == "uncertain":
        return "inspect_archive"
    if now >= datetime.fromisoformat(
        record["reminder_due_at"].replace("Z", "+00:00")
    ) and not record.get("reminder_attempted", False):
        return "remind"
    return "wait"


def advance_interview(
    record: InterviewRecord, event: str, now: datetime, *, reference: str | None = None
) -> InterviewRecord:
    """Persist intents before tools; confirm effects only from inspected tool evidence."""
    if record["completion_state"] != "active":
        raise ValueError("interview is already terminal")
    result = deepcopy(record)
    action = interview_action(record, now)
    if event == "reopening" and action == "unarchive":
        result["reopen_state"] = "uncertain"
    elif (
        event == "reopened" and record.get("reopen_state") == "uncertain" and reference
    ):
        result["reopen_state"] = "confirmed"
    elif event == "reminder_attempted" and action == "remind":
        result["reminder_attempted"] = True
        result["reminder_state"] = "due"
    elif event in {"reminder_sent", "reminder_failed"} and record.get(
        "reminder_attempted"
    ):
        result["reminder_state"] = "sent" if event == "reminder_sent" else "failed"
    elif event == "reply" and reference:
        result["response_reference"] = reference
    elif (
        event == "restored"
        and action in {"restore_archive", "inspect_archive"}
        and reference
    ):
        # In the uncertain case, inspect the task and confirm it is already archived.
        result["archive_restore_state"] = "complete"
        result["reopen_state"] = "confirmed"
        result["finish_due_at"] = utc_text(now)
    elif event == "complete" and action == "complete":
        result["completion_state"] = "complete"
        result["outcome"] = (
            "responded" if result.get("response_reference") else "missing_response"
        )
        if result.get("reopen_state") == "not_requested":
            result["archive_restore_state"] = "not_needed"
    else:
        raise ValueError(
            f"invalid interview transition {event!r} while action is {action!r}"
        )
    if reference:
        result["last_evidence_reference"] = reference
    result["updated_at"] = utc_text(now)
    return cast(InterviewRecord, validate_record(result))
