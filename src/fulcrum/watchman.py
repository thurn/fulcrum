"""Read-only patrol, anomaly deduplication, and recurring-work calculations."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Literal, TypedDict, cast

from fulcrum.records import (
    ExecutorEvidenceRecord,
    InterviewRecord,
    HoldsJobsRecord,
    PatrolCondition,
    ProgressRecord,
    ProjectRegistryRecord,
    RecurringJob,
    RoleRun,
    RoleRunRegistryRecord,
    validate_record,
)
from fulcrum.resources import utc_text
from fulcrum.interviews import interview_action

DAY = timedelta(hours=24)
INQUISITOR_OFFSET = timedelta(hours=12)
WATCHMAN_AUTOMATION_NAME = "Fulcrum Night Watchman patrol"
WATCHMAN_AUTOMATION_SEMANTIC_MARKER = "Perform the Fulcrum Night Watchman patrol."
WATCHMAN_AUTOMATION_PROMPT = (
    f"{WATCHMAN_AUTOMATION_SEMANTIC_MARKER} Read current registries and "
    "progress, compare them with supported Codex and Tollgate observations, "
    "and report only new, changed, or resolved anomalies and newly due recurring "
    "work to the registered Archon. Do not dispatch work or write Archon-owned "
    "records. Stay quiet when nothing meaningful changed."
)


class TaskObservation(TypedDict):
    available: bool
    state: Literal["running", "idle", "needs_attention", "archived"] | None
    observed_at: str
    detail: str | None


class CandidateObservation(TypedDict):
    candidate_id: str
    owner_task_id: str
    state: str
    error: str | None


class PatrolNotification(TypedDict):
    change: Literal["opened", "changed", "resolved"]
    condition: PatrolCondition


class PatrolResult(TypedDict):
    observed_at: str
    archon_task_id: str | None
    notify_archon: bool
    notify_human: bool
    notifications: list[PatrolNotification]
    current_conditions: list[PatrolCondition]
    due_job_ids: list[str]


class AutomationObservation(TypedDict):
    automation_id: str
    name: str
    target_task_id: str
    cadence: str
    prompt: str
    active: bool


class AutomationPlan(TypedDict):
    action: Literal["create", "update", "none"]
    automation_id: str | None
    name: str
    target_task_id: str
    cadence: Literal["hourly"]
    prompt: str
    active: bool


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include UTC timezone")
    return parsed.astimezone(timezone.utc)


def _job(
    job_id: str, role: Literal["sage", "inquisitor"], scope: str, due: datetime
) -> RecurringJob:
    timestamp = utc_text(due)
    return {
        "job_id": job_id,
        "role": role,
        "scope": scope,
        "cadence_anchor": timestamp,
        "next_due": timestamp,
        "active_run_id": None,
    }


def ensure_recurring_jobs(
    record: HoldsJobsRecord,
    *,
    sage_anchor: datetime,
    enabled_project_ids: list[str],
    now: str,
) -> HoldsJobsRecord:
    """Idempotently ensure one Sage and one offset Inquisitor job per project."""

    result = deepcopy(record)
    desired = [_job("sage:fleet", "sage", "fleet", sage_anchor)]
    desired.extend(
        _job(
            f"inquisitor:{project_id}",
            "inquisitor",
            f"project:{project_id}",
            sage_anchor + INQUISITOR_OFFSET,
        )
        for project_id in sorted(set(enabled_project_ids))
    )
    desired_by_id = {job["job_id"]: job for job in desired}
    existing = {job["job_id"]: job for job in result["recurring_jobs"]}
    for job_id, wanted in desired_by_id.items():
        current = existing.get(job_id)
        if current is None:
            result["recurring_jobs"].append(wanted)
        else:
            current.setdefault("role", wanted["role"])
            current.setdefault("scope", wanted["scope"])
    result["updated_at"] = now
    return cast(HoldsJobsRecord, validate_record(result))


def due_jobs(
    record: HoldsJobsRecord,
    *,
    enabled_project_ids: list[str],
    now: datetime,
) -> list[RecurringJob]:
    """Return due non-overlapping work; disabled project reviews stay dormant."""

    enabled_scopes = {f"project:{project}" for project in enabled_project_ids}
    due: list[RecurringJob] = []
    for job in record["recurring_jobs"]:
        role = job.get("role")
        scope = job.get("scope")
        if role == "inquisitor" and scope not in enabled_scopes:
            continue
        if role not in {"sage", "inquisitor"}:
            continue
        if job["active_run_id"] is not None:
            continue
        if _parse_time(job["next_due"]) <= now.astimezone(timezone.utc):
            due.append(job)
    return sorted(due, key=lambda item: (item["next_due"], item["job_id"]))


def start_recurring_job(
    record: HoldsJobsRecord,
    *,
    job_id: str,
    task_id: str,
    now: datetime,
) -> HoldsJobsRecord:
    """Record one catch-up occurrence and advance directly to a future due time."""

    if not task_id:
        raise ValueError("recurring work requires an actual task ID")
    result = deepcopy(record)
    selected = [job for job in result["recurring_jobs"] if job["job_id"] == job_id]
    if len(selected) != 1:
        raise ValueError(f"expected one recurring job {job_id!r}")
    job = selected[0]
    if job["active_run_id"] is not None:
        raise ValueError("recurring occurrence already has an active task")
    due = _parse_time(job["next_due"])
    current = now.astimezone(timezone.utc)
    while due <= current:
        due += DAY
    job["active_run_id"] = task_id
    job["next_due"] = utc_text(due)
    result["updated_at"] = utc_text(current)
    return cast(HoldsJobsRecord, validate_record(result))


def complete_recurring_job(
    record: HoldsJobsRecord, *, job_id: str, task_id: str, now: str
) -> HoldsJobsRecord:
    """Clear only the matching active occurrence; the next future due stays fixed."""

    result = deepcopy(record)
    selected = [job for job in result["recurring_jobs"] if job["job_id"] == job_id]
    if len(selected) != 1 or selected[0]["active_run_id"] != task_id:
        raise ValueError("recurring completion must match the active task")
    selected[0]["active_run_id"] = None
    result["updated_at"] = now
    return cast(HoldsJobsRecord, validate_record(result))


def _condition(
    identity: str, code: str, message: str, *references: str
) -> PatrolCondition:
    payload = json.dumps(
        [identity, code, message, sorted(references)], separators=(",", ":")
    )
    return {
        "identity": identity,
        "fingerprint": hashlib.sha256(payload.encode()).hexdigest(),
        "code": code,
        "message": message,
        "references": list(references),
    }


def _wait_is_expected(progress: ProgressRecord, now: datetime) -> bool:
    actor = progress["expected_next_actor"]
    if not progress["handoff_sent"] or actor in {None, progress["role_task_id"]}:
        return False
    deadline = progress.get("expected_by")
    return deadline is None or _parse_time(deadline) > now.astimezone(timezone.utc)


def patrol(
    *,
    role_registry: RoleRunRegistryRecord,
    project_registry: ProjectRegistryRecord,
    progress_records: list[ProgressRecord],
    task_observations: dict[str, TaskObservation],
    candidate_observations: list[CandidateObservation],
    executor_evidence: list[ExecutorEvidenceRecord],
    jobs_record: HoldsJobsRecord,
    previous_conditions: list[PatrolCondition],
    tollgate_observation_available: bool,
    now: datetime,
    interviews: list[InterviewRecord] | None = None,
) -> PatrolResult:
    """Compare supported evidence read-only and emit only meaningful changes."""

    conditions: dict[str, PatrolCondition] = {}
    for role in role_registry["roles"]:
        if role["identity_state"] != "resolved":
            identity = f"role-provisioning:{role['run_id']}:{role['role']}"
            conditions[identity] = _condition(
                identity,
                "role_provisioning_unresolved",
                f"{role['role']} identity is {role['identity_state']}",
                role["run_id"],
            )

    registered_tasks = {
        role["task_id"] for role in role_registry["roles"] if role["task_id"]
    }
    for progress in progress_records:
        task_id = progress["role_task_id"]
        if task_id not in registered_tasks:
            identity = f"unregistered-progress:{task_id}"
            conditions[identity] = _condition(
                identity,
                "unregistered_progress",
                "progress exists for a task absent from the role registry",
                task_id,
            )
        observation = task_observations.get(task_id)
        terminal = progress["phase"] in {"completed", "canceled"}
        if observation is None or not observation["available"]:
            identity = f"task-observation:{task_id}"
            conditions[identity] = _condition(
                identity,
                "task_observation_unavailable",
                "task state is unavailable; liveness is uncertain",
                task_id,
            )
        elif observation["state"] == "archived" and not terminal:
            identity = f"unexpected-archive:{task_id}"
            conditions[identity] = _condition(
                identity,
                "unexpected_archive",
                "nonterminal registered task is archived",
                task_id,
            )
        elif observation["state"] == "idle" and not terminal:
            if not _wait_is_expected(progress, now):
                deadline = progress.get("expected_by")
                overdue = deadline is not None and _parse_time(
                    deadline
                ) <= now.astimezone(timezone.utc)
                identity = f"task-stop:{task_id}"
                conditions[identity] = _condition(
                    identity,
                    "expected_wait_overdue" if overdue else "unexplained_stop",
                    (
                        "recorded handoff deadline elapsed"
                        if overdue
                        else "nonterminal task is idle without a healthy external wait"
                    ),
                    task_id,
                )
        delivery_error = progress["delivery_error"]
        if delivery_error is not None:
            identity = f"handoff-failure:{task_id}"
            conditions[identity] = _condition(
                identity,
                "handoff_failed",
                delivery_error,
                task_id,
            )
        for obligation in progress.get("push_obligations", []):
            identity = f"push:{task_id}:{obligation['source']}:{obligation['detail']}"
            conditions[identity] = _condition(
                identity,
                "push_pending",
                obligation["detail"],
                task_id,
                obligation["source"],
            )
        if (
            progress["role"] == "executor"
            and progress["phase"] == "promoting"
            and not tollgate_observation_available
        ):
            identity = f"tollgate-observation:{task_id}"
            conditions[identity] = _condition(
                identity,
                "tollgate_observation_unavailable",
                "candidate state cannot be verified",
                task_id,
            )

    for candidate in candidate_observations:
        if candidate["state"] in {"failed", "blocked", "push-failed"}:
            identity = f"candidate:{candidate['candidate_id']}"
            conditions[identity] = _condition(
                identity,
                "candidate_failed",
                candidate["error"] or f"candidate is {candidate['state']}",
                candidate["candidate_id"],
                candidate["owner_task_id"],
            )
    for evidence in executor_evidence:
        if evidence["push_state"] == "failed":
            identity = f"source-push:{evidence['candidate_id']}"
            conditions[identity] = _condition(
                identity,
                "source_push_failed",
                "promoted candidate still has a failed source push",
                evidence["candidate_id"],
                evidence["executor_task_id"],
            )

    enabled_projects = [
        project["project_id"]
        for project in project_registry["projects"]
        if project["enabled"]
    ]
    due = due_jobs(jobs_record, enabled_project_ids=enabled_projects, now=now)
    for job in due:
        identity = f"recurring-due:{job['job_id']}"
        conditions[identity] = _condition(
            identity,
            "recurring_work_due",
            f"{job.get('role', 'recurring work')} is due for {job.get('scope', job['job_id'])}",
            job["job_id"],
            job["next_due"],
        )

    for interview in interviews or []:
        action = interview_action(interview, now)
        if action not in {"none", "wait"}:
            identity = f"interview:{interview.get('interview_id', interview['run_id'])}"
            conditions[identity] = _condition(
                identity,
                "interview_obligation",
                f"Interview requires {action}; resume owning Sage or coordinate recovery",
                interview["sage_task_id"],
                interview["subject_task_id"],
            )

    previous = {condition["identity"]: condition for condition in previous_conditions}
    notifications: list[PatrolNotification] = []
    for identity, condition in sorted(conditions.items()):
        old = previous.get(identity)
        if old is None:
            notifications.append({"change": "opened", "condition": condition})
        elif old["fingerprint"] != condition["fingerprint"]:
            notifications.append({"change": "changed", "condition": condition})
    for identity, old in sorted(previous.items()):
        if identity not in conditions:
            resolved = _condition(
                identity,
                old["code"],
                f"Resolved: {old['message']}",
                *old["references"],
            )
            notifications.append({"change": "resolved", "condition": resolved})

    current = [conditions[identity] for identity in sorted(conditions)]
    return {
        "observed_at": utc_text(now),
        "archon_task_id": role_registry["current_archon_task_id"],
        "notify_archon": bool(notifications),
        "notify_human": False,
        "notifications": notifications,
        "current_conditions": current,
        "due_job_ids": [job["job_id"] for job in due],
    }


def watchman_automation_plan(
    watchman: RoleRun, existing: list[AutomationObservation]
) -> AutomationPlan:
    """Reconcile one hourly heartbeat attached to the human Watchman task.

    The target task and the stable patrol marker identify a renamed schedule;
    the canonical name is also an identity claim and cannot conflict with that
    semantic identity. Configuration differences on the one identified
    schedule are repaired in place, while ambiguity remains for a human.
    """

    task_id = watchman["task_id"]
    if (
        watchman["role"] != "night_watchman"
        or watchman["identity_state"] != "resolved"
        or not task_id
        or watchman["role_number"] is not None
        or watchman["model_authorization"]["source"] != "human"
    ):
        raise ValueError("schedule requires the resolved human-created Watchman")
    named = [item for item in existing if item["name"] == WATCHMAN_AUTOMATION_NAME]
    semantic = [
        item
        for item in existing
        if item["target_task_id"] == task_id
        and WATCHMAN_AUTOMATION_SEMANTIC_MARKER in item["prompt"]
    ]
    if len(named) > 1 or len(semantic) > 1:
        raise ValueError("multiple Watchman schedules require explicit reconciliation")
    if named and named[0]["target_task_id"] != task_id:
        raise ValueError(
            "Watchman schedule name conflicts with its registered target; "
            "explicit reconciliation is required"
        )
    if named and semantic and named[0]["automation_id"] != semantic[0]["automation_id"]:
        raise ValueError(
            "Watchman schedule name and target identity refer to different "
            "automations; explicit reconciliation is required"
        )
    matching = named or semantic
    desired: AutomationPlan = {
        "action": "create",
        "automation_id": None,
        "name": WATCHMAN_AUTOMATION_NAME,
        "target_task_id": task_id,
        "cadence": "hourly",
        "prompt": WATCHMAN_AUTOMATION_PROMPT,
        "active": True,
    }
    if not matching:
        return desired
    current = matching[0]
    desired["automation_id"] = current["automation_id"]
    if all(
        (
            current["name"] == desired["name"],
            current["target_task_id"] == desired["target_task_id"],
            current["cadence"] == desired["cadence"],
            current["prompt"] == desired["prompt"],
            current["active"] is True,
        )
    ):
        desired["action"] = "none"
    else:
        desired["action"] = "update"
    return desired
