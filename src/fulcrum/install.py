"""Fulcrum2 assets and per-instance macOS service definitions."""

from __future__ import annotations

import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class InstallationError(RuntimeError):
    pass


HUMAN_SKILLS = (
    "fulcrum-vizier",
    "fulcrum-marshal",
    "fulcrum-weaver",
    "fulcrum-executor",
    "fulcrum-warden",
    "fulcrum-sage",
    "fulcrum-mason",
    "fulcrum-justiciar",
    "fulcrum-bead",
)
REMOVED_SKILLS = (
    "fulcrum-setup",
    "fulcrum-archon",
    "fulcrum-operative",
    "fulcrum-overseer",
    "fulcrum-inquisitor",
    "fulcrum-night-watchman",
    "fulcrum-shared",
)
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


@dataclass(frozen=True)
class InstalledService:
    """One instance-owned launch service and its exact definition."""

    name: str
    label: str
    definition: Path


def package_root() -> Path:
    return Path(__file__).resolve().parent


def installation_source_root() -> Path:
    """Return the retained source when present, otherwise the installed package."""

    package = package_root()
    candidate = package.parents[1]
    if (candidate / "pyproject.toml").is_file() and (candidate / "skills").is_dir():
        return candidate
    return package


def owned_skills_source() -> Path:
    source = installation_source_root()
    checkout_skills = source / "skills"
    if checkout_skills.is_dir():
        return checkout_skills
    packaged = Path(sys.prefix) / "share" / "fulcrum" / "skills"
    return packaged if packaged.is_dir() else package_root() / "skills"


