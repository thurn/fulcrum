"""Application operations for managed workspaces and source delivery."""

from __future__ import annotations

from fulcrum.coordination import unlocked

from fulcrum.coordination import coordinated, external_effect

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, Coroutine, TypeVar
import uuid

from fulcrum.configuration import ConfigurationManager
from fulcrum.contracts import CommandResult, CommandState, FulcrumError, ParsedRequest
from fulcrum.delivery import (
    Delivery,
    DeliveryFacts,
    DeliveryProviderError,
    SourceRef,
    TollgateDelivery,
    WorkRef,
    WorkspaceFacts,
)
from fulcrum.ledger import (
    Ledger,
    LedgerRecord,
    OperationRecord,
    operation_view,
    utc_now,
)
from fulcrum.tollgate import Tollgate, TollgateError
from fulcrum.desktop_protocol import _protocol, require_run_control

T = TypeVar("T")
TERMINAL_STATES = {"completed", "failed", "uncertain", "cancelled"}
LOCAL_CHECK_NAMESPACE = uuid.UUID("2783db95-33a5-4f6e-b295-bdd07c96c9a5")


class DeliveryService:
    @coordinated
    def worktree_prepare(self, request: ParsedRequest) -> CommandResult:
        ledger, work, project, provider = _context(request)
        _authorize(ledger, request, work)
        require_run_control(ledger, "workspace preparation")
        operation, reused = ledger.create_operation(
            request,
            bead_id=work.id,
            planned={"work": _work_ref(request, work, project, "pending").to_dict()},
            next_action="Create or recover the exact provider-owned worktree.",
        )
        if reused and operation.operation.get("state") in TERMINAL_STATES:
            return _operation_result(operation)
        reference = _retained_work_ref(operation, operation.id)
        operation = ledger.update_operation(
            operation.id,
            step="workspace_identity_retained",
            planned={"work": reference.to_dict()},
            next_action="Create or inspect the retained branch through the delivery provider.",
        )
        try:
            with external_effect():
                facts = _call(provider.prepare(reference))
        except DeliveryProviderError as error:
            return _failed_operation(ledger, operation, error, "workspace_prepare")
        _retain_workspace(ledger, work, reference, facts, operation.id)
        operation = ledger.update_operation(
            operation.id,
            state="completed",
            step="workspace_verified",
            external={
                "provider": "tollgate",
                "repository_id": reference.repository_id,
                "path": facts.path,
                "branch": facts.branch,
            },
            result={"workspace": facts.to_dict(), "work": reference.to_dict()},
            next_action="Use the exact observed workspace for implementation.",
        )
        return _operation_result(operation)

    @coordinated
    def worktree_inspect(self, request: ParsedRequest) -> CommandResult:
        _, work, project, provider = _context(request)
        reference = _work_ref(request, work, project, _workspace_operation(work))
        try:
            with external_effect():
                facts = _call(provider.inspect_workspace(reference))
        except DeliveryProviderError as error:
            raise _public_error(error, request) from error
        return CommandResult.query(
            {
                "bead_id": work.id,
                "project": reference.project_id,
                "repository_id": reference.repository_id,
                "workspace": facts.to_dict(),
            }
        )

    @coordinated
    def worktree_cleanup(self, request: ParsedRequest) -> CommandResult:
        ledger, work, project, provider = _context(request)
        _authorize(ledger, request, work)
        require_run_control(ledger, "workspace cleanup")
        _require_cleanup_settled(work)
        operation, reused = ledger.create_operation(
            request,
            bead_id=work.id,
            planned={
                "work": _work_ref(
                    request, work, project, _workspace_operation(work)
                ).to_dict()
            },
            next_action="Verify settled delivery and remove only the retained clean workspace.",
        )
        if reused and operation.operation.get("state") in TERMINAL_STATES:
            return _operation_result(operation)
        reference = _retained_work_ref(operation, _workspace_operation(work))
        try:
            with external_effect():
                facts = _call(provider.cleanup(reference))
        except DeliveryProviderError as error:
            return _failed_operation(ledger, operation, error, "workspace_cleanup")
        current = ledger.show(work.id)
        assert current is not None and current.fc
        fc = dict(current.fc)
        workspace = dict(fc.get("worktree") or {})
        workspace.update(facts.to_dict())
        workspace["cleanup_operation"] = operation.id
        fc["worktree"] = workspace
        delivery = dict(fc.get("delivery") or {})
        delivery["cleanup"] = {
            "state": "observed",
            "observed_at": facts.observed_at,
            "workspace": facts.to_dict(),
        }
        fc["delivery"] = delivery
        fc["last_transition"] = operation.id
        ledger.update_fc(work.id, fc)
        operation = ledger.update_operation(
            operation.id,
            state="completed",
            step="workspace_cleanup_verified",
            result={"workspace": facts.to_dict()},
            next_action="No managed workspace cleanup remains.",
        )
        return _operation_result(operation)

    @coordinated
    def validation_check(self, request: ParsedRequest) -> CommandResult:
        ledger, work, project, provider = _context(request)
        _authorize(ledger, request, work)
        source_oid = str(request.arguments["source"])
        reference = _work_ref(request, work, project, _workspace_operation(work))
        source = SourceRef(reference, source_oid)
        canonical_request = replace(
            request,
            request_id=str(
                uuid.uuid5(
                    LOCAL_CHECK_NAMESPACE,
                    f"{work.id}:{source_oid}:{list(reference.validate_argv)!r}",
                )
            ),
        )
        operation, reused = ledger.create_operation(
            canonical_request,
            bead_id=work.id,
            planned={"source": source.to_dict()},
            next_action="Run the configured validation command once for this exact source.",
        )
        if reused and operation.operation.get("state") in TERMINAL_STATES:
            return _operation_result(operation)
        source = _retained_source_ref(operation)
        try:
            with external_effect():
                facts = _call(provider.validate(source))
        except DeliveryProviderError as error:
            _retain_local_check(
                ledger,
                work,
                {
                    "source_oid": source.oid,
                    "state": "unresolved",
                    "argv": list(source.work.validate_argv),
                    "operation_id": operation.id,
                    "error": {
                        "category": error.category,
                        "message": str(error),
                        "evidence": error.evidence,
                    },
                    "observed_at": utc_now(),
                },
            )
            return _failed_operation(ledger, operation, error, "local_check")
        retained = {**facts.to_dict(), "operation_id": operation.id}
        _retain_local_check(ledger, work, retained)
        if facts.state not in {"passed", "not_required"}:
            operation = ledger.update_operation(
                operation.id,
                state="failed",
                step="required_local_check_failed",
                error={
                    "code": "REQUIRED_CHECK_FAILED",
                    "message": "the configured exact-source validation command failed",
                    "retryable": False,
                    "evidence": retained,
                },
                result={"local_check": retained},
                next_action=(
                    "Warden must repair this exact failure in the same workspace and "
                    "submit a new source OID."
                ),
            )
            return _operation_result(operation)
        operation = ledger.update_operation(
            operation.id,
            state="completed",
            step=(
                "required_local_check_passed"
                if facts.state == "passed"
                else "local_check_not_required"
            ),
            result={"local_check": retained},
            next_action="Reuse this exact-source check receipt for provider validation.",
        )
        return _operation_result(operation)

    @coordinated
    def validation_start(self, request: ParsedRequest) -> CommandResult:
        ledger, work, project, provider = _context(request)
        _authorize(ledger, request, work)
        require_run_control(ledger, "provider validation submission")
        source_oid = str(request.arguments["source"])
        reference = _work_ref(request, work, project, _workspace_operation(work))
        source = SourceRef(reference, source_oid)
        operation, reused = ledger.create_operation(
            request,
            bead_id=work.id,
            planned={"source": source.to_dict()},
            next_action="Verify and submit the exact immutable source commit.",
        )
        if reused and operation.operation.get("state") in TERMINAL_STATES:
            return _operation_result(operation)
        source = _retained_source_ref(operation)
        with external_effect():
            local_check = self.validation_check(
                replace(
                    request,
                    command=("validation", "check"),
                    arguments={"bead": work.id, "source": source.oid},
                    input={},
                    request_id=str(uuid.uuid4()),
                )
            )
        if local_check.state is not CommandState.COMPLETED:
            payload = (
                dict(local_check.result)
                if isinstance(local_check.result, Mapping)
                else {}
            )
            operation = ledger.update_operation(
                operation.id,
                state=(
                    "uncertain"
                    if local_check.state is CommandState.UNCERTAIN
                    else "failed"
                ),
                step="required_local_check_unresolved",
                external={"local_check_operation": local_check.operation_id},
                result={"local_check": payload},
                error=(
                    dict(payload.get("error"))
                    if isinstance(payload.get("error"), Mapping)
                    else {
                        "code": "REQUIRED_CHECK_FAILED",
                        "message": "required exact-source validation did not pass",
                        "retryable": False,
                    }
                ),
                next_action=(
                    "Warden must inspect the authoritative configured validation "
                    "failure, repair the workspace, and submit a new source OID."
                ),
            )
            return _operation_result(operation)
        from fulcrum.scenario_delivery import wait_for_scenario_admission

        with external_effect():
            admission = wait_for_scenario_admission(request, ledger, work, source)
        try:
            with external_effect():
                facts = _call(provider.submit(source))
        except DeliveryProviderError as error:
            return _failed_operation(ledger, operation, error, "validation_submit")

        _retain_delivery(
            ledger,
            work,
            {
                "source_oid": facts.source_oid,
                "provider_handle": facts.handle,
                "validation": {
                    "state": facts.state,
                    "facts": facts.to_dict(),
                },
                "approved_source": None,
                "promotion": {"state": "not_started"},
                "synchronization": {"state": "pending"},
                "cleanup": {"state": "pending"},
                "evidence": {
                    "validation": facts.evidence,
                    **({"admission_barrier": admission} if admission else {}),
                },
            },
            operation.id,
        )
        _retain_conflict_base(ledger, work, facts.evidence, operation.id)
        current = ledger.show(work.id)
        if current is not None and current.fc and current.fc.get("role") == "warden":
            fc = dict(current.fc)
            fc["phase"] = "reviewing"
            fc["next_action"] = (
                "Inspect exact-source validation and explicitly approve the current source."
            )
            fc["last_transition"] = operation.id
            ledger.update_fc(work.id, fc)
        operation = ledger.update_operation(
            operation.id,
            state="completed",
            step="validation_submission_observed",
            external={
                "provider": "tollgate",
                "repository_id": source.work.repository_id,
                "handle": facts.handle,
                **({"admission_barrier": admission} if admission else {}),
            },
            result={"validation": facts.to_dict()},
            next_action="Inspect validation; pending/running work remains provider-owned.",
        )
        return _operation_result(operation)

    @coordinated
    def validation_show(self, request: ParsedRequest) -> CommandResult:
        _, work, project, provider = _context(request)
        source, handle = _retained_delivery_source(request, work, project)
        try:
            with external_effect():
                facts = _call(provider.inspect(source, handle))
        except DeliveryProviderError as error:
            raise _public_error(error, request) from error
        return CommandResult.query(
            {
                "bead_id": work.id,
                "handle": facts.handle,
                "source_oid": facts.source_oid,
                "state": facts.validation,
                "checks": facts.evidence.get("buildset"),
                "delivery": normalized_delivery(facts),
                "provider_delivery": facts.to_dict(),
            }
        )

    @coordinated
    def review_approve(self, request: ParsedRequest) -> CommandResult:
        ledger, work, project, provider = _context(request)
        _authorize_warden(ledger, request, work)
        requested_source = str(request.arguments["source"])
        summary = request.arguments.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise FulcrumError.invalid(
                "INVALID_INPUT", "review approval requires a summary"
            )
        source, handle = _retained_delivery_source(request, work, project)
        if requested_source != source.oid:
            raise FulcrumError(
                "STALE_SOURCE",
                "review approval source does not match the retained validation submission",
                exit_code=5,
                details={"requested": requested_source, "validated": source.oid},
            )
        delivery = (work.fc or {}).get("delivery")
        validation = (
            delivery.get("validation") if isinstance(delivery, Mapping) else None
        )
        if not isinstance(validation, Mapping) or validation.get("state") != "passed":
            raise FulcrumError(
                "VALIDATION_NOT_PASSED",
                "review approval requires passed validation of the exact source",
                exit_code=5,
                details={"validation": validation},
            )
        operation, reused = ledger.create_operation(
            request,
            bead_id=work.id,
            planned={
                "source": source.to_dict(),
                "handle": handle,
                "acceptance": list((work.fc or {}).get("acceptance") or []),
                "summary": summary,
            },
            next_action="Verify the current clean source and retain explicit Warden approval.",
        )
        if reused and operation.operation.get("state") in TERMINAL_STATES:
            return _operation_result(operation)
        try:
            with external_effect():
                workspace = _call(provider.inspect_workspace(source.work))
        except DeliveryProviderError as error:
            return _failed_operation(ledger, operation, error, "review_approval")
        if (
            not workspace.exists
            or not workspace.owned
            or workspace.dirty
            or workspace.head_oid != source.oid
        ):
            operation = ledger.update_operation(
                operation.id,
                state="failed",
                step="review_source_changed",
                error={
                    "code": "STALE_SOURCE",
                    "message": "workspace no longer matches the clean validated source",
                    "retryable": False,
                },
                result={"workspace": workspace.to_dict()},
                next_action="Commit and validate the current Warden source before approving it.",
            )
            return _operation_result(operation)
        current = ledger.show(work.id)
        assert current is not None and current.fc
        fc = dict(current.fc)
        retained_delivery = dict(fc.get("delivery") or {})
        retained_delivery["approved_source"] = {
            "oid": source.oid,
            "provider_handle": handle,
            "summary": summary.strip(),
            "acceptance": list(fc.get("acceptance") or []),
            "operation_id": operation.id,
            "approved_at": utc_now(),
        }
        fc["delivery"] = retained_delivery
        fc["phase"] = "delivering"
        fc["next_action"] = (
            "Promote the exact approved source through the delivery provider."
        )
        fc["last_transition"] = operation.id
        ledger.update_fc(work.id, fc)
        operation = ledger.update_operation(
            operation.id,
            state="completed",
            step="review_source_approved",
            external={
                "provider": "tollgate",
                "repository_id": source.work.repository_id,
                "handle": handle,
            },
            result={
                "bead_id": work.id,
                "source_oid": source.oid,
                "provider_handle": handle,
                "approved_source": retained_delivery["approved_source"],
                "workspace": workspace.to_dict(),
            },
            next_action=fc["next_action"],
        )
        return _operation_result(operation)

    @coordinated
    def promotion_start(self, request: ParsedRequest) -> CommandResult:
        ledger, work, project, provider = _context(request)
        _authorize_warden(ledger, request, work)
        require_run_control(ledger, "promotion")
        source, handle = _retained_delivery_source(request, work, project)
        requested_source = str(request.arguments["source"])
        if requested_source != source.oid:
            raise FulcrumError(
                "STALE_SOURCE",
                "promotion source does not match the retained validation submission",
                exit_code=5,
                details={"requested": requested_source, "validated": source.oid},
            )
        _require_approved_source(work, source.oid)
        operation, reused = ledger.create_operation(
            request,
            bead_id=work.id,
            planned={"source": source.to_dict(), "handle": handle},
            next_action="Authorize the exact provider handle without blocking on CI.",
        )
        if reused and operation.operation.get("state") in TERMINAL_STATES:
            return _operation_result(operation)
        try:
            with external_effect():
                workspace = _call(provider.inspect_workspace(source.work))
        except DeliveryProviderError as error:
            return _failed_operation(ledger, operation, error, "promotion_source")
        if (
            not workspace.exists
            or not workspace.owned
            or workspace.dirty
            or workspace.head_oid != source.oid
        ):
            operation = ledger.update_operation(
                operation.id,
                state="failed",
                step="promotion_source_changed",
                error={
                    "code": "STALE_SOURCE",
                    "message": "workspace no longer matches the exact approved source",
                    "retryable": False,
                },
                result={"workspace": workspace.to_dict()},
                next_action="Validate and approve the current Warden source before promotion.",
            )
            return _operation_result(operation)
        try:
            with external_effect():
                facts = _call(provider.promote(source, handle))
        except DeliveryProviderError as error:
            return _failed_operation(ledger, operation, error, "promotion_request")

        _retain_delivery(ledger, work, normalized_delivery(facts), operation.id)
        operation = ledger.update_operation(
            operation.id,
            state="completed",
            step="promotion_request_observed",
            external={
                "provider": "tollgate",
                "repository_id": source.work.repository_id,
                "handle": handle,
            },
            result={
                "delivery": normalized_delivery(facts),
                "provider_delivery": facts.to_dict(),
            },
            next_action=(
                "Synchronize the observed integration commit."
                if facts.promotion == "promoted"
                else "Inspect the nonblocking provider promotion."
            ),
        )
        return _operation_result(operation)

    @coordinated
    def promotion_show(self, request: ParsedRequest) -> CommandResult:
        _, work, project, provider = _context(request)
        source, handle = _retained_delivery_source(request, work, project)
        try:
            with external_effect():
                facts = _call(provider.inspect(source, handle))
        except DeliveryProviderError as error:
            raise _public_error(error, request) from error
        return CommandResult.query(
            {
                "bead_id": work.id,
                "delivery": normalized_delivery(facts),
                "provider_delivery": facts.to_dict(),
            }
        )

    @coordinated
    def source_sync(self, request: ParsedRequest) -> CommandResult:
        ledger, work, project, provider = _context(request)
        _authorize(ledger, request, work)
        require_run_control(ledger, "source synchronization")
        source, handle = _retained_delivery_source(request, work, project)
        operation, reused = ledger.create_operation(
            request,
            bead_id=work.id,
            planned={"source": source.to_dict(), "handle": handle},
            next_action="Inspect provider publication, then synchronize the recorded integration commit if required.",
        )
        if reused and operation.operation.get("state") in TERMINAL_STATES:
            return _operation_result(operation)
        try:
            with external_effect():
                facts = _call(provider.synchronize(source, handle))
        except DeliveryProviderError as error:
            return _failed_operation(ledger, operation, error, "source_sync")
        if facts.synchronization == "complete":
            from fulcrum.broker import broker_request

            try:
                with external_effect():
                    asyncio.run(
                        broker_request(
                            request.instance.instance_root / "broker.sock",
                            {"type": "signal"},
                        )
                    )
            except Exception:
                pass  # Remote polling also observes publication; delivery remains valid.
        _retain_delivery(ledger, work, normalized_delivery(facts), operation.id)
        operation = ledger.update_operation(
            operation.id,
            state="completed",
            step="source_synchronization_observed",
            external={
                "provider": "tollgate",
                "repository_id": source.work.repository_id,
                "handle": handle,
                "integration_oid": facts.integration_oid,
            },
            result={
                "delivery": normalized_delivery(facts),
                "provider_delivery": facts.to_dict(),
            },
            next_action=(
                "Clean the settled managed workspace."
                if facts.synchronization in {"complete", "not_required"}
                else "Resolve the retained source synchronization obligation."
            ),
        )
        return _operation_result(operation)


