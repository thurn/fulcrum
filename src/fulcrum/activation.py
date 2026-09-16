"""Published-source activation without restarting the connection owner.

Preparation never pauses serving. Selection is one atomic source/interpreter
pair, and existing processes retain concrete paths until they finish.
"""

from __future__ import annotations

import asyncio
import io
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import time
import tomllib
import uuid
import venv
from typing import Any

from fulcrum.bootstrap import selection
from fulcrum.coordination import ProcessLock

RESIDENT_INPUTS = (
    "src/fulcrum/resident.py",
    "src/fulcrum/transport.py",
    "src/fulcrum/bootstrap.py",
)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    temporary.write_text(json.dumps(value))
    os.replace(temporary, path)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


def requirements(source: Path) -> tuple[Any, Any]:
    project = tomllib.loads((source / "pyproject.toml").read_text())["project"]
    return project.get("requires-python"), project.get("dependencies", [])


def snapshot(repo: Path, commit: str, destination: Path) -> None:
    archive = subprocess.run(
        ["git", "-C", str(repo), "archive", commit],
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout
    destination.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        bundle.extractall(destination, filter="data")
    # Source is immutable by contract and by mode; leases live beside its files.
    for path in destination.rglob("*"):
        if path.is_file():
            path.chmod(0o555 if os.access(path, os.X_OK) else 0o444)


def prepare_python(
    source: Path, instance: Path, previous: dict[str, Any] | None
) -> str:
    if previous is not None and requirements(source) == requirements(
        Path(previous["source"])
    ):
        return previous["python"]
    if previous is None:
        # Setup supplies its independent, already provisioned interpreter.
        installed = instance / "runtime/current/bin/python"
        return (
            str(installed.parent.parent.resolve() / "bin/python")
            if installed.exists()
            else sys.executable
        )
    environment = instance / "environments" / uuid.uuid4().hex
    venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / "bin/python"
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            *requirements(source)[1],
        ],
        check=True,
        capture_output=True,
        timeout=180,
    )
    return str(python)


def preflight(candidate: dict[str, Any], config: Path) -> None:
    code = """import sys, pathlib
sys.path.insert(0, sys.argv[1])
from fulcrum.application import default_application
from fulcrum.configuration import ConfigurationManager
manager = ConfigurationManager(pathlib.Path(sys.argv[2]))
document, _ = manager.load()
manager.validate_document(document)
default_application()
root = pathlib.Path(sys.argv[1]) / 'fulcrum'
assert (root / 'formulas').is_dir() and (root / 'role_fallbacks').is_dir()
"""
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env.pop("PYTHONPATH", None)
    subprocess.run(
        [
            candidate["python"],
            "-I",
            "-B",
            "-c",
            code,
            str(Path(candidate["source"]) / "src"),
            str(config),
        ],
        env=env,
        check=True,
        capture_output=True,
        timeout=10,
    )


def activate(
    instance: Path,
    config: Path,
    source_config: dict[str, Any],
    *,
    maintenance: bool = False,
    retry: bool = False,
) -> dict[str, Any]:
    started = time.monotonic()
    status_path = instance / "activation.json"
    with ProcessLock(instance / "update.lock", blocking=False):
        previous = selection(instance)
        status: dict[str, Any] = {
            "selected": previous,
            "state": "checking",
            "timings": {},
        }
        try:
            repo = Path(source_config["repository"])
            remote, branch = source_config["remote"], source_config["branch"]
            # Fetch only committed remote state. Dirty files and local branches
            # cannot affect either candidate bytes or eligibility.
            ref = "refs/fulcrum/published"
            git(repo, "fetch", "--no-tags", remote, f"+refs/heads/{branch}:{ref}")
            commit = git(repo, "rev-parse", ref)
            status["observed_commit"] = commit
            status["timings"]["discovery"] = time.monotonic() - started
            if previous is not None and previous["commit"] == commit:
                status["state"] = "current"
                write_json(status_path, status)
                return status
            cached = json.loads(status_path.read_text()) if status_path.exists() else {}
            if (
                cached.get("observed_commit") == commit
                and cached.get("state") in {"maintenance_required", "rejected"}
                and not maintenance
                and not retry
            ):
                return cached
            local_started = time.monotonic()
            root = instance / "sources" / uuid.uuid4().hex
            snapshot(repo, commit, root)
            status["timings"]["snapshot"] = time.monotonic() - local_started
            python = prepare_python(root, instance, previous)
            candidate = {"source": str(root), "python": python, "commit": commit}
            status["candidate"] = candidate
            preflight_started = time.monotonic()
            preflight(candidate, config)
            status["timings"]["preflight"] = time.monotonic() - preflight_started
            if previous is not None:
                old = Path(previous["source"])
                changed = [
                    name
                    for name in RESIDENT_INPUTS
                    if not (old / name).exists()
                    or (old / name).read_bytes() != (root / name).read_bytes()
                ]
                if changed:
                    status.update(
                        state="maintenance_required",
                        reason="resident machinery changed",
                        changed=changed,
                    )
                    if maintenance:
                        perform_handoff(instance, config, candidate)
                        status.update(state="activated", selected=candidate)
                    write_json(status_path, status)
                    return status
                migration = root / "src/fulcrum/state_upgrade.py"
                old_migration = old / "src/fulcrum/state_upgrade.py"
                if migration.exists() and (
                    not old_migration.exists()
                    or migration.read_bytes() != old_migration.read_bytes()
                ):
                    status.update(
                        state="maintenance_required",
                        reason="explicit state migration required",
                    )
                    if maintenance:
                        perform_migration(instance, config, candidate)
                        select_candidate(instance, candidate)
                        status.update(state="activated", selected=candidate)
                    write_json(status_path, status)
                    return status
            # Recheck local observation after preparation; never select a stale
            # candidate over a newer notification or another activator.
            if git(repo, "rev-parse", ref) != commit:
                status["state"] = "superseded"
            else:
                select_candidate(instance, candidate)
                status.update(state="activated", selected=candidate)
                status["timings"]["local_activation"] = time.monotonic() - local_started
                try:
                    from fulcrum.resident_client import exchange

                    asyncio.run(
                        exchange(instance / "resident.sock", {"action": "wake"})
                    )
                except Exception:
                    pass
            write_json(status_path, status)
            cleanup_sources(
                instance, candidate if status["state"] == "activated" else previous
            )
            return status
        except Exception as error:
            status.update(state="rejected", reason=str(error))
            write_json(status_path, status)
            raise


