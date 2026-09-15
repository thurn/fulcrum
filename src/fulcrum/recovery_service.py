"""Beads-native recovery fences, typed repairs, and HUMAN resolution."""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from fulcrum.analytics import AnalyticsService
from fulcrum.configuration import ConfigurationManager, ROLES
from fulcrum.contracts import (
    ActorContext,
    CommandResult,
    CommandState,
    FulcrumError,
    ParsedRequest,
)
from fulcrum.continuity import exact_successor_record_id, replace_exact_task
from fulcrum.delivery_service import (
    _call as delivery_call,
    _context as delivery_context,
    _delivery_update,
    _retain_delivery,
    _retained_delivery_source,
)
from fulcrum.ledger import (
    Ledger,
    LedgerFailure,
    LedgerRecord,
    OperationRecord,
    operation_id,
    operation_view,
    utc_now,
)
from fulcrum.runtime_service import TaskService, _runtime_call

RECOVERY_NAMESPACE = uuid.UUID("af844450-8f4e-4bc1-91db-ab1214fc7721")
ACTIVE_FENCE_STATES = {"acquiring", "stopping", "active", "repairing", "failed"}
ACTION_ARGUMENTS: dict[str, set[str]] = {
    "interrupt": {"turn_id"},
    "release_subscription": set(),
    "terminate_owned_terminal": {"terminal_id"},
    "adopt_owner": {"thread_id", "role", "expected_ownership_operation"},
    "replace_thread": {"mode"},
    "cancel_delivery": {"source_oid", "provider_handle"},
    "reconcile_delivery": {"source_oid", "provider_handle"},
    "remove_worktree": set(),
    "set_disposition": {
        "outcome",
        "summary",
        "new_scope",
        "waived_requirements",
        "known_defects",
        "evidence",
    },
    "restore_leadership": {"role", "thread_id"},
    "repair_service": {"operation"},
    "reinstall": {"installation", "source_root"},
    "quarantine": set(),
    "beads_update": {"fields"},
    "git": {"argv"},
}
STOCK_UPDATE_FIELDS = {
    "title",
    "description",
    "acceptance",
    "priority",
    "status",
    "assignee",
}