def _context(
    request: ParsedRequest,
) -> tuple[Ledger, LedgerRecord, Mapping[str, Any], Delivery]:
    manager = ConfigurationManager(request.instance.config_path)
    document, _ = manager.load()
    config = manager.effective(document)
    if request.instance.brain_root is None:
        raise FulcrumError(
            "LEDGER_UNAVAILABLE", "delivery requires a configured brain", exit_code=4
        )
    beads = config["beads"]
    ledger = Ledger(
        request.instance.brain_root,
        executable=(str(beads["executable"]) if beads.get("executable") else None),
        timeout=request.timeout,
    )
    bead_id = str(request.arguments["bead"])
    work = ledger.show(bead_id)
    if work is None or work.kind != "work" or not work.fc:
        raise FulcrumError.invalid("NOT_FOUND", f"unknown work {bead_id}")
    project_id = str(work.fc.get("project") or request.project or "")
    project = config["projects"].get(project_id)
    if not isinstance(project, Mapping):
        raise FulcrumError.invalid(
            "PROJECT_NOT_FOUND", f"work {bead_id} has no enrolled project"
        )
    delivery_config = config["delivery"]
    try:
        tollgate = Tollgate(
            delivery_config.get("executable"), timeout=max(1, int(request.timeout))
        )
    except TollgateError as error:
        raise FulcrumError(
            "DELIVERY_UNAVAILABLE", str(error), exit_code=4, retryable=False
        ) from error
    return ledger, work, project, TollgateDelivery(tollgate)


