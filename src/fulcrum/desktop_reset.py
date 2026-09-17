"""Destructive, fenced cutover to an empty Fulcrum Desktop ledger."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fulcrum.configuration import ConfigurationManager
from fulcrum.contracts import CommandResult, CommandState, FulcrumError, ParsedRequest
from fulcrum.desktop_protocol import _protocol
from fulcrum.desktop_services import _services, _start, _stop
from fulcrum.install import inspect_service
from fulcrum.instance import WriterLock
from fulcrum.ledger import Ledger, LedgerFailure


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _load(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as error:
        raise FulcrumError(
            "RESET_FENCE_INVALID",
            f"cannot read the retained maintenance fence: {error}",
            exit_code=4,
        ) from error
    if not isinstance(value, dict):
        raise FulcrumError(
            "RESET_FENCE_INVALID", "maintenance fence must be an object", exit_code=4
        )
    return value


def _inventory(request: ParsedRequest, ledger: Ledger) -> dict[str, Any]:
    instance = request.instance.instance_root
    brain = request.instance.brain_root
    assert brain is not None
    services = _services(instance)
    service_rows = {
        name: {
            "label": service.label,
            "definition": str(service.definition),
            "running": inspect_service(service.label).running,
        }
        for name, service in services.items()
    }
    standing: dict[str, Any] = {}
    schedule: dict[str, Any] = {}
    blockers: list[dict[str, Any]] = []
    try:
        records = ledger.list_records(limit=0)
    except LedgerFailure as error:
        raise FulcrumError(
            "RESET_INVENTORY_UNAVAILABLE",
            f"cannot inventory the old ledger: {error}",
            exit_code=4,
            retryable=True,
        ) from error
    for record in records:
        fc = record.fc or {}
        desktop = _protocol(fc)
        if record.id == "fc-system":
            value = desktop.get("standing")
            standing = dict(value) if isinstance(value, Mapping) else {}
            value = desktop.get("marshal_schedule")
            schedule = dict(value) if isinstance(value, Mapping) else {}
            if desktop.get("run_control") != "paused":
                blockers.append({"record_id": record.id, "kind": "admission"})
        assignment = desktop.get("assignment")
        if isinstance(assignment, Mapping) and assignment.get("state") in {
            "reserved",
            "issuing",
            "active",
            "uncertain",
        }:
            blockers.append(
                {
                    "record_id": record.id,
                    "kind": "assignment",
                    "state": assignment.get("state"),
                }
            )
        for action in (desktop.get("actions") or {}).values():
            if isinstance(action, Mapping) and action.get("state") in {
                "issuing",
                "uncertain",
            }:
                blockers.append(
                    {
                        "record_id": record.id,
                        "kind": "native_action",
                        "action_id": action.get("action_id"),
                        "state": action.get("state"),
                    }
                )
        if record.kind == "operation" and record.status != "closed":
            operation = fc.get("operation")
            state = operation.get("state") if isinstance(operation, Mapping) else None
            if state not in {"completed", "failed", "cancelled"}:
                blockers.append(
                    {"record_id": record.id, "kind": "operation", "state": state}
                )
    owned_paths = [
        brain / ".beads",
        instance / "selected.json",
        instance / "activation.json",
        instance / "sources",
        instance / "environments",
        instance / "controller.sock",
        instance / "resident.sock",
    ]
    legacy_services = [
        instance / "services" / f"{name}.plist"
        for name in ("controller", "runtime", "resident")
    ]
    return {
        "brain": str(brain),
        "owned_paths": [
            str(path)
            for path in [*owned_paths, *legacy_services]
            if path.exists() or path.is_symlink()
        ],
        "services": service_rows,
        "blockers": blockers,
        "native_retention_exceptions": {
            "standing_tasks": {
                role: {
                    "task_id": value.get("task_id"),
                    "host_id": value.get("host_id"),
                }
                for role, value in standing.items()
                if isinstance(value, Mapping)
            },
            "marshal_schedule": (
                {
                    "automation_id": schedule.get("automation_id"),
                    "reason": "stock task tools expose no destructive task or automation deletion",
                }
                if schedule
                else None
            ),
        },
    }


def _delete_owned(inventory: Mapping[str, Any], *, brain: Path) -> list[str]:
    removed: list[str] = []
    allowed = {brain / ".beads"}
    for raw in inventory.get("owned_paths") or []:
        path = Path(str(raw)).absolute()
        if path == brain.absolute() or path == brain.parent.absolute():
            raise FulcrumError(
                "RESET_SCOPE_INVALID",
                f"refusing broad reset target {path}",
                exit_code=5,
            )
        allowed.add(path)
    for path in sorted(allowed, key=lambda value: len(value.parts), reverse=True):
        if path.is_symlink() or path.is_file() or path.is_socket():
            path.unlink(missing_ok=True)
            removed.append(str(path))
        elif path.is_dir():
            shutil.rmtree(path)
            removed.append(str(path))
    return removed


def _initialize_beads(
    request: ParsedRequest, config: Mapping[str, Any], dolt_service: Any
) -> None:
    brain = request.instance.brain_root
    assert brain is not None
    beads = config["beads"]
    (brain / ".beads" / "dolt").mkdir(parents=True, exist_ok=True, mode=0o700)
    _start(dolt_service)
    command = [
        str(beads["executable"]),
        "-C",
        str(brain),
        "init",
        "--server",
        "--external",
        "--server-host",
        str(beads["host"]),
        "--server-port",
        str(beads["port"]),
        "--database",
        str(beads["database"]),
        "--prefix",
        "fc",
        "--non-interactive",
        "--skip-agents",
        "--skip-hooks",
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=max(30.0, request.timeout),
    )
    if completed.returncode:
        raise FulcrumError(
            "BEADS_INIT_FAILED",
            completed.stderr.strip() or completed.stdout.strip() or "Beads init failed",
            exit_code=4,
            retryable=True,
        )


class ResetService:
    """Reset old workflow state without pretending native resources were deleted."""

    def hard_reset(self, request: ParsedRequest) -> CommandResult:
        if request.actor.kind != "human":
            raise FulcrumError(
                "RESET_AUTHORITY_DENIED",
                "hard reset requires explicit human authority",
                exit_code=5,
            )
        if not request.arguments.get("hard") or not request.arguments.get("yes"):
            raise FulcrumError.invalid(
                "CONFIRMATION_REQUIRED", "reset requires both --hard and --yes"
            )
        if not request.request_id:
            raise FulcrumError.invalid(
                "REQUEST_ID_REQUIRED", "hard reset requires a stable request ID"
            )
        if request.instance.brain_root is None or request.instance.lock_path is None:
            raise FulcrumError(
                "CONFIG_INVALID", "hard reset requires a valid brain root", exit_code=4
            )
        manager = ConfigurationManager(request.instance.config_path)
        document, _ = manager.load()
        config = manager.effective(document)
        fence_path = request.instance.instance_root / "maintenance-fence.json"
        fence: dict[str, Any] | None = _load(fence_path)
        if fence is not None and fence.get("request_id") != request.request_id:
            raise FulcrumError(
                "RESET_IN_PROGRESS",
                "another retained reset fence owns this instance",
                exit_code=5,
                details={
                    "fence": str(fence_path),
                    "request_id": fence.get("request_id"),
                },
            )
        services = _services(request.instance.instance_root)
        broker = services.get("broker")
        dolt = services.get("dolt")
        if broker is None or dolt is None:
            raise FulcrumError(
                "SERVICE_NOT_INSTALLED",
                "reset requires the exact owned broker and Dolt service definitions",
                exit_code=4,
            )
        if inspect_service(broker.label).running:
            raise FulcrumError(
                "RESET_SERVICE_ACTIVE",
                "stop the Fulcrum broker before destructive reset",
                exit_code=5,
                next_command=("fulcrum", "service", "stop", "--json"),
            )
        if fence and fence.get("state") == "bootstrap_required":
            return CommandResult(
                ok=True,
                state=CommandState.COMPLETED,
                request_id=request.request_id,
                result=dict(fence),
            )
        if fence and fence.get("state") in {
            "deleting_old_state",
            "old_state_removed",
            "initialization_failed",
        }:
            resumed: dict[str, Any] = dict(fence)
            with WriterLock(request.instance.lock_path):
                _stop(dolt)
                retained_inventory = resumed.get("inventory")
                if not isinstance(retained_inventory, Mapping):
                    raise FulcrumError(
                        "RESET_FENCE_INVALID",
                        "retained reset inventory is missing",
                        exit_code=4,
                    )
                removed = _delete_owned(
                    retained_inventory, brain=request.instance.brain_root
                )
                resumed.update(
                    state="old_state_removed",
                    removed=removed,
                    updated_at=_now(),
                )
                _write(fence_path, resumed)
                try:
                    _initialize_beads(request, config, dolt)
                except Exception as error:
                    resumed.update(
                        state="initialization_failed",
                        error=str(error),
                        updated_at=_now(),
                    )
                    _write(fence_path, resumed)
                    raise
                resumed.update(
                    state="bootstrap_required",
                    error=None,
                    updated_at=_now(),
                    next_command=["fulcrum", "bootstrap", "--json"],
                )
                _write(fence_path, resumed)
            return CommandResult(
                ok=True,
                state=CommandState.COMPLETED,
                request_id=request.request_id,
                result=resumed,
            )
        ledger = Ledger(
            request.instance.brain_root,
            executable=str(config["beads"]["executable"]),
            timeout=request.timeout,
        )
        inventory = _inventory(request, ledger)
        if inventory["blockers"]:
            raise FulcrumError(
                "RESET_WORK_ACTIVE",
                "old admission, operations, assignments, or native effects are not settled",
                exit_code=5,
                details={"blockers": inventory["blockers"]},
            )
        active_fence: dict[str, Any] = {
            "request_id": request.request_id,
            "state": "fenced",
            "created_at": _now(),
            "inventory": inventory,
            "admission": "paused",
        }
        _write(fence_path, active_fence)
        with WriterLock(request.instance.lock_path):
            active_fence.update(state="deleting_old_state", updated_at=_now())
            _write(fence_path, active_fence)
            _stop(dolt)
            removed = _delete_owned(inventory, brain=request.instance.brain_root)
            active_fence.update(
                state="old_state_removed", removed=removed, updated_at=_now()
            )
            _write(fence_path, active_fence)
            try:
                _initialize_beads(request, config, dolt)
            except Exception as error:
                active_fence.update(
                    state="initialization_failed", error=str(error), updated_at=_now()
                )
                _write(fence_path, active_fence)
                raise
            active_fence.update(
                state="bootstrap_required",
                updated_at=_now(),
                next_command=["fulcrum", "bootstrap", "--json"],
            )
            _write(fence_path, active_fence)
        return CommandResult(
            ok=True,
            state=CommandState.COMPLETED,
            request_id=request.request_id,
            result=active_fence,
            warnings=(
                "Native task and schedule deletion is unsupported; retained IDs are recorded in the maintenance fence.",
            ),
        )
