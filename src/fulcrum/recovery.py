"""Checkout-independent emergency Operative launcher.

Setup copies this module, the rest of the control-plane package, and every runtime
wheel payload into a private artifact.  The installed entry point therefore never
adds the configured checkout or its editable environment to ``sys.path``.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import select
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from fulcrum.config import RuntimePaths, load_installation, resolve_paths
from fulcrum.controller import Controller, INHERITED_LOCK_FD_ENV
from fulcrum.install import (
    CONTROLLER_LABEL,
    install_control_plane,
    install_recovery_artifact,
    inspect_service,
)
from fulcrum.operative import (
    authority_gate,
    journal_is_unfinished,
    read_journal,
    transition_journal,
    write_journal,
)
from fulcrum.runtime import CodexRuntime
from fulcrum.store import Store, StoreError, utc_now

MAX_INPUT_BYTES = 1_000_000
FINALIZER_READY_FD_ENV = "FULCRUM_OPERATIVE_FINALIZER_READY_FD"
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {"authorization", "password", "secret", "token", "api_key", "access_key"}
)


class RecoveryError(RuntimeError):
    pass


class _ControllerFenceHandle:
    """Track whether the shared flock description was handed to a child."""

    def __init__(self, handle: Any) -> None:
        self.handle = handle
        self.transferred = False

    def fileno(self) -> int:
        return int(self.handle.fileno())

    def transfer(self) -> None:
        self.transferred = True


def _redacted(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                "<redacted>" if str(key).lower() in SENSITIVE_KEYS else _redacted(child)
            )
            for key, child in list(value.items())[:200]
        }
    if isinstance(value, list):
        return [_redacted(item) for item in value[:200]]
    if isinstance(value, str) and len(value) > 16_384:
        return value[:16_384] + f"… <{len(value) - 16_384} bytes omitted>"
    return value


def _thread_id() -> str | None:
    for name in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_TASK_ID"):
        value = os.environ.get(name)
        if value:
            return value
    return None


def _read_input(path_value: str | None) -> tuple[dict[str, Any], str | None]:
    if path_value is None:
        return {}, None
    supplied = Path(path_value)
    if not supplied.is_absolute():
        raise RecoveryError("recovery --input must be an absolute path")
    status = supplied.lstat()
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
        raise RecoveryError("recovery --input must be a regular non-symlink file")
    descriptor = os.open(supplied, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        raw = handle.read(MAX_INPUT_BYTES + 1)
        after = os.fstat(handle.fileno())
    identity = lambda value: (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
    if identity(before) != identity(after):
        raise RecoveryError("recovery --input changed while being read")
    if len(raw) > MAX_INPUT_BYTES:
        raise RecoveryError("recovery --input exceeds the retained artifact bound")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RecoveryError(
            f"recovery --input must contain UTF-8 JSON: {error}"
        ) from error
    if not isinstance(value, dict):
        raise RecoveryError("recovery --input must contain a JSON object")
    return value, str(supplied.resolve(strict=True))


def _database_health(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"coverage": "unavailable", "state": "missing", "path": str(path)}
    try:
        connection = sqlite3.connect(
            f"file:{path}?mode=ro", uri=True, isolation_level=None, timeout=2
        )
        try:
            quick = connection.execute("PRAGMA quick_check").fetchone()
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        finally:
            connection.close()
    except (OSError, sqlite3.DatabaseError) as error:
        return {
            "coverage": "observed",
            "state": "corrupt",
            "path": str(path),
            "error": str(error),
        }
    quick_value = str(quick[0]) if quick else "unavailable"
    required = {"meta", "tasks", "actions", "operative_takeovers"}
    healthy = quick_value == "ok" and required.issubset(tables)
    return {
        "coverage": "observed",
        "state": "healthy" if healthy else "corrupt",
        "path": str(path),
        "quick_check": quick_value,
        "required_tables": sorted(required & tables),
    }


def _service_value(observation: Any) -> dict[str, Any]:
    return {
        "loaded": observation.loaded,
        "running": observation.running,
        "state": observation.state,
        "pid": observation.pid,
        "program_arguments": list(observation.program_arguments),
        "working_directory": observation.working_directory,
        "detail": observation.detail,
    }


def _controller_owned(paths: RuntimePaths, observation: Any) -> bool:
    expected = str(paths.control_root / "runtime" / "current")
    return bool(
        observation.loaded
        and (
            observation.working_directory == str(paths.control_root)
            or any(expected in argument for argument in observation.program_arguments)
        )
    )


def _git_probe(root: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"path": str(root), "coverage": "unavailable"}
    if not root.is_dir():
        result["reason"] = "source root is absent"
        return result
    values: dict[str, Any] = {}
    for name, arguments in (
        ("head", ("rev-parse", "HEAD")),
        ("branch", ("branch", "--show-current")),
        ("status", ("status", "--porcelain=v1", "--untracked-files=normal")),
        ("worktrees", ("worktree", "list", "--porcelain")),
    ):
        try:
            completed = subprocess.run(
                ["git", *arguments],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as error:
            result["reason"] = str(error)
            return result
        if completed.returncode != 0:
            result["reason"] = completed.stderr.strip() or f"git {name} failed"
            return result
        values[name] = completed.stdout.strip()
    return {
        "path": str(root),
        "coverage": "observed",
        "head": values["head"],
        "branch": values["branch"] or None,
        "dirty": bool(values["status"]),
        "changes": values["status"].splitlines()[:200],
        "worktrees": [
            line.removeprefix("worktree ")
            for line in values["worktrees"].splitlines()
            if line.startswith("worktree ")
        ],
    }


def probe(paths: RuntimePaths) -> dict[str, Any]:
    """Observe recovery facts without creating a directory or opening SQLite RW."""

    try:
        journal = read_journal(paths.operative_journal)
        journal_result: dict[str, Any] = {
            "coverage": "observed",
            "value": journal,
        }
    except Exception as error:
        journal_result = {"coverage": "unavailable", "error": str(error)}
    try:
        controller = _service_value(inspect_service(CONTROLLER_LABEL))
    except Exception as error:
        controller = {"coverage": "unavailable", "error": str(error)}
    config_result: dict[str, Any]
    try:
        config = load_installation(paths.config_file)
        config_result = {
            "coverage": "observed" if paths.config_file.is_file() else "unavailable",
            "path": str(paths.config_file),
            "source_root": config.source_root,
            "state_root": config.state_root,
            "app_server_endpoint": config.app_server_endpoint,
        }
        source = _git_probe(Path(config.source_root))
    except Exception as error:
        config_result = {"coverage": "unavailable", "error": str(error)}
        source = {"coverage": "unavailable", "reason": "configuration unavailable"}
    return {
        "ok": True,
        "mode": "read_only_probe",
        "thread_identity": (
            {"coverage": "observed", "native_thread_id": _thread_id()}
            if _thread_id()
            else {"coverage": "unavailable", "reason": "no environment thread identity"}
        ),
        "config": config_result,
        "journal": journal_result,
        "database": _database_health(paths.database),
        "controller": controller,
        "source": source,
        "recovery_launcher": {
            "coverage": "observed",
            "path": str(paths.recovery_launcher),
            "executable": os.access(paths.recovery_launcher, os.X_OK),
            "python": sys.executable,
            "imports_from_configured_checkout": any(
                isinstance(config_result.get("source_root"), str)
                and Path(entry)
                .resolve(strict=False)
                .is_relative_to(
                    Path(str(config_result["source_root"])).resolve(strict=False)
                )
                for entry in sys.path
                if entry
            ),
        },
    }


@contextmanager
def _controller_fence(paths: RuntimePaths, *, stop_controller: bool) -> Iterator[Any]:
    paths.control_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = paths.lock.open("a+")
    fence = _ControllerFenceHandle(handle)
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if not stop_controller:
                raise RecoveryError(
                    "controller lock is owned; use the healthy controller"
                )
            observation = inspect_service(CONTROLLER_LABEL)
            if not _controller_owned(paths, observation):
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        time.sleep(0.05)
                else:
                    raise RecoveryError(
                        "controller lock is owned but configured launchd ownership was not observed"
                    )
                yield fence
                return
            domain = f"gui/{os.getuid()}"
            stopped = subprocess.run(
                ["launchctl", "bootout", f"{domain}/{CONTROLLER_LABEL}"],
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )
            if stopped.returncode != 0:
                raise RecoveryError(
                    "could not boot out the observed controller: "
                    + (stopped.stderr.strip() or stopped.stdout.strip())
                )
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield fence
    finally:
        if not fence.transferred:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _quarantine_store(paths: RuntimePaths, *, reason: str) -> dict[str, Any]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    target = paths.operative_quarantine / f"sqlite-{stamp}-{uuid.uuid4()}"
    target.mkdir(parents=True, mode=0o700)
    copied: list[dict[str, Any]] = []
    for source in (
        paths.database,
        Path(str(paths.database) + "-wal"),
        Path(str(paths.database) + "-shm"),
    ):
        if not source.is_file():
            continue
        destination = target / source.name
        shutil.copy2(source, destination)
        descriptor = os.open(destination, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        copied.append(
            {
                "source": str(source),
                "copy": str(destination),
                "bytes": destination.stat().st_size,
            }
        )
    manifest = {
        "reason": reason,
        "created_at": utc_now(),
        "database": _database_health(paths.database),
        "copies": copied,
    }
    temporary = target / ".manifest.tmp"
    temporary.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, target / "manifest.json")
    directory = os.open(target, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return {"coverage": "observed", "path": str(target), **manifest}


async def _verified_runtime(
    config: Any, thread_id: str
) -> tuple[CodexRuntime | None, dict[str, Any]]:
    runtime = CodexRuntime(config.app_server_endpoint)
    try:
        await runtime.connect()
        thread = await runtime.read_thread(thread_id)
        if thread.get("id") != thread_id:
            raise RecoveryError(
                "App Server returned a different native thread identity"
            )
        return runtime, {
            "coverage": "observed",
            "method": "exact App Server thread/read",
            "native_thread_id": thread_id,
            "managed_agent": False,
        }
    except Exception as error:
        await runtime.close()
        return None, {
            "coverage": "unavailable",
            "method": "provisional environment thread identity",
            "native_thread_id": thread_id,
            "reason": str(error),
            "capabilities": ["filesystem", "git", "service_checks"],
        }


async def _with_controller(
    paths: RuntimePaths,
    config: Any,
    lock_handle: Any,
    runtime: CodexRuntime,
    operation: Any,
) -> dict[str, Any]:
    inherited = os.dup(lock_handle.fileno())
    os.set_inheritable(inherited, True)
    os.environ[INHERITED_LOCK_FD_ENV] = str(inherited)
    controller: Controller | None = None
    try:
        controller = Controller(paths, config)
        await controller.runtime.close()
        controller.runtime = runtime
        return await operation(controller)
    finally:
        os.environ.pop(INHERITED_LOCK_FD_ENV, None)
        if controller is not None:
            await controller.runtime.close()
            controller.store.close()
            if controller.lock_handle is not None:
                controller.lock_handle.close()
                controller.lock_handle = None
        else:
            os.close(inherited)


async def _run_closeout_finalizer(
    paths: RuntimePaths, takeover_id: str
) -> dict[str, Any]:
    """Own the controller lock until the exact fallback closeout is terminal."""

    ready_value = os.environ.pop(FINALIZER_READY_FD_ENV, None)
    ready_fd = int(ready_value) if ready_value is not None else None
    controller: Controller | None = None
    controller_task: asyncio.Task[None] | None = None
    ready_sent = False
    try:
        config = load_installation(paths.config_file)
        controller = Controller(paths, config)
        await controller.runtime.connect()
        journal = controller.operative_journal
        if (
            journal is None
            or journal.get("takeover_id") != takeover_id
            or journal.get("state") != "closing"
        ):
            raise RecoveryError(
                "closeout finalizer did not observe the exact closing takeover"
            )
        thread = await controller.runtime.read_thread(str(journal["native_thread_id"]))
        if thread.get("id") != journal["native_thread_id"]:
            raise RecoveryError(
                "closeout finalizer could not verify the exact Operative thread"
            )
        controller_task = asyncio.create_task(
            controller.start(), name="operative-closeout-finalizer"
        )
        for _attempt in range(200):
            if controller_task.done():
                await controller_task
                raise RecoveryError(
                    "closeout finalizer controller stopped during startup"
                )
            worker_rows = controller.store.rows(
                "SELECT worker_name, state FROM worker_heartbeats"
            )
            running_workers = {
                str(item["worker_name"])
                for item in worker_rows
                if item["state"] == "running"
            }
            controller_state = controller.store.row(
                "SELECT value FROM meta WHERE key = 'controller_state'"
            )
            if (
                controller.server is not None
                and controller.runtime.ready
                and controller.critical_workers.issubset(running_workers)
                and controller_state is not None
                and controller_state["value"] == "operative_only"
            ):
                break
            await asyncio.sleep(0.05)
        else:
            raise RecoveryError("closeout finalizer controller did not become ready")
        if ready_fd is not None:
            os.write(ready_fd, b"ready\n")
            os.close(ready_fd)
            ready_fd = None
        ready_sent = True
        while True:
            if controller_task.done():
                await controller_task
                raise RecoveryError(
                    "closeout finalizer controller stopped unexpectedly"
                )
            current = read_journal(paths.operative_journal)
            if current is None or current.get("takeover_id") != takeover_id:
                raise RecoveryError("closeout finalizer lost the exact takeover")
            if current.get("state") != "closing":
                controller.stop_event.set()
                await controller_task
                return {
                    "ok": current.get("state") == "closed",
                    "state": current.get("state"),
                    "takeover_id": takeover_id,
                }
            controller.advance_requested.set()
            await asyncio.sleep(0.25)
    except Exception as error:
        if ready_fd is not None:
            try:
                os.write(ready_fd, f"error:{error}\n".encode("utf-8")[:4096])
            finally:
                os.close(ready_fd)
        if ready_sent:
            raise
        return {"ok": False, "state": "startup_failed", "error": str(error)}
    finally:
        if controller is not None:
            if controller_task is not None and not controller_task.done():
                controller.stop_event.set()
                await asyncio.gather(controller_task, return_exceptions=True)
            else:
                await controller.runtime.close()
                controller.store.close()
                if controller.lock_handle is not None:
                    controller.lock_handle.close()
                    controller.lock_handle = None
    raise RecoveryError("closeout finalizer terminated without a retained result")


def _spawn_closeout_finalizer(
    paths: RuntimePaths, lock_handle: Any, takeover_id: str
) -> dict[str, Any]:
    """Hand the already-held controller lock to a detached durable process."""

    paths.logs_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_path = paths.logs_root / f"operative-finalizer-{takeover_id}.log"
    lock_fd = os.dup(lock_handle.fileno())
    os.set_inheritable(lock_fd, True)
    ready_read, ready_write = os.pipe()
    os.set_inheritable(ready_write, True)
    environment = dict(os.environ)
    environment.update(
        {
            INHERITED_LOCK_FD_ENV: str(lock_fd),
            FINALIZER_READY_FD_ENV: str(ready_write),
            "FULCRUM_CONFIG": str(paths.config_file),
            "FULCRUM_STATE_ROOT": str(paths.state_root),
            "FULCRUM_CONTROL_ROOT": str(paths.control_root),
        }
    )
    process: subprocess.Popen[Any] | None = None
    try:
        with log_path.open("ab", buffering=0) as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-m",
                    "fulcrum.recovery",
                    "--finalize-takeover",
                    takeover_id,
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                env=environment,
                pass_fds=(lock_fd, ready_write),
                start_new_session=True,
                close_fds=True,
            )
        os.close(ready_write)
        ready_write = -1
        readable, _, _ = select.select([ready_read], [], [], 15)
        detail = (
            os.read(ready_read, 4096).decode("utf-8", errors="replace").strip()
            if readable
            else ""
        )
        if not readable or detail != "ready" or process.poll() is not None:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            raise RecoveryError(
                "durable closeout finalizer did not accept lock ownership: "
                + (detail or "no readiness acknowledgement")
            )
        return {
            "coverage": "observed",
            "kind": "detached_recovery_finalizer",
            "pid": process.pid,
            "takeover_id": takeover_id,
            "log": str(log_path),
        }
    finally:
        os.close(lock_fd)
        os.close(ready_read)
        if ready_write >= 0:
            os.close(ready_write)


def _prior_dispatch(path: Path) -> bool:
    if _database_health(path).get("state") != "healthy":
        return False
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT value FROM meta WHERE key = 'dispatch_enabled'"
        ).fetchone()
        return bool(row and row[0] == "1")
    finally:
        connection.close()


def _assert_not_managed(path: Path, thread_id: str) -> None:
    if _database_health(path).get("state") != "healthy":
        return
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT role FROM tasks WHERE native_thread_id = ?", (thread_id,)
        ).fetchone()
    finally:
        connection.close()
    if row is not None and row[0] != "operative":
        raise RecoveryError("managed agents cannot register as Operative")


async def acquire(
    paths: RuntimePaths, supplied: dict[str, Any], input_path: str | None
) -> dict[str, Any]:
    thread_id = _thread_id()
    if thread_id is None:
        result = probe(paths)
        result.update(
            {
                "ok": False,
                "state": "read_only",
                "condition": "no current thread identity; authority was not invented",
            }
        )
        return result
    description = supplied.get("description")
    if not isinstance(description, str) or not description.strip():
        raise RecoveryError("operative registration requires a nonempty description")
    config = load_installation(paths.config_file)
    with _controller_fence(paths, stop_controller=True) as lock_handle:
        with authority_gate(paths.authority_lock, blocking=False):
            health = _database_health(paths.database)
            _assert_not_managed(paths.database, thread_id)
            journal = read_journal(paths.operative_journal)
            if journal_is_unfinished(journal):
                assert journal is not None
                if journal["native_thread_id"] != thread_id:
                    raise RecoveryError(
                        "operative takeover active for a different thread"
                    )
                if journal["state"] == "aborted":
                    raise RecoveryError(
                        "aborted takeover requires explicit human recovery"
                    )
            else:
                timestamp = utc_now()
                journal = {
                    "takeover_id": str(uuid.uuid4()),
                    "state": "acquiring",
                    "native_thread_id": thread_id,
                    "superseded_thread_ids": [],
                    "scope": description.strip(),
                    "prior_dispatch_enabled": _prior_dispatch(paths.database),
                    "completed_effects": [
                        "authority_fence_written",
                        "controller_service_disabled",
                    ],
                    "next_step": "verify caller and reconcile journal into SQLite",
                    "caller_verification": {
                        "coverage": "unavailable",
                        "method": "provisional environment thread identity",
                        "native_thread_id": thread_id,
                    },
                    "created_at": timestamp,
                    "updated_at": timestamp,
                }
                write_journal(paths.operative_journal, journal)
        quarantine = None
        if health["state"] == "corrupt":
            retained_quarantine = journal.get("store_quarantine")
            if (
                isinstance(retained_quarantine, dict)
                and isinstance(retained_quarantine.get("path"), str)
                and Path(retained_quarantine["path"]).is_dir()
            ):
                quarantine = retained_quarantine
            else:
                quarantine = _quarantine_store(
                    paths, reason="corrupt SQLite observed during Operative acquisition"
                )
                journal = dict(journal)
                journal["store_quarantine"] = quarantine
                effects = list(journal["completed_effects"])
                if "corrupt_store_quarantined" not in effects:
                    effects.append("corrupt_store_quarantined")
                journal["completed_effects"] = effects
                journal["updated_at"] = utc_now()
                write_journal(paths.operative_journal, journal)
        runtime, verification = await _verified_runtime(config, thread_id)
        journal = dict(journal)
        journal["caller_verification"] = verification
        journal["updated_at"] = utc_now()
        write_journal(paths.operative_journal, journal)
        if runtime is None or health["state"] != "healthy":
            return {
                "ok": True,
                "takeover_id": journal["takeover_id"],
                "state": "acquiring",
                "reused": "sqlite_authority_mirrored" in journal["completed_effects"],
                "authority": "provisional_local_repair",
                "caller_verification": verification,
                "database": health,
                "quarantine": quarantine,
                "next_step": "repair service/store, then run reconcile",
            }

        verified_thread_id: str = thread_id
        verified_description: str = description

        async def register(controller: Controller) -> dict[str, Any]:
            return await controller._register_operative(
                {
                    "thread_id": verified_thread_id,
                    "description": verified_description.strip(),
                    "model": str(supplied.get("model") or "gpt-6-astra"),
                    "effort": str(supplied.get("effort") or "high"),
                    "input_path": input_path,
                }
            )

        return await _with_controller(paths, config, lock_handle, runtime, register)


def _require_bound_journal(paths: RuntimePaths) -> dict[str, Any]:
    thread_id = _thread_id()
    if thread_id is None:
        raise RecoveryError("current thread identity is unavailable")
    journal = read_journal(paths.operative_journal)
    if not journal_is_unfinished(journal) or journal is None:
        raise RecoveryError("no unfinished operative takeover is available")
    if journal["native_thread_id"] != thread_id:
        raise RecoveryError("current thread is not the bound Operative")
    return journal


def _offline_dossier(paths: RuntimePaths) -> dict[str, Any]:
    journal = _require_bound_journal(paths)
    config = load_installation(paths.config_file)
    projects = [_git_probe(Path(project.repo_path)) for project in config.projects]
    result: dict[str, Any] = {
        "takeover": {"coverage": "observed", "value": journal},
        "database": _database_health(paths.database),
        "source_and_worktrees": {"coverage": "observed", "items": projects},
        "controller": probe(paths)["controller"],
        "app_server": {
            "coverage": "unavailable",
            "reason": "offline dossier does not infer App Server connectivity",
        },
        "workflow_state": {
            "coverage": "unavailable",
            "reason": "SQLite state is missing or unavailable",
        },
    }
    if result["database"]["state"] == "healthy":
        try:
            with Store(paths.database, readonly=True) as store:
                result["workflow_state"] = {
                    "coverage": "observed",
                    "status": store.status(event_limit=20),
                }
        except Exception as error:
            result["workflow_state"] = {"coverage": "unavailable", "reason": str(error)}
    return result


def _record_offline_operation(
    paths: RuntimePaths,
    journal: dict[str, Any],
    *,
    correlation_id: str,
    kind: str,
    target: str,
    before: dict[str, Any],
    result: dict[str, Any],
    after: dict[str, Any],
    evidence: str | None,
) -> dict[str, Any]:
    current = read_journal(paths.operative_journal)
    if current is not None:
        if current["takeover_id"] != journal["takeover_id"]:
            raise RecoveryError("operative takeover changed while retaining operation")
        journal = current
    operations = list(journal.get("offline_operations") or [])
    retained = next(
        (item for item in operations if item.get("correlation_id") == correlation_id),
        None,
    )
    if retained is not None:
        return retained
    retained = {
        "correlation_id": correlation_id,
        "state": "complete",
        "intent": {"kind": kind, "target": target},
        "kind": kind,
        "target": target,
        "before": _redacted(before),
        "observed_result": _redacted(result),
        "after": _redacted(after),
        "evidence": evidence,
        "created_at": utc_now(),
    }
    operations.append(retained)
    journal = dict(journal)
    journal["offline_operations"] = operations
    journal["updated_at"] = utc_now()
    write_journal(paths.operative_journal, journal)
    return retained


def _begin_offline_operation(
    paths: RuntimePaths,
    journal: dict[str, Any],
    *,
    correlation_id: str,
    kind: str,
    target: str,
    before: dict[str, Any],
    evidence: str | None,
    request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fsync one exact intent before the corresponding mutation boundary."""

    operations = list(journal.get("offline_operations") or [])
    retained = next(
        (item for item in operations if item.get("correlation_id") == correlation_id),
        None,
    )
    if retained is not None:
        return retained
    timestamp = utc_now()
    retained = {
        "correlation_id": correlation_id,
        "state": "intent",
        "intent": {"kind": kind, "target": target},
        "kind": kind,
        "target": target,
        "before": _redacted(before),
        "observed_result": {},
        "after": {"coverage": "unavailable"},
        "evidence": evidence,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    if request is not None:
        retained["request"] = request
    operations.append(retained)
    updated = dict(journal)
    updated["offline_operations"] = operations
    updated["updated_at"] = timestamp
    write_journal(paths.operative_journal, updated)
    return retained


def _transition_offline_operation(
    paths: RuntimePaths,
    correlation_id: str,
    *,
    state: str,
    before: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Durably transition an existing journal operation without recreating it."""

    if state not in {"sent", "complete", "failed", "uncertain"}:
        raise RecoveryError(f"invalid offline operation state {state!r}")
    journal = read_journal(paths.operative_journal)
    if journal is None:
        raise RecoveryError("operative journal disappeared during operation")
    operations = list(journal.get("offline_operations") or [])
    retained: dict[str, Any] | None = None
    for index, item in enumerate(operations):
        if item.get("correlation_id") != correlation_id:
            continue
        source = str(item.get("state"))
        if source == state:
            return item
        if source == "intent" and state not in {"sent", "failed"}:
            raise RecoveryError(
                f"invalid offline operation transition {source} -> {state}"
            )
        if source in {"sent", "uncertain"} and state not in {
            "complete",
            "failed",
            "uncertain",
        }:
            raise RecoveryError(
                f"invalid offline operation transition {source} -> {state}"
            )
        if source in {"complete", "failed"}:
            return item
        retained = dict(item)
        retained["state"] = state
        retained["updated_at"] = utc_now()
        if before is not None:
            retained["before"] = _redacted(before)
        if result is not None:
            retained["observed_result"] = _redacted(result)
        if after is not None:
            retained["after"] = _redacted(after)
        operations[index] = retained
        break
    if retained is None:
        raise RecoveryError("offline operation intent is unavailable")
    updated = dict(journal)
    updated["offline_operations"] = operations
    updated["updated_at"] = utc_now()
    write_journal(paths.operative_journal, updated)
    return retained


def _worktree_control(
    paths: RuntimePaths, supplied: dict[str, Any], input_path: str | None
) -> dict[str, Any]:
    journal = _require_bound_journal(paths)
    action = supplied.get("action")
    operation_key = supplied.get("operation_key")
    path_value = supplied.get("path")
    if action not in {"adopt", "release"}:
        raise RecoveryError("worktree action must be adopt or release")
    if not isinstance(operation_key, str) or not operation_key:
        raise RecoveryError("worktree operation_key is required")
    if not isinstance(path_value, str) or not Path(path_value).is_absolute():
        raise RecoveryError("worktree path must be exact and absolute")
    target = Path(path_value).resolve(strict=True)
    config = load_installation(paths.config_file)
    observations = [_git_probe(Path(project.repo_path)) for project in config.projects]
    all_paths = {
        Path(value).resolve(strict=True)
        for observed in observations
        for value in observed.get("worktrees", [])
    }
    if target not in all_paths:
        raise RecoveryError("path is not an enrolled repository worktree")
    before = _git_probe(target)
    adopted = dict(journal.get("adopted_worktrees") or {})
    if action == "release" and str(target) not in adopted:
        raise RecoveryError("worktree is not adopted by this takeover")
    if action == "adopt":
        adopted[str(target)] = {"before": before, "adopted_at": utc_now()}
    else:
        adopted.pop(str(target), None)
    updated = dict(journal)
    updated["adopted_worktrees"] = adopted
    updated["updated_at"] = utc_now()
    write_journal(paths.operative_journal, updated)
    correlation = (
        f"offline-worktree:{journal['takeover_id']}:{operation_key}:{action}:{target}"
    )
    retained = _record_offline_operation(
        paths,
        updated,
        correlation_id=correlation,
        kind=f"worktree_{action}",
        target=str(target),
        before=before,
        result={"accepted": True},
        after=_git_probe(target),
        evidence=input_path,
    )
    return {"ok": True, "operation": retained, "adopted_worktrees": adopted}


def _repair_check(paths: RuntimePaths) -> dict[str, Any]:
    journal = _require_bound_journal(paths)
    config = load_installation(paths.config_file)
    source = Path(config.source_root)
    syntax_errors: list[dict[str, str]] = []
    for path in (source / "src" / "fulcrum").rglob("*.py"):
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except (OSError, SyntaxError) as error:
            syntax_errors.append({"path": str(path), "error": str(error)})
    return {
        "ok": not syntax_errors,
        "takeover_id": journal["takeover_id"],
        "source": _git_probe(source),
        "syntax_errors": syntax_errors,
        "editable_environment_used": False,
        "recovery_import_root": str(Path(__file__).resolve().parents[1]),
    }


async def _controller_control(
    paths: RuntimePaths,
    command: str,
    supplied: dict[str, Any],
    input_path: str | None,
) -> dict[str, Any]:
    journal = _require_bound_journal(paths)
    config = load_installation(paths.config_file)
    if _database_health(paths.database)["state"] != "healthy":
        raise RecoveryError("controller-backed control requires a healthy SQLite store")
    runtime, verification = await _verified_runtime(
        config, str(journal["native_thread_id"])
    )
    if runtime is None:
        raise RecoveryError(
            "controller-backed control requires exact App Server caller verification: "
            + str(verification.get("reason"))
        )
    bound_journal: dict[str, Any] = journal
    with _controller_fence(paths, stop_controller=True) as acquired_lock:
        lock_handle: Any = acquired_lock

        async def invoke(controller: Controller) -> dict[str, Any]:
            if bound_journal["state"] == "acquiring":
                await controller._resume_operative_acquisition(supplied)
            payload: dict[str, Any] = {
                "command": f"operative_{command.replace('-', '_')}",
                "thread_id": bound_journal["native_thread_id"],
                "input": supplied,
                "input_path": input_path,
            }
            if command in {"finish", "abort", "recover", "register"}:
                payload.update(supplied)
            result = await controller.handle_request(payload)
            if command != "finish" or result.get("state") != "closing":
                return result
            try:
                finalizer = _spawn_closeout_finalizer(
                    paths, lock_handle, str(bound_journal["takeover_id"])
                )
            except Exception as error:
                current = read_journal(paths.operative_journal)
                if current is not None and current.get("state") == "closing":
                    active = transition_journal(
                        paths.operative_journal,
                        current,
                        "active",
                        now=utc_now(),
                        next_step="retry closeout after durable finalizer handoff is available",
                        closeout_failures=[str(error)],
                    )
                    controller.operative_journal = active
                    controller.store.mirror_operative_journal(active)
                raise RecoveryError(
                    "fallback closeout was not accepted because durable finalizer ownership could not be verified: "
                    + str(error)
                ) from error
            lock_handle.transfer()
            current = read_journal(paths.operative_journal)
            if current is None or current.get("state") != "closing":
                raise RecoveryError(
                    "fallback closeout finalizer changed state before handoff was retained"
                )
            updated = dict(current)
            updated["closeout_finalizer"] = finalizer
            updated["updated_at"] = utc_now()
            write_journal(paths.operative_journal, updated)
            controller.operative_journal = updated
            controller.store.mirror_operative_journal(updated)
            result["finalizer"] = finalizer
            return result

        return await _with_controller(paths, config, lock_handle, runtime, invoke)


async def _reconcile(
    paths: RuntimePaths, supplied: dict[str, Any], input_path: str | None
) -> dict[str, Any]:
    journal = _require_bound_journal(paths)
    config = load_installation(paths.config_file)
    health = _database_health(paths.database)
    reconstructed = False
    quarantine = None
    if health["state"] == "corrupt":
        with _controller_fence(paths, stop_controller=True):
            retained = journal.get("store_quarantine")
            if (
                isinstance(retained, dict)
                and isinstance(retained.get("path"), str)
                and Path(retained["path"]).is_dir()
            ):
                quarantine = retained
            else:
                quarantine = _quarantine_store(
                    paths,
                    reason="explicit Operative reconciliation found corrupt SQLite",
                )
                journal = dict(journal)
                journal["store_quarantine"] = quarantine
                journal["updated_at"] = utc_now()
                write_journal(paths.operative_journal, journal)
        if (
            supplied.get("confirm")
            != "reconstruct store from authoritative operative journal"
        ):
            return {
                "ok": False,
                "state": journal["state"],
                "database": health,
                "quarantine": quarantine,
                "condition": "supply the exact reconstruction confirmation; the corrupt original was not replaced",
            }
        quarantine_root = Path(str(quarantine["path"]))
        with _controller_fence(paths, stop_controller=True):
            for source in (
                paths.database,
                Path(str(paths.database) + "-wal"),
                Path(str(paths.database) + "-shm"),
            ):
                if source.exists():
                    os.replace(source, quarantine_root / f"displaced-{source.name}")
        reconstructed = True
        health = _database_health(paths.database)
    if health["state"] == "healthy" and supplied.get("target_kind") is not None:
        return await _controller_control(paths, "reconcile", supplied, input_path)
    runtime, verification = await _verified_runtime(
        config, str(journal["native_thread_id"])
    )
    if runtime is None:
        raise RecoveryError("App Server identity is not yet verifiable")
    bound_journal: dict[str, Any] = journal
    reconstructed_for_result: bool = reconstructed
    health_state_for_result: str = str(health["state"])
    quarantine_for_result: dict[str, Any] | None = quarantine
    with _controller_fence(paths, stop_controller=True) as lock_handle:
        current = read_journal(paths.operative_journal)
        if (
            current is None
            or current.get("takeover_id") != journal["takeover_id"]
            or current.get("native_thread_id") != journal["native_thread_id"]
        ):
            await runtime.close()
            raise RecoveryError(
                "operative takeover changed after exact App Server verification"
            )
        observed_verification = dict(verification)
        if (
            observed_verification.get("coverage") != "observed"
            or observed_verification.get("native_thread_id")
            != journal["native_thread_id"]
        ):
            await runtime.close()
            raise RecoveryError(
                "exact App Server verification returned contradictory evidence"
            )
        effects = list(current.get("completed_effects") or [])
        if "exact_caller_verified" not in effects:
            effects.append("exact_caller_verified")
        bound_journal = dict(current)
        bound_journal["caller_verification"] = observed_verification
        bound_journal["completed_effects"] = effects
        bound_journal["updated_at"] = utc_now()
        write_journal(paths.operative_journal, bound_journal)

        async def reconcile_controller(controller: Controller) -> dict[str, Any]:
            if (
                controller.operative_journal
                and controller.operative_journal["state"] == "acquiring"
            ):
                result = await controller._resume_operative_acquisition(
                    {
                        "thread_id": bound_journal["native_thread_id"],
                        "description": bound_journal["scope"],
                        "model": "gpt-6-astra",
                        "effort": "high",
                    }
                )
            else:
                await controller._reconcile_operative_mode()
                result = controller._operative_status(
                    {"thread_id": bound_journal["native_thread_id"]}
                )
            result["dossier"] = await controller._build_operative_dossier()
            result["store_reconstructed_from_journal"] = bool(
                reconstructed_for_result or health_state_for_result == "missing"
            )
            if quarantine_for_result is not None:
                result["store_quarantine"] = quarantine_for_result
            return result

        return await _with_controller(
            paths, config, lock_handle, runtime, reconcile_controller
        )


def _abort_offline(
    paths: RuntimePaths, supplied: dict[str, Any], input_path: str | None
) -> dict[str, Any]:
    journal = _require_bound_journal(paths)
    reason = supplied.get("reason")
    evidence = supplied.get("evidence")
    if not isinstance(reason, str) or not reason.strip():
        raise RecoveryError("abort requires an explicit human reason")
    if (
        not isinstance(evidence, str)
        or not Path(evidence).is_absolute()
        or not Path(evidence).is_file()
    ):
        raise RecoveryError("abort requires an absolute readable evidence file")
    if journal["state"] != "active":
        if journal["state"] == "aborted":
            return {"ok": False, "state": "aborted", "reused": True}
        raise RecoveryError(f"cannot abort a takeover in {journal['state']}")
    aborted = transition_journal(
        paths.operative_journal,
        journal,
        "aborted",
        now=utc_now(),
        next_step="explicit human recovery of this same takeover",
        completed_effect="human_abort_recorded",
        aborted_at=utc_now(),
        abort={"reason": reason.strip(), "evidence": str(Path(evidence).resolve())},
    )
    _record_offline_operation(
        paths,
        aborted,
        correlation_id=f"offline-abort:{journal['takeover_id']}",
        kind="human_abort",
        target=str(journal["takeover_id"]),
        before=journal,
        result={"success": False, "reason": reason.strip()},
        after=aborted,
        evidence=input_path,
    )
    return {"ok": False, "state": "aborted", "takeover_id": journal["takeover_id"]}


def _recover_offline(
    paths: RuntimePaths, supplied: dict[str, Any], input_path: str | None
) -> dict[str, Any]:
    thread_id = _thread_id()
    if thread_id is None:
        raise RecoveryError("human recovery requires a current thread identity")
    journal = read_journal(paths.operative_journal)
    if journal is None or journal.get("state") != "aborted":
        raise RecoveryError("no aborted operative takeover is available")
    if supplied.get("takeover_id") != journal["takeover_id"]:
        raise RecoveryError("human recovery requires the exact takeover ID")
    evidence = supplied.get("evidence")
    if (
        not isinstance(evidence, str)
        or not Path(evidence).is_absolute()
        or not Path(evidence).is_file()
    ):
        raise RecoveryError(
            "human recovery requires an absolute readable evidence file"
        )
    superseded = list(journal.get("superseded_thread_ids") or [])
    if (
        journal["native_thread_id"] != thread_id
        and journal["native_thread_id"] not in superseded
    ):
        superseded.append(str(journal["native_thread_id"]))
    acquiring = transition_journal(
        paths.operative_journal,
        journal,
        "acquiring",
        now=utc_now(),
        next_step="verify successor and reconcile the same takeover",
        completed_effect="human_successor_recovery_recorded",
        native_thread_id=thread_id,
        superseded_thread_ids=superseded,
        caller_verification={
            "coverage": "unavailable",
            "method": "provisional successor environment identity",
            "native_thread_id": thread_id,
            "evidence": str(Path(evidence).resolve()),
        },
        operative_identity=None,
        operative_action=None,
    )
    _record_offline_operation(
        paths,
        acquiring,
        correlation_id=f"offline-recover:{journal['takeover_id']}:{thread_id}",
        kind="human_recovery",
        target=str(journal["takeover_id"]),
        before=journal,
        result={"successor_thread_id": thread_id},
        after=acquiring,
        evidence=input_path,
    )
    return {
        "ok": True,
        "state": "acquiring",
        "takeover_id": journal["takeover_id"],
        "thread_id": thread_id,
        "superseded_thread_ids": superseded,
    }


def _reinstall(
    paths: RuntimePaths, supplied: dict[str, Any], input_path: str | None
) -> dict[str, Any]:
    journal = _require_bound_journal(paths)
    if supplied.get("confirm") != "reinstall retained recovery and control plane":
        raise RecoveryError("reinstall requires the exact confirmation phrase")
    if (
        not isinstance(supplied.get("operation_key"), str)
        or not supplied["operation_key"]
    ):
        raise RecoveryError("reinstall requires a stable operation_key")
    config = load_installation(paths.config_file)
    check = _repair_check(paths)
    if not check["ok"]:
        raise RecoveryError(
            "source syntax is broken; the retained recovery artifact was preserved"
        )
    correlation = (
        f"offline-reinstall:{journal['takeover_id']}:" f"{supplied['operation_key']}"
    )
    existing = next(
        (
            item
            for item in journal.get("offline_operations", [])
            if item.get("correlation_id") == correlation
        ),
        None,
    )
    if existing is not None:
        return {"ok": True, "reused": True, "operation": existing}
    before = probe(paths)
    with _controller_fence(paths, stop_controller=True):
        recovery, recovery_updated = install_recovery_artifact(config, paths)
        control, control_updated = install_control_plane(config, paths)
    after = probe(paths)
    retained = _record_offline_operation(
        paths,
        read_journal(paths.operative_journal) or journal,
        correlation_id=correlation,
        kind="reinstall",
        target=str(paths.control_root),
        before=before,
        result={
            "recovery_updated": recovery_updated,
            "control_plane_updated": control_updated,
        },
        after=after,
        evidence=input_path,
    )
    return {
        "ok": True,
        "recovery_launcher": str(recovery),
        "control_plane": str(control),
        "operation": retained,
    }


async def _require_verified_active_operative(
    paths: RuntimePaths,
) -> dict[str, Any]:
    """Reverify the exact caller and its durable task/action binding."""

    journal = _require_bound_journal(paths)
    if journal["state"] != "active":
        raise RecoveryError(
            "durable workflow repair requires an active verified Operative"
        )
    caller = journal.get("caller_verification")
    if (
        not isinstance(caller, dict)
        or caller.get("coverage") != "observed"
        or caller.get("native_thread_id") != journal["native_thread_id"]
        or not isinstance(journal.get("operative_identity"), dict)
        or not isinstance(journal.get("operative_action"), dict)
        or not isinstance(journal.get("task_id"), int)
        or not isinstance(journal.get("action_id"), int)
    ):
        raise RecoveryError(
            "durable workflow repair requires a bound verified Operative; run reconcile after exact App Server verification"
        )
    config = load_installation(paths.config_file)
    runtime, verification = await _verified_runtime(
        config, str(journal["native_thread_id"])
    )
    if runtime is None:
        raise RecoveryError(
            "durable workflow repair requires exact App Server caller verification: "
            + str(verification.get("reason"))
        )
    await runtime.close()
    with Store(paths.database, readonly=True) as store:
        binding = store.row(
            """SELECT t.id AS task_id, a.id AS action_id
               FROM tasks t JOIN actions a ON a.task_id = t.id
               WHERE t.id = ? AND a.id = ? AND t.native_thread_id = ?
                 AND t.role = 'operative' AND a.kind = 'operative'
                 AND a.state IN ('pending','starting','active','terminal','uncertain')""",
            (
                journal["task_id"],
                journal["action_id"],
                journal["native_thread_id"],
            ),
        )
    if binding is None:
        raise RecoveryError(
            "durable workflow repair requires the journal's exact live Operative task/action binding"
        )
    return journal


def _store_repair_boundary(_phase: str) -> None:
    """Test seam for process-crash boundaries; production intentionally does nothing."""


def _store_repair(
    paths: RuntimePaths,
    supplied: dict[str, Any],
    input_path: str | None,
    *,
    journal: dict[str, Any],
) -> dict[str, Any]:
    """Apply an exact, backed-up transaction only to an otherwise readable store."""

    if _database_health(paths.database)["state"] != "healthy":
        raise RecoveryError(
            "direct repair cannot mutate a missing/corrupt store; reconstruct a new database and retain the quarantine set"
        )
    operation_key = supplied.get("operation_key")
    reason = supplied.get("reason")
    statements = supplied.get("statements")
    observations = supplied.get("observations")
    if not isinstance(operation_key, str) or not operation_key:
        raise RecoveryError("store repair requires a stable operation_key")
    if not isinstance(reason, str) or not reason.strip():
        raise RecoveryError("store repair requires an explicit reason")
    if not isinstance(statements, list) or not statements:
        raise RecoveryError("store repair requires exact statements")
    if not isinstance(observations, list) or not observations:
        raise RecoveryError("store repair requires before/after observation queries")

    def validate_query(item: Any) -> dict[str, Any]:
        if not isinstance(item, dict) or not isinstance(item.get("sql"), str):
            raise RecoveryError("store observation must contain SQL")
        sql = item["sql"].strip()
        if not sql.upper().startswith("SELECT"):
            raise RecoveryError("store observations must be SELECT queries")
        parameters = item.get("parameters", [])
        if not isinstance(parameters, list):
            raise RecoveryError("store observation parameters must be a list")
        return {"sql": sql, "parameters": parameters}

    def validate_statement(item: Any) -> dict[str, Any]:
        if not isinstance(item, dict) or not isinstance(item.get("sql"), str):
            raise RecoveryError("store repair statement must contain SQL")
        sql = item["sql"].strip()
        verb = sql.split(None, 1)[0].upper() if sql else ""
        if verb not in {"INSERT", "UPDATE", "DELETE"}:
            raise RecoveryError("store repair permits only INSERT, UPDATE, or DELETE")
        parameters = item.get("parameters", [])
        if not isinstance(parameters, list):
            raise RecoveryError("store repair parameters must be a list")
        return {"sql": sql, "parameters": parameters}

    for statement in statements:
        validate_statement(statement)
    for observation in observations:
        validate_query(observation)
    supplied_json = json.dumps(
        {"input": supplied, "evidence": input_path},
        sort_keys=True,
        separators=(",", ":"),
    )
    repair_request = json.loads(supplied_json)
    correlation = f"offline-store-repair:{journal['takeover_id']}:{operation_key}"
    existing = next(
        (
            item
            for item in journal.get("offline_operations", [])
            if item.get("correlation_id") == correlation
        ),
        None,
    )
    if existing is not None:
        retained_request = existing.get("request")
        if not isinstance(retained_request, dict):
            raise RecoveryError(
                "retained store repair intent lacks its complete structured request"
            )
        retained_json = json.dumps(
            retained_request, sort_keys=True, separators=(",", ":")
        )
        if retained_json != supplied_json:
            raise RecoveryError(
                "operation_key is already bound to a different store repair request"
            )
        repair_request = retained_request
        if existing.get("state") in {"complete", "failed"}:
            return {
                "ok": existing.get("state") == "complete",
                "reused": True,
                "operation": existing,
            }

    retained_input = repair_request["input"]
    retained_reason = str(retained_input["reason"]).strip()
    retained_statements = retained_input["statements"]
    retained_observations = retained_input["observations"]

    def query(connection: sqlite3.Connection, item: Any) -> dict[str, Any]:
        validated = validate_query(item)
        sql = validated["sql"]
        parameters = validated["parameters"]
        cursor = connection.execute(sql, parameters)
        columns = [str(column[0]) for column in cursor.description or []]
        return {
            "sql": sql,
            "rows": [
                dict(zip(columns, row, strict=True)) for row in cursor.fetchall()[:200]
            ],
        }

    if existing is None:
        existing = _begin_offline_operation(
            paths,
            journal,
            correlation_id=correlation,
            kind="direct_store_repair",
            target=str(paths.database),
            before={
                "coverage": "intent_retained_before_database_mutation",
                "reason": retained_reason,
            },
            evidence=input_path,
            request=repair_request,
        )
        _store_repair_boundary("after_intent")

    if existing.get("state") in {"sent", "uncertain"}:
        connection = sqlite3.connect(
            f"file:{paths.database}?mode=ro", uri=True, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        try:
            observed = [query(connection, item) for item in retained_observations]
        finally:
            connection.close()
        before_queries = existing.get("before", {}).get("queries")
        unchanged = isinstance(before_queries, list) and observed == before_queries
        reconciled_state = "failed" if unchanged else "uncertain"
        retained = _transition_offline_operation(
            paths,
            correlation,
            state=reconciled_state,
            result={
                "committed": "unavailable",
                "reissued": False,
                "reason": "interrupted store repair reconciled by observation",
            },
            after={
                "queries": observed,
                "integrity": _database_health(paths.database),
            },
        )
        return {"ok": False, "reused": True, "operation": retained}

    quarantine = _quarantine_store(
        paths, reason=f"before direct repair: {retained_reason}"
    )
    copied_main = next(
        (
            Path(item["copy"])
            for item in quarantine["copies"]
            if item["source"] == str(paths.database)
        ),
        None,
    )
    if copied_main is None or _database_health(copied_main)["state"] != "healthy":
        raise RecoveryError(
            "the quarantined database copy did not verify readable and healthy"
        )
    connection = sqlite3.connect(paths.database, isolation_level=None)
    connection.row_factory = sqlite3.Row
    before: list[dict[str, Any]] = []
    after: list[dict[str, Any]] = []
    try:
        before = [query(connection, item) for item in retained_observations]
        _transition_offline_operation(
            paths,
            correlation,
            state="sent",
            before={
                "queries": before,
                "quarantine": quarantine,
                "reason": retained_reason,
            },
            result={"committed": "unavailable"},
            after={"coverage": "unavailable"},
        )
        _store_repair_boundary("after_sent")
        connection.execute("BEGIN IMMEDIATE")
        for item in retained_statements:
            validated = validate_statement(item)
            sql = validated["sql"]
            parameters = validated["parameters"]
            connection.execute(sql, parameters)
        _store_repair_boundary("during_transaction")
        quick = connection.execute("PRAGMA quick_check").fetchone()
        foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
        after = [query(connection, item) for item in retained_observations]
        if not quick or quick[0] != "ok" or foreign:
            raise RecoveryError(
                f"store repair invariant failed: quick_check={quick!r}, foreign_keys={foreign!r}"
            )
        connection.execute("COMMIT")
        _store_repair_boundary("after_commit")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
    operation = _transition_offline_operation(
        paths,
        correlation,
        state="complete",
        result={"committed": True, "reason": retained_reason},
        after={"queries": after, "integrity": _database_health(paths.database)},
    )
    return {
        "ok": True,
        "reused": False,
        "operation": operation,
        "quarantine": quarantine,
    }


def _quarantine_control(
    paths: RuntimePaths, supplied: dict[str, Any], input_path: str | None
) -> dict[str, Any]:
    journal = _require_bound_journal(paths)
    reason = supplied.get("reason")
    operation_key = supplied.get("operation_key")
    if not isinstance(reason, str) or not reason.strip():
        raise RecoveryError("quarantine requires an explicit reason")
    if not isinstance(operation_key, str) or not operation_key:
        raise RecoveryError("quarantine requires a stable operation_key")
    correlation = f"offline-store-quarantine:{journal['takeover_id']}:{operation_key}"
    existing = next(
        (
            item
            for item in journal.get("offline_operations", [])
            if item.get("correlation_id") == correlation
        ),
        None,
    )
    if existing is not None:
        return {"ok": True, "reused": True, "operation": existing}
    before = _database_health(paths.database)
    quarantine = _quarantine_store(paths, reason=reason.strip())
    operation = _record_offline_operation(
        paths,
        journal,
        correlation_id=correlation,
        kind="store_quarantine",
        target=str(paths.database),
        before=before,
        result=quarantine,
        after=_database_health(paths.database),
        evidence=input_path,
    )
    return {"ok": True, "reused": False, "operation": operation, **quarantine}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="operative-recovery")
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--finalize-takeover", help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("probe")
    commands.add_parser("status")
    commands.add_parser("dossier")
    commands.add_parser("repair-check")
    commands.add_parser("service-check")
    for name in (
        "register",
        "wind-down",
        "reconcile",
        "worktree",
        "reinstall",
        "finish",
        "abort",
        "recover",
        "quarantine",
        "store-repair",
    ):
        child = commands.add_parser(name)
        child.add_argument("--input", required=name not in {"reconcile"})
    return parser


async def _run(args: argparse.Namespace, paths: RuntimePaths) -> dict[str, Any]:
    supplied, input_path = _read_input(getattr(args, "input", None))
    command = args.command
    if command == "probe":
        return probe(paths)
    if command == "status":
        journal = _require_bound_journal(paths)
        return {
            "ok": True,
            "takeover_id": journal["takeover_id"],
            "state": journal["state"],
            "scope": journal["scope"],
            "next_step": journal["next_step"],
            "caller_verification": journal["caller_verification"],
            "dispatch_enabled": False,
        }
    if command == "dossier":
        return _offline_dossier(paths)
    if command == "register":
        return await acquire(paths, supplied, input_path)
    if command == "repair-check":
        return _repair_check(paths)
    if command == "service-check":
        _require_bound_journal(paths)
        result = probe(paths)
        return {
            "ok": True,
            "controller": result["controller"],
            "database": result["database"],
        }
    if command == "worktree":
        with _controller_fence(paths, stop_controller=True):
            return _worktree_control(paths, supplied, input_path)
    if command == "reinstall":
        return _reinstall(paths, supplied, input_path)
    if command == "reconcile":
        return await _reconcile(paths, supplied, input_path)
    if command == "quarantine":
        with _controller_fence(paths, stop_controller=True):
            return _quarantine_control(paths, supplied, input_path)
    if command == "store-repair":
        with _controller_fence(paths, stop_controller=True):
            journal = await _require_verified_active_operative(paths)
            return _store_repair(paths, supplied, input_path, journal=journal)
    if command == "abort" and _database_health(paths.database)["state"] != "healthy":
        with _controller_fence(paths, stop_controller=True):
            return _abort_offline(paths, supplied, input_path)
    if command == "recover":
        with _controller_fence(paths, stop_controller=True):
            result = _recover_offline(paths, supplied, input_path)
        try:
            reconciled = await _reconcile(paths, {}, input_path)
        except Exception as error:
            result["reconciliation"] = {"coverage": "unavailable", "reason": str(error)}
            return result
        result["reconciliation"] = reconciled
        return result
    if command in {"wind-down", "finish", "abort"}:
        return await _controller_control(paths, command, supplied, input_path)
    raise RecoveryError(f"unsupported recovery command {command!r}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = resolve_paths()
    if args.smoke_test:
        result = {
            "ok": True,
            "python": sys.executable,
            "module": str(Path(__file__).resolve()),
            "isolated": sys.flags.isolated == 1,
        }
        print(json.dumps(result, sort_keys=True))
        return 0
    if args.finalize_takeover is not None:
        try:
            result = asyncio.run(
                _run_closeout_finalizer(paths, str(args.finalize_takeover))
            )
            return 0 if result.get("ok", False) else 2
        except Exception as error:
            print(f"operative-recovery finalizer: {error}", file=sys.stderr)
            return 2
    if args.command is None:
        parser.error("a command is required")
    try:
        result = asyncio.run(_run(args, paths))
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("ok", True) else 2
    except Exception as error:
        print(f"operative-recovery: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
