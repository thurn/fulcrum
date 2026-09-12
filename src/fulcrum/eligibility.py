"""Read-only eligibility, completion, and plan-revision explanations."""

from __future__ import annotations

from typing import Literal, TypedDict, cast

from fulcrum.beads import BeadsError, bead_label_facts
from fulcrum.documents import PlanDocument
from fulcrum.records import AssignmentRecord, Hold, ProgressRecord


class CompletionEvidence(TypedDict):
    certified_promotion: bool | None
    source_synchronized: bool | None
    cleanup_complete: bool | None
    reference: str | None


class BeadSnapshot(TypedDict):
    bead_id: str
    title: str
    labels: list[str]
    status: Literal["open", "in_progress", "blocked", "deferred", "closed", "canceled"]
    ready: bool | None
    work_kind: Literal["code", "non_code"]
    description: str | None
    acceptance_criteria: str | None
    required: bool
    scope_decision: str | None
    completion_evidence: CompletionEvidence | None


class PlanCompletion(TypedDict):
    plan_id: str
    complete: bool
    reasons: list[str]
    contradictory_closures: list[str]


class ProjectIntegration(TypedDict):
    project_enabled: bool | None
    repository_available: bool | None
    codex_available: bool | None
    tollgate_available: bool | None


class ResourceFacts(TypedDict):
    observations_available: bool
    compatible: bool | None
    reasons: list[str]


class EligibilityReason(TypedDict):
    code: str
    message: str
    references: list[str]


class EligibilitySummary(TypedDict):
    bead_id: str
    project_id: str | None
    plan_id: str | None
    eligible: bool
    facts: dict[str, object]
    reasons: list[EligibilityReason]


class AssignmentReconciliation(TypedDict):
    assignment_id: str
    plan_id: str | None
    affected: bool
    approved_plan_commit: str | None
    current_plan_commit: str
    added_prerequisites: list[str]
    removed_prerequisites: list[str]
    activation_changed: bool
    body_changed: bool
    action: Literal["continue", "refresh_reference", "pause_and_reconcile"]
    mandate_preserved: bool
    reasons: list[str]


REQUIRED_BEAD_SECTIONS = (
    "## Problem",
    "## Outcome",
    "## Bounded scope",
    "## Context",
    "## Dependencies",
    "## Acceptance criteria",
    "## Validation",
    "## Authorized model overrides",
)


def _bead_done(bead: BeadSnapshot) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if bead["status"] == "canceled":
        return False, [f"{bead['bead_id']} is canceled"]
    if bead["status"] != "closed":
        return False, [f"{bead['bead_id']} is not closed"]
    if bead["work_kind"] == "non_code":
        return True, []

    evidence = bead["completion_evidence"]
    if evidence is None:
        return False, [
            f"{bead['bead_id']} is closed without retained code-delivery evidence"
        ]
    for value, label in (
        (evidence["certified_promotion"], "certified promotion"),
        (evidence["source_synchronized"], "source synchronization"),
        (evidence["cleanup_complete"], "cleanup"),
    ):
        if value is not True:
            state = "unknown" if value is None else "incomplete"
            reasons.append(f"{bead['bead_id']} {label} is {state}")
    return not reasons, reasons


def plan_completion(plan: PlanDocument, beads: list[BeadSnapshot]) -> PlanCompletion:
    """Explain whether all currently required plan work is actually complete."""

    reasons: list[str] = []
    contradictions: list[str] = []
    if plan["dependency_cycle"]:
        reasons.append("plan participates in a dependency cycle")

    required: list[BeadSnapshot] = []
    for bead in beads:
        if bead["required"]:
            required.append(bead)
        elif bead["scope_decision"]:
            continue
        else:
            required.append(bead)
            contradictions.append(
                f"{bead['bead_id']} was marked non-required without a recorded scope decision"
            )
    if not required:
        reasons.append("plan has no required beads; empty plans are not complete")

    for bead in required:
        done, bead_reasons = _bead_done(bead)
        if not done:
            reasons.extend(bead_reasons)
            if bead["status"] == "closed":
                contradictions.extend(bead_reasons)
    reasons.extend(contradictions)
    return {
        "plan_id": plan["plan_id"],
        "complete": not reasons,
        "reasons": list(dict.fromkeys(reasons)),
        "contradictory_closures": list(dict.fromkeys(contradictions)),
    }


