"""Role instructions, short notices, and separately retrievable action context."""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

from fulcrum.outcomes import finish_contract, finish_syntax


class PromptError(RuntimeError):
    pass


TEMPLATES = {
    "implement": "executor.md",
    "correct": "executor.md",
    "review": "overseer.md",
    "archon": "archon.md",
    "weaver": "weaver.md",
    "interview": "interview.md",
}


def load_template(action_kind: str, *, role: str | None = None) -> str:
    name = (
        f"{role}.md"
        if action_kind == "specialist" and role in {"sage", "inquisitor"}
        else TEMPLATES.get(action_kind)
    )
    if name is None:
        raise PromptError(f"no prompt template for {action_kind}")
    try:
        return (
            files("fulcrum")
            .joinpath("prompts", name)
            .read_text(encoding="utf-8")
            .strip()
        )
    except (FileNotFoundError, OSError) as error:
        raise PromptError(f"missing prompt template {name}: {error}") from error


def role_instructions(action_kind: str, *, role: str | None = None) -> str:
    """Onboarding and command reference, supplied once at task creation."""
    context_hint = (
        "If exact current-action facts are missing after compaction, run "
        "`fulcrum context`; otherwise continue from retained conversation context."
    )
    if action_kind == "archon":
        return load_template(action_kind, role=role) + "\n\n" + context_hint
    finish_kind = "correct" if action_kind == "implement" else action_kind
    interviews_allowed = role == "sage"
    lifecycle = {
        "executor": (
            "Finish this implementation or correction once, then end. The controller "
            "routes the result. Correct and retry a failed finish command. A later "
            "`Workflow debrief only` action replaces implementation duties."
        ),
        "overseer": (
            "Finish this review once, then end; the controller routes it. Wait only for "
            "helpers you started. Correct and retry a failed finish command. A later "
            "`Workflow debrief only` action replaces review duties."
        ),
    }.get(
        role or "",
        "Finish the current action exactly once, then end. The controller binds and "
        "routes the result. Wait only for helper agents you started. If finish fails, "
        "correct the reported error and retry. A `Workflow debrief only` action replaces "
        "the role's normal duties.",
    )
    return "\n\n".join(
        [
            load_template(action_kind, role=role),
            lifecycle,
            context_hint,
            finish_syntax(finish_kind, interviews_allowed=interviews_allowed),
            finish_contract(finish_kind, interviews_allowed=interviews_allowed),
        ]
    )


def action_payload(action: dict[str, Any]) -> dict[str, Any]:
    payload = action.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise PromptError("action payload must be an object")
    return payload


def _text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _facts(values: dict[str, Any]) -> str:
    return "; ".join(
        f"{key.replace('_', ' ')}: {_text(value)}"
        for key, value in values.items()
        if value is not None and value != [] and value != {}
    )


