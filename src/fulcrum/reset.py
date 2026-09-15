"""Resumable, enumerated Fulcrum2 hard reset.

The sibling reset workspace is an ordinary stock-Beads workspace. It is the
only mutation authority after the installed controller has stopped and until a
minimal terminal receipt has been written into the clean normal ledger.
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from fulcrum.configuration import ConfigurationManager
from fulcrum.contracts import (
    CommandResult,
    CommandState,
    ErrorInfo,
    FulcrumError,
    ParsedRequest,
)
from fulcrum.ledger import (
    Ledger,
    LedgerFailure,
    OperationRecord,
    accepted_input,
    operation_id,
    operation_view,
    utc_now,
)
from fulcrum.instance import WriterLock
from fulcrum.publication import _git_transport

TERMINAL_STATES = {"completed", "failed", "cancelled", "uncertain"}
RESET_BOUNDARIES = {
    "authority_recorded",
    "native_cleanup_recorded",
    "old_state_removed",
    "clean_ledger_initialized",
    "remote_replaced",
    "clean_receipt_written",
}
_LEGACY_RUNTIME_NAMES = {
    "fulcrum.sqlite3",
    "fulcrum.sqlite3-wal",
    "fulcrum.sqlite3-shm",
    "observations",
    "assignments",
    "progress",
    "evidence",
    "interviews",
    "registry",
    "operative.json",
    "reboot.json",
}


class ResetBoundaryCrash(RuntimeError):
    """Test-only crash raised by an injected boundary observer."""


def reset_workspace(instance_root: Path) -> Path:
    root = instance_root.resolve(strict=False)
    return root.parent / f"{root.name}.reset"


def unfinished_reset_workspace(instance_root: Path) -> Path | None:
    root = reset_workspace(instance_root)
    return root if root.exists() else None


def refuse_unfinished_reset(instance_root: Path) -> None:
    root = unfinished_reset_workspace(instance_root)
    if root is None:
        return
    raise FulcrumError(
        "RESET_INCOMPLETE",
        f"an unfinished hard reset owns this installation: {root}",
        exit_code=4,
        retryable=True,
        next_command=("fulcrum", "reset", "--hard", "--yes", "--json"),
        details={"reset_workspace": str(root)},
    )


def inspect_reset_inventory(request: ParsedRequest) -> dict[str, Any]:
    """Return the same ownership inventory reset would retain, without mutation."""

    config, _ = _load_config(request)
    return _read_only_inventory(request, config)


class ResetService:
    def _runtime(self, config: Mapping[str, Any]) -> Any:
        endpoint = str(config["runtime"]["endpoint"])
        from fulcrum.runtime import AppServerRuntime

        return AppServerRuntime(endpoint)

    def hard_reset(self, request: ParsedRequest) -> CommandResult:
        if request.actor.kind != "human":
            raise FulcrumError(
                "RESET_AUTHORITY_DENIED",
                "hard reset requires explicit human authority",
                exit_code=5,
                request_id=request.request_id,
            )
        if not request.arguments.get("hard") or not request.arguments.get("yes"):
            raise FulcrumError.invalid(
                "CONFIRMATION_REQUIRED", "reset requires both --hard and --yes"
            )
        if request.request_id is None:
            raise FulcrumError.invalid(
                "REQUEST_ID_REQUIRED", "hard reset requires a request ID"
            )
        if request.instance.brain_root is None or request.instance.lock_path is None:
            raise FulcrumError(
                "CONFIG_INVALID", "hard reset requires a valid brain root", exit_code=4
            )
        config, config_bytes = _load_config(request)
        reset_root = reset_workspace(request.instance.instance_root)
        if not reset_root.exists():
            try:
                clean = _configured_ledger(request, config)
                existing = _optional_operation(clean, operation_id(request.request_id))
            except LedgerFailure:
                existing = None
            if existing is not None:
                _verify_existing_request(existing, request)
                if existing.operation.get("state") == "completed":
                    return _operation_result(existing)

        service_facts = _stop_reset_services(request.instance.instance_root)
        with WriterLock(request.instance.lock_path):
            try:
                reset_ledger = _initialize_reset_authority(
                    reset_root, config, request.timeout
                )
            except (FulcrumError, LedgerFailure, OSError) as error:
                return CommandResult(
                    ok=True,
                    state=CommandState.DEGRADED,
                    request_id=request.request_id,
                    result={
                        "durable_receipt": False,
                        "destructive_actions_started": False,
                        "inventory": _read_only_inventory(request, config),
                        "service_control": service_facts,
                        "error": str(error),
                        "next_commands": [
                            [str(config["beads"]["executable"]), "init", "--help"],
                            [
                                "fulcrum",
                                "reset",
                                "--hard",
                                "--yes",
                                "--request-id",
                                request.request_id,
                                "--json",
                            ],
                        ],
                    },
                )
            result = self._run_locked(
                request,
                config,
                config_bytes,
                reset_ledger,
                reset_root,
                service_facts,
            )
        return result

    def _run_locked(
        self,
        request: ParsedRequest,
        config: Mapping[str, Any],
        config_bytes: bytes,
        reset_ledger: Ledger,
        reset_root: Path,
        service_facts: Sequence[Mapping[str, Any]],
    ) -> CommandResult:
        operation, reused = reset_ledger.create_operation(
            request,
            planned={
                "instance_root": str(request.instance.instance_root),
                "brain_root": str(request.instance.brain_root),
                "config_path": str(request.instance.config_path),
                "accepted_reset_input": accepted_input(request),
                "targets": [],
                "boundaries": [],
            },
            next_action="Record exact owned inventory before destructive cleanup.",
        )
        state = operation.operation.get("state")
        error = operation.operation.get("error")
        if (
            reused
            and state in TERMINAL_STATES - {"completed"}
            and isinstance(error, Mapping)
            and error.get("retryable") is False
        ):
            return _operation_result(operation)
        if reused and state in TERMINAL_STATES - {"completed"}:
            if operation.status == "closed":
                reset_ledger.run(("reopen", operation.id), mutating=True)
                current = reset_ledger.show(operation.id)
                if current is None:
                    raise RuntimeError("reset receipt disappeared while reopening")
                operation = OperationRecord.from_record(current)
            operation = reset_ledger.update_operation(
                operation,
                state="running",
                step="reset_resumed",
                attempts=int(operation.operation.get("attempts") or 0) + 1,
                error={},
                next_action="Resume from the first unresolved enumerated target.",
            )
        planned = dict(operation.operation.get("planned") or {})
        if not isinstance(planned.get("inventory"), Mapping):
            inventory = _inventory(request, config)
            supplied_expected = request.input.get("expected_remote_ref")
            actual_expected = inventory["remote_ledger"]["expected_oid"]
            if supplied_expected is not None and supplied_expected != actual_expected:
                operation = reset_ledger.update_operation(
                    operation,
                    state="failed",
                    step="remote_expectation_rejected",
                    planned={**planned, "inventory": inventory},
                    error={
                        "code": "REMOTE_REF_MISMATCH",
                        "message": "supplied expected_remote_ref differs from inventory",
                        "retryable": False,
                    },
                    next_action="Inspect the remote ledger ref before authorizing a new reset request.",
                )
                return _operation_result(operation)
            planned.update({"inventory": inventory, "targets": _targets(inventory)})
            operation = reset_ledger.update_operation(
                operation,
                state="running",
                step="inventory_recorded",
                planned=planned,
                result=_progress(planned, service_facts),
                next_action="Interrupt and delete only enumerated managed resources.",
            )
            operation = self._boundary(
                request,
                reset_ledger,
                operation,
                "authority_recorded",
                service_facts,
            )
        elif reused and operation.operation.get("state") == "completed":
            clean = _configured_ledger(request, config)
            clean_receipt = _optional_operation(clean, operation.id)
            if clean_receipt is None:
                raise FulcrumError(
                    "RESET_RECEIPT_MISSING",
                    "temporary reset completed without a clean-ledger receipt",
                    exit_code=4,
                )
            _remove_reset_workspace(reset_root)
            return CommandResult(
                ok=True,
                state=CommandState.COMPLETED,
                operation_id=clean_receipt.id,
                request_id=request.request_id,
                result={
                    **operation_view(clean_receipt),
                    "service_start": _start_normal_services(request, config),
                },
            )

        operation = self._cleanup_native(
            request, config, reset_ledger, operation, service_facts
        )
        if _kinds_completed(operation, "native_task"):
            operation = self._boundary(
                request,
                reset_ledger,
                operation,
                "native_cleanup_recorded",
                service_facts,
            )
        operation = self._cleanup_workspaces(
            config, reset_ledger, operation, service_facts
        )
        operation = self._remove_old_state(
            request, reset_ledger, operation, service_facts
        )
        if _kinds_completed(operation, "operational_path", "ledger_root"):
            operation = self._boundary(
                request,
                reset_ledger,
                operation,
                "old_state_removed",
                service_facts,
            )
        operation = self._initialize_clean_ledger(
            request, config, reset_ledger, operation, reset_root, service_facts
        )
        if _kinds_completed(operation, "clean_ledger"):
            operation = self._boundary(
                request,
                reset_ledger,
                operation,
                "clean_ledger_initialized",
                service_facts,
            )
        operation = self._replace_remote(
            request, config, reset_ledger, operation, reset_root, service_facts
        )
        if _kinds_completed(operation, "remote_ledger"):
            operation = self._boundary(
                request,
                reset_ledger,
                operation,
                "remote_replaced",
                service_facts,
            )
        operation = self._bootstrap_clean(
            request, config, reset_ledger, operation, service_facts
        )
        planned = dict(operation.operation.get("planned") or {})
        failures = _required_failures(planned)
        if failures:
            operation = reset_ledger.update_operation(
                operation,
                state="failed",
                step="required_cleanup_unresolved",
                planned=planned,
                result=_progress(planned, service_facts),
                error={
                    "code": "RESET_INCOMPLETE",
                    "message": "one or more enumerated reset targets remain unresolved",
                    "retryable": True,
                    "targets": failures,
                },
                next_action="Repair the listed target and repeat this exact request ID.",
            )
            return _operation_result(operation)

        if request.instance.config_path.read_bytes() != config_bytes:
            operation = reset_ledger.update_operation(
                operation,
                state="failed",
                step="configuration_changed_during_reset",
                planned=planned,
                error={
                    "code": "CONFIG_CONCURRENT_CHANGE",
                    "message": "authoritative YAML bytes changed during hard reset",
                    "retryable": True,
                },
                next_action="Reconcile the configuration change before resuming reset.",
            )
            return _operation_result(operation)

        clean_ledger = _configured_ledger(request, config)
        terminal = _write_clean_receipt(request, clean_ledger, planned)
        final_remote = _publish_clean_terminal(
            request, clean_ledger, planned["inventory"], terminal.id
        )
        remote_target = _one_target(planned, "remote_ledger")
        remote_after = dict(remote_target.get("after") or {})
        remote_after.update(final_remote)
        remote_target["after"] = remote_after
        operation = reset_ledger.update_operation(
            operation,
            state="completed",
            step="clean_terminal_receipt_written",
            planned=planned,
            result=_progress(planned, service_facts),
            next_action="Remove temporary reset authority and start normal service.",
        )

        _remove_reset_workspace(reset_root)
        start_facts = _start_normal_services(request, config)
        return CommandResult(
            ok=True,
            state=CommandState.COMPLETED,
            operation_id=terminal.id,
            request_id=request.request_id,
            result={
                **operation_view(terminal),
                **_progress(planned, service_facts),
                "service_start": start_facts,
            },
        )

    def _cleanup_native(
        self,
        request: ParsedRequest,
        config: Mapping[str, Any],
        ledger: Ledger,
        operation: OperationRecord,
        service_facts: Sequence[Mapping[str, Any]],
    ) -> OperationRecord:
        planned: dict[str, Any] = dict(operation.operation.get("planned") or {})
        pending: list[dict[str, Any]] = [
            target
            for target in _target_rows(planned, "native_task")
            if target.get("state") != "completed"
        ]
        for target in pending:
            error = target.get("error")
            message = error.get("message") if isinstance(error, Mapping) else None
            if isinstance(message, str) and "thread not found:" in message.lower():
                _complete_target(
                    target,
                    {
                        "thread_id": str(target["id"]),
                        "exists": False,
                        "already_absent": True,
                    },
                )
        pending = [target for target in pending if target.get("state") != "completed"]
        if not pending:
            return _persist_targets(
                ledger,
                operation.id,
                planned,
                service_facts,
                "native_cleanup_recorded",
            )
        runtime: Any = self._runtime(config)

        async def cleanup() -> None:
            try:
                await runtime.connect()
                for target in pending:
                    try:
                        after = await _delete_native_task(
                            runtime, str(target["id"]), request.timeout
                        )
                    except Exception as error:
                        if "thread not found:" in str(error).lower():
                            _complete_target(
                                target,
                                {
                                    "thread_id": str(target["id"]),
                                    "exists": False,
                                    "already_absent": True,
                                },
                            )
                        else:
                            _fail_target(target, error)
                    else:
                        _complete_target(target, after)
                    _persist_targets(
                        ledger, operation.id, planned, service_facts, "native_cleanup"
                    )
            finally:
                await runtime.close()

        try:
            asyncio.run(cleanup())
        except Exception as error:
            for target in pending:
                if target.get("state") != "completed":
                    _fail_target(target, error)
        return _persist_targets(
            ledger, operation.id, planned, service_facts, "native_cleanup_recorded"
        )

    def _cleanup_workspaces(
        self,
        config: Mapping[str, Any],
        ledger: Ledger,
        operation: OperationRecord,
        service_facts: Sequence[Mapping[str, Any]],
    ) -> OperationRecord:
        planned = dict(operation.operation.get("planned") or {})
        for target in _target_rows(planned, "provider_handle"):
            if target.get("state") == "completed":
                continue
            try:
                _complete_target(target, _cancel_provider_target(target, config))
            except Exception as error:
                _fail_target(target, error)
            operation = _persist_targets(
                ledger, operation.id, planned, service_facts, "provider_cleanup"
            )
        for target in _target_rows(planned, "worktree"):
            if target.get("state") == "completed":
                continue
            try:
                _complete_target(target, _remove_worktree_target(target, config))
            except Exception as error:
                _fail_target(target, error)
            operation = _persist_targets(
                ledger, operation.id, planned, service_facts, "worktree_cleanup"
            )
        return operation

    def _remove_old_state(
        self,
        request: ParsedRequest,
        ledger: Ledger,
        operation: OperationRecord,
        service_facts: Sequence[Mapping[str, Any]],
    ) -> OperationRecord:
        from fulcrum.installation_service import _stop_one, load_installed_services

        planned = dict(operation.operation.get("planned") or {})
        dolt = load_installed_services(request.instance.instance_root).get("dolt")
        if dolt is not None:
            try:
                _stop_one(dolt)
            except FulcrumError as error:
                for target in _target_rows(planned, "ledger_root"):
                    _fail_target(target, error)
                return _persist_targets(
                    ledger,
                    operation.id,
                    planned,
                    service_facts,
                    "old_ledger_stop_failed",
                )
        targets = _target_rows(planned, "operational_path") + _target_rows(
            planned, "ledger_root"
        )
        for target in targets:
            if target.get("state") == "completed":
                continue
            try:
                path = Path(str(target["before"]["path"]))
                _remove_exact_path(path)
                _complete_target(target, {"path": str(path), "exists": path.exists()})
            except Exception as error:
                _fail_target(target, error)
        return _persist_targets(
            ledger, operation.id, planned, service_facts, "old_state_cleanup"
        )

    def _initialize_clean_ledger(
        self,
        request: ParsedRequest,
        config: Mapping[str, Any],
        ledger: Ledger,
        operation: OperationRecord,
        reset_root: Path,
        service_facts: Sequence[Mapping[str, Any]],
    ) -> OperationRecord:
        planned = dict(operation.operation.get("planned") or {})
        target = _one_target(planned, "clean_ledger")
        if target.get("state") == "completed":
            return operation
        try:
            clean = _initialize_clean_normal_ledger(request, config, reset_root)
            old_ids = set(planned["inventory"].get("old_ledger_record_ids", []))
            observed_ids = {item.id for item in clean.list_records(limit=0)}
            retained = old_ids.intersection(observed_ids)
            if retained:
                raise FulcrumError(
                    "RESET_LEDGER_NOT_CLEAN",
                    "clean ledger still exposes old Fulcrum records",
                    exit_code=4,
                    details={"retained": sorted(retained)},
                )
            _complete_target(
                target,
                {
                    "workspace": str(request.instance.brain_root),
                    "old_records_absent": True,
                    "records": sorted(observed_ids),
                },
            )
        except Exception as error:
            _fail_target(target, error)
        return _persist_targets(
            ledger, operation.id, planned, service_facts, "clean_ledger_initialized"
        )

    def _replace_remote(
        self,
        request: ParsedRequest,
        config: Mapping[str, Any],
        ledger: Ledger,
        operation: OperationRecord,
        reset_root: Path,
        service_facts: Sequence[Mapping[str, Any]],
    ) -> OperationRecord:
        planned = dict(operation.operation.get("planned") or {})
        target = _one_target(planned, "remote_ledger")
        if target.get("state") == "completed":
            return operation
        if _one_target(planned, "clean_ledger").get("state") != "completed":
            _fail_target(target, RuntimeError("clean ledger is not initialized"))
        else:
            try:
                _complete_target(
                    target,
                    _replace_remote_ledger(
                        request, config, reset_root, target, planned["inventory"]
                    ),
                )
            except Exception as error:
                _fail_target(target, error)
        return _persist_targets(
            ledger, operation.id, planned, service_facts, "remote_replacement_recorded"
        )

    def _bootstrap_clean(
        self,
        request: ParsedRequest,
        config: Mapping[str, Any],
        ledger: Ledger,
        operation: OperationRecord,
        service_facts: Sequence[Mapping[str, Any]],
    ) -> OperationRecord:
        planned = dict(operation.operation.get("planned") or {})
        target = _one_target(planned, "clean_bootstrap")
        if target.get("state") == "completed":
            return operation
        if any(
            _one_target(planned, kind).get("state") != "completed"
            for kind in ("clean_ledger", "remote_ledger")
        ):
            _fail_target(target, RuntimeError("clean ledger or remote is unresolved"))
        else:
            try:
                _complete_target(
                    target,
                    self._bootstrap_control(
                        request, config, _configured_ledger(request, config)
                    ),
                )
            except Exception as error:
                _fail_target(target, error)
        return _persist_targets(
            ledger, operation.id, planned, service_facts, "clean_bootstrap_recorded"
        )

    def _bootstrap_control(
        self,
        request: ParsedRequest,
        config: Mapping[str, Any],
        ledger: Ledger,
    ) -> dict[str, Any]:
        initial = _bootstrap_control_record(request, ledger)
        from fulcrum.leadership import ensure_leadership

        runtime: Any = self._runtime(config)

        async def provision() -> list[dict[str, Any]]:
            try:
                actions = await ensure_leadership(request, ledger, runtime, config)
                return [dict(item) for item in actions]
            finally:
                await runtime.close()

        actions = asyncio.run(provision())
        by_role = {
            str(item.get("role")): item
            for item in actions
            if isinstance(item.get("thread_id"), str)
        }
        if set(by_role) != {"vizier", "marshal"}:
            raise RuntimeError(
                f"clean leadership identities were not both provisioned: {actions}"
            )
        return {
            "control_id": "fc-system",
            "created": initial["created"],
            "vizier_thread": by_role["vizier"]["thread_id"],
            "marshal_thread": by_role["marshal"]["thread_id"],
            "actions": actions,
            "turns_started": 0,
        }

    def _boundary(
        self,
        request: ParsedRequest,
        ledger: Ledger,
        operation: OperationRecord,
        name: str,
        service_facts: Sequence[Mapping[str, Any]],
    ) -> OperationRecord:
        if name not in RESET_BOUNDARIES:
            raise ValueError(name)
        planned = dict(operation.operation.get("planned") or {})
        boundaries = list(planned.get("boundaries") or [])
        if name not in boundaries:
            boundaries.append(name)
            planned["boundaries"] = boundaries
            operation = ledger.update_operation(
                operation,
                state="running",
                step=name,
                planned=planned,
                result=_progress(planned, service_facts),
            )
        return operation


def _load_config(request: ParsedRequest) -> tuple[dict[str, Any], bytes]:
    manager = ConfigurationManager(request.instance.config_path)
    document, raw = manager.load()
    return manager.effective(document), raw


def _configured_ledger(request: ParsedRequest, config: Mapping[str, Any]) -> Ledger:
    assert request.instance.brain_root is not None
    executable = config["beads"].get("executable")
    return Ledger(
        request.instance.brain_root,
        executable=str(executable) if executable else None,
        timeout=request.timeout,
    )


def _initialize_reset_authority(
    root: Path, config: Mapping[str, Any], timeout: float
) -> Ledger:
    executable = str(config["beads"].get("executable") or "bd")
    if not root.exists():
        root.mkdir(parents=False, mode=0o700)
    if not (root / ".git").is_dir():
        _run(("git", "init", "--initial-branch=reset", str(root)), timeout=timeout)
        _run(("git", "-C", str(root), "config", "user.name", "Fulcrum Reset"))
        _run(("git", "-C", str(root), "config", "user.email", "reset@fulcrum.invalid"))
    if not (root / ".beads" / "config.yaml").is_file():
        (root / ".beads" / "dolt").mkdir(parents=True, mode=0o700)
        completed = _run(
            (
                executable,
                "-C",
                str(root),
                "init",
                "--prefix",
                "fc",
                "--non-interactive",
                "--skip-agents",
                "--skip-hooks",
            ),
            timeout=max(30.0, timeout),
            check=False,
        )
        if completed.returncode != 0:
            raise FulcrumError(
                "RESET_AUTHORITY_UNAVAILABLE",
                completed.stderr.strip()
                or completed.stdout.strip()
                or "stock Beads could not initialize the reset workspace",
                exit_code=4,
                retryable=True,
            )
    ledger = Ledger(root, executable=executable, timeout=timeout)
    ledger.run(("status",))
    return ledger


def _stop_reset_services(instance_root: Path) -> list[dict[str, Any]]:
    from fulcrum.installation_service import _stop_one, load_installed_services

    installed = load_installed_services(instance_root)
    facts: list[dict[str, Any]] = []
    for name in ("updater", "controller"):
        service = installed.get(name)
        if service is None:
            facts.append({"name": name, "state": "not_installed"})
        else:
            facts.append({"name": name, **_stop_one(service)})
    return facts


def _start_normal_services(
    request: ParsedRequest, config: Mapping[str, Any]
) -> list[dict[str, Any]]:
    from fulcrum.installation_service import _start_one, load_installed_services

    installed = load_installed_services(request.instance.instance_root)
    facts: list[dict[str, Any]] = []
    dolt = installed.get("dolt")
    if dolt is not None:
        facts.append(
            _start_one(
                dolt,
                endpoint=f"tcp://{config['beads']['host']}:{config['beads']['port']}",
            )
        )
    controller = installed.get("controller")
    if controller is not None:
        facts.append(_start_one(controller, endpoint=None))
    return facts


def _inventory(request: ParsedRequest, config: Mapping[str, Any]) -> dict[str, Any]:
    assert request.instance.brain_root is not None
    records = _configured_ledger(request, config).list_records(limit=0)
    tasks: dict[str, dict[str, Any]] = {}
    worktrees: dict[tuple[str, str], dict[str, Any]] = {}
    providers: dict[tuple[str, str], dict[str, Any]] = {}
    projects = config.get("projects")
    projects = projects if isinstance(projects, Mapping) else {}
    for record in records:
        fc = record.fc or {}
        if record.kind == "task" and isinstance(fc.get("thread_id"), str):
            thread_id = str(fc["thread_id"])
            tasks[thread_id] = {
                "thread_id": thread_id,
                "record_id": record.id,
                "role": fc.get("role"),
                "ownership_evidence": "stock_beads_task_record",
            }
        if record.kind != "work":
            continue
        project_id = str(fc.get("project") or "")
        project = projects.get(project_id)
        project = project if isinstance(project, Mapping) else {}
        workspace = fc.get("worktree")
        if isinstance(workspace, Mapping) and isinstance(workspace.get("path"), str):
            key = (str(project.get("root") or ""), str(workspace["path"]))
            worktrees[key] = {
                "bead_id": record.id,
                "project": project_id,
                "project_root": str(project.get("root") or ""),
                "repository_id": _repository_id(project),
                "path": str(workspace["path"]),
                "branch": workspace.get("branch"),
                "ownership_evidence": "stock_beads_work_record",
            }
        delivery = fc.get("delivery")
        if isinstance(delivery, Mapping) and isinstance(
            delivery.get("provider_handle"), str
        ):
            handle = str(delivery["provider_handle"])
            repository_id = _repository_id(project)
            providers[(repository_id or "", handle)] = {
                "bead_id": record.id,
                "project": project_id,
                "repository_id": repository_id,
                "handle": handle,
                "source_oid": delivery.get("source_oid"),
                "ownership_evidence": "stock_beads_delivery_record",
            }
    legacy = _legacy_inventory(request)
    _validate_legacy_scope(request, config, legacy)
    for row in legacy["tasks"]:
        tasks.setdefault(str(row["thread_id"]), row)
    for row in legacy["worktrees"]:
        worktrees.setdefault((str(row["project_root"]), str(row["path"])), row)
    for row in legacy["provider_handles"]:
        providers.setdefault((str(row["repository_id"]), str(row["handle"])), row)
    ordinary_remote = _ordinary_remote(request.instance.brain_root, config)
    remote_refs = _remote_refs(ordinary_remote, request.timeout)
    return {
        "recorded_at": utc_now(),
        "instance_root": str(request.instance.instance_root),
        "brain_root": str(request.instance.brain_root),
        "config_path": str(request.instance.config_path),
        "old_ledger_record_ids": sorted(record.id for record in records),
        "native_tasks": sorted(tasks.values(), key=lambda item: str(item["thread_id"])),
        "worktrees": sorted(worktrees.values(), key=lambda item: str(item["path"])),
        "provider_handles": sorted(
            providers.values(), key=lambda item: str(item["handle"])
        ),
        "operational_paths": _operational_paths(request, legacy),
        "ledger_root": str(request.instance.brain_root / ".beads"),
        "legacy": legacy,
        "remote_ledger": {
            "ordinary_remote": ordinary_remote,
            "transport": _git_transport(ordinary_remote),
            "ref": "refs/dolt/data",
            "expected_oid": remote_refs.get("refs/dolt/data"),
            "unrelated_refs": {
                name: oid
                for name, oid in remote_refs.items()
                if name != "refs/dolt/data"
            },
        },
    }


def _read_only_inventory(
    request: ParsedRequest, config: Mapping[str, Any]
) -> dict[str, Any]:
    try:
        return _inventory(request, config)
    except Exception as error:
        return {
            "instance_root": str(request.instance.instance_root),
            "brain_root": str(request.instance.brain_root),
            "legacy": _legacy_inventory(request),
            "gaps": [{"component": "stock_beads", "reason": str(error)}],
        }


def _legacy_inventory(request: ParsedRequest) -> dict[str, Any]:
    state_root = (
        Path(
            str(
                request.input.get("legacy_state_root") or request.instance.instance_root
            )
        )
        .expanduser()
        .resolve(strict=False)
    )
    database = (
        Path(
            str(request.input.get("legacy_database") or state_root / "fulcrum.sqlite3")
        )
        .expanduser()
        .resolve(strict=False)
    )
    result: dict[str, Any] = {
        "state_root": str(state_root),
        "database": str(database),
        "database_exists": database.is_file(),
        "tasks": [],
        "worktrees": [],
        "provider_handles": [],
        "gaps": [],
    }
    if not database.is_file():
        return result
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
    except sqlite3.Error as error:
        result["gaps"].append(str(error))
        return result
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "tasks" in tables and "native_thread_id" in _sqlite_columns(
            connection, "tasks"
        ):
            for row in connection.execute(
                "SELECT native_thread_id FROM tasks WHERE native_thread_id IS NOT NULL"
            ):
                result["tasks"].append(
                    {
                        "thread_id": str(row[0]),
                        "record_id": None,
                        "role": None,
                        "ownership_evidence": "legacy_operational_store",
                    }
                )
        if "assignments" in tables:
            columns = _sqlite_columns(connection, "assignments")
            selected = [
                name
                for name in ("id", "worktree_path", "candidate_id")
                if name in columns
            ]
            if selected:
                for raw in connection.execute(
                    f"SELECT {','.join(selected)} FROM assignments"
                ):
                    row = dict(raw)
                    if row.get("worktree_path"):
                        result["worktrees"].append(
                            {
                                "bead_id": None,
                                "project": None,
                                "project_root": "",
                                "repository_id": None,
                                "path": str(row["worktree_path"]),
                                "branch": None,
                                "ownership_evidence": "legacy_operational_store",
                            }
                        )
                    if row.get("candidate_id"):
                        result["provider_handles"].append(
                            {
                                "bead_id": None,
                                "project": None,
                                "repository_id": None,
                                "handle": str(row["candidate_id"]),
                                "source_oid": None,
                                "ownership_evidence": "legacy_operational_store",
                            }
                        )
    except sqlite3.Error as error:
        result["gaps"].append(str(error))
    finally:
        connection.close()
    return result


def _validate_legacy_scope(
    request: ParsedRequest,
    config: Mapping[str, Any],
    legacy: Mapping[str, Any],
) -> None:
    assert request.instance.brain_root is not None
    instance = request.instance.instance_root.resolve(strict=False)
    state_root = Path(str(legacy["state_root"])).resolve(strict=False)
    database = Path(str(legacy["database"])).resolve(strict=False)
    protected = {
        Path("/").resolve(),
        Path.home().resolve(strict=False),
        request.instance.brain_root.resolve(strict=False),
    }
    projects = config.get("projects")
    if isinstance(projects, Mapping):
        for project in projects.values():
            if isinstance(project, Mapping) and isinstance(project.get("root"), str):
                protected.add(Path(str(project["root"])).resolve(strict=False))
    if state_root != instance and any(
        state_root == path
        or state_root.is_relative_to(path)
        or path.is_relative_to(state_root)
        for path in protected
    ):
        raise FulcrumError.invalid(
            "UNSAFE_RESET_TARGET",
            f"legacy_state_root overlaps protected content: {state_root}",
        )
    if not database.is_relative_to(state_root):
        raise FulcrumError.invalid(
            "UNSAFE_RESET_TARGET",
            "legacy_database must be inside the enumerated legacy_state_root",
        )


def _sqlite_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _operational_paths(
    request: ParsedRequest, legacy: Mapping[str, Any]
) -> list[dict[str, Any]]:
    instance = request.instance.instance_root.resolve(strict=False)
    state_root = Path(str(legacy["state_root"])).resolve(strict=False)
    paths = {
        instance / "logs",
        instance / "service-health.json",
        instance / "controller.sock",
        instance / "publication-worktrees",
        Path(str(legacy["database"])).resolve(strict=False),
        *(state_root / name for name in _LEGACY_RUNTIME_NAMES),
    }
    protected: set[Path] = {
        request.instance.config_path.resolve(strict=False),
        instance / "runtime",
        instance / "recovery",
        instance / "services",
        instance / "config",
    }
    if request.instance.brain_root is not None:
        protected.add(request.instance.brain_root.resolve(strict=False))
    return [
        {"path": str(path), "exists": path.exists()}
        for path in sorted(paths, key=str)
        if path not in protected and path != Path.home().resolve(strict=False)
    ]


def _targets(inventory: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for kind, key, identifier in (
        ("native_task", "native_tasks", "thread_id"),
        ("provider_handle", "provider_handles", "handle"),
        ("worktree", "worktrees", "path"),
        ("operational_path", "operational_paths", "path"),
    ):
        values = inventory.get(key)
        if isinstance(values, list):
            rows.extend(
                _target(kind, str(item[identifier]), item)
                for item in values
                if isinstance(item, Mapping)
            )
    rows.extend(
        (
            _target(
                "ledger_root",
                str(inventory["ledger_root"]),
                {"path": inventory["ledger_root"]},
            ),
            _target(
                "clean_ledger",
                str(inventory["brain_root"]),
                {"workspace": inventory["brain_root"]},
            ),
            _target("remote_ledger", "refs/dolt/data", inventory["remote_ledger"]),
            _target(
                "clean_bootstrap",
                "fc-system",
                {"instance_root": inventory["instance_root"]},
            ),
        )
    )
    return rows


def _target(kind: str, identifier: str, before: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "kind": kind,
        "id": identifier,
        "required": True,
        "state": "pending",
        "before": dict(before),
        "after": None,
        "error": None,
        "attempts": 0,
    }


def _target_rows(planned: Mapping[str, Any], kind: str) -> list[dict[str, Any]]:
    values = planned.get("targets")
    return (
        [item for item in values if isinstance(item, dict) and item.get("kind") == kind]
        if isinstance(values, list)
        else []
    )


def _one_target(planned: Mapping[str, Any], kind: str) -> dict[str, Any]:
    rows = _target_rows(planned, kind)
    if len(rows) != 1:
        raise FulcrumError(
            "RESET_RECEIPT_INVALID",
            f"reset receipt has {len(rows)} {kind} targets",
            exit_code=4,
        )
    return rows[0]


def _complete_target(target: dict[str, Any], after: Mapping[str, Any]) -> None:
    target["attempts"] = int(target.get("attempts") or 0) + 1
    target["state"] = "completed"
    target["after"] = {**dict(after), "observed_at": utc_now()}
    target["error"] = None


def _fail_target(target: dict[str, Any], error: Exception) -> None:
    target["attempts"] = int(target.get("attempts") or 0) + 1
    target["state"] = "failed"
    target["error"] = {
        "message": str(error),
        "type": type(error).__name__,
        "observed_at": utc_now(),
    }


def _persist_targets(
    ledger: Ledger,
    identifier: str,
    planned: dict[str, Any],
    service_facts: Sequence[Mapping[str, Any]],
    step: str,
) -> OperationRecord:
    return ledger.update_operation(
        identifier,
        state="running",
        step=step,
        planned=planned,
        result=_progress(planned, service_facts),
        next_action="Resume failed targets by inspecting recorded postconditions.",
    )


def _progress(
    planned: Mapping[str, Any], service_facts: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    values = planned.get("targets")
    rows = (
        [dict(item) for item in values if isinstance(item, Mapping)]
        if isinstance(values, list)
        else []
    )
    boundaries = planned.get("boundaries")
    return {
        "completed_targets": [
            item for item in rows if item.get("state") == "completed"
        ],
        "pending_targets": [item for item in rows if item.get("state") != "completed"],
        "current_step": (
            boundaries[-1]
            if isinstance(boundaries, list) and boundaries
            else "inventory"
        ),
        "service_control": [dict(item) for item in service_facts],
        "new_installation": {
            "instance_root": planned.get("instance_root"),
            "brain_root": planned.get("brain_root"),
            "config_path": planned.get("config_path"),
        },
    }


def _required_failures(planned: Mapping[str, Any]) -> list[dict[str, Any]]:
    values = planned.get("targets")
    return (
        [
            dict(item)
            for item in values
            if isinstance(item, Mapping)
            and item.get("required")
            and item.get("state") != "completed"
        ]
        if isinstance(values, list)
        else []
    )


def _target_counts(planned: Mapping[str, Any]) -> list[dict[str, Any]]:
    values = planned.get("targets")
    counts: dict[str, int] = {}
    if isinstance(values, list):
        for item in values:
            if isinstance(item, Mapping) and item.get("state") == "completed":
                kind = str(item.get("kind"))
                counts[kind] = counts.get(kind, 0) + 1
    return [{"kind": kind, "count": counts[kind]} for kind in sorted(counts)]


def _kinds_completed(operation: OperationRecord, *kinds: str) -> bool:
    planned = operation.operation.get("planned")
    if not isinstance(planned, Mapping):
        return False
    rows = [target for kind in kinds for target in _target_rows(planned, kind)]
    return all(target.get("state") == "completed" for target in rows)


async def _delete_native_task(
    runtime: Any, thread_id: str, timeout: float
) -> dict[str, Any]:
    try:
        facts = await runtime.inspect_task(thread_id)
    except Exception as error:
        if "thread not found:" not in str(error).lower():
            raise
        return {"thread_id": thread_id, "exists": False, "already_absent": True}
    if not facts.exists:
        return {"thread_id": thread_id, "exists": False, "already_absent": True}
    if facts.active_turn is not None:
        await runtime.interrupt(thread_id, facts.active_turn)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            facts = await runtime.inspect_task(thread_id)
            if facts.active_turn is None:
                break
            await asyncio.sleep(0.05)
        if facts.active_turn is not None:
            raise RuntimeError(f"native task {thread_id} still has an active turn")
    terminals = await runtime.terminals(thread_id, limit=0, cursor=None)
    items = terminals.get("items") if isinstance(terminals, Mapping) else None
    for item in items if isinstance(items, list) else []:
        terminal_id = item.get("terminal_id") if isinstance(item, Mapping) else None
        if isinstance(terminal_id, str):
            observed = await runtime.terminate_terminal(thread_id, terminal_id)
            if observed.get("still_running"):
                raise RuntimeError(f"owned terminal {terminal_id} remains running")
    deleted = await runtime.delete(thread_id)
    if (await runtime.inspect_task(thread_id)).exists:
        raise RuntimeError(f"native task {thread_id} remains after deletion")
    return {
        "thread_id": thread_id,
        "exists": False,
        "runtime_state": deleted.runtime_status,
    }


def _cancel_provider_target(
    target: Mapping[str, Any], config: Mapping[str, Any]
) -> dict[str, Any]:
    before = target.get("before")
    before = before if isinstance(before, Mapping) else {}
    repository_id, handle = before.get("repository_id"), before.get("handle")
    if not isinstance(repository_id, str) or not repository_id:
        inferred = {
            str(delivery["id"])
            for project in (config.get("projects") or {}).values()
            if isinstance(project, Mapping)
            and isinstance((delivery := project.get("delivery")), Mapping)
            and isinstance(delivery.get("id"), str)
            and delivery.get("id")
        }
        if (
            before.get("ownership_evidence") != "legacy_operational_store"
            or len(inferred) != 1
        ):
            raise RuntimeError("provider target has no exact repository ID")
        repository_id = inferred.pop()
    if not isinstance(handle, str) or not handle:
        raise RuntimeError("provider target has no exact handle")
    configured_provider = config.get("delivery")
    provider = configured_provider if isinstance(configured_provider, Mapping) else {}
    if provider.get("kind") != "tollgate" or not provider.get("executable"):
        raise RuntimeError("configured delivery provider cannot inspect this handle")
    from fulcrum.tollgate import Tollgate

    tollgate = Tollgate(str(provider["executable"]))
    observed = tollgate.status(repository_id, handle)
    row = _provider_row(observed, handle)
    state = str(row.get("state") if row else "absent")
    if state in {"queued", "running", "validated", "promoting"}:
        tollgate.cancel(repository_id, handle)
        row = _provider_row(tollgate.status(repository_id, handle), handle)
        state = str(row.get("state") if row else "absent")
    if state in {"queued", "running", "validated", "promoting"}:
        raise RuntimeError(f"provider handle {handle} remains active")
    return {"repository_id": repository_id, "handle": handle, "state": state}


def _provider_row(value: Any, handle: str) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        candidate = value.get("item")
        if (
            isinstance(candidate, Mapping)
            and str(candidate.get("id") or candidate.get("item_id")) == handle
        ):
            return candidate
        for key in ("items", "candidates", "history"):
            found = _provider_row(value.get(key), handle)
            if found is not None:
                return found
    if isinstance(value, list):
        for item in value:
            if (
                isinstance(item, Mapping)
                and str(item.get("id") or item.get("item_id")) == handle
            ):
                nested = item.get("item")
                return nested if isinstance(nested, Mapping) else item
    return None


def _remove_worktree_target(
    target: Mapping[str, Any], config: Mapping[str, Any]
) -> dict[str, Any]:
    before = target.get("before")
    before = before if isinstance(before, Mapping) else {}
    path = Path(str(before.get("path") or "")).resolve(strict=False)
    root_value = before.get("project_root")
    if not isinstance(root_value, str) or not root_value:
        if not path.exists():
            return {"path": str(path), "exists": False, "already_absent": True}
        roots = {
            Path(str(project["root"])).resolve(strict=True)
            for project in (config.get("projects") or {}).values()
            if isinstance(project, Mapping)
            and isinstance(project.get("root"), str)
            and path.is_relative_to(Path(str(project["root"])).resolve(strict=False))
        }
        owned = {
            root
            for root in roots
            if any(item.get("worktree") == str(path) for item in _git_worktrees(root))
        }
        if (
            before.get("ownership_evidence") != "legacy_operational_store"
            or len(owned) != 1
        ):
            raise RuntimeError("worktree target has no exact project root")
        root_value = str(owned.pop())
    root = Path(root_value).resolve(strict=True)
    matches = [
        item for item in _git_worktrees(root) if item.get("worktree") == str(path)
    ]
    if not matches:
        if path.exists():
            raise RuntimeError("path exists without exact Git worktree ownership")
    else:
        if len(matches) != 1:
            raise RuntimeError("worktree ownership is ambiguous")
        expected_branch = before.get("branch")
        observed_branch = str(matches[0].get("branch") or "").removeprefix(
            "refs/heads/"
        )
        if expected_branch and observed_branch != str(expected_branch):
            raise RuntimeError("worktree branch differs from recorded ownership")
        _run(("git", "-C", str(root), "worktree", "remove", "--force", str(path)))
    branch = before.get("branch")
    if isinstance(branch, str) and branch:
        reference = f"refs/heads/{branch}"
        if (
            _run(
                ("git", "-C", str(root), "show-ref", "--verify", "--quiet", reference),
                check=False,
            ).returncode
            == 0
        ):
            _run(("git", "-C", str(root), "branch", "-D", branch))
    return {"path": str(path), "exists": path.exists(), "branch_absent": True}


def _git_worktrees(root: Path) -> list[dict[str, str]]:
    value = _run(("git", "-C", str(root), "worktree", "list", "--porcelain")).stdout
    result: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in value.splitlines() + [""]:
        if not line:
            if current:
                result.append(current)
                current = {}
            continue
        key, _, item = line.partition(" ")
        current[key] = item
    return result


def _initialize_clean_normal_ledger(
    request: ParsedRequest, config: Mapping[str, Any], reset_root: Path
) -> Ledger:
    from fulcrum.installation_service import _start_one, load_installed_services

    assert request.instance.brain_root is not None
    brain = request.instance.brain_root
    executable = str(config["beads"].get("executable") or "bd")
    try:
        existing = Ledger(brain, executable=executable, timeout=request.timeout)
        existing.run(("status",))
        return existing
    except LedgerFailure:
        pass
    beads_dir = brain / ".beads"
    # The installed server reads this directory at startup, before bd init runs.
    (beads_dir / "dolt").mkdir(parents=True, exist_ok=True, mode=0o700)
    dolt = load_installed_services(request.instance.instance_root).get("dolt")
    server_args: list[str] = []
    if dolt is not None:
        _start_one(
            dolt, endpoint=f"tcp://{config['beads']['host']}:{config['beads']['port']}"
        )
        server_args = [
            "--server",
            "--external",
            "--server-host",
            str(config["beads"]["host"]),
            "--server-port",
            str(config["beads"]["port"]),
            "--database",
            str(config["beads"]["database"]),
        ]
    staging = reset_root / "clean-ledger-remote.git"
    if not staging.exists():
        _run(("git", "init", "--bare", "--initial-branch=main", str(staging)))
    _ensure_staging_anchor(reset_root, staging, request.timeout)
    (beads_dir / "config.yaml").write_text(
        f'sync.remote: "{_git_transport(str(staging))}"\n', encoding="utf-8"
    )
    completed = _run(
        (
            executable,
            "-C",
            str(brain),
            "init",
            *server_args,
            "--prefix",
            "fc",
            "--reinit-local",
            "--discard-remote",
            "--destroy-token",
            "DESTROY-fc",
            "--non-interactive",
            "--skip-agents",
            "--skip-hooks",
        ),
        timeout=max(30.0, request.timeout),
        check=False,
    )
    if completed.returncode != 0:
        raise FulcrumError(
            "CLEAN_LEDGER_INIT_FAILED",
            completed.stderr.strip() or completed.stdout.strip(),
            exit_code=4,
            retryable=True,
        )
    ledger = Ledger(brain, executable=executable, timeout=request.timeout)
    ledger.run(("status",))
    return ledger


def _ensure_staging_anchor(reset_root: Path, staging: Path, timeout: float) -> None:
    if _remote_refs(str(staging), timeout).get("refs/heads/main"):
        return
    head = _run(
        ("git", "-C", str(reset_root), "rev-parse", "--verify", "HEAD"),
        check=False,
    )
    if head.returncode != 0:
        _run(
            (
                "git",
                "-C",
                str(reset_root),
                "commit",
                "--allow-empty",
                "-m",
                "fulcrum reset staging anchor",
            )
        )
    _run(
        (
            "git",
            "-C",
            str(reset_root),
            "push",
            str(staging),
            "HEAD:refs/heads/main",
        ),
        timeout=timeout,
    )


def _replace_remote_ledger(
    request: ParsedRequest,
    config: Mapping[str, Any],
    reset_root: Path,
    target: Mapping[str, Any],
    inventory: Mapping[str, Any],
) -> dict[str, Any]:
    assert request.instance.brain_root is not None
    brain = request.instance.brain_root
    ledger = _configured_ledger(request, config)
    staging = reset_root / "clean-ledger-remote.git"
    _ensure_staging_anchor(reset_root, staging, request.timeout)
    staging_transport = _git_transport(str(staging))
    remotes = ledger.run(("dolt", "remote", "list")).value
    names = (
        {
            str(item.get("name"))
            for item in remotes
            if isinstance(item, Mapping) and item.get("name")
        }
        if isinstance(remotes, list)
        else set()
    )
    if "origin" in names:
        ledger.run(("dolt", "remote", "remove", "origin"), mutating=True)
    ledger.run(("dolt", "remote", "add", "origin", staging_transport), mutating=True)
    pushed = _run(
        (
            ledger.executable,
            "-C",
            str(brain),
            "dolt",
            "push",
            "--force",
            "--remote",
            "origin",
        ),
        timeout=max(30.0, request.timeout),
        check=False,
    )
    if pushed.returncode != 0:
        raise RuntimeError(pushed.stderr.strip() or pushed.stdout.strip())
    new_oid = _remote_refs(str(staging), request.timeout).get("refs/dolt/data")
    if not new_oid:
        raise RuntimeError("staging remote did not expose clean refs/dolt/data")
    before = target.get("before")
    before = before if isinstance(before, Mapping) else {}
    actual_remote = str(before["ordinary_remote"])
    expected = before.get("expected_oid")
    current = _remote_refs(actual_remote, request.timeout).get("refs/dolt/data")
    if current != new_oid:
        if current != expected:
            raise FulcrumError(
                "REMOTE_REF_CHANGED",
                "refs/dolt/data changed after reset inventory",
                exit_code=5,
                details={"expected": expected, "observed": current, "new": new_oid},
            )
        local_ref = f"refs/fulcrum-reset/{operation_id(request.request_id or '')}"
        try:
            _run(
                (
                    "git",
                    "-C",
                    str(brain),
                    "fetch",
                    str(staging),
                    f"refs/dolt/data:{local_ref}",
                ),
                timeout=request.timeout,
            )
            replaced = _run(
                (
                    "git",
                    "-C",
                    str(brain),
                    "push",
                    f"--force-with-lease=refs/dolt/data:{expected or ''}",
                    actual_remote,
                    f"{local_ref}:refs/dolt/data",
                ),
                timeout=max(30.0, request.timeout),
                check=False,
            )
            if replaced.returncode != 0:
                raise RuntimeError(replaced.stderr.strip() or replaced.stdout.strip())
        finally:
            _run(("git", "-C", str(brain), "update-ref", "-d", local_ref), check=False)
    observed = _remote_refs(actual_remote, request.timeout)
    if observed.get("refs/dolt/data") != new_oid:
        raise RuntimeError("remote ledger replacement was not observed")
    unrelated = before.get("unrelated_refs")
    unrelated = unrelated if isinstance(unrelated, Mapping) else {}
    changed = {
        name: {"before": oid, "after": observed.get(str(name))}
        for name, oid in unrelated.items()
        if observed.get(str(name)) != oid
    }
    if changed:
        raise RuntimeError(f"remote replacement changed unrelated refs: {changed}")
    ledger.run(("dolt", "remote", "remove", "origin"), mutating=True)
    ledger.run(
        ("dolt", "remote", "add", "origin", _git_transport(actual_remote)),
        mutating=True,
    )
    proof = _fresh_remote_proof(
        ledger.executable,
        actual_remote,
        inventory.get("old_ledger_record_ids", []),
        request.timeout,
    )
    return {
        "ref": "refs/dolt/data",
        "old_oid": expected,
        "new_oid": new_oid,
        "remote_contains_old_records": False,
        "unrelated_refs_unchanged": True,
        "fresh_clone": proof,
    }


def _fresh_remote_proof(
    executable: str, remote: str, old_ids: Any, timeout: float
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="fulcrum-reset-inspect-") as directory:
        root = Path(directory)
        _run(("git", "init", "--initial-branch=inspect", str(root)))
        (root / ".beads" / "dolt").mkdir(parents=True, mode=0o700)
        initialized = _run(
            (
                executable,
                "-C",
                str(root),
                "init",
                "--remote",
                _git_transport(remote),
                "--prefix",
                "fc",
                "--non-interactive",
                "--skip-agents",
                "--skip-hooks",
            ),
            timeout=max(30.0, timeout),
            check=False,
        )
        if initialized.returncode != 0:
            raise RuntimeError(initialized.stderr.strip() or initialized.stdout.strip())
        ids = {
            item.id
            for item in Ledger(
                root, executable=executable, timeout=timeout
            ).list_records(limit=0)
        }
        old = (
            {str(item) for item in old_ids if isinstance(item, str)}
            if isinstance(old_ids, list)
            else set()
        )
        retained = sorted(ids.intersection(old))
        if retained:
            raise RuntimeError(f"fresh remote clone retained old records: {retained}")
        return {"records": sorted(ids), "old_records_absent": True}


def _bootstrap_control_record(request: ParsedRequest, ledger: Ledger) -> dict[str, Any]:
    existing = ledger.show("fc-system")
    if existing is None:
        control = ledger.create_record(
            record_id="fc-system",
            kind="control",
            title="Fulcrum control",
            description="Clean leadership and installation binding after hard reset.",
            owner="HUMAN",
            fc={
                "kind": "control",
                "owner": "HUMAN",
                "instance_root": str(request.instance.instance_root),
                "brain_root": str(request.instance.brain_root),
                "vizier_thread": None,
                "marshal_thread": None,
                "vizier_creation_operation": None,
                "marshal_creation_operation": None,
                "active_takeover": None,
                "last_transition": operation_id(request.request_id or ""),
                "reset_request_id": request.request_id,
            },
        )
        created = True
    else:
        control, created = existing, False
    return {
        "control_id": control.id,
        "created": created,
        "vizier_thread": (control.fc or {}).get("vizier_thread"),
        "marshal_thread": (control.fc or {}).get("marshal_thread"),
        "turns_started": 0,
    }


def _write_clean_receipt(
    request: ParsedRequest, ledger: Ledger, planned: Mapping[str, Any]
) -> OperationRecord:
    operation, reused = ledger.create_operation(
        request,
        planned={
            "new_installation": {
                "instance_root": planned.get("instance_root"),
                "brain_root": planned.get("brain_root"),
                "config_path": planned.get("config_path"),
            },
            "accepted_reset_input": accepted_input(request),
        },
        next_action="Record the terminal reset result without old inventory.",
    )
    if reused and operation.operation.get("state") == "completed":
        return operation
    return ledger.update_operation(
        operation,
        state="completed",
        step="hard_reset_completed",
        result={
            "reset": "completed",
            "current_step": "hard_reset_completed",
            "completed_targets": _target_counts(planned),
            "pending_targets": [],
            "new_installation": {
                "instance_root": planned.get("instance_root"),
                "brain_root": planned.get("brain_root"),
                "config_path": planned.get("config_path"),
                "control": _one_target(planned, "clean_bootstrap").get("after"),
                "remote_ledger": _minimal_remote_result(
                    _one_target(planned, "remote_ledger").get("after")
                ),
            },
        },
        next_action="Normal service may start from the clean ledger.",
    )


def _minimal_remote_result(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    return {
        key: source.get(key)
        for key in (
            "ref",
            "new_oid",
            "remote_contains_old_records",
            "unrelated_refs_unchanged",
        )
    }


def _publish_clean_terminal(
    request: ParsedRequest,
    ledger: Ledger,
    inventory: Mapping[str, Any],
    terminal_id: str,
) -> dict[str, Any]:
    pushed = _run(
        (
            ledger.executable,
            "-C",
            str(ledger.workspace),
            "dolt",
            "push",
            "--remote",
            "origin",
        ),
        timeout=max(30.0, request.timeout),
        check=False,
    )
    if pushed.returncode != 0:
        raise FulcrumError(
            "REMOTE_TERMINAL_RECEIPT_FAILED",
            pushed.stderr.strip() or pushed.stdout.strip(),
            exit_code=4,
            retryable=True,
        )
    remote = str(inventory["remote_ledger"]["ordinary_remote"])
    proof = _fresh_remote_proof(
        ledger.executable,
        remote,
        inventory.get("old_ledger_record_ids", []),
        request.timeout,
    )
    if terminal_id not in proof["records"] or "fc-system" not in proof["records"]:
        raise FulcrumError(
            "REMOTE_TERMINAL_RECEIPT_MISSING",
            "fresh remote clone lacks clean control or terminal reset receipt",
            exit_code=4,
            retryable=True,
            details={"proof": proof},
        )
    return {
        "new_oid": _remote_refs(remote, request.timeout).get("refs/dolt/data"),
        "terminal_receipt_observed": True,
        "fresh_clone": proof,
    }


def _optional_operation(ledger: Ledger, identifier: str) -> OperationRecord | None:
    try:
        record = ledger.show(identifier)
    except LedgerFailure:
        return None
    if record is None or record.kind != "operation":
        return None
    return OperationRecord.from_record(record)


def _verify_existing_request(
    operation: OperationRecord, request: ParsedRequest
) -> None:
    fc = operation.operation
    if (
        fc.get("request_id") != request.request_id
        or fc.get("command") != "reset"
        or fc.get("input") != accepted_input(request)
    ):
        raise FulcrumError(
            "REQUEST_CONFLICT",
            "the clean ledger contains a different request under this reset ID",
            exit_code=5,
            operation_id=operation.id,
            request_id=request.request_id,
        )


def _remove_reset_workspace(root: Path) -> None:
    if not root.name.endswith(".reset"):
        raise RuntimeError(f"refusing unexpected reset workspace {root}")
    shutil.rmtree(root)


def _remove_exact_path(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _ordinary_remote(brain: Path, config: Mapping[str, Any]) -> str:
    name = str(config["brain"].get("remote") or "origin")
    completed = _run(("git", "-C", str(brain), "remote", "get-url", name), check=False)
    if completed.returncode != 0 or not completed.stdout.strip():
        raise FulcrumError(
            "REMOTE_UNAVAILABLE",
            completed.stderr.strip() or f"brain Git remote {name} is unavailable",
            exit_code=4,
            retryable=True,
        )
    return completed.stdout.strip()


def _remote_refs(remote: str, timeout: float) -> dict[str, str]:
    completed = _run(
        ("git", "ls-remote", remote), timeout=max(10.0, timeout), check=False
    )
    if completed.returncode != 0:
        raise FulcrumError(
            "REMOTE_UNAVAILABLE",
            completed.stderr.strip() or "remote Git inventory failed",
            exit_code=4,
            retryable=True,
        )
    result: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        oid, separator, reference = line.partition("\t")
        if separator and reference:
            result[reference] = oid
    return result


def _repository_id(project: Mapping[str, Any]) -> str | None:
    delivery = project.get("delivery")
    value = delivery.get("id") if isinstance(delivery, Mapping) else None
    return str(value) if isinstance(value, str) and value else None


def _run(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: float = 30.0,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [str(item) for item in argv],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if check and completed.returncode != 0:
        raise RuntimeError(
            completed.stderr.strip()
            or completed.stdout.strip()
            or f"command failed: {argv[0]}"
        )
    return completed


def _operation_result(operation: OperationRecord) -> CommandResult:
    value = str(operation.operation.get("state", "running"))
    state = (
        CommandState(value)
        if value in CommandState._value2member_map_
        else CommandState.RUNNING
    )
    failure = operation.operation.get("error")
    error = None
    if state in {CommandState.FAILED, CommandState.UNCERTAIN} and isinstance(
        failure, Mapping
    ):
        message = str(failure.get("message") or "hard reset failed")
        for target in failure.get("targets") or []:
            target_error = target.get("error") or {}
            if target_error.get("message"):
                message += (
                    f"; {target['kind']} {target['id']}: {target_error['message']}"
                )
        retryable = bool(failure.get("retryable"))
        error = ErrorInfo(
            code=str(failure.get("code") or "RESET_FAILED"),
            message=message,
            retryable=retryable,
            next_command=(
                (
                    "fulcrum",
                    "reset",
                    "--hard",
                    "--yes",
                    "--request-id",
                    str(operation.operation["request_id"]),
                )
                if retryable
                else None
            ),
            details=dict(failure),
        )
    return CommandResult(
        ok=state not in {CommandState.FAILED, CommandState.UNCERTAIN},
        state=state,
        operation_id=operation.id,
        request_id=str(operation.operation.get("request_id")),
        result=operation_view(operation),
        error=error,
    )
