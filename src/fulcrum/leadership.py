"""Standing leadership, decision briefs, and capacity-bounded admission."""

from __future__ import annotations

from fulcrum.coordination import coordinated

import json
import re
import threading
import uuid
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
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
    LedgerFailure,
    LedgerRecord,
    OperationRecord,
    operation_id,
    operation_view,
    utc_now,
)
from fulcrum.knowledge import select_memory
from fulcrum.roles import LEADERSHIP_TITLES, RoleService, fallback_instructions
from fulcrum.runtime import AppServerError, TaskFacts, TaskSpec
from fulcrum.work import WorkService, work_view

BRIEF_CHARACTER_LIMIT = 6000
BRIEF_ROW_LIMIT = 12
DECISION_KINDS = ("groom", "dispatch", "recover")
DECISION_ACTIONS = {
    "dispatch",
    "defer",
    "clarify",
    "duplicate",
    "reject",
    "recover",
    "human",
}
SATISFYING_DEPENDENCY_OUTCOMES = {"delivered", "answered", "findings", "duplicate"}
ADMISSION_NAMESPACE = uuid.UUID("f532b5b0-53d8-43eb-bf33-e786623536dc")
LEADERSHIP_NAMESPACE = uuid.UUID("242a7826-9e10-43a8-8c66-9294ecdd5b5d")