def _archon_finish_guidance(
    payload: dict[str, Any], *, action_id: int | str | None
) -> list[str]:
    """Give Archon only the result shapes relevant to this frozen action."""
    items = payload.get("batch_items", [])
    update_ids = [int(item["update_id"]) for item in items]
    contents = [
        item["content"] for item in items if isinstance(item.get("content"), dict)
    ]
    kinds = {content.get("kind") for content in contents}
    acknowledge_only = bool(update_ids) and kinds <= {
        "assignment_completed",
        "specialist_completed",
        "archon_succession_completed",
    }
    rendered_id = action_id if action_id is not None else "current"
    lines = [
        f"Action {rendered_id}. Finish required: yes. Relevant outcome: "
        + ("decisions." if acknowledge_only else "decisions or deferred.")
    ]

    if payload.get("purpose") == "initial_policies":
        projects = [str(project) for project in payload.get("projects", [])]
        lines.extend(
            [
                "Required result: choose positive global and per-project capacities, one fleet Sage cadence, and one Inquisitor cadence per project.",
                "Use the normal 86400-second cadence unless the operator requested another cadence; offset project Inquisitor anchors twelve hours from the fleet Sage anchor. Replace the quoted integer descriptions below with your chosen numbers.",
                "Decisions JSON shape: "
                + json.dumps(
                    {
                        "decisions": [],
                        "handled_update_ids": [],
                        "global_limit": "positive integer",
                        "project_limits": {
                            project: "positive integer" for project in projects
                        },
                        "recurring_policies": [
                            {
                                "kind": "sage",
                                "scope": None,
                                "cadence_seconds": "positive integer",
                                "anchor_at": "ISO timestamp",
                            },
                            *[
                                {
                                    "kind": "inquisitor",
                                    "scope": project,
                                    "cadence_seconds": "positive integer",
                                    "anchor_at": "ISO timestamp",
                                }
                                for project in projects
                            ],
                        ],
                    },
                    ensure_ascii=False,
                ),
            ]
        )
    elif update_ids:
        lines.append("Required handled_update_ids: " + json.dumps(update_ids) + ".")
        if acknowledge_only:
            lines.append(
                "No scheduling change is required unless the update itself identifies one. Acknowledge with: "
                + json.dumps(
                    {"decisions": [], "handled_update_ids": update_ids},
                    ensure_ascii=False,
                )
            )
            if any(content.get("minor_fixes") for content in contents):
                lines.append(
                    "Do not schedule the nonblocking follow-up; it requires a new Weaver bead."
                )
        if "proposal" in kinds:
            proposals = [
                {
                    "decision": "approve",
                    "project": content["project"],
                    "beads": [content["bead_id"]],
                    "scope": {
                        content["bead_id"]: (
                            f"exact Scope text from update {item['update_id']}"
                        )
                    },
                }
                for item in items
                if isinstance((content := item.get("content")), dict)
                and content.get("kind") == "proposal"
            ]
            lines.append(
                "For approval, use this shape and replace each scope marker with the exact Scope text above: "
                + json.dumps(
                    {"decisions": proposals, "handled_update_ids": update_ids},
                    ensure_ascii=False,
                )
            )
        for content in contents:
            if content.get("kind") == "review_failure_escalation":
                lines.append(
                    f"Assignment {content['assignment_id']} requires resolve_escalation; resolution must be one of "
                    + json.dumps(content.get("resolutions", []))
                    + ". Use scope only with rescope; use evidence for complete_non_code."
                )
            elif content.get("kind") == "operation_resolution":
                lines.append(
                    f"Operation {content['operation_id']} requires resolve_operation with observed_success, observed_failure, or confirmed_unsent and concrete evidence; observed_success also requires the operation-specific result identity."
                )
            elif content.get("kind") == "assignment_recovery":
                lines.append(
                    f"Assignment {content['assignment_id']} already has an automatic retry scheduled. Acknowledge it unchanged unless the stated condition justifies a hold, run cancellation, or priority change."
                )

    snapshot = payload.get("fleet_snapshot") or {}
    if snapshot.get("holds"):
        lines.append(
            "Relevant hold actions: release_hold accepts the exact hold_id after its stated condition is satisfied; a new hold requires scope, target except for global, reason, and release_condition."
        )

    lines.append(
        'Finish decisions: write complete JSON to a sibling temporary file, atomically rename it to "/absolute/decisions.json", then run `fulcrum finish decisions --input "/absolute/decisions.json"`.'
    )
    if not acknowledge_only:
        lines.append(
            'Defer the whole batch only when it must wait: `fulcrum finish deferred --reason "why" --input "/absolute/reactivation.json"`. Reactivation JSON must contain exactly one concrete trigger: capacity, dependency, hold, operator_change, or next_check_at.'
        )
    return lines


