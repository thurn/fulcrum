"""Self-contained removal of Fulcrum-owned local files and Codex configuration."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MCP_BEGIN = "# BEGIN FULCRUM STOCK DESKTOP"
MCP_END = "# END FULCRUM STOCK DESKTOP"
OWNED_SKILLS = (
    "fulcrum-bootstrap",
    "fulcrum-uninstall",
    "fulcrum-vizier",
    "fulcrum-marshal",
    "weaver",
    "fulcrum-executor",
    "fulcrum-warden",
    "fulcrum-sage",
    "fulcrum-mason",
    "fulcrum-justiciar",
    "fulcrum-bead",
    "fulcrum-weaver",
    "fulcrum-setup",
    "fulcrum-archon",
    "fulcrum-operative",
    "fulcrum-overseer",
    "fulcrum-inquisitor",
    "fulcrum-night-watchman",
    "fulcrum-shared",
)


@dataclass(frozen=True)
class UninstallPaths:
    home: Path
    source: Path
    instance: Path
    config: Path
    brain: Path
    codex_root: Path


@dataclass
class UninstallResult:
    dry_run: bool
    remove_source: bool
    planned: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": not self.errors,
            "dry_run": self.dry_run,
            "remove_source": self.remove_source,
            "planned": self.planned,
            "removed": self.removed,
            "changed": self.changed,
            "skipped": self.skipped,
            "errors": self.errors,
            "codex_restart_may_be_required": True,
            "native_cleanup_required": [
                "archive only Fulcrum role tasks",
                "delete the Fulcrum Marshal heartbeat",
            ],
        }


def _absolute(path: Path) -> Path:
    return path.expanduser().absolute()


def _validate(paths: UninstallPaths, *, remove_source: bool) -> None:
    home = _absolute(paths.home)
    candidates = {
        "source": paths.source,
        "instance": paths.instance,
        "brain": paths.brain,
    }
    for name, candidate in candidates.items():
        target = _absolute(candidate)
        if target in {Path("/"), home, home.parent}:
            raise ValueError(f"refusing unsafe {name} path: {target}")
    if _absolute(paths.codex_root) in {Path("/"), home}:
        raise ValueError(f"refusing unsafe Codex root: {paths.codex_root}")
    if remove_source and _absolute(paths.source) == _absolute(paths.codex_root):
        raise ValueError("source checkout cannot be the Codex root")


def _remove_table(text: str, header: str) -> str:
    lines = text.splitlines(keepends=True)
    output: list[str] = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        if stripped == header:
            skipping = True
            continue
        if skipping and stripped.startswith("["):
            skipping = False
        if not skipping:
            output.append(line)
    return "".join(output)


def remove_fulcrum_toml(text: str, source: Path) -> str:
    """Remove only Fulcrum-owned MCP and project configuration."""

    marker = re.compile(
        rf"(?ms)^\s*{re.escape(MCP_BEGIN)}\n.*?^\s*{re.escape(MCP_END)}\s*\n?"
    )
    updated = marker.sub("", text)
    updated = _remove_table(updated, "[mcp_servers.fulcrum]")
    project_header = f"[projects.{json.dumps(str(_absolute(source)))}]"
    return _remove_table(updated, project_header)


def remove_fulcrum_hooks(
    document: dict[str, Any], *, source: Path, instance: Path
) -> dict[str, Any]:
    """Remove Fulcrum command handlers while preserving unrelated hooks."""

    updated = dict(document)
    original_hooks = document.get("hooks", {})
    if not isinstance(original_hooks, dict):
        raise ValueError("Codex hook configuration has a non-object hooks value")
    hooks: dict[str, Any] = {}
    source_text = str(_absolute(source))
    instance_text = str(_absolute(instance))
    for event, groups in original_hooks.items():
        if not isinstance(groups, list):
            hooks[event] = groups
            continue
        retained_groups: list[Any] = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                retained_groups.append(group)
                continue
            retained_handlers = []
            for handler in group["hooks"]:
                if not isinstance(handler, dict):
                    retained_handlers.append(handler)
                    continue
                status = str(handler.get("statusMessage", ""))
                command = str(handler.get("command", ""))
                owned = status.startswith("Fulcrum:") or (
                    "fulcrum hook handle" in command
                    and (source_text in command or instance_text in command)
                )
                if not owned:
                    retained_handlers.append(handler)
            if retained_handlers:
                retained_groups.append({**group, "hooks": retained_handlers})
        if retained_groups:
            hooks[event] = retained_groups
    updated["hooks"] = hooks
    return updated


def _atomic_text(path: Path, text: str, *, mode: int = 0o600) -> None:
    temporary = path.with_name(f".{path.name}.fulcrum-uninstall-{os.getpid()}")
    temporary.write_text(text, encoding="utf-8")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def _inside(path: Path, root: Path) -> bool:
    try:
        _absolute(path).resolve(strict=False).relative_to(
            _absolute(root).resolve(strict=False)
        )
        return True
    except ValueError:
        return False


def _owned_link(path: Path, source: Path) -> bool:
    if not path.is_symlink():
        return False
    try:
        target = path.resolve(strict=False)
    except OSError:
        return path.name in OWNED_SKILLS
    return _inside(target, source) or (
        not target.exists() and path.name in OWNED_SKILLS
    )


def _service_labels(instance: Path, home: Path) -> set[str]:
    labels: set[str] = set()
    identity = instance / "service-identity"
    try:
        lines = identity.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    if len(lines) == 3 and re.fullmatch(r"[a-f0-9]{32}", lines[2]):
        labels.update(
            {
                f"dev.fulcrum.{lines[2]}.broker",
                f"dev.fulcrum.{lines[2]}.dolt",
            }
        )
    for plist in [
        *(instance / "services").glob("*.plist"),
        *(home / "Library" / "LaunchAgents").glob("dev.fulcrum.*.plist"),
    ]:
        try:
            label = str(plistlib.loads(plist.read_bytes()).get("Label", ""))
        except (OSError, plistlib.InvalidFileException):
            continue
        if label.startswith("dev.fulcrum."):
            labels.add(label)
    return labels


def _remove_path(path: Path, result: UninstallResult) -> None:
    label = str(path)
    if not path.exists() and not path.is_symlink():
        return
    result.planned.append(label)
    if result.dry_run:
        return
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        else:
            shutil.rmtree(path)
        result.removed.append(label)
    except OSError as error:
        result.errors.append(f"remove {label}: {error}")


def _rewrite_configs(paths: UninstallPaths, result: UninstallResult) -> None:
    config_toml = paths.codex_root / "config.toml"
    if config_toml.is_file():
        try:
            original = config_toml.read_text(encoding="utf-8")
            updated = remove_fulcrum_toml(original, paths.source)
            if updated != original:
                result.planned.append(f"edit {config_toml}")
                if not result.dry_run:
                    _atomic_text(config_toml, updated)
                    result.changed.append(str(config_toml))
        except OSError as error:
            result.errors.append(f"edit {config_toml}: {error}")

    hooks_path = paths.codex_root / "hooks.json"
    if hooks_path.is_file():
        try:
            original_text = hooks_path.read_text(encoding="utf-8")
            original = json.loads(original_text)
            if not isinstance(original, dict):
                raise ValueError("root value is not an object")
            updated = remove_fulcrum_hooks(
                original, source=paths.source, instance=paths.instance
            )
            updated_text = json.dumps(updated, indent=2, sort_keys=True) + "\n"
            if updated_text != original_text:
                result.planned.append(f"edit {hooks_path}")
                if not result.dry_run:
                    _atomic_text(hooks_path, updated_text)
                    result.changed.append(str(hooks_path))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            result.errors.append(f"edit {hooks_path}: {error}")


def _stop_services(paths: UninstallPaths, result: UninstallResult) -> None:
    for label in sorted(_service_labels(paths.instance, paths.home)):
        action = f"launchctl bootout gui/{os.getuid()}/{label}"
        result.planned.append(action)
        if result.dry_run:
            continue
        completed = subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        detail = (completed.stderr or completed.stdout).strip().lower()
        if completed.returncode and not any(
            phrase in detail for phrase in ("could not find", "no such process")
        ):
            result.errors.append(f"{action}: {detail or completed.returncode}")


def _stop_mcp_processes(paths: UninstallPaths, result: UninstallResult) -> None:
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as error:
        result.errors.append(f"inspect Fulcrum MCP processes: {error}")
        return
    executable = str(paths.source / ".venv" / "bin" / "fulcrum-mcp")
    instance_flag = f"--instance {paths.instance}"
    pids: list[int] = []
    for row in completed.stdout.splitlines():
        fields = row.strip().split(maxsplit=1)
        if len(fields) != 2 or "fulcrum-mcp" not in fields[1]:
            continue
        command = fields[1]
        if executable not in command and instance_flag not in command:
            continue
        try:
            pid = int(fields[0])
        except ValueError:
            continue
        if pid != os.getpid():
            pids.append(pid)
            result.planned.append(f"terminate Fulcrum MCP process {pid}")
    if result.dry_run:
        return
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except OSError as error:
            result.errors.append(f"terminate Fulcrum MCP process {pid}: {error}")
    if pids:
        time.sleep(0.2)


def uninstall(
    paths: UninstallPaths,
    *,
    apply: bool,
    remove_source: bool,
    external_actions: bool = True,
) -> UninstallResult:
    """Remove one canonical installation while preserving unrelated user data."""

    paths = UninstallPaths(
        home=_absolute(paths.home),
        source=_absolute(paths.source),
        instance=_absolute(paths.instance),
        config=_absolute(paths.config),
        brain=_absolute(paths.brain),
        codex_root=_absolute(paths.codex_root),
    )
    _validate(paths, remove_source=remove_source)
    result = UninstallResult(dry_run=not apply, remove_source=remove_source)
    if external_actions:
        _stop_services(paths, result)
        _stop_mcp_processes(paths, result)
    _rewrite_configs(paths, result)

    skills_root = paths.codex_root / "skills"
    for name in OWNED_SKILLS:
        target = skills_root / name
        if _owned_link(target, paths.source):
            _remove_path(target, result)
        elif target.exists() or target.is_symlink():
            result.skipped.append(f"foreign skill path preserved: {target}")

    for launcher in (
        paths.home / ".local" / "bin" / "fulcrum",
        paths.home / ".local" / "bin" / "fulcrum-recover",
        paths.codex_root / "bin" / "fulcrum",
        paths.codex_root / "hooks" / "fulcrum-hook",
    ):
        if _owned_link(launcher, paths.source):
            _remove_path(launcher, result)
        elif launcher.exists() or launcher.is_symlink():
            result.skipped.append(f"foreign executable preserved: {launcher}")

    for plist in (paths.home / "Library" / "LaunchAgents").glob("dev.fulcrum.*.plist"):
        _remove_path(plist, result)

    for temporary in (
        paths.home / ".codex" / "automations" / "fulcrum-marshal-check",
        paths.home / "Library" / "Application Support" / "Fulcrum Incidents",
        paths.home / "Library" / "Caches" / "fulcrum",
        paths.home / "Library" / "Logs" / "Fulcrum",
        paths.home / ".cache" / "fulcrum",
        paths.home / ".fulcrum",
    ):
        _remove_path(temporary, result)
    for root in (Path("/private/tmp/beads-circuit"), paths.codex_root / "tmp"):
        if root.is_dir():
            for temporary in root.glob("*fulcrum*"):
                _remove_path(temporary, result)

    _remove_path(paths.instance, result)
    if paths.config.exists() and not _inside(paths.config, paths.brain):
        _remove_path(paths.config, result)
    _remove_path(paths.brain, result)
    if remove_source:
        _remove_path(paths.source, result)
    else:
        _remove_path(paths.source / ".venv", result)
    return result


def _parser() -> argparse.ArgumentParser:
    home = Path.home()
    parser = argparse.ArgumentParser(
        description="Remove Fulcrum-owned services, state, links, and Codex configuration."
    )
    parser.add_argument("--yes", action="store_true", help="apply the removal")
    parser.add_argument(
        "--remove-source",
        action="store_true",
        help="also delete the canonical source checkout after cleanup",
    )
    parser.add_argument("--home", type=Path, default=home)
    parser.add_argument("--source", type=Path, default=home / "fulcrum")
    parser.add_argument(
        "--instance",
        type=Path,
        default=home / "Library" / "Application Support" / "Fulcrum",
    )
    parser.add_argument("--config", type=Path, default=home / "brain" / "fulcrum.yaml")
    parser.add_argument("--brain", type=Path, default=home / "brain")
    parser.add_argument("--codex-root", type=Path, default=home / ".codex")
    parser.add_argument("--json", action="store_true")
    return parser


def main(arguments: list[str] | None = None) -> int:
    parser = _parser()
    options = parser.parse_args(arguments)
    paths = UninstallPaths(
        home=options.home,
        source=options.source,
        instance=options.instance,
        config=options.config,
        brain=options.brain,
        codex_root=options.codex_root,
    )
    try:
        result = uninstall(
            paths,
            apply=options.yes,
            remove_source=options.remove_source,
        )
    except ValueError as error:
        parser.error(str(error))
    payload = result.as_dict()
    if options.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        verb = "removed" if options.yes else "would remove"
        for item in result.removed if options.yes else result.planned:
            print(f"{verb}: {item}")
        for item in result.skipped:
            print(f"preserved: {item}", file=sys.stderr)
        for item in result.errors:
            print(f"error: {item}", file=sys.stderr)
        if not options.yes:
            print("dry run only; pass --yes to apply", file=sys.stderr)
    return 0 if not result.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
