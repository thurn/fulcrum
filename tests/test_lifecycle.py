from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fulcrum.lifecycle import (
    accept_finish,
    apply_archon_decisions,
    observe_action_terminal,
)
from fulcrum.store import Store, StoreError


class LifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "state.db")
        now = "2026-01-01T00:00:00Z"
        self.store.execute(
            "INSERT INTO projects(project_id, repo_path) VALUES ('p', '/tmp/p')"
        )
        self.store.execute(
            "INSERT INTO beads VALUES ('p-1','key','p','Title','Approved scope','pending','sol','high','sol','high','default',NULL,NULL,'[]','complete',?,?)",
            (now, now),
        )
        run = apply_archon_decisions(
            self.store,
            {
                "global_limit": 2,
                "project_limits": {"p": 1},
                "decisions": [
                    {"decision": "approve", "project": "p", "beads": ["p-1"]}
                ],
            },
        )["created_runs"][0]
        self.assignment = self.store.row(
            "SELECT * FROM assignments WHERE run_id = ?", (run,)
        )
        self.task = self.store.register_task(
            native_thread_id="executor",
            role="executor",
            description="Title",
            model="sol",
            reasoning_effort="high",
            project_id="p",
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def _action(self, kind: str = "implement") -> int:
        now = "2026-01-01T00:00:00Z"
        cursor = self.store.execute(
            "INSERT INTO actions(task_id, assignment_id, kind, payload, state, created_at, updated_at) VALUES (?, ?, ?, '{}', 'active', ?, ?)",
            (self.task["id"], self.assignment["id"], kind, now, now),
        )
        self.store.execute(
            "INSERT INTO reservations(action_id,pair_id,state,created_at) VALUES (?,1,'active',?)",
            (cursor.lastrowid, now),
        )
        return int(cursor.lastrowid)

    def test_finish_is_bound_idempotent_and_advances_after_runtime_terminal(
        self,
    ) -> None:
        action = self._action()
        first = accept_finish(
            self.store,
            native_thread_id="executor",
            outcome_kind="ready_for_review",
            options={"evidence": "/tmp/evidence"},
        )
        second = accept_finish(
            self.store,
            native_thread_id="executor",
            outcome_kind="ready_for_review",
            options={"evidence": "/tmp/evidence"},
        )
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        with self.assertRaises(StoreError):
            accept_finish(
                self.store,
                native_thread_id="executor",
                outcome_kind="blocked",
                options={"reason": "different"},
            )
        result = observe_action_terminal(self.store, action)
        self.assertTrue(result["advanced"])
        self.assertEqual(
            self.store.row(
                "SELECT stage FROM assignments WHERE id = ?", (self.assignment["id"],)
            )["stage"],
            "review_pending",
        )
        self.assertIsNone(
            self.store.row("SELECT * FROM reservations WHERE action_id = ?", (action,))
        )

    def test_one_missing_outcome_reminder_then_condition(self) -> None:
        action = self._action()
        self.assertTrue(observe_action_terminal(self.store, action)["reminder"])
        result = observe_action_terminal(self.store, action)
        self.assertIn("condition", result)
        self.assertIsNone(
            self.store.row("SELECT * FROM reservations WHERE action_id = ?", (action,))
        )
        assignment = self.store.row(
            "SELECT * FROM assignments WHERE id = ?", (self.assignment["id"],)
        )
        self.assertEqual(assignment["stage"], "recovering")
        self.assertIsNotNone(assignment["next_attempt_at"])

    def test_runtime_failure_releases_capacity_and_schedules_retry(self) -> None:
        action = self._action()
        result = observe_action_terminal(self.store, action, runtime_state="failed")
        self.assertFalse(result["advanced"])
        self.assertIsNone(
            self.store.row("SELECT * FROM reservations WHERE action_id = ?", (action,))
        )
        assignment = self.store.row(
            "SELECT * FROM assignments WHERE id = ?", (self.assignment["id"],)
        )
        self.assertEqual(assignment["stage"], "recovering")
        self.assertIsNotNone(assignment["next_attempt_at"])

    def test_approval_and_covered_repair_retain_exact_mandate_linkage(self) -> None:
        self.store.execute(
            "UPDATE assignments SET candidate_id = 'candidate-1' WHERE id = ?",
            (self.assignment["id"],),
        )
        review = self._action("review")
        accept_finish(
            self.store,
            native_thread_id="executor",
            outcome_kind="approved",
            options={
                "assessment": "candidate matches the retained scope",
                "allow_repair": ["bounded_in_scope_ci_fix"],
            },
        )
        observe_action_terminal(self.store, review)
        mandate = self.store.row(
            "SELECT * FROM assignments WHERE id = ?", (self.assignment["id"],)
        )
        self.assertEqual(mandate["mandate_candidate_id"], "candidate-1")
        self.assertEqual(mandate["mandate_scope"], "Approved scope")

        self.store.execute(
            "UPDATE assignments SET stage = 'correcting', candidate_id = 'candidate-2' WHERE id = ?",
            (self.assignment["id"],),
        )
        repair = self._action("correct")
        accept_finish(
            self.store,
            native_thread_id="executor",
            outcome_kind="permitted_repair_complete",
            options={
                "repair_category": "bounded_in_scope_ci_fix",
                "repair_rationale": "fixed only the failing in-scope assertion",
                "evidence": "/tmp/evidence",
            },
        )
        result = observe_action_terminal(self.store, repair)
        self.assertEqual(result["stage"], "delivering")
        repaired = self.store.row(
            "SELECT * FROM assignments WHERE id = ?", (self.assignment["id"],)
        )
        self.assertEqual(repaired["predecessor_candidate_id"], "candidate-1")
        self.assertEqual(repaired["repair_category"], "bounded_in_scope_ci_fix")

    def test_sage_evidence_request_creates_one_interview_round(self) -> None:
        subject = self.store.register_task(
            native_thread_id="subject",
            role="overseer",
            description="Review subject",
            model="sol",
            reasoning_effort="high",
            project_id="p",
        )
        sage = self.store.register_task(
            native_thread_id="sage",
            role="sage",
            description="Postmortem",
            model="sol",
            reasoning_effort="high",
            project_id="p",
        )
        now = "2026-01-01T00:00:00Z"
        occurrence = self.store.execute(
            "INSERT INTO occurrences(kind, authority, state, created_at, updated_at) VALUES ('sage','test','active',?,?)",
            (now, now),
        )
        action = self.store.execute(
            "INSERT INTO actions(task_id, occurrence_id, kind, payload, state, outcome_kind, outcome_payload, created_at, updated_at) VALUES (?, ?, 'specialist', '{}', 'active', 'evidence_needed', ?, ?, ?)",
            (
                sage["id"],
                occurrence.lastrowid,
                json.dumps(
                    {
                        "requests": [
                            {
                                "subject": subject["title"],
                                "question": "What blocked review?",
                            }
                        ]
                    }
                ),
                now,
                now,
            ),
        )
        observe_action_terminal(self.store, int(action.lastrowid))
        interview = self.store.row(
            "SELECT * FROM interviews WHERE occurrence_id = ?",
            (occurrence.lastrowid,),
        )
        self.assertEqual(interview["subject_task_id"], subject["id"])
        self.assertEqual(interview["state"], "queued")


if __name__ == "__main__":
    unittest.main()
