"""Public disposable-fixture and deterministic scenario controls."""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
import uuid
from dataclasses import asdict
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from fulcrum.configuration import ConfigurationManager, ProjectService, ROLES
from fulcrum.contracts import CommandResult, CommandState, FulcrumError, ParsedRequest
from fulcrum.deterministic import (
    ProviderState,
    advance_clock,
    arm_fault,
    emit_provider_event,
    initial_provider_state,
    provider_observation,
)
from fulcrum.install import inspect_service
from fulcrum.installation_service import (
    ServiceService,
    _stop_one,
    load_installed_services,
)
from fulcrum.ledger import Ledger, OperationRecord, operation_id, operation_view
from fulcrum.setup import run_setup

TERMINAL_STATES = {"completed", "failed", "cancelled", "uncertain"}


class FixtureService:
    def create(self, request: ParsedRequest) -> CommandResult:
        _require_human(request)
        supplied = dict(request.input)
        root = _new_fixture_root(supplied.get("root"))
        model = _string(supplied.get("model") or "gpt-5.6-luna", "model")
        effort = _string(supplied.get("effort") or "low", "effort")
        capacity = supplied.get("capacity", 4)
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 1:
            raise FulcrumError.invalid("INVALID_INPUT", "capacity must be positive")
        source_sync = supplied.get("source_sync")
        if not isinstance(source_sync, bool):
            raise FulcrumError.invalid(
                "INVALID_INPUT", "source_sync must be explicitly true or false"
            )
        _provider_kind(supplied.get("runtime"), "runtime")
        _provider_kind(supplied.get("delivery"), "delivery")
        existing = _existing_fixture_receipt(request, root)
        if existing is not None and existing.operation.get("state") in TERMINAL_STATES:
            return _operation_result(existing)
        if root.exists() and existing is None:
            raise FulcrumError(
                "FIXTURE_ROOT_OWNERSHIP_UNKNOWN",
                "fixture root already exists without this exact completed creation receipt",
                exit_code=5,
                details={"root": str(root)},
            )
        try:
            if existing is None:
                paths = _scaffold_fixture(root)
                state_path = root / "providers" / "state.json"
                ProviderState(str(state_path)).initialize(
                    initial_provider_state(models={model: [effort]})
                )
                port = _free_port()
                models = {role: {"model": model, "effort": effort} for role in ROLES}
                setup_request = replace(
                    request,
                    command=("setup",),
                    request_id=_derived_request_id(request, "fixture-setup"),
                    input={
                        "brain": {
                            "root": str(paths["brain"]),
                            "remote": "origin",
                        },
                        "runtime": {
                            "kind": "deterministic",
                            "endpoint": str(state_path),
                            "executable": None,
                        },
                        "delivery": {
                            "kind": "deterministic",
                            "endpoint": str(state_path),
                            "executable": None,
                        },
                        "beads": {"host": "127.0.0.1", "port": port},
                        "models": models,
                        "policy": {
                            "automatic_capacity": capacity,
                            "default_project_capacity": capacity,
                            "project_capacity": {"fixture": capacity},
                            "paused_projects": [],
                            "suspended_rules": {},
                            "rationale": "Explicit disposable fixture capacity.",
                        },
                        "knowledge": {
                            "root": str(paths["brain"]),
                            "remote": "origin",
                            "branch": None,
                            "require_remote_sync": True,
                        },
                        "source_watch_root": None,
                    },
                    offline=True,
                )
                setup = run_setup(setup_request)
                ServiceService().stop(
                    replace(
                        request,
                        command=("service", "stop"),
                        request_id=_derived_request_id(
                            request, "fixture-controller-stop"
                        ),
                        input={},
                        arguments={},
                        offline=True,
                    )
                )
                ledger = Ledger(paths["brain"], timeout=request.timeout)
                operation, _ = ledger.create_operation(
                    request,
                    planned={
                        "fixture_root": str(root),
                        "owned_paths": [str(path) for path in paths.values()],
                        "provider_state": str(state_path),
                        "beads_port": port,
                        "setup_operation": setup.operation_id,
                    },
                    next_action="Enroll the committed toy project through the normal project operation.",
                )
                setup_operation = setup.operation_id
            else:
                paths = _fixture_paths(root)
                planned = existing.operation.get("planned") or {}
                state_path = Path(str(planned["provider_state"]))
                port = int(planned["beads_port"])
                setup_operation = str(
                    planned.get("setup_operation")
                    or operation_id(_derived_request_id(request, "fixture-setup"))
                )
                ProviderState(str(state_path)).read()
                ledger = Ledger(paths["brain"], timeout=request.timeout)
                operation = existing
            project_request = replace(
                request,
                command=("project", "add"),
                request_id=_derived_request_id(request, "fixture-project-add"),
                project="fixture",
                input={
                    "id": "fixture",
                    "root": str(paths["project"]),
                    "codex_project_id": "fixture-project-1",
                    "delivery": {
                        "id": "fixture-repository-1",
                        "registration": "created",
                    },
                    "integration_branch": "main",
                    "prepare_argv": [],
                    "validate_argv": [],
                    "enabled": True,
                    "source_remote": "origin",
                    "require_source_sync": source_sync,
                    "models": {},
                },
                offline=True,
            )
            project_result = ProjectService().add(project_request)
            ProviderState(str(state_path)).mutate(
                lambda state: state.setdefault("projects", {}).update(
                    {
                        "fixture-project-1": {
                            "id": "fixture-project-1",
                            "name": "fixture",
                            "root": str(paths["project"]),
                            "operation_id": project_result.operation_id,
                        }
                    }
                )
            )
            inventory = {
                "fixture_id": operation.id,
                "root": str(root),
                "instance": str(root / "instance"),
                "config": str(paths["brain"] / "fulcrum.yaml"),
                "brain": str(paths["brain"]),
                "project_id": "fixture",
                "project_root": str(paths["project"]),
                "remote": str(paths["brain_remote"]),
                "source_remote": str(paths["source_remote"]),
                "provider_state": str(state_path),
                "provider_ids": {
                    "runtime_project": "fixture-project-1",
                    "delivery_repository": "fixture-repository-1",
                },
                "beads_port": port,
                "model": model,
                "effort": effort,
                "capacity": capacity,
                "source_sync": source_sync,
                "service_started": False,
                "setup_operation": setup_operation,
                "project_operation": project_result.operation_id,
            }
            operation = ledger.update_operation(
                operation,
                state="completed",
                step="fixture_ready_controller_stopped",
                result=inventory,
                next_action="Prepare scenario state, then explicitly start fixture service.",
            )
            return _operation_result(operation)
        except Exception as error:
            raise FulcrumError(
                "FIXTURE_CREATE_FAILED",
                str(error),
                exit_code=4,
                retryable=False,
                details={
                    "created_root": str(root),
                    "cleanup_command": [
                        "fulcrum",
                        "--instance",
                        str(root / "instance"),
                        "fixture",
                        "cleanup",
                        operation_id(request.request_id or ""),
                        "--yes",
                        "--json",
                    ],
                },
            ) from error

    def show(self, request: ParsedRequest) -> CommandResult:
        operation = _fixture_operation(request, str(request.arguments["id"]))
        result = dict(operation.operation.get("result") or {})
        endpoint = result.get("provider_state")
        gaps: list[dict[str, Any]] = []
        provider: Mapping[str, Any] | None = None
        if isinstance(endpoint, str):
            try:
                provider = provider_observation(endpoint)
            except Exception as error:
                gaps.append({"component": "provider", "reason": str(error)})
        ledger = _ledger(request)
        pending = [
            record.id
            for record in ledger.list_records(kind="operation", limit=0)
            if (record.fc or {}).get("state") not in TERMINAL_STATES
        ]
        services = {
            name: asdict(inspect_service(service.label))
            for name, service in load_installed_services(
                request.instance.instance_root
            ).items()
        }
        return CommandResult.query(
            {
                "fixture": result,
                "creation": operation_view(operation),
                "provider": provider,
                "services": services,
                "pending_operations": pending,
                "gaps": gaps,
            }
        )

    def cleanup(self, request: ParsedRequest) -> CommandResult:
        _require_human(request)
        if not request.arguments.get("yes"):
            raise FulcrumError.invalid(
                "CONFIRMATION_REQUIRED", "fixture cleanup requires --yes"
            )
        creation = _fixture_operation(request, str(request.arguments["id"]))
        inventory = creation.operation.get("result")
        if not isinstance(inventory, Mapping):
            raise FulcrumError.invalid("FIXTURE_INVALID", "fixture has no inventory")
        root = Path(str(inventory.get("root") or "")).resolve(strict=False)
        expected_instance = root / "instance"
        if (
            root == Path.home().resolve(strict=True)
            or root == Path("/")
            or request.instance.instance_root.resolve(strict=False) != expected_instance
            or operation_id(str((creation.fc or {}).get("request_id"))) != creation.id
        ):
            raise FulcrumError(
                "FIXTURE_SCOPE_REFUSED",
                "cleanup target is not the exact selected disposable fixture",
                exit_code=5,
            )
        endpoint = str(inventory["provider_state"])
        provider = provider_observation(endpoint)
        runtime_tasks: list[str] = list(provider["runtime"].get("tasks", {}).keys())
        from fulcrum.deterministic import DeterministicRuntime

        runtime: DeterministicRuntime = DeterministicRuntime(endpoint)

        async def remove_tasks() -> list[str]:
            removed: list[str] = []
            for thread_id in runtime_tasks:
                facts = await runtime.inspect_task(thread_id)
                if facts.active_turn:
                    await runtime.interrupt(thread_id, facts.active_turn)
                if facts.exists:
                    await runtime.delete(thread_id)
                removed.append(thread_id)
            return removed

        import asyncio

        removed_tasks = asyncio.run(remove_tasks())
        stopped: list[dict[str, Any]] = []
        services = load_installed_services(request.instance.instance_root)
        failures: list[str] = []
        for name in ("updater", "controller", "runtime", "dolt"):
            service = services.get(name)
            if service is None:
                continue
            try:
                stopped.append(_stop_one(service))
            except Exception as error:
                failures.append(f"{name}: {error}")
        if failures:
            return CommandResult(
                ok=False,
                state=CommandState.FAILED,
                request_id=request.request_id,
                result={
                    "fixture_id": creation.id,
                    "root": str(root),
                    "removed_tasks": removed_tasks,
                    "services": stopped,
                    "cleanup_failures": failures,
                    "root_removed": False,
                },
            )
        shutil.rmtree(root)
        return CommandResult(
            ok=True,
            state=CommandState.COMPLETED,
            request_id=request.request_id,
            operation_id=operation_id(request.request_id or ""),
            result={
                "fixture_id": creation.id,
                "root": str(root),
                "removed_tasks": removed_tasks,
                "services": stopped,
                "cleanup_failures": [],
                "root_removed": not root.exists(),
            },
        )