def _work_ref(
    request: ParsedRequest,
    work: LedgerRecord,
    project: Mapping[str, Any],
    operation_id: str,
) -> WorkRef:
    fc = work.fc or {}
    project_id = str(fc.get("project") or request.project or "")
    provider = project.get("delivery")
    repository_id = provider.get("id") if isinstance(provider, Mapping) else None
    if not isinstance(repository_id, str) or not repository_id:
        raise FulcrumError(
            "CAPABILITY_UNAVAILABLE",
            f"project {project_id} has no Tollgate repository ID",
            exit_code=4,
        )
    integration_branch = project.get("integration_branch")
    if not isinstance(integration_branch, str) or not integration_branch:
        raise FulcrumError.invalid(
            "PROJECT_INVALID",
            f"project {project_id} has no integration_branch",
        )
    retained = fc.get("worktree")
    retained = retained if isinstance(retained, Mapping) else {}
    branch = str(retained.get("branch") or f"codex/{work.id}")
    intended_path = str(
        retained.get("intended_path")
        or (
            request.instance.instance_root / "worktrees" / project_id / work.id
        ).resolve(strict=False)
    )
    return WorkRef(
        bead_id=work.id,
        project_id=project_id,
        project_root=str(Path(str(project["root"])).resolve(strict=True)),
        repository_id=repository_id,
        intended_path=intended_path,
        branch=branch,
        integration_branch=integration_branch,
        operation_id=str(retained.get("operation_id") or operation_id),
        prepare_argv=tuple(str(item) for item in project.get("prepare_argv", [])),
        validate_argv=tuple(str(item) for item in project.get("validate_argv", [])),
        source_remote=(
            str(project["source_remote"]) if project.get("source_remote") else None
        ),
        require_source_sync=bool(project.get("require_source_sync", False)),
        actual_path=(
            str(retained["path"]) if isinstance(retained.get("path"), str) else None
        ),
        base_oid=(
            str(retained["base_oid"])
            if isinstance(retained.get("base_oid"), str)
            else None
        ),
    )


