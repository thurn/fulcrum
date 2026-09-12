"""Pre-Dashboard installation and integration diagnostics."""

from __future__ import annotations

import json
import shlex
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

from fulcrum.brain import brain_status
from fulcrum.config import RuntimePaths
from fulcrum.hook_config import FULCRUM_STATUS_PREFIX
from fulcrum.install import LINKED_SKILLS, ROLES, SETUP_SKILLS, runtime_package_root
from fulcrum.records import (
    ExecutorEvidenceRecord,
    HoldsJobsRecord,
    InstallationRecord,
    ProgressRecord,
    ProjectRegistryRecord,
    RoleRunRegistryRecord,
    load_record,
    schema_root,
)
from fulcrum.state import read_record


class Check(TypedDict):
    category: Literal["required", "optional", "push"]
    name: str
    status: Literal["pass", "fail", "unavailable"]
    detail: str


def _check(
    category: Literal["required", "optional", "push"],
    name: str,
    status: Literal["pass", "fail", "unavailable"],
    detail: str,
) -> Check:
    return {"category": category, "name": name, "status": status, "detail": detail}


def _tool_version(command: list[str]) -> str:
    result = subprocess.run(
        command, check=False, capture_output=True, text=True, timeout=5
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "failed")
    return (result.stdout.strip() or result.stderr.strip()).splitlines()[0]


def _tollgate_project_status(repository_id: str) -> dict[str, Any]:
    result = subprocess.run(
        [
            "tg",
            "--no-launch",
            "--repository",
            repository_id,
            "--json",
            "status",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "failed")
    value: object = json.loads(result.stdout)
    if not isinstance(value, dict) or not isinstance(value.get("state"), dict):
        raise RuntimeError("unexpected Tollgate status response")
    return cast(dict[str, Any], value["state"])


def _hooks_check(path: Path, expected_command: Path) -> Check:
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("hooks"), dict):
            raise ValueError("missing hooks object")
        hooks = cast(dict[str, Any], value["hooks"])
        marked = []
        for event in ("SessionStart", "Stop"):
            groups = hooks.get(event, [])
            if isinstance(groups, list):
                for group in groups:
                    if isinstance(group, dict) and isinstance(group.get("hooks"), list):
                        marked.extend(
                            handler
                            for handler in group["hooks"]
                            if isinstance(handler, dict)
                            and str(handler.get("statusMessage", "")).startswith(
                                FULCRUM_STATUS_PREFIX
                            )
                        )
        if len(marked) != 2:
            raise ValueError(f"expected two Fulcrum handlers, found {len(marked)}")
        if any(handler.get("timeout") != 2 for handler in marked):
            raise ValueError("Fulcrum handlers do not use the two-second ceiling")
        if any(
            shlex.split(str(handler.get("command", ""))) != [str(expected_command)]
            for handler in marked
        ):
            raise ValueError("Fulcrum handlers do not use the repository hook link")
        return _check("required", "hooks_config", "pass", str(path))
    except Exception as error:
        return _check("required", "hooks_config", "fail", str(error))


def _role_checks(paths: RuntimePaths) -> list[Check]:
    try:
        value = read_record(paths, "role_run_registry")
        if value["record_kind"] != "role_run_registry":
            raise ValueError("wrong record kind")
        registry = cast(RoleRunRegistryRecord, value)
    except Exception as error:
        return [_check("required", "human_role_enrollment", "fail", str(error))]
    archon = [
        role
        for role in registry["roles"]
        if role["role"] == "archon"
        and role["task_id"] == registry["current_archon_task_id"]
        and role["identity_state"] == "resolved"
        and role["model_authorization"]["source"] == "human"
    ]
    watchmen = [
        role
        for role in registry["roles"]
        if role["role"] == "night_watchman"
        and role["identity_state"] == "resolved"
        and role["task_id"]
        and role["model_authorization"]["source"] == "human"
    ]
    return [
        _check(
            "required",
            "human_role_enrollment",
            "pass" if len(archon) == 1 and len(watchmen) == 1 else "fail",
            f"resolved current Archons={len(archon)}; resolved Watchmen={len(watchmen)}",
        )
    ]