def _replace_owned_link(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.is_symlink():
        if target.resolve(strict=False) == source.resolve(strict=False):
            return
        target.unlink()
    elif target.exists():
        raise InstallationError(f"refusing to replace real user asset {target}")
    temporary = target.with_name(f".{target.name}.{os.getpid()}")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(source.absolute(), target_is_directory=source.is_dir())
    os.replace(temporary, target)


def install_hook_config(path: Path, command: str) -> None:
    """Preserve unrelated hooks and install one read-only compact handler."""

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
                    "command": command,
                    "timeout": 2,
                    "additionalContextLimit": 5000,
                    "statusMessage": "Fulcrum: restoring current work context",
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


def reconcile_fulcrum2_skills(
    instance_root: Path,
    *,
    production: bool,
    skills_root: Path | None = None,
) -> dict[str, Any]:
    """Repair owned role links and the read-only compaction hook."""

    source = owned_skills_source().resolve(strict=True)
    root = (
        skills_root
        or (
            Path.home() / ".codex" / "skills"
            if production
            else instance_root / "codex" / "skills"
        )
    ).resolve(strict=False)
    installed: list[str] = []
    unchanged: list[str] = []
    for name in HUMAN_SKILLS:
        skill = source / name
        if (
            not (skill / "SKILL.md").is_file()
            or not (skill / "agents" / "openai.yaml").is_file()
        ):
            raise InstallationError(f"missing packaged skill assets for {name}")
        target = root / name
        if target.exists() and not target.is_symlink():
            raise InstallationError(
                f"refusing to replace real user skill directory {target}"
            )
        if target.is_symlink() and target.resolve(strict=False) == skill.resolve(
            strict=False
        ):
            unchanged.append(str(target))
            continue
        _replace_owned_link(skill, target)
        installed.append(str(target))
    removed: list[str] = []
    for name in REMOVED_SKILLS:
        target = root / name
        if not target.is_symlink():
            continue
        resolved = target.resolve(strict=False)
        if resolved.is_relative_to(source) or not resolved.exists():
            target.unlink()
            removed.append(str(target))

    executable = instance_root / "runtime" / "current" / "bin" / "fulcrum"
    hook_config = root.parent / "hooks.json"
    hook_command: str | None = None
    if executable.is_file() and os.access(executable, os.X_OK):
        hook_command = " ".join(
            (
                shlex.quote(str(executable.resolve(strict=True))),
                "hook context --input - --instance",
                shlex.quote(str(instance_root.resolve(strict=False))),
            )
        )
        install_hook_config(hook_config, hook_command)
    return {
        "root": str(root),
        "installed": installed,
        "unchanged": unchanged,
        "removed": removed,
        "hook": {
            "config": str(hook_config),
            "command": hook_command,
            "installed": hook_command is not None,
        },
        "implicit_invocation": False,
    }


def _service_identity(instance_root: Path, brain_root: Path) -> str:
    identity_path = instance_root / "service-identity"
    expected_binding = (
        f"{instance_root.resolve(strict=False)}\n"
        f"{brain_root.resolve(strict=False)}\n"
    )
    if identity_path.exists() and identity_path.is_dir():
        raise InstallationError(
            f"service identity path is a user directory: {identity_path}"
        )
    if identity_path.is_file():
        lines = identity_path.read_text(encoding="utf-8").splitlines()
        if len(lines) != 3 or "\n".join(lines[:2]) + "\n" != expected_binding:
            raise InstallationError(
                "installed service identity is bound to a different instance or brain"
            )
        token = lines[2]
        if re.fullmatch(r"[a-f0-9]{32}", token):
            return token
        raise InstallationError(f"invalid installed service identity: {identity_path}")
    instance_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = uuid.uuid4().hex
    temporary = identity_path.with_name(f".{identity_path.name}.{os.getpid()}")
    temporary.write_text(expected_binding + token + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, identity_path)
    return token


def service_executable_path(*, user_home: Path | None = None) -> str:
    """Return a deterministic PATH suitable for processes started by launchd."""

    home = (user_home or Path.home()).resolve(strict=False)
    entries = [str(home / relative) for relative in USER_EXECUTABLE_PATHS]
    entries.extend(SYSTEM_EXECUTABLE_PATHS)
    return os.pathsep.join(entries)


def fulcrum2_service_definitions(
    *,
    instance_root: Path,
    config_path: Path,
    brain_root: Path,
    config: dict[str, Any],
    controller_executable: Path,
    production: bool,
) -> dict[str, dict[str, Any]]:
    """Build exact, uniquely owned per-instance LaunchAgent definitions."""

    token = _service_identity(instance_root, brain_root)
    prefix = f"dev.fulcrum.{token}"
    logs = instance_root / "logs"
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    environment = {
        "PATH": service_executable_path(),
        "FULCRUM_INSTANCE": str(instance_root.resolve(strict=False)),
    }
    beads = dict(config["beads"])
    dolt = shutil.which("dolt")
    if not dolt:
        raise InstallationError("required Dolt executable is unavailable")
    definitions: dict[str, dict[str, Any]] = {
        "dolt": {
            "Label": f"{prefix}.dolt",
            "ProgramArguments": [
                str(Path(dolt).resolve(strict=True)),
                "sql-server",
                "-H",
                str(beads["host"]),
                "-P",
                str(beads["port"]),
                "--data-dir",
                str(brain_root / ".beads" / "dolt"),
            ],
            "WorkingDirectory": str(brain_root),
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(logs / "dolt.log"),
            "StandardErrorPath": str(logs / "dolt-error.log"),
            "EnvironmentVariables": environment,
        },
        "controller": {
            "Label": f"{prefix}.controller",
            "ProgramArguments": [
                str(controller_executable.resolve(strict=True)),
                "serve",
                "--instance",
                str(instance_root.resolve(strict=False)),
                "--config",
                str(config_path.resolve(strict=False)),
            ],
            "WorkingDirectory": str(instance_root),
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(logs / "controller.log"),
            "StandardErrorPath": str(logs / "controller-error.log"),
            "EnvironmentVariables": environment,
        },
    }
    source_watch_root = config.get("source_watch_root")
    if isinstance(source_watch_root, str):
        definitions["updater"] = {
            "Label": f"{prefix}.updater",
            "ProgramArguments": [
                str(controller_executable.resolve(strict=True)),
                "service",
                "update",
                "--source",
                str(Path(source_watch_root).resolve(strict=True)),
                "--instance",
                str(instance_root.resolve(strict=False)),
                "--timeout",
                "300",
                "--json",
            ],
            "WorkingDirectory": str(instance_root),
            "RunAtLoad": False,
            "KeepAlive": False,
            "ProcessType": "Background",
            "StandardOutPath": str(logs / "updater.log"),
            "StandardErrorPath": str(logs / "updater-error.log"),
            "EnvironmentVariables": environment,
        }
    runtime = dict(config["runtime"])
    if production and runtime["kind"] == "codex":
        executable = runtime.get("executable")
        if not isinstance(executable, str) or not executable:
            raise InstallationError("runtime.executable is required for production")
        definitions["runtime"] = {
            "Label": f"{prefix}.runtime",
            "ProgramArguments": [
                str(Path(executable).resolve(strict=True)),
                "app-server",
                "--listen",
                str(runtime["endpoint"]),
            ],
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(logs / "runtime.log"),
            "StandardErrorPath": str(logs / "runtime-error.log"),
            "EnvironmentVariables": environment,
            "SoftResourceLimits": {
                "NumberOfFiles": int(config["resources"]["fd_soft_limit"])
            },
        }
    return definitions


def install_fulcrum2_service_definitions(
    definitions: dict[str, dict[str, Any]], instance_root: Path
) -> tuple[dict[str, InstalledService], list[str]]:
    root = instance_root / "services"
    if root.exists() and not root.is_dir():
        raise InstallationError(f"service asset path is not a directory: {root}")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    installed: dict[str, InstalledService] = {}
    changed: list[str] = []
    for name, definition in definitions.items():
        label = str(definition["Label"])
        path = root / f"{name}.plist"
        encoded = plistlib.dumps(definition, fmt=plistlib.FMT_XML, sort_keys=True)
        if not path.is_file() or path.read_bytes() != encoded:
            temporary = path.with_name(f".{path.name}.{os.getpid()}")
            temporary.write_bytes(encoded)
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            changed.append(name)
        installed[name] = InstalledService(name, label, path)
    return installed, changed


def _package_contents(package: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(package): path.read_bytes()
        for path in package.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }


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
