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
import subprocess
import time
from typing import Any
import uuid

_active_timing_span: str | None = None


class _BootstrapSpan:
    """Stdlib-only timing span; bootstrap cannot import application modules."""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        self.started = 0.0
        self.span_id: str | None = None
        self.parent_span_id: str | None = None
        self.previous: str | None = None

    def __enter__(self) -> _BootstrapSpan:
        global _active_timing_span
        if not os.environ.get("FULCRUM_TIMING_FILE"):
            return self
        self.started = time.monotonic()
        self.span_id = uuid.uuid4().hex
        self.previous = _active_timing_span
        self.parent_span_id = self.previous or os.environ.get(
            "FULCRUM_TIMING_PARENT_SPAN"
        )
        _active_timing_span = self.span_id
        return self

    def __exit__(self, exception_type: object, *_: object) -> None:
        global _active_timing_span
        if self.span_id is None:
            return
        _active_timing_span = self.previous
        row: dict[str, Any] = {
            "schema": 1,
            "trace_id": os.environ.get("FULCRUM_TRACE_ID") or f"process-{os.getpid()}",
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "pid": os.getpid(),
            "ppid": os.getppid(),
            "stage": self.stage,
            "started_monotonic": self.started,
            "duration_ms": (time.monotonic() - self.started) * 1000,
            "outcome": "error" if exception_type else "ok",
        }
        for environment, field, limit in (
            ("FULCRUM_TIMING_LABEL", "label", 128),
            ("FULCRUM_TIMING_SAMPLE", "sample", 32),
        ):
            value = os.environ.get(environment)
            if value:
                row[field] = value[:limit]
        path = os.environ.get("FULCRUM_TIMING_FILE")
        if not path:
            return
        try:
            fd = os.open(
                path,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT,
                0o600,
            )
            try:
                os.write(
                    fd,
                    (json.dumps(row, separators=(",", ":")) + "\n").encode(),
                )
            finally:
                os.close(fd)
        except OSError:
            pass


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


def source_repository(instance: Path) -> Path:
    """Every operation follows the authoritative local master checkout."""
    return Path.home() / "fulcrum"


def local_commit(repository: Path) -> str:
    with _BootstrapSpan("bootstrap.local_commit"):
        return subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "rev-parse",
                "--verify",
                "refs/heads/master^{commit}",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()


def fresh_selection(instance: Path, config: Path) -> tuple[dict[str, Any], int]:
    """Fresh work cannot silently run yesterday's selection after a failed update.

    Preparation is automatic and serialized, but never owns the connection.
    Recheck while acquiring the lease: another commit/cleanup may race preparation.
    """
    repository = source_repository(instance)
    for _ in range(8):
        commit = local_commit(repository)
        with _BootstrapSpan("bootstrap.pinned_selection"):
            selected, lease = pinned_selection(instance)
        if selected and selected["commit"] == commit:
            assert lease is not None
            return selected, lease
        if lease is not None:
            os.close(lease)
        # Only the selection mechanism comes from the checkout. Application code
        # and assets are loaded from exact committed bytes in the prepared source.
        command = launch_arguments(
            {"source": str(repository), "python": sys.executable},
            "fulcrum.activation",
            [str(instance), str(config), str(repository)],
        )
        with _BootstrapSpan("bootstrap.source_preparation"):
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=240
            )
        if completed.returncode:
            raise RuntimeError(
                f"cannot execute local master {commit}: "
                + (completed.stderr.strip() or completed.stdout.strip())
            )
    raise RuntimeError("local master kept changing during source preparation; retry")


def main(
    module: str = "fulcrum.cli",
    arguments: list[str] | None = None,
    instance: Path | None = None,
    config: Path | None = None,
) -> int:
    args = list(sys.argv[1:] if arguments is None else arguments)
    instance = instance or instance_from_args(args)
    instance = instance.expanduser()
    if not instance.is_absolute():
        return _invalid_path(args, "instance")
    if config is None:
        config = instance / "config"
        for i, arg in enumerate(args):
            if arg == "--config" and i + 1 < len(args):
                config = Path(args[i + 1])
            elif arg.startswith("--config="):
                config = Path(arg.split("=", 1)[1])
    config = config.expanduser()
    if not config.is_absolute():
        return _invalid_path(args, "config")
    try:
        # These diagnostics/repair commands must remain reachable when preparation
        # rejects master. Ordinary commands never take this explicit repair path.
        control = any(
            args[i : i + 2] in (["service", "status"], ["service", "update"])
            for i in range(len(args) - 1)
        )
        inherited = os.environ.get("FULCRUM_OPERATION_SOURCE")
        if inherited and module == "fulcrum.cli":
            # Synchronous child commands belong to their parent's source lease.
            # Independently scheduled workers explicitly begin a new operation.
            selected = {
                "source": inherited,
                "commit": os.environ["FULCRUM_COMMIT"],
                "python": sys.executable,
            }
            with _BootstrapSpan("bootstrap.inherited_pin"):
                fd = pin(Path(inherited))
        else:
            try:
                with _BootstrapSpan("bootstrap.source_selection"):
                    selected, fd = fresh_selection(instance, config)
            except (
                OSError,
                ValueError,
                RuntimeError,
                subprocess.SubprocessError,
            ) as error:
                if not control:
                    raise
                retained, retained_fd = pinned_selection(instance)
                if retained is None or retained_fd is None:
                    raise
                print(
                    f"Local master unavailable; using retained diagnostics: {error}",
                    file=sys.stderr,
                )
                selected, fd = retained, retained_fd
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Fulcrum source unavailable: {error}", file=sys.stderr)
        return 1
    # Retaining the descriptor across exec protects this snapshot until exit.
    env = dict(
        os.environ,
        FULCRUM_SOURCE=selected["source"],
        FULCRUM_OPERATION_SOURCE=selected["source"],
        FULCRUM_COMMIT=selected["commit"],
        FULCRUM_SOURCE_FD=str(fd),
        PYTHONDONTWRITEBYTECODE="1",
    )
    env.pop("PYTHONPATH", None)
    argv = launch_arguments(selected, module, args)
    os.execve(argv[0], argv, env)
    return 1


def _invalid_path(args: list[str], field: str) -> int:
    message = f"{field} must be an absolute path"
    if "--json" in args:
        print(
            json.dumps(
                {
                    "ok": False,
                    "state": "failed",
                    "operation_id": None,
                    "request_id": None,
                    "result": None,
                    "warnings": [],
                    "error": {
                        "code": "INVALID_PATH",
                        "message": message,
                        "retryable": False,
                        "next_command": None,
                        "details": {"field": field},
                    },
                },
                separators=(",", ":"),
            )
        )
    else:
        print(f"INVALID_PATH: {message}", file=sys.stderr)
    return 2


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


if __name__ == "__main__":
    raise SystemExit(main())
