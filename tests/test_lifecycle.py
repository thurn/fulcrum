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
from fulcrum.kernel import invariant_violations
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

    def _finish_review(
        self, outcome: str = "changes_requested", *, label: str = "defect"
    ) -> tuple[int, dict[str, object]]:
        action_id = self._action("review")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "review.json"
            content = (
                {
                    "findings": [
                        {
                            "problem": label,
                            "evidence": f"evidence for {label}",
                            "required_change": f"correct {label}",
                        }
                    ]
                }
                if outcome == "changes_requested"
                else {"missing_evidence": [label]}
            )
            path.write_text(json.dumps(content), encoding="utf-8")
            accept_finish(
                self.store,
                native_thread_id="executor",
                outcome_kind=outcome,
                options={"input": str(path)},
            )
        return action_id, observe_action_terminal(self.store, action_id)

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

    def test_third_review_failure_atomically_retains_one_archon_escalation(
        self,
    ) -> None:
        archon = self.store.register_task(
            native_thread_id="archon",
            role="archon",
            description="Fleet",
            model="sol",
            reasoning_effort="high",
        )

        first_action, first = self._finish_review(label="first defect")
        second_action, second = self._finish_review(label="second defect")

        self.assertEqual(first["stage"], "correcting")
        self.assertEqual(first["review_failures"], 1)
        self.assertEqual(second["stage"], "correcting")
        self.assertEqual(second["review_failures"], 2)
        self.assertEqual(self.store.rows("SELECT * FROM holds"), [])
        self.assertEqual(self.store.rows("SELECT * FROM updates"), [])
        self.assertEqual(
            len(
                self.store.rows(
                    "SELECT * FROM handoffs WHERE source_action_id IN (?, ?)",
                    (first_action, second_action),
                )
            ),
            2,
        )

        third_action, third = self._finish_review(label="third defect")

        self.assertEqual(third["stage"], "recovering")
        self.assertEqual(third["review_failures"], 3)
        assignment = self.store.row(
            "SELECT * FROM assignments WHERE id = ?", (self.assignment["id"],)
        )
        holds = self.store.rows(
            """SELECT * FROM holds WHERE scope = 'assignment' AND target = ?
               AND released_at IS NULL""",
            (str(self.assignment["id"]),),
        )
        updates = self.store.rows(
            "SELECT * FROM updates WHERE recipient_task_id = ?", (archon["id"],)
        )
        handoffs = self.store.rows(
            "SELECT * FROM handoffs WHERE source_action_id = ?", (third_action,)
        )
        self.assertEqual(assignment["stage"], "recovering")
        self.assertEqual(assignment["prior_stage"], "correcting")
        self.assertEqual(assignment["operator_hold_id"], holds[0]["id"])
        self.assertEqual(len(holds), 1)
        self.assertEqual(holds[0]["urgent"], 1)
        self.assertEqual(
            holds[0]["release_condition"],
            "Archon resolves escalation with retry, rescope, or cancel",
        )
        self.assertEqual(len(handoffs), 1)
        self.assertEqual(handoffs[0]["kind"], "review_findings")
        self.assertEqual(len(updates), 1)
        self.assertEqual(
            updates[0]["identity"],
            f"review-escalation:{self.assignment['id']}:{third_action}",
        )
        self.assertEqual(updates[0]["state"], "retained")
        escalation = json.loads(updates[0]["content"])
        self.assertEqual(
            escalation,
            {
                "action_id": third_action,
                "assignment_id": self.assignment["id"],
                "condition": "three substantive review failures require Archon decision",
                "hold_id": holds[0]["id"],
                "kind": "review_failure_escalation",
                "required_decision": "resolve_escalation",
                "resolutions": ["retry", "rescope", "cancel"],
                "review_action": "changes_requested",
                "review_failures": 3,
            },
        )

        self.assertEqual(
            observe_action_terminal(self.store, third_action),
            {"advanced": True, "reused": True},
        )
        self.assertEqual(len(self.store.rows("SELECT * FROM holds")), 1)
        self.assertEqual(len(self.store.rows("SELECT * FROM updates")), 1)
        self.assertEqual(len(self.store.rows("SELECT * FROM handoffs")), 6)

    def test_review_escalation_rolls_back_all_transition_effects_on_failure(
        self,
    ) -> None:
        archon = self.store.register_task(
            native_thread_id="archon",
            role="archon",
            description="Fleet",
            model="sol",
            reasoning_effort="high",
        )
        self._finish_review(label="first defect")
        self._finish_review(label="second defect")
        third_action = self._action("review")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "review.json"
            path.write_text(
                json.dumps(
                    {
                        "findings": [
                            {
                                "problem": "third defect",
                                "evidence": "third failure evidence",
                                "required_change": "correct the third defect",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            accept_finish(
                self.store,
                native_thread_id="executor",
                outcome_kind="changes_requested",
                options={"input": str(path)},
            )
        self.store.execute("""CREATE TEMP TRIGGER reject_review_escalation
               BEFORE INSERT ON updates
               WHEN NEW.identity LIKE 'review-escalation:%'
               BEGIN SELECT RAISE(ABORT, 'forced escalation insert failure'); END""")

        failed = observe_action_terminal(self.store, third_action)

        self.assertFalse(failed["advanced"])
        self.assertIn("forced escalation insert failure", failed["condition"])
        assignment = self.store.row(
            "SELECT * FROM assignments WHERE id = ?", (self.assignment["id"],)
        )
        self.assertEqual(assignment["stage"], "review_pending")
        self.assertEqual(assignment["review_failures"], 2)
        self.assertIsNone(assignment["operator_hold_id"])
        self.assertEqual(self.store.rows("SELECT * FROM holds"), [])
        self.assertEqual(
            self.store.rows(
                "SELECT * FROM updates WHERE recipient_task_id = ?", (archon["id"],)
            ),
            [],
        )
        self.assertEqual(
            self.store.rows(
                "SELECT * FROM handoffs WHERE source_action_id = ?", (third_action,)
            ),
            [],
        )

    def test_archon_cannot_consume_review_escalation_without_exact_resolution(
        self,
    ) -> None:
        archon = self.store.register_task(
            native_thread_id="archon",
            role="archon",
            description="Fleet",
            model="sol",
            reasoning_effort="high",
        )
        self._finish_review(label="first defect")
        self._finish_review(label="second defect")
        self._finish_review(label="third defect")
        escalation = self.store.row(
            "SELECT * FROM updates WHERE recipient_task_id = ?", (archon["id"],)
        )
        self.assertIsNotNone(escalation)
        assignment_id = int(self.assignment["id"])
        cases = [
            ("missing", [], "requires exactly one"),
            (
                "mismatched",
                [
                    {
                        "decision": "resolve_escalation",
                        "assignment_id": 999,
                        "resolution": "cancel",
                        "reason": "not the frozen escalation",
                    }
                ],
                "requires exactly one",
            ),
            (
                "duplicate",
                [
                    {
                        "decision": "resolve_escalation",
                        "assignment_id": assignment_id,
                        "resolution": "retry",
                        "reason": "first duplicate",
                    },
                    {
                        "decision": "resolve_escalation",
                        "assignment_id": assignment_id,
                        "resolution": "retry",
                        "reason": "second duplicate",
                    },
                ],
                "requires exactly one",
            ),
            (
                "unsupported",
                [
                    {
                        "decision": "resolve_escalation",
                        "assignment_id": assignment_id,
                        "resolution": "complete_non_code",
                        "reason": "not one of the frozen choices",
                        "evidence": "irrelevant",
                    }
                ],
                "supports only",
            ),
        ]
        for label, decisions, expected_error in cases:
            with self.subTest(label=label):
                action = self.store.execute(
                    """INSERT INTO actions(
                           task_id, kind, payload, state, created_at, updated_at
                       ) VALUES (?, 'archon', '{}', 'active', 'now', 'now')""",
                    (archon["id"],),
                )
                batch = self.store.execute(
                    """INSERT INTO batches(
                           recipient_task_id, state, action_id, created_at, updated_at
                       ) VALUES (?, 'frozen', ?, 'now', 'now')""",
                    (archon["id"], action.lastrowid),
                )
                self.store.execute(
                    "INSERT INTO batch_updates(batch_id, update_id) VALUES (?, ?)",
                    (batch.lastrowid, escalation["id"]),
                )
                self.store.execute(
                    "UPDATE updates SET state = 'batched' WHERE id = ?",
                    (escalation["id"],),
                )
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "decisions.json"
                    path.write_text(
                        json.dumps(
                            {
                                "decisions": decisions,
                                "handled_update_ids": [escalation["id"]],
                            }
                        ),
                        encoding="utf-8",
                    )
                    accept_finish(
                        self.store,
                        native_thread_id="archon",
                        outcome_kind="decisions",
                        options={"input": str(path)},
                    )

                result = observe_action_terminal(self.store, int(action.lastrowid))

                self.assertFalse(result["advanced"])
                self.assertIn(expected_error, result["condition"])
                self.assertEqual(
                    self.store.row(
                        "SELECT state FROM batches WHERE id = ?", (batch.lastrowid,)
                    )["state"],
                    "failed",
                )
                self.assertEqual(
                    self.store.row(
                        "SELECT state FROM updates WHERE id = ?", (escalation["id"],)
                    )["state"],
                    "retained",
                )

        assignment = self.store.row(
            "SELECT * FROM assignments WHERE id = ?", (self.assignment["id"],)
        )
        hold_id = json.loads(escalation["content"])["hold_id"]
        self.assertEqual(assignment["stage"], "recovering")
        self.assertEqual(assignment["operator_hold_id"], hold_id)
        self.assertIsNone(
            self.store.row("SELECT released_at FROM holds WHERE id = ?", (hold_id,))[
                "released_at"
            ]
        )
        self.assertEqual(len(self.store.rows("SELECT * FROM updates")), 1)

    def test_missing_evidence_does_not_cross_substantive_failure_threshold(
        self,
    ) -> None:
        self.store.register_task(
            native_thread_id="archon",
            role="archon",
            description="Fleet",
            model="sol",
            reasoning_effort="high",
        )
        self._finish_review(label="first defect")
        self._finish_review(label="second defect")

        _, result = self._finish_review("incomplete", label="validation transcript")

        self.assertEqual(result["stage"], "correcting")
        self.assertEqual(result["review_failures"], 2)
        self.assertEqual(self.store.rows("SELECT * FROM holds"), [])
        self.assertEqual(self.store.rows("SELECT * FROM updates"), [])

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

    def _held_worktree_operation(self) -> tuple[int, int]:
        timestamp = "2026-01-01T00:00:00Z"
        self.store.execute(
            "UPDATE assignments SET stage = 'preparing', worktree_path = NULL WHERE id = ?",
            (self.assignment["id"],),
        )
        operation_id = self.store.create_operation(
            "tollgate_worktree_create",
            str(self.assignment["id"]),
            {"repository_id": "repository", "name": "fulcrum-1"},
        )
        attempt = self.store.begin_operation_attempt(operation_id)
        self.store.finish_operation_attempt(
            operation_id,
            attempt,
            state="uncertain",
            error="transport ended after dispatch",
        )
        hold = self.store.execute(
            """INSERT INTO holds(scope, target, reason, urgent, release_condition, created_at)
               VALUES ('operation', ?, 'ambiguous worktree creation', 1,
                       'operator resolves ambiguous external effect', ?)""",
            (str(operation_id), timestamp),
        )
        hold_id = int(hold.lastrowid)
        self.store.execute(
            """UPDATE external_operations SET reconciliation_used = 1,
               operator_hold_id = ?, condition = 'targeted inventory remained ambiguous'
               WHERE id = ?""",
            (hold_id, operation_id),
        )
        self.store.execute(
            """UPDATE assignments SET prior_stage = stage, stage = 'recovering',
               operator_hold_id = ?, next_attempt_at = NULL,
               condition = 'targeted inventory remained ambiguous' WHERE id = ?""",
            (hold_id, self.assignment["id"]),
        )
        return operation_id, hold_id

    def test_bare_release_cannot_bypass_exhausted_operation_hold(self) -> None:
        operation_id, hold_id = self._held_worktree_operation()
        before_operation = self.store.row(
            "SELECT * FROM external_operations WHERE id = ?", (operation_id,)
        )
        before_assignment = self.store.row(
            "SELECT * FROM assignments WHERE id = ?", (self.assignment["id"],)
        )

        with self.assertRaisesRegex(StoreError, "use resolve_operation"):
            apply_archon_decisions(
                self.store,
                {"decisions": [{"decision": "release_hold", "hold_id": hold_id}]},
            )

        self.assertEqual(
            self.store.row(
                "SELECT * FROM external_operations WHERE id = ?", (operation_id,)
            ),
            before_operation,
        )
        self.assertEqual(
            self.store.row(
                "SELECT * FROM assignments WHERE id = ?", (self.assignment["id"],)
            ),
            before_assignment,
        )
        self.assertIsNone(
            self.store.row("SELECT released_at FROM holds WHERE id = ?", (hold_id,))[
                "released_at"
            ]
        )

    def test_operation_resolutions_are_atomic_and_restore_dispatchable_state(
        self,
    ) -> None:
        cases = [
            (
                "observed_success",
                "complete",
                "preparing",
                {"worktree_path": "/tmp/recovered"},
            ),
            ("observed_failure", "failed", "queued", None),
            ("confirmed_unsent", "canceled", "queued", None),
        ]
        for resolution, operation_state, assignment_stage, result in cases:
            with self.subTest(resolution=resolution):
                operation_id, hold_id = self._held_worktree_operation()
                decision = {
                    "decision": "resolve_operation",
                    "operation_id": operation_id,
                    "resolution": resolution,
                    "evidence": f"operator evidence for {resolution}",
                }
                if result is not None:
                    decision["result"] = result

                apply_archon_decisions(self.store, {"decisions": [decision]})

                operation = self.store.row(
                    "SELECT * FROM external_operations WHERE id = ?", (operation_id,)
                )
                assignment = self.store.row(
                    "SELECT * FROM assignments WHERE id = ?", (self.assignment["id"],)
                )
                self.assertEqual(operation["state"], operation_state)
                self.assertEqual(assignment["stage"], assignment_stage)
                self.assertIsNone(assignment["operator_hold_id"])
                self.assertIsNotNone(
                    self.store.row(
                        "SELECT released_at FROM holds WHERE id = ?", (hold_id,)
                    )["released_at"]
                )
                self.assertEqual(invariant_violations(self.store), [])
                if resolution != cases[-1][0]:
                    # Reuse the fixture's one assignment for the next terminal case.
                    self.store.execute(
                        "UPDATE assignments SET stage = 'implementing', worktree_path = '/tmp/p' WHERE id = ?",
                        (self.assignment["id"],),
                    )

    def test_operation_resolution_survives_restart_only_before_or_after_commit(
        self,
    ) -> None:
        operation_id, hold_id = self._held_worktree_operation()
        database = self.store.path
        self.store.close()
        self.store = Store(database)
        self.assertEqual(
            self.store.row(
                "SELECT state, operator_hold_id FROM external_operations WHERE id = ?",
                (operation_id,),
            ),
            {"state": "uncertain", "operator_hold_id": hold_id},
        )
        self.assertEqual(
            self.store.row(
                "SELECT stage, operator_hold_id FROM assignments WHERE id = ?",
                (self.assignment["id"],),
            ),
            {"stage": "recovering", "operator_hold_id": hold_id},
        )

        apply_archon_decisions(
            self.store,
            {
                "decisions": [
                    {
                        "decision": "resolve_operation",
                        "operation_id": operation_id,
                        "resolution": "observed_success",
                        "evidence": "exact worktree inventory and path",
                        "result": {"worktree_path": "/tmp/recovered"},
                    }
                ]
            },
        )
        self.store.close()
        self.store = Store(database)

        self.assertEqual(
            self.store.row(
                "SELECT state, operator_hold_id FROM external_operations WHERE id = ?",
                (operation_id,),
            ),
            {"state": "complete", "operator_hold_id": hold_id},
        )
        self.assertEqual(
            self.store.row(
                "SELECT stage, operator_hold_id, worktree_path FROM assignments WHERE id = ?",
                (self.assignment["id"],),
            ),
            {
                "stage": "preparing",
                "operator_hold_id": None,
                "worktree_path": "/tmp/recovered",
            },
        )
        self.assertIsNotNone(
            self.store.row("SELECT released_at FROM holds WHERE id = ?", (hold_id,))[
                "released_at"
            ]
        )
        self.assertEqual(invariant_violations(self.store), [])

    def test_every_operation_resolution_uses_archon_finish_and_is_restart_atomic(
        self,
    ) -> None:
        kinds = [
            "turn_start",
            "thread_start",
            "tollgate_worktree_create",
            "tollgate_candidate_create",
            "tollgate_approve",
            "beads_create",
            "beads_close",
            "setup_runtime_smoke",
        ]
        resolutions = [
            "observed_success",
            "observed_failure",
            "confirmed_unsent",
        ]
        for kind in kinds:
            for resolution in resolutions:
                with self.subTest(kind=kind, resolution=resolution):
                    self._assert_operation_resolution_case(kind, resolution)

    def test_recovery_batch_rejects_missing_and_partial_operation_decisions(
        self,
    ) -> None:
        for supplied_count in (0, 1):
            with self.subTest(supplied_count=supplied_count):
                with tempfile.TemporaryDirectory() as directory:
                    store = Store(Path(directory) / "missing-decisions.db")
                    assignment = self._seed_resolution_store(store)
                    operation_ids = [
                        self._hold_operation_for_matrix(
                            store, assignment, "setup_runtime_smoke"
                        )[0]
                        for _ in range(2)
                    ]
                    decisions = [
                        self._resolution_decision(
                            "setup_runtime_smoke",
                            operation_id,
                            "confirmed_unsent",
                        )
                        for operation_id in operation_ids[:supplied_count]
                    ]

                    result = self._archon_resolution_outcome(
                        store, Path(directory), operation_ids, decisions
                    )

                    self.assertFalse(result["advanced"])
                    self.assertIn(
                        "every unresolved operation-resolution update",
                        result["condition"],
                    )
                    for operation_id in operation_ids:
                        operation = store.row(
                            "SELECT * FROM external_operations WHERE id = ?",
                            (operation_id,),
                        )
                        self.assertEqual(operation["state"], "uncertain")
                        self.assertEqual(operation["reconciliation_used"], 1)
                    self.assertEqual(
                        {
                            row["state"]
                            for row in store.rows("SELECT state FROM updates")
                        },
                        {"retained"},
                    )
                    self.assertEqual(
                        store.row("SELECT state FROM batches")["state"], "failed"
                    )
                    store.close()

    def _assert_operation_resolution_case(self, kind: str, resolution: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "matrix.db"
            store = Store(database)
            assignment = self._seed_resolution_store(store)
            operation_id, hold_id = self._hold_operation_for_matrix(
                store, assignment, kind
            )
            before = {
                "operation": store.row(
                    "SELECT * FROM external_operations WHERE id = ?", (operation_id,)
                ),
                "hold": store.row("SELECT * FROM holds WHERE id = ?", (hold_id,)),
                "targets": store.rows(
                    "SELECT id, stage, operator_hold_id FROM assignments ORDER BY id"
                )
                + store.rows(
                    "SELECT id, state, operator_hold_id FROM actions ORDER BY id"
                )
                + store.rows(
                    "SELECT id, state, operator_hold_id FROM obligations ORDER BY id"
                ),
            }
            with self.assertRaisesRegex(StoreError, "use resolve_operation"):
                apply_archon_decisions(
                    store,
                    {"decisions": [{"decision": "release_hold", "hold_id": hold_id}]},
                )
            self.assertEqual(
                store.row(
                    "SELECT * FROM external_operations WHERE id = ?", (operation_id,)
                ),
                before["operation"],
            )
            self.assertEqual(
                store.row("SELECT * FROM holds WHERE id = ?", (hold_id,)),
                before["hold"],
            )
            self.assertEqual(
                store.rows(
                    "SELECT id, stage, operator_hold_id FROM assignments ORDER BY id"
                )
                + store.rows(
                    "SELECT id, state, operator_hold_id FROM actions ORDER BY id"
                )
                + store.rows(
                    "SELECT id, state, operator_hold_id FROM obligations ORDER BY id"
                ),
                before["targets"],
            )

            # A restart before the one resolution transaction exposes the complete
            # old state: uncertain operation, unreleased hold, and held target.
            store.close()
            store = Store(database)
            self.assertEqual(
                store.row(
                    "SELECT state, reconciliation_used, operator_hold_id FROM external_operations WHERE id = ?",
                    (operation_id,),
                ),
                {
                    "state": "uncertain",
                    "reconciliation_used": 1,
                    "operator_hold_id": hold_id,
                },
            )
            self.assertIsNone(
                store.row("SELECT released_at FROM holds WHERE id = ?", (hold_id,))[
                    "released_at"
                ]
            )

            decision = self._resolution_decision(kind, operation_id, resolution)
            self._resolve_through_archon_finish(store, Path(directory), decision)
            store.close()
            store = Store(database)

            expected_state = {
                "observed_success": "complete",
                "observed_failure": "failed",
                "confirmed_unsent": "canceled",
            }[resolution]
            operation = store.row(
                "SELECT * FROM external_operations WHERE id = ?", (operation_id,)
            )
            self.assertEqual(operation["state"], expected_state)
            self.assertIsNotNone(
                store.row("SELECT released_at FROM holds WHERE id = ?", (hold_id,))[
                    "released_at"
                ]
            )
            self.assertIsNone(
                store.row(
                    "SELECT 1 FROM assignments WHERE operator_hold_id = ? UNION ALL SELECT 1 FROM actions WHERE operator_hold_id = ? UNION ALL SELECT 1 FROM obligations WHERE operator_hold_id = ?",
                    (hold_id, hold_id, hold_id),
                )
            )
            self.assertEqual(invariant_violations(store), [])
            self._assert_resolved_target(store, assignment, kind, resolution)
            store.close()

    def _seed_resolution_store(self, store: Store) -> dict[str, object]:
        now = "2026-01-01T00:00:00Z"
        store.execute(
            "INSERT INTO projects(project_id, repo_path) VALUES ('p', '/tmp/p')"
        )
        store.execute(
            "INSERT INTO beads VALUES ('p-1','key','p','Title','Scope','pending','sol','high','sol','high','default',NULL,NULL,'[]','complete',?,?)",
            (now, now),
        )
        run_id = apply_archon_decisions(
            store,
            {
                "global_limit": 2,
                "project_limits": {"p": 1},
                "decisions": [
                    {"decision": "approve", "project": "p", "beads": ["p-1"]}
                ],
            },
        )["created_runs"][0]
        assignment = store.row("SELECT * FROM assignments WHERE run_id = ?", (run_id,))
        assert assignment is not None
        return assignment

    def _hold_operation_for_matrix(
        self, store: Store, assignment: dict[str, object], kind: str
    ) -> tuple[int, int]:
        now = "2026-01-01T00:00:00Z"
        assignment_id = int(assignment["id"])
        run_id = int(assignment["run_id"])
        target: str
        inputs: dict[str, object]
        held_table: str | None = None
        held_id: int | None = None
        if kind == "turn_start":
            worker = store.register_task(
                native_thread_id="turn-worker",
                role="executor",
                description="turn",
                model="sol",
                reasoning_effort="high",
                project_id="p",
            )
            action = store.execute(
                "INSERT INTO actions(task_id, assignment_id, kind, payload, state, created_at, updated_at) VALUES (?, ?, 'implement', '{}', 'uncertain', ?, ?)",
                (worker["id"], assignment_id, now, now),
            )
            store.execute(
                "INSERT INTO reservations(action_id, pair_id, state, created_at) VALUES (?, ?, 'uncertain', ?)",
                (action.lastrowid, run_id, now),
            )
            target = str(action.lastrowid)
            inputs = {"thread_id": "turn-worker", "prompt": "resume"}
            held_table, held_id = "actions", int(action.lastrowid)
        elif kind == "thread_start":
            target = "executor"
            inputs = {
                "role": "executor",
                "role_number": 9,
                "title": "Executor 9: recovered",
                "description": "recovered",
                "project_id": "codex-p",
                "local_project_id": "p",
                "cwd": "/tmp/p",
                "model": "sol",
                "effort": "high",
                "pair_id": run_id,
            }
            held_table, held_id = "assignments", assignment_id
        elif kind == "tollgate_worktree_create":
            target = str(assignment_id)
            inputs = {"repository_id": "repository", "name": "fulcrum-1"}
            store.execute(
                "UPDATE assignments SET stage = 'preparing' WHERE id = ?",
                (assignment_id,),
            )
            held_table, held_id = "assignments", assignment_id
        elif kind == "tollgate_candidate_create":
            target = str(assignment_id)
            inputs = {"repository_id": "repository", "revision": "source-oid"}
            store.execute(
                "UPDATE assignments SET stage = 'review_pending' WHERE id = ?",
                (assignment_id,),
            )
            held_table, held_id = "assignments", assignment_id
        elif kind == "tollgate_approve":
            target = "candidate-1"
            inputs = {"repository_id": "repository", "candidate_id": target}
            store.execute(
                "UPDATE assignments SET stage = 'delivering', candidate_id = ? WHERE id = ?",
                (target, assignment_id),
            )
            held_table, held_id = "assignments", assignment_id
        elif kind == "beads_create":
            target = "intake-new"
            inputs = {
                "intake_key": target,
                "project": "p",
                "title": "Recovered bead",
                "description": "Recovered scope",
            }
            obligation = store.execute(
                "INSERT INTO obligations(kind, identity, target, state, created_at, updated_at) VALUES ('beads_publication', ?, 'p', 'uncertain', ?, ?)",
                (target, now, now),
            )
            held_table, held_id = "obligations", int(obligation.lastrowid)
        elif kind == "beads_close":
            target = "p-1"
            inputs = {"reason": "delivered"}
            store.execute(
                "UPDATE assignments SET stage = 'delivering' WHERE id = ?",
                (assignment_id,),
            )
            held_table, held_id = "assignments", assignment_id
        else:
            target = "p"
            inputs = {"cwd": "/tmp/p", "project_id": "codex-p"}

        operation_id = store.create_operation(kind, target, inputs)
        attempt = store.begin_operation_attempt(operation_id)
        store.finish_operation_attempt(
            operation_id,
            attempt,
            state="uncertain",
            error="targeted observer remained ambiguous",
        )
        hold = store.execute(
            "INSERT INTO holds(scope, target, reason, urgent, release_condition, created_at) VALUES ('operation', ?, 'ambiguous effect', 1, 'operator resolves ambiguous external effect', ?)",
            (str(operation_id), now),
        )
        hold_id = int(hold.lastrowid)
        store.execute(
            "UPDATE external_operations SET reconciliation_used = 1, operator_hold_id = ? WHERE id = ?",
            (hold_id, operation_id),
        )
        if held_table == "assignments":
            store.execute(
                "UPDATE assignments SET prior_stage = stage, stage = 'recovering', operator_hold_id = ? WHERE id = ?",
                (hold_id, held_id),
            )
        elif held_table == "actions":
            store.execute(
                "UPDATE actions SET operator_hold_id = ? WHERE id = ?",
                (hold_id, held_id),
            )
        elif held_table == "obligations":
            store.execute(
                "UPDATE obligations SET operator_hold_id = ? WHERE id = ?",
                (hold_id, held_id),
            )
        return operation_id, hold_id

    def _resolution_decision(
        self, kind: str, operation_id: int, resolution: str
    ) -> dict[str, object]:
        decision: dict[str, object] = {
            "decision": "resolve_operation",
            "operation_id": operation_id,
            "resolution": resolution,
            "evidence": f"exact external inspection established {resolution}",
        }
        if resolution == "observed_success":
            if kind in {
                "turn_start",
                "thread_start",
                "tollgate_candidate_create",
                "beads_create",
            }:
                decision["native_id"] = f"observed-{kind}"
            if kind == "tollgate_worktree_create":
                decision["result"] = {"worktree_path": "/tmp/recovered"}
        return decision

    def _resolve_through_archon_finish(
        self, store: Store, directory: Path, decision: dict[str, object]
    ) -> None:
        result = self._archon_resolution_outcome(
            store,
            directory,
            [int(decision["operation_id"])],
            [decision],
        )
        self.assertTrue(result["advanced"])
        self.assertEqual(store.row("SELECT state FROM batches")["state"], "processed")

    def _archon_resolution_outcome(
        self,
        store: Store,
        directory: Path,
        operation_ids: list[int],
        decisions: list[dict[str, object]],
    ) -> dict[str, object]:
        now = "2026-01-01T00:00:00Z"
        archon = store.register_task(
            native_thread_id="matrix-archon",
            role="archon",
            description="Fleet",
            model="sol",
            reasoning_effort="high",
            project_id="p",
        )
        update_ids = []
        for operation_id in operation_ids:
            update = store.execute(
                "INSERT INTO updates(recipient_task_id, identity, content, actionable, state, created_at, updated_at) VALUES (?, ?, ?, 1, 'batched', ?, ?)",
                (
                    archon["id"],
                    f"operation-resolution:{operation_id}",
                    json.dumps(
                        {
                            "kind": "operation_resolution",
                            "operation_id": operation_id,
                        }
                    ),
                    now,
                    now,
                ),
            )
            update_ids.append(int(update.lastrowid))
        action = store.execute(
            "INSERT INTO actions(task_id, kind, payload, state, native_turn_id, created_at, updated_at) VALUES (?, 'archon', '{}', 'active', 'matrix-turn', ?, ?)",
            (archon["id"], now, now),
        )
        batch = store.execute(
            "INSERT INTO batches(recipient_task_id, action_id, state, created_at, updated_at) VALUES (?, ?, 'frozen', ?, ?)",
            (archon["id"], action.lastrowid, now, now),
        )
        for update_id in update_ids:
            store.execute(
                "INSERT INTO batch_updates(batch_id, update_id) VALUES (?, ?)",
                (batch.lastrowid, update_id),
            )
        decision_file = directory / "decision.json"
        decision_file.write_text(
            json.dumps(
                {
                    "decisions": decisions,
                    "handled_update_ids": update_ids,
                }
            ),
            encoding="utf-8",
        )
        accept_finish(
            store,
            native_thread_id="matrix-archon",
            outcome_kind="decisions",
            options={"input": str(decision_file)},
        )
        return observe_action_terminal(store, int(action.lastrowid))

    def _assert_resolved_target(
        self,
        store: Store,
        assignment: dict[str, object],
        kind: str,
        resolution: str,
    ) -> None:
        assignment_id = int(assignment["id"])
        current = store.row("SELECT * FROM assignments WHERE id = ?", (assignment_id,))
        success = resolution == "observed_success"
        if kind == "turn_start":
            action = store.row(
                "SELECT * FROM actions WHERE kind = 'implement' ORDER BY id LIMIT 1"
            )
            self.assertEqual(action["state"], "active" if success else "pending")
            self.assertEqual(bool(action["native_turn_id"]), success)
        elif kind == "thread_start":
            task = store.row(
                "SELECT * FROM tasks WHERE native_thread_id = 'observed-thread_start'"
            )
            self.assertEqual(task is not None, success)
            self.assertEqual(current["stage"], "queued")
        elif kind == "tollgate_worktree_create":
            self.assertEqual(current["stage"], "preparing" if success else "queued")
            self.assertEqual(
                current["worktree_path"], "/tmp/recovered" if success else None
            )
        elif kind == "tollgate_candidate_create":
            self.assertEqual(current["stage"], "review_pending")
            self.assertEqual(
                current["candidate_id"],
                "observed-tollgate_candidate_create" if success else None,
            )
        elif kind == "tollgate_approve":
            self.assertEqual(
                current["stage"],
                "correcting" if resolution == "observed_failure" else "delivering",
            )
        elif kind == "beads_create":
            obligation = store.row(
                "SELECT * FROM obligations WHERE identity = 'intake-new'"
            )
            self.assertEqual(obligation["state"], "complete" if success else "failed")
            self.assertEqual(
                store.row("SELECT 1 FROM beads WHERE bead_id = 'observed-beads_create'")
                is not None,
                success,
            )
        elif kind == "beads_close":
            self.assertEqual(current["stage"], "completed" if success else "delivering")
        elif kind == "setup_runtime_smoke":
            self.assertEqual(
                store.row("SELECT 1 FROM meta WHERE key = 'desktop_smoke_check'")
                is not None,
                success,
            )

    def test_approval_and_covered_repair_retain_exact_mandate_linkage(self) -> None:
        self.store.execute(
            "UPDATE assignments SET candidate_id = 'candidate-1' WHERE id = ?",
            (self.assignment["id"],),
        )
        review = self._action("review")
        approval = Path(self.temporary.name) / "approval.json"
        approval.write_text(
            json.dumps(
                {
                    "assessment": "candidate matches the retained scope",
                    "minor_fixes": [],
                    "repair_permissions": ["bounded_in_scope_ci_fix"],
                }
            ),
            encoding="utf-8",
        )
        accept_finish(
            self.store,
            native_thread_id="executor",
            outcome_kind="approved",
            options={"input": str(approval)},
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
