"""Plan authoring, stable graph publication, activation, and root completion."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any

from fulcrum.configuration import ConfigurationManager
from fulcrum.contracts import CommandResult, CommandState, FulcrumError, ParsedRequest
from fulcrum.ledger import (
    Ledger,
    LedgerRecord,
    OperationRecord,
    operation_view,
    utc_now,
)
from fulcrum.work import (
    WorkService,
    _marshal_or_human,
    _operation_result,
    _reconcile_dependencies,
    _render_acceptance,
    _unique_id,
    _validate_local_graph,
    _work_spec,
)

TERMINAL_OPERATION_STATES = {"completed", "failed", "uncertain", "cancelled"}
SUCCESSFUL_CHILD_OUTCOMES = {"answered", "delivered", "findings"}
ACTIVE_CHILD_PHASES = {"working", "reviewing", "handoff", "delivering", "recovering"}
REQUIRED_REVIEW_PERSPECTIVES = ("cold_reader", "requirements")


class PlanService:
    """Keep canonical plan scope in Beads and reconcile ordinary child work."""

    def draft(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        root = _root(ledger, str(request.arguments["bead"]))
        _authorize_author(request, root, ledger)
        draft = validate_plan_draft(request.input)
        operation, reused = ledger.create_operation(
            request,
            bead_id=root.id,
            owner=str((root.fc or {}).get("owner") or "HUMAN"),
            planned={"draft": draft},
            next_action="Retain the complete unpublished draft without changing its work graph.",
        )
        if reused and operation.operation.get("state") in TERMINAL_OPERATION_STATES:
            return _operation_result(operation)
        current = ledger.show(root.id) or root
        assert current.fc
        fc = dict(current.fc)
        plan = dict(fc.get("plan") or {})
        previous = plan.get("draft")
        reviews = dict(plan.get("reviews") or {})
        stale_reviews: list[str] = []
        if previous != draft:
            for perspective, value in tuple(reviews.items()):
                if not isinstance(value, Mapping):
                    continue
                row = dict(value)
                if row.get("reviewed_draft") != draft:
                    row["state"] = "stale"
                    row["stale_at"] = utc_now()
                    row["stale_by"] = operation.id
                    reviews[perspective] = row
                    stale_reviews.append(str(perspective))
            plan["approval_operation"] = None
            plan["approved_scope"] = None
        plan["draft"] = draft
        plan["validation"] = draft["validation"]
        plan["reviews"] = reviews
        plan.setdefault("children_by_key", {})
        plan.setdefault("approved_keys", [])
        plan.setdefault("published_scope", None)
        plan.setdefault("published_approval_operation", None)
        plan.setdefault("publication_operation", None)
        plan.setdefault("publication", None)
        plan.setdefault("activation", None)
        plan.setdefault("activation_authorization", None)
        fc["plan"] = plan
        fc["last_transition"] = operation.id
        fc["next_action"] = (
            "Review and approve the retained draft; no graph or document was published."
        )
        ledger.update_fc(root.id, fc)
        operation = ledger.update_operation(
            operation.id,
            state="completed",
            step="plan_draft_retained",
            result={
                "bead_id": root.id,
                "draft": draft,
                "stale_reviews": stale_reviews,
                "graph_changed": False,
                "publication_changed": False,
            },
            next_action=fc["next_action"],
        )
        return _operation_result(operation)

    def show(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        root = _root(ledger, str(request.arguments["id"]))
        return CommandResult.query(_plan_view(ledger, root))

    def approve(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        root = _root(ledger, str(request.arguments["bead"]))
        approved_by = _authorize_approver(request, ledger)
        plan = _plan(root)
        draft = validate_plan_draft(plan.get("draft"))
        reason = request.input.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise _invalid("reason", "plan approval requires a nonempty reason")
        resolutions = _string_mapping(
            request.input.get("resolutions", {}), "resolutions"
        )
        waivers = _waivers(request.input.get("waivers", []), approved_by)
        reviews = _approval_reviews(root, draft, resolutions, waivers)
        approved_scope = {
            "draft": draft,
            "reviews": reviews,
            "resolutions": resolutions,
            "waivers": waivers,
        }
        operation, reused = ledger.create_operation(
            request,
            bead_id=root.id,
            owner=approved_by,
            planned={"approved_scope": approved_scope, "approved_by": approved_by},
            next_action="Retain approval against the exact current draft and review evidence.",
        )
        if reused and operation.operation.get("state") in TERMINAL_OPERATION_STATES:
            return _operation_result(operation)
        approval_evidence = {
            "operation_id": operation.id,
            "reason": reason.strip(),
            "resolutions": resolutions,
            "waivers": waivers,
        }
        current = ledger.show(root.id) or root
        assert current.fc
        fc = dict(current.fc)
        current_plan = dict(fc.get("plan") or {})
        if current_plan.get("draft") != draft:
            raise FulcrumError(
                "APPROVAL_CONFLICT",
                "the retained draft changed while approval was being recorded",
                exit_code=5,
                operation_id=operation.id,
            )
        current_plan["approval_operation"] = operation.id
        current_plan["approved_scope"] = approved_scope
        current_plan["approved_by"] = approved_by
        current_plan["approval_evidence"] = approval_evidence
        current_plan["approved_at"] = utc_now()
        fc["plan"] = current_plan
        fc["last_transition"] = operation.id
        fc["next_action"] = (
            "Publish the exact approved plan scope or retain it for refinement."
        )
        ledger.update_fc(root.id, fc)
        operation = ledger.update_operation(
            operation.id,
            state="completed",
            step="plan_scope_approved",
            result={
                "bead_id": root.id,
                "approval_operation": operation.id,
                "approved_by": approved_by,
                "approval_evidence": approval_evidence,
                "approved_scope": approved_scope,
            },
            next_action=fc["next_action"],
        )
        return _operation_result(operation)

    def publish(self, request: ParsedRequest) -> CommandResult:
        return self._publish_or_refine(request, refining=False)

    def refine(self, request: ParsedRequest) -> CommandResult:
        return self._publish_or_refine(request, refining=True)

    def _publish_or_refine(
        self, request: ParsedRequest, *, refining: bool
    ) -> CommandResult:
        ledger = _ledger(request)
        root = _root(ledger, str(request.arguments["bead"]))
        _authorize_author(request, root, ledger, allow_marshal=True)
        plan = _plan(root)
        approval, approved_scope = _approved_scope(ledger, plan, request.input)
        draft = validate_plan_draft(approved_scope.get("draft"))
        activation = (
            str(request.input.get("activation"))
            if not refining
            else str(plan.get("activation") or "active")
        )
        if activation not in {"active", "future"}:
            raise _invalid("activation", "must be active or future")
        _verify_publication_input(
            request.input, plan, approved_scope, refining=refining
        )
        current_keys = [str(item) for item in plan.get("approved_keys", [])]
        if refining and plan.get("published_scope") is None:
            raise _invalid("plan", "refine requires an already published graph")
        if not refining and plan.get("published_scope") is not None:
            raise FulcrumError(
                "APPROVAL_CONFLICT",
                "this root already has a published graph; use plan refine",
                exit_code=5,
            )
        task_specs = {str(item["key"]): dict(item) for item in draft["tasks"]}
        existing_map = {
            str(key): str(value)
            for key, value in dict(plan.get("children_by_key") or {}).items()
        }
        removed = sorted(set(current_keys).difference(task_specs))
        added = sorted(set(task_specs).difference(existing_map))
        changed = _changed_keys(ledger, existing_map, task_specs, root)
        authoring_pending = bool(
            activation == "future"
            and request.actor.kind == "task"
            and (root.fc or {}).get("role") == "weaver"
            and request.thread_id == (root.fc or {}).get("owner")
        )
        dispositions = _dispositions(request.input.get("dispositions", {}))
        _validate_scope_changes(
            ledger,
            existing_map,
            task_specs,
            removed,
            changed,
            dispositions,
        )
        children_by_key = dict(existing_map)
        reserved = set(children_by_key.values()) | {root.id}
        for key in added:
            identifier = _unique_id(ledger, reserved=reserved)
            reserved.add(identifier)
            children_by_key[key] = identifier
        planned = {
            "root_id": root.id,
            "approval_operation": approval.id,
            "approved_scope": approved_scope,
            "activation": activation,
            "children_by_key": children_by_key,
            "active_keys": list(task_specs),
            "added": added,
            "removed": removed,
            "changed": changed,
            "dispositions": dispositions,
            "edges": _edges(task_specs),
            "mode": "refine" if refining else "publish",
            "authoring_pending": authoring_pending,
        }
        operation, reused = ledger.create_operation(
            request,
            bead_id=root.id,
            owner=str((root.fc or {}).get("owner") or "HUMAN"),
            planned=planned,
            next_action="Reconcile the retained stable child IDs and dependency edges.",
        )
        retained = operation.operation.get("planned")
        if not isinstance(retained, Mapping):
            raise FulcrumError(
                "OPERATION_CORRUPT",
                "plan publication lost its graph intent",
                exit_code=4,
            )
        if reused and operation.operation.get("state") in TERMINAL_OPERATION_STATES:
            return _operation_result(operation)
        children_by_key = {
            str(key): str(value)
            for key, value in dict(retained["children_by_key"]).items()
        }
        task_specs = {
            str(item["key"]): dict(item)
            for item in validate_plan_draft(dict(retained["approved_scope"])["draft"])[
                "tasks"
            ]
        }
        if task_specs:
            ledger.run(("update", root.id, "--type", "epic"), mutating=True)
        self._apply_graph(
            ledger,
            request,
            root,
            operation,
            task_specs,
            children_by_key,
            [str(item) for item in retained.get("removed", [])],
            [str(item) for item in retained.get("changed", [])],
            dict(retained.get("dispositions") or {}),
            str(retained["activation"]),
            _remote_publication_required(draft["publication"]),
            bool(retained.get("authoring_pending")),
        )
        current = ledger.show(root.id) or root
        assert current.fc
        fc = dict(current.fc)
        current_plan = dict(fc.get("plan") or {})
        publication = draft["publication"]
        publication_facts = _publication_facts(publication, operation.id)
        current_plan.update(
            {
                "published_scope": approved_scope,
                "published_approval_operation": approval.id,
                "children_by_key": children_by_key,
                "approved_keys": list(task_specs),
                "publication_operation": operation.id,
                "publication": publication_facts,
                "publication_ready": not _remote_publication_required(publication),
                "activation": str(retained["activation"]),
                "reopen_requirement": None,
                "authoring": (
                    {
                        "state": "awaiting_finish",
                        "thread_id": request.thread_id,
                        "publication_operation": operation.id,
                    }
                    if retained.get("authoring_pending")
                    else {"state": "completed", "publication_operation": operation.id}
                ),
                "authoring_ready": not bool(retained.get("authoring_pending")),
            }
        )
        if refining and approval.id != plan.get("published_approval_operation"):
            current_plan["activation_authorization"] = None
        marshal = _marshal_or_human(ledger)
        authoring_pending = bool(retained.get("authoring_pending"))
        fc["plan"] = current_plan
        if not authoring_pending:
            fc["owner"] = marshal
            fc["role"] = "marshal" if marshal != "HUMAN" else None
            fc["phase"] = "backlog"
        remote_publication_required = _remote_publication_required(publication)
        fc["waiting"] = (
            _plan_waiting(
                root.id,
                future=activation == "future",
                publication=remote_publication_required,
                authoring=authoring_pending,
            )
            if activation == "future"
            or remote_publication_required
            or authoring_pending
            else None
        )
        fc["next_action"] = (
            "Weaver must finish planned so authoring ends without activating the future root."
            if authoring_pending
            else _publication_next_action(publication_facts, activation)
        )
        fc["last_transition"] = operation.id
        ledger.update_fc(
            root.id,
            fc,
            assignee=(marshal if not authoring_pending else None),
            status="open" if not authoring_pending else "in_progress",
        )
        operation = ledger.update_operation(
            operation.id,
            state="completed",
            step="plan_graph_verified",
            result={
                "bead_id": root.id,
                "children_by_key": children_by_key,
                "active_keys": list(task_specs),
                "added": list(retained.get("added", [])),
                "removed": list(retained.get("removed", [])),
                "changed": list(retained.get("changed", [])),
                "activation": activation,
                "publication": publication_facts,
                "graph_ready": True,
            },
            next_action=fc["next_action"],
        )
        return _operation_result(operation)

    def _apply_graph(
        self,
        ledger: Ledger,
        request: ParsedRequest,
        root: LedgerRecord,
        operation: OperationRecord,
        task_specs: Mapping[str, Mapping[str, Any]],
        children_by_key: Mapping[str, str],
        removed: Sequence[str],
        changed: Sequence[str],
        dispositions: Mapping[str, Mapping[str, str]],
        activation: str,
        remote_publication_required: bool,
        authoring_pending: bool,
    ) -> None:
        assert root.fc
        project = str(root.fc["project"])
        owner = _marshal_or_human(ledger)
        for key, payload in task_specs.items():
            spec = _work_spec(payload, project=project, key=key)
            child_id = children_by_key[key]
            existing = ledger.show(child_id)
            if existing is None:
                child = WorkService()._create_planned_work(
                    ledger,
                    operation,
                    child_id,
                    spec,
                    owner=owner,
                    workflow_root=root.id,
                    parent=root.id,
                    issue_type="task",
                    request=request,
                )
            else:
                child = ledger.set_parent(child_id, root.id)
                if child.status != "closed" and key in changed:
                    child_fc = dict(child.fc or {})
                    for field in (
                        "outcome",
                        "acceptance",
                        "requested_role",
                        "priority",
                        "context",
                        "intake",
                        "models",
                        "size",
                        "overlap_tags",
                        "summary",
                    ):
                        if field in spec:
                            child_fc[field] = spec[field]
                    child_fc["dispatch"] = None
                    delivery = child_fc.get("delivery")
                    if isinstance(delivery, Mapping):
                        retained_delivery = dict(delivery)
                        retained_delivery["approved_source"] = None
                        retained_delivery["approval_invalidated_by"] = operation.id
                        child_fc["delivery"] = retained_delivery
                    child = ledger.update_fc(
                        child_id,
                        child_fc,
                        title=str(spec["title"]),
                        description=str(spec["outcome"]),
                        priority=int(spec["priority"]),
                        acceptance=_render_acceptance(spec["acceptance"]),
                    )
            is_new = existing is None
            child_fc = dict(child.fc or {})
            child_fc["plan"] = {
                "root_id": root.id,
                "key": key,
                "publication_operation": operation.id,
                "activation": activation,
                "activation_authorization": None,
                "publication_ready": not remote_publication_required,
                "authoring_ready": not authoring_pending,
                "retired": False,
            }
            if (
                activation == "future"
                or remote_publication_required
                or authoring_pending
            ):
                child_fc["waiting"] = _plan_waiting(
                    root.id,
                    future=activation == "future",
                    publication=remote_publication_required,
                    authoring=authoring_pending,
                )
                child_fc["next_action"] = (
                    "Wait for Weaver to finish plan authoring."
                    if authoring_pending
                    else (
                        "Wait for explicit activation of the approved future plan."
                        if activation == "future"
                        else "Wait for observed required remote plan publication."
                    )
                )
            elif is_new or key in changed:
                child_fc["waiting"] = _without_future_waiting(child_fc.get("waiting"))
                child_fc["next_action"] = (
                    "Marshal must authorize this approved plan child under current policy."
                )
            child_fc["last_transition"] = operation.id
            ledger.update_fc(child_id, child_fc)
        for key in removed:
            child_id = children_by_key[key]
            child = ledger.show(child_id)
            if child is None:
                continue
            child_fc = dict(child.fc or {})
            plan_link = dict(child_fc.get("plan") or {})
            plan_link["retired"] = True
            plan_link["retired_by"] = operation.id
            child_fc["plan"] = plan_link
            child_fc["last_transition"] = operation.id
            if child.status == "closed":
                ledger.update_fc(child_id, child_fc, status="closed")
                continue
            disposition = dispositions[key]
            if disposition["outcome"] == "cancelled":
                child_fc["phase"] = "done"
                child_fc["disposition"] = {
                    "outcome": "cancelled",
                    "summary": disposition["reason"],
                    "completed_at": utc_now(),
                    "ownership_operation": child_fc.get("ownership_operation"),
                }
                child_fc["next_action"] = (
                    "This retired plan obligation is explicitly cancelled."
                )
                ledger.update_fc(child_id, child_fc, status="closed")
            else:
                child_fc["phase"] = "backlog"
                child_fc["dispatch"] = None
                child_fc["waiting"] = {
                    "reasons": [
                        {
                            "id": f"plan-retired:{operation.id}",
                            "kind": "defer",
                            "reason": disposition["reason"],
                            "reconsider_when": [
                                {"event": "scope_updated", "subject": root.id}
                            ],
                            "since": utc_now(),
                        }
                    ]
                }
                child_fc["next_action"] = (
                    "Retired work remains deferred outside current plan obligations."
                )
                ledger.update_fc(child_id, child_fc, assignee=owner, status="open")
        for key, payload in task_specs.items():
            dependencies = [
                children_by_key.get(str(value), str(value))
                for value in payload.get("depends_on", [])
            ]
            _reconcile_dependencies(ledger, children_by_key[key], dependencies)
            # Beads represents hierarchy through a typed relationship. Reapply it
            # after ordinary dependency reconciliation so a broad edge diff cannot
            # remove the parent link from a newly materialized plan child.
            ledger.set_parent(children_by_key[key], root.id)

    def activate(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        root = _root(ledger, str(request.arguments["id"]))
        plan = _plan(root)
        if plan.get("activation") != "future":
            return CommandResult.query(
                {
                    "bead_id": root.id,
                    "activation": plan.get("activation"),
                    "activated": True,
                }
            )
        published_approval = plan.get("published_approval_operation")
        if not isinstance(published_approval, str):
            raise FulcrumError(
                "ACTIVATION_NOT_AUTHORIZED",
                "future activation requires a published approved scope",
                exit_code=5,
            )
        supplied = request.arguments.get("authorization")
        if supplied is None:
            authorizer = _authorize_activation(request, ledger)
            operation, reused = ledger.create_operation(
                request,
                bead_id=root.id,
                owner=authorizer,
                planned={
                    "approved_scope": published_approval,
                    "authorizer": authorizer,
                },
                next_action="Marshal must execute this exact retained activation authorization.",
            )
            if reused and operation.operation.get("state") in TERMINAL_OPERATION_STATES:
                return _operation_result(operation)
            current = ledger.show(root.id) or root
            assert current.fc
            fc = dict(current.fc)
            current_plan = dict(fc.get("plan") or {})
            if current_plan.get("published_approval_operation") != published_approval:
                raise FulcrumError(
                    "ACTIVATION_NOT_AUTHORIZED",
                    "approved scope changed before activation authorization settled",
                    exit_code=5,
                    operation_id=operation.id,
                )
            current_plan["activation_authorization"] = {
                "operation_id": operation.id,
                "approval_operation": published_approval,
                "authorizer": authorizer,
                "authorized_at": utc_now(),
            }
            fc["plan"] = current_plan
            fc["last_transition"] = operation.id
            fc["next_action"] = (
                "Marshal must execute the retained activation authorization."
            )
            ledger.update_fc(root.id, fc)
            operation = ledger.update_operation(
                operation.id,
                state="completed",
                step="future_activation_authorized",
                result={
                    "bead_id": root.id,
                    "activation_authorization": current_plan[
                        "activation_authorization"
                    ],
                    "activated": False,
                },
                next_action=fc["next_action"],
            )
            return _operation_result(operation)
        _authorize_activation_execution(request, ledger, root)
        authorization = plan.get("activation_authorization")
        if (
            not isinstance(authorization, Mapping)
            or authorization.get("operation_id") != supplied
            or authorization.get("approval_operation") != published_approval
        ):
            raise FulcrumError(
                "ACTIVATION_NOT_AUTHORIZED",
                "authorization does not match the current published approved scope",
                exit_code=5,
                details={"current_authorization": authorization},
            )
        _require_authoring_ready(plan)
        _require_publication_ready(plan)
        operation, reused = ledger.create_operation(
            request,
            bead_id=root.id,
            owner=str((root.fc or {}).get("owner") or "HUMAN"),
            planned={"authorization": dict(authorization)},
            next_action="Clear only the future-plan deferral for this exact approved scope.",
        )
        if reused and operation.operation.get("state") in TERMINAL_OPERATION_STATES:
            return _operation_result(operation)
        _activate_graph(ledger, root, operation.id, dict(authorization))
        operation = ledger.update_operation(
            operation.id,
            state="completed",
            step="future_plan_activated",
            result={
                "bead_id": root.id,
                "activation": "active",
                "authorization": dict(authorization),
                "children_by_key": dict(plan.get("children_by_key") or {}),
            },
            next_action="Marshal may dispatch approved children under current policy.",
        )
        return _operation_result(operation)

    def complete(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        root = _root(ledger, str(request.arguments["id"]))
        _authorize_completion(request, root, ledger)
        if root.status == "closed":
            return CommandResult.query(
                {
                    "bead_id": root.id,
                    "root_closed": True,
                    "disposition": (root.fc or {}).get("disposition"),
                    "unsatisfied": [],
                }
            )
        unsatisfied, children = _completion_obligations(ledger, root)
        if unsatisfied:
            return CommandResult.query(
                {
                    "bead_id": root.id,
                    "root_closed": False,
                    "unsatisfied": unsatisfied,
                    "children": children,
                    "next_commands": _completion_commands(root.id, unsatisfied),
                }
            )
        plan = _plan(root)
        operation, reused = ledger.create_operation(
            request,
            bead_id=root.id,
            owner=str((root.fc or {}).get("owner") or "HUMAN"),
            planned={
                "published_approval_operation": plan.get(
                    "published_approval_operation"
                ),
                "children": children,
            },
            next_action="Close the root from observed child and publication obligations.",
        )
        if reused and operation.operation.get("state") in TERMINAL_OPERATION_STATES:
            return _operation_result(operation)
        current = ledger.show(root.id) or root
        assert current.fc
        current_unsatisfied, current_children = _completion_obligations(ledger, current)
        if current_unsatisfied:
            return CommandResult.query(
                {
                    "bead_id": root.id,
                    "root_closed": False,
                    "unsatisfied": current_unsatisfied,
                    "children": current_children,
                }
            )
        fc = dict(current.fc)
        outcome = "delivered" if current_children else "answered"
        summary = str(
            dict(_plan(current).get("published_scope") or {})
            .get("draft", {})
            .get("summary", "Plan obligations satisfied")
        )
        fc["phase"] = "done"
        fc["disposition"] = {
            "outcome": outcome,
            "summary": summary,
            "completed_at": utc_now(),
            "ownership_operation": fc.get("ownership_operation"),
            "plan_completion_operation": operation.id,
        }
        fc["last_transition"] = operation.id
        fc["next_action"] = (
            "No plan obligations remain unless the root is explicitly reopened."
        )
        ledger.update_fc(root.id, fc, status="closed")
        operation = ledger.update_operation(
            operation.id,
            state="completed",
            step="plan_root_closed",
            result={
                "bead_id": root.id,
                "root_closed": True,
                "outcome": outcome,
                "children": current_children,
                "unsatisfied": [],
            },
            next_action=fc["next_action"],
        )
        return _operation_result(operation)


def validate_plan_draft(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _invalid("plan", "draft must be an object")
    required = {"text", "tasks", "summary", "publication", "validation"}
    if set(value) != required:
        raise _invalid(
            "plan",
            "draft requires exactly text, tasks, summary, publication, and validation",
        )
    text = value.get("text")
    summary = value.get("summary")
    if not isinstance(text, str) or not text.strip():
        raise _invalid("text", "draft text is required")
    if not isinstance(summary, str) or not summary.strip():
        raise _invalid("summary", "draft summary is required")
    tasks = value.get("tasks")
    if not isinstance(tasks, list) or not all(
        isinstance(item, Mapping) for item in tasks
    ):
        raise _invalid("tasks", "must be an array of work objects")
    allowed_task_fields = {
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
    }
    canonical_tasks: list[dict[str, Any]] = []
    keys: list[str] = []
    for item in tasks:
        row = dict(item)
        if set(row).difference(allowed_task_fields):
            raise _invalid("tasks", "contains unsupported work fields")
        key = row.get("key")
        if not isinstance(key, str) or not key or key == "root":
            raise _invalid("tasks.key", "must be a nonempty stable key other than root")
        keys.append(key)
        # Reuse the work schema for all substantive child fields.
        _work_spec(row, project="draft", key=key)
        canonical_tasks.append(json.loads(json.dumps(row, ensure_ascii=False)))
    if len(keys) != len(set(keys)):
        raise _invalid("tasks.key", "stable keys must be unique")
    specs = {str(row["key"]): row for row in canonical_tasks}
    _validate_local_graph(specs)
    publication = _validate_publication(value.get("publication"))
    validation = _validate_validation(value.get("validation"), set(keys))
    return {
        "text": text.strip(),
        "tasks": canonical_tasks,
        "summary": summary.strip(),
        "publication": publication,
        "validation": validation,
    }


def _validate_publication(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {
        "destination",
        "relative_path",
        "require_remote_sync",
    }:
        raise _invalid(
            "publication",
            "must be null or destination, relative_path, and require_remote_sync",
        )
    destination = value.get("destination")
    relative = value.get("relative_path")
    remote = value.get("require_remote_sync")
    if destination not in {"knowledge", "project"}:
        raise _invalid("publication.destination", "must be knowledge or project")
    if not isinstance(relative, str) or not relative.strip():
        raise _invalid("publication.relative_path", "is required")
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts:
        raise _invalid("publication.relative_path", "must stay within its destination")
    if not isinstance(remote, bool):
        raise _invalid("publication.require_remote_sync", "must be boolean")
    return {
        "destination": destination,
        "relative_path": str(path),
        "require_remote_sync": remote,
    }


def _validate_validation(value: Any, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"summary", "checks"}:
        raise _invalid("validation", "requires exactly summary and checks")
    summary = value.get("summary")
    checks = value.get("checks")
    if not isinstance(summary, str) or not summary.strip():
        raise _invalid("validation.summary", "is required")
    if not isinstance(checks, list):
        raise _invalid("validation.checks", "must be an array")
    canonical: list[dict[str, Any]] = []
    for check in checks:
        if not isinstance(check, Mapping) or set(check) != {
            "criterion",
            "task_keys",
            "evidence_required",
        }:
            raise _invalid(
                "validation.checks",
                "each check requires criterion, task_keys, and evidence_required",
            )
        criterion = check.get("criterion")
        task_keys = check.get("task_keys")
        evidence = check.get("evidence_required")
        if not isinstance(criterion, str) or not criterion.strip():
            raise _invalid("validation.checks.criterion", "is required")
        if not isinstance(task_keys, list) or not all(
            isinstance(item, str) for item in task_keys
        ):
            raise _invalid(
                "validation.checks.task_keys", "must be an array of stable keys"
            )
        unknown = sorted(set(task_keys).difference(keys))
        if unknown:
            raise _invalid(
                "validation.checks.task_keys",
                f"contains unknown keys: {', '.join(unknown)}",
            )
        if not isinstance(evidence, str) or not evidence.strip():
            raise _invalid("validation.checks.evidence_required", "is required")
        canonical.append(
            {
                "criterion": criterion.strip(),
                "task_keys": list(task_keys),
                "evidence_required": evidence.strip(),
            }
        )
    return {"summary": summary.strip(), "checks": canonical}


def _approved_scope(
    ledger: Ledger, plan: Mapping[str, Any], payload: Mapping[str, Any]
) -> tuple[OperationRecord, Mapping[str, Any]]:
    identifier = payload.get("approval_operation")
    if not isinstance(identifier, str) or identifier != plan.get("approval_operation"):
        raise FulcrumError(
            "APPROVAL_CONFLICT",
            "publication requires the current exact plan approval operation",
            exit_code=5,
            details={"current_approval": plan.get("approval_operation")},
        )
    record = ledger.show(identifier)
    if record is None or record.kind != "operation" or not record.fc:
        raise _invalid(
            "approval_operation", "does not identify a retained approval receipt"
        )
    operation = OperationRecord.from_record(record)
    planned = operation.operation.get("planned")
    scope = planned.get("approved_scope") if isinstance(planned, Mapping) else None
    if not isinstance(scope, Mapping) or scope != plan.get("approved_scope"):
        raise FulcrumError(
            "APPROVAL_CONFLICT",
            "approval receipt no longer matches the retained scope",
            exit_code=5,
        )
    return operation, scope


def _verify_publication_input(
    payload: Mapping[str, Any],
    plan: Mapping[str, Any],
    approved_scope: Mapping[str, Any],
    *,
    refining: bool,
) -> None:
    draft = validate_plan_draft(approved_scope.get("draft"))
    expected = {
        "tasks": draft["tasks"],
        "publication": draft["publication"],
    }
    if not refining:
        expected.update(
            {
                "text": draft["text"],
                "reviews": approved_scope.get("reviews"),
                "approved_by": plan.get("approved_by"),
                "approval_evidence": plan.get("approval_evidence"),
            }
        )
    conflicts = {
        key: {"expected": value, "supplied": payload.get(key)}
        for key, value in expected.items()
        if payload.get(key) != value
    }
    if conflicts:
        raise FulcrumError(
            "APPROVAL_CONFLICT",
            "publication input differs from the exact approved plan scope",
            exit_code=5,
            details={"conflicts": conflicts},
        )


def _approval_reviews(
    root: LedgerRecord,
    draft: Mapping[str, Any],
    resolutions: Mapping[str, str],
    waivers: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    plan = _plan(root)
    retained = dict(plan.get("reviews") or {})
    substantial = (root.fc or {}).get("size") in {"medium", "large"} or len(
        draft["tasks"]
    ) > 1
    required = REQUIRED_REVIEW_PERSPECTIVES if substantial else ()
    waiver_by_perspective = {str(row["perspective"]): dict(row) for row in waivers}
    result: dict[str, Any] = {}
    missing: list[str] = []
    unresolved: list[str] = []
    for perspective in REQUIRED_REVIEW_PERSPECTIVES:
        row = retained.get(perspective)
        if (
            isinstance(row, Mapping)
            and row.get("state") == "completed"
            and row.get("reviewed_draft") == draft
        ):
            findings = (
                row.get("findings") if isinstance(row.get("findings"), list) else []
            )
            review_operation = str(row.get("review_operation") or "")
            if findings and review_operation not in resolutions:
                unresolved.append(review_operation or perspective)
            result[perspective] = dict(row)
        elif perspective in waiver_by_perspective:
            result[perspective] = {
                "state": "waived",
                "reviewed_draft": draft,
                "waiver": waiver_by_perspective[perspective],
            }
        elif perspective in required:
            missing.append(perspective)
    if missing or unresolved:
        raise FulcrumError(
            "STALE_REVIEW",
            "substantial plan approval requires current reviews or explicit waivers and resolved findings",
            exit_code=5,
            details={"missing": missing, "unresolved": unresolved},
        )
    return result


def _changed_keys(
    ledger: Ledger,
    children_by_key: Mapping[str, str],
    task_specs: Mapping[str, Mapping[str, Any]],
    root: LedgerRecord,
) -> list[str]:
    changed: list[str] = []
    project = str((root.fc or {}).get("project"))
    for key, payload in task_specs.items():
        identifier = children_by_key.get(key)
        if identifier is None:
            continue
        child = ledger.show(identifier)
        if child is None:
            changed.append(key)
            continue
        spec = _work_spec(payload, project=project, key=key)
        fc = child.fc or {}
        current = {
            "title": child.title,
            "outcome": fc.get("outcome"),
            "acceptance": fc.get("acceptance"),
            "requested_role": fc.get("requested_role"),
            "priority": child.native.get("priority"),
            "context": fc.get("context", []),
            "intake": fc.get("intake"),
            "models": fc.get("models", {}),
            "size": fc.get("size", "unknown"),
            "overlap_tags": fc.get("overlap_tags", []),
            "summary": fc.get("summary"),
            "depends_on": sorted(
                dependency
                for dependency in ledger.dependencies(identifier)
                if dependency != root.id
            ),
        }
        proposed = {key: spec[key] for key in current if key != "depends_on"}
        proposed["depends_on"] = sorted(
            children_by_key.get(str(value), str(value))
            for value in spec.get("depends_on", [])
        )
        if current != proposed:
            changed.append(key)
    return sorted(changed)


def _validate_scope_changes(
    ledger: Ledger,
    children_by_key: Mapping[str, str],
    task_specs: Mapping[str, Mapping[str, Any]],
    removed: Sequence[str],
    changed: Sequence[str],
    dispositions: Mapping[str, Mapping[str, str]],
) -> None:
    missing_dispositions: list[str] = []
    active: list[dict[str, Any]] = []
    immutable: list[str] = []
    for key in sorted(set(removed).union(changed)):
        identifier = children_by_key.get(key)
        child = ledger.show(identifier) if identifier else None
        if child is None:
            continue
        fc = child.fc or {}
        if child.status == "closed":
            outcome = (
                fc.get("disposition", {}).get("outcome")
                if isinstance(fc.get("disposition"), Mapping)
                else None
            )
            if key in task_specs and key in changed:
                immutable.append(key)
            if key in removed and outcome not in SUCCESSFUL_CHILD_OUTCOMES:
                # It is already explicit history; removal can proceed but completion
                # still depends only on the newly approved key set.
                continue
            continue
        if fc.get("phase") in ACTIVE_CHILD_PHASES or fc.get("owner") not in {
            None,
            "HUMAN",
            _marshal_or_human(ledger),
        }:
            active.append(
                {
                    "key": key,
                    "bead_id": child.id,
                    "owner": fc.get("owner"),
                    "phase": fc.get("phase"),
                    "next_command": [
                        "fulcrum",
                        "task",
                        "interrupt",
                        "--task",
                        str(fc.get("owner")),
                        "--reason",
                        "approved plan scope changed",
                        "--json",
                    ],
                }
            )
        if key in removed and key not in dispositions:
            missing_dispositions.append(key)
    if immutable:
        raise FulcrumError(
            "APPROVAL_CONFLICT",
            "delivered plan children are immutable; retain their approved scope",
            exit_code=5,
            details={"delivered_changed": immutable},
        )
    if active:
        raise FulcrumError(
            "ACTIVE_SCOPE_CHANGE",
            "stop affected active children before changing their approved scope",
            exit_code=5,
            details={"active": active},
        )
    if missing_dispositions:
        raise FulcrumError(
            "APPROVAL_CONFLICT",
            "deleted unfinished children require an explicit cancellation or deferral",
            exit_code=5,
            details={"missing_dispositions": missing_dispositions},
        )


def _completion_obligations(
    ledger: Ledger, root: LedgerRecord
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    plan = _plan(root)
    unsatisfied: list[dict[str, Any]] = []
    if plan.get("published_scope") is None:
        unsatisfied.append({"kind": "plan_publication", "state": "not_published"})
    if plan.get("authoring_ready") is False:
        unsatisfied.append({"kind": "authoring", "state": plan.get("authoring")})
    if isinstance(plan.get("reopen_requirement"), Mapping):
        unsatisfied.append(
            {
                "kind": "reopen_scope",
                "state": dict(plan["reopen_requirement"]),
            }
        )
    if plan.get("activation") == "future":
        unsatisfied.append({"kind": "activation", "state": "future"})
    publication = plan.get("publication")
    if isinstance(publication, Mapping) and publication.get("required"):
        local = publication.get("local")
        remote = publication.get("remote")
        if not isinstance(local, Mapping) or local.get("state") != "observed":
            unsatisfied.append(
                {"kind": "publication", "scope": "local", "state": local}
            )
        if publication.get("require_remote_sync") and (
            not isinstance(remote, Mapping) or remote.get("state") != "observed"
        ):
            unsatisfied.append(
                {"kind": "publication", "scope": "remote", "state": remote}
            )
    mapping = dict(plan.get("children_by_key") or {})
    children: dict[str, Any] = {}
    for key in plan.get("approved_keys", []):
        identifier = mapping.get(str(key))
        child = ledger.show(str(identifier)) if identifier else None
        if child is None:
            row = {"bead_id": identifier, "state": "missing", "outcome": None}
            unsatisfied.append({"kind": "child", "key": key, **row})
            children[str(key)] = row
            continue
        disposition = (
            child.fc.get("disposition")
            if child.fc and isinstance(child.fc.get("disposition"), Mapping)
            else {}
        )
        outcome = disposition.get("outcome")
        row = {"bead_id": child.id, "state": child.status, "outcome": outcome}
        children[str(key)] = row
        if child.status != "closed" or outcome not in SUCCESSFUL_CHILD_OUTCOMES:
            unsatisfied.append({"kind": "child", "key": key, **row})
    return unsatisfied, children


def _activate_graph(
    ledger: Ledger,
    root: LedgerRecord,
    transition: str,
    authorization: Mapping[str, Any],
) -> None:
    assert root.fc
    fc = dict(root.fc)
    plan = dict(fc.get("plan") or {})
    plan["activation"] = "active"
    plan["activation_authorization"] = dict(authorization)
    fc["plan"] = plan
    fc["waiting"] = _without_future_waiting(fc.get("waiting"))
    fc["next_action"] = "Marshal may dispatch approved children under current policy."
    fc["last_transition"] = transition
    ledger.update_fc(root.id, fc)
    mapping = dict(plan.get("children_by_key") or {})
    for key in plan.get("approved_keys", []):
        child = ledger.show(str(mapping.get(str(key))))
        if child is None or not child.fc or child.status == "closed":
            continue
        child_fc = dict(child.fc)
        link = dict(child_fc.get("plan") or {})
        link["activation"] = "active"
        link["activation_authorization"] = dict(authorization)
        child_fc["plan"] = link
        child_fc["waiting"] = _without_future_waiting(child_fc.get("waiting"))
        child_fc["next_action"] = "Marshal may authorize this approved plan child."
        child_fc["last_transition"] = transition
        ledger.update_fc(child.id, child_fc)


def _publication_facts(value: Any, operation_id: str) -> dict[str, Any]:
    if value is None:
        return {
            "required": False,
            "destination": None,
            "relative_path": None,
            "require_remote_sync": False,
            "ledger": {"state": "observed", "operation_id": operation_id},
            "local": {"state": "not_required"},
            "remote": {"state": "not_required"},
        }
    publication = dict(value)
    return {
        "required": True,
        **publication,
        "ledger": {"state": "observed", "operation_id": operation_id},
        "local": {"state": "pending", "adapter": "knowledge_publication"},
        "remote": {
            "state": (
                "pending" if publication["require_remote_sync"] else "not_required"
            ),
            "adapter": "knowledge_publication",
        },
    }


def _require_publication_ready(plan: Mapping[str, Any]) -> None:
    publication = plan.get("publication")
    if not isinstance(publication, Mapping):
        return
    if publication.get("require_remote_sync"):
        remote = publication.get("remote")
        if not isinstance(remote, Mapping) or remote.get("state") != "observed":
            raise FulcrumError(
                "ACTIVATION_NOT_AUTHORIZED",
                "required remote plan publication is not observed",
                exit_code=5,
                details={"publication": publication},
            )


def _require_authoring_ready(plan: Mapping[str, Any]) -> None:
    if plan.get("authoring_ready") is False:
        raise FulcrumError(
            "ACTIVATION_NOT_AUTHORIZED",
            "Weaver must finish published plan authoring before activation executes",
            exit_code=5,
            details={"authoring": plan.get("authoring")},
        )


def _publication_next_action(publication: Mapping[str, Any], activation: str) -> str:
    if publication.get("required"):
        return "Publish the retained document; required remote synchronization blocks activation."
    if activation == "future":
        return "Human or Vizier must explicitly authorize this future plan."
    return "Marshal may dispatch approved plan children under current policy."


def _plan_view(ledger: Ledger, root: LedgerRecord) -> dict[str, Any]:
    plan = _plan(root)
    unsatisfied, children = _completion_obligations(ledger, root)
    return {
        "bead_id": root.id,
        "status": root.status,
        "owner": (root.fc or {}).get("owner"),
        "draft": plan.get("draft"),
        "validation": plan.get("validation"),
        "reviews": plan.get("reviews", {}),
        "approval_operation": plan.get("approval_operation"),
        "approved_by": plan.get("approved_by"),
        "approval_evidence": plan.get("approval_evidence"),
        "approved_scope": plan.get("approved_scope"),
        "published_scope": plan.get("published_scope"),
        "published_approval_operation": plan.get("published_approval_operation"),
        "children_by_key": plan.get("children_by_key", {}),
        "active_keys": plan.get("approved_keys", []),
        "activation": plan.get("activation"),
        "activation_authorization": plan.get("activation_authorization"),
        "authoring": plan.get("authoring"),
        "authoring_ready": plan.get("authoring_ready"),
        "publication_operation": plan.get("publication_operation"),
        "publication": plan.get("publication"),
        "children": children,
        "root_closed": root.status == "closed",
        "unsatisfied": unsatisfied,
        "next_commands": _completion_commands(root.id, unsatisfied),
    }


def _completion_commands(
    root_id: str, unsatisfied: Sequence[Mapping[str, Any]]
) -> list[list[str]]:
    commands: list[list[str]] = []
    if any(row.get("kind") == "activation" for row in unsatisfied):
        commands.append(["fulcrum", "plan", "activate", root_id, "--json"])
    if any(row.get("kind") == "publication" for row in unsatisfied):
        commands.append(
            ["fulcrum", "knowledge", "publish", "--bead", root_id, "--json"]
        )
    for row in unsatisfied:
        if row.get("kind") == "child" and row.get("bead_id"):
            commands.append(["fulcrum", "work", "show", str(row["bead_id"]), "--json"])
    return commands


def _future_waiting(root_id: str) -> dict[str, Any]:
    return _plan_waiting(root_id, future=True)


def _plan_waiting(
    root_id: str,
    *,
    future: bool = False,
    publication: bool = False,
    authoring: bool = False,
) -> dict[str, Any]:
    reasons: list[dict[str, Any]] = []
    if future:
        reasons.append(
            {
                "id": f"future-plan:{root_id}",
                "kind": "future_activation",
                "reason": "future plan has no executed human/Vizier activation authorization",
                "reconsider_when": [{"event": "policy_changed", "subject": root_id}],
                "since": utc_now(),
            }
        )
    if publication:
        reasons.append(
            {
                "id": f"plan-publication:{root_id}",
                "kind": "external",
                "reason": "required remote plan publication has not been observed",
                "reconsider_when": [{"event": "external_changed", "subject": root_id}],
                "since": utc_now(),
            }
        )
    if authoring:
        reasons.append(
            {
                "id": f"plan-authoring:{root_id}",
                "kind": "authoring",
                "reason": "Weaver has not finished the published plan authoring turn",
                "reconsider_when": [{"event": "owner_changed", "subject": root_id}],
                "since": utc_now(),
            }
        )
    return {"reasons": reasons}


def _remote_publication_required(value: Any) -> bool:
    return isinstance(value, Mapping) and bool(value.get("require_remote_sync"))


def _without_future_waiting(value: Any) -> dict[str, Any] | None:
    reasons = value.get("reasons", []) if isinstance(value, Mapping) else []
    retained = [
        dict(row)
        for row in reasons
        if isinstance(row, Mapping) and row.get("kind") != "future_activation"
    ]
    return {"reasons": retained} if retained else None


def _edges(tasks: Mapping[str, Mapping[str, Any]]) -> list[dict[str, str]]:
    return [
        {"from": key, "to": str(target)}
        for key, spec in tasks.items()
        for target in spec.get("depends_on", [])
    ]


def _dispositions(value: Any) -> dict[str, dict[str, str]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise _invalid("dispositions", "must map stable keys to outcome and reason")
    result: dict[str, dict[str, str]] = {}
    for key, row in value.items():
        if (
            not isinstance(key, str)
            or not isinstance(row, Mapping)
            or set(row) != {"outcome", "reason"}
            or row.get("outcome") not in {"cancelled", "deferred"}
            or not isinstance(row.get("reason"), str)
            or not str(row["reason"]).strip()
        ):
            raise _invalid(
                "dispositions",
                "entries require outcome cancelled|deferred and nonempty reason",
            )
        result[key] = {
            "outcome": str(row["outcome"]),
            "reason": str(row["reason"]).strip(),
        }
    return result


def _waivers(value: Any, actor: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise _invalid("waivers", "must be an array")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in value:
        if (
            not isinstance(row, Mapping)
            or set(row) != {"perspective", "reason"}
            or row.get("perspective") not in REQUIRED_REVIEW_PERSPECTIVES
            or not isinstance(row.get("reason"), str)
            or not str(row["reason"]).strip()
        ):
            raise _invalid("waivers", "entries require perspective and nonempty reason")
        perspective = str(row["perspective"])
        if perspective in seen:
            raise _invalid("waivers", "perspectives must be unique")
        seen.add(perspective)
        result.append(
            {
                "perspective": perspective,
                "reason": str(row["reason"]).strip(),
                "actor": actor,
            }
        )
    return result


def _string_mapping(value: Any, field: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) and isinstance(item, str) and item.strip()
        for key, item in value.items()
    ):
        raise _invalid(field, "must map operation IDs to nonempty text")
    return {str(key): str(item).strip() for key, item in value.items()}


def _root(ledger: Ledger, identifier: str) -> LedgerRecord:
    record = ledger.show(identifier)
    if record is None or record.kind != "work" or not record.fc:
        raise FulcrumError.invalid("NOT_FOUND", f"unknown plan root {identifier}")
    if record.fc.get("workflow_root") != record.id:
        raise _invalid("bead", f"{identifier} is not a workflow root")
    return record


def _plan(root: LedgerRecord) -> dict[str, Any]:
    value = (root.fc or {}).get("plan")
    if not isinstance(value, Mapping):
        raise FulcrumError.invalid(
            "PLAN_DRAFT_REQUIRED", "root has no retained plan draft"
        )
    return dict(value)


def _authorize_author(
    request: ParsedRequest,
    root: LedgerRecord,
    ledger: Ledger,
    *,
    allow_marshal: bool = False,
) -> None:
    if request.actor.kind == "human":
        return
    fc = root.fc or {}
    allowed = {fc.get("owner")}
    if allow_marshal:
        allowed.add(_marshal_or_human(ledger))
    if (
        request.actor.kind == "task"
        and request.actor.task_id == request.thread_id
        and request.thread_id in allowed
        and request.ownership_operation == fc.get("ownership_operation")
    ):
        return
    raise FulcrumError(
        "OWNERSHIP_CONFLICT",
        "plan mutation requires the current author or human",
        exit_code=5,
    )


def _authorize_approver(request: ParsedRequest, ledger: Ledger) -> str:
    if request.actor.kind == "human":
        return "human"
    control = ledger.show("fc-system")
    vizier = control.fc.get("vizier_thread") if control and control.fc else None
    if (
        request.actor.kind == "task"
        and request.actor.task_id == vizier
        and request.thread_id == vizier
    ):
        return str(vizier)
    raise FulcrumError(
        "OWNERSHIP_CONFLICT",
        "only a human or current Vizier may approve a plan",
        exit_code=5,
    )


def _authorize_activation(request: ParsedRequest, ledger: Ledger) -> str:
    return _authorize_approver(request, ledger)


def _authorize_activation_execution(
    request: ParsedRequest, ledger: Ledger, root: LedgerRecord
) -> None:
    if request.actor.kind in {"human", "controller"}:
        return
    marshal = _marshal_or_human(ledger)
    if (
        request.actor.kind == "task"
        and request.thread_id == marshal
        and request.actor.task_id == marshal
    ):
        return
    raise FulcrumError(
        "ACTIVATION_NOT_AUTHORIZED",
        "only Marshal may execute a retained activation authorization",
        exit_code=5,
    )


def _authorize_completion(
    request: ParsedRequest, root: LedgerRecord, ledger: Ledger
) -> None:
    if request.actor.kind in {"human", "controller"}:
        return
    fc = root.fc or {}
    if (
        request.actor.kind == "task"
        and request.actor.task_id == request.thread_id == fc.get("owner")
        and request.ownership_operation == fc.get("ownership_operation")
    ):
        return
    raise FulcrumError(
        "OWNERSHIP_CONFLICT",
        "plan completion requires the current owner, controller, or human",
        exit_code=5,
    )


def _ledger(request: ParsedRequest) -> Ledger:
    if request.instance.brain_root is None:
        raise FulcrumError(
            "LEDGER_UNAVAILABLE", "brain root is unavailable", exit_code=4
        )
    manager = ConfigurationManager(request.instance.config_path)
    document, _ = manager.load()
    config = manager.effective(document)
    beads = config["beads"]
    executable = beads.get("executable") if isinstance(beads, Mapping) else None
    return Ledger(
        request.instance.brain_root,
        executable=str(executable) if executable else None,
        timeout=request.timeout,
    )


def _invalid(field: str, message: str) -> FulcrumError:
    return FulcrumError.invalid("INVALID_PLAN", f"{field}: {message}")
