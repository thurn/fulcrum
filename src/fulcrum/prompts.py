"""Fresh, self-contained action briefs loaded from the editable checkout."""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

from fulcrum.outcomes import finish_syntax


class PromptError(RuntimeError):
    pass


TEMPLATES = {
    "implement": "executor.md",
    "correct": "executor.md",
    "review": "overseer.md",
    "archon": "archon.md",
    "weaver": "weaver.md",
    "specialist": "specialist.md",
    "interview": "interview.md",
}


def load_template(action_kind: str) -> str:
    name = TEMPLATES.get(action_kind)
    if name is None:
        raise PromptError(f"no prompt template for {action_kind}")
    resource = files("fulcrum").joinpath("prompts", name)
    try:
        return resource.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError) as error:
        raise PromptError(f"missing prompt template {name}: {error}") from error


def build_prompt(
    *,
    action_kind: str,
    task: dict[str, Any],
    action: dict[str, Any],
    assignment: dict[str, Any] | None = None,
    constraints: list[str] | None = None,
    evidence: list[str] | None = None,
) -> str:
    """Build the complete brief used for dispatch and compaction refresh."""

    payload = action.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {"detail": payload}
    lines = [
        load_template(action_kind),
        "",
        f"Identity: {task['title']} ({task['native_thread_id']}).",
    ]
    if assignment is not None:
        lines.extend(
            [
                f"Assignment: {assignment['id']}; bead: {assignment['bead_id']}; stage: {assignment['stage']}.",
                "Approved scope:",
                assignment["scope_snapshot"],
            ]
        )
        if assignment.get("candidate_id"):
            lines.append(
                f"Candidate: {assignment['candidate_id']}; source: {assignment.get('source_oid') or 'unavailable'}."
            )
        if assignment.get("worktree_path"):
            lines.append(f"Worktree: {assignment['worktree_path']}.")
    lines.append(
        "Current action:\n" + json.dumps(payload or {}, indent=2, sort_keys=True)
    )
    if constraints:
        lines.append(
            "Current constraints:\n" + "\n".join(f"- {item}" for item in constraints)
        )
    if evidence:
        lines.append(
            "Evidence references:\n" + "\n".join(f"- {item}" for item in evidence)
        )
    lines.extend(
        [
            "Finish exactly once with one compatible command:",
            finish_syntax(action_kind),
            "End after the finish command succeeds.",
        ]
    )
    return "\n\n".join(lines) + "\n"
