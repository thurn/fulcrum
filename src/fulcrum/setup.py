"""Idempotent constructors for the human-authorized fleet bootstrap."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, cast

from fulcrum.config import RuntimePaths
from fulcrum.coordination import enroll_project
from fulcrum.records import (
    InstallationRecord,
    HoldsJobsRecord,
    Project,
    ProjectRegistryRecord,
    RoleRun,
    RoleRunRegistryRecord,
    validate_record,
)
from fulcrum.roles import initialize_progress
from fulcrum.state import atomic_write_record, read_record, selected_record_path
from fulcrum.watchman import ensure_recurring_jobs


class SetupError(RuntimeError):
    """Bootstrap observations cannot safely initialize the fleet."""


def _timestamp(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise SetupError(f"{label} is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise SetupError(f"{label} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise SetupError(f"{label} must include a timezone")
    return value


def _timestamp_value(value: object, label: str) -> datetime:
    timestamp = _timestamp(value, label)
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SetupError(f"{label} is required")
    return value


def _human_role(raw: object, role: str) -> RoleRun:
    if not isinstance(raw, dict) or raw.get("human_created") is not True:
        raise SetupError(f"{role} must be explicitly marked human-created")
    task_id = _text(raw.get("task_id"), f"{role}.task_id")
    reference = _text(
        raw.get("authorization_reference"), f"{role}.authorization_reference"
    )
    return cast(
        RoleRun,
        {
            "role": role,
            "project_id": _text(raw.get("project_id"), f"{role}.project_id"),
            "host_id": _text(raw.get("host_id"), f"{role}.host_id"),
            "task_id": task_id,
            "identity_state": "resolved",
            "role_number": None,
            "run_id": f"persistent-{role}",
            "pair_id": None,
            "title": _text(raw.get("title"), f"{role}.title"),
            "selected_model": _text(
                raw.get("selected_model"), f"{role}.selected_model"
            ),
            "selected_reasoning": _text(
                raw.get("selected_reasoning"), f"{role}.selected_reasoning"
            ),
            "model_authorization": {"source": "human", "reference": reference},
        },
    )


def _merge_roles(
    existing: RoleRunRegistryRecord | None,
    archon: RoleRun,
    watchman: RoleRun,
    now: str,
) -> RoleRunRegistryRecord:
    archon_id = cast(str, archon["task_id"])
    if existing is not None and existing["current_archon_task_id"] != archon_id:
        raise SetupError("another current Archon exists; use cooperative handover")
    roles = list(existing["roles"]) if existing is not None else []
    for desired in (archon, watchman):
        conflicts = [
            role
            for role in roles
            if role["role"] == desired["role"] and role["task_id"] != desired["task_id"]
        ]
        if conflicts and desired["role"] != "archon":
            raise SetupError(f"conflicting {desired['role']} registration exists")
        roles = [role for role in roles if role["task_id"] != desired["task_id"]]
        roles.append(desired)
    return cast(
        RoleRunRegistryRecord,
        validate_record(
            {
                "record_kind": "role_run_registry",
                "schema_version": 1,
                "writer_id": archon_id,
                "updated_at": now,
                "current_archon_task_id": archon_id,
                "roles": roles,
            }
        ),
    )


def _project(raw: object) -> tuple[Project, list[str]]:
    if not isinstance(raw, dict) or not isinstance(raw.get("observations"), dict):
        raise SetupError("each project requires current observations")
    project: Project = {
        "project_id": _text(raw.get("project_id"), "project_id"),
        "repo_path": _text(raw.get("repo_path"), "repo_path"),
        "host_id": _text(raw.get("host_id"), "host_id"),
        "codex_project_id": _text(raw.get("codex_project_id"), "codex_project_id"),
        "tollgate_repo_id": _text(raw.get("tollgate_repo_id"), "tollgate_repo_id"),
        "enabled": False,
    }
    observations = cast(dict[str, Any], raw["observations"])
    enrolled, reasons = enroll_project(
        project,
        git_root=observations.get("git_root"),
        codex_id=observations.get("codex_id"),
        codex_path=observations.get("codex_path"),
        codex_host=observations.get("codex_host"),
        codex_is_git=observations.get("codex_is_git"),
        tollgate_id=observations.get("tollgate_id"),
        tollgate_path=observations.get("tollgate_path"),
        tollgate_healthy=observations.get("tollgate_healthy"),
    )
    if reasons:
        enrolled["ineligibility_reason"] = "; ".join(reasons)
    return enrolled, reasons


def _merge_projects(
    existing: ProjectRegistryRecord | None, projects: list[Project]
) -> list[Project]:
    incoming = {project["project_id"] for project in projects}
    preserved = (
        [
            project
            for project in existing["projects"]
            if project["project_id"] not in incoming
        ]
        if existing is not None
        else []
    )
    return [*preserved, *projects]


def bootstrap_fleet(paths: RuntimePaths, value: object) -> dict[str, Any]:
    """Create or reconcile all persistent state required for fleet patrols."""

    if not isinstance(value, dict):
        raise SetupError("bootstrap input must be an object")
    now = _timestamp(value.get("observed_at"), "observed_at")
    installation = read_record(paths, "installation")
    if installation["record_kind"] != "installation":
        raise SetupError("installation record is unavailable")
    installation = cast(InstallationRecord, installation)
    configured_anchor = value.get("sage_cadence_anchor")
    if configured_anchor is None:
        configured_anchor = installation["observations"].get("sage_cadence_anchor")
    sage_anchor = _timestamp_value(configured_anchor, "sage_cadence_anchor")
    archon = _human_role(value.get("archon"), "archon")
    watchman = _human_role(value.get("watchman"), "night_watchman")
    if archon["task_id"] == watchman["task_id"]:
        raise SetupError("Archon and Watchman must be distinct human-created tasks")

    raw_projects = value.get("projects")
    if not isinstance(raw_projects, list) or len(raw_projects) != 3:
        raise SetupError("bootstrap requires exactly three initial projects")
    projects: list[Project] = []
    problems: dict[str, list[str]] = {}
    for raw in raw_projects:
        project, reasons = _project(raw)
        if project["project_id"] in {item["project_id"] for item in projects}:
            raise SetupError(f"duplicate project {project['project_id']}")
        projects.append(project)
        if reasons:
            problems[project["project_id"]] = reasons
    required_projects = {"fulcrum", "tollgate", "battlement"}
    if {project["project_id"] for project in projects} != required_projects:
        raise SetupError("initial projects must be Fulcrum, Tollgate, and Battlement")

    existing: RoleRunRegistryRecord | None = None
    role_path = selected_record_path(paths, "role_run_registry", None)
    if role_path.is_file():
        loaded = read_record(paths, "role_run_registry")
        if loaded["record_kind"] != "role_run_registry":
            raise SetupError("invalid role registry")
        existing = cast(RoleRunRegistryRecord, loaded)
    registry = _merge_roles(existing, archon, watchman, now)
    existing_projects: ProjectRegistryRecord | None = None
    project_path = selected_record_path(paths, "project_registry", None)
    if project_path.is_file():
        loaded_projects = read_record(paths, "project_registry")
        if loaded_projects["record_kind"] != "project_registry":
            raise SetupError("invalid project registry")
        existing_projects = cast(ProjectRegistryRecord, loaded_projects)
    project_registry = cast(
        ProjectRegistryRecord,
        validate_record(
            {
                "record_kind": "project_registry",
                "schema_version": 1,
                "writer_id": archon["task_id"],
                "updated_at": now,
                "projects": _merge_projects(existing_projects, projects),
            }
        ),
    )

    holds_path = selected_record_path(paths, "holds_jobs", None)
    if holds_path.is_file():
        loaded_holds = read_record(paths, "holds_jobs")
        if loaded_holds["record_kind"] != "holds_jobs":
            raise SetupError("invalid holds/jobs registry")
        holds_jobs = cast(HoldsJobsRecord, loaded_holds)
    else:
        holds_jobs = cast(
            HoldsJobsRecord,
            validate_record(
                {
                    "record_kind": "holds_jobs",
                    "schema_version": 1,
                    "writer_id": cast(str, archon["task_id"]),
                    "updated_at": now,
                    "holds": [],
                    "recurring_jobs": [],
                }
            ),
        )
    holds_jobs["writer_id"] = cast(str, archon["task_id"])
    holds_jobs = ensure_recurring_jobs(
        holds_jobs,
        sage_anchor=sage_anchor,
        enabled_project_ids=[
            project["project_id"]
            for project in project_registry["projects"]
            if project["enabled"]
        ],
        now=now,
    )

    progress_records = []
    for role, action in (
        (archon, "Complete fleet bootstrap and readiness gate"),
        (watchman, "Run the hourly patrol and report meaningful changes"),
    ):
        progress_path = selected_record_path(
            paths, "progress", cast(str, role["task_id"])
        )
        if progress_path.is_file():
            progress_records.append(read_record(paths, "progress", role["task_id"]))
            continue
        progress = initialize_progress(role, now)
        progress["phase"] = "implementing"
        progress["expected_next_action"] = action
        progress_records.append(progress)

    records = [registry, project_registry, holds_jobs, *progress_records]
    try:
        for record in records:
            atomic_write_record(paths, record)
    except Exception as error:
        raise SetupError(f"fleet bootstrap state write failed: {error}") from error
    return {
        "ok": not problems,
        "current_archon_task_id": archon["task_id"],
        "watchman_task_id": watchman["task_id"],
        "projects": projects,
        "project_problems": problems,
    }


def record_setup_evidence(
    paths: RuntimePaths,
    *,
    archon_task_id: str,
    watchman_schedule_id: str,
    codex_projects_verified_at: str,
    hooks_verified_at: str | None = None,
    hooks_evidence: str | None = None,
    first_patrol_observed_at: str | None = None,
    first_patrol_evidence: str | None = None,
) -> dict[str, Any]:
    """Record setup observations and explicit first-patrol evidence."""

    projects_at = _timestamp(codex_projects_verified_at, "codex_projects_verified_at")
    schedule_id = _text(watchman_schedule_id, "watchman_schedule_id")
    if (hooks_verified_at is None) != (hooks_evidence is None):
        raise SetupError(
            "hook verification timestamp and evidence must be supplied together"
        )
    if (first_patrol_observed_at is None) != (first_patrol_evidence is None):
        raise SetupError(
            "first patrol timestamp and evidence must be supplied together"
        )
    registry = read_record(paths, "role_run_registry")
    if registry["record_kind"] != "role_run_registry":
        raise SetupError("role registry is unavailable")
    roles = cast(RoleRunRegistryRecord, registry)
    if roles["current_archon_task_id"] != archon_task_id:
        raise SetupError("only the current Archon may finish bootstrap")
    watchmen = [
        role
        for role in roles["roles"]
        if role["role"] == "night_watchman"
        and role["identity_state"] == "resolved"
        and role["task_id"]
        and role["role_number"] is None
        and role["model_authorization"]["source"] == "human"
    ]
    if len(watchmen) != 1:
        raise SetupError("exactly one resolved human-created Watchman is required")
    loaded = read_record(paths, "installation")
    if loaded["record_kind"] != "installation":
        raise SetupError("installation record is unavailable")
    installation = deepcopy(cast(InstallationRecord, loaded))
    installation["writer_id"] = "setup"
    installation["updated_at"] = projects_at
    installation["observations"].update(
        {
            "codex_projects_verified_at": projects_at,
            "watchman_schedule": "ready",
            "watchman_schedule_id": schedule_id,
        }
    )
    if hooks_verified_at is not None and hooks_evidence is not None:
        hook_at = _timestamp(hooks_verified_at, "hooks_verified_at")
        evidence = _text(hooks_evidence, "hooks_evidence")
        installation["updated_at"] = hook_at
        installation["observations"].update(
            {
                "hook_trust": "desktop_verified",
                "hook_verified_at": hook_at,
                "hook_evidence": evidence,
            }
        )
    if first_patrol_observed_at is not None and first_patrol_evidence is not None:
        patrol_at = _timestamp(first_patrol_observed_at, "first_patrol_observed_at")
        patrol_evidence = _text(first_patrol_evidence, "first_patrol_evidence")
        installation["first_watchman_patrol"] = {
            "watchman_task_id": cast(str, watchmen[0]["task_id"]),
            "observed_at": patrol_at,
            "outcome": "success",
            "evidence": patrol_evidence,
        }
        installation["updated_at"] = patrol_at
    target = atomic_write_record(paths, installation)
    return {
        "ok": True,
        "path": str(target),
        "observations": installation["observations"],
        "first_watchman_patrol": installation.get("first_watchman_patrol"),
    }
