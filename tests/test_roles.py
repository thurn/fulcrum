import json
from pathlib import Path
import unittest

from fulcrum.roles import (
    resolve_identity,
    next_role_number,
    initialize_progress,
    prepare_handoff,
    finish_handoff,
)


class RoleTests(unittest.TestCase):
    def setUp(self):
        self.role = json.loads(
            (
                Path(__file__).parent / "fixtures/records/unresolved-task-identity.json"
            ).read_text()
        )["roles"][0]
        self.now = "2026-09-11T20:00:00Z"

    def test_pending_ambiguous_and_duplicate_resolution(self):
        for matches in (
            [],
            [("one", "local"), ("two", "local")],
            [(self.role["client_thread_id"], "local")],
        ):
            with self.assertRaises(ValueError):
                resolve_identity(self.role, matches)
        resolved = resolve_identity(self.role, [("real", "local")])
        self.assertEqual(resolve_identity(resolved, [("real", "local")]), resolved)
        with self.assertRaises(ValueError):
            resolve_identity(resolved, [("other", "local")])
        self.assertIsNone(self.role["task_id"])

    def test_activation_and_uncertain_handoff(self):
        with self.assertRaises(ValueError):
            initialize_progress(self.role, self.now)
        role = resolve_identity(self.role, [("real", "local")])
        with self.assertRaises(ValueError):
            initialize_progress(role, self.now, plan_mode=True)
        progress = initialize_progress(role, self.now)
        recipient = resolve_identity(self.role, [("recipient", "local")])
        pending = prepare_handoff(progress, recipient, "Review candidate", self.now)
        failed = finish_handoff(pending, self.now, delivered=False)
        with self.assertRaises(ValueError):
            prepare_handoff(failed, recipient, "retry", self.now)
        sent = finish_handoff(failed, self.now, delivered=True)
        self.assertTrue(sent["handoff_sent"])
        self.assertEqual(sent["expected_next_actor"], "recipient")
        self.assertFalse(progress["handoff_needed"])

    def test_pair_numbers_preserve_history(self):
        self.role.update(role="executor", role_number=8)
        self.assertEqual(next_role_number([self.role], "overseer"), 9)
        self.assertEqual(next_role_number([self.role], "sage"), 1)
