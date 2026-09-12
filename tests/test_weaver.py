"""Exercise authored intake across document and eligibility boundaries."""

import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fulcrum.beads import BeadDraft, ensure_bead
from fulcrum.documents import parse_plan
from fulcrum.eligibility import summarize_eligibility, reconcile_plan_revision
from test_eligibility import bead, integration, resources, assignment


def intake(plan_id=None):
    return BeadDraft(
        intake_key="missing-handoff",
        title="Retain failed review delivery",
        project_id="fulcrum",
        problem="A failed review send can appear complete.",
        outcome="The Executor retains a visible retry obligation.",
        bounded_scope="Progress delivery fields only; no retry scheduler.",
        context="Repository skills/fulcrum-shared/handoffs.md; approved review-contract plan.",
        dependencies=(),
        acceptance_criteria="A failed send keeps handoff_sent false and an error; a confirmed send records the intended Overseer.",
        validation="Exercise send failure and inspected delivery success against disposable state.",
        plan_id=plan_id,
        activation=None if plan_id else "queued",
        issue_type="bug",
    )


class WeaverWorkflowTests(unittest.TestCase):
    def test_future_and_queued_approval_and_active_refinement(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plans/fulcrum/review-contract.md"
            path.parent.mkdir(parents=True)
            for activation in ("future", "queued"):
                path.write_text(
                    f"---\nplan_id: review-contract\nproject: fulcrum\nactivation: {activation}\nrequires_plans: []\n---\n# Review delivery\nRetain failed handoffs.\n"
                )
                document = parse_plan(path, {"fulcrum"})
                draft = intake(document["plan_id"])
                issue = bead(labels=list(draft.labels))
                issue.update(
                    description=draft.description(),
                    acceptance_criteria=draft.acceptance_criteria,
                )
                result = summarize_eligibility(
                    issue,
                    plans=[document],
                    plan_beads={document["plan_id"]: [issue]},
                    plan_completions={},
                    holds=[],
                    integration=integration(),
                    assignments=[],
                    resources=resources(),
                )
                self.assertEqual(result["eligible"], activation == "queued")
                # Discovery/intake never dispatches; eligibility is only an explanation.
                owner = assignment(plan_id=document["plan_id"])
                before = copy.deepcopy(owner)
                revised = dict(
                    document, body=document["body"] + "Escalate unresolved delivery."
                )
                change = reconcile_plan_revision(
                    owner,
                    previous_plan=document,
                    current_plan=revised,
                    current_plan_commit="new-approved-commit",
                )
                self.assertEqual(change["action"], "pause_and_reconcile")
                self.assertEqual(owner, before)

    @patch("fulcrum.beads.run_beads")
    def test_direct_bug_intake_recovers_interrupted_creation(self, run):
        draft = intake()
        run.side_effect = [
            [],
            {"id": "brain-bug"},
            [
                {
                    "id": "brain-bug",
                    "external_ref": draft.external_reference,
                    "labels": list(draft.labels),
                }
            ],
        ]
        first = ensure_bead(Path("/disposable-brain"), draft)
        self.assertEqual(ensure_bead(Path("/disposable-brain"), draft), first)
        self.assertEqual(
            sum(call.args[1][0] == "create" for call in run.call_args_list), 1
        )
        self.assertIsNone(draft.plan_id)
        issue = bead(labels=list(draft.labels))
        issue.update(
            description=draft.description(),
            acceptance_criteria=draft.acceptance_criteria,
        )
        self.assertTrue(
            summarize_eligibility(
                issue,
                plans=[],
                plan_beads={},
                plan_completions={},
                holds=[],
                integration=integration(),
                assignments=[],
                resources=resources(),
            )["eligible"]
        )
