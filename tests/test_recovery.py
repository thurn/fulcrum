"""Tabletop recovery coverage without disrupting production Tollgate."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

from fulcrum.records import ExecutorEvidenceRecord, ProgressRecord, validate_record
from fulcrum.recovery import (
    build_escalation,
    candidate_failure_action,
    completion_recovery,
    emergency_authorization,
    emergency_next_step,
    investigation_project,
    mark_unchanged_retry,
    record_candidate_failure,
    resume_suspended_investigation,
    suspend_for_investigation,
    unavailable_task_action,
)

SCENARIOS = json.loads(
    (Path(__file__).parent / "fixtures" / "recovery" / "tabletop.json").read_text()
)
NOW = datetime(2026, 9, 11, 21, 0, tzinfo=timezone.utc)


def progress() -> ProgressRecord:
    return {
        "record_kind": "progress",
        "schema_version": 1,
        "writer_id": "task-executor",
        "updated_at": "2026-09-11T20:00:00Z",
        "role_task_id": "task-executor",
        "role": "executor",
        "phase": "investigating",
        "phase_started_at": "2026-09-11T20:00:00Z",
        "expected_next_actor": None,
        "expected_next_action": "Diagnose failure",
        "handoff_needed": False,
        "handoff_sent": False,
        "delivery_error": None,
        "owned_resources": [{"kind": "worktree", "identifier": "/tmp/owned"}],
    }


def evidence() -> ExecutorEvidenceRecord:
    return {
        "record_kind": "executor_evidence",
        "schema_version": 1,
        "writer_id": "task-executor",
        "updated_at": "2026-09-11T20:00:00Z",
        "executor_task_id": "task-executor",
        "worktree_path": "/tmp/owned",
        "base_oid": "0123456789abcdef0123456789abcdef01234567",
        "source_oid": "123456789abcdef0123456789abcdef012345678",
        "candidate_id": "candidate-91",
        "tested_oid": "23456789abcdef0123456789abcdef0123456789",
        "queue_revision": 7,
        "push_state": "pending",
        "cleanup_state": "pending",
        "suspended_investigations": [],
    }


class EscalationTest(unittest.TestCase):
    def test_send_failure_preserves_complete_escalation_and_next_actor(self) -> None:
        scenario = SCENARIOS["send_failure"]
        result = build_escalation(
            progress(),
            boundary=scenario["boundary"],
            error=scenario["error"],
            evidence=[scenario["evidence"]],
            attempts=[
                {
                    "at": "2026-09-11T21:00:00Z",
                    "hypothesis": "task identity may be stale",
                    "action": "inspect task registry",
                    "outcome": "destination remains unavailable",
                    "evidence": "registry:task-overseer",
                }
            ],
            retained_work=["worktree:/tmp/owned", "commit:1234567"],
            untried_recovery=["Archon may provision a replacement"],
            requested_decision="Inspect destination and choose recovery",
            expected_next_actor="task-overseer",
            now="2026-09-11T21:00:00Z",
            delivery_error=scenario["error"],
        )
        self.assertEqual(result["expected_next_actor"], "task-overseer")
        self.assertFalse(result["handoff_sent"])
        self.assertEqual(result["escalation"]["boundary"], scenario["boundary"])
        self.assertEqual(validate_record(result)["record_kind"], "progress")

    def test_repeated_ci_failure_has_one_retry_and_bounded_diagnosis(self) -> None:
        scenario = SCENARIOS["repeated_ci_failure"]
        failed = record_candidate_failure(
            evidence(),
            boundary=scenario["boundary"],
            error_evidence=scenario["evidence"],
            now=NOW,
        )
        event = failed["failure_history"][-1]
        self.assertEqual(
            candidate_failure_action(event, now=NOW, diagnose_complete=False),
            "tg_diagnose",
        )
        self.assertEqual(
            candidate_failure_action(
                event,
                now=NOW,
                diagnose_complete=True,
                retry_hypothesis="transient resource pressure",
            ),
            "retry_once",
        )
        retried = mark_unchanged_retry(
            failed,
            boundary=scenario["boundary"],
            hypothesis="transient resource pressure",
            now="2026-09-11T21:01:00Z",
        )
        with self.assertRaisesRegex(ValueError, "already used"):
            mark_unchanged_retry(
                retried,
                boundary=scenario["boundary"],
                hypothesis="try again",
                now="2026-09-11T21:02:00Z",
            )
        self.assertEqual(
            candidate_failure_action(
                retried["failure_history"][-1],
                now=NOW + timedelta(minutes=16),
                diagnose_complete=True,
            ),
            "repair_rollback_or_escalate",
        )


class InvestigationAndLossTest(unittest.TestCase):
    def test_nested_investigations_preserve_lifo_work_and_history(self) -> None:
        scenario = SCENARIOS["nested_investigation"]
        original = evidence()
        outer = suspend_for_investigation(
            original,
            investigation_task_id=scenario["outer"],
            project_id="fulcrum",
            reason="suspect build runner",
            evidence_reference="buildset-8",
            review_history=[],
            now="2026-09-11T21:00:00Z",
        )
        inner = suspend_for_investigation(
            outer,
            investigation_task_id=scenario["inner"],
            project_id="fulcrum",
            reason="suspect filesystem",
            evidence_reference="diagnose-9",
            review_history=[],
            now="2026-09-11T21:01:00Z",
        )
        resumed, newest = resume_suspended_investigation(
            inner, now="2026-09-11T21:02:00Z"
        )
        self.assertEqual(newest["task_id"], scenario["inner"])
        self.assertEqual(
            resumed["suspended_investigations"][-1]["task_id"], scenario["outer"]
        )
        self.assertEqual(newest["worktree_path"], original["worktree_path"])
        self.assertEqual(investigation_project("product", "tollgate"), "tollgate")

    def test_lost_task_reconciles_promotion_before_replacement(self) -> None:
        retained = SCENARIOS["lost_task"]["retained_commit"]
        self.assertEqual(
            unavailable_task_action(
                original_available=False,
                candidate_promoted=True,
                dirty_work_disposition_known=False,
                retained_commit=retained,
            ),
            "reconcile_promoted_candidate",
        )
        self.assertEqual(
            unavailable_task_action(
                original_available=False,
                candidate_promoted=False,
                dirty_work_disposition_known=True,
                retained_commit=retained,
            ),
            "fresh_worktree_from_retained_commit",
        )

    def test_source_push_failure_is_recovery_not_implementation_redo(self) -> None:
        failed = evidence()
        failed["push_state"] = "failed"
        self.assertEqual(completion_recovery(failed), "retry_source_push")


class EmergencyTest(unittest.TestCase):
    def test_tollgate_outage_uses_reviewed_provisional_then_normal_gate(self) -> None:
        scenario = SCENARIOS["tollgate_worktree_outage"]
        authorization = emergency_authorization(
            outage_evidence=scenario["evidence"],
            attempted_recovery=["diagnose service", "restart supported service"],
            scope="tollgate:worktree-create",
            repair_owner="task-executor-tollgate",
            permitted_runtime_changes=["install reviewed provisional binary"],
            certified_release_oid=scenario["certified_release"],
        )
        retained = evidence()
        retained["emergency_authorization"] = authorization
        self.assertEqual(validate_record(retained)["record_kind"], "executor_evidence")
        self.assertEqual(
            emergency_next_step(
                service_restored=False,
                provisional_reviewed=True,
                normally_certified=False,
                installed_version_matches=False,
            ),
            "install_reviewed_provisional_repair",
        )
        self.assertEqual(
            emergency_next_step(
                service_restored=True,
                provisional_reviewed=True,
                normally_certified=False,
                installed_version_matches=False,
            ),
            "certify_normally",
        )
        self.assertEqual(
            emergency_next_step(
                service_restored=True,
                provisional_reviewed=True,
                normally_certified=True,
                installed_version_matches=True,
            ),
            "cleanup_emergency_resources",
        )

    def test_emergency_scope_cannot_apply_to_normal_feature_work(self) -> None:
        with self.assertRaisesRegex(ValueError, "owned by Tollgate"):
            emergency_authorization(
                outage_evidence="outage",
                attempted_recovery=["restart"],
                scope="product:feature",
                repair_owner="task-executor",
                permitted_runtime_changes=[],
                certified_release_oid="0" * 40,
            )


if __name__ == "__main__":
    unittest.main()