class RecoveryService:
    def __init__(self, application: Any) -> None:
        self.application = application

    def inspect(self, request: ParsedRequest) -> CommandResult:
        scope_text = str(request.arguments["scope"])
        parsed = _parse_scope(scope_text)
        gaps: list[dict[str, Any]] = []
        config: Mapping[str, Any] | None = None
        try:
            manager = ConfigurationManager(request.instance.config_path)
            document, _ = manager.load()
            config = manager.effective(document)
        except Exception as error:
            gaps.append({"component": "configuration", "reason": str(error)})
        ledger: Ledger | None = None
        inventory: dict[str, Any] = {
            "scope": scope_text,
            "parsed_scope": parsed,
            "control": None,
            "work": [],
            "tasks": [],
            "repositories": [],
        }
        try:
            ledger = _ledger(request)
            selected = _resolve_scope(ledger, config or {}, parsed)
            inventory.update(_inventory(ledger, selected))
        except Exception as error:
            gaps.append({"component": "ledger", "reason": str(error)})
            ledger = None
            selected = {
                "scope": scope_text,
                "kind": parsed["kind"],
                "bead_ids": parsed.get("ids", []),
                "project_ids": (
                    [parsed["value"]] if parsed["kind"] == "project" else []
                ),
            }
        if config is not None:
            inventory["repositories"] = _repository_inventory(config, selected)
        result = {
            **inventory,
            "observed_at": utc_now(),
            "gaps": gaps,
            "durable_receipt": ledger is not None,
        }
        if gaps:
            return CommandResult(
                ok=True,
                state=CommandState.DEGRADED,
                operation_id=None,
                request_id=request.request_id,
                result=result,
            )
        return CommandResult.query(result)

    def takeover(self, request: ParsedRequest) -> CommandResult:
        _authorize_recovery_actor(request)
        ledger = _ledger(request)
        config = _config(request)
        scope_text = str(request.arguments["scope"])
        reason = str(request.arguments["reason"])
        selected = _resolve_scope(ledger, config, _parse_scope(scope_text))
        control = _control(ledger)
        current_takeover = (control.fc or {}).get("active_takeover")
        if request.request_id is None:
            raise FulcrumError.invalid(
                "REQUEST_ID_REQUIRED", "recovery takeover requires a request ID"
            )
        expected_operation = operation_id(request.request_id)
        if (
            isinstance(current_takeover, Mapping)
            and current_takeover.get("state") in ACTIVE_FENCE_STATES
            and current_takeover.get("operation_id") != expected_operation
        ):
            raise FulcrumError(
                "RECOVERY_SCOPE_CONFLICT",
                "another recovery fence is already active",
                exit_code=5,
                details={"active_takeover": dict(current_takeover)},
            )
        operation, reused = ledger.create_operation(
            request,
            bead_id=(
                selected["bead_ids"][0] if len(selected["bead_ids"]) == 1 else None
            ),
            planned={
                "scope": scope_text,
                "reason": reason,
                "inventory": _inventory(ledger, selected),
                "selected": selected,
            },
            next_action="Persist the fence, stop competing managed work, then transfer scoped ownership.",
        )
        if reused and operation.operation.get("state") in {
            "completed",
            "failed",
            "uncertain",
            "cancelled",
        }:
            return _operation_result(operation)
        retained = operation.operation.get("planned")
        if isinstance(retained, Mapping) and isinstance(
            retained.get("selected"), Mapping
        ):
            selected = dict(retained["selected"])
        fence = {
            "operation_id": operation.id,
            "scope": scope_text,
            "reason": reason,
            "state": "acquiring",
            "bead_ids": list(selected["bead_ids"]),
            "project_ids": list(selected["project_ids"]),
            "started_at": utc_now(),
            "owner_thread": request.thread_id,
            "prior_marshal_thread": (control.fc or {}).get("marshal_thread"),
        }
        _write_control_fence(ledger, control, fence)
        for bead_id in selected["bead_ids"]:
            work = ledger.show(str(bead_id))
            if work is None or work.kind != "work" or not work.fc:
                continue
            fc = dict(work.fc)
            if not isinstance(fc.get("recovery_fence"), Mapping):
                fc["pre_recovery"] = {
                    "owner": fc.get("owner"),
                    "role": fc.get("role"),
                    "ownership_operation": fc.get("ownership_operation"),
                    "phase": fc.get("phase"),
                    "status": work.status,
                }
            fc["recovery_fence"] = dict(fence)
            if work.status != "closed":
                fc["phase"] = "recovering"
            fc["last_transition"] = operation.id
            fc["next_action"] = "Stop competing managed work before Justiciar mutation."
            ledger.update_fc(
                work.id, fc, status="blocked" if work.status != "closed" else None
            )
        stopped, unresolved, released = self._stop_competing(
            request, ledger, selected, operation.id
        )
        if unresolved:
            fence["state"] = "stopping"
            fence["unresolved_tasks"] = unresolved
            _set_fence_state(ledger, fence, "stopping", unresolved_tasks=unresolved)
            operation = ledger.update_operation(
                operation,
                state="accepted",
                step="recovery_fence_waiting_for_termination",
                result={
                    "scope": scope_text,
                    "stopped": stopped,
                    "released_idle": released,
                    "unresolved_tasks": unresolved,
                    "fence": fence,
                },
                next_action="Reconcile the exact active turns; ownership has not transferred.",
            )
            return _operation_result(operation)
        justiciar = _select_justiciar_thread(ledger, request, control)
        if justiciar is not None:
            task = _task_for_thread(ledger, justiciar)
            if task is not None and task.fc:
                task_fc = dict(task.fc)
                task_fc["pre_recovery_role"] = task_fc.get("role")
                task_fc["pre_recovery_task"] = {
                    "role": task_fc.get("role"),
                    "purpose": task_fc.get("purpose"),
                    "work_bead": task_fc.get("work_bead"),
                    "associated_beads": list(task_fc.get("associated_beads") or []),
                    "ownership_operation": task_fc.get("ownership_operation"),
                }
                task_fc["role"] = "justiciar"
                task_fc["purpose"] = "recovery"
                task_fc["recovery_operation"] = operation.id
                task_fc["associated_beads"] = list(selected["bead_ids"])
                task_fc["work_bead"] = (
                    selected["bead_ids"][0] if len(selected["bead_ids"]) == 1 else None
                )
                task_fc["ownership_operation"] = operation.id
                task_fc["last_transition"] = operation.id
                ledger.update_fc(
                    task.id,
                    task_fc,
                    title=f"🔥[justiciar] Recovery {scope_text}",
                )
        owner = justiciar or "HUMAN"
        for bead_id in selected["bead_ids"]:
            work = ledger.show(str(bead_id))
            if work is None or work.kind != "work" or not work.fc:
                continue
            fc = dict(work.fc)
            fence_value = dict(fc.get("recovery_fence") or fence)
            fence_value["state"] = "active"
            fence_value["owner_thread"] = justiciar
            fc["recovery_fence"] = fence_value
            if work.status != "closed":
                fc["owner"] = owner
                fc["role"] = "justiciar"
                fc["ownership_operation"] = operation.id
                fc["next_action"] = (
                    "Justiciar may execute only typed repairs inside the retained scope."
                )
                ledger.update_fc(work.id, fc, assignee=owner, status="blocked")
            else:
                ledger.update_fc(work.id, fc)
        fence["state"] = "active"
        fence["owner_thread"] = justiciar
        _write_control_fence(ledger, _control(ledger), fence)
        operation = ledger.update_operation(
            operation,
            state="completed",
            step="justiciar_scope_acquired",
            result={
                "scope": scope_text,
                "fence": fence,
                "justiciar_thread": justiciar,
                "owner": owner,
                "stopped": stopped,
                "released_idle": released,
            },
            next_action="Execute an explicit repair payload, then release the fence.",
        )
        return _operation_result(operation)

    def repair(self, request: ParsedRequest) -> CommandResult:
        actions = _validate_actions(request.input)
        try:
            ledger = _ledger(request)
            ledger.run(("status",))
            config = _config(request)
        except (FulcrumError, LedgerFailure) as error:
            return self._degraded_repair(request, actions, error)
        selected = _resolve_scope(
            ledger, config, _parse_scope(str(request.arguments["scope"]))
        )
        control = _control(ledger)
        fence = _authorized_fence(request, control, selected)
        operation, reused = ledger.create_operation(
            request,
            bead_id=(
                selected["bead_ids"][0] if len(selected["bead_ids"]) == 1 else None
            ),
            planned={
                "scope": request.arguments["scope"],
                "takeover_operation": fence.get("operation_id"),
                "actions": actions,
                "selected": selected,
            },
            next_action="Inspect and execute each typed repair in retained order.",
        )
        if reused and operation.operation.get("state") in {
            "completed",
            "failed",
            "uncertain",
            "cancelled",
        }:
            return _operation_result(operation)
        retained = operation.operation.get("planned")
        if isinstance(retained, Mapping):
            actions = [dict(item) for item in retained.get("actions", [])]
        progress = retained.get("progress") if isinstance(retained, Mapping) else None
        completed = (
            list(progress.get("completed_steps") or [])
            if isinstance(progress, Mapping)
            else []
        )
        evidence = (
            list(progress.get("evidence") or [])
            if isinstance(progress, Mapping)
            else []
        )
        _set_fence_state(ledger, fence, "repairing")
        for index, action in enumerate(actions):
            step = f"repair:{index}"
            if step in completed:
                continue
            before = _observe_target(ledger, action["target"], request)
            effect_plan = _effect_plan(action, operation.id, index)
            operation = ledger.update_operation(
                operation,
                step=f"repair_{index}_intent_retained",
                planned={
                    **dict(operation.operation.get("planned") or {}),
                    "progress": {
                        "completed_steps": completed,
                        "evidence": [*evidence, {"step": step, "before": before}],
                        "effect_plan": effect_plan,
                    },
                },
                next_action=f"Execute {action['action']} only against {action['target']}.",
            )
            try:
                result = self._execute_action(
                    request, ledger, config, selected, operation.id, index, action
                )
                after = _observe_target(ledger, action["target"], request)
            except Exception as error:
                _set_fence_state(ledger, fence, "failed", error=str(error))
                uncertain = (
                    isinstance(error, LedgerFailure) and error.uncertain
                ) or bool(getattr(error, "possible_effect", False))
                operation = ledger.update_operation(
                    operation,
                    state="uncertain" if uncertain else "failed",
                    step=f"repair_{index}_failed",
                    error={
                        "code": getattr(error, "code", type(error).__name__),
                        "message": str(error),
                    },
                    next_action="Inspect the failed exact target; the recovery fence remains active.",
                )
                return _operation_result(operation)
            completed.append(step)
            evidence = [
                *evidence,
                {"step": step, "before": before, "effect": result, "after": after},
            ]
            operation = ledger.update_operation(
                operation,
                step=f"repair_{index}_observed",
                planned={
                    **dict(operation.operation.get("planned") or {}),
                    "progress": {
                        "completed_steps": completed,
                        "evidence": evidence,
                    },
                },
                next_action="Advance to the next retained repair action.",
            )
        _set_fence_state(ledger, fence, "active")
        operation = ledger.update_operation(
            operation,
            state="completed",
            step="typed_repairs_observed",
            result={
                "actions": len(actions),
                "evidence": evidence,
                "fence_active": True,
            },
            next_action="Inspect the scope and explicitly release the recovery fence.",
        )
        return _operation_result(operation)

    def _degraded_repair(
        self,
        request: ParsedRequest,
        actions: Sequence[Mapping[str, Any]],
        ledger_error: Exception,
    ) -> CommandResult:
        if request.actor.kind != "human":
            raise FulcrumError(
                "RECOVERY_AUTHORITY_REQUIRED",
                "no-ledger repair requires explicit human authority",
                exit_code=5,
            )
        allowed = {
            "interrupt",
            "release_subscription",
            "terminate_owned_terminal",
            "repair_service",
            "reinstall",
        }
        disallowed = [
            item["action"] for item in actions if item["action"] not in allowed
        ]
        if disallowed:
            raise FulcrumError(
                "NO_RECEIPT_REPAIR_DENIED",
                "only essential runtime resource or installation repair is allowed without Beads",
                exit_code=5,
                details={"disallowed_actions": disallowed},
            )
        effects: list[Mapping[str, Any]] = []
        for action in actions:
            kind = str(action["action"])
            target = str(action["target"])
            arguments = dict(action["arguments"])
            if kind == "release_subscription":
                effect = _runtime_call(
                    request, lambda runtime, thread=target: runtime.release(thread)
                ).to_dict()
            elif kind == "interrupt":
                effect = _runtime_call(
                    request,
                    lambda runtime, thread=target, turn=str(
                        arguments["turn_id"]
                    ): runtime.interrupt(thread, turn),
                ).to_dict()
            elif kind == "terminate_owned_terminal":
                effect = _runtime_call(
                    request,
                    lambda runtime, thread=target, terminal=str(
                        arguments["terminal_id"]
                    ): runtime.terminate_terminal(thread, terminal),
                )
            elif kind == "repair_service":
                effect = _repair_service(request, target, arguments)
            else:
                effect = _repair_reinstall(
                    request,
                    target,
                    arguments,
                    operation_key=request.request_id or str(uuid.uuid4()),
                )
            effects.append(
                {
                    "action": kind,
                    "target": target,
                    "effect": (effect if isinstance(effect, Mapping) else dict(effect)),
                }
            )
        return CommandResult(
            ok=True,
            state=CommandState.DEGRADED,
            operation_id=None,
            request_id=request.request_id,
            result={
                "durable_receipt": False,
                "ledger_error": str(ledger_error),
                "effects": effects,
                "warning": "Actual effects were observed without a durable Beads receipt; reconcile before restoring authority.",
            },
        )

    def release(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        config = _config(request)
        selected = _resolve_scope(
            ledger, config, _parse_scope(str(request.arguments["scope"]))
        )
        control = _control(ledger)
        fence = _authorized_fence(request, control, selected)
        summary = str(request.arguments["summary"])
        operation, reused = ledger.create_operation(
            request,
            bead_id=(
                selected["bead_ids"][0] if len(selected["bead_ids"]) == 1 else None
            ),
            planned={
                "scope": request.arguments["scope"],
                "takeover_operation": fence.get("operation_id"),
                "summary": summary,
            },
            next_action="Reconcile scoped facts before invalidating Justiciar authority.",
        )
        if reused and operation.operation.get("state") in {
            "completed",
            "failed",
            "uncertain",
            "cancelled",
        }:
            return _operation_result(operation)
        for bead_id in selected["bead_ids"]:
            work = ledger.show(str(bead_id))
            if work is None or work.kind != "work" or not work.fc:
                continue
            fc = dict(work.fc)
            fc["recovery_history"] = [
                *list(fc.get("recovery_history") or []),
                {**dict(fence), "released_at": utc_now(), "summary": summary},
            ]
            fc["recovery_fence"] = None
            if work.status != "closed":
                marshal = (control.fc or {}).get("marshal_thread") or "HUMAN"
                fc["owner"] = marshal
                fc["role"] = "marshal" if marshal != "HUMAN" else None
                fc["ownership_operation"] = operation.id
                fc["phase"] = "backlog"
                fc["next_action"] = (
                    "Marshal must assess the reconciled recovery result."
                )
                ledger.update_fc(
                    work.id,
                    fc,
                    assignee=str(marshal),
                    status="open" if fc["phase"] == "backlog" else "blocked",
                )
            else:
                ledger.update_fc(work.id, fc)
        justiciar = fence.get("owner_thread")
        task = _task_for_thread(ledger, str(justiciar)) if justiciar else None
        if task is not None and task.fc:
            task_fc = dict(task.fc)
            prior = task_fc.get("pre_recovery_task")
            if isinstance(prior, Mapping):
                for field in (
                    "role",
                    "purpose",
                    "work_bead",
                    "associated_beads",
                    "ownership_operation",
                ):
                    task_fc[field] = prior.get(field)
            task_fc["recovery_operation"] = None
            task_fc["pre_recovery_task"] = None
            task_fc["last_transition"] = operation.id
            ledger.update_fc(task.id, task_fc)
        released_fence = {
            **dict(fence),
            "state": "released",
            "released_at": utc_now(),
            "summary": summary,
        }
        control_fc = dict(control.fc or {})
        control_fc["active_takeover"] = None
        control_fc["recovery_history"] = [
            *list(control_fc.get("recovery_history") or []),
            released_fence,
        ]
        control_fc["last_transition"] = operation.id
        ledger.update_fc(control.id, control_fc)
        operation = ledger.update_operation(
            operation,
            state="completed",
            step="recovery_authority_released",
            result={"scope": request.arguments["scope"], "fence": released_fence},
            next_action="Ordinary Marshal authority and dispatch may resume.",
        )
        return _operation_result(operation)

    def _stop_competing(
        self,
        request: ParsedRequest,
        ledger: Ledger,
        selected: Mapping[str, Any],
        operation_id: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        stopped: list[dict[str, Any]] = []
        unresolved: list[dict[str, Any]] = []
        released: list[dict[str, Any]] = []
        chosen = _select_justiciar_thread(ledger, request, _control(ledger))
        for task_index, task in enumerate(_tasks_in_scope(ledger, selected)):
            thread_id = str((task.fc or {}).get("thread_id") or "")
            if thread_id == chosen:
                continue
            facts = _retained_observation(task)
            if request.runtime_submit is not None:
                try:
                    facts = _runtime_call(
                        request,
                        lambda runtime, thread=thread_id: runtime.inspect_task(thread),
                    ).to_dict()
                except FulcrumError as error:
                    unresolved.append(
                        {
                            "task_record_id": task.id,
                            "thread_id": thread_id,
                            "reason": error.message,
                        }
                    )
                    continue
            active_turn = (
                facts.get("active_turn") if isinstance(facts, Mapping) else None
            )
            runtime_status = (
                facts.get("runtime_status") if isinstance(facts, Mapping) else None
            )
            if request.runtime_submit is None and (
                not facts
                or active_turn is not None
                or runtime_status not in {"idle", "notLoaded", "completed", "failed"}
            ):
                unresolved.append(
                    {
                        "task_record_id": task.id,
                        "thread_id": thread_id,
                        "active_turn": active_turn,
                        "reason": "retained state does not prove competing native work stopped",
                    }
                )
                continue
            if active_turn:
                child = self.application.dispatch(
                    _child_request(
                        request,
                        operation_id,
                        task_index,
                        command=("task", "interrupt"),
                        arguments={"id": task.id, "turn_id": str(active_turn)},
                    )
                )
                if child.state != CommandState.COMPLETED:
                    unresolved.append(
                        {
                            "task_record_id": task.id,
                            "thread_id": thread_id,
                            "operation_id": child.operation_id,
                            "state": child.state.value,
                        }
                    )
                    continue
                stopped.append(
                    {
                        "task_record_id": task.id,
                        "thread_id": thread_id,
                        "operation_id": child.operation_id,
                    }
                )
            elif request.runtime_submit is not None:
                child = self.application.dispatch(
                    _child_request(
                        request,
                        operation_id,
                        task_index,
                        command=("task", "release"),
                        arguments={"id": task.id},
                    )
                )
                if child.state == CommandState.COMPLETED:
                    released.append(
                        {
                            "task_record_id": task.id,
                            "thread_id": thread_id,
                            "operation_id": child.operation_id,
                        }
                    )
        return stopped, unresolved, released

    def _execute_action(
        self,
        request: ParsedRequest,
        ledger: Ledger,
        config: Mapping[str, Any],
        selected: Mapping[str, Any],
        operation_id: str,
        index: int,
        action: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        kind = str(action["action"])
        target = str(action["target"])
        arguments = dict(action["arguments"])
        _target_in_scope(ledger, config, selected, target, kind)
        if kind in {"interrupt", "release_subscription", "terminate_owned_terminal"}:
            command = {
                "interrupt": ("task", "interrupt"),
                "release_subscription": ("task", "release"),
                "terminate_owned_terminal": ("task", "terminal", "stop"),
            }[kind]
            child_arguments: dict[str, Any] = {"id": target}
            if kind == "interrupt":
                child_arguments["turn_id"] = arguments["turn_id"]
            elif kind == "terminate_owned_terminal":
                child_arguments.update(
                    {
                        "terminal": arguments["terminal_id"],
                        "reason": action["reason"],
                    }
                )
            result = self.application.dispatch(
                _child_request(
                    request,
                    operation_id,
                    index,
                    command=command,
                    arguments=child_arguments,
                )
            )
            if result.state != CommandState.COMPLETED:
                raise FulcrumError(
                    "REPAIR_EFFECT_UNRESOLVED",
                    f"{kind} did not reach an observed completed state",
                    exit_code=5,
                    details={
                        "operation_id": result.operation_id,
                        "state": result.state.value,
                    },
                )
            return result.to_dict()
        if kind == "adopt_owner":
            return _repair_adopt_owner(ledger, target, arguments, operation_id)
        if kind == "replace_thread":
            return replace_exact_task(
                request,
                ledger,
                target,
                str(arguments["mode"]),
                operation_id,
                index,
            )
        if kind in {"cancel_delivery", "reconcile_delivery"}:
            delivery_request = replace(request, arguments={"bead": target})
            delivery_ledger, work, project, provider = delivery_context(
                delivery_request
            )
            source, retained_handle = _retained_delivery_source(
                delivery_request, work, project
            )
            if source.oid != arguments["source_oid"]:
                raise FulcrumError.invalid(
                    "STALE_SOURCE",
                    "repair source_oid differs from retained delivery source",
                )
            supplied_handle = arguments.get("provider_handle")
            handle = str(supplied_handle or retained_handle)
            facts = delivery_call(
                provider.cancel(source, handle)
                if kind == "cancel_delivery"
                else provider.inspect(source, handle)
            )
            _retain_delivery(
                delivery_ledger, work, _delivery_update(facts), operation_id
            )
            return {"delivery": facts.to_dict()}
        if kind == "remove_worktree":
            result = self.application.dispatch(
                _child_request(
                    request,
                    operation_id,
                    index,
                    command=("worktree", "cleanup"),
                    arguments={"bead": target},
                )
            )
            if result.state != CommandState.COMPLETED:
                raise FulcrumError(
                    "REPAIR_EFFECT_UNRESOLVED",
                    "managed worktree cleanup did not complete",
                    exit_code=5,
                )
            return result.to_dict()
        if kind == "set_disposition":
            return _repair_set_disposition(ledger, target, arguments, operation_id)
        if kind == "restore_leadership":
            return _repair_restore_leadership(ledger, target, arguments, operation_id)
        if kind == "repair_service":
            return _repair_service(request, target, arguments)
        if kind == "reinstall":
            return _repair_reinstall(
                request,
                target,
                arguments,
                operation_key=f"{operation_id}-{index}",
            )
        if kind == "quarantine":
            return _repair_quarantine(request, target, operation_id, index)
        if kind == "beads_update":
            return _repair_beads_update(ledger, target, arguments)
        if kind == "git":
            return _repair_git(target, arguments)
        raise AssertionError(f"validated action {kind} has no implementation")


class HumanService:
    def list(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        rows: list[dict[str, Any]] = []
        for work in ledger.list_records(kind="work", limit=0):
            fc = work.fc or {}
            reasons = [
                dict(item)
                for item in _waiting_reasons(fc.get("waiting"))
                if item.get("kind") == "human"
            ]
            if fc.get("owner") != "HUMAN" and not reasons:
                continue
            rows.append(
                {
                    "bead_id": work.id,
                    "title": work.title,
                    "phase": fc.get("phase"),
                    "owner": fc.get("owner"),
                    "reasons": reasons,
                    "required_actions": [
                        item.get("required_action") for item in reasons
                    ],
                    "next_action": fc.get("next_action"),
                }
            )
        return CommandResult.query(
            {"items": sorted(rows, key=lambda item: item["bead_id"])}
        )

    def resolve(self, request: ParsedRequest) -> CommandResult:
        if request.actor.kind != "human" and not _actor_has_role(request, "vizier"):
            raise FulcrumError(
                "ROLE_AUTHORITY_DENIED",
                "human resolution requires human or Vizier authority",
                exit_code=5,
            )
        ledger = _ledger(request)
        work = ledger.show(str(request.arguments["id"]))
        if work is None or work.kind != "work" or not work.fc:
            raise FulcrumError.invalid("NOT_FOUND", "unknown HUMAN work bead")
        reason_id = request.input.get("reason_id")
        answer = request.input.get("answer")
        resume_role = request.input.get("resume_role", "marshal")
        if not isinstance(reason_id, str) or not reason_id:
            raise FulcrumError.invalid("INVALID_INPUT", "reason_id is required")
        if not isinstance(answer, str) or not answer.strip():
            raise FulcrumError.invalid("INVALID_INPUT", "answer is required")
        if resume_role not in ROLES:
            raise FulcrumError.invalid("INVALID_INPUT", "resume_role is invalid")
        reasons = _waiting_reasons(work.fc.get("waiting"))
        selected = next((item for item in reasons if item.get("id") == reason_id), None)
        if selected is None or selected.get("kind") != "human":
            raise FulcrumError.invalid(
                "HUMAN_REASON_NOT_FOUND", "reason_id is not an unresolved human reason"
            )
        scope_change = request.input.get("scope_change")
        if scope_change is not None:
            if not isinstance(scope_change, Mapping):
                raise FulcrumError.invalid(
                    "INVALID_INPUT", "scope_change must be an object"
                )
            allowed = {"title", "outcome", "acceptance", "summary", "context"}
            unknown = set(scope_change).difference(allowed)
            if unknown:
                raise FulcrumError.invalid(
                    "INVALID_INPUT", f"unknown scope_change fields: {sorted(unknown)}"
                )
        operation, reused = ledger.create_operation(
            request,
            bead_id=work.id,
            planned={
                "reason_id": reason_id,
                "answer": answer,
                "scope_change": scope_change,
                "resume_role": resume_role,
            },
            next_action="Resolve exactly one retained human reason and preserve all others.",
        )
        if reused and operation.operation.get("state") in {
            "completed",
            "failed",
            "uncertain",
            "cancelled",
        }:
            return _operation_result(operation)
        fc = dict(work.fc)
        if scope_change is not None:
            fc.update(dict(scope_change))
        remaining = [item for item in reasons if item.get("id") != reason_id]
        fc["waiting"] = {"reasons": remaining} if remaining else None
        marshal = _marshal_thread(ledger) or "HUMAN"
        fc["owner"] = marshal
        fc["role"] = "marshal" if marshal != "HUMAN" else None
        fc["requested_role"] = resume_role
        fc["ownership_operation"] = operation.id
        fc["phase"] = "backlog" if not remaining else "blocked"
        fc["human_resolutions"] = [
            *list(fc.get("human_resolutions") or []),
            {
                "reason_id": reason_id,
                "question": selected.get("reason"),
                "answer": answer,
                "scope_change": scope_change,
                "resume_role": resume_role,
                "operation_id": operation.id,
                "resolved_at": utc_now(),
            },
        ]
        fc["last_transition"] = operation.id
        fc["next_action"] = (
            "Marshal must continue with the retained answer and remaining blockers."
            if remaining
            else f"Marshal may authorize {resume_role} continuation from the retained answer."
        )
        updated = ledger.update_fc(
            work.id,
            fc,
            assignee=marshal,
            status="blocked" if remaining else "open",
            title=(
                str(scope_change["title"])
                if isinstance(scope_change, Mapping) and "title" in scope_change
                else None
            ),
        )
        operation = ledger.update_operation(
            operation,
            state="completed",
            step="human_reason_resolved",
            result={
                "bead_id": work.id,
                "reason_id": reason_id,
                "remaining_reasons": remaining,
                "owner": marshal,
                "work": {"id": updated.id, "fc": updated.fc},
            },
            next_action=fc["next_action"],
        )
        return _operation_result(operation)


def active_recovery_fence(ledger: Ledger) -> Mapping[str, Any] | None:
    control = ledger.show("fc-system")
    fence = (control.fc or {}).get("active_takeover") if control else None
    return (
        dict(fence)
        if isinstance(fence, Mapping) and fence.get("state") in ACTIVE_FENCE_STATES
        else None
    )


def _validate_actions(value: Mapping[str, Any]) -> list[dict[str, Any]]:
    if set(value) != {"actions"} or not isinstance(value.get("actions"), list):
        raise FulcrumError.invalid(
            "INVALID_REPAIR", "repair input requires exactly an actions array"
        )
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value["actions"]):
        if not isinstance(item, Mapping) or set(item) != {
            "action",
            "target",
            "arguments",
            "reason",
        }:
            raise FulcrumError.invalid(
                "INVALID_REPAIR", f"repair action {index} has invalid fields"
            )
        kind = item.get("action")
        target = item.get("target")
        arguments = item.get("arguments")
        reason = item.get("reason")
        if kind not in ACTION_ARGUMENTS:
            raise FulcrumError.invalid(
                "INVALID_REPAIR", f"unknown repair action {kind!r}"
            )
        if not isinstance(target, str) or not target:
            raise FulcrumError.invalid(
                "INVALID_REPAIR", f"repair action {index} requires an exact target"
            )
        if (
            not isinstance(arguments, Mapping)
            or set(arguments) != ACTION_ARGUMENTS[kind]
        ):
            raise FulcrumError.invalid(
                "INVALID_REPAIR",
                f"{kind} arguments require exactly {sorted(ACTION_ARGUMENTS[kind])}",
            )
        if not isinstance(reason, str) or not reason.strip():
            raise FulcrumError.invalid(
                "INVALID_REPAIR", f"repair action {index} requires a reason"
            )
        _validate_action_values(str(kind), arguments)
        result.append(
            {
                "action": str(kind),
                "target": target,
                "arguments": dict(arguments),
                "reason": reason,
            }
        )
    return result


def _validate_action_values(kind: str, arguments: Mapping[str, Any]) -> None:
    if kind == "interrupt" and not _nonempty(arguments.get("turn_id")):
        raise FulcrumError.invalid("INVALID_REPAIR", "interrupt turn_id is required")
    if kind == "terminate_owned_terminal" and not _nonempty(
        arguments.get("terminal_id")
    ):
        raise FulcrumError.invalid("INVALID_REPAIR", "terminal_id is required")
    if kind == "adopt_owner":
        if (
            not _nonempty(arguments.get("thread_id"))
            or arguments.get("role") not in ROLES
            or not _nonempty(arguments.get("expected_ownership_operation"))
        ):
            raise FulcrumError.invalid(
                "INVALID_REPAIR", "adopt_owner arguments are invalid"
            )
    if kind == "replace_thread" and arguments.get("mode") not in {"drain", "interrupt"}:
        raise FulcrumError.invalid("INVALID_REPAIR", "replace_thread mode is invalid")
    if kind in {"cancel_delivery", "reconcile_delivery"}:
        if (
            not _nonempty(arguments.get("source_oid"))
            or arguments.get("provider_handle") is not None
            and not _nonempty(arguments.get("provider_handle"))
        ):
            raise FulcrumError.invalid(
                "INVALID_REPAIR", f"{kind} delivery identity is invalid"
            )
    if kind == "set_disposition":
        for name in ("outcome", "summary"):
            if not _nonempty(arguments.get(name)):
                raise FulcrumError.invalid(
                    "INVALID_REPAIR", f"set_disposition {name} is required"
                )
        for name in ("waived_requirements", "known_defects", "evidence"):
            value = arguments.get(name)
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                raise FulcrumError.invalid(
                    "INVALID_REPAIR", f"set_disposition {name} must be strings"
                )
        if not isinstance(arguments.get("new_scope"), Mapping):
            raise FulcrumError.invalid("INVALID_REPAIR", "new_scope must be an object")
        allowed_scope = {"title", "outcome", "acceptance", "summary", "context"}
        unknown_scope = set(arguments["new_scope"]).difference(allowed_scope)
        if unknown_scope:
            raise FulcrumError.invalid(
                "INVALID_REPAIR",
                f"set_disposition new_scope has forbidden fields: {sorted(unknown_scope)}",
            )
    if kind == "restore_leadership" and (
        arguments.get("role") not in {"marshal", "vizier"}
        or not _nonempty(arguments.get("thread_id"))
    ):
        raise FulcrumError.invalid(
            "INVALID_REPAIR", "restore_leadership arguments are invalid"
        )
    if kind == "repair_service" and arguments.get("operation") not in {
        "start",
        "stop",
        "restart",
        "reinstall_definition",
    }:
        raise FulcrumError.invalid(
            "INVALID_REPAIR", "repair_service operation is invalid"
        )
    if kind == "reinstall" and (
        arguments.get("installation") not in {"main", "recovery"}
        or not isinstance(arguments.get("source_root"), str)
        or not Path(str(arguments["source_root"])).is_absolute()
    ):
        raise FulcrumError.invalid("INVALID_REPAIR", "reinstall arguments are invalid")
    if kind == "beads_update":
        fields = arguments.get("fields")
        if (
            not isinstance(fields, Mapping)
            or not fields
            or set(fields).difference(STOCK_UPDATE_FIELDS)
        ):
            raise FulcrumError.invalid(
                "INVALID_REPAIR", "beads_update fields are invalid"
            )
    if kind == "git":
        argv = arguments.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(item, str) and item for item in argv)
        ):
            raise FulcrumError.invalid(
                "INVALID_REPAIR", "git argv must be nonempty literal strings"
            )


def _parse_scope(value: str) -> dict[str, Any]:
    if value == "instance":
        return {"kind": "instance", "value": "instance", "ids": []}
    if value.startswith("bead:") and value[5:]:
        return {"kind": "beads", "value": value[5:], "ids": [value[5:]]}
    if value.startswith("beads:"):
        identifiers = value[6:].split(",")
        if (
            identifiers
            and all(identifiers)
            and len(set(identifiers)) == len(identifiers)
        ):
            return {"kind": "beads", "value": value[6:], "ids": identifiers}
    if value.startswith("project:") and value[8:]:
        return {"kind": "project", "value": value[8:], "ids": []}
    raise FulcrumError.invalid(
        "INVALID_SCOPE",
        "scope must be bead:ID, beads:ID,ID, project:ID, or instance",
    )


def _resolve_scope(
    ledger: Ledger, config: Mapping[str, Any], parsed: Mapping[str, Any]
) -> dict[str, Any]:
    work = ledger.list_records(kind="work", limit=0)
    kind = str(parsed["kind"])
    if kind == "beads":
        requested = [str(item) for item in parsed["ids"]]
        requested_records = {
            identifier: ledger.show(identifier) for identifier in requested
        }
        missing = [
            identifier
            for identifier, record in requested_records.items()
            if record is None or record.kind != "work"
        ]
        if missing:
            raise FulcrumError.invalid("NOT_FOUND", f"unknown work beads: {missing}")
        bead_ids = requested
        scoped_projects: set[str] = set()
        for identifier in bead_ids:
            requested_record = requested_records[identifier]
            assert requested_record is not None
            project = (requested_record.fc or {}).get("project")
            if project:
                scoped_projects.add(str(project))
        project_ids = sorted(scoped_projects)
    elif kind == "project":
        project = str(parsed["value"])
        projects = config.get("projects")
        if not isinstance(projects, Mapping) or project not in projects:
            raise FulcrumError.invalid(
                "PROJECT_NOT_FOUND", f"unknown project {project}"
            )
        bead_ids = [
            item.id for item in work if (item.fc or {}).get("project") == project
        ]
        project_ids = [project]
    else:
        bead_ids = [item.id for item in work]
        projects = config.get("projects")
        project_ids = (
            sorted(str(item) for item in projects)
            if isinstance(projects, Mapping)
            else []
        )
    return {
        "scope": (
            "instance"
            if kind == "instance"
            else (
                f"project:{parsed['value']}"
                if kind == "project"
                else "beads:" + ",".join(bead_ids)
            )
        ),
        "kind": kind,
        "bead_ids": bead_ids,
        "project_ids": project_ids,
    }


def _inventory(ledger: Ledger, selected: Mapping[str, Any]) -> dict[str, Any]:
    control = ledger.show("fc-system")
    works = [ledger.show(str(item)) for item in selected["bead_ids"]]
    tasks = _tasks_in_scope(ledger, selected)
    return {
        "control": (
            {"id": control.id, "status": control.status, "fc": control.fc}
            if control
            else None
        ),
        "work": [
            {
                "id": item.id,
                "status": item.status,
                "assignee": item.assignee,
                "fc": item.fc,
            }
            for item in works
            if item is not None
        ],
        "tasks": [
            {
                "id": item.id,
                "status": item.status,
                "assignee": item.assignee,
                "fc": item.fc,
            }
            for item in tasks
        ],
    }


def _tasks_in_scope(ledger: Ledger, selected: Mapping[str, Any]) -> list[LedgerRecord]:
    beads = set(str(item) for item in selected["bead_ids"])
    instance = selected.get("kind") == "instance"
    result: list[LedgerRecord] = []
    for task in ledger.list_records(kind="task", limit=0):
        fc = task.fc or {}
        associated = fc.get("associated_beads")
        related = {
            str(item)
            for item in [
                fc.get("work_bead"),
                *(associated if isinstance(associated, list) else []),
            ]
            if item
        }
        if instance or beads.intersection(related):
            result.append(task)
    return result


def _repository_inventory(
    config: Mapping[str, Any], selected: Mapping[str, Any]
) -> list[dict[str, Any]]:
    projects = config.get("projects")
    if not isinstance(projects, Mapping):
        return []
    rows: list[dict[str, Any]] = []
    for project_id in selected.get("project_ids", []):
        project = projects.get(project_id)
        root = project.get("root") if isinstance(project, Mapping) else None
        if not isinstance(root, str):
            continue
        path = Path(root)
        command = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain=v1"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        rows.append(
            {
                "project": project_id,
                "root": str(path),
                "exists": path.is_dir(),
                "git_available": command.returncode == 0,
                "dirty": bool(command.stdout),
                "changes": command.stdout.splitlines()[:200],
                "error": command.stderr.strip() or None,
            }
        )
    return rows


