"""Guided, repeatable one-command installation."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Any

from fulcrum.config import (
    InstallationConfig,
    ProjectConfig,
    RuntimePaths,
    load_installation,
    save_installation,
)
from fulcrum.install import (
    install_control_plane,
    install_links,
    install_services,
    start_services,
    verify_runtime_ownership_or_availability,
    verify_editable_source,
)
from fulcrum.ipc import request_sync


class SetupError(RuntimeError):
    pass


def _prompt(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default or ""


def _load_input(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SetupError(f"cannot read setup config {path}: {error}") from error
    if not isinstance(value, dict):
        raise SetupError("setup config must contain an object")
    return value


def _project(value: dict[str, Any]) -> ProjectConfig:
    try:
        path = str(Path(value["repo_path"]).expanduser().resolve(strict=True))
        project_id = str(value.get("project_id") or Path(path).name)
    except (KeyError, OSError) as error:
        raise SetupError(f"invalid project selection: {error}") from error
    return ProjectConfig(
        project_id=project_id,
        repo_path=path,
        codex_project_id=value.get("codex_project_id"),
        tollgate_repo_id=value.get("tollgate_repo_id"),
        validation_command=list(value.get("validation_command", [])),
        source_remote=value.get("source_remote"),
        enabled=bool(value.get("enabled", True)),
    )


def collect_config(
    paths: RuntimePaths, supplied: dict[str, Any], *, non_interactive: bool
) -> InstallationConfig:
    current = load_installation(paths.config_file)
    source = str(Path(supplied.get("source_root", current.source_root)).resolve())
    brain = str(
        Path(supplied.get("brain_root", current.brain_root)).expanduser().resolve()
    )
    projects_raw = supplied.get("projects")
    projects = (
        [_project(item) for item in projects_raw]
        if isinstance(projects_raw, list)
        else current.projects
    )
    archon_model = supplied.get("archon_model", current.archon_model)
    archon_effort = supplied.get(
        "archon_reasoning_effort", current.archon_reasoning_effort
    )
    brain_remote = supplied.get("brain_remote", current.brain_remote)
    if not non_interactive:
        if not archon_model:
            archon_model = _prompt("Archon model")
        if not archon_effort:
            archon_effort = _prompt("Archon reasoning effort", "high")
        if not brain_remote:
            brain_remote = _prompt("Private brain Git remote")
        if not projects:
            selected = _prompt("Repository to enroll", source)
            projects = [_project({"repo_path": selected})]
    missing: list[str] = []
    if not archon_model:
        missing.append("archon_model")
    if not archon_effort:
        missing.append("archon_reasoning_effort")
    if not brain_remote:
        missing.append("brain_remote")
    if not projects:
        missing.append("projects")
    for project in projects:
        if not project.validation_command:
            missing.append(f"projects[{project.project_id}].validation_command")
    if missing:
        raise SetupError(
            "setup incomplete; missing required choices: " + ", ".join(missing)
        )
    codex = supplied.get("codex_bin") or (shutil.which("codex") or current.codex_bin)
    desktop = supplied.get("desktop_executable", current.desktop_executable)
    return replace(
        current,
        source_root=source,
        brain_root=brain,
        state_root=str(paths.state_root),
        codex_bin=str(Path(codex).resolve()),
        desktop_executable=str(Path(desktop).resolve()),
        app_server_endpoint=supplied.get(
            "app_server_endpoint", current.app_server_endpoint
        ),
        archon_model=str(archon_model),
        archon_reasoning_effort=str(archon_effort),
        brain_remote=str(brain_remote),
        projects=projects,
        turn_check_after_seconds=int(
            supplied.get("turn_check_after_seconds", current.turn_check_after_seconds)
        ),
    )


def _prepare_brain(config: InstallationConfig) -> None:
    root = Path(config.brain_root)
    if not root.exists():
        result = subprocess.run(
            ["git", "clone", str(config.brain_remote), str(root)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            root.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init", str(root)], capture_output=True, check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "remote",
                    "add",
                    "origin",
                    str(config.brain_remote),
                ],
                capture_output=True,
                check=True,
            )
    bd = shutil.which("bd") or "bd"
    if (root / ".beads").is_dir():
        status = subprocess.run(
            [bd, "-C", str(root), "status"],
            capture_output=True,
            text=True,
            check=False,
        )
        if status.returncode == 0:
            return
    bootstrapped = subprocess.run(
        [
            bd,
            "-C",
            str(root),
            "bootstrap",
            "--non-interactive",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if bootstrapped.returncode != 0:
        raise SetupError(
            "Beads bootstrap failed: "
            + (
                bootstrapped.stderr.strip()
                or bootstrapped.stdout.strip()
                or "no diagnostic output"
            )
        )
    verified = subprocess.run(
        [bd, "-C", str(root), "status"],
        capture_output=True,
        text=True,
        check=False,
    )
    if verified.returncode != 0:
        raise SetupError(
            "Beads bootstrap did not produce a readable project: "
            + (verified.stderr.strip() or verified.stdout.strip() or "no output")
        )


def _ready_url(endpoint: str) -> str:
    return (
        endpoint.replace("ws://", "http://", 1)
        .replace("wss://", "https://", 1)
        .rstrip("/")
        + "/readyz"
    )


def _wait_ready(
    config: InstallationConfig, paths: RuntimePaths, timeout: float = 20
) -> None:
    deadline = time.monotonic() + timeout
    last = "not observed"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                _ready_url(config.app_server_endpoint), timeout=1
            ) as response:
                if response.status == 200 and paths.socket.exists():
                    return
        except Exception as error:
            last = str(error)
        time.sleep(0.2)
    raise SetupError(
        f"setup incomplete; shared runtime/controller readiness failed: {last}"
    )


def _wait_for_archon_readiness(
    paths: RuntimePaths, *, timeout: float
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        initialized = request_sync(
            paths.socket,
            {"command": "setup_initialize"},
            timeout=10,
        )
        data = initialized.get("data")
        if isinstance(data, dict) and data.get("ready"):
            return data
        time.sleep(0.5)
    return None


def run_setup(
    paths: RuntimePaths, *, input_path: Path | None, non_interactive: bool
) -> dict[str, Any]:
    supplied = _load_input(input_path)
    config = collect_config(paths, supplied, non_interactive=non_interactive)
    verify_editable_source(Path(config.source_root))
    for command in (
        config.codex_bin,
        shutil.which("git"),
        shutil.which("bd"),
        shutil.which("tg") or "/Applications/Tollgate.app/Contents/MacOS/tg",
    ):
        if not command or not Path(command).exists():
            raise SetupError(
                f"setup incomplete; required dependency unavailable: {command}"
            )
    verify_runtime_ownership_or_availability(config.app_server_endpoint)
    save_installation(paths.config_file, config)
    _prepare_brain(config)
    links = install_links(config)
    install_control_plane(config, paths)
    services, updated_services = install_services(config, paths)
    start_services(
        services,
        updated=updated_services,
        app_server_endpoint=config.app_server_endpoint,
    )
    _wait_ready(config, paths)
    initialized = request_sync(
        paths.socket, {"command": "setup_initialize"}, timeout=240
    )
    data = initialized.get("data", {})
    ready = bool(isinstance(data, dict) and data.get("ready"))
    if not ready:
        completed = _wait_for_archon_readiness(
            paths, timeout=float(config.turn_check_after_seconds)
        )
        if completed is not None:
            data = completed
            ready = True
    return {
        "ok": ready,
        "ready": ready,
        "status": (
            "ready"
            if ready
            else "setup incomplete; Archon must materialize and establish the required fleet configuration"
        ),
        "archon": data.get("archon") if isinstance(data, dict) else None,
        "projects": [project.project_id for project in config.projects],
        "cli": links["cli"],
        "desktop_launcher": services["desktop_wrapper"],
    }
