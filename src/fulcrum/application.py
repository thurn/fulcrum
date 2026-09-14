"""Small application boundary shared by the controller and offline CLI."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from fulcrum.contracts import CommandResult, CommandState, FulcrumError, ParsedRequest
from fulcrum.configuration import ConfigurationService, ProjectService
from fulcrum.ledger import (
    Ledger,
    LedgerFailure,
    OperationRecord,
    OperationService,
    operation_view,
)

Handler = Callable[[ParsedRequest], CommandResult]


class Application:
    def __init__(self) -> None:
        self._handlers: dict[tuple[str, ...], Handler] = {}
        self.register(("status",), self._status)
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
        self.register(("operation", "show"), self._operation_show)
        self.register(("operation", "list"), self._operation_list)
        self.register(("operation", "wait"), self._operation_wait)
        self.register(("operation", "cancel"), self._operation_cancel)
        self.register(("operation", "reconcile"), self._operation_reconcile)

    def register(self, command: tuple[str, ...], handler: Handler) -> None:
        if command in self._handlers:
            raise ValueError(f"handler already registered for {' '.join(command)}")
        self._handlers[command] = handler

    def dispatch(self, request: ParsedRequest) -> CommandResult:
        handler = self._handlers.get(request.command)
        if handler is None:
            raise FulcrumError(
                "CAPABILITY_UNAVAILABLE",
                f"{' '.join(request.command)} is not implemented by this installation",
                exit_code=4,
                retryable=False,
                request_id=request.request_id,
                next_command=("fulcrum", "doctor", "--json"),
                details={"command": list(request.command)},
            )
        try:
            return handler(request)
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

    def _status(self, request: ParsedRequest) -> CommandResult:
        operations: list[dict[str, Any]] = []
        gaps: list[dict[str, Any]] = []
        try:
            service = self._operations(request)
            operations = [
                operation_view(OperationRecord.from_record(item))
                for item in service.ledger.list_records(kind="operation", limit=20)
            ]
        except (FulcrumError, LedgerFailure) as error:
            gaps.append({"component": "ledger", "reason": str(error)})
        return CommandResult.query(
            {
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "instance": request.instance.to_dict(),
                "work": [],
                "operations": operations,
                "capacity": None,
                "publication": None,
                "gaps": gaps,
            }
        )

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
        return CommandResult.query(
            {
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "instance": str(request.instance.instance_root),
                "socket": {"path": str(socket), "exists": socket.exists()},
                "config": {
                    "path": str(request.instance.config_path),
                    "exists": request.instance.config_path.exists(),
                },
                "process": {"pid": os.getpid(), "controller_pid": None},
                "responsive": None,
                "gaps": ["controller process ownership is not yet recorded"],
            }
        )


def default_application() -> Application:
    return Application()