def _target_in_scope(
    ledger: Ledger,
    config: Mapping[str, Any],
    selected: Mapping[str, Any],
    target: str,
    action: str,
) -> None:
    if action in {"repair_service", "reinstall"}:
        if selected.get("kind") != "instance":
            raise FulcrumError.invalid(
                "TARGET_OUT_OF_SCOPE", f"{action} requires instance scope"
            )
        return
    if action in {"quarantine", "git"}:
        path = Path(target).resolve(strict=False)
        if path == Path(str(config.get("_config_path", ""))).resolve(strict=False):
            raise FulcrumError(
                "CONFIG_AUTHORITY_DENIED",
                "Justiciar cannot modify authoritative fulcrum.yaml",
                exit_code=5,
            )
        roots = _allowed_paths(ledger, config, selected)
        if not any(path == root or root in path.parents for root in roots):
            raise FulcrumError.invalid(
                "TARGET_OUT_OF_SCOPE", f"path {target} is outside recovery scope"
            )
        return
    record = ledger.show(target)
    if action == "restore_leadership":
        if target != "fc-system":
            raise FulcrumError.invalid(
                "TARGET_OUT_OF_SCOPE", "leadership target must be fc-system"
            )
        return
    if record is None:
        raise FulcrumError.invalid("NOT_FOUND", f"unknown repair target {target}")
    if record.kind == "work" and target in selected["bead_ids"]:
        return
    if record.kind == "task" and record in _tasks_in_scope(ledger, selected):
        return
    raise FulcrumError.invalid(
        "TARGET_OUT_OF_SCOPE", f"target {target} is outside recovery scope"
    )


