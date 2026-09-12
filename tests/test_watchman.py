"""Fake-clock patrol and recurring-work tests."""

from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from fulcrum.records import (
    ExecutorEvidenceRecord,
    HoldsJobsRecord,
    ProgressRecord,
    ProjectRegistryRecord,
    RoleRun,
    RoleRunRegistryRecord,
)
from fulcrum.watchman import (
    WATCHMAN_AUTOMATION_NAME,
    WATCHMAN_AUTOMATION_PROMPT,
    AutomationObservation,
    CandidateObservation,
    TaskObservation,
    complete_recurring_job,
    due_jobs,
    ensure_recurring_jobs,
    patrol,
    start_recurring_job,
    watchman_automation_plan,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "watchman" / "patrol.json").read_text()
)


def moment(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def role(name: str, task_id: str | None, *, identity: str = "resolved") -> RoleRun:
    return cast(
        RoleRun,
        {
            "role": name,
            "project_id": "fulcrum" if name != "night_watchman" else "fleet",
            "host_id": "local",
            "task_id": task_id,
            "identity_state": identity,
            "role_number": 7 if name in {"executor", "overseer"} else None,
            "run_id": f"run-{name}",
            "pair_id": "pair-7" if name in {"executor", "overseer"} else None,
            "title": name.replace("_", " ").title(),
            "selected_model": "gpt-test",
            "selected_reasoning": "high",
            "model_authorization": {
                "source": "human" if name == "night_watchman" else "default",
                "reference": "human setup" if name == "night_watchman" else None,
            },
        },
    )


def registries() -> tuple[RoleRunRegistryRecord, ProjectRegistryRecord]:
    roles: RoleRunRegistryRecord = {
        "record_kind": "role_run_registry",
        "schema_version": 1,
        "writer_id": FIXTURE["archon_task_id"],
        "updated_at": "2026-09-11T20:00:00Z",
        "current_archon_task_id": FIXTURE["archon_task_id"],
        "roles": [
            role("archon", FIXTURE["archon_task_id"]),
            role("night_watchman", FIXTURE["watchman_task_id"]),
            role("executor", FIXTURE["executor_task_id"]),
        ],
    }
    projects: ProjectRegistryRecord = {
        "record_kind": "project_registry",
        "schema_version": 1,
        "writer_id": FIXTURE["archon_task_id"],
        "updated_at": "2026-09-11T20:00:00Z",
        "projects": [
            {
                "project_id": "fulcrum",
                "repo_path": "/projects/fulcrum",
                "host_id": "local",
                "codex_project_id": "codex-fulcrum",
                "tollgate_repo_id": "tg-fulcrum",
                "enabled": True,
            },
            {
                "project_id": "disabled",
                "repo_path": "/projects/disabled",
                "host_id": "local",
                "codex_project_id": "codex-disabled",
                "tollgate_repo_id": "tg-disabled",
                "enabled": False,
            },
        ],
    }
    return roles, projects


def jobs() -> HoldsJobsRecord:
    empty: HoldsJobsRecord = {
        "record_kind": "holds_jobs",
        "schema_version": 1,
        "writer_id": FIXTURE["archon_task_id"],
        "updated_at": "2026-09-11T08:00:00Z",
        "holds": [],
        "recurring_jobs": [],
    }
    return ensure_recurring_jobs(
        empty,
        sage_anchor=moment(FIXTURE["sage_anchor"]),
        enabled_project_ids=["fulcrum", "disabled"],
        now="2026-09-11T08:00:00Z",
    )


def executor_progress(*, expected_by: str | None = None) -> ProgressRecord:
    result: ProgressRecord = {
        "record_kind": "progress",
        "schema_version": 1,
        "writer_id": FIXTURE["executor_task_id"],
        "updated_at": "2026-09-11T20:00:00Z",
        "role_task_id": FIXTURE["executor_task_id"],
        "role": "executor",
        "phase": "reviewing",
        "phase_started_at": "2026-09-11T20:00:00Z",
        "expected_next_actor": "task-overseer-7",
        "expected_next_action": "Review candidate",
        "handoff_needed": True,
        "handoff_sent": True,
        "delivery_error": None,
        "owned_resources": [],
    }
    if expected_by is not None:
        result["expected_by"] = expected_by
    return result


def task_observation(state: str = "idle") -> TaskObservation:
    return cast(
        TaskObservation,
        {
            "available": True,
            "state": state,
            "observed_at": FIXTURE["patrol_time"],
            "detail": None,
        },
    )


class RecurringWorkTest(unittest.TestCase):
    def test_offset_overlap_downtime_and_disabled_project(self) -> None:
        record = jobs()
        by_id = {job["job_id"]: job for job in record["recurring_jobs"]}
        self.assertEqual(by_id["sage:fleet"]["next_due"], "2026-09-11T08:00:00Z")
        self.assertEqual(
            by_id["inquisitor:fulcrum"]["next_due"], "2026-09-11T20:00:00Z"
        )

        due = due_jobs(
            record,
            enabled_project_ids=["fulcrum"],
            now=moment(FIXTURE["patrol_time"]),
        )
        self.assertEqual(
            [job["job_id"] for job in due], ["sage:fleet", "inquisitor:fulcrum"]
        )
        self.assertNotIn("inquisitor:disabled", [job["job_id"] for job in due])

        started = start_recurring_job(
            record,
            job_id="sage:fleet",
            task_id="task-sage-catchup",
            now=moment(FIXTURE["patrol_time"]),
        )
        sage = next(
            job for job in started["recurring_jobs"] if job["job_id"] == "sage:fleet"
        )
        self.assertEqual(sage["next_due"], "2026-09-13T08:00:00Z")
        with self.assertRaisesRegex(ValueError, "active task"):
            start_recurring_job(
                started,
                job_id="sage:fleet",
                task_id="task-sage-overlap",
                now=moment(FIXTURE["patrol_time"]),
            )
        completed = complete_recurring_job(
            started,
            job_id="sage:fleet",
            task_id="task-sage-catchup",
            now="2026-09-12T10:00:00Z",
        )
        self.assertFalse(
            due_jobs(
                completed,
                enabled_project_ids=["fulcrum"],
                now=moment("2026-09-12T10:00:00Z"),
            )[0]["job_id"]
            == "sage:fleet"
        )

    def test_job_installation_is_idempotent(self) -> None:
        record = jobs()
        repeated = ensure_recurring_jobs(
            record,
            sage_anchor=moment(FIXTURE["sage_anchor"]),
            enabled_project_ids=["fulcrum", "disabled"],
            now="2026-09-11T09:00:00Z",
        )
        self.assertEqual(len(repeated["recurring_jobs"]), 3)


class PatrolTest(unittest.TestCase):
    def run_patrol(
        self,
        progress_records: list[ProgressRecord],
        observations: dict[str, TaskObservation],
        *,
        previous: list[dict[str, object]] | None = None,
        candidates: list[CandidateObservation] | None = None,
        evidence: list[ExecutorEvidenceRecord] | None = None,
        jobs_record: HoldsJobsRecord | None = None,
    ) -> dict[str, object]:
        roles, projects = registries()
        return patrol(
            role_registry=roles,
            project_registry=projects,
            progress_records=progress_records,
            task_observations=observations,
            candidate_observations=candidates or [],
            executor_evidence=evidence or [],
            jobs_record=jobs_record
            or {
                "record_kind": "holds_jobs",
                "schema_version": 1,
                "writer_id": FIXTURE["archon_task_id"],
                "updated_at": "2026-09-11T20:00:00Z",
                "holds": [],
                "recurring_jobs": [],
            },
            previous_conditions=cast(list, previous or []),
            tollgate_observation_available=True,
            now=moment(FIXTURE["patrol_time"]),
        )

    def test_real_anomaly_targets_registered_archon_and_deduplicates(self) -> None:
        stopped = executor_progress()
        stopped["handoff_sent"] = False
        inputs = {
            "progress": [stopped],
            "observations": {FIXTURE["executor_task_id"]: task_observation()},
        }
        before = copy.deepcopy(inputs)
        first = self.run_patrol(inputs["progress"], inputs["observations"])
        self.assertEqual(first["archon_task_id"], FIXTURE["archon_task_id"])
        self.assertTrue(first["notify_archon"])
        self.assertFalse(first["notify_human"])
        self.assertEqual(inputs, before)

        repeated = self.run_patrol(
            inputs["progress"],
            inputs["observations"],
            previous=first["current_conditions"],  # type: ignore[arg-type]
        )
        self.assertFalse(repeated["notify_archon"])
        self.assertEqual(repeated["notifications"], [])

        resolved = self.run_patrol(
            inputs["progress"],
            {FIXTURE["executor_task_id"]: task_observation("running")},
            previous=first["current_conditions"],  # type: ignore[arg-type]
        )
        self.assertEqual(resolved["notifications"][0]["change"], "resolved")  # type: ignore[index]

    def test_healthy_idle_wait_is_quiet_until_recorded_deadline(self) -> None:
        healthy = self.run_patrol(
            [executor_progress()],
            {FIXTURE["executor_task_id"]: task_observation()},
        )
        self.assertFalse(healthy["notify_archon"])

        overdue = self.run_patrol(
            [executor_progress(expected_by="2026-09-12T08:00:00Z")],
            {FIXTURE["executor_task_id"]: task_observation()},
        )
        codes = [
            item["condition"]["code"] for item in overdue["notifications"]  # type: ignore[index]
        ]
        self.assertIn("expected_wait_overdue", codes)

    def test_unavailable_observation_and_failed_push_report_uncertainty_and_recovery(
        self,
    ) -> None:
        unavailable = task_observation()
        unavailable["available"] = False
        failed = cast(
            ExecutorEvidenceRecord,
            {
                "record_kind": "executor_evidence",
                "schema_version": 1,
                "writer_id": FIXTURE["executor_task_id"],
                "updated_at": "2026-09-11T20:00:00Z",
                "executor_task_id": FIXTURE["executor_task_id"],
                "worktree_path": "/tmp/worktree",
                "base_oid": "0" * 40,
                "source_oid": "1" * 40,
                "candidate_id": "candidate-1",
                "tested_oid": "2" * 40,
                "queue_revision": 1,
                "push_state": "failed",
                "cleanup_state": "pending",
                "suspended_investigations": [],
            },
        )
        result = self.run_patrol(
            [executor_progress()],
            {FIXTURE["executor_task_id"]: unavailable},
            evidence=[failed],
        )
        codes = [
            item["condition"]["code"] for item in result["notifications"]  # type: ignore[index]
        ]
        self.assertIn("task_observation_unavailable", codes)
        self.assertIn("source_push_failed", codes)


class AutomationTest(unittest.TestCase):
    def test_schedule_rerun_does_not_duplicate_hourly_wake(self) -> None:
        watchman = role("night_watchman", FIXTURE["watchman_task_id"])
        create = watchman_automation_plan(watchman, [])
        self.assertEqual(create["action"], "create")
        installed: AutomationObservation = {
            "automation_id": "automation-1",
            "name": WATCHMAN_AUTOMATION_NAME,
            "target_task_id": FIXTURE["watchman_task_id"],
            "cadence": "hourly",
            "prompt": WATCHMAN_AUTOMATION_PROMPT,
            "active": True,
        }
        repeated = watchman_automation_plan(watchman, [installed])
        self.assertEqual(repeated["action"], "none")
        self.assertEqual(repeated["automation_id"], "automation-1")

    def test_schedule_requires_human_created_resolved_watchman(self) -> None:
        pending = role("night_watchman", None, identity="pending")
        with self.assertRaisesRegex(ValueError, "human-created Watchman"):
            watchman_automation_plan(pending, [])


if __name__ == "__main__":
    unittest.main()
