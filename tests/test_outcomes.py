from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fulcrum.outcomes import OutcomeError, finish_syntax, validate_outcome


class OutcomesTest(unittest.TestCase):
    def test_concrete_forms(self) -> None:
        self.assertEqual(
            validate_outcome(
                "review",
                "approved",
                {"assessment": "Correct", "allow_repair": ["formatting"]},
            ),
            {"assessment": "Correct", "allow_repair": ["formatting"]},
        )
        self.assertIn("fulcrum finish", finish_syntax("implement"))
        with self.assertRaises(OutcomeError):
            validate_outcome("review", "ready_for_review", {"evidence": "x"})

    def test_report_requires_explicit_evidence_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text(
                json.dumps(
                    {
                        "summary": "No findings",
                        "coverage": ["all source"],
                        "findings": [],
                    }
                )
            )
            self.assertEqual(
                validate_outcome("specialist", "report", {"input": str(path)}),
                {
                    "summary": "No findings",
                    "coverage": ["all source"],
                    "findings": [],
                },
            )
            path.write_text(
                json.dumps(
                    {
                        "summary": "Finding",
                        "coverage": ["source"],
                        "findings": [{"identity": "x", "problem": "x"}],
                    }
                )
            )
            with self.assertRaisesRegex(OutcomeError, "evidence"):
                validate_outcome("specialist", "report", {"input": str(path)})

    def test_unknown_archon_decision_is_rejected_before_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "decisions.json"
            path.write_text(json.dumps({"decisions": [{"decision": "wish"}]}))
            with self.assertRaisesRegex(OutcomeError, "unsupported Archon decision"):
                validate_outcome("archon", "decisions", {"input": str(path)})

    def test_deferral_needs_reactivation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "defer.json"
            path.write_text("{}")
            with self.assertRaises(OutcomeError):
                validate_outcome(
                    "archon", "deferred", {"reason": "later", "input": str(path)}
                )


if __name__ == "__main__":
    unittest.main()