class ScenarioService:
    def emit(self, request: ParsedRequest) -> CommandResult:
        return self._effect(
            request,
            lambda endpoint: emit_provider_event(endpoint, request.input),
            "provider_event_emitted",
        )

    def advance(self, request: ParsedRequest) -> CommandResult:
        seconds = request.arguments.get("seconds")
        if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
            raise FulcrumError.invalid("INVALID_INPUT", "--seconds is required")
        return self._effect(
            request,
            lambda endpoint: advance_clock(endpoint, float(seconds)),
            "fixture_clock_advanced",
        )

    def fault(self, request: ParsedRequest) -> CommandResult:
        return self._effect(
            request,
            lambda endpoint: arm_fault(endpoint, request.input),
            "provider_fault_armed",
        )

    def crash(self, request: ParsedRequest) -> CommandResult:
        target_operation: str = str(request.arguments.get("operation") or "")
        boundary: str = str(request.arguments.get("boundary") or "")
        allowed = {
            "receipt_created",
            "work_written",
            "task_created",
            "turn_started",
            "handoff_owner_written",
            "delivery_effect_observed",
            "publication_pushed",
            "reset_inventory_recorded",
            "reset_old_ledger_removed",
            "reset_remote_replaced",
            "reset_terminal_written",
        }
        if not target_operation or boundary not in allowed:
            raise FulcrumError.invalid(
                "INVALID_CRASH_BOUNDARY",
                "operation and a supported boundary are required",
            )

        def arm(endpoint: str) -> dict[str, Any]:
            row = {
                "operation_id": target_operation,
                "boundary": boundary,
                "armed_by": operation_id(request.request_id or ""),
                "consumed": False,
            }
            ProviderState(endpoint).mutate(lambda state: state["crashes"].append(row))
            return row

        return self._effect(request, arm, "controller_crash_armed")

    def _effect(
        self,
        request: ParsedRequest,
        action: Any,
        step: str,
    ) -> CommandResult:
        config = _deterministic_config(request)
        endpoint = str(config["runtime"]["endpoint"])
        ledger = _ledger(request)
        operation, reused = ledger.create_operation(
            request,
            planned={
                "input": dict(request.input),
                "arguments": dict(request.arguments),
            },
            next_action="Apply one deterministic external-provider control.",
        )
        if reused and operation.operation.get("state") in TERMINAL_STATES:
            return _operation_result(operation)
        try:
            result = action(endpoint)
        except (RuntimeError, ValueError) as error:
            operation = ledger.update_operation(
                operation,
                state="failed",
                step=f"{step}_rejected",
                error={
                    "code": "SCENARIO_REJECTED",
                    "message": str(error),
                    "retryable": False,
                },
                next_action="Correct the exact fixture provider control input.",
            )
            return _operation_result(operation)
        operation = ledger.update_operation(
            operation,
            state="completed",
            step=step,
            result={"control": result, "provider_state": endpoint},
            next_action="Observe workflow consequences through public Fulcrum commands.",
        )
        return _operation_result(operation)