def _linked_assets_check(
    installation: InstallationRecord, skills_root: Path, hooks_config: Path
) -> Check:
    source_value = installation["observations"].get("installed_source_root")
    if not source_value:
        return _check("required", "repository_links", "fail", "source root missing")
    source_root = Path(source_value).resolve(strict=False)
    failures: list[str] = []
    for name in LINKED_SKILLS:
        link = skills_root / name
        expected = (source_root / "skills" / name).resolve(strict=False)
        if not link.is_symlink() or link.resolve(strict=False) != expected:
            failures.append(str(link))
    hook_link = skills_root.parent / "hooks" / "fulcrum"
    expected_hook = (source_root / "hooks").resolve(strict=False)
    if not hook_link.is_symlink() or hook_link.resolve(strict=False) != expected_hook:
        failures.append(str(hook_link))
    cli_link = skills_root.parent / "bin" / "fulcrum"
    expected_cli = (source_root / ".venv" / "bin" / "fulcrum").resolve(strict=False)
    if not cli_link.is_symlink() or cli_link.resolve(strict=False) != expected_cli:
        failures.append(str(cli_link))
    if runtime_package_root() != source_root / "src" / "fulcrum":
        failures.append(f"Python import: {runtime_package_root()}")
    try:
        actual_schemas = schema_root()
        if actual_schemas != source_root / "schemas":
            failures.append(f"schemas: {actual_schemas}")
    except Exception as error:
        failures.append(f"schemas: {error}")
    if hooks_config.parent.resolve(strict=False) != skills_root.parent.resolve(
        strict=False
    ):
        failures.append("Codex home mismatch")
    return _check(
        "required",
        "repository_links",
        "fail" if failures else "pass",
        "invalid: " + ", ".join(failures) if failures else str(source_root),
    )


def _project_checks(paths: RuntimePaths, codex_projects_verified: bool) -> list[Check]:
    try:
        value = read_record(paths, "project_registry")
        if value["record_kind"] != "project_registry":
            raise ValueError("wrong record kind")
        registry = cast(ProjectRegistryRecord, value)
    except Exception as error:
        return [_check("required", "initial_project_integrations", "fail", str(error))]
    failures: list[str] = []
    for project in registry["projects"]:
        root = Path(project["repo_path"])
        if not root.is_dir() or not (root / ".git").exists():
            failures.append(f"{project['project_id']}: repository unavailable")
        if not project["codex_project_id"]:
            failures.append(f"{project['project_id']}: Codex project unresolved")
        if not project["tollgate_repo_id"]:
            failures.append(f"{project['project_id']}: Tollgate repository unresolved")
        else:
            try:
                status = _tollgate_project_status(
                    cast(str, project["tollgate_repo_id"])
                )
                if Path(str(status.get("path", ""))).resolve() != root.resolve():
                    failures.append(f"{project['project_id']}: Tollgate path mismatch")
                if status.get("execution_state") != "active" or status.get(
                    "block_reasons"
                ):
                    failures.append(f"{project['project_id']}: Tollgate is not healthy")
                if status.get("remote_enabled") is not True:
                    failures.append(
                        f"{project['project_id']}: Tollgate push is disabled"
                    )
            except Exception as error:
                failures.append(
                    f"{project['project_id']}: Tollgate unavailable ({error})"
                )
        if not project["enabled"]:
            if not project.get("ineligibility_reason"):
                failures.append(
                    f"{project['project_id']}: disabled without an ineligibility reason"
                )
            if not project.get("scope_decision"):
                failures.append(
                    f"{project['project_id']}: disabled without a recorded scope decision"
                )
            failures.append(
                f"{project['project_id']}: integration must be repaired before readiness"
            )
    required_projects = {"fulcrum", "tollgate", "battlement"}
    present_projects = {project["project_id"] for project in registry["projects"]}
    missing_projects = required_projects - present_projects
    if missing_projects:
        failures.append(
            "missing initial projects: " + ", ".join(sorted(missing_projects))
        )
    if not codex_projects_verified:
        failures.append("Codex project listing has not been externally verified")
    return [
        _check(
            "required",
            "initial_project_integrations",
            "fail" if failures else "pass",
            (
                "; ".join(failures)
                if failures
                else "three Git/Codex/Tollgate mappings present"
            ),
        )
    ]


