"""Repeat findings update evidence while preserving active contracts."""

import copy
from pathlib import Path
import unittest
from unittest.mock import patch

from fulcrum.findings import Finding, publish_finding
from fulcrum.beads import BeadsError


def finding():
    return Finding(
        project_id="fulcrum",
        problem_key="redundant-context",
        title="Bound repeated context",
        evidence="Synthetic run: three repeated full-context reads; token timing unavailable",
        impact="Fewer repeated reads; no measured speedup is claimed",
        proposed_change="Select role context once per turn and refresh on contract change",
        acceptance="Unchanged turns reuse bounded context; changed assignments refresh it",
        interfaces="context reader and role entry points",
        validation="Measure calls and elapsed time on matched runs",
    )


class FindingTests(unittest.TestCase):
    @patch("fulcrum.findings.run_beads")
    def test_semantic_match_preserves_active_assignment_and_deduplicates(self, run):
        value = finding()
        issue = dict(
            id="brain-existing",
            external_ref="older-key",
            labels=["project:fulcrum", "activation:queued"],
            status="in_progress",
            description="Approved scope",
            notes="",
            assignee="executor",
        )
        before = copy.deepcopy(issue)
        run.side_effect = [[issue], {}]
        self.assertEqual(
            publish_finding(Path("/brain"), value, matching_issue_id="brain-existing"),
            "brain-existing",
        )
        command = run.call_args_list[-1].args[1]
        self.assertEqual(command[:3], ["update", "brain-existing", "--append-notes"])
        self.assertEqual(issue, before)
        issue["notes"] = command[3]
        run.reset_mock()
        run.side_effect = [[issue]]
        publish_finding(Path("/brain"), value, matching_issue_id="brain-existing")
        self.assertEqual(run.call_count, 1)

    @patch("fulcrum.findings.ensure_bead", return_value="brain-new")
    @patch("fulcrum.findings.run_beads", return_value=[])
    def test_new_findings_are_future(self, run, ensure):
        self.assertEqual(publish_finding(Path("/brain"), finding()), "brain-new")
        draft = ensure.call_args.args[1]
        self.assertIn("activation:future", draft.labels)
        self.assertIn("Expected benefit (not a measured result)", draft.description())

    @patch("fulcrum.findings.run_beads")
    def test_wrong_project_terminal_or_ambiguous_match_refused(self, run):
        draft = finding().draft()
        good = dict(
            id="brain-one",
            external_ref=draft.external_reference,
            labels=["project:fulcrum"],
            status="open",
        )
        for issues in (
            [dict(good, labels=["project:other"])],
            [dict(good, status="closed")],
            [good, dict(good, id="brain-two")],
        ):
            run.return_value = issues
            with self.assertRaises(BeadsError):
                publish_finding(Path("/brain"), finding())
