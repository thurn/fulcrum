"""Small application boundary shared by the controller and offline CLI."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from datetime import datetime, timezone

from fulcrum.contracts import CommandResult, CommandState, FulcrumError, ParsedRequest
from fulcrum.configuration import ConfigurationService, ProjectService
from fulcrum.completion import CompletionService
from fulcrum.diagnostics import DiagnosticLog, DiagnosticService
from fulcrum.delivery_service import DeliveryService
from fulcrum.ledger import (
    Ledger,
    LedgerFailure,
    OperationRecord,
    OperationService,
)
from fulcrum.leadership import LeadershipService
from fulcrum.knowledge import KnowledgeService, MemoryService
from fulcrum.plans import PlanService
from fulcrum.publication import LedgerPublicationService
from fulcrum.runtime_service import RuntimeService, TaskService
from fulcrum.reviews import ReviewService
from fulcrum.roles import RoleService
from fulcrum.supervision import ReconciliationService
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
        self.register(("service", "status"), self._service_status)
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
        self.register(("project", "remove"), projects.remove)
        runtime = RuntimeService()
        self.register(("runtime", "capabilities"), runtime.capabilities)
        self.register(("runtime", "status"), runtime.status)
        tasks = TaskService()
        self.register(("task", "list"), tasks.list)
        self.register(("task", "show"), tasks.show)
        self.register(("task", "start"), tasks.start)
        self.register(("task", "send"), tasks.send)
        self.register(("task", "output"), tasks.output)
        self.register(("task", "wait"), tasks.wait)
        self.register(("task", "interrupt"), tasks.interrupt)
        self.register(("task", "requests"), tasks.requests)
        self.register(("task", "respond"), tasks.respond)
        self.register(("task", "terminals"), tasks.terminals)
        self.register(("task", "terminal", "stop"), tasks.terminal_stop)
        self.register(("task", "release"), tasks.release)
        self.register(("task", "archive"), tasks.archive)
        self.register(("task", "unarchive"), tasks.unarchive)
        self.register(("task", "delete"), tasks.delete)
        delivery = DeliveryService()
        self.register(("worktree", "prepare"), delivery.worktree_prepare)
        self.register(("worktree", "inspect"), delivery.worktree_inspect)
        self.register(("worktree", "cleanup"), delivery.worktree_cleanup)
        self.register(("validation", "start"), delivery.validation_start)
        self.register(("validation", "show"), delivery.validation_show)
        self.register(("review", "approve"), delivery.review_approve)
        self.register(("promotion", "start"), delivery.promotion_start)
        self.register(("promotion", "show"), delivery.promotion_show)
        self.register(("source", "sync"), delivery.source_sync)
        roles = RoleService()
        self.register(("enter",), roles.enter)
        self.register(("context",), roles.context)
        self.register(("hook", "context"), roles.hook_context)
        leadership = LeadershipService()
        self.register(("leader", "show"), leadership.leader_show)
        self.register(("marshal", "brief"), leadership.marshal_brief)
        self.register(("marshal", "request"), leadership.marshal_request)
        self.register(("marshal", "decide"), leadership.marshal_decide)
        self.register(("backlog", "list"), leadership.backlog_list)
        self.register(("dispatch",), leadership.dispatch)
        reviews = ReviewService()
        self.register(("plan", "review", "start"), reviews.start)
        self.register(("plan", "review", "finish"), reviews.finish)
        plans = PlanService()
        self.register(("plan", "draft"), plans.draft)
        self.register(("plan", "show"), plans.show)
        self.register(("plan", "approve"), plans.approve)
        self.register(("plan", "publish"), plans.publish)
        self.register(("plan", "refine"), plans.refine)
        self.register(("plan", "activate"), plans.activate)
        self.register(("plan", "complete"), plans.complete)
        memory = MemoryService()
        self.register(("memory", "list"), memory.list)
        self.register(("memory", "show"), memory.show)
        self.register(("memory", "set"), memory.set)
        knowledge = KnowledgeService()
        self.register(("knowledge", "publish"), knowledge.publish)
        self.register(("config", "sync"), knowledge.config_sync)
        publication = LedgerPublicationService()
        self.register(("ledger", "sync"), publication.sync)
        self.register(("ledger", "status"), publication.status)
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
        self.register(("reconcile",), ReconciliationService(self).reconcile)

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
            "turn_id": None,
            "role": request.arguments.get("role"),
            "adapter": "beads" if adapter_error else "application",
            "duration_ms": int((time.monotonic() - started) * 1000),
            "outcome": result.state.value if result else "failed",
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
        except (OSError, FulcrumError):
            pass

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
        return self._ledger_call(request, "reconcile")

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

    @staticmethod
    def _service_status(request: ParsedRequest) -> CommandResult:
        socket = request.instance.socket_path
        health_path = request.instance.instance_root / "service-health.json"
        controller_pid: int | None = None
        try:
            retained = json.loads(health_path.read_text(encoding="utf-8"))
            if isinstance(retained, dict):
                pids = [
                    value.get("pid")
                    for value in retained.values()
                    if isinstance(value, dict) and isinstance(value.get("pid"), int)
                ]
                if pids and len(set(pids)) == 1:
                    controller_pid = pids[0]
        except (OSError, json.JSONDecodeError):
            pass
        if not request.offline:
            controller_pid = os.getpid()
        return CommandResult.query(
            {
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "instance": str(request.instance.instance_root),
                "socket": {"path": str(socket), "exists": socket.exists()},
                "config": {
                    "path": str(request.instance.config_path),
                    "exists": request.instance.config_path.exists(),
                },
                "process": {
                    "pid": os.getpid(),
                    "controller_pid": controller_pid,
                },
                "responsive": not request.offline,
                "gaps": (
                    []
                    if not request.offline
                    else ["controller did not answer this inspection"]
                ),
            }
        )


def default_application() -> Application:
    return Application()