def _valid_utc_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _progress_state_check(
    paths: RuntimePaths,
    *,
    task_id: str | None,
    role: str,
    name: str,
    registry_error: str | None = None,
) -> Check:
    if registry_error is not None:
        return _check("required", name, "fail", registry_error)
    if not task_id:
        return _check(
            "required",
            name,
            "fail",
            f"resolved current {role} task ID is missing",
        )
    try:
        loaded = read_record(paths, "progress", task_id)
        if loaded["record_kind"] != "progress":
            raise ValueError("wrong record kind")
        progress = cast(ProgressRecord, loaded)
        failures = []
        if progress["role_task_id"] != task_id:
            failures.append("role_task_id does not match the registered task")
        if progress["writer_id"] != task_id:
            failures.append("writer_id does not match the registered task")
        if progress["role"] != role:
            failures.append(f"role is {progress['role']!r}, expected {role!r}")
        return _check(
            "required",
            name,
            "fail" if failures else "pass",
            (
                "; ".join(failures)
                if failures
                else f"{role} progress is present and owned by {task_id}"
            ),
        )
    except Exception as error:
        return _check("required", name, "fail", str(error))


def _holds_jobs_state_check(
    paths: RuntimePaths,
    *,
    current_archon_task_id: str | None,
    registry_error: str | None,
) -> Check:
    if registry_error is not None:
        return _check("required", "holds_jobs_state", "fail", registry_error)
    failures: list[str] = []
    try:
        loaded = read_record(paths, "holds_jobs")
        if loaded["record_kind"] != "holds_jobs":
            raise ValueError("wrong record kind")
        jobs_record = cast(HoldsJobsRecord, loaded)
    except Exception as error:
        return _check("required", "holds_jobs_state", "fail", str(error))

    if not current_archon_task_id:
        failures.append("current Archon task ID is missing")
    elif jobs_record["writer_id"] != current_archon_task_id:
        failures.append(
            "writer_id does not match current Archon " f"{current_archon_task_id}"
        )

    try:
        loaded_projects = read_record(paths, "project_registry")
        if loaded_projects["record_kind"] != "project_registry":
            raise ValueError("wrong record kind")
        project_registry = cast(ProjectRegistryRecord, loaded_projects)
        enabled_project_ids = {
            project["project_id"]
            for project in project_registry["projects"]
            if project["enabled"]
        }
    except Exception as error:
        enabled_project_ids = set()
        failures.append(f"project registry unavailable: {error}")

    jobs = jobs_record["recurring_jobs"]
    counts = Counter(job["job_id"] for job in jobs)
    duplicate_ids = sorted(job_id for job_id, count in counts.items() if count > 1)
    if duplicate_ids:
        failures.append("duplicate recurring job IDs: " + ", ".join(duplicate_ids))

    for job in jobs:
        if job.get("role") not in {"sage", "inquisitor"}:
            failures.append(f"{job['job_id']}: recurring role is missing or invalid")
        if not isinstance(job.get("scope"), str) or not job["scope"]:
            failures.append(f"{job['job_id']}: recurring scope is missing")
        if not _valid_utc_timestamp(job["cadence_anchor"]):
            failures.append(f"{job['job_id']}: invalid cadence_anchor")
        if not _valid_utc_timestamp(job["next_due"]):
            failures.append(f"{job['job_id']}: invalid next_due")

    expected: dict[str, tuple[str, str]] = {"sage:fleet": ("sage", "fleet")}
    expected.update(
        {
            f"inquisitor:{project_id}": ("inquisitor", f"project:{project_id}")
            for project_id in enabled_project_ids
        }
    )
    for job_id, (role, scope) in sorted(expected.items()):
        matches = [job for job in jobs if job["job_id"] == job_id]
        if len(matches) != 1:
            failures.append(
                f"{job_id}: expected exactly one recurring job, found {len(matches)}"
            )
            continue
        job = matches[0]
        if job.get("role") != role:
            failures.append(f"{job_id}: role is {job.get('role')!r}, expected {role!r}")
        if job.get("scope") != scope:
            failures.append(
                f"{job_id}: scope is {job.get('scope')!r}, expected {scope!r}"
            )

    return _check(
        "required",
        "holds_jobs_state",
        "fail" if failures else "pass",
        (
            "; ".join(failures)
            if failures
            else "holds/jobs ledger is present, Archon-owned, and complete"
        ),
    )


