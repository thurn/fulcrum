"""Durable stock-Desktop workflow protocol.

The policy in this module runs only in fresh command processes.  Beads remains
the authority; the broker and MCP server retain connections, never workflow
decisions.  Every public mutation is an idempotent replacement of one owning
record under the process-shared state lock supplied by :mod:`fulcrum.coordination`.
"""

from __future__ import annotations

import copy
import os
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from fulcrum.configuration import ConfigurationManager
from fulcrum.contracts import CommandResult, CommandState, FulcrumError, ParsedRequest
from fulcrum.coordination import coordinated
from fulcrum.diagnostics import DiagnosticLog
from fulcrum.ledger import Ledger, LedgerRecord

ACTION_STATES = {
    "pending",
    "issuing",
    "succeeded",
    "rejected",
    "uncertain",
    "superseded",
}
TERMINAL_ACTION_STATES = {"succeeded", "rejected", "superseded"}
WAIT_STATES = {"waiting", "resolved", "cancelled", "expired"}
STANDING_ROLES = {"steward", "marshal", "vizier"}
WORKER_ROLES = {"weaver", "executor", "warden", "sage", "mason", "justiciar"}
MAX_UNPROJECTED_TRANSITIONS = 128


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _opaque(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4()}"


def action_marker(
    instance: str, record_id: str, action_id: str, assignment_token: str | None = None
) -> str:
    import json

    value: dict[str, str] = {
        "instance": instance,
        "record_id": record_id,
        "action_id": action_id,
    }
    if assignment_token:
        value["assignment_token"] = assignment_token
    return "Fulcrum-Action: " + json.dumps(value, separators=(",", ":"))


def _protocol(fc: Mapping[str, Any]) -> dict[str, Any]:
    value = fc.get("desktop")
    return copy.deepcopy(dict(value)) if isinstance(value, Mapping) else {}


def _with_protocol(
    fc: Mapping[str, Any], protocol: Mapping[str, Any]
) -> dict[str, Any]:
    return {**dict(fc), "desktop": copy.deepcopy(dict(protocol))}


def _request_input(request: ParsedRequest) -> dict[str, Any]:
    return {
        "command": list(request.command),
        "arguments": copy.deepcopy(dict(request.arguments)),
        "input": copy.deepcopy(dict(request.input)),
        "actor": request.actor.to_dict(),
        "thread_id": request.thread_id,
        "project": request.project,
        "ownership_operation": request.ownership_operation,
    }


def _request_id(request: ParsedRequest) -> str:
    if not request.request_id:
        raise FulcrumError.invalid(
            "REQUEST_ID_REQUIRED", "this mutation requires a stable request ID"
        )
    return request.request_id


def _saved_request(
    protocol: Mapping[str, Any], request: ParsedRequest
) -> Mapping[str, Any] | None:
    requests = protocol.get("requests")
    saved = (
        requests.get(_request_id(request)) if isinstance(requests, Mapping) else None
    )
    if not isinstance(saved, Mapping):
        return None
    if saved.get("accepted_input") != _request_input(request):
        raise FulcrumError(
            "REQUEST_CONFLICT",
            "request ID was already used with different input",
            exit_code=5,
            request_id=request.request_id,
        )
    return saved


def _save_request(
    protocol: dict[str, Any],
    request: ParsedRequest,
    result: Mapping[str, Any],
    *,
    state: str = "completed",
) -> None:
    requests = dict(protocol.get("requests") or {})
    requests[_request_id(request)] = {
        "accepted_input": _request_input(request),
        "source_commit": os.environ.get("FULCRUM_COMMIT"),
        "operation_id": _opaque("operation"),
        "state": state,
        "result": copy.deepcopy(dict(result)),
        "recorded_at": _utc_now(),
    }
    protocol["requests"] = requests


def _replay(saved: Mapping[str, Any], request: ParsedRequest) -> CommandResult:
    result = saved.get("result")
    return CommandResult(
        ok=str(saved.get("state")) not in {"failed", "uncertain"},
        state=(
            CommandState(str(saved.get("state")))
            if str(saved.get("state")) in CommandState._value2member_map_
            else CommandState.COMPLETED
        ),
        request_id=request.request_id,
        operation_id=(
            str(saved.get("operation_id")) if saved.get("operation_id") else None
        ),
        result=dict(result) if isinstance(result, Mapping) else {},
    )


