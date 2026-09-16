"""Stable stdlib-only launcher. Never import application code before selection.

Each invocation pins a concrete source directory, including delayed imports and
assets. A moving sys.path symlink would silently mix code across an operation.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import sys
from typing import Any


def instance_from_args(args: list[str]) -> Path:
    for i, arg in enumerate(args):
        if arg == "--instance" and i + 1 < len(args):
            return Path(args[i + 1])
        if arg.startswith("--instance="):
            return Path(arg.split("=", 1)[1])
    return Path(
        os.environ.get(
            "FULCRUM_INSTANCE", str(Path.home() / "Library/Application Support/Fulcrum")
        )
    )


def selection(instance: Path) -> dict[str, Any] | None:
    path = instance / "selected.json"
    return json.loads(path.read_text()) if path.exists() else None


def pin(source: Path) -> int:
    fd = os.open(source / ".in-use", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_SH)
    os.set_inheritable(fd, True)
    return fd


def launch_arguments(
    selected: dict[str, Any], module: str, args: list[str]
) -> list[str]:
    code = (
        "import sys; sys.path.insert(0, sys.argv.pop(1)); from "
        + module
        + " import main; raise SystemExit(main())"
    )
    return [
        selected["python"],
        "-B",
        "-c",
        code,
        str(Path(selected["source"]) / "src"),
        *args,
    ]


def main() -> int:
    instance = instance_from_args(sys.argv[1:])
    selected, fd = pinned_selection(instance)
    if selected is None:
        from fulcrum.cli import main as cli_main

        return cli_main()
    # Retaining the descriptor across exec protects this snapshot until exit.
    assert fd is not None
    env = dict(
        os.environ,
        FULCRUM_SOURCE=selected["source"],
        FULCRUM_COMMIT=selected["commit"],
        FULCRUM_SOURCE_FD=str(fd),
        PYTHONDONTWRITEBYTECODE="1",
    )
    env.pop("PYTHONPATH", None)
    argv = launch_arguments(selected, "fulcrum.cli", sys.argv[1:])
    os.execve(argv[0], argv, env)
    return 1


def pinned_selection(instance: Path) -> tuple[dict[str, Any] | None, int | None]:
    """Selection and pin acquisition are indivisible with respect to cleanup."""
    instance.mkdir(parents=True, exist_ok=True)
    fd = os.open(instance / "activation.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        selected = selection(instance)
        lease = pin(Path(selected["source"])) if selected else None
        return selected, lease
    finally:
        os.close(fd)
