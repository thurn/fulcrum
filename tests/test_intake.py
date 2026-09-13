from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from fulcrum.intake import file_graph, file_task, task_from_payload
from fulcrum.store import Store, StoreError


class FakeBeads:
    def __init__(self) -> None:
        self.tasks: list[Any] = []

    def create(self, task: Any) -> str:
        self.tasks.append(task)
        return f"fc-{len(self.tasks)}"


class IntakeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "state.db")
        self.store.execute(
            "INSERT INTO projects(project_id, repo_path) VALUES ('fulcrum', '/tmp/fulcrum')"
        )
        self.beads = FakeBeads()

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_small_task_defaults_and_idempotency(self) -> None:
        draft = task_from_payload(
            {
                "project": "fulcrum",
                "title": "Correct install example",
                "description": "Update the example and test the documented command.",
            },
            intake_key="stable",
        )
        first = file_task(self.store, self.beads, draft)  # type: ignore[arg-type]
        second = file_task(self.store, self.beads, draft)  # type: ignore[arg-type]
        self.assertEqual(first["bead_id"], "fc-1")
        self.assertTrue(second["reused"])
        self.assertEqual(len(self.beads.tasks), 1)
        row = self.store.row("SELECT * FROM beads WHERE bead_id = 'fc-1'")
        self.assertEqual(row["executor_model"], "gpt-5.6-sol")
        self.assertEqual(row["overseer_reasoning_effort"], "high")

    def test_successful_same_key_retry_supersedes_failed_publication_records(
        self,
    ) -> None:
        draft = task_from_payload(
            {"project": "fulcrum", "title": "Stable", "description": "Scope"},
            intake_key="stable",
        )
        first = file_task(self.store, self.beads, draft)  # type: ignore[arg-type]
        now = "2026-01-01T00:00:00Z"
        self.store.execute(
            """INSERT INTO external_operations(
                   kind, target, input_json, state, correlation_id, created_at, updated_at
               ) VALUES ('beads_create', 'stable', '{}', 'failed', 'old', ?, ?)""",
            (now, now),
        )
        self.store.execute(
            """INSERT INTO obligations(kind, identity, target, state, detail, created_at, updated_at)
               VALUES ('beads_publication', 'stable', 'fulcrum', 'failed', 'old', ?, ?)""",
            (now, now),
        )
        second = file_task(self.store, self.beads, draft)  # type: ignore[arg-type]
        self.assertEqual(second["bead_id"], first["bead_id"])
        self.assertEqual(
            self.store.row("SELECT state FROM obligations WHERE identity = 'stable'")[
                "state"
            ],
            "complete",
        )
        self.assertEqual(
            self.store.row(
                "SELECT state FROM external_operations WHERE correlation_id = 'old'"
            )["state"],
            "canceled",
        )

    def test_graph_is_topological_and_marked_complete(self) -> None:
        result = file_graph(
            self.store,
            self.beads,  # type: ignore[arg-type]
            {
                "project": "fulcrum",
                "tasks": [
                    {
                        "intake_key": "second",
                        "title": "Second",
                        "description": "Do second and verify it.",
                        "depends_on": ["first"],
                    },
                    {
                        "intake_key": "first",
                        "title": "First",
                        "description": "Do first and verify it.",
                    },
                ],
            },
            group_id="graph",
        )
        self.assertEqual(result["bead_ids"], ["fc-1", "fc-2"])
        self.assertEqual(self.beads.tasks[0].intake_key, "first")
        self.assertEqual(self.beads.tasks[1].dependencies, ("fc-1",))
        self.assertEqual(
            self.store.row("SELECT state FROM intake_groups WHERE id = 'graph'")[
                "state"
            ],
            "complete",
        )

    def test_graph_cycle_fails_before_publication(self) -> None:
        with self.assertRaisesRegex(StoreError, "cycle"):
            file_graph(
                self.store,
                self.beads,  # type: ignore[arg-type]
                {
                    "project": "fulcrum",
                    "tasks": [
                        {
                            "intake_key": "a",
                            "title": "A",
                            "description": "A scope",
                            "depends_on": ["b"],
                        },
                        {
                            "intake_key": "b",
                            "title": "B",
                            "description": "B scope",
                            "depends_on": ["a"],
                        },
                    ],
                },
            )
        self.assertEqual(self.beads.tasks, [])

    def test_direct_and_graph_intake_retain_the_calling_weaver_lineage(self) -> None:
        weaver = self.store.register_task(
            native_thread_id="weaver-thread",
            role="weaver",
            description="File related work",
            model="sol",
            reasoning_effort="high",
            project_id="fulcrum",
        )
        executor = self.store.register_task(
            native_thread_id="executor-thread",
            role="executor",
            description="Prior work",
            model="sol",
            reasoning_effort="high",
            project_id="fulcrum",
            lineage_number=int(weaver["lineage_number"]),
        )
        self.store.execute(
            """INSERT INTO obligations(
                   kind, identity, target, state, created_at, updated_at
               ) VALUES ('archive', ?, ?, 'pending', 'now', 'now')""",
            (str(executor["id"]), executor["native_thread_id"]),
        )
        direct = file_task(
            self.store,
            self.beads,  # type: ignore[arg-type]
            task_from_payload(
                {"project": "fulcrum", "title": "Direct", "description": "Scope"},
                intake_key="direct-lineage",
            ),
            weaver_task_id=int(weaver["id"]),
        )
        graph = file_graph(
            self.store,
            self.beads,  # type: ignore[arg-type]
            {
                "project": "fulcrum",
                "tasks": [
                    {"title": "First", "description": "First scope"},
                    {"title": "Second", "description": "Second scope"},
                ],
            },
            group_id="lineage-graph",
            weaver_task_id=int(weaver["id"]),
        )
        retained = self.store.rows(
            """SELECT bead_id, weaver_task_id, lineage_number FROM bead_lineages
               WHERE bead_id IN (?, ?, ?) ORDER BY bead_id""",
            (direct["bead_id"], *graph["bead_ids"]),
        )
        self.assertEqual(len(retained), 3)
        self.assertEqual({row["weaver_task_id"] for row in retained}, {weaver["id"]})
        self.assertEqual(
            {row["lineage_number"] for row in retained}, {weaver["role_number"]}
        )
        self.assertEqual(
            self.store.row(
                "SELECT state FROM obligations WHERE target = 'executor-thread'"
            )["state"],
            "canceled",
        )


if __name__ == "__main__":
    unittest.main()