def _first_watchman_patrol_check(
    installation: InstallationRecord, *, watchman_task_id: str | None
) -> Check:
    raw = installation.get("first_watchman_patrol")
    if not isinstance(raw, dict):
        return _check(
            "required",
            "watchman_first_patrol",
            "fail",
            "durable evidence of a successful first Watchman patrol is missing",
        )
    if raw.get("outcome") != "success":
        return _check(
            "required",
            "watchman_first_patrol",
            "fail",
            "first Watchman patrol evidence does not report success",
        )
    if not watchman_task_id:
        return _check(
            "required",
            "watchman_first_patrol",
            "fail",
            "cannot verify first patrol ownership without a resolved Watchman",
        )
    if raw.get("watchman_task_id") != watchman_task_id:
        return _check(
            "required",
            "watchman_first_patrol",
            "fail",
            "first patrol evidence belongs to a different Watchman task",
        )
    if not _valid_utc_timestamp(raw.get("observed_at")):
        return _check(
            "required",
            "watchman_first_patrol",
            "fail",
            "first patrol evidence has an invalid observed_at timestamp",
        )
    evidence = raw.get("evidence")
    if not isinstance(evidence, str) or not evidence.strip():
        return _check(
            "required",
            "watchman_first_patrol",
            "fail",
            "first patrol evidence is empty",
        )
    return _check(
        "required",
        "watchman_first_patrol",
        "pass",
        f"successful first patrol by {watchman_task_id} at {raw['observed_at']}",
    )


def _patrol_ready_state_checks(
    paths: RuntimePaths, installation: InstallationRecord
) -> list[Check]:
    archon_registry_error: str | None = None
    watchman_registry_error: str | None = None
    current_archon_task_id: str | None = None
    watchman_task_id: str | None = None
    try:
        loaded = read_record(paths, "role_run_registry")
        if loaded["record_kind"] != "role_run_registry":
            raise ValueError("wrong record kind")
        registry = cast(RoleRunRegistryRecord, loaded)
        current_archon_task_id = registry["current_archon_task_id"]
        current_archons = [
            role
            for role in registry["roles"]
            if role["role"] == "archon"
            and role["task_id"] == current_archon_task_id
            and role["identity_state"] == "resolved"
        ]
        if len(current_archons) != 1:
            current_archon_task_id = None
            archon_registry_error = (
                "role registry must contain exactly one resolved current Archon; "
                f"found {len(current_archons)}"
            )
        watchmen = [
            role
            for role in registry["roles"]
            if role["role"] == "night_watchman"
            and role["task_id"]
            and role["identity_state"] == "resolved"
        ]
        if len(watchmen) == 1:
            watchman_task_id = cast(str, watchmen[0]["task_id"])
        else:
            watchman_registry_error = (
                "role registry must contain exactly one resolved Watchman; "
                f"found {len(watchmen)}"
            )
    except Exception as error:
        archon_registry_error = f"role registry unavailable: {error}"
        watchman_registry_error = archon_registry_error

    return [
        _holds_jobs_state_check(
            paths,
            current_archon_task_id=current_archon_task_id,
            registry_error=archon_registry_error,
        ),
        _progress_state_check(
            paths,
            task_id=current_archon_task_id,
            role="archon",
            name="archon_progress",
            registry_error=archon_registry_error,
        ),
        _progress_state_check(
            paths,
            task_id=watchman_task_id,
            role="night_watchman",
            name="watchman_progress",
            registry_error=watchman_registry_error,
        ),
        _first_watchman_patrol_check(installation, watchman_task_id=watchman_task_id),
    ]


def _push_checks(paths: RuntimePaths) -> list[Check]:
    failures: list[str] = []
    for directory, kind in (
        ("progress", "progress"),
        ("evidence", "executor_evidence"),
    ):
        root = paths.state_root / directory
        if not root.is_dir():
            continue
        for path in root.glob("*.json"):
            try:
                record = load_record(path)
            except Exception:
                continue
            if kind == "progress" and record["record_kind"] == "progress":
                progress = cast(ProgressRecord, record)
                for obligation in progress.get("push_obligations", []):
                    failures.append(
                        f"{path.name}: {obligation['source']} {obligation['detail']}"
                    )
            if (
                kind == "executor_evidence"
                and record["record_kind"] == "executor_evidence"
            ):
                evidence = cast(ExecutorEvidenceRecord, record)
                if evidence["push_state"] == "failed":
                    failures.append(f"{path.name}: source push failed")
    return [
        _check(
            "push",
            "failed_pushes",
            "fail" if failures else "pass",
            "; ".join(failures) if failures else "no retained push failures",
        )
    ]