class LeadershipService:
    """Public leadership projections and receipt-backed Marshal decisions."""

    def __init__(self) -> None:
        self._admission = AdmissionService()

    @coordinated
    def leader_show(self, request: ParsedRequest) -> CommandResult:
        role = str(request.arguments["role"])
        if role not in LEADERSHIP_TITLES:
            raise FulcrumError.invalid(
                "INVALID_ROLE", "leader must be vizier or marshal"
            )
        ledger = _ledger(request)
        control = ledger.show("fc-system")
        control_fc = control.fc if control and control.fc else {}
        thread_id = control_fc.get(f"{role}_thread")
        task = _task_for_thread(ledger, str(thread_id)) if thread_id else None
        return CommandResult.query(
            {
                "role": role,
                "title": LEADERSHIP_TITLES[role],
                "thread_id": thread_id,
                "available": isinstance(thread_id, str) and task is not None,
                "control_bead": control.id if control else None,
                "creation_operation": (
                    task.fc.get("creation_operation") if task and task.fc else None
                ),
                "task_record_id": task.id if task else None,
                "accountable_owner": control_fc.get("owner", "HUMAN"),
                "next_commands": (
                    []
                    if isinstance(thread_id, str) and task is not None
                    else [["fulcrum", "reconcile", "--json"]]
                ),
            }
        )

    @coordinated
    def marshal_brief(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        config = _config(request)
        brief, _ = build_brief(
            ledger,
            config,
            requested_kind=str(request.arguments.get("kind", "auto")),
            bead_id=_optional_string(request.arguments.get("bead")),
        )
        return CommandResult.query(brief)

    @coordinated
    def marshal_request(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        _require_marshal_or_human(request, ledger)
        config = _config(request)
        outstanding = _outstanding_decision(ledger)
        if outstanding is not None:
            return _operation_result(outstanding)
        brief, comparisons = build_brief(
            ledger,
            config,
            requested_kind=str(request.arguments.get("kind", "auto")),
            bead_id=_optional_string(request.arguments.get("bead")),
        )
        operation, reused = ledger.create_operation(
            request,
            owner=_marshal_owner(ledger),
            planned={
                "brief": brief,
                "selected_ids": [row["bead_id"] for row in brief["rows"]],
                "comparison_facts": comparisons,
                "serialized_brief": _serialize(brief),
            },
            next_action=(
                "Marshal must return one decision for each unchanged selected row."
                if brief["decision_required"]
                else "No Marshal judgment is currently required."
            ),
        )
        if reused:
            return _operation_result(operation)
        if not brief["decision_required"]:
            operation = ledger.update_operation(
                operation,
                state="completed",
                step="no_decision_required",
                result={
                    "decision_required": False,
                    "kind": brief["kind"],
                    "selected_ids": [],
                    "brief": brief,
                    "task": None,
                    "turn": None,
                },
                next_action="No Marshal turn was started.",
            )
            return _operation_result(operation)

        # Task 06 owns transport recovery. Dispatching the decision prompt through
        # the standing task is retained on this receipt before the external call.
        marshal = _marshal_task(ledger)
        if marshal is None or not marshal.fc:
            operation = ledger.update_operation(
                operation,
                state="failed",
                step="marshal_unavailable",
                error={
                    "code": "LEADERSHIP_UNAVAILABLE",
                    "message": "standing Marshal task is not established",
                    "retryable": True,
                },
                next_action="Reconcile standing leadership, then request the decision again.",
            )
            return _operation_result(operation)
        from fulcrum.runtime import TurnInput
        from fulcrum.runtime_service import (
            _runtime_call,
            routing_developer_instructions,
        )

        fc = marshal.fc
        turn = _runtime_call(
            request,
            lambda runtime: runtime.start_turn(
                str(fc["thread_id"]),
                TurnInput(
                    text=_marshal_decision_prompt(
                        operation.id,
                        str(operation.operation["planned"]["serialized_brief"]),
                    ),
                    cwd=str(fc.get("creation_cwd") or request.instance.instance_root),
                    workspace_roots=tuple(
                        str(item)
                        for item in (
                            (fc.get("last_observed") or {}).get("workspace_roots")
                            or [
                                fc.get("creation_cwd") or request.instance.instance_root
                            ]
                        )
                    ),
                    model=str(fc.get("model")),
                    effort=str(fc.get("effort")),
                    operation_id=operation.id,
                    ownership_operation=None,
                    developer_instructions=routing_developer_instructions(request),
                ),
            ),
        )
        marshal_fc = dict(fc)
        marshal_fc["last_turn"] = turn.to_dict()
        marshal_fc["last_transition"] = operation.id
        ledger.update_fc(marshal.id, marshal_fc)
        operation = ledger.update_operation(
            operation,
            state="running",
            step="decision_turn_started",
            external={"thread_id": fc["thread_id"], "turn_id": turn.id},
            result={
                "decision_required": True,
                "kind": brief["kind"],
                "selected_ids": [row["bead_id"] for row in brief["rows"]],
                "brief": brief,
                "task": marshal.id,
                "turn": turn.to_dict(),
            },
            next_action="Wait for Marshal decisions tied to this receipt.",
        )
        return _operation_result(operation)

    @coordinated
    def marshal_decide(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        _require_marshal_or_human(request, ledger)
        decision_operation_id = request.input.get("decision_operation")
        decisions = request.input.get("decisions")
        if not isinstance(decision_operation_id, str) or not decision_operation_id:
            raise FulcrumError.invalid(
                "INVALID_INPUT", "marshal decide requires decision_operation"
            )
        if not isinstance(decisions, list) or not decisions:
            raise FulcrumError.invalid(
                "INVALID_INPUT", "marshal decide requires a nonempty decisions array"
            )
        decision_record = ledger.show(decision_operation_id)
        if decision_record is None or decision_record.kind != "operation":
            raise FulcrumError.invalid(
                "NOT_FOUND", f"unknown decision operation {decision_operation_id}"
            )
        decision_operation = OperationRecord.from_record(decision_record)
        if decision_operation.operation.get(
            "command"
        ) != "marshal.request" or decision_operation.operation.get("state") not in {
            "accepted",
            "running",
        }:
            raise FulcrumError(
                "STALE_DECISION",
                "decision operation is not an outstanding Marshal request",
                exit_code=5,
                operation_id=decision_operation.id,
            )
        planned = decision_operation.operation.get("planned")
        comparisons = (
            planned.get("comparison_facts") if isinstance(planned, Mapping) else None
        )
        if not isinstance(comparisons, Mapping):
            raise FulcrumError(
                "OPERATION_CORRUPT",
                "decision receipt has no comparison facts",
                exit_code=4,
            )
        supplied_ids = [
            item.get("bead_id")
            for item in decisions
            if isinstance(item, Mapping) and isinstance(item.get("bead_id"), str)
        ]
        if (
            len(supplied_ids) != len(decisions)
            or len(set(supplied_ids)) != len(supplied_ids)
            or set(supplied_ids) != set(comparisons)
        ):
            raise FulcrumError.invalid(
                "INVALID_DECISION",
                "decisions must name each selected bead exactly once",
                details={
                    "selected_ids": sorted(str(item) for item in comparisons),
                    "supplied_ids": supplied_ids,
                },
            )
        receipt, reused = ledger.create_operation(
            request,
            owner=_marshal_owner(ledger),
            planned={"decision_operation": decision_operation_id},
            next_action="Apply each unchanged decision independently.",
        )
        if reused and receipt.operation.get("state") in {
            "completed",
            "failed",
            "cancelled",
        }:
            return _operation_result(receipt)
        seen: set[str] = set()
        results: list[dict[str, Any]] = []
        for supplied in decisions:
            if not isinstance(supplied, Mapping):
                results.append(
                    {
                        "applied": False,
                        "code": "INVALID_DECISION",
                        "reason": "row is not an object",
                    }
                )
                continue
            bead_id = _optional_string(supplied.get("bead_id"))
            if bead_id is None or bead_id in seen:
                results.append(
                    {
                        "bead_id": bead_id,
                        "applied": False,
                        "code": "INVALID_DECISION",
                        "reason": "bead_id is missing or duplicated",
                    }
                )
                continue
            seen.add(bead_id)
            expected = comparisons.get(bead_id)
            current_record = ledger.show(bead_id)
            if not isinstance(expected, Mapping) or current_record is None:
                results.append(
                    {
                        "bead_id": bead_id,
                        "applied": False,
                        "code": "STALE_DECISION",
                        "reason": "work is absent from the retained decision batch",
                    }
                )
                continue
            current = comparison_facts(ledger, current_record, _config(request))
            if (
                current != dict(expected)
                or supplied.get("expected_ownership_operation")
                != current.get("ownership_operation")
                or supplied.get("expected_phase") != current.get("phase")
            ):
                results.append(
                    {
                        "bead_id": bead_id,
                        "applied": False,
                        "code": "STALE_DECISION",
                        "reason": "decision-relevant work facts changed",
                        "expected": dict(expected),
                        "current": current,
                    }
                )
                continue
            try:
                updated = _apply_decision(
                    ledger, current_record, supplied, decision_operation_id
                )
            except FulcrumError as error:
                results.append(
                    {
                        "bead_id": bead_id,
                        "applied": False,
                        "code": error.code,
                        "reason": error.message,
                    }
                )
                continue
            results.append(
                {
                    "bead_id": bead_id,
                    "applied": True,
                    "action": supplied.get("action"),
                    "work": work_view(ledger, updated),
                }
            )
        decision_operation = ledger.update_operation(
            decision_operation,
            state="completed",
            step="decisions_applied",
            result={
                "decision_operation": decision_operation_id,
                "rows": results,
            },
            next_action="Inspect row conflicts; mechanically start unchanged authorizations.",
        )
        receipt = ledger.update_operation(
            receipt,
            state="completed",
            step="independent_rows_applied",
            result={
                "decision_operation": decision_operation.id,
                "rows": results,
                "applied": sum(bool(row.get("applied")) for row in results),
                "conflicts": sum(not bool(row.get("applied")) for row in results),
            },
            next_action="Dispatch authorization is durable; capacity changes need no new judgment.",
        )
        return _operation_result(receipt)

    @coordinated
    def backlog_list(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        include_deferred = bool(request.arguments.get("include_deferred", False))
        ready_only = bool(request.arguments.get("ready", False))
        limit = _limit(request.arguments.get("limit"))
        items: list[dict[str, Any]] = []
        for record in _open_work(ledger):
            fc = record.fc or {}
            deferred = bool(_waiting_reasons(fc.get("waiting")))
            ready, blockers = dependency_readiness(ledger, record)
            if deferred and not include_deferred:
                continue
            if ready_only and (not ready or fc.get("dispatch") is None):
                continue
            items.append(
                {
                    "id": record.id,
                    "title": record.title,
                    "priority": _priority(record),
                    "project": fc.get("project"),
                    "owner": fc.get("owner") or _marshal_owner(ledger),
                    "role": fc.get("role"),
                    "phase": fc.get("phase", "intake"),
                    "requested_role": fc.get("requested_role"),
                    "ready": ready,
                    "dependency_blockers": blockers,
                    "waiting": fc.get("waiting"),
                    "dispatch": fc.get("dispatch"),
                    "next_action": fc.get("next_action"),
                }
            )
        items.sort(key=lambda item: (int(item["priority"]), str(item["id"])))
        selected = items if limit == 0 else items[:limit]
        return CommandResult.query(
            {
                "items": selected,
                "next_cursor": None,
                "omitted": len(items) - len(selected),
            }
        )

    @coordinated
    def dispatch(self, request: ParsedRequest) -> CommandResult:
        return self._admission.dispatch(request)


class AdmissionService:
    """Short reservation section followed by idempotent role entry."""

    def __init__(self) -> None:
        self._capacity_lock = threading.RLock()
        self._bead_locks_guard = threading.Lock()
        self._bead_locks: dict[str, threading.RLock] = {}

    def _bead_lock(self, bead_id: str) -> threading.RLock:
        with self._bead_locks_guard:
            return self._bead_locks.setdefault(bead_id, threading.RLock())

    @coordinated
    def dispatch(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        bead_id = str(request.arguments["bead"])
        authorize = bool(request.arguments.get("authorize", False))
        human_bypass = bool(request.arguments.get("human", False))
        with self._bead_lock(bead_id):
            record = _work(ledger, bead_id)
            fc = dict(record.fc or {})
            if authorize or human_bypass:
                _require_authorizer(request, ledger, human_bypass=human_bypass)
                role = str(fc.get("requested_role") or "weaver")
                operation, reused = ledger.create_operation(
                    request,
                    bead_id=bead_id,
                    owner=_marshal_owner(ledger),
                    planned={"role": role, "human_bypass": human_bypass},
                    next_action="Persist dispatch authorization before reserving a start.",
                )
                if reused and operation.operation.get("state") in {
                    "completed",
                    "failed",
                    "uncertain",
                    "cancelled",
                }:
                    return _operation_result(operation)
                fc["dispatch"] = {
                    "role": role,
                    "reason": (
                        "explicit human policy bypass"
                        if human_bypass
                        else "explicit operator authorization"
                    ),
                    "decision_operation": operation.id,
                    "authorized_at": utc_now(),
                    "human_bypass": human_bypass,
                    "reservation": None,
                }
                fc["next_action"] = (
                    "Start the authorized role when dependencies and capacity allow."
                )
                fc["last_transition"] = operation.id
                record = ledger.update_fc(record.id, fc)
            else:
                operation, reused = ledger.create_operation(
                    request,
                    bead_id=bead_id,
                    owner=_marshal_owner(ledger),
                    planned={"authorization": fc.get("dispatch")},
                    next_action="Inspect dependencies and reserve an eligible authorized start.",
                )
                if reused and operation.operation.get("state") in {
                    "completed",
                    "failed",
                    "uncertain",
                    "cancelled",
                }:
                    return _operation_result(operation)
            dispatch = fc.get("dispatch")
            if not isinstance(dispatch, Mapping):
                operation = ledger.update_operation(
                    operation,
                    state="failed",
                    step="authorization_missing",
                    error={
                        "code": "DISPATCH_NOT_AUTHORIZED",
                        "message": "work has no durable dispatch authorization",
                        "retryable": False,
                    },
                    next_action="Request a grooming or dispatch decision.",
                )
                return _operation_result(operation)
            plan = fc.get("plan")
            if isinstance(plan, Mapping) and plan.get("activation") == "future":
                _set_waiting(
                    fc,
                    reason_id=f"future-plan:{record.id}",
                    reason="future plan has no human/Vizier activation authorization",
                    triggers=[{"event": "policy_changed", "subject": record.id}],
                    decision_operation=str(dispatch.get("decision_operation")),
                    kind="future_activation",
                )
                fc["next_action"] = (
                    "Human or Vizier must authorize, then Marshal must execute, the current approved future plan."
                )
                ledger.update_fc(record.id, fc)
                operation = ledger.update_operation(
                    operation,
                    state="failed",
                    step="future_activation_required",
                    error={
                        "code": "ACTIVATION_REQUIRED",
                        "message": "Marshal dispatch cannot activate future work",
                        "retryable": False,
                    },
                    next_action=fc["next_action"],
                )
                return _operation_result(operation)
            if isinstance(plan, Mapping) and plan.get("publication_ready") is False:
                _set_waiting(
                    fc,
                    reason_id=f"plan-publication:{record.id}",
                    reason="required remote plan publication has not been observed",
                    triggers=[{"event": "external_changed", "subject": record.id}],
                    decision_operation=str(dispatch.get("decision_operation")),
                    kind="external",
                )
                fc["next_action"] = (
                    "Observe required remote plan publication before implementation starts."
                )
                ledger.update_fc(record.id, fc)
                operation = ledger.update_operation(
                    operation,
                    state="failed",
                    step="plan_publication_required",
                    error={
                        "code": "PUBLICATION_NOT_READY",
                        "message": "required remote plan publication is not observed",
                        "retryable": False,
                    },
                    next_action=fc["next_action"],
                )
                return _operation_result(operation)
            if isinstance(plan, Mapping) and plan.get("authoring_ready") is False:
                _set_waiting(
                    fc,
                    reason_id=f"plan-authoring:{record.id}",
                    reason="Weaver has not finished published plan authoring",
                    triggers=[{"event": "owner_changed", "subject": record.id}],
                    decision_operation=str(dispatch.get("decision_operation")),
                    kind="authoring",
                )
                fc["next_action"] = (
                    "Weaver must finish the published plan authoring turn."
                )
                ledger.update_fc(record.id, fc)
                operation = ledger.update_operation(
                    operation,
                    state="failed",
                    step="plan_authoring_not_finished",
                    error={
                        "code": "PLAN_AUTHORING_NOT_FINISHED",
                        "message": "published plan authoring is not finished",
                        "retryable": False,
                    },
                    next_action=fc["next_action"],
                )
                return _operation_result(operation)
            ready, blockers = dependency_readiness(ledger, record)
            if not ready:
                _set_waiting(
                    fc,
                    reason_id=f"dependencies:{operation.id}",
                    reason="dispatch dependencies are not satisfied",
                    triggers=[
                        {"event": "dependency_closed", "subject": dependency}
                        for dependency in blockers
                    ],
                    decision_operation=str(dispatch.get("decision_operation")),
                )
                fc["next_action"] = (
                    "Wait for the named dependencies to satisfy their declared outcomes."
                )
                ledger.update_fc(record.id, fc)
                operation = ledger.update_operation(
                    operation,
                    state="completed",
                    step="authorized_start_waiting",
                    result={
                        "bead_id": bead_id,
                        "started": False,
                        "queued": True,
                        "blockers": blockers,
                    },
                    next_action=fc["next_action"],
                )
                return _operation_result(operation)
            config = _config(request)
            project = str(fc.get("project") or "")
            paused = project in config["policy"].get("paused_projects", [])
            reservation_lock = nullcontext() if paused else self._capacity_lock
            with reservation_lock:
                capacity = capacity_snapshot(ledger, config)
                project_row = capacity["projects"].get(project, {})
                at_limit = (
                    capacity["occupied"] >= capacity["global_limit"]
                    or int(project_row.get("occupied", 0))
                    >= int(project_row.get("limit", capacity["default_project_limit"]))
                    or project in capacity["paused_projects"]
                )
                bypass = bool(dispatch.get("human_bypass"))
                if at_limit and not bypass:
                    _set_waiting(
                        fc,
                        reason_id=f"capacity:{dispatch.get('decision_operation')}",
                        reason="authorized start is queued at configured capacity",
                        triggers=[
                            {"event": "capacity_available", "subject": project or None}
                        ],
                        decision_operation=str(dispatch.get("decision_operation")),
                    )
                    fc["next_action"] = (
                        "Start mechanically when configured capacity becomes available."
                    )
                    ledger.update_fc(record.id, fc)
                    operation = ledger.update_operation(
                        operation,
                        state="completed",
                        step="authorized_start_queued",
                        result={
                            "bead_id": bead_id,
                            "started": False,
                            "queued": True,
                            "capacity": capacity,
                        },
                        next_action=fc["next_action"],
                    )
                    return _operation_result(operation)
                reservation = dispatch.get("reservation")
                if (
                    not isinstance(reservation, Mapping)
                    or reservation.get("operation_id") != operation.id
                ):
                    dispatch = dict(dispatch)
                    dispatch["reservation"] = {
                        "operation_id": operation.id,
                        "state": "in_flight",
                        "reserved_at": utc_now(),
                        "human_bypass": bypass,
                    }
                    fc["dispatch"] = dispatch
                    fc["waiting"] = _remove_waiting_events(
                        fc.get("waiting"),
                        {"capacity_available", "dependency_closed"},
                    )
                    fc["active_operation"] = operation.id
                    fc["last_transition"] = operation.id
                    ledger.update_fc(record.id, fc)

        role = str(dispatch.get("role") or fc.get("requested_role") or "weaver")
        entry_request = ParsedRequest(
            command=("enter",),
            arguments={"role": role, "origin": "dispatch"},
            input={
                "bead": bead_id,
                "description": str(
                    fc.get("next_action")
                    or fc.get("outcome")
                    or f"Carry out authorized {role} work."
                ),
            },
            actor=ActorContext(kind="controller"),
            instance=request.instance,
            request_id=str(
                uuid.uuid5(ADMISSION_NAMESPACE, f"{operation.id}:{bead_id}:{role}")
            ),
            project=_optional_string(fc.get("project")),
            timeout=request.timeout,
            runtime_submit=request.runtime_submit,
        )
        result = RoleService().enter(entry_request)
        with self._bead_lock(bead_id):
            entry_record = (
                ledger.show(str(result.operation_id)) if result.operation_id else None
            )
            entry_state = (
                entry_record.fc.get("state")
                if entry_record is not None and entry_record.fc
                else None
            )
            unresolved = (
                result.state is CommandState.DEGRADED and entry_state != "failed"
            )
            started = result.state is not CommandState.DEGRADED
            current = _work(ledger, bead_id)
            current_fc = dict(current.fc or {})
            current_dispatch = dict(current_fc.get("dispatch") or dispatch)
            current_reservation = dict(current_dispatch.get("reservation") or {})
            current_reservation["state"] = (
                "started" if started else ("unknown" if unresolved else "failed")
            )
            current_reservation["entry_operation"] = result.operation_id
            current_reservation["settled_at"] = utc_now()
            current_dispatch["reservation"] = current_reservation
            current_fc["dispatch"] = current_dispatch
            current_fc["active_operation"] = None
            ledger.update_fc(current.id, current_fc)
            operation = ledger.update_operation(
                operation,
                state=(
                    "completed"
                    if started
                    else ("uncertain" if unresolved else "failed")
                ),
                step=(
                    "authorized_role_started"
                    if started
                    else ("start_unresolved" if unresolved else "start_failed")
                ),
                external={"entry_operation": result.operation_id},
                result={
                    "bead_id": bead_id,
                    "started": started,
                    "queued": False,
                    "human_bypass": bypass,
                    "entry": result.to_dict(),
                },
                next_action=(
                    "Observe the admitted native task."
                    if started
                    else (
                        "Inspect the retained reservation and native task before replay."
                        if unresolved
                        else "Repair the failed prerequisite before requesting a new start."
                    )
                ),
            )
        return _operation_result(operation)


async def ensure_leadership(
    request: ParsedRequest,
    ledger: Ledger,
    runtime: Any,
    config: Mapping[str, Any],
    *,
    send_initial_requests: bool = False,
) -> list[dict[str, Any]]:
    """Provision standing identities and optionally initialize empty native tasks."""

    control = ledger.show("fc-system")
    if control is None:
        control = ledger.create_record(
            record_id="fc-system",
            kind="control",
            title="Fulcrum control",
            description="Standing leadership identities and installation binding.",
            owner="HUMAN",
            fc={
                "kind": "control",
                "owner": "HUMAN",
                "instance_root": str(request.instance.instance_root),
                "brain_root": (
                    str(request.instance.brain_root)
                    if request.instance.brain_root is not None
                    else None
                ),
                "vizier_thread": None,
                "marshal_thread": None,
                "vizier_creation_operation": None,
                "marshal_creation_operation": None,
                "active_takeover": None,
                "last_transition": None,
            },
        )
    if not control.fc or control.fc.get("kind") != "control":
        raise FulcrumError(
            "CONTROL_CONFLICT",
            "reserved fc-system is not a Fulcrum control record",
            exit_code=5,
        )
    expected_instance = str(request.instance.instance_root)
    expected_brain = (
        str(request.instance.brain_root) if request.instance.brain_root else None
    )
    if control.fc.get("instance_root") not in {
        None,
        expected_instance,
    } or control.fc.get("brain_root") not in {None, expected_brain}:
        raise FulcrumError(
            "INSTANCE_CONFLICT",
            "the ledger is already bound to a different Fulcrum instance",
            exit_code=5,
            details={
                "recorded_instance_root": control.fc.get("instance_root"),
                "recorded_brain_root": control.fc.get("brain_root"),
                "requested_instance_root": expected_instance,
                "requested_brain_root": expected_brain,
            },
        )
    if control.fc.get("instance_root") is None or control.fc.get("brain_root") is None:
        bound_fc = dict(control.fc)
        bound_fc["instance_root"] = expected_instance
        bound_fc["brain_root"] = expected_brain
        control = ledger.update_fc(
            control.id,
            bound_fc,
            assignee=str(bound_fc.get("owner") or "HUMAN"),
        )
    takeover = (control.fc or {}).get("active_takeover")
    if isinstance(takeover, Mapping) and takeover.get("state") in {
        "acquiring",
        "stopping",
        "active",
        "repairing",
        "failed",
    }:
        return [
            {
                "role": "justiciar",
                "thread_id": takeover.get("owner_thread"),
                "created": False,
                "recovery_fence": dict(takeover),
            }
        ]
    actions: list[dict[str, Any]] = []
    connection_error: AppServerError | None = None
    known_tasks = {
        str(record.fc.get("thread_id")): record
        for record in ledger.list_records(kind="task", limit=0)
        if record.fc and record.fc.get("thread_id")
    }
    retired_threads = {
        thread_id
        for thread_id, record in known_tasks.items()
        if (record.fc or {}).get("deleted_at") is not None
        or (record.fc or {}).get("replaced_by") is not None
    }
    current_control = control
    for role in ("vizier", "marshal"):
        current = current_control
        current_fc = dict(current.fc or {})
        recorded_thread = _optional_string(current_fc.get(f"{role}_thread"))
        stale_task: LedgerRecord | None = None
        if recorded_thread and recorded_thread in known_tasks:
            try:
                if connection_error is not None:
                    raise connection_error
                await runtime.connect()
                observed = await runtime.inspect_task(recorded_thread)
            except AppServerError as error:
                if error.category in {"unavailable", "transient"}:
                    connection_error = error
                observed = None
            if observed is not None and observed.exists:
                action = {
                    "role": role,
                    "thread_id": recorded_thread,
                    "created": False,
                }
                if send_initial_requests:
                    action.update(
                        await _send_initial_leadership_request(
                            request,
                            ledger,
                            runtime,
                            config,
                            role=role,
                            task=known_tasks[recorded_thread],
                            native=observed,
                        )
                    )
                actions.append(action)
                continue
            stale_task = known_tasks[recorded_thread]
        request_id = str(
            uuid.uuid5(
                LEADERSHIP_NAMESPACE,
                (
                    f"{expected_instance}:{expected_brain}:{role}:standing"
                    if stale_task is None
                    else f"{expected_instance}:{expected_brain}:{role}:standing-repair:{recorded_thread}"
                ),
            )
        )
        task_record_id = f"fc-{uuid.uuid5(LEADERSHIP_NAMESPACE, request_id).hex[:8]}"
        creation_cwd = str(
            (request.instance.instance_root / "leaders" / role).resolve(strict=False)
        )
        leader_request = ParsedRequest(
            command=("controller", "leadership_bootstrap"),
            arguments={"role": role},
            input={},
            actor=ActorContext(kind="controller"),
            instance=request.instance,
            request_id=request_id,
            timeout=request.timeout,
        )
        operation, _ = ledger.create_operation(
            leader_request,
            owner="HUMAN" if role == "vizier" else _marshal_owner(ledger),
            planned={
                "role": role,
                "task_record_id": task_record_id,
                "creation_cwd": creation_cwd,
                "title": LEADERSHIP_TITLES[role],
            },
            next_action="Create or recover the exact standing native identity without starting a turn.",
        )
        planned = dict(operation.operation.get("planned") or {})
        attempts = int(operation.operation.get("attempts") or 0)
        due = _optional_string(planned.get("next_retry_at"))
        if due and datetime.now(timezone.utc) < _parse_time(due):
            actions.append(
                {
                    "role": role,
                    "operation_id": operation.id,
                    "created": False,
                    "retry_due_at": due,
                }
            )
            continue
        if operation.operation.get("state") in {"failed", "uncertain", "cancelled"}:
            actions.append(
                {
                    "role": role,
                    "operation_id": operation.id,
                    "created": False,
                    "state": operation.operation.get("state"),
                }
            )
            continue
        models = _mapping(config["models"])
        model_row = _mapping(models[role])
        project_root = Path(str(request.instance.brain_root)).resolve(strict=False)
        Path(creation_cwd).mkdir(parents=True, exist_ok=True)
        spec = TaskSpec(
            creation_cwd=creation_cwd,
            cwd=str(project_root),
            project_id=None,
            workspace_roots=(str(project_root),),
            title=LEADERSHIP_TITLES[role],
            model=str(model_row["model"]),
            effort=str(model_row["effort"]),
        )
        try:
            if connection_error is not None:
                raise connection_error
            await runtime.connect()
            native, created = await _create_or_recover_leader(
                runtime,
                spec,
                excluded_threads=(
                    retired_threads.union({recorded_thread})
                    if stale_task is not None and recorded_thread is not None
                    else retired_threads
                ),
            )
        except AppServerError as error:
            if error.category in {"unavailable", "transient"}:
                connection_error = error
            attempts += 1
            if error.uncertain:
                state = "uncertain"
                next_action = "Inspect the retained creation path before any replay."
            elif attempts >= 3:
                state = "failed"
                next_action = "Request an explicit leadership recovery decision."
            else:
                state = None
                delay = (2, 10)[attempts - 1]
                planned["next_retry_at"] = (
                    (datetime.now(timezone.utc) + timedelta(seconds=delay))
                    .isoformat()
                    .replace("+00:00", "Z")
                )
                next_action = f"Retry the same retained creation after {delay} seconds."
            operation = ledger.update_operation(
                operation,
                state=state,
                step=(
                    "leadership_creation_retry"
                    if state is None
                    else "leadership_creation_stopped"
                ),
                attempts=attempts,
                planned=planned,
                error={
                    "code": error.category,
                    "message": str(error),
                    "retryable": state is None,
                },
                next_action=next_action,
            )
            actions.append(
                {
                    "role": role,
                    "operation_id": operation.id,
                    "state": operation.operation.get("state"),
                    "retry_due_at": planned.get("next_retry_at"),
                }
            )
            continue
        task_fc = {
            "kind": "task",
            "owner": native.id,
            "thread_id": native.id,
            "role": role,
            "purpose": "leadership",
            "work_bead": None,
            "ownership_operation": None,
            "creation_operation": operation.id,
            "creation_cwd": creation_cwd,
            "model": spec.model,
            "effort": spec.effort,
            "model_origin": f"models.{role}",
            "associated_beads": [],
            "replaced_by": None,
            "deleted_at": None,
            "archive_state": "leadership",
            "last_observed": native.to_dict(),
            "last_turn": None,
            "last_transition": operation.id,
        }
        existing_task = ledger.show(task_record_id)
        if existing_task is None:
            managed_task = ledger.create_record(
                record_id=task_record_id,
                kind="task",
                title=f"Managed task: {LEADERSHIP_TITLES[role]}",
                description=f"Standing {role} native task {native.id}.",
                owner=native.id,
                fc=task_fc,
                external_ref=f"fulcrum:thread:{native.id}",
            )
            known_tasks[native.id] = managed_task
        elif existing_task.kind != "task" or (
            existing_task.fc and existing_task.fc.get("thread_id") != native.id
        ):
            operation = ledger.update_operation(
                operation,
                state="uncertain",
                step="leadership_task_record_conflict",
                error={
                    "code": "TASK_CONFLICT",
                    "message": f"retained task record {task_record_id} has different identity",
                    "retryable": False,
                },
                next_action="Inspect the conflicting retained task record.",
            )
            actions.append(
                {"role": role, "operation_id": operation.id, "state": "uncertain"}
            )
            continue
        else:
            managed_task = existing_task
        if stale_task is not None:
            stale_fc = dict(stale_task.fc or {})
            stale_fc["replaced_by"] = task_record_id
            stale_fc["deleted_at"] = utc_now()
            stale_fc["archive_state"] = "replaced"
            stale_fc["last_transition"] = operation.id
            ledger.update_fc(
                stale_task.id,
                stale_fc,
                assignee=stale_task.assignee,
                status=stale_task.status,
            )
        current_fc = dict(current.fc or {})
        current_fc["instance_root"] = expected_instance
        current_fc["brain_root"] = expected_brain
        current_fc[f"{role}_thread"] = native.id
        current_fc[f"{role}_creation_operation"] = operation.id
        current_fc["last_transition"] = operation.id
        if role == "marshal":
            current_fc["owner"] = native.id
        current_control = ledger.update_fc(
            current.id,
            current_fc,
            assignee=str(current_fc.get("owner") or "HUMAN"),
        )
        operation = ledger.update_operation(
            operation,
            state="completed",
            step="standing_identity_recorded",
            external={"thread_id": native.id},
            result={
                "role": role,
                "thread_id": native.id,
                "task_record_id": task_record_id,
                "turn_started": False,
            },
            next_action="Wait idle until an explicit leadership judgment is requested.",
        )
        action = {
            "role": role,
            "operation_id": operation.id,
            "thread_id": native.id,
            "created": created,
            "turn_started": False,
        }
        if send_initial_requests:
            action.update(
                await _send_initial_leadership_request(
                    request,
                    ledger,
                    runtime,
                    config,
                    role=role,
                    task=managed_task,
                    native=native,
                )
            )
        actions.append(action)
    return actions


async def _send_initial_leadership_request(
    request: ParsedRequest,
    ledger: Ledger,
    runtime: Any,
    config: Mapping[str, Any],
    *,
    role: str,
    task: LedgerRecord,
    native: TaskFacts,
) -> dict[str, Any]:
    if native.active_turn is not None:
        return {
            "turn_started": False,
            "initial_request": "already_active",
            "turn_id": native.active_turn,
        }
    if native.last_turn is not None:
        return {
            "turn_started": False,
            "initial_request": "already_sent",
            "turn_id": native.last_turn.get("id"),
        }
    assert request.request_id is not None
    from fulcrum.runtime_service import (
        _start_or_recover,
        routing_developer_instructions,
    )

    models = config["models"]
    selection = models[role]
    brain_root = str(request.instance.brain_root)
    task_fc = task.fc or {}
    spec = TaskSpec(
        creation_cwd=str(
            task_fc.get("creation_cwd")
            or request.instance.instance_root / "leaders" / role
        ),
        cwd=brain_root,
        project_id=None,
        workspace_roots=(brain_root,),
        title=LEADERSHIP_TITLES[role],
        model=str(selection["model"]),
        effort=str(selection["effort"]),
        developer_instructions=routing_developer_instructions(request),
    )
    initialization_operation = operation_id(request.request_id)
    prompt = (
        fallback_instructions(role)
        + "\n\nFulcrum setup has created this standing leadership task. Confirm that "
        + f"the {role.title()} role is ready to receive explicit requests, then wait. "
        + "Do not change policy, dispatch work, or begin project work from this setup request."
    )
    turn = await _start_or_recover(
        runtime,
        native.id,
        spec,
        initialization_operation,
        prompt,
    )
    current_task = ledger.show(task.id)
    if current_task is None or not current_task.fc:
        raise FulcrumError(
            "TASK_CORRUPT",
            f"standing {role} task disappeared during setup initialization",
            exit_code=4,
        )
    current_fc = dict(current_task.fc)
    current_fc["last_turn"] = turn.to_dict()
    current_fc["initialization_operation"] = initialization_operation
    current_fc["last_transition"] = initialization_operation
    ledger.update_fc(current_task.id, current_fc)
    return {
        "turn_started": True,
        "initial_request": "sent",
        "turn_id": turn.id,
        "initialization_operation": initialization_operation,
    }


async def _create_or_recover_leader(
    runtime: Any,
    spec: TaskSpec,
    *,
    excluded_threads: set[str],
) -> tuple[Any, bool]:
    ignored = set(excluded_threads)
    for attempt in range(3):
        matches = [
            task
            for task in await runtime.find_tasks(spec.creation_cwd)
            if task.id not in ignored
        ]
        if len(matches) == 1:
            return matches[0], False
        if len(matches) > 1:
            raise AppServerError(
                "multiple native tasks match the retained leadership creation path",
                category="uncertain",
                uncertain=True,
            )
        try:
            return await runtime.create_task(spec), True
        except AppServerError as error:
            absent = re.search(r"thread not found: ([0-9a-f-]+)", str(error), re.I)
            if absent is not None:
                ignored.add(absent.group(1))
                if attempt < 2:
                    continue
            if not error.uncertain:
                raise
            observed = [
                task
                for task in await runtime.find_tasks(spec.creation_cwd)
                if task.id not in ignored
            ]
            if len(observed) == 1:
                return observed[0], True
            raise AppServerError(
                "leadership creation response was lost and the retained path is inconclusive",
                category="uncertain",
                uncertain=True,
            ) from error
    raise AssertionError("bounded leadership creation loop did not return")


def normalize_native_intake(
    request: ParsedRequest, ledger: Ledger
) -> list[dict[str, Any]]:
    """Adopt resolvable raw issues; preserve conflicts as truthful intake facts."""

    results: list[dict[str, Any]] = []
    for record in ledger.list_records(limit=0):
        if record.kind is not None or record.status == "closed":
            continue
        role = (
            str(record.assignee)
            if record.assignee
            in {"weaver", "executor", "warden", "sage", "mason", "justiciar"}
            else "weaver"
        )
        adopt_request = ParsedRequest(
            command=("work", "adopt"),
            arguments={"id": record.id, "role": role},
            input={},
            actor=ActorContext(kind="controller"),
            instance=request.instance,
            request_id=str(
                uuid.uuid5(ADMISSION_NAMESPACE, f"native-intake:{record.id}")
            ),
            timeout=request.timeout,
        )
        try:
            result = WorkService().adopt(adopt_request)
        except FulcrumError as error:
            results.append(
                {
                    "bead_id": record.id,
                    "adopted": False,
                    "code": error.code,
                    "reason": error.message,
                }
            )
            continue
        results.append(
            {
                "bead_id": record.id,
                "adopted": True,
                "operation_id": result.operation_id,
            }
        )
    return results


def build_brief(
    ledger: Ledger,
    config: Mapping[str, Any],
    *,
    requested_kind: str,
    bead_id: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if requested_kind not in {"auto", *DECISION_KINDS}:
        raise FulcrumError.invalid(
            "INVALID_KIND", "kind must be auto, groom, dispatch, or recover"
        )
    candidates: dict[str, list[tuple[LedgerRecord, dict[str, Any], dict[str, Any]]]] = {
        kind: [] for kind in DECISION_KINDS
    }
    for record in _open_work(ledger):
        if bead_id is not None and record.id != bead_id:
            continue
        reasons = _waiting_reasons((record.fc or {}).get("waiting"))
        if (
            reasons
            and all(
                reason.get("kind")
                in {"defer", "recovery", "human", "future_activation"}
                for reason in reasons
            )
            and not _waiting_triggered(ledger, record, config, reasons)
        ):
            continue
        kind, row = _decision_row(ledger, record)
        if kind is None or row is None:
            continue
        if (
            requested_kind == "auto"
            and kind == "dispatch"
            and not row["decision_context"]["dependencies_ready"]
        ):
            continue
        candidates[kind].append((record, row, comparison_facts(ledger, record, config)))
    for values in candidates.values():
        values.sort(
            key=lambda item: (_priority(item[0]), _created_at(item[0]), item[0].id)
        )
    kind = (
        requested_kind
        if requested_kind != "auto"
        else next(
            (
                candidate
                for candidate in ("recover", "groom", "dispatch")
                if candidates[candidate]
            ),
            None,
        )
    )
    eligible = candidates[kind] if kind else []
    selected = eligible[:BRIEF_ROW_LIMIT]
    omitted_counts = {name: len(values) for name, values in candidates.items()}
    if kind:
        omitted_counts[kind] = max(0, len(eligible) - len(selected))
    why_now = _why_now(kind, selected)
    continuations = [
        ["fulcrum", "marshal", "brief", "--kind", name, "--json"]
        for name, count in omitted_counts.items()
        if count > 0 and name != kind
    ]
    if kind and len(eligible) > len(selected):
        continuations.append(
            ["fulcrum", "backlog", "list", "--include-deferred", "--json"]
        )
    brief: dict[str, Any] = {
        "kind": kind,
        "decision_required": bool(selected),
        "why_now": why_now,
        "policy_capacity": capacity_snapshot(ledger, config),
        "rows": [row for _, row, _ in selected],
        "omitted_counts": omitted_counts,
        "continuations": continuations,
    }
    projects = sorted(
        {
            str((record.fc or {}).get("project"))
            for record, _, _ in selected
            if (record.fc or {}).get("project")
        }
    )
    memory_gap: str | None = None
    try:
        memory = select_memory(
            ledger,
            project_ids=projects,
            role="marshal",
            max_chars=750,
        )
    except LedgerFailure as error:
        memory = {"items": []}
        memory_gap = str(error)
    if memory["items"]:
        brief["memory"] = memory
    if memory_gap:
        brief["memory_gap"] = memory_gap
    while brief["rows"] and len(_serialize(brief)) > BRIEF_CHARACTER_LIMIT:
        brief["rows"].pop()
        if kind:
            brief["omitted_counts"][kind] += 1
    brief["decision_required"] = bool(brief["rows"])
    if not brief["decision_required"] and eligible:
        brief["why_now"] = (
            "Eligible work exceeds the bounded brief; inspect it with the continuation command."
        )
        if ["fulcrum", "backlog", "list", "--include-deferred", "--json"] not in brief[
            "continuations"
        ]:
            brief["continuations"].append(
                ["fulcrum", "backlog", "list", "--include-deferred", "--json"]
            )
    comparisons = {
        record.id: comparison
        for record, _, comparison in selected[: len(brief["rows"])]
    }
    if len(_serialize(brief)) > BRIEF_CHARACTER_LIMIT:
        raise FulcrumError(
            "BRIEF_TOO_LARGE",
            "policy and continuation facts exceed the Marshal brief bound",
            exit_code=4,
        )
    return brief, comparisons


def marshal_context(request: ParsedRequest) -> CommandResult:
    ledger = _ledger(request)
    config = _config(request)
    brief, _ = build_brief(ledger, config, requested_kind="auto", bead_id=None)
    outstanding = _outstanding_decision(ledger)
    current_rows: list[dict[str, Any]] = []
    for record in _open_work(ledger):
        fc = record.fc or {}
        if fc.get("dispatch") is None and not _waiting_reasons(fc.get("waiting")):
            continue
        current_rows.append(
            {
                "bead_id": record.id,
                "title": record.title,
                "phase": fc.get("phase"),
                "owner": fc.get("owner"),
                "dispatch": fc.get("dispatch"),
                "waiting": fc.get("waiting"),
                "next_action": fc.get("next_action"),
            }
        )
    current_rows.sort(key=lambda row: str(row["bead_id"]))
    projection: dict[str, Any] = {
        "role": "marshal",
        "leader": LeadershipService().leader_show(request).result,
        "policy": config["policy"],
        "capacity": capacity_snapshot(ledger, config),
        "outstanding_decision": (
            operation_view(outstanding) if outstanding is not None else None
        ),
        "next_brief": brief,
        "current_rows": current_rows[:BRIEF_ROW_LIMIT],
        "omitted_current_rows": max(0, len(current_rows) - BRIEF_ROW_LIMIT),
        "continuations": [
            ["fulcrum", "backlog", "list", "--include-deferred", "--json"]
        ],
    }
    while (
        projection["current_rows"]
        and len(_serialize(projection)) > BRIEF_CHARACTER_LIMIT
    ):
        projection["current_rows"].pop()
        projection["omitted_current_rows"] += 1
    if len(_serialize(projection)) > BRIEF_CHARACTER_LIMIT:
        projection["next_brief"] = {
            "kind": brief["kind"],
            "decision_required": brief["decision_required"],
            "rows": [],
            "omitted_counts": brief["omitted_counts"],
            "continuations": brief["continuations"],
        }
    return CommandResult.query(projection)


def capacity_snapshot(ledger: Ledger, config: Mapping[str, Any]) -> dict[str, Any]:
    policy = _mapping(config["policy"])
    projects = _mapping(config.get("projects", {}))
    records = ledger.list_records(limit=0)
    task_records = [record for record in records if record.kind == "task"]
    work_records = [
        record
        for record in records
        if record.status != "closed" and record.kind in {None, "work"}
    ]
    active_ids: list[str] = []
    unknown_ids: list[str] = []
    human_bypasses: list[str] = []
    project_counts: dict[str, int] = {}
    counted_threads: set[str] = set()
    counted_work: set[str] = set()
    counted_creation_operations: set[str] = set()
    work_by_id = {record.id: record for record in work_records}
    for task in task_records:
        fc = task.fc or {}
        thread_id = _optional_string(fc.get("thread_id"))
        if (
            thread_id is None
            or thread_id in counted_threads
            or fc.get("deleted_at") is not None
        ):
            continue
        observed = fc.get("last_observed")
        last_turn = fc.get("last_turn")
        role = fc.get("role")
        active_turn = (
            observed.get("active_turn") if isinstance(observed, Mapping) else None
        )
        runtime_status = (
            observed.get("runtime_status") if isinstance(observed, Mapping) else None
        )
        observed_at = (
            str(observed.get("observed_at") or "")
            if isinstance(observed, Mapping)
            else ""
        )
        if (
            active_turn is None
            and isinstance(last_turn, Mapping)
            and not bool(last_turn.get("completed"))
            and str(last_turn.get("observed_at") or "") >= observed_at
        ):
            active_turn = last_turn.get("id") or "retained-active-turn"
        unknown = not isinstance(observed, Mapping) or runtime_status is None
        active = (
            active_turn is not None
            or unknown
            or runtime_status not in {"idle", "notLoaded"}
        )
        if role in LEADERSHIP_TITLES and not active:
            continue
        if not active:
            continue
        counted_threads.add(thread_id)
        creation_operation = _optional_string(fc.get("creation_operation"))
        if creation_operation is not None:
            counted_creation_operations.add(creation_operation)
        active_ids.append(thread_id)
        if unknown:
            unknown_ids.append(thread_id)
        work = work_by_id.get(str(fc.get("work_bead")))
        if work is not None:
            counted_work.add(work.id)
        project = str(
            (work.fc or {}).get("project")
            if work and work.fc
            else fc.get("project") or ""
        )
        project_counts[project] = project_counts.get(project, 0) + 1
        dispatch = (work.fc or {}).get("dispatch") if work and work.fc else None
        if isinstance(dispatch, Mapping) and dispatch.get("human_bypass"):
            human_bypasses.append(thread_id)
    reservations: list[str] = []
    for work in work_records:
        if work.id in counted_work:
            continue
        fc = work.fc or {}
        dispatch = fc.get("dispatch")
        reservation = (
            dispatch.get("reservation") if isinstance(dispatch, Mapping) else None
        )
        if not isinstance(reservation, Mapping) or reservation.get("state") not in {
            "in_flight",
            "unknown",
        }:
            continue
        operation_id = _optional_string(reservation.get("operation_id"))
        if operation_id is None:
            continue
        reservations.append(operation_id)
        project = str(fc.get("project") or "")
        project_counts[project] = project_counts.get(project, 0) + 1
        if reservation.get("human_bypass"):
            human_bypasses.append(operation_id)
    for operation_record in records:
        operation_fc = operation_record.fc or {}
        if (
            operation_record.kind != "operation"
            or operation_record.id in counted_creation_operations
            or operation_fc.get("command") != "plan.review.start"
            or operation_fc.get("state") not in {"accepted", "running"}
        ):
            continue
        planned = operation_fc.get("planned")
        review_reservation = (
            planned.get("reservation") if isinstance(planned, Mapping) else None
        )
        if not isinstance(review_reservation, Mapping) or review_reservation.get(
            "state"
        ) not in {"in_flight", "unknown"}:
            continue
        reservations.append(operation_record.id)
        project = str(planned.get("project") or "")
        project_counts[project] = project_counts.get(project, 0) + 1
    project_overrides = _mapping(policy.get("project_capacity", {}))
    default_project = int(policy["default_project_capacity"])
    project_rows = {
        str(project): {
            "limit": int(project_overrides.get(project, default_project)),
            "occupied": project_counts.get(str(project), 0),
        }
        for project in set(projects).union(project_counts)
    }
    global_limit = int(policy["automatic_capacity"])
    occupied = len(active_ids) + len(reservations)
    return {
        "global_limit": global_limit,
        "default_project_limit": default_project,
        "project_limits": {
            str(key): int(value) for key, value in project_overrides.items()
        },
        "projects": project_rows,
        "occupied": occupied,
        "available": max(0, global_limit - occupied),
        "active_managed_ids": sorted(active_ids),
        "pending_start_reservations": sorted(reservations),
        "unknown_managed_ids": sorted(unknown_ids),
        "human_bypasses": sorted(set(human_bypasses)),
        "paused_projects": list(policy.get("paused_projects", [])),
        "pressure": occupied / global_limit if global_limit else None,
    }


def comparison_facts(
    ledger: Ledger, record: LedgerRecord, config: Mapping[str, Any]
) -> dict[str, Any]:
    fc = record.fc or {}
    delivery = fc.get("delivery")
    policy = _mapping(config["policy"])
    project = str(fc.get("project") or "")
    return {
        "owner": fc.get("owner") if fc else _marshal_owner(ledger),
        "ownership_operation": fc.get("ownership_operation"),
        "phase": fc.get("phase", "intake"),
        "outcome": fc.get("outcome") or record.native.get("description"),
        "acceptance": fc.get("acceptance")
        or record.native.get("acceptance_criteria")
        or [],
        "dependencies": sorted(ledger.dependencies(record.id)),
        "priority": _priority(record),
        "waiting": fc.get("waiting"),
        "source": delivery.get("source_oid") if isinstance(delivery, Mapping) else None,
        "approved_source": (
            delivery.get("approved_source") if isinstance(delivery, Mapping) else None
        ),
        "intake": fc.get("intake"),
        "overlap_tags": fc.get("overlap_tags", []),
        "policy": {
            "automatic_capacity": policy["automatic_capacity"],
            "project_capacity": _mapping(policy.get("project_capacity", {})).get(
                project, policy["default_project_capacity"]
            ),
            "project_paused": project in policy.get("paused_projects", []),
            "suspended_rules": policy.get("suspended_rules", {}),
        },
    }


def dependency_readiness(
    ledger: Ledger, record: LedgerRecord
) -> tuple[bool, list[str]]:
    blockers: list[str] = []
    for dependency_id in ledger.dependencies(record.id):
        dependency = ledger.show(dependency_id)
        if dependency is None or dependency.status != "closed":
            blockers.append(dependency_id)
            continue
        disposition = (dependency.fc or {}).get("disposition")
        outcome = (
            disposition.get("outcome") if isinstance(disposition, Mapping) else None
        )
        if outcome not in SATISFYING_DEPENDENCY_OUTCOMES:
            blockers.append(dependency_id)
    return not blockers, blockers


def _decision_row(
    ledger: Ledger, record: LedgerRecord
) -> tuple[str | None, dict[str, Any] | None]:
    fc = record.fc or {}
    plan = fc.get("plan")
    if isinstance(plan, Mapping) and (
        plan.get("draft") is not None or plan.get("published_scope") is not None
    ):
        return None, None
    phase = str(fc.get("phase", "intake"))
    if phase in {"working", "handoff", "reviewing", "delivering", "done"}:
        return None, None
    waiting = _waiting_reasons(fc.get("waiting"))
    if phase in {"recovering", "human"} or any(
        reason.get("kind") in {"recovery", "external", "human"} for reason in waiting
    ):
        kind = "recover"
        needed = "Choose scoped recovery or the exact external action needed."
    else:
        intake = fc.get("intake")
        unknowns: list[Any] = []
        if not fc:
            unknowns.extend(
                ["project", "outcome", "acceptance", "benefit", "uncertainties"]
            )
        elif intake is None:
            unknowns.extend(["benefit", "uncertainties"])
        elif isinstance(intake, Mapping):
            if intake.get("benefit") is None:
                unknowns.append("benefit")
            if intake.get("uncertainties") is None:
                unknowns.append("uncertainties")
            elif isinstance(intake.get("uncertainties"), list):
                unknowns.extend(intake["uncertainties"])
        if fc.get("dispatch") is not None:
            return None, None
        if unknowns:
            kind = "groom"
            needed = "Resolve scope/readiness, request focused clarification, or dispose of the proposal."
        else:
            kind = "dispatch"
            needed = "Choose the authorized role and ordering under current capacity/overlap policy."
    ready, blockers = dependency_readiness(ledger, record)
    intake = fc.get("intake")
    unknowns = []
    if not fc:
        unknowns = ["project", "outcome", "acceptance", "benefit", "uncertainties"]
    elif intake is None:
        unknowns = ["benefit", "uncertainties"]
    elif isinstance(intake, Mapping):
        if intake.get("benefit") is None:
            unknowns.append("benefit")
        if intake.get("uncertainties") is None:
            unknowns.append("uncertainties")
        elif isinstance(intake.get("uncertainties"), list):
            unknowns.extend(intake["uncertainties"])
    evidence_refs = list(fc.get("context", []))
    if fc.get("last_progress"):
        evidence_refs.append(f"work:{record.id}:last_progress")
    return kind, {
        "bead_id": record.id,
        "title": record.title,
        "outcome": fc.get("outcome") or record.native.get("description"),
        "owner": fc.get("owner") or _marshal_owner(ledger),
        "phase": phase,
        "expected_ownership_operation": fc.get("ownership_operation"),
        "expected_phase": phase,
        "decision_needed": needed,
        "decision_context": {
            "dependencies_ready": ready,
            "dependency_blockers": blockers,
            "waiting": waiting,
            "size": fc.get("size", "unknown"),
            "overlap_tags": fc.get("overlap_tags", []),
            "priority": _priority(record),
            "last_progress": fc.get("last_progress"),
        },
        "unknowns": unknowns,
        "evidence_refs": evidence_refs,
        "proposed_action": (
            {
                "action": "dispatch",
                "role": fc.get("requested_role"),
                "source": "author proposal",
            }
            if fc.get("requested_role") and kind in {"groom", "dispatch"}
            else None
        ),
    }


def _apply_decision(
    ledger: Ledger,
    record: LedgerRecord,
    supplied: Mapping[str, Any],
    decision_operation: str,
) -> LedgerRecord:
    action = supplied.get("action")
    reason = supplied.get("reason")
    if action not in DECISION_ACTIONS:
        raise FulcrumError.invalid("INVALID_ACTION", f"unknown Marshal action {action}")
    if not isinstance(reason, str) or not reason.strip():
        raise FulcrumError.invalid("INVALID_DECISION", "decision reason is required")
    fc = dict(record.fc or {})
    if not fc:
        raise FulcrumError.invalid(
            "INTAKE_NOT_ADOPTED", "raw native intake must be normalized before decision"
        )
    now = utc_now()
    decision_record = ledger.show(decision_operation)
    decision_planned = (
        decision_record.fc.get("planned")
        if decision_record and decision_record.fc
        else None
    )
    comparison_rows = (
        decision_planned.get("comparison_facts")
        if isinstance(decision_planned, Mapping)
        else None
    )
    baseline = (
        comparison_rows.get(record.id) if isinstance(comparison_rows, Mapping) else None
    )
    status = record.status
    assignee = str(fc.get("owner") or _marshal_owner(ledger))
    if action == "dispatch":
        role = supplied.get("role")
        if not isinstance(role, str) or role not in {
            "weaver",
            "executor",
            "warden",
            "sage",
            "mason",
            "justiciar",
        }:
            raise FulcrumError.invalid(
                "INVALID_ROLE", "dispatch requires an executable role"
            )
        fc["dispatch"] = {
            "role": role,
            "reason": reason,
            "decision_operation": decision_operation,
            "authorized_at": now,
            "human_bypass": False,
            "reservation": None,
        }
        fc["waiting"] = _remove_waiting_events(
            fc.get("waiting"), {"scope_updated", "priority_changed", "policy_changed"}
        )
        fc["next_action"] = (
            "Start the authorized role when dependencies and capacity allow."
        )
    elif action == "defer":
        triggers = _validate_triggers(supplied.get("reconsider_when"))
        _set_waiting(
            fc,
            reason_id=f"decision:{decision_operation}",
            reason=reason,
            triggers=triggers,
            decision_operation=decision_operation,
            kind="defer",
            baseline=baseline if isinstance(baseline, Mapping) else None,
        )
        fc["dispatch"] = None
        fc["next_action"] = "Reconsider only when a recorded relevant trigger changes."
    elif action == "clarify":
        question = supplied.get("question")
        if not isinstance(question, str) or not question.strip():
            raise FulcrumError.invalid("INVALID_DECISION", "clarify requires question")
        fc["clarification"] = {
            "question": question,
            "expected_result": supplied.get("expected_result")
            or "Return evidence or refined scope on this same bead.",
            "decision_operation": decision_operation,
        }
        fc["dispatch"] = {
            "role": "weaver",
            "reason": reason,
            "decision_operation": decision_operation,
            "authorized_at": now,
            "human_bypass": False,
            "reservation": None,
        }
        fc["next_action"] = f"Weaver must answer: {question}"
    elif action in {"duplicate", "reject"}:
        canonical = supplied.get("canonical_bead") if action == "duplicate" else None
        if action == "duplicate":
            if not isinstance(canonical, str) or canonical == record.id:
                raise FulcrumError.invalid(
                    "INVALID_DECISION", "duplicate requires a different canonical_bead"
                )
            target = ledger.show(canonical)
            if target is None or target.kind != "work":
                raise FulcrumError.invalid(
                    "INVALID_DECISION", "canonical_bead must name existing work"
                )
        fc["phase"] = "done"
        fc["disposition"] = {
            "outcome": "rejected" if action == "reject" else "duplicate",
            "summary": reason,
            "waived_requirements": [],
            "known_defects": [],
            "canonical_bead": canonical,
            "completed_at": now,
            "decision_operation": decision_operation,
        }
        fc["next_action"] = "No further action is required unless explicitly reopened."
        status = "closed"
    elif action == "recover":
        scope = supplied.get("scope")
        diagnosis = supplied.get("diagnosis")
        if (
            not isinstance(scope, str)
            or not scope
            or not isinstance(diagnosis, str)
            or not diagnosis
        ):
            raise FulcrumError.invalid(
                "INVALID_DECISION", "recover requires scope and diagnosis"
            )
        fc["phase"] = "recovering"
        fc["recovery"] = {
            "scope": scope,
            "diagnosis": diagnosis,
            "reason": reason,
            "decision_operation": decision_operation,
        }
        _set_waiting(
            fc,
            reason_id=f"recovery:{decision_operation}",
            reason=reason,
            triggers=[{"event": "scope_updated", "subject": record.id}],
            decision_operation=decision_operation,
            kind="recovery",
            baseline=baseline if isinstance(baseline, Mapping) else None,
        )
        fc["next_action"] = "Perform the recorded scoped recovery decision."
        status = "blocked"
    elif action == "human":
        question = supplied.get("question")
        required = supplied.get("required_action")
        if (
            not isinstance(question, str)
            or not question
            or not isinstance(required, str)
            or not required
        ):
            raise FulcrumError.invalid(
                "INVALID_DECISION", "human requires question and required_action"
            )
        fc["owner"] = "HUMAN"
        fc["role"] = None
        fc["ownership_operation"] = decision_operation
        fc["phase"] = "human"
        _set_waiting(
            fc,
            reason_id=f"human:{decision_operation}",
            reason=question,
            triggers=[{"event": "human_resolved", "subject": record.id}],
            decision_operation=decision_operation,
            kind="human",
            required_action=required,
            baseline=baseline if isinstance(baseline, Mapping) else None,
        )
        fc["next_action"] = required
        status = "blocked"
        assignee = "HUMAN"
    fc["last_transition"] = decision_operation
    return ledger.update_fc(record.id, fc, assignee=assignee, status=status)


def _set_waiting(
    fc: dict[str, Any],
    *,
    reason_id: str,
    reason: str,
    triggers: Sequence[Mapping[str, Any]],
    decision_operation: str,
    kind: str = "wait",
    required_action: str | None = None,
    baseline: Mapping[str, Any] | None = None,
) -> None:
    reasons = [
        item
        for item in _waiting_reasons(fc.get("waiting"))
        if item.get("id") != reason_id
    ]
    row: dict[str, Any] = {
        "id": reason_id,
        "kind": kind,
        "reason": reason,
        "reconsider_when": [dict(item) for item in triggers],
        "decision_operation": decision_operation,
        "recorded_at": utc_now(),
    }
    if required_action is not None:
        row["required_action"] = required_action
    if baseline is not None:
        row["baseline"] = dict(baseline)
    reasons.append(row)
    fc["waiting"] = {"reasons": reasons}


def _waiting_reasons(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Mapping):
        return []
    reasons = value.get("reasons")
    if isinstance(reasons, list):
        return [dict(item) for item in reasons if isinstance(item, Mapping)]
    if value.get("reason"):
        return [dict(value)]
    return []


def _waiting_triggered(
    ledger: Ledger,
    record: LedgerRecord,
    config: Mapping[str, Any],
    reasons: Sequence[Mapping[str, Any]],
) -> bool:
    current = comparison_facts(ledger, record, config)
    capacity: Mapping[str, Any] | None = None
    for reason in reasons:
        baseline = reason.get("baseline")
        triggers = reason.get("reconsider_when")
        if not isinstance(triggers, list):
            continue
        for trigger in triggers:
            if not isinstance(trigger, Mapping):
                continue
            event = trigger.get("event")
            subject = _optional_string(trigger.get("subject"))
            if event == "dependency_closed" and subject:
                dependency = ledger.show(subject)
                disposition = (
                    (dependency.fc or {}).get("disposition") if dependency else None
                )
                outcome = (
                    disposition.get("outcome")
                    if isinstance(disposition, Mapping)
                    else None
                )
                if (
                    dependency
                    and dependency.status == "closed"
                    and outcome in SATISFYING_DEPENDENCY_OUTCOMES
                ):
                    return True
            elif event == "capacity_available":
                if capacity is None:
                    capacity = capacity_snapshot(ledger, config)
                project = str((record.fc or {}).get("project") or "")
                project_row = capacity["projects"].get(project, {})
                if capacity["available"] > 0 and int(
                    project_row.get("occupied", 0)
                ) < int(project_row.get("limit", capacity["default_project_limit"])):
                    return True
            elif isinstance(baseline, Mapping):
                if event == "priority_changed" and current.get(
                    "priority"
                ) != baseline.get("priority"):
                    return True
                if event == "policy_changed" and current.get("policy") != baseline.get(
                    "policy"
                ):
                    return True
                if event == "scope_updated" and any(
                    current.get(key) != baseline.get(key)
                    for key in ("outcome", "acceptance", "intake", "overlap_tags")
                ):
                    return True
                if event == "publication_settled" and current.get(
                    "source"
                ) != baseline.get("source"):
                    return True
    return False


def _remove_waiting_events(value: Any, events: set[str]) -> dict[str, Any] | None:
    retained: list[dict[str, Any]] = []
    for reason in _waiting_reasons(value):
        triggers = reason.get("reconsider_when")
        trigger_rows = (
            [item for item in triggers if isinstance(item, Mapping)]
            if isinstance(triggers, list)
            else []
        )
        if trigger_rows and all(
            str(item.get("event")) in events for item in trigger_rows
        ):
            continue
        retained.append(reason)
    return {"reasons": retained} if retained else None


def _validate_triggers(value: Any) -> list[dict[str, Any]]:
    supported = {
        "dependency_closed",
        "capacity_available",
        "priority_changed",
        "policy_changed",
        "publication_settled",
        "scope_updated",
        "human_resolved",
    }
    if not isinstance(value, list) or not value:
        raise FulcrumError.invalid(
            "INVALID_DECISION", "defer requires reconsider_when triggers"
        )
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping) or item.get("event") not in supported:
            raise FulcrumError.invalid(
                "INVALID_DECISION", "unsupported reconsideration trigger"
            )
        if any(key not in {"event", "subject"} for key in item):
            raise FulcrumError.invalid(
                "INVALID_DECISION", "unknown reconsideration trigger field"
            )
        result.append({"event": item["event"], "subject": item.get("subject")})
    return result


def _require_authorizer(
    request: ParsedRequest, ledger: Ledger, *, human_bypass: bool
) -> None:
    if request.actor.kind == "human":
        return
    if human_bypass:
        raise FulcrumError(
            "DISPATCH_AUTHORITY_DENIED",
            "only a human may bypass capacity policy",
            exit_code=5,
        )
    _require_marshal_or_human(request, ledger)


def _require_marshal_or_human(request: ParsedRequest, ledger: Ledger) -> None:
    if request.actor.kind == "human":
        return
    if request.actor.kind == "task" and request.actor.task_id == _marshal_owner(ledger):
        return
    raise FulcrumError(
        "MARSHAL_AUTHORITY_DENIED",
        "only the current Marshal or human may make ordinary admission decisions",
        exit_code=5,
    )


def _outstanding_decision(ledger: Ledger) -> OperationRecord | None:
    candidates: list[OperationRecord] = []
    for record in ledger.list_records(kind="operation", limit=0):
        operation = OperationRecord.from_record(record)
        if operation.operation.get("command") != "marshal.request":
            continue
        if operation.operation.get("state") in {"accepted", "running"}:
            candidates.append(operation)
    candidates.sort(key=lambda item: str(item.operation.get("created_at") or ""))
    return candidates[0] if candidates else None


def _marshal_task(ledger: Ledger) -> LedgerRecord | None:
    thread = _marshal_owner(ledger)
    return _task_for_thread(ledger, thread) if thread != "HUMAN" else None


def _task_for_thread(ledger: Ledger, thread_id: str) -> LedgerRecord | None:
    matches = [
        record
        for record in ledger.list_records(kind="task", limit=0)
        if record.fc
        and record.fc.get("thread_id") == thread_id
        and record.fc.get("deleted_at") is None
    ]
    return matches[0] if len(matches) == 1 else None


def _open_work(ledger: Ledger) -> list[LedgerRecord]:
    return [
        record
        for record in ledger.list_records(limit=0)
        if record.status != "closed" and (record.kind in {None, "work"})
    ]


def _work(ledger: Ledger, bead_id: str) -> LedgerRecord:
    record = ledger.show(bead_id)
    if record is None or record.kind != "work" or not record.fc:
        raise FulcrumError.invalid("NOT_FOUND", f"unknown admitted work {bead_id}")
    return record


def _marshal_owner(ledger: Ledger) -> str:
    control = ledger.show("fc-system")
    value = control.fc.get("marshal_thread") if control and control.fc else None
    return str(value) if isinstance(value, str) and value else "HUMAN"


def _config(request: ParsedRequest) -> dict[str, Any]:
    manager = ConfigurationManager(request.instance.config_path)
    document, _ = manager.load()
    return manager.effective(document)


def _ledger(request: ParsedRequest) -> Ledger:
    if request.instance.brain_root is None:
        raise FulcrumError(
            "LEDGER_UNAVAILABLE", "brain root is unavailable", exit_code=4
        )
    config = _config(request)
    beads = _mapping(config["beads"])
    executable = beads.get("executable")
    return Ledger(
        request.instance.brain_root,
        executable=str(executable) if executable else None,
        timeout=request.timeout,
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FulcrumError(
            "CONFIG_INVALID", "effective configuration is invalid", exit_code=4
        )
    return value


def _priority(record: LedgerRecord) -> int:
    value = record.native.get("priority", 2)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 2


def _created_at(record: LedgerRecord) -> str:
    return str(record.native.get("created_at") or "")


def _why_now(
    kind: str | None,
    selected: Sequence[tuple[LedgerRecord, Mapping[str, Any], Mapping[str, Any]]],
) -> str:
    if not selected:
        return "No current work requires new Marshal judgment."
    return {
        "recover": "Current work requires a scoped recovery or external intervention choice.",
        "groom": "Current proposals require scope, readiness, duplicate, or clarification judgment.",
        "dispatch": "Actionable work requires role or ordering authorization under current policy.",
    }[str(kind)]


def _serialize(value: Mapping[str, Any]) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, sort_keys=True)


def _marshal_decision_prompt(operation_id: str, serialized_brief: str) -> str:
    return (
        "You are the standing Fulcrum Marshal for one retained decision batch. "
        "Evaluate every selected row from evidence, keep material unknowns explicit, "
        "and keep independent actionable work moving. Use the selected Fulcrum "
        "instance CLI to call `marshal decide --input - --json` exactly once with "
        f"`decision_operation` set to `{operation_id}` and one decision per row. "
        "Each decision must repeat `bead_id`, `expected_ownership_operation`, and "
        "`expected_phase` from the row, then choose an allowed action and give an "
        "evidence-based reason plus its action-specific fields. Do not edit source "
        "or fulcrum.yaml. End the turn after the command result is observed.\n\n"
        "## Retained decision brief\n"
        f"{serialized_brief}"
    )


def _optional_string(value: Any) -> str | None:
    return str(value) if isinstance(value, str) and value else None


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _limit(value: Any) -> int:
    result = int(value) if value is not None else 20
    if result < 0:
        raise FulcrumError.invalid("INVALID_LIMIT", "limit cannot be negative")
    return result


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
