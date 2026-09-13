from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fulcrum.scheduling import (
    assignment_blockers,
    can_start_task,
    next_cadence,
    ready_assignments,
)
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

    def test_only_the_earliest_unfinished_bead_in_a_run_is_ready(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            Store(Path(directory) / "state.db") as store,
        ):
            now = "2026-01-01T00:00:00Z"
            store.execute(
                "INSERT INTO projects(project_id, repo_path) VALUES ('p', '/tmp/p')"
            )
            for bead_id in ("b-1", "b-2"):
                store.execute(
                    """INSERT INTO beads(
                           bead_id, intake_key, project_id, title, description,
                           activation, executor_model, executor_reasoning_effort,
                           overseer_model, overseer_reasoning_effort,
                           model_provenance, publication_state, created_at, updated_at
                       ) VALUES (?, ?, 'p', ?, 'scope', 'pending', 'sol', 'high',
                                 'sol', 'high', 'default', 'complete', ?, ?)""",
                    (bead_id, bead_id, bead_id, now, now),
                )
            run = store.execute(
                "INSERT INTO runs(project_id, authority, created_at, updated_at) VALUES ('p','archon',?,?)",
                (now, now),
            )
            for position, bead_id in enumerate(("b-1", "b-2")):
                store.execute(
                    "INSERT INTO run_beads(run_id, bead_id, position, scope_snapshot) VALUES (?, ?, ?, 'scope')",
                    (run.lastrowid, bead_id, position),
                )
                store.execute(
                    "INSERT INTO assignments(run_id, bead_id, stage, scope_snapshot, created_at, updated_at) VALUES (?, ?, 'queued', 'scope', ?, ?)",
                    (run.lastrowid, bead_id, now, now),
                )
            store.execute(
                "INSERT INTO meta(key, value) VALUES ('global_limit','1'), ('project_limits','{\"p\":1}'), ('dispatch_enabled','1')"
            )
            self.assertEqual(
                [row["bead_id"] for row in ready_assignments(store)], ["b-1"]
            )
            store.execute(
                "UPDATE assignments SET stage = 'completed' WHERE bead_id = 'b-1'"
            )
            self.assertEqual(
                [row["bead_id"] for row in ready_assignments(store)], ["b-2"]
            )


if __name__ == "__main__":
    unittest.main()
