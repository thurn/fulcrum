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
from fulcrum.records import validate_record


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
        pending = prepare_handoff(
            progress,
            recipient,
            "Review candidate",
            self.now,
            expected_by="2026-09-11T21:00:00Z",
        )
        self.assertEqual(pending["expected_by"], "2026-09-11T21:00:00Z")
        failed = finish_handoff(pending, self.now, delivered=False)
        with self.assertRaises(ValueError):
            prepare_handoff(failed, recipient, "retry", self.now)
        sent = finish_handoff(failed, self.now, delivered=True)
        self.assertTrue(sent["handoff_sent"])
        self.assertEqual(sent["expected_next_actor"], "recipient")
        self.assertFalse(progress["handoff_needed"])

    def test_weaver_cannot_enter_registered_role_state(self):
        weaver = dict(self.role, role="weaver", task_id="task-weaver")
        with self.assertRaisesRegex(ValueError, "Weaver is ephemeral"):
            resolve_identity(weaver, [("task-weaver", "local")])
        with self.assertRaisesRegex(ValueError, "Weaver is ephemeral"):
            initialize_progress(weaver, self.now)

        role = resolve_identity(self.role, [("real", "local")])
        progress = initialize_progress(role, self.now)
        with self.assertRaisesRegex(ValueError, "Weaver is ephemeral"):
            prepare_handoff(progress, weaver, "Report result", self.now)

    def test_retained_weaver_records_remain_readable(self):
        weaver = dict(self.role, role="weaver", task_id="task-weaver")
        registry = dict(
            self.role_registry(),
            roles=[weaver],
        )
        self.assertEqual(validate_record(registry)["roles"][0]["role"], "weaver")
        progress = {
            "record_kind": "progress",
            "schema_version": 1,
            "writer_id": "task-weaver",
            "updated_at": self.now,
            "role_task_id": "task-weaver",
            "role": "weaver",
            "phase": "completed",
            "phase_started_at": self.now,
            "expected_next_actor": None,
            "expected_next_action": "Retained historical record",
            "handoff_needed": False,
            "handoff_sent": True,
            "delivery_error": None,
            "owned_resources": [],
        }
        self.assertEqual(validate_record(progress)["role"], "weaver")

    def role_registry(self):
        return {
            "record_kind": "role_run_registry",
            "schema_version": 1,
            "writer_id": "task-archon",
            "updated_at": self.now,
            "current_archon_task_id": "task-archon",
            "roles": [],
        }

    def test_pair_numbers_preserve_history(self):
        self.role.update(role="executor", role_number=8)
        self.assertEqual(next_role_number([self.role], "overseer"), 9)
        self.assertEqual(next_role_number([self.role], "sage"), 1)
