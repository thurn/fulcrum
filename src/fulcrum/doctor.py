"""Evidence-backed diagnostics for the assembled local product."""

from __future__ import annotations

import asyncio
import json
import os
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
    package_root,
)
from fulcrum.prompts import TEMPLATES, load_template
from fulcrum.readiness import state_readiness
from fulcrum.runtime import CodexRuntime
from fulcrum.store import Store
from fulcrum.tollgate import Tollgate


async def _runtime_inventory(
    endpoint: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    runtime = CodexRuntime(endpoint)
    try:
        await runtime.connect()
        return await runtime.list_models(), await runtime.list_projects()
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


def doctor(paths: RuntimePaths) -> dict[str, Any]:
    config = load_installation(paths.config_file)
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    check(
        "editable_import",
        package_root() == Path(config.source_root) / "src" / "fulcrum",
        str(package_root()),
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
    codex_root = Path.home() / ".codex" / "skills"
    for name in HUMAN_SKILLS:
        target = codex_root / name
        check(
            f"skill:{name}",
            target.is_symlink()
            and target.resolve(strict=False).is_relative_to(Path(config.source_root)),
            str(target),
        )
    for name in REMOVED_SKILLS:
        check(
            f"removed_skill:{name}",
            not (codex_root / name).exists(),
            str(codex_root / name),
        )
    agents = Path.home() / "Library" / "LaunchAgents"
    for label in (APP_SERVER_LABEL, CONTROLLER_LABEL):
        check(
            f"service:{label}",
            (agents / f"{label}.plist").is_file(),
            str(agents / f"{label}.plist"),
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
    domain = f"gui/{os.getuid()}"
    controller_service = subprocess.run(
        ["launchctl", "print", f"{domain}/{CONTROLLER_LABEL}"],
        capture_output=True,
        check=False,
    )
    check(
        "controller_service_loaded",
        controller_service.returncode == 0,
        CONTROLLER_LABEL,
    )
    runtime_service = subprocess.run(
        ["launchctl", "print", f"{domain}/{APP_SERVER_LABEL}"],
        capture_output=True,
        check=False,
    )
    check(
        "shared_runtime_active",
        runtime_service.returncode == 0 or app_server_ready,
        APP_SERVER_LABEL if runtime_service.returncode == 0 else endpoint,
    )

    models: list[dict[str, Any]] = []
    codex_projects: list[dict[str, Any]] = []
    if app_server_ready:
        try:
            models, codex_projects = asyncio.run(
                _runtime_inventory(config.app_server_endpoint)
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
            with Store(paths.database, readonly=True) as store:
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
            check("policies", bool(policies), f"{len(policies)} active")
            check(
                "durable_readiness",
                durable_ready,
                "ready" if durable_ready else "; ".join(readiness_reasons),
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
