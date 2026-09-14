"""Evidence-backed diagnostics for the assembled local product."""

from __future__ import annotations

import asyncio
import json
import os
import plistlib
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

from fulcrum.config import RuntimePaths, load_installation
from fulcrum.install import (
    APP_SERVER_LABEL,
    CONTROLLER_LABEL,
    HUMAN_SKILLS,
    REMOVED_SKILLS,
    control_plane_source,
    inspect_service,
    package_root,
    service_definitions,
)
from fulcrum.prompts import TEMPLATES, load_template
from fulcrum.readiness import progress_readiness, state_readiness
from fulcrum.runtime import CodexRuntime
from fulcrum.store import Store
from fulcrum.tollgate import Tollgate


async def _runtime_inventory(endpoint: str, archon_id: str | None) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any] | None,
    bool,
    str | None,
]:
    runtime = CodexRuntime(endpoint)
    try:
        await runtime.connect()
        models = await runtime.list_models()
        projects = await runtime.list_projects()
        if archon_id is None:
            return models, projects, None, False, None
        try:
            thread = await runtime.read_thread(archon_id)
            listed = await runtime.thread_is_listed(archon_id)
            return models, projects, thread, listed, None
        except Exception as error:
            return models, projects, None, False, str(error)
    finally:
        await runtime.close()


def _contains_identity(value: Any, identifier: str) -> bool:
    if isinstance(value, dict):
        return value.get("id") == identifier or any(
            _contains_identity(child, identifier) for child in value.values()
        )
    if isinstance(value, list):
        return any(_contains_identity(child, identifier) for child in value)
    return False


def _json_command(command: list[str]) -> tuple[Any, str | None]:
    completed = subprocess.run(
        command, capture_output=True, text=True, check=False, timeout=20
    )
    if completed.returncode != 0:
        return None, completed.stderr.strip() or completed.stdout.strip()
    try:
        return json.loads(completed.stdout), None
    except json.JSONDecodeError:
        return None, "command returned invalid JSON"


def human_skill_link_checks(
    source_root: Path, *, codex_root: Path | None = None
) -> list[dict[str, Any]]:
    """Describe whether each human skill is a checkout-backed symlink."""

    root = codex_root or Path.home() / ".codex" / "skills"
    source = source_root.resolve(strict=False)
    return [
        {
            "name": f"skill:{name}",
            "ok": (root / name).is_symlink()
            and (root / name).resolve(strict=False).is_relative_to(source),
            "detail": str(root / name),
        }
        for name in HUMAN_SKILLS
    ]