def _archon_message(payload: dict[str, Any], *, action_id: int | str | None) -> str:
    """Render the pending decisions, not the entire fleet record or role manual."""
    items = payload.get("batch_items", [])
    lines: list[str] = []
    projects: set[str] = set()
    for item in items:
        content = item["content"]
        prefix = f"Update {item['update_id']}: "
        if not isinstance(content, dict):
            lines.append(prefix + _text(content))
            continue
        project = content.get("project") or content.get("project_id")
        if project:
            projects.add(str(project))
        kind = content.get("kind")
        if kind == "proposal":
            lines.append(
                prefix
                + f"Approve or defer {content['bead_id']} ({project}): {content['title']}.\nScope: {content['scope']}"
            )
            details = {key: content.get(key) for key in ("dependencies", "context")}
            plan = content.get("plan") or {}
            if plan.get("id"):
                details["plan"] = plan
            models = content.get("models") or {}
            if models.get("provenance") not in {None, "default"}:
                details["models"] = models
            if _facts(details):
                lines.append(_facts(details))
        elif kind == "assignment_completed":
            lines.append(
                prefix
                + f"{content['bead_id']} completed (assignment {content['assignment_id']}, run {content['run_id']})."
            )
            if content.get("minor_fixes"):
                lines.append(
                    "Overseer nonblocking follow-up (not approved work): "
                    + _text(content["minor_fixes"])
                )
        elif kind == "assignment_recovery":
            lines.append(
                prefix
                + f"Assignment {content['assignment_id']} ({content['bead_id']}, run {content['run_id']}) needs recovery: {content['condition']}. "
                + _facts(
                    {
                        "attempt": content.get("attempt"),
                        "next_attempt_at": content.get("next_attempt_at"),
                    }
                )
            )
        elif kind == "review_failure_escalation":
            lines.append(
                prefix
                + f"Assignment {content['assignment_id']} reached {content['review_failures']} substantive review failures: {content['condition']}. "
                + _facts(
                    {
                        "review_action_id": content.get("action_id"),
                        "review_action": content.get("review_action"),
                        "hold_id": content.get("hold_id"),
                        "required_decision": content.get("required_decision"),
                        "resolutions": content.get("resolutions"),
                    }
                )
            )
        elif kind == "specialist_completed":
            lines.append(
                prefix
                + f"{content['specialist']} report {content['occurrence_id']}: {content['summary']} ({content['finding_count']} findings). "
                + _facts({"scope": content.get("scope")})
            )
        else:
            # Unknown exceptions retain their actual decision data instead of
            # silently becoming an uninformative notification count.
            lines.append(prefix + _facts(content))
    snapshot = payload.get("fleet_snapshot") or {}
    capacity = snapshot.get("capacity") or {}
    if capacity:
        limits = capacity.get("project_limits") or {}
        usage = capacity.get("project_usage") or {}
        if not items:
            projects.update(limits)
        line = f"Capacity used: global {capacity.get('global_usage', 0)}/{capacity.get('global_limit', 'unset')}"
        for project in sorted(projects):
            line += (
                f"; {project} {usage.get(project, 0)}/{limits.get(project, 'unset')}"
            )
        lines.append(line + ".")
    # Active work is pertinent when approving overlapping work. Completion-only
    # notifications need neither the whole project backlog nor unchanged policies.
    scheduling = not items or any(
        isinstance(item["content"], dict)
        and item["content"].get("kind")
        in {
            "proposal",
            "assignment_recovery",
            "review_failure_escalation",
            "archon_succession_completed",
        }
        for item in items
    )
    if scheduling:
        active = snapshot.get("unfinished_assignments", [])
        for row in active:
            if projects and row.get("project_id") not in projects:
                continue
            lines.append(
                f"Existing {row['bead_id']} ({row.get('project_id')}, run {row['run_id']}, assignment {row['id']}): {row['stage']}"
                + (f" — {row['condition']}" if row.get("condition") else "")
                + f"; priority {row.get('priority', 0)}."
            )
        for hold in snapshot.get("holds", []):
            # Run and assignment holds are retained: their scope may be relevant
            # even when the update does not include a project binding.
            if (
                hold.get("scope") == "project"
                and projects
                and str(hold.get("target")) not in projects
            ):
                continue
            lines.append(
                f"Hold {hold['id']}: "
                + _facts(
                    {
                        key: hold.get(key)
                        for key in (
                            "scope",
                            "target",
                            "reason",
                            "urgent",
                            "release_condition",
                        )
                    }
                )
            )
        for operation in snapshot.get("uncertain_operations", []):
            lines.append(
                f"External operation {operation['id']} requires explicit resolution: "
                + _facts(
                    {
                        key: operation.get(key)
                        for key in (
                            "kind",
                            "target",
                            "condition",
                            "operator_hold_id",
                        )
                    }
                )
            )
    if not items:
        lines.insert(
            0,
            str(
                payload.get("required")
                or "Set the initial fleet capacity and recurring policies."
            ),
        )
        if payload.get("projects"):
            lines.append("Projects: " + ", ".join(payload["projects"]) + ".")
        for policy in snapshot.get("policies", []):
            lines.append("Policy: " + _facts(policy))
    guidance = _archon_finish_guidance(payload, action_id=action_id)
    return "\n".join([guidance[0], *lines, *guidance[1:]])


