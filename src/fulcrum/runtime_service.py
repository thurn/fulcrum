"""Public runtime and managed-task operations over the typed Codex adapter."""

from __future__ import annotations

from fulcrum.timing import timed

from fulcrum.coordination import unlocked

from fulcrum.coordination import coordinated

import asyncio
import concurrent.futures
import os
import shlex
import shutil
import subprocess
from collections.abc import Callable, Coroutine, Mapping, Sequence
from pathlib import Path
from typing import Any, TypeVar, cast

from fulcrum.configuration import ConfigurationManager, ROLES
from fulcrum.contracts import CommandResult, CommandState, FulcrumError, ParsedRequest
from fulcrum.ledger import (
    Ledger,
    LedgerRecord,
    OperationRecord,
    operation_id,
    operation_view,
    random_record_id,
    utc_now,
)
from fulcrum.runtime import (
    AppServerError,
    AppServerRuntime,
    RuntimeCapabilities,
    Runtime,
    TaskFacts,
    TaskSpec,
    TurnFacts,
    TurnInput,
)


def terminal_stop_command(
    thread_id: str, ownership_operation: str, *, reason: str
) -> str:
    """Render the owner-qualified all-terminal command used by role formulas."""

    return shlex.join(
        [
            "fulcrum",
            "task",
            "terminal",
            "stop",
            thread_id,
            "--ownership-operation",
            ownership_operation,
            "--all-owned",
            "--reason",
            reason,
            "--json",
        ]
    )


T = TypeVar("T")


def routing_developer_instructions(
    request: ParsedRequest, extra: str | None = None
) -> str:
    executable = (
        request.instance.instance_root / "runtime" / "current" / "bin" / "fulcrum"
    ).resolve(strict=False)
    instance = request.instance.instance_root.resolve(strict=False)
    routing = (
        "Fulcrum command routing: this task belongs only to the selected instance "
        f"at {instance}. Run workflow commands through {executable} with "
        f"`--instance {instance}`. Preserve the native CODEX_THREAD_ID actor "
        "identity; do not use a production/default Fulcrum instance."
    )
    return routing if not extra else f"{routing}\n\n{extra}"