def _allowed_paths(
    ledger: Ledger, config: Mapping[str, Any], selected: Mapping[str, Any]
) -> list[Path]:
    roots: set[Path] = set()
    projects = config.get("projects")
    if isinstance(projects, Mapping):
        for project_id in selected.get("project_ids", []):
            project = projects.get(project_id)
            if isinstance(project, Mapping) and isinstance(project.get("root"), str):
                roots.add(Path(str(project["root"])).resolve(strict=False))
    for bead_id in selected.get("bead_ids", []):
        work = ledger.show(str(bead_id))
        workspace = (work.fc or {}).get("worktree") if work else None
        if isinstance(workspace, Mapping) and isinstance(workspace.get("path"), str):
            roots.add(Path(str(workspace["path"])).resolve(strict=False))
    return sorted(roots)


def _authorized_fence(
    request: ParsedRequest, control: LedgerRecord, selected: Mapping[str, Any]
) -> dict[str, Any]:
    fence = (control.fc or {}).get("active_takeover")
    if request.actor.kind == "human":
        if not isinstance(fence, Mapping):
            raise FulcrumError(
                "RECOVERY_AUTHORITY_REQUIRED", "no active recovery fence", exit_code=5
            )
    elif not isinstance(fence, Mapping) or request.thread_id != fence.get(
        "owner_thread"
    ):
        raise FulcrumError(
            "RECOVERY_AUTHORITY_REQUIRED",
            "caller does not own the active recovery fence",
            exit_code=5,
        )
    assert isinstance(fence, Mapping)
    if fence.get("state") not in ACTIVE_FENCE_STATES:
        raise FulcrumError(
            "RECOVERY_AUTHORITY_REQUIRED", "recovery fence is not active", exit_code=5
        )
    if set(fence.get("bead_ids", [])) != set(selected.get("bead_ids", [])) or set(
        fence.get("project_ids", [])
    ) != set(selected.get("project_ids", [])):
        raise FulcrumError(
            "RECOVERY_SCOPE_CONFLICT",
            "requested scope differs from the active fence",
            exit_code=5,
        )
    return dict(fence)


