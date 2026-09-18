"""Role finish operations and the Executor-to-Warden handoff."""

from __future__ import annotations

from fulcrum.timing import timed

from fulcrum.coordination import coordinated, external_effect

import asyncio
import subprocess
import uuid
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from fulcrum.analytics import AnalyticsService
from fulcrum.configuration import ConfigurationManager
from fulcrum.contracts import CommandResult, CommandState, FulcrumError, ParsedRequest
from fulcrum.delivery_service import DeliveryService, _context
from fulcrum.ledger import (
    Ledger,
    LedgerRecord,
    OperationRecord,
    accepted_input,
    operation_id,
    operation_reply,
    utc_now,
)
from fulcrum.work import WorkService, _marshal_or_human

HANDOFF_NAMESPACE = uuid.UUID("5f41c1ab-35bf-4df6-a85b-a75cb82ab40e")
FINISH_CHILD_NAMESPACE = uuid.UUID("c5846683-3cc4-40a8-92af-cb14a44ca88b")
FINDING_CHILD_NAMESPACE = uuid.UUID("492f4f42-4d3d-4ec0-a243-2d19ae305fca")
TERMINAL_STATES = {"completed", "failed", "uncertain", "cancelled"}
CHECK_STATES = {"passed", "failed", "not_run"}


