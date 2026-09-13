from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from fulcrum.config import InstallationConfig, ProjectConfig, RuntimePaths
from fulcrum.controller import (
    Controller,
    INHERITED_LOCK_FD_ENV,
    _candidate_definitively_failed,
    _find_candidate,
)
from fulcrum.lifecycle import apply_archon_decisions, observe_action_terminal
from fulcrum.store import StoreError
from fulcrum.tollgate import TollgateError, TollgateUncertainError


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

    def test_definitive_tollgate_failure_is_not_marked_uncertain(self) -> None:
        operation = self.controller.store.create_operation(
            "tollgate_candidate_create", "definitive", {}
        )
        attempt = self.controller.store.begin_operation_attempt(operation)
        self.controller._operation_failed(
            operation,
            TollgateError("rejected", returncode=1),
            attempt=attempt,
            mutation=True,
        )
        retained = self.controller.store.row(
            "SELECT * FROM external_operations WHERE id = ?", (operation,)
        )
        self.assertEqual(retained["state"], "failed")

    def test_codex_intake_requires_an_active_registered_weaver(self) -> None:
        self.controller._require_registered_weaver(None)
        with self.assertRaisesRegex(StoreError, "weaver register"):
            self.controller._require_registered_weaver("unregistered")

        weaver = self.controller.store.register_task(
            native_thread_id="registered-weaver",
            role="weaver",
            description="Intake",
            model="sol",
            reasoning_effort="high",
            project_id="p",
        )
        self.controller.store.execute(
            """INSERT INTO actions(task_id, kind, payload, state, created_at, updated_at)
               VALUES (?, 'weaver', '{}', 'active', 'now', 'now')""",
            (weaver["id"],),
        )
        self.controller._require_registered_weaver("registered-weaver")

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

    async def test_setup_materializes_a_new_archon_when_policies_are_retained(
        self,
    ) -> None:
        self.controller.config = replace(
            self.controller.config,
            archon_model="sol",
            archon_reasoning_effort="high",
        )
        archon = self.controller.store.register_task(
            native_thread_id="new-archon",
            role="archon",
            description="",
            model="sol",
            reasoning_effort="high",
            project_id="p",
        )
        self.controller.store.execute(
            "UPDATE tasks SET runtime_status = 'unmaterialized' WHERE id = ?",
            (archon["id"],),
        )
        self.controller.store.execute(
            "INSERT INTO policies(kind, scope, cadence_seconds, anchor_at, next_due_at, active) VALUES ('sage', NULL, 86400, 'now', 'later', 1)"
        )
        runtime = AsyncMock()
        runtime.ready = True
        runtime.list_models.return_value = [
            {
                "model": "sol",
                "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
            }
        ]
        runtime.read_thread.return_value = {
            "id": "new-archon",
            "status": {"type": "idle"},
            "turns": [],
        }
        self.controller.runtime = runtime

        with (
            patch.object(self.controller, "_verify_projects", new=AsyncMock()),
            patch.object(self.controller, "_setup_smoke_check", new=AsyncMock()),
            patch.object(
                self.controller, "_dispatch_action", new=AsyncMock()
            ) as dispatch,
        ):
            result = await self.controller._setup_initialize()

        self.assertFalse(result["ready"])
        self.assertEqual(result["condition"], "Archon materialization pending")
        action = self.controller.store.row(
            "SELECT * FROM actions WHERE task_id = ?", (archon["id"],)
        )
        self.assertIsNotNone(action)
        self.assertEqual(json.loads(action["payload"])["purpose"], "materialize_archon")
        dispatch.assert_awaited_once()

    async def test_weaver_registration_binds_the_current_native_turn(self) -> None:
        runtime = AsyncMock()
        runtime.read_thread.return_value = {
            "id": "weaver-thread",
            "name": "temporary",
            "status": {"type": "active"},
            "turns": [{"id": "weaver-turn", "status": "inProgress", "items": []}],
        }
        self.controller.runtime = runtime

        result = await self.controller._register_weaver(
            {
                "thread_id": "weaver-thread",
                "project": "p",
                "description": "File a task",
                "writable": True,
            }
        )

        action = self.controller.store.row(
            """SELECT * FROM actions WHERE task_id =
                   (SELECT id FROM tasks WHERE native_thread_id = ?)""",
            ("weaver-thread",),
        )
        self.assertEqual(action["native_turn_id"], "weaver-turn")
        self.assertEqual(result["thread_id"], "weaver-thread")

    async def test_dispatch_resumes_a_not_loaded_thread_before_starting(self) -> None:
        self.controller.store.execute(
            "UPDATE tasks SET runtime_status = 'notLoaded' WHERE id = ?",
            (self.executor["id"],),
        )
        cursor = self.controller.store.execute(
            """INSERT INTO actions(task_id, assignment_id, kind, payload, state, created_at, updated_at)
               VALUES (?, ?, 'implement', '{}', 'pending', 'now', 'now')""",
            (self.executor["id"], self.assignment["id"]),
        )
        action = self.controller.store.row(
            "SELECT * FROM actions WHERE id = ?", (cursor.lastrowid,)
        )
        runtime = AsyncMock()
        runtime.read_thread.side_effect = [
            {
                "id": "executor",
                "status": {"type": "notLoaded"},
                "turns": [],
            },
            {
                "id": "executor",
                "status": {"type": "idle"},
                "turns": [],
            },
        ]
        runtime.start_turn.return_value = "turn-1"
        self.controller.runtime = runtime

        await self.controller._dispatch_action(action)

        runtime.resume_thread.assert_awaited_once_with("executor")
        runtime.start_turn.assert_awaited_once()

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

    async def test_overseer_is_provisioned_only_when_review_starts(self) -> None:
        now = "2026-01-01T00:00:00Z"
        self.controller.store.execute(
            """INSERT INTO beads VALUES (
                   'p-lazy','lazy','p','Lazy review','Scope','pending',
                   'sol','high','sol','high','default',NULL,NULL,'[]','complete',?,?
               )""",
            (now, now),
        )
        run = self.controller.store.execute(
            """INSERT INTO runs(project_id, authority, state, created_at, updated_at)
               VALUES ('p','test','approved',?,?)""",
            (now, now),
        )
        assignment_cursor = self.controller.store.execute(
            """INSERT INTO assignments(
                   run_id, bead_id, stage, scope_snapshot, worktree_path,
                   created_at, updated_at
               ) VALUES (?, 'p-lazy', 'preparing', 'Scope', ?, ?, ?)""",
            (run.lastrowid, str(self.worktree), now, now),
        )
        assignment = self.controller.store.row(
            """SELECT a.*, r.project_id FROM assignments a
               JOIN runs r ON r.id = a.run_id WHERE a.id = ?""",
            (assignment_cursor.lastrowid,),
        )
        executor = self.controller.store.register_task(
            native_thread_id="lazy-executor",
            role="executor",
            description="Lazy review",
            model="sol",
            reasoning_effort="high",
            project_id="p",
            pair_id=int(run.lastrowid),
        )
        overseer = self.controller.store.register_task(
            native_thread_id="lazy-overseer",
            role="overseer",
            description="Lazy review",
            model="sol",
            reasoning_effort="high",
            project_id="p",
            pair_id=int(run.lastrowid),
        )
        provision = AsyncMock(side_effect=[executor, overseer])
        with patch.object(self.controller, "_provision_task", new=provision):
            await self.controller._ensure_pair(assignment)
            retained = self.controller.store.row(
                "SELECT * FROM assignments WHERE id = ?", (assignment["id"],)
            )
            self.assertEqual(retained["executor_task_id"], executor["id"])
            self.assertIsNone(retained["overseer_task_id"])
            provision.assert_awaited_once()
            self.assertEqual(provision.await_args.kwargs["cwd"], str(self.worktree))

            assignment.update(retained)
            await self.controller._ensure_pair(assignment, include_overseer=True)
        retained = self.controller.store.row(
            "SELECT * FROM assignments WHERE id = ?", (assignment["id"],)
        )
        self.assertEqual(retained["overseer_task_id"], overseer["id"])
        self.assertEqual(provision.await_count, 2)
        self.assertEqual(provision.await_args.kwargs["role"], "overseer")
        self.assertEqual(provision.await_args.kwargs["cwd"], str(self.worktree))

    async def test_dispatch_sends_scope_candidate_and_evidence_inline(
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
        retained = self.controller.store.row(
            "SELECT * FROM actions WHERE id = ?", (action.lastrowid,)
        )
        with (
            patch.object(
                self.controller,
                "_refresh_task",
                new=AsyncMock(
                    return_value={
                        "can_start": True,
                        "last_turn_id": None,
                        "runtime_status": "idle",
                    }
                ),
            ),
            patch.object(
                self.controller.runtime,
                "start_turn",
                new=AsyncMock(return_value="review-turn"),
            ) as start,
        ):
            await self.controller._dispatch_action(retained)
        prompt = start.call_args.args[1]
        self.assertIn("Approved scope:\nScope", prompt)
        self.assertIn("candidate-1", prompt)
        self.assertIn("commit abc; tests passed", prompt)
        self.assertNotIn("fulcrum instructions", prompt)
        self.assertNotIn("You are Overseer", prompt)
        self.assertNotIn("--section", prompt)

    async def test_archon_batch_dispatch_contains_actual_proposal(self) -> None:
        self.controller.store.register_task(
            native_thread_id="archon-inline",
            role="archon",
            description="Fleet",
            model="sol",
            reasoning_effort="high",
            project_id="p",
            state="idle",
        )
        self.controller._queue_archon_update(
            "proposal:p-2",
            {
                "kind": "proposal",
                "bead_id": "p-2",
                "project": "p",
                "title": "Fix empty results",
                "scope": "Show an empty state for zero matches; verify matching results remain correct.",
            },
        )
        with (
            patch.object(
                self.controller,
                "_refresh_task",
                new=AsyncMock(
                    return_value={
                        "can_start": True,
                        "last_turn_id": None,
                        "runtime_status": "idle",
                    }
                ),
            ),
            patch.object(
                self.controller.runtime,
                "start_turn",
                new=AsyncMock(return_value="archon-turn"),
            ) as start,
        ):
            await self.controller._deliver_update_batch()
        text = start.call_args.args[1]
        self.assertIn("Approve or defer p-2 (p): Fix empty results", text)
        self.assertIn(
            "Show an empty state for zero matches; verify matching results remain correct.",
            text,
        )
        self.assertIn("Capacity used:", text)
        self.assertIn("Existing p-1", text)
        self.assertNotIn("fulcrum instructions", text)
        self.assertNotIn("You are Archon", text)
        self.assertNotIn("Exact JSON", text)
        self.assertLess(len(text.split()), 100)

    async def test_missing_finish_notice_preserves_original_batch(self) -> None:
        archon = self.controller.store.register_task(
            native_thread_id="archon-notice",
            role="archon",
            description="Fleet",
            model="sol",
            reasoning_effort="high",
            project_id="p",
        )
        original = {
            "batch_items": [
                {"update_id": 17, "content": {"scope": "Exact approved proposal"}}
            ]
        }
        action = self.controller.store.execute(
            "INSERT INTO actions(task_id, kind, payload, state, native_turn_id, created_at, updated_at) VALUES (?, 'archon', ?, 'active', 'turn-1', 'now', 'now')",
            (archon["id"], json.dumps(original)),
        )
        self.controller.store.execute(
            "UPDATE tasks SET last_turn_terminal = 1, helpers_terminal = 1 WHERE id = ?",
            (archon["id"],),
        )
        with (
            patch.object(
                self.controller,
                "_refresh_task",
                new=AsyncMock(
                    return_value={
                        "last_turn_id": "turn-1",
                        "last_turn_status": "completed",
                        "last_turn_terminal": True,
                        "helpers_terminal": True,
                    }
                ),
            ),
            patch.object(
                self.controller, "_dispatch_action", new=AsyncMock()
            ) as dispatch,
        ):
            await self.controller._handle_runtime_event(
                "turn/completed",
                {
                    "threadId": "archon-notice",
                    "turn": {"id": "turn-1", "status": "completed"},
                },
            )
        retained = self.controller.store.row(
            "SELECT * FROM actions WHERE id = ?", (action.lastrowid,)
        )
        self.assertEqual(json.loads(retained["payload"]), original)
        self.assertEqual(retained["reminder_sent"], 1)
        self.assertEqual(dispatch.call_args.args[0]["reminder_sent"], 1)
        text = self.controller._build_action_message(retained, archon, None)
        self.assertIn("outstanding result", text)
        self.assertNotIn("fulcrum instructions", text)
        self.assertNotIn("Exact approved proposal", text)

    async def test_unconfirmed_interrupted_event_does_not_fail_new_turn(self) -> None:
        action = self.controller.store.execute(
            """INSERT INTO actions(
                   task_id, assignment_id, kind, payload, state, native_turn_id,
                   created_at, updated_at
               ) VALUES (?, ?, 'implement', '{}', 'active', 'new-turn', 'now', 'now')""",
            (self.executor["id"], self.assignment["id"]),
        )
        with patch.object(
            self.controller,
            "_refresh_task",
            new=AsyncMock(
                return_value={
                    "last_turn_id": None,
                    "last_turn_terminal": True,
                    "helpers_terminal": True,
                }
            ),
        ):
            await self.controller._handle_runtime_event(
                "turn/completed",
                {
                    "threadId": "executor",
                    "turn": {"id": "new-turn", "status": "interrupted"},
                },
            )

        retained = self.controller.store.row(
            "SELECT state, condition FROM actions WHERE id = ?", (action.lastrowid,)
        )
        self.assertEqual(retained, {"state": "active", "condition": None})

    async def test_completed_thread_overrides_stale_interrupted_event(self) -> None:
        archon = self.controller.store.register_task(
            native_thread_id="archon-stale-status",
            role="archon",
            description="Fleet",
            model="sol",
            reasoning_effort="high",
            project_id="p",
        )
        action = self.controller.store.execute(
            """INSERT INTO actions(
                   task_id, kind, payload, state, native_turn_id, outcome_kind,
                   outcome_payload, created_at, updated_at
               ) VALUES (?, 'archon', '{}', 'active', 'turn-1', 'decisions',
                         ?, 'now', 'now')""",
            (archon["id"], '{"decisions":[],"handled_update_ids":[]}'),
        )
        self.controller.store.execute(
            "UPDATE tasks SET last_turn_terminal = 1, helpers_terminal = 1 WHERE id = ?",
            (archon["id"],),
        )
        with patch.object(
            self.controller,
            "_refresh_task",
            new=AsyncMock(
                return_value={
                    "last_turn_id": "turn-1",
                    "last_turn_status": "completed",
                    "last_turn_terminal": True,
                    "helpers_terminal": True,
                }
            ),
        ):
            await self.controller._handle_runtime_event(
                "turn/completed",
                {
                    "threadId": "archon-stale-status",
                    "turn": {"id": "turn-1", "status": "interrupted"},
                },
            )

        retained = self.controller.store.row(
            "SELECT state, condition FROM actions WHERE id = ?", (action.lastrowid,)
        )
        self.assertEqual(retained, {"state": "processed", "condition": None})

    async def test_archived_task_ignores_late_runtime_events(self) -> None:
        self.controller.store.execute(
            "UPDATE tasks SET state = 'archived', archived = 1 WHERE id = ?",
            (self.executor["id"],),
        )

        await self.controller._handle_runtime_event(
            "turn/started",
            {
                "threadId": "executor",
                "turn": {"id": "late-turn", "status": "inProgress"},
            },
        )

        retained = self.controller.store.row(
            "SELECT state, archived FROM tasks WHERE id = ?", (self.executor["id"],)
        )
        self.assertEqual(retained, {"state": "archived", "archived": 1})

        runtime = AsyncMock()
        runtime.read_thread.return_value = {
            "id": "executor",
            "name": self.executor["title"],
            "status": {"type": "notLoaded"},
            "archived": False,
            "turns": [],
        }
        self.controller.runtime = runtime
        await self.controller._refresh_task(
            self.controller.store.row(
                "SELECT * FROM tasks WHERE id = ?", (self.executor["id"],)
            )
        )
        retained = self.controller.store.row(
            "SELECT state, archived FROM tasks WHERE id = ?", (self.executor["id"],)
        )
        self.assertEqual(retained, {"state": "archived", "archived": 1})

    async def test_late_unowned_start_event_does_not_reactivate_idle_task(self) -> None:
        relevant = await self.controller._handle_runtime_event(
            "turn/started",
            {
                "threadId": "executor",
                "turn": {"id": "late-turn", "status": "inProgress"},
            },
        )

        retained = self.controller.store.row(
            "SELECT state, runtime_status FROM tasks WHERE id = ?",
            (self.executor["id"],),
        )
        self.assertEqual(retained["state"], "idle")
        self.assertFalse(relevant)

    async def test_high_volume_unrelated_runtime_event_does_not_request_advance(
        self,
    ) -> None:
        relevant = await self.controller._handle_runtime_event(
            "item/agentMessage/delta",
            {"threadId": "executor", "delta": "token"},
        )

        self.assertFalse(relevant)

    async def test_unchanged_runtime_status_does_not_request_advance(self) -> None:
        self.controller.store.execute(
            "UPDATE tasks SET runtime_status = 'active' WHERE id = ?",
            (self.executor["id"],),
        )

        relevant = await self.controller._handle_runtime_event(
            "thread/status/changed",
            {"threadId": "executor", "status": {"type": "active"}},
        )

        self.assertFalse(relevant)

    async def test_refresh_repairs_idle_runtime_without_current_action(self) -> None:
        self.controller.store.execute(
            "UPDATE tasks SET state = 'active' WHERE id = ?", (self.executor["id"],)
        )
        runtime = AsyncMock()
        runtime.read_thread.return_value = {
            "id": "executor",
            "name": self.executor["title"],
            "status": {"type": "idle"},
            "turns": [{"id": "done", "status": "completed", "items": []}],
        }
        self.controller.runtime = runtime

        await self.controller._refresh_task(
            self.controller.store.row(
                "SELECT * FROM tasks WHERE id = ?", (self.executor["id"],)
            )
        )

        retained = self.controller.store.row(
            "SELECT state FROM tasks WHERE id = ?", (self.executor["id"],)
        )
        self.assertEqual(retained["state"], "idle")

    async def test_reconciliation_repairs_terminal_task_without_current_action(
        self,
    ) -> None:
        self.controller.store.execute(
            """UPDATE tasks SET state = 'active', runtime_status = 'notLoaded',
               last_turn_terminal = 1, helpers_terminal = 1 WHERE id = ?""",
            (self.executor["id"],),
        )

        self.controller._normalize_unowned_tasks()

        retained = self.controller.store.row(
            "SELECT state FROM tasks WHERE id = ?", (self.executor["id"],)
        )
        self.assertEqual(retained["state"], "idle")

    async def test_repair_context_exposes_permission_and_retained_diagnosis(
        self,
    ) -> None:
        self.controller.store.execute(
            "UPDATE assignments SET candidate_id = 'c-1', mandate_candidate_id = 'c-1', mandate_scope = 'Scope', repair_permissions = ? WHERE id = ?",
            ('["bounded_in_scope_ci_fix"]', self.assignment["id"]),
        )
        operation = self.controller.store.create_operation(
            "tollgate_approve", "c-1", {}
        )
        self.controller.store.execute(
            "UPDATE external_operations SET result_json = ?, condition = 'CI failed' WHERE id = ?",
            (json.dumps({"diagnosis": "actual failed check log"}), operation),
        )
        self.controller.store.execute(
            "INSERT INTO actions(task_id, assignment_id, kind, payload, state, created_at, updated_at) VALUES (?, ?, 'correct', '{}', 'active', 'now', 'now')",
            (self.executor["id"], self.assignment["id"]),
        )
        action = self.controller.store.current_action("executor")
        assignment = self.controller.store.row(
            "SELECT * FROM assignments WHERE id = ?", (self.assignment["id"],)
        )
        text = self.controller._build_action_message(action, self.executor, assignment)
        self.assertIn("bounded_in_scope_ci_fix", text)
        self.assertIn("CI failed", text)
        self.assertIn("actual failed check log", text)
        self.assertNotIn("fulcrum instructions", text)

    def test_specialist_evidence_reports_interval_scope_and_truncation(self) -> None:
        scope = json.dumps({"global": False, "projects": ["p"]})
        self.controller.store.execute(
            "INSERT INTO occurrences(kind, scope, authority, state, evidence_json, report_json, created_at, updated_at) VALUES ('sage', ?, 'test', 'complete', ?, '{}', '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z')",
            (scope, json.dumps({"captured_at": "2026-01-01T00:00:00Z"})),
        )
        current = self.controller.store.execute(
            "INSERT INTO occurrences(kind, scope, authority, state, created_at, updated_at) VALUES ('sage', ?, 'test', 'queued', '2026-02-01T00:00:00Z', '2026-02-01T00:00:00Z')",
            (scope,),
        )
        self.controller.store.event("old", "old event", now="2025-12-01T00:00:00Z")
        for n in range(205):
            self.controller.store.event(
                "sample",
                f"scoped {n}",
                entity_type="project",
                entity_id="p",
                now="2026-02-01T00:00:00Z",
            )
        self.controller.store.event(
            "other",
            "outside project",
            entity_type="project",
            entity_id="other",
            now="2026-02-01T00:00:00Z",
        )
        occurrence = self.controller.store.row(
            "SELECT * FROM occurrences WHERE id = ?", (current.lastrowid,)
        )
        with patch("fulcrum.controller.utc_now", return_value="2026-03-01T00:00:00Z"):
            evidence = self.controller._specialist_evidence(
                json.loads(scope), occurrence=occurrence
            )
        self.assertEqual(evidence["window"]["after"], "2026-01-01T00:00:00Z")
        self.assertTrue(evidence["coverage"]["events"]["truncated"])
        self.assertEqual(len(evidence["recent_events"]), 200)
        self.assertNotIn("old event", json.dumps(evidence))
        self.assertNotIn("outside project", json.dumps(evidence))

    async def test_global_sage_retains_project_scope_without_reserving_project_slots(
        self,
    ) -> None:
        scope = {"global": True, "projects": ["p"]}
        self.controller.store.execute(
            "INSERT INTO occurrences(kind, scope, authority, state, created_at, updated_at) VALUES ('sage', ?, 'test', 'queued', 'now', 'now')",
            (json.dumps(scope),),
        )
        sage = self.controller.store.register_task(
            native_thread_id="sage",
            role="sage",
            description="Report",
            model="sol",
            reasoning_effort="high",
            project_id="p",
        )
        with (
            patch.object(
                self.controller, "_provision_task", new=AsyncMock(return_value=sage)
            ),
            patch.object(self.controller, "_dispatch_action", new=AsyncMock()),
        ):
            await self.controller._start_specialists()
        action = self.controller.store.row(
            "SELECT * FROM actions WHERE task_id = ?", (sage["id"],)
        )
        self.assertEqual(json.loads(action["payload"])["scope"], scope)
        reservation = self.controller.store.row(
            "SELECT project_ids FROM reservations WHERE action_id = ?", (action["id"],)
        )
        self.assertEqual(json.loads(reservation["project_ids"]), [])

    async def test_specialist_findings_default_pending_and_preserve_explicit_deferral(
        self,
    ) -> None:
        report = {
            "summary": "Findings",
            "coverage": ["source"],
            "findings": [
                {
                    "identity": "first",
                    "project": "p",
                    "problem": "Problem",
                    "evidence": "source",
                    "expected_benefit": "fix",
                    "acceptance_criteria": "verified",
                },
                {
                    "identity": "second",
                    "project": "p",
                    "problem": "Later problem",
                    "evidence": "source",
                    "expected_benefit": "fix",
                    "acceptance_criteria": "verified",
                    "activation": "future",
                    "deferral_reason": "human deferred",
                },
            ],
        }
        self.controller.store.execute(
            "INSERT INTO occurrences(kind, authority, state, report_json, created_at, updated_at) VALUES ('sage', 'test', 'publishing', ?, 'now', 'now')",
            (json.dumps(report),),
        )
        with (
            patch.object(
                self.controller,
                "_publish_specialist_report",
                return_value={"local_revision": "revision"},
            ),
            patch("fulcrum.controller.file_task", return_value={}) as publish,
        ):
            await self.controller._publish_occurrences()
        self.assertEqual(
            [call.args[2].activation for call in publish.call_args_list],
            ["pending", "future"],
        )

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
        with patch("fulcrum.controller._worktree_head", return_value="source-1"):
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

    def test_candidate_lookup_ignores_stale_reused_worktree_history(self) -> None:
        status = {
            "history_items": [
                {
                    "item": {
                        "id": "stale",
                        "metadata": {"worktree_path": str(self.worktree)},
                        "source_oid": "old-head",
                        "state": "canceled",
                        "admission_sequence": 9,
                    }
                }
            ]
        }
        self.assertIsNone(
            _find_candidate(status, str(self.worktree), source_oid="current-head")
        )

    def test_promoted_candidate_with_pending_cleanup_is_not_a_failed_delivery(
        self,
    ) -> None:
        self.assertFalse(
            _candidate_definitively_failed(
                {
                    "state": "promoted",
                    "remote_state": "pending",
                    "cleanup_state": "pending",
                }
            )
        )
        self.assertTrue(_candidate_definitively_failed({"state": "failed"}))
        self.assertTrue(_candidate_definitively_failed({"state": "canceled"}))

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

    async def test_active_pair_delays_delivery_without_operator_hold(self) -> None:
        self.controller.store.execute(
            """UPDATE assignments SET stage = 'delivering', candidate_id = 'candidate-1',
               mandate_candidate_id = 'candidate-1', mandate_scope = scope_snapshot
               WHERE id = ?""",
            (self.assignment["id"],),
        )
        self.controller.store.execute(
            "UPDATE tasks SET runtime_status = 'active' WHERE id = ?",
            (self.overseer["id"],),
        )
        assignment = self.controller.store.row(
            """SELECT a.*, r.project_id FROM assignments a JOIN runs r ON r.id = a.run_id
               WHERE a.id = ?""",
            (self.assignment["id"],),
        )
        self.controller.tollgate = AsyncMock()

        await self.controller._deliver(assignment)

        retained = self.controller.store.row(
            "SELECT stage, operator_hold_id FROM assignments WHERE id = ?",
            (self.assignment["id"],),
        )
        self.assertEqual(retained, {"stage": "delivering", "operator_hold_id": None})
        self.controller.tollgate.approve.assert_not_called()

    async def test_reconcile_releases_stale_delivery_boundary_hold(self) -> None:
        hold = self.controller.store.execute(
            """INSERT INTO holds(scope, target, reason, urgent, release_condition, created_at)
               VALUES ('assignment', ?, 'pair is not confirmed inactive at the delivery boundary',
                       1, 'operator supplies a specific recovery decision', 'now')""",
            (str(self.assignment["id"]),),
        )
        self.controller.store.execute(
            """UPDATE assignments SET prior_stage = 'delivering', stage = 'recovering',
               condition = 'pair is not confirmed inactive at the delivery boundary',
               operator_hold_id = ? WHERE id = ?""",
            (hold.lastrowid, self.assignment["id"]),
        )

        await self.controller._reconcile_delivery_boundary_holds()

        retained = self.controller.store.row(
            "SELECT stage, operator_hold_id, condition FROM assignments WHERE id = ?",
            (self.assignment["id"],),
        )
        self.assertEqual(
            retained,
            {"stage": "delivering", "operator_hold_id": None, "condition": None},
        )
        released = self.controller.store.row(
            "SELECT released_at FROM holds WHERE id = ?", (hold.lastrowid,)
        )
        self.assertIsNotNone(released["released_at"])

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
        workers = {
            row["worker_name"]: row["state"]
            for row in self.controller.store.rows(
                "SELECT worker_name, state FROM worker_heartbeats"
            )
        }
        self.assertEqual(
            workers,
            {name: "running" for name in self.controller.critical_workers},
        )

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