def _result(request: ParsedRequest, value: Mapping[str, Any]) -> CommandResult:
    return CommandResult(
        ok=True,
        state=CommandState.COMPLETED,
        request_id=request.request_id,
        result=dict(value),
    )


class DesktopProtocolService:
    """Fresh-process policy over the authoritative Beads records."""

    def __init__(
        self,
        ledger: Ledger | None = None,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._ledger_override = ledger
        self.now: Callable[[], datetime] = now or (lambda: datetime.now(timezone.utc))

    def _ledger(self, request: ParsedRequest) -> Ledger:
        if self._ledger_override is not None:
            return self._ledger_override
        if request.instance.brain_root is None:
            raise FulcrumError(
                "LEDGER_UNAVAILABLE", "a valid brain root is required", exit_code=4
            )
        executable: str | None = None
        try:
            manager = ConfigurationManager(request.instance.config_path)
            document, _ = manager.load()
            configured = manager.effective(document)["beads"].get("executable")
            executable = str(configured) if configured else None
        except FulcrumError:
            pass
        return Ledger(
            request.instance.brain_root,
            executable=executable,
            timeout=request.timeout,
        )

    def _system(self, ledger: Ledger) -> LedgerRecord:
        record = ledger.show("fc-system")
        if record is not None:
            return record
        return ledger.create_record(
            record_id="fc-system",
            kind="control",
            title="Fulcrum system",
            description="Authoritative stock Desktop coordination state.",
            owner="SYSTEM",
            fc={
                "kind": "control",
                "owner": "SYSTEM",
                "desktop": {
                    "run_control": "paused",
                    "standing": {},
                    "requests": {},
                    "instruction_waits": {},
                },
            },
        )

    def _record(self, ledger: Ledger, record_id: str) -> LedgerRecord:
        record = ledger.show(record_id)
        if record is None:
            raise FulcrumError.invalid("NOT_FOUND", f"unknown record {record_id}")
        return record

    def _standing_actor(
        self, ledger: Ledger, role: str, request: ParsedRequest
    ) -> Mapping[str, Any]:
        system = self._system(ledger)
        standing = _protocol(system.fc or {}).get("standing")
        binding = standing.get(role) if isinstance(standing, Mapping) else None
        if not isinstance(binding, Mapping) or binding.get("state") != "registered":
            raise FulcrumError(
                "STANDING_NOT_REGISTERED",
                f"{role} is not registered",
                exit_code=5,
            )
        task_id = request.actor.task_id or request.thread_id
        if request.actor.kind != "task" or task_id != binding.get("task_id"):
            raise FulcrumError(
                "AUTHORITY_MISMATCH",
                f"only the registered {role} may perform this operation",
                exit_code=5,
            )
        return binding

    def _write_event(self, request: ParsedRequest, event: str, **fields: Any) -> None:
        if self._ledger_override is not None:
            return
        try:
            DiagnosticLog.from_request(request).append(
                {
                    "event": event,
                    "component": "desktop_protocol",
                    "process_id": os.getpid(),
                    "source_commit": os.environ.get("FULCRUM_COMMIT"),
                    "request_id": request.request_id,
                    **fields,
                }
            )
        except OSError as error:
            DiagnosticLog.report_failure(request.instance.instance_root, error)

    @coordinated
    def register_standing(self, request: ParsedRequest) -> CommandResult:
        ledger = self._ledger(request)
        system = self._system(ledger)
        protocol = _protocol(system.fc or {})
        saved = _saved_request(protocol, request)
        if saved:
            return _replay(saved, request)
        role = str(request.input.get("role") or request.arguments.get("role") or "")
        if role not in STANDING_ROLES:
            raise FulcrumError.invalid("INVALID_ROLE", "unknown standing role")
        task_id = str(request.input.get("task_id") or request.thread_id or "")
        host_id = str(request.input.get("host_id") or "")
        session_id = str(request.input.get("session_id") or "")
        if not task_id or not session_id:
            raise FulcrumError.invalid(
                "IDENTITY_REQUIRED", "task_id and session_id are required"
            )
        standing = dict(protocol.get("standing") or {})
        current = standing.get(role)
        if isinstance(current, Mapping) and current.get("task_id") != task_id:
            raise FulcrumError(
                "IDENTITY_CONFLICT",
                f"{role} is already bound to another task",
                exit_code=5,
            )
        binding = {
            "role": role,
            "task_id": task_id,
            "host_id": host_id or None,
            "session_id": session_id,
            "turn_id": request.input.get("turn_id"),
            "state": "registered",
            "registered_at": _utc_now(),
        }
        standing[role] = binding
        protocol["standing"] = standing
        value = {"standing": binding}
        _save_request(protocol, request, value)
        ledger.update_fc(system.id, _with_protocol(system.fc or {}, protocol))
        self._write_event(
            request,
            "standing_registered",
            task_id=task_id,
            role=role,
            outcome="completed",
        )
        return _result(request, value)

    @coordinated
    def queue_action(self, request: ParsedRequest) -> CommandResult:
        """Internal deterministic compiler boundary used by setup/work transitions."""

        ledger = self._ledger(request)
        record_id = str(request.input.get("record_id") or "fc-system")
        record = self._record(ledger, record_id)
        protocol = _protocol(record.fc or {})
        saved = _saved_request(protocol, request)
        if saved:
            return _replay(saved, request)
        executor = str(request.input.get("executor") or "")
        tool = str(request.input.get("tool") or "")
        arguments = request.input.get("arguments")
        if executor not in {*STANDING_ROLES, "bootstrap"} or not tool:
            raise FulcrumError.invalid(
                "INVALID_ACTION", "executor and tool are required"
            )
        if not isinstance(arguments, Mapping):
            raise FulcrumError.invalid(
                "INVALID_ACTION", "action arguments must be an object"
            )
        actions = dict(protocol.get("actions") or {})
        action_id = _opaque("action")
        assignment = request.input.get("assignment_token")
        action = {
            "action_id": action_id,
            "record_id": record.id,
            "executor": executor,
            "tool": tool,
            "arguments": copy.deepcopy(dict(arguments)),
            "expected_result": copy.deepcopy(request.input.get("expected_result")),
            "reporting": copy.deepcopy(request.input.get("reporting") or {}),
            "assignment_token": assignment,
            "state": "pending",
            "attempts": [],
            "created_at": _utc_now(),
        }
        actions[action_id] = action
        protocol["actions"] = actions
        value = {"action": self._action_response(request, action)}
        _save_request(protocol, request, value)
        ledger.update_fc(record.id, _with_protocol(record.fc or {}, protocol))
        self._write_event(
            request,
            "action_committed",
            action_id=action_id,
            bead_id=None if record.id == "fc-system" else record.id,
            tool=tool,
            executor=executor,
            outcome="pending",
        )
        return _result(request, value)

    def _action_response(
        self, request: ParsedRequest, action: Mapping[str, Any]
    ) -> dict[str, Any]:
        arguments = copy.deepcopy(dict(action.get("arguments") or {}))
        marker = action_marker(
            str(request.instance.instance_root),
            str(action["record_id"]),
            str(action["action_id"]),
            (
                str(action["assignment_token"])
                if action.get("assignment_token")
                else None
            ),
        )
        for field in ("prompt", "text"):
            if isinstance(arguments.get(field), str):
                arguments[field] = marker + "\n" + arguments[field]
                break
        else:
            arguments["prompt"] = marker
        return {
            "action_id": action["action_id"],
            "record_id": action["record_id"],
            "executor": action["executor"],
            "tool": action["tool"],
            "arguments": arguments,
            "expected_result": copy.deepcopy(action.get("expected_result")),
            "reporting": copy.deepcopy(action.get("reporting") or {}),
            "state": action["state"],
        }

    def _find_action(
        self, ledger: Ledger, record_id: str, action_id: str
    ) -> tuple[LedgerRecord, dict[str, Any], dict[str, Any]]:
        record = self._record(ledger, record_id)
        protocol = _protocol(record.fc or {})
        actions = dict(protocol.get("actions") or {})
        action = actions.get(action_id)
        if not isinstance(action, Mapping):
            raise FulcrumError.invalid("ACTION_NOT_FOUND", "unknown native action")
        value = copy.deepcopy(dict(action))
        if value.get("state") not in ACTION_STATES:
            raise FulcrumError.invalid(
                "ACTION_CORRUPT", "native action has invalid state"
            )
        return record, protocol, value

    def _authorize_executor(
        self, ledger: Ledger, request: ParsedRequest, action: Mapping[str, Any]
    ) -> None:
        executor = str(action.get("executor"))
        if executor == "bootstrap":
            if request.actor.kind != "human":
                raise FulcrumError(
                    "AUTHORITY_MISMATCH",
                    "bootstrap action requires the authorized caller",
                    exit_code=5,
                )
            return
        self._standing_actor(ledger, executor, request)

    @coordinated
    def claim_action(self, request: ParsedRequest) -> CommandResult:
        ledger = self._ledger(request)
        record_id = str(
            request.arguments.get("record_id") or request.input.get("record_id") or ""
        )
        action_id = str(
            request.arguments.get("action_id") or request.input.get("action_id") or ""
        )
        attempt_id = str(request.input.get("attempt_id") or "")
        if not record_id or not action_id or not attempt_id:
            raise FulcrumError.invalid(
                "INVALID_CLAIM", "record_id, action_id, and attempt_id are required"
            )
        record, protocol, action = self._find_action(ledger, record_id, action_id)
        saved = _saved_request(protocol, request)
        if saved:
            return _replay(saved, request)
        self._authorize_executor(ledger, request, action)
        attempts = list(action.get("attempts") or [])
        if action["state"] == "issuing":
            current = attempts[-1] if attempts else {}
            if current.get("attempt_id") != attempt_id:
                raise FulcrumError(
                    "ACTION_ALREADY_CLAIMED",
                    "action already has an issuing attempt",
                    exit_code=5,
                )
            value = {
                "action": self._action_response(request, action),
                "attempt_id": attempt_id,
                "invoke": False,
            }
        elif action["state"] != "pending":
            raise FulcrumError(
                "ACTION_NOT_PENDING", f"action is {action['state']}", exit_code=5
            )
        else:
            attempt = {
                "attempt_id": attempt_id,
                "actor_task_id": request.actor.task_id,
                "native_tool_use_id": request.input.get("native_tool_use_id"),
                "state": "issuing",
                "claimed_at": _utc_now(),
            }
            attempts.append(attempt)
            action["attempts"] = attempts
            action["state"] = "issuing"
            action["claimed_by"] = request.actor.task_id or request.actor.kind
            action["claimed_at"] = _utc_now()
            actions = dict(protocol.get("actions") or {})
            actions[action_id] = action
            protocol["actions"] = actions
            value = {
                "action": self._action_response(request, action),
                "attempt_id": attempt_id,
                "invoke": True,
            }
        _save_request(protocol, request, value)
        ledger.update_fc(record.id, _with_protocol(record.fc or {}, protocol))
        self._write_event(
            request,
            "action_claimed",
            action_id=action_id,
            attempt_id=attempt_id,
            bead_id=None if record.id == "fc-system" else record.id,
            tool=action.get("tool"),
            outcome="issuing",
            invoke=value["invoke"],
        )
        return _result(request, value)

    @coordinated
    def report_action_result(self, request: ParsedRequest) -> CommandResult:
        ledger = self._ledger(request)
        record_id = str(
            request.arguments.get("record_id") or request.input.get("record_id") or ""
        )
        action_id = str(
            request.arguments.get("action_id") or request.input.get("action_id") or ""
        )
        attempt_id = str(request.input.get("attempt_id") or "")
        outcome = str(request.input.get("outcome") or "")
        if outcome not in {"succeeded", "rejected", "uncertain"}:
            raise FulcrumError.invalid(
                "INVALID_OUTCOME",
                "action result must be succeeded, rejected, or uncertain",
            )
        record, protocol, action = self._find_action(ledger, record_id, action_id)
        saved = _saved_request(protocol, request)
        if saved:
            return _replay(saved, request)
        self._authorize_executor(ledger, request, action)
        attempts = list(action.get("attempts") or [])
        if not attempts or attempts[-1].get("attempt_id") != attempt_id:
            raise FulcrumError(
                "ATTEMPT_MISMATCH",
                "result does not match the issuing attempt",
                exit_code=5,
            )
        if action["state"] != "issuing":
            raise FulcrumError(
                "ACTION_NOT_ISSUING", f"action is {action['state']}", exit_code=5
            )
        attempt = dict(attempts[-1])
        attempt.update(
            {
                "state": outcome,
                "outcome": outcome,
                "evidence": copy.deepcopy(request.input.get("evidence")),
                "native_result": copy.deepcopy(request.input.get("native_result")),
                "completed_at": _utc_now(),
            }
        )
        attempts[-1] = attempt
        action["attempts"] = attempts
        action["state"] = outcome
        action["completed_at"] = _utc_now()
        actions = dict(protocol.get("actions") or {})
        actions[action_id] = action
        protocol["actions"] = actions
        value = {
            "action_id": action_id,
            "attempt_id": attempt_id,
            "state": outcome,
            "reservation_retained": outcome == "uncertain",
        }
        _save_request(protocol, request, value)
        ledger.update_fc(record.id, _with_protocol(record.fc or {}, protocol))
        self._write_event(
            request,
            "action_result_recorded",
            action_id=action_id,
            attempt_id=attempt_id,
            bead_id=None if record.id == "fc-system" else record.id,
            tool=action.get("tool"),
            outcome=outcome,
        )
        return _result(request, value)

    def _recover_wait_grant(
        self, ledger: Ledger, wait_id: str
    ) -> tuple[LedgerRecord, Mapping[str, Any]] | None:
        for record in ledger.list_records(limit=0):
            protocol = _protocol(record.fc or {})
            actions = protocol.get("actions")
            if not isinstance(actions, Mapping):
                continue
            for value in actions.values():
                if (
                    isinstance(value, Mapping)
                    and value.get("granted_wait_id") == wait_id
                ):
                    return record, value
        return None

    def _eligible_actions(
        self, ledger: Ledger, *, paused: bool
    ) -> list[tuple[LedgerRecord, dict[str, Any]]]:
        candidates: list[tuple[LedgerRecord, dict[str, Any]]] = []
        for record in ledger.list_records(limit=0):
            protocol = _protocol(record.fc or {})
            actions = protocol.get("actions")
            if not isinstance(actions, Mapping):
                continue
            for value in actions.values():
                if not isinstance(value, Mapping):
                    continue
                action = dict(value)
                if (
                    action.get("executor") != "steward"
                    or action.get("state") != "pending"
                ):
                    continue
                if paused and action.get("purpose") not in {"diagnostic", "settlement"}:
                    continue
                candidates.append((record, action))
        candidates.sort(
            key=lambda item: (
                int((item[0].fc or {}).get("priority", 2)),
                item[0].id,
                str(item[1].get("created_at") or ""),
                str(item[1].get("action_id") or ""),
            )
        )
        return candidates

    @coordinated
    def wait_for_instructions(self, request: ParsedRequest) -> CommandResult:
        ledger = self._ledger(request)
        binding = self._standing_actor(ledger, "steward", request)
        system = self._system(ledger)
        protocol = _protocol(system.fc or {})
        saved = _saved_request(protocol, request)
        if saved:
            return _replay(saved, request)
        request_id = _request_id(request)
        waits = dict(protocol.get("instruction_waits") or {})
        outstanding = [
            value
            for value in waits.values()
            if isinstance(value, Mapping) and value.get("state") == "waiting"
        ]
        matching = next(
            (
                value
                for value in outstanding
                if value.get("request_id") == request_id
                and value.get("accepted_input") == _request_input(request)
            ),
            None,
        )
        if outstanding and matching is None:
            raise FulcrumError(
                "INSTRUCTION_WAIT_ACTIVE",
                "Steward already has a pending instruction request",
                exit_code=5,
            )
        if matching is not None:
            wait = copy.deepcopy(dict(matching))
            wait_id = str(wait["wait_id"])
        else:
            wait_id = _opaque("wait")
            idle_seconds = int(request.input.get("idle_seconds") or 3600)
            wait = {
                "wait_id": wait_id,
                "request_id": request_id,
                "accepted_input": _request_input(request),
                "task_id": binding["task_id"],
                "host_id": binding.get("host_id"),
                "turn_id": request.input.get("turn_id"),
                "loop_id": request.input.get("loop_id"),
                "state": "waiting",
                "registered_at": _utc_now(),
                "deadline": (self.now() + timedelta(seconds=idle_seconds))
                .isoformat()
                .replace("+00:00", "Z"),
            }
            waits[wait_id] = wait
            protocol["instruction_waits"] = waits
            ledger.update_fc(system.id, _with_protocol(system.fc or {}, protocol))

        recovered = self._recover_wait_grant(ledger, wait_id)
        candidates = self._eligible_actions(
            ledger, paused=protocol.get("run_control", "paused") == "paused"
        )
        selected = recovered or (candidates[0] if candidates else None)
        if selected is None:
            deadline = datetime.fromisoformat(
                str(wait["deadline"]).replace("Z", "+00:00")
            )
            if self.now() >= deadline:
                value = {
                    "kind": "stop",
                    "reason": "idle_deadline",
                    "wait_id": wait_id,
                    "retained_obligation": False,
                }
                wait["state"] = "expired"
                wait["resolved_at"] = _utc_now()
                wait["response"] = copy.deepcopy(value)
                waits[wait_id] = wait
                protocol["instruction_waits"] = waits
                _save_request(protocol, request, value)
                ledger.update_fc(system.id, _with_protocol(system.fc or {}, protocol))
                self._write_event(
                    request,
                    "instruction_wait_expired",
                    wait_id=wait_id,
                    task_id=binding["task_id"],
                    outcome="idle_deadline",
                )
                return _result(request, value)
            value = {"transport_wait": {"kind": "instruction", **wait}}
            self._write_event(
                request,
                "instruction_wait_registered",
                wait_id=wait_id,
                task_id=binding["task_id"],
                outcome="waiting",
            )
            return CommandResult(
                ok=True,
                state=CommandState.RUNNING,
                request_id=request.request_id,
                result=value,
            )

        record, selected_action = selected
        action = copy.deepcopy(dict(selected_action))
        if not action.get("granted_wait_id"):
            action["granted_wait_id"] = wait_id
            actions = dict(_protocol(record.fc or {}).get("actions") or {})
            actions[str(action["action_id"])] = action
            record_protocol = _protocol(record.fc or {})
            record_protocol["actions"] = actions
            ledger.update_fc(
                record.id, _with_protocol(record.fc or {}, record_protocol)
            )
        value = {
            "kind": "action",
            "action": self._action_response(request, action),
            "wait_id": wait_id,
        }
        wait["state"] = "resolved"
        wait["resolved_at"] = _utc_now()
        wait["response"] = copy.deepcopy(value)
        waits[wait_id] = wait
        protocol["instruction_waits"] = waits
        _save_request(protocol, request, value)
        ledger.update_fc(system.id, _with_protocol(system.fc or {}, protocol))
        self._write_event(
            request,
            "instruction_resolved",
            wait_id=wait_id,
            action_id=action["action_id"],
            bead_id=None if record.id == "fc-system" else record.id,
            outcome="resolved",
        )
        return _result(request, value)

    @coordinated
    def pause(self, request: ParsedRequest) -> CommandResult:
        return self._run_control(request, "paused")

    @coordinated
    def resume(self, request: ParsedRequest) -> CommandResult:
        return self._run_control(request, "running")

    def _run_control(self, request: ParsedRequest, state: str) -> CommandResult:
        if request.actor.kind != "human":
            # Vizier is accepted only through its registered task identity.
            ledger = self._ledger(request)
            self._standing_actor(ledger, "vizier", request)
        else:
            ledger = self._ledger(request)
        system = self._system(ledger)
        protocol = _protocol(system.fc or {})
        saved = _saved_request(protocol, request)
        if saved:
            return _replay(saved, request)
        protocol["run_control"] = state
        protocol["run_control_reason"] = request.input.get("reason")
        protocol["run_control_changed_at"] = _utc_now()
        in_flight: list[dict[str, Any]] = []
        for record in ledger.list_records(limit=0):
            actions = _protocol(record.fc or {}).get("actions")
            if not isinstance(actions, Mapping):
                continue
            for action in actions.values():
                if isinstance(action, Mapping) and action.get("state") in {
                    "issuing",
                    "uncertain",
                }:
                    in_flight.append(
                        {
                            "record_id": record.id,
                            "action_id": action.get("action_id"),
                            "state": action.get("state"),
                        }
                    )
        value = {"run_control": state, "in_flight": in_flight}
        _save_request(protocol, request, value)
        ledger.update_fc(system.id, _with_protocol(system.fc or {}, protocol))
        self._write_event(
            request, "run_control_changed", outcome=state, in_flight=in_flight
        )
        return _result(request, value)

    @coordinated
    def register_worker(self, request: ParsedRequest) -> CommandResult:
        ledger = self._ledger(request)
        bead_id = str(request.arguments.get("bead") or request.input.get("bead") or "")
        record = self._record(ledger, bead_id)
        protocol = _protocol(record.fc or {})
        saved = _saved_request(protocol, request)
        if saved:
            return _replay(saved, request)
        assignment = protocol.get("assignment")
        if not isinstance(assignment, Mapping):
            raise FulcrumError(
                "ASSIGNMENT_REQUIRED", "work has no reserved assignment", exit_code=5
            )
        supplied = str(request.input.get("assignment_token") or "")
        if supplied != assignment.get("assignment_token"):
            raise FulcrumError(
                "ASSIGNMENT_MISMATCH", "assignment token does not match", exit_code=5
            )
        task_id = request.actor.task_id or request.thread_id
        if assignment.get("task_id") and assignment.get("task_id") != task_id:
            raise FulcrumError(
                "ASSIGNMENT_CONFLICT",
                "assignment is already bound to another task",
                exit_code=5,
            )
        observed = request.input.get("workspace")
        if observed != assignment.get("workspace"):
            raise FulcrumError(
                "WORKSPACE_MISMATCH",
                "worker is not in the assigned workspace",
                exit_code=5,
            )
        active = {
            **dict(assignment),
            "task_id": task_id,
            "host_id": request.input.get("host_id"),
            "turn_id": request.input.get("turn_id"),
            "state": "active",
            "registered_at": _utc_now(),
        }
        protocol["assignment"] = active
        value = {"assignment": active}
        _save_request(protocol, request, value)
        ledger.update_fc(
            record.id, _with_protocol(record.fc or {}, protocol), assignee=str(task_id)
        )
        self._write_event(
            request,
            "worker_registered",
            bead_id=bead_id,
            task_id=task_id,
            turn_id=active.get("turn_id"),
            outcome="active",
        )
        return _result(request, value)

    @coordinated
    def submit_candidate(self, request: ParsedRequest) -> CommandResult:
        ledger = self._ledger(request)
        bead_id = str(request.arguments.get("bead") or request.input.get("bead") or "")
        record = self._record(ledger, bead_id)
        protocol = _protocol(record.fc or {})
        saved = _saved_request(protocol, request)
        if saved:
            return _replay(saved, request)
        assignment = protocol.get("assignment")
        if (
            not isinstance(assignment, Mapping)
            or assignment.get("role") != "warden"
            or assignment.get("state") != "active"
        ):
            raise FulcrumError(
                "WARDEN_REQUIRED",
                "candidate submission requires the active Warden",
                exit_code=5,
            )
        if request.actor.task_id != assignment.get("task_id"):
            raise FulcrumError(
                "ASSIGNMENT_MISMATCH",
                "candidate does not belong to this task",
                exit_code=5,
            )
        candidate_id = str(request.input.get("candidate_id") or _opaque("candidate"))
        candidate = {
            "candidate_id": candidate_id,
            "source": request.input.get("source"),
            "provider_run_id": request.input.get("provider_run_id"),
            "state": str(request.input.get("state") or "submitted"),
            "submitted_at": _utc_now(),
            "deadline": (
                self.now()
                + timedelta(seconds=int(request.input.get("deadline_seconds") or 1800))
            )
            .isoformat()
            .replace("+00:00", "Z"),
            "evidence": copy.deepcopy(request.input.get("evidence") or {}),
        }
        protocol["candidate"] = candidate
        value = {"candidate": candidate}
        _save_request(protocol, request, value)
        ledger.update_fc(record.id, _with_protocol(record.fc or {}, protocol))
        self._write_event(
            request,
            "candidate_submitted",
            bead_id=bead_id,
            candidate_id=candidate_id,
            task_id=request.actor.task_id,
            outcome=candidate["state"],
        )
        return _result(request, value)

    @coordinated
    def wait_for_ci_results(self, request: ParsedRequest) -> CommandResult:
        ledger = self._ledger(request)
        bead_id = str(request.arguments.get("bead") or request.input.get("bead") or "")
        record = self._record(ledger, bead_id)
        protocol = _protocol(record.fc or {})
        saved = _saved_request(protocol, request)
        if saved:
            return _replay(saved, request)
        assignment = protocol.get("assignment")
        candidate = protocol.get("candidate")
        if not isinstance(
            assignment, Mapping
        ) or request.actor.task_id != assignment.get("task_id"):
            raise FulcrumError(
                "ASSIGNMENT_MISMATCH",
                "CI wait requires the active assignment",
                exit_code=5,
            )
        if not isinstance(candidate, Mapping) or candidate.get(
            "candidate_id"
        ) != request.input.get("candidate_id"):
            raise FulcrumError(
                "CANDIDATE_MISMATCH", "CI wait candidate does not match", exit_code=5
            )
        state = str(candidate.get("state"))
        if state in {"passed", "failed", "blocked"}:
            value = {"status": state, "candidate": copy.deepcopy(dict(candidate))}
            _save_request(protocol, request, value)
            ledger.update_fc(record.id, _with_protocol(record.fc or {}, protocol))
            return _result(request, value)
        waits = dict(protocol.get("ci_waits") or {})
        matching = next(
            (
                item
                for item in waits.values()
                if isinstance(item, Mapping)
                and item.get("state") == "waiting"
                and item.get("request_id") == _request_id(request)
                and item.get("accepted_input") == _request_input(request)
            ),
            None,
        )
        if (
            any(
                isinstance(item, Mapping) and item.get("state") == "waiting"
                for item in waits.values()
            )
            and matching is None
        ):
            raise FulcrumError(
                "CI_WAIT_ACTIVE", "candidate already has a pending CI wait", exit_code=5
            )
        if matching is not None:
            deadline = datetime.fromisoformat(
                str(candidate["deadline"]).replace("Z", "+00:00")
            )
            if self.now() >= deadline:
                value = {
                    "status": "blocked",
                    "reason": "ci_deadline",
                    "candidate": copy.deepcopy(dict(candidate)),
                }
                wait = copy.deepcopy(dict(matching))
                wait["state"] = "expired"
                wait["resolved_at"] = _utc_now()
                waits[str(wait["wait_id"])] = wait
                protocol["ci_waits"] = waits
                _save_request(protocol, request, value)
                ledger.update_fc(record.id, _with_protocol(record.fc or {}, protocol))
                return _result(request, value)
            return CommandResult(
                ok=True,
                state=CommandState.RUNNING,
                request_id=request.request_id,
                result={
                    "transport_wait": {
                        "kind": "ci",
                        "bead": bead_id,
                        **dict(matching),
                    }
                },
            )
        wait_id = _opaque("wait")
        wait = {
            "wait_id": wait_id,
            "request_id": _request_id(request),
            "accepted_input": _request_input(request),
            "candidate_id": candidate["candidate_id"],
            "task_id": assignment["task_id"],
            "turn_id": request.input.get("turn_id"),
            "deadline": candidate["deadline"],
            "state": "waiting",
            "registered_at": _utc_now(),
        }
        waits[wait_id] = wait
        protocol["ci_waits"] = waits
        ledger.update_fc(record.id, _with_protocol(record.fc or {}, protocol))
        self._write_event(
            request,
            "ci_wait_registered",
            bead_id=bead_id,
            wait_id=wait_id,
            candidate_id=candidate["candidate_id"],
            task_id=assignment["task_id"],
            outcome="waiting",
        )
        return CommandResult(
            ok=True,
            state=CommandState.RUNNING,
            request_id=request.request_id,
            result={"transport_wait": {"kind": "ci", "bead": bead_id, **wait}},
        )
