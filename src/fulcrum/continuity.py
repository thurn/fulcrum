"""Archive-once lifecycle and crash-resumable managed task replacement."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fulcrum.configuration import ConfigurationManager
from fulcrum.contracts import (
    ActorContext,
    CommandResult,
    CommandState,
    FulcrumError,
    ParsedRequest,
)
from fulcrum.ledger import (
    Ledger,
    LedgerRecord,
    OperationRecord,
    operation_id,
    operation_view,
    random_record_id,
    utc_now,
)
from fulcrum.runtime import AppServerError, TaskFacts, TaskSpec
from fulcrum.runtime_service import _create_and_configure, _runtime_call

ARCHIVE_NAMESPACE = uuid.UUID("bb56e1b3-521f-4b93-b85a-5d3d774fca01")
EXACT_REPLACEMENT_NAMESPACE = uuid.UUID("4311c857-b591-4c86-a9cb-ad42f512a0fb")
TERMINAL_OPERATION_STATES = {"completed", "failed", "uncertain", "cancelled"}
REPLACEMENT_STAGES = {
    "selected": 0,
    "stopped": 1,
    "successor_recorded": 2,
    "transferred": 3,
}


class ContinuityService:
    def leader_replace(self, request: ParsedRequest) -> CommandResult:
        role = str(request.arguments["role"])
        return self._replace(request, leader_role=role)

    def fleet_replace(self, request: ParsedRequest) -> CommandResult:
        return self._replace(request, leader_role=None)

    def _replace(
        self, request: ParsedRequest, *, leader_role: str | None
    ) -> CommandResult:
        _authorize(request)
        ledger = _ledger(request)
        mode = str(request.arguments.get("mode") or "drain")
        if mode not in {"drain", "interrupt"}:
            raise FulcrumError.invalid(
                "INVALID_MODE", "mode must be drain or interrupt"
            )
        reason = request.arguments.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise FulcrumError.invalid(
                "INVALID_INPUT", "replacement reason is required"
            )
        project = request.project
        if leader_role is not None:
            if leader_role not in {"vizier", "marshal"}:
                raise FulcrumError.invalid("INVALID_ROLE", "unknown leadership role")
            if project is not None:
                raise FulcrumError.invalid(
                    "INVALID_SCOPE",
                    "standing leadership replacement is instance-scoped",
                )
        selected = _select_tasks(ledger, project=project, leader_role=leader_role)
        if leader_role is not None and not selected:
            raise FulcrumError.invalid(
                "LEADER_NOT_FOUND", f"standing {leader_role} task is unavailable"
            )
        control = _control(ledger)
        active_recovery = (control.fc or {}).get("active_takeover")
        if isinstance(active_recovery, Mapping) and active_recovery.get("state") in {
            "acquiring",
            "stopping",
            "active",
            "repairing",
            "failed",
        }:
            raise FulcrumError(
                "RECOVERY_SCOPE_CONFLICT",
                "fleet replacement cannot overlap an active recovery fence",
                exit_code=5,
            )
        if request.request_id is None:
            raise FulcrumError.invalid(
                "REQUEST_ID_REQUIRED", "replacement requires a request ID"
            )
        active_replacement = (control.fc or {}).get("fleet_replacement")
        expected_operation = operation_id(request.request_id)
        if (
            isinstance(active_replacement, Mapping)
            and active_replacement.get("state") == "active"
            and active_replacement.get("operation_id") != expected_operation
        ):
            raise FulcrumError(
                "REPLACEMENT_CONFLICT",
                "another fleet replacement is active",
                exit_code=5,
            )
        mapping = {
            task.id: {
                "old_task_record_id": task.id,
                "old_thread_id": _thread_id(task),
                "new_task_record_id": random_record_id(),
                "new_thread_id": None,
                "creation_cwd": None,
                "stage": "selected",
                "stop_evidence": None,
                "transfers": [],
                "pending_transfers": _owned_work_ids(ledger, task),
                "archive": None,
            }
            for task in selected
        }
        inventory = [_task_snapshot(task) for task in selected]
        operation, reused = ledger.create_operation(
            request,
            planned={
                "mode": mode,
                "reason": reason,
                "project": project,
                "leader_role": leader_role,
                "selected_task_ids": [task.id for task in selected],
                "inventory": inventory,
                "mapping": mapping,
            },
            next_action="Pause selected admission and observe every old writer before creating successors.",
        )
        if reused and operation.operation.get("state") in TERMINAL_OPERATION_STATES:
            return _operation_result(operation)
        retained = operation.operation.get("planned")
        if not isinstance(retained, Mapping):
            raise FulcrumError(
                "OPERATION_CORRUPT",
                "replacement receipt has no retained plan",
                exit_code=4,
            )
        planned = dict(retained)
        mapping_value = planned.get("mapping")
        if not isinstance(mapping_value, Mapping):
            raise FulcrumError(
                "OPERATION_CORRUPT",
                "replacement receipt has no task mapping",
                exit_code=4,
            )
        mapping = {str(key): dict(value) for key, value in mapping_value.items()}
        operation = _retain_replacement(ledger, operation, planned, mapping)
        pending: list[dict[str, Any]] = []
        fatal: list[dict[str, Any]] = []
        for old_id in planned.get("selected_task_ids", []):
            old = ledger.show(str(old_id))
            entry = mapping.get(str(old_id))
            if old is None or old.kind != "task" or not old.fc or entry is None:
                pending.append(
                    {
                        "task_record_id": old_id,
                        "reason": "retained task record is unavailable",
                    }
                )
                continue
            try:
                operation, entry = self._advance_one(
                    request, ledger, operation, planned, mapping, old, entry, mode
                )
                mapping[old.id] = entry
            except FulcrumError as error:
                row = {
                    "task_record_id": old.id,
                    "thread_id": _thread_id(old),
                    "code": error.code,
                    "reason": error.message,
                }
                if error.code in {
                    "REPLACEMENT_DRAINING",
                    "RUNTIME_UNAVAILABLE",
                    "RUNTIME_UNCERTAIN",
                }:
                    pending.append(row)
                else:
                    fatal.append(row)
        planned["mapping"] = mapping
        if fatal:
            operation = ledger.update_operation(
                operation,
                state="failed",
                step="replacement_failed_with_scope_paused",
                planned=planned,
                error={"code": "REPLACEMENT_FAILED", "tasks": fatal},
                result={
                    "selected_task_ids": list(planned.get("selected_task_ids", [])),
                    "mapping": mapping,
                    "pending": [*pending, *fatal],
                    "admission_paused": True,
                    "mode": mode,
                },
                next_action="Inspect the retained mapping under recovery authority; selected admission remains paused.",
            )
            return _operation_result(operation)
        if pending or any(
            entry.get("stage") != "transferred" for entry in mapping.values()
        ):
            operation = ledger.update_operation(
                operation,
                state="accepted",
                step="replacement_waiting_for_old_writers",
                planned=planned,
                result={
                    "selected_task_ids": list(planned.get("selected_task_ids", [])),
                    "mapping": mapping,
                    "pending": pending,
                    "admission_paused": True,
                    "mode": mode,
                },
                next_action=(
                    "Wait for the exact managed turns and tools to stop; drain never escalates to interrupt."
                    if mode == "drain"
                    else "Reconcile runtime availability and the explicitly interrupted task postconditions."
                ),
            )
            return _operation_result(operation)
        _finish_replacement_fence(ledger, operation, planned, mapping)
        operation = ledger.update_operation(
            operation,
            state="completed",
            step="replacement_transfers_observed",
            planned=planned,
            result={
                "selected_task_ids": list(planned.get("selected_task_ids", [])),
                "mapping": mapping,
                "pending": [],
                "admission_paused": False,
                "mode": mode,
            },
            next_action="Use the successor tasks with their retained current context.",
        )
        return _operation_result(operation)

    def _advance_one(
        self,
        request: ParsedRequest,
        ledger: Ledger,
        operation: OperationRecord,
        planned: dict[str, Any],
        mapping: dict[str, dict[str, Any]],
        old: LedgerRecord,
        entry: dict[str, Any],
        mode: str,
    ) -> tuple[OperationRecord, dict[str, Any]]:
        stage = str(entry.get("stage") or "selected")
        if _stage(stage) < _stage("stopped"):
            stopped = _runtime_call(
                request,
                lambda runtime: _stop_for_replacement(runtime, _thread_id(old), mode),
            )
            if not stopped["stopped"]:
                raise FulcrumError(
                    "REPLACEMENT_DRAINING",
                    str(stopped["reason"]),
                    exit_code=3,
                    retryable=True,
                    details=stopped,
                )
            entry["stage"] = "stopped"
            entry["stop_evidence"] = stopped
            mapping[old.id] = entry
            operation = _retain_mapping(
                ledger, operation, planned, mapping, "old_task_observed_stopped"
            )
        successor_id = str(entry["new_task_record_id"])
        successor = ledger.show(successor_id)
        if _stage(str(entry["stage"])) < _stage("successor_recorded"):
            spec = _successor_spec(request, ledger, old, operation.id, successor_id)
            entry["creation_cwd"] = spec.creation_cwd
            mapping[old.id] = entry
            operation = _retain_mapping(
                ledger, operation, planned, mapping, "successor_identity_retained"
            )
            native = _runtime_call(
                request,
                lambda runtime: _create_and_configure(
                    runtime, spec, {"thread_id": entry.get("new_thread_id")}
                ),
            )
            entry["new_thread_id"] = native.id
            successor_fc = _successor_fc(old, native, operation.id, spec.creation_cwd)
            successor = ledger.show(successor_id)
            if successor is None:
                successor = ledger.create_record(
                    record_id=successor_id,
                    kind="task",
                    title=old.title,
                    description=(
                        f"Successor native task {native.id}; replaces retained task {old.id}."
                    ),
                    owner=native.id,
                    external_ref=f"fulcrum:thread:{native.id}",
                    fc=successor_fc,
                )
            elif (
                successor.kind != "task"
                or not successor.fc
                or successor.fc.get("thread_id") != native.id
            ):
                raise FulcrumError(
                    "REPLACEMENT_CONFLICT",
                    "retained successor task identity is occupied",
                    exit_code=5,
                )
            entry["stage"] = "successor_recorded"
            mapping[old.id] = entry
            operation = _retain_mapping(
                ledger, operation, planned, mapping, "successor_record_observed"
            )
        if successor is None:
            successor = ledger.show(successor_id)
        if successor is None or not successor.fc:
            raise FulcrumError(
                "REPLACEMENT_UNCERTAIN",
                "successor record is not observable",
                exit_code=4,
            )
        if _stage(str(entry["stage"])) < _stage("transferred"):
            transfers = _transfer_owned_work(ledger, old, successor, operation.id)
            successor = ledger.show(successor.id)
            assert successor is not None and successor.fc
            successor_fc = dict(successor.fc)
            if successor_fc.get("role") == "marshal":
                successor_fc["continuity_context"] = _marshal_projection(
                    request, successor_fc["thread_id"]
                )
            ledger.update_fc(successor.id, successor_fc)
            old_fc = dict(old.fc or {})
            old_fc["replaced_by"] = successor.id
            old_fc["replacement_operation"] = operation.id
            old_fc["last_transition"] = operation.id
            archive: Mapping[str, Any] | None = None
            try:
                archived = _runtime_call(
                    request, lambda runtime: runtime.archive(_thread_id(old))
                )
                archive = archived.to_dict()
                old_fc["archive_state"] = "done" if archived.archived else "requested"
            except FulcrumError as error:
                archive = {
                    "state": "unresolved",
                    "code": error.code,
                    "reason": error.message,
                }
            old_fc["archive_operation"] = operation.id
            ledger.update_fc(old.id, old_fc)
            entry["transfers"] = transfers
            entry["pending_transfers"] = []
            entry["archive"] = archive
            entry["stage"] = "transferred"
            mapping[old.id] = entry
            operation = _retain_mapping(
                ledger, operation, planned, mapping, "ownership_transfers_observed"
            )
        return operation, entry


def replace_exact_task(
    request: ParsedRequest,
    ledger: Ledger,
    target: str,
    mode: str,
    operation_id: str,
    index: int,
) -> Mapping[str, Any]:
    """Execute one recovery-authorized replacement under its parent receipt."""

    old = ledger.show(target)
    if old is None or old.kind != "task" or not old.fc:
        raise FulcrumError.invalid("NOT_FOUND", "replace_thread target must be a task")
    successor_id = exact_successor_record_id(operation_id, index, target)
    stopped = _runtime_call(
        request,
        lambda runtime: _stop_for_replacement(runtime, _thread_id(old), mode),
    )
    if not stopped["stopped"]:
        raise FulcrumError(
            "REPLACEMENT_DRAINING",
            str(stopped["reason"]),
            exit_code=3,
            retryable=True,
            details=stopped,
        )
    spec = _successor_spec(request, ledger, old, operation_id, successor_id)
    existing = ledger.show(successor_id)
    external = (
        {"thread_id": (existing.fc or {}).get("thread_id")}
        if existing is not None
        else None
    )
    native = _runtime_call(
        request,
        lambda runtime: _create_and_configure(runtime, spec, external),
    )
    successor_fc = _successor_fc(old, native, operation_id, spec.creation_cwd)
    if existing is None:
        successor = ledger.create_record(
            record_id=successor_id,
            kind="task",
            title=old.title,
            description=f"Recovery successor {native.id} for retained task {old.id}.",
            owner=native.id,
            external_ref=f"fulcrum:thread:{native.id}",
            fc=successor_fc,
        )
    elif (
        existing.kind != "task"
        or not existing.fc
        or existing.fc.get("thread_id") != native.id
    ):
        raise FulcrumError(
            "REPLACEMENT_CONFLICT",
            "retained recovery successor identity is occupied",
            exit_code=5,
        )
    else:
        successor = existing
    transfers = _transfer_owned_work(ledger, old, successor, operation_id)
    old = ledger.show(old.id) or old
    old_fc = dict(old.fc or {})
    old_fc["replaced_by"] = successor.id
    old_fc["replacement_operation"] = operation_id
    old_fc["last_transition"] = operation_id
    ledger.update_fc(old.id, old_fc)
    return {
        "old_task_record_id": old.id,
        "old_thread_id": _thread_id(old),
        "new_task_record_id": successor.id,
        "new_thread_id": native.id,
        "mode": mode,
        "stop_evidence": stopped,
        "transfers": transfers,
    }


def exact_successor_record_id(operation_id: str, index: int, target: str) -> str:
    return (
        "fc-"
        + uuid.uuid5(
            EXACT_REPLACEMENT_NAMESPACE, f"{operation_id}:{index}:{target}"
        ).hex[:8]
    )


async def reconcile_archive_once(
    ledger: Ledger,
    runtime: Any,
    tasks: Sequence[LedgerRecord],
    facts: Mapping[str, TaskFacts],
    *,
    now: datetime,
    request: ParsedRequest,
    archive_idle_seconds: float,
) -> list[Mapping[str, Any]]:
    """Advance each eligible non-leader through its one automatic archive request."""

    actions: list[Mapping[str, Any]] = []
    for task in tasks:
        latest = ledger.show(task.id)
        if latest is not None and latest.kind == "task":
            task = latest
        fc = dict(task.fc or {})
        if (
            not fc
            or fc.get("purpose") == "leadership"
            or fc.get("role") in {"vizier", "marshal"}
            or fc.get("deleted_at") is not None
        ):
            continue
        thread_id = _thread_id(task)
        observed = facts.get(thread_id)
        if observed is None:
            continue
        state = str(fc.get("archive_state") or "pending")
        if state == "done" and not observed.archived:
            fc["archive_state"] = "suppressed"
            fc["archive_suppressed_reason"] = (
                "observed native unarchive after automatic archival"
            )
            fc["last_observed"] = observed.to_dict()
            ledger.update_fc(task.id, fc)
            actions.append(
                {
                    "kind": "archive",
                    "task_record_id": task.id,
                    "state": "suppressed",
                }
            )
            continue
        if state in {"done", "suppressed"}:
            continue
        if state == "requested":
            if observed.archived:
                fc["archive_state"] = "done"
                fc["last_observed"] = observed.to_dict()
                ledger.update_fc(task.id, fc)
                _settle_archive_operation(ledger, fc.get("archive_operation"), observed)
                actions.append(
                    {"kind": "archive", "task_record_id": task.id, "state": "done"}
                )
            elif fc.get("archive_last_observed_archived") is False:
                fc["archive_state"] = "suppressed"
                fc["archive_suppressed_reason"] = (
                    "observed native unarchive after the lifetime request"
                )
                fc["last_observed"] = observed.to_dict()
                ledger.update_fc(task.id, fc)
                actions.append(
                    {
                        "kind": "archive",
                        "task_record_id": task.id,
                        "state": "suppressed",
                    }
                )
            else:
                fc["archive_last_observed_archived"] = False
                fc["last_observed"] = observed.to_dict()
                ledger.update_fc(task.id, fc)
            continue
        if not _archive_eligible(ledger, task, observed):
            if fc.get("archive_due_at") is not None:
                fc["archive_due_at"] = None
                ledger.update_fc(task.id, fc)
            continue
        if fc.get("subscription_state") != "released":
            try:
                released = await runtime.release(thread_id)
            except AppServerError:
                released = None
            if released is not None:
                fc["subscription_state"] = "released"
                fc["subscription_release"] = released.to_dict()
                ledger.update_fc(task.id, fc)
        due = _parse_time(fc.get("archive_due_at"))
        if due is None:
            due = now + timedelta(seconds=archive_idle_seconds)
            fc["archive_state"] = "pending"
            fc["archive_due_at"] = _format_time(due)
            ledger.update_fc(task.id, fc)
            actions.append(
                {
                    "kind": "archive",
                    "task_record_id": task.id,
                    "state": "pending",
                    "due_at": fc["archive_due_at"],
                }
            )
            continue
        if now < due:
            continue
        request_id = str(uuid.uuid5(ARCHIVE_NAMESPACE, task.id))
        archive_request = ParsedRequest(
            command=("task", "archive"),
            arguments={"id": task.id},
            input={},
            actor=ActorContext(kind="controller"),
            instance=request.instance,
            request_id=request_id,
            timeout=request.timeout,
            offline=True,
        )
        operation, reused = ledger.create_operation(
            archive_request,
            bead_id=str(fc.get("work_bead") or "") or None,
            planned={
                "task_record_id": task.id,
                "thread_id": thread_id,
                "automatic": True,
            },
            next_action="Record requested before the one lifetime native archive send.",
        )
        fc["archive_state"] = "requested"
        fc["archive_operation"] = operation.id
        fc["archive_requested_at"] = _format_time(now)
        fc["archive_last_observed_archived"] = None
        ledger.update_fc(task.id, fc)
        if reused:
            continue
        try:
            archived = await runtime.archive(thread_id)
        except AppServerError as error:
            try:
                inspected = await runtime.inspect_task(thread_id)
            except AppServerError:
                inspected = None
            if inspected is not None and inspected.archived:
                fc["archive_state"] = "done"
                fc["last_observed"] = inspected.to_dict()
                ledger.update_fc(task.id, fc)
                _settle_archive_operation(ledger, operation.id, inspected)
                state_value = "done"
            else:
                fc["archive_last_observed_archived"] = (
                    inspected.archived if inspected is not None else None
                )
                ledger.update_fc(task.id, fc)
                ledger.update_operation(
                    operation,
                    state="uncertain" if error.uncertain else "failed",
                    step="automatic_archive_unresolved",
                    error={"code": error.category, "message": str(error)},
                    next_action="Inspect native archive state; never resend the lifetime request.",
                )
                state_value = "uncertain" if error.uncertain else "failed"
            actions.append(
                {"kind": "archive", "task_record_id": task.id, "state": state_value}
            )
            continue
        fc["archive_state"] = "done" if archived.archived else "requested"
        fc["last_observed"] = archived.to_dict()
        fc["archive_last_observed_archived"] = archived.archived
        ledger.update_fc(task.id, fc)
        if archived.archived:
            _settle_archive_operation(ledger, operation.id, archived)
        actions.append(
            {"kind": "archive", "task_record_id": task.id, "state": fc["archive_state"]}
        )
    return actions


def fleet_admission_pause(ledger: Ledger, project: str) -> Mapping[str, Any] | None:
    control = ledger.show("fc-system")
    active = (control.fc or {}).get("fleet_replacement") if control else None
    if not isinstance(active, Mapping) or active.get("state") != "active":
        return None
    selected_project = active.get("project")
    if selected_project is None or selected_project == project:
        return dict(active)
    return None


def _retain_replacement(
    ledger: Ledger,
    operation: OperationRecord,
    planned: Mapping[str, Any],
    mapping: Mapping[str, Any],
) -> OperationRecord:
    control = _control(ledger)
    current = (control.fc or {}).get("fleet_replacement")
    if isinstance(current, Mapping) and current.get("operation_id") != operation.id:
        raise FulcrumError(
            "REPLACEMENT_CONFLICT", "another fleet replacement is active", exit_code=5
        )
    fc = dict(control.fc or {})
    fc["fleet_replacement"] = {
        "operation_id": operation.id,
        "state": "active",
        "project": planned.get("project"),
        "leader_role": planned.get("leader_role"),
        "selected_task_ids": list(planned.get("selected_task_ids", [])),
        "mapping": {str(key): dict(value) for key, value in mapping.items()},
        "started_at": operation.operation.get("created_at"),
    }
    fc["last_transition"] = operation.id
    ledger.update_fc(control.id, fc)
    return operation


def _retain_mapping(
    ledger: Ledger,
    operation: OperationRecord,
    planned: dict[str, Any],
    mapping: Mapping[str, Any],
    step: str,
) -> OperationRecord:
    planned["mapping"] = {str(key): dict(value) for key, value in mapping.items()}
    updated = ledger.update_operation(
        operation,
        step=step,
        planned=planned,
        next_action="Resume from the retained old-to-new mapping without a duplicate native start.",
    )
    _retain_replacement(ledger, updated, planned, mapping)
    return updated


def _finish_replacement_fence(
    ledger: Ledger,
    operation: OperationRecord,
    planned: Mapping[str, Any],
    mapping: Mapping[str, Any],
) -> None:
    control = _control(ledger)
    fc = dict(control.fc or {})
    active = fc.get("fleet_replacement")
    if not isinstance(active, Mapping) or active.get("operation_id") != operation.id:
        raise FulcrumError(
            "REPLACEMENT_AUTHORITY_LOST", "fleet replacement fence changed", exit_code=5
        )
    fc["fleet_replacement"] = None
    fc["fleet_replacement_history"] = [
        *list(fc.get("fleet_replacement_history") or []),
        {
            **dict(active),
            "state": "completed",
            "completed_at": utc_now(),
            "mapping": {str(key): dict(value) for key, value in mapping.items()},
            "reason": planned.get("reason"),
        },
    ]
    fc["last_transition"] = operation.id
    ledger.update_fc(control.id, fc)


def _select_tasks(
    ledger: Ledger, *, project: str | None, leader_role: str | None
) -> list[LedgerRecord]:
    selected: list[LedgerRecord] = []
    control = _control(ledger)
    current_leader = (
        (control.fc or {}).get(f"{leader_role}_thread") if leader_role else None
    )
    for task in ledger.list_records(kind="task", limit=0):
        fc = task.fc or {}
        if fc.get("deleted_at") is not None or fc.get("replaced_by") is not None:
            continue
        leadership = fc.get("purpose") == "leadership" or fc.get("role") in {
            "vizier",
            "marshal",
        }
        if leader_role is not None:
            if (
                leadership
                and fc.get("role") == leader_role
                and fc.get("thread_id") == current_leader
            ):
                selected.append(task)
            continue
        if project is not None:
            if leadership:
                continue
            if _task_project(ledger, task) != project:
                continue
        selected.append(task)
    selected.sort(key=lambda item: item.id)
    return selected


def _successor_spec(
    request: ParsedRequest,
    ledger: Ledger,
    old: LedgerRecord,
    operation_id: str,
    successor_id: str,
) -> TaskSpec:
    fc = old.fc or {}
    observed = fc.get("last_observed")
    observed = observed if isinstance(observed, Mapping) else {}
    config = _config(request)
    project_id = _task_project(ledger, old)
    projects = config.get("projects")
    project = projects.get(project_id) if isinstance(projects, Mapping) else None
    project_root = project.get("root") if isinstance(project, Mapping) else None
    cwd = observed.get("cwd") or project_root or str(request.instance.brain_root)
    roots = observed.get("workspace_roots")
    if not isinstance(roots, list) or not all(isinstance(item, str) for item in roots):
        roots = [str(project_root or cwd)]
    creation_cwd = str(
        (
            request.instance.instance_root / "threads" / operation_id / successor_id
        ).resolve(strict=False)
    )
    Path(creation_cwd).mkdir(parents=True, exist_ok=True)
    return TaskSpec(
        creation_cwd=creation_cwd,
        cwd=str(cwd),
        project_id=(
            str(project.get("codex_project_id"))
            if isinstance(project, Mapping) and project.get("codex_project_id")
            else (
                observed.get("project_id")
                if isinstance(observed.get("project_id"), str)
                else None
            )
        ),
        workspace_roots=tuple(str(item) for item in roots),
        title=str(observed.get("title") or old.title.removeprefix("Managed task: ")),
        model=str(fc.get("model")),
        effort=str(fc.get("effort")),
    )


async def _stop_for_replacement(
    runtime: Any, thread_id: str, mode: str
) -> dict[str, Any]:
    facts = await runtime.inspect_task(thread_id)
    interrupted: Mapping[str, Any] | None = None
    if facts.active_turn is not None:
        if mode == "drain":
            return {
                "stopped": False,
                "reason": "drain is waiting for the active turn",
                "task": facts.to_dict(),
            }
        interrupted_facts = await runtime.interrupt(thread_id, facts.active_turn)
        interrupted = interrupted_facts.to_dict()
        facts = await runtime.inspect_task(thread_id)
        if facts.active_turn is not None:
            return {
                "stopped": False,
                "reason": "interrupted turn is still active",
                "task": facts.to_dict(),
                "interrupt": interrupted,
            }
    terminal_page = await runtime.terminals(thread_id, limit=0, cursor=None)
    items = terminal_page.get("items")
    terminals = (
        [dict(item) for item in items if isinstance(item, Mapping)]
        if isinstance(items, list)
        else []
    )
    if terminals and mode == "drain":
        return {
            "stopped": False,
            "reason": "drain is waiting for owned background terminals",
            "task": facts.to_dict(),
            "terminals": terminals,
        }
    terminated: list[Mapping[str, Any]] = []
    if terminals:
        for terminal in terminals:
            terminal_id = terminal.get("terminal_id") or terminal.get("processId")
            if not isinstance(terminal_id, str):
                return {
                    "stopped": False,
                    "reason": "owned terminal has no exact identifier",
                    "terminals": terminals,
                }
            terminated.append(
                dict(await runtime.terminate_terminal(thread_id, terminal_id))
            )
        checked = await runtime.terminals(thread_id, limit=0, cursor=None)
        remaining = checked.get("items")
        if isinstance(remaining, list) and remaining:
            return {
                "stopped": False,
                "reason": "owned terminals remain after explicit interruption",
                "terminals": remaining,
            }
    released = await runtime.release(thread_id)
    return {
        "stopped": not released.active_terminals,
        "reason": (
            None
            if not released.active_terminals
            else "release observed active terminals"
        ),
        "task": facts.to_dict(),
        "interrupt": interrupted,
        "terminated_terminals": terminated,
        "release": released.to_dict(),
    }


def _successor_fc(
    old: LedgerRecord,
    native: TaskFacts,
    operation_id: str,
    creation_cwd: str,
) -> dict[str, Any]:
    fc = dict(old.fc or {})
    fc.update(
        {
            "owner": native.id,
            "thread_id": native.id,
            "creation_operation": operation_id,
            "creation_cwd": creation_cwd,
            "ownership_operation": operation_id if fc.get("work_bead") else None,
            "replaces": old.id,
            "replaced_by": None,
            "replacement_operation": operation_id,
            "archive_state": (
                "leadership" if fc.get("purpose") == "leadership" else "pending"
            ),
            "archive_due_at": None,
            "archive_operation": None,
            "last_observed": native.to_dict(),
            "last_turn": None,
            "subscription_state": "released",
            "last_transition": operation_id,
        }
    )
    return fc


def _transfer_owned_work(
    ledger: Ledger,
    old: LedgerRecord,
    successor: LedgerRecord,
    operation_id: str,
) -> list[dict[str, Any]]:
    old_thread = _thread_id(old)
    new_thread = _thread_id(successor)
    transferred: list[dict[str, Any]] = []
    for work in ledger.list_records(kind="work", limit=0):
        if work.status == "closed" or not work.fc:
            continue
        continuity = work.fc.get("continuity")
        already_transferred = (
            work.fc.get("owner") == new_thread
            and work.fc.get("ownership_operation") == operation_id
            and isinstance(continuity, Mapping)
            and continuity.get("replacement_operation") == operation_id
        )
        if already_transferred:
            transferred.append(
                {
                    "bead_id": work.id,
                    "from_thread": continuity.get("from_thread"),
                    "from_ownership_operation": continuity.get(
                        "from_ownership_operation"
                    ),
                    "to_thread": new_thread,
                    "to_ownership_operation": operation_id,
                    "reconciled": True,
                }
            )
            continue
        if work.fc.get("owner") != old_thread:
            continue
        prior = work.fc.get("ownership_operation")
        fc = dict(work.fc)
        fc["owner"] = new_thread
        fc["ownership_operation"] = operation_id
        fc["last_transition"] = operation_id
        fc["continuity"] = {
            "from_thread": old_thread,
            "from_ownership_operation": prior,
            "to_thread": new_thread,
            "to_role": fc.get("role"),
            "replacement_operation": operation_id,
        }
        ledger.update_fc(work.id, fc, assignee=new_thread)
        transferred.append(
            {
                "bead_id": work.id,
                "from_thread": old_thread,
                "from_ownership_operation": prior,
                "to_thread": new_thread,
                "to_ownership_operation": operation_id,
            }
        )
    role = (old.fc or {}).get("role")
    if role in {"vizier", "marshal"}:
        control = _control(ledger)
        control_fc = dict(control.fc or {})
        control_fc[f"{role}_thread"] = new_thread
        if role == "marshal":
            control_fc["owner"] = new_thread
        control_fc["last_transition"] = operation_id
        ledger.update_fc(control.id, control_fc, assignee=str(control_fc.get("owner")))
    return transferred


def _owned_work_ids(ledger: Ledger, task: LedgerRecord) -> list[str]:
    thread_id = _thread_id(task)
    return sorted(
        work.id
        for work in ledger.list_records(kind="work", limit=0)
        if work.status != "closed" and work.fc and work.fc.get("owner") == thread_id
    )


def _marshal_projection(request: ParsedRequest, thread_id: str) -> Mapping[str, Any]:
    from fulcrum.roles import RoleService

    result = RoleService().context(
        replace(
            request,
            command=("context",),
            arguments={"role": "marshal"},
            input={},
            request_id=None,
            thread_id=thread_id,
        )
    )
    return dict(result.result or {})


def _archive_eligible(ledger: Ledger, task: LedgerRecord, facts: TaskFacts) -> bool:
    if facts.archived:
        return True
    if facts.active_turn is not None or facts.runtime_status not in {
        "idle",
        "completed",
        "failed",
        "notLoaded",
        "notSubscribed",
    }:
        return False
    fc = task.fc or {}
    identifiers = [fc.get("work_bead"), *(fc.get("associated_beads") or [])]
    for identifier in identifiers:
        if not identifier:
            continue
        work = ledger.show(str(identifier))
        if work is None or work.kind != "work" or work.status == "closed":
            continue
        if not _future_only(work):
            return False
    return True


def _future_only(work: LedgerRecord) -> bool:
    fc = work.fc or {}
    activation = fc.get("activation")
    if isinstance(activation, Mapping) and activation.get("mode") == "future":
        return True
    waiting = fc.get("waiting")
    reasons = waiting.get("reasons") if isinstance(waiting, Mapping) else None
    if isinstance(reasons, list) and reasons:
        kinds = {item.get("kind") for item in reasons if isinstance(item, Mapping)}
        return bool(kinds) and kinds.issubset({"future", "scheduled", "deferred"})
    return False


def _settle_archive_operation(
    ledger: Ledger, identifier: Any, facts: TaskFacts
) -> None:
    if not isinstance(identifier, str):
        return
    operation = ledger.show(identifier)
    if operation is None or operation.kind != "operation" or not operation.fc:
        return
    if operation.fc.get("state") == "completed":
        return
    ledger.update_operation(
        OperationRecord.from_record(operation),
        state="completed",
        step="automatic_archive_observed",
        result={"thread_id": facts.id, "facts": facts.to_dict()},
        next_action="Automatic archival is complete for this task lifetime.",
    )


def _task_project(ledger: Ledger, task: LedgerRecord) -> str | None:
    fc = task.fc or {}
    if isinstance(fc.get("project"), str):
        return str(fc["project"])
    identifiers = [fc.get("work_bead"), *(fc.get("associated_beads") or [])]
    for identifier in identifiers:
        work = ledger.show(str(identifier)) if identifier else None
        project = (work.fc or {}).get("project") if work else None
        if isinstance(project, str):
            return project
    return None


def _task_snapshot(task: LedgerRecord) -> dict[str, Any]:
    fc = task.fc or {}
    return {
        "task_record_id": task.id,
        "thread_id": fc.get("thread_id"),
        "role": fc.get("role"),
        "purpose": fc.get("purpose"),
        "work_bead": fc.get("work_bead"),
        "associated_beads": list(fc.get("associated_beads") or []),
        "ownership_operation": fc.get("ownership_operation"),
        "model": fc.get("model"),
        "effort": fc.get("effort"),
        "archive_state": fc.get("archive_state"),
        "last_observed": fc.get("last_observed"),
    }


def _authorize(request: ParsedRequest) -> None:
    if request.actor.kind == "human":
        return
    if request.actor.kind != "task" or request.actor.task_id != request.thread_id:
        raise FulcrumError(
            "ROLE_AUTHORITY_DENIED",
            "replacement requires human or Vizier authority",
            exit_code=5,
        )
    ledger = _ledger(request)
    matches = [
        task
        for task in ledger.list_records(kind="task", limit=0)
        if task.fc
        and task.fc.get("thread_id") == request.thread_id
        and task.fc.get("role") == "vizier"
        and task.fc.get("purpose") == "leadership"
    ]
    if len(matches) != 1:
        raise FulcrumError(
            "ROLE_AUTHORITY_DENIED",
            "replacement requires human or Vizier authority",
            exit_code=5,
        )


def _config(request: ParsedRequest) -> Mapping[str, Any]:
    manager = ConfigurationManager(request.instance.config_path)
    document, _ = manager.load()
    return manager.effective(document)


def _ledger(request: ParsedRequest) -> Ledger:
    if request.instance.brain_root is None:
        raise FulcrumError(
            "LEDGER_UNAVAILABLE",
            "continuity requires the configured brain",
            exit_code=4,
        )
    config = _config(request)
    beads = config.get("beads")
    executable = beads.get("executable") if isinstance(beads, Mapping) else None
    return Ledger(
        request.instance.brain_root,
        executable=str(executable) if executable else None,
        timeout=request.timeout,
    )


def _control(ledger: Ledger) -> LedgerRecord:
    control = ledger.show("fc-system")
    if control is None or control.kind != "control" or not control.fc:
        raise FulcrumError(
            "CONTROL_UNAVAILABLE", "standing control record is unavailable", exit_code=4
        )
    return control


def _thread_id(task: LedgerRecord) -> str:
    value = (task.fc or {}).get("thread_id")
    if not isinstance(value, str) or not value:
        raise FulcrumError(
            "TASK_CORRUPT", f"task {task.id} has no native ID", exit_code=4
        )
    return value


def _stage(value: str) -> int:
    return REPLACEMENT_STAGES.get(value, -1)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _operation_result(operation: OperationRecord) -> CommandResult:
    state_value = str(operation.operation.get("state") or "accepted")
    state = (
        CommandState(state_value)
        if state_value in CommandState._value2member_map_
        else CommandState.ACCEPTED
    )
    return CommandResult(
        ok=state not in {CommandState.FAILED, CommandState.UNCERTAIN},
        state=state,
        operation_id=operation.id,
        request_id=operation.operation.get("request_id"),
        result={"operation": operation_view(operation)},
    )