def _cleanup_sources(instance: Path, selected: dict[str, Any] | None) -> None:
    """Called under activation.lock; a live source lease forbids deletion."""
    for root in (instance / "sources").glob("*"):
        if not root.is_dir() or (selected and str(root) == selected["source"]):
            continue
        fd = os.open(root / ".in-use", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            shutil.rmtree(root)
        finally:
            os.close(fd)


def perform_migration(instance: Path, config: Path, candidate: dict[str, Any]) -> None:
    from fulcrum.configuration import ConfigurationManager

    manager = ConfigurationManager(config)
    document, _ = manager.load()
    root = Path(manager.effective(document)["brain"]["root"])
    fence = root / ".fulcrum-locks/migration.json"
    write_json(fence, {"candidate": candidate, "state": "waiting"})
    # Nonblocking: never kill an operation or hold activation waiting on a turn.
    with ProcessLock(root / ".fulcrum-locks/maintenance", blocking=False):
        write_json(fence, {"candidate": candidate, "state": "running"})
        code = "import sys; sys.path.insert(0, sys.argv[1]); from fulcrum.state_upgrade import migrate; migrate(sys.argv[2])"
        subprocess.run(
            [
                candidate["python"],
                "-B",
                "-c",
                code,
                str(Path(candidate["source"]) / "src"),
                str(config),
            ],
            check=True,
            timeout=60,
        )
        previous = selection(instance)
        if previous:
            (Path(previous["source"]) / ".retired-for-state").touch()
        select_candidate(instance, candidate)
        fence.unlink()


def perform_handoff(instance: Path, config: Path, candidate: dict[str, Any]) -> None:
    """Explicit resident maintenance; refusal leaves the original host running."""
    from fulcrum.bootstrap import launch_arguments
    from fulcrum.contracts import ActorContext, ParsedRequest
    from fulcrum.instance import resolve_instance
    from fulcrum.installation_service import ServiceService

    context = resolve_instance(instance=str(instance), config=str(config))
    request = ParsedRequest(
        command=("service", "stop"),
        arguments={},
        input={},
        actor=ActorContext(kind="human"),
        instance=context,
        request_id=str(uuid.uuid4()),
    )
    stopped = ServiceService().stop(request)
    if not stopped.ok:
        raise RuntimeError("resident did not reach a safe handoff boundary")
    select_candidate(instance, candidate)
    command = launch_arguments(
        candidate,
        "fulcrum.cli",
        [
            "service",
            "start",
            "--instance",
            str(instance),
            "--config",
            str(config),
            "--json",
        ],
    )
    subprocess.run(command, check=True, capture_output=True, timeout=60)


def select_candidate(instance: Path, candidate: dict[str, Any]) -> None:
    # The build lock and selection lock MUST stay distinct. Dependency setup
    # may take minutes; fresh commands can pin the old selection throughout it.
    with ProcessLock(instance / "activation.lock"):
        write_json(instance / "selected.json", candidate)


def cleanup_sources(instance: Path, selected: dict[str, Any] | None) -> None:
    with ProcessLock(instance / "activation.lock"):
        _cleanup_sources(instance, selected)
