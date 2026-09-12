from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fulcrum.readiness import state_readiness
from fulcrum.store import Store


class ReadinessTest(unittest.TestCase):
    def test_requires_every_capacity_and_default_recurring_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Store(Path(directory) / "state.sqlite3") as store:
                store.execute(
                    "INSERT INTO projects(project_id, repo_path) VALUES ('one', '/one')"
                )
                store.register_task(
                    native_thread_id="archon",
                    role="archon",
                    description="",
                    model="sol",
                    reasoning_effort="high",
                )
                ready, reasons = state_readiness(store)
                self.assertFalse(ready)
                self.assertIn("global capacity is missing", reasons)
                self.assertIn("project capacity is missing: one", reasons)
                self.assertTrue(any("fleet Sage" in reason for reason in reasons))
                self.assertTrue(any("one Inquisitor" in reason for reason in reasons))

                store.execute(
                    "INSERT INTO meta(key, value) VALUES ('global_limit', '4')"
                )
                store.execute(
                    "INSERT INTO meta(key, value) VALUES ('project_limits', ?)",
                    (json.dumps({"one": 2}),),
                )
                store.execute(
                    "INSERT INTO policies(kind, scope, cadence_seconds, anchor_at, next_due_at, active) VALUES ('sage', NULL, 86400, 'now', 'later', 1)"
                )
                store.execute(
                    "INSERT INTO policies(kind, scope, cadence_seconds, anchor_at, next_due_at, active) VALUES ('inquisitor', 'one', 86400, 'now', 'later', 1)"
                )
                ready, reasons = state_readiness(store)
                self.assertTrue(ready)
                self.assertEqual(reasons, [])


if __name__ == "__main__":
    unittest.main()
