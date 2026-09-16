"""Fulcrum2 installation asset and local service operations."""

from __future__ import annotations

import os
import asyncio
import json
import plistlib
import socket
import subprocess
import time
import uuid
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fulcrum.configuration import ConfigurationManager
from fulcrum.coordination import maintenance_operation
from fulcrum.contracts import CommandResult, CommandState, FulcrumError, ParsedRequest
from fulcrum.install import (
    InstalledService,
    InstallationError,
    install_fulcrum2_service_definitions,
    inspect_service,
    reconcile_fulcrum2_skills,
)
from fulcrum.ipc import ControllerTimedOut, ControllerUnavailable, request_sync
from fulcrum.ledger import Ledger, LedgerFailure, OperationRecord, operation_view
from fulcrum.runtime_service import _runtime_call
from fulcrum.transport import AppServerError


def load_installed_services(instance_root: Path) -> dict[str, InstalledService]:
    result: dict[str, InstalledService] = {}
    root = instance_root / "services"
    if not root.is_dir():
        return result
    for path in sorted(root.glob("*.plist")):
        try:
            with path.open("rb") as stream:
                definition = plistlib.load(stream)
        except (OSError, plistlib.InvalidFileException):
            continue
        label = definition.get("Label") if isinstance(definition, Mapping) else None
        if isinstance(label, str) and label:
            result[path.stem] = InstalledService(path.stem, label, path)
    return result