class BarrierService:
    def prepare(self, request: ParsedRequest) -> CommandResult:
        participants = request.input.get("participants")
        if not isinstance(participants, list) or not all(
            isinstance(item, str) and item for item in participants
        ):
            raise FulcrumError.invalid(
                "INVALID_INPUT", "participants must be an array of task IDs"
            )
        return self._mutate(
            request,
            lambda barriers, key: _prepare_barrier(barriers, key, participants),
            "barrier_prepared",
        )

    def arrive(self, request: ParsedRequest) -> CommandResult:
        participant = str(request.arguments.get("participant") or "")
        if not participant:
            raise FulcrumError.invalid("INVALID_INPUT", "participant is required")
        result = self._mutate(
            request,
            lambda barriers, key: _arrive_barrier(barriers, key, participant),
            "barrier_arrived",
        )
        if not request.wait:
            return result
        endpoint = _provider_endpoint(request)
        key = str(request.arguments["name"])
        deadline = time.monotonic() + request.timeout
        while time.monotonic() < deadline:
            barrier = provider_observation(endpoint)["barriers"].get(key)
            if isinstance(barrier, Mapping) and barrier.get("released"):
                return CommandResult(
                    ok=True,
                    state=CommandState.COMPLETED,
                    operation_id=result.operation_id,
                    request_id=result.request_id,
                    result={"barrier": dict(barrier), "waited": True},
                )
            time.sleep(0.05)
        raise FulcrumError(
            "WAIT_TIMEOUT",
            "barrier arrival is durable but release was not observed before timeout",
            exit_code=3,
            retryable=True,
            operation_id=result.operation_id,
            details={"fixture_id": request.arguments["id"], "name": key},
        )

    def release(self, request: ParsedRequest) -> CommandResult:
        _require_human(request)
        return self._mutate(
            request,
            lambda barriers, key: _release_barrier(barriers, key),
            "barrier_released",
        )

    def show(self, request: ParsedRequest) -> CommandResult:
        _fixture_operation(request, str(request.arguments["id"]))
        endpoint = _provider_endpoint(request)
        key = str(request.arguments["name"])
        barrier = provider_observation(endpoint)["barriers"].get(key)
        if not isinstance(barrier, Mapping):
            raise FulcrumError.invalid("NOT_FOUND", f"unknown fixture barrier {key}")
        return CommandResult.query(
            {"fixture_id": request.arguments["id"], "name": key, **barrier}
        )

    def _mutate(self, request: ParsedRequest, action: Any, step: str) -> CommandResult:
        _fixture_operation(request, str(request.arguments["id"]))
        endpoint = _provider_endpoint(request)
        ledger = _ledger(request)
        operation, reused = ledger.create_operation(
            request,
            planned={
                "fixture_id": request.arguments["id"],
                "name": request.arguments["name"],
            },
            next_action="Apply one exact external fixture barrier mutation.",
        )
        if reused and operation.operation.get("state") in TERMINAL_STATES:
            return _operation_result(operation)
        try:
            result = ProviderState(endpoint).mutate(
                lambda state: action(state["barriers"], str(request.arguments["name"]))
            )
        except ValueError as error:
            raise FulcrumError.invalid("BARRIER_CONFLICT", str(error)) from error
        operation = ledger.update_operation(
            operation,
            state="completed",
            step=step,
            result={"barrier": result},
            next_action="Observe or release the external fixture barrier.",
        )
        return _operation_result(operation)


