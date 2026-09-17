"""Local-master source preparation without restarting the connection owner.

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

CONNECTION_OWNER_INPUTS = ("src/fulcrum/broker.py",)


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
        # The launcher already has provisioned dependencies. Never install a
        # second application package just to obtain an interpreter.
        return sys.executable
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
if pathlib.Path(sys.argv[2]).exists():
    document, _ = manager.load()
    manager.validate_document(document)
default_application()
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
    with ProcessLock(instance / "update.lock"):
        previous = selection(instance)
        prior_status = (
            json.loads(status_path.read_text()) if status_path.exists() else {}
        )
        status: dict[str, Any] = {
            "selected": previous,
            "state": "checking",
            "timings": {},
            "last_activation": prior_status.get("last_activation"),
            "broker_maintenance": prior_status.get("broker_maintenance"),
        }
        try:
            repo = Path(source_config["repository"])
            # A local commit is sufficient. No network or installation gate may
            # stand between committing a fix and the next command using it.
            ref = "refs/heads/master"
            commit = git(repo, "rev-parse", ref)
            status["observed_commit"] = commit
            status["timings"]["discovery"] = time.monotonic() - started
            if previous is not None and previous["commit"] == commit:
                if maintenance and prior_status.get("broker_maintenance"):
                    perform_handoff(instance, config, previous)
                    status["broker_maintenance"] = None
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
                raise RuntimeError(
                    cached.get("reason", "local master requires maintenance")
                )
            local_started = time.monotonic()
            root = instance / "sources" / uuid.uuid4().hex
            snapshot(repo, commit, root)
            status["timings"]["snapshot"] = time.monotonic() - local_started
            python = prepare_python(root, instance, previous)
            candidate = {"source": str(root), "python": python, "commit": commit}
            status["candidate"] = candidate
            preflight_started = time.monotonic()
            preflight(candidate, config)
            status["config_schema_verified"] = True
            status["timings"]["preflight"] = time.monotonic() - preflight_started
            if previous is not None:
                old = Path(previous["source"])
                changed = [
                    name
                    for name in CONNECTION_OWNER_INPUTS
                    if not (old / name).exists()
                    or (old / name).read_bytes() != (root / name).read_bytes()
                ]
                if changed:
                    status["broker_maintenance"] = {
                        "changed": changed,
                        "reason": "connection owner retains its running code until safe handoff",
                    }
                if changed:
                    status.update(
                        state="maintenance_required",
                        reason="broker handoff is required for connection-owner changes",
                        changed=changed,
                    )
                    if maintenance:
                        perform_handoff(instance, config, candidate)
                        status.update(state="activated", selected=candidate)
                    write_json(status_path, status)
                    if not maintenance:
                        raise RuntimeError(status["reason"])
                    return status
            # Recheck local observation after preparation; never select a stale
            # candidate over a newer notification or another activator.
            if git(repo, "rev-parse", ref) != commit:
                status["state"] = "superseded"
            else:
                selection_started = time.monotonic()
                select_candidate(instance, candidate, config)
                status["timings"]["selection"] = time.monotonic() - selection_started
                status.update(state="activated", selected=candidate)
                status["timings"]["local_activation"] = time.monotonic() - local_started
                status["last_activation"] = {
                    "commit": commit,
                    "timings": dict(status["timings"]),
                }
                try:
                    from fulcrum.broker import broker_request

                    asyncio.run(
                        broker_request(instance / "broker.sock", {"type": "signal"})
                    )
                except Exception:
                    pass
            write_json(status_path, status)
            cleanup_sources(
                instance, candidate if status["state"] == "activated" else previous
            )
            return status
        except Exception as error:
            reason = str(error)
            if isinstance(error, subprocess.CalledProcessError):
                reason += ": " + str(error.stderr or error.stdout or "").strip()
            status.update(state="rejected", reason=reason)
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


def perform_handoff(instance: Path, config: Path, candidate: dict[str, Any]) -> None:
    """Explicit broker maintenance; refusal leaves the original host running."""
    from fulcrum.bootstrap import launch_arguments
    from fulcrum.contracts import ActorContext, ParsedRequest
    from fulcrum.instance import resolve_instance
    from fulcrum.desktop_services import ServiceService

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
        raise RuntimeError("broker did not reach a safe handoff boundary")
    select_candidate(instance, candidate, config)
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


def select_candidate(
    instance: Path, candidate: dict[str, Any], config: Path | None = None
) -> None:
    # The build lock and selection lock MUST stay distinct. Dependency setup
    # may take minutes; fresh commands can pin the old selection throughout it.
    with ProcessLock(instance / "activation.lock"):
        from fulcrum.install import reconcile_fulcrum2_skills

        # Skills are live source, not selected deployment assets. Their direct
        # master links must never follow an activation snapshot.
        production = (
            instance.resolve()
            == (Path.home() / "Library/Application Support/Fulcrum").resolve()
        )
        reconcile_fulcrum2_skills(instance, production=production, config_path=config)
        write_json(instance / "selected.json", candidate)


def cleanup_sources(instance: Path, selected: dict[str, Any] | None) -> None:
    with ProcessLock(instance / "activation.lock"):
        _cleanup_sources(instance, selected)


def main() -> int:
    """Internal preparation entry point, invoked by every stale fresh launcher."""
    try:
        result = activate(
            Path(sys.argv[1]),
            Path(sys.argv[2]),
            {"repository": sys.argv[3], "remote": "origin", "branch": "master"},
            retry=Path(sys.argv[2]).name == ".recovery-source-preflight",
        )
        return 0 if result["state"] in {"current", "activated", "superseded"} else 1
    except Exception as error:
        print(str(error), file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
