"""Controlled checks for resource observation, holds, and pause/resume."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from typing import cast

from fulcrum.records import HoldsJobsRecord, validate_record
from fulcrum.resources import (
    CommandResult,
    ResourceObservation,
    active_holds,
    checkpoint_is_candidate,
    checkpoint_report,
    collect_resources,
    hold_blocks,
    hold_scope,
    new_hold,
    quiet_report,
    release_hold,
    resume_report,
    run_bounded,
)

NOW = datetime(2026, 9, 11, 20, 0, tzinfo=timezone.utc)


def command_result(output: str = "", *, available: bool = True) -> CommandResult:
    return {
        "available": available,
        "returncode": 0 if available else 1,
        "stdout": output,
        "error": None if available else "unavailable",
        "truncated": False,
    }


def observation_runner(
    command: list[str] | tuple[str, ...], timeout: float, limit: int
) -> CommandResult:
    if command[0] == "memory_pressure":
        return command_result("System-wide memory free percentage: 42%")
    if command[0] == "tg":
        return command_result(json.dumps([]))
    return run_bounded(command, timeout, limit)


class ResourceObservationTest(unittest.TestCase):
    def test_controlled_owned_process_prevents_false_quiet(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"]
        )

        def controlled_runner(
            command: list[str] | tuple[str, ...], timeout: float, limit: int
        ) -> CommandResult:
            if command[0] == "ps":
                if process.poll() is None:
                    return command_result(
                        f"{process.pid} {process.pid - 1} 0.0 1024 controlled-worker"
                    )
                return command_result("")
            return observation_runner(command, timeout, limit)

        try:
            running = collect_resources(
                repository_id="repo-1",
                owned_pids=[process.pid],
                now=NOW,
                run=controlled_runner,
            )
            report = quiet_report(running)
            self.assertFalse(report["quiet"])
            self.assertIn(process.pid, report["live_process_ids"])
        finally:
            process.terminate()
            process.wait(timeout=5)

        drained = collect_resources(
            repository_id="repo-1",
            owned_pids=[process.pid],
            now=NOW,
            run=controlled_runner,
        )
        self.assertTrue(quiet_report(drained)["quiet"])

    def test_missing_measurements_are_explicitly_unavailable(self) -> None:
        def unavailable(
            command: list[str] | tuple[str, ...], timeout: float, limit: int
        ) -> CommandResult:
            del command, timeout, limit
            return command_result(available=False)

        result = collect_resources(now=NOW, run=unavailable)
        self.assertFalse(result["memory_free_percent"]["available"])
        self.assertFalse(result["relevant_processes"]["available"])
        self.assertFalse(result["tollgate"]["available"])
        self.assertFalse(quiet_report(result)["quiet"])

    def test_tollgate_work_must_drain(self) -> None:
        observation = cast(
            ResourceObservation,
            {
                "observed_at": "2026-09-11T20:00:00Z",
                "cpu_load_1m": {
                    "available": True,
                    "value": 1,
                    "unit": "load",
                    "error": None,
                },
                "logical_cpus": {
                    "available": True,
                    "value": 8,
                    "unit": "count",
                    "error": None,
                },
                "memory_free_percent": {
                    "available": True,
                    "value": 50,
                    "unit": "percent",
                    "error": None,
                },
                "relevant_processes": {
                    "available": True,
                    "processes": [],
                    "error": None,
                    "truncated": False,
                },
                "tollgate": {
                    "available": True,
                    "repository_id": "repo-1",
                    "execution_state": "paused",
                    "queue_items": 1,
                    "active_runs": 0,
                    "queued_runs": 0,
                    "error": None,
                    "truncated": False,
                },
            },
        )
        self.assertFalse(quiet_report(observation)["quiet"])


class HoldTest(unittest.TestCase):
    def record(self) -> HoldsJobsRecord:
        return {
            "record_kind": "holds_jobs",
            "schema_version": 1,
            "writer_id": "task-archon",
            "updated_at": "2026-09-11T20:00:00Z",
            "holds": [
                new_hold(
                    "fleet-human",
                    scope=hold_scope("fleet"),
                    reason="Human quiet interval",
                    release_condition="Human explicitly releases it",
                    owner_id="human",
                    release_mode="human",
                ),
                new_hold(
                    "project-build",
                    scope=hold_scope("project", "fulcrum"),
                    reason="Exclusive build",
                    release_condition="Build evidence is retained",
                    owner_id="task-archon",
                    release_mode="evidence",
                    exceptions=[("incident:startup", ["cpu:1", "memory:1g"])],
                ),
            ],
            "recurring_jobs": [],
        }

    def test_overlapping_holds_survive_partial_release(self) -> None:
        record = self.record()
        with self.assertRaisesRegex(ValueError, "by its owner"):
            release_hold(
                record,
                "fleet-human",
                released_at="2026-09-11T20:05:00Z",
                actor_id="task-archon",
                evidence=None,
            )
        released = release_hold(
            record,
            "fleet-human",
            released_at="2026-09-11T20:05:00Z",
            actor_id="human",
            evidence=None,
        )
        active = active_holds(released, ["fleet", "project:fulcrum"])
        self.assertEqual([hold["hold_id"] for hold in active], ["project-build"])

    def test_evidence_release_and_structured_exception_are_enforced(self) -> None:
        record = self.record()
        self.assertEqual(validate_record(record)["record_kind"], "holds_jobs")
        project_hold = record["holds"][1]
        with self.assertRaisesRegex(ValueError, "release evidence"):
            release_hold(
                record,
                "project-build",
                released_at="2026-09-11T20:05:00Z",
                actor_id="task-archon",
                evidence=None,
            )
        self.assertFalse(
            hold_blocks(
                project_hold,
                scopes=["project:fulcrum"],
                recovery_scope="incident:startup",
                requested_resources=["cpu:1"],
            )
        )
        self.assertTrue(
            hold_blocks(
                project_hold,
                scopes=["project:fulcrum"],
                recovery_scope="incident:startup",
                requested_resources=["cpu:2"],
            )
        )


class PauseResumeTest(unittest.TestCase):
    def test_checkpoint_deadlines_and_candidate_boundary(self) -> None:
        checkpoint = checkpoint_report(
            "assignment-1",
            requested_at=NOW,
            recorded_at=NOW,
            dirty_paths=["src/work.py"],
            intended_changes_preserved=True,
            candidate_id="candidate-draft",
            candidate_validated=False,
            owned_process_ids=[99],
        )
        self.assertEqual(checkpoint["inventory_due_at"], "2026-09-11T20:05:00Z")
        self.assertEqual(checkpoint["preservation_due_at"], "2026-09-11T20:10:00Z")
        self.assertFalse(checkpoint_is_candidate(checkpoint))

    def test_resume_requires_release_and_identity_rechecks(self) -> None:
        hold = new_hold(
            "pause",
            scope="assignment:a1",
            reason="Pause",
            release_condition="Explicit release",
            owner_id="task-archon",
            release_mode="human",
        )
        blocked = resume_report(
            applicable=[hold],
            worktree_matches=True,
            contract_matches=True,
            candidate_reconciled=True,
            base_matches=True,
        )
        self.assertFalse(blocked["resumable"])
        unknown = resume_report(
            applicable=[],
            worktree_matches=True,
            contract_matches=None,
            candidate_reconciled=True,
            base_matches=True,
        )
        self.assertFalse(unknown["resumable"])
        self.assertIn("assignment contract is unknown", unknown["reasons"])


if __name__ == "__main__":
    unittest.main()
