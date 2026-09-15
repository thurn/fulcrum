"""Persisted deterministic external providers for disposable CLI fixtures.

The JSON file owned by these adapters contains only fake runtime, delivery, clock,
fault, crash, and barrier facts.  Fulcrum workflow authority remains in stock Beads.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import subprocess
import tempfile
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypeVar

from fulcrum.delivery import (
    DeliveryFacts,
    DeliveryProviderError,
    SourceRef,
    ValidationFacts,
    WorkRef,
    WorkspaceFacts,
)
from fulcrum.runtime import (
    AppServerError,
    ReleaseFacts,
    ResourceFacts,
    RuntimeCapabilities,
    RuntimeEvent,
    TaskFacts,
    TaskSpec,
    TurnFacts,
    TurnInput,
)

T = TypeVar("T")


def provider_path(endpoint: str) -> Path:
    value = endpoint.removeprefix("file://")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("deterministic provider endpoint must be an absolute path")
    return path.resolve(strict=False)


def initial_provider_state(
    *, models: Mapping[str, list[str]] | None = None
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "schema": 1,
        "clock": now,
        "models": dict(models or {"gpt-5.6-luna": ["low"]}),
        "runtime": {"next_task": 1, "next_turn": 1, "tasks": {}, "events": []},
        "delivery": {"next_handle": 1, "handles": {}},
        "calls": {},
        "faults": [],
        "crashes": [],
        "barriers": {},
    }


class ProviderState:
    def __init__(self, endpoint: str) -> None:
        self.path: Path = provider_path(endpoint)
        self.lock_path: Path = self.path.with_suffix(self.path.suffix + ".lock")

    def initialize(self, state: Mapping[str, Any] | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.exists():
            return
        self._replace(dict(state or initial_provider_state()))

    def read(self) -> dict[str, Any]:
        with self._locked():
            return self._read_unlocked()

    def mutate(self, action: Callable[[dict[str, Any]], T]) -> T:
        with self._locked():
            state = self._read_unlocked()
            result = action(state)
            self._replace_unlocked(state)
            return result

    def consume_fault(self, provider: str, method: str) -> Mapping[str, Any] | None:
        def consume(state: dict[str, Any]) -> Mapping[str, Any] | None:
            faults = state.setdefault("faults", [])
            key = f"{provider}.{method}"
            calls = state.setdefault("calls", {})
            occurrence = int(calls.get(key) or 0) + 1
            calls[key] = occurrence
            for item in faults:
                if item.get("provider") == provider and item.get("method") == method:
                    if not item.get("consumed") and occurrence == int(
                        item.get("occurrence") or 1
                    ):
                        item["consumed"] = True
                        return dict(item)
            return None

        return self.mutate(consume)

    def now(self) -> str:
        return str(self.read()["clock"])

    def _locked(self):  # type: ignore[no-untyped-def]
        self.lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = self.lock_path.open("a+")
        fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX)

        class Lock:
            def __enter__(self) -> None:
                return None

            def __exit__(self, *_: object) -> None:
                fcntl.flock(descriptor.fileno(), fcntl.LOCK_UN)
                descriptor.close()

        return Lock()

    def _read_unlocked(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise RuntimeError(
                f"deterministic provider state is unavailable: {self.path}"
            ) from error
        if not isinstance(value, dict) or value.get("schema") != 1:
            raise RuntimeError("deterministic provider state is invalid")
        return value

    def _replace(self, state: Mapping[str, Any]) -> None:
        with self._locked():
            self._replace_unlocked(state)

    def _replace_unlocked(self, state: Mapping[str, Any]) -> None:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(state, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)


class DeterministicClock:
    def __init__(self, endpoint: str) -> None:
        self.state = ProviderState(endpoint)

    def now(self) -> datetime:
        return _parse_time(self.state.now())

    def monotonic(self) -> float:
        return self.now().timestamp()


def trigger_crash_boundary(request: Any, target_operation: str, boundary: str) -> None:
    """Consume one armed deterministic crash after a persisted real step."""

    try:
        from fulcrum.configuration import ConfigurationManager

        manager = ConfigurationManager(request.instance.config_path)
        document, _ = manager.load()
        config = manager.effective(document)
    except Exception:
        return
    if config["runtime"].get("kind") != "deterministic":
        return
    endpoint = config["runtime"].get("endpoint")
    if not isinstance(endpoint, str) or not endpoint:
        return

    def consume(state: dict[str, Any]) -> bool:
        for item in state.get("crashes") or []:
            if (
                item.get("operation_id") == target_operation
                and item.get("boundary") == boundary
                and not item.get("consumed")
            ):
                item["consumed"] = True
                item["observed_at"] = state["clock"]
                return True
        return False

    try:
        armed = ProviderState(endpoint).mutate(consume)
    except (OSError, RuntimeError, ValueError):
        return
    if not armed:
        return
    os._exit(86)


class DeterministicRuntime:
    METHODS = (
        "task/create",
        "task/read",
        "task/start",
        "task/interrupt",
        "task/archive",
        "task/unarchive",
        "task/delete",
        "task/release",
        "task/respond",
    )

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self.state = ProviderState(endpoint)
        self.connected = False
        self.transport: DeterministicRuntime = self

    async def connect(self) -> None:
        self.state.read()
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def capabilities(self) -> RuntimeCapabilities:
        state = self.state.read()
        models = {
            str(model): tuple(str(item) for item in efforts)
            for model, efforts in dict(state.get("models") or {}).items()
        }
        return RuntimeCapabilities(True, self.endpoint, self.METHODS, models)

    async def set_name(self, thread_id: str, name: str) -> None:
        self.state.mutate(
            lambda state: _task_row(state, thread_id).update({"title": name})
        )

    async def find_projects(self, root: str) -> list[dict[str, Any]]:
        state = self.state.read()
        projects = state.get("projects") or {}
        return [
            dict(item)
            for item in projects.values()
            if isinstance(item, Mapping) and item.get("root") == root
        ]

    async def ensure_project(
        self, *, name: str, root: str, operation_id: str
    ) -> dict[str, Any]:
        def ensure(state: dict[str, Any]) -> dict[str, Any]:
            projects = state.setdefault("projects", {})
            for value in projects.values():
                if value.get("root") == root:
                    return dict(value)
            identifier = f"fixture-project-{len(projects) + 1}"
            row = {
                "id": identifier,
                "name": name,
                "root": root,
                "operation_id": operation_id,
            }
            projects[identifier] = row
            return dict(row)

        return self.state.mutate(ensure)

    async def create_task(self, spec: TaskSpec) -> TaskFacts:
        fault = self.state.consume_fault("runtime", "create_task")
        if fault and fault.get("effect") == "not_applied":
            _raise_runtime_fault(fault)

        def create(state: dict[str, Any]) -> str:
            runtime = state["runtime"]
            identifier = f"fixture-task-{int(runtime['next_task']):04d}"
            runtime["next_task"] = int(runtime["next_task"]) + 1
            runtime["tasks"][identifier] = {
                "id": identifier,
                "title": spec.title,
                "creation_cwd": spec.creation_cwd,
                "cwd": spec.cwd,
                "project_id": spec.project_id,
                "workspace_roots": list(spec.workspace_roots),
                "model": spec.model,
                "effort": spec.effort,
                "archived": False,
                "exists": True,
                "loaded": True,
                "runtime_status": "idle",
                "active_turn": None,
                "last_turn": None,
                "pending_requests": [],
                "turns": {},
                "output": [],
                "terminals": [],
                "release_count": 0,
                "archive_count": 0,
            }
            return identifier

        identifier = self.state.mutate(create)
        if fault and fault.get("effect") == "applied":
            _raise_runtime_fault(fault)
        return await self.inspect_task(identifier)

    async def find_tasks(self, creation_cwd: str) -> list[TaskFacts]:
        state = self.state.read()
        return [
            _task_facts(item, str(state["clock"]))
            for item in state["runtime"]["tasks"].values()
            if item.get("creation_cwd") == creation_cwd and item.get("exists")
        ]

    async def inspect_task(self, thread_id: str) -> TaskFacts:
        state = self.state.read()
        item = state["runtime"]["tasks"].get(thread_id)
        if item is None:
            return TaskFacts(
                id=thread_id,
                title=None,
                cwd=None,
                project_id=None,
                workspace_roots=(),
                archived=False,
                exists=False,
                loaded=False,
                runtime_status="deleted",
                active_turn=None,
                last_turn=None,
                pending_requests=(),
                observed_at=str(state["clock"]),
            )
        return _task_facts(item, str(state["clock"]))

    async def configure_task(self, thread_id: str, spec: TaskSpec) -> TaskFacts:
        def configure(state: dict[str, Any]) -> None:
            item = _task_row(state, thread_id)
            item.update(
                {
                    "title": spec.title,
                    "cwd": spec.cwd,
                    "project_id": spec.project_id,
                    "workspace_roots": list(spec.workspace_roots),
                    "model": spec.model,
                    "effort": spec.effort,
                }
            )

        self.state.mutate(configure)
        return await self.inspect_task(thread_id)

    async def start_turn(self, thread_id: str, input: TurnInput) -> TurnFacts:
        fault = self.state.consume_fault("runtime", "start_turn")
        if fault and fault.get("effect") == "not_applied":
            _raise_runtime_fault(fault)

        def start(state: dict[str, Any]) -> str:
            runtime = state["runtime"]
            task = _task_row(state, thread_id)
            if task.get("active_turn"):
                raise AppServerError(
                    "task already has an active turn", category="rejected"
                )
            turn_id = f"fixture-turn-{int(runtime['next_turn']):04d}"
            runtime["next_turn"] = int(runtime["next_turn"]) + 1
            row = {
                "id": turn_id,
                "thread_id": thread_id,
                "state": "in_progress",
                "operation_id": input.operation_id,
                "completed": False,
                "error": None,
                "tools": [],
                "usage": None,
                "input": {
                    "text": input.text,
                    "cwd": input.cwd,
                    "workspace_roots": list(input.workspace_roots),
                    "model": input.model,
                    "effort": input.effort,
                    "operation_id": input.operation_id,
                    "ownership_operation": input.ownership_operation,
                    "developer_instructions": input.developer_instructions,
                },
            }
            task["turns"][turn_id] = row
            task["active_turn"] = turn_id
            task["runtime_status"] = "active"
            task["loaded"] = True
            return turn_id

        turn_id = self.state.mutate(start)
        if fault and fault.get("effect") == "applied":
            _raise_runtime_fault(fault)
        result = await self.inspect_turn(thread_id, turn_id)
        assert result is not None
        return result

    async def find_turn(self, thread_id: str, operation_id: str) -> TurnFacts | None:
        state = self.state.read()
        item = state["runtime"]["tasks"].get(thread_id)
        if not isinstance(item, Mapping):
            return None
        for row in item.get("turns", {}).values():
            if row.get("operation_id") == operation_id:
                return _turn_facts(row, str(state["clock"]))
        return None

    async def inspect_turn(self, thread_id: str, turn_id: str) -> TurnFacts | None:
        state = self.state.read()
        item = state["runtime"]["tasks"].get(thread_id)
        row = item.get("turns", {}).get(turn_id) if isinstance(item, Mapping) else None
        return (
            _turn_facts(row, str(state["clock"])) if isinstance(row, Mapping) else None
        )

    async def interrupt(self, thread_id: str, turn_id: str) -> TurnFacts:
        def interrupt(state: dict[str, Any]) -> None:
            task = _task_row(state, thread_id)
            row = task["turns"].get(turn_id)
            if row is None:
                raise AppServerError("unknown turn", category="rejected")
            row.update(
                {
                    "state": "interrupted",
                    "completed": True,
                    "error": {"code": "interrupted"},
                }
            )
            task["active_turn"] = None
            task["last_turn"] = dict(row)
            task["runtime_status"] = "idle"

        self.state.mutate(interrupt)
        result = await self.inspect_turn(thread_id, turn_id)
        assert result is not None
        return result

    async def respond(
        self, thread_id: str, request_id: str, response: dict[str, Any]
    ) -> None:
        def respond(state: dict[str, Any]) -> None:
            task = _task_row(state, thread_id)
            pending = task["pending_requests"]
            matches = [item for item in pending if item.get("request_id") == request_id]
            if len(matches) != 1:
                raise AppServerError("unknown native request", category="rejected")
            pending.remove(matches[0])
            task.setdefault("responses", []).append(
                {"request_id": request_id, "response": response}
            )

        self.state.mutate(respond)

    async def output(
        self,
        thread_id: str,
        *,
        turn_id: str | None,
        limit: int,
        cursor: str | None,
        max_bytes: int,
    ) -> Mapping[str, Any]:
        state = self.state.read()
        task = _task_row(state, thread_id)
        rows = list(task.get("output") or [])
        if turn_id:
            rows = [item for item in rows if item.get("turn_id") == turn_id]
        offset = int(cursor or 0)
        selected = rows[offset:] if limit == 0 else rows[offset : offset + limit]
        return {
            "items": selected,
            "next_cursor": (
                str(offset + len(selected))
                if offset + len(selected) < len(rows)
                else None
            ),
            "truncated": False,
            "max_bytes": max_bytes,
        }

    async def terminals(
        self, thread_id: str, *, limit: int, cursor: str | None
    ) -> Mapping[str, Any]:
        state = self.state.read()
        rows = list(_task_row(state, thread_id).get("terminals") or [])
        offset = int(cursor or 0)
        selected = rows[offset:] if limit == 0 else rows[offset : offset + limit]
        return {
            "items": selected,
            "next_cursor": (
                str(offset + len(selected))
                if offset + len(selected) < len(rows)
                else None
            ),
        }

    async def terminate_terminal(
        self, thread_id: str, process_id: str
    ) -> Mapping[str, Any]:
        def terminate(state: dict[str, Any]) -> dict[str, Any]:
            task = _task_row(state, thread_id)
            for item in task.get("terminals") or []:
                if str(item.get("terminal_id")) == process_id:
                    item["still_running"] = False
                    return dict(item)
            raise AppServerError("unknown terminal", category="rejected")

        return self.state.mutate(terminate)

    async def release(self, thread_id: str) -> ReleaseFacts:
        def release(state: dict[str, Any]) -> list[dict[str, Any]]:
            task = _task_row(state, thread_id)
            task["loaded"] = False
            task["release_count"] = int(task.get("release_count") or 0) + 1
            return [
                dict(item)
                for item in task.get("terminals") or []
                if item.get("still_running")
            ]

        terminals = self.state.mutate(release)
        return ReleaseFacts(thread_id, "released", tuple(terminals), self.state.now())

    async def archive(self, thread_id: str) -> TaskFacts:
        def archive(state: dict[str, Any]) -> None:
            task = _task_row(state, thread_id)
            task["archived"] = True
            task["loaded"] = False
            task["archive_count"] = int(task.get("archive_count") or 0) + 1

        self.state.mutate(archive)
        return await self.inspect_task(thread_id)

    async def unarchive(self, thread_id: str) -> TaskFacts:
        def unarchive(state: dict[str, Any]) -> None:
            task = _task_row(state, thread_id)
            task["archived"] = False

        self.state.mutate(unarchive)
        return await self.inspect_task(thread_id)

    async def delete(self, thread_id: str) -> TaskFacts:
        def delete(state: dict[str, Any]) -> None:
            task = _task_row(state, thread_id)
            task.update(
                {
                    "exists": False,
                    "loaded": False,
                    "active_turn": None,
                    "runtime_status": "deleted",
                }
            )

        self.state.mutate(delete)
        return await self.inspect_task(thread_id)

    async def resources(self) -> ResourceFacts:
        state = self.state.read()
        tasks = list(state["runtime"]["tasks"].values())
        loaded = tuple(
            str(item["id"])
            for item in tasks
            if item.get("exists") and item.get("loaded")
        )
        active = sum(1 for item in tasks if item.get("active_turn"))
        return ResourceFacts(
            loaded_count=len(loaded),
            active_count=active,
            loaded_ids=loaded,
            fd_soft_limit=4096,
            fd_usage=0,
            overloaded=False,
            observed_at=str(state["clock"]),
        )

    async def events(self) -> AsyncIterator[RuntimeEvent]:
        cursor = 0
        while True:
            state = self.state.read()
            events = state["runtime"].get("events") or []
            if cursor < len(events):
                event = events[cursor]
                cursor += 1
                yield RuntimeEvent(
                    method=str(event["method"]),
                    params=dict(event.get("params") or {}),
                    request_id=event.get("request_id"),
                    observed_at=str(event["observed_at"]),
                )
                continue
            await asyncio.sleep(0.05)


class DeterministicDelivery:
    def __init__(self, endpoint: str) -> None:
        self.state = ProviderState(endpoint)

    async def prepare(self, work: WorkRef) -> WorkspaceFacts:
        fault = self.state.consume_fault("delivery", "prepare")
        if fault and fault.get("effect") == "not_applied":
            _raise_delivery_fault(fault)
        existing = await self.inspect_workspace(work)
        if not existing.exists:
            path = Path(work.intended_path).resolve(strict=False)
            path.parent.mkdir(parents=True, exist_ok=True)
            _git(
                work.project_root,
                "worktree",
                "add",
                "-b",
                work.branch,
                str(path),
                work.integration_branch,
            )
        facts = await self.inspect_workspace(work)
        if fault and fault.get("effect") == "applied":
            _raise_delivery_fault(fault)
        return facts

    async def inspect_workspace(self, work: WorkRef) -> WorkspaceFacts:
        root = Path(work.project_root).resolve(strict=True)
        path = Path(work.actual_path or work.intended_path).resolve(strict=False)
        reference = f"refs/heads/{work.branch}"
        branch_exists = _git_ok(root, "show-ref", "--verify", "--quiet", reference)
        exists = path.is_dir() and (path / ".git").exists()
        owned = exists and branch_exists
        head = _git(path, "rev-parse", "HEAD") if owned else None
        base = (
            _git(root, "rev-parse", work.integration_branch) if branch_exists else None
        )
        dirty_entries = (
            tuple(line for line in _git(path, "status", "--porcelain").splitlines())
            if owned
            else ()
        )
        return WorkspaceFacts(
            path=str(path),
            branch=work.branch,
            base_oid=base,
            head_oid=head,
            exists=exists,
            owned=owned,
            dirty=bool(dirty_entries) if owned else None,
            dirty_entries=dirty_entries,
            preparation="passed" if owned else "absent",
            ownership_evidence={
                "project_root": str(root),
                "branch_ref": reference,
                "operation_id": work.operation_id,
            },
            observed_at=self.state.now(),
        )

    async def submit(self, source: SourceRef) -> ValidationFacts:
        fault = self.state.consume_fault("delivery", "submit")
        if fault and fault.get("effect") == "not_applied":
            _raise_delivery_fault(fault)
        workspace = await self.inspect_workspace(source.work)
        if not workspace.owned or workspace.head_oid != source.oid or workspace.dirty:
            raise DeliveryProviderError(
                "submitted source is not the exact clean fixture worktree head",
                category="rejected",
                evidence=workspace.to_dict(),
            )
        if not _git_ok(
            source.work.project_root, "cat-file", "-e", f"{source.oid}^{{commit}}"
        ):
            raise DeliveryProviderError(
                "submitted source is not a commit", category="rejected"
            )

        def submit(state: dict[str, Any]) -> tuple[str, str]:
            delivery = state["delivery"]
            for handle, item in delivery["handles"].items():
                if (
                    item.get("source_oid") == source.oid
                    and item.get("bead_id") == source.work.bead_id
                ):
                    return str(handle), str(state["clock"])
            handle = f"fixture-delivery-{int(delivery['next_handle']):04d}"
            delivery["next_handle"] = int(delivery["next_handle"]) + 1
            delivery["handles"][handle] = {
                "handle": handle,
                "bead_id": source.work.bead_id,
                "source_oid": source.oid,
                "work": source.work.to_dict(),
                "validation": "pending",
                "promotion": "not_started",
                "integration_oid": None,
                "synchronization": "pending",
                "cleanup": "pending",
                "checks": [],
            }
            return handle, str(state["clock"])

        handle, observed_at = self.state.mutate(submit)
        if fault and fault.get("effect") == "applied":
            _raise_delivery_fault(fault)
        return ValidationFacts(handle, source.oid, "pending", (), {}, observed_at)

    async def inspect(self, source: SourceRef, handle: str | None) -> DeliveryFacts:
        if not handle:
            raise DeliveryProviderError(
                "delivery handle is required", category="rejected"
            )
        state = self.state.read()
        row = state["delivery"]["handles"].get(handle)
        if not isinstance(row, Mapping) or row.get("source_oid") != source.oid:
            raise DeliveryProviderError(
                "delivery handle/source mismatch", category="rejected"
            )
        return _delivery_facts(row, str(state["clock"]))

    async def promote(self, source: SourceRef, handle: str) -> DeliveryFacts:
        fault = self.state.consume_fault("delivery", "promote")
        if fault and fault.get("effect") == "not_applied":
            _raise_delivery_fault(fault)

        def promote(state: dict[str, Any]) -> None:
            row = _delivery_row(state, handle, source.oid)
            if row.get("validation") != "passed":
                raise DeliveryProviderError(
                    "fixture validation has not passed", category="rejected"
                )
            if row.get("promotion") != "promoted":
                row["promotion"] = "pending"

        self.state.mutate(promote)
        if fault and fault.get("effect") == "applied":
            self.state.mutate(
                lambda state: _promote_delivery_row(
                    _delivery_row(state, handle, source.oid)
                )
            )
            _raise_delivery_fault(fault)
        return await self.inspect(source, handle)

    async def cancel(self, source: SourceRef, handle: str) -> DeliveryFacts:
        self.state.mutate(
            lambda state: _delivery_row(state, handle, source.oid).update(
                {"promotion": "canceled"}
            )
        )
        return await self.inspect(source, handle)

    async def synchronize(self, source: SourceRef, handle: str) -> DeliveryFacts:
        state = self.state.read()
        row = state["delivery"]["handles"].get(handle)
        if not isinstance(row, Mapping):
            raise DeliveryProviderError("unknown fixture delivery", category="rejected")
        work = row["work"]
        remote = work.get("source_remote")
        integration = str(work["integration_branch"])
        local_oid = _git(str(work["project_root"]), "rev-parse", integration)
        synchronization = "not_required"
        if work.get("require_source_sync"):
            if not remote:
                raise DeliveryProviderError(
                    "source synchronization remote is unavailable", category="rejected"
                )
            remote_oid = _git(
                str(work["project_root"]),
                "ls-remote",
                str(remote),
                f"refs/heads/{integration}",
            ).split()[0]
            synchronization = "complete" if remote_oid == local_oid else "pending"
        self.state.mutate(
            lambda value: _delivery_row(value, handle, source.oid).update(
                {"synchronization": synchronization}
            )
        )
        return await self.inspect(source, handle)

    async def cleanup(self, work: WorkRef) -> WorkspaceFacts:
        facts = await self.inspect_workspace(work)
        if facts.exists:
            if facts.dirty:
                raise DeliveryProviderError(
                    "fixture worktree is dirty",
                    category="rejected",
                    evidence=facts.to_dict(),
                )
            _git(work.project_root, "worktree", "remove", str(facts.path))
        if _git_ok(
            work.project_root,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{work.branch}",
        ):
            _git(work.project_root, "branch", "-D", work.branch)
        return await self.inspect_workspace(work)


def emit_provider_event(endpoint: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    provider: str = str(payload.get("provider") or "")
    event: str = str(payload.get("event") or "")
    target: str = str(payload.get("target") or "")
    data = payload.get("data")
    facts: dict[str, Any] = dict(data) if isinstance(data, Mapping) else {}
    state_store = ProviderState(endpoint)
    if provider == "runtime":
        if event not in {
            "runtime.turn_completed",
            "runtime.turn_failed",
            "runtime.input_requested",
            "runtime.unarchived",
        }:
            raise ValueError(f"unsupported deterministic runtime event {event}")

        def runtime_event(state: dict[str, Any]) -> dict[str, Any]:
            task = _task_row(state, target)
            turn_id = str(facts.get("turn_id") or task.get("active_turn") or "")
            if event in {"runtime.turn_completed", "runtime.turn_failed"}:
                row = task["turns"].get(turn_id)
                if row is None:
                    raise ValueError("runtime terminal event has no matching turn")
                failed = event.endswith("failed")
                row.update(
                    {
                        "state": "failed" if failed else "completed",
                        "completed": True,
                        "error": facts.get("error") if failed else None,
                        "tools": list(facts.get("tools") or []),
                        "usage": facts.get("usage"),
                    }
                )
                task["active_turn"] = None
                task["last_turn"] = dict(row)
                task["runtime_status"] = "idle"
                if facts.get("output") is not None:
                    task["output"].append(
                        {"turn_id": turn_id, "text": str(facts["output"])}
                    )
            elif event == "runtime.input_requested":
                request_id = str(facts.get("request_id") or "")
                method = str(facts.get("method") or "")
                if not request_id or not method:
                    raise ValueError(
                        "input request requires data.request_id and data.method"
                    )
                task["pending_requests"].append(
                    {
                        "request_id": request_id,
                        "method": method,
                        "params": facts.get("params") or {},
                    }
                )
            else:
                task["archived"] = False
            observed = str(state["clock"])
            params = {"thread_id": target, "turn_id": turn_id, **facts}
            state["runtime"]["events"].append(
                {
                    "method": event,
                    "params": params,
                    "request_id": facts.get("request_id"),
                    "observed_at": observed,
                }
            )
            return {
                "provider": provider,
                "event": event,
                "target": target,
                "facts": params,
                "observed_at": observed,
            }

        return state_store.mutate(runtime_event)
    if provider != "delivery" or event not in {
        "delivery.validation_passed",
        "delivery.validation_failed",
        "delivery.promoted",
    }:
        raise ValueError(f"unsupported deterministic provider event {event}")

    def delivery_event(state: dict[str, Any]) -> dict[str, Any]:
        row = state["delivery"]["handles"].get(target)
        if not isinstance(row, dict):
            raise ValueError("unknown deterministic delivery handle")
        if event == "delivery.validation_passed":
            row["validation"] = "passed"
            row["checks"] = list(facts.get("checks") or [])
        elif event == "delivery.validation_failed":
            row["validation"] = "failed"
            row["checks"] = list(facts.get("checks") or [])
        else:
            if row.get("validation") != "passed" or row.get("promotion") != "pending":
                raise ValueError(
                    "delivery promotion is not pending after passed validation"
                )
            _promote_delivery_row(row)
        return {
            "provider": provider,
            "event": event,
            "target": target,
            "facts": dict(row),
            "observed_at": str(state["clock"]),
        }

    return state_store.mutate(delivery_event)


def _promote_delivery_row(row: dict[str, Any]) -> None:
    work = row["work"]
    root = str(work["project_root"])
    branch = str(work["integration_branch"])
    _git(root, "merge", "--ff-only", str(row["source_oid"]))
    integration_oid = _git(root, "rev-parse", branch)
    remote = work.get("source_remote")
    synchronization = "not_required"
    if work.get("require_source_sync"):
        if not remote:
            raise ValueError("fixture source remote is unavailable")
        _git(root, "push", str(remote), f"{branch}:{branch}")
        synchronization = "complete"
    row.update(
        {
            "promotion": "promoted",
            "integration_oid": integration_oid,
            "synchronization": synchronization,
        }
    )


def advance_clock(endpoint: str, seconds: float) -> dict[str, Any]:
    if seconds <= 0:
        raise ValueError("seconds must be positive")

    def advance(state: dict[str, Any]) -> dict[str, Any]:
        before = _parse_time(str(state["clock"]))
        after = before + timedelta(seconds=seconds)
        state["clock"] = after.isoformat().replace("+00:00", "Z")
        return {
            "before": before.isoformat().replace("+00:00", "Z"),
            "after": state["clock"],
            "seconds": seconds,
        }

    return ProviderState(endpoint).mutate(advance)


def arm_fault(endpoint: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    provider = str(payload.get("provider") or "")
    method = str(payload.get("method") or "")
    effect = str(payload.get("effect") or "")
    response = str(payload.get("response") or "")
    occurrence = payload.get("occurrence")
    if provider not in {"runtime", "delivery"}:
        raise ValueError("provider must be runtime or delivery")
    if not method:
        raise ValueError("method is required")
    if (
        not isinstance(occurrence, int)
        or isinstance(occurrence, bool)
        or occurrence < 1
    ):
        raise ValueError("occurrence must be a positive integer")
    if effect not in {"applied", "not_applied"}:
        raise ValueError("effect must be applied or not_applied")
    if response not in {"timeout", "disconnect", "reject", "malformed"}:
        raise ValueError("response must be timeout, disconnect, reject, or malformed")
    fault = {
        "provider": provider,
        "method": method,
        "occurrence": occurrence,
        "effect": effect,
        "response": response,
        "consumed": False,
    }
    ProviderState(endpoint).mutate(lambda state: state["faults"].append(fault))
    return dict(fault)


def provider_observation(endpoint: str) -> dict[str, Any]:
    state = ProviderState(endpoint).read()
    return {
        "clock": state["clock"],
        "runtime": state["runtime"],
        "delivery": state["delivery"],
        "faults": state["faults"],
        "calls": state.get("calls", {}),
        "crashes": state["crashes"],
        "barriers": state["barriers"],
    }


def _task_row(state: Mapping[str, Any], thread_id: str) -> dict[str, Any]:
    row = state["runtime"]["tasks"].get(thread_id)
    if not isinstance(row, dict) or not row.get("exists"):
        raise AppServerError(
            f"unknown deterministic task {thread_id}", category="rejected"
        )
    return row


def _task_facts(item: Mapping[str, Any], observed_at: str) -> TaskFacts:
    return TaskFacts(
        id=str(item["id"]),
        title=item.get("title"),
        cwd=item.get("cwd"),
        project_id=item.get("project_id"),
        workspace_roots=tuple(item.get("workspace_roots") or []),
        archived=bool(item.get("archived")),
        exists=bool(item.get("exists")),
        loaded=bool(item.get("loaded")),
        runtime_status=str(item.get("runtime_status") or "idle"),
        active_turn=item.get("active_turn"),
        last_turn=(
            dict(item["last_turn"])
            if isinstance(item.get("last_turn"), Mapping)
            else None
        ),
        pending_requests=tuple(dict(row) for row in item.get("pending_requests") or []),
        observed_at=observed_at,
    )


def _turn_facts(item: Mapping[str, Any], observed_at: str) -> TurnFacts:
    return TurnFacts(
        id=str(item["id"]),
        thread_id=str(item["thread_id"]),
        state=str(item["state"]),
        operation_id=item.get("operation_id"),
        completed=bool(item.get("completed")),
        error=dict(item["error"]) if isinstance(item.get("error"), Mapping) else None,
        tools=tuple(dict(row) for row in item.get("tools") or []),
        usage=dict(item["usage"]) if isinstance(item.get("usage"), Mapping) else None,
        observed_at=observed_at,
    )


def _delivery_row(
    state: Mapping[str, Any], handle: str, source_oid: str
) -> dict[str, Any]:
    row = state["delivery"]["handles"].get(handle)
    if not isinstance(row, dict) or row.get("source_oid") != source_oid:
        raise DeliveryProviderError(
            "delivery handle/source mismatch", category="rejected"
        )
    return row


def _delivery_facts(row: Mapping[str, Any], observed_at: str) -> DeliveryFacts:
    return DeliveryFacts(
        handle=str(row["handle"]),
        source_oid=str(row["source_oid"]),
        validation=str(row.get("validation") or "pending"),
        promotion=str(row.get("promotion") or "not_started"),
        integration_oid=row.get("integration_oid"),
        synchronization=str(row.get("synchronization") or "pending"),
        cleanup=str(row.get("cleanup") or "pending"),
        evidence={"buildset": list(row.get("checks") or [])},
        observed_at=observed_at,
    )


def _raise_runtime_fault(fault: Mapping[str, Any]) -> None:
    response = str(fault.get("response"))
    raise AppServerError(
        f"deterministic {response} after {fault.get('effect')} effect",
        category="rejected" if response in {"reject", "malformed"} else "transient",
        uncertain=fault.get("effect") == "applied",
    )


def _raise_delivery_fault(fault: Mapping[str, Any]) -> None:
    response = str(fault.get("response"))
    raise DeliveryProviderError(
        f"deterministic {response} after {fault.get('effect')} effect",
        category="rejected" if response in {"reject", "malformed"} else "transient",
        possible_effect=fault.get("effect") == "applied",
        evidence={"fault": dict(fault)},
    )


def _git(root: str | Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(root), *arguments),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
    return completed.stdout.strip()


def _git_ok(root: str | Path, *arguments: str) -> bool:
    return (
        subprocess.run(
            ("git", "-C", str(root), *arguments),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        ).returncode
        == 0
    )


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
