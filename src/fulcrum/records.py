"""Typed local record contracts and JSON Schema validation."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict, cast

from jsonschema import Draft202012Validator, FormatChecker  # pyre-ignore[21]

SCHEMA_VERSION = 1

RecordKind = Literal[
    "installation",
    "project_registry",
    "role_run_registry",
    "holds_jobs",
    "assignment",
    "progress",
    "executor_evidence",
    "interview",
]


class InstallationRecord(TypedDict):
    record_kind: Literal["installation"]
    schema_version: int
    writer_id: str
    updated_at: str
    brain_root: str
    state_root: str
    host_id: str
    configured_services: list[str]
    observations: dict[str, str]


class Project(TypedDict):
    project_id: str
    repo_path: str
    host_id: str
    codex_project_id: str | None
    tollgate_repo_id: str | None
    enabled: bool


class ProjectRegistryRecord(TypedDict):
    record_kind: Literal["project_registry"]
    schema_version: int
    writer_id: str
    updated_at: str
    projects: list[Project]


class ModelAuthorization(TypedDict):
    source: Literal["human", "plan", "default"]
    reference: str | None


class RoleRun(TypedDict):
    role: str
    project_id: str
    host_id: str
    task_id: str | None
    identity_state: Literal["resolved", "pending", "unavailable"]
    role_number: int | None
    run_id: str
    pair_id: str | None
    title: str
    selected_model: str
    selected_reasoning: str
    model_authorization: ModelAuthorization
    skill_revision: str
    client_thread_id: NotRequired[str]
    agent_recommendation: NotRequired[str | None]


class RoleRunRegistryRecord(TypedDict):
    record_kind: Literal["role_run_registry"]
    schema_version: int
    writer_id: str
    updated_at: str
    current_archon_task_id: str | None
    roles: list[RoleRun]


class Hold(TypedDict):
    hold_id: str
    scope: str
    reason: str
    release_condition: str
    permitted_exceptions: list[str]


class RecurringJob(TypedDict):
    job_id: str
    cadence_anchor: str
    next_due: str
    active_run_id: str | None


class HoldsJobsRecord(TypedDict):
    record_kind: Literal["holds_jobs"]
    schema_version: int
    writer_id: str
    updated_at: str
    holds: list[Hold]
    recurring_jobs: list[RecurringJob]


class ReviewEntry(TypedDict):
    attempt: int
    outcome: Literal["approved", "changes_requested", "escalated"]
    candidate_id: str
    at: str
    notes: str


class Mandate(TypedDict):
    candidate_id: str
    scope: str
    granted_at: str


class AssignmentRecord(TypedDict):
    record_kind: Literal["assignment"]
    schema_version: int
    writer_id: str
    updated_at: str
    assignment_id: str
    bead_id: str
    plan_id: str | None
    approved_plan_commit: str | None
    executor_task_id: str
    overseer_task_id: str
    scope_reference: str
    review_history: list[ReviewEntry]
    mandate: Mandate | None


class OwnedResource(TypedDict):
    kind: str
    identifier: str


class PushObligation(TypedDict):
    source: Literal["git", "beads"]
    command: list[str]
    detail: str


class ProgressRecord(TypedDict):
    record_kind: Literal["progress"]
    schema_version: int
    writer_id: str
    updated_at: str
    role_task_id: str
    role: str
    phase: str
    phase_started_at: str
    expected_next_actor: str | None
    expected_next_action: str
    handoff_needed: bool
    handoff_sent: bool
    delivery_error: str | None
    owned_resources: list[OwnedResource]
    push_obligations: NotRequired[list[PushObligation]]


class SuspendedInvestigation(TypedDict):
    task_id: str
    reason: str
    evidence_reference: str


class ExecutorEvidenceRecord(TypedDict):
    record_kind: Literal["executor_evidence"]
    schema_version: int
    writer_id: str
    updated_at: str
    executor_task_id: str
    worktree_path: str
    base_oid: str
    source_oid: str
    candidate_id: str
    tested_oid: str | None
    queue_revision: int
    push_state: Literal["not_required", "pending", "complete", "failed"]
    cleanup_state: Literal["not_eligible", "pending", "complete", "failed"]
    suspended_investigations: list[SuspendedInvestigation]


class InterviewRecord(TypedDict):
    record_kind: Literal["interview"]
    schema_version: int
    writer_id: str
    updated_at: str
    sage_task_id: str
    run_id: str
    subject_task_id: str
    prior_archived: bool
    reminder_state: Literal["not_due", "due", "sent", "failed"]
    completion_state: Literal["active", "complete", "canceled"]


Record = (
    InstallationRecord
    | ProjectRegistryRecord
    | RoleRunRegistryRecord
    | HoldsJobsRecord
    | AssignmentRecord
    | ProgressRecord
    | ExecutorEvidenceRecord
    | InterviewRecord
)


class RecordValidationError(ValueError):
    """A record failed a declared schema contract."""

    def __init__(self, kind: str, problems: list[str]) -> None:
        self.kind = kind
        self.problems = problems
        super().__init__(f"invalid {kind} record: " + "; ".join(problems))


def schema_root() -> Path:
    """Find checked-in schemas or their installed data-file location."""

    override = os.environ.get("FULCRUM_SCHEMA_ROOT")
    if override:
        return Path(override).expanduser().resolve()

    for parent in Path(__file__).resolve().parents:
        candidate = parent / "schemas"
        if (candidate / "records-v1.schema.json").is_file():
            return candidate

    installed = Path(sys.prefix) / "share" / "fulcrum" / "schemas"
    if (installed / "records-v1.schema.json").is_file():
        return installed
    raise FileNotFoundError("could not locate Fulcrum record schemas")


def _format_error(error: Any) -> str:
    location = "$"
    if error.absolute_path:
        location += "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}"
            for part in error.absolute_path
        )
    return f"{location}: {error.message}"


def validate_record(value: object) -> Record:
    """Validate an object and return it narrowed to a supported record type."""

    if not isinstance(value, dict):
        raise RecordValidationError("unknown", ["$: expected a JSON object"])

    kind = value.get("record_kind")
    if not isinstance(kind, str):
        raise RecordValidationError("unknown", ["$.record_kind: required string"])

    version = value.get("schema_version")
    if version != SCHEMA_VERSION:
        raise RecordValidationError(
            kind,
            [
                "$.schema_version: unsupported version "
                f"{version!r}; supported versions: [{SCHEMA_VERSION}]"
            ],
        )

    schema = json.loads((schema_root() / "records-v1.schema.json").read_text())
    definitions = schema["$defs"]
    if kind not in definitions or kind in {"timestamp", "base"}:
        supported = ", ".join(
            sorted(k for k in definitions if k not in {"timestamp", "base"})
        )
        raise RecordValidationError(
            kind, [f"$.record_kind: unknown kind {kind!r}; expected one of {supported}"]
        )

    validator = Draft202012Validator(
        {"$ref": f"#/$defs/{kind}", "$defs": definitions},
        format_checker=FormatChecker(),
    )
    errors = sorted(validator.iter_errors(value), key=lambda item: list(item.path))
    if errors:
        raise RecordValidationError(kind, [_format_error(error) for error in errors])
    return cast(Record, value)


def load_record(path: Path) -> Record:
    """Load and validate a UTF-8 JSON record."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RecordValidationError("unknown", [f"{path}: {error}"]) from error
    return validate_record(value)