class CompletionService:
    @coordinated
    @timed("completion.finish")
    def finish(self, request: ParsedRequest) -> CommandResult:
        bead_id = request.input.get("bead") or request.arguments.get("bead")
        if not isinstance(bead_id, str) or not bead_id:
            raise FulcrumError.invalid("INVALID_INPUT", "finish requires a bead")
        request = replace(
            request, arguments={**dict(request.arguments), "bead": bead_id}
        )
        ledger = _ledger(request)
        replayed = _finish_replay(request, ledger)
        if replayed is not None:
            return replayed
        ledger, work = _owned_work(request, ledger)
        outcome = request.input.get("outcome") or request.arguments.get("outcome")
        role = str((work.fc or {}).get("role") or "")
        if role == "justiciar" and outcome == "blocked":
            result = self._justiciar_blocked(request, ledger, work)
        elif outcome == "blocked":
            result = self._blocked(request, ledger, work)
        elif role == "justiciar" and outcome == "repaired":
            result = self._justiciar_repaired(request, ledger, work)
        elif role == "weaver" and outcome == "answered":
            result = self._weaver_answered(request, ledger, work)
        elif role == "weaver" and outcome == "ready":
            result = self._weaver_ready(request, ledger, work)
        elif role == "weaver" and outcome == "planned":
            result = self._weaver_planned(request, ledger, work)
        elif role == "executor" and outcome == "ready_for_review":
            result = self._executor_finish(request, ledger, work)
        elif role == "warden" and outcome == "approved":
            result = self._warden_finish(request, ledger, work)
        elif role in {"sage", "mason"} and outcome == "findings":
            result = self._investigation_findings(request, ledger, work)
        elif role in {"vizier", "marshal"} and outcome == "completed":
            result = self._leadership_completed(request, ledger, work)
        else:
            raise FulcrumError(
                "OUTCOME_NOT_IMPLEMENTED",
                f"{role or 'unassigned'} cannot finish with outcome {outcome!r} yet",
                exit_code=4,
                details={"role": role, "outcome": outcome},
            )
        if result.request_id == request.request_id:
            _wake_broker(request)
        return result

    def _leadership_completed(
        self, request: ParsedRequest, ledger: Ledger, work: LedgerRecord
    ) -> CommandResult:
        summary = request.input.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise FulcrumError.invalid("INVALID_INPUT", "completed summary is required")
        operation, reused = ledger.create_operation(
            request,
            bead_id=work.id,
            planned={"summary": summary, "role": (work.fc or {}).get("role")},
            next_action="Close only the explicit leadership request; retain the standing task.",
        )
        if reused and operation.operation.get("state") in TERMINAL_STATES:
            return _operation_result(operation)
        work = _record_task_finish(ledger, work, operation.id)
        fc = dict(work.fc or {})
        fc["phase"] = "done"
        fc["disposition"] = {
            "outcome": "answered",
            "role_outcome": "completed",
            "summary": summary.strip(),
            "waived_requirements": [],
            "known_defects": [],
            "canonical_bead": None,
            "completed_at": utc_now(),
            "ownership_operation": request.ownership_operation,
        }
        fc["last_transition"] = operation.id
        fc["next_action"] = (
            "The standing leadership task remains available for an explicit request."
        )
        closed = ledger.update_fc(work.id, fc, status="closed")
        completion_cost = (
            AnalyticsService().finalize_root(ledger, closed, operation.id)
            if fc.get("workflow_root") == work.id
            else None
        )
        operation = ledger.update_operation(
            operation,
            state="completed",
            step="leadership_request_completed",
            result={
                "bead_id": work.id,
                "accepted": True,
                "role": (work.fc or {}).get("role"),
                "standing_task_retained": True,
                "completion_cost": completion_cost,
            },
            next_action=fc["next_action"],
        )
        return _operation_result(operation)

    def _investigation_findings(
        self, request: ParsedRequest, ledger: Ledger, work: LedgerRecord
    ) -> CommandResult:
        summary = request.input.get("summary")
        findings = request.input.get("findings")
        if not isinstance(summary, str) or not summary.strip():
            raise FulcrumError.invalid("INVALID_INPUT", "findings summary is required")
        if not isinstance(findings, list) or not findings:
            raise FulcrumError.invalid(
                "INVALID_INPUT", "findings must be a nonempty array of report objects"
            )
        normalized = [
            _normalize_finding(item, work.id, index)
            for index, item in enumerate(findings)
        ]
        operation, reused = ledger.create_operation(
            request,
            bead_id=work.id,
            planned={"summary": summary, "findings": normalized},
            next_action="File each independently attributable finding before settling the investigation.",
        )
        if reused and operation.operation.get("state") in TERMINAL_STATES:
            return _operation_result(operation)
        reports: list[dict[str, Any]] = []
        for index, finding in enumerate(normalized):
            report = WorkService().report(
                replace(
                    request,
                    command=("report",),
                    arguments={},
                    input=finding,
                    project=str((work.fc or {}).get("project") or request.project),
                    request_id=str(
                        uuid.uuid5(
                            FINDING_CHILD_NAMESPACE,
                            f"{operation.id}:{index}",
                        )
                    ),
                )
            )
            reports.append(_child_result(report))
        work = _record_task_finish(ledger, work, operation.id)
        fc = dict(work.fc or {})
        interrupted = fc.get("interrupted_work")
        investigation = {
            "role": fc.get("role"),
            "summary": summary.strip(),
            "findings": normalized,
            "reports": [
                (row.get("result") or {}).get("result", {}).get("bead_id")
                for row in reports
            ],
            "operation_id": operation.id,
            "completed_at": utc_now(),
        }
        history = list(fc.get("investigations") or [])
        history.append(investigation)
        fc["investigations"] = history
        if isinstance(interrupted, Mapping) and interrupted.get("status") == "closed":
            fc["phase"] = interrupted.get("phase") or "done"
            fc["disposition"] = interrupted.get("disposition")
            fc["next_action"] = (
                "The prior terminal disposition is restored; reports are held for Marshal grooming."
            )
            status = "closed"
        elif isinstance(interrupted, Mapping):
            owner = _marshal_or_human(ledger)
            fc["owner"] = owner
            fc["role"] = "marshal" if owner != "HUMAN" else None
            fc["ownership_operation"] = operation.id
            fc["phase"] = "backlog"
            fc["next_action"] = (
                "Marshal must decide any follow-up without losing the investigation evidence."
            )
            status = "open"
            fc["disposition"] = {
                "outcome": "findings",
                "summary": summary.strip(),
                "completed_at": utc_now(),
                "ownership_operation": request.ownership_operation,
            }
        else:
            fc["phase"] = "done"
            fc["next_action"] = (
                "The standalone investigation is complete; reports remain queued for Marshal grooming."
            )
            fc["disposition"] = {
                "outcome": "findings",
                "summary": summary.strip(),
                "completed_at": utc_now(),
                "ownership_operation": request.ownership_operation,
            }
            status = "closed"
        fc["last_transition"] = operation.id
        settled = ledger.update_fc(work.id, fc, status=status)
        completion_cost = (
            AnalyticsService().finalize_root(ledger, settled, operation.id)
            if status == "closed" and fc.get("workflow_root") == work.id
            else None
        )
        operation = ledger.update_operation(
            operation,
            state="completed",
            step="investigation_findings_retained",
            result={
                "bead_id": work.id,
                "accepted": True,
                "stable_identity": work.id,
                "reports": reports,
                "restored_interrupted_disposition": (
                    isinstance(interrupted, Mapping)
                    and interrupted.get("status") == "closed"
                ),
                "completion_cost": completion_cost,
            },
            next_action=fc["next_action"],
        )
        return _operation_result(operation)

    def _blocked(
        self, request: ParsedRequest, ledger: Ledger, work: LedgerRecord
    ) -> CommandResult:
        summary = request.input.get("summary")
        blocker = request.input.get("blocker")
        attempts = request.input.get("attempts")
        required = request.input.get("required_action")
        if not all(
            isinstance(item, str) and item.strip()
            for item in (summary, blocker, required)
        ):
            raise FulcrumError.invalid(
                "INVALID_INPUT",
                "blocked requires nonempty summary, blocker, and required_action",
            )
        if not isinstance(attempts, list) or not all(
            isinstance(item, str) and item.strip() for item in attempts
        ):
            raise FulcrumError.invalid(
                "INVALID_INPUT", "blocked attempts must be an array of strings"
            )
        operation, reused = ledger.create_operation(
            request,
            bead_id=work.id,
            planned={
                "summary": summary,
                "blocker": blocker,
                "attempts": list(attempts),
                "required_action": required,
            },
            next_action="Transfer accountability to Marshal with exact attempted evidence.",
        )
        if reused and operation.operation.get("state") in TERMINAL_STATES:
            return _operation_result(operation)
        work = _record_task_finish(ledger, work, operation.id)
        marshal = _marshal_or_human(ledger)
        fc = dict(work.fc or {})
        reasons = _waiting_reasons(fc.get("waiting"))
        reason_id = f"blocked:{operation.id}"
        if not any(item.get("id") == reason_id for item in reasons):
            reasons.append(
                {
                    "id": reason_id,
                    "kind": "blocked",
                    "reason": blocker,
                    "attempts": list(attempts),
                    "required_action": required,
                    "decision_operation": operation.id,
                    "recorded_at": utc_now(),
                }
            )
        fc["waiting"] = {"reasons": reasons}
        fc["owner"] = marshal
        fc["role"] = "marshal" if marshal != "HUMAN" else None
        fc["ownership_operation"] = operation.id
        fc["phase"] = "backlog" if marshal != "HUMAN" else "human"
        fc["last_transition"] = operation.id
        fc["next_action"] = str(required)
        ledger.update_fc(work.id, fc, assignee=marshal, status="blocked")
        operation = ledger.update_operation(
            operation,
            state="completed",
            step="blocker_transferred",
            result={
                "bead_id": work.id,
                "owner": marshal,
                "reason_id": reason_id,
                "blocked": True,
                "report_reminder": _report_reminder(work.id),
            },
            next_action=str(required),
        )
        return _operation_result(operation)

    def _justiciar_repaired(
        self, request: ParsedRequest, ledger: Ledger, work: LedgerRecord
    ) -> CommandResult:
        summary = request.input.get("summary")
        changes = request.input.get("changes")
        waived = request.input.get("waived_requirements")
        defects = request.input.get("known_defects")
        evidence = request.input.get("evidence")
        source_oid = request.input.get("source_oid")
        if not isinstance(summary, str) or not summary.strip():
            raise FulcrumError.invalid("INVALID_INPUT", "repaired summary is required")
        if not isinstance(changes, list) or not all(
            isinstance(item, (str, Mapping)) for item in changes
        ):
            raise FulcrumError.invalid(
                "INVALID_INPUT", "repaired changes must be an array"
            )
        for name, value in (
            ("waived_requirements", waived),
            ("known_defects", defects),
            ("evidence", evidence),
        ):
            if not isinstance(value, list) or not all(
                isinstance(item, str) and item.strip() for item in value
            ):
                raise FulcrumError.invalid(
                    "INVALID_INPUT", f"repaired {name} must be strings"
                )
        fence = (work.fc or {}).get("recovery_fence")
        if not isinstance(fence, Mapping) or fence.get("state") not in {
            "active",
            "repairing",
            "failed",
        }:
            raise FulcrumError(
                "RECOVERY_AUTHORITY_REQUIRED",
                "Justiciar repaired requires an active scoped takeover",
                exit_code=5,
            )
        source_evidence = None
        if source_oid is not None:
            if not isinstance(source_oid, str) or not source_oid:
                raise FulcrumError.invalid("INVALID_INPUT", "source_oid is invalid")
            work = _reload_work(ledger, work.id)
            current_fence = (work.fc or {}).get("recovery_fence")
            if (
                not isinstance(current_fence, Mapping)
                or current_fence.get("operation_id") != fence.get("operation_id")
                or current_fence.get("state") not in {"active", "repairing"}
            ):
                raise FulcrumError(
                    "RECOVERY_CONFLICT",
                    "recovery authority changed before delivery evidence was sealed",
                    exit_code=5,
                    retryable=True,
                )
            current_assignment = (
                ((work.fc or {}).get("desktop") or {}).get("assignment")
                if isinstance((work.fc or {}).get("desktop"), Mapping)
                else None
            )
            if (
                not isinstance(current_assignment, Mapping)
                or current_assignment.get("assignment_token")
                != request.ownership_operation
                or current_assignment.get("task_id")
                != (request.actor.task_id or request.thread_id)
            ):
                raise FulcrumError(
                    "AUTHORITY_MISMATCH",
                    "recovery assignment changed while source evidence was inspected",
                    exit_code=5,
                )
            fence = current_fence
            delivery = _settled_delivery_for_source(work, source_oid)
            if delivery is None:
                raise FulcrumError(
                    "DELIVERY_NOT_SETTLED",
                    "changed recovery source must complete exact-source validation, review, promotion, synchronization, and cleanup before repair can finish",
                    exit_code=5,
                    details={"bead_id": work.id, "source_oid": source_oid},
                )
            source_evidence = {"delivery": dict(delivery)}
        operation, reused = ledger.create_operation(
            request,
            bead_id=work.id,
            planned={
                "summary": summary,
                "changes": list(changes),
                "waived_requirements": list(waived),
                "known_defects": list(defects),
                "source_oid": source_oid,
                "evidence": list(evidence),
                "takeover_operation": fence.get("operation_id"),
            },
            next_action="Reconcile the reduced scope and actual evidence before closing work.",
        )
        if reused and operation.operation.get("state") in TERMINAL_STATES:
            return _operation_result(operation)
        work = _record_task_finish(ledger, work, operation.id)
        fc = dict(work.fc or {})
        fc["phase"] = "done"
        fc["disposition"] = {
            "outcome": "repaired",
            "summary": summary,
            "changes": list(changes),
            "waived_requirements": list(waived),
            "known_defects": list(defects),
            "source_oid": source_oid,
            "source_evidence": source_evidence,
            "evidence": list(evidence),
            "completed_at": utc_now(),
            "ownership_operation": request.ownership_operation,
            "repair_operation": operation.id,
        }
        recovery = dict(fc.get("recovery") or {})
        recovery["result"] = fc["disposition"]
        fc["recovery"] = recovery
        from fulcrum.desktop_protocol import (
            DesktopProtocolService,
            _protocol,
            _with_protocol,
        )

        protocol = _protocol(fc)
        incidents = dict(protocol.get("incidents") or {})
        incident_key = str(fence.get("incident_key") or "")
        incident = incidents.get(incident_key)
        if isinstance(incident, Mapping):
            incidents[incident_key] = {
                **dict(incident),
                "state": "resolved",
                "resolved_at": utc_now(),
                "repair_operation": operation.id,
            }
            protocol["incidents"] = incidents
        resume_action = None
        scope = str(fence.get("scope") or "").lower()
        if "steward" in scope:
            system = ledger.show("fc-system")
            standing = (
                (_protocol(system.fc or {}).get("standing") or {})
                if system is not None
                else {}
            )
            steward = standing.get("steward")
            if isinstance(steward, Mapping) and steward.get("state") == "stopped":
                resume_action = DesktopProtocolService(ledger)._append_action(
                    protocol,
                    record_id=work.id,
                    executor="justiciar",
                    tool="send_message_to_thread",
                    arguments={
                        "threadId": steward.get("task_id"),
                        "prompt": (
                            "Resume as the existing Steward. Re-read durable Fulcrum "
                            "state, reconcile retained actions and waits, then call "
                            "wait_for_instructions."
                        ),
                    },
                    purpose=f"resume_repaired_steward:{operation.id}",
                    expected_result={"thread_id": steward.get("task_id")},
                    reporting={"kind": "same_steward_resumption"},
                    assignment_token=request.ownership_operation,
                )
        fc = _with_protocol(fc, protocol)
        fc["last_transition"] = operation.id
        fc["next_action"] = (
            "Release the retained takeover after scoped facts are reconciled."
        )
        closed = ledger.update_fc(work.id, fc, status="closed")
        completion_cost = (
            AnalyticsService().finalize_root(ledger, closed, operation.id)
            if fc.get("workflow_root") == work.id
            else None
        )
        operation = ledger.update_operation(
            operation,
            state="completed",
            step="justiciar_repair_retained",
            result={
                "bead_id": work.id,
                "accepted": True,
                "disposition": fc["disposition"],
                "completion_cost": completion_cost,
                "takeover_still_active": True,
                "resume_action": resume_action,
                "report_reminder": _report_reminder(work.id),
            },
            next_action=fc["next_action"],
        )
        return _operation_result(operation)

    def _justiciar_blocked(
        self, request: ParsedRequest, ledger: Ledger, work: LedgerRecord
    ) -> CommandResult:
        summary = request.input.get("summary")
        blocker = request.input.get("blocker")
        attempts = request.input.get("attempts")
        required = request.input.get("required_action")
        if not all(
            isinstance(item, str) and item.strip()
            for item in (summary, blocker, required)
        ):
            raise FulcrumError.invalid(
                "INVALID_INPUT",
                "blocked requires nonempty summary, blocker, and required_action",
            )
        if not isinstance(attempts, list) or not all(
            isinstance(item, str) and item.strip() for item in attempts
        ):
            raise FulcrumError.invalid(
                "INVALID_INPUT", "blocked attempts must be an array of strings"
            )
        fence = (work.fc or {}).get("recovery_fence")
        if not isinstance(fence, Mapping) or fence.get("state") not in {
            "active",
            "repairing",
        }:
            raise FulcrumError(
                "RECOVERY_AUTHORITY_REQUIRED",
                "Justiciar blocked requires an active scoped takeover",
                exit_code=5,
            )
        operation, reused = ledger.create_operation(
            request,
            bead_id=work.id,
            planned={
                "summary": summary,
                "blocker": blocker,
                "attempts": list(attempts),
                "required_action": required,
                "takeover_operation": fence.get("operation_id"),
            },
            next_action="Retain the failed intervention and escalate to Vizier for human direction.",
        )
        if reused and operation.operation.get("state") in TERMINAL_STATES:
            return _operation_result(operation)
        work = _record_task_finish(ledger, work, operation.id)
        from fulcrum.desktop_protocol import (
            DesktopProtocolService,
            _opaque,
            _protocol,
            _with_protocol,
        )

        current = _reload_work(ledger, work.id)
        fc = dict(current.fc or {})
        protocol = _protocol(fc)
        incidents = dict(protocol.get("incidents") or {})
        incident_key = str(fence.get("incident_key") or "justiciar-intervention")
        retained = incidents.get(incident_key)
        incident = {
            **(dict(retained) if isinstance(retained, Mapping) else {}),
            "incident_id": (
                retained.get("incident_id")
                if isinstance(retained, Mapping)
                else _opaque("incident")
            ),
            "incident_key": incident_key,
            "state": "open",
            "scope": fence.get("scope"),
            "required_decision": str(required),
            "evidence": {
                "summary": summary,
                "blocker": blocker,
                "attempts": list(attempts),
                "operation_id": operation.id,
            },
            "justiciar_interventions": max(
                1, int((retained or {}).get("justiciar_interventions", 0))
            ),
            "repair_hold": True,
            "updated_at": utc_now(),
        }
        incidents[incident_key] = incident
        protocol["incidents"] = incidents
        decisions = dict(protocol.get("human_decisions") or {})
        decision_id = _opaque("human-decision")
        decision = {
            "decision_id": decision_id,
            "incident_id": incident["incident_id"],
            "state": "open",
            "question": str(required),
            "scope": fence.get("scope"),
            "evidence": incident["evidence"],
            "created_at": utc_now(),
        }
        decisions[decision_id] = decision
        protocol["human_decisions"] = decisions
        system = ledger.show("fc-system")
        standing = (
            (_protocol(system.fc or {}).get("standing") or {})
            if system is not None
            else {}
        )
        vizier = standing.get("vizier")
        notice = None
        if isinstance(vizier, Mapping) and vizier.get("state") == "registered":
            notice = DesktopProtocolService(ledger)._append_action(
                protocol,
                record_id=work.id,
                executor="justiciar",
                tool="send_message_to_thread",
                arguments={
                    "threadId": vizier.get("task_id"),
                    "prompt": (
                        f"Human decision {decision_id} is required for {work.id} "
                        f"after the scoped Justiciar intervention failed: {required}"
                    ),
                },
                purpose=f"human_decision:{decision_id}",
                expected_result={"thread_id": vizier.get("task_id")},
                reporting={"kind": "recorded_notification", "target": "vizier"},
                assignment_token=request.ownership_operation,
            )
            decision["notice"] = dict(notice)
            decisions[decision_id] = decision
            protocol["human_decisions"] = decisions
        failed_fence = {**dict(fence), "state": "failed", "failed_at": utc_now()}
        fc["recovery_fence"] = failed_fence
        fc["phase"] = "human"
        fc["owner"] = "HUMAN"
        fc["role"] = "justiciar"
        fc["next_action"] = str(required)
        fc["last_transition"] = operation.id
        ledger.update_fc(
            work.id, _with_protocol(fc, protocol), assignee="HUMAN", status="blocked"
        )
        operation = ledger.update_operation(
            operation.id,
            state="completed",
            step="justiciar_failure_escalated",
            result={
                "bead_id": work.id,
                "blocked": True,
                "incident": incident,
                "decision": decision,
                "notice": notice,
            },
            next_action=str(required),
        )
        return _operation_result(operation)

    def _weaver_answered(
        self, request: ParsedRequest, ledger: Ledger, work: LedgerRecord
    ) -> CommandResult:
        summary, evidence = _weaver_payload(request, evidence_required=True)
        operation, reused = ledger.create_operation(
            request,
            bead_id=work.id,
            planned={"summary": summary, "evidence": evidence},
            next_action="Retain the answer evidence and close only this question root.",
        )
        if reused and operation.operation.get("state") in TERMINAL_STATES:
            return _operation_result(operation)
        work = _record_task_finish(ledger, work, operation.id)
        fc = dict(work.fc or {})
        fc["phase"] = "done"
        fc["disposition"] = {
            "outcome": "answered",
            "summary": summary,
            "evidence": evidence,
            "completed_at": utc_now(),
            "ownership_operation": request.ownership_operation,
        }
        fc["last_transition"] = operation.id
        fc["next_action"] = (
            "No question work remains unless the root is explicitly reopened."
        )
        closed = ledger.update_fc(work.id, fc, status="closed")
        completion_cost = AnalyticsService().finalize_root(ledger, closed, operation.id)
        operation = ledger.update_operation(
            operation.id,
            state="completed",
            step="weaver_answer_retained",
            result={
                "bead_id": work.id,
                "accepted": True,
                "disposition": fc["disposition"],
                "report_reminder": _report_reminder(work.id),
                "completion_cost": completion_cost,
            },
            next_action=fc["next_action"],
        )
        return _operation_result(operation)

    def _weaver_ready(
        self, request: ParsedRequest, ledger: Ledger, work: LedgerRecord
    ) -> CommandResult:
        desktop = (work.fc or {}).get("desktop")
        assignment = desktop.get("assignment") if isinstance(desktop, Mapping) else None
        actions = desktop.get("actions") if isinstance(desktop, Mapping) else None
        unresolved_title = next(
            (
                action
                for action in (actions or {}).values()
                if isinstance(action, Mapping)
                and action.get("executor") == "weaver"
                and action.get("assignment_token")
                == (
                    assignment.get("assignment_token")
                    if isinstance(assignment, Mapping)
                    else None
                )
                and action.get("state") != "succeeded"
            ),
            None,
        )
        if isinstance(unresolved_title, Mapping):
            raise FulcrumError(
                "ACTION_RESULT_REQUIRED",
                "report the Weaver title action result before finishing intake",
                exit_code=5,
                details={
                    "record_id": work.id,
                    "action_id": unresolved_title.get("action_id"),
                    "state": unresolved_title.get("state"),
                },
            )
        summary, _ = _weaver_payload(request, evidence_required=False)
        acceptance = request.input.get("acceptance")
        implementation_notes = request.input.get("implementation_notes", [])
        if (
            not isinstance(acceptance, list)
            or not acceptance
            or not all(isinstance(item, str) and item.strip() for item in acceptance)
        ):
            raise FulcrumError.invalid(
                "INVALID_INPUT",
                "ready requires a nonempty acceptance array of observable checks",
            )
        if not isinstance(implementation_notes, list) or not all(
            isinstance(item, str) and item.strip() for item in implementation_notes
        ):
            raise FulcrumError.invalid(
                "INVALID_INPUT", "implementation_notes must be an array of strings"
            )
        owner = "STEWARD"
        operation, reused = ledger.create_operation(
            request,
            bead_id=work.id,
            planned={
                "summary": summary,
                "acceptance": acceptance,
                "implementation_notes": list(implementation_notes),
                "owner": owner,
            },
            next_action="Commit implementation-ready scope for automatic Steward selection.",
        )
        if reused and operation.operation.get("state") in TERMINAL_STATES:
            return _operation_result(operation)
        owner = str(operation.operation["planned"]["owner"])
        work = _record_task_finish(ledger, work, operation.id)
        fc = dict(work.fc or {})
        fc["summary"] = summary
        fc["acceptance"] = list(acceptance)
        fc["requested_role"] = "executor"
        fc["scope"] = {
            "summary": summary,
            "acceptance": list(acceptance),
            "evidence": list(request.input.get("evidence", [])),
            "implementation_notes": list(implementation_notes),
            "finish_operation": operation.id,
        }
        fc["owner"] = owner
        fc["role"] = None
        fc["phase"] = "ready"
        fc["dispatch"] = None
        fc["waiting"] = _without_waiting_kind(fc.get("waiting"), "authoring")
        fc["last_transition"] = operation.id
        fc["next_action"] = _scope_next_action(owner)
        transferred = ledger.update_fc(work.id, fc, assignee=owner, status="open")
        return _complete_scope_return(ledger, operation, transferred)

    def _weaver_planned(
        self, request: ParsedRequest, ledger: Ledger, work: LedgerRecord
    ) -> CommandResult:
        summary, _ = _weaver_payload(request, evidence_required=False)
        plan_id = request.input.get("plan_id")
        if plan_id != work.id:
            raise FulcrumError.invalid(
                "INVALID_PLAN",
                "finish planned must reference the Weaver-owned plan root",
            )
        plan = (work.fc or {}).get("plan")
        if (
            not isinstance(plan, Mapping)
            or plan.get("published_scope") is None
            or plan.get("activation") != "future"
        ):
            raise FulcrumError(
                "APPROVAL_CONFLICT",
                "finish planned requires a published future plan",
                exit_code=5,
            )
        operation, reused = ledger.create_operation(
            request,
            bead_id=work.id,
            planned={
                "summary": summary,
                "plan_id": plan_id,
                "published_approval_operation": plan.get(
                    "published_approval_operation"
                ),
            },
            next_action="End Weaver authoring while retaining the future root and deferral.",
        )
        if reused and operation.operation.get("state") in TERMINAL_STATES:
            return _operation_result(operation)
        work = _record_task_finish(ledger, work, operation.id)
        owner = _marshal_or_human(ledger)
        current = _reload_work(ledger, work.id)
        fc = dict(current.fc or {})
        current_plan = dict(fc.get("plan") or {})
        current_plan["authoring"] = {
            "state": "completed",
            "finish_operation": operation.id,
            "summary": summary,
            "completed_at": utc_now(),
        }
        current_plan["authoring_ready"] = True
        fc["plan"] = current_plan
        fc["summary"] = summary
        fc["owner"] = owner
        fc["role"] = "marshal" if owner != "HUMAN" else None
        fc["phase"] = "backlog"
        fc["dispatch"] = None
        fc["waiting"] = _without_waiting_kind(fc.get("waiting"), "authoring")
        fc["last_transition"] = operation.id
        fc["next_action"] = (
            "Future plan remains deferred until human/Vizier authorization and Marshal execution."
        )
        ledger.update_fc(work.id, fc, assignee=owner, status="open")
        _settle_plan_authoring_children(ledger, current_plan, operation.id)
        operation = ledger.update_operation(
            operation.id,
            state="completed",
            step="future_plan_authoring_completed",
            result={
                "bead_id": work.id,
                "plan_id": work.id,
                "accepted": True,
                "root_closed": False,
                "activation": "future",
                "owner": owner,
                "report_reminder": _report_reminder(work.id),
            },
            next_action=fc["next_action"],
        )
        return _operation_result(operation)

    def _executor_finish(
        self, request: ParsedRequest, ledger: Ledger, work: LedgerRecord
    ) -> CommandResult:
        payload = _finish_payload(request)
        sealed = (work.fc or {}).get("finish")
        if isinstance(sealed, Mapping):
            if sealed.get("request_id") == request.request_id:
                existing = ledger.show(str(sealed.get("operation_id")))
                if existing is not None and existing.kind == "operation":
                    return _operation_result(OperationRecord.from_record(existing))
            raise FulcrumError(
                "FINISH_SEALED",
                "Executor finish input is already sealed for this acquisition",
                exit_code=5,
                details={
                    "operation_id": sealed.get("operation_id"),
                    "source_oid": sealed.get("source_oid"),
                },
            )
        assert request.request_id is not None
        start_request_id = str(
            uuid.uuid5(HANDOFF_NAMESPACE, f"warden:{work.id}:{request.request_id}")
        )
        operation, reused = ledger.create_operation(
            request,
            bead_id=work.id,
            planned={
                "finish": payload,
                "from_thread": request.thread_id,
                "from_ownership_operation": request.ownership_operation,
                "warden_start_request_id": start_request_id,
            },
            next_action="Seal implementation evidence and wait for the Executor turn and tools to stop.",
        )
        if reused and operation.operation.get("state") in TERMINAL_STATES:
            return _operation_result(operation)
        workspace = _inspect_clean_source(request, payload["source_oid"])
        local_check_result = DeliveryService().validation_check(
            _child_request(
                request, operation.id, "local-check", ("validation", "check")
            )
        )
        children = {"local_check": _child_result(local_check_result)}
        work = _reload_work(ledger, work.id)
        fc = dict(work.fc or {})
        fc["finish"] = {
            **payload,
            "request_id": request.request_id,
            "operation_id": operation.id,
            "ownership_operation": request.ownership_operation,
            "sealed_at": utc_now(),
        }
        fc["phase"] = "ready"
        fc["requested_role"] = "warden"
        fc["source"] = payload["source_oid"]
        fc["handoff"] = {
            "from_thread": request.thread_id,
            "from_ownership_operation": request.ownership_operation,
            "to_thread": None,
            "to_role": "warden",
            "source_oid": payload["source_oid"],
            "evidence": {
                "checks": payload["checks"],
                "references": payload["evidence"],
                "workspace": workspace,
                "local_check": fc.get("local_check"),
            },
            "receipt": operation.id,
            "start_request_id": start_request_id,
            "state": "waiting_for_executor_terminal",
        }
        fc["next_action"] = (
            "End the Executor turn; Steward may start Warden after positive native "
            "turn completion and settled process ownership are observed."
        )
        fc["last_transition"] = operation.id
        updated = ledger.update_fc(work.id, fc, status="in_progress")
        _record_task_finish(ledger, work, operation.id)
        operation = ledger.update_operation(
            operation.id,
            state="completed",
            step="executor_finish_sealed",
            planned={
                "finish": payload,
                "from_thread": request.thread_id,
                "from_ownership_operation": request.ownership_operation,
                "warden_start_request_id": start_request_id,
                "children": children,
            },
            result={
                "bead_id": work.id,
                "accepted": True,
                "handoff": fc["handoff"],
                "children": children,
                "work": _work_summary(updated),
                "report_reminder": _report_reminder(work.id),
            },
            next_action=fc["next_action"],
        )
        return _operation_result(operation)

    def _warden_finish(
        self, request: ParsedRequest, ledger: Ledger, work: LedgerRecord
    ) -> CommandResult:
        payload = _finish_payload(request)
        sealed = (work.fc or {}).get("delivery_finish")
        workspace: Mapping[str, Any] | None = None
        if isinstance(sealed, Mapping):
            if (
                sealed.get("source_oid") == payload["source_oid"]
                and sealed.get("summary") == payload["summary"]
            ):
                existing = ledger.show(str(sealed.get("operation_id")))
                if existing is not None and existing.kind == "operation":
                    return _operation_result(OperationRecord.from_record(existing))
            if sealed.get("source_oid") != payload["source_oid"]:
                workspace = _inspect_clean_source(request, payload["source_oid"])
                work = _supersede_delivery_finish(
                    ledger,
                    work,
                    sealed,
                    new_source_oid=payload["source_oid"],
                    superseding_operation=operation_id(str(request.request_id)),
                )
            else:
                raise FulcrumError(
                    "FINISH_SEALED",
                    "Warden judgment is already sealed for this acquisition",
                    exit_code=5,
                    details={
                        "operation_id": sealed.get("operation_id"),
                        "source_oid": sealed.get("source_oid"),
                    },
                )
        operation, reused = ledger.create_operation(
            request,
            bead_id=work.id,
            planned={"finish": payload, "children": {}},
            next_action="Submit exact-source validation, then seal one Warden judgment for software-owned delivery.",
        )
        if reused and operation.operation.get("state") in TERMINAL_STATES:
            return _operation_result(operation)
        settled_delivery = _settled_delivery_for_source(work, payload["source_oid"])
        if settled_delivery is not None:
            children = {
                "reconciliation": {
                    "state": "completed",
                    "ok": True,
                    "result": {
                        "delivery": "already_settled",
                        "source_oid": payload["source_oid"],
                    },
                }
            }
            return _accept_warden_finish(
                ledger,
                work,
                operation,
                payload,
                children,
                {
                    "exists": False,
                    "state": "provider_cleaned",
                    "source_oid": payload["source_oid"],
                },
                settled_delivery["validation"],
            )
        if workspace is None:
            workspace = _inspect_clean_source(request, payload["source_oid"])
        topology = _single_task_commit_topology(workspace, payload["source_oid"])
        if topology is not None and not topology["accepted"]:
            fc = dict(work.fc or {})
            fc["phase"] = "reviewing"
            fc["next_action"] = (
                "Consolidate the reviewed source to exactly one task commit atop "
                "the retained worktree base, then finish with the new source OID."
            )
            fc["last_transition"] = operation.id
            ledger.update_fc(work.id, fc)
            operation = ledger.update_operation(
                operation.id,
                state="failed",
                step="warden_source_topology_correctable",
                result={
                    "bead_id": work.id,
                    "accepted": False,
                    "correctable": True,
                    "source_oid": payload["source_oid"],
                    "topology": topology,
                    "workspace": workspace,
                },
                error={
                    "code": "SOURCE_TOPOLOGY_INVALID",
                    "message": (
                        "Warden source must contain exactly one task commit atop "
                        "the retained worktree base"
                    ),
                    "retryable": False,
                    "evidence": topology,
                },
                next_action=fc["next_action"],
            )
            return _operation_result(operation)
        children: dict[str, Any] = {}
        delivery = (work.fc or {}).get("delivery")
        validation = (
            delivery.get("validation") if isinstance(delivery, Mapping) else None
        )
        validated_source = (
            delivery.get("source_oid") if isinstance(delivery, Mapping) else None
        )
        service = DeliveryService()
        if validated_source != payload["source_oid"] or not isinstance(
            validation, Mapping
        ):
            validation_result = service.validation_start(
                _child_request(
                    request, operation.id, "validation", ("validation", "start")
                )
            )
            children["validation"] = _child_result(validation_result)
            if validation_result.state is not CommandState.COMPLETED:
                return _finish_child_unresolved(
                    ledger,
                    work,
                    operation,
                    payload,
                    children,
                    workspace,
                    "validation",
                    validation_result,
                )
            work = _reload_work(ledger, work.id)
            delivery = (work.fc or {}).get("delivery")
            validation = (
                delivery.get("validation") if isinstance(delivery, Mapping) else None
            )
            if not isinstance(validation, Mapping):
                return _finish_child_unresolved(
                    ledger,
                    work,
                    operation,
                    payload,
                    children,
                    workspace,
                    "validation",
                    validation_result,
                )
        if isinstance(validation, Mapping) and validation.get("state") == "failed":
            fc = dict(work.fc or {})
            fc["phase"] = "reviewing"
            fc["next_action"] = (
                "Inspect failed validation, fix the same workspace, and submit a new source."
            )
            fc["last_transition"] = operation.id
            ledger.update_fc(work.id, fc)
            operation = ledger.update_operation(
                operation.id,
                state="failed",
                step="warden_validation_failed",
                result={
                    "bead_id": work.id,
                    "accepted": False,
                    "correctable": True,
                    "source_oid": payload["source_oid"],
                    "validation": validation,
                    "children": children,
                    "workspace": workspace,
                    "report_reminder": _report_reminder(work.id),
                },
                next_action=fc["next_action"],
            )
            return _operation_result(operation)
        if not isinstance(validation, Mapping) or validation.get("state") != "passed":
            operation = ledger.update_operation(
                operation.id,
                state="failed",
                step="warden_validation_not_terminal",
                result={
                    "bead_id": work.id,
                    "accepted": False,
                    "validation": validation,
                    "children": children,
                },
                error={
                    "code": "VALIDATION_NOT_PASSED",
                    "message": "Warden finish requires the retained exact-source CI result to pass",
                    "retryable": False,
                },
                next_action="Remain in the same assignment and wait for exact-source CI.",
            )
            return _operation_result(operation)

        work = _reload_work(ledger, work.id)
        delivery = (work.fc or {}).get("delivery")
        approved = (
            delivery.get("approved_source") if isinstance(delivery, Mapping) else None
        )
        if (
            not isinstance(approved, Mapping)
            or approved.get("oid") != payload["source_oid"]
        ):
            review = service.review_approve(
                _child_request(request, operation.id, "review", ("review", "approve"))
            )
            children["review"] = _child_result(review)
            if review.state is not CommandState.COMPLETED:
                return _finish_child_unresolved(
                    ledger,
                    work,
                    operation,
                    payload,
                    children,
                    workspace,
                    "review",
                    review,
                )

        promotion = service.promotion_start(
            _child_request(request, operation.id, "promotion", ("promotion", "start"))
        )
        children["promotion"] = _child_result(promotion)
        if promotion.state is not CommandState.COMPLETED:
            return _finish_child_unresolved(
                ledger,
                work,
                operation,
                payload,
                children,
                workspace,
                "promotion",
                promotion,
            )
        work = _reload_work(ledger, work.id)
        delivery = (work.fc or {}).get("delivery")
        promotion_state = (
            delivery.get("promotion") if isinstance(delivery, Mapping) else None
        )
        if (
            not isinstance(promotion_state, Mapping)
            or promotion_state.get("state") != "observed"
        ):
            operation = ledger.update_operation(
                operation.id,
                state="uncertain",
                step="promotion_not_observed",
                result={"children": children, "delivery": delivery},
                error={
                    "code": "PROMOTION_NOT_OBSERVED",
                    "message": "provider accepted promotion but did not return a terminal promoted result",
                    "retryable": True,
                },
                next_action="Inspect the retained provider handle before another effect.",
            )
            return _operation_result(operation)

        synchronization = service.source_sync(
            _child_request(request, operation.id, "source-sync", ("source", "sync"))
        )
        children["source_sync"] = _child_result(synchronization)
        if synchronization.state is not CommandState.COMPLETED:
            return _finish_child_unresolved(
                ledger,
                work,
                operation,
                payload,
                children,
                workspace,
                "source_sync",
                synchronization,
            )
        return _accept_warden_finish(
            ledger,
            work,
            operation,
            payload,
            children,
            workspace,
            validation,
        )


