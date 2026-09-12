"""Infrastructure readiness evidence must fail closed without hiding fallbacks."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from fulcrum.readiness import (
    ReadinessError,
    apply_doctor_evidence,
    evaluate_readiness,
    load_and_evaluate,
)


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

    def test_live_doctor_can_clear_only_runtime_dependent_rows(self) -> None:
        matrix = json.loads(
            (Path(__file__).parents[1] / "docs" / "readiness-evidence.json").read_text()
        )
        doctor = {
            "ready": True,
            "required_failures": [],
            "checks": [
                {"name": "human_role_enrollment", "status": "pass", "detail": "two"},
                {"name": "watchman_hourly_schedule", "status": "pass", "detail": "one"},
                {
                    "name": "initial_project_integrations",
                    "status": "pass",
                    "detail": "three",
                },
                {
                    "name": "desktop_hook_delivery",
                    "status": "pass",
                    "detail": "trusted",
                },
                {
                    "name": "runtime_observation",
                    "status": "unavailable",
                    "detail": "fallback",
                },
            ],
        }
        result = apply_doctor_evidence(
            matrix, doctor, observed_at="2026-09-11T21:00:00Z"
        )
        self.assertTrue(result["ready"])
        self.assertFalse(result["baseline_ready"])
        doctor["ready"] = False
        doctor["required_failures"] = [{"name": "holds_jobs_state"}]
        failed = apply_doctor_evidence(matrix, doctor)
        self.assertFalse(failed["ready"])
        self.assertEqual(failed["blockers"][-1]["id"], "live-doctor")

        doctor["ready"] = False
        doctor["required_failures"] = [{"name": "watchman_first_patrol"}]
        failed = apply_doctor_evidence(matrix, doctor)
        self.assertFalse(failed["ready"])
        self.assertEqual(failed["blockers"][-1]["id"], "live-doctor")


if __name__ == "__main__":
    unittest.main()
