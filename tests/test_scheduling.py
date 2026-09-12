from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fulcrum.scheduling import assignment_blockers, can_start_task, next_cadence
from fulcrum.store import Store


class SchedulingTest(unittest.TestCase):
    def test_idle_requires_all_runtime_facts(self) -> None:
        task = {
            "last_turn_terminal": 1,
            "runtime_status": "idle",
            "helpers_terminal": 1,
            "state": "idle",
        }
        self.assertTrue(can_start_task(task))
        self.assertFalse(can_start_task({**task, "helpers_terminal": 0}))
        self.assertFalse(can_start_task(task, unresolved_start=True))

    def test_cadence_skips_missed_intervals(self) -> None:
        anchor = datetime(2026, 1, 1, tzinfo=timezone.utc)
        now = datetime(2026, 1, 3, 12, tzinfo=timezone.utc)
        self.assertEqual(
            next_cadence(anchor, 86400, now),
            datetime(2026, 1, 4, tzinfo=timezone.utc),
        )

    def test_holds_capacity_and_dependencies_compose(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            Store(Path(directory) / "state.db") as store,
        ):
            now = "2026-01-01T00:00:00Z"
            store.execute(
                "INSERT INTO projects(project_id, repo_path) VALUES ('p', '/tmp/p')"
            )
            store.execute(
                "INSERT INTO beads VALUES ('b','k','p','t','scope','pending','sol','high','sol','high','default',NULL,NULL,'[]','complete',?,?)",
                (now, now),
            )
            run = store.execute(
                "INSERT INTO runs(project_id, authority, created_at, updated_at) VALUES ('p','archon',?,?)",
                (now, now),
            )
            assignment = {
                "id": 1,
                "run_id": run.lastrowid,
                "bead_id": "b",
                "project_id": "p",
            }
            blockers = assignment_blockers(store, assignment)
            self.assertIn("global capacity", " ".join(blockers))
            store.execute(
                "INSERT INTO meta VALUES ('global_limit','1'), ('project_limits',?)",
                (json.dumps({"p": 1}),),
            )
            store.execute(
                "INSERT INTO holds(scope,target,reason,release_condition,created_at) VALUES ('project','p','quiet','manual',?)",
                (now,),
            )
            self.assertIn("quiet", " ".join(assignment_blockers(store, assignment)))


if __name__ == "__main__":
    unittest.main()
