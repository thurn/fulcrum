"""Role formulas, direct human entry, and read-only compaction context."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import replace
from importlib.resources import files
from pathlib import Path
from typing import Any

from fulcrum.configuration import ConfigurationManager, ROLES
from fulcrum.contracts import CommandResult, CommandState, FulcrumError, ParsedRequest
from fulcrum.ledger import (
    Ledger,
    LedgerFailure,
    LedgerRecord,
    OperationRecord,
    operation_id,
    operation_view,
    random_record_id,
    utc_now,
)
from fulcrum.knowledge import render_selected_memory, select_memory
from fulcrum.runtime import TaskFacts, TaskSpec
from fulcrum.runtime_service import (
    _create_and_configure,
    _phase_for,
    _project_config,
    _runtime_call,
    _select_model,
    _start_or_recover,
)
from fulcrum.work import WorkService

ROLE_TITLES: Mapping[str, tuple[str, str]] = {
    "weaver": ("🧵", "wvr"),
    "executor": ("🛠️", "exe"),
    "warden": ("🛡️", "war"),
    "sage": ("📖", "sge"),
    "mason": ("🧱", "mas"),
    "justiciar": ("🔥", "jus"),
}
LEADERSHIP_TITLES: Mapping[str, str] = {
    "vizier": "🔮 VIZIER 🔮",
    "marshal": "🧭 MARSHAL 🧭",
}
DEGRADABLE_CODES: set[str] = {
    "CONFIG_NOT_FOUND",
    "CONFIG_INVALID",
    "LEDGER_UNAVAILABLE",
    "LEDGER_UNCERTAIN",
    "RUNTIME_UNAVAILABLE",
    "RUNTIME_UNCERTAIN",
    "RUNTIME_UNSUPPORTED",
}


def role_title(role: str, bead_id: str, title: str) -> str:
    if role in LEADERSHIP_TITLES:
        return LEADERSHIP_TITLES[role]
    if role not in ROLE_TITLES:
        raise FulcrumError.invalid("INVALID_ROLE", f"unknown role {role}")
    suffix = bead_id[3:] if bead_id.startswith("fc-") else bead_id
    emoji, code = ROLE_TITLES[role]
    return f"{emoji}[{code}-{suffix}] {title}"


def fallback_instructions(role: str) -> str:
    if role not in ROLES:
        raise FulcrumError.invalid("INVALID_ROLE", f"unknown role {role}")
    try:
        return (
            files("fulcrum")
            .joinpath("role_fallbacks", f"{role}.md")
            .read_text(encoding="utf-8")
            .strip()
        )
    except (FileNotFoundError, OSError) as error:
        raise FulcrumError(
            "ROLE_ASSET_MISSING",
            f"fallback instructions for {role} are unavailable: {error}",
            exit_code=4,
        ) from error


class RoleService:
    def enter(self, request: ParsedRequest) -> CommandResult:
        role = str(request.arguments["role"])
        origin = str(request.arguments.get("origin", "human"))
        if origin == "dispatch" and role == "vizier":
            raise FulcrumError(
                "VIZIER_UNSOLICITED",
                "Vizier cannot be entered by automatic dispatch",
                exit_code=5,
            )
        description = request.input.get("description")
        if not isinstance(description, str) or not description.strip():
            raise FulcrumError.invalid(
                "INVALID_INPUT", "enter requires a literal description"
            )
        try:
            return self._enter(request, role, description)
        except LedgerFailure as error:
            return _degraded(
                request,
                role,
                reason=str(error),
                bead_id=str(request.input.get("bead") or "") or None,
                thread_id=request.thread_id,
            )
        except FulcrumError as error:
            if error.code not in DEGRADABLE_CODES:
                raise
            return _degraded(
                request,
                role,
                reason=error.message,
                bead_id=str(request.input.get("bead") or "") or None,
                thread_id=request.thread_id,
                operation=error.operation_id,
            )

    def _enter(
        self, request: ParsedRequest, role: str, description: str
    ) -> CommandResult:
        ledger = _ledger(request)
        bead_id = self._resolve_work(request, ledger, role, description)
        work = ledger.show(bead_id)
        if work is None or work.kind != "work" or not work.fc:
            raise FulcrumError.invalid("NOT_FOUND", f"unknown work {bead_id}")
        routed_leader = False
        if role in LEADERSHIP_TITLES and request.thread_id is None:
            leader_thread = _leader_thread(ledger, role)
            if leader_thread is None:
                return _degraded(
                    request,
                    role,
                    reason=f"standing {role} leadership is unavailable",
                    bead_id=bead_id,
                    thread_id=None,
                )
            request = replace(request, thread_id=leader_thread)
            routed_leader = True
        if work.status == "closed" and role not in {"sage", "mason"}:
            raise FulcrumError(
                "OWNERSHIP_CONFLICT",
                "only scoped Sage or Mason investigation may enter closed work directly",
                exit_code=5,
            )
        current_owner = work.fc.get("owner")
        handoff = work.fc.get("handoff")
        controller_handoff = (
            request.actor.kind == "controller"
            and role == "warden"
            and isinstance(handoff, Mapping)
            and handoff.get("to_role") == "warden"
            and handoff.get("start_request_id") == request.request_id
            and (
                (
                    work.fc.get("phase") == "handoff"
                    and handoff.get("from_thread") == current_owner
                )
                or (
                    work.fc.get("role") == "warden"
                    and handoff.get("to_thread") == current_owner
                )
            )
        )
        existing = _valid_existing_entry(ledger, work, request.thread_id, role)
        if existing is not None and not controller_handoff:
            config, project = _project_config(request, str(work.fc.get("project")))
            model, effort, _ = _select_model(request, config, project, work.fc, role)
            existing_task = next(
                item
                for item in ledger.list_records(kind="task", limit=0)
                if item.fc
                and item.fc.get("thread_id") == existing[0]
                and item.fc.get("work_bead") == work.id
                and item.fc.get("ownership_operation") == existing[1]
            )
            if not _entry_configuration_compatible(
                existing_task, project, model, effort
            ):
                raise FulcrumError(
                    "TASK_CONFIGURATION_CHANGED",
                    "the current task acquisition is incompatible with current project/model configuration; use fleet replace",
                    exit_code=5,
                )
            if request.thread_id is None:
                observed = _runtime_call(
                    request,
                    lambda runtime: runtime.inspect_task(existing[0]),
                )
                if observed.archived or observed.active_turn is not None:
                    raise FulcrumError(
                        "TASK_NOT_REUSABLE",
                        "the current task is not observably idle and unarchived",
                        exit_code=5,
                    )
            instructions = _cook_role(
                ledger,
                request,
                work,
                role,
                existing[0],
                existing[1],
                start_operation=existing[1],
            )["instructions"]
            return CommandResult.query(
                {
                    "bead_id": bead_id,
                    "thread_id": existing[0],
                    "role": role,
                    "ownership_operation": existing[1],
                    "instructions": instructions,
                    "operation": None,
                    "reused": True,
                }
            )
        allowed = {"HUMAN", _marshal_owner(ledger), request.thread_id}
        if (
            work.status != "closed"
            and current_owner not in allowed
            and not controller_handoff
        ):
            raise FulcrumError(
                "OWNERSHIP_CONFLICT",
                "role entry cannot start a second writer over active owned work",
                exit_code=5,
                details={"bead_id": bead_id, "owner": current_owner},
            )
        assert request.request_id is not None
        receipt_id = operation_id(request.request_id)
        spec: TaskSpec | None = None
        if request.thread_id and role not in LEADERSHIP_TITLES:
            _return_prior_work(
                ledger,
                request.thread_id,
                except_bead=bead_id,
                transition_operation=receipt_id,
            )
        bound_records = (
            [
                item
                for item in ledger.list_records(kind="task", limit=0)
                if item.fc and item.fc.get("thread_id") == request.thread_id
            ]
            if request.thread_id
            else []
        )
        if len(bound_records) > 1:
            raise FulcrumError(
                "TASK_CORRUPT",
                "current native task has multiple managed task records",
                exit_code=4,
            )
        task_record_id = (
            bound_records[0].id if bound_records else _unique_task_id(ledger)
        )
        retained_creation_cwd = (
            bound_records[0].fc.get("creation_cwd")
            if bound_records and bound_records[0].fc
            else None
        )
        creation_cwd = (
            str(retained_creation_cwd)
            if retained_creation_cwd
            else str(
                (request.instance.instance_root / "threads" / receipt_id).resolve(
                    strict=False
                )
            )
        )
        config, project = _project_config(request, str(work.fc.get("project")))
        model, effort, model_origin = _select_model(
            request, config, project, work.fc, role
        )
        planned = {
            "bead_id": bead_id,
            "role": role,
            "description": description,
            "task_record_id": task_record_id,
            "creation_cwd": creation_cwd,
            "model": model,
            "effort": effort,
            "model_origin": model_origin,
            "turn_input": None,
        }
        operation, reused = ledger.create_operation(
            request,
            bead_id=bead_id,
            planned=planned,
            next_action="Create or bind the exact native task, then cook its complete role input.",
        )
        retained = operation.operation.get("planned")
        if not isinstance(retained, Mapping):
            raise FulcrumError(
                "OPERATION_CORRUPT", "entry receipt has no plan", exit_code=4
            )
        if reused:
            if operation.operation.get("state") in {
                "completed",
                "failed",
                "uncertain",
                "cancelled",
            }:
                return _operation_result(operation)
            task_record_id = str(retained["task_record_id"])
            creation_cwd = str(retained["creation_cwd"])
            model = str(retained["model"])
            effort = str(retained["effort"])
            model_origin = str(retained["model_origin"])
        root = str(Path(str(project["root"])).resolve(strict=True))
        if request.thread_id:
            thread_id = request.thread_id
            expected_title = role_title(role, bead_id, work.title)
            try:
                native = _runtime_call(
                    request,
                    lambda runtime: _name_current_task(
                        runtime, thread_id, expected_title
                    ),
                )
            except FulcrumError as error:
                if error.code not in DEGRADABLE_CODES:
                    raise
                return _entry_degraded(
                    ledger, operation, request, role, bead_id, thread_id, error
                )
            if routed_leader:
                spec = TaskSpec(
                    creation_cwd=creation_cwd,
                    cwd=root,
                    project_id=(
                        str(project["codex_project_id"])
                        if project.get("codex_project_id")
                        else None
                    ),
                    workspace_roots=(root,),
                    title=expected_title,
                    model=model,
                    effort=effort,
                )
        else:
            Path(creation_cwd).mkdir(parents=True, exist_ok=True)
            spec = TaskSpec(
                creation_cwd=creation_cwd,
                cwd=root,
                project_id=(
                    str(project["codex_project_id"])
                    if project.get("codex_project_id")
                    else None
                ),
                workspace_roots=(root,),
                title=role_title(role, bead_id, work.title),
                model=model,
                effort=effort,
            )
            try:
                native = _runtime_call(
                    request,
                    lambda runtime: _create_and_configure(
                        runtime, spec, operation.operation.get("external")
                    ),
                )
            except FulcrumError as error:
                if error.code not in DEGRADABLE_CODES:
                    raise
                return _entry_degraded(
                    ledger, operation, request, role, bead_id, None, error
                )
            thread_id = native.id
            operation = ledger.update_operation(
                operation,
                step="native_task_configured",
                external={"adapter": "codex", "thread_id": thread_id},
                result={"task": native.to_dict()},
                next_action="Cook and retain the exact role input before starting its turn.",
            )
            from fulcrum.deterministic import trigger_crash_boundary

            trigger_crash_boundary(request, operation.id, "task_created")
        try:
            cooked = _cook_role(
                ledger,
                request,
                work,
                role,
                thread_id,
                receipt_id,
                start_operation=receipt_id,
            )
        except (LedgerFailure, FulcrumError) as error:
            if isinstance(error, FulcrumError) and error.code not in DEGRADABLE_CODES:
                raise
            return _entry_degraded(
                ledger, operation, request, role, bead_id, thread_id, error
            )
        instructions = cooked["instructions"]
        planned = {**dict(retained), "turn_input": instructions, "thread_id": thread_id}
        operation = ledger.update_operation(
            operation,
            step="role_input_retained",
            planned=planned,
            next_action="Bind ownership before sending the retained native turn.",
        )
        updated_work = _bind_work(
            ledger,
            work,
            role=role,
            thread_id=thread_id,
            ownership_operation=receipt_id,
            compiled_description=cooked["description"],
        )
        _bind_task(
            ledger,
            task_record_id=task_record_id,
            work=updated_work,
            role=role,
            native=native,
            ownership_operation=receipt_id,
            creation_cwd=creation_cwd,
            model=model,
            effort=effort,
            model_origin=model_origin,
        )
        if role == "warden" and request.arguments.get("origin") == "dispatch":
            from fulcrum.deterministic import trigger_crash_boundary

            trigger_crash_boundary(request, operation.id, "handoff_owner_written")
        turn = None
        if request.thread_id is None or routed_leader:
            assert spec is not None
            try:
                turn = _runtime_call(
                    request,
                    lambda runtime: _start_or_recover(
                        runtime,
                        thread_id,
                        spec,
                        receipt_id,
                        instructions,
                    ),
                )
            except FulcrumError as error:
                if error.code not in DEGRADABLE_CODES:
                    raise
                return _entry_degraded(
                    ledger, operation, request, role, bead_id, thread_id, error
                )
            task = ledger.show(task_record_id)
            if task is not None and task.fc:
                task_fc = dict(task.fc)
                task_fc["last_turn"] = turn.to_dict()
                task_fc["last_transition"] = receipt_id
                ledger.update_fc(task.id, task_fc)
            from fulcrum.deterministic import trigger_crash_boundary

            trigger_crash_boundary(request, operation.id, "turn_started")
        operation = ledger.update_operation(
            operation,
            state="completed",
            step=("current_task_bound" if turn is None else "role_turn_started"),
            result={
                "bead_id": bead_id,
                "thread_id": thread_id,
                "task_record_id": task_record_id,
                "role": role,
                "ownership_operation": receipt_id,
                "instructions": instructions,
                "model": model,
                "effort": effort,
                "model_origin": model_origin,
                "turn": turn.to_dict() if turn else None,
            },
            next_action="Carry out the complete cooked role responsibility.",
        )
        return _operation_result(operation)

    def _resolve_work(
        self,
        request: ParsedRequest,
        ledger: Ledger,
        role: str,
        description: str,
    ) -> str:
        supplied = request.input.get("bead")
        if supplied:
            record = ledger.show(str(supplied))
            if record is None:
                raise FulcrumError.invalid("NOT_FOUND", f"unknown work {supplied}")
            if record.kind == "work":
                return record.id
            adopted = WorkService().adopt(
                replace(
                    request,
                    command=("work", "adopt"),
                    arguments={"id": record.id, "role": role},
                    input={},
                    request_id=_derived_request_id(request, "adopt"),
                )
            )
            result = adopted.result or {}
            nested = result.get("result")
            if not isinstance(nested, Mapping):
                raise FulcrumError(
                    "ENTRY_UNCERTAIN", "adoption returned no work", exit_code=4
                )
            return str(nested["bead_id"])
        project = _entry_project(request)
        title = " ".join(description.strip().splitlines()[0].split())
        created = WorkService().create(
            replace(
                request,
                command=("work", "create"),
                arguments={},
                input={
                    "title": title,
                    "outcome": description,
                    "project": project,
                    "acceptance": [
                        "Acceptance is not yet specified; establish proportionate observable checks before risky work."
                    ],
                    "requested_role": role,
                },
                request_id=_derived_request_id(request, "work"),
                project=project,
            )
        )
        result = created.result or {}
        nested = result.get("result")
        if not isinstance(nested, Mapping):
            raise FulcrumError(
                "ENTRY_UNCERTAIN", "work creation returned no bead", exit_code=4
            )
        return str(nested["bead_id"])

    def context(self, request: ParsedRequest) -> CommandResult:
        if request.arguments.get("role") == "marshal":
            from fulcrum.leadership import marshal_context

            return marshal_context(request)
        role = request.arguments.get("role")
        bead_id = request.arguments.get("bead") or request.input.get("bead")
        if bead_id:
            ledger = _ledger(request)
            record = ledger.show(str(bead_id))
            if record is None or record.kind != "work":
                raise FulcrumError.invalid("NOT_FOUND", f"unknown work {bead_id}")
            fc = record.fc or {}
            return CommandResult.query(
                {
                    "bead_id": record.id,
                    "role": fc.get("role"),
                    "thread_id": (
                        fc.get("owner") if fc.get("owner") != "HUMAN" else None
                    ),
                    "ownership_operation": fc.get("ownership_operation"),
                    "instructions": record.native.get("description"),
                    "work": _context_work(record),
                    "registered": True,
                }
            )
        if role:
            return CommandResult.query(
                {
                    "bead_id": None,
                    "thread_id": request.thread_id,
                    "role": role,
                    "ownership_operation": None,
                    "instructions": fallback_instructions(str(role)),
                    "registered": False,
                    "gaps": ["no managed bead was selected"],
                }
            )
        raise FulcrumError.invalid("INVALID_INPUT", "context requires --bead or --role")

    def hook_context(self, request: ParsedRequest) -> CommandResult:
        event = request.input
        response: dict[str, Any] = {"continue": True}
        if (
            event.get("hook_event_name") != "SessionStart"
            or event.get("source") != "compact"
        ):
            return CommandResult.query(response)
        thread_id = event.get("session_id")
        if not isinstance(thread_id, str) or not thread_id:
            return CommandResult.query(response)
        try:
            ledger = _ledger(request)
            tasks = [
                item
                for item in ledger.list_records(kind="task", limit=0)
                if item.fc
                and item.fc.get("thread_id") == thread_id
                and item.fc.get("deleted_at") is None
            ]
            if len(tasks) != 1:
                return CommandResult.query(response)
            task_fc = tasks[0].fc
            bead_id = task_fc.get("work_bead") if task_fc else None
            work = ledger.show(str(bead_id)) if bead_id else None
            if work is None or work.kind != "work" or not work.fc:
                return CommandResult.query(response)
            if work.fc.get("owner") != thread_id:
                return CommandResult.query(response)
            instructions = work.native.get("description")
            if not isinstance(instructions, str) or not instructions:
                return CommandResult.query(response)
            response["hookSpecificOutput"] = {
                "hookEventName": "SessionStart",
                "additionalContext": instructions,
            }
        except Exception:
            pass
        return CommandResult.query(response)


def _cook_role(
    ledger: Ledger,
    request: ParsedRequest,
    work: LedgerRecord,
    role: str,
    thread_id: str,
    ownership_operation: str,
    *,
    start_operation: str,
) -> dict[str, str]:
    fc = work.fc or {}
    _, project = _project_config(request, str(fc.get("project")))
    worktree = fc.get("worktree")
    workspace = (
        str(worktree.get("path"))
        if isinstance(worktree, Mapping) and worktree.get("path")
        else str(project["root"])
    )
    acceptance = fc.get("acceptance")
    acceptance_text = (
        "\n".join(f"- {item}" for item in acceptance)
        if isinstance(acceptance, list) and acceptance
        else "- Acceptance is not yet specified; resolve it before risky work."
    )
    evidence = {
        "summary": fc.get("summary"),
        "last_progress": fc.get("last_progress"),
        "finish": fc.get("finish"),
        "handoff": fc.get("handoff"),
        "delivery": fc.get("delivery"),
    }
    try:
        selected_memory = select_memory(
            ledger,
            project_ids=(str(fc.get("project")),),
            role=role,
        )
    except LedgerFailure:
        selected_memory = {"items": []}
    work_context = _render_facts(fc.get("context"), "No additional task context.")
    memory_context = (
        work_context
        if not selected_memory["items"]
        else work_context
        + "\n\nCurated memory:\n"
        + render_selected_memory(selected_memory)
    )
    variables = {
        "title": work.title,
        "outcome": str(
            fc.get("outcome") or work.native.get("description") or work.title
        ),
        "project": str(fc.get("project")),
        "workspace": workspace,
        "acceptance": acceptance_text,
        "current_evidence": _render_facts(evidence, "No prior evidence."),
        "blockers": _render_facts(fc.get("waiting"), "No current blocker."),
        "context": memory_context,
        "next_action": _role_next_action(role),
        "bead": work.id,
        "thread": thread_id,
        "ownership_operation": ownership_operation,
    }
    asset = files("fulcrum").joinpath("formulas", f"fulcrum-{role}.formula.json")
    cooked = ledger.cook(str(asset), variables)
    correlation = (
        f"[Fulcrum operation {start_operation}; ownership operation "
        f"{ownership_operation}]"
    )
    return {
        "title": cooked["title"],
        "description": cooked["description"],
        "instructions": cooked["description"] + "\n\n" + correlation,
    }


async def _name_current_task(runtime: Any, thread_id: str, title: str) -> TaskFacts:
    await runtime.transport.set_name(thread_id, title)
    facts = await runtime.inspect_task(thread_id)
    if facts.title != title:
        raise FulcrumError(
            "RUNTIME_UNCERTAIN",
            f"native task {thread_id} title could not be verified",
            exit_code=4,
            state=CommandState.UNCERTAIN,
        )
    return facts


def _bind_work(
    ledger: Ledger,
    work: LedgerRecord,
    *,
    role: str,
    thread_id: str,
    ownership_operation: str,
    compiled_description: str,
) -> LedgerRecord:
    fc = dict(work.fc or {})
    prior_phase = fc.get("phase")
    if work.status == "closed":
        if role not in {"sage", "mason"}:
            raise FulcrumError(
                "OWNERSHIP_CONFLICT",
                "only scoped Sage or Mason investigation may enter closed work directly",
                exit_code=5,
            )
        fc["interrupted_work"] = {
            "status": work.status,
            "phase": fc.get("phase"),
            "disposition": fc.get("disposition"),
            "owner": fc.get("owner"),
        }
    fc.update(
        {
            "owner": thread_id,
            "role": role,
            "ownership_operation": ownership_operation,
            "phase": _phase_for(role),
            "last_transition": ownership_operation,
            "next_action": f"Complete the active {role} responsibility and call fulcrum finish.",
            "compiled_role": {
                "formula": f"fulcrum-{role}",
                "thread_id": thread_id,
                "ownership_operation": ownership_operation,
                "compiled_at": utc_now(),
            },
        }
    )
    handoff = fc.get("handoff")
    if prior_phase == "handoff" and role == "warden" and isinstance(handoff, Mapping):
        fc["handoff"] = {
            **dict(handoff),
            "to_thread": thread_id,
            "to_ownership_operation": ownership_operation,
            "state": "transferred",
            "transferred_at": utc_now(),
        }
    return ledger.update_fc(
        work.id,
        fc,
        assignee=thread_id,
        status="in_progress",
        title=work.title,
        description=compiled_description,
    )


def _bind_task(
    ledger: Ledger,
    *,
    task_record_id: str,
    work: LedgerRecord,
    role: str,
    native: TaskFacts,
    ownership_operation: str,
    creation_cwd: str,
    model: str,
    effort: str,
    model_origin: str,
) -> LedgerRecord:
    fc = {
        "kind": "task",
        "owner": native.id,
        "thread_id": native.id,
        "role": role,
        "work_bead": work.id,
        "ownership_operation": ownership_operation,
        "creation_operation": ownership_operation,
        "creation_cwd": creation_cwd,
        "model": model,
        "effort": effort,
        "model_origin": model_origin,
        "associated_beads": [],
        "replaced_by": None,
        "missing_finish_reminder": None,
        "archive_state": "pending",
        "archive_due_at": None,
        "archive_operation": None,
        "last_observed": native.to_dict(),
        "last_turn": None,
        "last_runtime_event_at": utc_now(),
        "last_substantive_progress_at": utc_now(),
        "last_inspection_at": None,
        "last_checkpoint_operation": None,
        "last_finish_reminder_operation": None,
        "recovery_requested_operation": None,
        "last_transition": ownership_operation,
    }
    existing = ledger.show(task_record_id)
    if existing is None:
        return ledger.create_record(
            record_id=task_record_id,
            kind="task",
            title=f"Managed task: {native.title or role_title(role, work.id, work.title)}",
            description=f"Native Codex task {native.id} for {work.id}.",
            owner=native.id,
            fc=fc,
            external_ref=f"fulcrum:thread:{native.id}",
        )
    if (
        existing.kind != "task"
        or not existing.fc
        or existing.fc.get("thread_id") != native.id
    ):
        raise FulcrumError(
            "REQUEST_CONFLICT", "planned task record is occupied", exit_code=5
        )
    fc["creation_operation"] = existing.fc.get("creation_operation")
    fc["creation_cwd"] = existing.fc.get("creation_cwd")
    return ledger.update_fc(existing.id, fc, assignee=native.id)


def _valid_existing_entry(
    ledger: Ledger,
    work: LedgerRecord,
    requested_thread: str | None,
    role: str,
) -> tuple[str, str] | None:
    fc = work.fc or {}
    owner = fc.get("owner")
    acquisition = fc.get("ownership_operation")
    if (
        isinstance(owner, str)
        and owner != "HUMAN"
        and isinstance(acquisition, str)
        and fc.get("role") == role
        and (requested_thread is None or requested_thread == owner)
    ):
        tasks = [
            item
            for item in ledger.list_records(kind="task", limit=0)
            if item.fc
            and item.fc.get("thread_id") == owner
            and item.fc.get("work_bead") == work.id
            and item.fc.get("ownership_operation") == acquisition
            and item.fc.get("deleted_at") is None
            and item.fc.get("replaced_by") is None
        ]
        if len(tasks) == 1:
            return owner, acquisition
    return None


def _entry_configuration_compatible(
    task: LedgerRecord,
    project: Mapping[str, Any],
    model: str,
    effort: str,
) -> bool:
    fc = task.fc or {}
    if fc.get("model") != model or fc.get("effort") != effort:
        return False
    root_value = project.get("root")
    if not isinstance(root_value, str):
        return False
    expected_root = str(Path(root_value).resolve(strict=True))
    observed = fc.get("last_observed")
    if not isinstance(observed, Mapping):
        return False
    roots = observed.get("workspace_roots")
    return isinstance(roots, list) and expected_root in {
        str(Path(str(item)).resolve(strict=False)) for item in roots
    }


def _return_prior_work(
    ledger: Ledger,
    thread_id: str,
    *,
    except_bead: str,
    transition_operation: str,
) -> None:
    tasks = [
        item
        for item in ledger.list_records(kind="task", limit=0)
        if item.fc
        and item.fc.get("thread_id") == thread_id
        and item.fc.get("work_bead") != except_bead
    ]
    for task in tasks:
        prior_id = task.fc.get("work_bead") if task.fc else None
        prior = ledger.show(str(prior_id)) if prior_id else None
        if prior is None or prior.status == "closed" or not prior.fc:
            continue
        if prior.fc.get("owner") != thread_id:
            continue
        fc = dict(prior.fc)
        fc.update(
            {
                "owner": _marshal_owner(ledger),
                "role": "marshal",
                "phase": "backlog",
                "waiting": {
                    "reason": "worker entered a different explicit bead",
                    "reconsider_when": "Marshal selects continuation",
                },
                "next_action": "Marshal must decide how the checkpointed work continues.",
                "last_transition": transition_operation,
            }
        )
        ledger.update_fc(prior.id, fc, assignee=str(fc["owner"]), status="open")


def _role_next_action(role: str) -> str:
    return f"Complete the active {role} responsibility and call fulcrum finish."


def _context_work(record: LedgerRecord) -> dict[str, Any]:
    fc = record.fc or {}
    return {
        "id": record.id,
        "title": record.title,
        "status": record.status,
        "project": fc.get("project"),
        "outcome": fc.get("outcome"),
        "acceptance": fc.get("acceptance", []),
        "owner": fc.get("owner"),
        "role": fc.get("role"),
        "phase": fc.get("phase"),
        "next_action": fc.get("next_action"),
        "waiting": fc.get("waiting"),
        "last_progress": fc.get("last_progress"),
        "delivery": fc.get("delivery"),
        "interrupted_work": fc.get("interrupted_work"),
    }


def _render_facts(value: Any, empty: str) -> str:
    if value is None or value == [] or value == {}:
        return empty
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "\n".join(f"- {item}" for item in value)
    return json.dumps(value, ensure_ascii=False, indent=2)


def _entry_project(request: ParsedRequest) -> str:
    manager = ConfigurationManager(request.instance.config_path)
    document, _ = manager.load()
    projects = manager.effective(document)["projects"]
    if request.project:
        selected = projects.get(request.project)
        if not isinstance(selected, Mapping):
            raise FulcrumError.invalid(
                "INVALID_PROJECT", f"project {request.project} is not enrolled"
            )
        return request.project
    enabled = [
        key
        for key, value in projects.items()
        if isinstance(value, Mapping) and value.get("enabled", True)
    ]
    if len(enabled) != 1:
        raise FulcrumError.invalid(
            "INVALID_PROJECT",
            "enter requires --project when there is not exactly one enabled project",
        )
    return str(enabled[0])


def _derived_request_id(request: ParsedRequest, purpose: str) -> str:
    assert request.request_id is not None
    return str(uuid.uuid5(uuid.UUID(request.request_id), purpose))


def _unique_task_id(ledger: Ledger) -> str:
    for _ in range(20):
        candidate = random_record_id()
        if ledger.show(candidate) is None:
            return candidate
    raise FulcrumError(
        "ID_ALLOCATION_FAILED", "could not reserve a task record ID", exit_code=4
    )


def _marshal_owner(ledger: Ledger) -> str:
    control = ledger.show("fc-system")
    if control and control.fc and control.fc.get("marshal_thread"):
        return str(control.fc["marshal_thread"])
    return "HUMAN"


def _leader_thread(ledger: Ledger, role: str) -> str | None:
    control = ledger.show("fc-system")
    key = f"{role}_thread"
    value = control.fc.get(key) if control and control.fc else None
    return str(value) if isinstance(value, str) and value else None


def _ledger(request: ParsedRequest) -> Ledger:
    if request.instance.brain_root is None:
        raise FulcrumError(
            "LEDGER_UNAVAILABLE", "brain root is unavailable", exit_code=4
        )
    manager = ConfigurationManager(request.instance.config_path)
    document, _ = manager.load()
    beads = manager.effective(document)["beads"]
    executable = beads.get("executable") if isinstance(beads, Mapping) else None
    return Ledger(
        request.instance.brain_root,
        executable=str(executable) if executable else None,
        timeout=request.timeout,
    )


def _entry_degraded(
    ledger: Ledger,
    operation: OperationRecord,
    request: ParsedRequest,
    role: str,
    bead_id: str,
    thread_id: str | None,
    error: LedgerFailure | FulcrumError,
) -> CommandResult:
    uncertain = (
        error.uncertain
        if isinstance(error, LedgerFailure)
        else error.state is CommandState.UNCERTAIN
    )
    code = error.category if isinstance(error, LedgerFailure) else error.code
    try:
        operation = ledger.update_operation(
            operation,
            state="uncertain" if uncertain else "failed",
            step="entry_degraded",
            error={
                "code": code,
                "message": str(error),
                "retryable": error.retryable,
            },
            next_action=(
                "Inspect the retained receipt and native facts before retrying."
                if uncertain
                else "Repair the unavailable dependency before registering the role again."
            ),
        )
    except LedgerFailure:
        pass
    return _degraded(
        request,
        role,
        reason=str(error),
        bead_id=bead_id,
        thread_id=thread_id,
        operation=operation.id,
    )


def _degraded(
    request: ParsedRequest,
    role: str,
    *,
    reason: str,
    bead_id: str | None,
    thread_id: str | None,
    operation: str | None = None,
) -> CommandResult:
    return CommandResult(
        ok=True,
        state=CommandState.DEGRADED,
        operation_id=operation,
        request_id=request.request_id,
        result={
            "bead_id": bead_id,
            "thread_id": thread_id,
            "role": role,
            "ownership_operation": None,
            "instructions": fallback_instructions(role),
            "operation": operation,
            "registered": False,
            "gaps": [reason],
        },
        warnings=("role registration is incomplete; no replay was queued",),
    )


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
