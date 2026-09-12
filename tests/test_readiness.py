"""Infrastructure readiness evidence must fail closed without hiding fallbacks."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from fulcrum.readiness import ReadinessError, evaluate_readiness, load_and_evaluate


class ReadinessTest(unittest.TestCase):
    def test_checked_in_matrix_blocks_task_21_on_required_failures(self) -> None:
        matrix = Path(__file__).parents[1] / "docs" / "readiness-evidence.json"
        result = load_and_evaluate(matrix)
        self.assertFalse(result["ready"])
        self.assertEqual(
            [entry["id"] for entry in result["blockers"]],
            ["human-roles-schedule", "initial-project-registry"],
        )
        self.assertEqual(
            [entry["id"] for entry in result["unsupported_optional"]],
            ["desktop-hooks", "runtime-visibility"],
        )

    def test_all_required_pass_allows_optional_unsupported(self) -> None:
        result = evaluate_readiness(
            {
                "schema_version": 1,
                "entries": [
                    {
                        "id": "required",
                        "requirement": "works",
                        "required": True,
                        "status": "pass",
                        "detail": "verified",
                        "evidence": ["test"],
                    },
                    {
                        "id": "optional",
                        "requirement": "observable",
                        "required": False,
                        "status": "unsupported",
                        "detail": "fallback",
                        "evidence": ["test"],
                    },
                ],
            }
        )
        self.assertTrue(result["ready"])

    def test_required_unsupported_and_duplicate_ids_are_invalid(self) -> None:
        entry = {
            "id": "same",
            "requirement": "works",
            "required": True,
            "status": "unsupported",
            "detail": "unknown",
            "evidence": ["test"],
        }
        with self.assertRaises(ReadinessError):
            evaluate_readiness({"schema_version": 1, "entries": [entry]})
        passing = dict(entry, status="pass")
        with self.assertRaises(ReadinessError):
            evaluate_readiness({"schema_version": 1, "entries": [passing, passing]})


if __name__ == "__main__":
    unittest.main()
