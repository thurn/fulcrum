"""Independent plan reviews implemented as ordinary managed Codex tasks."""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fulcrum.configuration import ConfigurationManager
from fulcrum.contracts import CommandResult, CommandState, FulcrumError, ParsedRequest
from fulcrum.leadership import capacity_snapshot
from fulcrum.ledger import (
    Ledger,
    OperationRecord,
    operation_id,
    operation_view,
    random_record_id,
    utc_now,
)
from fulcrum.plans import validate_plan_draft
from fulcrum.runtime import TaskSpec
from fulcrum.runtime_service import (
    _create_and_configure,
    _project_config,
    _runtime_call,
    _select_model,
    _start_or_recover,
    routing_developer_instructions,
)


class ReviewService:
    """Start and finish independently attributed review tasks."""

    _reservation_lock = threading.RLock()

    def start(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        bead_id = str(request.arguments["bead"])
        perspective = str(request.arguments["perspective"])
        if perspective not in {"cold_reader", "requirements"}:
            raise FulcrumError.invalid(
                "INVALID_REVIEW",
                "review perspective must be cold_reader or requirements",
            )
        root = ledger.show(bead_id)
        if root is None or root.kind != "work" or not root.fc:
            raise FulcrumError.invalid("NOT_FOUND", f"unknown plan root {bead_id}")
        _authorize_author(request, root.fc)
        plan = root.fc.get("plan")
        draft = plan.get("draft") if isinstance(plan, Mapping) else None
        retained_draft = validate_plan_draft(draft)
        assert request.request_id is not None
        expected_operation = operation_id(request.request_id)
        reviews = plan.get("reviews") if isinstance(plan, Mapping) else None
        existing_review = (
            reviews.get(perspective) if isinstance(reviews, Mapping) else None
        )
        if (
            isinstance(existing_review, Mapping)
            and existing_review.get("state") == "running"
            and existing_review.get("reviewed_draft") == retained_draft
            and existing_review.get("review_operation") != expected_operation
        ):
            raise FulcrumError(
                "REVIEW_IN_PROGRESS",
                f"a {perspective} review of this exact draft is already running",
                exit_code=5,
                details={
                    "review_operation": existing_review.get("review_operation"),
                    "task_record_id": existing_review.get("task_record_id"),
                },
            )
        prompt = _review_prompt(root, perspective, retained_draft, expected_operation)
        config, project = _project_config(request, str(root.fc.get("project")))
        model, effort, model_origin = _select_model(
            request, config, project, root.fc, "weaver"
        )
        task_record_id = random_record_id()
        creation_cwd = str(
            (
                request.instance.instance_root
                / "threads"
                / f"review-{request.request_id}"
            ).resolve(strict=False)
        )
        planned = {
            "bead_id": bead_id,
            "perspective": perspective,
            "draft": retained_draft,
            "review_input": prompt,
            "task_record_id": task_record_id,
            "creation_cwd": creation_cwd,
            "project": root.fc.get("project"),
            "model": model,
            "effort": effort,
            "model_origin": model_origin,
            "reservation": None,
        }
        operation, reused = ledger.create_operation(
            request,
            bead_id=bead_id,
            owner=str(root.fc.get("owner") or "HUMAN"),
            planned=planned,
            next_action="Reserve ordinary capacity and create an independent review task.",
        )
        retained = operation.operation.get("planned")
        if not isinstance(retained, Mapping):
            raise FulcrumError(
                "OPERATION_CORRUPT", "review receipt has no retained plan", exit_code=4
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
            prompt = str(retained["review_input"])
            model = str(retained["model"])
            effort = str(retained["effort"])
            model_origin = str(retained["model_origin"])

        with self._reservation_lock:
            fresh = ledger.show(operation.id)
            if fresh is None or not fresh.fc:
                raise FulcrumError(
                    "OPERATION_CORRUPT", "review receipt disappeared", exit_code=4
                )
            operation = OperationRecord.from_record(fresh)
            retained = dict(operation.operation.get("planned") or {})
            reservation = retained.get("reservation")
            if not (
                isinstance(reservation, Mapping)
                and reservation.get("state") in {"in_flight", "unknown"}
            ):
                capacity = capacity_snapshot(ledger, config)
                project_id = str(root.fc.get("project") or "")
                project_capacity = capacity["projects"].get(project_id, {})
                if (
                    capacity["occupied"] >= capacity["global_limit"]
                    or int(project_capacity.get("occupied", 0))
                    >= int(
                        project_capacity.get("limit", capacity["default_project_limit"])
                    )
                    or project_id in capacity["paused_projects"]
                ):
                    operation = ledger.update_operation(
                        operation.id,
                        step="review_waiting_for_capacity",
                        result={
                            "started": False,
                            "queued": True,
                            "capacity": capacity,
                        },
                        next_action="Start this retained review when ordinary capacity becomes available.",
                    )
                    return _operation_result(operation)
                retained["reservation"] = {
                    "state": "in_flight",
                    "reserved_at": utc_now(),
                    "operation_id": operation.id,
                }
                operation = ledger.update_operation(
                    operation.id,
                    step="review_capacity_reserved",
                    planned=retained,
                    next_action="Create or recover the exact independent native task.",
                )

        Path(creation_cwd).mkdir(parents=True, exist_ok=True)
        project_root = str(Path(str(project["root"])).resolve(strict=True))
        spec = TaskSpec(
            creation_cwd=creation_cwd,
            cwd=project_root,
            project_id=(
                str(project["codex_project_id"])
                if project.get("codex_project_id")
                else None
            ),
            workspace_roots=(project_root,),
            title=f"Plan review ({perspective}): {root.title}",
            model=model,
            effort=effort,
            developer_instructions=routing_developer_instructions(request),
        )
        native = _runtime_call(
            request,
            lambda runtime: _create_and_configure(
                runtime, spec, operation.operation.get("external")
            ),
        )
        operation = ledger.update_operation(
            operation.id,
            step="review_task_configured",
            external={"adapter": "codex", "thread_id": native.id},
            result={"task": native.to_dict()},
            next_action="Record the review task before starting its retained input.",
        )
        task_fc = {
            "kind": "task",
            "owner": native.id,
            "thread_id": native.id,
            "role": "weaver",
            "purpose": "plan_review",
            "perspective": perspective,
            "project": root.fc.get("project"),
            "work_bead": None,
            "related_task": (
                root.fc.get("owner")
                if root.fc.get("owner") not in {None, "HUMAN"}
                else None
            ),
            "associated_beads": [bead_id],
            "review_operation": operation.id,
            "ownership_operation": None,
            "creation_operation": operation.id,
            "creation_cwd": creation_cwd,
            "model": model,
            "effort": effort,
            "model_origin": model_origin,
            "review_result_operation": None,
            "missing_finish_reminder": None,
            "recovery_requested_operation": None,
            "archive_state": "pending",
            "archive_due_at": None,
            "last_observed": native.to_dict(),
            "last_turn": None,
            "deleted_at": None,
            "replaced_by": None,
            "last_transition": operation.id,
        }
        existing_task = ledger.show(task_record_id)
        if existing_task is None:
            ledger.create_record(
                record_id=task_record_id,
                kind="task",
                title=f"Managed task: plan review {perspective}",
                description=f"Independent {perspective} review for {bead_id}.",
                owner=native.id,
                fc=task_fc,
                external_ref=f"fulcrum:thread:{native.id}",
            )
        elif (
            existing_task.kind != "task"
            or not existing_task.fc
            or existing_task.fc.get("creation_operation") != operation.id
        ):
            raise FulcrumError(
                "REQUEST_CONFLICT",
                "planned review task record is occupied",
                exit_code=5,
            )
        if bead_id not in ledger.dependencies(task_record_id):
            ledger.run(("dep", "add", task_record_id, bead_id), mutating=True)
        if bead_id not in ledger.dependencies(task_record_id):
            raise FulcrumError(
                "LEDGER_UNCERTAIN",
                "review task relationship could not be verified",
                exit_code=4,
                retryable=True,
                state=CommandState.UNCERTAIN,
                operation_id=operation.id,
            )
        turn = _runtime_call(
            request,
            lambda runtime: _start_or_recover(
                runtime, native.id, spec, operation.id, prompt
            ),
        )
        task = ledger.show(task_record_id)
        assert task is not None and task.fc
        task_fc = dict(task.fc)
        task_fc["last_turn"] = turn.to_dict()
        ledger.update_fc(task.id, task_fc)

        current_root = ledger.show(bead_id)
        if current_root is None or not current_root.fc:
            raise FulcrumError(
                "WORK_MISSING", "plan root disappeared after review start", exit_code=4
            )
        current_fc = dict(current_root.fc)
        current_plan = dict(current_fc.get("plan") or {})
        reviews = dict(current_plan.get("reviews") or {})
        reviews[perspective] = {
            "state": "running",
            "thread_id": native.id,
            "task_record_id": task_record_id,
            "review_operation": operation.id,
            "findings": None,
            "summary": None,
            "reviewed_draft": retained_draft,
        }
        current_plan["reviews"] = reviews
        current_fc["plan"] = current_plan
        current_fc["last_transition"] = operation.id
        ledger.update_fc(current_root.id, current_fc)
        retained = dict(operation.operation.get("planned") or retained)
        retained["reservation"] = {
            "state": "started",
            "operation_id": operation.id,
            "thread_id": native.id,
            "settled_at": utc_now(),
        }
        operation = ledger.update_operation(
            operation.id,
            state="completed",
            step="review_turn_started",
            planned=retained,
            external={"thread_id": native.id, "turn_id": turn.id},
            result={
                "review_operation": operation.id,
                "bead_id": bead_id,
                "perspective": perspective,
                "thread_id": native.id,
                "task_record_id": task_record_id,
                "input": prompt,
                "turn": turn.to_dict(),
            },
            next_action="Use task output/requests/wait, then finish from this exact review task.",
        )
        return _operation_result(operation)

    def finish(self, request: ParsedRequest) -> CommandResult:
        ledger = _ledger(request)
        task = _task(ledger, str(request.arguments["task"]))
        task_fc = task.fc or {}
        if task_fc.get("purpose") != "plan_review":
            raise FulcrumError.invalid(
                "INVALID_REVIEW_TASK", "task is not an independent plan review"
            )
        if (
            request.actor.kind != "task"
            or request.actor.task_id != task_fc.get("thread_id")
            or request.thread_id != task_fc.get("thread_id")
        ):
            raise FulcrumError(
                "OWNERSHIP_CONFLICT",
                "only the named independent review task may finish this review",
                exit_code=5,
            )
        review_operation_id = request.input.get("review_operation")
        if review_operation_id != task_fc.get("review_operation"):
            raise FulcrumError(
                "STALE_REVIEW",
                "review output does not reference this task's review operation",
                exit_code=5,
            )
        summary = request.input.get("summary")
        findings = _validate_findings(request.input.get("findings"))
        if not isinstance(summary, str) or not summary.strip():
            raise FulcrumError.invalid(
                "INVALID_REVIEW", "review summary must be nonempty text"
            )
        review_record = ledger.show(str(review_operation_id))
        if (
            review_record is None
            or review_record.kind != "operation"
            or not review_record.fc
        ):
            raise FulcrumError.invalid(
                "NOT_FOUND", f"unknown review operation {review_operation_id}"
            )
        review_operation = OperationRecord.from_record(review_record)
        planned = review_operation.operation.get("planned")
        if not isinstance(planned, Mapping):
            raise FulcrumError(
                "OPERATION_CORRUPT",
                "review operation lost its draft snapshot",
                exit_code=4,
            )
        bead_id = str(planned["bead_id"])
        root = ledger.show(bead_id)
        if root is None or not root.fc:
            raise FulcrumError(
                "WORK_MISSING", "reviewed plan root no longer exists", exit_code=4
            )
        receipt, reused = ledger.create_operation(
            request,
            bead_id=bead_id,
            owner=str(task_fc.get("thread_id")),
            planned={
                "review_operation": review_operation.id,
                "task_record_id": task.id,
                "thread_id": task_fc.get("thread_id"),
                "reviewed_draft": planned.get("draft"),
                "summary": summary,
                "findings": findings,
            },
            next_action="Compare the current draft before attaching these findings.",
        )
        if reused and receipt.operation.get("state") in {
            "completed",
            "failed",
            "uncertain",
            "cancelled",
        }:
            return _operation_result(receipt)
        current_plan = root.fc.get("plan")
        current_draft = (
            current_plan.get("draft") if isinstance(current_plan, Mapping) else None
        )
        if current_draft != planned.get("draft"):
            receipt = ledger.update_operation(
                receipt.id,
                state="failed",
                step="stale_review_retained",
                error={
                    "code": "STALE_REVIEW",
                    "message": "the reviewed draft changed before findings were submitted",
                    "retryable": False,
                },
                result={
                    "review_operation": review_operation.id,
                    "summary": summary,
                    "findings": findings,
                    "applied": False,
                    "current_draft": current_draft,
                },
                next_action="Start a new independent review for the current draft.",
            )
            raise FulcrumError(
                "STALE_REVIEW",
                "the reviewed draft changed before findings were submitted",
                exit_code=5,
                operation_id=receipt.id,
                request_id=request.request_id,
                details={"historical_findings_retained": True},
            )

        root_fc = dict(root.fc)
        root_plan = dict(current_plan or {})
        reviews = dict(root_plan.get("reviews") or {})
        perspective = str(planned["perspective"])
        reviews[perspective] = {
            "state": "completed",
            "thread_id": task_fc.get("thread_id"),
            "task_record_id": task.id,
            "review_operation": review_operation.id,
            "review_result_operation": receipt.id,
            "summary": summary,
            "findings": findings,
            "reviewed_draft": planned.get("draft"),
            "completed_at": utc_now(),
        }
        root_plan["reviews"] = reviews
        root_fc["plan"] = root_plan
        root_fc["last_transition"] = receipt.id
        ledger.update_fc(root.id, root_fc)
        updated_task_fc = dict(task_fc)
        updated_task_fc["review_result_operation"] = receipt.id
        updated_task_fc["last_transition"] = receipt.id
        ledger.update_fc(task.id, updated_task_fc)
        start_result = dict(review_operation.operation.get("result") or {})
        start_result["review"] = reviews[perspective]
        ledger.update_operation(
            review_operation.id,
            result=start_result,
            next_action="Review findings are attached to the unchanged draft.",
        )
        receipt = ledger.update_operation(
            receipt.id,
            state="completed",
            step="review_findings_attached",
            result={
                "review_operation": review_operation.id,
                "review_result_operation": receipt.id,
                "bead_id": root.id,
                "perspective": perspective,
                "summary": summary,
                "findings": findings,
                "plan_owner": root_fc.get("owner"),
            },
            next_action="Resolve or waive findings during explicit plan approval.",
        )
        return _operation_result(receipt)


def _review_prompt(
    root: Any,
    perspective: str,
    draft: Mapping[str, Any],
    review_operation: str,
) -> str:
    instructions = {
        "role": "weaver",
        "purpose": "plan_review",
        "perspective": perspective,
        "review_instructions": (
            "Review the candidate as a cold reader. Identify only concrete ambiguity, missing dependency, unsafe assumption, or unverifiable acceptance."
            if perspective == "cold_reader"
            else "Compare every original requirement and retained discussion fact with the candidate. Identify omissions, contradictions, or inadequate evidence."
        ),
        "candidate_draft": dict(draft),
        "finish": {
            "command": (
                "fulcrum plan review finish --task $CODEX_THREAD_ID --input - --json"
            ),
            "review_operation": review_operation,
            "payload": {
                "review_operation": review_operation,
                "findings": [],
                "summary": "replace with the independent review summary",
            },
            "findings_shape": {
                "problem": "text",
                "required_change": "text",
                "evidence": "text",
            },
        },
    }
    if perspective == "requirements":
        fc = root.fc or {}
        instructions["original_requirements"] = {
            "outcome": fc.get("outcome") or root.native.get("description"),
            "acceptance": fc.get("acceptance")
            or root.native.get("acceptance_criteria")
            or [],
            "intake": fc.get("intake"),
            "discussion": fc.get("context", []),
        }
    return json.dumps(instructions, separators=(",", ":"), ensure_ascii=False)


def _validate_findings(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise FulcrumError.invalid("INVALID_REVIEW", "review findings must be an array")
    results: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {
            "problem",
            "required_change",
            "evidence",
        }:
            raise FulcrumError.invalid(
                "INVALID_REVIEW", "each finding requires exactly three typed fields"
            )
        row = {key: item[key] for key in ("problem", "required_change", "evidence")}
        if not all(isinstance(item, str) and item.strip() for item in row.values()):
            raise FulcrumError.invalid(
                "INVALID_REVIEW", "finding fields must be nonempty text"
            )
        results.append({key: str(item) for key, item in row.items()})
    return results


def _authorize_author(request: ParsedRequest, fc: Mapping[str, Any]) -> None:
    if request.actor.kind == "human":
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
        "only the current plan author or human may start an independent review",
        exit_code=5,
    )


def _task(ledger: Ledger, identifier: str) -> Any:
    direct = ledger.show(identifier)
    if direct is not None and direct.kind == "task" and direct.fc:
        return direct
    matches = [
        record
        for record in ledger.list_records(kind="task", limit=0)
        if record.fc and record.fc.get("thread_id") == identifier
    ]
    if len(matches) != 1:
        raise FulcrumError.invalid("NOT_FOUND", f"unknown review task {identifier}")
    return matches[0]


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
