"""Fresh-process application boundary shared by CLI and MCP operations."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from fulcrum.analytics import AnalyticsService
from fulcrum.completion import CompletionService
from fulcrum.configuration import ConfigurationService, ProjectService
from fulcrum.contracts import CommandResult, CommandState, FulcrumError, ParsedRequest
from fulcrum.diagnostics import DiagnosticLog, DiagnosticService
from fulcrum.delivery_service import DeliveryService
from fulcrum.desktop_protocol import DesktopProtocolService
from fulcrum.desktop_leadership import DesktopLeadershipService
from fulcrum.desktop_setup import DesktopSetupService
from fulcrum.ledger import (
    Ledger,
    LedgerFailure,
    OperationRecord,
    OperationService,
    operation_view,
)
from fulcrum.knowledge import KnowledgeService
from fulcrum.hooks import HookService
from fulcrum.plans import PlanService
from fulcrum.publication import LedgerPublicationService
from fulcrum.desktop_reset import ResetService
from fulcrum.desktop_services import ServiceService, SkillsService
from fulcrum.work import WorkService

Handler = Callable[[ParsedRequest], CommandResult]


class Application:
    def __init__(self) -> None:
        self._handlers: dict[tuple[str, ...], Handler] = {}
        diagnostics = DiagnosticService()
        self.register(("status",), diagnostics.status)
        self.register(("doctor",), diagnostics.doctor)
        self.register(("logs",), diagnostics.logs)
        self.register(("logs", "prune"), diagnostics.prune)
        self.register(("trace",), diagnostics.trace)
        self.register(("wait",), diagnostics.wait)
        self.register(("bootstrap",), DesktopSetupService().bootstrap)
        services = ServiceService()
        self.register(("service", "start"), services.start)
        self.register(("service", "stop"), services.stop)
        self.register(("service", "restart"), services.restart)
        self.register(("service", "update"), services.update)
        self.register(("service", "status"), services.status)
        self.register(("reset",), ResetService().hard_reset)
        self.register(("skills", "reconcile"), SkillsService().reconcile)
        configuration = ConfigurationService()
        self.register(("config", "show"), configuration.show)
        self.register(("config", "validate"), configuration.validate)
        self.register(("config", "set"), configuration.set)
        self.register(("policy", "show"), configuration.policy_show)
        self.register(("policy", "set"), configuration.policy_set)
        projects = ProjectService()
        self.register(("project", "list"), projects.list)
        self.register(("project", "show"), projects.show)
        self.register(("project", "add"), projects.add)
        self.register(("project", "enable"), projects.enable)
        self.register(("project", "disable"), projects.disable)
        delivery = DeliveryService()
        self.register(("worktree", "prepare"), delivery.worktree_prepare)
        self.register(("worktree", "inspect"), delivery.worktree_inspect)
        self.register(("worktree", "cleanup"), delivery.worktree_cleanup)
        self.register(("validation", "check"), delivery.validation_check)
        self.register(("validation", "start"), delivery.validation_start)
        self.register(("validation", "show"), delivery.validation_show)
        self.register(("review", "approve"), delivery.review_approve)
        self.register(("promotion", "start"), delivery.promotion_start)
        self.register(("promotion", "show"), delivery.promotion_show)
        self.register(("source", "sync"), delivery.source_sync)
        plans = PlanService()
        self.register(("plan", "draft"), plans.draft)
        self.register(("plan", "show"), plans.show)
        self.register(("plan", "approve"), plans.approve)
        self.register(("plan", "publish"), plans.publish)
        self.register(("plan", "refine"), plans.refine)
        self.register(("plan", "activate"), plans.activate)
        self.register(("plan", "complete"), plans.complete)
        knowledge = KnowledgeService()
        self.register(("config", "sync"), knowledge.config_sync)
        publication = LedgerPublicationService()
        self.register(("ledger", "sync"), publication.sync)
        self.register(("ledger", "status"), publication.status)
        analytics = AnalyticsService()
        self.register(("usage",), analytics.usage)
        self.register(("cost",), analytics.cost)
        self.register(("rates", "list"), analytics.rates_list)
        self.register(("rates", "show"), analytics.rates_show)
        self.register(("rates", "add"), analytics.rates_add)
        self.register(("usage", "reconcile"), analytics.reconcile)
        work = WorkService()
        self.register(("work", "create"), work.create)
        self.register(("work", "show"), work.show)
        self.register(("work", "list"), work.list)
        self.register(("work", "children"), work.children)
        self.register(("work", "adopt"), work.adopt)
        self.register(("work", "update"), work.update)
        self.register(("work", "dependencies"), work.dependencies)
        self.register(("work", "close"), work.close)
        self.register(("work", "reopen"), work.reopen)
        self.register(("finish",), CompletionService().finish)
        self.register(("progress",), work.progress)
        self.register(("report",), work.report)
        self.register(("operation", "show"), self._operation_show)
        self.register(("operation", "list"), self._operation_list)
        self.register(("operation", "wait"), self._operation_wait)
        self.register(("operation", "cancel"), self._operation_cancel)
        self.register(("operation", "reconcile"), self._operation_reconcile)
        desktop = DesktopProtocolService()
        self.register(("register", "standing"), desktop.register_standing)
        self.register(("action", "claim"), desktop.claim_action)
        self.register(("action", "result"), desktop.report_action_result)
        self.register(("instruction", "wait"), desktop.wait_for_instructions)
        self.register(("worker", "register"), desktop.register_worker)
        self.register(("candidate", "submit"), desktop.submit_candidate)
        self.register(("ci", "wait"), desktop.wait_for_ci_results)
        self.register(("pause",), desktop.pause)
        self.register(("resume",), desktop.resume)
        self.register(("hook", "handle"), HookService().handle)
        stock_leadership = DesktopLeadershipService()
        self.register(("marshal", "check"), stock_leadership.marshal_check)
        self.register(("marshal", "apply"), stock_leadership.marshal_decide)
        self.register(("incident", "report"), stock_leadership.report_incident)
        self.register(("repair", "record"), stock_leadership.record_repair)
        self.register(("recovery", "prepare"), stock_leadership.recovery_prepare)
        self.register(("decision", "respond"), stock_leadership.decision_respond)

    def register(self, command: tuple[str, ...], handler: Handler) -> None:
        if command in self._handlers:
            raise ValueError(f"handler already registered for {' '.join(command)}")
        self._handlers[command] = handler

    def replace_handler(self, command: tuple[str, ...], handler: Handler) -> None:
        if command not in self._handlers:
            raise ValueError(
                f"cannot replace unregistered handler for {' '.join(command)}"
            )
        self._handlers[command] = handler

    def dispatch(self, request: ParsedRequest) -> CommandResult:
        started = time.monotonic()
        handler = self._handlers.get(request.command)
        if handler is None:
            error = FulcrumError(
                "CAPABILITY_UNAVAILABLE",
                f"{' '.join(request.command)} is not implemented by this installation",
                exit_code=4,
                retryable=False,
                request_id=request.request_id,
                next_command=("fulcrum", "doctor", "--json"),
                details={"command": list(request.command)},
            )
            self._log(request, started, error=error)
            raise error
        try:
            result = handler(request)
        except LedgerFailure as error:
            state = CommandState.UNCERTAIN if error.uncertain else CommandState.FAILED
            converted = FulcrumError(
                "LEDGER_UNCERTAIN" if error.uncertain else "LEDGER_UNAVAILABLE",
                str(error),
                exit_code=4,
                retryable=error.retryable,
                state=state,
                request_id=request.request_id,
                details={
                    "category": error.category,
                    "returncode": error.returncode,
                    "duration_ms": error.duration_ms,
                    "truncated": error.truncated,
                },
            )
            self._log(request, started, error=converted, adapter_error=error)
            raise converted from error
        except FulcrumError as error:
            self._log(request, started, error=error)
            raise
        except Exception as error:
            self._log(request, started, error=error)
            raise
        self._log(request, started, result=result)
        return result

    @staticmethod
    def _log(
        request: ParsedRequest,
        started: float,
        *,
        result: CommandResult | None = None,
        error: Exception | None = None,
        adapter_error: LedgerFailure | None = None,
    ) -> None:
        public_error = error if isinstance(error, FulcrumError) else None
        event = {
            "event": "command_completed",
            "command": request.command_name,
            "request_id": request.request_id,
            "operation_id": (
                result.operation_id
                if result
                else public_error.operation_id if public_error else None
            ),
            "bead_id": request.arguments.get("id")
            or request.arguments.get("bead")
            or request.input.get("bead"),
            "task_id": request.thread_id,
            "turn_id": _result_turn_id(result),
            "associated_beads": _result_associated_beads(result),
            "role": request.arguments.get("role"),
            "adapter": "beads" if adapter_error else "application",
            "duration_ms": int((time.monotonic() - started) * 1000),
            "outcome": result.state.value if result else "failed",
            "request": _bounded_diagnostic_mapping(
                {
                    "arguments": dict(request.arguments),
                    "input": dict(request.input),
                    "project": request.project,
                    "ownership_operation": request.ownership_operation,
                }
            ),
            "result": _bounded_diagnostic_mapping(
                result.to_dict() if result is not None else {}
            ),
            "error_category": (
                adapter_error.category
                if adapter_error
                else (
                    public_error.code
                    if public_error
                    else type(error).__name__ if error else None
                )
            ),
            "error": str(error) if error else None,
            "stdout": adapter_error.stdout if adapter_error else None,
            "stderr": adapter_error.stderr if adapter_error else None,
            "truncated": adapter_error.truncated if adapter_error else False,
        }
        try:
            DiagnosticLog.from_request(request).append(event)
        except (OSError, FulcrumError) as log_error:
            DiagnosticLog.report_failure(request.instance.instance_root, log_error)

    @staticmethod
    def _operations(request: ParsedRequest) -> OperationService:
        if request.instance.brain_root is None:
            raise FulcrumError(
                "LEDGER_UNAVAILABLE", "a valid brain root is required", exit_code=4
            )
        return OperationService(
            Ledger(request.instance.brain_root, timeout=request.timeout)
        )

    def _operation_show(self, request: ParsedRequest) -> CommandResult:
        return self._ledger_call(request, "show")

    def _operation_list(self, request: ParsedRequest) -> CommandResult:
        return self._ledger_call(request, "list")

    def _operation_wait(self, request: ParsedRequest) -> CommandResult:
        return self._ledger_call(request, "wait")

    def _operation_cancel(self, request: ParsedRequest) -> CommandResult:
        return self._ledger_call(request, "cancel")

    def _operation_reconcile(self, request: ParsedRequest) -> CommandResult:
        service = self._operations(request)
        target_id = str(request.arguments["id"])
        target = service.ledger.show(target_id)
        if (
            target is not None
            and target.kind == "operation"
            and (target.fc or {}).get("command") == "ledger.sync"
        ):
            return LedgerPublicationService().reconcile(
                request, OperationRecord.from_record(target)
            )
        if (
            target is not None
            and target.kind == "operation"
            and (target.fc or {}).get("command")
            in {"validation.start", "promotion.start"}
        ):
            return self._reconcile_delivery_operation(
                request, service, OperationRecord.from_record(target)
            )
        return self._ledger_call(request, "reconcile")

    def _reconcile_delivery_operation(
        self,
        request: ParsedRequest,
        service: OperationService,
        target: OperationRecord,
    ) -> CommandResult:
        receipt, reused = service.ledger.create_operation(
            request,
            planned={"target_operation": target.id},
            next_action="Inspect the exact retained delivery source and provider handle.",
        )
        if reused and receipt.operation.get("state") in {
            "completed",
            "failed",
            "uncertain",
            "cancelled",
        }:
            return _operation_result(receipt)
        bead_id = target.operation.get("bead_id")
        if not isinstance(bead_id, str):
            raise FulcrumError.invalid(
                "OPERATION_CORRUPT", "delivery receipt has no work bead"
            )
        command = str(target.operation.get("command"))
        query = replace(
            request,
            command=(
                ("validation", "show")
                if command == "validation.start"
                else ("promotion", "show")
            ),
            arguments={"bead": bead_id},
            input={},
            request_id=None,
        )
        try:
            observed = self.dispatch(query).result or {}
        except FulcrumError as error:
            receipt = service.ledger.update_operation(
                receipt,
                state="uncertain",
                step="delivery_postcondition_unproved",
                error={
                    "code": error.code,
                    "message": error.message,
                    "retryable": error.retryable,
                },
                next_action="Repair provider inspection before deciding whether to replay.",
            )
            return _operation_result(receipt)
        delivery = observed.get("delivery") if isinstance(observed, dict) else None
        state = (
            observed.get("state")
            if command == "validation.start"
            else delivery.get("promotion") if isinstance(delivery, dict) else None
        )
        applied = state in {
            "pending",
            "running",
            "passed",
            "failed",
            "promoted",
        }
        if applied:
            target = service.ledger.update_operation(
                target,
                state="completed",
                step="delivery_effect_reconciled",
                external={
                    "provider": "observed",
                    "handle": observed.get("handle")
                    or (delivery.get("handle") if isinstance(delivery, dict) else None),
                },
                result={"observed": observed},
                error={},
                next_action="Observe the retained provider state through normal reconciliation.",
            )
            receipt_state = "completed"
        else:
            receipt_state = "uncertain"
        receipt = service.ledger.update_operation(
            receipt,
            state=receipt_state,
            step="delivery_postcondition_inspected",
            result={
                "target": operation_view(target),
                "settled": applied,
                "observed": observed,
            },
            next_action=(
                "No further action is required."
                if applied
                else "Resolve the provider observation gap without replaying the effect."
            ),
        )
        return _operation_result(receipt)

    def _ledger_call(self, request: ParsedRequest, method: str) -> CommandResult:
        try:
            service = self._operations(request)
            return getattr(service, method)(request)
        except LedgerFailure as error:
            state = CommandState.UNCERTAIN if error.uncertain else CommandState.FAILED
            raise FulcrumError(
                "LEDGER_UNCERTAIN" if error.uncertain else "LEDGER_UNAVAILABLE",
                str(error),
                exit_code=4,
                retryable=error.retryable,
                state=state,
                request_id=request.request_id,
                details={
                    "category": error.category,
                    "returncode": error.returncode,
                    "duration_ms": error.duration_ms,
                    "truncated": error.truncated,
                },
            ) from error


def default_application() -> Application:
    return Application()


def _result_payload(result: CommandResult | None) -> Mapping[str, Any]:
    if result is None or not isinstance(result.result, Mapping):
        return {}
    nested = result.result.get("result")
    return nested if isinstance(nested, Mapping) else result.result


def _result_turn_id(result: CommandResult | None) -> str | None:
    payload = _result_payload(result)
    turn = payload.get("turn")
    return str(turn["id"]) if isinstance(turn, Mapping) and turn.get("id") else None


def _result_associated_beads(result: CommandResult | None) -> list[str]:
    payload = _result_payload(result)
    values = payload.get("selected_ids") or payload.get("associated_beads") or []
    if isinstance(values, list) and values:
        return [str(item) for item in values]
    rows = payload.get("rows")
    if isinstance(rows, list):
        return [
            str(row["bead_id"])
            for row in rows
            if isinstance(row, Mapping) and row.get("bead_id")
        ]
    bead_id = payload.get("bead_id")
    return [str(bead_id)] if bead_id else []


def _bounded_diagnostic_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    if len(encoded.encode("utf-8")) <= 32 * 1024:
        return dict(value)
    return {
        "truncated": True,
        "encoded_bytes": len(encoded.encode("utf-8")),
        "keys": sorted(str(key) for key in value),
    }


def _operation_result(operation: OperationRecord) -> CommandResult:
    value = str(operation.operation.get("state") or "running")
    state = (
        CommandState(value)
        if value in CommandState._value2member_map_
        else CommandState.RUNNING
    )
    return CommandResult(
        ok=state
        not in {
            CommandState.FAILED,
            CommandState.UNCERTAIN,
            CommandState.CANCELLED,
        },
        state=state,
        operation_id=operation.id,
        request_id=str(operation.operation.get("request_id")),
        result=operation_view(operation),
    )