def _retained_work_ref(operation: OperationRecord, operation_id: str) -> WorkRef:
    planned = operation.operation.get("planned")
    value = planned.get("work") if isinstance(planned, Mapping) else None
    if not isinstance(value, Mapping):
        raise FulcrumError(
            "OPERATION_CORRUPT", "delivery receipt lost its work identity", exit_code=4
        )
    return _work_ref_from_mapping(value, operation_id)


def _work_ref_from_mapping(value: Mapping[str, Any], operation_id: str) -> WorkRef:
    return WorkRef(
        bead_id=str(value["bead_id"]),
        project_id=str(value["project_id"]),
        project_root=str(value["project_root"]),
        repository_id=str(value["repository_id"]),
        intended_path=str(value["intended_path"]),
        branch=str(value["branch"]),
        integration_branch=str(value["integration_branch"]),
        operation_id=(
            operation_id
            if value.get("operation_id") == "pending"
            else str(value["operation_id"])
        ),
        prepare_argv=tuple(str(item) for item in value.get("prepare_argv", [])),
        validate_argv=tuple(str(item) for item in value.get("validate_argv", [])),
        source_remote=(
            str(value["source_remote"]) if value.get("source_remote") else None
        ),
        require_source_sync=bool(value.get("require_source_sync")),
        actual_path=(str(value["actual_path"]) if value.get("actual_path") else None),
        base_oid=str(value["base_oid"]) if value.get("base_oid") else None,
    )


