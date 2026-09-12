"""Pre-Dashboard installation and integration diagnostics."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

from fulcrum import __version__
from fulcrum.brain import brain_status
from fulcrum.config import RuntimePaths
from fulcrum.hook_config import FULCRUM_STATUS_PREFIX
from fulcrum.install import ROLES, SETUP_SKILLS
from fulcrum.records import (
    ExecutorEvidenceRecord,
    InstallationRecord,
    ProgressRecord,
    ProjectRegistryRecord,
    RoleRunRegistryRecord,
    load_record,
)
from fulcrum.state import read_record
from fulcrum.version import source_revision


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


def _hooks_check(path: Path) -> Check:
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
        return _check("required", "hooks_config", "pass", str(path))
    except Exception as error:
        return _check("required", "hooks_config", "fail", str(error))


def _role_checks(paths: RuntimePaths, installed_skill_revision: str) -> list[Check]:
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
    result = [
        _check(
            "required",
            "human_role_enrollment",
            "pass" if len(archon) == 1 and len(watchmen) == 1 else "fail",
            f"resolved current Archons={len(archon)}; resolved Watchmen={len(watchmen)}",
        )
    ]
    stale: list[str] = []
    for role in registry["roles"]:
        task_id = role.get("task_id")
        if not task_id:
            continue
        try:
            progress = read_record(paths, "progress", task_id)
        except Exception:
            continue
        if progress["record_kind"] == "progress" and cast(ProgressRecord, progress)[
            "phase"
        ] not in {"completed", "canceled"}:
            if role["skill_revision"] != installed_skill_revision:
                stale.append(task_id)
    result.append(
        _check(
            "required",
            "active_skill_reconciliation",
            "fail" if stale else "pass",
            (
                "active runs on older skill revisions: " + ", ".join(stale)
                if stale
                else "active runs agree with the installed skill revision"
            ),
        )
    )
    return result


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
    revision = source_revision()
    configured_revision = observations.get("source_revision")
    versions_match = observations.get("package_version") == __version__ and (
        revision is None or revision == configured_revision
    )
    checks.append(
        _check(
            "required",
            "installed_version",
            "pass" if versions_match else "fail",
            f"package={__version__}; runtime_source={revision}; configured_source={configured_revision}",
        )
    )
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
    checks.append(_hooks_check(hooks_config))
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
    checks.extend(_role_checks(paths, observations.get("skill_revision", "")))
    checks.extend(
        _project_checks(paths, bool(observations.get("codex_projects_verified_at")))
    )
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
