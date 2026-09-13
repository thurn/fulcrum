from __future__ import annotations

import unittest

from fulcrum.outcomes import OutcomeError, finish_syntax, validate_outcome


class OutcomesTest(unittest.TestCase):
    def test_concrete_forms(self) -> None:
        approval = {
            "assessment": "Correct",
            "minor_fixes": [
                {
                    "problem": "Awkward name",
                    "evidence": "module.py:10",
                    "requested_change": "Use the domain term next time",
                }
            ],
            "repair_permissions": ["formatting"],
        }
        self.assertEqual(
            validate_outcome("review", "approved", {"input": approval}), approval
        )
        self.assertIn("fulcrum finish", finish_syntax("implement"))
        with self.assertRaises(OutcomeError):
            validate_outcome("review", "ready_for_review", {"evidence": "x"})

    def test_approval_requires_explicit_well_formed_minor_fixes(self) -> None:
        for content in (
            {"assessment": "Correct"},
            {"assessment": "Correct", "minor_fixes": [{}]},
            {
                "assessment": "Correct",
                "minor_fixes": [],
                "repair_permissions": [""],
            },
        ):
            with self.assertRaises(OutcomeError):
                validate_outcome("review", "approved", {"input": content})

    def test_report_requires_explicit_evidence_fields(self) -> None:
        report = {
            "summary": "No findings",
            "coverage": ["all source"],
            "findings": [],
        }
        self.assertEqual(
            validate_outcome("specialist", "report", {"input": report}), report
        )
        invalid = {
            "summary": "Finding",
            "coverage": ["source"],
            "findings": [{"identity": "x", "problem": "x"}],
        }
        with self.assertRaisesRegex(OutcomeError, "evidence"):
            validate_outcome("specialist", "report", {"input": invalid})

    def test_unknown_archon_decision_is_rejected_before_acceptance(self) -> None:
        with self.assertRaisesRegex(OutcomeError, "unsupported Archon decision"):
            validate_outcome(
                "archon",
                "decisions",
                {"input": {"decisions": [{"decision": "wish"}]}},
            )

    def test_deferral_needs_reactivation(self) -> None:
        with self.assertRaises(OutcomeError):
            validate_outcome("archon", "deferred", {"reason": "later", "input": {}})


if __name__ == "__main__":
    unittest.main()