def _retained_source_ref(operation: OperationRecord) -> SourceRef:
    planned = operation.operation.get("planned")
    value = planned.get("source") if isinstance(planned, Mapping) else None
    if not isinstance(value, Mapping) or not isinstance(value.get("work"), Mapping):
        raise FulcrumError(
            "OPERATION_CORRUPT",
            "delivery receipt lost its source identity",
            exit_code=4,
        )
    return SourceRef(
        _work_ref_from_mapping(value["work"], operation.id), str(value["oid"])
    )


def _retained_delivery_source(
    request: ParsedRequest,
    work: LedgerRecord,
    project: Mapping[str, Any],
) -> tuple[SourceRef, str]:
    delivery = (work.fc or {}).get("delivery")
    if not isinstance(delivery, Mapping):
        raise FulcrumError.invalid(
            "DELIVERY_NOT_STARTED", "work has no retained validation submission"
        )
    source_oid = delivery.get("source_oid")
    handle = delivery.get("provider_handle")
    if not isinstance(source_oid, str) or not isinstance(handle, str):
        raise FulcrumError(
            "DELIVERY_UNCERTAIN",
            "retained delivery facts lack exact source or provider handle",
            exit_code=4,
        )
    return (
        SourceRef(
            _work_ref(request, work, project, _workspace_operation(work)), source_oid
        ),
        handle,
    )