class RuntimeService:
    def launch_desktop(self, request: ParsedRequest) -> CommandResult:
        manager = ConfigurationManager(request.instance.config_path)
        endpoint = manager.desktop_endpoint()
        candidates = (
            Path("/Applications/Codex.app/Contents/MacOS/Codex"),
            Path("/Applications/ChatGPT.app/Contents/MacOS/ChatGPT"),
        )
        executable = next(
            (path for path in candidates if path.is_file()),
            None,
        )
        if executable is None:
            opener = shutil.which("open")
            if opener is None:
                raise FulcrumError(
                    "DESKTOP_UNAVAILABLE",
                    "no Codex/ChatGPT Desktop executable is installed",
                    exit_code=4,
                )
            argv = [opener, "-a", "ChatGPT"]
        else:
            argv = [str(executable.resolve(strict=True))]
        environment = dict(os.environ)
        environment["CODEX_APP_SERVER_WS_URL"] = endpoint
        # Desktop also stores local sidebar state outside the app-server. An
        # empty/relative home can select a different profile from the runtime.
        codex_home = Path(
            environment.get("CODEX_HOME") or Path.home() / ".codex"
        ).expanduser()
        if not codex_home.is_absolute():
            raise FulcrumError(
                "INVALID_CODEX_HOME",
                "CODEX_HOME must be absolute; unset it to use ~/.codex",
                exit_code=2,
            )
        environment["CODEX_HOME"] = str(codex_home)
        try:
            process = subprocess.Popen(
                argv,
                env=environment,
                cwd=Path.home(),
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            raise FulcrumError(
                "DESKTOP_LAUNCH_FAILED", str(error), exit_code=4, retryable=True
            ) from error
        # Desktop is a local debugging entry point, independent of controller
        # maintenance and workflow storage availability.
        return CommandResult(
            ok=True,
            state=CommandState.COMPLETED,
            request_id=request.request_id,
            result={
                "launched": True,
                "pid": process.pid,
                "endpoint": endpoint,
                "codex_home": str(codex_home),
                "attachment": {
                    "state": "unknown",
                    "reason": (
                        "the native protocol does not expose proof that this Desktop "
                        "process attached to the selected endpoint"
                    ),
                },
                "separate_runtime_terminated": False,
            },
        )

    @coordinated
    def capabilities(self, request: ParsedRequest) -> CommandResult:
        try:
            capabilities = _runtime_call(
                request, lambda runtime: runtime.capabilities()
            )
        except FulcrumError as error:
            if error.code != "RUNTIME_UNAVAILABLE":
                raise
            capabilities = RuntimeCapabilities(
                available=False,
                endpoint=_runtime_endpoint(request),
                methods=AppServerRuntime.METHODS,
                models={},
                gaps=(error.message,),
            )
        return CommandResult.query(capabilities.to_dict())

    @coordinated
    def status(self, request: ParsedRequest) -> CommandResult:
        observed_at = utc_now()
        try:
            capabilities, resources = _runtime_call(
                request,
                lambda runtime: _capabilities_and_resources(runtime),
            )
            return CommandResult.query(
                {
                    "observed_at": observed_at,
                    "endpoint": _runtime_endpoint(request),
                    "available": True,
                    "capabilities": capabilities.to_dict(),
                    "resources": resources.to_dict(),
                    "gaps": [],
                }
            )
        except FulcrumError as error:
            if error.code != "RUNTIME_UNAVAILABLE":
                raise
            return CommandResult.query(
                {
                    "observed_at": observed_at,
                    "endpoint": _runtime_endpoint(request),
                    "available": False,
                    "capabilities": None,
                    "resources": None,
                    "gaps": [{"category": "unavailable", "reason": error.message}],
                }
            )


class TaskService:
    @coordinated
    def list(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        records = ledger.list_records(kind="task", limit=0)
        records.sort(key=lambda item: item.id)
        limit = _limit(request.arguments.get("limit"))
        cursor = request.arguments.get("cursor")
        if cursor is not None:
            records = [item for item in records if item.id > str(cursor)]
        selected = records if limit == 0 else records[:limit]
        observed: dict[str, TaskFacts] = {}
        gaps: list[dict[str, Any]] = []
        try:
            observed = _runtime_call(
                request,
                lambda runtime: _inspect_many(
                    runtime,
                    [str(item.fc.get("thread_id")) for item in selected if item.fc],
                ),
            )
        except FulcrumError as error:
            gaps.append({"component": "runtime", "reason": error.message})
        rows = [_task_view(item, observed.get(_thread_id(item))) for item in selected]
        return CommandResult.query(
            {
                "items": rows,
                "next_cursor": (
                    selected[-1].id
                    if selected and len(selected) < len(records)
                    else None
                ),
                "observed_at": utc_now(),
                "gaps": gaps,
            }
        )

    @coordinated
    def show(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        record = _find_task(ledger, str(request.arguments["id"]))
        facts: TaskFacts | None = None
        gaps: list[dict[str, Any]] = []
        try:
            facts = _runtime_call(
                request, lambda runtime: runtime.inspect_task(_thread_id(record))
            )
        except FulcrumError as error:
            gaps.append({"component": "runtime", "reason": error.message})
        view = _task_view(record, facts)
        view["gaps"] = gaps + list(view.get("gaps", []))
        return CommandResult.query(view)

    @coordinated
    def start(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        bead_id = str(request.arguments["bead"])
        role = str(request.arguments["role"])
        if role not in ROLES:
            raise FulcrumError.invalid("INVALID_ROLE", f"unknown role {role}")
        work = ledger.show(bead_id)
        if work is None or work.kind != "work" or not work.fc:
            raise FulcrumError.invalid("NOT_FOUND", f"unknown work {bead_id}")
        if work.status == "closed":
            raise FulcrumError(
                "OWNERSHIP_CONFLICT",
                "closed work must be explicitly reopened",
                exit_code=5,
            )
        dispatch = work.fc.get("dispatch")
        if isinstance(dispatch, Mapping) and dispatch.get("role") != role:
            raise FulcrumError(
                "ROLE_MISMATCH",
                "task role differs from the durable dispatch authorization",
                exit_code=5,
                details={
                    "bead_id": bead_id,
                    "authorized_role": dispatch.get("role"),
                    "observed_role": role,
                    "decision_operation": dispatch.get("decision_operation"),
                },
            )
        assert request.request_id is not None
        receipt_id = operation_id(request.request_id)
        if (
            work.fc.get("owner")
            not in {
                "HUMAN",
                _marshal_owner(ledger),
            }
            and work.fc.get("ownership_operation") != receipt_id
        ):
            raise FulcrumError(
                "OWNERSHIP_CONFLICT",
                "task start cannot replace an existing worker; use controlled transfer",
                exit_code=5,
                details={"owner": work.fc.get("owner")},
            )
        instructions = request.input.get("instructions")
        if not isinstance(instructions, str) or not instructions.strip():
            raise FulcrumError.invalid(
                "INVALID_INPUT", "task start requires full instructions"
            )
        associated_beads = request.input.get("associated_beads", [])
        if not isinstance(associated_beads, list) or not all(
            isinstance(item, str) and item for item in associated_beads
        ):
            raise FulcrumError.invalid(
                "INVALID_INPUT", "associated_beads must be an array of bead IDs"
            )
        config, project = _project_config(request, str(work.fc.get("project")))
        model, effort, model_origin = _select_model(
            request, config, project, work.fc, role
        )
        from fulcrum.roles import _compiled_contract, _role_authority_instructions

        compiled_contract = _compiled_contract(work, role)
        creation_cwd = str(
            (request.instance.instance_root / "threads" / receipt_id).resolve(
                strict=False
            )
        )
        task_record_id = random_record_id()
        planned = {
            "task_record_id": task_record_id,
            "creation_cwd": creation_cwd,
            "bead_id": bead_id,
            "role": role,
            "model": model,
            "effort": effort,
            "model_origin": model_origin,
            "turn_input": instructions,
            "compiled_contract": compiled_contract,
        }
        operation, reused = ledger.create_operation(
            request,
            bead_id=bead_id,
            planned=planned,
            next_action="Create or recover the indexed native task before starting its turn.",
        )
        retained = operation.operation.get("planned")
        if not isinstance(retained, Mapping):
            raise FulcrumError(
                "OPERATION_CORRUPT", "task receipt has no plan", exit_code=4
            )
        if reused:
            task_record_id = str(retained["task_record_id"])
            creation_cwd = str(retained["creation_cwd"])
            model = str(retained["model"])
            effort = str(retained["effort"])
            instructions = str(retained["turn_input"])
            if operation.operation.get("state") in {
                "completed",
                "failed",
                "uncertain",
                "cancelled",
            }:
                return _operation_result(operation)
        Path(creation_cwd).mkdir(parents=True, exist_ok=True)
        root = str(Path(str(project["root"])).resolve(strict=True))
        spec = TaskSpec(
            creation_cwd=creation_cwd,
            cwd=root,
            project_id=(
                str(project["codex_project_id"])
                if project.get("codex_project_id")
                else None
            ),
            workspace_roots=(root,),
            title=str(request.input.get("title") or work.title),
            model=model,
            effort=effort,
            permissions=None,
            developer_instructions=(
                routing_developer_instructions(
                    request,
                    "\n\n".join(
                        item
                        for item in (
                            _role_authority_instructions(role, compiled_contract),
                            (
                                str(request.input["developer_instructions"])
                                if request.input.get("developer_instructions")
                                is not None
                                else None
                            ),
                        )
                        if item
                    ),
                )
            ),
        )
        native = _runtime_call(
            request,
            lambda runtime: _create_and_configure(
                runtime,
                spec,
                operation.operation.get("external"),
            ),
        )
        thread_id = native.id
        operation = ledger.update_operation(
            operation,
            step="native_task_configured",
            external={"adapter": "codex", "thread_id": thread_id},
            result={"task": native.to_dict()},
            next_action="Persist task/work ownership before sending the retained turn input.",
        )
        task_fc = {
            "kind": "task",
            "owner": thread_id,
            "thread_id": thread_id,
            "role": role,
            "work_bead": bead_id,
            "ownership_operation": receipt_id,
            "creation_operation": receipt_id,
            "creation_cwd": creation_cwd,
            "model": model,
            "effort": effort,
            "model_origin": model_origin,
            "compiled_contract": compiled_contract,
            "associated_beads": list(associated_beads),
            "replaced_by": None,
            "missing_finish_reminder": None,
            "archive_state": "pending",
            "archive_due_at": None,
            "archive_operation": None,
            "last_observed": native.to_dict(),
            "last_turn": None,
            "last_transition": receipt_id,
        }
        existing_task = ledger.show(task_record_id)
        if existing_task is None:
            ledger.create_record(
                record_id=task_record_id,
                kind="task",
                title=f"Managed task: {native.title or work.title}",
                description=f"Native Codex task {thread_id} for {bead_id}.",
                owner=thread_id,
                fc=task_fc,
                external_ref=f"fulcrum:thread:{thread_id}",
            )
        elif (
            existing_task.kind != "task"
            or not existing_task.fc
            or existing_task.fc.get("creation_operation") != receipt_id
        ):
            raise FulcrumError(
                "REQUEST_CONFLICT", "planned task record is occupied", exit_code=5
            )
        else:
            ledger.update_fc(task_record_id, task_fc, assignee=thread_id)
        work_fc = dict(work.fc)
        work_fc.update(
            {
                "owner": thread_id,
                "role": role,
                "ownership_operation": receipt_id,
                "phase": _phase_for(role),
                "handoff": {
                    "from_thread": work.fc.get("owner"),
                    "from_ownership_operation": work.fc.get("ownership_operation"),
                    "to_thread": thread_id,
                    "to_role": role,
                    "receipt": receipt_id,
                },
                "last_transition": receipt_id,
                "next_action": "Complete the active role responsibility and call fulcrum finish.",
                "compiled_role": {
                    "formula": f"fulcrum-{role}",
                    "authorized_role": role,
                    "thread_id": thread_id,
                    "ownership_operation": receipt_id,
                    "contract": compiled_contract,
                    "compiled_at": utc_now(),
                },
            }
        )
        ledger.update_fc(bead_id, work_fc, assignee=thread_id, status="in_progress")
        turn = _runtime_call(
            request,
            lambda runtime: _start_or_recover(
                runtime,
                native.id,
                spec,
                receipt_id,
                instructions,
            ),
        )
        current_task = ledger.show(task_record_id)
        if current_task is None or not current_task.fc:
            raise FulcrumError(
                "TASK_CORRUPT",
                "task record disappeared before turn observation",
                exit_code=4,
            )
        task_fc = dict(current_task.fc)
        task_fc["last_turn"] = turn.to_dict()
        task_fc["last_transition"] = receipt_id
        ledger.update_fc(task_record_id, task_fc)
        operation = ledger.update_operation(
            operation,
            state="completed",
            step="ownership_and_turn_verified",
            result={
                "bead_id": bead_id,
                "task_record_id": task_record_id,
                "thread_id": thread_id,
                "turn_id": turn.id,
                "ownership_operation": receipt_id,
                "compiled_contract": compiled_contract,
                "model": model,
                "effort": effort,
                "model_origin": model_origin,
                "task": native.to_dict(),
                "turn": turn.to_dict(),
            },
            next_action="Observe the native turn and retain substantive progress.",
        )
        return _operation_result(operation)

    @coordinated
    def send(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        record = _find_task(ledger, str(request.arguments["id"]))
        record_fc = record.fc or {}
        text = request.input.get("text", request.input.get("input"))
        if not isinstance(text, str) or not text.strip():
            raise FulcrumError.invalid("INVALID_INPUT", "task send requires text")
        _authorize_task_message(ledger, request, record)
        operation, reused = ledger.create_operation(
            request,
            bead_id=str(record_fc.get("work_bead")),
            planned={"thread_id": _thread_id(record), "text": text},
            next_action="Inspect the target task before sending the retained input.",
        )
        if reused and operation.operation.get("state") in {
            "completed",
            "failed",
            "uncertain",
            "cancelled",
        }:
            return _operation_result(operation)
        turn = _runtime_call(
            request,
            lambda runtime: _send_turn(
                runtime,
                record,
                operation.id,
                text,
            ),
        )
        task_fc = dict(record.fc or {})
        task_fc["last_turn"] = turn.to_dict()
        task_fc["last_transition"] = operation.id
        ledger.update_fc(record.id, task_fc)
        operation = ledger.update_operation(
            operation,
            state="completed",
            step="turn_started",
            external={
                "adapter": "codex",
                "thread_id": _thread_id(record),
                "turn_id": turn.id,
            },
            result={"thread_id": _thread_id(record), "turn": turn.to_dict()},
            next_action="Observe the native turn before sending another message.",
        )
        return _operation_result(operation)

    @coordinated
    def output(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        record = _find_task(ledger, str(request.arguments["id"]))
        limit = _limit(request.arguments.get("limit"))
        max_bytes = int(request.arguments.get("max_bytes", 262144))
        if max_bytes < 128 or max_bytes > 4 * 1024 * 1024:
            raise FulcrumError.invalid(
                "INVALID_LIMIT", "max-bytes must be between 128 and 4194304"
            )
        result = _runtime_call(
            request,
            lambda runtime: runtime.output(
                _thread_id(record),
                turn_id=_optional_string(request.arguments.get("turn_id")),
                limit=limit,
                cursor=_optional_string(request.arguments.get("cursor")),
                max_bytes=max_bytes,
            ),
        )
        return CommandResult.query(dict(result))

    def wait(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        record = _find_task(ledger, str(request.arguments["id"]))
        until = str(request.arguments["until"])
        result = _runtime_call(
            request,
            lambda runtime: _wait_for_task(
                runtime,
                _thread_id(record),
                turn_id=_optional_string(request.arguments.get("turn_id")),
                until=until,
                timeout=request.timeout,
            ),
        )
        return CommandResult.query(result)

    @coordinated
    def interrupt(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        record = _find_task(ledger, str(request.arguments["id"]))
        record_fc = record.fc or {}
        _authorize_task_message(ledger, request, record)
        task_facts = _runtime_call(
            request, lambda runtime: runtime.inspect_task(_thread_id(record))
        )
        turn_id = request.arguments.get("turn_id") or task_facts.active_turn
        if not turn_id:
            raise FulcrumError.invalid("NO_ACTIVE_TURN", "task has no active turn")
        operation, reused = ledger.create_operation(
            request,
            bead_id=str(record_fc.get("work_bead")),
            planned={"thread_id": _thread_id(record), "turn_id": str(turn_id)},
            next_action="Request interruption and inspect the exact turn.",
        )
        if reused and operation.operation.get("state") in {
            "completed",
            "failed",
            "uncertain",
            "cancelled",
        }:
            return _operation_result(operation)
        observed = _runtime_call(
            request,
            lambda runtime: _interrupt_or_observe(
                runtime, _thread_id(record), str(turn_id)
            ),
        )
        state = "completed" if observed.completed else "uncertain"
        operation = ledger.update_operation(
            operation,
            state=state,
            step=(
                "interrupt_observed" if observed.completed else "interrupt_acknowledged"
            ),
            result={"turn": observed.to_dict()},
            next_action=(
                "No further action is required."
                if observed.completed
                else "Wait for native terminal evidence before transferring ownership."
            ),
        )
        return _operation_result(operation)

    @coordinated
    def requests(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        record = _find_task(ledger, str(request.arguments["id"]))
        facts = _runtime_call(
            request, lambda runtime: runtime.inspect_task(_thread_id(record))
        )
        gaps = list(facts.gaps)
        if not facts.pending_requests and facts.runtime_status not in {
            "idle",
            "notLoaded",
        }:
            gaps.append(
                "the new subscription did not recover an in-memory native request identity"
            )
        return CommandResult.query(
            {
                "thread_id": facts.id,
                "items": [dict(item) for item in facts.pending_requests],
                "observed_at": facts.observed_at,
                "gaps": gaps,
            }
        )

    @coordinated
    def respond(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        record = _find_task(ledger, str(request.arguments["id"]))
        _authorize_task_message(ledger, request, record)
        native_request_id = str(request.arguments["request"])
        response = request.input.get("response")
        if not isinstance(response, Mapping):
            raise FulcrumError.invalid(
                "INVALID_INPUT", "task respond requires a response object"
            )
        operation = _runtime_call(
            request,
            lambda runtime: _respond_transaction(
                runtime,
                ledger,
                request,
                record,
                native_request_id,
                dict(response),
            ),
        )
        return _operation_result(operation)

    @coordinated
    def terminals(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        record = _find_task(ledger, str(request.arguments["id"]))
        limit = _limit(request.arguments.get("limit"))
        result = _runtime_call(
            request,
            lambda runtime: runtime.terminals(
                _thread_id(record),
                limit=limit,
                cursor=_optional_string(request.arguments.get("cursor")),
            ),
        )
        return CommandResult.query(dict(result))

    @coordinated
    def terminal_stop(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        record = _find_task(ledger, str(request.arguments["id"]))
        _authorize_task_message(ledger, request, record)
        listed = _runtime_call(
            request,
            lambda runtime: runtime.terminals(_thread_id(record), limit=0, cursor=None),
        )
        available = [
            str(item.get("terminal_id"))
            for item in listed.get("items", [])
            if isinstance(item, Mapping) and item.get("terminal_id")
        ]
        selected = (
            available
            if request.arguments.get("all_owned")
            else [str(request.arguments["terminal"])]
        )
        if not request.arguments.get("all_owned") and selected[0] not in available:
            raise FulcrumError.invalid(
                "TERMINAL_NOT_OWNED",
                "the selected terminal is not a running terminal owned by this task",
                details={"terminal_id": selected[0], "owned": available},
            )
        operation, reused = ledger.create_operation(
            request,
            bead_id=str((record.fc or {}).get("work_bead") or "") or None,
            planned={
                "thread_id": _thread_id(record),
                "terminal_ids": selected,
                "all_owned": bool(request.arguments.get("all_owned")),
                "reason": request.arguments.get("reason"),
            },
            next_action="Terminate only the exact retained owned terminal IDs and inspect absence.",
        )
        if reused and operation.operation.get("state") in {
            "completed",
            "failed",
            "uncertain",
            "cancelled",
        }:
            return _operation_result(operation)
        try:
            results = _runtime_call(
                request,
                lambda runtime: _terminate_terminals(
                    runtime, _thread_id(record), selected
                ),
            )
        except FulcrumError as error:
            operation = ledger.update_operation(
                operation,
                state=(
                    "uncertain" if error.state is CommandState.UNCERTAIN else "failed"
                ),
                step="terminal_stop_unresolved",
                error={
                    "code": error.code,
                    "message": error.message,
                    "retryable": error.retryable,
                },
                next_action=(
                    "Inspect the exact terminal IDs before any retry."
                    if error.state is CommandState.UNCERTAIN
                    else "Use --all-owned only if stopping every owned terminal is intended."
                ),
            )
            return _operation_result(operation)
        operation = ledger.update_operation(
            operation,
            state="completed",
            step="owned_terminals_stopped",
            result={
                "thread_id": _thread_id(record),
                "terminals": results,
                "all_owned": bool(request.arguments.get("all_owned")),
            },
            next_action="No selected owned terminal remains running.",
        )
        return _operation_result(operation)

    @coordinated
    def release(self, request: ParsedRequest) -> CommandResult:
        return self._lifecycle(request, "release")

    @coordinated
    def archive(self, request: ParsedRequest) -> CommandResult:
        return self._lifecycle(request, "archive")

    @coordinated
    def unarchive(self, request: ParsedRequest) -> CommandResult:
        return self._lifecycle(request, "unarchive")

    @coordinated
    def delete(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        record = _find_task(ledger, str(request.arguments["id"]))
        if not request.arguments.get("yes"):
            raise FulcrumError.invalid(
                "CONFIRMATION_REQUIRED", "task delete requires --yes"
            )
        work = ledger.show(str(record.fc.get("work_bead"))) if record.fc else None
        if work is not None and work.status != "closed":
            raise FulcrumError(
                "OWNERSHIP_CONFLICT",
                "cannot delete a managed task while its work remains open",
                exit_code=5,
            )
        return self._lifecycle(request, "delete")

    def _lifecycle(self, request: ParsedRequest, action: str) -> CommandResult:
        ledger = _ledger(request)
        record = _find_task(ledger, str(request.arguments["id"]))
        _authorize_task_message(ledger, request, record)
        if action in {"archive", "delete"}:
            snapshot = _runtime_call(
                request,
                lambda runtime: _task_lifecycle_snapshot(runtime, _thread_id(record)),
            )
            task_facts = snapshot["task"]
            terminals = snapshot["terminals"]
            if task_facts.active_turn is not None or terminals:
                raise FulcrumError(
                    "TASK_NOT_IDLE",
                    f"cannot {action} a task with active native work",
                    exit_code=5,
                    details={
                        "active_turn": task_facts.active_turn,
                        "terminal_ids": [
                            item.get("terminal_id")
                            for item in terminals
                            if isinstance(item, Mapping)
                        ],
                    },
                )
        operation, reused = ledger.create_operation(
            request,
            bead_id=str(record.fc.get("work_bead")) if record.fc else None,
            planned={"thread_id": _thread_id(record), "action": action},
            next_action=f"Apply and observe native task {action}.",
        )
        if reused and operation.operation.get("state") in {
            "completed",
            "failed",
            "uncertain",
            "cancelled",
        }:
            return _operation_result(operation)
        if action == "release":
            facts = _runtime_call(
                request, lambda runtime: runtime.release(_thread_id(record))
            ).to_dict()
        elif action == "archive":
            facts = _runtime_call(
                request, lambda runtime: runtime.archive(_thread_id(record))
            ).to_dict()
        elif action == "unarchive":
            facts = _runtime_call(
                request, lambda runtime: runtime.unarchive(_thread_id(record))
            ).to_dict()
        else:
            facts = _runtime_call(
                request, lambda runtime: runtime.delete(_thread_id(record))
            ).to_dict()
        fc = dict(record.fc or {})
        if action == "archive":
            fc["archive_state"] = "done"
            fc["archive_operation"] = operation.id
        elif action == "unarchive":
            fc["archive_state"] = "suppressed"
            fc["archive_suppressed_reason"] = "explicit unarchive"
        elif action == "delete":
            fc["deleted_at"] = utc_now()
        fc["last_observed"] = facts
        fc["last_transition"] = operation.id
        ledger.update_fc(record.id, fc)
        operation = ledger.update_operation(
            operation,
            state="completed",
            step=f"task_{action}_observed",
            result={"thread_id": _thread_id(record), "facts": facts},
            next_action="No further action is required.",
        )
        return _operation_result(operation)


async def _capabilities_and_resources(
    runtime: AppServerRuntime,
) -> tuple[RuntimeCapabilities, Any]:
    return await runtime.capabilities(), await runtime.resources()


async def _inspect_many(
    runtime: AppServerRuntime, thread_ids: Sequence[str]
) -> dict[str, TaskFacts]:
    results = await asyncio.gather(
        *(runtime.inspect_task(thread_id) for thread_id in thread_ids),
        return_exceptions=True,
    )
    return {
        thread_id: result
        for thread_id, result in zip(thread_ids, results)
        if isinstance(result, TaskFacts)
    }


async def _task_lifecycle_snapshot(
    runtime: AppServerRuntime, thread_id: str
) -> dict[str, Any]:
    task = await runtime.inspect_task(thread_id)
    terminal_page = await runtime.terminals(thread_id, limit=0, cursor=None)
    terminals = terminal_page.get("items")
    return {
        "task": task,
        "terminals": (
            [dict(item) for item in terminals if isinstance(item, Mapping)]
            if isinstance(terminals, list)
            else []
        ),
    }


async def _wait_for_task(
    runtime: AppServerRuntime,
    thread_id: str,
    *,
    turn_id: str | None,
    until: str,
    timeout: float,
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    last: TaskFacts | None = None
    selected_turn: TurnFacts | None = None
    while True:
        try:
            last = await runtime.inspect_task(thread_id)
            if turn_id is not None:
                selected_turn = await runtime.inspect_turn(thread_id, turn_id)
        except AppServerError as error:
            if error.category not in {"unavailable", "transient"}:
                raise
            if asyncio.get_running_loop().time() >= deadline:
                raise
            await asyncio.sleep(0.25)
            await runtime.connect()
            continue
        if (
            until == "idle"
            and last.active_turn is None
            and last.runtime_status
            in {
                "idle",
                "notLoaded",
                "completed",
                "failed",
            }
        ):
            break
        if until == "terminal":
            if (
                turn_id is not None
                and selected_turn is not None
                and selected_turn.completed
            ):
                break
            if turn_id is None and last.last_turn is not None:
                status = str(last.last_turn.get("status") or "")
                if last.active_turn is None and status in {
                    "completed",
                    "failed",
                    "interrupted",
                }:
                    selected_turn = TurnFacts(
                        id=str(last.last_turn.get("id") or ""),
                        thread_id=thread_id,
                        state=status,
                        operation_id=None,
                        completed=True,
                        error=(
                            dict(last.last_turn["error"])
                            if isinstance(last.last_turn.get("error"), Mapping)
                            else None
                        ),
                        tools=(),
                        usage=(
                            dict(last.last_turn["usage"])
                            if isinstance(last.last_turn.get("usage"), Mapping)
                            else None
                        ),
                        observed_at=last.observed_at,
                    )
                    break
        if asyncio.get_running_loop().time() >= deadline:
            raise FulcrumError(
                "WAIT_TIMEOUT",
                f"task {thread_id} did not reach {until} before the deadline",
                exit_code=3,
                retryable=True,
                details={
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "last_observation": last.to_dict(),
                },
            )
        await asyncio.sleep(
            min(
                0.25,
                max(0.0, deadline - asyncio.get_running_loop().time()),
            )
        )
    assert last is not None
    return {
        "thread_id": thread_id,
        "until": until,
        "satisfied": True,
        "turn": selected_turn.to_dict() if selected_turn is not None else None,
        "task": last.to_dict(),
        "pending_requests": [dict(item) for item in last.pending_requests],
        "observed_at": last.observed_at,
        "gaps": list(last.gaps),
    }


async def _respond_and_observe(
    runtime: AppServerRuntime,
    thread_id: str,
    request_id: str,
    response: dict[str, Any],
) -> dict[str, Any]:
    try:
        await runtime.respond(thread_id, request_id, response)
    except AppServerError as error:
        if not error.uncertain:
            raise
        facts = await runtime.inspect_task(thread_id)
        still_pending = any(
            str(item.get("request_id")) == request_id for item in facts.pending_requests
        )
        if still_pending:
            raise
        return {
            "thread_id": thread_id,
            "request_id": request_id,
            "resolved": False,
            "uncertain": True,
            "gap": "request disappeared after response transport loss",
            "observed_at": facts.observed_at,
        }
    facts = await runtime.inspect_task(thread_id)
    still_pending = any(
        str(item.get("request_id")) == request_id for item in facts.pending_requests
    )
    return {
        "thread_id": thread_id,
        "request_id": request_id,
        "resolved": not still_pending,
        "uncertain": still_pending,
        "gap": (
            "native request remained pending after response" if still_pending else None
        ),
        "observed_at": facts.observed_at,
    }


async def _respond_transaction(
    runtime: AppServerRuntime,
    ledger: Ledger,
    request: ParsedRequest,
    record: LedgerRecord,
    request_id: str,
    response: dict[str, Any],
) -> OperationRecord:
    """Retain and answer one live native request without dropping its subscription."""

    thread_id = _thread_id(record)
    facts = await runtime.inspect_task(thread_id)
    matches = [
        item
        for item in facts.pending_requests
        if str(item.get("request_id")) == request_id
    ]
    if len(matches) != 1:
        raise FulcrumError(
            "REQUEST_IDENTITY_LOST",
            "the native request is no longer present on this live subscription",
            exit_code=5,
            retryable=False,
            details={
                "thread_id": facts.id,
                "request_id": request_id,
                "gaps": list(facts.gaps)
                + ["reconnect did not recover the in-memory request identity"],
            },
        )
    method = str(matches[0].get("method") or "")
    _validate_native_response(method, response)
    operation, reused = await asyncio.to_thread(
        ledger.create_operation,
        request,
        bead_id=str((record.fc or {}).get("work_bead") or "") or None,
        planned={
            "thread_id": thread_id,
            "native_request_id": request_id,
            "method": method,
            "response": response,
        },
        next_action="Send the typed response once and inspect whether the request remains pending.",
    )
    if reused and operation.operation.get("state") in {
        "completed",
        "failed",
        "uncertain",
        "cancelled",
    }:
        return operation
    observed = await _respond_and_observe(runtime, thread_id, request_id, response)
    state = "uncertain" if observed["uncertain"] else "completed"
    return await asyncio.to_thread(
        ledger.update_operation,
        operation,
        state=state,
        step=(
            "response_identity_lost"
            if observed["uncertain"]
            else "native_request_resolved"
        ),
        external={
            "adapter": "codex",
            "thread_id": thread_id,
            "request_id": request_id,
            "method": method,
        },
        result=observed,
        next_action=(
            "Inspect the task and request history; do not replay an absent request."
            if observed["uncertain"]
            else "Observe the continuing native turn."
        ),
    )


async def _terminate_terminals(
    runtime: AppServerRuntime, thread_id: str, terminal_ids: Sequence[str]
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for terminal_id in terminal_ids:
        results.append(dict(await runtime.terminate_terminal(thread_id, terminal_id)))
    return results


async def _create_and_configure(
    runtime: AppServerRuntime,
    spec: TaskSpec,
    external: Any,
) -> TaskFacts:
    capabilities = await runtime.capabilities()
    efforts = capabilities.models.get(spec.model)
    if efforts is None:
        raise AppServerError(
            f"model {spec.model} is not available", category="rejected"
        )
    if efforts and spec.effort not in efforts:
        raise AppServerError(
            f"effort {spec.effort} is not available for {spec.model}",
            category="rejected",
        )
    native: TaskFacts | None = None
    if isinstance(external, Mapping) and isinstance(external.get("thread_id"), str):
        native = await runtime.inspect_task(str(external["thread_id"]))
    if native is None or not native.exists:
        matches = await runtime.find_tasks(spec.creation_cwd)
        if len(matches) > 1:
            raise AppServerError(
                "multiple native tasks match the recorded creation cwd",
                category="uncertain",
                uncertain=True,
            )
        if matches:
            native = matches[0]
        else:
            try:
                native = await runtime.create_task(spec)
            except AppServerError as error:
                if not error.uncertain and error.category not in {
                    "unavailable",
                    "transient",
                }:
                    raise
                matches = await runtime.find_tasks(spec.creation_cwd)
                if len(matches) != 1:
                    raise
                native = matches[0]
    return await runtime.configure_task(native.id, spec)


async def _start_or_recover(
    runtime: AppServerRuntime,
    thread_id: str,
    spec: TaskSpec,
    operation: str,
    instructions: str,
    *,
    ownership_operation: str | None = None,
) -> TurnFacts:
    found = await runtime.find_turn(thread_id, operation)
    if found is not None:
        return found
    try:
        turn = await runtime.start_turn(
            thread_id,
            TurnInput(
                text=instructions,
                cwd=spec.cwd,
                workspace_roots=spec.workspace_roots,
                model=spec.model,
                effort=spec.effort,
                operation_id=operation,
                ownership_operation=ownership_operation or operation,
                developer_instructions=spec.developer_instructions,
            ),
        )
    except AppServerError as error:
        if not error.uncertain and error.category not in {
            "unavailable",
            "transient",
        }:
            raise
        found = await runtime.find_turn(thread_id, operation)
        if found is None:
            raise
        turn = found
    return turn


async def _interrupt_or_observe(
    runtime: AppServerRuntime, thread_id: str, turn_id: str
) -> TurnFacts:
    try:
        return await runtime.interrupt(thread_id, turn_id)
    except AppServerError as error:
        if error.category != "rejected" or "no active turn" not in str(error).lower():
            raise
        observed = await runtime.inspect_turn(thread_id, turn_id)
        if observed is None or not observed.completed:
            raise
        return observed


async def _send_turn(
    runtime: AppServerRuntime,
    record: LedgerRecord,
    operation: str,
    text: str,
) -> TurnFacts:
    facts = await runtime.inspect_task(_thread_id(record))
    if facts.active_turn:
        raise AppServerError(
            f"task {_thread_id(record)} already has active turn {facts.active_turn}",
            category="rejected",
        )
    existing = await runtime.find_turn(_thread_id(record), operation)
    if existing is not None:
        return existing
    fc = record.fc or {}
    observed = facts.to_dict()
    cwd = str(observed.get("cwd") or fc.get("creation_cwd"))
    roots = tuple(str(item) for item in observed.get("workspace_roots", [])) or (cwd,)
    try:
        return await runtime.start_turn(
            _thread_id(record),
            TurnInput(
                text=text,
                cwd=cwd,
                workspace_roots=roots,
                model=str(fc.get("model")),
                effort=str(fc.get("effort")),
                operation_id=operation,
                ownership_operation=(
                    str(fc["ownership_operation"])
                    if fc.get("ownership_operation")
                    else None
                ),
            ),
        )
    except AppServerError as error:
        if not error.uncertain:
            raise
        found = await runtime.find_turn(_thread_id(record), operation)
        if found is None:
            raise
        return found


@unlocked
@timed("runtime_service._runtime_call")
def _runtime_call(
    request: ParsedRequest,
    action: Callable[[Runtime], Coroutine[Any, Any, T]],
) -> T:
    async def invoke() -> T:
        endpoint = _runtime_endpoint(request)
        from fulcrum.resident_client import ResidentTransport

        runtime = AppServerRuntime(
            endpoint,
            transport=ResidentTransport(
                request.instance.instance_root / "resident.sock"
            ),
        )
        try:
            await runtime.connect()
            return await action(runtime)
        finally:
            await runtime.close()

    try:
        if request.runtime_submit is not None:
            return cast(T, request.runtime_submit(action, request.timeout + 5))
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(invoke())
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(invoke())).result(
                timeout=request.timeout + 5
            )
    except AppServerError as error:
        code = {
            "unsupported": "RUNTIME_UNSUPPORTED",
            "rejected": "RUNTIME_REJECTED",
            "uncertain": "RUNTIME_UNCERTAIN",
        }.get(error.category, "RUNTIME_UNAVAILABLE")
        raise FulcrumError(
            code,
            str(error),
            exit_code=4 if error.category != "rejected" else 2,
            retryable=error.category in {"transient", "unavailable"},
            state=CommandState.UNCERTAIN if error.uncertain else CommandState.FAILED,
        ) from error
    except (TimeoutError, OSError) as error:
        raise FulcrumError(
            "RUNTIME_UNAVAILABLE", str(error), exit_code=4, retryable=True
        ) from error


def _runtime_endpoint(request: ParsedRequest) -> str:
    manager = ConfigurationManager(request.instance.config_path)
    document, _ = manager.load()
    runtime = manager.effective(document)["runtime"]
    endpoint = runtime.get("endpoint") if isinstance(runtime, Mapping) else None
    if not isinstance(endpoint, str) or not endpoint:
        raise FulcrumError(
            "RUNTIME_UNAVAILABLE", "runtime endpoint is not configured", exit_code=4
        )
    return endpoint


def _ledger(request: ParsedRequest) -> Ledger:
    if request.instance.brain_root is None:
        raise FulcrumError(
            "LEDGER_UNAVAILABLE", "brain root is unavailable", exit_code=4
        )
    return Ledger(request.instance.brain_root, timeout=request.timeout)


def _project_config(
    request: ParsedRequest, project_id: str
) -> tuple[dict[str, Any], Mapping[str, Any]]:
    manager = ConfigurationManager(request.instance.config_path)
    document, _ = manager.load()
    config = manager.effective(document)
    project = config["projects"].get(project_id)
    if not isinstance(project, Mapping):
        raise FulcrumError.invalid(
            "INVALID_PROJECT", f"project {project_id} is not enrolled"
        )
    return config, project


def _select_model(
    request: ParsedRequest,
    config: Mapping[str, Any],
    project: Mapping[str, Any],
    work: Mapping[str, Any],
    role: str,
) -> tuple[str, str, str]:
    explicit_model = request.arguments.get("model")
    explicit_effort = request.arguments.get("effort")
    layers = [
        ("explicit", {"model": explicit_model, "effort": explicit_effort}),
        (
            "work",
            (
                work.get("models", {}).get(role, {})
                if isinstance(work.get("models"), Mapping)
                else {}
            ),
        ),
        (
            "project",
            (
                project.get("models", {}).get(role, {})
                if isinstance(project.get("models"), Mapping)
                else {}
            ),
        ),
        (
            "instance",
            (
                config.get("models", {}).get(role, {})
                if isinstance(config.get("models"), Mapping)
                else {}
            ),
        ),
    ]
    model: str | None = None
    effort: str | None = None
    origins: list[str] = []
    for origin, value in layers:
        if not isinstance(value, Mapping):
            continue
        if model is None and isinstance(value.get("model"), str):
            model = str(value["model"])
            origins.append(origin + ":model")
        if effort is None and isinstance(value.get("effort"), str):
            effort = str(value["effort"])
            origins.append(origin + ":effort")
    if model is None or effort is None:
        raise FulcrumError.invalid(
            "MODEL_UNAVAILABLE", f"no complete model selection for {role}"
        )
    return model, effort, ",".join(origins)


def _find_task(ledger: Ledger, identifier: str) -> LedgerRecord:
    direct = ledger.show(identifier)
    if direct is not None and direct.kind == "task":
        return direct
    matches = [
        item
        for item in ledger.list_records(kind="task", limit=0)
        if item.fc and item.fc.get("thread_id") == identifier
    ]
    if len(matches) != 1:
        raise FulcrumError.invalid("NOT_FOUND", f"unknown managed task {identifier}")
    return matches[0]


def _thread_id(record: LedgerRecord) -> str:
    if not record.fc or not isinstance(record.fc.get("thread_id"), str):
        raise FulcrumError(
            "TASK_CORRUPT", f"task record {record.id} has no thread ID", exit_code=4
        )
    return str(record.fc["thread_id"])


def _task_view(record: LedgerRecord, facts: TaskFacts | None) -> dict[str, Any]:
    fc = dict(record.fc or {})
    return {
        "id": record.id,
        "thread_id": fc.get("thread_id"),
        "role": fc.get("role"),
        "work_bead": fc.get("work_bead"),
        "ownership_operation": fc.get("ownership_operation"),
        "creation_operation": fc.get("creation_operation"),
        "creation_cwd": fc.get("creation_cwd"),
        "model": fc.get("model"),
        "effort": fc.get("effort"),
        "associated_beads": fc.get("associated_beads", []),
        "replaced_by": fc.get("replaced_by"),
        "archive_state": fc.get("archive_state"),
        "archive_due_at": fc.get("archive_due_at"),
        "archive_operation": fc.get("archive_operation"),
        "subscription": {
            "state": fc.get("subscription_state"),
            "release": fc.get("subscription_release"),
            "observed_loaded": facts.loaded if facts else None,
            "observed_runtime_status": facts.runtime_status if facts else None,
        },
        "native": facts.to_dict() if facts else fc.get("last_observed"),
        "observation_source": "live" if facts else "retained",
        "gaps": list(facts.gaps) if facts else ["live runtime observation unavailable"],
        "next_commands": [
            ["fulcrum", "task", "show", str(fc.get("thread_id")), "--json"]
        ],
    }


def _validate_native_response(method: str, response: Mapping[str, Any]) -> None:
    if method == "item/commandExecution/requestApproval":
        decision = response.get("decision")
        if not _valid_command_approval_decision(decision):
            raise FulcrumError.invalid(
                "INVALID_NATIVE_RESPONSE",
                "command approval response does not match the native decision type",
            )
        return
    if method == "item/fileChange/requestApproval":
        decision = response.get("decision")
        if not isinstance(decision, str) or decision not in {
            "accept",
            "acceptForSession",
            "decline",
            "cancel",
        }:
            raise FulcrumError.invalid(
                "INVALID_NATIVE_RESPONSE",
                "file-change approval requires a supported native decision",
            )
        return
    if method == "item/permissions/requestApproval":
        if not isinstance(response.get("permissions"), Mapping):
            raise FulcrumError.invalid(
                "INVALID_NATIVE_RESPONSE",
                "permissions approval requires a permissions object",
            )
        scope = response.get("scope", "turn")
        if not isinstance(scope, str) or scope not in {"turn", "session"}:
            raise FulcrumError.invalid(
                "INVALID_NATIVE_RESPONSE",
                "permissions approval scope must be turn or session",
            )
        strict = response.get("strictAutoReview")
        if strict is not None and not isinstance(strict, bool):
            raise FulcrumError.invalid(
                "INVALID_NATIVE_RESPONSE",
                "strictAutoReview must be boolean or null",
            )
        return
    if method in {"execCommandApproval", "applyPatchApproval"}:
        if not _valid_legacy_approval_decision(response.get("decision")):
            raise FulcrumError.invalid(
                "INVALID_NATIVE_RESPONSE",
                f"{method} response does not match the native review decision type",
            )
        return
    if method == "item/tool/requestUserInput":
        answers = response.get("answers")
        if not isinstance(answers, Mapping):
            raise FulcrumError.invalid(
                "INVALID_NATIVE_RESPONSE",
                "request-user-input response requires an answers object",
            )
        for value in answers.values():
            if (
                not isinstance(value, Mapping)
                or not isinstance(value.get("answers"), list)
                or not all(isinstance(item, str) for item in value["answers"])
            ):
                raise FulcrumError.invalid(
                    "INVALID_NATIVE_RESPONSE",
                    "each request-user-input answer must contain a string array",
                )
        return
    if method == "mcpServer/elicitation/request":
        action = response.get("action")
        if action not in {"accept", "decline", "cancel"}:
            raise FulcrumError.invalid(
                "INVALID_NATIVE_RESPONSE",
                "elicitation response requires accept, decline, or cancel",
            )
        if action == "accept" and "content" not in response:
            raise FulcrumError.invalid(
                "INVALID_NATIVE_RESPONSE",
                "accepted elicitation requires content",
            )
        return
    raise FulcrumError(
        "RUNTIME_UNSUPPORTED",
        f"Fulcrum cannot safely validate response type {method}",
        exit_code=4,
    )


def _valid_command_approval_decision(value: Any) -> bool:
    if isinstance(value, str) and value in {
        "accept",
        "acceptForSession",
        "decline",
        "cancel",
    }:
        return True
    if not isinstance(value, Mapping) or len(value) != 1:
        return False
    amendment = value.get("acceptWithExecpolicyAmendment")
    if isinstance(amendment, Mapping):
        rules = amendment.get("execpolicy_amendment")
        return isinstance(rules, list) and all(isinstance(rule, str) for rule in rules)
    amendment = value.get("applyNetworkPolicyAmendment")
    if not isinstance(amendment, Mapping):
        return False
    policy = amendment.get("network_policy_amendment")
    return (
        isinstance(policy, Mapping)
        and isinstance(policy.get("host"), str)
        and bool(policy.get("host"))
        and policy.get("action") in {"allow", "deny"}
    )


def _valid_legacy_approval_decision(value: Any) -> bool:
    if isinstance(value, str) and value in {
        "approved",
        "approved_for_session",
        "approved_mcp_policy_amendment",
        "timed_out",
        "abort",
    }:
        return True
    if not isinstance(value, Mapping) or len(value) != 1:
        return False
    amendment = value.get("approved_execpolicy_amendment")
    if isinstance(amendment, Mapping):
        rules = amendment.get("proposed_execpolicy_amendment")
        return isinstance(rules, list) and all(isinstance(rule, str) for rule in rules)
    denied = value.get("denied")
    if isinstance(denied, Mapping):
        return isinstance(denied.get("rejection"), str)
    amendment = value.get("network_policy_amendment")
    if not isinstance(amendment, Mapping):
        return False
    policy = amendment.get("network_policy_amendment")
    return (
        isinstance(policy, Mapping)
        and isinstance(policy.get("host"), str)
        and bool(policy.get("host"))
        and policy.get("action") in {"allow", "deny"}
    )


def _authorize_task_message(
    ledger: Ledger, request: ParsedRequest, record: LedgerRecord
) -> None:
    if request.actor.kind in {"human", "controller"}:
        return
    fc = record.fc or {}
    if request.thread_id not in {_thread_id(record), _marshal_owner(ledger)}:
        raise FulcrumError(
            "OWNERSHIP_CONFLICT", "caller cannot control this task", exit_code=5
        )
    if request.thread_id == _thread_id(
        record
    ) and request.ownership_operation != fc.get("ownership_operation"):
        raise FulcrumError(
            "OWNERSHIP_CONFLICT",
            "task control requires the current acquisition",
            exit_code=5,
        )


def _marshal_owner(ledger: Ledger) -> str:
    control = ledger.show("fc-system")
    return (
        str(control.fc["marshal_thread"])
        if control and control.fc and control.fc.get("marshal_thread")
        else "HUMAN"
    )


def _phase_for(role: str) -> str:
    if role == "warden":
        return "reviewing"
    if role == "justiciar":
        return "recovering"
    return "working"


def _limit(value: Any) -> int:
    result = int(value) if value is not None else 20
    if result < 0:
        raise FulcrumError.invalid("INVALID_LIMIT", "limit cannot be negative")
    return result


def _optional_string(value: Any) -> str | None:
    return str(value) if isinstance(value, str) and value else None


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
