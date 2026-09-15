"""Small supervised Fulcrum2 controller and bounded reconciliation passes."""

from __future__ import annotations

import asyncio
import json
import os
import plistlib
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from watchfiles import awatch

from fulcrum.analytics import AnalyticsService
from fulcrum.configuration import ConfigurationManager
from fulcrum.contracts import (
    ActorContext,
    CommandResult,
    CommandState,
    FulcrumError,
    ParsedRequest,
)
from fulcrum.continuity import (
    fleet_admission_pause,
    reconcile_archive_once,
)
from fulcrum.ipc import IpcServer
from fulcrum.install import inspect_service
from fulcrum.ledger import (
    Ledger,
    LedgerFailure,
    OperationRecord,
    operation_id as operation_id_from_request,
    operation_view,
    utc_now,
)
from fulcrum.leadership import (
    ADMISSION_NAMESPACE,
    LEADERSHIP_NAMESPACE,
    build_brief,
    capacity_snapshot,
    dependency_readiness,
    ensure_leadership,
    normalize_native_intake,
)
from fulcrum.publication import LedgerPublicationService
from fulcrum.recovery_service import active_recovery_fence
from fulcrum.runtime import (
    AppServerError,
    AppServerRuntime,
    Runtime,
    TaskFacts,
    TurnInput,
)
from fulcrum.source_refresh import (
    maintenance_path,
    maintenance_ready_path,
    read_maintenance,
    write_maintenance,
)

TERMINAL_OPERATION_STATES = {"completed", "failed", "cancelled"}
RETRY_DELAYS = (2.0, 10.0)
EXTERNAL_RUNNER_LIMIT = 10
MAX_SENDS = 3
REMINDER_NAMESPACE = uuid.UUID("8842f0c3-557a-44d9-8e4a-c8c96f3955d1")
DELIVERY_NAMESPACE = uuid.UUID("d531e65f-328e-4a90-a1f0-eb56bd09eedb")
PLAN_COMPLETION_NAMESPACE = uuid.UUID("6d2a024c-9c95-44d6-bf2d-19877ac43a5d")
PUBLICATION_SHUTDOWN_NAMESPACE = uuid.UUID("05f85606-fb24-46f7-b2a0-10439d42950e")


class Clock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()


