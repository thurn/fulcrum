"""Scenario coverage for eligibility and active-plan reconciliation."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from typing import cast

from fulcrum.beads import BeadDraft
from fulcrum.documents import PlanDocument
from fulcrum.eligibility import (
    BeadSnapshot,
    PlanCompletion,
    ProjectIntegration,
    ResourceFacts,
    plan_completion,
    reconcile_plan_revision,
    summarize_eligibility,
)
from fulcrum.records import AssignmentRecord, Hold

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "eligibility" / "scenarios.json").read_text()
)


def description() -> str:
    return BeadDraft(
        intake_key="fixture",
        title="Fixture",
        project_id="fulcrum",
        problem="A scenario needs evaluation.",
        outcome="The facts receive an explanation.",
        bounded_scope="Read-only evaluation.",
        context="tests/fixtures/eligibility/scenarios.json",
        dependencies=(),
        acceptance_criteria="The expected reason is present.",
        validation="Run the scenario tests.",
        activation="queued",
    ).description()


def bead(
    bead_id: str = "brain-work",
    *,
    labels: list[str] | None = None,
    status: str = "open",
    ready: bool | None = True,
    work_kind: str = "code",
    prepared: bool = True,
    required: bool = True,
    scope_decision: str | None = None,
    evidence: dict[str, object] | None = None,
) -> BeadSnapshot:
    return cast(
        BeadSnapshot,
        {
            "bead_id": bead_id,
            "title": "Scenario work",
            "labels": labels or ["project:fulcrum", "plan:queued-plan"],
            "status": status,
            "ready": ready,
            "work_kind": work_kind,
            "description": description() if prepared else "Incomplete intake",
            "acceptance_criteria": "Expected behavior" if prepared else None,
            "required": required,
            "scope_decision": scope_decision,
            "completion_evidence": evidence,
        },
    )


def plan(
    plan_id: str = "queued-plan",
    *,
    project: str = "fulcrum",
    activation: str = "queued",
    requires: list[str] | None = None,
    cycle: bool = False,
    body: str = "# Plan\n\nApproved scope.\n",
) -> PlanDocument:
    return cast(
        PlanDocument,
        {
            "path": f"/brain/plans/{project}/{plan_id}.md",
            "plan_id": plan_id,
            "project": project,
            "activation": activation,
            "requires_plans": requires or [],
            "dependency_cycle": cycle,
            "body": body,
        },
    )


def integration(**changes: bool | None) -> ProjectIntegration:
    value: ProjectIntegration = {
        "project_enabled": True,
        "repository_available": True,
        "codex_available": True,
        "tollgate_available": True,
    }
    value.update(changes)  # pyre-ignore[6]
    return value


def resources(
    *, available: bool = True, compatible: bool | None = True
) -> ResourceFacts:
    return {
        "observations_available": available,
        "compatible": compatible,
        "reasons": [] if compatible is True else ["full build slot occupied"],
    }


def assignment(
    assignment_id: str = "assignment-1", plan_id: str = "active-plan"
) -> AssignmentRecord:
    return {
        "record_kind": "assignment",
        "schema_version": 1,
        "writer_id": "task-overseer",
        "updated_at": "2026-09-11T23:00:00Z",
        "assignment_id": assignment_id,
        "bead_id": "brain-work",
        "plan_id": plan_id,
        "approved_plan_commit": FIXTURE["refinement"]["approved_commit"],
        "executor_task_id": "task-executor",
        "overseer_task_id": "task-overseer",
        "scope_reference": "plans/fulcrum/active-plan.md#scope",
        "review_history": [],
        "mandate": {
            "candidate_id": "candidate-1",
            "scope": "Approved scope",
            "granted_at": "2026-09-11T23:00:00Z",
        },
    }


class EligibilityScenarioTest(unittest.TestCase):
    def summarize(
        self,
        target: BeadSnapshot,
        *,
        plans: list[PlanDocument] | None = None,
        plan_beads: dict[str, list[BeadSnapshot]] | None = None,
        completions: dict[str, PlanCompletion] | None = None,
        holds: list[Hold] | None = None,
        assignments: list[AssignmentRecord] | None = None,
        project_integration: ProjectIntegration | None = None,
        resource_facts: ResourceFacts | None = None,
    ) -> dict[str, object]:
        return summarize_eligibility(
            target,
            plans=plans or [plan()],
            plan_beads=plan_beads or {"queued-plan": [target]},
            plan_completions=completions or {},
            holds=holds or [],
            integration=project_integration or integration(),
            assignments=assignments or [],
            resources=resource_facts or resources(),
        )

    def codes(self, summary: dict[str, object]) -> list[str]:
        return [reason["code"] for reason in summary["reasons"]]  # type: ignore[index]

    def test_queued_and_future_activation_remain_distinct(self) -> None:
        queued = bead()
        self.assertTrue(self.summarize(queued)["eligible"])

        future = bead(
            FIXTURE["queued_vs_future"]["future_standalone"],
            labels=["project:fulcrum", "activation:future"],
        )
        summary = self.summarize(future, plans=[], plan_beads={})
        self.assertFalse(summary["eligible"])
        self.assertIn("future_activation", self.codes(summary))

    def test_composed_holds_are_reported_independently(self) -> None:
        summary = self.summarize(
            bead(), holds=cast(list[Hold], FIXTURE["composed_holds"])
        )
        self.assertEqual(self.codes(summary).count("held"), 2)

    def test_cross_project_prerequisite_must_be_complete(self) -> None:
        requirement = FIXTURE["cross_project_prerequisite"]["requires"]
        target_plan = plan(
            FIXTURE["cross_project_prerequisite"]["plan_id"],
            project="battlement",
            requires=[requirement],
        )
        target = bead(labels=["project:battlement", f"plan:{target_plan['plan_id']}"])
        incomplete: PlanCompletion = {
            "plan_id": requirement,
            "complete": False,
            "reasons": ["required bead is canceled"],
            "contradictory_closures": [],
        }
        summary = self.summarize(
            target,
            plans=[target_plan],
            plan_beads={target_plan["plan_id"]: [target]},
            completions={requirement: incomplete},
        )
        self.assertIn("plan_prerequisite_incomplete", self.codes(summary))

    def test_cycle_and_partial_intake_each_block_dispatch(self) -> None:
        cycle_plan = plan(FIXTURE["cycle"][0], cycle=True)
        cycle_bead = bead(labels=["project:fulcrum", f"plan:{cycle_plan['plan_id']}"])
        cycle_summary = self.summarize(
            cycle_bead,
            plans=[cycle_plan],
            plan_beads={cycle_plan["plan_id"]: [cycle_bead]},
        )
        self.assertIn("plan_dependency_cycle", self.codes(cycle_summary))

        partial_plan = plan(FIXTURE["partial_intake"]["plan_id"])
        partial_bead = bead(
            labels=["project:fulcrum", f"plan:{partial_plan['plan_id']}"],
            prepared=False,
        )
        partial_summary = self.summarize(
            partial_bead,
            plans=[partial_plan],
            plan_beads={partial_plan["plan_id"]: [partial_bead]},
        )
        self.assertIn("awaiting_plan_preparation", self.codes(partial_summary))

    def test_plan_eligibility_does_not_depend_on_weaver_progress(self) -> None:
        target_plan = plan()
        summary = self.summarize(
            bead(),
            plans=[target_plan],
            plan_beads={target_plan["plan_id"]: [bead()]},
        )
        self.assertTrue(summary["eligible"])

    def test_unknown_integration_and_resources_never_grant_eligibility(self) -> None:
        summary = self.summarize(
            bead(),
            project_integration=integration(codex_available=None),
            resource_facts=resources(available=False, compatible=None),
        )
        self.assertFalse(summary["eligible"])
        self.assertIn("integration_unknown", self.codes(summary))
        self.assertIn("resources_unknown", self.codes(summary))

    def test_summary_is_read_only_and_reports_existing_assignment(self) -> None:
        target = bead()
        owned = assignment(plan_id="queued-plan")
        inputs = {
            "target": target,
            "plans": [plan()],
            "plan_beads": {"queued-plan": [target]},
            "assignments": [owned],
        }
        before = copy.deepcopy(inputs)
        summary = self.summarize(
            target,
            plans=inputs["plans"],
            plan_beads=inputs["plan_beads"],
            assignments=inputs["assignments"],
        )
        self.assertIn("already_assigned", self.codes(summary))
        self.assertEqual(inputs, before)


class CompletionScenarioTest(unittest.TestCase):
    def test_canceled_and_empty_plans_do_not_complete(self) -> None:
        canceled = bead(
            FIXTURE["canceled_prerequisite"],
            status="canceled",
            work_kind="non_code",
        )
        canceled_result = plan_completion(plan("prerequisite"), [canceled])
        self.assertFalse(canceled_result["complete"])
        self.assertIn("canceled", canceled_result["reasons"][0])

        empty_result = plan_completion(plan(FIXTURE["empty_plan"]), [])
        self.assertFalse(empty_result["complete"])
        self.assertIn("no required beads", empty_result["reasons"][0])

    def test_code_closure_requires_promotion_push_and_cleanup(self) -> None:
        closed = bead(
            status="closed",
            evidence={
                "certified_promotion": True,
                "source_synchronized": False,
                "cleanup_complete": True,
                "reference": "candidate-1",
            },
        )
        result = plan_completion(plan(), [closed])
        self.assertFalse(result["complete"])
        self.assertIn("source synchronization", result["contradictory_closures"][0])

    def test_removed_requirement_needs_scope_decision(self) -> None:
        removed = bead(required=False, scope_decision=None, status="canceled")
        result = plan_completion(plan(), [removed])
        self.assertFalse(result["complete"])
        self.assertTrue(
            any(
                "scope decision" in reason
                for reason in result["contradictory_closures"]
            )
        )


class ReconciliationScenarioTest(unittest.TestCase):
    def test_refinement_targets_only_affected_assignment_and_preserves_mandate(
        self,
    ) -> None:
        scenario = FIXTURE["refinement"]
        previous = plan(scenario["plan_id"], body="# Plan\n\nOriginal scope.\n")
        current = plan(
            scenario["plan_id"],
            requires=[scenario["added_prerequisite"]],
            body="# Plan\n\nRefined scope.\n",
        )
        active = assignment(plan_id=scenario["plan_id"])
        result = reconcile_plan_revision(
            active,
            previous_plan=previous,
            current_plan=current,
            current_plan_commit=scenario["current_commit"],
        )
        self.assertEqual(result["action"], "pause_and_reconcile")
        self.assertEqual(
            result["added_prerequisites"], [scenario["added_prerequisite"]]
        )
        self.assertTrue(result["mandate_preserved"])
        self.assertEqual(active["mandate"]["candidate_id"], "candidate-1")  # type: ignore[index]

        unaffected = assignment("assignment-2", plan_id="other-plan")
        untouched = reconcile_plan_revision(
            unaffected,
            previous_plan=previous,
            current_plan=current,
            current_plan_commit=scenario["current_commit"],
        )
        self.assertEqual(untouched["action"], "continue")
        self.assertFalse(untouched["affected"])


if __name__ == "__main__":
    unittest.main()
