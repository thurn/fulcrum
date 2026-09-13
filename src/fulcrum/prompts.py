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
    return (
        load_template(action_kind, role=role)
        + "\n\n"
        + (
            "Each message identifies your current action. Read `fulcrum instructions` "
            "for its exact scope and current decision data before acting. Read "
            "`fulcrum instructions --section evidence` for retained evidence, and "
            "`fulcrum instructions --section finish` for commands and exact file examples "
            "when needed. `--section role` restores this guidance. Do not rediscover "
            "fleet state or manage other conversations. An interview action temporarily "
            "replaces your normal role duties. Finish the current action once through "
            "`fulcrum finish`, then end; the controller binds identity and routes the result. "
            "If a finish call fails, inspect the error and correct it without inventing "
            "success. Native helpers must finish before you submit. Plan-mode Weaver "
            "has no finish obligation.\n"
        )
    )


def action_payload(action: dict[str, Any]) -> dict[str, Any]:
    payload = action.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise PromptError("action payload must be an object")
    return payload


def action_notice(action: dict[str, Any]) -> str:
    """A small wake message; never repeat the role manual or evidence history."""
    if action.get("reminder_sent"):
        return (
            "Your previous turn ended without a finish outcome. Submit only the "
            "outstanding result; do not repeat the work. The original action remains "
            "available through `fulcrum instructions`; use `--section finish` for syntax."
        )
    payload = action_payload(action)
    kind = action["kind"]
    if kind == "archon":
        count = len(payload.get("batch_items", []))
        lead = (
            f"{count} updates need your scheduling decision."
            if count
            else (
                "Confirm the retained fleet configuration and initialize this coordinator."
                if payload.get("purpose") == "materialize_archon"
                else "Set the initial fleet capacity and recurring policies."
            )
        )
    elif kind == "implement":
        lead = f"Implement bead {payload.get('bead_id', 'in the current assignment')}."
    elif kind == "correct":
        lead = (
            "Address the current correction request; check retained repair permissions."
        )
    elif kind == "review":
        candidate = payload.get("candidate") or {}
        lead = f"Review candidate {candidate.get('id', 'in the current assignment')} independently."
    elif kind == "specialist":
        lead = (
            "Interview collection is complete. Use the answers and gaps to finish your report."
            if payload.get("continuation")
            else "Perform the requested analysis within the retained project scope."
        )
    elif kind == "interview":
        lead = "Answer the assigned debrief question only; do not resume prior work."
    else:
        lead = "Complete the current authoring action."
    return (
        lead
        + " Read `fulcrum instructions` for details, then submit the appropriate finish outcome."
    )


def compaction_reminder(task: dict[str, Any], action: dict[str, Any]) -> str:
    obligation = (
        "Only submit the outstanding finish result; do not repeat completed work."
        if action.get("reminder_sent")
        else "Continue this action and submit its finish outcome when done."
    )
    boundary = (
        "This is debrief only; prior implementation and review authority do not apply."
        if action["kind"] == "interview"
        else "The controller owns dispatch and delivery."
    )
    return (
        f"Fulcrum role: {task['role']}. Current action: {action['kind']}. {obligation} "
        f"{boundary} If context was lost, read `fulcrum instructions` for the authoritative "
        "scope and current request; `--section evidence`, `--section role`, and "
        "`--section finish` retrieve only the reference you need."
    )


def build_context(
    *,
    task: dict[str, Any],
    action: dict[str, Any],
    assignment: dict[str, Any] | None = None,
    constraints: list[str] | None = None,
    section: str = "context",
) -> str:
    """Return selected durable facts without repeating onboarding on every read."""
    kind = action["kind"]
    payload = dict(action_payload(action))
    if section == "role":
        return role_instructions(kind, role=str(task.get("role") or ""))
    if section == "finish":
        interviews_allowed = task.get("role") == "sage" and not payload.get(
            "continuation"
        )
        return (
            finish_syntax(kind, interviews_allowed=interviews_allowed)
            + "\n\n"
            + finish_contract(kind, interviews_allowed=interviews_allowed)
        )
    if section == "evidence":
        evidence = {
            key: payload[key]
            for key in (
                "handoffs",
                "retained_evidence",
                "answers",
                "missing_evidence",
                "delivery_evidence",
            )
            if key in payload
        }
        return (
            json.dumps(evidence, indent=2, sort_keys=True)
            if evidence
            else "No retained evidence for this action."
        )
    if section != "context":
        raise PromptError(f"unknown instruction section: {section}")
    lines = [f"Current action: {kind}."]
    if assignment is not None:
        lines += [
            f"Bead: {assignment['bead_id']}; assignment: {assignment['id']}.",
            "Approved scope:\n\n" + assignment["scope_snapshot"],
            f"Worktree: {assignment.get('worktree_path') or 'unavailable'}.",
        ]
        if kind == "correct":
            lines.append(
                "Repair authority:\n"
                + json.dumps(
                    {
                        "approved_candidate": assignment.get("mandate_candidate_id"),
                        "approved_scope": assignment.get("mandate_scope"),
                        "allowed_categories": json.loads(
                            assignment.get("repair_permissions") or "[]"
                        ),
                        "condition": assignment.get("condition"),
                    },
                    indent=2,
                )
            )
    handoffs = payload.pop("handoffs", [])
    if handoffs:
        # History remains available in evidence; only the most recent review
        # request belongs in the current correction brief.
        current = next(
            (
                item
                for item in reversed(handoffs)
                if item.get("kind") in {"review_findings", "missing_evidence"}
            ),
            None,
        )
        if kind == "correct" and current is not None:
            payload["current_correction"] = current
        lines.append(
            "Implementation evidence and review history: `fulcrum instructions --section evidence`."
        )
    if payload.pop("delivery_evidence", None) is not None:
        lines.append(
            "Retained delivery diagnosis: `fulcrum instructions --section evidence`."
        )
    retained = payload.pop("retained_evidence", None)
    if retained is not None:
        payload["evidence_summary"] = {
            key: retained[key]
            for key in ("captured_at", "window", "projects", "coverage")
            if key in retained
        }
        lines.append(
            "Source records and prior reports: `fulcrum instructions --section evidence`."
        )
    if "answers" in payload:
        payload["answer_count"] = len(payload.pop("answers"))
        lines.append("Interview answers: `fulcrum instructions --section evidence`.")
    lines.append(json.dumps(payload, indent=2, sort_keys=True))
    if constraints:
        lines.append(
            "Current constraints:\n" + "\n".join(f"- {item}" for item in constraints)
        )
    if action.get("reminder_sent"):
        lines.append(
            "Only the outstanding finish result is requested; do not repeat completed work."
        )
    return "\n\n".join(lines) + "\n"


def weaver_instructions(*, plan_mode: bool, project: str) -> str:
    mode = (
        "Planning turn: inspect and propose; do not publish or call finish."
        if plan_mode
        else "Writable authoring turn: use small intake, approved-plan publication, or refinement as requested. Finish after filing."
    )
    return f"Project: {project}. {mode}\n\n" + role_instructions(
        "weaver", role="weaver"
    )