def _repair_adopt_owner(
    ledger: Ledger, target: str, arguments: Mapping[str, Any], operation_id: str
) -> Mapping[str, Any]:
    work = ledger.show(target)
    if work is None or work.kind != "work" or not work.fc:
        raise FulcrumError.invalid("NOT_FOUND", "adopt_owner target must be work")
    if work.fc.get("ownership_operation") != arguments["expected_ownership_operation"]:
        raise FulcrumError(
            "OWNERSHIP_CONFLICT", "expected ownership operation is stale", exit_code=5
        )
    fc = dict(work.fc)
    fc["owner"] = arguments["thread_id"]
    fc["role"] = arguments["role"]
    fc["ownership_operation"] = operation_id
    fc["last_transition"] = operation_id
    updated = ledger.update_fc(target, fc, assignee=str(arguments["thread_id"]))
    return {
        "work": updated.id,
        "owner": arguments["thread_id"],
        "role": arguments["role"],
    }


def _repair_set_disposition(
    ledger: Ledger, target: str, arguments: Mapping[str, Any], operation_id: str
) -> Mapping[str, Any]:
    work = ledger.show(target)
    if work is None or work.kind != "work" or not work.fc:
        raise FulcrumError.invalid("NOT_FOUND", "set_disposition target must be work")
    if arguments["outcome"] == "delivered":
        delivery = work.fc.get("delivery")
        promotion = delivery.get("promotion") if isinstance(delivery, Mapping) else None
        if not isinstance(promotion, Mapping) or promotion.get("state") != "observed":
            raise FulcrumError(
                "DELIVERY_NOT_OBSERVED",
                "recovery cannot claim an unobserved Git promotion",
                exit_code=5,
            )
    fc = dict(work.fc)
    fc.update(dict(arguments["new_scope"]))
    fc["phase"] = "done"
    fc["disposition"] = {
        "outcome": arguments["outcome"],
        "summary": arguments["summary"],
        "new_scope": dict(arguments["new_scope"]),
        "waived_requirements": list(arguments["waived_requirements"]),
        "known_defects": list(arguments["known_defects"]),
        "evidence": list(arguments["evidence"]),
        "completed_at": utc_now(),
        "recovery_operation": operation_id,
    }
    fc["last_transition"] = operation_id
    closed = ledger.update_fc(work.id, fc, status="closed")
    completion_cost = (
        AnalyticsService().finalize_root(ledger, closed, operation_id)
        if fc.get("workflow_root") == work.id
        else None
    )
    return {
        "bead_id": work.id,
        "disposition": fc["disposition"],
        "completion_cost": completion_cost,
    }


