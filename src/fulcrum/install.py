"""Checkout-backed assets and macOS service configuration."""

from __future__ import annotations

import json
import os
import plistlib
import shlex
import shutil
import subprocess
import time
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fulcrum.config import InstallationConfig, RuntimePaths


class InstallationError(RuntimeError):
    pass


HUMAN_SKILLS = ("fulcrum-setup", "fulcrum-weaver", "fulcrum-archon")
REMOVED_SKILLS = (
    "fulcrum-executor",
    "fulcrum-overseer",
    "fulcrum-sage",
    "fulcrum-inquisitor",
    "fulcrum-night-watchman",
    "fulcrum-shared",
)
APP_SERVER_LABEL = "dev.fulcrum.codex-app-server"
CONTROLLER_LABEL = "dev.fulcrum.controller"
SYSTEM_EXECUTABLE_PATHS = (
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
    "/usr/local/sbin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)
USER_EXECUTABLE_PATHS = (".local/bin", "bin")


@dataclass(frozen=True)
class ServiceObservation:
    """Typed evidence about one launchd job, not merely its plist on disk."""

    label: str
    loaded: bool
    state: str | None
    pid: int | None
    executable_path: str | None
    program_arguments: tuple[str, ...]
    working_directory: str | None
    detail: str

    @property
    def running(self) -> bool:
        return self.loaded and self.state == "running" and self.pid is not None


def service_executable_path(*, user_home: Path | None = None) -> str:
    """Return a deterministic PATH suitable for processes started by launchd."""

    home = (user_home or Path.home()).resolve(strict=False)
    entries = [str(home / relative) for relative in USER_EXECUTABLE_PATHS]
    entries.extend(SYSTEM_EXECUTABLE_PATHS)
    return os.pathsep.join(entries)


def package_root() -> Path:
    return Path(__file__).resolve().parent


def control_plane_source(paths: RuntimePaths) -> Path:
    return paths.control_root / "runtime" / "current" / "fulcrum"


def _package_contents(package: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(package): path.read_bytes()
        for path in package.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }


def install_control_plane(config: InstallationConfig, paths: RuntimePaths) -> Path:
    """Atomically install an immutable controller snapshot outside managed source."""

    source = Path(config.source_root).resolve(strict=True) / "src" / "fulcrum"
    runtime_root = paths.control_root / "runtime"
    runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    deployment: Path | None = None
    for _attempt in range(3):
        before = _package_contents(source)
        candidate = runtime_root / f"deployment-{os.getpid()}-{uuid.uuid4().hex}"
        try:
            shutil.copytree(
                source.parent,
                candidate,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            after = _package_contents(source)
            copied = _package_contents(candidate / "fulcrum")
        except Exception:
            shutil.rmtree(candidate, ignore_errors=True)
            raise
        if before == after == copied and before:
            deployment = candidate
            break
        shutil.rmtree(candidate, ignore_errors=True)
    if deployment is None:
        raise InstallationError(
            "controller source changed throughout snapshot installation; retry when stable"
        )
    current = runtime_root / "current"
    temporary = runtime_root / f".current.{os.getpid()}.tmp"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(deployment.name, target_is_directory=True)
    os.replace(temporary, current)
    active = current.resolve(strict=True)
    for child in runtime_root.glob("deployment-*"):
        if child.resolve(strict=False) != active and child.is_dir():
            shutil.rmtree(child)
    return current / "fulcrum"


def controller_program_arguments(
    config: InstallationConfig, paths: RuntimePaths
) -> list[str]:
    python = Path(config.source_root) / ".venv" / "bin" / "python"
    runtime_root = control_plane_source(paths).parent
    launcher = (
        "import sys; "
        f"sys.path.insert(0, {str(runtime_root)!r}); "
        "from fulcrum.cli import main; raise SystemExit(main())"
    )
    return [str(python), "-I", "-c", launcher, "serve"]


def verify_editable_source(source_root: Path) -> None:
    root = source_root.resolve(strict=True)
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or Path(result.stdout.strip()).resolve() != root:
        raise InstallationError("source root must be the retained Git checkout root")
    if ".worktrees" in root.parts:
        raise InstallationError("source root cannot be a disposable worktree")
    if package_root() != root / "src" / "fulcrum":
        raise InstallationError(
            f"Fulcrum imports from {package_root()}, not editable source {root}"
        )


def _replace_owned_link(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.is_symlink():
        if target.resolve(strict=False) == source.resolve(strict=False):
            return
        target.unlink()
    elif target.exists():
        raise InstallationError(f"refusing to replace non-symlink asset {target}")
    temporary = target.with_name(f".{target.name}.{os.getpid()}")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(source.absolute(), target_is_directory=source.is_dir())
    os.replace(temporary, target)


def install_links(
    config: InstallationConfig, *, codex_root: Path | None = None
) -> dict[str, Any]:
    source = Path(config.source_root).resolve(strict=True)
    root = codex_root or Path.home() / ".codex"
    installed: list[str] = []
    for name in HUMAN_SKILLS:
        skill = source / "skills" / name
        if not (skill / "SKILL.md").is_file():
            raise InstallationError(f"missing human entry skill {skill}")
        target = root / "skills" / name
        _replace_owned_link(skill, target)
        installed.append(str(target))
    removed: list[str] = []
    for name in REMOVED_SKILLS:
        target = root / "skills" / name
        if target.is_symlink():
            resolved = target.resolve(strict=False)
            if resolved.is_relative_to(source) or not resolved.exists():
                target.unlink()
                removed.append(str(target))
    hook_source = source / "hooks" / "fulcrum-hook"
    cli_source = source / ".venv" / "bin" / "fulcrum"
    if not os.access(hook_source, os.X_OK) or not os.access(cli_source, os.X_OK):
        raise InstallationError("editable CLI and hook executables must exist")
    hook_target = root / "hooks" / "fulcrum-hook"
    cli_target = root / "bin" / "fulcrum"
    obsolete_hook = root / "hooks" / "fulcrum"
    if obsolete_hook.is_symlink() and obsolete_hook.resolve(
        strict=False
    ).is_relative_to(source):
        obsolete_hook.unlink()
        removed.append(str(obsolete_hook))
    _replace_owned_link(hook_source, hook_target)
    _replace_owned_link(cli_source, cli_target)
    install_hook_config(root / "hooks.json", hook_target)
    return {
        "skills": installed,
        "removed": removed,
        "hook": str(hook_target),
        "cli": str(cli_target),
    }


def install_hook_config(path: Path, hook_command: Path) -> None:
    """Preserve unrelated hooks and install only one marked compact handler."""

    existing: Any = (
        json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    )
    if not isinstance(existing, dict) or not isinstance(
        existing.get("hooks", {}), dict
    ):
        raise InstallationError(f"Codex hook configuration is invalid: {path}")
    hooks = dict(existing.get("hooks", {}))
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        retained = []
        for group in groups:
            if not isinstance(group, dict):
                retained.append(group)
                continue
            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                retained.append(group)
                continue
            remaining = [
                item
                for item in handlers
                if not (
                    isinstance(item, dict)
                    and str(item.get("statusMessage", "")).startswith("Fulcrum:")
                )
            ]
            if remaining:
                retained.append({**group, "hooks": remaining})
        if retained:
            hooks[event] = retained
        else:
            hooks.pop(event, None)
    hooks["SessionStart"] = [
        *hooks.get("SessionStart", []),
        {
            "matcher": "^compact$",
            "hooks": [
                {
                    "type": "command",
                    "command": shlex.quote(str(hook_command.absolute())),
                    "timeout": 2,
                    "additionalContextLimit": 5000,
                    "statusMessage": "Fulcrum: restoring current action",
                }
            ],
        },
    ]
    existing["hooks"] = hooks
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    temporary.write_text(
        json.dumps(existing, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def service_definitions(
    config: InstallationConfig,
    paths: RuntimePaths,
    *,
    user_home: Path | None = None,
) -> dict[str, dict[str, Any]]:
    source = Path(config.source_root)
    logs = paths.logs_root
    executable_path = service_executable_path(user_home=user_home)
    return {
        APP_SERVER_LABEL: {
            "Label": APP_SERVER_LABEL,
            "ProgramArguments": [
                config.codex_bin,
                "app-server",
                "--listen",
                config.app_server_endpoint,
            ],
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(logs / "app-server.log"),
            "StandardErrorPath": str(logs / "app-server-error.log"),
            "EnvironmentVariables": {"PATH": executable_path},
        },
        CONTROLLER_LABEL: {
            "Label": CONTROLLER_LABEL,
            "ProgramArguments": controller_program_arguments(config, paths),
            "WorkingDirectory": str(paths.control_root),
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(logs / "controller.log"),
            "StandardErrorPath": str(logs / "controller-error.log"),
            "EnvironmentVariables": {
                "FULCRUM_CONFIG": str(paths.config_file),
                "PATH": executable_path,
            },
        },
    }


def install_services(
    config: InstallationConfig,
    paths: RuntimePaths,
    *,
    launch_agents: Path | None = None,
    user_home: Path | None = None,
) -> tuple[dict[str, str], frozenset[str]]:
    target_root = launch_agents or Path.home() / "Library" / "LaunchAgents"
    target_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths.logs_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    installed: dict[str, str] = {}
    updated: set[str] = set()
    for label, definition in service_definitions(
        config, paths, user_home=user_home
    ).items():
        target = target_root / f"{label}.plist"
        encoded = plistlib.dumps(definition, fmt=plistlib.FMT_XML, sort_keys=True)
        if not target.is_file() or target.read_bytes() != encoded:
            temporary = target.with_name(f".{target.name}.{os.getpid()}")
            temporary.write_bytes(encoded)
            os.replace(temporary, target)
            updated.add(label)
        installed[label] = str(target)
    wrapper = paths.control_root / "open-codex-with-fulcrum"
    wrapper.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    script = f'#!/bin/sh\nexec env CODEX_APP_SERVER_WS_URL={json.dumps(config.app_server_endpoint)} {json.dumps(config.desktop_executable)} "$@"\n'
    if not wrapper.is_file() or wrapper.read_text(encoding="utf-8") != script:
        temporary = wrapper.with_name(f".{wrapper.name}.{os.getpid()}")
        temporary.write_text(script, encoding="utf-8")
        os.chmod(temporary, 0o700)
        os.replace(temporary, wrapper)
    installed["desktop_wrapper"] = str(wrapper)
    return installed, frozenset(updated)


def _installed_service_path(definition: str) -> str | None:
    try:
        with Path(definition).open("rb") as handle:
            service = plistlib.load(handle)
        value = service.get("EnvironmentVariables", {}).get("PATH")
        return value if isinstance(value, str) else None
    except (OSError, plistlib.InvalidFileException, AttributeError):
        return None


def _launchctl_text(output: bytes | str | None) -> str:
    return (
        output.decode(errors="replace") if isinstance(output, bytes) else output or ""
    )


def _loaded_service_path(output: bytes | str | None) -> str | None:
    text = _launchctl_text(output)
    in_environment = False
    for line in text.splitlines():
        if line == "\tenvironment = {":
            in_environment = True
        elif in_environment and line == "\t}":
            return None
        elif in_environment and line.startswith("\t\tPATH => "):
            return line.removeprefix("\t\tPATH => ")
    return None


def _service_observation_from_result(
    label: str, result: subprocess.CompletedProcess[Any]
) -> ServiceObservation:
    text = _launchctl_text(result.stdout if result.returncode == 0 else result.stderr)
    state: str | None = None
    pid: int | None = None
    working_directory: str | None = None
    arguments: list[str] = []
    in_arguments = False
    for line in text.splitlines():
        if line == "\targuments = {":
            in_arguments = True
            continue
        if in_arguments:
            if line == "\t}":
                in_arguments = False
            elif line.startswith("\t\t"):
                arguments.append(line[2:])
            continue
        if line.startswith("\t\t") or not line.startswith("\t"):
            continue
        field = line[1:]
        if field.startswith("state = ") and state is None:
            state = field.removeprefix("state = ")
        elif field.startswith("pid = ") and pid is None:
            try:
                pid = int(field.removeprefix("pid = "))
            except ValueError:
                pid = None
        elif field.startswith("working directory = "):
            working_directory = field.removeprefix("working directory = ")
    return ServiceObservation(
        label=label,
        loaded=result.returncode == 0,
        state=state,
        pid=pid,
        executable_path=_loaded_service_path(result.stdout),
        program_arguments=tuple(arguments),
        working_directory=working_directory,
        detail=text.strip(),
    )


def inspect_service(label: str) -> ServiceObservation:
    domain = f"gui/{os.getuid()}"
    result = subprocess.run(
        ["launchctl", "print", f"{domain}/{label}"],
        capture_output=True,
        check=False,
    )
    return _service_observation_from_result(label, result)


def _endpoint_is_ready(endpoint: str) -> bool:
    ready_url = (
        endpoint.replace("ws://", "http://", 1)
        .replace("wss://", "https://", 1)
        .rstrip("/")
        + "/readyz"
    )
    try:
        with urllib.request.urlopen(ready_url, timeout=1) as response:
            return response.status == 200
    except Exception:
        return False


def _wait_for_running_service(label: str, *, timeout: float = 10) -> ServiceObservation:
    deadline = time.monotonic() + timeout
    consecutive = 0
    latest = inspect_service(label)
    while time.monotonic() < deadline:
        latest = inspect_service(label)
        if latest.running:
            consecutive += 1
            if consecutive >= 2:
                return latest
        else:
            consecutive = 0
        time.sleep(0.2)
    return latest


def _wait_for_unloaded_service(
    label: str, *, timeout: float = 10
) -> ServiceObservation:
    """Wait until launchd has finished removing a booted-out job."""

    deadline = time.monotonic() + timeout
    latest = inspect_service(label)
    while latest.loaded and time.monotonic() < deadline:
        time.sleep(0.2)
        latest = inspect_service(label)
    return latest


def verify_runtime_ownership_or_availability(endpoint: str) -> ServiceObservation:
    """Reject a ready app-server endpoint not owned by the configured launchd job."""

    observation = inspect_service(APP_SERVER_LABEL)
    if _endpoint_is_ready(endpoint) and not observation.running:
        raise InstallationError(
            f"refusing to accept an unmanaged listener at {endpoint}; "
            f"{APP_SERVER_LABEL} is not the running owner"
        )
    return observation


def start_services(
    definitions: dict[str, str],
    *,
    updated: frozenset[str],
    app_server_endpoint: str | None = None,
) -> None:
    domain = f"gui/{os.getuid()}"
    initial_app_server = (
        verify_runtime_ownership_or_availability(app_server_endpoint)
        if app_server_endpoint is not None
        else inspect_service(APP_SERVER_LABEL)
    )
    for label in (APP_SERVER_LABEL, CONTROLLER_LABEL):
        observation = (
            initial_app_server if label == APP_SERVER_LABEL else inspect_service(label)
        )
        loaded = observation.loaded
        expected_path = _installed_service_path(definitions[label])
        loaded_path = observation.executable_path
        reloading = loaded and (
            label in updated
            or (expected_path is not None and loaded_path != expected_path)
        )
        if reloading:
            result = subprocess.run(
                ["launchctl", "bootout", f"{domain}/{label}"],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                raise InstallationError(
                    f"could not unload stale {label}: "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )
            stopped = _wait_for_unloaded_service(label)
            if stopped.loaded:
                raise InstallationError(
                    f"configured service {label} did not finish unloading; "
                    f"domain={domain}; state={stopped.state!r}; "
                    f"pid={stopped.pid!r}; detail={stopped.detail!r}"
                )
            loaded = False
            if (
                label == APP_SERVER_LABEL
                and app_server_endpoint is not None
                and _endpoint_is_ready(app_server_endpoint)
            ):
                raise InstallationError(
                    f"{APP_SERVER_LABEL} stopped but {app_server_endpoint} remains "
                    "occupied; refusing to start competing runtimes"
                )
        if not loaded:
            result = subprocess.run(
                ["launchctl", "bootstrap", domain, definitions[label]],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                observed = subprocess.run(
                    ["launchctl", "print", f"{domain}/{label}"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if observed.returncode != 0:
                    retried = subprocess.run(
                        ["launchctl", "bootstrap", domain, definitions[label]],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                else:
                    retried = observed
                if retried.returncode != 0:
                    raise InstallationError(
                        "could not establish configured service "
                        f"{label}; domain={domain}; plist={definitions[label]}; "
                        f"first_stdout={result.stdout.strip()!r}; "
                        f"first_stderr={result.stderr.strip()!r}; "
                        f"observed_state={observed.stdout.strip() or observed.stderr.strip()!r}; "
                        f"retry_stdout={retried.stdout.strip()!r}; "
                        f"retry_stderr={retried.stderr.strip()!r}"
                    )
        else:
            result = subprocess.run(
                ["launchctl", "kickstart", f"{domain}/{label}"],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                raise InstallationError(
                    f"could not kickstart {label}: "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )
        verified = _wait_for_running_service(label)
        if not verified.running:
            raise InstallationError(
                f"configured service {label} is not stably running after setup; "
                f"domain={domain}; plist={definitions[label]}; "
                f"loaded={verified.loaded}; state={verified.state!r}; "
                f"pid={verified.pid!r}; detail={verified.detail!r}"
            )
        verified_path = verified.executable_path
        if expected_path is not None and verified_path != expected_path:
            raise InstallationError(
                f"configured service {label} loaded with an unexpected PATH; "
                f"expected={expected_path!r}; observed={verified_path!r}"
            )