def _prepare_barrier(
    barriers: dict[str, Any], key: str, participants: list[str]
) -> dict[str, Any]:
    expected = sorted(set(participants))
    existing = barriers.get(key)
    if isinstance(existing, Mapping):
        if existing.get("expected") != expected:
            raise ValueError("barrier membership differs from its retained expectation")
        return dict(existing)
    barrier = {"expected": expected, "arrived": [], "released": False}
    barriers[key] = barrier
    return dict(barrier)


def _arrive_barrier(
    barriers: dict[str, Any], key: str, participant: str
) -> dict[str, Any]:
    barrier = barriers.get(key)
    if not isinstance(barrier, dict):
        raise ValueError("barrier is not prepared")
    if participant not in barrier["expected"]:
        raise ValueError("participant is not expected at this barrier")
    arrived = set(barrier["arrived"])
    arrived.add(participant)
    barrier["arrived"] = sorted(arrived)
    barrier["ready"] = arrived == set(barrier["expected"])
    return dict(barrier)


def _release_barrier(barriers: dict[str, Any], key: str) -> dict[str, Any]:
    barrier = barriers.get(key)
    if not isinstance(barrier, dict):
        raise ValueError("barrier is not prepared")
    barrier["released"] = True
    return dict(barrier)


def _scaffold_fixture(root: Path) -> dict[str, Path]:
    root.mkdir(parents=False, mode=0o700)
    brain = root / "brain"
    project = root / "project"
    remotes = root / "remotes"
    brain_remote = remotes / "brain.git"
    source_remote = remotes / "source.git"
    remotes.mkdir()
    for repository in (brain, project):
        repository.mkdir()
        _run("git", "init", "--initial-branch=main", str(repository))
        _run("git", "-C", str(repository), "config", "user.name", "Fulcrum Fixture")
        _run("git", "-C", str(repository), "config", "user.email", "fixture@invalid")
    (brain / "README.md").write_text("# Disposable Fulcrum fixture\n", encoding="utf-8")
    _run("git", "-C", str(brain), "add", "README.md")
    _run("git", "-C", str(brain), "commit", "-m", "chore: initialize fixture brain")
    (project / "fixture.txt").write_text("initial fixture source\n", encoding="utf-8")
    _run("git", "-C", str(project), "add", "fixture.txt")
    _run("git", "-C", str(project), "commit", "-m", "chore: initialize fixture source")
    _run("git", "init", "--bare", "--initial-branch=main", str(brain_remote))
    _run("git", "init", "--bare", "--initial-branch=main", str(source_remote))
    _run("git", "-C", str(brain), "remote", "add", "origin", str(brain_remote))
    _run("git", "-C", str(project), "remote", "add", "origin", str(source_remote))
    _run("git", "-C", str(brain), "push", "-u", "origin", "main")
    _run("git", "-C", str(project), "push", "-u", "origin", "main")
    return {
        "brain": brain,
        "project": project,
        "brain_remote": brain_remote,
        "source_remote": source_remote,
    }


