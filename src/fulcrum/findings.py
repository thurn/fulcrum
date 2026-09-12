"""Project-scoped finding intake without rewriting existing implementation scope."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fulcrum.beads import (
    BeadDraft,
    BeadsError,
    bead_label_facts,
    ensure_bead,
    run_beads,
)


@dataclass(frozen=True)
class Finding:
    project_id: str
    problem_key: str
    title: str
    evidence: str
    impact: str
    proposed_change: str
    acceptance: str
    interfaces: str
    validation: str
    suggested_priority: int = 2

    def draft(self) -> BeadDraft:
        if (
            not self.problem_key.strip()
            or not self.evidence.strip()
            or not self.impact.strip()
        ):
            raise BeadsError(
                "finding requires stable underlying problem, evidence, and impact"
            )
        return BeadDraft(
            intake_key=f"finding:{self.project_id}:{self.problem_key}",
            title=self.title,
            project_id=self.project_id,
            problem=f"{self.problem_key}\n\nEvidence: {self.evidence}",
            outcome=f"Expected benefit (not a measured result): {self.impact}",
            bounded_scope=self.proposed_change,
            context=self.interfaces,
            dependencies=(),
            acceptance_criteria=self.acceptance,
            validation=self.validation,
            activation="future",
            priority=self.suggested_priority,
        )


def publish_finding(
    brain: Path, finding: Finding, *, matching_issue_id: str | None = None
) -> str:
    """Caller matches underlying problems, not wording; existing issues get notes only."""
    draft = finding.draft()
    issues = run_beads(brain, ["list", "--all", "--limit", "0"])
    if not isinstance(issues, list) or any(
        not isinstance(item, dict) for item in issues
    ):
        raise BeadsError("findings require a complete Beads issue list")
    matches = [
        item
        for item in issues
        if item.get("external_ref") == draft.external_reference
        or (matching_issue_id is not None and item.get("id") == matching_issue_id)
    ]
    if matching_issue_id and not any(
        item.get("id") == matching_issue_id for item in matches
    ):
        raise BeadsError(
            "matched finding issue is unavailable; inspect before creating"
        )
    if len(matches) > 1:
        raise BeadsError("multiple underlying-problem matches require reconciliation")
    if not matches:
        return ensure_bead(brain, draft)
    issue = matches[0]
    labels = issue.get("labels")
    if not isinstance(labels, list) or not all(
        isinstance(label, str) for label in labels
    ):
        raise BeadsError("finding issue has malformed project labels")
    if bead_label_facts(labels)["project_id"] != finding.project_id:
        raise BeadsError("finding cannot cross project scope")
    if issue.get("status") not in {"open", "in_progress", "blocked", "deferred"}:
        raise BeadsError(
            "prior finding status is terminal or unavailable; ask Archon to classify"
        )
    issue_id = issue.get("id")
    if not isinstance(issue_id, str) or not issue_id:
        raise BeadsError("matched finding has no actual issue ID")
    note = (
        "Postmortem/architecture evidence (proposal only; existing scope unchanged):\n"
        + draft.description()
    )
    existing_notes = issue.get("notes", "") or ""
    if not isinstance(existing_notes, str):
        raise BeadsError("finding issue notes are malformed")
    if note not in existing_notes:
        run_beads(
            brain,
            ["update", issue_id, "--append-notes", note, "--dolt-auto-commit", "batch"],
        )
    return issue_id