def _handoff_text(content: Any) -> str:
    if isinstance(content, dict) and content.get("evidence_content"):
        # Show retained authored evidence once, not the same evidence as path,
        # raw JSON, and copied text. Other fields may include validation gaps.
        return str(content["evidence_content"]) + (
            "\n"
            + _facts(
                {
                    key: value
                    for key, value in content.items()
                    if key not in {"evidence_content", "evidence", "evidence_path"}
                }
            )
            if any(
                key not in {"evidence_content", "evidence", "evidence_path"}
                for key in content
            )
            else ""
        )
    return _facts(content) if isinstance(content, dict) else _text(content)


def action_message(
    *,
    action: dict[str, Any],
    assignment: dict[str, Any] | None = None,
    constraints: list[str] | None = None,
    include_scope: bool = True,
    full_context: bool = False,
) -> str:
    """Deliver the actual action inline, with no prerequisite instruction read."""
    kind = action["kind"]
    payload = action_payload(action)
    if action.get("reminder_sent"):
        if kind == "archon":
            return (
                f"Action {action.get('id', 'current')}. Finish required: yes. "
                "The previous turn ended without an outcome. Submit only that "
                "action's outstanding result using the finish shape already supplied; "
                "do not repeat completed work."
            )
        return "Your previous turn ended without a finish outcome. Submit the outstanding result for that action; do not repeat completed work."
    if kind == "archon":
        return _archon_message(payload, action_id=action.get("id"))
    if kind == "interview":
        return (
            "Workflow debrief only; do not resume prior work.\n"
            + str(payload["question"])
            + '\nSubmit `fulcrum finish interview_answer --input "/absolute/answer.json"` '
            + 'with {"answer":"your answer","evidence":["reference"]}.'
        )
    if kind == "specialist":
        scope = payload.get("scope") or {}
        lines = [
            str(
                payload.get("prompt")
                or "Review the selected projects using your role's normal scope."
            ),
            "Projects: " + ", ".join(scope.get("projects", [])) + ".",
        ]
        if payload.get("continuation"):
            lines.append(
                "Interview collection is complete. Finish the report; no further interview round."
            )
            for answer in payload.get("answers", []):
                lines.append(_facts(answer))
            if payload.get("missing_evidence"):
                lines.append("Missing responses: " + _text(payload["missing_evidence"]))
            if full_context:
                evidence = payload.get("retained_evidence") or {}
                for key in (
                    "window",
                    "captured_at",
                    "projects",
                    "coverage",
                    "assignments",
                    "recent_events",
                    "prior_reports",
                ):
                    if evidence.get(key):
                        lines.append(
                            "Retained "
                            + key.replace("_", " ")
                            + ": "
                            + _text(evidence[key])
                        )
        else:
            evidence = payload.get("retained_evidence") or {}
            for key in (
                "window",
                "captured_at",
                "projects",
                "coverage",
                "assignments",
                "recent_events",
                "prior_reports",
            ):
                if evidence.get(key):
                    lines.append(
                        key.replace("_", " ").capitalize() + ": " + _text(evidence[key])
                    )
        return "\n".join(lines)
    if kind == "weaver":
        return "Complete the requested authoring work. " + _facts(payload)
    if assignment is None:
        raise PromptError(f"{kind} action requires its assignment")
    lead = {"implement": "Implement", "correct": "Correct", "review": "Review"}[kind]
    lines = (
        [
            f"{lead} {assignment['bead_id']}. Approved scope:\n"
            f"{assignment['scope_snapshot']}"
        ]
        if include_scope
        else [
            f"{lead} {assignment['bead_id']}. The approved scope is unchanged from "
            "this task's earlier assignment action."
        ]
    )
    run_context = {
        key: value
        for key, value in {
            "run_id": payload.get("run_id") or assignment.get("run_id"),
            "role": payload.get("role"),
        }.items()
        if value is not None
    }
    if run_context:
        lines.append("Run context: " + _facts(run_context))
    lines.append(f"Worktree: {assignment.get('worktree_path') or 'unavailable'}.")
    candidate = payload.get("candidate") or {}
    if candidate.get("id") or assignment.get("candidate_id"):
        lines.append(
            "Candidate: "
            + _facts(
                {
                    "id": candidate.get("id") or assignment.get("candidate_id"),
                    "source": candidate.get("source_revision")
                    or assignment.get("source_oid"),
                    "tested": candidate.get("tested_revision")
                    or assignment.get("tested_oid"),
                }
            )
        )
    if constraints:
        lines.append("Constraints:\n" + "\n".join(constraints))
    handoffs = payload.get("handoffs", [])
    if kind in {"review", "correct"}:
        latest = next(
            (
                item
                for item in reversed(handoffs)
                if item.get("kind")
                == (
                    "implementation_evidence" if kind == "review" else "review_findings"
                )
                or (kind == "correct" and item.get("kind") == "missing_evidence")
            ),
            None,
        )
        if latest:
            lines.append(
                (
                    "Implementation evidence:\n"
                    if kind == "review"
                    else "Current correction:\n"
                )
                + _handoff_text(latest["content"])
            )
    if kind == "correct":
        lines.append(
            "Replacement candidate rule: leave exactly one task commit based on the "
            "current promoted `release`; amend or squash the predecessor candidate "
            "and rebase it onto `release` before finishing."
        )
        if assignment.get("mandate_candidate_id"):
            lines.append(
                "Repair permission: "
                + _facts(
                    {
                        "approved_candidate": assignment["mandate_candidate_id"],
                        "scope": assignment.get("mandate_scope"),
                        "allowed_categories": json.loads(
                            assignment.get("repair_permissions") or "[]"
                        ),
                    }
                )
            )
        failure = payload.get("delivery_failure") or assignment.get("condition")
        if failure:
            lines.append("Delivery failure: " + str(failure))
        evidence = payload.get("delivery_evidence") or {}
        if evidence:
            lines.append("Diagnosis: " + _text(evidence.get("diagnosis") or evidence))
    return "\n\n".join(lines) + "\n"