def _repair_restore_leadership(
    ledger: Ledger, target: str, arguments: Mapping[str, Any], operation_id: str
) -> Mapping[str, Any]:
    control = ledger.show(target)
    if control is None or control.kind != "control" or not control.fc:
        raise FulcrumError.invalid(
            "NOT_FOUND", "restore_leadership target must be fc-system"
        )
    task = _task_for_thread(ledger, str(arguments["thread_id"]))
    if task is None or not task.fc:
        raise FulcrumError.invalid("NOT_FOUND", "leadership thread is not managed")
    fc = dict(task.fc)
    fc["role"] = arguments["role"]
    fc["purpose"] = "leadership"
    fc["last_transition"] = operation_id
    ledger.update_fc(task.id, fc)
    control_fc = dict(control.fc)
    control_fc[f"{arguments['role']}_thread"] = arguments["thread_id"]
    control_fc["last_transition"] = operation_id
    ledger.update_fc(control.id, control_fc)
    return {"role": arguments["role"], "thread_id": arguments["thread_id"]}


def _repair_service(
    request: ParsedRequest, target: str, arguments: Mapping[str, Any]
) -> Mapping[str, Any]:
    labels = {
        "controller": "dev.fulcrum.controller",
        "fulcrum-controller": "dev.fulcrum.controller",
        "app-server": "dev.fulcrum.codex-app-server",
        "codex-app-server": "dev.fulcrum.codex-app-server",
    }
    if target not in labels:
        raise FulcrumError.invalid("TARGET_OUT_OF_SCOPE", "unknown owned service")
    operation = str(arguments["operation"])
    label = labels[target]
    if operation == "reinstall_definition":
        raise FulcrumError(
            "CAPABILITY_UNAVAILABLE",
            "service-definition reinstall requires the installation service",
            exit_code=4,
        )
    argv = ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"]
    if operation == "start":
        argv = ["launchctl", "kickstart", f"gui/{os.getuid()}/{label}"]
    elif operation == "stop":
        argv = [
            "launchctl",
            "kill",
            "SIGTERM",
            f"gui/{os.getuid()}/{label}",
        ]
    completed = subprocess.run(
        argv, capture_output=True, text=True, check=False, timeout=request.timeout
    )
    if completed.returncode != 0:
        raise FulcrumError(
            "SERVICE_REPAIR_FAILED",
            completed.stderr.strip() or "launchctl repair failed",
            exit_code=4,
        )
    return {
        "service": target,
        "operation": operation,
        "argv": argv,
        "stdout": completed.stdout.strip(),
    }