def _retain_workspace(
    ledger: Ledger,
    work: LedgerRecord,
    reference: WorkRef,
    facts: WorkspaceFacts,
    operation_id: str,
) -> None:
    current = ledger.show(work.id)
    assert current is not None and current.fc
    fc = dict(current.fc)
    fc["worktree"] = {
        **facts.to_dict(),
        "operation_id": operation_id,
        "intended_path": reference.intended_path,
    }
    fc["last_transition"] = operation_id
    ledger.update_fc(work.id, fc)


def _retain_delivery(
    ledger: Ledger,
    work: LedgerRecord,
    facts: Mapping[str, Any],
    operation_id: str,
) -> None:
    current = ledger.show(work.id)
    assert current is not None and current.fc
    fc = dict(current.fc)
    previous = fc.get("delivery")
    delivery = dict(previous) if isinstance(previous, Mapping) else {}
    delivery.update(dict(facts))
    delivery["last_operation"] = operation_id
    delivery["observed_at"] = facts.get("observed_at", utc_now())
    fc["delivery"] = delivery
    fc["last_transition"] = operation_id
    ledger.update_fc(work.id, fc)


def _retain_conflict_base(
    ledger: Ledger,
    work: LedgerRecord,
    evidence: Mapping[str, Any],
    operation_id: str,
) -> None:
    conflict = evidence.get("merge_conflict")
    if not isinstance(conflict, Mapping):
        return
    integration_base = conflict.get("integration_base_oid")
    source_base = conflict.get("source_base_oid")
    if not isinstance(integration_base, str) or not isinstance(source_base, str):
        return
    current = ledger.show(work.id)
    assert current is not None and current.fc
    fc = dict(current.fc)
    retained = fc.get("worktree")
    if not isinstance(retained, Mapping) or retained.get("base_oid") != source_base:
        return
    workspace = dict(retained)
    history = list(workspace.get("base_history") or [])
    history.append(
        {
            "base_oid": source_base,
            "replaced_by": integration_base,
            "reason": "provider_merge_conflict",
            "operation_id": operation_id,
            "observed_at": utc_now(),
            "paths": list(conflict.get("paths") or []),
        }
    )
    workspace["base_history"] = history[-10:]
    workspace["base_oid"] = integration_base
    workspace["base_update"] = {
        "state": "repair_required",
        "reason": "provider_merge_conflict",
        "operation_id": operation_id,
        "observed_at": utc_now(),
        "paths": list(conflict.get("paths") or []),
    }
    fc["worktree"] = workspace
    ledger.update_fc(work.id, fc)


