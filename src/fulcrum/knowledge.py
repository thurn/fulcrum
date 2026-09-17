"""Canonical plan and configuration document publication."""

from __future__ import annotations

from fulcrum.coordination import coordinated

import uuid
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from fulcrum.brain import (
    IsolatedGitPublisher,
    KnowledgePublicationError,
    PublicationDestination,
)
from fulcrum.configuration import ConfigurationManager
from fulcrum.contracts import CommandResult, CommandState, FulcrumError, ParsedRequest
from fulcrum.ledger import (
    Ledger,
    LedgerRecord,
    OperationRecord,
    operation_id,
    operation_view,
    utc_now,
)

TERMINAL_OPERATION_STATES = {"completed", "failed", "uncertain", "cancelled"}
CONFIG_LEDGER_SYNC_NAMESPACE = uuid.UUID("17bc2d92-d45a-4f92-a8db-3469d4de82a7")


class KnowledgeService:
    def __init__(self, publisher: IsolatedGitPublisher | None = None) -> None:
        self.publisher: IsolatedGitPublisher = publisher or IsolatedGitPublisher()

    @coordinated
    def publish(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        identifier = str(request.arguments["bead"])
        record = ledger.show(identifier)
        if record is None or record.kind != "work":
            raise FulcrumError.invalid(
                "NOT_FOUND", f"no publishable plan exists at {identifier}"
            )
        _authorize_publication(request, ledger, record)
        config = _config(request)
        documents, destination_name, publication = _publication_documents(
            record, config
        )
        destination = _destination(
            request, record, config, destination_name, publication
        )
        try:
            validated = self.publisher.validate(documents, destination)
        except (OSError, KnowledgePublicationError) as error:
            raise FulcrumError(
                "INVALID_PUBLICATION",
                str(error),
                exit_code=5,
                details={"bead_id": record.id},
            ) from error
        operation, reused = ledger.create_operation(
            request,
            bead_id=record.id,
            owner=str((record.fc or {}).get("owner") or "HUMAN"),
            planned={
                "publication_intent": {
                    "operation_id": None,
                    "origin_task": request.thread_id or request.actor.task_id,
                    "origin_bead": record.id,
                    "destination": destination_name,
                    "prior_publication_commit": _prior_publication_commit(record),
                    **validated,
                }
            },
            next_action="Publish only the retained selected content from an isolated Git worktree.",
        )
        if reused and operation.operation.get("state") in TERMINAL_OPERATION_STATES:
            return _operation_result(operation)
        retained = operation.operation.get("planned")
        if not isinstance(retained, Mapping) or not isinstance(
            retained.get("publication_intent"), Mapping
        ):
            raise FulcrumError(
                "OPERATION_CORRUPT",
                "publication receipt lost its selected content",
                exit_code=4,
            )
        intent = dict(retained["publication_intent"])
        if intent.get("operation_id") is None:
            intent["operation_id"] = operation.id
            operation = ledger.update_operation(
                operation,
                planned={"publication_intent": intent},
                step="publication_content_retained",
                external={
                    "adapter": "isolated_git",
                    "destination_root": validated["root"],
                    "selected_paths": [
                        row["relative_path"] for row in validated["documents"]
                    ],
                },
            )
        try:
            result = self.publisher.publish(
                {
                    "operation_id": operation.id,
                    "documents": intent["documents"],
                    "prior_publication_commit": intent.get("prior_publication_commit"),
                },
                destination,
            )
        except KnowledgePublicationError as error:
            # The canonical Beads content remains authoritative and visible. A
            # local Git problem is a repair fact, not permission to discard it.
            result = {
                "destination_root": validated["root"],
                "selected_paths": [
                    row["relative_path"] for row in validated["documents"]
                ],
                "branch": validated["branch"],
                "remote": {
                    "state": "failed",
                    "commit": None,
                    "contains_local": False,
                    "error": str(error),
                },
                "local": {"state": "failed", "commit": None},
                "repair": {
                    "kind": "publication_local",
                    "reason": str(error),
                },
            }
        facts = {
            **result,
            "operation_id": operation.id,
            "origin_task": request.thread_id or request.actor.task_id,
            "origin_bead": record.id,
            "destination": destination_name,
            "relative_paths": [row["relative_path"] for row in validated["documents"]],
            "require_remote_sync": destination.require_remote_sync,
        }
        updated = _record_publication(ledger, record, facts, operation.id)
        ready = _publication_ready(facts)
        operation = ledger.update_operation(
            operation,
            state="completed",
            step=(
                "publication_observed" if ready else "publication_retained_for_repair"
            ),
            external={
                "adapter": "isolated_git",
                "destination_root": facts.get("destination_root"),
                "worktree_path": facts.get("worktree_path"),
                "local_commit": _nested(facts, "local", "commit"),
                "remote_commit": _nested(facts, "remote", "commit"),
            },
            result={
                "bead_id": updated.id,
                "publication": facts,
                "publication_ready": ready,
            },
            error=(
                None
                if ready
                else {
                    "code": "PUBLICATION_NEEDS_REPAIR",
                    "message": str(
                        _nested(facts, "repair", "reason")
                        or "publication is not fully observed"
                    ),
                    "retryable": True,
                }
            ),
            next_action=(
                "No publication action remains."
                if ready
                else "Repair or retry the retained publication without discarding local content."
            ),
        )
        return _operation_result(operation)

    @coordinated
    def config_sync(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        _authorize_config_publication(request, ledger)
        config = _config(request)
        raw = request.instance.config_path.read_text(encoding="utf-8")
        destination = _destination(
            request,
            None,
            config,
            "knowledge",
            {"require_remote_sync": bool(config["knowledge"]["require_remote_sync"])},
        )
        documents = [{"relative_path": "fulcrum.yaml", "content": raw}]
        try:
            validated = self.publisher.validate(documents, destination)
        except (OSError, KnowledgePublicationError) as error:
            raise FulcrumError(
                "INVALID_PUBLICATION", str(error), exit_code=5
            ) from error
        operation, reused = ledger.create_operation(
            request,
            owner=request.thread_id or request.actor.task_id or "HUMAN",
            planned={
                "publication_intent": {
                    "operation_id": None,
                    "origin_task": request.thread_id or request.actor.task_id,
                    "origin_bead": "fc-system",
                    "destination": "knowledge",
                    "prior_publication_commit": _control_publication_commit(ledger),
                    **validated,
                }
            },
            next_action="Publish the exact currently authorized YAML from an isolated worktree.",
        )
        if reused and operation.operation.get("state") in TERMINAL_OPERATION_STATES:
            return _operation_result(operation)
        retained = dict(operation.operation.get("planned") or {})
        intent = dict(retained.get("publication_intent") or {})
        intent["operation_id"] = operation.id
        operation = ledger.update_operation(
            operation,
            planned={"publication_intent": intent},
            step="configuration_content_retained",
        )
        try:
            result = self.publisher.publish(
                {
                    "operation_id": operation.id,
                    "documents": intent["documents"],
                    "allow_live_selected": True,
                    "prior_publication_commit": intent.get("prior_publication_commit"),
                },
                destination,
            )
        except KnowledgePublicationError as error:
            result = {
                "local": {"state": "failed", "commit": None},
                "remote": {
                    "state": "failed",
                    "commit": None,
                    "contains_local": False,
                    "error": str(error),
                },
                "repair": {"kind": "publication_local", "reason": str(error)},
            }
        facts = {
            **result,
            "operation_id": operation.id,
            "origin_task": request.thread_id or request.actor.task_id,
            "origin_bead": "fc-system",
            "destination": "knowledge",
            "relative_paths": ["fulcrum.yaml"],
            "require_remote_sync": destination.require_remote_sync,
        }
        ready = _publication_ready(facts)
        ledger_request_id = str(uuid.uuid5(CONFIG_LEDGER_SYNC_NAMESPACE, operation.id))
        control = ledger.show("fc-system")
        if control is not None and control.fc:
            fc = dict(control.fc)
            fc["config_publication"] = facts
            fc["last_transition"] = operation.id
            ledger.update_fc(control.id, fc)
        operation = ledger.update_operation(
            operation,
            state="completed",
            step=(
                "configuration_publication_observed"
                if ready
                else "configuration_publication_needs_repair"
            ),
            result={
                "path": str(request.instance.config_path),
                "publication": facts,
                "publication_ready": ready,
                "ledger_publication_operation": operation_id(ledger_request_id),
            },
            error=(
                None
                if ready
                else {
                    "code": "PUBLICATION_NEEDS_REPAIR",
                    "message": str(
                        _nested(facts, "repair", "reason")
                        or "configuration publication is incomplete"
                    ),
                    "retryable": True,
                }
            ),
            next_action=(
                "No publication action remains."
                if ready
                else "Repair or retry the retained configuration publication."
            ),
        )
        from fulcrum.publication import LedgerPublicationService

        native = LedgerPublicationService().sync(
            replace(
                request,
                command=("ledger", "sync"),
                arguments={},
                input={},
                request_id=ledger_request_id,
            )
        )
        view = operation_view(operation)
        retained_result = dict(view.get("result") or {})
        retained_result["ledger_publication"] = native.to_dict()
        view["result"] = retained_result
        return CommandResult(
            ok=operation.operation.get("state") not in {"failed", "uncertain"},
            state=CommandState(str(operation.operation.get("state"))),
            operation_id=operation.id,
            request_id=request.request_id,
            result=view,
        )


def _publication_documents(
    record: LedgerRecord, config: Mapping[str, Any]
) -> tuple[list[dict[str, str]], str, Mapping[str, Any]]:
    plan = (record.fc or {}).get("plan")
    if not isinstance(plan, Mapping) or not isinstance(
        plan.get("published_scope"), Mapping
    ):
        raise FulcrumError.invalid(
            "PLAN_NOT_PUBLISHED",
            "knowledge publication requires canonical published plan scope",
        )
    publication = plan.get("publication")
    if not isinstance(publication, Mapping) or not publication.get("required"):
        raise FulcrumError.invalid(
            "PUBLICATION_NOT_REQUESTED",
            "this plan has no document publication destination",
        )
    relative = publication.get("relative_path")
    destination = publication.get("destination")
    if not isinstance(relative, str) or destination not in {"knowledge", "project"}:
        raise FulcrumError.invalid(
            "INVALID_PUBLICATION", "plan publication facts are incomplete"
        )
    return (
        [{"relative_path": relative, "content": _render_plan_markdown(record, plan)}],
        str(destination),
        publication,
    )


def _destination(
    request: ParsedRequest,
    record: LedgerRecord | None,
    config: Mapping[str, Any],
    name: str,
    publication: Mapping[str, Any],
) -> PublicationDestination:
    if name == "knowledge":
        selected = config["knowledge"]
        root = Path(str(selected["root"]))
        remote = selected.get("remote")
        branch = selected.get("branch")
    else:
        if record is None or not record.fc:
            raise FulcrumError.invalid(
                "INVALID_PUBLICATION", "project destination requires a plan"
            )
        project_id = str(record.fc.get("project"))
        selected = config["projects"].get(project_id)
        if not isinstance(selected, Mapping):
            raise FulcrumError.invalid(
                "INVALID_PROJECT", f"unknown project {project_id}"
            )
        root = Path(str(selected["root"]))
        remote = selected.get("source_remote")
        branch = selected.get("integration_branch")
    return PublicationDestination(
        root=root,
        worktree_root=request.instance.instance_root / "publication-worktrees",
        remote=str(remote) if remote else None,
        branch=str(branch) if branch else None,
        require_remote_sync=bool(publication.get("require_remote_sync", False)),
    )


def _record_publication(
    ledger: Ledger, record: LedgerRecord, facts: Mapping[str, Any], operation_id: str
) -> LedgerRecord:
    fc = dict(record.fc or {})
    plan = dict(fc.get("plan") or {})
    prior = dict(plan.get("publication") or {})
    prior.update(dict(facts))
    plan["publication"] = prior
    plan["previous_publication"] = None
    ready = _publication_ready(facts)
    activation_ready = _activation_publication_ready(facts)
    plan["publication_ready"] = activation_ready
    plan["publication_operation"] = operation_id
    fc["plan"] = plan
    if activation_ready:
        fc["waiting"] = _without_waiting(fc.get("waiting"), "external")
        fc["next_action"] = _next_action(fc, ready)
        mapping = dict(plan.get("children_by_key") or {})
        for key in plan.get("approved_keys", []):
            child = ledger.show(str(mapping.get(str(key))))
            if child is None or child.status == "closed" or not child.fc:
                continue
            child_fc = dict(child.fc)
            link = dict(child_fc.get("plan") or {})
            link["publication_ready"] = activation_ready
            link["publication_operation"] = operation_id
            child_fc["plan"] = link
            if activation_ready:
                child_fc["waiting"] = _without_waiting(
                    child_fc.get("waiting"), "external"
                )
            child_fc["next_action"] = _next_action(child_fc, activation_ready)
            child_fc["last_transition"] = operation_id
            ledger.update_fc(child.id, child_fc)
    fc["last_transition"] = operation_id
    return ledger.update_fc(record.id, fc)


def _next_action(fc: Mapping[str, Any], publication_ready: bool) -> str:
    reasons = (
        fc.get("waiting", {}).get("reasons", [])
        if isinstance(fc.get("waiting"), Mapping)
        else []
    )
    kinds = {
        str(reason.get("kind")) for reason in reasons if isinstance(reason, Mapping)
    }
    if not publication_ready:
        return (
            "Repair required publication while preserving the retained local content."
        )
    if "authoring" in kinds:
        return "Weaver must finish the published plan authoring turn."
    if "future_activation" in kinds:
        return "Wait for explicit activation of the approved future plan."
    return "Marshal may dispatch approved plan children under current policy."


def _publication_ready(facts: Mapping[str, Any]) -> bool:
    local = facts.get("local")
    remote = facts.get("remote")
    if not isinstance(local, Mapping) or local.get("state") != "observed":
        return False
    return not facts.get("require_remote_sync") or (
        isinstance(remote, Mapping)
        and remote.get("state") == "observed"
        and remote.get("contains_local") is True
    )


def _activation_publication_ready(facts: Mapping[str, Any]) -> bool:
    if not facts.get("require_remote_sync"):
        return True
    remote = facts.get("remote")
    return (
        isinstance(remote, Mapping)
        and remote.get("state") == "observed"
        and remote.get("contains_local") is True
    )


def _render_plan_markdown(record: LedgerRecord, plan: Mapping[str, Any]) -> str:
    scope = dict(plan["published_scope"])
    draft = dict(scope.get("draft") or {})
    lines = [
        f"# {record.title}",
        "",
        str(draft.get("summary") or ""),
        "",
        "## Plan",
        "",
        str(draft.get("text") or ""),
        "",
        "## Tasks",
        "",
    ]
    mapping = dict(plan.get("children_by_key") or {})
    for task in draft.get("tasks", []):
        if not isinstance(task, Mapping):
            continue
        key = str(task.get("key"))
        lines.extend(
            (
                f"### {key} — {task.get('title')}",
                "",
                f"Bead: `{mapping.get(key)}`",
                "",
                str(task.get("outcome") or ""),
                "",
                "Acceptance:",
                *(f"- {item}" for item in task.get("acceptance", [])),
                "",
                "Dependencies: "
                + (
                    ", ".join(str(item) for item in task.get("depends_on", []))
                    or "none"
                ),
                "",
            )
        )
    validation = draft.get("validation")
    if isinstance(validation, Mapping):
        lines.extend(("## Validation", "", str(validation.get("summary") or ""), ""))
        for check in validation.get("checks", []):
            if isinstance(check, Mapping):
                lines.append(
                    f"- {check.get('criterion')} — evidence: {check.get('evidence_required')}"
                )
    lines.extend(("", f"_Plan root: {record.id}_", ""))
    return "\n".join(lines)


def _authorize_publication(
    request: ParsedRequest, ledger: Ledger, record: LedgerRecord
) -> None:
    if request.actor.kind in {"human", "system"}:
        return
    actor = request.thread_id or request.actor.task_id
    if request.actor.kind == "task" and actor == (record.fc or {}).get("owner"):
        return
    control = ledger.show("fc-system")
    control_fc = control.fc if control is not None and control.fc else {}
    if actor in {control_fc.get("marshal_thread"), control_fc.get("vizier_thread")}:
        return
    raise FulcrumError(
        "OWNERSHIP_CONFLICT",
        "knowledge publication requires human, current owner, or standing leadership authority",
        exit_code=5,
    )


def _authorize_config_publication(request: ParsedRequest, ledger: Ledger) -> None:
    if request.actor.kind in {"human", "system"}:
        return
    control = ledger.show("fc-system")
    vizier = (control.fc or {}).get("vizier_thread") if control is not None else None
    actor = request.thread_id or request.actor.task_id
    if request.actor.kind == "task" and actor == vizier:
        return
    raise FulcrumError(
        "AUTHORITY_REQUIRED",
        "only human or current Vizier may publish authoritative configuration",
        exit_code=5,
    )


def _without_waiting(value: Any, kind: str) -> dict[str, Any] | None:
    reasons = value.get("reasons", []) if isinstance(value, Mapping) else []
    retained = [
        dict(reason)
        for reason in reasons
        if isinstance(reason, Mapping) and reason.get("kind") != kind
    ]
    return {"reasons": retained} if retained else None


def _nested(value: Mapping[str, Any], first: str, second: str) -> Any:
    child = value.get(first)
    return child.get(second) if isinstance(child, Mapping) else None


def _prior_publication_commit(record: LedgerRecord) -> str | None:
    fc = record.fc or {}
    plan = fc.get("plan")
    publication = (
        plan.get("previous_publication") or plan.get("publication")
        if isinstance(plan, Mapping)
        else None
    )
    local = publication.get("local") if isinstance(publication, Mapping) else None
    commit = local.get("commit") if isinstance(local, Mapping) else None
    return str(commit) if isinstance(commit, str) else None


def _control_publication_commit(ledger: Ledger) -> str | None:
    control = ledger.show("fc-system")
    publication = (
        control.fc.get("config_publication")
        if control is not None and control.fc
        else None
    )
    local = publication.get("local") if isinstance(publication, Mapping) else None
    commit = local.get("commit") if isinstance(local, Mapping) else None
    return str(commit) if isinstance(commit, str) else None


def _config(request: ParsedRequest) -> dict[str, Any]:
    manager = ConfigurationManager(request.instance.config_path)
    document, _ = manager.load()
    return manager.effective(document)


def _ledger(request: ParsedRequest) -> Ledger:
    if request.instance.brain_root is None:
        raise FulcrumError(
            "LEDGER_UNAVAILABLE", "knowledge requires a configured brain", exit_code=4
        )
    config = _config(request)
    beads = config["beads"]
    executable = beads.get("executable") if isinstance(beads, Mapping) else None
    return Ledger(
        request.instance.brain_root,
        executable=str(executable) if executable else None,
        timeout=request.timeout,
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
        request_id=(
            str(operation.operation.get("request_id"))
            if operation.operation.get("request_id")
            else None
        ),
        operation_id=operation.id,
        result=operation_view(operation),
    )