def _repair_reinstall(
    request: ParsedRequest,
    target: str,
    arguments: Mapping[str, Any],
    *,
    operation_key: str,
) -> Mapping[str, Any]:
    from fulcrum.install import (
        fulcrum2_service_definitions,
        install_fulcrum2_service_definitions,
    )
    from fulcrum.source_refresh import (
        build_installed_environment,
        install_recovery_link,
        switch_installed_pointer,
    )

    source = Path(str(arguments["source_root"])).resolve(strict=True)
    if not source.is_dir():
        raise FulcrumError.invalid(
            "NOT_FOUND", "reinstall source_root is not a directory"
        )
    installation = str(arguments["installation"])
    token = operation_key.removeprefix("fc-").replace("/", "-")
    if installation == "recovery":
        deployment = request.instance.instance_root / "recovery" / f"deployment-{token}"
        recovery_config: Path | None = request.instance.config_path
        try:
            ConfigurationManager(request.instance.config_path).load()
        except FulcrumError:
            recovery_config = None
        probe = build_installed_environment(
            source,
            deployment,
            config_path=recovery_config,
            recovery=True,
        )
        active = switch_installed_pointer(
            request.instance.instance_root / "recovery", deployment
        )
        launcher = install_recovery_link(
            request.instance.instance_root,
            production=not request.instance.explicit_selection,
        )
        return {
            "target": target,
            "installation": installation,
            "source_root": str(source),
            "active": str(active),
            "launcher": str(launcher),
            "probe": probe,
            "development_environment_used": False,
        }
    manager = ConfigurationManager(request.instance.config_path)
    document, _ = manager.load()
    config = manager.effective(document)
    if request.instance.brain_root is None:
        raise FulcrumError(
            "CONFIG_INVALID", "main reinstall requires a valid brain root", exit_code=4
        )
    deployment = request.instance.instance_root / "runtime" / f"deployment-{token}"
    probe = build_installed_environment(
        source,
        deployment,
        config_path=request.instance.config_path,
        recovery=False,
    )
    active = switch_installed_pointer(
        request.instance.instance_root / "runtime", deployment
    )
    definitions = fulcrum2_service_definitions(
        instance_root=request.instance.instance_root,
        config_path=request.instance.config_path,
        brain_root=request.instance.brain_root,
        config=config,
        controller_executable=active / "bin" / "fulcrum",
        production=not request.instance.explicit_selection,
    )
    _installed, changed = install_fulcrum2_service_definitions(
        definitions, request.instance.instance_root
    )
    return {
        "target": target,
        "installation": installation,
        "source_root": str(source),
        "active": str(active),
        "probe": probe,
        "changed_service_definitions": changed,
        "controller_started": False,
        "development_environment_used": False,
    }


def _repair_quarantine(
    request: ParsedRequest, target: str, operation_id: str, index: int
) -> Mapping[str, Any]:
    source = Path(target).resolve(strict=True)
    if source == request.instance.config_path.resolve(strict=False):
        raise FulcrumError(
            "CONFIG_AUTHORITY_DENIED",
            "Justiciar cannot quarantine fulcrum.yaml",
            exit_code=5,
        )
    destination = source.with_name(
        f"{source.name}.quarantine-{operation_id[-8:]}-{index}"
    )
    if destination.exists():
        raise FulcrumError(
            "QUARANTINE_CONFLICT", "planned quarantine path exists", exit_code=5
        )
    shutil.move(str(source), str(destination))
    return {
        "source": str(source),
        "quarantine_path": str(destination),
        "retained": destination.exists(),
    }


def _effect_plan(
    action: Mapping[str, Any], operation_id: str, index: int
) -> Mapping[str, Any]:
    if action.get("action") == "replace_thread":
        return {
            "old_task_record_id": action.get("target"),
            "new_task_record_id": exact_successor_record_id(
                operation_id, index, str(action.get("target"))
            ),
            "mode": (action.get("arguments") or {}).get("mode"),
        }
    if action.get("action") == "quarantine":
        source = Path(str(action["target"])).resolve(strict=False)
        return {
            "source": str(source),
            "quarantine_path": str(
                source.with_name(
                    f"{source.name}.quarantine-{operation_id[-8:]}-{index}"
                )
            ),
        }
    return {
        "action": action.get("action"),
        "target": action.get("target"),
        "arguments": dict(action.get("arguments") or {}),
    }


