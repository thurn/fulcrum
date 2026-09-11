"""Tests for concise task context assembly."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fulcrum.config import RuntimePaths
from fulcrum.context import read_task_context
from fulcrum.state import atomic_write_record

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "records"


def fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURE_ROOT / name).read_text())


class ContextReaderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.paths = RuntimePaths(
            brain_root=root / "brain",
            state_root=root / "state",
            config_file=root / "state" / "config.json",
        )
        registry = fixture("unresolved-task-identity.json")
        registry["roles"][0].update(  # type: ignore[index,union-attr]
            {"task_id": "task-executor-3", "identity_state": "resolved"}
        )
        registry["roles"][0].pop("client_thread_id")  # type: ignore[index,union-attr]
        atomic_write_record(self.paths, registry)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_unknown_task_gets_no_private_role_context(self) -> None:
        result = read_task_context(self.paths, "not-registered")
        self.assertEqual(
            result, {"task_id": "not-registered", "known": False, "errors": []}
        )
        self.assertNotIn("role", result)
        self.assertNotIn("assignments", result)

    def test_missing_memory_does_not_hide_assignment(self) -> None:
        atomic_write_record(self.paths, fixture("healthy-review-wait-assignment.json"))
        atomic_write_record(self.paths, fixture("paused-work.json"))
        result = read_task_context(self.paths, "task-executor-3")
        self.assertTrue(result["known"])
        self.assertEqual(len(result["assignments"]), 1)
        self.assertEqual(result["assignments"][0]["bead_id"], "fc-91")
        self.assertIsNone(result["memory"]["global"])
        self.assertGreaterEqual(len(result["errors"]), 2)

    def test_invalid_registry_is_visible_instead_of_empty_fleet(self) -> None:
        registry_path = self.paths.state_root / "registry" / "roles.json"
        registry_path.write_text("{not json", encoding="utf-8")
        result = read_task_context(self.paths, "task-executor-3")
        self.assertFalse(result["known"])
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("roles.json", result["errors"][0]["path"])


if __name__ == "__main__":
    unittest.main()
