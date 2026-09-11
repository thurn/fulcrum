"""Thin diagnostics and idempotent setup for the shared Beads brain."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, TypedDict, cast

COMMAND_TIMEOUT_SECONDS = 15


class BrainError(RuntimeError):
    """The configured brain is missing or does not match its contract."""


class BrainStatus(TypedDict):
    brain_root: str
    beads_root: str
    database_path: str
    database: str
    host: str
    git_remote: str
    dolt_remote: str
    server: dict[str, Any]
    connectivity: dict[str, Any]


def _run(command: list[str], *, cwd: Path | None = None) -> str:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BrainError(f"could not run {command[0]!r}: {error}") from error
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no output"
        raise BrainError(
            f"command failed ({completed.returncode}): {' '.join(command)}: {detail}"
        )
    return completed.stdout


def _json_command(command: list[str]) -> Any:
    output = _run(command)
    try:
        return json.loads(output)
    except json.JSONDecodeError as error:
        raise BrainError(
            f"command returned invalid JSON: {' '.join(command)}: {error}"
        ) from error


def _canonical_remote(remote: str) -> str:
    """Normalize the equivalent Git and Dolt spelling of an SSH remote."""

    if remote.startswith("git@") and ":" in remote:
        authority, path = remote.split(":", 1)
        return f"ssh://{authority}/{path}"
    if remote.startswith("git+ssh://"):
        return "ssh://" + remote.removeprefix("git+ssh://")
    return remote.removesuffix("/")


def _require_expected_remote(actual: str, expected: str, label: str) -> None:
    if _canonical_remote(actual) != _canonical_remote(expected):
        raise BrainError(
            f"{label} is {actual!r}; expected the configured private remote "
            f"{expected!r}"
        )


def _metadata(brain_root: Path) -> dict[str, Any]:
    path = brain_root / ".beads" / "metadata.json"
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BrainError(f"could not read Beads metadata at {path}: {error}") from error
    if not isinstance(value, dict):
        raise BrainError(f"Beads metadata at {path} must be a JSON object")
    return cast(dict[str, Any], value)


def brain_status(brain_root: Path, expected_remote: str) -> BrainStatus:
    """Verify server mode, private remotes, and Beads-managed connectivity."""

    root = brain_root.resolve(strict=False)
    if not root.is_dir() or not (root / ".git").exists():
        raise BrainError(f"brain is not a Git checkout: {root}")

    git_remote = _run(["git", "-C", str(root), "remote", "get-url", "origin"]).strip()
    _require_expected_remote(git_remote, expected_remote, "Git origin")

    metadata = _metadata(root)
    if metadata.get("backend") != "dolt" or metadata.get("dolt_mode") != "server":
        raise BrainError("brain is not configured for Beads Dolt server mode")
    host = metadata.get("dolt_server_host")
    if host != "127.0.0.1":
        raise BrainError(f"Beads server must use loopback 127.0.0.1, got {host!r}")
    database = metadata.get("dolt_database")
    if not isinstance(database, str) or not database:
        raise BrainError("Beads metadata does not declare a Dolt database")

    prefix = ["bd", "--directory", str(root)]
    location = _json_command([*prefix, "where", "--json"])
    remotes = _json_command([*prefix, "dolt", "remote", "list", "--json"])
    server = _json_command([*prefix, "dolt", "status", "--json"])
    connectivity = _json_command([*prefix, "dolt", "test", "--json"])
    if not isinstance(location, dict):
        raise BrainError("bd where returned an unexpected response")
    if not isinstance(remotes, list):
        raise BrainError("bd dolt remote list returned an unexpected response")
    origin = next(
        (
            item
            for item in remotes
            if isinstance(item, dict) and item.get("name") == "origin"
        ),
        None,
    )
    if not isinstance(origin, dict) or not isinstance(origin.get("url"), str):
        raise BrainError("brain has no configured Dolt origin remote")
    dolt_remote = cast(str, origin["url"])
    _require_expected_remote(dolt_remote, expected_remote, "Dolt origin")
    if not isinstance(server, dict) or server.get("running") is not True:
        raise BrainError("Beads-managed Dolt server is not running")
    if (
        not isinstance(connectivity, dict)
        or connectivity.get("connection_ok") is not True
    ):
        raise BrainError("Beads could not connect to the configured Dolt server")

    beads_root = location.get("path")
    database_path = location.get("database_path")
    if not isinstance(beads_root, str) or not isinstance(database_path, str):
        raise BrainError("bd where did not return database paths")
    return {
        "brain_root": str(root),
        "beads_root": beads_root,
        "database_path": database_path,
        "database": database,
        "host": host,
        "git_remote": git_remote,
        "dolt_remote": dolt_remote,
        "server": cast(dict[str, Any], server),
        "connectivity": cast(dict[str, Any], connectivity),
    }


def initialize_brain(brain_root: Path, expected_remote: str) -> BrainStatus:
    """Initialize a missing brain store once, then verify the existing store."""

    root = brain_root.resolve(strict=False)
    if not root.is_dir() or not (root / ".git").exists():
        raise BrainError(f"brain is not a Git checkout: {root}")
    git_remote = _run(["git", "-C", str(root), "remote", "get-url", "origin"]).strip()
    _require_expected_remote(git_remote, expected_remote, "Git origin")

    if not (root / ".beads").exists():
        name = root.name.lower().replace("_", "-")
        _run(
            [
                "bd",
                "init",
                "--server",
                "--server-host",
                "127.0.0.1",
                "--remote",
                expected_remote,
                "--non-interactive",
                "--skip-agents",
                "--skip-hooks",
                "--init-if-missing",
                "--prefix",
                name,
            ],
            cwd=root,
        )
    return brain_status(root, expected_remote)