def _retain_local_check(
    ledger: Ledger, work: LedgerRecord, facts: Mapping[str, Any]
) -> None:
    current = ledger.show(work.id)
    assert current is not None and current.fc
    fc = dict(current.fc)
    prior = fc.get("local_check")
    if isinstance(prior, Mapping) and prior.get("source_oid") != facts.get(
        "source_oid"
    ):
        history = list(fc.get("local_check_history") or [])
        history.append(dict(prior))
        fc["local_check_history"] = history[-10:]
    fc["local_check"] = dict(facts)
    ledger.update_fc(work.id, fc)


def normalized_delivery(facts: DeliveryFacts) -> dict[str, Any]:
    promotion_state = {
        "promoted": "observed",
        "failed": "failed",
        "pending": "pending",
        "not_started": "not_started",
        "unknown": "unknown",
    }.get(facts.promotion, "unknown")
    synchronization_state = {
        "complete": "observed",
        "not_required": "not_required",
        "failed": "failed",
        "pending": "pending",
    }.get(facts.synchronization, "unknown")
    cleanup_state = {
        "complete": "observed",
        "failed": "failed",
        "pending": "pending",
    }.get(facts.cleanup, "unknown")
    return {
        "source_oid": facts.source_oid,
        "provider_handle": facts.handle,
        "validation": {
            "state": facts.validation,
            "provider_state": facts.validation,
            "observed_at": facts.observed_at,
            "facts": facts.to_dict(),
        },
        "promotion": {
            "state": promotion_state,
            "provider_state": facts.promotion,
            "integration_oid": facts.integration_oid,
            "observed_at": facts.observed_at,
        },
        "synchronization": {
            "state": synchronization_state,
            "provider_state": facts.synchronization,
            "integration_oid": facts.integration_oid,
            "observed_at": facts.observed_at,
        },
        "cleanup": {
            "state": cleanup_state,
            "provider_state": facts.cleanup,
            "observed_at": facts.observed_at,
        },
        "evidence": facts.evidence,
        "gaps": list(facts.gaps),
    }


def _workspace_operation(work: LedgerRecord) -> str:
    workspace = (work.fc or {}).get("worktree")
    if isinstance(workspace, Mapping) and workspace.get("operation_id"):
        return str(workspace["operation_id"])
    return "unprepared"


def _require_cleanup_settled(work: LedgerRecord) -> None:
    delivery = (work.fc or {}).get("delivery")
    if not isinstance(delivery, Mapping):
        if work.status == "closed":
            return
        raise FulcrumError(
            "DELIVERY_NOT_SETTLED",
            "open work without a delivery result cannot discard its managed workspace",
            exit_code=5,
        )
    promotion = delivery.get("promotion")
    promotion = promotion if isinstance(promotion, Mapping) else {}
    synchronization = delivery.get("synchronization")
    synchronization = synchronization if isinstance(synchronization, Mapping) else {}
    integration_oid = promotion.get("integration_oid")
    if integration_oid and synchronization.get("state") not in {
        "observed",
        "not_required",
    }:
        raise FulcrumError(
            "DELIVERY_NOT_SETTLED",
            "workspace contains an unsynchronized promoted integration source",
            exit_code=5,
            details={
                "integration_oid": integration_oid,
                "synchronization": synchronization,
            },
        )
    if work.status != "closed" and promotion.get("state") != "observed":
        raise FulcrumError(
            "DELIVERY_NOT_SETTLED",
            "open work cleanup requires an observed promoted delivery",
            exit_code=5,
            details={"promotion": promotion},
        )


