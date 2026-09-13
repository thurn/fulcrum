from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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
        if kind == "review":
            self.store.execute(
                """UPDATE assignments SET candidate_id = COALESCE(candidate_id, 'candidate'),
                   source_oid = 'source' WHERE id = ?""",
                (self.assignment["id"],),
            )
            source = self.store.execute(
                """INSERT INTO actions(
                       task_id, assignment_id, kind, payload, state, outcome_kind,
                       outcome_payload, created_at, updated_at
                   ) VALUES (?, ?, 'implement', '{}', 'processed',
                             'ready_for_review', ?, ?, ?)""",
                (
                    self.task["id"],
                    self.assignment["id"],
                    json.dumps({"evidence": "implementation"}),
                    now,
                    now,
                ),
            )
            self.store.execute(
                """INSERT INTO handoffs(
                       assignment_id, source_action_id, kind, content_json, created_at
                   ) VALUES (?, ?, 'implementation_evidence', ?, ?)""",
                (
                    self.assignment["id"],
                    source.lastrowid,
                    json.dumps({"evidence": "implementation"}),
                    now,
                ),
            )
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
        self.store.execute(
            "UPDATE assignments SET candidate_id = 'candidate', source_oid = 'source' WHERE id = ?",
            (self.assignment["id"],),
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
        handoff = self.store.row(
            "SELECT * FROM handoffs WHERE source_action_id = ?", (action,)
        )
        self.assertEqual(handoff["kind"], "implementation_evidence")
        self.assertEqual(
            json.loads(handoff["content_json"])["evidence"], "/tmp/evidence"
        )
        repeated = observe_action_terminal(self.store, action)
        self.assertEqual(repeated, {"advanced": True, "reused": True})
        self.assertEqual(
            len(
                self.store.rows(
                    "SELECT * FROM handoffs WHERE source_action_id = ?", (action,)
                )
            ),
            1,
        )

    def test_blocked_assignment_notifies_archon(self) -> None:
        archon = self.store.register_task(
            native_thread_id="archon",
            role="archon",
            description="Fleet",
            model="sol",
            reasoning_effort="high",
        )
        action = self._action()
        accept_finish(
            self.store,
            native_thread_id="executor",
            outcome_kind="blocked",
            options={"reason": "requires an explicit administrative decision"},
        )

        result = observe_action_terminal(self.store, action)

        self.assertTrue(result["advanced"])
        update = self.store.row(
            "SELECT * FROM updates WHERE recipient_task_id = ?", (archon["id"],)
        )
        self.assertEqual(json.loads(update["content"])["kind"], "assignment_blocked")
        self.assertIn("administrative decision", update["content"])

    def test_review_findings_are_retained_for_the_correction_handoff(self) -> None:
        action = self._action("review")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "findings.json"
            path.write_text(
                json.dumps(
                    {
                        "findings": [
                            {
                                "problem": "wrong result",
                                "evidence": "test failed",
                                "required_change": "return the expected result",
                            }
                        ]
                    }
                )
            )
            accept_finish(
                self.store,
                native_thread_id="executor",
                outcome_kind="changes_requested",
                options={"input": str(path)},
            )
        observe_action_terminal(self.store, action)
        handoff = self.store.row(
            "SELECT * FROM handoffs WHERE source_action_id = ?", (action,)
        )
        self.assertEqual(handoff["kind"], "review_findings")
        self.assertEqual(
            json.loads(handoff["content_json"])["findings"][0]["problem"],
            "wrong result",
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

    def test_completed_observation_recovers_false_runtime_failure(self) -> None:
        archon = self.store.register_task(
            native_thread_id="archon",
            role="archon",
            description="Fleet",
            model="sol",
            reasoning_effort="high",
            project_id="p",
        )
        cursor = self.store.execute(
            """INSERT INTO actions(
                   task_id, kind, payload, state, outcome_kind, outcome_payload,
                   condition, created_at, updated_at
               ) VALUES (?, 'archon', '{}', 'failed', 'decisions', ?, ?, 'now', 'now')""",
            (
                archon["id"],
                '{"decisions":[],"handled_update_ids":[]}',
                "runtime turn interrupted; retained for specific recovery",
            ),
        )
        self.store.execute(
            "UPDATE tasks SET last_turn_terminal = 1, helpers_terminal = 1 WHERE id = ?",
            (archon["id"],),
        )

        result = observe_action_terminal(
            self.store, int(cursor.lastrowid), runtime_state="completed"
        )

        self.assertTrue(result["advanced"])
        self.assertEqual(
            self.store.row(
                "SELECT state, condition FROM actions WHERE id = ?",
                (cursor.lastrowid,),
            ),
            {"state": "processed", "condition": None},
        )

    def test_archon_cannot_lower_capacity_below_active_usage(self) -> None:
        self.store.execute("UPDATE meta SET value = '2' WHERE key = 'global_limit'")
        self.store.execute(
            "UPDATE meta SET value = '{\"p\": 2}' WHERE key = 'project_limits'"
        )
        self._action()
        second = self.store.register_task(
            native_thread_id="second-executor",
            role="executor",
            description="Second",
            model="sol",
            reasoning_effort="high",
            project_id="p",
        )
        now = "2026-01-01T00:00:00Z"
        action = self.store.execute(
            """INSERT INTO actions(task_id, kind, payload, state, created_at, updated_at)
               VALUES (?, 'implement', '{}', 'active', ?, ?)""",
            (second["id"], now, now),
        )
        self.store.execute(
            """INSERT INTO reservations(action_id, project_ids, state, created_at)
               VALUES (?, '[\"p\"]', 'active', ?)""",
            (action.lastrowid, now),
        )

        with self.assertRaisesRegex(StoreError, "below active usage"):
            apply_archon_decisions(self.store, {"global_limit": 1})
        self.assertEqual(
            self.store.row("SELECT value FROM meta WHERE key = 'global_limit'")[
                "value"
            ],
            "2",
        )

    def test_archon_policy_must_describe_complete_valid_topology(self) -> None:
        self.store.execute(
            "INSERT INTO projects(project_id, repo_path) VALUES ('q', '/tmp/q')"
        )
        with self.assertRaisesRegex(StoreError, "exactly cover enabled projects"):
            apply_archon_decisions(self.store, {"project_limits": {"p": 1}})
        with self.assertRaisesRegex(StoreError, "fleet Sage"):
            apply_archon_decisions(
                self.store,
                {
                    "recurring_policies": [
                        {"kind": "sage", "scope": "p", "cadence_seconds": 60}
                    ]
                },
            )

    def test_archon_policy_reapplication_is_idempotent_for_global_scope(self) -> None:
        payload = {
            "recurring_policies": [
                {
                    "kind": "sage",
                    "scope": None,
                    "cadence_seconds": 86400,
                    "anchor_at": "2026-01-01T06:00:00Z",
                },
                {
                    "kind": "inquisitor",
                    "scope": "p",
                    "cadence_seconds": 86400,
                    "anchor_at": "2026-01-01T18:00:00Z",
                },
            ]
        }

        apply_archon_decisions(self.store, payload)
        apply_archon_decisions(self.store, payload)

        policies = self.store.rows("SELECT kind, scope FROM policies ORDER BY kind")
        self.assertEqual(
            [(row["kind"], row["scope"]) for row in policies],
            [("inquisitor", "p"), ("sage", None)],
        )

    def test_policy_without_anchor_first_runs_after_one_cadence(self) -> None:
        before = datetime.now(timezone.utc)
        apply_archon_decisions(
            self.store,
            {
                "recurring_policies": [
                    {"kind": "sage", "scope": None, "cadence_seconds": 3600}
                ]
            },
        )

        policy = self.store.row("SELECT * FROM policies WHERE kind = 'sage'")
        anchor = datetime.fromisoformat(policy["anchor_at"].replace("Z", "+00:00"))
        self.assertGreaterEqual(anchor, before + timedelta(minutes=59))

    def test_unknown_archon_decision_rolls_back_the_entire_payload(self) -> None:
        before = len(self.store.rows("SELECT * FROM runs"))
        now = "2026-01-01T00:00:00Z"
        self.store.execute(
            """INSERT INTO beads(
                   bead_id, intake_key, project_id, title, description, activation,
                   executor_model, executor_reasoning_effort, overseer_model,
                   overseer_reasoning_effort, model_provenance, publication_state,
                   created_at, updated_at
               ) VALUES ('p-2','key-2','p','Second','Scope','pending','sol','high',
                         'sol','high','default','complete',?,?)""",
            (now, now),
        )
        with self.assertRaisesRegex(StoreError, "unsupported Archon decision"):
            apply_archon_decisions(
                self.store,
                {
                    "decisions": [
                        {
                            "decision": "approve",
                            "project": "p",
                            "beads": ["p-2"],
                        },
                        {"decision": "silently-ignore-me"},
                    ]
                },
            )
        self.assertEqual(len(self.store.rows("SELECT * FROM runs")), before)

    def test_invalid_archon_target_releases_the_frozen_batch_for_retry(self) -> None:
        archon = self.store.register_task(
            native_thread_id="archon",
            role="archon",
            description="Fleet",
            model="sol",
            reasoning_effort="high",
        )
        now = "2026-01-01T00:00:00Z"
        update = self.store.execute(
            """INSERT INTO updates(recipient_task_id, identity, content, state, created_at, updated_at)
               VALUES (?, 'recovery:1', '{}', 'batched', ?, ?)""",
            (archon["id"], now, now),
        )
        outcome = {
            "decisions": [{"decision": "release_hold", "hold_id": 999}],
            "handled_update_ids": [update.lastrowid],
        }
        action = self.store.execute(
            """INSERT INTO actions(task_id, kind, payload, state, outcome_kind,
                   outcome_payload, created_at, updated_at)
               VALUES (?, 'archon', '{}', 'active', 'decisions', ?, ?, ?)""",
            (archon["id"], json.dumps(outcome), now, now),
        )
        batch = self.store.execute(
            """INSERT INTO batches(recipient_task_id, state, action_id, created_at, updated_at)
               VALUES (?, 'frozen', ?, ?, ?)""",
            (archon["id"], action.lastrowid, now, now),
        )
        self.store.execute(
            "INSERT INTO batch_updates(batch_id, update_id) VALUES (?, ?)",
            (batch.lastrowid, update.lastrowid),
        )
        result = observe_action_terminal(self.store, int(action.lastrowid))
        self.assertFalse(result["advanced"])
        self.assertEqual(
            self.store.row(
                "SELECT state FROM actions WHERE id = ?", (action.lastrowid,)
            )["state"],
            "failed",
        )
        self.assertEqual(
            self.store.row(
                "SELECT state FROM updates WHERE id = ?", (update.lastrowid,)
            )["state"],
            "retained",
        )

    def test_archon_can_hold_release_prioritize_and_request_specialist(self) -> None:
        run_id = int(self.assignment["run_id"])
        result = apply_archon_decisions(
            self.store,
            {
                "decisions": [
                    {
                        "decision": "hold",
                        "scope": "run",
                        "target": run_id,
                        "reason": "conflict",
                        "release_condition": "other run completes",
                    },
                    {"decision": "set_priority", "run_id": run_id, "priority": 9},
                    {
                        "decision": "request_specialist",
                        "kind": "inquisitor",
                        "projects": ["p"],
                        "prompt": "inspect boundaries",
                    },
                ]
            },
        )
        hold_id = result["applied_decisions"][0]["hold_id"]
        self.assertEqual(
            self.store.row("SELECT priority FROM runs WHERE id = ?", (run_id,))[
                "priority"
            ],
            9,
        )
        self.assertIsNotNone(
            self.store.row("SELECT * FROM occurrences WHERE authority = 'archon'")
        )
        apply_archon_decisions(
            self.store,
            {"decisions": [{"decision": "release_hold", "hold_id": hold_id}]},
        )
        self.assertIsNotNone(
            self.store.row("SELECT released_at FROM holds WHERE id = ?", (hold_id,))[
                "released_at"
            ]
        )
        reused = apply_archon_decisions(
            self.store,
            {"decisions": [{"decision": "release_hold", "hold_id": hold_id}]},
        )
        self.assertTrue(reused["applied_decisions"][0]["reused"])
        with self.assertRaisesRegex(StoreError, "enabled project scope"):
            apply_archon_decisions(
                self.store,
                {
                    "recurring_policies": [
                        {
                            "kind": "inquisitor",
                            "scope": "missing",
                            "cadence_seconds": 60,
                        }
                    ]
                },
            )

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

    def test_mismatched_revisions_require_controller_exact_source_evidence(
        self,
    ) -> None:
        action = self._action()
        self.store.execute(
            """UPDATE assignments SET candidate_id = 'candidate-source',
               source_oid = 'source-revision', tested_oid = 'tested-revision'
               WHERE id = ?""",
            (self.assignment["id"],),
        )
        accept_finish(
            self.store,
            native_thread_id="executor",
            outcome_kind="ready_for_review",
            options={"evidence": "/tmp/evidence"},
        )

        result = observe_action_terminal(self.store, action)

        self.assertFalse(result["advanced"])
        self.assertIn(
            "exact-source validation evidence is required", result["condition"]
        )
        retained = self.store.row(
            "SELECT stage FROM assignments WHERE id = ?", (self.assignment["id"],)
        )
        self.assertEqual(retained["stage"], "recovering")
        self.assertIsNone(
            self.store.row(
                "SELECT id FROM handoffs WHERE source_action_id = ?", (action,)
            )
        )

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