def _fixture_paths(root: Path) -> dict[str, Path]:
    return {
        "brain": root / "brain",
        "project": root / "project",
        "brain_remote": root / "remotes" / "brain.git",
        "source_remote": root / "remotes" / "source.git",
    }


def _new_fixture_root(value: Any) -> Path:
    if not isinstance(value, str) or not value:
        raise FulcrumError.invalid("INVALID_INPUT", "fixture root is required")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise FulcrumError.invalid("INVALID_PATH", "fixture root must be absolute")
    root = path.resolve(strict=False)
    home = Path.home().resolve(strict=True)
    if root in {Path("/"), home} or home.is_relative_to(root):
        raise FulcrumError.invalid(
            "UNSAFE_FIXTURE_ROOT", "fixture root cannot contain the home directory"
        )
    return root


def _existing_fixture_receipt(
    request: ParsedRequest, root: Path
) -> OperationRecord | None:
    config = root / "brain" / "fulcrum.yaml"
    if not config.is_file() or request.request_id is None:
        return None
    try:
        record = Ledger(root / "brain", timeout=request.timeout).show(
            operation_id(request.request_id)
        )
    except Exception:
        return None
    if record is None or record.kind != "operation":
        return None
    operation = OperationRecord.from_record(record)
    if operation.operation.get("command") != "fixture.create":
        return None
    if operation.operation.get("input", {}).get("input") != dict(request.input):
        raise FulcrumError(
            "REQUEST_CONFLICT",
            f"request ID {request.request_id} was already used with different input",
            exit_code=5,
            operation_id=operation.id,
        )
    return operation


