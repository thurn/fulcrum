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
    finish_kind = "correct" if action_kind == "implement" else action_kind
    interviews_allowed = role == "sage"
    return "\n\n".join(
        [
            load_template(action_kind, role=role),
            "Subsequent messages contain the actual request and necessary facts. Use them "
            "directly. Do not manage other conversations. An interview temporarily replaces "
            "your normal duties. Finish the current action once through `fulcrum finish`, "
            "then end; the controller binds identity and routes the result. Wait for native "
            "helpers before submitting. If finish fails, correct the reported error. "
            "Planning Weaver has no finish obligation.",
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


def _archon_message(payload: dict[str, Any]) -> str:
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
        in {"proposal", "assignment_recovery", "archon_succession_completed"}
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
    return "\n".join(lines)


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
) -> str:
    """Deliver the actual action inline, with no prerequisite instruction read."""
    kind = action["kind"]
    payload = action_payload(action)
    if action.get("reminder_sent"):
        return "Your previous turn ended without a finish outcome. Submit the outstanding result for that action; do not repeat completed work."
    if kind == "archon":
        return _archon_message(payload)
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
    lines = [
        f"{lead} {assignment['bead_id']}. Approved scope:\n{assignment['scope_snapshot']}"
    ]
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
    return f"You are {task['role']}; current action: {action['kind']}{target}. {obligation} {boundary}"


def weaver_instructions(*, plan_mode: bool, project: str) -> str:
    if plan_mode:
        return (
            f"Project: {project}. Planning turn: inspect and propose; do not publish or call finish.\n\n"
            + load_template("weaver", role="weaver")
        )
    return (
        f"Project: {project}. Complete the requested writable authoring work.\n\n"
        + role_instructions("weaver", role="weaver")
    )
