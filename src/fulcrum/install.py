"""Checkout-backed assets and macOS service configuration."""

from __future__ import annotations

import json
import os
import plistlib
import shlex
import subprocess
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


def package_root() -> Path:
    return Path(__file__).resolve().parent


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
    config: InstallationConfig, paths: RuntimePaths
) -> dict[str, dict[str, Any]]:
    source = Path(config.source_root)
    python = source / ".venv" / "bin" / "python"
    logs = paths.logs_root
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
        },
        CONTROLLER_LABEL: {
            "Label": CONTROLLER_LABEL,
            "ProgramArguments": [str(python), "-m", "fulcrum.cli", "serve"],
            "WorkingDirectory": str(source),
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(logs / "controller.log"),
            "StandardErrorPath": str(logs / "controller-error.log"),
            "EnvironmentVariables": {"FULCRUM_CONFIG": str(paths.config_file)},
        },
    }


def install_services(
    config: InstallationConfig,
    paths: RuntimePaths,
    *,
    launch_agents: Path | None = None,
) -> dict[str, str]:
    target_root = launch_agents or Path.home() / "Library" / "LaunchAgents"
    target_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths.logs_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    installed: dict[str, str] = {}
    for label, definition in service_definitions(config, paths).items():
        target = target_root / f"{label}.plist"
        encoded = plistlib.dumps(definition, fmt=plistlib.FMT_XML, sort_keys=True)
        if not target.is_file() or target.read_bytes() != encoded:
            temporary = target.with_name(f".{target.name}.{os.getpid()}")
            temporary.write_bytes(encoded)
            os.replace(temporary, target)
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
    return installed


def start_services(definitions: dict[str, str]) -> None:
    domain = f"gui/{os.getuid()}"
    for label in (APP_SERVER_LABEL, CONTROLLER_LABEL):
        check = subprocess.run(
            ["launchctl", "print", f"{domain}/{label}"],
            capture_output=True,
            check=False,
        )
        if check.returncode != 0:
            result = subprocess.run(
                ["launchctl", "bootstrap", domain, definitions[label]],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                raise InstallationError(
                    f"could not start {label}: {result.stderr.strip() or result.stdout.strip()}"
                )
        else:
            subprocess.run(
                ["launchctl", "kickstart", f"{domain}/{label}"],
                capture_output=True,
                check=False,
            )
