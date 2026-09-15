"""Role finish operations and the Executor-to-Warden handoff."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from fulcrum.analytics import AnalyticsService
from fulcrum.configuration import ConfigurationManager
from fulcrum.contracts import CommandResult, CommandState, FulcrumError, ParsedRequest
from fulcrum.delivery_service import DeliveryService, _context
from fulcrum.ledger import (
    Ledger,
    LedgerRecord,
    OperationRecord,
    accepted_input,
    operation_id,
    operation_view,
    utc_now,
)
from fulcrum.work import _marshal_or_human

HANDOFF_NAMESPACE = uuid.UUID("5f41c1ab-35bf-4df6-a85b-a75cb82ab40e")
FINISH_CHILD_NAMESPACE = uuid.UUID("c5846683-3cc4-40a8-92af-cb14a44ca88b")
TERMINAL_STATES = {"completed", "failed", "uncertain", "cancelled"}
CHECK_STATES = {"passed", "failed", "not_run"}


class CompletionService:
    def finish(self, request: ParsedRequest) -> CommandResult:
        bead_id = request.input.get("bead") or request.arguments.get("bead")
        if not isinstance(bead_id, str) or not bead_id:
            raise FulcrumError.invalid("INVALID_INPUT", "finish requires a bead")
        request = replace(
            request, arguments={**dict(request.arguments), "bead": bead_id}
        )
        replayed = _finish_replay(request)
        if replayed is not None:
            return replayed
        ledger, work = _owned_work(request)
        outcome = request.input.get("outcome") or request.arguments.get("outcome")
        role = str((work.fc or {}).get("role") or "")
        if role == "weaver" and outcome == "answered":
            return self._weaver_answered(request, ledger, work)
        if role == "weaver" and outcome == "ready":
            return self._weaver_ready(request, ledger, work)
        if role == "weaver" and outcome == "planned":
            return self._weaver_planned(request, ledger, work)
        if role == "executor" and outcome == "ready_for_review":
            return self._executor_finish(request, ledger, work)
        if role == "warden" and outcome == "approved":
            return self._warden_finish(request, ledger, work)
        raise FulcrumError(
            "OUTCOME_NOT_IMPLEMENTED",
            f"{role or 'unassigned'} cannot finish with outcome {outcome!r} yet",
            exit_code=4,
            details={"role": role, "outcome": outcome},
        )

    def _weaver_answered(
        self, request: ParsedRequest, ledger: Ledger, work: LedgerRecord
    ) -> CommandResult:
        summary, evidence = _weaver_payload(request, evidence_required=True)
        operation, reused = ledger.create_operation(
            request,
            bead_id=work.id,
            planned={"summary": summary, "evidence": evidence},
            next_action="Retain the answer evidence and close only this question root.",
        )
        if reused and operation.operation.get("state") in TERMINAL_STATES:
            return _operation_result(operation)
        _record_task_finish(ledger, work, operation.id)
        fc = dict(work.fc or {})
        fc["phase"] = "done"
        fc["disposition"] = {
            "outcome": "answered",
            "summary": summary,
            "evidence": evidence,
            "completed_at": utc_now(),
            "ownership_operation": request.ownership_operation,
        }
        fc["last_transition"] = operation.id
        fc["next_action"] = (
            "No question work remains unless the root is explicitly reopened."
        )
        closed = ledger.update_fc(work.id, fc, status="closed")
        completion_cost = AnalyticsService().finalize_root(ledger, closed, operation.id)
        operation = ledger.update_operation(
            operation.id,
            state="completed",
            step="weaver_answer_retained",
            result={
                "bead_id": work.id,
                "accepted": True,
                "disposition": fc["disposition"],
                "report_reminder": _report_reminder(work.id),
                "completion_cost": completion_cost,
            },
            next_action=fc["next_action"],
        )
        return _operation_result(operation)

    def _weaver_ready(
        self, request: ParsedRequest, ledger: Ledger, work: LedgerRecord
    ) -> CommandResult:
        summary, _ = _weaver_payload(request, evidence_required=False)
        operation, reused = ledger.create_operation(
            request,
            bead_id=work.id,
            planned={"summary": summary},
            next_action="Return the completed scope to Marshal backlog without dispatching it.",
        )
        if reused and operation.operation.get("state") in TERMINAL_STATES:
            return _operation_result(operation)
        _record_task_finish(ledger, work, operation.id)
        owner = _marshal_or_human(ledger)
        fc = dict(work.fc or {})
        fc["summary"] = summary
        fc["owner"] = owner
        fc["role"] = "marshal" if owner != "HUMAN" else None
        fc["phase"] = "backlog"
        fc["dispatch"] = None
        fc["waiting"] = _without_waiting_kind(fc.get("waiting"), "authoring")
        fc["last_transition"] = operation.id
        fc["next_action"] = "Marshal must review and authorize the completed scope."
        ledger.update_fc(work.id, fc, assignee=owner, status="open")
        operation = ledger.update_operation(
            operation.id,
            state="completed",
            step="weaver_scope_returned",
            result={
                "bead_id": work.id,
                "accepted": True,
                "owner": owner,
                "phase": "backlog",
                "report_reminder": _report_reminder(work.id),
            },
            next_action=fc["next_action"],
        )
        return _operation_result(operation)

    def _weaver_planned(
        self, request: ParsedRequest, ledger: Ledger, work: LedgerRecord
    ) -> CommandResult:
        summary, _ = _weaver_payload(request, evidence_required=False)
        plan_id = request.input.get("plan_id")
        if plan_id != work.id:
            raise FulcrumError.invalid(
                "INVALID_PLAN",
                "finish planned must reference the Weaver-owned plan root",
            )
        plan = (work.fc or {}).get("plan")
        if (
            not isinstance(plan, Mapping)
            or plan.get("published_scope") is None
            or plan.get("activation") != "future"
        ):
            raise FulcrumError(
                "APPROVAL_CONFLICT",
                "finish planned requires a published future plan",
                exit_code=5,
            )
        operation, reused = ledger.create_operation(
            request,
            bead_id=work.id,
            planned={
                "summary": summary,
                "plan_id": plan_id,
                "published_approval_operation": plan.get(
                    "published_approval_operation"
                ),
            },
            next_action="End Weaver authoring while retaining the future root and deferral.",
        )
        if reused and operation.operation.get("state") in TERMINAL_STATES:
            return _operation_result(operation)
        _record_task_finish(ledger, work, operation.id)
        owner = _marshal_or_human(ledger)
        current = _reload_work(ledger, work.id)
        fc = dict(current.fc or {})
        current_plan = dict(fc.get("plan") or {})
        current_plan["authoring"] = {
            "state": "completed",
            "finish_operation": operation.id,
            "summary": summary,
            "completed_at": utc_now(),
        }
        current_plan["authoring_ready"] = True
        fc["plan"] = current_plan
        fc["summary"] = summary
        fc["owner"] = owner
        fc["role"] = "marshal" if owner != "HUMAN" else None
        fc["phase"] = "backlog"
        fc["dispatch"] = None
        fc["waiting"] = _without_waiting_kind(fc.get("waiting"), "authoring")
        fc["last_transition"] = operation.id
        fc["next_action"] = (
            "Future plan remains deferred until human/Vizier authorization and Marshal execution."
        )
        ledger.update_fc(work.id, fc, assignee=owner, status="open")
        _settle_plan_authoring_children(ledger, current_plan, operation.id)
        operation = ledger.update_operation(
            operation.id,
            state="completed",
            step="future_plan_authoring_completed",
            result={
                "bead_id": work.id,
                "plan_id": work.id,
                "accepted": True,
                "root_closed": False,
                "activation": "future",
                "owner": owner,
                "report_reminder": _report_reminder(work.id),
            },
            next_action=fc["next_action"],
        )
        return _operation_result(operation)

    def _executor_finish(
        self, request: ParsedRequest, ledger: Ledger, work: LedgerRecord
    ) -> CommandResult:
        payload = _finish_payload(request)
        sealed = (work.fc or {}).get("finish")
        if isinstance(sealed, Mapping):
            if sealed.get("request_id") == request.request_id:
                existing = ledger.show(str(sealed.get("operation_id")))
                if existing is not None and existing.kind == "operation":
                    return _operation_result(OperationRecord.from_record(existing))
            raise FulcrumError(
                "FINISH_SEALED",
                "Executor finish input is already sealed for this acquisition",
                exit_code=5,
                details={
                    "operation_id": sealed.get("operation_id"),
                    "source_oid": sealed.get("source_oid"),
                },
            )
        assert request.request_id is not None
        start_request_id = str(
            uuid.uuid5(HANDOFF_NAMESPACE, f"warden:{work.id}:{request.request_id}")
        )
        operation, reused = ledger.create_operation(
            request,
            bead_id=work.id,
            planned={
                "finish": payload,
                "from_thread": request.thread_id,
                "from_ownership_operation": request.ownership_operation,
                "warden_start_request_id": start_request_id,
            },
            next_action="Seal implementation evidence and wait for the Executor turn and tools to stop.",
        )
        if reused and operation.operation.get("state") in TERMINAL_STATES:
            return _operation_result(operation)
        workspace = _inspect_clean_source(request, payload["source_oid"])
        fc = dict(work.fc or {})
        fc["finish"] = {
            **payload,
            "request_id": request.request_id,
            "operation_id": operation.id,
            "ownership_operation": request.ownership_operation,
            "sealed_at": utc_now(),
        }
        fc["phase"] = "handoff"
        fc["handoff"] = {
            "from_thread": request.thread_id,
            "from_ownership_operation": request.ownership_operation,
            "to_thread": None,
            "to_role": "warden",
            "source_oid": payload["source_oid"],
            "evidence": {
                "checks": payload["checks"],
                "references": payload["evidence"],
                "workspace": workspace,
            },
            "receipt": operation.id,
            "start_request_id": start_request_id,
            "state": "waiting_for_executor_terminal",
        }
        fc["next_action"] = (
            "End the Executor turn; the controller will start Warden only after the "
            "turn, subscription, and owned terminals are observed stopped."
        )
        fc["last_transition"] = operation.id
        updated = ledger.update_fc(work.id, fc, status="in_progress")
        _record_task_finish(ledger, work, operation.id)
        operation = ledger.update_operation(
            operation.id,
            state="completed",
            step="executor_finish_sealed",
            result={
                "bead_id": work.id,
                "accepted": True,
                "handoff": fc["handoff"],
                "work": _work_summary(updated),
                "report_reminder": _report_reminder(work.id),
            },
            next_action=fc["next_action"],
        )
        return _operation_result(operation)

    def _warden_finish(
        self, request: ParsedRequest, ledger: Ledger, work: LedgerRecord
    ) -> CommandResult:
        payload = _finish_payload(request)
        operation, reused = ledger.create_operation(
            request,
            bead_id=work.id,
            planned={"finish": payload, "children": {}},
            next_action="Validate, approve, and request promotion of the exact Warden source.",
        )
        if reused and operation.operation.get("state") in TERMINAL_STATES:
            return _operation_result(operation)
        workspace = _inspect_clean_source(request, payload["source_oid"])
        children: dict[str, Any] = {}
        delivery = (work.fc or {}).get("delivery")
        validation = (
            delivery.get("validation") if isinstance(delivery, Mapping) else None
        )
        validated_source = (
            delivery.get("source_oid") if isinstance(delivery, Mapping) else None
        )
        service = DeliveryService()
        if validated_source != payload["source_oid"] or not isinstance(
            validation, Mapping
        ):
            validation_result = service.validation_start(
                _child_request(
                    request, operation.id, "validation", ("validation", "start")
                )
            )
            children["validation"] = _child_result(validation_result)
            work = _reload_work(ledger, work.id)
            delivery = (work.fc or {}).get("delivery")
            validation = (
                delivery.get("validation") if isinstance(delivery, Mapping) else None
            )
        if not isinstance(validation, Mapping) or validation.get("state") != "passed":
            fc = dict(work.fc or {})
            fc["phase"] = "reviewing"
            fc["next_action"] = (
                "Inspect failed validation, fix the same workspace, and submit a new source."
                if isinstance(validation, Mapping)
                and validation.get("state") == "failed"
                else "Wait for exact-source validation, then inspect and finish approval again."
            )
            fc["last_transition"] = operation.id
            ledger.update_fc(work.id, fc)
            _record_task_finish(ledger, work, operation.id)
            operation = ledger.update_operation(
                operation.id,
                state="completed",
                step="warden_validation_pending",
                result={
                    "bead_id": work.id,
                    "accepted": True,
                    "source_oid": payload["source_oid"],
                    "validation": validation,
                    "children": children,
                    "workspace": workspace,
                    "report_reminder": _report_reminder(work.id),
                },
                next_action=fc["next_action"],
            )
            return _operation_result(operation)
        approved = (
            delivery.get("approved_source") if isinstance(delivery, Mapping) else None
        )
        if (
            not isinstance(approved, Mapping)
            or approved.get("oid") != payload["source_oid"]
        ):
            approval_result = service.review_approve(
                _child_request(request, operation.id, "approval", ("review", "approve"))
            )
            children["approval"] = _child_result(approval_result)
            if approval_result.state != CommandState.COMPLETED:
                return _finish_child_unresolved(
                    ledger,
                    work,
                    operation,
                    payload,
                    children,
                    workspace,
                    "approval",
                    approval_result,
                )
        promotion_result = service.promotion_start(
            _child_request(request, operation.id, "promotion", ("promotion", "start"))
        )
        children["promotion"] = _child_result(promotion_result)
        if promotion_result.state == CommandState.FAILED:
            return _finish_child_unresolved(
                ledger,
                work,
                operation,
                payload,
                children,
                workspace,
                "promotion",
                promotion_result,
            )
        work = _reload_work(ledger, work.id)
        fc = dict(work.fc or {})
        fc["phase"] = "delivering"
        fc["delivery_finish"] = {
            "operation_id": operation.id,
            "source_oid": payload["source_oid"],
            "summary": payload["summary"],
            "state": "waiting_for_warden_terminal",
            "requested_at": utc_now(),
        }
        fc["next_action"] = (
            "End the Warden turn; the controller will observe promotion, source "
            "synchronization, cleanup, and closure independently."
        )
        fc["last_transition"] = operation.id
        ledger.update_fc(work.id, fc)
        _record_task_finish(ledger, work, operation.id)
        operation = ledger.update_operation(
            operation.id,
            state="completed",
            step="warden_delivery_started",
            planned={"finish": payload, "children": children},
            result={
                "bead_id": work.id,
                "accepted": True,
                "source_oid": payload["source_oid"],
                "children": children,
                "workspace": workspace,
                "report_reminder": _report_reminder(work.id),
            },
            next_action=fc["next_action"],
        )
        return _operation_result(operation)


def _finish_child_unresolved(
    ledger: Ledger,
    work: LedgerRecord,
    operation: OperationRecord,
    payload: Mapping[str, Any],
    children: Mapping[str, Any],
    workspace: Mapping[str, Any],
    step: str,
    child: CommandResult,
) -> CommandResult:
    work = _reload_work(ledger, work.id)
    fc = dict(work.fc or {})
    fc["phase"] = "reviewing"
    fc["next_action"] = (
        f"Warden must inspect the failed {step} receipt and keep the current source "
        "recoverable before another delivery attempt."
    )
    fc["last_transition"] = operation.id
    ledger.update_fc(work.id, fc)
    _record_task_finish(ledger, work, operation.id)
    operation = ledger.update_operation(
        operation.id,
        state="completed",
        step=f"warden_{step}_unresolved",
        result={
            "bead_id": work.id,
            "accepted": True,
            "source_oid": payload["source_oid"],
            "children": dict(children),
            "workspace": dict(workspace),
            "unresolved_operation": child.operation_id,
            "report_reminder": _report_reminder(work.id),
        },
        next_action=fc["next_action"],
    )
    return _operation_result(operation)


def _owned_work(request: ParsedRequest) -> tuple[Ledger, LedgerRecord]:
    ledger = _ledger(request)
    work = ledger.show(str(request.arguments["bead"]))
    if work is None or work.kind != "work" or not work.fc:
        raise FulcrumError.invalid(
            "NOT_FOUND", f"unknown work {request.arguments['bead']}"
        )
    if request.actor.kind != "human" and (
        request.thread_id != (work.fc or {}).get("owner")
        or request.ownership_operation != (work.fc or {}).get("ownership_operation")
    ):
        raise FulcrumError(
            "OWNERSHIP_CONFLICT",
            "finish requires the current task and ownership operation",
            exit_code=5,
            details={
                "current_owner": (work.fc or {}).get("owner"),
                "current_ownership_operation": (work.fc or {}).get(
                    "ownership_operation"
                ),
            },
        )
    return ledger, work


def _ledger(request: ParsedRequest) -> Ledger:
    if request.instance.brain_root is None:
        raise FulcrumError(
            "LEDGER_UNAVAILABLE", "finish requires a configured brain", exit_code=4
        )
    manager = ConfigurationManager(request.instance.config_path)
    document, _ = manager.load()
    config = manager.effective(document)
    beads = config["beads"]
    executable = beads.get("executable") if isinstance(beads, Mapping) else None
    return Ledger(
        request.instance.brain_root,
        executable=str(executable) if executable else None,
        timeout=request.timeout,
    )


def _finish_replay(request: ParsedRequest) -> CommandResult | None:
    if request.request_id is None:
        return None
    ledger = _ledger(request)
    record = ledger.show(operation_id(request.request_id))
    if record is None:
        return None
    operation = OperationRecord.from_record(record)
    if operation.operation.get("command") != "finish" or operation.operation.get(
        "input"
    ) != accepted_input(request):
        raise FulcrumError(
            "REQUEST_CONFLICT",
            f"request ID {request.request_id} was already used with different input",
            exit_code=5,
            request_id=request.request_id,
            operation_id=operation.id,
        )
    if operation.operation.get("state") in TERMINAL_STATES:
        return _operation_result(operation)
    return None


def _weaver_payload(
    request: ParsedRequest, *, evidence_required: bool
) -> tuple[str, list[str]]:
    summary = request.input.get("summary")
    evidence = request.input.get("evidence", [])
    if not isinstance(summary, str) or not summary.strip():
        raise FulcrumError.invalid("INVALID_INPUT", "Weaver finish requires summary")
    if (
        not isinstance(evidence, list)
        or not all(isinstance(item, str) and item.strip() for item in evidence)
        or (evidence_required and not evidence)
    ):
        raise FulcrumError.invalid(
            "INVALID_INPUT",
            (
                "Weaver answer evidence must be a nonempty array of references"
                if evidence_required
                else "Weaver finish evidence must be an array of references"
            ),
        )
    return summary.strip(), list(evidence)


def _settle_plan_authoring_children(
    ledger: Ledger, plan: Mapping[str, Any], operation_id: str
) -> None:
    mapping = plan.get("children_by_key")
    keys = plan.get("approved_keys")
    if not isinstance(mapping, Mapping) or not isinstance(keys, list):
        return
    for key in keys:
        identifier = mapping.get(str(key))
        child = ledger.show(str(identifier)) if identifier else None
        if child is None or child.status == "closed" or not child.fc:
            continue
        fc = dict(child.fc)
        link = dict(fc.get("plan") or {})
        link["authoring_ready"] = True
        link["authoring_finish_operation"] = operation_id
        fc["plan"] = link
        fc["waiting"] = _without_waiting_kind(fc.get("waiting"), "authoring")
        remaining_reasons = (
            fc["waiting"].get("reasons", [])
            if isinstance(fc.get("waiting"), Mapping)
            else []
        )
        if any(
            isinstance(reason, Mapping) and reason.get("kind") == "external"
            for reason in remaining_reasons
        ):
            fc["next_action"] = (
                "Wait for required remote publication, then explicit future-plan activation."
            )
        else:
            fc["next_action"] = (
                "Wait for explicit activation of the approved future plan."
            )
        fc["last_transition"] = operation_id
        ledger.update_fc(child.id, fc)


def _without_waiting_kind(value: Any, kind: str) -> dict[str, Any] | None:
    reasons = value.get("reasons", []) if isinstance(value, Mapping) else []
    retained = [
        dict(reason)
        for reason in reasons
        if isinstance(reason, Mapping) and reason.get("kind") != kind
    ]
    return {"reasons": retained} if retained else None


def _finish_payload(request: ParsedRequest) -> dict[str, Any]:
    summary = request.input.get("summary")
    source_oid = request.input.get("source_oid")
    checks = request.input.get("checks")
    evidence = request.input.get("evidence")
    if not isinstance(summary, str) or not summary.strip():
        raise FulcrumError.invalid("INVALID_INPUT", "finish requires a summary")
    if (
        not isinstance(source_oid, str)
        or len(source_oid) != 40
        or any(character not in "0123456789abcdef" for character in source_oid)
    ):
        raise FulcrumError.invalid(
            "INVALID_INPUT", "finish requires a full lowercase source commit OID"
        )
    if not isinstance(checks, list) or not all(_valid_check(item) for item in checks):
        raise FulcrumError.invalid(
            "INVALID_INPUT",
            "finish checks must be objects with name, status, and evidence",
        )
    if not isinstance(evidence, list) or not all(
        isinstance(item, str) and item.strip() for item in evidence
    ):
        raise FulcrumError.invalid(
            "INVALID_INPUT", "finish evidence must be an array of references"
        )
    return {
        "summary": summary.strip(),
        "source_oid": source_oid,
        "checks": [dict(item) for item in checks],
        "evidence": list(evidence),
    }


def _valid_check(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value) == {"name", "status", "evidence"}
        and isinstance(value.get("name"), str)
        and bool(str(value["name"]).strip())
        and value.get("status") in CHECK_STATES
        and isinstance(value.get("evidence"), str)
        and bool(str(value["evidence"]).strip())
    )


def _inspect_clean_source(request: ParsedRequest, source_oid: str) -> dict[str, Any]:
    ledger, _, project, provider = _context(request)
    work = _reload_work(ledger, str(request.arguments["bead"]))
    from fulcrum.delivery_service import _work_ref

    reference = _work_ref(
        request, work, project, str((work.fc or {}).get("ownership_operation"))
    )
    facts = asyncio.run(provider.inspect_workspace(reference))
    if (
        not facts.exists
        or not facts.owned
        or facts.dirty
        or facts.head_oid != source_oid
    ):
        raise FulcrumError(
            "SOURCE_NOT_READY",
            "finish source must be the exact clean commit in the owned workspace",
            exit_code=5,
            details={"source_oid": source_oid, "workspace": facts.to_dict()},
        )
    return facts.to_dict()


def _child_request(
    request: ParsedRequest,
    parent_operation: str,
    purpose: str,
    command: tuple[str, ...],
) -> ParsedRequest:
    source_oid = str(request.input["source_oid"])
    arguments: dict[str, Any] = {
        "bead": request.arguments["bead"],
        "source": source_oid,
    }
    if command == ("review", "approve"):
        arguments["summary"] = str(request.input["summary"])
    return replace(
        request,
        command=command,
        arguments=arguments,
        input={},
        request_id=str(
            uuid.uuid5(FINISH_CHILD_NAMESPACE, f"{parent_operation}:{purpose}")
        ),
    )


def _child_result(result: CommandResult) -> dict[str, Any]:
    return {
        "operation_id": result.operation_id,
        "state": result.state.value,
        "ok": result.ok,
        "result": result.result,
    }


def _record_task_finish(ledger: Ledger, work: LedgerRecord, operation_id: str) -> None:
    owner = (work.fc or {}).get("owner")
    acquisition = (work.fc or {}).get("ownership_operation")
    matches = [
        item
        for item in ledger.list_records(kind="task", limit=0)
        if item.fc
        and item.fc.get("thread_id") == owner
        and item.fc.get("work_bead") == work.id
        and item.fc.get("ownership_operation") == acquisition
    ]
    if len(matches) != 1:
        raise FulcrumError(
            "TASK_CORRUPT",
            "finish could not identify one accountable managed task",
            exit_code=4,
            details={"matches": [item.id for item in matches]},
        )
    fc = dict(matches[0].fc or {})
    fc["finish_operation"] = operation_id
    fc["last_transition"] = operation_id
    ledger.update_fc(matches[0].id, fc)


def _reload_work(ledger: Ledger, bead_id: str) -> LedgerRecord:
    work = ledger.show(bead_id)
    if work is None or work.kind != "work" or not work.fc:
        raise FulcrumError.invalid("NOT_FOUND", f"unknown work {bead_id}")
    return work


def _work_summary(work: LedgerRecord) -> dict[str, Any]:
    fc = work.fc or {}
    return {
        "bead_id": work.id,
        "owner": fc.get("owner"),
        "role": fc.get("role"),
        "phase": fc.get("phase"),
        "ownership_operation": fc.get("ownership_operation"),
        "next_action": fc.get("next_action"),
    }


def _report_reminder(bead_id: str) -> dict[str, Any]:
    return {
        "required": False,
        "message": "Report incidental problems independently; a report failure does not revoke this accepted finish.",
        "next_command": ["fulcrum", "report", "--input", "-", "--json"],
        "discovered_from": bead_id,
    }


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