def _prepared(bead: BeadSnapshot) -> bool:
    description = bead["description"]
    acceptance = bead["acceptance_criteria"]
    return (
        isinstance(description, str)
        and all(section in description for section in REQUIRED_BEAD_SECTIONS)
        and isinstance(acceptance, str)
        and bool(acceptance.strip())
    )


def _active_author(progress: ProgressRecord | None) -> bool:
    return (
        progress is not None
        and progress["role"] == "weaver"
        and progress["phase"] not in {"completed", "canceled"}
    )


def _reason(
    reasons: list[EligibilityReason], code: str, message: str, *references: str
) -> None:
    reasons.append({"code": code, "message": message, "references": list(references)})


def summarize_eligibility(
    bead: BeadSnapshot,
    *,
    plans: list[PlanDocument],
    plan_beads: dict[str, list[BeadSnapshot]],
    plan_completions: dict[str, PlanCompletion],
    holds: list[Hold],
    integration: ProjectIntegration,
    assignments: list[AssignmentRecord],
    resources: ResourceFacts,
    author_progress: ProgressRecord | None = None,
) -> EligibilitySummary:
    """Compose independent facts without changing any source of truth."""

    reasons: list[EligibilityReason] = []
    project_id: str | None = None
    plan_id: str | None = None
    activation: str | None = None
    try:
        labels = bead_label_facts(bead["labels"])
        project_id = labels["project_id"]
        plan_id = labels["plan_id"]
        activation = labels["activation"]
    except BeadsError as error:
        _reason(reasons, "invalid_labels", str(error), bead["bead_id"])

    plan = next((item for item in plans if item["plan_id"] == plan_id), None)
    if plan_id is not None:
        if plan is None:
            _reason(
                reasons,
                "plan_unknown",
                f"plan {plan_id!r} is missing or invalid",
                plan_id,
            )
        else:
            activation = plan["activation"]
            if plan["project"] != project_id:
                _reason(
                    reasons,
                    "plan_project_mismatch",
                    "bead and plan project identities differ",
                    bead["bead_id"],
                    plan_id,
                )
            if plan["dependency_cycle"]:
                _reason(
                    reasons,
                    "plan_dependency_cycle",
                    f"plan {plan_id!r} participates in a dependency cycle",
                    plan_id,
                )
            for prerequisite in plan["requires_plans"]:
                completion = plan_completions.get(prerequisite)
                if completion is None:
                    _reason(
                        reasons,
                        "plan_prerequisite_unknown",
                        f"prerequisite plan {prerequisite!r} has no completion facts",
                        prerequisite,
                    )
                elif not completion["complete"]:
                    _reason(
                        reasons,
                        "plan_prerequisite_incomplete",
                        f"prerequisite plan {prerequisite!r} is incomplete",
                        prerequisite,
                    )

            linked = plan_beads.get(plan_id, [])
            if (
                not linked
                or any(not _prepared(item) for item in linked)
                or _active_author(author_progress)
            ):
                _reason(
                    reasons,
                    "awaiting_plan_preparation",
                    "plan intake is empty, incomplete, or still owned by an active author",
                    plan_id,
                )
    elif not _prepared(bead):
        _reason(
            reasons,
            "awaiting_intake_completion",
            "standalone bead content is not implementation-ready",
            bead["bead_id"],
        )

    if activation is None:
        _reason(
            reasons,
            "activation_unknown",
            "activation could not be established",
            bead["bead_id"],
        )
    elif activation == "future":
        _reason(
            reasons,
            "future_activation",
            "work is saved for the future and is not queued",
            plan_id or bead["bead_id"],
        )

    if bead["ready"] is None:
        _reason(
            reasons,
            "beads_readiness_unknown",
            "Beads readiness is unavailable",
            bead["bead_id"],
        )
    elif not bead["ready"]:
        _reason(
            reasons,
            "beads_not_ready",
            "Beads dependencies or lifecycle state are not ready",
            bead["bead_id"],
        )

    scopes = {"global", "fleet", f"bead:{bead['bead_id']}"}
    if project_id is not None:
        scopes.add(f"project:{project_id}")
    if plan_id is not None:
        scopes.add(f"plan:{plan_id}")
    exceptions = {bead["bead_id"], f"bead:{bead['bead_id']}"}
    for hold in holds:
        if (
            hold.get("released_at") is None
            and hold["scope"] in scopes
            and not exceptions.intersection(hold["permitted_exceptions"])
        ):
            _reason(
                reasons,
                "held",
                f"{hold['reason']} Release when: {hold['release_condition']}",
                hold["hold_id"],
            )

    integration_values = [
        integration["project_enabled"],
        integration["repository_available"],
        integration["codex_available"],
        integration["tollgate_available"],
    ]
    if any(value is False for value in integration_values):
        _reason(
            reasons,
            "integration_unavailable",
            "one or more required project integrations are unavailable",
            project_id or bead["bead_id"],
        )
    elif any(value is None for value in integration_values):
        _reason(
            reasons,
            "integration_unknown",
            "project integration health is incomplete",
            project_id or bead["bead_id"],
        )

    owners = [
        assignment["assignment_id"]
        for assignment in assignments
        if assignment["bead_id"] == bead["bead_id"]
    ]
    if owners:
        _reason(
            reasons,
            "already_assigned",
            "work already has assignment ownership",
            *owners,
        )

    if not resources["observations_available"] or resources["compatible"] is None:
        _reason(
            reasons,
            "resources_unknown",
            "resource compatibility has not been observed",
            *resources["reasons"],
        )
    elif not resources["compatible"]:
        _reason(
            reasons,
            "resources_unavailable",
            "current reservations or exclusive work prevent dispatch",
            *resources["reasons"],
        )

    return {
        "bead_id": bead["bead_id"],
        "project_id": project_id,
        "plan_id": plan_id,
        "eligible": not reasons,
        "facts": {
            "beads_ready": bead["ready"],
            "activation": activation,
            "project_integration": integration,
            "resource_compatible": resources["compatible"],
            "assignment_count": len(owners),
        },
        "reasons": reasons,
    }


