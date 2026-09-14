"""Shared command and result contracts for Fulcrum.

The public CLI, IPC transport, controller, and offline execution all exchange these
immutable values. This module deliberately contains no workflow policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping


class CommandState(StrEnum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    CANCELLED = "cancelled"
    DEGRADED = "degraded"


@dataclass(frozen=True)
class ActorContext:
    kind: str
    task_id: str | None = None

    @classmethod
    def parse(cls, value: str) -> ActorContext:
        if value == "human":
            return cls(kind="human")
        if value.startswith("task:") and value[5:]:
            return cls(kind="task", task_id=value[5:])
        raise FulcrumError.invalid(
            "INVALID_ACTOR", "actor must be 'human' or 'task:<native-task-id>'"
        )

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "task_id": self.task_id}


@dataclass(frozen=True)
class InstanceContext:
    instance_root: Path
    config_path: Path
    brain_root: Path | None
    socket_path: Path
    lock_path: Path | None
    explicit_selection: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance_root": str(self.instance_root),
            "config_path": str(self.config_path),
            "brain_root": str(self.brain_root) if self.brain_root else None,
            "socket_path": str(self.socket_path),
            "lock_path": str(self.lock_path) if self.lock_path else None,
            "explicit_selection": self.explicit_selection,
        }


@dataclass(frozen=True)
class ParsedRequest:
    command: tuple[str, ...]
    arguments: Mapping[str, Any]
    input: Mapping[str, Any]
    actor: ActorContext
    instance: InstanceContext
    request_id: str | None
    project: str | None = None
    thread_id: str | None = None
    ownership_operation: str | None = None
    wait: bool = False
    timeout: float = 30.0
    offline: bool = False

    @property
    def command_name(self) -> str:
        return " ".join(self.command)

    def to_wire(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "arguments": dict(self.arguments),
            "input": dict(self.input),
            "actor": self.actor.to_dict(),
            "instance": self.instance.to_dict(),
            "request_id": self.request_id,
            "project": self.project,
            "thread_id": self.thread_id,
            "ownership_operation": self.ownership_operation,
            "wait": self.wait,
            "timeout": self.timeout,
            "offline": self.offline,
        }

    @classmethod
    def from_wire(cls, value: Mapping[str, Any]) -> ParsedRequest:
        try:
            command = value["command"]
            arguments = value["arguments"]
            supplied_input = value["input"]
            actor = value["actor"]
            instance = value["instance"]
            if not isinstance(command, list) or not all(
                isinstance(item, str) for item in command
            ):
                raise TypeError("command")
            if not isinstance(arguments, dict) or not isinstance(supplied_input, dict):
                raise TypeError("arguments/input")
            if not isinstance(actor, dict) or not isinstance(instance, dict):
                raise TypeError("actor/instance")
            brain = instance.get("brain_root")
            lock = instance.get("lock_path")
            return cls(
                command=tuple(command),
                arguments=arguments,
                input=supplied_input,
                actor=ActorContext(
                    kind=str(actor["kind"]), task_id=actor.get("task_id")
                ),
                instance=InstanceContext(
                    instance_root=Path(str(instance["instance_root"])),
                    config_path=Path(str(instance["config_path"])),
                    brain_root=Path(str(brain)) if brain else None,
                    socket_path=Path(str(instance["socket_path"])),
                    lock_path=Path(str(lock)) if lock else None,
                    explicit_selection=bool(instance.get("explicit_selection", True)),
                ),
                request_id=value.get("request_id"),
                project=value.get("project"),
                thread_id=value.get("thread_id"),
                ownership_operation=value.get("ownership_operation"),
                wait=bool(value.get("wait", False)),
                timeout=float(value.get("timeout", 30.0)),
                offline=bool(value.get("offline", False)),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise FulcrumError.invalid(
                "INVALID_REQUEST", "IPC request does not match the command contract"
            ) from error


@dataclass(frozen=True)
class ErrorInfo:
    code: str
    message: str
    retryable: bool
    next_command: tuple[str, ...] | None = None
    details: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "next_command": list(self.next_command) if self.next_command else None,
            "details": dict(self.details) if self.details is not None else None,
        }


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    state: CommandState
    operation_id: str | None = None
    request_id: str | None = None
    result: Mapping[str, Any] | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)
    error: ErrorInfo | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "state": self.state.value,
            "operation_id": self.operation_id,
            "request_id": self.request_id,
            "result": dict(self.result) if self.result is not None else None,
            "warnings": list(self.warnings),
            "error": self.error.to_dict() if self.error else None,
        }

    @classmethod
    def query(cls, result: Mapping[str, Any]) -> CommandResult:
        return cls(ok=True, state=CommandState.COMPLETED, result=result)


class FulcrumError(Exception):
    """A classified public failure with a stable CLI exit code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        exit_code: int,
        retryable: bool = False,
        state: CommandState = CommandState.FAILED,
        next_command: tuple[str, ...] | None = None,
        details: Mapping[str, Any] | None = None,
        request_id: str | None = None,
        operation_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.retryable = retryable
        self.state = state
        self.next_command = next_command
        self.details = details
        self.request_id = request_id
        self.operation_id = operation_id

    @classmethod
    def invalid(
        cls, code: str, message: str, *, details: Mapping[str, Any] | None = None
    ) -> FulcrumError:
        return cls(code, message, exit_code=2, details=details)

    def to_result(self) -> CommandResult:
        return CommandResult(
            ok=False,
            state=self.state,
            operation_id=self.operation_id,
            request_id=self.request_id,
            error=ErrorInfo(
                code=self.code,
                message=self.message,
                retryable=self.retryable,
                next_command=self.next_command,
                details=self.details,
            ),
        )