def _settled_delivery_for_source(
    work: LedgerRecord, source_oid: str
) -> Mapping[str, Any] | None:
    """Return exact terminal delivery evidence after provider-owned cleanup."""

    delivery = (work.fc or {}).get("delivery")
    if not isinstance(delivery, Mapping) or delivery.get("source_oid") != source_oid:
        return None
    validation = delivery.get("validation")
    approved = delivery.get("approved_source")
    promotion = delivery.get("promotion")
    synchronization = delivery.get("synchronization")
    cleanup = delivery.get("cleanup")
    if not (
        isinstance(validation, Mapping)
        and validation.get("state") == "passed"
        and isinstance(approved, Mapping)
        and approved.get("oid") == source_oid
        and isinstance(promotion, Mapping)
        and promotion.get("state") == "observed"
        and isinstance(promotion.get("integration_oid"), str)
        and isinstance(synchronization, Mapping)
        and synchronization.get("state") == "observed"
        and synchronization.get("integration_oid") == promotion.get("integration_oid")
        and isinstance(cleanup, Mapping)
        and cleanup.get("state") == "observed"
    ):
        return None
    return delivery


def _accept_warden_finish(
    ledger: Ledger,
    work: LedgerRecord,
    operation: OperationRecord,
    payload: Mapping[str, Any],
    children: Mapping[str, Any],
    workspace: Mapping[str, Any],
    validation: Mapping[str, Any],
) -> CommandResult:
    work = _reload_work(ledger, work.id)
    fc = dict(work.fc or {})
    fc["phase"] = "awaiting_native_completion"
    fc["delivery_finish"] = {
        "operation_id": operation.id,
        "source_oid": payload["source_oid"],
        "summary": payload["summary"],
        "checks": payload["checks"],
        "evidence": payload["evidence"],
        "state": "awaiting_native_completion",
        "accepted_at": utc_now(),
    }
    fc["next_action"] = (
        "End the Warden turn; transcript-confirmed native completion authorizes "
        "workspace cleanup and assignment release."
    )
    fc["last_transition"] = operation.id
    awaiting = ledger.update_fc(work.id, fc, status="in_progress")
    _record_task_finish(ledger, work, operation.id)
    operation = ledger.update_operation(
        operation.id,
        state="completed",
        step="warden_finish_accepted",
        planned={"finish": dict(payload), "children": dict(children)},
        result={
            "bead_id": work.id,
            "accepted": True,
            "source_oid": payload["source_oid"],
            "validation": dict(validation),
            "children": dict(children),
            "workspace": dict(workspace),
            "delivery": (awaiting.fc or {}).get("delivery"),
            "native_completion_required": True,
            "report_reminder": _report_reminder(work.id),
        },
        next_action=fc["next_action"],
    )
    return _operation_result(operation)


