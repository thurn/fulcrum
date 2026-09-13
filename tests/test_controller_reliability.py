from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fulcrum.config import InstallationConfig, ProjectConfig, RuntimePaths
from fulcrum.controller import Controller
from fulcrum.lifecycle import apply_archon_decisions
from fulcrum.store import StoreError
from fulcrum.tollgate import TollgateUncertainError


class FakeCandidateTollgate:
    def __init__(self, worktree: str) -> None:
        self.worktree = worktree
        self.submissions: list[Path | None] = []

    def status(self, _repository: str, _candidate: str | None = None) -> dict[str, Any]:
        if not self.submissions:
            return {"queue": []}
        return {
            "queue": [
                {
                    "item": {
                        "id": "candidate-1",
                        "metadata": {"worktree_path": self.worktree},
                        "source_oid": "source-1",
                    },
                    "generation": {"tested_oid": "tested-1"},
                }
            ]
        }

    def submit_candidate(
        self, _repository: str, _revision: str, *, cwd: Path | None = None
    ) -> dict[str, Any]:
        self.submissions.append(cwd)
        return {"id": "candidate-1"}


class FakeApprovalTollgate:
    def approve(self, _repository: str, _candidate: str) -> dict[str, Any]:
        raise TollgateUncertainError(
            "approval output was malformed; result is uncertain",
            stdout="promoted but not json",
        )

    def status(self, _repository: str, candidate: str | None = None) -> dict[str, Any]:
        return {
            "configuration": {"remote_enabled": True},
            "candidate": {
                "item": {
                    "id": candidate,
                    "state": "promoted",
                    "remote_state": "synchronized",
                    "cleanup_state": "completed",
                    "certificate_id": "certificate-1",
                }
            },
        }


class FakeBeads:
    def __init__(self) -> None:
        self.closed: list[str] = []

    def close(self, bead_id: str, _reason: str) -> None:
        self.closed.append(bead_id)


class FakeArchiveRuntime:
    def __init__(self) -> None:
        self.archived: list[str] = []

    async def archive(self, thread_id: str) -> None:
        self.archived.append(thread_id)
        if thread_id == "executor":
            raise RuntimeError("rollout is missing")


class ControllerReliabilityTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        source = root / "source"
        source.mkdir()
        self.worktree = root / "worktree"
        self.worktree.mkdir()
        self.paths = RuntimePaths(
            brain_root=root / "brain",
            state_root=root / "state",
            config_file=root / "config.json",
            control_root=root / "control",
        )
        self.config = InstallationConfig(
            source_root=str(source),
            brain_root=str(self.paths.brain_root),
            state_root=str(self.paths.state_root),
            codex_bin="/bin/codex",
            desktop_executable="/Applications/ChatGPT.app/ChatGPT",
            projects=[
                ProjectConfig(
                    "p",
                    str(source),
                    codex_project_id="codex-p",
                    tollgate_repo_id="tg-p",
                    validation_command=["true"],
                )
            ],
        )
        self.controller = Controller(self.paths, self.config)
        self.controller._initialize_configuration()
        now = "2026-01-01T00:00:00Z"
        self.controller.store.execute(
            "INSERT INTO beads VALUES ('p-1','key','p','Title','Scope','pending','sol','high','sol','high','default',NULL,NULL,'[]','complete',?,?)",
            (now, now),
        )
        run_id = apply_archon_decisions(
            self.controller.store,
            {
                "global_limit": 2,
                "project_limits": {"p": 2},
                "decisions": [
                    {"decision": "approve", "project": "p", "beads": ["p-1"]}
                ],
            },
        )["created_runs"][0]
        self.assignment = self.controller.store.row(
            "SELECT a.*, r.project_id FROM assignments a JOIN runs r ON r.id = a.run_id WHERE a.run_id = ?",
            (run_id,),
        )
        self.executor = self.controller.store.register_task(
            native_thread_id="executor",
            role="executor",
            description="Title",
            model="sol",
            reasoning_effort="high",
            project_id="p",
        )
        self.overseer = self.controller.store.register_task(
            native_thread_id="overseer",
            role="overseer",
            description="Title",
            model="sol",
            reasoning_effort="high",
            project_id="p",
        )
        self.controller.store.execute(
            """UPDATE assignments SET stage = 'implementing', worktree_path = ?,
               executor_task_id = ?, overseer_task_id = ? WHERE id = ?""",
            (
                str(self.worktree),
                self.executor["id"],
                self.overseer["id"],
                self.assignment["id"],
            ),
        )

    def tearDown(self) -> None:
        self.controller.store.close()
        if self.controller.lock_handle is not None:
            self.controller.lock_handle.close()
        self.temporary.cleanup()

    async def test_controller_creates_and_captures_candidate_after_executor(
        self,
    ) -> None:
        self.controller.store.execute(
            """INSERT INTO actions(task_id, assignment_id, kind, payload, state,
               outcome_kind, outcome_payload, created_at, updated_at)
               VALUES (?, ?, 'implement', ?, 'active', 'ready_for_review', '{}', ?, ?)""",
            (
                self.executor["id"],
                self.assignment["id"],
                json.dumps({"predecessor_candidate_id": None}),
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
            ),
        )
        tollgate = FakeCandidateTollgate(str(self.worktree))
        self.controller.tollgate = tollgate  # type: ignore[assignment]
        captured = await self.controller._capture_submitted_candidate(
            int(self.assignment["id"])
        )
        self.assertTrue(captured)
        self.assertEqual(tollgate.submissions, [self.worktree])
        updated = self.controller.store.row(
            "SELECT * FROM assignments WHERE id = ?", (self.assignment["id"],)
        )
        self.assertEqual(updated["candidate_id"], "candidate-1")
        self.assertEqual(updated["tested_oid"], "tested-1")
        operation = self.controller.store.row(
            "SELECT * FROM external_operations WHERE kind = 'tollgate_candidate_create'"
        )
        self.assertEqual(operation["state"], "complete")

    async def test_ambiguous_approval_is_reconciled_to_completed_delivery(self) -> None:
        self.controller.store.execute(
            """UPDATE assignments SET stage = 'delivering', candidate_id = 'candidate-1',
               mandate_candidate_id = 'candidate-1', mandate_scope = scope_snapshot
               WHERE id = ?""",
            (self.assignment["id"],),
        )
        assignment = self.controller.store.row(
            """SELECT a.*, r.project_id FROM assignments a JOIN runs r ON r.id = a.run_id
               WHERE a.id = ?""",
            (self.assignment["id"],),
        )
        self.controller.tollgate = FakeApprovalTollgate()  # type: ignore[assignment]
        beads = FakeBeads()
        self.controller.beads = beads  # type: ignore[assignment]
        await self.controller._deliver(assignment)
        completed = self.controller.store.row(
            "SELECT stage FROM assignments WHERE id = ?", (self.assignment["id"],)
        )
        self.assertEqual(completed["stage"], "completed")
        self.assertEqual(beads.closed, ["p-1"])
        operation = self.controller.store.row(
            "SELECT * FROM external_operations WHERE kind = 'tollgate_approve'"
        )
        self.assertEqual(operation["state"], "complete")
        attempt = self.controller.store.row(
            "SELECT * FROM operation_attempts WHERE operation_id = ?",
            (operation["id"],),
        )
        self.assertEqual(attempt["state"], "uncertain")
        self.assertEqual(attempt["stdout"], "promoted but not json")

    async def test_losing_controller_does_not_emit_a_startup_event(self) -> None:
        before = len(self.controller.store.rows("SELECT * FROM events"))
        with self.assertRaises(StoreError):
            Controller(self.paths, self.config)
        after = len(self.controller.store.rows("SELECT * FROM events"))
        self.assertEqual(after, before)

    async def test_reset_checkpoints_missing_worktree_and_quarantines_bad_archive(
        self,
    ) -> None:
        missing = Path(self.temporary.name) / "already-absent"
        self.controller.store.execute(
            "UPDATE assignments SET worktree_path = ? WHERE id = ?",
            (str(missing), self.assignment["id"]),
        )
        runtime = FakeArchiveRuntime()
        self.controller.runtime = runtime  # type: ignore[assignment]
        with patch("fulcrum.controller.reset_brain", return_value={}):
            exceptions = await self.controller._reset_state({"mode": "reset"})
        self.assertEqual(runtime.archived, ["executor", "overseer"])
        self.assertEqual(exceptions[0]["thread_id"], "executor")
        event = self.controller.store.row(
            "SELECT * FROM events WHERE kind = 'reset_archive_exceptions'"
        )
        self.assertIsNotNone(event)


if __name__ == "__main__":
    unittest.main()
