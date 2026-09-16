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


from fulcrum.analytics import AnalyticsService
from fulcrum.configuration import ConfigurationManager
from fulcrum.coordination import operation_lock, transition
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
from fulcrum.diagnostics import DiagnosticLog
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

TERMINAL_OPERATION_STATES = {"completed", "failed", "cancelled"}
RETRY_DELAYS = (2.0, 10.0)
EXTERNAL_RUNNER_LIMIT = 32
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
        active_episode = previous.get("failure_episode")
        last_episode = previous.get("last_failure_episode")
        if isinstance(active_episode, Mapping):
            last_episode = {
                **dict(active_episode),
                "recovered_at": now,
            }
        self.values[name] = {
            **previous,
            "state": "healthy",
            "last_attempt_at": now,
            "last_success_at": now,
            "consecutive_failures": 0,
            "error": None,
            "failure_episode": None,
            "last_failure_episode": last_episode,
            "evidence": dict(evidence),
            "pid": os.getpid(),
        }
        self._write()

    def failure(self, name: str, error: BaseException) -> int:
        now = _format_time(self.clock.now())
        previous = self.values.get(name, {})
        failures = int(previous.get("consecutive_failures", 0)) + 1
        prior_episode = previous.get("failure_episode")
        episode = dict(prior_episode) if isinstance(prior_episode, Mapping) else {}
        episode.update(
            {
                "first_seen_at": episode.get("first_seen_at") or now,
                "last_seen_at": now,
                "count": int(episode.get("count", 0)) + 1,
                "error_category": type(error).__name__,
                "error": str(error),
            }
        )
        self.values[name] = {
            **previous,
            "state": "unavailable",
            "last_attempt_at": now,
            "consecutive_failures": failures,
            "error": str(error),
            "failure_episode": episode,
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
        else:
            self.clock = SystemClock()
        if runtime is not None:
            self.runtime = runtime
        else:
            from fulcrum.resident_client import ResidentTransport

            self.runtime = AppServerRuntime(
                endpoint,
                transport=ResidentTransport(
                    request.instance.instance_root / "resident.sock"
                ),
            )
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
        pass_id = str(uuid.uuid4())
        started = self.clock.monotonic()
        self._record_reconciliation(
            {
                "event": "reconciliation_started",
                "pass_id": pass_id,
                "bead_id": bead_id,
                "operation_id": operation_id,
                "trigger": self.request.command_name,
            }
        )
        try:
            async with self.reconcile_lock:
                summary = await self._run_once(
                    bead_id=bead_id,
                    operation_id=operation_id,
                    keep_runtime=keep_runtime,
                    pass_id=pass_id,
                )
        except BaseException as error:
            self._record_reconciliation(
                {
                    "event": "reconciliation_failed",
                    "pass_id": pass_id,
                    "bead_id": bead_id,
                    "operation_id": operation_id,
                    "duration_ms": int((self.clock.monotonic() - started) * 1000),
                    "error_category": type(error).__name__,
                    "error": str(error),
                }
            )
            raise
        self._record_reconciliation(
            {
                "event": "reconciliation_completed",
                "pass_id": pass_id,
                "bead_id": bead_id,
                "operation_id": operation_id,
                "duration_ms": int((self.clock.monotonic() - started) * 1000),
                "counts": {
                    "intake": len(summary.intake),
                    "operations": len(summary.operations),
                    "work": len(summary.work),
                    "tasks": len(summary.tasks),
                    "actions": len(summary.next_actions),
                    "gaps": len(summary.gaps),
                },
                "actions": list(summary.next_actions),
                "gaps": list(summary.gaps),
                "pressure": dict(summary.pressure),
                "associated_beads": sorted(
                    {
                        str(value)
                        for row in tuple(summary.work) + tuple(summary.next_actions)
                        if isinstance(row, Mapping)
                        for value in (row.get("bead_id"),)
                        if value
                    }
                ),
            }
        )
        return summary

    def _record_reconciliation(self, event: Mapping[str, Any]) -> None:
        try:
            DiagnosticLog.from_request(self.request).append(event)
        except (OSError, FulcrumError):
            pass

    async def _run_once(
        self,
        *,
        bead_id: str | None = None,
        operation_id: str | None = None,
        keep_runtime: bool = False,
        pass_id: str | None = None,
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
        resumed: list[Mapping[str, Any]] = []
        operation_outcomes = await asyncio.gather(
            *(self._ordered_reconcile(operation) for operation in ordered_operations),
            return_exceptions=True,
        )
        for operation, outcome in zip(ordered_operations, operation_outcomes):
            if isinstance(outcome, BaseException):
                if not isinstance(outcome, Exception):
                    raise outcome
                operation_bead = operation.operation.get("bead_id")
                failure = {
                    "kind": "operation_reconciliation_failure",
                    "operation_id": operation.id,
                    "bead_id": operation_bead,
                    "reason": str(outcome),
                    "effect": "isolated; unrelated work continued",
                }
                gap = {
                    "component": "operation_reconciliation",
                    "availability": "degraded",
                    "operation_id": operation.id,
                    "bead_id": operation_bead,
                    "reason": str(outcome),
                }
                actions.append(failure)
                gaps.append(gap)
                self.health.failure("reconciliation", outcome)
                self._record_reconciliation(
                    {
                        "event": "operation_reconciliation_failed",
                        "pass_id": pass_id,
                        "operation_id": operation.id,
                        "bead_id": operation_bead,
                        "associated_beads": (
                            [operation_bead] if operation_bead else []
                        ),
                        "reason": str(outcome),
                        "outcome": "isolated",
                    }
                )
                continue
            resumed.append(outcome)
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
            try:
                refreshed, delivery_action = await self._reconcile_work_delivery(work)
            except Exception as error:
                actions.append(
                    {
                        "kind": "work_delivery_reconciliation_failure",
                        "bead_id": work.id,
                        "reason": str(error),
                        "effect": "isolated; unrelated work continued",
                    }
                )
                gaps.append(
                    {
                        "component": "work_delivery_reconciliation",
                        "availability": "degraded",
                        "bead_id": work.id,
                        "reason": str(error),
                    }
                )
                self.health.failure("reconciliation", error)
                self._record_reconciliation(
                    {
                        "event": "work_delivery_reconciliation_failed",
                        "pass_id": pass_id,
                        "bead_id": work.id,
                        "associated_beads": [work.id],
                        "reason": str(error),
                        "outcome": "isolated",
                    }
                )
                continue
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
            pending_tasks: list[Any] = []
            for task in tasks:
                suppression = self._task_reconciliation_suppression(task)
                if suppression is None:
                    pending_tasks.append(task)
                    continue
                row, action, gap = self._suppressed_task_reconciliation(
                    task, suppression
                )
                task_rows.append(row)
                actions.append(action)
                gaps.append(gap)
            reconciled_tasks = await asyncio.gather(
                *(
                    self._ordered_task_reconcile(
                        task,
                        work_by_id,
                        facts.get(_thread_id(task.fc or {})),
                    )
                    for task in pending_tasks
                ),
                return_exceptions=True,
            )
            isolated_failures = 0
            for task, outcome in zip(pending_tasks, reconciled_tasks):
                if isinstance(outcome, BaseException):
                    if not isinstance(outcome, Exception):
                        raise outcome
                    isolated_failures += 1
                    action, gap = await self._retain_task_reconciliation_failure(
                        task, outcome, pass_id=pass_id
                    )
                    task_rows.append(
                        {
                            "task_record_id": task.id,
                            "thread_id": _thread_id(task.fc or {}),
                            "work_bead": (task.fc or {}).get("work_bead"),
                            "observed": facts.get(_thread_id(task.fc or {}))
                            is not None,
                            "reconciliation": "isolated_failure",
                        }
                    )
                    actions.append(action)
                    gaps.append(gap)
                    self.health.failure("reconciliation", outcome)
                    continue
                task_row, task_actions = outcome
                task_rows.append(task_row)
                actions.extend(task_actions)
                await self._clear_task_reconciliation_failure(task)
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
                    "isolated_failures": isolated_failures,
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
        candidate = dict(delivery)
        for key in (
            "source_oid",
            "provider_handle",
            "validation",
            "promotion",
            "synchronization",
            "cleanup",
            "evidence",
            "gaps",
        ):
            if key in observed:
                candidate[key] = observed[key]
        candidate["observed_at"] = observed.get("observed_at")
        changed = _stable_delivery(candidate) != _stable_delivery(delivery)
        if not changed:
            return work, None
        fc["delivery"] = candidate
        updated = await asyncio.to_thread(self.ledger.update_fc, work.id, fc)
        return updated, {
            "kind": "delivery_observation",
            "bead_id": work.id,
            "advanced": True,
            "validation": _delivery_state(observed, "validation"),
            "promotion": _delivery_state(observed, "promotion"),
            "source_oid": observed.get("source_oid"),
            "provider_handle": observed.get("provider_handle"),
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
            return {
                "kind": "marshal_decision",
                "started": False,
                "reason": "leadership control unavailable; HUMAN review required",
            }
        marshal_thread = _optional_string(control.fc.get("marshal_thread"))
        if marshal_thread is None:
            return {
                "kind": "marshal_decision",
                "started": False,
                "reason": "no standing Marshal; HUMAN review required",
            }
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
                and record.fc.get("state") in {"accepted", "running", "uncertain"}
            ),
            None,
        )
        if outstanding is not None:
            return {
                "kind": "marshal_decision",
                "started": False,
                "operation_id": outstanding.id,
                "reason": "one Marshal decision is outstanding or requires inspection",
            }
        now = self.clock.now()
        selected_ids = [row["bead_id"] for row in brief["rows"]]
        if (
            not isinstance(pending, Mapping)
            or pending.get("kind") != brief["kind"]
            or pending.get("selected_ids") != selected_ids
        ):
            pending = {
                "kind": brief["kind"],
                "first_seen_at": _format_time(now),
                "selected_ids": selected_ids,
            }
            fc["pending_decision"] = pending
            await asyncio.to_thread(self.ledger.update_fc, control.id, fc)
        retry_at = pending.get("retry_at")
        if isinstance(retry_at, str) and now < _parse_time(retry_at):
            return {
                "kind": "marshal_decision",
                "started": False,
                "reason": "waiting to retry unavailable leadership",
                "eligible_at": retry_at,
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
                    f"automatic:{brief['kind']}:{pending['first_seen_at']}:{pending.get('attempt', 0)}",
                )
            ),
            thread_id=marshal_thread,
            timeout=self.request.timeout,
            runtime_submit=self._runtime_submit,
        )
        async with self.external_slots:
            result = await asyncio.to_thread(self.application.dispatch, request)
        latest = await asyncio.to_thread(self.ledger.show, "fc-system")
        if (
            result.ok
            and result.state in {CommandState.RUNNING, CommandState.COMPLETED}
            and latest is not None
            and latest.fc
        ):
            latest_fc = dict(latest.fc)
            latest_fc["pending_decision"] = None
            await asyncio.to_thread(self.ledger.update_fc, latest.id, latest_fc)
        elif latest is not None and latest.fc and result.state is CommandState.FAILED:
            latest_fc = dict(latest.fc)
            latest_fc["pending_decision"] = {
                **dict(pending),
                "attempt": int(pending.get("attempt", 0)) + 1,
                "retry_at": _format_time(
                    now
                    + timedelta(
                        seconds=min(
                            60.0,
                            float(_mapping(config["timing"])["reconcile_seconds"])
                            * (int(pending.get("attempt", 0)) + 1),
                        )
                    )
                ),
            }
            await asyncio.to_thread(self.ledger.update_fc, latest.id, latest_fc)
        return {
            "kind": "marshal_decision",
            "started": result.ok
            and result.state in {CommandState.RUNNING, CommandState.COMPLETED},
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
        attempts: int = int(operation.operation.get("attempts") or 0)
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
        try:
            request: ParsedRequest = _restore_request(
                self.request,
                command,
                accepted,
                _optional_string(operation.operation.get("request_id")),
            )

            def execute() -> CommandResult:
                assert request.request_id is not None
                with operation_lock(self.ledger.workspace, request.request_id):
                    with transition(self.ledger.workspace):
                        self.ledger.update_operation(operation, attempts=attempts + 1)
                    return self.application.dispatch(request)

            async with self.external_slots:
                result = await asyncio.to_thread(execute)
            return {
                **operation_view(operation),
                "advanced": True,
                "result_state": result.state.value,
            }
        except FulcrumError as error:
            if error.code == "OPERATION_BUSY":
                return {
                    **operation_view(operation),
                    "advanced": False,
                    "in_flight": True,
                }
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
        attempts: int = int(operation.operation.get("attempts") or 0)
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
        retained_facts = facts.to_dict()
        retained_turn = retained_facts.get("last_turn")
        now = self.clock.now()
        if (
            facts.active_turn is not None
            and facts.last_turn is not None
            and retained_turn != previous_turn
        ):
            fc["last_tool_evidence_at"] = _format_time(now)
        fc["last_observed"] = retained_facts
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
            actor=ActorContext(kind="controller"),
            instance=self.request.instance,
            request_id=None,
            thread_id=None,
            ownership_operation=str(work.fc.get("ownership_operation")),
            timeout=self.request.timeout,
            runtime_submit=self._runtime_submit,
        )
        try:
            observed = await asyncio.to_thread(self.application.dispatch, base)
        except FulcrumError as error:
            if error.code == "DELIVERY_NOT_STARTED":
                child = await asyncio.to_thread(
                    _retained_finish_child,
                    self.ledger,
                    finish_operation,
                    "validation",
                )
                return await self._return_warden_to_review(
                    work,
                    delivery_finish,
                    reason="validation_delivery_absent",
                    evidence={"error": error.to_result().to_dict(), "child": child},
                )
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
            _delivery_state(delivery, "validation") == "failed"
            or _delivery_state(delivery, "promotion") == "failed"
        ):
            current = await asyncio.to_thread(self.ledger.show, work.id)
            if current is not None and current.fc:
                fc = dict(current.fc)
                failed_finishes = list(fc.get("failed_delivery_finishes") or [])
                failed_finishes.append(
                    {
                        **dict(delivery_finish),
                        "provider": dict(delivery),
                        "failed_at": _format_time(self.clock.now()),
                    }
                )
                fc["failed_delivery_finishes"] = failed_finishes[-10:]
                fc.pop("delivery_finish", None)
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
        if _delivery_state(delivery, "validation") != "passed":
            return {
                "kind": "warden_delivery",
                "bead_id": work.id,
                "advanced": False,
                "state": "validation_pending",
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
        retained_release = (task.fc or {}).get("subscription_release")
        if (task.fc or {}).get("subscription_state") == "released" and isinstance(
            retained_release, Mapping
        ):
            release_result = dict(retained_release)
        else:
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
            release_result = released.to_dict()
        current_task = await asyncio.to_thread(self.ledger.show, task.id)
        if current_task is not None and current_task.fc:
            task_fc = dict(current_task.fc)
            task_fc["subscription_state"] = "released"
            task_fc["subscription_release"] = release_result
            await asyncio.to_thread(self.ledger.update_fc, current_task.id, task_fc)
        source_oid = str(delivery_finish.get("source_oid") or "")
        retained_delivery = (work.fc or {}).get("delivery")
        approved = (
            retained_delivery.get("approved_source")
            if isinstance(retained_delivery, Mapping)
            else None
        )
        if not isinstance(approved, Mapping) or approved.get("oid") != source_oid:
            try:
                approval = await asyncio.to_thread(
                    self.application.dispatch,
                    replace(
                        base,
                        command=("review", "approve"),
                        arguments={
                            "bead": work.id,
                            "source": source_oid,
                            "summary": str(
                                delivery_finish.get("summary") or "Warden approved"
                            ),
                        },
                        request_id=_delivery_request_id(
                            finish_operation, "controller-approval"
                        ),
                    ),
                )
            except FulcrumError as error:
                if error.code == "STALE_SOURCE":
                    return await self._return_warden_to_review(
                        work,
                        delivery_finish,
                        reason="workspace_source_changed",
                        evidence={
                            "approval": error.to_result().to_dict(),
                            "retained_delivery": dict(retained_delivery or {}),
                        },
                    )
                return {
                    "kind": "warden_delivery",
                    "bead_id": work.id,
                    "advanced": False,
                    "state": "approval_unresolved",
                    "code": error.code,
                    "reason": error.message,
                }
            if approval.state is not CommandState.COMPLETED:
                if _command_error_code(approval) == "STALE_SOURCE":
                    return await self._return_warden_to_review(
                        work,
                        delivery_finish,
                        reason="workspace_source_changed",
                        evidence={"approval": dict(approval.result or {})},
                    )
                return {
                    "kind": "warden_delivery",
                    "bead_id": work.id,
                    "advanced": False,
                    "state": "approval_unresolved",
                    "operation_id": approval.operation_id,
                    "result_state": approval.state.value,
                }
        if _delivery_state(delivery, "promotion") != "observed":
            try:
                promotion = await asyncio.to_thread(
                    self.application.dispatch,
                    replace(
                        base,
                        command=("promotion", "start"),
                        arguments={"bead": work.id, "source": source_oid},
                        request_id=_delivery_request_id(
                            finish_operation, "controller-promotion"
                        ),
                    ),
                )
            except FulcrumError as error:
                if error.code == "STALE_SOURCE":
                    return await self._return_warden_to_review(
                        work,
                        delivery_finish,
                        reason="workspace_source_changed",
                        evidence={
                            "promotion": error.to_result().to_dict(),
                            "retained_delivery": dict(retained_delivery or {}),
                        },
                    )
                return {
                    "kind": "warden_delivery",
                    "bead_id": work.id,
                    "advanced": False,
                    "state": "promotion_unresolved",
                    "code": error.code,
                    "reason": error.message,
                }
            if promotion.state is not CommandState.COMPLETED:
                if _command_error_code(promotion) == "STALE_SOURCE":
                    return await self._return_warden_to_review(
                        work,
                        delivery_finish,
                        reason="workspace_source_changed",
                        evidence={"promotion": dict(promotion.result or {})},
                    )
                return {
                    "kind": "warden_delivery",
                    "bead_id": work.id,
                    "advanced": False,
                    "state": "promotion_unresolved",
                    "operation_id": promotion.operation_id,
                    "result_state": promotion.state.value,
                }
            observed = await asyncio.to_thread(self.application.dispatch, base)
            result = observed.result or {}
            delivery = result.get("delivery") if isinstance(result, Mapping) else None
            if (
                not isinstance(delivery, Mapping)
                or _delivery_state(delivery, "promotion") != "observed"
            ):
                return {
                    "kind": "warden_delivery",
                    "bead_id": work.id,
                    "advanced": True,
                    "state": "promotion_pending",
                    "operation_id": promotion.operation_id,
                    "delivery": dict(delivery or {}),
                }
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
            "warden_release": release_result,
        }

    async def _return_warden_to_review(
        self,
        work: Any,
        delivery_finish: Mapping[str, Any],
        *,
        reason: str,
        evidence: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        current = await asyncio.to_thread(self.ledger.show, work.id)
        if current is not None and current.fc:
            fc = dict(current.fc)
            retained = fc.get("delivery_finish")
            sealed = (
                dict(retained)
                if isinstance(retained, Mapping)
                else dict(delivery_finish)
            )
            history = list(fc.get("failed_delivery_finishes") or [])
            history.append(
                {
                    **sealed,
                    "state": "superseded",
                    "reason": reason,
                    "evidence": dict(evidence),
                    "failed_at": _format_time(self.clock.now()),
                }
            )
            fc["failed_delivery_finishes"] = history[-10:]
            fc.pop("delivery_finish", None)
            delivery = fc.get("delivery")
            if isinstance(delivery, Mapping):
                invalidated = dict(delivery)
                invalidated["approved_source"] = None
                invalidated["approval_invalidated_by"] = sealed.get("operation_id")
                fc["delivery"] = invalidated
            fc["phase"] = "reviewing"
            fc["next_action"] = (
                "Warden must inspect the retained validation/source evidence, repair "
                "the same workspace, and submit a new exact-source judgment."
            )
            fc["last_transition"] = sealed.get("operation_id")
            await asyncio.to_thread(self.ledger.update_fc, current.id, fc)
        return {
            "kind": "warden_delivery",
            "bead_id": work.id,
            "advanced": True,
            "state": "returned_to_review",
            "reason": reason,
            "evidence": dict(evidence),
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

    def _task_reconciliation_suppression(self, task: Any) -> Mapping[str, Any] | None:
        failure = (task.fc or {}).get("reconciliation_failure")
        if not isinstance(failure, Mapping):
            return None
        if failure.get("ownership_operation") != (task.fc or {}).get(
            "ownership_operation"
        ):
            return None
        next_retry_at = failure.get("next_retry_at")
        if not isinstance(next_retry_at, str):
            return None
        try:
            return failure if self.clock.now() < _parse_time(next_retry_at) else None
        except ValueError:
            return None

    def _suppressed_task_reconciliation(
        self, task: Any, failure: Mapping[str, Any]
    ) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
        bead_id = (task.fc or {}).get("work_bead")
        row = {
            "task_record_id": task.id,
            "thread_id": (task.fc or {}).get("thread_id"),
            "work_bead": bead_id,
            "observed": False,
            "reconciliation": "backed_off",
        }
        action = {
            "kind": "task_reconciliation_suppressed",
            "task_record_id": task.id,
            "bead_id": bead_id,
            "attempts": failure.get("attempts"),
            "next_retry_at": failure.get("next_retry_at"),
        }
        gap = {
            "component": "task_reconciliation",
            "availability": "degraded",
            "task_record_id": task.id,
            "bead_id": bead_id,
            "reason": failure.get("error"),
            "code": failure.get("code"),
            "retry_at": failure.get("next_retry_at"),
        }
        return row, action, gap

    async def _retain_task_reconciliation_failure(
        self, task: Any, error: Exception, *, pass_id: str | None
    ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        current = await asyncio.to_thread(self.ledger.show, task.id) or task
        fc = dict(current.fc or {})
        previous = fc.get("reconciliation_failure")
        code = error.code if isinstance(error, FulcrumError) else type(error).__name__
        message = error.message if isinstance(error, FulcrumError) else str(error)
        signature = f"{code}:{message}"[:1000]
        acquisition = fc.get("ownership_operation")
        same_episode = (
            isinstance(previous, Mapping)
            and previous.get("signature") == signature
            and previous.get("ownership_operation") == acquisition
        )
        attempts = int(previous.get("attempts", 0)) + 1 if same_episode else 1
        delays = (15, 30, 60, 120, 300)
        delay = delays[min(attempts - 1, len(delays) - 1)]
        now = self.clock.now()
        retained = {
            "signature": signature,
            "code": code,
            "error": message,
            "attempts": attempts,
            "first_seen_at": (
                previous.get("first_seen_at")
                if same_episode and isinstance(previous, Mapping)
                else _format_time(now)
            ),
            "last_seen_at": _format_time(now),
            "next_retry_at": _format_time(now + timedelta(seconds=delay)),
            "ownership_operation": acquisition,
            "pass_id": pass_id,
        }
        fc["reconciliation_failure"] = retained
        await asyncio.to_thread(self.ledger.update_fc, current.id, fc)
        bead_id = fc.get("work_bead")
        event = {
            "event": "task_reconciliation_failed",
            "pass_id": pass_id,
            "task_id": fc.get("thread_id"),
            "task_record_id": current.id,
            "bead_id": bead_id,
            "associated_beads": [bead_id] if bead_id else [],
            "error_category": type(error).__name__,
            "code": code,
            "reason": message,
            "attempts": attempts,
            "next_retry_at": retained["next_retry_at"],
            "outcome": "isolated",
        }
        self._record_reconciliation(event)
        action = {
            "kind": "task_reconciliation_failure",
            "task_record_id": current.id,
            "bead_id": bead_id,
            "code": code,
            "reason": message,
            "attempts": attempts,
            "next_retry_at": retained["next_retry_at"],
            "effect": "isolated; unrelated work continued",
        }
        gap = {
            "component": "task_reconciliation",
            "availability": "degraded",
            "task_record_id": current.id,
            "bead_id": bead_id,
            "code": code,
            "reason": message,
            "retry_at": retained["next_retry_at"],
        }
        return action, gap

    async def _clear_task_reconciliation_failure(self, task: Any) -> None:
        if not isinstance((task.fc or {}).get("reconciliation_failure"), Mapping):
            return
        current = await asyncio.to_thread(self.ledger.show, task.id)
        if current is None or not current.fc:
            return
        fc = dict(current.fc)
        failure = fc.pop("reconciliation_failure", None)
        if isinstance(failure, Mapping):
            fc["last_reconciliation_failure"] = {
                **dict(failure),
                "recovered_at": _format_time(self.clock.now()),
            }
            await asyncio.to_thread(self.ledger.update_fc, current.id, fc)

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


def _command_error_code(result: CommandResult) -> str | None:
    payload = result.result if isinstance(result.result, Mapping) else {}
    error = payload.get("error") if isinstance(payload, Mapping) else None
    return (
        str(error.get("code"))
        if isinstance(error, Mapping) and error.get("code")
        else None
    )


def _delivery_state(delivery: Mapping[str, Any], field: str) -> str | None:
    value = delivery.get(field)
    if isinstance(value, Mapping):
        state = value.get("state")
    else:
        state = value
    if not isinstance(state, str):
        return None
    if field == "promotion" and state == "promoted":
        return "observed"
    if field in {"synchronization", "cleanup"} and state == "complete":
        return "observed"
    return state


def _stable_delivery(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _stable_delivery(item)
            for key, item in value.items()
            if key != "observed_at"
        }
    if isinstance(value, list):
        return [_stable_delivery(item) for item in value]
    return value


def _retained_finish_child(
    ledger: Ledger, finish_operation: str, purpose: str
) -> Mapping[str, Any] | None:
    record = ledger.show(finish_operation)
    if record is None or record.kind != "operation":
        return None
    operation = OperationRecord.from_record(record)
    child: Mapping[str, Any] | None = None
    for container_name in ("planned", "result"):
        container = operation.operation.get(container_name)
        children = container.get("children") if isinstance(container, Mapping) else None
        candidate = children.get(purpose) if isinstance(children, Mapping) else None
        if isinstance(candidate, Mapping):
            child = candidate
            break
    child_id = child.get("operation_id") if child is not None else None
    retained = ledger.show(str(child_id)) if child_id else None
    if retained is not None and retained.kind == "operation":
        return operation_view(OperationRecord.from_record(retained))
    return dict(child) if child is not None else None


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
