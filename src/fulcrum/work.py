"""Work graph, ownership, progress, and terminal disposition operations."""

from __future__ import annotations

import os
from pathlib import Path

from fulcrum.coordination import coordinated

from collections.abc import Mapping, Sequence
from typing import Any

from fulcrum.analytics import AnalyticsService
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

WORK_UPDATE_FIELDS: set[str] = {
    "title",
    "outcome",
    "acceptance",
    "summary",
    "size",
    "overlap_tags",
    "context",
    "priority",
    "intake",
    "models",
}
DISPOSITIONS: set[str] = {
    "answered",
    "delivered",
    "reduced_scope",
    "findings",
    "rejected",
    "duplicate",
    "cancelled",
}
PROGRESS_KINDS: set[str] = {"investigation", "source", "validation", "blocker"}


class WorkService:
    @coordinated
    def enter(self, request: ParsedRequest) -> CommandResult:
        """Bind the invoking task as Weaver; this command never creates a task."""

        role = str(request.arguments.get("role") or "")
        if role != "weaver":
            raise FulcrumError(
                "ROLE_NOT_ENTERABLE",
                "only Weaver may be entered directly; downstream roles are dispatched from approved bead scope",
                exit_code=5,
            )
        task_id = request.actor.task_id or request.thread_id
        if request.actor.kind != "task" or not task_id:
            raise FulcrumError(
                "SAME_TASK_REQUIRED",
                "Weaver entry requires the invoking native task identity",
                exit_code=5,
            )
        description = request.input.get("description")
        if not isinstance(description, str) or not description.strip():
            raise _invalid("description", "is required")
        title = " ".join(description.strip().splitlines()[0].split())[:120]
        ledger = _ledger(request)
        supplied = request.input.get("bead")
        existing = ledger.show(str(supplied)) if supplied else None
        if supplied and (
            existing is None or existing.kind != "work" or not existing.fc
        ):
            raise FulcrumError.invalid("NOT_FOUND", f"unknown managed work {supplied}")
        if existing is not None:
            project = str((existing.fc or {}).get("project") or "")
            _project_from_request(request, {"project": project})
            bead_id = existing.id
        else:
            project = _project_for_entry(request)
            bead_id = _unique_id(ledger)
        operation, reused = ledger.create_operation(
            request,
            bead_id=bead_id,
            planned={
                "bead_id": bead_id,
                "role": "weaver",
                "task_id": task_id,
                "project": project,
                "creates_native_task": False,
            },
            next_action="Bind the invoking task as the only Weaver for this work.",
        )
        if reused and operation.operation.get("state") in {
            "completed",
            "failed",
            "uncertain",
            "cancelled",
        }:
            return _operation_result(operation)
        for candidate in ledger.list_records(kind="work", limit=0):
            assignment = ((candidate.fc or {}).get("desktop") or {}).get("assignment")
            if (
                isinstance(assignment, Mapping)
                and assignment.get("task_id") == task_id
                and candidate.id != bead_id
                and assignment.get("state")
                in {"reserved", "issuing", "active", "uncertain"}
            ):
                raise FulcrumError(
                    "OWNERSHIP_CONFLICT",
                    "the invoking task already owns different Fulcrum work",
                    exit_code=5,
                    details={"bead_id": candidate.id, "task_id": task_id},
                )
        if existing is None:
            spec = _work_spec(
                {
                    "title": title or "Prepare implementation scope",
                    "outcome": description,
                    "acceptance": [],
                    "requested_role": "executor",
                },
                project=project,
                key="entry",
            )
            existing = self._create_planned_work(
                ledger,
                operation,
                bead_id,
                spec,
                owner=str(task_id),
                workflow_root=bead_id,
                parent=None,
                issue_type="task",
                request=request,
                check_existing=reused,
            )
        assert existing.fc is not None
        fc = dict(existing.fc)
        desktop = dict(fc.get("desktop") or {})
        retained = desktop.get("assignment")
        if isinstance(retained, Mapping):
            raise FulcrumError(
                "OWNERSHIP_CONFLICT",
                "work already has an active assignment",
                exit_code=5,
                details={"bead_id": bead_id, "assignment": dict(retained)},
            )
        manager = ConfigurationManager(request.instance.config_path)
        document, _ = manager.load()
        project_config = manager.effective(document)["projects"][project]
        workspace = str(project_config["root"])
        assignment = {
            "assignment_token": operation.id,
            "role": "weaver",
            "task_id": str(task_id),
            "session_id": os.environ.get("CODEX_SESSION_ID"),
            "workspace": workspace,
            "project": project,
            "capacity_class": "entry",
            "entry_mode": "same_task",
            "state": "active",
            "registered_at": utc_now(),
        }
        desktop["assignment"] = assignment
        from fulcrum.desktop_protocol import DesktopProtocolService, role_title

        protocol_service = DesktopProtocolService(ledger)
        native_title = role_title("weaver", bead_id, title)
        title_action = protocol_service._append_action(
            desktop,
            record_id=bead_id,
            executor="weaver",
            tool="set_thread_title",
            arguments={"threadId": str(task_id), "title": native_title},
            purpose=f"weaver_title:{operation.id}",
            expected_result={"threadId": str(task_id), "title": native_title},
            assignment_token=operation.id,
            reporting={"kind": "same_task_weaver_title"},
        )
        fc.update(
            {
                "desktop": desktop,
                "owner": str(task_id),
                "role": "weaver",
                "ownership_operation": operation.id,
                "phase": "working",
                "requested_role": "executor",
                "next_action": "Investigate and finish with ready, answered, planned, or blocked.",
                "last_transition": operation.id,
            }
        )
        entered = ledger.update_fc(
            bead_id, fc, assignee=str(task_id), status="in_progress"
        )
        instructions = (
            f"You are the Weaver for {bead_id} in this existing task. Do not create or "
            "delegate to another Weaver. Investigate the human request, but do not "
            "implement repository changes. Retain a concise behavioral summary, "
            "observable acceptance checks, evidence, and optional implementation notes "
            "with Fulcrum finish. Use ownership operation "
            f"{operation.id} for later mutations."
        )
        operation = ledger.update_operation(
            operation,
            state="completed",
            step="same_task_weaver_entered",
            result={
                "bead_id": bead_id,
                "role": "weaver",
                "task_id": task_id,
                "ownership_operation": operation.id,
                "native_task_created": False,
                "title_action": protocol_service._action_response(
                    request, title_action
                ),
                "instructions": instructions,
                "work": work_view(ledger, entered),
            },
            next_action="Investigate in the invoking task and finish the Weaver responsibility.",
        )
        return _operation_result(operation)

    @coordinated
    def create(self, request: ParsedRequest) -> CommandResult:
        payload = dict(request.input)
        _known(
            payload,
            {
                "title",
                "outcome",
                "project",
                "acceptance",
                "requested_role",
                "priority",
                "context",
                "intake",
                "models",
                "children",
                "dependencies",
                "size",
                "overlap_tags",
                "summary",
            },
        )
        project = _project_from_request(request, payload)
        root_spec = _work_spec(payload, project=project, key="root")
        children = payload.get("children", [])
        if not isinstance(children, list) or not all(
            isinstance(item, Mapping) for item in children
        ):
            raise _invalid("children", "must be an array of work objects")
        child_specs: dict[str, dict[str, Any]] = {}
        for item in children:
            child = dict(item)
            _known(
                child,
                {
                    "key",
                    "title",
                    "outcome",
                    "acceptance",
                    "requested_role",
                    "priority",
                    "context",
                    "intake",
                    "models",
                    "depends_on",
                    "size",
                    "overlap_tags",
                    "summary",
                },
            )
            key = child.get("key")
            if (
                not isinstance(key, str)
                or not key
                or key in child_specs
                or key == "root"
            ):
                raise _invalid("children.key", "must be a unique nonempty key")
            child_specs[key] = _work_spec(child, project=project, key=key)
        _validate_local_graph(child_specs)
        ledger = _ledger(request)
        external_dependencies = {
            target
            for spec in (root_spec, *child_specs.values())
            for target in spec.get("depends_on", [])
            if target not in child_specs
        }
        missing_dependencies = sorted(
            target for target in external_dependencies if ledger.show(target) is None
        )
        if missing_dependencies:
            raise _invalid(
                "dependencies",
                f"unknown dependency targets: {', '.join(missing_dependencies)}",
            )
        reserved: set[str] = set()
        root_id = _unique_id(ledger, reserved=reserved)
        reserved.add(root_id)
        children_by_key: dict[str, str] = {}
        for key in child_specs:
            child_id = _unique_id(ledger, reserved=reserved)
            reserved.add(child_id)
            children_by_key[key] = child_id
        planned = {
            "root_id": root_id,
            "children_by_key": children_by_key,
            "edges": _planned_edges(child_specs),
        }
        operation, reused = ledger.create_operation(
            request,
            bead_id=str(planned["root_id"]),
            planned=planned,
            next_action="Create the planned work records and dependency edges.",
        )
        if reused and operation.operation.get("state") in {
            "completed",
            "failed",
            "uncertain",
            "cancelled",
        }:
            return _operation_result(operation)
        retained = operation.operation.get("planned")
        if not isinstance(retained, Mapping):
            raise FulcrumError(
                "OPERATION_CORRUPT",
                "work creation receipt has no planned IDs",
                exit_code=4,
            )
        root_id = str(retained["root_id"])
        children_by_key = {
            str(key): str(value)
            for key, value in _mapping(
                retained["children_by_key"], "planned.children_by_key"
            ).items()
        }
        owner = _marshal_or_human(ledger)
        self._create_planned_work(
            ledger,
            operation,
            root_id,
            root_spec,
            owner=owner,
            workflow_root=root_id,
            parent=None,
            issue_type="epic" if child_specs else "task",
            request=request,
            check_existing=reused,
        )
        for key, spec in child_specs.items():
            self._create_planned_work(
                ledger,
                operation,
                children_by_key[key],
                spec,
                owner=owner,
                workflow_root=root_id,
                parent=root_id,
                issue_type="task",
                request=request,
                check_existing=reused,
            )
        for key, spec in child_specs.items():
            child_id = children_by_key[key]
            expected = [
                children_by_key.get(value, value) for value in spec["depends_on"]
            ]
            _reconcile_dependencies(ledger, child_id, expected)
            ledger.set_parent(child_id, root_id)
        root_dependencies = root_spec.get("depends_on", [])
        if root_dependencies:
            _reconcile_dependencies(
                ledger, root_id, [str(item) for item in root_dependencies]
            )

        operation = ledger.update_operation(
            operation,
            state="completed",
            step="graph_verified",
            result={"bead_id": root_id, "children_by_key": children_by_key},
            next_action="Inspect or dispatch the created work through Fulcrum.",
        )
        return _operation_result(operation)

    def _create_planned_work(
        self,
        ledger: Ledger,
        operation: OperationRecord,
        bead_id: str,
        spec: Mapping[str, Any],
        *,
        owner: str,
        workflow_root: str,
        parent: str | None,
        issue_type: str,
        request: ParsedRequest,
        check_existing: bool,
    ) -> LedgerRecord:
        existing = ledger.show(bead_id) if check_existing else None
        if existing is not None:
            fc = existing.fc
            if (
                fc
                and fc.get("kind") == "work"
                and _mapping(fc.get("origin"), "origin").get("creation_operation")
                == operation.id
            ):
                if parent is not None:
                    return ledger.set_parent(bead_id, parent)
                return existing
            raise FulcrumError(
                "REQUEST_CONFLICT",
                f"planned work ID {bead_id} is already occupied",
                exit_code=5,
                details={"id": bead_id},
            )
        fc = _initial_fc(
            spec,
            bead_id=bead_id,
            workflow_root=workflow_root,
            owner=owner,
            operation_id=operation.id,
            request=request,
        )
        return ledger.create_record(
            record_id=bead_id,
            kind="work",
            title=str(spec["title"]),
            description=str(spec["outcome"]),
            owner=owner,
            fc=fc,
            issue_type=issue_type,
            priority=int(spec["priority"]),
            acceptance=_render_acceptance(spec["acceptance"]),
            labels=(f"project:{spec['project']}",),
            parent=parent,
        )

    @coordinated
    def show(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        record = ledger.show(str(request.arguments["id"]))
        if record is None or (record.kind is not None and record.kind != "work"):
            raise FulcrumError.invalid(
                "NOT_FOUND", f"unknown work {request.arguments['id']}"
            )
        return CommandResult.query(_authorized_work_view(ledger, record, request))

    @coordinated
    def list(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        downstream = _downstream_assignment(ledger, request)
        limit = int(request.arguments.get("limit", 20))
        rows: list[dict[str, Any]] = []
        for record in ledger.list_records(limit=0):
            if record.kind in {"control", "task", "analytics", "operation"}:
                continue
            if downstream is not None and record.id != downstream[0]:
                continue
            view = work_view(ledger, record)
            if (
                request.project
                and view["fc"]
                and view["fc"].get("project") != request.project
            ):
                continue
            if (
                request.arguments.get("role")
                and view["fc"]
                and view["fc"].get("role") != request.arguments["role"]
            ):
                continue
            if (
                request.arguments.get("owner")
                and view.get("effective_owner") != request.arguments["owner"]
            ):
                continue
            if (
                request.arguments.get("phase")
                and view.get("phase") != request.arguments["phase"]
            ):
                continue
            rows.append(_authorized_work_view(ledger, record, request))
            if limit and len(rows) >= limit:
                break
        return CommandResult.query({"items": rows, "next_cursor": None})

    @coordinated
    def children(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        bead_id = str(request.arguments["id"])
        downstream = _downstream_assignment(ledger, request)
        if downstream is not None:
            raise FulcrumError(
                "AUTHORITY_MISMATCH",
                "downstream workers may inspect only their assigned work contract",
                exit_code=5,
                details={"assigned_bead": downstream[0]},
            )
        parent = ledger.show(bead_id)
        if parent is None:
            raise FulcrumError.invalid("NOT_FOUND", f"unknown work {bead_id}")
        return CommandResult.query(
            {
                "bead_id": bead_id,
                "items": [
                    _authorized_work_view(ledger, item, request)
                    for item in ledger.children(bead_id)
                ],
                "next_cursor": None,
            }
        )

    @coordinated
    def adopt(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        bead_id = str(request.arguments["id"])
        record = ledger.show(bead_id)
        if record is None:
            raise FulcrumError.invalid("NOT_FOUND", f"unknown native issue {bead_id}")
        if record.kind and record.kind != "work":
            raise FulcrumError.invalid("WRONG_RECORD_KIND", f"{bead_id} is not work")
        if record.kind == "work":
            raise FulcrumError(
                "OWNERSHIP_CONFLICT",
                f"{bead_id} is already adopted; use transfer or reopen",
                exit_code=5,
                details={"owner": record.fc.get("owner") if record.fc else None},
            )
        project = _resolve_native_project(request, record)
        role = str(request.arguments.get("role", "weaver"))
        if role not in ROLES:
            raise _invalid("role", "is not a Fulcrum role")
        if role == "weaver":
            raise FulcrumError(
                "WEAVER_TASK_FORBIDDEN",
                "adopt through `fulcrum enter weaver --bead ID` in the invoking task; Fulcrum never creates Weaver tasks",
                exit_code=5,
            )
        operation, reused = ledger.create_operation(
            request,
            bead_id=bead_id,
            planned={"native_snapshot": _native_scope(record)},
            next_action="Normalize the native issue into accountable Fulcrum work.",
        )
        if reused and operation.operation.get("state") in {
            "completed",
            "failed",
            "uncertain",
            "cancelled",
        }:
            return _operation_result(operation)
        owner = _marshal_or_human(ledger)
        fc = _initial_fc(
            {
                "title": record.title,
                "outcome": str(record.native.get("description") or record.title),
                "acceptance": _native_acceptance(record),
                "project": project,
                "requested_role": role,
                "priority": int(record.native.get("priority", 2)),
                "context": [],
                "intake": None,
                "models": {},
                "size": "unknown",
                "overlap_tags": [],
                "summary": None,
            },
            bead_id=bead_id,
            workflow_root=bead_id,
            owner=owner,
            operation_id=operation.id,
            request=request,
        )
        fc["origin"]["native_snapshot"] = _native_scope(record)
        updated = ledger.update_fc(bead_id, fc, assignee=owner, status="open")
        ledger.add_labels(bead_id, ("fc:work", f"project:{project}"))
        operation = ledger.update_operation(
            operation,
            state="completed",
            step="work_adopted",
            result={"bead_id": bead_id, "work": work_view(ledger, updated)},
            next_action="Inspect the adopted work and choose its next role.",
        )
        return _operation_result(operation)

    @coordinated
    def update(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        record = _owned_work(ledger, request, str(request.arguments["id"]))
        payload = dict(request.input)
        _known(payload, WORK_UPDATE_FIELDS)
        if not payload:
            raise _invalid("input", "must change at least one work field")
        _validate_update(payload)
        operation, reused = ledger.create_operation(
            request,
            bead_id=record.id,
            planned={"changed_fields": sorted(payload)},
            next_action="Apply the authorized work scope update.",
        )
        if reused and operation.operation.get("state") in {
            "completed",
            "failed",
            "uncertain",
            "cancelled",
        }:
            return _operation_result(operation)
        fc = dict(record.fc or {})
        scope_change = bool(
            set(payload).intersection({"outcome", "acceptance", "title"})
        )
        for key, value in payload.items():
            if key != "priority" and key != "title":
                fc[key] = value
        if scope_change and isinstance(fc.get("delivery"), Mapping):
            delivery = dict(fc["delivery"])
            delivery["approved_source"] = None
            delivery["approval_invalidated_by"] = operation.id
            fc["delivery"] = delivery
            if fc.get("role") == "warden" and record.status != "closed":
                fc["phase"] = "reviewing"
            fc["next_action"] = "Review the changed scope before continuing delivery."
        fc["last_transition"] = operation.id
        updated = ledger.update_fc(
            record.id,
            fc,
            title=str(payload["title"]) if "title" in payload else None,
            priority=int(payload["priority"]) if "priority" in payload else None,
            description=str(payload["outcome"]) if "outcome" in payload else None,
            acceptance=(
                _render_acceptance(payload["acceptance"])
                if "acceptance" in payload
                else None
            ),
        )
        operation = ledger.update_operation(
            operation,
            state="completed",
            step="work_updated",
            result={
                "bead_id": record.id,
                "changed_fields": sorted(payload),
                "work": work_view(ledger, updated),
            },
            next_action="Continue from the work's current next action.",
        )
        return _operation_result(operation)

    @coordinated
    def dependencies(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        record = _owned_work(ledger, request, str(request.arguments["id"]))
        add = request.input.get("add", [])
        remove = request.input.get("remove", [])
        if (
            not isinstance(add, list)
            or not isinstance(remove, list)
            or not all(isinstance(item, str) for item in list(add) + list(remove))
        ):
            raise _invalid("dependencies", "add and remove must be arrays of bead IDs")
        if set(add).intersection(remove):
            raise _invalid("dependencies", "the same edge cannot be added and removed")
        if record.id in add:
            raise _invalid("dependencies.add", "work cannot depend on itself")
        for target in add:
            if ledger.show(target) is None:
                raise _invalid("dependencies.add", f"unknown dependency {target}")
        current = set(ledger.dependencies(record.id))
        proposed = current.difference(remove).union(add)
        _reject_cycle(ledger, record.id, proposed)
        operation, reused = ledger.create_operation(
            request,
            bead_id=record.id,
            planned={
                "add": list(add),
                "remove": list(remove),
                "result": sorted(proposed),
            },
            next_action="Apply and inspect each planned dependency edge.",
        )
        if reused and operation.operation.get("state") in {
            "completed",
            "failed",
            "uncertain",
            "cancelled",
        }:
            return _operation_result(operation)
        _reconcile_dependencies(ledger, record.id, sorted(proposed))
        fc = dict(record.fc or {})
        fc["last_transition"] = operation.id
        fc["waiting"] = _dependency_waiting(ledger, fc.get("waiting"), proposed)
        ledger.update_fc(record.id, fc)
        operation = ledger.update_operation(
            operation,
            state="completed",
            step="dependencies_verified",
            result={
                "bead_id": record.id,
                "dependencies": sorted(proposed),
                "waiting": fc.get("waiting"),
            },
            next_action="Reconsider the work when an unsatisfied dependency closes.",
        )
        return _operation_result(operation)

    @coordinated
    def close(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        record = _owned_work(ledger, request, str(request.arguments["id"]))
        outcome = request.arguments.get("outcome")
        summary = request.arguments.get("summary")
        if outcome not in DISPOSITIONS:
            raise _invalid("outcome", f"must be one of {sorted(DISPOSITIONS)}")
        if not isinstance(summary, str) or not summary.strip():
            raise _invalid("summary", "is required")
        if outcome == "delivered" and not _delivery_settled(record.fc or {}):
            raise FulcrumError(
                "DELIVERY_NOT_OBSERVED",
                "delivered closure requires observed promotion, synchronization, and cleanup",
                exit_code=5,
                details={"delivery": (record.fc or {}).get("delivery")},
            )
        operation, reused = ledger.create_operation(
            request,
            bead_id=record.id,
            next_action="Seal the terminal disposition and close the native issue.",
        )
        if reused and operation.operation.get("state") in {
            "completed",
            "failed",
            "uncertain",
            "cancelled",
        }:
            return _operation_result(operation)
        existing_cost = (record.fc or {}).get("completion_cost")
        if record.status == "closed" and (
            (record.fc or {}).get("workflow_root") != record.id
            or isinstance(existing_cost, Mapping)
        ):
            operation = ledger.update_operation(
                operation,
                state="completed",
                step="work_already_closed",
                result={
                    "bead_id": record.id,
                    "work": work_view(ledger, record),
                    "completion_cost": existing_cost,
                },
                next_action="No further action is required unless the work is explicitly reopened.",
            )
            return _operation_result(operation)
        fc = dict(record.fc or {})
        fc["phase"] = "done"
        fc["disposition"] = {
            "outcome": outcome,
            "summary": summary,
            "waived_requirements": request.input.get("waived_requirements", []),
            "known_defects": request.input.get("known_defects", []),
            "canonical_bead": request.input.get("canonical_bead"),
            "completed_at": utc_now(),
            "ownership_operation": fc.get("ownership_operation"),
        }
        fc["last_transition"] = operation.id
        updated = ledger.update_fc(record.id, fc, status="closed")
        completion_cost = None
        if fc.get("workflow_root") == record.id:
            completion_cost = AnalyticsService().finalize_root(
                ledger, updated, operation.id
            )
            updated = ledger.show(record.id) or updated
        operation = ledger.update_operation(
            operation,
            state="completed",
            step="work_closed",
            result={
                "bead_id": record.id,
                "work": work_view(ledger, updated),
                "completion_cost": completion_cost,
            },
            next_action="No further action is required unless the work is explicitly reopened.",
        )
        return _operation_result(operation)

    @coordinated
    def reopen(self, request: ParsedRequest) -> CommandResult:
        if request.actor.kind != "human":
            raise FulcrumError(
                "OWNERSHIP_CONFLICT",
                "only a human may explicitly reopen terminal work",
                exit_code=5,
            )
        ledger = _ledger(request)
        bead_id = str(request.arguments["id"])
        record = ledger.show(bead_id)
        if record is None or record.kind != "work" or record.status != "closed":
            raise FulcrumError.invalid(
                "INVALID_REOPEN", f"{bead_id} is not closed Fulcrum work"
            )
        reason = request.arguments.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise _invalid("reason", "is required")
        operation, reused = ledger.create_operation(
            request,
            bead_id=bead_id,
            next_action="Reopen the native issue and establish a new ownership cycle.",
        )
        if reused and operation.operation.get("state") in {
            "completed",
            "failed",
            "uncertain",
            "cancelled",
        }:
            return _operation_result(operation)
        role = request.arguments.get("role")
        if role == "weaver":
            raise FulcrumError(
                "WEAVER_TASK_FORBIDDEN",
                "reopen the bead without a role, then enter Weaver from the invoking task",
                exit_code=5,
            )
        owner = (
            request.thread_id
            if role and request.thread_id
            else _marshal_or_human(ledger)
        )
        ledger.run(("reopen", bead_id), mutating=True)
        current = ledger.show(bead_id) or record
        fc = dict(current.fc or {})
        fc["owner"] = owner
        fc["role"] = role if role else ("marshal" if owner != "HUMAN" else None)
        fc["ownership_operation"] = operation.id
        fc["phase"] = "working" if role and request.thread_id else "backlog"
        fc["reopen_reason"] = reason
        if isinstance(fc.get("plan"), Mapping):
            plan = dict(fc["plan"])
            intervals = list(plan.get("reopen_intervals") or [])
            if isinstance(fc.get("disposition"), Mapping):
                intervals.append(
                    {
                        "disposition": dict(fc["disposition"]),
                        "completion_cost": fc.get("completion_cost"),
                        "reopened_by": operation.id,
                    }
                )
            plan["reopen_intervals"] = intervals
            plan["reopen_requirement"] = {
                "operation_id": operation.id,
                "reason": reason.strip(),
                "state": "requires_approved_refinement",
            }
            fc["plan"] = plan
        fc["last_transition"] = operation.id
        updated = ledger.update_fc(
            bead_id,
            fc,
            assignee=owner,
            status="in_progress" if fc["phase"] == "working" else "open",
        )
        operation = ledger.update_operation(
            operation,
            state="completed",
            step="work_reopened",
            result={
                "bead_id": bead_id,
                "ownership_operation": operation.id,
                "work": work_view(ledger, updated),
            },
            next_action="Continue under the new ownership operation.",
        )
        return _operation_result(operation)

    @coordinated
    def progress(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        bead_id = str(request.input.get("bead") or request.arguments.get("bead") or "")
        record = _owned_work(ledger, request, bead_id)
        kind = request.input.get("kind")
        summary = request.input.get("summary")
        evidence = request.input.get("evidence", [])
        if kind not in PROGRESS_KINDS:
            raise _invalid("kind", f"must be one of {sorted(PROGRESS_KINDS)}")
        if not isinstance(summary, str) or len(summary.strip()) < 8:
            raise _invalid("summary", "must describe substantive progress")
        if (
            not isinstance(evidence, list)
            or not evidence
            or not all(isinstance(item, str) for item in evidence)
        ):
            raise _invalid("evidence", "must be a nonempty array of references")
        operation, reused = ledger.create_operation(
            request,
            bead_id=bead_id,
            next_action="Record the substantive progress evidence.",
        )
        if reused and operation.operation.get("state") in {
            "completed",
            "failed",
            "uncertain",
            "cancelled",
        }:
            return _operation_result(operation)
        fc = dict(record.fc or {})
        fc["last_progress_at"] = utc_now()
        fc["last_progress"] = {"kind": kind, "summary": summary, "evidence": evidence}
        fc["last_transition"] = operation.id
        ledger.update_fc(bead_id, fc)
        operation = ledger.update_operation(
            operation,
            state="completed",
            step="progress_recorded",
            result={
                "bead_id": bead_id,
                "progress": fc["last_progress"],
                "at": fc["last_progress_at"],
            },
            next_action=str(fc.get("next_action") or "Continue the current work."),
        )
        return _operation_result(operation)

    @coordinated
    def report(self, request: ParsedRequest) -> CommandResult:
        payload = dict(request.input)
        _known(
            payload,
            {
                "title",
                "problem",
                "observed_evidence",
                "required_change",
                "acceptance_checks",
                "project",
                "discovered_from",
                "context",
                "intake",
                "report_key",
                "implementation_ready",
                "dependencies",
            },
        )
        for field in ("title", "problem", "observed_evidence", "required_change"):
            if not isinstance(payload.get(field), str) or not payload[field].strip():
                raise _invalid(field, "is required")
        acceptance = payload.get("acceptance_checks", [])
        if not isinstance(acceptance, list) or not all(
            isinstance(item, str) and item.strip() for item in acceptance
        ):
            raise _invalid("acceptance_checks", "must be an array of nonempty strings")
        report_key = payload.get("report_key")
        if not isinstance(report_key, str) or not report_key.strip():
            raise _invalid("report_key", "is required")
        dependencies = payload.get("dependencies", [])
        if not isinstance(dependencies, list) or not all(
            isinstance(item, str) and item for item in dependencies
        ):
            raise _invalid("dependencies", "must be an array of bead IDs")
        project = _project_from_request(request, payload)
        ledger = _ledger(request)
        missing_dependencies = sorted(
            identifier for identifier in dependencies if ledger.show(identifier) is None
        )
        if missing_dependencies:
            raise _invalid(
                "dependencies",
                f"unknown dependency targets: {', '.join(missing_dependencies)}",
            )
        accepted = {
            "title": payload["title"],
            "problem": payload["problem"],
            "observed_evidence": payload["observed_evidence"],
            "required_change": payload["required_change"],
            "acceptance_checks": acceptance,
            "project": project,
            "discovered_from": payload.get("discovered_from"),
            "context": payload.get("context", []),
            "intake": payload.get("intake"),
            "implementation_ready": bool(payload.get("implementation_ready")),
            "dependencies": dependencies,
        }
        for existing in ledger.list_records(kind="work", limit=0):
            report = (existing.fc or {}).get("report")
            if (
                not isinstance(report, Mapping)
                or report.get("report_key") != report_key
            ):
                continue
            if report.get("accepted_input") != accepted:
                raise FulcrumError(
                    "REQUEST_CONFLICT",
                    "report_key was already used with different input",
                    exit_code=5,
                )
            return CommandResult(
                ok=True,
                state=CommandState.COMPLETED,
                request_id=request.request_id,
                result={
                    "bead_id": existing.id,
                    "filing_state": "replayed",
                    "work": work_view(ledger, existing),
                },
            )
        root_id = _unique_id(ledger)
        operation, reused = ledger.create_operation(
            request,
            bead_id=root_id,
            planned={"root_id": root_id},
            next_action="Create the independently attributed follow-up work root.",
        )
        if reused:
            planned = _mapping(operation.operation.get("planned"), "planned")
            root_id = str(planned["root_id"])
            if operation.operation.get("state") in {
                "completed",
                "failed",
                "uncertain",
                "cancelled",
            }:
                return _operation_result(operation)
        spec = _work_spec(
            {
                "title": payload["title"],
                "outcome": payload["required_change"],
                "acceptance": acceptance,
                "requested_role": "executor",
                "priority": 2,
                "context": payload.get("context", []),
                "intake": payload.get("intake"),
                "summary": payload["problem"],
            },
            project=project,
            key="report",
        )
        record = self._create_planned_work(
            ledger,
            operation,
            root_id,
            spec,
            owner=_marshal_or_human(ledger),
            workflow_root=root_id,
            parent=None,
            issue_type="bug",
            request=request,
            check_existing=reused,
        )
        fc = dict(record.fc or {})
        fc["caused_by"] = payload.get("discovered_from")
        fc["report"] = {
            "report_key": report_key,
            "accepted_input": accepted,
            "problem": payload["problem"],
            "observed_evidence": payload["observed_evidence"],
            "reporter_task": request.thread_id,
        }
        if payload.get("implementation_ready"):
            fc["phase"] = "ready"
        if dependencies:
            _reconcile_dependencies(ledger, root_id, dependencies)
        record = ledger.update_fc(root_id, fc)
        operation = ledger.update_operation(
            operation,
            state="completed",
            step="report_filed",
            result={
                "bead_id": root_id,
                "filing_state": "accepted",
                "work": work_view(ledger, record),
            },
            next_action=(
                "Steward may select this implementation-ready report."
                if payload.get("implementation_ready")
                else "Weaver preparation is required before implementation."
            ),
        )
        return _operation_result(operation)

    @coordinated
    def context(self, request: ParsedRequest) -> CommandResult:
        bead_id = request.arguments.get("bead") or request.input.get("bead")
        if not bead_id:
            raise _invalid(
                "bead", "is required until role fallback context is implemented"
            )
        ledger = _ledger(request)
        record = ledger.show(str(bead_id))
        if record is None:
            raise FulcrumError.invalid("NOT_FOUND", f"unknown work {bead_id}")
        view = _authorized_work_view(ledger, record, request)
        downstream = _downstream_assignment(ledger, request)
        assignment = downstream[1] if downstream is not None else None
        return CommandResult.query(
            {
                "work": view,
                "instructions": (
                    "Use only the retained authorized assignment contract. Raw intake is withheld."
                    if downstream
                    else record.native.get("description")
                ),
                "contract": (
                    assignment.get("scope")
                    if isinstance(assignment, Mapping) and downstream
                    else None
                ),
                "ownership_operation": view.get("ownership_operation"),
            }
        )


def work_view(ledger: Ledger, record: LedgerRecord) -> dict[str, Any]:
    fc = dict(record.fc) if record.fc else None
    effective_owner = fc.get("owner") if fc else _marshal_or_human(ledger)
    phase = fc.get("phase") if fc else "intake"
    ownership_conflict = (
        {
            "native_assignee": record.assignee,
            "admitted_owner": fc.get("owner"),
            "next_command": [
                "fulcrum",
                "recover",
                "inspect",
                "--scope",
                f"bead:{record.id}",
                "--json",
            ],
        }
        if fc and record.assignee != fc.get("owner")
        else None
    )
    try:
        dependencies = ledger.dependencies(record.id)
    except Exception:
        dependencies = []
    return {
        "id": record.id,
        "title": record.title,
        "status": record.status,
        "priority": record.native.get("priority"),
        "description": record.native.get("description"),
        "acceptance_criteria": record.native.get("acceptance_criteria")
        or record.native.get("acceptance"),
        "dependencies": dependencies,
        "fc": fc,
        "effective_owner": effective_owner,
        "phase": phase,
        "ownership_operation": fc.get("ownership_operation") if fc else None,
        "next_action": (
            fc.get("next_action") if fc else "Adopt this native intake through Fulcrum."
        ),
        "waiting": fc.get("waiting") if fc else None,
        "active_task": fc.get("owner") if fc and fc.get("owner") != "HUMAN" else None,
        "last_progress_at": fc.get("last_progress_at") if fc else None,
        "delivery": fc.get("delivery") if fc else None,
        "next_commands": [["fulcrum", "work", "show", record.id, "--json"]],
        "ownership_conflict": ownership_conflict,
    }


def _authorized_work_view(
    ledger: Ledger, record: LedgerRecord, request: ParsedRequest
) -> dict[str, Any]:
    view = work_view(ledger, record)
    downstream = _downstream_assignment(ledger, request)
    if downstream is None:
        return view
    assigned_bead, assignment = downstream
    if record.id != assigned_bead:
        raise FulcrumError(
            "AUTHORITY_MISMATCH",
            "downstream workers may inspect only their assigned work contract",
            exit_code=5,
            details={"assigned_bead": assigned_bead},
        )
    fc = record.fc or {}
    contract = assignment.get("scope")
    if not isinstance(contract, Mapping):
        contract = {}
    behavioral = str(contract.get("behavioral_outcome") or "Authorized work")
    safe_fc = {
        key: fc.get(key)
        for key in (
            "kind",
            "project",
            "owner",
            "role",
            "ownership_operation",
            "phase",
            "requested_role",
            "workflow_root",
            "next_action",
            "waiting",
            "worktree",
            "source",
            "delivery",
        )
    }
    safe_fc.update(
        {
            "summary": behavioral,
            "outcome": behavioral,
            "acceptance": list(contract.get("acceptance") or []),
            "context": list(contract.get("evidence") or []),
            "scope": dict(contract),
        }
    )
    return {
        **view,
        "title": behavioral,
        "description": behavioral,
        "acceptance_criteria": list(contract.get("acceptance") or []),
        "fc": safe_fc,
    }


def _downstream_assignment(
    ledger: Ledger, request: ParsedRequest
) -> tuple[str, Mapping[str, Any]] | None:
    task_id = request.actor.task_id or request.thread_id
    if request.actor.kind != "task" or not task_id:
        return None
    for candidate in ledger.list_records(kind="work", limit=0):
        desktop = (candidate.fc or {}).get("desktop")
        assignment = desktop.get("assignment") if isinstance(desktop, Mapping) else None
        if (
            isinstance(assignment, Mapping)
            and assignment.get("task_id") == task_id
            and assignment.get("state") == "active"
            and assignment.get("role") in {"executor", "warden"}
        ):
            return candidate.id, assignment
    return None


def _initial_fc(
    spec: Mapping[str, Any],
    *,
    bead_id: str,
    workflow_root: str,
    owner: str,
    operation_id: str,
    request: ParsedRequest,
) -> dict[str, Any]:
    return {
        "kind": "work",
        "project": spec["project"],
        "owner": owner,
        "role": "marshal" if owner != "HUMAN" else None,
        "ownership_operation": operation_id,
        "phase": "backlog",
        "requested_role": spec["requested_role"],
        "origin": {
            "thread_id": request.thread_id,
            "request_id": request.request_id,
            "bead_id": bead_id,
            "creation_operation": operation_id,
        },
        "workflow_root": workflow_root,
        "caused_by": None,
        "models": spec.get("models", {}),
        "plan": None,
        "completion_cost": None,
        "summary": spec.get("summary"),
        "outcome": spec["outcome"],
        "acceptance": spec["acceptance"],
        "context": spec.get("context", []),
        "intake": spec.get("intake"),
        "size": spec.get("size", "unknown"),
        "overlap_tags": spec.get("overlap_tags", []),
        "next_action": "Review intake and authorize the appropriate role.",
        "last_progress_at": None,
        "last_progress": None,
        "waiting": None,
        "dispatch": None,
        "worktree": None,
        "delivery": None,
        "handoff": None,
        "interrupted_work": None,
        "active_operation": None,
        "last_transition": operation_id,
        "disposition": None,
    }


def _work_spec(payload: Mapping[str, Any], *, project: str, key: str) -> dict[str, Any]:
    title = payload.get("title")
    outcome = payload.get("outcome")
    if not isinstance(title, str) or not title.strip():
        raise _invalid(f"{key}.title", "is required")
    if not isinstance(outcome, str) or not outcome.strip():
        raise _invalid(f"{key}.outcome", "is required")
    acceptance = payload.get("acceptance", [])
    if not isinstance(acceptance, list) or not all(
        isinstance(item, str) and item.strip() for item in acceptance
    ):
        raise _invalid(f"{key}.acceptance", "must be an array of nonempty strings")
    priority = payload.get("priority", 2)
    if (
        not isinstance(priority, int)
        or isinstance(priority, bool)
        or not 0 <= priority <= 4
    ):
        raise _invalid(f"{key}.priority", "must be an integer from zero through four")
    requested_role = payload.get("requested_role", "executor")
    if requested_role not in ROLES:
        raise _invalid(f"{key}.requested_role", "is not a Fulcrum role")
    if requested_role == "weaver":
        raise FulcrumError(
            "WEAVER_TASK_FORBIDDEN",
            "work creation cannot request Weaver dispatch; enter Weaver in the invoking task",
            exit_code=5,
            details={"field": f"{key}.requested_role"},
        )
    for field in ("context", "overlap_tags", "depends_on"):
        value = payload.get(field, [])
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise _invalid(f"{key}.{field}", "must be an array of strings")
    intake = payload.get("intake")
    if intake is not None:
        if not isinstance(intake, Mapping) or set(intake).difference(
            {"benefit", "uncertainties"}
        ):
            raise _invalid(
                f"{key}.intake", "must contain only benefit and uncertainties"
            )
        uncertainties = intake.get("uncertainties")
        if uncertainties is not None and (
            not isinstance(uncertainties, list)
            or not all(isinstance(item, str) for item in uncertainties)
        ):
            raise _invalid(
                f"{key}.intake.uncertainties", "must be null or an array of strings"
            )
    size = payload.get("size", "unknown")
    if size not in {"small", "medium", "large", "unknown"}:
        raise _invalid(f"{key}.size", "must be small, medium, large, or unknown")
    return {
        "title": title,
        "outcome": outcome,
        "acceptance": acceptance,
        "priority": priority,
        "requested_role": requested_role,
        "context": payload.get("context", []),
        "overlap_tags": payload.get("overlap_tags", []),
        "intake": dict(intake) if isinstance(intake, Mapping) else None,
        "models": payload.get("models", {}),
        "size": size,
        "summary": payload.get("summary"),
        "depends_on": payload.get("depends_on", payload.get("dependencies", [])),
        "project": project,
    }


def _validate_update(payload: Mapping[str, Any]) -> None:
    if "acceptance" in payload and (
        not isinstance(payload["acceptance"], list)
        or not all(isinstance(item, str) for item in payload["acceptance"])
    ):
        raise _invalid("acceptance", "must be an array of strings")
    if "priority" in payload and (
        not isinstance(payload["priority"], int)
        or isinstance(payload["priority"], bool)
        or not 0 <= payload["priority"] <= 4
    ):
        raise _invalid("priority", "must be an integer from zero through four")
    if "size" in payload and payload["size"] not in {
        "small",
        "medium",
        "large",
        "unknown",
    }:
        raise _invalid("size", "must be small, medium, large, or unknown")


def _project_from_request(request: ParsedRequest, payload: Mapping[str, Any]) -> str:
    supplied = payload.get("project")
    if (
        supplied is not None
        and request.project is not None
        and supplied != request.project
    ):
        raise FulcrumError(
            "SCOPE_CONFLICT",
            "project was supplied with contradictory values",
            exit_code=5,
        )
    project = request.project or supplied
    if not isinstance(project, str) or not project:
        raise _invalid("project", "is required")
    config = ConfigurationManager(request.instance.config_path)
    document, _ = config.load()
    enrolled = config.effective(document)["projects"].get(project)
    if not isinstance(enrolled, Mapping):
        raise _invalid("project", f"{project} is not enrolled")
    if not enrolled.get("enabled", True):
        raise FulcrumError(
            "PROJECT_DISABLED",
            f"project {project} is disabled for new work",
            exit_code=5,
        )
    return project


def _project_for_entry(request: ParsedRequest) -> str:
    if request.project:
        return _project_from_request(request, {"project": request.project})
    manager = ConfigurationManager(request.instance.config_path)
    document, _ = manager.load()
    projects = manager.effective(document)["projects"]
    enabled = {
        str(key): value
        for key, value in projects.items()
        if isinstance(value, Mapping) and value.get("enabled", True)
    }
    cwd = Path.cwd().resolve(strict=False)
    matches: list[tuple[int, str]] = []
    for key, value in enabled.items():
        root_value = value.get("root")
        if not isinstance(root_value, str):
            continue
        root = Path(root_value).resolve(strict=False)
        if cwd == root or root in cwd.parents:
            matches.append((len(root.parts), key))
    if matches:
        matches.sort(reverse=True)
        if len(matches) == 1 or matches[0][0] > matches[1][0]:
            return matches[0][1]
    if len(enabled) == 1:
        return next(iter(enabled))
    raise _invalid(
        "project",
        "is required because the current directory does not identify one enrolled project",
    )


def _resolve_native_project(request: ParsedRequest, record: LedgerRecord) -> str:
    evidence: list[tuple[str, str]] = []
    if request.project:
        evidence.append(("explicit", request.project))
    if record.fc and isinstance(record.fc.get("project"), str):
        evidence.append(("metadata", str(record.fc["project"])))
    labels = [
        label.split(":", 1)[1]
        for label in record.labels
        if label.startswith("project:")
    ]
    if len(labels) == 1:
        evidence.append(("label", labels[0]))
    created_by = record.native.get("created_by")
    if isinstance(created_by, str) and created_by.startswith("project:"):
        actor_project = created_by[8:].split("/thread:", 1)[0]
        evidence.append(("actor", actor_project))
    values = {value for _, value in evidence}
    if len(values) != 1:
        raise FulcrumError(
            "SCOPE_CONFLICT",
            "native project evidence is missing or contradictory",
            exit_code=5,
            details={"evidence": evidence},
        )
    return values.pop()


def _marshal_or_human(ledger: Ledger) -> str:
    control = ledger.show("fc-system")
    desktop = (control.fc or {}).get("desktop") if control is not None else None
    standing = desktop.get("standing") if isinstance(desktop, Mapping) else None
    marshal = standing.get("marshal") if isinstance(standing, Mapping) else None
    if isinstance(marshal, Mapping) and isinstance(marshal.get("task_id"), str):
        return str(marshal["task_id"])
    return "HUMAN"


def _owned_work(ledger: Ledger, request: ParsedRequest, bead_id: str) -> LedgerRecord:
    record = ledger.show(bead_id)
    if record is None or record.kind != "work" or not record.fc:
        raise FulcrumError.invalid("NOT_FOUND", f"unknown Fulcrum work {bead_id}")
    if record.assignee != record.fc.get("owner"):
        raise FulcrumError(
            "OWNERSHIP_CONFLICT",
            "native assignee differs from the admitted owner and must be reconciled before effects",
            exit_code=5,
            details={
                "native_assignee": record.assignee,
                "admitted_owner": record.fc.get("owner"),
            },
        )
    if request.actor.kind in {"human", "system"}:
        return record
    if request.thread_id != record.fc.get(
        "owner"
    ) or request.ownership_operation != record.fc.get("ownership_operation"):
        raise FulcrumError(
            "OWNERSHIP_CONFLICT",
            "task ID and ownership operation must match the current acquisition",
            exit_code=5,
            request_id=request.request_id,
            details={
                "current_owner": record.fc.get("owner"),
                "current_ownership_operation": record.fc.get("ownership_operation"),
            },
        )
    return record


def _unique_id(ledger: Ledger, *, reserved: set[str] | None = None) -> str:
    reserved = reserved or set()
    for _ in range(32):
        candidate = random_record_id()
        if candidate not in reserved and ledger.show(candidate) is None:
            return candidate
    raise FulcrumError(
        "ID_EXHAUSTED", "could not preselect an unused Beads ID", exit_code=4
    )


def _planned_edges(children: Mapping[str, Mapping[str, Any]]) -> list[dict[str, str]]:
    return [
        {"from": key, "to": target}
        for key, spec in children.items()
        for target in spec.get("depends_on", [])
    ]


def _validate_local_graph(children: Mapping[str, Mapping[str, Any]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key in visiting:
            raise _invalid("children.depends_on", "contains a dependency cycle")
        if key in visited:
            return
        visiting.add(key)
        for target in children[key].get("depends_on", []):
            if target in children:
                visit(target)
        visiting.remove(key)
        visited.add(key)

    for key in children:
        visit(key)


def _reject_cycle(ledger: Ledger, bead_id: str, proposed: set[str]) -> None:
    def reaches(current: str, seen: set[str]) -> bool:
        if current == bead_id:
            return True
        if current in seen:
            return False
        seen.add(current)
        return any(reaches(child, seen) for child in ledger.dependencies(current))

    if any(reaches(target, set()) for target in proposed):
        raise _invalid("dependencies", "would create a cycle")


def _reconcile_dependencies(
    ledger: Ledger, bead_id: str, expected: Sequence[str]
) -> None:
    current = set(ledger.dependencies(bead_id))
    desired = set(expected)
    for target in sorted(current.difference(desired)):
        ledger.run(("dep", "remove", bead_id, target), mutating=True)
    for target in sorted(desired.difference(current)):
        ledger.run(("dep", "add", bead_id, target), mutating=True)
    observed = set(ledger.dependencies(bead_id))
    if observed != desired:
        raise FulcrumError(
            "DEPENDENCY_UNCERTAIN",
            "dependency postcondition could not be established",
            exit_code=4,
            details={"expected": sorted(desired), "observed": sorted(observed)},
        )


def _dependency_waiting(
    ledger: Ledger, waiting: Any, dependencies: set[str]
) -> dict[str, Any] | None:
    old_reasons = waiting.get("reasons", []) if isinstance(waiting, Mapping) else []
    preserved = [
        dict(reason)
        for reason in old_reasons
        if isinstance(reason, Mapping) and reason.get("kind") != "dependency"
    ]
    old_since = {
        str(reason.get("id")): reason.get("since")
        for reason in old_reasons
        if isinstance(reason, Mapping) and reason.get("kind") == "dependency"
    }
    for dependency in sorted(dependencies):
        satisfied, observed, reason, trigger = _dependency_state(
            ledger, dependency, seen=set()
        )
        if satisfied:
            continue
        reason_id = f"dependency:{dependency}"
        preserved.append(
            {
                "id": reason_id,
                "kind": "dependency",
                "reason": reason,
                "depends_on": observed,
                "reconsider_when": [{"event": trigger, "subject": observed}],
                "since": old_since.get(reason_id) or utc_now(),
            }
        )
    return {"reasons": preserved} if preserved else None


def _dependency_state(
    ledger: Ledger, dependency: str, *, seen: set[str]
) -> tuple[bool, str, str, str]:
    if dependency in seen:
        return (
            False,
            dependency,
            f"Duplicate chain from {dependency} contains a cycle",
            "human_resolved",
        )
    seen.add(dependency)
    target = ledger.show(dependency)
    if target is None or target.status != "closed":
        return (
            False,
            dependency,
            f"Waiting for {dependency}",
            "dependency_closed",
        )
    disposition = (
        target.fc.get("disposition")
        if target.fc and isinstance(target.fc.get("disposition"), Mapping)
        else {}
    )
    outcome = disposition.get("outcome")
    if outcome in {"delivered", "answered", "findings"}:
        return (True, dependency, "", "dependency_closed")
    if outcome == "duplicate" and isinstance(disposition.get("canonical_bead"), str):
        return _dependency_state(ledger, str(disposition["canonical_bead"]), seen=seen)
    return (
        False,
        dependency,
        f"{dependency} closed as {outcome or 'unknown'}; prerequisite outcome requires judgment",
        "human_resolved",
    )


def _native_acceptance(record: LedgerRecord) -> list[str]:
    value = record.native.get("acceptance_criteria") or record.native.get("acceptance")
    return [str(value)] if value else []


def _native_scope(record: LedgerRecord) -> dict[str, Any]:
    return {
        key: record.native.get(key)
        for key in (
            "title",
            "description",
            "acceptance_criteria",
            "priority",
            "status",
            "owner",
            "created_by",
            "labels",
        )
    }


def _render_acceptance(values: Any) -> str:
    if not isinstance(values, list):
        return ""
    return "\n".join(f"- {value}" for value in values)


def _delivery_settled(fc: Mapping[str, Any]) -> bool:
    delivery = fc.get("delivery")
    if not isinstance(delivery, Mapping):
        return False
    promotion = delivery.get("promotion")
    synchronization = delivery.get("synchronization")
    cleanup = delivery.get("cleanup")
    return (
        isinstance(promotion, Mapping)
        and promotion.get("state") == "observed"
        and isinstance(synchronization, Mapping)
        and synchronization.get("state") in {"observed", "not_required"}
        and isinstance(cleanup, Mapping)
        and cleanup.get("state") == "observed"
    )


def _ledger(request: ParsedRequest) -> Ledger:
    if request.instance.brain_root is None:
        raise FulcrumError(
            "LEDGER_UNAVAILABLE", "configuration has no brain root", exit_code=4
        )
    manager = ConfigurationManager(request.instance.config_path)
    document, _ = manager.load()
    executable = manager.effective(document)["beads"].get("executable")
    return Ledger(
        request.instance.brain_root,
        executable=str(executable) if executable else None,
        timeout=request.timeout,
    )


def _operation_result(operation: OperationRecord) -> CommandResult:
    state_value = str(operation.operation.get("state", "running"))
    state = (
        CommandState(state_value)
        if state_value in CommandState._value2member_map_
        else CommandState.RUNNING
    )
    return CommandResult(
        ok=state not in {CommandState.FAILED, CommandState.UNCERTAIN},
        state=state,
        operation_id=operation.id,
        request_id=str(operation.operation.get("request_id")),
        result=operation_view(operation),
    )


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _invalid(field, "must be an object")
    return value


def _known(value: Mapping[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(value).difference(allowed))
    if unknown:
        raise FulcrumError.invalid(
            "UNKNOWN_FIELDS",
            "input contains unknown fields",
            details={"fields": unknown},
        )


def _invalid(field: str, reason: str) -> FulcrumError:
    return FulcrumError.invalid(
        "INVALID_INPUT", f"{field} {reason}", details={"field": field}
    )
