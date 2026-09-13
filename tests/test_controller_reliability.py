from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from fulcrum.config import InstallationConfig, ProjectConfig, RuntimePaths
from fulcrum.controller import Controller, INHERITED_LOCK_FD_ENV
from fulcrum.lifecycle import apply_archon_decisions, observe_action_terminal
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
        self.ready = True

    async def archive(self, thread_id: str) -> None:
        self.archived.append(thread_id)
        if thread_id == "executor":
            raise RuntimeError("rollout is missing")


class FakeResetTollgate:
    def __init__(self) -> None:
        self.active = True
        self.calls: list[str] = []

    def status(self, _repository: str, candidate: str | None = None) -> dict[str, Any]:
        self.calls.append("status")
        return {
            "candidate": {
                "item": {
                    "id": candidate,
                    "state": "queued" if self.active else "canceled",
                }
            }
        }

    def cancel(self, _repository: str, _candidate: str) -> dict[str, Any]:
        self.calls.append("cancel")
        self.active = False
        return {"canceled": True}

    def remove_worktree(self, _repository: str, path: str) -> dict[str, Any]:
        self.calls.append("remove")
        Path(path).rmdir()
        return {"removed": True}


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
            pair_id=run_id,
        )
        self.overseer = self.controller.store.register_task(
            native_thread_id="overseer",
            role="overseer",
            description="Title",
            model="sol",
            reasoning_effort="high",
            project_id="p",
            pair_id=run_id,
        )
        self.controller.store.execute(
            """UPDATE runs SET state = 'active', executor_task_id = ?,
               overseer_task_id = ? WHERE id = ?""",
            (self.executor["id"], self.overseer["id"], run_id),
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
        os.environ.pop(INHERITED_LOCK_FD_ENV, None)
        self.controller.store.close()
        if self.controller.lock_handle is not None:
            self.controller.lock_handle.close()
        self.temporary.cleanup()

    def test_reexec_adopts_inherited_lock_without_permitting_a_contender(self) -> None:
        inherited = os.dup(self.controller.lock_handle.fileno())
        os.set_inheritable(inherited, True)
        os.environ[INHERITED_LOCK_FD_ENV] = str(inherited)

        successor = Controller(self.paths, self.config)
        self.assertEqual(successor.lock_handle.fileno(), inherited)
        successor.store.close()
        successor.lock_handle.close()

        with self.assertRaisesRegex(StoreError, "another Fulcrum controller"):
            Controller(self.paths, self.config)

    async def test_source_refresh_is_consumed_before_lock_handoff(self) -> None:
        self.controller.store.execute(
            "INSERT INTO meta(key, value) VALUES ('source_refresh_pending', '1') "
            "ON CONFLICT(key) DO UPDATE SET value = '1'"
        )
        self.controller.runtime.close = AsyncMock()  # type: ignore[method-assign]
        descriptor = self.controller.lock_handle.fileno()

        with (
            patch("fulcrum.controller.install_control_plane"),
            patch(
                "fulcrum.controller.controller_program_arguments",
                return_value=["/control/python", "serve"],
            ),
            patch(
                "fulcrum.controller.os.execv",
                side_effect=RuntimeError("exec intercepted"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "exec intercepted"):
                await self.controller._maybe_refresh_source()

        with self.controller.store.__class__(
            self.paths.database, readonly=True
        ) as observer:
            pending = observer.row(
                "SELECT value FROM meta WHERE key = 'source_refresh_pending'"
            )
        self.assertEqual(pending["value"], "0")
        self.assertEqual(os.environ[INHERITED_LOCK_FD_ENV], str(descriptor))

    async def test_run_reuses_one_pair_for_later_beads(self) -> None:
        now = "2026-01-01T00:00:00Z"
        self.controller.store.execute(
            """INSERT INTO beads(
                   bead_id, intake_key, project_id, title, description, activation,
                   executor_model, executor_reasoning_effort, overseer_model,
                   overseer_reasoning_effort, model_provenance, publication_state,
                   created_at, updated_at
               ) VALUES ('p-2','key-2','p','Second','Second scope','pending',
                         'sol','high','sol','high','default','complete',?,?)""",
            (now, now),
        )
        position = self.controller.store.execute(
            "INSERT INTO run_beads(run_id, bead_id, position, scope_snapshot) VALUES (?, 'p-2', 1, 'Second scope')",
            (self.assignment["run_id"],),
        )
        later = self.controller.store.execute(
            "INSERT INTO assignments(run_id, bead_id, stage, scope_snapshot, created_at, updated_at) VALUES (?, 'p-2', 'queued', 'Second scope', ?, ?)",
            (self.assignment["run_id"], now, now),
        )
        self.assertIsNotNone(position)
        assignment = self.controller.store.row(
            """SELECT a.*, r.project_id FROM assignments a JOIN runs r ON r.id = a.run_id
               WHERE a.id = ?""",
            (later.lastrowid,),
        )
        await self.controller._ensure_pair(assignment)
        retained = self.controller.store.row(
            "SELECT * FROM assignments WHERE id = ?", (later.lastrowid,)
        )
        self.assertEqual(retained["executor_task_id"], self.executor["id"])
        self.assertEqual(retained["overseer_task_id"], self.overseer["id"])

    async def test_refreshed_instructions_retain_scope_candidate_and_handoffs(
        self,
    ) -> None:
        now = "2026-01-01T00:00:00Z"
        self.controller.store.execute(
            "UPDATE assignments SET candidate_id = 'candidate-1', source_oid = 'abc' WHERE id = ?",
            (self.assignment["id"],),
        )
        implementation = self.controller.store.execute(
            """INSERT INTO actions(task_id, assignment_id, kind, payload, state,
                   outcome_kind, outcome_payload, created_at, updated_at)
               VALUES (?, ?, 'implement', '{}', 'processed', 'ready_for_review', ?, ?, ?)""",
            (
                self.executor["id"],
                self.assignment["id"],
                json.dumps({"evidence": "commit abc; tests passed"}),
                now,
                now,
            ),
        )
        self.controller.store.execute(
            """INSERT INTO handoffs(assignment_id, source_action_id, kind, content_json, created_at)
               VALUES (?, ?, 'implementation_evidence', ?, ?)""",
            (
                self.assignment["id"],
                implementation.lastrowid,
                json.dumps({"evidence": "commit abc; tests passed"}),
                now,
            ),
        )
        action = self.controller.store.execute(
            """INSERT INTO actions(task_id, assignment_id, kind, payload, state,
                   created_at, updated_at) VALUES (?, ?, 'review', ?, 'active', ?, ?)""",
            (
                self.overseer["id"],
                self.assignment["id"],
                json.dumps({"candidate": {"id": "candidate-1"}}),
                now,
                now,
            ),
        )
        result = await self.controller.handle_request(
            {"command": "instructions", "thread_id": "overseer"}
        )
        prompt = result["instructions"]
        self.assertIn("Approved scope:\n\nScope", prompt)
        self.assertIn("implementation_evidence", prompt)
        self.assertIn("commit abc; tests passed", prompt)

    async def test_failed_specialist_publication_remains_runnable(self) -> None:
        now = "2026-01-01T00:00:00Z"
        occurrence = self.controller.store.execute(
            """INSERT INTO occurrences(kind, authority, state, report_json, created_at, updated_at)
               VALUES ('inquisitor','test','publishing',?, ?, ?)""",
            (
                json.dumps(
                    {
                        "summary": "No findings",
                        "coverage": ["source"],
                        "findings": [],
                    }
                ),
                now,
                now,
            ),
        )
        await self.controller._publish_occurrences()
        retained = self.controller.store.row(
            "SELECT * FROM occurrences WHERE id = ?", (occurrence.lastrowid,)
        )
        obligation = self.controller.store.row(
            "SELECT * FROM obligations WHERE kind = 'finding_publication' AND identity = ?",
            (str(occurrence.lastrowid),),
        )
        self.assertEqual(retained["state"], "publishing")
        self.assertEqual(obligation["state"], "failed")
        self.assertIsNotNone(obligation["next_attempt_at"])

    async def test_specialist_report_is_committed_notified_and_archived(self) -> None:
        brain = self.paths.brain_root
        brain.mkdir()
        subprocess.run(["git", "-C", str(brain), "init", "-q"], check=True)
        subprocess.run(
            ["git", "-C", str(brain), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(brain), "config", "user.name", "Test"], check=True
        )
        (brain / "README.md").write_text("brain\n")
        subprocess.run(["git", "-C", str(brain), "add", "README.md"], check=True)
        subprocess.run(
            ["git", "-C", str(brain), "commit", "-q", "-m", "init"], check=True
        )
        archon = self.controller.store.register_task(
            native_thread_id="archon",
            role="archon",
            description="Fleet",
            model="sol",
            reasoning_effort="high",
        )
        specialist = self.controller.store.register_task(
            native_thread_id="inquisitor",
            role="inquisitor",
            description="Review",
            model="sol",
            reasoning_effort="high",
            project_id="p",
        )
        now = "2026-01-01T00:00:00Z"
        occurrence = self.controller.store.execute(
            """INSERT INTO occurrences(
                   kind, authority, state, report_json, evidence_json, created_at, updated_at
               ) VALUES ('inquisitor','test','publishing',?, ?, ?, ?)""",
            (
                json.dumps(
                    {
                        "summary": "No findings",
                        "coverage": ["all source"],
                        "findings": [],
                    }
                ),
                json.dumps(
                    {"projects": [{"project_id": "p", "source_revision": "abc"}]}
                ),
                now,
                now,
            ),
        )
        self.controller.store.execute(
            """INSERT INTO actions(task_id, occurrence_id, kind, payload, state,
                   created_at, updated_at) VALUES (?, ?, 'specialist', '{}', 'processed', ?, ?)""",
            (specialist["id"], occurrence.lastrowid, now, now),
        )
        await self.controller._publish_occurrences()
        retained = self.controller.store.row(
            "SELECT * FROM occurrences WHERE id = ?", (occurrence.lastrowid,)
        )
        self.assertEqual(retained["state"], "complete")
        self.assertTrue(retained["publication_revision"])
        report_path = brain / "reports" / "inquisitor" / f"{occurrence.lastrowid}.json"
        self.assertTrue(report_path.is_file())
        self.assertEqual(
            json.loads(report_path.read_text())["reviewed_evidence"]["projects"][0][
                "source_revision"
            ],
            "abc",
        )
        self.assertIsNotNone(
            self.controller.store.row(
                "SELECT 1 FROM updates WHERE recipient_task_id = ? AND identity = ?",
                (archon["id"], f"specialist:{occurrence.lastrowid}"),
            )
        )
        self.assertIsNotNone(
            self.controller.store.row(
                "SELECT 1 FROM obligations WHERE kind = 'archive' AND target = 'inquisitor'"
            )
        )

    async def test_deferred_archon_batch_reactivates_without_losing_updates(
        self,
    ) -> None:
        now = "2026-01-01T00:00:00Z"
        archon = self.controller.store.register_task(
            native_thread_id="archon",
            role="archon",
            description="Fleet",
            model="sol",
            reasoning_effort="high",
        )
        update = self.controller.store.execute(
            """INSERT INTO updates(recipient_task_id, identity, content, state, created_at, updated_at)
               VALUES (?, 'proposal:test', '{}', 'batched', ?, ?)""",
            (archon["id"], now, now),
        )
        action = self.controller.store.execute(
            """INSERT INTO actions(task_id, kind, payload, state, outcome_kind,
                   outcome_payload, created_at, updated_at)
               VALUES (?, 'archon', '{}', 'active', 'deferred', ?, ?, ?)""",
            (
                archon["id"],
                json.dumps(
                    {
                        "reason": "wait",
                        "reactivation": {"next_check_at": "2020-01-01T00:00:00Z"},
                    }
                ),
                now,
                now,
            ),
        )
        batch = self.controller.store.execute(
            """INSERT INTO batches(recipient_task_id, state, action_id, created_at, updated_at)
               VALUES (?, 'frozen', ?, ?, ?)""",
            (archon["id"], action.lastrowid, now, now),
        )
        self.controller.store.execute(
            "INSERT INTO batch_updates(batch_id, update_id) VALUES (?, ?)",
            (batch.lastrowid, update.lastrowid),
        )
        observe_action_terminal(self.controller.store, int(action.lastrowid))
        self.assertIsNotNone(
            self.controller.store.row(
                "SELECT * FROM deferred_batches WHERE batch_id = ?",
                (batch.lastrowid,),
            )
        )
        self.controller._reactivate_deferred_batches()
        self.assertEqual(
            self.controller.store.row(
                "SELECT state FROM updates WHERE id = ?", (update.lastrowid,)
            )["state"],
            "retained",
        )
        self.assertIsNone(
            self.controller.store.row(
                "SELECT * FROM deferred_batches WHERE batch_id = ?",
                (batch.lastrowid,),
            )
        )
        repeated = self.controller.store.execute(
            """INSERT INTO batches(recipient_task_id, state, created_at, updated_at)
               VALUES (?, 'frozen', ?, ?)""",
            (archon["id"], now, now),
        )
        self.controller.store.execute(
            "INSERT INTO batch_updates(batch_id, update_id) VALUES (?, ?)",
            (repeated.lastrowid, update.lastrowid),
        )

    async def test_operator_change_reactivates_a_deferred_archon_batch(self) -> None:
        now = "2026-01-01T00:00:00Z"
        archon = self.controller.store.register_task(
            native_thread_id="archon",
            role="archon",
            description="Fleet",
            model="sol",
            reasoning_effort="high",
        )
        old_update = self.controller.store.execute(
            """INSERT INTO updates(recipient_task_id, identity, content, state, created_at, updated_at)
               VALUES (?, 'proposal:old', '{}', 'batched', ?, ?)""",
            (archon["id"], now, now),
        )
        action = self.controller.store.execute(
            """INSERT INTO actions(task_id, kind, payload, state, outcome_kind,
                   outcome_payload, created_at, updated_at)
               VALUES (?, 'archon', '{}', 'active', 'deferred', ?, ?, ?)""",
            (
                archon["id"],
                json.dumps(
                    {
                        "reason": "wait for operator input",
                        "reactivation": {"operator_change": True},
                    }
                ),
                now,
                now,
            ),
        )
        batch = self.controller.store.execute(
            """INSERT INTO batches(recipient_task_id, state, action_id, created_at, updated_at)
               VALUES (?, 'frozen', ?, ?, ?)""",
            (archon["id"], action.lastrowid, now, now),
        )
        self.controller.store.execute(
            "INSERT INTO batch_updates(batch_id, update_id) VALUES (?, ?)",
            (batch.lastrowid, old_update.lastrowid),
        )
        observe_action_terminal(self.controller.store, int(action.lastrowid))

        self.controller._reactivate_deferred_batches()
        self.assertEqual(
            self.controller.store.row(
                "SELECT state FROM updates WHERE id = ?", (old_update.lastrowid,)
            )["state"],
            "batched",
        )
        self.controller.store.execute(
            """INSERT INTO updates(recipient_task_id, identity, content, state, created_at, updated_at)
               VALUES (?, 'operator:new', '{}', 'retained', ?, ?)""",
            (archon["id"], now, now),
        )
        self.controller._reactivate_deferred_batches()
        self.assertEqual(
            self.controller.store.row(
                "SELECT state FROM updates WHERE id = ?", (old_update.lastrowid,)
            )["state"],
            "retained",
        )

    async def test_archon_succession_progresses_while_dispatch_is_disabled(
        self,
    ) -> None:
        old = self.controller.store.register_task(
            native_thread_id="old-archon",
            role="archon",
            description="Fleet",
            model="sol",
            reasoning_effort="high",
        )
        self.controller.store.execute(
            "INSERT INTO meta(key, value) VALUES ('archon_succession_request', ?)",
            (
                json.dumps(
                    {
                        "task_id": old["id"],
                        "successor_model": "sol",
                        "successor_reasoning_effort": "high",
                        "reason": "handover",
                    }
                ),
            ),
        )
        runtime = FakeArchiveRuntime()
        self.controller.runtime = runtime  # type: ignore[assignment]

        async def provision(**_arguments: Any) -> dict[str, Any]:
            return self.controller.store.register_task(
                native_thread_id="new-archon",
                role="archon",
                description="Fleet",
                model="sol",
                reasoning_effort="high",
            )

        self.controller.starts_enabled = False
        with patch.object(self.controller, "_provision_task", side_effect=provision):
            await self.controller.advance()
        self.assertEqual(runtime.archived, ["old-archon"])
        self.assertEqual(
            self.controller.store.row(
                "SELECT state FROM tasks WHERE id = ?", (old["id"],)
            )["state"],
            "archived",
        )
        self.assertIsNotNone(
            self.controller.store.row(
                "SELECT * FROM tasks WHERE native_thread_id = 'new-archon'"
            )
        )
        self.assertIsNone(
            self.controller.store.row(
                "SELECT * FROM meta WHERE key = 'archon_succession_request'"
            )
        )

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

    async def test_restart_adopts_sent_effects_and_replays_only_confirmed_unsent(
        self,
    ) -> None:
        now = "2026-01-01T00:00:00Z"
        action = self.controller.store.execute(
            """INSERT INTO actions(task_id, assignment_id, kind, payload, state,
               created_at, updated_at) VALUES (?, ?, 'implement', '{}', 'starting', ?, ?)""",
            (self.executor["id"], self.assignment["id"], now, now),
        )
        unsent = self.controller.store.create_operation(
            "turn_start", str(action.lastrowid), {"thread_id": "executor"}
        )
        sent = self.controller.store.create_operation(
            "unknown_mutation", "native", {"value": 1}
        )
        self.controller.store.begin_operation_attempt(sent)
        self.controller._adopt_stranded_operations()
        self.assertEqual(
            self.controller.store.row(
                "SELECT state FROM external_operations WHERE id = ?", (unsent,)
            )["state"],
            "canceled",
        )
        self.assertEqual(
            self.controller.store.row(
                "SELECT state FROM actions WHERE id = ?", (action.lastrowid,)
            )["state"],
            "pending",
        )
        self.assertEqual(
            self.controller.store.row(
                "SELECT state FROM external_operations WHERE id = ?", (sent,)
            )["state"],
            "uncertain",
        )

    async def test_reset_cancels_candidate_before_confirming_worktree_absent(
        self,
    ) -> None:
        self.controller.store.execute(
            "UPDATE assignments SET candidate_id = 'candidate-1' WHERE id = ?",
            (self.assignment["id"],),
        )
        tollgate = FakeResetTollgate()
        runtime = FakeArchiveRuntime()
        self.controller.tollgate = tollgate  # type: ignore[assignment]
        self.controller.runtime = runtime  # type: ignore[assignment]
        with patch("fulcrum.controller.reset_brain", return_value={}):
            await self.controller._reset_state({"mode": "reset"})
        self.assertEqual(tollgate.calls, ["status", "cancel", "status", "remove"])
        self.assertFalse(self.worktree.exists())

    async def test_corrupt_thread_does_not_block_later_task_reconciliation(
        self,
    ) -> None:
        now = "2026-01-01T00:00:00Z"
        first = self.controller.store.execute(
            """INSERT INTO actions(task_id, kind, payload, state, native_turn_id,
               created_at, updated_at) VALUES (?, 'implement', '{}', 'active', 'turn-1', ?, ?)""",
            (self.executor["id"], now, now),
        )
        second = self.controller.store.execute(
            """INSERT INTO actions(task_id, kind, payload, state, native_turn_id,
               created_at, updated_at) VALUES (?, 'review', '{}', 'active', 'turn-2', ?, ?)""",
            (self.overseer["id"], now, now),
        )

        async def refresh(task: dict[str, Any]) -> dict[str, Any]:
            if task["id"] == self.executor["id"]:
                raise RuntimeError("missing rollout")
            return {
                "last_turn_terminal": True,
                "helpers_terminal": True,
                "last_turn_id": "turn-2",
                "last_turn_status": "completed",
            }

        self.controller.runtime.ready = True
        with patch.object(self.controller, "_refresh_task", side_effect=refresh):
            await self.controller.reconcile()
        self.assertEqual(
            self.controller.store.row(
                "SELECT state FROM tasks WHERE id = ?", (self.executor["id"],)
            )["state"],
            "uncertain",
        )
        self.assertEqual(
            self.controller.store.row(
                "SELECT state FROM actions WHERE id = ?", (second.lastrowid,)
            )["state"],
            "terminal",
        )
        self.assertIsNotNone(
            self.controller.store.row(
                "SELECT * FROM events WHERE kind = 'reconcile_failed' AND entity_id = ?",
                (str(self.executor["id"]),),
            )
        )
        self.assertEqual(
            self.controller.store.row(
                "SELECT state FROM actions WHERE id = ?", (first.lastrowid,)
            )["state"],
            "active",
        )


if __name__ == "__main__":
    unittest.main()