def _finish_child_unresolved(
    ledger: Ledger,
    work: LedgerRecord,
    operation: OperationRecord,
    payload: Mapping[str, Any],
    children: Mapping[str, Any],
    workspace: Mapping[str, Any],
    step: str,
    child: CommandResult,
) -> CommandResult:
    work = _reload_work(ledger, work.id)
    fc = dict(work.fc or {})
    fc["phase"] = "reviewing"
    fc["next_action"] = (
        f"Warden must inspect the failed {step} receipt and keep the current source "
        "recoverable before another delivery attempt."
    )
    fc["last_transition"] = operation.id
    ledger.update_fc(work.id, fc)
    operation = ledger.update_operation(
        operation.id,
        state=(
            "uncertain"
            if child.state
            in {CommandState.RUNNING, CommandState.UNCERTAIN, CommandState.DEGRADED}
            else "failed"
        ),
        step=f"warden_{step}_unresolved",
        result={
            "bead_id": work.id,
            "accepted": False,
            "correctable": True,
            "source_oid": payload["source_oid"],
            "children": dict(children),
            "workspace": dict(workspace),
            "unresolved_operation": child.operation_id,
            "report_reminder": _report_reminder(work.id),
        },
        next_action=fc["next_action"],
    )
    return _operation_result(operation)


