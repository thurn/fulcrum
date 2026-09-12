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
            path.write_text(json.dumps({"findings": []}))
            self.assertEqual(
                validate_outcome("specialist", "report", {"input": str(path)}),
                {"findings": []},
            )
            path.write_text(json.dumps({"findings": [{"problem": "x"}]}))
            with self.assertRaisesRegex(OutcomeError, "evidence"):
                validate_outcome("specialist", "report", {"input": str(path)})

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