def doctor(paths: RuntimePaths) -> dict[str, Any]:
    config = load_installation(paths.config_file)
    checks: list[dict[str, Any]] = []
    configured_archon: dict[str, Any] | None = None
    if paths.database.is_file():
        try:
            with Store(paths.database, readonly=True) as store:
                configured_archon = store.row(
                    "SELECT * FROM tasks WHERE role = 'archon' AND state NOT IN ('retired','archived')"
                )
        except Exception:
            configured_archon = None

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    check(
        "editable_import",
        package_root() == Path(config.source_root) / "src" / "fulcrum",
        str(package_root()),
    )
    source_package = Path(config.source_root) / "src" / "fulcrum"
    deployed_package = control_plane_source(paths)
    source_files = {
        path.relative_to(source_package): path.read_bytes()
        for path in source_package.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    deployed_files = (
        {
            path.relative_to(deployed_package): path.read_bytes()
            for path in deployed_package.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        }
        if deployed_package.is_dir()
        else {}
    )
    check(
        "control_plane_snapshot",
        bool(source_files) and deployed_files == source_files,
        str(deployed_package),
    )
    for name, command in (
        ("codex", config.codex_bin),
        ("git", shutil.which("git")),
        ("beads", shutil.which("bd")),
        (
            "tollgate",
            shutil.which("tg") or "/Applications/Tollgate.app/Contents/MacOS/tg",
        ),
    ):
        check(
            name,
            bool(command and Path(command).exists()),
            str(command or "unavailable"),
        )
    for name in TEMPLATES:
        try:
            load_template(name)
            check(f"prompt:{name}", True, "loaded from editable source")
        except Exception as error:
            check(f"prompt:{name}", False, str(error))
    for role in ("sage", "inquisitor"):
        try:
            load_template("specialist", role=role)
            check(f"prompt:{role}", True, "loaded from editable source")
        except Exception as error:
            check(f"prompt:{role}", False, str(error))
    codex_root = Path.home() / ".codex" / "skills"
    for skill_check in human_skill_link_checks(Path(config.source_root)):
        check(skill_check["name"], skill_check["ok"], skill_check["detail"])
    for name in REMOVED_SKILLS:
        target = codex_root / name
        check(
            f"removed_skill:{name}",
            not target.exists() and not target.is_symlink(),
            str(target),
        )
    hook_target = Path.home() / ".codex" / "hooks" / "fulcrum-hook"
    cli_target = Path.home() / ".codex" / "bin" / "fulcrum"
    check(
        "hook_link",
        hook_target.is_symlink()
        and hook_target.resolve(strict=False).is_relative_to(Path(config.source_root)),
        str(hook_target),
    )
    check(
        "cli_link",
        cli_target.is_symlink()
        and cli_target.resolve(strict=False)
        == Path(config.source_root) / ".venv" / "bin" / "fulcrum",
        str(cli_target),
    )
    agents = Path.home() / "Library" / "LaunchAgents"
    expected_services = service_definitions(config, paths)
    observed_services = {
        label: inspect_service(label) for label in (APP_SERVER_LABEL, CONTROLLER_LABEL)
    }
    for label in (APP_SERVER_LABEL, CONTROLLER_LABEL):
        service_file = agents / f"{label}.plist"
        try:
            with service_file.open("rb") as handle:
                installed_service = plistlib.load(handle)
            check(
                f"service:{label}",
                installed_service == expected_services[label],
                (
                    str(service_file)
                    if installed_service == expected_services[label]
                    else f"definition differs; rerun ./scripts/setup: {service_file}"
                ),
            )
            installed_path = installed_service.get("EnvironmentVariables", {}).get(
                "PATH"
            )
            expected_path = expected_services[label]["EnvironmentVariables"]["PATH"]
            check(
                f"service_path:{label}",
                installed_path == expected_path,
                str(installed_path or "missing; rerun ./scripts/setup"),
            )
        except (OSError, plistlib.InvalidFileException, AttributeError) as error:
            check(f"service:{label}", False, str(error))
            check(f"service_path:{label}", False, str(error))
        observed = observed_services[label]
        expected = expected_services[label]
        expected_arguments = tuple(str(item) for item in expected["ProgramArguments"])
        expected_directory = expected.get("WorkingDirectory")
        runtime_definition_matches = (
            observed.program_arguments == expected_arguments
            and observed.working_directory == expected_directory
            and observed.executable_path == expected["EnvironmentVariables"].get("PATH")
        )
        check(
            f"service_runtime:{label}",
            observed.running and runtime_definition_matches,
            (
                f"pid={observed.pid}; definition matches"
                if observed.running and runtime_definition_matches
                else f"loaded={observed.loaded}; state={observed.state}; pid={observed.pid}; definition differs from running job"
            ),
        )
    endpoint = (
        config.app_server_endpoint.replace("ws://", "http://", 1)
        .replace("wss://", "https://", 1)
        .rstrip("/")
        + "/readyz"
    )
    app_server_ready = False
    try:
        with urllib.request.urlopen(endpoint, timeout=2) as response:
            app_server_ready = response.status == 200
            check("app_server_ready", app_server_ready, endpoint)
    except Exception as error:
        check("app_server_ready", False, str(error))
    controller_service = observed_services[CONTROLLER_LABEL]
    check(
        "controller_service_loaded",
        controller_service.running,
        (
            f"{CONTROLLER_LABEL} pid={controller_service.pid}"
            if controller_service.running
            else f"loaded={controller_service.loaded}; state={controller_service.state}; pid={controller_service.pid}"
        ),
    )
    runtime_service = observed_services[APP_SERVER_LABEL]
    check(
        "shared_runtime_active",
        runtime_service.running,
        (
            f"{APP_SERVER_LABEL} pid={runtime_service.pid}"
            if runtime_service.running
            else f"unmanaged listener may be serving {endpoint}"
        ),
    )

    models: list[dict[str, Any]] = []
    codex_projects: list[dict[str, Any]] = []
    runtime_archon: dict[str, Any] | None = None
    archon_listed = False
    archon_runtime_error: str | None = None
    if app_server_ready:
        try:
            (
                models,
                codex_projects,
                runtime_archon,
                archon_listed,
                archon_runtime_error,
            ) = asyncio.run(
                _runtime_inventory(
                    config.app_server_endpoint,
                    (
                        str(configured_archon["native_thread_id"])
                        if configured_archon is not None
                        else None
                    ),
                )
            )
            configured_model = next(
                (
                    model
                    for model in models
                    if str(model.get("model") or model.get("id")) == config.archon_model
                ),
                None,
            )
            efforts = (
                {
                    str(item.get("reasoningEffort") or item.get("effort"))
                    for item in configured_model.get("supportedReasoningEfforts", [])
                    if isinstance(item, dict)
                }
                if configured_model
                else set()
            )
            check(
                "archon_model",
                configured_model is not None
                and (not efforts or config.archon_reasoning_effort in efforts),
                f"{config.archon_model}/{config.archon_reasoning_effort}",
            )
        except Exception as error:
            check("runtime_inventory", False, str(error))

    brain = Path(config.brain_root)
    brain_git = subprocess.run(
        ["git", "-C", str(brain), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    check(
        "brain_git_remote",
        brain_git.returncode == 0 and brain_git.stdout.strip() == config.brain_remote,
        brain_git.stdout.strip() or brain_git.stderr.strip() or str(brain),
    )
    beads_bin = shutil.which("bd") or "bd"
    beads_status, beads_error = _json_command(
        [beads_bin, "-C", str(brain), "status", "--json"]
    )
    summary = beads_status.get("summary") if isinstance(beads_status, dict) else None
    check(
        "brain_beads",
        isinstance(summary, dict),
        (
            beads_error or f"{summary.get('total_issues', 0)} issues"
            if isinstance(summary, dict)
            else "status unavailable"
        ),
    )
    dolt_remotes, dolt_error = _json_command(
        [beads_bin, "-C", str(brain), "dolt", "remote", "list", "--json"]
    )
    origin = (
        next(
            (
                item
                for item in dolt_remotes
                if isinstance(item, dict) and item.get("name") == "origin"
            ),
            None,
        )
        if isinstance(dolt_remotes, list)
        else None
    )
    check(
        "brain_dolt_remote",
        bool(origin and origin.get("status") == "ok"),
        dolt_error or str(origin or "origin unavailable"),
    )

    tollgate = Tollgate(shutil.which("tg")) if shutil.which("tg") else None
    for project in config.projects:
        root = Path(project.repo_path)
        remote = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
        )
        check(
            f"project:{project.project_id}:git",
            remote.returncode == 0
            and (
                project.source_remote is None
                or remote.stdout.strip() == project.source_remote
            ),
            remote.stdout.strip() or remote.stderr.strip() or str(root),
        )
        check(
            f"project:{project.project_id}:codex",
            bool(
                project.codex_project_id
                and _contains_identity(codex_projects, project.codex_project_id)
            ),
            project.codex_project_id or "unavailable",
        )
        try:
            observed = (
                tollgate.status(project.tollgate_repo_id)
                if tollgate is not None and project.tollgate_repo_id
                else None
            )
            state = observed.get("state") if isinstance(observed, dict) else None
            check(
                f"project:{project.project_id}:tollgate",
                bool(
                    isinstance(state, dict)
                    and state.get("id") == project.tollgate_repo_id
                    and Path(str(state.get("path"))).resolve(strict=False)
                    == root.resolve(strict=False)
                ),
                str(
                    state.get("execution_state")
                    if isinstance(state, dict)
                    else "unavailable"
                ),
            )
        except Exception as error:
            check(f"project:{project.project_id}:tollgate", False, str(error))

    check("controller_socket", paths.socket.exists(), str(paths.socket))
    if paths.database.is_file():
        try:
            with Store(
                paths.database,
                readonly=True,
                operative_journal=paths.operative_journal,
            ) as store:
                integrity = store.row("PRAGMA integrity_check")
                foreign_keys = store.row("PRAGMA foreign_keys")
                policies = store.rows("SELECT * FROM policies WHERE active = 1")
                archon = store.row(
                    "SELECT * FROM tasks WHERE role = 'archon' AND state NOT IN ('retired','archived')"
                )
                dispatch = store.row(
                    "SELECT value FROM meta WHERE key = 'dispatch_enabled'"
                )
                durable_ready, readiness_reasons = state_readiness(store)
                progress_ready, progress_reasons = progress_readiness(
                    store,
                    critical_workers={
                        "events",
                        "fallback",
                        "advancement",
                        "source-watch",
                    },
                )
            check(
                "sqlite_integrity",
                bool(integrity and next(iter(integrity.values())) == "ok"),
                str(integrity),
            )
            check(
                "sqlite_foreign_keys",
                bool(foreign_keys and next(iter(foreign_keys.values())) == 1),
                str(foreign_keys),
            )
            check(
                "archon",
                archon is not None,
                archon["native_thread_id"] if archon else "missing",
            )
            if archon is not None:
                turns = (
                    runtime_archon.get("turns")
                    if isinstance(runtime_archon, dict)
                    else None
                )
                check(
                    "archon_materialized",
                    isinstance(turns, list) and bool(turns),
                    (
                        f"{len(turns)} turns"
                        if isinstance(turns, list)
                        else archon_runtime_error or "runtime thread unavailable"
                    ),
                )
                check(
                    "archon_visible",
                    archon_listed,
                    (
                        "discoverable through thread/list"
                        if archon_listed
                        else archon_runtime_error
                        or "missing from active thread/list; rerun ./scripts/setup"
                    ),
                )
            check("policies", bool(policies), f"{len(policies)} active")
            check(
                "durable_readiness",
                durable_ready,
                "ready" if durable_ready else "; ".join(readiness_reasons),
            )
            check(
                "workflow_progress",
                progress_ready,
                "healthy" if progress_ready else "; ".join(progress_reasons),
            )
            check(
                "dispatch",
                bool(dispatch and dispatch["value"] == "1"),
                "enabled" if dispatch and dispatch["value"] == "1" else "disabled",
            )
        except Exception as error:
            check("controller_database", False, str(error))
    else:
        check("controller_database", False, str(paths.database))
    failures = [item for item in checks if not item["ok"]]
    return {
        "ok": not failures,
        "ready": not failures,
        "checks": checks,
        "failures": failures,
    }
