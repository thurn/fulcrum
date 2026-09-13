from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fulcrum.store import Store, StoreError


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "state.sqlite3")
        self.store.execute(
            "INSERT INTO projects(project_id, repo_path) VALUES ('fulcrum', '/tmp/fulcrum')"
        )
        self.store.execute(
            "INSERT INTO meta(key, value) VALUES ('global_limit', '10'), ('project_limits', ?) ",
            (json.dumps({"fulcrum": 10}),),
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_pragmas_and_names_are_role_specific(self) -> None:
        self.assertEqual(self.store.row("PRAGMA journal_mode")["journal_mode"], "wal")
        self.assertEqual(self.store.row("PRAGMA foreign_keys")["foreign_keys"], 1)
        sage = self.store.register_task(
            native_thread_id="sage-1",
            role="sage",
            description="Workflow postmortem",
            model="sol",
            reasoning_effort="high",
        )
        inquisitor = self.store.register_task(
            native_thread_id="inq-1",
            role="inquisitor",
            description="Architecture",
            model="sol",
            reasoning_effort="high",
        )
        self.assertEqual(sage["title"], "📖 [SAGE0001] Workflow postmortem")
        self.assertEqual(inquisitor["title"], "🛡️ [INQ0001] Architecture")
        self.store.execute(
            "UPDATE role_counters SET next_number = 10000 WHERE role = 'executor'"
        )
        _, title = self.store.allocate_name("executor", "Large fleet")
        self.assertEqual(title, "⚒️ [EXE10000] Large fleet")

    def test_registration_retry_and_conflict(self) -> None:
        first = self.store.register_task(
            native_thread_id="thread-1",
            role="weaver",
            description="Intake",
            model="sol",
            reasoning_effort="high",
            project_id="fulcrum",
        )
        second = self.store.register_task(
            native_thread_id="thread-1",
            role="weaver",
            description="Changed text does not reallocate",
            model="sol",
            reasoning_effort="high",
            project_id="fulcrum",
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(
            self.store.row(
                "SELECT next_number FROM role_counters WHERE role = 'weaver'"
            )["next_number"],
            2,
        )
        with self.assertRaises(StoreError):
            self.store.register_task(
                native_thread_id="thread-1",
                role="executor",
                description="Conflict",
                model="sol",
                reasoning_effort="high",
                project_id="fulcrum",
            )

    def test_constraints_prevent_two_current_actions_and_pair_reservations(
        self,
    ) -> None:
        task = self.store.register_task(
            native_thread_id="thread-1",
            role="executor",
            description="Work",
            model="sol",
            reasoning_effort="high",
            project_id="fulcrum",
        )
        now = "2026-01-01T00:00:00Z"
        first = self.store.execute(
            "INSERT INTO actions(task_id, kind, payload, state, created_at, updated_at) VALUES (?, 'implement', '{}', 'active', ?, ?)",
            (task["id"], now, now),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.execute(
                "INSERT INTO actions(task_id, kind, payload, state, created_at, updated_at) VALUES (?, 'correct', '{}', 'pending', ?, ?)",
                (task["id"], now, now),
            )
        self.store.execute(
            "INSERT INTO reservations(action_id, pair_id, state, created_at) VALUES (?, 7, 'active', ?)",
            (first.lastrowid, now),
        )
        other = self.store.register_task(
            native_thread_id="thread-2",
            role="overseer",
            description="Review",
            model="sol",
            reasoning_effort="high",
            project_id="fulcrum",
        )
        action = self.store.execute(
            "INSERT INTO actions(task_id, kind, payload, state, created_at, updated_at) VALUES (?, 'review', '{}', 'active', ?, ?)",
            (other["id"], now, now),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.execute(
                "INSERT INTO reservations(action_id, pair_id, state, created_at) VALUES (?, 7, 'active', ?)",
                (action.lastrowid, now),
            )

    def test_status_has_controller_read_model(self) -> None:
        status = self.store.status()
        self.assertEqual(status["unfinished_work_count"], 0)
        self.assertEqual(status["slot_usage"], {"global": 0, "projects": {}})
        self.assertIn("operations", status)
        self.assertIn("obligations", status)

    def test_database_rejects_recovery_without_retry_or_hold(self) -> None:
        now = "2026-01-01T00:00:00Z"
        self.store.execute(
            "INSERT INTO beads VALUES ('b','k','fulcrum','t','scope','pending','sol','high','sol','high','default',NULL,NULL,'[]','complete',?,?)",
            (now, now),
        )
        run = self.store.execute(
            "INSERT INTO runs(project_id, authority, created_at, updated_at) VALUES ('fulcrum','test',?,?)",
            (now, now),
        )
        assignment = self.store.execute(
            "INSERT INTO assignments(run_id, bead_id, stage, scope_snapshot, created_at, updated_at) VALUES (?, 'b', 'queued', 'scope', ?, ?)",
            (run.lastrowid, now, now),
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "retry deadline"):
            self.store.execute(
                "UPDATE assignments SET stage = 'recovering' WHERE id = ?",
                (assignment.lastrowid,),
            )

    def test_terminal_action_automatically_releases_reservation(self) -> None:
        task = self.store.register_task(
            native_thread_id="terminal",
            role="executor",
            description="Terminal",
            model="sol",
            reasoning_effort="high",
            project_id="fulcrum",
        )
        now = "2026-01-01T00:00:00Z"
        action = self.store.execute(
            "INSERT INTO actions(task_id, kind, payload, state, created_at, updated_at) VALUES (?, 'implement', '{}', 'active', ?, ?)",
            (task["id"], now, now),
        )
        self.store.execute(
            "INSERT INTO reservations(action_id, state, created_at) VALUES (?, 'active', ?)",
            (action.lastrowid, now),
        )
        self.store.execute(
            "UPDATE actions SET state = 'failed' WHERE id = ?", (action.lastrowid,)
        )
        self.assertIsNone(
            self.store.row(
                "SELECT * FROM reservations WHERE action_id = ?",
                (action.lastrowid,),
            )
        )
        transition = self.store.row(
            "SELECT * FROM state_transitions WHERE entity_type = 'action' AND entity_id = ?",
            (str(action.lastrowid),),
        )
        self.assertEqual(transition["from_state"], "active")
        self.assertEqual(transition["to_state"], "failed")

    def test_existing_recovery_without_progress_is_migrated_to_a_hold(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            connection = sqlite3.connect(path)
            connection.execute("""CREATE TABLE assignments(
                     id INTEGER PRIMARY KEY, bead_id TEXT, stage TEXT,
                     condition TEXT, updated_at TEXT
                   )""")
            connection.execute(
                """INSERT INTO assignments(id, bead_id, stage, updated_at)
                   VALUES (1, 'b', 'recovering', '2026-01-01T00:00:00Z')"""
            )
            connection.commit()
            connection.close()
            with Store(path) as migrated:
                assignment = migrated.row("SELECT * FROM assignments WHERE id = 1")
                self.assertIsNotNone(assignment["operator_hold_id"])
                hold = migrated.row(
                    "SELECT * FROM holds WHERE id = ?",
                    (assignment["operator_hold_id"],),
                )
                self.assertIn("legacy recovery", hold["reason"])


if __name__ == "__main__":
    unittest.main()