def service_status_result(request: ParsedRequest) -> dict[str, Any]:
    installed = load_installed_services(request.instance.instance_root)
    rows: dict[str, Any] = {}
    for name, service in installed.items():
        observation = inspect_service(service.label)
        rows[name] = {
            "label": service.label,
            "definition": str(service.definition),
            "definition_valid": True,
            "loaded": observation.loaded,
            "running": observation.running,
            "state": observation.state,
            "pid": observation.pid,
            "arguments": list(observation.program_arguments),
            "detail": observation.detail,
        }
    service_root = request.instance.instance_root / "services"
    if service_root.is_dir():
        for path in sorted(service_root.glob("*.plist")):
            if path.stem not in rows:
                rows[path.stem] = {
                    "label": None,
                    "definition": str(path),
                    "definition_valid": False,
                    "loaded": None,
                    "running": None,
                    "state": "invalid",
                    "pid": None,
                    "arguments": [],
                    "detail": "the owned service definition is unreadable",
                }
    health_path = request.instance.instance_root / "service-health.json"
    health: Any = None
    try:
        import json

        loaded = json.loads(health_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            health = loaded
    except (OSError, ValueError):
        pass
    from fulcrum.bootstrap import selection
    from fulcrum.resident_client import exchange

    try:
        resident = asyncio.run(
            exchange(
                request.instance.instance_root / "resident.sock",
                {"action": "health", "timeout": 1},
            )
        )
    except Exception as error:
        resident = {"available": False, "reason": str(error)}
    try:
        activation = json.loads(
            (request.instance.instance_root / "activation.json").read_text()
        )
    except (OSError, ValueError):
        activation = None
    active = []
    for path in (request.instance.instance_root / "active-operations").glob("*.json"):
        try:
            row = json.loads(path.read_text())
            os.kill(row["pid"], 0)
            active.append(row)
        except (OSError, ValueError):
            pass
    return {
        "selected": selection(request.instance.instance_root),
        "activation": activation,
        "resident": resident,
        "active_operations": active,
        "observed_at": _now(),
        "instance": str(request.instance.instance_root),
        "config": {
            "path": str(request.instance.config_path),
            "exists": request.instance.config_path.is_file(),
        },
        "socket": {
            "path": str(request.instance.socket_path),
            "exists": request.instance.socket_path.exists(),
        },
        "services": rows,
        "health": health,
        "responsive": None,
        "gaps": ([] if installed else ["no installed service definitions were found"])
        + ["controller responsiveness was not inferred from its socket artifact"],
    }


class SkillsService:
    def reconcile(self, request: ParsedRequest) -> CommandResult:
        config = _effective(request)
        ledger = _ledger(request, config)
        operation, reused = ledger.create_operation(
            request,
            planned={
                "production": not request.instance.explicit_selection,
                "instance": str(request.instance.instance_root),
            },
            next_action="Inspect and repair only Fulcrum-owned skill links.",
        )
        if reused and operation.operation.get("state") in {
            "completed",
            "failed",
            "cancelled",
            "uncertain",
        }:
            return _operation_result(operation)
        try:
            result = reconcile_fulcrum2_skills(
                request.instance.instance_root,
                production=not request.instance.explicit_selection,
            )
        except InstallationError as error:
            operation = ledger.update_operation(
                operation,
                state="failed",
                step="skill_reconciliation_refused",
                error={
                    "code": "ASSET_CONFLICT",
                    "message": str(error),
                    "retryable": False,
                },
                next_action="Move or rename the conflicting user directory explicitly.",
            )
            return _operation_result(operation)
        operation = ledger.update_operation(
            operation,
            state="completed",
            step="skill_links_verified",
            result=result,
            next_action="No further action is required.",
        )
        return _operation_result(operation)


class ServiceService:
    def status(self, request: ParsedRequest) -> CommandResult:
        return CommandResult.query(service_status_result(request))

    def start(self, request: ParsedRequest) -> CommandResult:
        from fulcrum.reset import refuse_unfinished_reset

        refuse_unfinished_reset(request.instance.instance_root)
        services = load_installed_services(request.instance.instance_root)
        if "controller" not in services or "dolt" not in services:
            raise FulcrumError(
                "SERVICE_NOT_INSTALLED",
                "controller and Dolt service definitions are required; run fulcrum setup",
                exit_code=4,
            )
        config = _effective(request)
        ledger = _ledger(request, config)
        operation, reused = ledger.create_operation(
            request,
            planned={"services": sorted(services), "reconcile_before_admission": True},
            next_action="Start prerequisites, reconcile retained state, then start the controller.",
        )
        if reused and operation.operation.get("state") in {
            "completed",
            "failed",
            "cancelled",
            "uncertain",
        }:
            return _operation_result(operation)
        results: list[dict[str, Any]] = []
        for name in ("runtime", "dolt"):
            service = services.get(name)
            if service is None:
                continue
            endpoint = (
                str(config["runtime"]["endpoint"])
                if name == "runtime"
                else f"tcp://{config['beads']['host']}:{config['beads']['port']}"
            )
            results.append(_start_one(service, endpoint=endpoint))
        # A bounded foreground reconciliation observes retained identities before
        # the long-running controller is admitted. Failure is visible and leaves
        # the service stopped rather than silently skipping reconciliation.
        controller = services["controller"]
        arguments = _program_arguments(controller.definition)
        controller_observation = inspect_service(controller.label)
        controller_was_running = controller_observation.running
        controller_was_current = controller_was_running and (
            controller_observation.program_arguments == tuple(arguments)
        )
        if controller_was_running and not controller_was_current:
            _set_service_pause(
                request,
                config,
                {
                    "state": "draining",
                    "requested_at": _now(),
                    "operation": operation.id,
                },
            )
            _stop_one(controller)
            controller_was_running = False
        if not controller_was_running:
            from fulcrum.activation import activate
            from fulcrum.bootstrap import selection

            if selection(request.instance.instance_root) is None:
                activate(
                    request.instance.instance_root,
                    request.instance.config_path,
                    dict(config["source"]),
                )
        # Reconciliation ran with the retained pause. Clear it immediately
        # before launch so the new controller observes admission enabled.
        _set_service_pause(request, config, None)
        controller_start = _start_one(controller, endpoint=None)
        results.append(controller_start)
        try:
            readiness = _wait_for_controller(
                request,
                request.instance.instance_root / "resident.sock",
                controller,
                timeout=max(request.timeout, 1.0),
            )
        except FulcrumError as error:
            if controller_start.get("state") == "started":
                try:
                    _stop_one(controller)
                except Exception:
                    pass
            operation = ledger.update_operation(
                operation,
                state="failed",
                step="controller_readiness_failed",
                error={
                    "code": error.code,
                    "message": str(error),
                    "retryable": error.retryable,
                },
                result={"services": results},
                next_action="Inspect service status and controller logs before retrying start.",
            )
            return _operation_result(operation)
        operation = ledger.update_operation(
            operation,
            state="completed",
            step="controller_started",
            result={
                "services": results,
                "reconciled_before_admission": not controller_was_running,
                "already_running": controller_was_running,
                "identities_retained": True,
                "controller_readiness": readiness,
            },
            next_action="No further action is required.",
        )
        return _operation_result(operation)

    @maintenance_operation
    def stop(self, request: ParsedRequest) -> CommandResult:
        services = load_installed_services(request.instance.instance_root)
        controller = services.get("controller")
        if controller is None:
            raise FulcrumError(
                "SERVICE_NOT_INSTALLED",
                "controller service is not installed",
                exit_code=4,
            )
        config = _effective(request)
        interrupt = bool(request.arguments.get("interrupt", False))
        ledger = _ledger(request, config)
        operation, reused = ledger.create_operation(
            request,
            planned={"controller": controller.label, "interrupt": interrupt},
            next_action="Pause admission and observe the requested shutdown mode.",
        )
        if reused and operation.operation.get("state") in {
            "completed",
            "failed",
            "cancelled",
            "uncertain",
        }:
            return _operation_result(operation)
        _set_service_pause(
            request,
            config,
            {
                "state": "interrupting" if interrupt else "draining",
                "requested_at": _now(),
                "operation": request.request_id,
            },
        )
        interrupted: list[dict[str, str]] = []
        if interrupt:
            tasks = ledger.list_records(kind="task", limit=0)
            for task in tasks:
                fc = task.fc or {}
                thread_id = fc.get("thread_id")
                if not isinstance(thread_id, str):
                    continue
                try:
                    facts = _runtime_call(
                        request,
                        lambda runtime, value=thread_id: runtime.inspect_task(value),
                    )
                    if facts.active_turn:
                        _runtime_call(
                            request,
                            lambda runtime, value=thread_id, turn=facts.active_turn: runtime.interrupt(
                                value, turn
                            ),
                        )
                        interrupted.append(
                            {"thread_id": thread_id, "turn_id": facts.active_turn}
                        )
                except FulcrumError:
                    raise
        if (
            not interrupt
            and (request.instance.instance_root / "resident.sock").exists()
        ):
            from fulcrum.resident_client import exchange

            facts = asyncio.run(
                exchange(
                    request.instance.instance_root / "resident.sock",
                    {"action": "health"},
                )
            )
            if facts["pending"]:
                raise FulcrumError(
                    "SERVICE_BUSY",
                    "native requests must settle before resident handoff",
                    exit_code=3,
                    retryable=True,
                )
            for task in ledger.list_records(kind="task", limit=0):
                thread_id = (task.fc or {}).get("thread_id")
                if isinstance(thread_id, str):
                    observed = _runtime_call(
                        request,
                        lambda runtime, value=thread_id: runtime.inspect_task(value),
                    )
                    if observed.active_turn or observed.pending_requests:
                        raise FulcrumError(
                            "SERVICE_BUSY",
                            "active turns must settle before resident handoff",
                            exit_code=3,
                            retryable=True,
                        )
        stopped = _stop_one(controller)
        operation = ledger.update_operation(
            operation,
            state="completed",
            step="controller_stopped",
            result={
                "controller": stopped,
                "admission_paused": True,
                "mode": "interrupt" if interrupt else "drain",
                "interrupted": interrupted,
                "shared_runtime_stopped": False,
                "dolt_stopped": False,
            },
            next_action="Run service start to reconcile and resume admission.",
        )
        return _operation_result(operation)

    def restart(self, request: ParsedRequest) -> CommandResult:
        config = _effective(request)
        ledger = _ledger(request, config)
        operation, reused = ledger.create_operation(
            request,
            planned={"interrupt": bool(request.arguments.get("interrupt", False))},
            next_action="Stop and restart the exact installed controller.",
        )
        if reused and operation.operation.get("state") in {
            "completed",
            "failed",
            "cancelled",
            "uncertain",
        }:
            return _operation_result(operation)
        assert request.request_id is not None
        namespace = uuid.UUID(request.request_id)
        stop_request = replace(
            request,
            command=("service", "stop"),
            request_id=str(uuid.uuid5(namespace, "service-stop")),
        )
        start_request = replace(
            request,
            command=("service", "start"),
            arguments={},
            request_id=str(uuid.uuid5(namespace, "service-start")),
        )
        stopped = self.stop(stop_request)
        stop_failure = _service_child_failure(stopped, "stop")
        if stop_failure is not None:
            operation = ledger.update_operation(
                operation,
                state="failed",
                step="controller_restart_stop_failed",
                error=stop_failure,
                result={"stop_operation": stopped.operation_id},
                next_action="Inspect the retained stop operation before retrying restart.",
            )
            return _operation_result(operation)
        started = self.start(start_request)
        start_failure = _service_child_failure(started, "start")
        if start_failure is not None:
            operation = ledger.update_operation(
                operation,
                state="failed",
                step="controller_restart_start_failed",
                error=start_failure,
                result={
                    "stop_operation": stopped.operation_id,
                    "start_operation": started.operation_id,
                },
                next_action="Inspect controller service status and the retained start operation before retrying restart.",
            )
            return _operation_result(operation)
        operation = ledger.update_operation(
            operation,
            state="completed",
            step="controller_restarted",
            result={
                "stop_operation": stopped.operation_id,
                "start_operation": started.operation_id,
                "identities_retained": True,
            },
            next_action="No further action is required.",
        )
        return _operation_result(operation)


def _effective(request: ParsedRequest) -> dict[str, Any]:
    manager = ConfigurationManager(request.instance.config_path)
    document, _ = manager.load()
    return manager.effective(document)


def _ledger(request: ParsedRequest, config: Mapping[str, Any]) -> Ledger:
    if request.instance.brain_root is None:
        raise FulcrumError(
            "LEDGER_UNAVAILABLE", "configuration has no brain root", exit_code=4
        )
    beads = config["beads"]
    executable = beads.get("executable") if isinstance(beads, Mapping) else None
    try:
        return Ledger(
            request.instance.brain_root,
            executable=str(executable) if executable else None,
            timeout=request.timeout,
        )
    except LedgerFailure as error:
        raise FulcrumError(
            "LEDGER_UNAVAILABLE", str(error), exit_code=4, retryable=error.retryable
        ) from error


def _set_service_pause(
    request: ParsedRequest,
    config: Mapping[str, Any],
    pause: Mapping[str, Any] | None,
) -> None:
    try:
        ledger = _ledger(request, config)
        control = ledger.show("fc-system")
        if control is None or not control.fc:
            return
        fc = dict(control.fc)
        fc["service_pause"] = dict(pause) if pause is not None else None
        ledger.update_fc(control.id, fc, assignee=control.assignee)
    except (FulcrumError, LedgerFailure):
        # Status/service control remains available when Beads is unhealthy. The
        # inability to retain a pause is reflected by controller absence.
        return


def service_admission_paused(ledger: Ledger) -> Mapping[str, Any] | None:
    control = ledger.show("fc-system")
    value = control.fc.get("service_pause") if control and control.fc else None
    return value if isinstance(value, Mapping) else None


def _program_arguments(path: Path) -> list[str]:
    try:
        with path.open("rb") as stream:
            value = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException):
        return []
    arguments = value.get("ProgramArguments") if isinstance(value, Mapping) else None
    return [str(item) for item in arguments] if isinstance(arguments, list) else []