def _fixture_operation(request: ParsedRequest, fixture_id: str) -> OperationRecord:
    record = _ledger(request).show(fixture_id)
    if record is None or record.kind != "operation":
        raise FulcrumError.invalid("NOT_FOUND", f"unknown fixture {fixture_id}")
    operation = OperationRecord.from_record(record)
    if operation.operation.get("command") != "fixture.create":
        raise FulcrumError.invalid(
            "WRONG_RECORD_KIND", f"{fixture_id} is not a fixture"
        )
    return operation


def _deterministic_config(request: ParsedRequest) -> dict[str, Any]:
    manager = ConfigurationManager(request.instance.config_path)
    document, _ = manager.load()
    config = manager.effective(document)
    if (
        config["runtime"].get("kind") != "deterministic"
        or config["delivery"].get("kind") != "deterministic"
    ):
        raise FulcrumError(
            "TEST_CONTROL_DENIED",
            "scenario controls require deterministic runtime and delivery adapters",
            exit_code=5,
        )
    return config


def _provider_endpoint(request: ParsedRequest) -> str:
    return str(_deterministic_config(request)["runtime"]["endpoint"])


def _provider_kind(value: Any, field: str) -> None:
    if not isinstance(value, Mapping) or value.get("kind") != "deterministic":
        raise FulcrumError.invalid(
            "INVALID_INPUT", f"{field}.kind must explicitly be deterministic"
        )


def _ledger(request: ParsedRequest) -> Ledger:
    if request.instance.brain_root is None:
        raise FulcrumError(
            "LEDGER_UNAVAILABLE", "fixture requires a brain", exit_code=4
        )
    manager = ConfigurationManager(request.instance.config_path)
    document, _ = manager.load()
    config = manager.effective(document)
    executable = config["beads"].get("executable")
    return Ledger(
        request.instance.brain_root,
        executable=str(executable) if executable else None,
        timeout=request.timeout,
    )


def _operation_result(operation: OperationRecord) -> CommandResult:
    state_value = str(operation.operation.get("state") or "running")
    state = (
        CommandState(state_value)
        if state_value in CommandState._value2member_map_
        else CommandState.RUNNING
    )
    return CommandResult(
        ok=state
        not in {CommandState.FAILED, CommandState.UNCERTAIN, CommandState.CANCELLED},
        state=state,
        operation_id=operation.id,
        request_id=str(operation.operation.get("request_id")),
        result=operation_view(operation),
    )


def _require_human(request: ParsedRequest) -> None:
    if request.actor.kind != "human":
        raise FulcrumError(
            "FIXTURE_AUTHORITY_DENIED",
            "fixture lifecycle requires a human test client",
            exit_code=5,
        )


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise FulcrumError.invalid(
            "INVALID_INPUT", f"{field} must be a non-empty string"
        )
    return value


def _derived_request_id(request: ParsedRequest, name: str) -> str:
    if request.request_id is None:
        raise ValueError("fixture mutation requires a request ID")
    return str(uuid.uuid5(uuid.UUID(request.request_id), name))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _run(*argv: str) -> str:
    completed = subprocess.run(
        argv, capture_output=True, text=True, check=False, timeout=60
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
    return completed.stdout.strip()