def reconcile_plan_revision(
    assignment: AssignmentRecord,
    *,
    previous_plan: PlanDocument,
    current_plan: PlanDocument,
    current_plan_commit: str,
) -> AssignmentReconciliation:
    """Route only assignments affected by a changed approved plan revision."""

    affected = assignment["plan_id"] == current_plan["plan_id"]
    if not affected:
        return {
            "assignment_id": assignment["assignment_id"],
            "plan_id": assignment["plan_id"],
            "affected": False,
            "approved_plan_commit": assignment["approved_plan_commit"],
            "current_plan_commit": current_plan_commit,
            "added_prerequisites": [],
            "removed_prerequisites": [],
            "activation_changed": False,
            "body_changed": False,
            "action": "continue",
            "mandate_preserved": True,
            "reasons": [],
        }

    added = sorted(
        set(current_plan["requires_plans"]) - set(previous_plan["requires_plans"])
    )
    removed = sorted(
        set(previous_plan["requires_plans"]) - set(current_plan["requires_plans"])
    )
    activation_changed = previous_plan["activation"] != current_plan["activation"]
    body_changed = previous_plan["body"] != current_plan["body"]
    commit_changed = assignment["approved_plan_commit"] != current_plan_commit
    reasons: list[str] = []
    if added:
        reasons.append("new plan prerequisites require targeted reconciliation")
    if removed:
        reasons.append("removed prerequisites require a recorded scope decision")
    if activation_changed:
        reasons.append("plan activation changed")
    if body_changed:
        reasons.append("plan body changed and must be compared with active scope")
    if current_plan["dependency_cycle"]:
        reasons.append("current plan participates in a dependency cycle")

    action: Literal["continue", "refresh_reference", "pause_and_reconcile"]
    if reasons:
        action = "pause_and_reconcile"
    elif commit_changed:
        action = "refresh_reference"
    else:
        action = "continue"
    return {
        "assignment_id": assignment["assignment_id"],
        "plan_id": assignment["plan_id"],
        "affected": True,
        "approved_plan_commit": assignment["approved_plan_commit"],
        "current_plan_commit": current_plan_commit,
        "added_prerequisites": added,
        "removed_prerequisites": removed,
        "activation_changed": activation_changed,
        "body_changed": body_changed,
        "action": cast(
            Literal["continue", "refresh_reference", "pause_and_reconcile"], action
        ),
        "mandate_preserved": True,
        "reasons": reasons,
    }