@dataclass(frozen=True)
class PassSummary:
    observed_at: str
    intake: tuple[str, ...]
    operations: tuple[Mapping[str, Any], ...]
    work: tuple[Mapping[str, Any], ...]
    tasks: tuple[Mapping[str, Any], ...]
    next_actions: tuple[Mapping[str, Any], ...]
    pressure: Mapping[str, Any]
    gaps: tuple[Mapping[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed_at": self.observed_at,
            "intake": list(self.intake),
            "operations": [dict(item) for item in self.operations],
            "work": [dict(item) for item in self.work],
            "tasks": [dict(item) for item in self.tasks],
            "next_actions": [dict(item) for item in self.next_actions],
            "pressure": dict(self.pressure),
            "gaps": [dict(item) for item in self.gaps],
        }


class HealthFile:
    """Atomic observations consumed by ``doctor``; it is not workflow state."""

    def __init__(self, path: Path, clock: Clock) -> None:
        self.path = path
        self.clock = clock
        self.values: dict[str, dict[str, Any]] = {}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self.values = {
                    str(key): dict(value)
                    for key, value in loaded.items()
                    if isinstance(value, Mapping)
                }
        except (OSError, json.JSONDecodeError):
            pass

    def success(self, name: str, evidence: Mapping[str, Any]) -> None:
        now = _format_time(self.clock.now())
        previous = self.values.get(name, {})
        self.values[name] = {
            **previous,
            "state": "healthy",
            "last_attempt_at": now,
            "last_success_at": now,
            "consecutive_failures": 0,
            "error": None,
            "evidence": dict(evidence),
            "pid": os.getpid(),
        }
        self._write()

    def failure(self, name: str, error: BaseException) -> int:
        now = _format_time(self.clock.now())
        previous = self.values.get(name, {})
        failures = int(previous.get("consecutive_failures", 0)) + 1
        self.values[name] = {
            **previous,
            "state": "unavailable",
            "last_attempt_at": now,
            "consecutive_failures": failures,
            "error": str(error),
            "pid": os.getpid(),
        }
        self._write()
        return failures

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(
            self.values, separators=(",", ":"), ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


class ControllerSupervisor:
    """Own the four controller loops and one shared app-server connection."""

    def __init__(
        self,
        request: ParsedRequest,
        application: Any,
        *,
        clock: Clock | None = None,
        runtime: Runtime | None = None,
    ) -> None:
        self.application = application
        manager = ConfigurationManager(request.instance.config_path)
        document, _ = manager.load()
        self.config: dict[str, Any] = manager.effective(document)
        beads = self.config["beads"]
        executable = beads.get("executable") if isinstance(beads, Mapping) else None
        if request.instance.brain_root is None:
            raise FulcrumError(
                "LEDGER_UNAVAILABLE", "controller requires a brain root", exit_code=4
            )
        self.ledger = Ledger(
            request.instance.brain_root,
            executable=str(executable) if executable else None,
            timeout=request.timeout,
        )
        runtime_config = self.config["runtime"]
        endpoint = str(runtime_config["endpoint"])
        self.clock: Clock
        self.runtime: Runtime
        if clock is not None:
            self.clock = clock
        elif runtime_config.get("kind") == "deterministic":
            from fulcrum.deterministic import DeterministicClock

            self.clock = DeterministicClock(endpoint)
        else:
            self.clock = SystemClock()
        if runtime is not None:
            self.runtime = runtime
        elif runtime_config.get("kind") == "deterministic":
            from fulcrum.deterministic import DeterministicRuntime

            self.runtime = DeterministicRuntime(endpoint)
        else:
            self.runtime = AppServerRuntime(endpoint)
        self.health = HealthFile(
            request.instance.instance_root / "service-health.json", self.clock
        )
        self.publication = LedgerPublicationService(now=self.clock.now)
        self.analytics = AnalyticsService()
        self.external_slots = asyncio.Semaphore(EXTERNAL_RUNNER_LIMIT)
        self.bead_locks: dict[str, asyncio.Lock] = {}
        self.reconcile_lock = asyncio.Lock()
        self._ipc_lock = threading.Lock()
        self._active_ipc_mutations = 0
        self._active_ipc_operations: dict[str, int] = {}
        self._stop = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.request: ParsedRequest = replace(
            request, runtime_submit=self._runtime_submit
        )

    async def run_once(
        self,
        *,
        bead_id: str | None = None,
        operation_id: str | None = None,
        keep_runtime: bool = False,
    ) -> PassSummary:
        async with self.reconcile_lock:
            return await self._run_once(
                bead_id=bead_id,
                operation_id=operation_id,
                keep_runtime=keep_runtime,
            )

    async def _run_once(
        self,
        *,
        bead_id: str | None = None,
        operation_id: str | None = None,
        keep_runtime: bool = False,
    ) -> PassSummary:
        current_loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not current_loop:
            # Bounded callers may reuse one supervisor across separate asyncio.run
            # invocations.  Coordination primitives bind lazily to the first loop
            # that contends on them, so rebuild only those process-local primitives
            # at that boundary.  Durable ordering still comes from ledger receipts.
            self.external_slots = asyncio.Semaphore(EXTERNAL_RUNNER_LIMIT)
            self.bead_locks = {}
            self.reconcile_lock = asyncio.Lock()
        self._loop = current_loop
        records = await asyncio.to_thread(self.ledger.list_records, limit=0)
        intake = tuple(
            sorted(
                record.id
                for record in records
                if record.kind is None and record.status != "closed"
            )
        )
        actions: list[Mapping[str, Any]] = []
        gaps: list[Mapping[str, Any]] = []
        leadership_actions = await ensure_leadership(
            self.request, self.ledger, self.runtime, self.config
        )
        actions.extend(
            {"kind": "leadership", **action} for action in leadership_actions
        )
        intake_actions = await asyncio.to_thread(
            normalize_native_intake, self.request, self.ledger
        )
        actions.extend({"kind": "intake", **action} for action in intake_actions)
        if leadership_actions or intake_actions:
            records = await asyncio.to_thread(self.ledger.list_records, limit=0)
        operations = [
            OperationRecord.from_record(record)
            for record in records
            if record.kind == "operation"
            and (record.fc or {}).get("state") not in TERMINAL_OPERATION_STATES
            and (operation_id is None or record.id == operation_id)
            and (bead_id is None or (record.fc or {}).get("bead_id") in {None, bead_id})
        ]
        if operation_id is not None and not any(
            operation.id == operation_id for operation in operations
        ):
            selected = await asyncio.to_thread(self.ledger.show, operation_id)
            if selected is None or selected.kind != "operation":
                raise FulcrumError.invalid(
                    "NOT_FOUND", f"unknown operation {operation_id}"
                )
        ordered_operations = sorted(operations, key=lambda item: item.id)
        resumed = list(
            await asyncio.gather(
                *(
                    self._ordered_reconcile(operation)
                    for operation in ordered_operations
                )
            )
        )
        tasks = [
            record
            for record in records
            if record.kind == "task"
            and record.fc
            and record.fc.get("deleted_at") is None
            and (
                bead_id is None
                or record.fc.get("work_bead") == bead_id
                or bead_id in record.fc.get("associated_beads", [])
            )
        ]
        work_by_id = {
            record.id: record
            for record in records
            if record.kind == "work" and (bead_id is None or record.id == bead_id)
        }
        for work_id, work in list(work_by_id.items()):
            refreshed, delivery_action = await self._reconcile_work_delivery(work)
            work_by_id[work_id] = refreshed
            if delivery_action is not None:
                actions.append(delivery_action)
        task_rows: list[Mapping[str, Any]] = []
        work_rows: list[Mapping[str, Any]] = []
        facts: dict[str, TaskFacts] = {}
        pressure: dict[str, Any] = {"paused": False, "reason": None}
        if tasks:
            try:
                await self.runtime.connect()
                facts = await self._inspect_tasks(tasks)
                pressure = await self._resource_pressure(tasks, work_by_id, facts)
                self.health.success(
                    "event", {"connection": "observed", "task_count": len(tasks)}
                )
            except AppServerError as error:
                gaps.append(
                    {
                        "component": "runtime",
                        "availability": "unavailable",
                        "reason": str(error),
                    }
                )
                self.health.failure("event", error)
        recovery_fence = await asyncio.to_thread(active_recovery_fence, self.ledger)
        if recovery_fence is not None:
            pressure = {
                "paused": True,
                "reason": "active recovery fence",
                "recovery_fence": recovery_fence,
            }
            actions.append(
                {
                    "kind": "recovery_fence",
                    "effect": "ordinary_dispatch_paused",
                    "fence": recovery_fence,
                }
            )
        try:
            reconciled_tasks = await asyncio.gather(
                *(
                    self._ordered_task_reconcile(
                        task,
                        work_by_id,
                        facts.get(_thread_id(task.fc or {})),
                    )
                    for task in tasks
                )
            )
            for task_row, task_actions in reconciled_tasks:
                task_rows.append(task_row)
                actions.extend(task_actions)
            if recovery_fence is None:
                archive_actions = await reconcile_archive_once(
                    self.ledger,
                    self.runtime,
                    tasks,
                    facts,
                    now=self.clock.now(),
                    request=self.request,
                    archive_idle_seconds=float(
                        _mapping(self.config["timing"])["archive_idle_seconds"]
                    ),
                )
                actions.extend(archive_actions)
            for work in work_by_id.values():
                fc = work.fc or {}
                work_rows.append(
                    {
                        "bead_id": work.id,
                        "owner": fc.get("owner"),
                        "phase": fc.get("phase"),
                        "ownership_operation": fc.get("ownership_operation"),
                        "next_action": fc.get("next_action"),
                        "waiting": fc.get("waiting"),
                    }
                )
            plan_actions, closed_plans = await self._complete_plan_roots(records)
            actions.extend(plan_actions)
            judgment_candidate = bool(intake) or any(
                work.status != "closed"
                and work.id not in closed_plans
                and work.fc
                and work.fc.get("phase") in {"backlog", "recovering", "human"}
                and work.fc.get("dispatch") is None
                and not _durably_deferred(work.fc.get("waiting"))
                for work in work_by_id.values()
            )
            if not pressure["paused"] and judgment_candidate:
                judgment = await self._request_marshal_judgment(facts)
                if judgment is not None:
                    actions.append(judgment)
            if not pressure["paused"]:
                actions.extend(await self._start_authorized_work())
            self.health.success(
                "reconciliation",
                {
                    "operations": len(operations),
                    "tasks": len(tasks),
                    "work": len(work_by_id),
                },
            )
            self.health.success(
                "runner", {"advanced": len(resumed), "actions": len(actions)}
            )
            self.health.success(
                "intake", {"discovered": len(intake), "busy": bool(intake)}
            )
        finally:
            if not keep_runtime:
                await self.runtime.close()
        return PassSummary(
            observed_at=_format_time(self.clock.now()),
            intake=intake,
            operations=tuple(resumed),
            work=tuple(work_rows),
            tasks=tuple(task_rows),
            next_actions=tuple(actions),
            pressure=pressure,
            gaps=tuple(gaps),
        )

    async def _reconcile_work_delivery(
        self, work: Any
    ) -> tuple[Any, Mapping[str, Any] | None]:
        retained = (work.fc or {}).get("delivery") if work.fc else None
        if (
            not isinstance(retained, Mapping)
            or not retained.get("source_oid")
            or not retained.get("provider_handle")
        ):
            return work, None
        request = ParsedRequest(
            command=("promotion", "show"),
            arguments={"bead": work.id},
            input={},
            actor=ActorContext(kind="controller"),
            instance=self.request.instance,
            request_id=None,
            timeout=self.request.timeout,
            offline=True,
            runtime_submit=self._runtime_submit,
        )
        try:
            result = await asyncio.to_thread(self.application.dispatch, request)
        except FulcrumError as error:
            return work, {
                "kind": "delivery_observation",
                "bead_id": work.id,
                "advanced": False,
                "code": error.code,
                "reason": error.message,
            }
        payload = result.result or {}
        observed = payload.get("delivery") if isinstance(payload, Mapping) else None
        if not isinstance(observed, Mapping):
            return work, None
        fc = dict(work.fc or {})
        delivery = dict(retained)
        validation = dict(delivery.get("validation") or {})
        previous_facts = validation.get("facts")
        stable_previous = (
            {
                key: value
                for key, value in previous_facts.items()
                if key != "observed_at"
            }
            if isinstance(previous_facts, Mapping)
            else None
        )
        stable_observed = {
            key: value for key, value in observed.items() if key != "observed_at"
        }
        promotion = dict(delivery.get("promotion") or {})
        synchronization = dict(delivery.get("synchronization") or {})
        cleanup = dict(delivery.get("cleanup") or {})
        changed = (
            validation.get("state") != observed.get("validation")
            or promotion.get("state") != observed.get("promotion")
            or synchronization.get("state") != observed.get("synchronization")
            or cleanup.get("state") != observed.get("cleanup")
            or stable_previous != stable_observed
        )
        if not changed:
            return work, None
        validation.update(
            {"state": observed.get("validation"), "facts": dict(observed)}
        )
        promotion["state"] = observed.get("promotion")
        synchronization["state"] = observed.get("synchronization")
        cleanup["state"] = observed.get("cleanup")
        delivery.update(
            {
                "validation": validation,
                "promotion": promotion,
                "synchronization": synchronization,
                "cleanup": cleanup,
                "observed_at": observed.get("observed_at"),
                "evidence": observed.get("evidence"),
            }
        )
        fc["delivery"] = delivery
        updated = await asyncio.to_thread(self.ledger.update_fc, work.id, fc)
        return updated, {
            "kind": "delivery_observation",
            "bead_id": work.id,
            "advanced": True,
            "validation": observed.get("validation"),
            "promotion": observed.get("promotion"),
            "source_oid": observed.get("source_oid"),
            "provider_handle": observed.get("handle"),
        }

    async def _complete_plan_roots(
        self, records: Sequence[Any]
    ) -> tuple[list[Mapping[str, Any]], set[str]]:
        actions: list[Mapping[str, Any]] = []
        closed: set[str] = set()
        roots = [
            record
            for record in records
            if record.kind == "work"
            and record.status != "closed"
            and record.fc
            and record.fc.get("workflow_root") == record.id
            and isinstance(record.fc.get("plan"), Mapping)
            and record.fc["plan"].get("published_scope") is not None
        ]
        for root in roots:
            plan = root.fc.get("plan") if root.fc else None
            assert isinstance(plan, Mapping)
            boundary = str(
                plan.get("published_approval_operation")
                or root.fc.get("ownership_operation")
            )
            request = ParsedRequest(
                command=("plan", "complete"),
                arguments={"id": root.id},
                input={},
                actor=ActorContext(kind="controller"),
                instance=self.request.instance,
                request_id=str(
                    uuid.uuid5(PLAN_COMPLETION_NAMESPACE, f"{root.id}:{boundary}")
                ),
                timeout=self.request.timeout,
                offline=True,
                runtime_submit=self._runtime_submit,
            )
            try:
                result = await asyncio.to_thread(self.application.dispatch, request)
            except FulcrumError as error:
                actions.append(
                    {
                        "kind": "plan_completion",
                        "bead_id": root.id,
                        "root_closed": False,
                        "code": error.code,
                        "reason": error.message,
                    }
                )
                continue
            payload = result.result or {}
            observed = (
                payload.get("result")
                if isinstance(payload, Mapping)
                and isinstance(payload.get("result"), Mapping)
                else payload
            )
            root_closed = bool(
                isinstance(observed, Mapping) and observed.get("root_closed")
            )
            if root_closed:
                closed.add(root.id)
            actions.append(
                {
                    "kind": "plan_completion",
                    "bead_id": root.id,
                    "root_closed": root_closed,
                    "operation_id": result.operation_id,
                    "unsatisfied": (
                        observed.get("unsatisfied", [])
                        if isinstance(observed, Mapping)
                        else []
                    ),
                }
            )
        return actions, closed

    async def _request_marshal_judgment(
        self, facts: Mapping[str, TaskFacts]
    ) -> Mapping[str, Any] | None:
        control = await asyncio.to_thread(self.ledger.show, "fc-system")
        if control is None or not control.fc:
            return None
        marshal_thread = _optional_string(control.fc.get("marshal_thread"))
        if marshal_thread is None:
            return None
        observed = facts.get(marshal_thread)
        if observed is not None and observed.active_turn is not None:
            return {
                "kind": "marshal_decision",
                "started": False,
                "reason": "standing Marshal already has an active turn",
            }
        manager = ConfigurationManager(self.request.instance.config_path)
        document, _ = manager.load()
        config = manager.effective(document)
        brief, _ = await asyncio.to_thread(
            build_brief,
            self.ledger,
            config,
            requested_kind="auto",
            bead_id=None,
        )
        fc = dict(control.fc)
        pending = fc.get("pending_decision")
        if not brief["decision_required"]:
            if pending is not None:
                fc["pending_decision"] = None
                await asyncio.to_thread(self.ledger.update_fc, control.id, fc)
            return None
        operations = await asyncio.to_thread(
            self.ledger.list_records, kind="operation", limit=0
        )
        outstanding = next(
            (
                record
                for record in operations
                if record.fc
                and record.fc.get("command") == "marshal.request"
                and record.fc.get("state") in {"accepted", "running"}
            ),
            None,
        )
        if outstanding is not None:
            return {
                "kind": "marshal_decision",
                "started": False,
                "operation_id": outstanding.id,
                "reason": "one Marshal decision is already outstanding",
            }
        now = self.clock.now()
        if not isinstance(pending, Mapping) or pending.get("kind") != brief["kind"]:
            pending = {
                "kind": brief["kind"],
                "first_seen_at": _format_time(now),
                "selected_ids": [row["bead_id"] for row in brief["rows"]],
            }
            fc["pending_decision"] = pending
            await asyncio.to_thread(self.ledger.update_fc, control.id, fc)
            return {
                "kind": "marshal_decision",
                "started": False,
                "reason": "coalescing compatible judgment events",
                "eligible_at": _format_time(
                    now
                    + timedelta(
                        seconds=float(
                            _mapping(config["timing"])["marshal_coalesce_seconds"]
                        )
                    )
                ),
            }
        first_seen = _parse_time(str(pending["first_seen_at"]))
        coalesce = float(_mapping(config["timing"])["marshal_coalesce_seconds"])
        if (now - first_seen).total_seconds() < coalesce:
            return {
                "kind": "marshal_decision",
                "started": False,
                "reason": "coalescing compatible judgment events",
                "eligible_at": _format_time(first_seen + timedelta(seconds=coalesce)),
            }
        request = ParsedRequest(
            command=("marshal", "request"),
            arguments={"kind": brief["kind"]},
            input={},
            actor=ActorContext(kind="task", task_id=marshal_thread),
            instance=self.request.instance,
            request_id=str(
                uuid.uuid5(
                    LEADERSHIP_NAMESPACE,
                    f"automatic:{brief['kind']}:{pending['first_seen_at']}",
                )
            ),
            thread_id=marshal_thread,
            timeout=self.request.timeout,
            offline=True,
            runtime_submit=self._runtime_submit,
        )
        async with self.external_slots:
            result = await asyncio.to_thread(self.application.dispatch, request)
        latest = await asyncio.to_thread(self.ledger.show, "fc-system")
        if latest is not None and latest.fc:
            latest_fc = dict(latest.fc)
            latest_fc["pending_decision"] = None
            await asyncio.to_thread(self.ledger.update_fc, latest.id, latest_fc)
        return {
            "kind": "marshal_decision",
            "started": result.state in {CommandState.RUNNING, CommandState.COMPLETED},
            "operation_id": result.operation_id,
            "state": result.state.value,
        }

    async def _start_authorized_work(self) -> list[Mapping[str, Any]]:
        records = await asyncio.to_thread(
            self.ledger.list_records, kind="work", limit=0
        )
        manager = ConfigurationManager(self.request.instance.config_path)
        document, _ = manager.load()
        config = manager.effective(document)
        candidates = sorted(
            records, key=lambda item: (int(item.native.get("priority", 2)), item.id)
        )
        candidates = [
            record
            for record in candidates
            if record.status != "closed"
            and (record.fc or {}).get("phase") == "backlog"
            and isinstance((record.fc or {}).get("dispatch"), Mapping)
            and not (
                isinstance(
                    (record.fc or {}).get("dispatch", {}).get("reservation"), Mapping
                )
                and (record.fc or {})["dispatch"]["reservation"].get("state")
                in {"in_flight", "unknown", "started"}
            )
        ]
        if not candidates:
            return []
        readiness = await asyncio.gather(
            *(
                asyncio.to_thread(dependency_readiness, self.ledger, record)
                for record in candidates
            )
        )
        capacity = await asyncio.to_thread(capacity_snapshot, self.ledger, config)
        projects = sorted(
            {str((record.fc or {}).get("project") or "") for record in candidates}
        )
        replacement_pauses = dict(
            zip(
                projects,
                await asyncio.gather(
                    *(
                        asyncio.to_thread(fleet_admission_pause, self.ledger, project)
                        for project in projects
                    )
                ),
            )
        )
        requests: list[tuple[str, ParsedRequest]] = []
        for record, (ready, _) in zip(candidates, readiness):
            fc = record.fc or {}
            dispatch = fc.get("dispatch")
            assert isinstance(dispatch, Mapping)
            if not ready:
                continue
            project = str(fc.get("project") or "")
            if replacement_pauses[project] is not None:
                continue
            project_row = capacity["projects"].get(project, {})
            if not dispatch.get("human_bypass") and (
                capacity["occupied"] >= capacity["global_limit"]
                or int(project_row.get("occupied", 0))
                >= int(project_row.get("limit", capacity["default_project_limit"]))
                or project in capacity["paused_projects"]
            ):
                continue
            decision = str(dispatch.get("decision_operation") or "none")
            request = ParsedRequest(
                command=("dispatch",),
                arguments={"bead": record.id},
                input={},
                actor=ActorContext(kind="controller"),
                instance=self.request.instance,
                request_id=str(
                    uuid.uuid5(
                        ADMISSION_NAMESPACE,
                        f"mechanical:{record.id}:{decision}",
                    )
                ),
                timeout=self.request.timeout,
                offline=True,
                runtime_submit=self._runtime_submit,
            )
            requests.append((record.id, request))

        async def start(bead_id: str, request: ParsedRequest) -> Mapping[str, Any]:
            async with self.external_slots:
                result = await asyncio.to_thread(self.application.dispatch, request)
            return {
                "kind": "authorized_start",
                "bead_id": bead_id,
                "operation_id": result.operation_id,
                "state": result.state.value,
            }

        return list(
            await asyncio.gather(
                *(start(bead_id, request) for bead_id, request in requests)
            )
        )

    def _runtime_submit(
        self,
        action: Callable[[Runtime], Awaitable[Any]],
        timeout: float,
    ) -> Any:
        """Run command-layer runtime work on the controller's shared loop."""

        loop = self._loop
        if loop is None:
            raise RuntimeError("controller runtime loop is not established")

        async def invoke() -> Any:
            await self.runtime.connect()
            return await action(self.runtime)

        future = asyncio.run_coroutine_threadsafe(invoke(), loop)
        return future.result(timeout=timeout)

    async def serve(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.application.replace_handler(("reconcile",), self.reconcile_from_ipc)
        await self._startup_reconcile()
        server = IpcServer(self.request.instance.socket_path, self._dispatch_from_ipc)
        tasks = [
            asyncio.create_task(server.run(), name="fulcrum-ipc"),
            asyncio.create_task(
                self._supervise("intake", self._intake_loop),
                name="fulcrum-intake",
            ),
            asyncio.create_task(
                self._supervise("reconciliation", self._reconciliation_loop),
                name="fulcrum-reconciliation",
            ),
            asyncio.create_task(
                self._supervise("runner", self._runner_loop),
                name="fulcrum-runner",
            ),
            asyncio.create_task(
                self._supervise("event", self._event_loop),
                name="fulcrum-events",
            ),
            asyncio.create_task(
                self._supervise("publication", self._publication_loop),
                name="fulcrum-publication",
            ),
            asyncio.create_task(
                self._supervise("maintenance", self._maintenance_loop),
                name="fulcrum-maintenance",
            ),
        ]
        if self.config.get("source_watch_root") is not None:
            tasks.append(
                asyncio.create_task(
                    self._supervise("source-watch", self._source_watch_loop),
                    name="fulcrum-source-watch",
                )
            )
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                exception = task.exception()
                if exception is not None:
                    raise exception
        finally:
            self._stop.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await asyncio.wait_for(self._shutdown_cleanup(), 35)
            except (TimeoutError, AppServerError, LedgerFailure):
                pass
            await self.runtime.close()

    def reconcile_from_ipc(self, request: ParsedRequest) -> CommandResult:
        loop = self._loop
        if loop is None:
            raise FulcrumError(
                "CONTROLLER_UNAVAILABLE",
                "controller reconciliation loop is not running",
                exit_code=4,
            )
        future = asyncio.run_coroutine_threadsafe(
            self.run_once(
                bead_id=_optional_string(request.arguments.get("bead")),
                operation_id=_optional_string(request.arguments.get("operation")),
                keep_runtime=True,
            ),
            loop,
        )
        try:
            summary = future.result(timeout=request.timeout)
        except TimeoutError as error:
            raise FulcrumError(
                "WAIT_TIMEOUT",
                "bounded reconciliation continues after the client timeout",
                exit_code=3,
                retryable=True,
            ) from error
        return CommandResult.query(summary.to_dict())

    def _dispatch_from_ipc(self, request: ParsedRequest) -> CommandResult:
        mutating = request.request_id is not None
        active_operation = (
            operation_id_from_request(request.request_id)
            if request.request_id is not None
            else None
        )
        if (
            mutating
            and read_maintenance(maintenance_path(self.request.instance.instance_root))
            is not None
        ):
            raise FulcrumError(
                "MAINTENANCE_IN_PROGRESS",
                "installed source activation is waiting at a quiescent boundary",
                exit_code=4,
                retryable=True,
            )
        if mutating:
            with self._ipc_lock:
                self._active_ipc_mutations += 1
                assert active_operation is not None
                self._active_ipc_operations[active_operation] = (
                    self._active_ipc_operations.get(active_operation, 0) + 1
                )
        try:
            return self.application.dispatch(
                replace(request, runtime_submit=self._runtime_submit)
            )
        finally:
            if mutating:
                with self._ipc_lock:
                    self._active_ipc_mutations -= 1
                    assert active_operation is not None
                    remaining = self._active_ipc_operations[active_operation] - 1
                    if remaining:
                        self._active_ipc_operations[active_operation] = remaining
                    else:
                        self._active_ipc_operations.pop(active_operation)

    async def _maintenance_loop(self) -> None:
        request_path = maintenance_path(self.request.instance.instance_root)
        ready_path = maintenance_ready_path(self.request.instance.instance_root)
        while not self._stop.is_set():
            requested = read_maintenance(request_path)
            if requested is None:
                ready_path.unlink(missing_ok=True)
                await self._wait(0.1)
                continue
            operation_id = requested.get("operation_id")
            if not isinstance(operation_id, str):
                await self._wait(0.1)
                continue
            async with self.reconcile_lock:
                while not self._stop.is_set():
                    with self._ipc_lock:
                        active = self._active_ipc_mutations
                    current = read_maintenance(request_path)
                    if current is None or current.get("operation_id") != operation_id:
                        break
                    if active == 0:
                        write_maintenance(
                            ready_path,
                            {
                                "operation_id": operation_id,
                                "acknowledged_at": _format_time(self.clock.now()),
                                "active_ipc_mutations": 0,
                            },
                        )
                        self.health.success(
                            "maintenance", {"operation_id": operation_id, "ready": True}
                        )
                        while not self._stop.is_set():
                            current = read_maintenance(request_path)
                            if (
                                current is None
                                or current.get("operation_id") != operation_id
                            ):
                                break
                            await self._wait(0.1)
                        break
                    await self._wait(0.05)

    async def _source_watch_loop(self) -> None:
        configured = self.config.get("source_watch_root")
        if not isinstance(configured, str):
            return
        source = Path(configured).resolve(strict=True)
        instance = self.request.instance.instance_root.resolve(strict=False)
        async for changes in awatch(source, debounce=500, step=100):
            relevant = []
            for _kind, raw_path in changes:
                path = Path(raw_path).resolve(strict=False)
                relative = path.relative_to(source)
                if path.is_relative_to(instance) or any(
                    part in {".git", ".venv", "__pycache__"} for part in relative.parts
                ):
                    continue
                if path.suffix in {".py", ".toml", ".lock", ".json", ".yaml", ".md"}:
                    relevant.append(str(path))
            if not relevant:
                continue
            try:
                detail = await asyncio.to_thread(
                    _launch_source_updater,
                    instance / "services" / "updater.plist",
                )
                self.health.success(
                    "source-watch", {**detail, "change_count": len(relevant)}
                )
                if detail.get("state") == "started":
                    return
            except Exception as error:
                self.health.failure("source-watch", error)

    async def _startup_reconcile(self) -> None:
        try:
            await self.run_once()
        except LedgerFailure as error:
            # Keep the inspection socket available while the supervised loops prove
            # whether an unavailable ledger is transient or requires a restart.
            self.health.failure("reconciliation", error)
        try:
            publication = await asyncio.to_thread(self.publication.tick, self.request)
            self.health.success("publication", publication)
        except Exception as error:
            self.health.failure("publication", error)

    async def _supervise(self, name: str, loop: Callable[[], Awaitable[None]]) -> None:
        failures = 0
        while not self._stop.is_set():
            try:
                await loop()
                return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                failures += 1
                self.health.failure(name, error)
                if failures >= 3:
                    raise RuntimeError(
                        f"critical controller loop {name} failed {failures} times"
                    ) from error
                await asyncio.sleep(RETRY_DELAYS[min(failures - 1, 1)])

    async def _intake_loop(self) -> None:
        timing = _mapping(self.config["timing"])
        while not self._stop.is_set():
            records = await asyncio.to_thread(self.ledger.list_records, limit=0)
            intake = [
                item.id
                for item in records
                if item.kind is None and item.status != "closed"
            ]
            self.health.success("intake", {"discovered": len(intake)})
            if intake:
                await self.run_once(keep_runtime=True)
            delay = float(
                timing["intake_busy_seconds"]
                if intake
                else timing["intake_idle_seconds"]
            )
            await self._wait(delay)

    async def _reconciliation_loop(self) -> None:
        interval = float(_mapping(self.config["timing"])["reconcile_seconds"])
        while not self._stop.is_set():
            await self.run_once(keep_runtime=True)
            await self._wait(interval)

    async def _runner_loop(self) -> None:
        interval = float(_mapping(self.config["timing"])["reconcile_seconds"])
        while not self._stop.is_set():
            self.health.success("runner", {"pending": 0})
            await self._wait(interval)

    async def _publication_loop(self) -> None:
        cadence = float(_mapping(self.config["brain"])["push_interval_seconds"])
        while not self._stop.is_set():
            result = await asyncio.to_thread(self.publication.tick, self.request)
            self.health.success("publication", result)
            delay = cadence
            command_result = result.get("result")
            if isinstance(command_result, Mapping):
                operation = command_result.get("result")
                planned = (
                    operation.get("planned") if isinstance(operation, Mapping) else None
                )
                retry_at = (
                    planned.get("next_retry_at")
                    if isinstance(planned, Mapping)
                    else None
                )
                if isinstance(retry_at, str):
                    delay = min(
                        cadence,
                        max(
                            0.1,
                            (_parse_time(retry_at) - self.clock.now()).total_seconds(),
                        ),
                    )
            await self._wait(delay)

    async def _event_loop(self) -> None:
        silence = float(_mapping(self.config["timing"])["event_silence_seconds"])
        await self.runtime.connect()
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=256)

        async def read() -> None:
            async for event in self.runtime.events():
                if queue.full():
                    queue.get_nowait()
                queue.put_nowait(event)

        reader = asyncio.create_task(read(), name="fulcrum-runtime-events")
        try:
            while not self._stop.is_set():
                next_event = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait(
                    {next_event, reader},
                    timeout=silence,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if reader in done:
                    next_event.cancel()
                    await asyncio.gather(next_event, return_exceptions=True)
                    exception = reader.exception()
                    if exception is not None:
                        raise exception
                    raise RuntimeError("runtime event reader stopped")
                if next_event not in done:
                    next_event.cancel()
                    await asyncio.gather(next_event, return_exceptions=True)
                    await self._silence_inspection()
                    self.health.success("event", {"silence_inspection": True})
                    continue
                event = next_event.result()
                await self._record_runtime_event(event)
                self.health.success(
                    "event", {"method": event.method, "observed_at": event.observed_at}
                )
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    async def _wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), seconds)
        except TimeoutError:
            pass

    async def _inspect_tasks(self, tasks: Sequence[Any]) -> dict[str, TaskFacts]:
        async def inspect(thread_id: str) -> tuple[str, TaskFacts | None]:
            async with self.external_slots:
                try:
                    return thread_id, await self.runtime.inspect_task(thread_id)
                except AppServerError:
                    return thread_id, None

        observed = await asyncio.gather(
            *(inspect(_thread_id(task.fc or {})) for task in tasks)
        )
        return {key: value for key, value in observed if value is not None}

    async def _reconcile_operation(
        self, operation: OperationRecord
    ) -> Mapping[str, Any]:
        state = str(operation.operation.get("state"))
        if state == "uncertain":
            return {
                **operation_view(operation),
                "advanced": False,
                "recovery_required": True,
            }
        with self._ipc_lock:
            in_flight = self._active_ipc_operations.get(operation.id, 0) > 0
        if in_flight:
            return {
                **operation_view(operation),
                "advanced": False,
                "in_flight": True,
            }
        planned = operation.operation.get("planned")
        due = planned.get("next_retry_at") if isinstance(planned, Mapping) else None
        if isinstance(due, str) and self.clock.now() < _parse_time(due):
            return {**operation_view(operation), "advanced": False, "retry_due_at": due}
        attempts = int(operation.operation.get("attempts") or 0)
        if attempts >= MAX_SENDS:
            return {**operation_view(operation), "advanced": False, "exhausted": True}
        accepted = operation.operation.get("input")
        if not isinstance(accepted, Mapping):
            return {**operation_view(operation), "advanced": False, "corrupt": True}
        command = str(operation.operation.get("command") or "")
        if command.startswith("controller.") or command in {
            "setup",
            "service.start",
            "service.stop",
            "service.restart",
            "service.update",
            "operation.reconcile",
            "operation.cancel",
        }:
            return {**operation_view(operation), "advanced": False}
        operation = await asyncio.to_thread(
            self.ledger.update_operation, operation, attempts=attempts + 1
        )
        try:
            request = _restore_request(
                self.request,
                command,
                accepted,
                _optional_string(operation.operation.get("request_id")),
            )
            async with self.external_slots:
                result = await asyncio.to_thread(self.application.dispatch, request)
            return {
                **operation_view(operation),
                "advanced": True,
                "result_state": result.state.value,
            }
        except FulcrumError as error:
            return await self._record_retry_failure(operation, error)
        except LedgerFailure as error:
            return await self._record_retry_failure(operation, error)

    async def _ordered_reconcile(self, operation: OperationRecord) -> Mapping[str, Any]:
        bead_id = operation.operation.get("bead_id")
        key = str(bead_id or f"operation:{operation.id}")
        lock = self.bead_locks.setdefault(key, asyncio.Lock())
        async with lock:
            fresh = await asyncio.to_thread(self.ledger.show, operation.id)
            if fresh is None or fresh.kind != "operation":
                return {"id": operation.id, "advanced": False, "missing": True}
            current = OperationRecord.from_record(fresh)
            if current.operation.get("state") in TERMINAL_OPERATION_STATES:
                return {**operation_view(current), "advanced": False, "settled": True}
            return await self._reconcile_operation(current)

    async def _record_retry_failure(
        self, operation: OperationRecord, error: FulcrumError | LedgerFailure
    ) -> Mapping[str, Any]:
        attempts = int(operation.operation.get("attempts") or 0)
        uncertain = (
            error.uncertain
            if isinstance(error, LedgerFailure)
            else error.state.value == "uncertain"
        )
        retryable = error.retryable
        planned = dict(operation.operation.get("planned") or {})
        if uncertain:
            state = "uncertain"
            next_action = "Inspect the recorded external postcondition before replay."
        elif retryable and attempts < MAX_SENDS:
            state = "accepted"
            delay = RETRY_DELAYS[min(attempts - 1, len(RETRY_DELAYS) - 1)]
            planned["next_retry_at"] = _format_time(
                self.clock.now() + timedelta(seconds=delay)
            )
            next_action = f"Retry the same retained effect after {int(delay)} seconds."
        else:
            state = "failed"
            next_action = (
                "Request one explicit recovery decision; automatic sends are exhausted."
            )
        updated = await asyncio.to_thread(
            self.ledger.update_operation,
            operation,
            state=state,
            step="retry_wait" if state == "accepted" else "automatic_replay_stopped",
            error={
                "code": (
                    error.code if isinstance(error, FulcrumError) else error.category
                ),
                "message": str(error),
                "retryable": retryable,
            },
            planned=planned,
            next_action=next_action,
        )
        return {
            **operation_view(updated),
            "advanced": False,
            "exhausted": state == "failed",
            "recovery_required": state in {"failed", "uncertain"},
        }

    async def _reconcile_task(
        self,
        task: Any,
        work_by_id: Mapping[str, Any],
        facts: TaskFacts | None,
    ) -> tuple[Mapping[str, Any], list[Mapping[str, Any]]]:
        actions: list[Mapping[str, Any]] = []
        if facts is None:
            fc = dict(task.fc or {})
            return (
                {
                    "task_record_id": task.id,
                    "thread_id": fc.get("thread_id"),
                    "observed": False,
                },
                actions,
            )
        await asyncio.to_thread(self.analytics.observe_task, self.ledger, task, facts)
        task = await asyncio.to_thread(self.ledger.show, task.id) or task
        fc = dict(task.fc or {})
        original_fc = dict(fc)
        previous_observed = fc.get("last_observed")
        previous_turn = (
            previous_observed.get("last_turn")
            if isinstance(previous_observed, Mapping)
            else None
        )
        now = self.clock.now()
        if (
            facts.active_turn is not None
            and facts.last_turn is not None
            and facts.last_turn != previous_turn
        ):
            fc["last_tool_evidence_at"] = _format_time(now)
        fc["last_observed"] = facts.to_dict()
        if fc.get("purpose") == "leadership":
            if fc != original_fc:
                await asyncio.to_thread(self.ledger.update_fc, task.id, fc)
            return (
                {
                    "task_record_id": task.id,
                    "thread_id": facts.id,
                    "runtime_status": facts.runtime_status,
                    "active_turn": facts.active_turn,
                    "work_bead": None,
                    "progress_age_seconds": 0.0,
                    "observed": True,
                },
                actions,
            )
        pending_ids = {
            str(item.get("request_id"))
            for item in facts.pending_requests
            if item.get("request_id") is not None
        }
        previously_surfaced = set(fc.get("surfaced_request_ids", []))
        newly_surfaced = sorted(pending_ids.difference(previously_surfaced))
        if newly_surfaced:
            fc["surfaced_request_ids"] = sorted(previously_surfaced.union(pending_ids))
            fc["pending_native_requests"] = [
                dict(item) for item in facts.pending_requests
            ]
            actions.append(
                {
                    "kind": "native_request",
                    "thread_id": facts.id,
                    "request_ids": newly_surfaced,
                    "effect": "surface_only",
                }
            )
        event_at = _optional_time(fc.get("last_runtime_event_at")) or _optional_time(
            fc.get("last_inspection_at")
        )
        timing = _mapping(self.config["timing"])
        if event_at is None or (now - event_at).total_seconds() >= float(
            timing["event_silence_seconds"]
        ):
            fc["last_inspection_at"] = _format_time(now)
            actions.append(
                {
                    "kind": "silence_inspection",
                    "thread_id": facts.id,
                    "effect": "read_only",
                }
            )
        work_id = fc.get("work_bead")
        work = work_by_id.get(str(work_id)) if work_id else None
        progress_at = _optional_time(
            (work.fc or {}).get("last_progress_at") if work is not None else None
        ) or _optional_time(fc.get("last_substantive_progress_at"))
        tool_evidence_at = _optional_time(fc.get("last_tool_evidence_at"))
        if tool_evidence_at is not None and (
            progress_at is None or tool_evidence_at > progress_at
        ):
            progress_at = tool_evidence_at
        progress_age = (now - progress_at).total_seconds() if progress_at else 0.0
        acquisition = fc.get("ownership_operation")
        terminal = (
            facts.active_turn is None
            and facts.last_turn is not None
            and facts.runtime_status in {"idle", "completed", "failed"}
        )
        handoff_advanced = False
        if (
            terminal
            and work is not None
            and work.fc
            and work.fc.get("phase") == "handoff"
            and isinstance(work.fc.get("handoff"), Mapping)
            and work.fc["handoff"].get("from_thread") == facts.id
        ):
            handoff_action = await self._advance_executor_handoff(task, work, facts)
            actions.append(handoff_action)
            handoff_advanced = bool(handoff_action.get("started"))
        delivery_advanced = False
        if (
            terminal
            and work is not None
            and work.fc
            and work.fc.get("phase") == "delivering"
            and isinstance(work.fc.get("delivery_finish"), Mapping)
            and work.fc.get("owner") == facts.id
        ):
            delivery_action = await self._advance_warden_delivery(task, work, facts)
            actions.append(delivery_action)
            delivery_advanced = bool(delivery_action.get("advanced"))
        if fc.get("purpose") == "plan_review" and terminal:
            if fc.get("review_result_operation"):
                if fc.get("subscription_state") != "released":
                    try:
                        released = await self.runtime.release(facts.id)
                    except AppServerError as error:
                        actions.append(
                            {
                                "kind": "review_release",
                                "thread_id": facts.id,
                                "state": "failed",
                                "reason": str(error),
                            }
                        )
                    else:
                        fc["subscription_state"] = "released"
                        fc["subscription_release"] = released.to_dict()
                        actions.append(
                            {
                                "kind": "review_release",
                                "thread_id": facts.id,
                                "state": "completed",
                            }
                        )
            elif not fc.get("missing_finish_reminder"):
                reminder = await self._send_review_finish_reminder(task, facts)
                fc["missing_finish_reminder"] = reminder["operation_id"]
                fc["missing_finish_reminder_turn"] = reminder.get("turn_id")
                actions.append(reminder)
            elif (
                not fc.get("missing_finish_reminder_turn")
                or (
                    isinstance(facts.last_turn, Mapping)
                    and facts.last_turn.get("id")
                    == fc.get("missing_finish_reminder_turn")
                )
            ) and not fc.get("recovery_requested_operation"):
                recovery = await self._record_review_recovery(task)
                fc["recovery_requested_operation"] = recovery["operation_id"]
                actions.append(recovery)
        if (
            work is not None
            and work.status != "closed"
            and work.fc
            and work.fc.get("owner") == facts.id
            and not fc.get("awaiting_role_entry")
        ):
            if (
                terminal
                and not handoff_advanced
                and not delivery_advanced
                and work.fc.get("phase") not in {"handoff", "delivering"}
                and fc.get("last_finish_reminder_acquisition") != acquisition
            ):
                action = await self._send_task_notice(
                    task,
                    facts,
                    work,
                    kind="finish_reminder",
                    text=(
                        "Your native turn ended without the required Fulcrum finish. "
                        f"Preserve the current scope and run `fulcrum finish --bead {work.id} "
                        f"--thread-id {facts.id} --ownership-operation {acquisition} --json` "
                        "with the role-required evidence."
                    ),
                )
                fc["last_finish_reminder_operation"] = action.get("operation_id")
                fc["last_finish_reminder_acquisition"] = acquisition
                actions.append(action)
            elif (
                progress_age >= float(timing["checkpoint_seconds"])
                and terminal
                and fc.get("last_checkpoint_acquisition") != acquisition
            ):
                action = await self._send_task_notice(
                    task,
                    facts,
                    work,
                    kind="checkpoint",
                    text=(
                        "Record one substantive checkpoint for the current scope: a completed "
                        "investigation, source change, validation result, or concrete blocker. "
                        "Do not report a timer heartbeat."
                    ),
                )
                fc["last_checkpoint_operation"] = action.get("operation_id")
                fc["last_checkpoint_acquisition"] = acquisition
                actions.append(action)
            if (
                progress_age >= float(timing["stalled_seconds"])
                and fc.get("recovery_requested_acquisition") != acquisition
            ):
                recovery = await self._record_recovery_request(task, work)
                fc["recovery_requested_operation"] = recovery["operation_id"]
                fc["recovery_requested_acquisition"] = acquisition
                actions.append(recovery)
        if fc != original_fc:
            await asyncio.to_thread(self.ledger.update_fc, task.id, fc)
        return (
            {
                "task_record_id": task.id,
                "thread_id": facts.id,
                "runtime_status": facts.runtime_status,
                "active_turn": facts.active_turn,
                "work_bead": work_id,
                "progress_age_seconds": progress_age,
                "observed": True,
            },
            actions,
        )

    async def _advance_executor_handoff(
        self, task: Any, work: Any, facts: TaskFacts
    ) -> Mapping[str, Any]:
        handoff = work.fc.get("handoff") if work.fc else None
        assert isinstance(handoff, Mapping)
        request_id = _optional_string(handoff.get("start_request_id"))
        if request_id is None:
            return {
                "kind": "executor_warden_handoff",
                "bead_id": work.id,
                "started": False,
                "reason": "retained handoff has no Warden start identity",
            }
        try:
            terminal_facts = await self.runtime.terminals(
                facts.id, limit=0, cursor=None
            )
        except AppServerError as error:
            return {
                "kind": "executor_warden_handoff",
                "bead_id": work.id,
                "started": False,
                "reason": str(error),
                "gap": "owned terminal state could not be observed",
            }
        running = terminal_facts.get("items")
        if isinstance(running, list) and running:
            return {
                "kind": "executor_warden_handoff",
                "bead_id": work.id,
                "started": False,
                "reason": "Executor still owns running background terminals",
                "terminals": running,
            }
        try:
            released = await self.runtime.release(facts.id)
        except AppServerError as error:
            return {
                "kind": "executor_warden_handoff",
                "bead_id": work.id,
                "started": False,
                "reason": str(error),
            }
        if released.active_terminals:
            return {
                "kind": "executor_warden_handoff",
                "bead_id": work.id,
                "started": False,
                "reason": "Executor release still observed active terminals",
                "release": released.to_dict(),
            }
        current_task = await asyncio.to_thread(self.ledger.show, task.id)
        if current_task is not None and current_task.fc:
            task_fc = dict(current_task.fc)
            task_fc["subscription_state"] = "released"
            task_fc["subscription_release"] = released.to_dict()
            await asyncio.to_thread(self.ledger.update_fc, current_task.id, task_fc)
        request = ParsedRequest(
            command=("enter",),
            arguments={"role": "warden", "origin": "dispatch"},
            input={
                "description": (
                    "Review the retained current source, fix any issue in the same "
                    "workspace, and deliver only the exact approved source."
                ),
                "bead": work.id,
            },
            actor=ActorContext(kind="controller"),
            instance=self.request.instance,
            request_id=request_id,
            timeout=self.request.timeout,
            offline=True,
            runtime_submit=self._runtime_submit,
        )
        async with self.external_slots:
            result = await asyncio.to_thread(self.application.dispatch, request)
        return {
            "kind": "executor_warden_handoff",
            "bead_id": work.id,
            "started": result.state in {CommandState.RUNNING, CommandState.COMPLETED},
            "operation_id": result.operation_id,
            "state": result.state.value,
            "executor_release": released.to_dict(),
        }

    async def _advance_warden_delivery(
        self, task: Any, work: Any, facts: TaskFacts
    ) -> Mapping[str, Any]:
        delivery_finish = work.fc.get("delivery_finish") if work.fc else None
        assert isinstance(delivery_finish, Mapping)
        finish_operation = str(delivery_finish.get("operation_id") or "unknown")
        base = ParsedRequest(
            command=("promotion", "show"),
            arguments={"bead": work.id},
            input={},
            actor=ActorContext(kind="task", task_id=facts.id),
            instance=self.request.instance,
            request_id=None,
            thread_id=facts.id,
            ownership_operation=str(work.fc.get("ownership_operation")),
            timeout=self.request.timeout,
            offline=True,
            runtime_submit=self._runtime_submit,
        )
        try:
            observed = await asyncio.to_thread(self.application.dispatch, base)
        except FulcrumError as error:
            return {
                "kind": "warden_delivery",
                "bead_id": work.id,
                "advanced": False,
                "reason": error.message,
                "code": error.code,
            }
        result = observed.result or {}
        delivery = result.get("delivery") if isinstance(result, Mapping) else None
        if not isinstance(delivery, Mapping):
            return {
                "kind": "warden_delivery",
                "bead_id": work.id,
                "advanced": False,
                "reason": "promotion inspection returned no delivery facts",
            }
        if (
            delivery.get("validation") == "failed"
            or delivery.get("promotion") == "failed"
        ):
            current = await asyncio.to_thread(self.ledger.show, work.id)
            if current is not None and current.fc:
                fc = dict(current.fc)
                fc["phase"] = "reviewing"
                fc["next_action"] = (
                    "Warden must inspect failed delivery evidence, fix the same workspace, "
                    "and validate a new exact source."
                )
                fc["last_transition"] = finish_operation
                await asyncio.to_thread(self.ledger.update_fc, current.id, fc)
            return {
                "kind": "warden_delivery",
                "bead_id": work.id,
                "advanced": True,
                "state": "returned_to_review",
                "delivery": dict(delivery),
            }
        if delivery.get("promotion") != "promoted":
            return {
                "kind": "warden_delivery",
                "bead_id": work.id,
                "advanced": False,
                "state": "provider_pending",
                "delivery": dict(delivery),
            }
        try:
            terminal_facts = await self.runtime.terminals(
                facts.id, limit=0, cursor=None
            )
        except AppServerError as error:
            return {
                "kind": "warden_delivery",
                "bead_id": work.id,
                "advanced": False,
                "reason": str(error),
                "gap": "owned terminal state could not be observed",
            }
        running = terminal_facts.get("items")
        if isinstance(running, list) and running:
            return {
                "kind": "warden_delivery",
                "bead_id": work.id,
                "advanced": False,
                "reason": "Warden still owns running background terminals",
                "terminals": running,
            }
        try:
            released = await self.runtime.release(facts.id)
        except AppServerError as error:
            return {
                "kind": "warden_delivery",
                "bead_id": work.id,
                "advanced": False,
                "reason": str(error),
            }
        if released.active_terminals:
            return {
                "kind": "warden_delivery",
                "bead_id": work.id,
                "advanced": False,
                "reason": "Warden release still observed active terminals",
                "release": released.to_dict(),
            }
        current_task = await asyncio.to_thread(self.ledger.show, task.id)
        if current_task is not None and current_task.fc:
            task_fc = dict(current_task.fc)
            task_fc["subscription_state"] = "released"
            task_fc["subscription_release"] = released.to_dict()
            await asyncio.to_thread(self.ledger.update_fc, current_task.id, task_fc)
        sync = await asyncio.to_thread(
            self.application.dispatch,
            replace(
                base,
                command=("source", "sync"),
                request_id=_delivery_request_id(finish_operation, "source-sync"),
            ),
        )
        if sync.state not in {CommandState.COMPLETED}:
            return {
                "kind": "warden_delivery",
                "bead_id": work.id,
                "advanced": False,
                "state": "synchronization_unresolved",
                "operation_id": sync.operation_id,
                "result_state": sync.state.value,
            }
        cleanup = await asyncio.to_thread(
            self.application.dispatch,
            replace(
                base,
                command=("worktree", "cleanup"),
                request_id=_delivery_request_id(finish_operation, "cleanup"),
            ),
        )
        if cleanup.state not in {CommandState.COMPLETED}:
            return {
                "kind": "warden_delivery",
                "bead_id": work.id,
                "advanced": False,
                "state": "cleanup_unresolved",
                "operation_id": cleanup.operation_id,
                "result_state": cleanup.state.value,
            }
        close = await asyncio.to_thread(
            self.application.dispatch,
            replace(
                base,
                command=("work", "close"),
                arguments={
                    "id": work.id,
                    "outcome": "delivered",
                    "summary": str(delivery_finish.get("summary") or "Delivered"),
                },
                request_id=_delivery_request_id(finish_operation, "close"),
            ),
        )
        return {
            "kind": "warden_delivery",
            "bead_id": work.id,
            "advanced": close.state == CommandState.COMPLETED,
            "state": (
                "closed" if close.state == CommandState.COMPLETED else close.state.value
            ),
            "source_sync_operation": sync.operation_id,
            "cleanup_operation": cleanup.operation_id,
            "close_operation": close.operation_id,
            "warden_release": released.to_dict(),
        }

    async def _send_review_finish_reminder(
        self, task: Any, facts: TaskFacts
    ) -> Mapping[str, Any]:
        fc = task.fc or {}
        review_operation = str(fc.get("review_operation") or "none")
        associated = fc.get("associated_beads")
        bead_id = (
            str(associated[0]) if isinstance(associated, list) and associated else None
        )
        request = ParsedRequest(
            command=("controller", "review_finish_reminder"),
            arguments={"task": task.id},
            input={"bead": bead_id},
            actor=ActorContext(kind="controller"),
            instance=self.request.instance,
            request_id=str(
                uuid.uuid5(
                    REMINDER_NAMESPACE,
                    f"review:{task.id}:{review_operation}:finish",
                )
            ),
            thread_id=facts.id,
            timeout=self.request.timeout,
            offline=True,
            runtime_submit=self._runtime_submit,
        )
        operation, reused = await asyncio.to_thread(
            self.ledger.create_operation,
            request,
            bead_id=bead_id,
            planned={
                "thread_id": facts.id,
                "review_operation": review_operation,
            },
            next_action="Send one scope-preserving finish reminder.",
        )
        if reused and operation.operation.get("state") in TERMINAL_OPERATION_STATES:
            external = operation.operation.get("external")
            return {
                "kind": "review_finish_reminder",
                "operation_id": operation.id,
                "turn_id": (
                    external.get("turn_id") if isinstance(external, Mapping) else None
                ),
                "reused": True,
            }
        try:
            turn = await self.runtime.start_turn(
                facts.id,
                TurnInput(
                    text=(
                        "The independent review turn ended without a review result. "
                        f"Submit it now with `fulcrum plan review finish --task {task.id} "
                        f"--thread-id {facts.id} --input - --json`, referencing review "
                        f"operation {review_operation}. Do not change or acquire the plan."
                    ),
                    cwd=facts.cwd or str(self.request.instance.instance_root),
                    workspace_roots=facts.workspace_roots
                    or (facts.cwd or str(self.request.instance.instance_root),),
                    model=str(fc.get("model")),
                    effort=str(fc.get("effort")),
                    operation_id=operation.id,
                    ownership_operation=None,
                ),
            )
        except AppServerError as error:
            state = "uncertain" if error.uncertain else "failed"
            operation = await asyncio.to_thread(
                self.ledger.update_operation,
                operation.id,
                state=state,
                step="review_finish_reminder_unresolved",
                error={
                    "code": error.category,
                    "message": str(error),
                    "retryable": error.category == "transient",
                },
                next_action="Request recovery without replaying an unresolved reminder.",
            )
            return {
                "kind": "review_finish_reminder",
                "operation_id": operation.id,
                "state": state,
            }
        operation = await asyncio.to_thread(
            self.ledger.update_operation,
            operation.id,
            state="completed",
            step="review_finish_reminder_started",
            external={"thread_id": facts.id, "turn_id": turn.id},
            result={"turn": turn.to_dict()},
            next_action="Wait for the one reminder turn to finish.",
        )
        return {
            "kind": "review_finish_reminder",
            "operation_id": operation.id,
            "turn_id": turn.id,
            "state": "completed",
        }

    async def _record_review_recovery(self, task: Any) -> Mapping[str, Any]:
        fc = task.fc or {}
        review_operation = str(fc.get("review_operation") or "none")
        associated = fc.get("associated_beads")
        bead_id = (
            str(associated[0]) if isinstance(associated, list) and associated else None
        )
        request = ParsedRequest(
            command=("controller", "recovery_request"),
            arguments={"task": task.id, "kind": "missing_review_finish"},
            input={"bead": bead_id},
            actor=ActorContext(kind="controller"),
            instance=self.request.instance,
            request_id=str(
                uuid.uuid5(
                    REMINDER_NAMESPACE,
                    f"review:{task.id}:{review_operation}:recovery",
                )
            ),
            timeout=self.request.timeout,
            offline=True,
        )
        operation, reused = await asyncio.to_thread(
            self.ledger.create_operation,
            request,
            bead_id=bead_id,
            planned={
                "task_record_id": task.id,
                "thread_id": fc.get("thread_id"),
                "review_operation": review_operation,
                "reason": "review reminder ended without typed findings",
            },
            next_action="Marshal must choose scoped review recovery.",
        )
        if not reused:
            operation = await asyncio.to_thread(
                self.ledger.update_operation,
                operation.id,
                state="completed",
                step="review_recovery_requested",
                next_action="Marshal must choose scoped review recovery.",
            )
            if bead_id is not None:
                root = await asyncio.to_thread(self.ledger.show, bead_id)
                if root is not None and root.fc:
                    root_fc = dict(root.fc)
                    plan = dict(root_fc.get("plan") or {})
                    reviews = dict(plan.get("reviews") or {})
                    perspective = str(fc.get("perspective") or "unknown")
                    row = dict(reviews.get(perspective) or {})
                    row["state"] = "recovery_required"
                    row["recovery_operation"] = operation.id
                    reviews[perspective] = row
                    plan["reviews"] = reviews
                    root_fc["plan"] = plan
                    await asyncio.to_thread(self.ledger.update_fc, root.id, root_fc)
        return {
            "kind": "review_recovery_request",
            "operation_id": operation.id,
            "state": operation.operation.get("state"),
        }

    async def _ordered_task_reconcile(
        self,
        task: Any,
        work_by_id: Mapping[str, Any],
        facts: TaskFacts | None,
    ) -> tuple[Mapping[str, Any], list[Mapping[str, Any]]]:
        fc = task.fc or {}
        key = str(fc.get("work_bead") or f"task:{task.id}")
        lock = self.bead_locks.setdefault(key, asyncio.Lock())
        async with lock:
            fresh = (
                task
                if fc.get("purpose") == "leadership"
                else await asyncio.to_thread(self.ledger.show, task.id)
            )
            return await self._reconcile_task(
                fresh if fresh is not None else task, work_by_id, facts
            )

    async def _send_task_notice(
        self, task: Any, facts: TaskFacts, work: Any, *, kind: str, text: str
    ) -> Mapping[str, Any]:
        fc = task.fc or {}
        acquisition = str(fc.get("ownership_operation") or "none")
        request_id = str(
            uuid.uuid5(REMINDER_NAMESPACE, f"{task.id}:{acquisition}:{kind}")
        )
        request = ParsedRequest(
            command=("controller", kind),
            arguments={"task": task.id},
            input={"text": text},
            actor=ActorContext(kind="task", task_id=facts.id),
            instance=self.request.instance,
            request_id=request_id,
            project=None,
            thread_id=facts.id,
            ownership_operation=acquisition,
            timeout=self.request.timeout,
            offline=True,
        )
        operation, reused = await asyncio.to_thread(
            self.ledger.create_operation,
            request,
            bead_id=work.id,
            planned={"thread_id": facts.id, "text": text, "kind": kind},
            next_action="Send the retained scope-preserving notice once.",
        )
        if reused and operation.operation.get("state") in TERMINAL_OPERATION_STATES:
            return {
                "kind": kind,
                "operation_id": operation.id,
                "reused": True,
            }
        try:
            turn = await self.runtime.start_turn(
                facts.id,
                TurnInput(
                    text=text,
                    cwd=facts.cwd or str(self.request.instance.instance_root),
                    workspace_roots=facts.workspace_roots
                    or (facts.cwd or str(self.request.instance.instance_root),),
                    model=str(fc.get("model")),
                    effort=str(fc.get("effort")),
                    operation_id=operation.id,
                    ownership_operation=acquisition,
                ),
            )
        except AppServerError as error:
            state = "uncertain" if error.uncertain else "failed"
            operation = await asyncio.to_thread(
                self.ledger.update_operation,
                operation,
                state=state,
                step="notice_send_unresolved",
                error={
                    "code": error.category,
                    "message": str(error),
                    "retryable": error.category == "transient",
                },
                next_action="Inspect the exact native task before any replay.",
            )
            return {
                "kind": kind,
                "operation_id": operation.id,
                "state": state,
            }
        operation = await asyncio.to_thread(
            self.ledger.update_operation,
            operation,
            state="completed",
            step="notice_turn_started",
            external={"thread_id": facts.id, "turn_id": turn.id},
            result={"turn": turn.to_dict()},
            next_action="Observe the notice turn; do not send another for this acquisition.",
        )
        return {
            "kind": kind,
            "operation_id": operation.id,
            "state": "completed",
        }

    async def _record_recovery_request(self, task: Any, work: Any) -> Mapping[str, Any]:
        fc = task.fc or {}
        acquisition = str(fc.get("ownership_operation") or "none")
        marshal_thread = _marshal_thread(self.ledger)
        request_id = str(
            uuid.uuid5(REMINDER_NAMESPACE, f"{task.id}:{acquisition}:recovery")
        )
        request = ParsedRequest(
            command=("controller", "recovery_request"),
            arguments={"task": task.id},
            input={"bead": work.id},
            actor=(
                ActorContext(kind="task", task_id=marshal_thread)
                if marshal_thread is not None
                else ActorContext(kind="human")
            ),
            instance=self.request.instance,
            request_id=request_id,
            thread_id=marshal_thread,
            timeout=self.request.timeout,
            offline=True,
        )
        operation, reused = await asyncio.to_thread(
            self.ledger.create_operation,
            request,
            bead_id=work.id,
            planned={
                "thread_id": _thread_id(fc),
                "ownership_operation": acquisition,
                "reason": "thirty minutes without substantive progress",
            },
            next_action="Marshal must make one recovery decision for current facts.",
        )
        if not reused:
            work_fc = dict(work.fc or {})
            work_fc["waiting"] = {
                "reason": "stalled without substantive progress",
                "reconsider_when": "Marshal recovery decision is recorded",
                "decision_operation": operation.id,
            }
            work_fc["last_transition"] = operation.id
            await asyncio.to_thread(self.ledger.update_fc, work.id, work_fc)
            operation = await asyncio.to_thread(
                self.ledger.update_operation,
                operation,
                state="completed",
                step="recovery_decision_requested",
                result={"bead_id": work.id, "task_record_id": task.id},
                next_action="Marshal must make one recovery decision for current facts.",
            )
        return {
            "kind": "recovery_request",
            "operation_id": operation.id,
            "state": operation.operation.get("state"),
        }

    async def _resource_pressure(
        self,
        tasks: Sequence[Any],
        work_by_id: Mapping[str, Any],
        facts: Mapping[str, TaskFacts],
    ) -> dict[str, Any]:
        resources = await self.runtime.resources()
        configured = _mapping(self.config["resources"])
        limit = resources.fd_soft_limit or int(configured["fd_soft_limit"])
        usage = resources.fd_usage
        ratio = usage / limit if usage is not None and limit > 0 else None
        paused = bool(resources.overloaded) or (
            ratio is not None and ratio >= float(configured["pause_ratio"])
        )
        released: list[str] = []
        if paused:
            for task in tasks:
                fc = task.fc or {}
                thread_id = _thread_id(fc)
                observed = facts.get(thread_id)
                work = work_by_id.get(str(fc.get("work_bead")))
                if (
                    observed is not None
                    and observed.active_turn is None
                    and fc.get("purpose") != "leadership"
                    and (work is None or work.status == "closed")
                ):
                    try:
                        release = await self.runtime.release(thread_id)
                    except AppServerError:
                        continue
                    released.append(thread_id)
                    current = await asyncio.to_thread(self.ledger.show, task.id)
                    if current is not None and current.fc:
                        task_fc = dict(current.fc)
                        task_fc["subscription_state"] = "released"
                        task_fc["subscription_release"] = release.to_dict()
                        await asyncio.to_thread(
                            self.ledger.update_fc, current.id, task_fc
                        )
        return {
            "paused": paused,
            "reason": "runtime resource pressure" if paused else None,
            "fd_usage": usage,
            "fd_limit": limit,
            "ratio": ratio,
            "released_idle_subscriptions": released,
        }

    async def _silence_inspection(self) -> None:
        records = await asyncio.to_thread(
            self.ledger.list_records, kind="task", limit=0
        )
        await self._inspect_tasks(records)

    async def _shutdown_cleanup(self) -> None:
        records = await asyncio.to_thread(
            self.ledger.list_records, kind="task", limit=0
        )
        unresolved: list[str] = []
        released: list[str] = []
        for task in records:
            fc = task.fc or {}
            thread_id = _thread_id(fc)
            facts = await self.runtime.inspect_task(thread_id)
            if facts.active_turn is not None:
                unresolved.append(thread_id)
                continue
            await self.runtime.release(thread_id)
            released.append(thread_id)
        flush_request = replace(
            self.request,
            command=("ledger", "sync"),
            arguments={},
            input={},
            request_id=str(
                uuid.uuid5(
                    PUBLICATION_SHUTDOWN_NAMESPACE,
                    _format_time(self.clock.now()),
                )
            ),
            offline=True,
        )
        publication_flush: Mapping[str, Any]
        try:
            flushed = await asyncio.wait_for(
                asyncio.to_thread(self.publication.flush, flush_request), 30
            )
            publication_flush = flushed.to_dict()
        except Exception as error:
            publication_flush = {
                "state": "failed",
                "local_work_preserved": True,
                "error": str(error),
            }
        self.health.success(
            "runner",
            {
                "shutdown": "drained",
                "unresolved_active_tasks": unresolved,
                "released_idle_subscriptions": released,
                "publication_flush": publication_flush,
            },
        )

    async def _record_runtime_event(self, event: Any) -> None:
        thread_id = event.params.get("threadId") or event.params.get("thread_id")
        if not isinstance(thread_id, str):
            return
        records = await asyncio.to_thread(
            self.ledger.list_records, kind="task", limit=0
        )
        matches = [
            record
            for record in records
            if record.fc and record.fc.get("thread_id") == thread_id
        ]
        if len(matches) != 1:
            return
        await asyncio.to_thread(
            self.analytics.record_runtime_event,
            self.ledger,
            matches[0],
            event.method,
            event.params,
        )
        current = await asyncio.to_thread(self.ledger.show, matches[0].id)
        fc = dict((current or matches[0]).fc or {})
        fc["last_runtime_event_at"] = event.observed_at
        fc["last_runtime_event"] = {
            "method": event.method,
            "observed_at": event.observed_at,
        }
        await asyncio.to_thread(self.ledger.update_fc, matches[0].id, fc)


def _launch_source_updater(definition_path: Path) -> dict[str, Any]:
    """Kick a separate launchd job so controller shutdown cannot kill the updater."""

    try:
        with definition_path.open("rb") as stream:
            definition = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException) as error:
        raise RuntimeError(
            f"source updater definition is unavailable: {error}"
        ) from error
    label = definition.get("Label") if isinstance(definition, Mapping) else None
    arguments = (
        definition.get("ProgramArguments") if isinstance(definition, Mapping) else None
    )
    if not isinstance(label, str) or not isinstance(arguments, list):
        raise RuntimeError("source updater definition is invalid")
    expected = tuple(str(item) for item in arguments)
    observed = inspect_service(label)
    domain = f"gui/{os.getuid()}"
    if observed.running:
        return {"label": label, "state": "coalesced", "pid": observed.pid}
    if observed.loaded and observed.program_arguments != expected:
        stopped = subprocess.run(
            ["launchctl", "bootout", f"{domain}/{label}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if stopped.returncode != 0:
            raise RuntimeError(stopped.stderr.strip() or stopped.stdout.strip())
        observed = inspect_service(label)
    if not observed.loaded:
        loaded = subprocess.run(
            ["launchctl", "bootstrap", domain, str(definition_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if loaded.returncode != 0:
            raise RuntimeError(loaded.stderr.strip() or loaded.stdout.strip())
    started = subprocess.run(
        ["launchctl", "kickstart", "-k", f"{domain}/{label}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if started.returncode != 0:
        raise RuntimeError(started.stderr.strip() or started.stdout.strip())
    current = inspect_service(label)
    return {"label": label, "state": "started", "pid": current.pid}


class ReconciliationService:
    def __init__(self, application: Any) -> None:
        self.application = application

    def reconcile(self, request: ParsedRequest) -> CommandResult:
        supervisor = ControllerSupervisor(request, self.application)
        summary = asyncio.run(
            supervisor.run_once(
                bead_id=_optional_string(request.arguments.get("bead")),
                operation_id=_optional_string(request.arguments.get("operation")),
            )
        )
        return CommandResult.query(summary.to_dict())


def _restore_request(
    template: ParsedRequest,
    command: str,
    accepted: Mapping[str, Any],
    request_id: str | None,
) -> ParsedRequest:
    actor_value = accepted.get("actor")
    actor = (
        ActorContext(
            kind=str(actor_value.get("kind")), task_id=actor_value.get("task_id")
        )
        if isinstance(actor_value, Mapping)
        else ActorContext(kind="human")
    )
    arguments = accepted.get("arguments")
    supplied_input = accepted.get("input")
    return ParsedRequest(
        command=tuple(command.split(".")),
        arguments=dict(arguments) if isinstance(arguments, Mapping) else {},
        input=dict(supplied_input) if isinstance(supplied_input, Mapping) else {},
        actor=actor,
        instance=template.instance,
        request_id=request_id,
        project=_optional_string(accepted.get("project")),
        thread_id=_optional_string(accepted.get("thread_id")),
        ownership_operation=_optional_string(accepted.get("ownership_operation")),
        timeout=template.timeout,
        offline=True,
        runtime_submit=template.runtime_submit,
    )


def _thread_id(fc: Mapping[str, Any]) -> str:
    value = fc.get("thread_id")
    if not isinstance(value, str) or not value:
        raise FulcrumError(
            "TASK_CORRUPT", "managed task has no native thread ID", exit_code=4
        )
    return value


def _marshal_thread(ledger: Ledger) -> str | None:
    control = ledger.show("fc-system")
    value = control.fc.get("marshal_thread") if control and control.fc else None
    return str(value) if isinstance(value, str) and value else None


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FulcrumError(
            "CONFIG_INVALID",
            "effective controller configuration is invalid",
            exit_code=4,
        )
    return value


def _optional_string(value: Any) -> str | None:
    return str(value) if value is not None else None


def _delivery_request_id(finish_operation: str, step: str) -> str:
    return str(uuid.uuid5(DELIVERY_NAMESPACE, f"{finish_operation}:{step}"))


def _durably_deferred(value: Any) -> bool:
    reasons = value.get("reasons", []) if isinstance(value, Mapping) else []
    return any(
        isinstance(reason, Mapping)
        and reason.get("kind")
        in {"future_activation", "defer", "external", "authoring"}
        for reason in reasons
    )


def _optional_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _parse_time(value)
    except ValueError:
        return None


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