def _single_task_commit_topology(
    workspace: Mapping[str, Any], source_oid: str
) -> dict[str, Any] | None:
    """Enforce the provider's one-task-commit shape before external submission."""

    path = workspace.get("path")
    base_oid = workspace.get("base_oid")
    if not isinstance(path, str) or not isinstance(base_oid, str):
        return None
    try:
        ancestor = subprocess.run(
            ["git", "-C", path, "merge-base", "--is-ancestor", base_oid, source_oid],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        count = subprocess.run(
            ["git", "-C", path, "rev-list", "--count", f"{base_oid}..{source_oid}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise FulcrumError(
            "SOURCE_TOPOLOGY_UNAVAILABLE",
            f"could not verify source commit topology: {error}",
            exit_code=4,
            retryable=True,
        ) from error
    try:
        commit_count = int(count.stdout.strip()) if count.returncode == 0 else None
    except ValueError:
        commit_count = None
    if ancestor.returncode not in {0, 1} or commit_count is None:
        raise FulcrumError(
            "SOURCE_TOPOLOGY_UNAVAILABLE",
            "Git could not verify the retained base-to-source topology",
            exit_code=4,
            details={"stderr": count.stderr.strip() or ancestor.stderr.strip()},
        )
    return {
        "accepted": ancestor.returncode == 0 and commit_count == 1,
        "base_oid": base_oid,
        "source_oid": source_oid,
        "commit_count": commit_count,
        "base_is_ancestor": ancestor.returncode == 0,
    }


def _supersede_delivery_finish(
    ledger: Ledger,
    work: LedgerRecord,
    sealed: Mapping[str, Any],
    *,
    new_source_oid: str,
    superseding_operation: str,
) -> LedgerRecord:
    fc = dict(work.fc or {})
    history = list(fc.get("failed_delivery_finishes") or [])
    history.append(
        {
            **dict(sealed),
            "state": "superseded",
            "reason": "workspace_source_changed",
            "superseded_by_source_oid": new_source_oid,
            "superseded_by_operation": superseding_operation,
            "superseded_at": utc_now(),
        }
    )
    fc["failed_delivery_finishes"] = history[-10:]
    fc.pop("delivery_finish", None)
    delivery = fc.get("delivery")
    if isinstance(delivery, Mapping):
        invalidated = dict(delivery)
        invalidated["approved_source"] = None
        invalidated["approval_invalidated_by"] = superseding_operation
        fc["delivery"] = invalidated
    fc["phase"] = "reviewing"
    fc["next_action"] = (
        "Validate and approve the new exact workspace source; the prior Warden "
        "judgment was superseded."
    )
    fc["last_transition"] = superseding_operation
    return ledger.update_fc(work.id, fc)


def _owned_work(request: ParsedRequest, ledger: Ledger) -> tuple[Ledger, LedgerRecord]:
    work = ledger.show(str(request.arguments["bead"]))
    if work is None or work.kind != "work" or not work.fc:
        raise FulcrumError.invalid(
            "NOT_FOUND", f"unknown work {request.arguments['bead']}"
        )
    protocol = (work.fc or {}).get("desktop")
    assignment = protocol.get("assignment") if isinstance(protocol, Mapping) else None
    if request.actor.kind != "human" and (
        not isinstance(assignment, Mapping)
        or assignment.get("state") != "active"
        or request.thread_id != assignment.get("task_id")
        or request.ownership_operation != assignment.get("assignment_token")
    ):
        raise FulcrumError(
            "OWNERSHIP_CONFLICT",
            "finish requires the current task and ownership operation",
            exit_code=5,
            details={
                "current_owner": (
                    assignment.get("task_id")
                    if isinstance(assignment, Mapping)
                    else None
                ),
                "current_ownership_operation": (
                    assignment.get("assignment_token")
                    if isinstance(assignment, Mapping)
                    else None
                ),
            },
        )
    if request.actor.kind != "human" and isinstance(assignment, Mapping):
        role = (work.fc or {}).get("role")
        if role != assignment.get("role"):
            raise FulcrumError(
                "ROLE_MISMATCH",
                "finish role differs from the active Desktop assignment",
                exit_code=5,
                details={"work_role": role, "assignment_role": assignment.get("role")},
            )
    return ledger, work


def _ledger(request: ParsedRequest) -> Ledger:
    if request.instance.brain_root is None:
        raise FulcrumError(
            "LEDGER_UNAVAILABLE", "finish requires a configured brain", exit_code=4
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


def _finish_replay(request: ParsedRequest, ledger: Ledger) -> CommandResult | None:
    if request.request_id is None:
        return None
    record = ledger.show(operation_id(request.request_id))
    if record is None:
        return None
    operation = OperationRecord.from_record(record)
    if operation.operation.get("command") != "finish" or operation.operation.get(
        "input"
    ) != accepted_input(request):
        raise FulcrumError(
            "REQUEST_CONFLICT",
            f"request ID {request.request_id} was already used with different input",
            exit_code=5,
            request_id=request.request_id,
            operation_id=operation.id,
        )
    if operation.operation.get("state") in TERMINAL_STATES:
        return _operation_result(operation)
    # Ownership may already have transferred before receipt finalization. The
    # retained scope proves this exact finish even if Marshal has since acted.
    work = ledger.show(str(operation.operation.get("bead_id")))
    scope = (work.fc or {}).get("scope") if work else None
    if (
        (request.input.get("outcome") or request.arguments.get("outcome")) == "ready"
        and isinstance(scope, Mapping)
        and scope.get("finish_operation") == operation.id
        and scope.get("summary") == request.input.get("summary", "").strip()
        and scope.get("acceptance") == request.input.get("acceptance")
    ):
        assert work is not None
        return _complete_scope_return(ledger, operation, work)
    if work is not None and work.fc:
        for field in ("finish", "delivery_finish"):
            sealed = work.fc.get(field)
            if (
                isinstance(sealed, Mapping)
                and sealed.get("operation_id")
                and sealed.get("operation_id") != operation.id
            ):
                operation = ledger.update_operation(
                    operation,
                    state="cancelled",
                    step="finish_superseded",
                    result={
                        "bead_id": work.id,
                        "accepted": False,
                        "superseded_by": sealed.get("operation_id"),
                        "source_oid": sealed.get("source_oid"),
                    },
                    next_action="No recovery is required; a later accepted finish advanced the work.",
                )
                return _operation_result(operation)
    return None


def _scope_next_action(owner: str) -> str:
    return "Steward may select this implementation-ready scope when capacity permits."


def _complete_scope_return(
    ledger: Ledger, operation: OperationRecord, work: LedgerRecord
) -> CommandResult:
    owner = str(operation.operation["planned"]["owner"])
    operation = ledger.update_operation(
        operation,
        state="completed",
        step="weaver_scope_returned",
        result={
            "bead_id": work.id,
            "accepted": True,
            "owner": owner,
            "phase": "ready",
            "scope_state": "implementation_ready",
            "attention": "steward_selection",
            "implementation_authorized": True,
            "next_actor": "steward",
        },
        next_action=_scope_next_action(owner),
    )
    return _operation_result(operation)


def _waiting_reasons(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Mapping):
        return []
    reasons = value.get("reasons")
    return (
        [dict(item) for item in reasons if isinstance(item, Mapping)]
        if isinstance(reasons, list)
        else []
    )


def _weaver_payload(
    request: ParsedRequest, *, evidence_required: bool
) -> tuple[str, list[str]]:
    summary = request.input.get("summary")
    evidence = request.input.get("evidence", [])
    if not isinstance(summary, str) or not summary.strip():
        raise FulcrumError.invalid("INVALID_INPUT", "Weaver finish requires summary")
    if (
        not isinstance(evidence, list)
        or not all(isinstance(item, str) and item.strip() for item in evidence)
        or (evidence_required and not evidence)
    ):
        raise FulcrumError.invalid(
            "INVALID_INPUT",
            (
                "Weaver answer evidence must be a nonempty array of references"
                if evidence_required
                else "Weaver finish evidence must be an array of references"
            ),
        )
    return summary.strip(), list(evidence)


def _settle_plan_authoring_children(
    ledger: Ledger, plan: Mapping[str, Any], operation_id: str
) -> None:
    mapping = plan.get("children_by_key")
    keys = plan.get("approved_keys")
    if not isinstance(mapping, Mapping) or not isinstance(keys, list):
        return
    for key in keys:
        identifier = mapping.get(str(key))
        child = ledger.show(str(identifier)) if identifier else None
        if child is None or child.status == "closed" or not child.fc:
            continue
        fc = dict(child.fc)
        link = dict(fc.get("plan") or {})
        link["authoring_ready"] = True
        link["authoring_finish_operation"] = operation_id
        fc["plan"] = link
        fc["waiting"] = _without_waiting_kind(fc.get("waiting"), "authoring")
        remaining_reasons = (
            fc["waiting"].get("reasons", [])
            if isinstance(fc.get("waiting"), Mapping)
            else []
        )
        if any(
            isinstance(reason, Mapping) and reason.get("kind") == "external"
            for reason in remaining_reasons
        ):
            fc["next_action"] = (
                "Wait for required remote publication, then explicit future-plan activation."
            )
        else:
            fc["next_action"] = (
                "Wait for explicit activation of the approved future plan."
            )
        fc["last_transition"] = operation_id
        ledger.update_fc(child.id, fc)


def _without_waiting_kind(value: Any, kind: str) -> dict[str, Any] | None:
    reasons = value.get("reasons", []) if isinstance(value, Mapping) else []
    retained = [
        dict(reason)
        for reason in reasons
        if isinstance(reason, Mapping) and reason.get("kind") != kind
    ]
    return {"reasons": retained} if retained else None


def _finish_payload(request: ParsedRequest) -> dict[str, Any]:
    summary = request.input.get("summary")
    source_oid = request.input.get("source_oid")
    checks = request.input.get("checks")
    evidence = request.input.get("evidence")
    if not isinstance(summary, str) or not summary.strip():
        raise FulcrumError.invalid("INVALID_INPUT", "finish requires a summary")
    if (
        not isinstance(source_oid, str)
        or len(source_oid) != 40
        or any(character not in "0123456789abcdef" for character in source_oid)
    ):
        raise FulcrumError.invalid(
            "INVALID_INPUT", "finish requires a full lowercase source commit OID"
        )
    if not isinstance(checks, list) or not all(_valid_check(item) for item in checks):
        raise FulcrumError.invalid(
            "INVALID_INPUT",
            "finish checks must be objects with name, status, and evidence",
        )
    if not isinstance(evidence, list) or not all(
        isinstance(item, str) and item.strip() for item in evidence
    ):
        raise FulcrumError.invalid(
            "INVALID_INPUT", "finish evidence must be an array of references"
        )
    return {
        "summary": summary.strip(),
        "source_oid": source_oid,
        "checks": [dict(item) for item in checks],
        "evidence": list(evidence),
    }


def _valid_check(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value) == {"name", "status", "evidence"}
        and isinstance(value.get("name"), str)
        and bool(str(value["name"]).strip())
        and value.get("status") in CHECK_STATES
        and isinstance(value.get("evidence"), str)
        and bool(str(value["evidence"]).strip())
    )


def _inspect_clean_source(request: ParsedRequest, source_oid: str) -> dict[str, Any]:
    ledger, _, project, provider = _context(request)
    work = _reload_work(ledger, str(request.arguments["bead"]))
    from fulcrum.delivery_service import _work_ref

    reference = _work_ref(
        request, work, project, str((work.fc or {}).get("ownership_operation"))
    )
    facts = asyncio.run(provider.inspect_workspace(reference))
    if (
        not facts.exists
        or not facts.owned
        or facts.dirty
        or facts.head_oid != source_oid
    ):
        raise FulcrumError(
            "SOURCE_NOT_READY",
            "finish source must be the exact clean commit in the owned workspace",
            exit_code=5,
            details={"source_oid": source_oid, "workspace": facts.to_dict()},
        )
    return facts.to_dict()


def _child_request(
    request: ParsedRequest,
    parent_operation: str,
    purpose: str,
    command: tuple[str, ...],
) -> ParsedRequest:
    source_oid = str(request.input["source_oid"])
    arguments: dict[str, Any] = {
        "bead": request.arguments["bead"],
        "source": source_oid,
    }
    if command == ("review", "approve"):
        arguments["summary"] = str(request.input["summary"])
    return replace(
        request,
        command=command,
        arguments=arguments,
        input={},
        request_id=str(
            uuid.uuid5(FINISH_CHILD_NAMESPACE, f"{parent_operation}:{purpose}")
        ),
    )


def _child_result(result: CommandResult) -> dict[str, Any]:
    return {
        "operation_id": result.operation_id,
        "state": result.state.value,
        "ok": result.ok,
        "result": result.result,
    }


def _normalize_finding(value: Any, discovered_from: str, index: int) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise FulcrumError.invalid(
            "INVALID_INPUT", f"findings[{index}] must be a report object"
        )
    title = value.get("title")
    problem = value.get("problem")
    evidence = value.get("observed_evidence") or value.get("evidence")
    required = value.get("required_change")
    acceptance = value.get("acceptance_checks")
    for field, item in (
        ("title", title),
        ("problem", problem),
        ("observed_evidence", evidence),
        ("required_change", required),
    ):
        if not isinstance(item, str) or not item.strip():
            raise FulcrumError.invalid(
                "INVALID_INPUT", f"findings[{index}].{field} is required"
            )
    if not isinstance(acceptance, list) or not all(
        isinstance(item, str) and item.strip() for item in acceptance
    ):
        raise FulcrumError.invalid(
            "INVALID_INPUT",
            f"findings[{index}].acceptance_checks must be an array of strings",
        )
    return {
        "title": title.strip(),
        "problem": problem.strip(),
        "observed_evidence": evidence.strip(),
        "required_change": required.strip(),
        "acceptance_checks": list(acceptance),
        "discovered_from": discovered_from,
    }


def _record_task_finish(
    ledger: Ledger, work: LedgerRecord, operation_id: str
) -> LedgerRecord:
    current = ledger.show(work.id)
    if current is None or not current.fc:
        raise FulcrumError.invalid("NOT_FOUND", f"unknown work {work.id}")
    fc = dict(current.fc)
    desktop = dict(fc.get("desktop") or {})
    assignment = desktop.get("assignment")
    if not isinstance(assignment, Mapping):
        raise FulcrumError(
            "TASK_CORRUPT",
            "finish could not identify one accountable Desktop assignment",
            exit_code=4,
        )
    desktop["assignment"] = {
        **dict(assignment),
        "finish_operation": operation_id,
        "finish_recorded_at": utc_now(),
    }
    fc["desktop"] = desktop
    return ledger.update_fc(current.id, fc)


def _release_desktop_assignment(
    ledger: Ledger,
    bead_id: str,
    finish_operation: str | None,
    native_turn_id: str | None = None,
) -> Mapping[str, Any] | None:
    current = ledger.show(bead_id)
    if current is None or not current.fc:
        return None
    fc = dict(current.fc)
    desktop = dict(fc.get("desktop") or {})
    assignment = desktop.pop("assignment", None)
    if not isinstance(assignment, Mapping):
        return None
    history = list(desktop.get("assignment_history") or [])
    history.append(
        {
            **dict(assignment),
            **({"turn_id": native_turn_id} if native_turn_id else {}),
            "state": "finished",
            "finish_operation": finish_operation,
            "released_at": utc_now(),
        }
    )
    desktop["assignment_history"] = history[-20:]
    fc["desktop"] = desktop
    if assignment.get("capacity_class") == "recovery":
        fence = fc.get("recovery_fence")
        recovery_history = list(fc.get("recovery_history") or [])
        accepted = current.status == "closed" and fc.get("phase") == "done"
        if isinstance(fence, Mapping) and accepted:
            fc.pop("recovery_fence", None)
            recovery_history.append(
                {
                    **dict(fence),
                    "state": "released",
                    "released_at": utc_now(),
                    "release_reason": "accepted outcome and native completion",
                }
            )
            fc["recovery_history"] = recovery_history[-20:]
        elif isinstance(fence, Mapping):
            fc["recovery_fence"] = {
                **dict(fence),
                "state": "failed",
                "assignment_released_at": utc_now(),
                "release_reason": "failed intervention retained for human direction",
            }
    retained_fence = fc.get("recovery_fence")
    owner = (
        "SYSTEM"
        if current.status == "closed"
        else (
            "HUMAN"
            if isinstance(retained_fence, Mapping)
            and retained_fence.get("state") == "failed"
            else "STEWARD"
        )
    )
    fc["owner"] = owner
    fc["role"] = None
    fc["ownership_operation"] = None
    ledger.update_fc(current.id, fc, assignee=owner)
    return assignment


def _release_recovery_slot(
    ledger: Ledger, bead_id: str, assignment: Mapping[str, Any]
) -> None:
    if assignment.get("capacity_class") != "recovery":
        return
    system = ledger.show("fc-system")
    if system is None or not system.fc:
        return
    system_fc = dict(system.fc)
    desktop = dict(system_fc.get("desktop") or {})
    slot = desktop.get("recovery_slot")
    if not isinstance(slot, Mapping):
        return
    if slot.get("bead") != bead_id or slot.get("recovery_id") != assignment.get(
        "recovery_id"
    ):
        return
    desktop["recovery_slot"] = {
        **dict(slot),
        "state": "released",
        "released_at": utc_now(),
        "release_reason": "accepted outcome and native completion",
    }
    system_fc["desktop"] = desktop
    ledger.update_fc(system.id, system_fc)


def settle_native_completion(
    request: ParsedRequest, ledger: Ledger, bead_id: str
) -> bool:
    """Settle a finished assignment only after its exact native turn is terminal."""

    current = ledger.show(bead_id)
    if current is None or not current.fc:
        return False
    fc = dict(current.fc)
    desktop = dict(fc.get("desktop") or {})
    assignment = desktop.get("assignment")
    if not isinstance(assignment, Mapping) or not assignment.get("finish_operation"):
        return False
    from fulcrum.desktop_protocol import _positive_native_completion

    if not _positive_native_completion(desktop, assignment):
        return False
    actions = desktop.get("actions")
    if isinstance(actions, Mapping) and any(
        isinstance(action, Mapping)
        and action.get("executor") == assignment.get("role")
        and action.get("assignment_token") == assignment.get("assignment_token")
        and action.get("state") in {"pending", "issuing", "uncertain"}
        for action in actions.values()
    ):
        return False
    finish_operation = str(assignment["finish_operation"])
    delivery_finish = fc.get("delivery_finish")
    if (
        fc.get("phase") == "awaiting_native_completion"
        and isinstance(delivery_finish, Mapping)
        and delivery_finish.get("state") == "awaiting_native_completion"
    ):
        delivery = fc.get("delivery")
        retained_cleanup = (
            delivery.get("cleanup") if isinstance(delivery, Mapping) else None
        )
        cleanup_completed = (
            isinstance(retained_cleanup, Mapping)
            and retained_cleanup.get("state") == "observed"
        )
        cleanup: CommandResult | None = None
        if not cleanup_completed:
            cleanup_request = replace(
                request,
                command=("worktree", "cleanup"),
                arguments={"bead": bead_id},
                input={},
                actor=replace(request.actor, kind="system", task_id=None),
                request_id=str(
                    uuid.uuid5(FINISH_CHILD_NAMESPACE, f"{finish_operation}:cleanup")
                ),
                thread_id=None,
                ownership_operation=None,
            )
            try:
                cleanup = DeliveryService().worktree_cleanup(cleanup_request)
            except FulcrumError as error:
                cleanup = CommandResult(
                    ok=False,
                    state=error.state,
                    result={"error": error.to_result().to_dict()["error"]},
                    request_id=cleanup_request.request_id,
                )
            cleanup_completed = cleanup.state is CommandState.COMPLETED
        current = _reload_work(ledger, bead_id)
        fc = dict(current.fc or {})
        if cleanup_completed:
            completed_at = utc_now()
            fc["phase"] = "done"
            fc["delivery_finish"] = {
                **dict(delivery_finish),
                "state": "completed",
                "completed_at": completed_at,
            }
            fc["disposition"] = {
                "outcome": "approved",
                "summary": delivery_finish.get("summary"),
                "source_oid": delivery_finish.get("source_oid"),
                "completed_at": completed_at,
            }
            fc["next_action"] = "No delivery or cleanup obligation remains."
            closed = ledger.update_fc(bead_id, fc, status="closed")
            if fc.get("workflow_root") == bead_id:
                AnalyticsService().finalize_root(ledger, closed, finish_operation)
        else:
            assert cleanup is not None
            fc["phase"] = "cleanup_pending"
            fc["cleanup_obligation"] = {
                "finish_operation": finish_operation,
                "result": cleanup.to_dict(),
                "recorded_at": utc_now(),
            }
            fc["next_action"] = (
                "Reconcile the retained cleanup operation; do not repeat an uncertain effect."
            )
            ledger.update_fc(bead_id, fc, status="in_progress")
    # Reconciliation often runs inside Steward's instruction-wait request. Its
    # turn_id belongs to Steward, not to the worker being released. The worker's
    # exact turn was already proven above and retained on the assignment.
    native_turn_id = assignment.get("turn_id")
    released = _release_desktop_assignment(
        ledger,
        bead_id,
        finish_operation,
        str(native_turn_id) if native_turn_id else None,
    )
    if released is not None:
        _release_recovery_slot(ledger, bead_id, released)
        _wake_broker(request)
    return True


def _wake_broker(request: ParsedRequest) -> None:
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
        # Broker delivery is only a hint; a missed signal must never roll back
        # an already-recorded finish transition.
        pass


def _reload_work(ledger: Ledger, bead_id: str) -> LedgerRecord:
    work = ledger.show(bead_id)
    if work is None or work.kind != "work" or not work.fc:
        raise FulcrumError.invalid("NOT_FOUND", f"unknown work {bead_id}")
    return work


def _work_summary(work: LedgerRecord) -> dict[str, Any]:
    fc = work.fc or {}
    return {
        "bead_id": work.id,
        "owner": fc.get("owner"),
        "role": fc.get("role"),
        "phase": fc.get("phase"),
        "ownership_operation": fc.get("ownership_operation"),
        "next_action": fc.get("next_action"),
    }


def _report_reminder(bead_id: str) -> dict[str, Any]:
    return {
        "required": False,
        "message": "Report incidental problems independently; a report failure does not revoke this accepted finish.",
        "next_command": ["fulcrum", "report", "--input", "-", "--json"],
        "discovered_from": bead_id,
    }


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
        result=operation_reply(operation),
    )