def _repair_beads_update(
    ledger: Ledger, target: str, arguments: Mapping[str, Any]
) -> Mapping[str, Any]:
    fields = dict(arguments["fields"])
    argv = ["update", target]
    for name, value in fields.items():
        argv.extend((f"--{name.replace('_', '-')}", str(value)))
    ledger.run(argv, mutating=True)
    observed = ledger.show(target)
    if observed is None:
        raise LedgerFailure(
            "beads update target disappeared",
            category="uncertain",
            retryable=True,
            uncertain=True,
        )
    return {"bead_id": target, "fields": fields, "observed": observed.native}


def _repair_git(target: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    root = Path(target).resolve(strict=True)
    if not root.is_dir():
        raise FulcrumError.invalid("NOT_FOUND", "git target must be an exact directory")
    argv = ["git", *list(arguments["argv"])]
    completed = subprocess.run(
        argv, cwd=root, capture_output=True, text=True, check=False, timeout=30
    )
    result = {
        "cwd": str(root),
        "argv": argv,
        "returncode": completed.returncode,
        "stdout": completed.stdout[-262144:],
        "stderr": completed.stderr[-262144:],
    }
    if completed.returncode != 0:
        raise FulcrumError(
            "GIT_REPAIR_FAILED",
            completed.stderr.strip() or "git command failed",
            exit_code=5,
            details=result,
        )
    return result


def _observe_target(
    ledger: Ledger, target: str, request: ParsedRequest
) -> Mapping[str, Any]:
    record = ledger.show(target)
    if record is not None:
        result: dict[str, Any] = {
            "kind": "record",
            "id": record.id,
            "record_kind": record.kind,
            "status": record.status,
            "assignee": record.assignee,
            "fc": record.fc,
        }
        if record.kind == "task" and request.runtime_submit is not None:
            try:
                facts = _runtime_call(
                    request,
                    lambda runtime: runtime.inspect_task(
                        str((record.fc or {}).get("thread_id") or "")
                    ),
                )
                result["runtime"] = facts.to_dict()
            except FulcrumError as error:
                result["runtime_gap"] = error.message
        return result
    path = Path(target).resolve(strict=False)
    return {
        "kind": "path",
        "path": str(path),
        "exists": path.exists(),
        "is_dir": path.is_dir(),
        "is_file": path.is_file(),
    }


def _write_control_fence(
    ledger: Ledger, control: LedgerRecord, fence: Mapping[str, Any]
) -> None:
    fc = dict(control.fc or {})
    fc["active_takeover"] = dict(fence)
    fc["last_transition"] = fence.get("operation_id")
    ledger.update_fc(control.id, fc)


def _set_fence_state(
    ledger: Ledger, fence: Mapping[str, Any], state: str, **values: Any
) -> None:
    control = _control(ledger)
    current = (control.fc or {}).get("active_takeover")
    if not isinstance(current, Mapping) or current.get("operation_id") != fence.get(
        "operation_id"
    ):
        raise FulcrumError(
            "RECOVERY_AUTHORITY_LOST", "active fence changed", exit_code=5
        )
    updated = {**dict(current), "state": state, **values}
    _write_control_fence(ledger, control, updated)
    for bead_id in updated.get("bead_ids", []):
        work = ledger.show(str(bead_id))
        if work is None or work.kind != "work" or not work.fc:
            continue
        fc = dict(work.fc)
        work_fence = fc.get("recovery_fence")
        if not isinstance(work_fence, Mapping) or work_fence.get(
            "operation_id"
        ) != updated.get("operation_id"):
            continue
        fc["recovery_fence"] = dict(updated)
        fc["last_transition"] = updated.get("operation_id")
        ledger.update_fc(work.id, fc)


def _control(ledger: Ledger) -> LedgerRecord:
    control = ledger.show("fc-system")
    if control is None or control.kind != "control" or not control.fc:
        raise FulcrumError(
            "CONTROL_UNAVAILABLE",
            "recovery requires the Fulcrum control record",
            exit_code=4,
        )
    return control


def _select_justiciar_thread(
    ledger: Ledger, request: ParsedRequest, control: LedgerRecord
) -> str | None:
    if request.thread_id:
        task = _task_for_thread(ledger, request.thread_id)
        if task is not None:
            return request.thread_id
    marshal = (control.fc or {}).get("marshal_thread")
    return (
        str(marshal)
        if isinstance(marshal, str) and _task_for_thread(ledger, marshal)
        else None
    )


def _task_for_thread(ledger: Ledger, thread_id: str) -> LedgerRecord | None:
    return next(
        (
            task
            for task in ledger.list_records(kind="task", limit=0)
            if (task.fc or {}).get("thread_id") == thread_id
        ),
        None,
    )


def _retained_observation(task: LedgerRecord) -> Mapping[str, Any]:
    value = (task.fc or {}).get("last_observed")
    return dict(value) if isinstance(value, Mapping) else {}


def _child_request(
    request: ParsedRequest,
    operation_id: str,
    index: int,
    *,
    command: tuple[str, ...],
    arguments: Mapping[str, Any],
) -> ParsedRequest:
    return replace(
        request,
        command=command,
        arguments=dict(arguments),
        input={},
        actor=ActorContext(kind="controller"),
        request_id=str(
            uuid.uuid5(
                RECOVERY_NAMESPACE, f"{operation_id}:{index}:{'.'.join(command)}"
            )
        ),
        offline=True,
    )


def _actor_has_role(request: ParsedRequest, role: str) -> bool:
    try:
        ledger = _ledger(request)
    except Exception:
        return False
    task = _task_for_thread(
        ledger, str(request.thread_id or request.actor.task_id or "")
    )
    return bool(task and task.fc and task.fc.get("role") == role)


def _authorize_recovery_actor(request: ParsedRequest) -> None:
    if request.actor.kind == "human":
        return
    if any(_actor_has_role(request, role) for role in ("marshal", "justiciar")):
        return
    raise FulcrumError(
        "ROLE_AUTHORITY_DENIED",
        "recovery takeover requires human, Marshal, or Justiciar authority",
        exit_code=5,
    )


def _marshal_thread(ledger: Ledger) -> str | None:
    control = ledger.show("fc-system")
    value = (control.fc or {}).get("marshal_thread") if control else None
    return str(value) if isinstance(value, str) else None


def _waiting_reasons(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Mapping):
        return []
    reasons = value.get("reasons")
    return (
        [dict(item) for item in reasons if isinstance(item, Mapping)]
        if isinstance(reasons, list)
        else []
    )


def _config(request: ParsedRequest) -> dict[str, Any]:
    manager = ConfigurationManager(request.instance.config_path)
    document, _ = manager.load()
    config = manager.effective(document)
    config["_config_path"] = str(request.instance.config_path.resolve(strict=False))
    return config


def _ledger(request: ParsedRequest) -> Ledger:
    if request.instance.brain_root is None:
        raise FulcrumError(
            "LEDGER_UNAVAILABLE", "recovery requires a configured brain", exit_code=4
        )
    config = _config(request)
    beads = config["beads"]
    return Ledger(
        request.instance.brain_root,
        executable=str(beads.get("executable")) if beads.get("executable") else None,
        timeout=request.timeout,
    )


def _operation_result(operation: OperationRecord) -> CommandResult:
    value = str(operation.operation.get("state", "running"))
    state = (
        CommandState(value)
        if value in CommandState._value2member_map_
        else CommandState.RUNNING
    )
    return CommandResult(
        ok=state not in {CommandState.FAILED, CommandState.UNCERTAIN},
        state=state,
        operation_id=operation.id,
        request_id=str(operation.operation.get("request_id")),
        result=operation_view(operation),
    )


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value)
