from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

from fulcrum.ledger import Ledger


class Fulcrum2WorkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name).resolve()
        cls.brain = cls.root / "brain"
        cls.instance = cls.root / "instance"
        cls.project = cls.root / "project"
        for path in (cls.brain, cls.instance, cls.project):
            path.mkdir()
        for path in (cls.brain, cls.project):
            subprocess.run(["git", "init", "--quiet"], cwd=path, check=True)
            subprocess.run(
                ["git", "config", "user.email", "fixture@example.com"],
                cwd=path,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Fixture"], cwd=path, check=True
            )
            subprocess.run(
                ["git", "config", "beads.role", "maintainer"], cwd=path, check=True
            )
        subprocess.run(
            [
                "bd",
                "init",
                "--prefix",
                "fc",
                "--non-interactive",
                "--skip-agents",
                "--skip-hooks",
            ],
            cwd=cls.brain,
            capture_output=True,
            check=True,
            timeout=60,
        )
        cls.config = cls.brain / "fulcrum.yaml"
        cls.config.write_text(
            f"brain:\n  root: {cls.brain}\nprojects:\n  toy:\n    root: {cls.project}\n    enabled: true\n",
            encoding="utf-8",
        )
        (cls.instance / "config").symlink_to(cls.config)
        cls.executable = Path(os.sys.executable).with_name("fulcrum")

    @classmethod
    def tearDownClass(cls) -> None:
        subprocess.run(
            ["bd", "-C", str(cls.brain), "dolt", "stop"],
            capture_output=True,
            check=False,
            timeout=20,
        )
        cls.temporary.cleanup()

    def invoke(
        self, *arguments: str, payload: dict[str, object] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.executable), *arguments],
            input=json.dumps(payload) if payload is not None else None,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    def create_work(
        self, title: str, *, request_id: str | None = None
    ) -> tuple[str, dict[str, object]]:
        request_id = request_id or str(uuid.uuid4())
        payload: dict[str, object] = {
            "title": title,
            "outcome": f"Produce the {title} outcome",
            "project": "toy",
            "acceptance": [f"{title} is observable"],
            "requested_role": "executor",
        }
        result = self.invoke(
            "work",
            "create",
            "--instance",
            str(self.instance),
            "--offline",
            "--actor",
            "human",
            "--request-id",
            request_id,
            "--input",
            "-",
            "--json",
            payload=payload,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        envelope = json.loads(result.stdout)
        return envelope["result"]["result"]["bead_id"], payload

    def test_graph_creation_is_idempotent_and_dependencies_are_native(self) -> None:
        request_id = str(uuid.uuid4())
        payload = {
            "title": "Ordering plan",
            "outcome": "Deliver two ordered components",
            "project": "toy",
            "acceptance": ["Both components exist"],
            "children": [
                {
                    "key": "first",
                    "title": "First component",
                    "outcome": "Build first",
                    "acceptance": ["First passes"],
                },
                {
                    "key": "second",
                    "title": "Second component",
                    "outcome": "Build second",
                    "acceptance": ["Second passes"],
                    "depends_on": ["first"],
                },
            ],
        }
        arguments = (
            "work",
            "create",
            "--instance",
            str(self.instance),
            "--offline",
            "--actor",
            "human",
            "--request-id",
            request_id,
            "--input",
            "-",
            "--json",
        )
        first = self.invoke(*arguments, payload=payload)
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        first_result = json.loads(first.stdout)["result"]["result"]
        second = self.invoke(*arguments, payload=payload)
        self.assertEqual(second.returncode, 0, second.stderr + second.stdout)
        self.assertEqual(json.loads(second.stdout)["result"]["result"], first_result)
        self.assertEqual(len(first_result["children_by_key"]), 2)
        ledger = Ledger(self.brain)
        dependent = first_result["children_by_key"]["second"]
        prerequisite = first_result["children_by_key"]["first"]
        self.assertEqual(ledger.dependencies(dependent), [prerequisite])
        self.assertEqual(
            {item.id for item in ledger.children(first_result["bead_id"])},
            {dependent, prerequisite},
        )

    def test_dependency_cycle_is_rejected_before_an_edge_is_written(self) -> None:
        first, _ = self.create_work("Cycle first")
        second, _ = self.create_work("Cycle second")
        added = self.invoke(
            "work",
            "dependencies",
            first,
            "--instance",
            str(self.instance),
            "--offline",
            "--actor",
            "human",
            "--input",
            "-",
            "--json",
            payload={"add": [second], "remove": []},
        )
        self.assertEqual(added.returncode, 0, added.stderr + added.stdout)
        rejected = self.invoke(
            "work",
            "dependencies",
            second,
            "--instance",
            str(self.instance),
            "--offline",
            "--actor",
            "human",
            "--input",
            "-",
            "--json",
            payload={"add": [first], "remove": []},
        )
        self.assertEqual(rejected.returncode, 2)
        self.assertEqual(json.loads(rejected.stdout)["error"]["code"], "INVALID_INPUT")
        self.assertEqual(Ledger(self.brain).dependencies(second), [])

    def test_reopen_invalidates_old_acquisition_for_the_same_task(self) -> None:
        bead_id, _ = self.create_work("Ownership cycle")
        original = json.loads(
            self.invoke(
                "work", "show", bead_id, "--instance", str(self.instance), "--json"
            ).stdout
        )["result"]["ownership_operation"]
        closed = self.invoke(
            "work",
            "close",
            bead_id,
            "--outcome",
            "answered",
            "--summary",
            "Initial question was answered",
            "--instance",
            str(self.instance),
            "--offline",
            "--actor",
            "human",
            "--json",
        )
        self.assertEqual(closed.returncode, 0, closed.stderr + closed.stdout)
        closed_root = Ledger(self.brain).show(bead_id)
        assert closed_root is not None and closed_root.fc
        completion_cost = closed_root.fc.get("completion_cost")
        self.assertEqual(completion_cost["state"], "finalized")
        self.assertEqual(completion_cost["coverage"], "unknown")
        self.assertIsNone(completion_cost["estimated_api_cost_usd"])
        self.assertIsNotNone(Ledger(self.brain).show(completion_cost["summary_bead"]))
        duplicate = self.invoke(
            "work",
            "close",
            bead_id,
            "--outcome",
            "answered",
            "--summary",
            "Initial question was answered",
            "--instance",
            str(self.instance),
            "--offline",
            "--actor",
            "human",
            "--json",
        )
        self.assertEqual(duplicate.returncode, 0, duplicate.stderr + duplicate.stdout)
        unchanged = Ledger(self.brain).show(bead_id)
        assert unchanged is not None and unchanged.fc
        self.assertEqual(unchanged.fc["completion_cost"], completion_cost)
        reopened = self.invoke(
            "work",
            "reopen",
            bead_id,
            "--reason",
            "New evidence needs investigation",
            "--role",
            "weaver",
            "--thread-id",
            "worker-one",
            "--instance",
            str(self.instance),
            "--offline",
            "--actor",
            "human",
            "--json",
        )
        self.assertEqual(reopened.returncode, 0, reopened.stderr + reopened.stdout)
        current = json.loads(reopened.stdout)["result"]["result"]["ownership_operation"]
        self.assertNotEqual(current, original)

        stale = self.invoke(
            "progress",
            "--bead",
            bead_id,
            "--kind",
            "investigation",
            "--summary",
            "Found relevant new evidence",
            "--evidence",
            "artifact://new-evidence",
            "--thread-id",
            "worker-one",
            "--ownership-operation",
            original,
            "--actor",
            "task:worker-one",
            "--instance",
            str(self.instance),
            "--offline",
            "--json",
        )
        self.assertEqual(stale.returncode, 5)
        self.assertEqual(
            json.loads(stale.stdout)["error"]["code"], "OWNERSHIP_CONFLICT"
        )
        accepted = self.invoke(
            "progress",
            "--bead",
            bead_id,
            "--kind",
            "investigation",
            "--summary",
            "Found relevant new evidence",
            "--evidence",
            "artifact://new-evidence",
            "--thread-id",
            "worker-one",
            "--ownership-operation",
            current,
            "--actor",
            "task:worker-one",
            "--instance",
            str(self.instance),
            "--offline",
            "--json",
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr + accepted.stdout)

    def test_native_intake_adoption_preserves_original_and_detects_assignee_interference(
        self,
    ) -> None:
        created = subprocess.run(
            [
                "bd",
                "-C",
                str(self.brain),
                "--actor",
                "project:toy",
                "--json",
                "create",
                "--title",
                "Native request",
                "--description",
                "Original native outcome",
                "--acceptance",
                "Native acceptance",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        bead_id = json.loads(created.stdout)["id"]
        adopted = self.invoke(
            "work",
            "adopt",
            bead_id,
            "--role",
            "weaver",
            "--instance",
            str(self.instance),
            "--offline",
            "--actor",
            "human",
            "--json",
        )
        self.assertEqual(adopted.returncode, 0, adopted.stderr + adopted.stdout)
        shown = json.loads(
            self.invoke(
                "work", "show", bead_id, "--instance", str(self.instance), "--json"
            ).stdout
        )["result"]
        self.assertEqual(shown["fc"]["outcome"], "Original native outcome")
        self.assertEqual(
            shown["fc"]["origin"]["native_snapshot"]["description"],
            "Original native outcome",
        )

        subprocess.run(
            [
                "bd",
                "-C",
                str(self.brain),
                "update",
                bead_id,
                "--assignee",
                "outside-task",
            ],
            capture_output=True,
            check=True,
            timeout=30,
        )
        conflicted = self.invoke(
            "work",
            "update",
            bead_id,
            "--instance",
            str(self.instance),
            "--offline",
            "--actor",
            "human",
            "--input",
            "-",
            "--json",
            payload={"summary": "Should not overwrite reassignment"},
        )
        self.assertEqual(conflicted.returncode, 5)
        self.assertEqual(
            json.loads(conflicted.stdout)["error"]["code"], "OWNERSHIP_CONFLICT"
        )

    def test_report_is_independent_and_delivered_close_needs_evidence(self) -> None:
        request_id = str(uuid.uuid4())
        payload = {
            "title": "Repair formatter probe",
            "problem": "The formatter probe omitted a missing binary",
            "observed_evidence": "Fixture command exited before diagnostics",
            "required_change": "Report the missing formatter clearly",
            "acceptance_checks": ["Missing formatter has an actionable error"],
            "project": "toy",
            "discovered_from": "fc-existing",
        }
        args = (
            "report",
            "--instance",
            str(self.instance),
            "--offline",
            "--actor",
            "human",
            "--thread-id",
            "reporter-task",
            "--request-id",
            request_id,
            "--input",
            "-",
            "--json",
        )
        first = self.invoke(*args, payload=payload)
        second = self.invoke(*args, payload=payload)
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        first_id = json.loads(first.stdout)["result"]["result"]["bead_id"]
        self.assertEqual(
            json.loads(second.stdout)["result"]["result"]["bead_id"], first_id
        )
        shown = json.loads(
            self.invoke(
                "work", "show", first_id, "--instance", str(self.instance), "--json"
            ).stdout
        )["result"]
        self.assertEqual(shown["fc"]["report"]["reporter_task"], "reporter-task")
        self.assertNotEqual(shown["effective_owner"], "reporter-task")

        rejected = self.invoke(
            "work",
            "close",
            first_id,
            "--outcome",
            "delivered",
            "--summary",
            "Claimed delivery",
            "--instance",
            str(self.instance),
            "--offline",
            "--actor",
            "human",
            "--json",
        )
        self.assertEqual(rejected.returncode, 5)
        self.assertEqual(
            json.loads(rejected.stdout)["error"]["code"], "DELIVERY_NOT_OBSERVED"
        )


if __name__ == "__main__":
    unittest.main()
