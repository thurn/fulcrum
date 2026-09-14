"""Role instructions, short notices, and separately retrievable action context."""

from __future__ import annotations

import json
from collections.abc import Mapping
from importlib.resources import files
from typing import Any

from fulcrum.outcomes import finish_contract, finish_syntax


class PromptError(RuntimeError):
    pass


MAX_ARCHON_TEXT = 240
MAX_ARCHON_ROWS = 20
MAX_ARCHON_FACTS = 480
MAX_ARCHON_MESSAGE_CHARS = 32_000
MAX_ARCHON_ACTION_CHARS = 28_000


TEMPLATES = {
    "implement": "executor.md",
    "correct": "executor.md",
    "review": "overseer.md",
    "archon": "archon.md",
    "weaver": "weaver.md",
    "interview": "interview.md",
    "operative": "operative.md",
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
    """Onboarding and command reference, supplied in the first action prompt."""
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


def _bounded(value: Any, limit: int = MAX_ARCHON_TEXT) -> str:
    """Render one decision fact with a deterministic prompt-size ceiling."""

    rendered = " ".join(_text(value).split())
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 1].rstrip() + "…"


def _bounded_facts(values: dict[str, Any]) -> str:
    rendered = "; ".join(
        f"{_bounded(key.replace('_', ' '), 80)}: {_bounded(value)}"
        for key, value in list(values.items())[:12]
        if value is not None and value != [] and value != {}
    )
    return _bounded(rendered, MAX_ARCHON_FACTS)


def _budgeted_archon_message(
    required_lines: list[str],
    snapshot_sections: list[tuple[str, str, list[str]]],
    finish_lines: list[str],
) -> str:
    """Fit optional fleet detail without displacing frozen decision facts.

    Snapshot rows are admitted round-robin so active work, holds, and uncertain
    operations all remain represented under pressure. Each section summary is
    rendered from the final admitted count, making omitted-state accounting
    deterministic and exact.
    """

    shown = [0] * len(snapshot_sections)

    def render(counts: list[int]) -> str:
        rendered_lines = list(required_lines)
        for index, (label, retained_noun, rows) in enumerate(snapshot_sections):
            if not rows:
                continue
            count = counts[index]
            omitted = len(rows) - count
            summary = f"{label}: showing {count}/{len(rows)}"
            if omitted:
                summary += (
                    f"; {omitted} additional {retained_noun} retained by the "
                    "controller."
                )
            else:
                summary += "; all relevant details shown."
            rendered_lines.append(summary)
            rendered_lines.extend(rows[:count])
        rendered_lines.extend(finish_lines)
        return "\n".join(rendered_lines)

    message = render(shown)
    if len(message) > MAX_ARCHON_ACTION_CHARS:
        raise PromptError(
            "required Archon decision facts exceed their deterministic size ceiling"
        )

    while True:
        admitted = False
        for index, (_, _, rows) in enumerate(snapshot_sections):
            if shown[index] >= len(rows):
                continue
            candidate = list(shown)
            candidate[index] += 1
            candidate_message = render(candidate)
            if len(candidate_message) <= MAX_ARCHON_ACTION_CHARS:
                shown = candidate
                message = candidate_message
                admitted = True
        if not admitted:
            return message


