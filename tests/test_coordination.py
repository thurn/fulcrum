import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fulcrum.config import RuntimePaths
from fulcrum.coordination import transfer_archon, enroll_project
from fulcrum.records import validate_record
from fulcrum.state import atomic_write_record, read_record, OwnershipError


class CoordinationTests(unittest.TestCase):
    def test_handover_preserves_mandate_and_old_writer_yields(self):
        fixtures = Path(__file__).parent / "fixtures/records"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = RuntimePaths(root / "brain", root / "state", root / "config.json")
            registry = json.loads(
                (fixtures / "unresolved-task-identity.json").read_text()
            )
            assignment = json.loads(
                (fixtures / "healthy-review-wait-assignment.json").read_text()
            )
            previous_role = {
                "role": "archon",
                "project_id": "fulcrum",
                "host_id": "local",
                "task_id": "task-archon",
                "identity_state": "resolved",
                "role_number": None,
                "run_id": "persistent-archon",
                "pair_id": None,
                "title": "Former Archon",
                "selected_model": "example-model",
                "selected_reasoning": "medium",
                "model_authorization": {
                    "source": "human",
                    "reference": "human:handover",
                },
            }
            registry["roles"].append(previous_role)
            successor = dict(
                previous_role,
                task_id="new",
                run_id="persistent-archon-successor",
                title="Successor Archon",
            )
            assignment["mandate"] = {
                "candidate_id": "candidate-91",
                "scope": "docs/example-plan.md#task-1",
                "granted_at": "2026-09-11T22:15:00Z",
            }
            atomic_write_record(paths, registry)
            target = atomic_write_record(paths, assignment)
            before = target.read_bytes()
            with self.assertRaises(ValueError):
                transfer_archon(paths, "task-archon", "new", "", "2026-09-11T20:00:00Z")
            progress = transfer_archon(
                paths,
                "task-archon",
                successor,
                "previous task acknowledged relinquishment",
                "2026-09-11T20:00:00Z",
            )
            self.assertEqual(validate_record(progress)["role"], "archon")
            self.assertEqual(progress["role_task_id"], "new")
            transferred = read_record(paths, "role_run_registry")
            self.assertEqual(transferred["current_archon_task_id"], "new")
            self.assertEqual(transferred["writer_id"], "new")
            self.assertEqual(transferred["roles"][0], registry["roles"][0])
            self.assertIn(previous_role, transferred["roles"])
            self.assertIn(successor, transferred["roles"])
            self.assertEqual(target.read_bytes(), before)
            with self.assertRaises(OwnershipError):
                atomic_write_record(paths, registry)
            with self.assertRaises(OwnershipError):
                transfer_archon(
                    paths,
                    "task-archon",
                    dict(successor, task_id="other", title="Other Archon"),
                    "stale acknowledgment",
                    "2026-09-11T20:00:00Z",
                )
            with self.assertRaises(ValueError):
                transfer_archon(
                    paths,
                    "new",
                    dict(successor, task_id="task-archon"),
                    "conflicting registration",
                    "2026-09-11T20:00:00Z",
                )

    def test_interrupted_transfer_preserves_registry_and_assignments(self):
        fixtures = Path(__file__).parent / "fixtures/records"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = RuntimePaths(root / "brain", root / "state", root / "config.json")
            registry = json.loads(
                (fixtures / "unresolved-task-identity.json").read_text()
            )
            previous_role = {
                "role": "archon",
                "project_id": "fulcrum",
                "host_id": "local",
                "task_id": "task-archon",
                "identity_state": "resolved",
                "role_number": None,
                "run_id": "persistent-archon",
                "pair_id": None,
                "title": "Former Archon",
                "selected_model": "example-model",
                "selected_reasoning": "medium",
                "model_authorization": {
                    "source": "human",
                    "reference": "human:handover",
                },
            }
            registry["roles"].append(previous_role)
            successor = dict(
                previous_role,
                task_id="new",
                run_id="persistent-archon-successor",
                title="Successor Archon",
            )
            assignment = json.loads(
                (fixtures / "healthy-review-wait-assignment.json").read_text()
            )
            assignment["mandate"] = {
                "candidate_id": "candidate-91",
                "scope": "docs/example-plan.md#task-1",
                "granted_at": "2026-09-11T22:15:00Z",
            }
            registry_target = atomic_write_record(paths, registry)
            assignment_target = atomic_write_record(paths, assignment)
            registry_before = registry_target.read_bytes()
            assignment_before = assignment_target.read_bytes()
            with patch(
                "fulcrum.state.os.replace", side_effect=OSError("transfer interrupted")
            ):
                with self.assertRaisesRegex(OSError, "transfer interrupted"):
                    transfer_archon(
                        paths,
                        "task-archon",
                        successor,
                        "previous task acknowledged relinquishment",
                        "2026-09-11T20:00:00Z",
                    )
            self.assertEqual(registry_target.read_bytes(), registry_before)
            self.assertEqual(assignment_target.read_bytes(), assignment_before)

    def test_enrollment_requires_matching_healthy_observations(self):
        project = dict(
            project_id="example",
            repo_path="/tmp/project",
            host_id="local",
            codex_project_id="codex",
            tollgate_repo_id="tg",
            enabled=False,
        )
        facts = dict(
            git_root="/tmp/project",
            codex_id="codex",
            codex_path="/tmp/project",
            codex_host="local",
            codex_is_git=True,
            tollgate_id="tg",
            tollgate_path="/tmp/project",
            tollgate_healthy=True,
        )
        self.assertTrue(enroll_project(project, **facts)[0]["enabled"])
        for field, bad in (
            ("git_root", None),
            ("codex_id", "wrong"),
            ("codex_host", "other"),
            ("codex_is_git", False),
            ("tollgate_path", "/tmp/elsewhere"),
            ("tollgate_healthy", None),
            ("tollgate_healthy", False),
        ):
            with self.subTest(field=field, bad=bad):
                entry, reasons = enroll_project(project, **dict(facts, **{field: bad}))
                self.assertFalse(entry["enabled"])
                self.assertTrue(reasons)
        self.assertFalse(project["enabled"])