def _authorize(ledger: Ledger, request: ParsedRequest, work: LedgerRecord) -> None:
    if request.actor.kind in {"human", "system"}:
        return
    fc = work.fc or {}
    control = ledger.show("fc-system")
    standing = (
        _protocol(control.fc or {}).get("standing") if control is not None else None
    )
    marshal_binding = standing.get("marshal") if isinstance(standing, Mapping) else None
    marshal = (
        str(marshal_binding.get("task_id"))
        if isinstance(marshal_binding, Mapping) and marshal_binding.get("task_id")
        else None
    )
    if request.thread_id == marshal:
        return
    if (
        request.actor.kind == "task"
        and request.actor.task_id == fc.get("owner")
        and request.thread_id == fc.get("owner")
        and request.ownership_operation == fc.get("ownership_operation")
    ):
        return
    raise FulcrumError(
        "OWNERSHIP_CONFLICT",
        "delivery mutation requires the current owner, Marshal, system settlement, or human",
        exit_code=5,
    )


def _authorize_warden(
    ledger: Ledger, request: ParsedRequest, work: LedgerRecord
) -> None:
    _authorize(ledger, request, work)
    if request.actor.kind in {"human", "system"}:
        return
    role = (work.fc or {}).get("role")
    if role not in {"warden", "justiciar"}:
        raise FulcrumError(
            "ROLE_AUTHORITY_DENIED",
            "only the current Warden or Justiciar may approve or promote source",
            exit_code=5,
            details={"role": role},
        )


def _require_approved_source(work: LedgerRecord, source_oid: str) -> None:
    delivery = (work.fc or {}).get("delivery")
    approved = (
        delivery.get("approved_source") if isinstance(delivery, Mapping) else None
    )
    if not isinstance(approved, Mapping) or approved.get("oid") != source_oid:
        raise FulcrumError(
            "STALE_APPROVAL",
            "promotion requires explicit approval of the current validated source",
            exit_code=5,
            details={"source_oid": source_oid, "approved_source": approved},
        )
    validation = delivery.get("validation") if isinstance(delivery, Mapping) else None
    if not isinstance(validation, Mapping) or validation.get("state") != "passed":
        raise FulcrumError(
            "VALIDATION_NOT_PASSED",
            "promotion requires passed validation of the approved source",
            exit_code=5,
            details={"validation": validation},
        )


@unlocked
def _call(action: Coroutine[Any, Any, T]) -> T:
    try:
        return asyncio.run(action)
    except RuntimeError as error:
        if "cannot be called from a running event loop" not in str(error):
            raise
        raise FulcrumError(
            "INTERNAL_ASYNC_CONTEXT",
            "delivery command must run outside an asynchronous policy call",
            exit_code=4,
        ) from error


def _failed_operation(
    ledger: Ledger,
    operation: OperationRecord,
    error: DeliveryProviderError,
    step: str,
) -> CommandResult:
    state = (
        "uncertain"
        if error.possible_effect or error.category == "uncertain"
        else "failed"
    )
    operation = ledger.update_operation(
        operation.id,
        state=state,
        step=f"{step}_unresolved",
        error={
            "code": f"DELIVERY_{error.category.upper()}",
            "message": str(error),
            "retryable": error.category in {"transient", "unavailable"},
            "possible_effect": error.possible_effect,
            "evidence": error.evidence,
        },
        result={"evidence": error.evidence},
        next_action="Inspect the retained workspace/source/provider handle before any retry.",
    )
    return _operation_result(operation)


def _public_error(error: DeliveryProviderError, request: ParsedRequest) -> FulcrumError:
    uncertain = error.possible_effect or error.category == "uncertain"
    return FulcrumError(
        f"DELIVERY_{error.category.upper()}",
        str(error),
        exit_code=4 if error.category != "rejected" else 5,
        retryable=error.category in {"transient", "unavailable"},
        state=CommandState.UNCERTAIN if uncertain else CommandState.FAILED,
        request_id=request.request_id,
        details=error.evidence,
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