def _archon_finish_guidance(
    payload: dict[str, Any],
    *,
    action_id: int | str | None,
    handoff_paths: dict[str, str] | None = None,
) -> list[str]:
    """Give Archon only the result shapes relevant to this frozen action."""
    items = payload.get("batch_items", [])
    update_ids = [int(item["update_id"]) for item in items]
    contents = [
        item["content"] for item in items if isinstance(item.get("content"), dict)
    ]
    kinds = {content.get("kind") for content in contents}
    relevant = _relevant_structured_outcomes("archon", payload, None)
    finish_choices = (
        "decisions" if relevant == ("decisions",) else "decisions or deferred"
    )
    decision_kind = "Acknowledgement" if relevant == ("decisions",) else "Scheduling"
    rendered_id = action_id if action_id is not None else "current"
    lines = [
        f"Action {rendered_id}. Finish required: yes. "
        f"Update IDs: {json.dumps(update_ids)}. "
        f"{decision_kind} decision required; finish with {finish_choices}."
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
        if "proposal" in kinds:
            proposals = [
                {
                    "decision": "approve",
                    "project": content["project"],
                    "beads": [content["bead_id"]],
                    "scope_references": {
                        content["bead_id"]: content.get("scope_reference", "missing")
                    },
                }
                for item in items
                if isinstance((content := item.get("content")), dict)
                and content.get("kind") == "proposal"
            ]
            lines.append(
                "Approve only with the listed controller-retained scope reference: "
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

    lines.extend(_structured_finish_guidance(handoff_paths, relevant))
    if "deferred" in relevant:
        lines.append(
            "Defer the whole batch only when it must wait. Reactivation JSON must "
            "contain exactly one concrete trigger: capacity, dependency, hold, "
            "operator_change, or next_check_at."
        )
    return lines


def _archon_message(
    payload: dict[str, Any],
    *,
    action_id: int | str | None,
    handoff_paths: dict[str, str] | None = None,
) -> str:
    """Render the pending decisions, not the entire fleet record or role manual."""
    items = payload.get("batch_items", [])
    lines: list[str] = []
    snapshot_sections: list[tuple[str, str, list[str]]] = []
    projects: set[str] = set()
    for item in items:
        content = item["content"]
        prefix = f"Update {item['update_id']}: "
        if not isinstance(content, dict):
            lines.append(prefix + _bounded(content))
            continue
        project = content.get("project") or content.get("project_id")
        if project:
            projects.add(str(project))
        kind = content.get("kind")
        if kind == "proposal":
            lines.append(
                prefix
                + f"{content['bead_id']} ({project}) — {_bounded(content['title'])}. "
                + f"Scope summary: {_bounded(content.get('scope_summary', 'unavailable'))}. "
                + f"Retained scope: {content.get('scope_reference', 'missing')}."
            )
            details = {key: content.get(key) for key in ("dependencies", "context")}
            plan = content.get("plan") or {}
            if plan.get("id"):
                details["plan"] = plan
            models = content.get("models") or {}
            if models.get("provenance") not in {None, "default"}:
                details["models"] = models
            if _bounded_facts(details):
                lines.append(_bounded_facts(details))
        elif kind == "assignment_completed":
            lines.append(
                prefix
                + f"{_bounded(content['bead_id'])} completed (assignment {content['assignment_id']}, run {content['run_id']})."
            )
            lines.extend(_archon_cost_confirmation(content))
            if content.get("minor_fixes"):
                lines.append(
                    "Overseer nonblocking follow-up (not approved work): "
                    + _bounded(content["minor_fixes"])
                )
        elif kind == "assignment_recovery":
            lines.append(
                prefix
                + f"Assignment {content['assignment_id']} ({_bounded(content['bead_id'])}, run {content['run_id']}) needs recovery: {_bounded(content['condition'])}. "
                + _bounded_facts(
                    {
                        "attempt": content.get("attempt"),
                        "next_attempt_at": content.get("next_attempt_at"),
                    }
                )
            )
        elif kind == "review_failure_escalation":
            lines.append(
                prefix
                + f"Assignment {content['assignment_id']} reached {content['review_failures']} substantive review failures: {_bounded(content['condition'])}. "
                + f"Bead/project: {_bounded(content.get('bead_id'))}/{_bounded(content.get('project'))}."
            )
            for label, value in (
                ("Candidate revision", content.get("candidate_revision")),
                ("Unresolved findings", content.get("unresolved_findings")),
                ("Prior review history", content.get("prior_review_history")),
                (
                    "Changes since prior attempt",
                    content.get("changes_since_prior_attempt"),
                ),
                ("Reviewer recommendation", content.get("reviewer_recommendation")),
            ):
                lines.append(f"{label.lower()}: {_bounded(value, MAX_ARCHON_FACTS)}")
            lines.append(
                _bounded_facts(
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
                + f"{_bounded(content['specialist'])} report {content['occurrence_id']}: {_bounded(content['summary'])} ({content['finding_count']} findings). "
                + _bounded_facts({"scope": content.get("scope")})
            )
            lines.extend(_archon_cost_confirmation(content))
        else:
            # Unknown exceptions retain their actual decision data instead of
            # silently becoming an uninformative notification count.
            lines.append(prefix + _bounded_facts(content))
    snapshot = payload.get("fleet_snapshot") or {}
    capacity = snapshot.get("capacity") or {}
    if capacity:
        limits = capacity.get("project_limits") or {}
        usage = capacity.get("project_usage") or {}
        if not items:
            projects.update(limits)
        line = f"Capacity used: global {_bounded(capacity.get('global_usage', 0), 32)}/{_bounded(capacity.get('global_limit', 'unset'), 32)}"
        for project in sorted(projects):
            line += f"; {_bounded(project, 120)} {_bounded(usage.get(project, 0), 32)}/{_bounded(limits.get(project, 'unset'), 32)}"
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
        active = [
            row
            for row in snapshot.get("unfinished_assignments", [])
            if not projects or row.get("project_id") in projects
        ]
        active_rows: list[str] = []
        for row in active:
            detail = (
                f"Existing {_bounded(row['bead_id'])} ({_bounded(row.get('project_id'))}, run {row['run_id']}, assignment {row['id']}): {_bounded(row['stage'], 80)}"
                + (f" — {_bounded(row['condition'])}" if row.get("condition") else "")
                + f"; priority {row.get('priority', 0)}."
            )
            if row.get("conflict_keys"):
                detail += (
                    f"\nConflicts for assignment {row['id']}: "
                    f"{_bounded(row['conflict_keys'])}."
                )
            active_rows.append(detail)
        snapshot_sections.append(("Active work", "relevant assignments", active_rows))
        holds = [
            hold
            for hold in snapshot.get("holds", [])
            if not (
                hold.get("scope") == "project"
                and projects
                and str(hold.get("target")) not in projects
            )
        ]
        hold_rows: list[str] = []
        for hold in holds:
            # Run and assignment holds are retained: their scope may be relevant
            # even when the update does not include a project binding.
            hold_rows.append(
                f"Hold {hold['id']}: "
                + _bounded_facts(
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
        snapshot_sections.append(("Holds", "relevant holds", hold_rows))
        operations = snapshot.get("uncertain_operations", [])
        operation_rows: list[str] = []
        for operation in operations:
            operation_rows.append(
                f"External operation {operation['id']} requires explicit resolution: "
                + _bounded_facts(
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
        snapshot_sections.append(
            (
                "External operations",
                "unresolved operations",
                operation_rows,
            )
        )
    if not items:
        lines.insert(
            0,
            _bounded(
                payload.get("required")
                or "Set the initial fleet capacity and recurring policies."
            ),
        )
        if payload.get("projects"):
            lines.append("Projects: " + _bounded(payload["projects"]) + ".")
        for policy in snapshot.get("policies", [])[:MAX_ARCHON_ROWS]:
            lines.append("Policy: " + _bounded_facts(policy))
    guidance = _archon_finish_guidance(
        payload, action_id=action_id, handoff_paths=handoff_paths
    )
    return _budgeted_archon_message(
        [guidance[0], *lines], snapshot_sections, guidance[1:]
    )


def archon_action_fits(payload: dict[str, Any]) -> bool:
    """Return whether one frozen batch fits the reserved Archon action budget."""

    try:
        # SQLite action IDs cannot exceed this value. Using it before the action
        # row exists makes admission conservative and independent of ID width.
        _archon_message(payload, action_id=9_223_372_036_854_775_807)
    except PromptError:
        return False
    return True


def _archon_cost_confirmation(content: dict[str, Any]) -> list[str]:
    """Render only controller-frozen values; Archon must never do cost arithmetic."""

    cost = content.get("cost")
    completed_action_id = content.get("action_id")
    if not isinstance(cost, dict) or not isinstance(completed_action_id, int):
        return ["No controller-supplied cost estimate is available; do not invent one."]
    action = cost.get("action")
    display = action.get("attributed_display") if isinstance(action, dict) else None
    if not isinstance(display, str):
        return ["No controller-supplied cost estimate is available; do not invent one."]
    lines = [
        f"Action {completed_action_id} completed at estimated API cost of {display}."
    ]
    workflow = cost.get("workflow_through_completion")
    workflow_display = workflow.get("display") if isinstance(workflow, dict) else None
    if isinstance(workflow_display, str):
        lines.append(
            "Frozen end-to-end workflow estimate through this completion: "
            f"{workflow_display}; it excludes the currently running Archon "
            "acknowledgement turn."
        )
    if cost.get("coverage") != "complete":
        reasons = [
            *(
                cost.get("assumptions", [])
                if isinstance(cost.get("assumptions"), list)
                else []
            ),
            *(
                cost.get("exclusions", [])
                if isinstance(cost.get("exclusions"), list)
                else []
            ),
        ]
        qualification = (
            str(reasons[0]) if reasons else "some pricing facts are unavailable"
        )
        lines.append(f"Partial estimate: {qualification}.")
    return lines


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


def _structured_finish_guidance(
    handoff_paths: dict[str, str] | None, outcomes: tuple[str, ...]
) -> list[str]:
    """Render exact controller-owned destinations for a bound action."""

    if not outcomes:
        return []
    if not handoff_paths:
        return [
            "The controller supplies the exact action-bound destination in the "
            "current action. "
            + " ".join(
                f"Use `fulcrum finish {outcome} --input CONTROLLER_SUPPLIED_PATH`."
                for outcome in outcomes
            )
        ]
    lines: list[str] = []
    for outcome in outcomes:
        path = handoff_paths.get(outcome)
        if path is None:
            continue
        reason = ' --reason "Why this batch must wait"' if outcome == "deferred" else ""
        lines.append(
            f"Finish {outcome}: write complete JSON to a sibling temporary file, "
            f'atomically rename it to "{path}", then run '
            f'`fulcrum finish {outcome}{reason} --input "{path}"`.'
        )
    return lines


def _relevant_structured_outcomes(
    kind: str, payload: dict[str, Any], role: str | None
) -> tuple[str, ...]:
    if kind == "review":
        return ("approved", "changes_requested", "incomplete")
    if kind == "interview":
        return ("interview_answer",)
    if kind == "specialist":
        return (
            ("report", "evidence_needed")
            if role == "sage" and not payload.get("continuation")
            else ("report",)
        )
    if kind == "archon":
        contents = [
            item.get("content")
            for item in payload.get("batch_items", [])
            if isinstance(item, dict)
        ]
        completion_kinds = {
            "assignment_completed",
            "specialist_completed",
            "archon_succession_completed",
        }
        acknowledge_only = bool(contents) and all(
            isinstance(content, dict)
            and content.get("kind") in completion_kinds
            and not content.get("required_decision")
            and not content.get("requires_decision")
            for content in contents
        )
        return ("decisions",) if acknowledge_only else ("decisions", "deferred")
    return ()


def action_message(
    *,
    action: dict[str, Any],
    assignment: dict[str, Any] | None = None,
    constraints: list[str] | None = None,
    include_scope: bool = True,
    full_context: bool = False,
    handoff_paths: dict[str, str] | None = None,
    role: str | None = None,
) -> str:
    """Deliver the actual action inline, with no prerequisite instruction read."""
    kind = action["kind"]
    payload = action_payload(action)
    if action.get("reminder_sent"):
        lead = (
            f"Action {action.get('id', 'current')}. Finish required: yes. "
            "The previous turn ended without an outcome. Submit only that "
            "action's outstanding result; do not repeat completed work."
            if kind == "archon"
            else "Your previous turn ended without a finish outcome. Submit the outstanding result for that action; do not repeat completed work."
        )
        outcomes = _relevant_structured_outcomes(kind, payload, role)
        return "\n".join([lead, *_structured_finish_guidance(handoff_paths, outcomes)])
    if kind == "archon":
        return _archon_message(
            payload,
            action_id=action.get("id"),
            handoff_paths=handoff_paths,
        )
    if kind == "interview":
        return "\n".join(
            [
                "Workflow debrief only; do not resume prior work.\n"
                + str(payload["question"])
                + '\nUse {"answer":"your answer","evidence":["reference"]}.',
                *_structured_finish_guidance(handoff_paths, ("interview_answer",)),
            ]
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
                keys = (
                    (
                        "target",
                        "captured_at",
                        "coverage",
                        "workflow_actions",
                        "handoffs",
                        "external_operations",
                        "controller_events",
                        "candidate_evidence",
                        "token_usage",
                        "frozen_accounting",
                        "existing_beads",
                    )
                    if payload.get("direct_item")
                    else (
                        "window",
                        "captured_at",
                        "projects",
                        "coverage",
                        "assignments",
                        "recent_events",
                        "prior_reports",
                    )
                )
                for key in keys:
                    if evidence.get(key):
                        lines.append(
                            "Retained "
                            + key.replace("_", " ")
                            + ": "
                            + _text(evidence[key])
                        )
        else:
            evidence = payload.get("retained_evidence") or {}
            keys = (
                (
                    "target",
                    "captured_at",
                    "coverage",
                    "workflow_actions",
                    "handoffs",
                    "external_operations",
                    "controller_events",
                    "candidate_evidence",
                    "token_usage",
                    "frozen_accounting",
                    "existing_beads",
                )
                if payload.get("direct_item")
                else (
                    "window",
                    "captured_at",
                    "projects",
                    "coverage",
                    "assignments",
                    "recent_events",
                    "prior_reports",
                )
            )
            for key in keys:
                if evidence.get(key):
                    lines.append(
                        key.replace("_", " ").capitalize() + ": " + _text(evidence[key])
                    )
        outcomes = ["report"]
        if role == "sage" and not payload.get("continuation"):
            outcomes.append("evidence_needed")
        return "\n".join(
            [*lines, *_structured_finish_guidance(handoff_paths, tuple(outcomes))]
        )
    if kind == "operative":
        return (
            "Resolve only the human-stated emergency. The operative journal is the "
            "authority fence; inspect the dossier before mutation, preserve unrelated "
            "changes, and record exact evidence for every bypass. Finish only after all "
            "implicated agents, effects, source ownership, validation, and delivery are "
            "coherent."
        )
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
    if kind == "review":
        lines.extend(
            _structured_finish_guidance(
                handoff_paths, ("approved", "changes_requested", "incomplete")
            )
        )
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


def operative_compaction_reminder(journal: Mapping[str, Any]) -> str:
    """Restore emergency authority without asserting an ordinary role boundary."""

    return " ".join(
        [
            "You are the bound Operative for takeover "
            f"{journal.get('takeover_id', 'unavailable')}; current action: operative; "
            f"state: {journal.get('state', 'unavailable')}.",
            f"Exact emergency scope: {journal.get('scope', 'unavailable')}.",
            "Within that scope you hold emergency Fulcrum authority; human constraints "
            "and platform safety remain authoritative, and ordinary roles remain fenced.",
            "Continue evidence-preserving reconciliation, retrieve the bounded dossier "
            "when facts may have changed, and retain exact intent/sent/result evidence "
            "for external effects.",
            "Successful closeout remains two phase: submit the complete closeout evidence "
            "contract, then remain bound until the Operative turn and helpers are terminal "
            "and the controller revalidates and closes the journal.",
            f"Next durable step: {journal.get('next_step', 'retrieve fulcrum operative dossier')}.",
            "If exact current-action facts are missing, run `fulcrum context`.",
        ]
    )


def operative_instructions() -> str:
    """Return the complete installed emergency authority contract."""

    return "\n\n".join(
        [
            load_template("operative"),
            "Use caller-bound `fulcrum operative` controls with absolute JSON input "
            "files. If the live CLI or controller fails, invoke the retained recovery "
            "launcher under the configured control root.",
            "If exact current-action facts are missing after compaction, run `fulcrum "
            "operative dossier`; otherwise continue from retained conversation context.",
        ]
    )


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


def direct_sage_instructions(action: dict[str, Any]) -> str:
    """Return the full controller-owned manual for a human-adopted Sage."""

    return "\n\n".join(
        [
            role_instructions("specialist", role="sage"),
            "# Current action",
            action_message(action=action),
        ]
    )