def compaction_reminder(task: dict[str, Any], action: dict[str, Any]) -> str:
    payload = action_payload(action)
    target = f" for {payload['bead_id']}" if payload.get("bead_id") else ""
    obligation = (
        "Submit only the outstanding finish result; do not repeat completed work."
        if action.get("reminder_sent")
        else "Continue from the conversation summary and finish when done."
    )
    boundary = (
        "Debrief only; do not resume prior work."
        if action["kind"] == "interview"
        else "The controller handles dispatch and delivery."
    )
    recovery = " If exact scope or action facts are missing, run `fulcrum context`."
    return f"You are {task['role']}; current action: {action['kind']}{target}. {obligation} {boundary}{recovery}"


def weaver_instructions(*, plan_mode: bool, project: str) -> str:
    if plan_mode:
        return (
            f"Project: {project}. Planning turn: inspect and propose; do not publish or call finish.\n\n"
            + load_template("weaver", role="weaver")
        )
    return "\n\n".join(
        [
            f"Project: {project}. Complete the requested writable authoring work.",
            load_template("weaver", role="weaver"),
            "The human's request is input to the authoring path above, even when "
            "phrased as a command to change the project. Use supplied facts "
            "directly. Wait for any required native helper reviews before "
            "submitting. If a finish command fails, correct the reported error "
            "and retry that same outcome.",
            "If exact current-action facts are missing after compaction, run "
            "`fulcrum context`; otherwise continue from retained conversation context.",
            finish_contract("weaver"),
            finish_syntax("weaver"),
        ]
    )