def doctor_runtime(
    *,
    paths: RuntimePaths,
    expected_brain_remote: str,
    hooks_config: Path,
    skills_root: Path,
) -> dict[str, Any]:
    """Report required failures, optional gaps, and push failures separately."""

    checks: list[Check] = []
    try:
        loaded = read_record(paths, "installation")
        if loaded["record_kind"] != "installation":
            raise ValueError("wrong record kind")
        installation = cast(InstallationRecord, loaded)
        checks.append(
            _check("required", "installation_record", "pass", str(paths.config_file))
        )
    except Exception as error:
        failure = _check("required", "installation_record", "fail", str(error))
        return {
            "ready": False,
            "required_failures": [failure],
            "optional_gaps": [],
            "push_failures": [],
            "checks": [failure],
        }
    observations = installation["observations"]
    missing_setup_skills = [
        skill
        for skill in SETUP_SKILLS
        if not (skills_root / skill / "SKILL.md").is_file()
    ]
    checks.append(
        _check(
            "required",
            "setup_skill",
            "fail" if missing_setup_skills else "pass",
            (
                "missing: " + ", ".join(missing_setup_skills)
                if missing_setup_skills
                else "Fulcrum setup skill installed"
            ),
        )
    )
    try:
        status = brain_status(paths.brain_root, expected_brain_remote)
        checks.append(
            _check(
                "required",
                "beads_database",
                "pass",
                f"Beads reports {status['database']} on {status['host']} reachable",
            )
        )
    except Exception as error:
        checks.append(_check("required", "beads_database", "fail", str(error)))
    for name, command in (
        ("beads_version", ["bd", "--version"]),
        ("dolt_version", ["dolt", "version"]),
        ("tollgate_version", ["tg", "--version"]),
    ):
        try:
            checks.append(_check("required", name, "pass", _tool_version(command)))
        except Exception as error:
            checks.append(_check("required", name, "fail", str(error)))
    hook_command = skills_root.parent / "hooks" / "fulcrum" / "fulcrum-hook"
    checks.append(_linked_assets_check(installation, skills_root, hooks_config))
    checks.append(_hooks_check(hooks_config, hook_command))
    trust = observations.get("hook_trust")
    checks.append(
        _check(
            "optional",
            "desktop_hook_delivery",
            "pass" if trust == "desktop_verified" else "unavailable",
            trust or "not observed",
        )
    )
    missing_skills = [
        role
        for role in ROLES
        if not (skills_root / f"fulcrum-{role}" / "SKILL.md").is_file()
    ]
    checks.append(
        _check(
            "required",
            "role_skills",
            "fail" if missing_skills else "pass",
            (
                "missing: " + ", ".join(missing_skills)
                if missing_skills
                else "all seven installed"
            ),
        )
    )
    checks.extend(_role_checks(paths))
    checks.extend(
        _project_checks(paths, bool(observations.get("codex_projects_verified_at")))
    )
    checks.extend(_patrol_ready_state_checks(paths, installation))
    checks.append(
        _check(
            "required",
            "watchman_hourly_schedule",
            "pass" if observations.get("watchman_schedule") == "ready" else "fail",
            observations.get("watchman_schedule", "not observed"),
        )
    )
    checks.append(
        _check(
            "required",
            "sage_cadence_anchor",
            "pass" if observations.get("sage_cadence_anchor") else "fail",
            observations.get("sage_cadence_anchor", "missing"),
        )
    )
    runtime = observations.get("runtime_observation")
    checks.append(
        _check(
            "optional",
            "runtime_observation",
            "pass" if runtime == "available" else "unavailable",
            runtime or "not configured",
        )
    )
    checks.extend(_push_checks(paths))
    required_failures = [
        check
        for check in checks
        if check["category"] == "required" and check["status"] != "pass"
    ]
    push_failures = [
        check
        for check in checks
        if check["category"] == "push" and check["status"] == "fail"
    ]
    return {
        "ready": not required_failures and not push_failures,
        "required_failures": required_failures,
        "optional_gaps": [
            check
            for check in checks
            if check["category"] == "optional" and check["status"] != "pass"
        ],
        "push_failures": push_failures,
        "checks": checks,
    }
