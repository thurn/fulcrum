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


if __name__ == "__main__":
    unittest.main()
