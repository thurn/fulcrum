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


class FailOnceBeads(FakeBeads):
    def create(self, task: Any) -> str:
        if not self.tasks:
            self.tasks.append(task)
            raise OSError("deterministic publication failure")
        self.tasks.append(task)
        return "fc-recovered"


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

    def test_successful_retry_supersedes_failed_publication(self) -> None:
        draft = task_from_payload(
            {
                "project": "fulcrum",
                "title": "Retry publication",
                "description": "Publish one durable bead after a deterministic failure.",
            },
            intake_key="retry-publication",
        )
        beads = FailOnceBeads()

        with self.assertRaisesRegex(OSError, "deterministic publication failure"):
            file_task(self.store, beads, draft)  # type: ignore[arg-type]
        result = file_task(self.store, beads, draft)  # type: ignore[arg-type]

        self.assertEqual(result["bead_id"], "fc-recovered")
        self.assertEqual(
            self.store.rows(
                "SELECT bead_id FROM beads WHERE intake_key = 'retry-publication'"
            ),
            [{"bead_id": "fc-recovered"}],
        )
        operations = self.store.rows("""SELECT state, condition FROM external_operations
               WHERE kind = 'beads_create' AND target = 'retry-publication'
               ORDER BY id""")
        self.assertEqual(
            [operation["state"] for operation in operations], ["canceled", "complete"]
        )
        self.assertEqual(
            operations[0]["condition"], "deterministic publication failure"
        )
        obligation = self.store.row("""SELECT state, detail FROM obligations
               WHERE kind = 'beads_publication' AND identity = 'retry-publication'""")
        self.assertEqual(obligation, {"state": "complete", "detail": None})
        status = self.store.status()
        self.assertEqual(status["operations"], [])
        self.assertEqual(status["obligations"], [])

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


if __name__ == "__main__":
    unittest.main()