def _port(endpoint: str) -> tuple[str, int] | None:
    parsed = urlparse(endpoint)
    try:
        port = parsed.port
    except ValueError:
        return None
    if port is None:
        return None
    return parsed.hostname or "127.0.0.1", port


def _occupied(endpoint: str) -> bool:
    target = _port(endpoint)
    if target is None:
        return False
    try:
        with socket.create_connection(target, timeout=0.25):
            return True
    except OSError:
        return False


def _wait_for_controller(
    request: ParsedRequest,
    socket_path: Path,
    service: InstalledService,
    *,
    timeout: float,
    stability_seconds: float = 1.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_observation = inspect_service(service.label)
    last_error: str | None = None
    while time.monotonic() < deadline:
        last_observation = inspect_service(service.label)
        remaining = deadline - time.monotonic()
        probe = replace(
            request,
            command=("service", "status"),
            arguments={},
            input={},
            request_id=None,
            timeout=min(5.0, max(0.1, remaining)),
        )
        try:
            from fulcrum.resident_client import exchange

            response = asyncio.run(
                exchange(socket_path, {"action": "health", "timeout": probe.timeout})
            )
            observed_pid = last_observation.pid
            stable_until = time.monotonic() + stability_seconds
            stable = True
            while time.monotonic() < stable_until:
                current = inspect_service(service.label)
                if (
                    not current.running
                    or observed_pid is None
                    or current.pid != observed_pid
                ):
                    last_observation = current
                    last_error = "controller process changed after readiness response"
                    stable = False
                    break
                time.sleep(0.1)
            if not stable:
                continue
            return {
                "socket": str(socket_path),
                "responsive": True,
                "pid": observed_pid,
                "probe_state": response.get("state"),
                "stable_seconds": stability_seconds,
            }
        except (ControllerUnavailable, ControllerTimedOut, AppServerError) as error:
            last_error = str(error)
        time.sleep(0.1)
    raise FulcrumError(
        "CONTROLLER_UNAVAILABLE",
        f"controller did not accept connections at {socket_path}: {last_error}",
        exit_code=4,
        retryable=True,
        details={
            "service": service.label,
            "state": last_observation.state,
            "pid": last_observation.pid,
            "socket": str(socket_path),
        },
    )


def _service_child_failure(result: CommandResult, action: str) -> dict[str, Any] | None:
    if result.state == CommandState.COMPLETED and result.ok:
        return None
    retained_error = (
        result.result.get("error") if isinstance(result.result, Mapping) else None
    )
    if result.error is not None:
        retained_error = result.error.to_dict()
    if isinstance(retained_error, Mapping):
        return {
            "code": str(retained_error.get("code") or "SERVICE_RESTART_FAILED"),
            "message": str(
                retained_error.get("message")
                or f"controller {action} operation did not complete"
            ),
            "retryable": bool(retained_error.get("retryable", True)),
        }
    return {
        "code": "SERVICE_RESTART_FAILED",
        "message": f"controller {action} operation ended in {result.state.value}",
        "retryable": True,
    }


def _start_one(service: InstalledService, *, endpoint: str | None) -> dict[str, Any]:
    observation = inspect_service(service.label)
    expected_arguments = tuple(_program_arguments(service.definition))
    if observation.running and observation.program_arguments != expected_arguments:
        _stop_one(service)
        observation = inspect_service(service.label)
    if observation.running:
        return {
            "name": service.name,
            "label": service.label,
            "state": "reused",
            "pid": observation.pid,
        }
    if endpoint is not None and _occupied(endpoint):
        raise FulcrumError(
            "PORT_CONFLICT",
            f"{endpoint} is occupied by a process not owned by {service.label}",
            exit_code=4,
            details={"endpoint": endpoint, "service": service.label},
        )
    domain = f"gui/{os.getuid()}"
    action = (
        ["launchctl", "kickstart", "-k", f"{domain}/{service.label}"]
        if observation.loaded
        else ["launchctl", "bootstrap", domain, str(service.definition)]
    )
    completed = subprocess.run(
        action,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise FulcrumError(
            "SERVICE_START_FAILED",
            completed.stderr.strip() or completed.stdout.strip() or service.label,
            exit_code=4,
            retryable=True,
        )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        observation = inspect_service(service.label)
        if observation.running:
            return {
                "name": service.name,
                "label": service.label,
                "state": "started",
                "pid": observation.pid,
            }
        time.sleep(0.1)
    raise FulcrumError(
        "SERVICE_START_FAILED",
        f"{service.label} did not become stably running",
        exit_code=4,
        retryable=True,
    )


def _stop_one(service: InstalledService) -> dict[str, Any]:
    observation = inspect_service(service.label)
    if not observation.loaded:
        return {"label": service.label, "state": "already_stopped", "pid": None}
    domain = f"gui/{os.getuid()}"
    completed = subprocess.run(
        ["launchctl", "bootout", f"{domain}/{service.label}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise FulcrumError(
            "SERVICE_STOP_FAILED",
            completed.stderr.strip() or completed.stdout.strip() or service.label,
            exit_code=4,
            retryable=True,
        )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not inspect_service(service.label).loaded:
            return {"label": service.label, "state": "stopped", "pid": None}
        time.sleep(0.1)
    raise FulcrumError(
        "SERVICE_STOP_FAILED",
        f"{service.label} remained loaded",
        exit_code=4,
        retryable=True,
    )


def _operation_result(operation: OperationRecord) -> CommandResult:
    view = operation_view(operation)
    value = str(operation.operation.get("state", "running"))
    state = (
        CommandState(value)
        if value in CommandState._value2member_map_
        else CommandState.RUNNING
    )
    return CommandResult(
        ok=operation.operation.get("state") not in {"failed", "cancelled"},
        state=state,
        operation_id=operation.id,
        request_id=str(operation.operation.get("request_id")),
        result=view,
    )


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
