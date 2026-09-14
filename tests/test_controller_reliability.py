from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from fulcrum.config import (
    InstallationConfig,
    ProjectConfig,
    RuntimePaths,
    save_installation,
)
from fulcrum.controller import (
    Controller,
    DeliveryDisposition,
    INHERITED_LOCK_FD_ENV,
    _classify_delivery_status,
    _find_candidate,
)
from fulcrum.kernel import LeaseRequest, acquire_lease, invariant_violations
from fulcrum.ipc import request
from fulcrum.intake import file_graph
from fulcrum.lifecycle import (
    accept_finish,
    apply_archon_decisions,
    observe_action_terminal,
    schedule_run_archival,
)
from fulcrum.scheduling import ready_assignments
from fulcrum.runtime import AppServerError, thread_facts
from fulcrum.store import Store, StoreError, utc_now
from fulcrum.tollgate import Tollgate, TollgateError, TollgateUncertainError


class FakeCandidateTollgate:
    def __init__(
        self,
        worktree: str,
        *,
        source_oid: str = "source-1",
        tested_oid: str = "tested-1",
    ) -> None:
        self.worktree = worktree
        self.source_oid = source_oid
        self.tested_oid = tested_oid
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
                        "source_oid": self.source_oid,
                    },
                    "generation": {"tested_oid": self.tested_oid},
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


class FakeSuccessfulTollgate:
    def __init__(self) -> None:
        self.approved: list[str] = []

    def approve(self, _repository: str, candidate: str) -> dict[str, Any]:
        self.approved.append(candidate)
        return {"id": candidate, "state": "promoted"}

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


class SequencedDeliveryTollgate:
    def __init__(
        self, *candidates: dict[str, Any], remote_enabled: bool = True
    ) -> None:
        self.candidates = list(candidates)
        self.remote_enabled = remote_enabled
        self.approved: list[str] = []
        self.status_calls = 0
        self.diagnosed: list[str] = []

    def approve(self, _repository: str, candidate: str) -> dict[str, Any]:
        self.approved.append(candidate)
        return {
            "id": candidate,
            "state": "promoted",
            "remote_state": "synchronized",
            "cleanup_state": "pending",
            "certificate_id": "certificate-1",
        }

    def status(self, _repository: str, candidate: str | None = None) -> dict[str, Any]:
        index = min(self.status_calls, len(self.candidates) - 1)
        self.status_calls += 1
        item = dict(self.candidates[index])
        item.setdefault("id", candidate)
        return {
            "configuration": {"remote_enabled": self.remote_enabled},
            "candidate": {"item": item},
        }

    def diagnose(self, _repository: str, candidate: str) -> dict[str, Any]:
        self.diagnosed.append(candidate)
        return {"candidate_id": candidate, "diagnosis": "retained"}


class TerminalDeliveryTollgate(SequencedDeliveryTollgate):
    def __init__(self, state: str) -> None:
        super().__init__({"state": state}, remote_enabled=False)

    def approve(self, _repository: str, candidate: str) -> dict[str, Any]:
        self.approved.append(candidate)
        raise TollgateUncertainError("approval wait ended without usable JSON")


class TransientStatusDeliveryTollgate(SequencedDeliveryTollgate):
    def __init__(self, failures: int, *candidates: dict[str, Any]) -> None:
        super().__init__(*candidates)
        self.failures = failures

    def status(self, repository: str, candidate: str | None = None) -> dict[str, Any]:
        if self.failures:
            self.failures -= 1
            self.status_calls += 1
            raise TollgateError("temporary candidate status read failure")
        return super().status(repository, candidate)


class BlockingApprovalTollgate(FakeSuccessfulTollgate):
    """Approval fake that stays blocked until released by the test.

    The ten-second timeout is only a test deadlock guard. In the successful path,
    the test observes status and releases the fake while approval is still in
    flight.
    """

    def __init__(self) -> None:
        super().__init__()
        self.approval_started = threading.Event()
        self.release_approval = threading.Event()

    def approve(self, repository: str, candidate: str) -> dict[str, Any]:
        self.approval_started.set()
        if not self.release_approval.wait(timeout=10):
            raise TimeoutError(
                "test did not release blocked approval within 10 seconds"
            )
        return super().approve(repository, candidate)


class FakeTerminalTollgate:
    def status(self, _repository: str, candidate: str | None = None) -> dict[str, Any]:
        return {"item": {"id": candidate, "state": "merge-conflict"}}

    def diagnose(self, _repository: str, _candidate: str) -> dict[str, Any]:
        raise TollgateError("diagnosis requires a prepared validation generation")


class FakeBeads:
    def __init__(self) -> None:
        self.closed: list[str] = []
        self.close_reasons: list[str] = []
        self.created: list[Any] = []

    def create(self, task: Any) -> str:
        self.created.append(task)
        return f"p-child-{len(self.created)}"

    def close(self, bead_id: str, reason: str) -> None:
        self.closed.append(bead_id)
        self.close_reasons.append(reason)


class FakeArchiveRuntime:
    def __init__(self) -> None:
        self.archived: list[str] = []
        self.ready = True

    async def archive(self, thread_id: str) -> None:
        self.archived.append(thread_id)
        if thread_id == "executor":
            raise RuntimeError("rollout is missing")


class ControlledArchiveRuntime:
    def __init__(self, thread_ids: list[str]) -> None:
        self.ready = True
        self.statuses = {thread_id: "idle" for thread_id in thread_ids}
        self.turn_ids = {thread_id: f"{thread_id}-turn" for thread_id in thread_ids}
        self.archived: list[str] = []
        self.archive_attempts: list[str] = []
        self.failures_remaining: dict[str, int] = {}

    async def read_thread(
        self, thread_id: str, *, include_turns: bool = True
    ) -> dict[str, Any]:
        thread: dict[str, Any] = {
            "id": thread_id,
            "status": {"type": self.statuses[thread_id]},
            "archived": thread_id in self.archived,
        }
        if include_turns:
            thread["turns"] = [
                {
                    "id": self.turn_ids[thread_id],
                    "status": "completed",
                    "items": [],
                }
            ]
        return thread

    async def archive(self, thread_id: str) -> None:
        self.archive_attempts.append(thread_id)
        remaining = self.failures_remaining.get(thread_id, 0)
        if remaining:
            self.failures_remaining[thread_id] = remaining - 1
            raise RuntimeError("archive failed")
        self.archived.append(thread_id)


class DelayedWeaverRuntime:
    def __init__(self, thread_id: str, *, project_id: str | None = "codex-p") -> None:
        self.ready = True
        self.thread_id = thread_id
        self.project_id = project_id
        self.name = "temporary"
        self.status = "active"
        self.turn_visible = False
        self.turn_id = "weaver-current-turn"
        self.turn_status = "inProgress"
        self.archived: list[str] = []
        self.archive_attempts: list[str] = []

    async def set_name(self, thread_id: str, name: str) -> None:
        assert thread_id == self.thread_id
        self.name = name

    async def read_thread(
        self, thread_id: str, *, include_turns: bool = True
    ) -> dict[str, Any]:
        assert thread_id == self.thread_id
        thread: dict[str, Any] = {
            "id": thread_id,
            "name": self.name,
            "projectId": self.project_id,
            "status": {"type": self.status},
            "archived": thread_id in self.archived,
        }
        if include_turns:
            thread["turns"] = (
                [
                    {
                        "id": self.turn_id,
                        "status": self.turn_status,
                        "items": [],
                    }
                ]
                if self.turn_visible
                else []
            )
        return thread

    async def assign_thread_project(
        self, thread_id: str, project_id: str
    ) -> dict[str, Any]:
        assert thread_id == self.thread_id
        self.project_id = project_id
        # thread/metadata/update may return a summary with no turn history.
        return {
            "thread": {
                "id": thread_id,
                "name": self.name,
                "projectId": project_id,
                "status": {"type": self.status},
            }
        }

    async def archive(self, thread_id: str) -> None:
        assert thread_id == self.thread_id
        self.archive_attempts.append(thread_id)
        self.archived.append(thread_id)


class FreshConversationRuntime:
    def __init__(self, previous_marker: str) -> None:
        self.ready = True
        self.histories = {
            "executor": previous_marker,
            "overseer": previous_marker,
        }
        self.created: list[str] = []
        self.started: list[tuple[str, str]] = []
        self.archived: list[str] = []

    async def create_thread(self, **_kwargs: Any) -> dict[str, Any]:
        thread_id = f"fresh-{len(self.created) + 1}"
        self.created.append(thread_id)
        self.histories[thread_id] = ""
        return {"thread": {"id": thread_id}}

    async def set_name(self, _thread_id: str, _name: str) -> None:
        return None

    async def start_turn(self, thread_id: str, prompt: str, **_kwargs: Any) -> str:
        context = self.histories[thread_id] + prompt
        self.started.append((thread_id, context))
        return f"turn-{len(self.started)}"

    async def read_thread(
        self, thread_id: str, *, include_turns: bool = True
    ) -> dict[str, Any]:
        thread: dict[str, Any] = {
            "id": thread_id,
            "status": {"type": "active" if include_turns else "idle"},
        }
        if include_turns:
            thread["turns"] = [{"id": "turn-1", "status": "inProgress", "items": []}]
        return thread

    async def archive(self, thread_id: str) -> None:
        self.archived.append(thread_id)


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

    def _restart_controller(self) -> None:
        self.controller.store.close()
        if self.controller.lock_handle is not None:
            self.controller.lock_handle.close()
            self.controller.lock_handle = None
        self.controller = Controller(self.paths, self.config)

    def _structured_specialist_action(
        self, thread_id: str = "structured-specialist"
    ) -> tuple[dict[str, Any], Path]:
        task = self.controller.store.register_task(
            native_thread_id=thread_id,
            role="inquisitor",
            description="Structured report",
            model="sol",
            reasoning_effort="high",
        )
        cursor = self.controller.store.execute(
            """INSERT INTO actions(
                   task_id, kind, payload, state, created_at, updated_at
               ) VALUES (?, 'specialist', '{"scope":{"projects":["p"]}}',
                         'active', 'now', 'now')""",
            (task["id"],),
        )
        action = self.controller.store.row(
            "SELECT * FROM actions WHERE id = ?", (cursor.lastrowid,)
        )
        assert action is not None
        path = Path(self.controller._handoff_destinations(action)["report"])
        return action, path

    @staticmethod
    def _structured_options(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
        status = path.stat()
        return {
            "input": payload,
            "input_path": str(path),
            "input_identity": {
                "device": status.st_dev,
                "inode": status.st_ino,
                "size": status.st_size,
                "mtime_ns": status.st_mtime_ns,
                "ctime_ns": status.st_ctime_ns,
            },
        }

    def test_structured_finish_is_bound_cleaned_and_durable(self) -> None:
        action, path = self._structured_specialist_action()
        payload = {"summary": "done", "coverage": ["tests"], "findings": []}
        with self.assertRaisesRegex(StoreError, "missing before acceptance"):
            self.controller._accept_finish_request(
                "structured-specialist",
                "report",
                {"input_path": str(path), "input_missing": True},
            )
        path.write_text(json.dumps(payload), encoding="utf-8")
        result = self.controller._accept_finish_request(
            "structured-specialist", "report", self._structured_options(path, payload)
        )
        self.assertFalse(result["reused"])
        self.assertEqual(result["cleanup"], {"removed": True})
        self.assertFalse(path.exists())
        retained = self.controller.store.row(
            "SELECT * FROM actions WHERE id = ?", (action["id"],)
        )
        self.assertEqual(json.loads(retained["outcome_payload"]), payload)
        self.assertEqual(retained["outcome_input_path"], str(path))

    async def test_action_message_and_context_use_exact_stable_handoff_paths(
        self,
    ) -> None:
        action, path = self._structured_specialist_action()
        task = self.controller.store.row(
            "SELECT * FROM tasks WHERE id = ?", (action["task_id"],)
        )
        assert task is not None
        initial = self.controller._build_action_message(action, task, None)
        self.assertEqual(initial.count(str(path)), 2)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertIn("sibling temporary file", initial)
        self.assertIn("atomically rename", initial)
        self.assertNotIn("evidence_needed", initial)
        self.assertNotIn("/absolute/report.json", initial)

        reminder = self.controller._build_action_message(
            {**action, "reminder_sent": 1}, task, None
        )
        self.assertIn(str(path), reminder)
        context = await self.controller.handle_request(
            {"command": "context", "thread_id": "structured-specialist"}
        )
        self.assertIn(str(path), context["context"])

    def test_structured_finish_rejects_wrong_action_outcome_and_swapped_file(
        self,
    ) -> None:
        _action, report_path = self._structured_specialist_action()
        payload = {"summary": "done", "coverage": ["tests"], "findings": []}
        report_path.write_text(json.dumps(payload), encoding="utf-8")
        options = self._structured_options(report_path, payload)

        wrong = report_path.with_name("requests.json")
        wrong.write_text(json.dumps({"requests": []}), encoding="utf-8")
        with self.assertRaisesRegex(StoreError, "does not match this action"):
            self.controller._accept_finish_request(
                "structured-specialist",
                "report",
                self._structured_options(wrong, {"requests": []}),
            )
        with self.assertRaisesRegex(StoreError, "not a structured outcome"):
            self.controller._accept_finish_request(
                "structured-specialist", "approved", options
            )
        foreign = (
            self.paths.handoff_root
            / ("b" * 32)
            / report_path.parent.name
            / report_path.name
        ).resolve(strict=False)
        foreign.parent.mkdir(parents=True)
        foreign.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(StoreError, "does not match this action"):
            self.controller._accept_finish_request(
                "structured-specialist",
                "report",
                self._structured_options(foreign, payload),
            )

        replacement = report_path.with_name("replacement.json")
        replacement.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(replacement, report_path)
        with self.assertRaisesRegex(StoreError, "identity changed"):
            self.controller._accept_finish_request(
                "structured-specialist", "report", options
            )
        self.assertTrue(report_path.exists())
        report_path.unlink()
        symlink_target = report_path.with_name("symlink-target.json")
        symlink_target.write_text(json.dumps(payload), encoding="utf-8")
        report_path.symlink_to(symlink_target)
        with self.assertRaisesRegex(StoreError, "canonical|non-symlink regular file"):
            self.controller._accept_finish_request(
                "structured-specialist",
                "report",
                self._structured_options(report_path, payload),
            )
        self.assertTrue(report_path.is_symlink())

    def test_structured_finish_cleanup_failure_keeps_accepted_file(self) -> None:
        action, path = self._structured_specialist_action()
        payload = {"summary": "done", "coverage": ["tests"], "findings": []}
        path.write_text(json.dumps(payload), encoding="utf-8")
        options = self._structured_options(path, payload)
        original_unlink = Path.unlink

        def fail_target(candidate: Path, *args: Any, **kwargs: Any) -> None:
            if candidate.name == path.name and candidate.parent.name.startswith(
                ".cleanup-"
            ):
                raise PermissionError("simulated cleanup denial")
            original_unlink(candidate, *args, **kwargs)

        with patch("pathlib.Path.unlink", new=fail_target):
            result = self.controller._accept_finish_request(
                "structured-specialist", "report", options
            )
        self.assertFalse(result["cleanup"]["removed"])
        self.assertIn("simulated cleanup denial", result["cleanup"]["error"])
        retained_path = Path(result["cleanup"]["retained_path"])
        self.assertFalse(path.exists())
        self.assertTrue(retained_path.exists())
        self.assertEqual(json.loads(retained_path.read_text()), payload)
        retained = self.controller.store.row(
            "SELECT outcome_kind FROM actions WHERE id = ?", (action["id"],)
        )
        self.assertEqual(retained["outcome_kind"], "report")
        retry = self.controller._accept_finish_request(
            "structured-specialist",
            "report",
            {"input_path": str(path), "input_missing": True},
        )
        self.assertTrue(retry["reused"])
        self.assertEqual(retry["cleanup"], result["cleanup"])
        self.assertTrue(retained_path.exists())

    def test_cleanup_quarantines_a_path_swap_without_deleting_replacement(
        self,
    ) -> None:
        action, path = self._structured_specialist_action()
        payload = {"summary": "done", "coverage": ["tests"], "findings": []}
        path.write_text(json.dumps(payload), encoding="utf-8")
        options = self._structured_options(path, payload)
        accepted_elsewhere = path.with_name("accepted-moved-aside.json")
        replacement = path.with_name("unrelated-replacement.json")
        replacement_payload = {"unrelated": True}
        replacement.write_text(json.dumps(replacement_payload), encoding="utf-8")
        original_identity = self.controller._handoff_identity
        calls = 0

        def swap_after_cleanup_check(value: os.stat_result) -> dict[str, int]:
            nonlocal calls
            result = original_identity(value)
            calls += 1
            if calls == 4:
                os.rename(path, accepted_elsewhere)
                os.rename(replacement, path)
            return result

        with patch.object(
            self.controller,
            "_handoff_identity",
            side_effect=swap_after_cleanup_check,
        ):
            result = self.controller._accept_finish_request(
                "structured-specialist", "report", options
            )

        self.assertFalse(result["cleanup"]["removed"])
        self.assertIn("retained without deletion", result["cleanup"]["error"])
        retained_replacement = Path(result["cleanup"]["retained_path"])
        self.assertTrue(accepted_elsewhere.exists())
        self.assertEqual(json.loads(accepted_elsewhere.read_text()), payload)
        self.assertTrue(retained_replacement.exists())
        self.assertEqual(
            json.loads(retained_replacement.read_text()), replacement_payload
        )
        self.assertFalse(path.exists())
        retained = self.controller.store.row(
            "SELECT outcome_kind, outcome_input_cleanup FROM actions WHERE id = ?",
            (action["id"],),
        )
        self.assertEqual(retained["outcome_kind"], "report")
        self.assertFalse(json.loads(retained["outcome_input_cleanup"])["removed"])

    def test_exact_retry_survives_cleanup_restart_and_processing(self) -> None:
        action, path = self._structured_specialist_action()
        payload = {"summary": "done", "coverage": ["tests"], "findings": []}
        path.write_text(json.dumps(payload), encoding="utf-8")
        first = self.controller._accept_finish_request(
            "structured-specialist", "report", self._structured_options(path, payload)
        )
        self.assertFalse(first["reused"])
        missing = {"input_path": str(path), "input_missing": True}
        repeated = self.controller._accept_finish_request(
            "structured-specialist", "report", missing
        )
        self.assertTrue(repeated["reused"])
        self.controller.store.execute(
            "UPDATE actions SET state = 'processed' WHERE id = ?", (action["id"],)
        )
        self._restart_controller()
        processed = self.controller._accept_finish_request(
            "structured-specialist", "report", missing
        )
        self.assertTrue(processed["reused"])
        different = {
            "summary": "different",
            "coverage": ["tests"],
            "findings": [],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(different), encoding="utf-8")
        with self.assertRaisesRegex(StoreError, "differs from accepted"):
            self.controller._accept_finish_request(
                "structured-specialist",
                "report",
                self._structured_options(path, different),
            )
        self.assertTrue(path.exists())

    def test_old_action_path_is_rejected_when_a_new_action_is_current(self) -> None:
        action, path = self._structured_specialist_action()
        payload = {"summary": "done", "coverage": ["tests"], "findings": []}
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.controller._accept_finish_request(
            "structured-specialist", "report", self._structured_options(path, payload)
        )
        self.controller.store.execute(
            "UPDATE actions SET state = 'processed' WHERE id = ?", (action["id"],)
        )
        task = self.controller.store.row(
            "SELECT task_id FROM actions WHERE id = ?", (action["id"],)
        )
        self.controller.store.execute(
            """INSERT INTO actions(task_id, kind, payload, state, created_at, updated_at)
               VALUES (?, 'specialist', '{}', 'active', 'later', 'later')""",
            (task["task_id"],),
        )
        with self.assertRaisesRegex(StoreError, "does not match this action"):
            self.controller._accept_finish_request(
                "structured-specialist",
                "report",
                {"input_path": str(path), "input_missing": True},
            )

    def test_handoff_paths_survive_restart_but_new_state_does_not_collide(self) -> None:
        action, path = self._structured_specialist_action()
        concurrent = Path(
            self.controller._handoff_destinations(
                {"id": int(action["id"]) + 1, "kind": "specialist"}
            )["report"]
        )
        self.assertNotEqual(path, concurrent)
        identity = self.controller.store.state_identity()
        self._restart_controller()
        retained = self.controller.store.row(
            "SELECT * FROM actions WHERE id = ?", (action["id"],)
        )
        self.assertEqual(
            Path(self.controller._handoff_destinations(retained)["report"]), path
        )
        self.assertEqual(self.controller.store.state_identity(), identity)

        other_root = Path(self.temporary.name) / "other-state"
        with Store(other_root / "fulcrum.sqlite3") as replacement:
            other = self.paths.handoff_path(
                replacement.state_identity(), int(action["id"]), "report.json"
            )
        self.assertNotEqual(path, other)

    def test_repository_sentinels_are_never_handoff_targets(self) -> None:
        roots = [Path(self.config.projects[0].repo_path), self.worktree]
        for root in roots:
            (root / "decisions.json").write_text("user decisions", encoding="utf-8")
            (root / "reactivation.json").write_text(
                "user reactivation", encoding="utf-8"
            )
        _action, path = self._structured_specialist_action()
        for root in roots:
            self.assertFalse(path.is_relative_to(root))
            self.assertEqual(
                (root / "decisions.json").read_text(encoding="utf-8"),
                "user decisions",
            )
            self.assertEqual(
                (root / "reactivation.json").read_text(encoding="utf-8"),
                "user reactivation",
            )
            self.assertFalse((root / "handoffs").exists())

    def _attach_current_assignment_to_weaver_lineage(self) -> dict[str, Any]:
        weaver = self.controller.store.register_task(
            native_thread_id="bound-worker-weaver",
            role="weaver",
            description="Bound worker lineage",
            model="sol",
            reasoning_effort="high",
            project_id="p",
        )
        lineage = int(weaver["lineage_number"])
        self.controller.store.execute(
            "UPDATE tasks SET lineage_number = ? WHERE id IN (?, ?)",
            (lineage, self.executor["id"], self.overseer["id"]),
        )
        self.controller.store.execute(
            """INSERT INTO bead_lineages(bead_id, weaver_task_id, lineage_number)
               VALUES ('p-1', ?, ?)""",
            (weaver["id"], lineage),
        )
        self.controller.store.execute(
            """UPDATE runs SET weaver_task_id = ?, lineage_number = ?
               WHERE id = ?""",
            (weaver["id"], lineage, self.assignment["run_id"]),
        )
        self.controller.store.execute(
            """UPDATE assignments SET weaver_task_id = ?, lineage_number = ?
               WHERE id = ?""",
            (weaver["id"], lineage, self.assignment["id"]),
        )
        return weaver

    def _prepare_delivery(self, candidate_id: str = "candidate-1") -> dict[str, Any]:
        self.controller.store.execute(
            """UPDATE assignments SET stage = 'delivering', candidate_id = ?,
               mandate_candidate_id = ?, mandate_scope = scope_snapshot,
               condition = NULL, operator_hold_id = NULL WHERE id = ?""",
            (candidate_id, candidate_id, self.assignment["id"]),
        )
        assignment = self.controller.store.row(
            """SELECT a.*, r.project_id FROM assignments a JOIN runs r ON r.id = a.run_id
               WHERE a.id = ?""",
            (self.assignment["id"],),
        )
        assert assignment is not None
        return assignment

    def _delivery_assignment(self) -> dict[str, Any]:
        assignment = self.controller.store.row(
            """SELECT a.*, r.project_id FROM assignments a JOIN runs r ON r.id = a.run_id
               WHERE a.id = ?""",
            (self.assignment["id"],),
        )
        assert assignment is not None
        return assignment

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

    def test_controller_restart_marks_open_usage_partial_without_fabrication(
        self,
    ) -> None:
        now = "2026-01-01T00:00:00Z"
        action_id = int(
            self.controller.store.execute(
                """INSERT INTO actions(task_id, assignment_id, kind, payload,
                       state, native_turn_id, created_at, updated_at)
                   VALUES (?, ?, 'implement', '{}', 'active', 'restart-turn', ?, ?)""",
                (self.executor["id"], self.assignment["id"], now, now),
            ).lastrowid
        )
        self.controller.store.bind_action_turn(action_id, "executor", "restart-turn")
        self.controller.store.observe_turn_usage(
            {
                "threadId": "executor",
                "turnId": "restart-turn",
                "tokenUsage": {
                    "total": {
                        "inputTokens": 5,
                        "cachedInputTokens": 2,
                        "cacheWriteInputTokens": 0,
                        "outputTokens": 1,
                        "reasoningOutputTokens": 0,
                        "totalTokens": 6,
                    }
                },
            }
        )

        self._restart_controller()

        usage = self.controller.store.row(
            "SELECT * FROM action_turn_usage WHERE native_turn_id = 'restart-turn'"
        )
        self.assertEqual(usage["total_tokens"], 6)
        self.assertEqual(usage["coverage"], "partial")
        self.assertIn("controller restarted", usage["gap_reason"])

    def test_current_collaboration_shape_blocks_terminal_until_helper_finishes(
        self,
    ) -> None:
        thread = {
            "status": {"type": "idle"},
            "turns": [
                {
                    "id": "turn",
                    "status": "completed",
                    "items": [
                        {
                            "type": "collabToolCall",
                            "agentStatus": "running",
                            "newThreadId": "helper",
                        }
                    ],
                }
            ],
        }
        self.assertFalse(thread_facts(thread)["helpers_terminal"])
        thread["turns"][0]["items"][0]["agentStatus"] = "completed"
        self.assertTrue(thread_facts(thread)["helpers_terminal"])

    def test_managed_worktree_environment_isolated_from_controller_install(
        self,
    ) -> None:
        fixture = Path(self.temporary.name) / "environment-isolation"
        source = fixture / "source"
        worktree = fixture / "worktree"
        repository = Path(__file__).resolve().parents[1]
        for checkout in (source, worktree):
            checkout.mkdir(parents=True)
            shutil.copytree(repository / "src", checkout / "src")
            shutil.copytree(repository / "scripts", checkout / "scripts")
            for name in (
                "LICENSE",
                "README.md",
                "pyproject.toml",
                "requirements-dev.lock",
            ):
                shutil.copy2(repository / name, checkout / name)

        root_environment = source / ".venv"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "venv",
                "--system-site-packages",
                str(root_environment),
            ],
            check=True,
        )
        root_python = root_environment / "bin" / "python"
        subprocess.run(
            [
                str(root_python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--requirement",
                str(source / "requirements-dev.lock"),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                str(root_python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-deps",
                "--editable",
                str(source),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        root_cli = root_environment / "bin" / "fulcrum"
        root_cli_before = root_cli.read_bytes()

        self.controller.config = InstallationConfig(
            source_root=str(source),
            brain_root=self.config.brain_root,
            state_root=self.config.state_root,
            codex_bin=self.config.codex_bin,
            desktop_executable=self.config.desktop_executable,
            projects=self.config.projects,
        )
        (worktree / ".venv").symlink_to(root_environment, target_is_directory=True)
        asyncio.run(self.controller._ensure_worktree_environment(worktree))
        managed_environment = worktree / ".venv"
        managed_python = managed_environment / "bin" / "python"
        self.assertTrue(managed_environment.is_dir())
        self.assertFalse(managed_environment.is_symlink())

        inspect = """
import importlib.metadata
import json
import pathlib
import site
import sys
distribution = importlib.metadata.distribution('fulcrum')
print(json.dumps({
    'prefix': sys.prefix,
    'site_packages': site.getsitepackages(),
    'distribution_root': str(distribution.locate_file('')),
    'controller': str(pathlib.Path(__import__('fulcrum.controller').controller.__file__).resolve()),
}))
"""

        def inspect_environment(python: Path) -> dict[str, object]:
            result = subprocess.run(
                [str(python), "-I", "-c", inspect],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        root_install = inspect_environment(root_python)
        managed_install = inspect_environment(managed_python)
        self.assertEqual(
            Path(str(root_install["prefix"])).resolve(), root_environment.resolve()
        )
        self.assertEqual(
            Path(str(managed_install["prefix"])).resolve(),
            managed_environment.resolve(),
        )
        self.assertNotEqual(
            root_install["site_packages"], managed_install["site_packages"]
        )
        self.assertNotEqual(
            root_install["distribution_root"], managed_install["distribution_root"]
        )
        self.assertTrue(
            Path(str(root_install["controller"])).is_relative_to(source.resolve())
        )
        self.assertTrue(
            Path(str(managed_install["controller"])).is_relative_to(worktree.resolve())
        )
        self.assertIn(
            str(root_python), root_cli.read_text(encoding="utf-8").splitlines()[0]
        )
        self.assertIn(
            str(managed_python),
            (managed_environment / "bin" / "fulcrum")
            .read_text(encoding="utf-8")
            .splitlines()[0],
        )
        for command in ("black", "pyre"):
            subprocess.run(
                [str(managed_environment / "bin" / command), "--version"],
                check=True,
                capture_output=True,
                text=True,
            )

        sentinel = managed_environment / "executor-owned-sentinel"
        sentinel.write_text("preserve me\n", encoding="utf-8")
        environment_inode = managed_environment.stat().st_ino
        managed_cli_before = (managed_environment / "bin" / "fulcrum").read_bytes()
        asyncio.run(self.controller._ensure_worktree_environment(worktree))
        self.assertEqual(managed_environment.stat().st_ino, environment_inode)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve me\n")
        self.assertEqual(
            (managed_environment / "bin" / "fulcrum").read_bytes(),
            managed_cli_before,
        )

        subprocess.run(
            [str(managed_python), "-m", "pip", "install", "-e", "."],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
        )
        (worktree / "src" / "fulcrum" / "controller.py").write_text(
            "    transient indentation failure\n", encoding="utf-8"
        )
        broken_import = subprocess.run(
            [str(managed_python), "-c", "import fulcrum.controller"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(broken_import.returncode, 0)
        self.assertIn("IndentationError", broken_import.stderr)

        self.assertEqual(root_cli.read_bytes(), root_cli_before)
        root_after = inspect_environment(root_python)
        self.assertTrue(
            Path(str(root_after["controller"])).is_relative_to(source.resolve())
        )
        state = fixture / "state"
        config_path = fixture / "config.json"
        paths = RuntimePaths(
            brain_root=fixture / "brain",
            state_root=state,
            config_file=config_path,
            control_root=fixture / "control",
        )
        with Store(paths.database):
            pass
        save_installation(
            config_path,
            InstallationConfig(
                source_root=str(source),
                brain_root=str(paths.brain_root),
                state_root=str(paths.state_root),
                codex_bin="/bin/false",
                desktop_executable="/bin/false",
            ),
        )
        root_status = subprocess.run(
            [str(root_cli), "status", "--json"],
            env={**os.environ, "FULCRUM_CONFIG": str(config_path)},
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(root_status.returncode, 0, root_status.stderr)
        self.assertIsInstance(json.loads(root_status.stdout), dict)

    async def test_environment_failure_prevents_executor_preparation(
        self,
    ) -> None:
        self.controller.store.execute(
            "UPDATE assignments SET stage = 'preparing' WHERE id = ?",
            (self.assignment["id"],),
        )
        assignment = self.controller.store.row(
            """SELECT a.*, r.project_id FROM assignments a JOIN runs r ON r.id = a.run_id
               WHERE a.id = ?""",
            (self.assignment["id"],),
        )
        self.assertEqual(assignment["stage"], "preparing")
        self.controller.runtime.ready = True
        self.controller.starts_enabled = True
        with (
            patch("fulcrum.controller.ready_assignments", return_value=[assignment]),
            patch.object(
                self.controller,
                "_ensure_worktree_environment",
                side_effect=RuntimeError("environment provisioning failed"),
            ) as ensure_environment,
            patch.object(
                self.controller, "_ensure_pair", new=AsyncMock()
            ) as ensure_pair,
            patch.object(self.controller, "_manage_interviews", new=AsyncMock()),
            patch.object(self.controller, "_start_specialists", new=AsyncMock()),
            patch.object(self.controller, "_queue_proposals", new=AsyncMock()),
            patch.object(self.controller, "_deliver_update_batch", new=AsyncMock()),
            patch.object(self.controller, "_update_readiness"),
        ):
            await self.controller.advance()

        recovered = self.controller.store.row(
            "SELECT * FROM assignments WHERE id = ?", (self.assignment["id"],)
        )
        self.assertEqual(recovered["stage"], "recovering")
        self.assertEqual(recovered["prior_stage"], "preparing")
        self.assertIn("environment provisioning failed", recovered["condition"])
        ensure_environment.assert_awaited_once_with(self.worktree)
        ensure_pair.assert_not_awaited()
        self.assertEqual(
            self.controller.store.rows(
                "SELECT id FROM actions WHERE assignment_id = ? AND state = 'active'",
                (self.assignment["id"],),
            ),
            [],
        )

    async def test_exhausted_worktree_observer_requires_explicit_resolution(
        self,
    ) -> None:
        archon = self.controller.store.register_task(
            native_thread_id="archon",
            role="archon",
            description="Fleet",
            model="sol",
            reasoning_effort="high",
        )
        self.controller.store.execute(
            "UPDATE tasks SET runtime_status = 'idle' WHERE id = ?", (archon["id"],)
        )
        apply_archon_decisions(
            self.controller.store,
            {
                "recurring_policies": [
                    {"kind": "sage", "scope": None},
                    {"kind": "inquisitor", "scope": "p"},
                ]
            },
        )
        for worker in self.controller.critical_workers:
            self.controller.store.heartbeat(worker)
        self.controller.store.execute(
            "INSERT INTO meta(key, value) VALUES ('last_reconciliation', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (
                self.controller.store.row(
                    "SELECT updated_at FROM assignments WHERE id = ?",
                    (self.assignment["id"],),
                )["updated_at"],
            ),
        )
        self.controller.runtime.ready = True
        self.controller.store.execute(
            """UPDATE assignments SET stage = 'preparing', worktree_path = NULL
               WHERE id = ?""",
            (self.assignment["id"],),
        )
        operation_id = self.controller.store.create_operation(
            "tollgate_worktree_create",
            str(self.assignment["id"]),
            {"repository_id": "tg-p", "name": "not-present"},
        )
        attempt = self.controller.store.begin_operation_attempt(operation_id)
        self.controller.store.finish_operation_attempt(
            operation_id,
            attempt,
            state="uncertain",
            error="worktree response was lost",
        )
        operation = self.controller.store.row(
            "SELECT * FROM external_operations WHERE id = ?", (operation_id,)
        )

        self.controller._reconcile_worktree_create(operation)

        exhausted = self.controller.store.row(
            "SELECT * FROM external_operations WHERE id = ?", (operation_id,)
        )
        held = self.controller.store.row(
            "SELECT * FROM assignments WHERE id = ?", (self.assignment["id"],)
        )
        self.assertEqual(exhausted["state"], "uncertain")
        self.assertEqual(exhausted["reconciliation_used"], 1)
        self.assertEqual(held["stage"], "recovering")
        self.assertEqual(held["operator_hold_id"], exhausted["operator_hold_id"])
        self.assertEqual(
            invariant_violations(self.controller.store),
            [
                f"external operation {operation_id} is uncertain after targeted observation"
            ],
        )
        self.controller._update_readiness()
        self.assertFalse(self.controller.starts_enabled)

        apply_archon_decisions(
            self.controller.store,
            {
                "decisions": [
                    {
                        "decision": "resolve_operation",
                        "operation_id": operation_id,
                        "resolution": "confirmed_unsent",
                        "evidence": "operator inspected the exact Git worktree inventory",
                    }
                ]
            },
        )

        recovered = self.controller.store.row(
            "SELECT * FROM assignments WHERE id = ?", (self.assignment["id"],)
        )
        self.assertEqual(recovered["stage"], "queued")
        self.assertIsNone(recovered["operator_hold_id"])
        self.assertEqual(invariant_violations(self.controller.store), [])
        self.controller.store.execute(
            "UPDATE meta SET value = datetime('now') || 'Z' WHERE key = 'last_reconciliation'"
        )
        self.controller._update_readiness()
        self.assertTrue(self.controller.starts_enabled)

    async def test_reconcile_delivers_public_archon_operation_recovery_while_stopped(
        self,
    ) -> None:
        archon = self.controller.store.register_task(
            native_thread_id="archon-recovery",
            role="archon",
            description="Fleet",
            model="sol",
            reasoning_effort="high",
            project_id="p",
        )
        self.controller.store.execute(
            "UPDATE tasks SET runtime_status = 'idle' WHERE id = ?", (archon["id"],)
        )
        apply_archon_decisions(
            self.controller.store,
            {
                "recurring_policies": [
                    {"kind": "sage", "scope": None},
                    {"kind": "inquisitor", "scope": "p"},
                ]
            },
        )
        for worker in self.controller.critical_workers:
            self.controller.store.heartbeat(worker)
        self.controller.store.execute(
            "UPDATE assignments SET stage = 'preparing', worktree_path = NULL WHERE id = ?",
            (self.assignment["id"],),
        )
        operation_id = self.controller.store.create_operation(
            "tollgate_worktree_create",
            str(self.assignment["id"]),
            {"repository_id": "tg-p", "name": "not-present"},
        )
        attempt = self.controller.store.begin_operation_attempt(operation_id)
        self.controller.store.finish_operation_attempt(
            operation_id,
            attempt,
            state="uncertain",
            error="response ended after dispatch",
        )

        database = self.controller.store.path
        event_log = self.controller.store.event_log
        self.controller.store.close()
        self.controller.store = Store(database, event_log=event_log)
        self.controller.runtime.ready = True
        await self.controller.reconcile()

        held = self.controller.store.row(
            "SELECT * FROM external_operations WHERE id = ?", (operation_id,)
        )
        self.assertEqual(held["reconciliation_used"], 1)
        self.assertFalse(self.controller.starts_enabled)
        recovery_update = self.controller.store.row(
            "SELECT * FROM updates WHERE identity = ?",
            (f"operation-resolution:{operation_id}",),
        )
        self.assertIsNotNone(recovery_update)
        self.controller._queue_archon_update(
            "unrelated-while-stopped",
            {"kind": "proposal", "scope": "must remain frozen"},
            actionable=True,
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
                new=AsyncMock(return_value="recovery-turn"),
            ),
        ):
            await self.controller.advance()

        recovery_action = self.controller.store.row(
            """SELECT * FROM actions WHERE task_id = ? AND kind = 'archon'
               ORDER BY id DESC LIMIT 1""",
            (archon["id"],),
        )
        self.assertEqual(recovery_action["state"], "active")
        batch = self.controller.store.row(
            "SELECT id FROM batches WHERE action_id = ?", (recovery_action["id"],)
        )
        handled = [
            row["update_id"]
            for row in self.controller.store.rows(
                "SELECT update_id FROM batch_updates WHERE batch_id = ? ORDER BY update_id",
                (batch["id"],),
            )
        ]
        self.assertEqual(handled, [recovery_update["id"]])
        self.assertEqual(
            self.controller.store.row(
                "SELECT state FROM updates WHERE identity = 'unrelated-while-stopped'"
            )["state"],
            "retained",
        )
        deferral_file = Path(
            self.controller._handoff_destinations(recovery_action)["deferred"]
        )
        deferral_payload = {"next_check_at": "2020-01-01T00:00:00Z"}
        deferral_file.write_text(json.dumps(deferral_payload), encoding="utf-8")
        deferral_options = self._structured_options(deferral_file, deferral_payload)
        deferral_options["reason"] = "collect exact external evidence"
        await self.controller.handle_request(
            {
                "command": "finish",
                "thread_id": "archon-recovery",
                "outcome": "deferred",
                "options": deferral_options,
            }
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
                    "last_turn_id": recovery_action["native_turn_id"],
                    "last_turn_status": "completed",
                    "last_turn_terminal": True,
                    "helpers_terminal": True,
                }
            ),
        ):
            deferred = await self.controller._handle_runtime_event(
                "turn/completed",
                {
                    "threadId": "archon-recovery",
                    "turn": {
                        "id": recovery_action["native_turn_id"],
                        "status": "completed",
                    },
                },
            )
        self.assertTrue(deferred)
        self.assertEqual(
            self.controller.store.row(
                "SELECT state FROM external_operations WHERE id = ?", (operation_id,)
            )["state"],
            "uncertain",
        )
        self.assertFalse(self.controller.starts_enabled)

        with (
            patch.object(
                self.controller,
                "_refresh_task",
                new=AsyncMock(
                    return_value={
                        "can_start": True,
                        "last_turn_id": recovery_action["native_turn_id"],
                        "runtime_status": "idle",
                    }
                ),
            ),
            patch.object(
                self.controller.runtime,
                "start_turn",
                new=AsyncMock(return_value="recovery-turn-redelivered"),
            ),
        ):
            await self.controller.advance()

        recovery_action = self.controller.store.row(
            """SELECT * FROM actions WHERE task_id = ? AND kind = 'archon'
               ORDER BY id DESC LIMIT 1""",
            (archon["id"],),
        )
        self.assertEqual(recovery_action["native_turn_id"], "recovery-turn-redelivered")
        batch = self.controller.store.row(
            "SELECT id FROM batches WHERE action_id = ?", (recovery_action["id"],)
        )
        handled = [
            row["update_id"]
            for row in self.controller.store.rows(
                "SELECT update_id FROM batch_updates WHERE batch_id = ? ORDER BY update_id",
                (batch["id"],),
            )
        ]
        self.assertEqual(handled, [recovery_update["id"]])
        decision_file = Path(
            self.controller._handoff_destinations(recovery_action)["decisions"]
        )
        decision_payload = {
            "decisions": [
                {
                    "decision": "resolve_operation",
                    "operation_id": operation_id,
                    "resolution": "confirmed_unsent",
                    "evidence": "exact Git inventory contains no matching worktree",
                }
            ],
            "handled_update_ids": handled,
        }
        decision_file.write_text(json.dumps(decision_payload), encoding="utf-8")
        await self.controller.handle_request(
            {
                "command": "finish",
                "thread_id": "archon-recovery",
                "outcome": "decisions",
                "options": self._structured_options(decision_file, decision_payload),
            }
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
                    "last_turn_id": recovery_action["native_turn_id"],
                    "last_turn_status": "completed",
                    "last_turn_terminal": True,
                    "helpers_terminal": True,
                }
            ),
        ):
            processed = await self.controller._handle_runtime_event(
                "turn/completed",
                {
                    "threadId": "archon-recovery",
                    "turn": {
                        "id": recovery_action["native_turn_id"],
                        "status": "completed",
                    },
                },
            )
        self.assertTrue(processed)

        self.controller.store.close()
        self.controller.store = Store(database, event_log=event_log)
        resolved = self.controller.store.row(
            "SELECT * FROM external_operations WHERE id = ?", (operation_id,)
        )
        assignment = self.controller.store.row(
            "SELECT * FROM assignments WHERE id = ?", (self.assignment["id"],)
        )
        self.assertEqual(resolved["state"], "canceled")
        self.assertEqual(assignment["stage"], "queued")
        self.assertIsNone(assignment["operator_hold_id"])
        self.assertEqual(invariant_violations(self.controller.store), [])
        self.controller.store.execute(
            "UPDATE tasks SET state = 'idle', runtime_status = 'idle' WHERE id = ?",
            (archon["id"],),
        )
        self.controller.store.execute(
            "UPDATE meta SET value = ? WHERE key = 'last_reconciliation'", (utc_now(),)
        )
        self.controller._update_readiness()
        self.assertTrue(self.controller.starts_enabled)
        self.assertEqual(
            [row["id"] for row in ready_assignments(self.controller.store)],
            [self.assignment["id"]],
        )

    async def test_each_ambiguous_observer_kind_has_supported_resolution(
        self,
    ) -> None:
        class AmbiguousRuntime:
            ready = True

            async def read_thread(
                self, _thread_id: str, *, include_turns: bool = True
            ) -> dict[str, Any]:
                return {"id": "runtime-thread", "turns": [] if include_turns else []}

            async def list_threads(self, **_kwargs: Any) -> list[dict[str, Any]]:
                return []

        class AmbiguousTollgate:
            def status(
                self, _repository: str, candidate: str | None = None
            ) -> dict[str, Any]:
                if candidate is None:
                    return {"queue": []}
                return {
                    "configuration": {"remote_enabled": True},
                    "candidate": {"item": {"id": candidate, "state": "queued"}},
                }

        class AmbiguousBeads:
            def find_intake(self, _intake_key: str) -> list[dict[str, Any]]:
                return []

            def show(self, _bead_id: str) -> None:
                return None

        self.controller.runtime = AmbiguousRuntime()  # type: ignore[assignment]
        self.controller.tollgate = AmbiguousTollgate()  # type: ignore[assignment]
        self.controller.beads = AmbiguousBeads()  # type: ignore[assignment]
        now = "2026-01-01T00:00:00Z"

        def uncertain_operation(
            kind: str, target: str, inputs: dict[str, Any]
        ) -> dict[str, Any]:
            operation_id = self.controller.store.create_operation(kind, target, inputs)
            attempt = self.controller.store.begin_operation_attempt(operation_id)
            self.controller.store.finish_operation_attempt(
                operation_id,
                attempt,
                state="uncertain",
                error="external response was ambiguous",
            )
            operation = self.controller.store.row(
                "SELECT * FROM external_operations WHERE id = ?", (operation_id,)
            )
            assert operation is not None
            return operation

        def resolve(operation: dict[str, Any]) -> None:
            self.assertEqual(operation["state"], "uncertain")
            operation_id = int(operation["id"])
            apply_archon_decisions(
                self.controller.store,
                {
                    "decisions": [
                        {
                            "decision": "resolve_operation",
                            "operation_id": operation_id,
                            "resolution": "confirmed_unsent",
                            "evidence": "operator checked the exact external identity",
                        }
                    ]
                },
            )
            retained = self.controller.store.row(
                "SELECT * FROM external_operations WHERE id = ?", (operation_id,)
            )
            self.assertEqual(retained["state"], "canceled")
            self.assertIsNotNone(
                self.controller.store.row(
                    "SELECT released_at FROM holds WHERE id = ?",
                    (operation["operator_hold_id"],),
                )["released_at"]
            )
            self.assertEqual(invariant_violations(self.controller.store), [])

        self.controller.store.execute(
            "UPDATE assignments SET stage = 'preparing', worktree_path = NULL WHERE id = ?",
            (self.assignment["id"],),
        )
        operation = uncertain_operation(
            "tollgate_worktree_create",
            str(self.assignment["id"]),
            {"repository_id": "tg-p", "name": "not-present"},
        )
        self.controller._reconcile_worktree_create(operation)
        resolve(
            self.controller.store.row(
                "SELECT * FROM external_operations WHERE id = ?", (operation["id"],)
            )
        )

        self.controller.store.execute(
            "UPDATE assignments SET stage = 'implementing', worktree_path = ? WHERE id = ?",
            (str(self.worktree), self.assignment["id"]),
        )
        operation = uncertain_operation(
            "tollgate_candidate_create",
            str(self.assignment["id"]),
            {
                "repository_id": "tg-p",
                "worktree_path": str(self.worktree),
                "revision": "source-1",
            },
        )
        await self.controller._reconcile_tollgate_candidate(operation)
        resolve(
            self.controller.store.row(
                "SELECT * FROM external_operations WHERE id = ?", (operation["id"],)
            )
        )

        self.controller.store.execute(
            "UPDATE assignments SET stage = 'delivering', candidate_id = 'candidate-x' WHERE id = ?",
            (self.assignment["id"],),
        )
        operation = uncertain_operation("tollgate_approve", "candidate-x", {})
        await self.controller._reconcile_tollgate_approve(operation)
        resolve(
            self.controller.store.row(
                "SELECT * FROM external_operations WHERE id = ?", (operation["id"],)
            )
        )

        operation = uncertain_operation("beads_close", "p-1", {})
        self.controller._reconcile_beads_close(operation)
        resolve(
            self.controller.store.row(
                "SELECT * FROM external_operations WHERE id = ?", (operation["id"],)
            )
        )

        action = self.controller.store.execute(
            """INSERT INTO actions(task_id, assignment_id, kind, payload, state,
               created_at, updated_at) VALUES (?, ?, 'implement', '{}', 'uncertain', ?, ?)""",
            (self.executor["id"], self.assignment["id"], now, now),
        )
        self.controller.store.execute(
            """INSERT INTO reservations(action_id, pair_id, state, created_at)
               VALUES (?, ?, 'uncertain', ?)""",
            (action.lastrowid, self.assignment["run_id"], now),
        )
        operation = uncertain_operation(
            "turn_start", str(action.lastrowid), {"thread_id": "executor"}
        )
        await self.controller._reconcile_turn_start(operation)
        resolve(
            self.controller.store.row(
                "SELECT * FROM external_operations WHERE id = ?", (operation["id"],)
            )
        )
        self.controller.store.execute(
            "UPDATE actions SET state = 'canceled' WHERE id = ?", (action.lastrowid,)
        )

        operation = uncertain_operation(
            "thread_start",
            "executor",
            {
                "role": "executor",
                "description": "replacement",
                "project_id": "codex-p",
                "local_project_id": "p",
                "cwd": str(self.worktree),
                "model": "sol",
                "effort": "high",
                "pair_id": self.assignment["run_id"],
                "role_number": 99,
                "title": "replacement",
            },
        )
        await self.controller._reconcile_thread_start(operation)
        resolve(
            self.controller.store.row(
                "SELECT * FROM external_operations WHERE id = ?", (operation["id"],)
            )
        )

        self.controller.store.execute(
            """INSERT INTO obligations(kind, identity, target, state, created_at, updated_at)
               VALUES ('beads_publication', 'new-intake', 'p', 'uncertain', ?, ?)""",
            (now, now),
        )
        operation = uncertain_operation(
            "beads_create",
            "new-intake",
            {
                "intake_key": "new-intake",
                "project": "p",
                "title": "New",
                "description": "New scope",
                "activation": "pending",
                "dependencies": [],
                "context": [],
            },
        )
        self.controller._reconcile_beads_create(operation)
        resolve(
            self.controller.store.row(
                "SELECT * FROM external_operations WHERE id = ?", (operation["id"],)
            )
        )

        operation = uncertain_operation("setup_runtime_smoke", "p", {})
        await self.controller._reconcile_setup_runtime_smoke(operation)
        resolve(
            self.controller.store.row(
                "SELECT * FROM external_operations WHERE id = ?", (operation["id"],)
            )
        )

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

    async def test_unregistered_task_files_one_report_with_metadata_and_retries(
        self,
    ) -> None:
        beads = FakeBeads()
        self.controller.beads = beads  # type: ignore[assignment]
        report = {
            "report_key": "ordinary-one",
            "title": "Repair a small defect",
            "problem": "The helper rejects an empty value",
            "observed_evidence": "The focused test exits with status 1",
            "required_change": "Accept the documented empty value",
            "acceptance_checks": ["The focused empty-value test passes"],
            "dependencies": ["p-1"],
            "context": ["Found while validating unrelated work"],
        }

        first = await self.controller.handle_request(
            {"command": "report", "thread_id": "ordinary", "report": report}
        )
        repeated = await self.controller.handle_request(
            {"command": "report", "thread_id": "ordinary", "report": report}
        )

        self.assertEqual(first["bead_id"], "p-child-1")
        self.assertEqual(first["publication_state"], "complete")
        self.assertFalse(first["reused"])
        self.assertTrue(repeated["reused"])
        self.assertEqual(len(beads.created), 1)
        task = beads.created[0]
        self.assertEqual(task.intake_key, "report:ordinary-one")
        self.assertEqual(task.project, "p")
        self.assertEqual(task.dependencies, ("p-1",))
        self.assertIn("Observed evidence:", task.description)
        self.assertEqual(
            task.report_provenance,
            {
                "source": "unregistered-codex-task",
                "source_thread_id": "ordinary",
            },
        )

        changed = {**report, "required_change": "A different fix"}
        with self.assertRaisesRegex(StoreError, "different content"):
            await self.controller.handle_request(
                {"command": "report", "thread_id": "ordinary", "report": changed}
            )

        separate = {**report, "report_key": "ordinary-two"}
        second = await self.controller.handle_request(
            {"command": "report", "thread_id": "ordinary", "report": separate}
        )
        self.assertEqual(second["bead_id"], "p-child-2")
        self.assertEqual(len(beads.created), 2)

    async def test_managed_report_requires_finish_and_retains_source_provenance(
        self,
    ) -> None:
        beads = FakeBeads()
        self.controller.beads = beads  # type: ignore[assignment]
        action = self.controller.store.execute(
            """INSERT INTO actions(
                   task_id, assignment_id, kind, payload, state, created_at, updated_at
               ) VALUES (?, ?, 'implement', '{}', 'active', 'now', 'now')""",
            (self.executor["id"], self.assignment["id"]),
        )
        report = {
            "report_key": "managed-one",
            "title": "Repair adjacent tooling",
            "problem": "The adjacent tool fails",
            "observed_evidence": "A diagnostic command returned status 2",
            "required_change": "Handle the observed condition",
            "acceptance_checks": ["The diagnostic command passes"],
        }

        with self.assertRaisesRegex(StoreError, "finish the assigned work first"):
            await self.controller.handle_request(
                {"command": "report", "thread_id": "executor", "report": report}
            )
        self.assertEqual(beads.created, [])

        accepted = accept_finish(
            self.controller.store,
            native_thread_id="executor",
            outcome_kind="ready_for_review",
            options={"evidence": "/tmp/evidence"},
        )
        result = await self.controller.handle_request(
            {"command": "report", "thread_id": "executor", "report": report}
        )

        self.assertEqual(result["publication_state"], "complete")
        self.assertEqual(
            beads.created[0].report_provenance,
            {
                "source": "managed-fulcrum-session",
                "source_thread_id": "executor",
                "source_task_id": self.executor["id"],
                "source_role": "executor",
                "source_action_id": action.lastrowid,
                "source_outcome": "ready_for_review",
                "source_assignment_id": self.assignment["id"],
                "source_run_id": self.assignment["run_id"],
                "source_bead_id": "p-1",
            },
        )
        retained = self.controller.store.row(
            "SELECT outcome_kind, outcome_payload, state FROM actions WHERE id = ?",
            (action.lastrowid,),
        )
        self.assertEqual(retained["outcome_kind"], accepted["outcome"])
        self.assertEqual(retained["state"], "active")
        self.assertEqual(
            json.loads(retained["outcome_payload"]), {"evidence": "/tmp/evidence"}
        )

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

    async def test_setup_provisions_recovery_archon_before_retrying_ambiguous_smoke(
        self,
    ) -> None:
        self.controller.config = replace(
            self.controller.config,
            archon_model="sol",
            archon_reasoning_effort="high",
        )
        operation_id = self.controller.store.create_operation(
            "setup_runtime_smoke",
            "p",
            {"cwd": self.config.source_root, "project_id": "codex-p"},
        )
        attempt = self.controller.store.begin_operation_attempt(operation_id)
        self.controller.store.finish_operation_attempt(
            operation_id,
            attempt,
            state="uncertain",
            error="runtime response ended after thread creation",
        )
        operation = self.controller.store.row(
            "SELECT * FROM external_operations WHERE id = ?", (operation_id,)
        )
        self.controller._retain_uncertain_condition(
            operation,
            "setup runtime smoke remains ambiguous after targeted observation",
        )

        runtime = AsyncMock()
        runtime.ready = True
        runtime.list_models.return_value = [
            {
                "model": "sol",
                "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
            }
        ]
        runtime.create_thread.return_value = {"thread": {"id": "setup-archon"}}
        runtime.read_thread.return_value = {
            "id": "setup-archon",
            "name": "Archon",
            "status": {"type": "idle"},
            "turns": [],
        }
        runtime.start_turn.return_value = "recovery-turn"
        self.controller.runtime = runtime

        with patch.object(self.controller, "_verify_projects", new=AsyncMock()):
            first = await self.controller._setup_initialize()
            second = await self.controller._setup_initialize()

        self.assertFalse(first["ready"])
        self.assertEqual(first["archon"], "setup-archon")
        self.assertIn(f"operation {operation_id} remains uncertain", first["condition"])
        self.assertEqual(second["condition"], first["condition"])
        runtime.create_thread.assert_awaited_once()
        self.assertEqual(
            self.controller.store.row(
                "SELECT COUNT(*) AS count FROM external_operations WHERE kind = 'setup_runtime_smoke'"
            )["count"],
            1,
        )
        retained = self.controller.store.row(
            "SELECT * FROM external_operations WHERE id = ?", (operation_id,)
        )
        self.assertEqual(retained["state"], "uncertain")
        self.assertEqual(retained["reconciliation_used"], 1)
        self.assertIsNotNone(retained["operator_hold_id"])
        update = self.controller.store.row(
            "SELECT * FROM updates WHERE identity = ?",
            (f"operation-resolution:{operation_id}",),
        )
        self.assertIsNotNone(update)
        action = self.controller.store.row(
            "SELECT * FROM actions WHERE task_id = (SELECT id FROM tasks WHERE role = 'archon')"
        )
        self.assertEqual(action["kind"], "archon")
        self.assertEqual(action["state"], "active")
        self.assertIn(
            f'"operation_id": {operation_id}', json.dumps(json.loads(action["payload"]))
        )

    async def test_setup_does_not_repeat_ambiguous_initial_archon_creation(
        self,
    ) -> None:
        self.controller.config = replace(
            self.controller.config,
            archon_model="sol",
            archon_reasoning_effort="high",
        )
        runtime = AsyncMock()
        runtime.ready = True
        runtime.list_models.return_value = [
            {
                "model": "sol",
                "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
            }
        ]
        runtime.create_thread.side_effect = AppServerError(
            "connection ended after create dispatch"
        )
        runtime.list_threads.return_value = []
        self.controller.runtime = runtime

        with (
            patch.object(self.controller, "_verify_projects", new=AsyncMock()),
            patch.object(self.controller, "_setup_smoke_check", new=AsyncMock()),
        ):
            with self.assertRaises(AppServerError):
                await self.controller._setup_initialize()

            database = self.controller.store.path
            event_log = self.controller.store.event_log
            self.controller.store.close()
            self.controller.store = Store(database, event_log=event_log)
            with self.assertRaisesRegex(
                StoreError, "resolve it with `fulcrum resolve-operation`"
            ):
                await self.controller._setup_initialize()

            operation = self.controller.store.row(
                "SELECT * FROM external_operations WHERE kind = 'thread_start' AND target = 'archon'"
            )
            self.assertEqual(operation["state"], "uncertain")
            self.assertEqual(operation["reconciliation_used"], 1)
            self.assertIsNotNone(operation["operator_hold_id"])
            runtime.create_thread.assert_awaited_once()

            resolved = await self.controller.handle_request(
                {
                    "command": "resolve_operation",
                    "decision": {
                        "operation_id": operation["id"],
                        "resolution": "confirmed_unsent",
                        "evidence": "filtered runtime inventory contains no thread",
                    },
                }
            )
            self.assertEqual(resolved["applied_decisions"][0]["state"], "canceled")

            runtime.create_thread.side_effect = None
            runtime.create_thread.return_value = {
                "thread": {"id": "recovered-bootstrap-archon"}
            }
            runtime.read_thread.return_value = {
                "id": "recovered-bootstrap-archon",
                "name": "Archon",
                "status": {"type": "idle"},
                "turns": [],
            }
            runtime.start_turn.return_value = "initial-policy-turn"
            result = await self.controller._setup_initialize()

        self.assertFalse(result["ready"])
        self.assertEqual(result["archon"], "recovered-bootstrap-archon")
        self.assertEqual(runtime.create_thread.await_count, 2)
        self.assertEqual(invariant_violations(self.controller.store), [])

    async def test_direct_resolution_recovers_ambiguous_archon_recovery_turn(
        self,
    ) -> None:
        archon = self.controller.store.register_task(
            native_thread_id="self-recovery-archon",
            role="archon",
            description="Fleet",
            model="sol",
            reasoning_effort="high",
            project_id="p",
        )
        primary_id = self.controller.store.create_operation(
            "setup_runtime_smoke", "p", {"cwd": "/tmp/p", "project_id": "codex-p"}
        )
        attempt = self.controller.store.begin_operation_attempt(primary_id)
        self.controller.store.finish_operation_attempt(
            primary_id,
            attempt,
            state="uncertain",
            error="smoke response was lost",
        )
        primary = self.controller.store.row(
            "SELECT * FROM external_operations WHERE id = ?", (primary_id,)
        )
        self.controller._retain_uncertain_condition(
            primary, "setup smoke remained ambiguous after targeted observation"
        )
        self.controller.runtime.ready = True
        self.controller._update_readiness()
        self.assertFalse(self.controller.starts_enabled)
        self.controller.runtime.read_thread = AsyncMock(
            return_value={
                "id": "self-recovery-archon",
                "name": archon["title"],
                "status": {"type": "idle"},
                "turns": [],
            }
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
                new=AsyncMock(side_effect=AppServerError("turn response lost")),
            ),
        ):
            await self.controller.advance()

        turn_operation = self.controller.store.row(
            "SELECT * FROM external_operations WHERE kind = 'turn_start' ORDER BY id DESC LIMIT 1"
        )
        recovery_action = self.controller.store.row(
            "SELECT * FROM actions WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (archon["id"],),
        )
        self.assertEqual(turn_operation["state"], "uncertain")
        self.assertEqual(turn_operation["reconciliation_used"], 1)
        self.assertEqual(recovery_action["state"], "uncertain")
        self.assertEqual(
            recovery_action["operator_hold_id"], turn_operation["operator_hold_id"]
        )

        database = self.controller.store.path
        event_log = self.controller.store.event_log
        self.controller.store.close()
        self.controller.store = Store(database, event_log=event_log)
        await self.controller.handle_request(
            {
                "command": "resolve_operation",
                "decision": {
                    "operation_id": turn_operation["id"],
                    "resolution": "confirmed_unsent",
                    "evidence": "exact Archon history contains no correlated turn",
                },
            }
        )
        self.assertEqual(
            self.controller.store.row(
                "SELECT state FROM actions WHERE id = ?", (recovery_action["id"],)
            )["state"],
            "pending",
        )
        self.assertFalse(self.controller.starts_enabled)

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
                new=AsyncMock(return_value="safe-recovery-retry"),
            ) as retry,
        ):
            await self.controller.advance()

        retry.assert_awaited_once()
        recovered_action = self.controller.store.row(
            "SELECT * FROM actions WHERE id = ?", (recovery_action["id"],)
        )
        self.assertEqual(recovered_action["state"], "active")
        self.assertEqual(recovered_action["native_turn_id"], "safe-recovery-retry")
        self.assertEqual(
            self.controller.store.row(
                "SELECT state FROM external_operations WHERE id = ?",
                (turn_operation["id"],),
            )["state"],
            "canceled",
        )

    async def test_resolved_uncertain_turn_binds_preobserved_usage_immediately(
        self,
    ) -> None:
        now = "2026-01-01T00:00:00Z"
        action_id = int(
            self.controller.store.execute(
                """INSERT INTO actions(task_id, assignment_id, kind, payload,
                       state, created_at, updated_at)
                   VALUES (?, ?, 'implement', '{}', 'uncertain', ?, ?)""",
                (self.executor["id"], self.assignment["id"], now, now),
            ).lastrowid
        )
        self.controller.store.execute(
            """INSERT INTO reservations(action_id, pair_id, global_slots,
                   project_ids, state, created_at)
               VALUES (?, ?, 1, '["p"]', 'uncertain', ?)""",
            (action_id, self.executor["pair_id"], now),
        )
        operation_id = self.controller.store.create_operation(
            "turn_start",
            str(action_id),
            {
                "thread_id": "executor",
                "baseline_turn_id": "prior-turn",
                "prompt": "retained prompt",
            },
        )
        attempt = self.controller.store.begin_operation_attempt(operation_id)
        self.controller.store.finish_operation_attempt(
            operation_id,
            attempt,
            state="uncertain",
            error="turn start response was lost",
        )
        operation = self.controller.store.row(
            "SELECT * FROM external_operations WHERE id = ?", (operation_id,)
        )
        self.controller._retain_uncertain_condition(
            operation, "turn start remained ambiguous after targeted observation"
        )

        self.controller.store.observe_turn_usage(
            {
                "threadId": "executor",
                "turnId": "resolved-turn",
                "tokenUsage": {
                    "total": {
                        "inputTokens": 8,
                        "cachedInputTokens": 4,
                        "cacheWriteInputTokens": 0,
                        "outputTokens": 2,
                        "reasoningOutputTokens": 1,
                        "totalTokens": 10,
                    }
                },
            }
        )
        before = self.controller.store.row(
            """SELECT action_id, attributed_action_id, coverage
               FROM action_turn_usage WHERE native_thread_id = 'executor'
                 AND native_turn_id = 'resolved-turn'"""
        )
        self.assertEqual(
            before,
            {
                "action_id": None,
                "attributed_action_id": None,
                "coverage": "observed",
            },
        )
        request = {
            "command": "resolve_operation",
            "decision": {
                "operation_id": operation_id,
                "resolution": "observed_success",
                "native_id": "resolved-turn",
                "evidence": "operator inspected the exact native turn identity",
            },
        }

        resolved = await self.controller.handle_request(request)
        replayed = await self.controller.handle_request(request)

        self.assertFalse(resolved["applied_decisions"][0]["reused"])
        self.assertTrue(replayed["applied_decisions"][0]["reused"])
        action = self.controller.store.row(
            "SELECT state, native_turn_id FROM actions WHERE id = ?", (action_id,)
        )
        self.assertEqual(action, {"state": "active", "native_turn_id": "resolved-turn"})
        usage = self.controller.store.row(
            """SELECT action_id, attributed_action_id, total_tokens, coverage
               FROM action_turn_usage WHERE native_thread_id = 'executor'
                 AND native_turn_id = 'resolved-turn'"""
        )
        self.assertEqual(
            usage,
            {
                "action_id": action_id,
                "attributed_action_id": action_id,
                "total_tokens": 10,
                "coverage": "observed",
            },
        )
        self.assertEqual(
            self.controller.store.row("""SELECT COUNT(*) AS count FROM action_turn_usage
                   WHERE native_thread_id = 'executor'
                     AND native_turn_id = 'resolved-turn'""")["count"],
            1,
        )
        self.assertEqual(
            self.controller.store.action_usage_summary(action_id),
            {
                "direct_total_tokens": 10,
                "attributed_total_tokens": 10,
                "coverage": "observed",
                "turn_count": 1,
            },
        )
        for report in (
            self.controller.store.usage_report(
                assignment_id=int(self.assignment["id"]), group_by="assignment"
            ),
            self.controller.store.usage_report(
                run_id=int(self.assignment["run_id"]), group_by="run"
            ),
            self.controller.store.usage_report(role="executor", group_by="role"),
            self.controller.store.usage_report(project_id="p", group_by="project"),
        ):
            self.assertEqual(report["groups"][0]["direct"]["total_tokens"], 10)
            self.assertEqual(report["groups"][0]["attributed"]["total_tokens"], 10)

    async def test_reconcile_repairs_interrupted_resolved_turn_usage_binding(
        self,
    ) -> None:
        now = "2026-01-01T00:00:00Z"
        action_id = int(
            self.controller.store.execute(
                """INSERT INTO actions(task_id, assignment_id, kind, payload,
                       state, created_at, updated_at)
                   VALUES (?, ?, 'implement', '{}', 'uncertain', ?, ?)""",
                (self.executor["id"], self.assignment["id"], now, now),
            ).lastrowid
        )
        self.controller.store.execute(
            """INSERT INTO reservations(action_id, pair_id, global_slots,
                   project_ids, state, created_at)
               VALUES (?, ?, 1, '["p"]', 'uncertain', ?)""",
            (action_id, self.executor["pair_id"], now),
        )
        operation_id = self.controller.store.create_operation(
            "turn_start",
            str(action_id),
            {
                "thread_id": "executor",
                "baseline_turn_id": "prior-turn",
                "prompt": "retained prompt",
            },
        )
        attempt = self.controller.store.begin_operation_attempt(operation_id)
        self.controller.store.finish_operation_attempt(
            operation_id,
            attempt,
            state="uncertain",
            error="turn start response was lost",
        )
        operation = self.controller.store.row(
            "SELECT * FROM external_operations WHERE id = ?", (operation_id,)
        )
        self.controller._retain_uncertain_condition(
            operation, "turn start remained ambiguous after targeted observation"
        )
        self.controller.store.observe_turn_usage(
            {
                "threadId": "executor",
                "turnId": "resolved-turn",
                "tokenUsage": {
                    "total": {
                        "inputTokens": 8,
                        "cachedInputTokens": 4,
                        "cacheWriteInputTokens": 0,
                        "outputTokens": 2,
                        "reasoningOutputTokens": 1,
                        "totalTokens": 10,
                    }
                },
            }
        )

        # Commit the lifecycle mutation directly to model process loss before the
        # controller command can perform its follow-up usage binding.
        apply_archon_decisions(
            self.controller.store,
            {
                "decisions": [
                    {
                        "decision": "resolve_operation",
                        "operation_id": operation_id,
                        "resolution": "observed_success",
                        "native_id": "resolved-turn",
                        "evidence": "operator inspected the exact native turn identity",
                    }
                ]
            },
        )
        orphaned = self.controller.store.row(
            """SELECT action_id, attributed_action_id FROM action_turn_usage
               WHERE native_thread_id = 'executor'
                 AND native_turn_id = 'resolved-turn'"""
        )
        self.assertEqual(orphaned, {"action_id": None, "attributed_action_id": None})

        self._restart_controller()
        runtime = AsyncMock()
        runtime.ready = True
        runtime.read_thread.return_value = {
            "id": "executor",
            "name": self.executor["title"],
            "projectId": "codex-p",
            "status": {"type": "active"},
            "turns": [{"id": "resolved-turn", "status": "inProgress", "items": []}],
        }
        self.controller.runtime = runtime

        await self.controller.reconcile()
        await self.controller.reconcile()

        usage = self.controller.store.row(
            """SELECT action_id, attributed_action_id, total_tokens,
                      coverage, gap_reason
               FROM action_turn_usage WHERE native_thread_id = 'executor'
                 AND native_turn_id = 'resolved-turn'"""
        )
        self.assertEqual(usage["action_id"], action_id)
        self.assertEqual(usage["attributed_action_id"], action_id)
        self.assertEqual(usage["total_tokens"], 10)
        self.assertEqual(usage["coverage"], "partial")
        self.assertIn("controller restarted", usage["gap_reason"])
        self.assertEqual(
            self.controller.store.row("""SELECT COUNT(*) AS count FROM action_turn_usage
                   WHERE native_thread_id = 'executor'
                     AND native_turn_id = 'resolved-turn'""")["count"],
            1,
        )
        self.assertEqual(
            self.controller.store.action_usage_summary(action_id),
            {
                "direct_total_tokens": 10,
                "attributed_total_tokens": 10,
                "coverage": "partial",
                "turn_count": 1,
            },
        )
        for report in (
            self.controller.store.usage_report(
                assignment_id=int(self.assignment["id"]), group_by="assignment"
            ),
            self.controller.store.usage_report(
                run_id=int(self.assignment["run_id"]), group_by="run"
            ),
            self.controller.store.usage_report(role="executor", group_by="role"),
            self.controller.store.usage_report(project_id="p", group_by="project"),
        ):
            group = report["groups"][0]
            self.assertEqual(group["direct"]["total_tokens"], 10)
            self.assertEqual(group["attributed"]["total_tokens"], 10)
            self.assertEqual(group["coverage"], "partial")

    async def test_weaver_registration_binds_the_current_native_turn(self) -> None:
        runtime = AsyncMock()
        runtime.read_thread.return_value = {
            "id": "weaver-thread",
            "name": "temporary",
            "projectId": None,
            "status": {"type": "active"},
            "turns": [{"id": "weaver-turn", "status": "inProgress", "items": []}],
        }
        runtime.assign_thread_project.return_value = {
            "thread": {
                "id": "weaver-thread",
                "name": "temporary",
                "projectId": "codex-p",
                "status": {"type": "active"},
            }
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
        task = self.controller.store.row(
            "SELECT * FROM tasks WHERE native_thread_id = ?", ("weaver-thread",)
        )
        self.assertEqual(action["native_turn_id"], "weaver-turn")
        self.assertEqual(result["thread_id"], "weaver-thread")
        self.assertEqual(task["description"], "File a task")
        self.assertEqual(task["title"], "🧵 [WVR0001] File a task")
        self.assertEqual(result["title"], task["title"])
        self.assertIn(
            ("weaver-thread", task["title"]),
            [call.args for call in runtime.set_name.await_args_list],
        )
        runtime.assign_thread_project.assert_awaited_once_with(
            "weaver-thread", "codex-p"
        )

    async def test_unbound_weaver_reconciles_and_archives_after_idle_delay(
        self,
    ) -> None:
        runtime = DelayedWeaverRuntime("delayed-weaver", project_id=None)
        self.controller.runtime = runtime  # type: ignore[assignment]
        await self.controller._register_weaver(
            {
                "thread_id": "delayed-weaver",
                "project": "p",
                "description": "Retain delayed intake",
                "writable": True,
            }
        )
        task = self.controller.store.row(
            "SELECT * FROM tasks WHERE native_thread_id = 'delayed-weaver'"
        )
        action = self.controller.store.row(
            "SELECT * FROM actions WHERE task_id = ?", (task["id"],)
        )
        self.assertIsNone(action["native_turn_id"])
        self.assertTrue(json.loads(action["payload"])["adopt_current_turn"])
        accept_finish(
            self.controller.store,
            native_thread_id="delayed-weaver",
            outcome_kind="intake_complete",
            options={},
        )

        self._restart_controller()
        self.controller.runtime = runtime  # type: ignore[assignment]
        runtime.turn_visible = True
        await self.controller.reconcile()
        action = self.controller.store.row(
            "SELECT * FROM actions WHERE id = ?", (action["id"],)
        )
        self.assertEqual(action["native_turn_id"], runtime.turn_id)
        self.assertEqual(action["state"], "active")

        runtime.status = "idle"
        runtime.turn_status = "completed"
        await self.controller.reconcile()
        action = self.controller.store.row(
            "SELECT * FROM actions WHERE id = ?", (action["id"],)
        )
        self.assertEqual(action["state"], "processed")
        obligations = self.controller.store.rows(
            "SELECT * FROM obligations WHERE kind = 'archive' AND target = ?",
            ("delayed-weaver",),
        )
        self.assertEqual(len(obligations), 1)

        with patch("fulcrum.controller.utc_now", return_value="2026-01-01T00:00:00Z"):
            await self.controller._archive_ready_tasks()
        self.assertEqual(runtime.archive_attempts, [])
        deadline = self.controller.store.row(
            "SELECT archive_eligible_at FROM tasks WHERE id = ?", (task["id"],)
        )["archive_eligible_at"]
        self.assertEqual(deadline, "2026-01-01T00:10:00Z")

        self._restart_controller()
        self.controller.runtime = runtime  # type: ignore[assignment]
        with patch("fulcrum.controller.utc_now", return_value="2026-01-01T00:05:00Z"):
            await self.controller.reconcile()
        self.assertEqual(
            self.controller.store.row(
                "SELECT archive_eligible_at FROM tasks WHERE id = ?", (task["id"],)
            )["archive_eligible_at"],
            deadline,
        )
        with patch("fulcrum.controller.utc_now", return_value="2026-01-01T00:09:59Z"):
            await self.controller._archive_ready_tasks()
        self.assertEqual(runtime.archive_attempts, [])
        with patch("fulcrum.controller.utc_now", return_value="2026-01-01T00:10:00Z"):
            await self.controller._archive_ready_tasks()
        self.assertEqual(runtime.archive_attempts, ["delayed-weaver"])

    async def test_completion_event_adopts_matching_unbound_weaver_turn(self) -> None:
        runtime = DelayedWeaverRuntime("event-weaver")
        self.controller.runtime = runtime  # type: ignore[assignment]
        await self.controller._register_weaver(
            {
                "thread_id": "event-weaver",
                "project": "p",
                "description": "Complete event intake",
                "writable": True,
            }
        )
        accept_finish(
            self.controller.store,
            native_thread_id="event-weaver",
            outcome_kind="intake_complete",
            options={},
        )
        action = self.controller.store.current_action("event-weaver")
        # Retain the pre-repair payload shape from already-stranded installations.
        self.controller.store.execute(
            "UPDATE actions SET payload = ? WHERE id = ?",
            (json.dumps({"mode": "intake", "project": "p"}), action["id"]),
        )
        runtime.turn_visible = True
        runtime.status = "idle"
        runtime.turn_status = "completed"

        handled = await self.controller._handle_runtime_event(
            "turn/completed",
            {
                "threadId": "event-weaver",
                "turn": {"id": runtime.turn_id, "status": "completed"},
            },
        )

        self.assertTrue(handled)
        action = self.controller.store.row("""SELECT * FROM actions WHERE task_id =
                   (SELECT id FROM tasks WHERE native_thread_id = 'event-weaver')""")
        self.assertEqual(action["native_turn_id"], runtime.turn_id)
        self.assertEqual(action["state"], "processed")

    async def test_invisible_completion_does_not_authorize_a_later_turn(self) -> None:
        runtime = DelayedWeaverRuntime("eventually-consistent-weaver")
        self.controller.runtime = runtime  # type: ignore[assignment]
        await self.controller._register_weaver(
            {
                "thread_id": "eventually-consistent-weaver",
                "project": "p",
                "description": "Retain the original completion",
                "writable": True,
            }
        )
        accept_finish(
            self.controller.store,
            native_thread_id="eventually-consistent-weaver",
            outcome_kind="intake_complete",
            options={},
        )

        handled = await self.controller._handle_runtime_event(
            "turn/completed",
            {
                "threadId": "eventually-consistent-weaver",
                "turn": {"id": "original-turn", "status": "completed"},
            },
        )
        self.assertTrue(handled)
        action = self.controller.store.current_action("eventually-consistent-weaver")
        self.assertIsNone(action["native_turn_id"])
        self.assertEqual(
            json.loads(action["payload"])["unbound_completion_turn_id"],
            "original-turn",
        )

        self._restart_controller()
        self.controller.runtime = runtime  # type: ignore[assignment]
        runtime.turn_visible = True
        runtime.turn_id = "later-unrelated-turn"
        await self.controller.reconcile()
        action = self.controller.store.current_action("eventually-consistent-weaver")
        self.assertIsNone(action["native_turn_id"])
        self.assertEqual(action["state"], "active")

        runtime.turn_id = "original-turn"
        runtime.turn_status = "completed"
        runtime.status = "idle"
        await self.controller.reconcile()
        action = self.controller.store.row(
            "SELECT * FROM actions WHERE id = ?", (action["id"],)
        )
        self.assertEqual(action["native_turn_id"], "original-turn")
        self.assertEqual(action["state"], "processed")
        self.assertEqual(
            len(
                self.controller.store.rows(
                    """SELECT * FROM obligations
                       WHERE kind = 'archive' AND target = ?""",
                    ("eventually-consistent-weaver",),
                )
            ),
            1,
        )

    async def test_unbound_weaver_does_not_adopt_mismatched_or_preexisting_turn(
        self,
    ) -> None:
        runtime = DelayedWeaverRuntime("stale-weaver")
        self.controller.runtime = runtime  # type: ignore[assignment]
        await self.controller._register_weaver(
            {
                "thread_id": "stale-weaver",
                "project": "p",
                "description": "Reject stale completion",
                "writable": True,
            }
        )
        runtime.turn_visible = True
        runtime.status = "idle"
        runtime.turn_status = "completed"

        handled = await self.controller._handle_runtime_event(
            "turn/completed",
            {
                "threadId": "stale-weaver",
                "turn": {"id": "different-old-turn", "status": "completed"},
            },
        )
        self.assertFalse(handled)
        action = self.controller.store.row("""SELECT * FROM actions WHERE task_id =
                   (SELECT id FROM tasks WHERE native_thread_id = 'stale-weaver')""")
        self.assertIsNone(action["native_turn_id"])
        self.controller.store.execute(
            "UPDATE actions SET state = 'canceled' WHERE id = ?", (action["id"],)
        )

        idle_runtime = DelayedWeaverRuntime("preexisting-weaver")
        idle_runtime.status = "idle"
        self.controller.runtime = idle_runtime  # type: ignore[assignment]
        await self.controller._register_weaver(
            {
                "thread_id": "preexisting-weaver",
                "project": "p",
                "description": "Reject pre-registration history",
                "writable": True,
            }
        )
        accept_finish(
            self.controller.store,
            native_thread_id="preexisting-weaver",
            outcome_kind="intake_complete",
            options={},
        )
        idle_runtime.turn_visible = True
        idle_runtime.turn_status = "completed"
        await self.controller.reconcile()
        preexisting = self.controller.store.row(
            """SELECT * FROM actions WHERE task_id =
                   (SELECT id FROM tasks WHERE native_thread_id = 'preexisting-weaver')"""
        )
        self.assertIsNone(preexisting["native_turn_id"])

    async def test_weaver_registration_rejects_missing_and_empty_descriptions(
        self,
    ) -> None:
        self.controller.runtime = AsyncMock()

        for description in (None, "", " \t\n "):
            request = {
                "thread_id": "invalid-weaver",
                "project": "p",
                "writable": True,
            }
            if description is not None:
                request["description"] = description
            with (
                self.subTest(description=description),
                self.assertRaisesRegex(StoreError, "nonempty description"),
            ):
                await self.controller._register_weaver(request)

        self.assertIsNone(
            self.controller.store.row("SELECT * FROM tasks WHERE role = 'weaver'")
        )
        self.controller.runtime.set_name.assert_not_awaited()

    async def test_plan_mode_weaver_registration_retains_name_without_action(
        self,
    ) -> None:
        title = "🧵 [WVR0001] Plan safer authentication"
        runtime = AsyncMock()
        runtime.read_thread.return_value = {
            "id": "planning-weaver",
            "name": title,
            "projectId": "codex-p",
            "status": {"type": "active"},
            "turns": [{"id": "planning-turn", "status": "inProgress", "items": []}],
        }
        self.controller.runtime = runtime

        result = await self.controller._register_weaver(
            {
                "thread_id": "planning-weaver",
                "project": "p",
                "description": "Plan safer authentication",
                "writable": False,
            }
        )

        task = self.controller.store.row(
            "SELECT * FROM tasks WHERE native_thread_id = ?", ("planning-weaver",)
        )
        self.assertEqual(task["description"], "Plan safer authentication")
        self.assertEqual(task["title"], title)
        self.assertEqual(result["title"], title)
        self.assertIn("do not publish or call finish", result["instructions"])
        self.assertIsNone(
            self.controller.store.row(
                "SELECT * FROM actions WHERE task_id = ?", (task["id"],)
            )
        )
        runtime.set_name.assert_awaited_once_with("planning-weaver", title)

    async def test_weaver_registration_retry_keeps_original_task_and_action(
        self,
    ) -> None:
        title = "🧵 [WVR0001] Diagnose flaky checkout"
        runtime = AsyncMock()
        runtime.read_thread.return_value = {
            "id": "retry-weaver",
            "name": title,
            "projectId": "codex-p",
            "status": {"type": "active"},
            "turns": [{"id": "retry-turn", "status": "inProgress", "items": []}],
        }
        self.controller.runtime = runtime
        initial_request = {
            "thread_id": "retry-weaver",
            "project": "p",
            "description": "Diagnose flaky checkout",
            "writable": True,
        }

        first = await self.controller._register_weaver(initial_request)
        second = await self.controller._register_weaver(
            {**initial_request, "description": "A changed retry description"}
        )

        tasks = self.controller.store.rows(
            "SELECT * FROM tasks WHERE native_thread_id = ?", ("retry-weaver",)
        )
        actions = self.controller.store.rows(
            "SELECT * FROM actions WHERE task_id = ?", (tasks[0]["id"],)
        )
        self.assertEqual(len(tasks), 1)
        self.assertEqual(len(actions), 1)
        self.assertEqual(tasks[0]["description"], "Diagnose flaky checkout")
        self.assertEqual(first["title"], title)
        self.assertEqual(second["title"], title)

    async def test_project_verification_replaces_a_mismatched_saved_identity(
        self,
    ) -> None:
        runtime = AsyncMock()
        runtime.list_projects.return_value = [
            {
                "id": "wrong-project",
                "roots": [{"path": str(self.temporary.name)}],
            },
            {
                "id": "correct-project",
                "roots": [{"path": self.config.source_root}],
            },
        ]
        self.controller.runtime = runtime

        await self.controller._verify_projects()

        self.assertEqual(
            self.controller.config.projects[0].codex_project_id,
            "correct-project",
        )
        self.assertEqual(
            self.controller.store.row(
                "SELECT codex_project_id FROM projects WHERE project_id = 'p'"
            )["codex_project_id"],
            "correct-project",
        )

    async def test_thread_reconciliation_adopts_and_attaches_unassigned_thread(
        self,
    ) -> None:
        inputs = {
            "role": "executor",
            "description": "Recovered executor",
            "project_id": "codex-p",
            "local_project_id": "p",
            "cwd": str(self.worktree),
            "model": "sol",
            "effort": "high",
            "pair_id": None,
            "role_number": 2,
            "title": "Recovered executor",
        }
        operation_id = self.controller.store.create_operation(
            "thread_start", "executor", inputs
        )
        attempt = self.controller.store.begin_operation_attempt(operation_id)
        self.controller.store.finish_operation_attempt(
            operation_id,
            attempt,
            state="uncertain",
            error="connection ended after dispatch",
        )
        operation = self.controller.store.row(
            "SELECT * FROM external_operations WHERE id = ?", (operation_id,)
        )
        created_at = datetime.fromisoformat(
            operation["created_at"].replace("Z", "+00:00")
        )
        unassigned = {
            "id": "unassigned-executor",
            "model": "sol",
            "projectId": None,
            "createdAt": int(created_at.timestamp()),
            "status": {"type": "idle"},
            "turns": [],
        }
        runtime = AsyncMock()
        runtime.list_threads.side_effect = [[], [unassigned]]
        runtime.assign_thread_project.return_value = {
            "thread": {**unassigned, "projectId": "codex-p"}
        }
        self.controller.runtime = runtime

        await self.controller._reconcile_thread_start(operation)

        self.assertEqual(
            runtime.list_threads.await_args_list[0].kwargs["project_id"], "codex-p"
        )
        self.assertIsNone(runtime.list_threads.await_args_list[1].kwargs["project_id"])
        runtime.assign_thread_project.assert_awaited_once_with(
            "unassigned-executor", "codex-p"
        )
        retained = self.controller.store.row(
            "SELECT state, native_id FROM external_operations WHERE id = ?",
            (operation_id,),
        )
        self.assertEqual(
            retained, {"state": "complete", "native_id": "unassigned-executor"}
        )

    async def test_startup_repairs_an_existing_unassigned_task(self) -> None:
        self.controller.store.execute(
            "UPDATE tasks SET archived = 1 WHERE id = ?", (self.overseer["id"],)
        )
        runtime = AsyncMock()
        runtime.read_thread.return_value = {
            "id": "executor",
            "projectId": None,
        }
        runtime.assign_thread_project.return_value = {
            "thread": {"id": "executor", "projectId": "codex-p"}
        }
        self.controller.runtime = runtime

        await self.controller._repair_task_project_bindings()

        runtime.read_thread.assert_awaited_once_with("executor", include_turns=False)
        runtime.assign_thread_project.assert_awaited_once_with("executor", "codex-p")
        event = self.controller.store.row(
            "SELECT * FROM events WHERE kind = 'task_project_repaired'"
        )
        self.assertEqual(event["entity_id"], str(self.executor["id"]))

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
                "projectId": "codex-p",
                "status": {"type": "notLoaded"},
                "turns": [],
            },
            {
                "id": "executor",
                "projectId": "codex-p",
                "status": {"type": "idle"},
                "turns": [],
            },
        ]
        runtime.start_turn.return_value = "turn-1"
        self.controller.runtime = runtime

        await self.controller._dispatch_action(action)

        runtime.resume_thread.assert_awaited_once_with("executor")
        runtime.start_turn.assert_awaited_once()
        self.assertEqual(
            runtime.start_turn.await_args.kwargs["cwd"], self.config.source_root
        )
        self.assertEqual(
            runtime.start_turn.await_args.kwargs["workspace_root"],
            self.config.source_root,
        )
        routing = runtime.start_turn.await_args.kwargs["developer_instructions"]
        self.assertIn(str(self.worktree), routing)
        self.assertIn(self.config.source_root, routing)
        self.assertIn("Do not modify the canonical checkout", routing)

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

    async def test_sequential_assignments_reuse_lineage_pair_conversations(
        self,
    ) -> None:
        now = "2026-01-01T00:00:00Z"
        weaver = self.controller.store.register_task(
            native_thread_id="lineage-weaver",
            role="weaver",
            description="Related work",
            model="sol",
            reasoning_effort="high",
            project_id="p",
        )
        self.controller.store.execute(
            "UPDATE tasks SET lineage_number = ? WHERE id IN (?, ?)",
            (weaver["role_number"], self.executor["id"], self.overseer["id"]),
        )
        self.controller.store.execute(
            "INSERT INTO bead_lineages(bead_id, weaver_task_id, lineage_number) VALUES ('p-1', ?, ?)",
            (weaver["id"], weaver["role_number"]),
        )
        self.controller.store.execute(
            "UPDATE runs SET weaver_task_id = ?, lineage_number = ? WHERE id = ?",
            (weaver["id"], weaver["role_number"], self.assignment["run_id"]),
        )
        previous_marker = "FIRST-ASSIGNMENT-RAW-TOOL-OUTPUT-9f3b"
        old_payload = json.dumps({"raw_output": previous_marker})
        implementation = self.controller.store.execute(
            """INSERT INTO actions(
                   task_id, assignment_id, kind, payload, state,
                   outcome_kind, outcome_payload, created_at, updated_at
               ) VALUES (?, ?, 'implement', ?, 'processed',
                         'ready_for_review', ?, ?, ?)""",
            (
                self.executor["id"],
                self.assignment["id"],
                old_payload,
                old_payload,
                now,
                now,
            ),
        )
        self.controller.store.execute(
            """UPDATE assignments SET candidate_id = 'candidate-1',
                   source_oid = 'source-1', tested_oid = 'tested-1' WHERE id = ?""",
            (self.assignment["id"],),
        )
        self.controller.store.execute(
            """INSERT INTO handoffs(
                   assignment_id, source_action_id, kind, content_json, created_at
               ) VALUES (?, ?, 'implementation_evidence', ?, ?)""",
            (self.assignment["id"], implementation.lastrowid, old_payload, now),
        )
        self.controller.store.execute(
            """INSERT INTO actions(
                   task_id, assignment_id, kind, payload, state,
                   outcome_kind, outcome_payload, created_at, updated_at
               ) VALUES (?, ?, 'review', ?, 'processed', 'approved', ?, ?, ?)""",
            (
                self.overseer["id"],
                self.assignment["id"],
                old_payload,
                old_payload,
                now,
                now,
            ),
        )
        self.controller.store.execute(
            "UPDATE assignments SET stage = 'completed' WHERE id = ?",
            (self.assignment["id"],),
        )
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
        self.controller.store.execute(
            "INSERT INTO bead_lineages(bead_id, weaver_task_id, lineage_number) VALUES ('p-2', ?, ?)",
            (weaver["id"], weaver["role_number"]),
        )
        position = self.controller.store.execute(
            "INSERT INTO run_beads(run_id, bead_id, position, scope_snapshot) VALUES (?, 'p-2', 1, 'Second scope')",
            (self.assignment["run_id"],),
        )
        later = self.controller.store.execute(
            """INSERT INTO assignments(
                   run_id, bead_id, stage, scope_snapshot, worktree_path,
                   created_at, updated_at
               ) VALUES (?, 'p-2', 'queued', 'Second scope', ?, ?, ?)""",
            (self.assignment["run_id"], str(self.worktree), now, now),
        )
        self.assertIsNotNone(position)
        assignment = self.controller.store.row(
            """SELECT a.*, r.project_id FROM assignments a JOIN runs r ON r.id = a.run_id
               WHERE a.id = ?""",
            (later.lastrowid,),
        )
        runtime = FreshConversationRuntime(previous_marker)
        self.controller.runtime = runtime

        await self.controller._ensure_pair(assignment)
        assignment = self.controller.store.row(
            """SELECT a.*, r.project_id FROM assignments a JOIN runs r ON r.id = a.run_id
               WHERE a.id = ?""",
            (later.lastrowid,),
        )
        retained_executor_id = assignment["executor_task_id"]
        current_marker = "CURRENT-ASSIGNMENT-HANDOFF-e7c1"
        implementation = self.controller.store.execute(
            """INSERT INTO actions(
                   task_id, assignment_id, kind, payload, state,
                   outcome_kind, outcome_payload, created_at, updated_at
               ) VALUES (?, ?, 'implement', '{}', 'processed',
                         'ready_for_review', ?, ?, ?)""",
            (
                retained_executor_id,
                assignment["id"],
                json.dumps({"evidence": current_marker}),
                now,
                now,
            ),
        )
        self.controller.store.execute(
            """INSERT INTO handoffs(
                   assignment_id, source_action_id, kind, content_json, created_at
               ) VALUES (?, ?, 'implementation_evidence', ?, ?)""",
            (
                assignment["id"],
                implementation.lastrowid,
                json.dumps({"evidence": current_marker}),
                now,
            ),
        )
        self.controller.store.execute(
            """UPDATE assignments SET stage = 'review_pending',
                   candidate_id = 'candidate-2', source_oid = 'source-2',
                   tested_oid = 'tested-2' WHERE id = ?""",
            (assignment["id"],),
        )
        assignment.update(
            self.controller.store.row(
                "SELECT * FROM assignments WHERE id = ?", (assignment["id"],)
            )
        )
        self.controller.store.execute(
            "UPDATE tasks SET runtime_status = 'unmaterialized' WHERE id = ?",
            (self.overseer["id"],),
        )

        await self.controller._start_assignment_action(assignment)

        retained = self.controller.store.row(
            "SELECT * FROM assignments WHERE id = ?", (later.lastrowid,)
        )
        first_retained = self.controller.store.row(
            "SELECT * FROM assignments WHERE id = ?", (self.assignment["id"],)
        )
        old_tasks = self.controller.store.rows(
            "SELECT * FROM tasks WHERE id IN (?, ?) ORDER BY id",
            (self.executor["id"], self.overseer["id"]),
        )
        archive_targets = {
            row["target"]
            for row in self.controller.store.rows(
                "SELECT * FROM obligations WHERE kind = 'archive' AND state = 'pending'"
            )
        }
        self.assertEqual([task["state"] for task in old_tasks], ["idle", "active"])
        self.assertEqual(archive_targets, set())
        self.assertEqual(first_retained["executor_task_id"], self.executor["id"])
        self.assertEqual(first_retained["overseer_task_id"], self.overseer["id"])
        self.assertEqual(retained["executor_task_id"], self.executor["id"])
        self.assertEqual(retained["overseer_task_id"], self.overseer["id"])
        self.assertEqual(runtime.created, [])
        self.assertEqual(runtime.started[0][0], "overseer")
        second_context = runtime.started[0][1]
        self.assertIn(previous_marker, second_context)
        self.assertIn("Second scope", second_context)
        self.assertIn(f'run id: {assignment["run_id"]}', second_context)
        self.assertIn("role: overseer", second_context)
        self.assertIn(str(self.worktree), second_context)
        self.assertIn("candidate-2", second_context)
        self.assertIn("source-2", second_context)
        self.assertIn("tested-2", second_context)
        self.assertIn(current_marker, second_context)

        await self.controller.reconcile()
        await self.controller.advance()

        archived_tasks = self.controller.store.rows(
            "SELECT state, archived FROM tasks WHERE id IN (?, ?) ORDER BY id",
            (self.executor["id"], self.overseer["id"]),
        )
        completed_obligations = self.controller.store.rows(
            """SELECT target, state FROM obligations
               WHERE kind = 'archive' ORDER BY target"""
        )
        current_run = self.controller.store.row(
            "SELECT * FROM runs WHERE id = ?", (assignment["run_id"],)
        )
        current_assignment = self.controller.store.row(
            "SELECT * FROM assignments WHERE id = ?", (assignment["id"],)
        )
        self.assertEqual(runtime.archived, [])
        self.assertTrue(
            all(
                task["state"] not in {"retired", "archived"} and task["archived"] == 0
                for task in archived_tasks
            )
        )
        self.assertEqual(completed_obligations, [])
        self.assertEqual(current_run["executor_task_id"], retained["executor_task_id"])
        self.assertEqual(current_run["overseer_task_id"], retained["overseer_task_id"])
        self.assertEqual(
            current_assignment["executor_task_id"], retained["executor_task_id"]
        )
        self.assertEqual(
            current_assignment["overseer_task_id"], retained["overseer_task_id"]
        )

    async def test_restart_rechecks_and_reuses_usable_bound_lineage_worker(
        self,
    ) -> None:
        self._attach_current_assignment_to_weaver_lineage()
        executor_id = int(self.executor["id"])
        self._restart_controller()
        runtime = AsyncMock()
        runtime.read_thread.return_value = {
            "id": "executor",
            "status": {"type": "idle"},
            "archived": False,
            "turns": [],
        }
        self.controller.runtime = runtime
        assignment = self.controller.store.row(
            """SELECT a.*, r.project_id FROM assignments a
               JOIN runs r ON r.id = a.run_id WHERE a.id = ?""",
            (self.assignment["id"],),
        )

        await self.controller._ensure_pair(assignment)

        retained = self.controller.store.row(
            "SELECT * FROM assignments WHERE id = ?", (assignment["id"],)
        )
        self.assertEqual(retained["executor_task_id"], executor_id)
        runtime.read_thread.assert_awaited_once_with("executor", include_turns=False)
        runtime.create_thread.assert_not_awaited()

    async def test_bound_missing_lineage_worker_uses_next_stable_suffix(self) -> None:
        self._attach_current_assignment_to_weaver_lineage()
        runtime = AsyncMock()
        runtime.read_thread.side_effect = AppServerError("thread not found")
        runtime.create_thread.return_value = {"thread": {"id": "missing-fallback"}}
        self.controller.runtime = runtime
        assignment = self.controller.store.row(
            """SELECT a.*, r.project_id FROM assignments a
               JOIN runs r ON r.id = a.run_id WHERE a.id = ?""",
            (self.assignment["id"],),
        )

        await self.controller._ensure_pair(assignment)

        retained = self.controller.store.row(
            "SELECT * FROM assignments WHERE id = ?", (assignment["id"],)
        )
        fallback = self.controller.store.row(
            "SELECT * FROM tasks WHERE id = ?", (retained["executor_task_id"],)
        )
        self.assertEqual(fallback["native_thread_id"], "missing-fallback")
        self.assertIn("[EXE0001B]", fallback["title"])
        self.assertEqual(
            self.controller.store.row(
                "SELECT state FROM tasks WHERE id = ?", (self.executor["id"],)
            )["state"],
            "retired",
        )

    async def test_bound_natively_archived_lineage_worker_uses_next_suffix(
        self,
    ) -> None:
        self._attach_current_assignment_to_weaver_lineage()
        runtime = AsyncMock()
        runtime.read_thread.return_value = {
            "id": "executor",
            "status": {"type": "idle"},
            "archived": True,
            "turns": [],
        }
        runtime.create_thread.return_value = {"thread": {"id": "archive-fallback"}}
        self.controller.runtime = runtime
        assignment = self.controller.store.row(
            """SELECT a.*, r.project_id FROM assignments a
               JOIN runs r ON r.id = a.run_id WHERE a.id = ?""",
            (self.assignment["id"],),
        )

        await self.controller._ensure_pair(assignment)

        retained = self.controller.store.row(
            "SELECT * FROM assignments WHERE id = ?", (assignment["id"],)
        )
        fallback = self.controller.store.row(
            "SELECT * FROM tasks WHERE id = ?", (retained["executor_task_id"],)
        )
        self.assertIn("[EXE0001B]", fallback["title"])
        old = self.controller.store.row(
            "SELECT state, archived FROM tasks WHERE id = ?", (self.executor["id"],)
        )
        self.assertEqual((old["state"], old["archived"]), ("archived", 1))

    async def test_bound_incompatibly_active_lineage_worker_uses_next_suffix(
        self,
    ) -> None:
        self._attach_current_assignment_to_weaver_lineage()
        runtime = AsyncMock()
        runtime.read_thread.return_value = {
            "id": "executor",
            "status": {"type": "active"},
            "archived": False,
            "turns": [],
        }
        runtime.create_thread.return_value = {"thread": {"id": "active-fallback"}}
        self.controller.runtime = runtime
        assignment = self.controller.store.row(
            """SELECT a.*, r.project_id FROM assignments a
               JOIN runs r ON r.id = a.run_id WHERE a.id = ?""",
            (self.assignment["id"],),
        )

        await self.controller._ensure_pair(assignment)

        retained = self.controller.store.row(
            "SELECT * FROM assignments WHERE id = ?", (assignment["id"],)
        )
        fallback = self.controller.store.row(
            "SELECT * FROM tasks WHERE id = ?", (retained["executor_task_id"],)
        )
        self.assertIn("[EXE0001B]", fallback["title"])
        self.assertEqual(fallback["native_thread_id"], "active-fallback")

    async def test_lineage_overflow_is_stable_and_different_weavers_are_isolated(
        self,
    ) -> None:
        now = "2026-01-01T00:00:00Z"
        first_weaver = self.controller.store.register_task(
            native_thread_id="first-weaver",
            role="weaver",
            description="First lineage",
            model="sol",
            reasoning_effort="high",
            project_id="p",
        )
        lineage = int(first_weaver["lineage_number"])
        self.controller.store.execute(
            "UPDATE tasks SET lineage_number = ? WHERE id IN (?, ?)",
            (lineage, self.executor["id"], self.overseer["id"]),
        )
        self.controller.store.execute(
            "INSERT INTO bead_lineages(bead_id, weaver_task_id, lineage_number) VALUES ('p-1', ?, ?)",
            (first_weaver["id"], lineage),
        )
        self.controller.store.execute(
            "UPDATE runs SET weaver_task_id = ?, lineage_number = ? WHERE id = ?",
            (first_weaver["id"], lineage, self.assignment["run_id"]),
        )
        self.assertIn("[WVR0001]", first_weaver["title"])
        self.assertIn("[EXE0001]", self.executor["title"])
        self.assertIn("[OVR0001]", self.overseer["title"])
        self.controller.store.execute(
            "UPDATE assignments SET stage = 'completed' WHERE id = ?",
            (self.assignment["id"],),
        )
        self.controller.store.execute(
            "UPDATE runs SET state = 'completed' WHERE id = ?",
            (self.assignment["run_id"],),
        )

        def approve_lineage_bead(
            bead_id: str, weaver: dict[str, Any]
        ) -> dict[str, Any]:
            self.controller.store.execute(
                """INSERT INTO beads(
                       bead_id, intake_key, project_id, title, description, activation,
                       executor_model, executor_reasoning_effort, overseer_model,
                       overseer_reasoning_effort, model_provenance, publication_state,
                       created_at, updated_at
                   ) VALUES (?, ?, 'p', ?, 'Scope', 'pending', 'sol', 'high',
                             'sol', 'high', 'default', 'complete', ?, ?)""",
                (bead_id, f"key-{bead_id}", bead_id, now, now),
            )
            self.controller.store.execute(
                """INSERT INTO bead_lineages(bead_id, weaver_task_id, lineage_number)
                   VALUES (?, ?, ?)""",
                (bead_id, weaver["id"], weaver["lineage_number"]),
            )
            run_id = apply_archon_decisions(
                self.controller.store,
                {
                    "decisions": [
                        {"decision": "approve", "project": "p", "beads": [bead_id]}
                    ]
                },
            )["created_runs"][0]
            return self.controller.store.row(
                """SELECT a.*, r.project_id FROM assignments a
                   JOIN runs r ON r.id = a.run_id WHERE a.run_id = ?""",
                (run_id,),
            )

        class MissingPrimaryRuntime(FreshConversationRuntime):
            async def read_thread(
                self, thread_id: str, *, include_turns: bool = True
            ) -> dict[str, Any]:
                if thread_id == "executor":
                    raise AppServerError("thread not found")
                return await super().read_thread(thread_id, include_turns=include_turns)

        runtime = MissingPrimaryRuntime("")
        self.controller.runtime = runtime  # type: ignore[assignment]
        second_assignment = approve_lineage_bead("p-overflow-b", first_weaver)
        await self.controller._ensure_pair(second_assignment)
        second_assignment = self.controller.store.row(
            "SELECT * FROM assignments WHERE id = ?", (second_assignment["id"],)
        )
        overflow_b = self.controller.store.row(
            "SELECT * FROM tasks WHERE id = ?",
            (second_assignment["executor_task_id"],),
        )
        self.assertEqual(
            self.controller.store.row(
                "SELECT state FROM tasks WHERE id = ?", (self.executor["id"],)
            )["state"],
            "retired",
        )
        self.assertIn("[EXE0001B]", overflow_b["title"])
        self.assertEqual(runtime.created, ["fresh-1"])

        self._restart_controller()
        self.controller.runtime = runtime  # type: ignore[assignment]
        second_assignment = self.controller.store.row(
            """SELECT a.*, r.project_id FROM assignments a
               JOIN runs r ON r.id = a.run_id WHERE a.id = ?""",
            (second_assignment["id"],),
        )
        await self.controller._ensure_pair(second_assignment)
        self.assertEqual(runtime.created, ["fresh-1"])
        self.assertEqual(
            self.controller.store.row(
                "SELECT COUNT(*) AS count FROM tasks WHERE role = 'executor' AND lineage_number = ?",
                (lineage,),
            )["count"],
            2,
        )

        self.controller.store.execute(
            "UPDATE tasks SET runtime_status = 'active' WHERE id = ?",
            (overflow_b["id"],),
        )
        third_assignment = approve_lineage_bead("p-overflow-c", first_weaver)
        await self.controller._ensure_pair(third_assignment)
        overflow_c = self.controller.store.row(
            """SELECT t.* FROM tasks t JOIN assignments a ON a.executor_task_id = t.id
               WHERE a.id = ?""",
            (third_assignment["id"],),
        )
        self.assertIn("[EXE0001C]", overflow_c["title"])

        second_weaver = self.controller.store.register_task(
            native_thread_id="second-weaver",
            role="weaver",
            description="Second lineage",
            model="sol",
            reasoning_effort="high",
            project_id="p",
        )
        isolated_assignment = approve_lineage_bead("p-isolated", second_weaver)
        await self.controller._ensure_pair(isolated_assignment, include_overseer=True)
        isolated = self.controller.store.row(
            """SELECT t.* FROM tasks t JOIN assignments a ON a.executor_task_id = t.id
               WHERE a.id = ?""",
            (isolated_assignment["id"],),
        )
        self.assertIn("[EXE0002]", isolated["title"])
        self.assertEqual(isolated["lineage_number"], second_weaver["lineage_number"])
        isolated_overseer = self.controller.store.row(
            """SELECT t.* FROM tasks t JOIN assignments a ON a.overseer_task_id = t.id
               WHERE a.id = ?""",
            (isolated_assignment["id"],),
        )
        self.assertIn("[OVR0002]", isolated_overseer["title"])
        self.assertEqual(
            isolated_overseer["lineage_number"], second_weaver["lineage_number"]
        )
        self.assertNotIn(
            isolated["id"], {self.executor["id"], overflow_b["id"], overflow_c["id"]}
        )

    async def test_followup_run_reuses_threads_and_cancels_completion_archival(
        self,
    ) -> None:
        now = "2026-01-01T00:00:00Z"
        weaver = self.controller.store.register_task(
            native_thread_id="followup-weaver",
            role="weaver",
            description="Follow-up lineage",
            model="sol",
            reasoning_effort="high",
            project_id="p",
        )
        lineage = int(weaver["lineage_number"])
        self.controller.store.execute(
            "UPDATE tasks SET lineage_number = ? WHERE id IN (?, ?)",
            (lineage, self.executor["id"], self.overseer["id"]),
        )
        self.controller.store.execute(
            "INSERT INTO bead_lineages(bead_id, weaver_task_id, lineage_number) VALUES ('p-1', ?, ?)",
            (weaver["id"], lineage),
        )
        old_run = int(self.assignment["run_id"])
        self.controller.store.execute(
            "UPDATE runs SET state = 'completed', weaver_task_id = ?, lineage_number = ? WHERE id = ?",
            (weaver["id"], lineage, old_run),
        )
        self.controller.store.execute(
            "UPDATE assignments SET stage = 'completed' WHERE id = ?",
            (self.assignment["id"],),
        )
        for task in (self.executor, self.overseer):
            self.controller.store.execute(
                """INSERT INTO obligations(
                       kind, identity, target, state, created_at, updated_at
                   ) VALUES ('archive', ?, ?, 'pending', ?, ?)""",
                (str(task["id"]), task["native_thread_id"], now, now),
            )
        self.controller.store.execute(
            """INSERT INTO beads(
                   bead_id, intake_key, project_id, title, description, activation,
                   executor_model, executor_reasoning_effort, overseer_model,
                   overseer_reasoning_effort, model_provenance, publication_state,
                   created_at, updated_at
               ) VALUES ('p-followup', 'followup-key', 'p', 'Follow up', 'Scope',
                         'pending', 'sol', 'high', 'sol', 'high', 'default',
                         'complete', ?, ?)""",
            (now, now),
        )
        self.controller.store.execute(
            """INSERT INTO bead_lineages(bead_id, weaver_task_id, lineage_number)
               VALUES ('p-followup', ?, ?)""",
            (weaver["id"], lineage),
        )
        new_run = apply_archon_decisions(
            self.controller.store,
            {
                "decisions": [
                    {
                        "decision": "approve",
                        "project": "p",
                        "beads": ["p-followup"],
                    }
                ]
            },
        )["created_runs"][0]
        followup = self.controller.store.row(
            """SELECT a.*, r.project_id FROM assignments a
               JOIN runs r ON r.id = a.run_id WHERE a.run_id = ?""",
            (new_run,),
        )

        runtime = FreshConversationRuntime("")
        self.controller.runtime = runtime  # type: ignore[assignment]
        await self.controller._ensure_pair(followup, include_overseer=True)

        retained = self.controller.store.row(
            "SELECT * FROM assignments WHERE id = ?", (followup["id"],)
        )
        self.assertEqual(retained["executor_task_id"], self.executor["id"])
        self.assertEqual(retained["overseer_task_id"], self.overseer["id"])
        self.assertEqual(runtime.created, [])
        old = self.controller.store.row("SELECT * FROM runs WHERE id = ?", (old_run,))
        self.assertIsNone(old["executor_task_id"])
        self.assertIsNone(old["overseer_task_id"])
        self.assertEqual(
            {
                row["state"]
                for row in self.controller.store.rows(
                    "SELECT state FROM obligations WHERE kind = 'archive'"
                )
            },
            {"canceled"},
        )
        self.assertEqual(invariant_violations(self.controller.store), [])

        self.controller.store.execute(
            """INSERT INTO beads(
                   bead_id, intake_key, project_id, title, description, activation,
                   executor_model, executor_reasoning_effort, overseer_model,
                   overseer_reasoning_effort, model_provenance, publication_state,
                   created_at, updated_at
               ) VALUES ('p-overflow-final', 'overflow-final-key', 'p',
                         'Concurrent overflow', 'Scope', 'pending', 'sol', 'high',
                         'sol', 'high', 'default', 'complete', ?, ?)""",
            (now, now),
        )
        self.controller.store.execute(
            """INSERT INTO bead_lineages(bead_id, weaver_task_id, lineage_number)
               VALUES ('p-overflow-final', ?, ?)""",
            (weaver["id"], lineage),
        )
        overflow_run = apply_archon_decisions(
            self.controller.store,
            {
                "decisions": [
                    {
                        "decision": "approve",
                        "project": "p",
                        "beads": ["p-overflow-final"],
                    }
                ]
            },
        )["created_runs"][0]
        overflow_assignment = self.controller.store.row(
            """SELECT a.*, r.project_id FROM assignments a
               JOIN runs r ON r.id = a.run_id WHERE a.run_id = ?""",
            (overflow_run,),
        )
        await self.controller._ensure_pair(overflow_assignment, include_overseer=True)
        overflow_assignment = self.controller.store.row(
            "SELECT * FROM assignments WHERE id = ?", (overflow_assignment["id"],)
        )
        overflow_executor = self.controller.store.row(
            "SELECT * FROM tasks WHERE id = ?",
            (overflow_assignment["executor_task_id"],),
        )
        overflow_overseer = self.controller.store.row(
            "SELECT * FROM tasks WHERE id = ?",
            (overflow_assignment["overseer_task_id"],),
        )
        self.assertIn("[EXE0001B]", overflow_executor["title"])
        self.assertIn("[OVR0001B]", overflow_overseer["title"])

        self.controller.store.execute(
            "UPDATE assignments SET stage = 'completed' WHERE id = ?",
            (followup["id"],),
        )
        self.controller.store.execute(
            "UPDATE runs SET state = 'completed' WHERE id = ?", (new_run,)
        )
        schedule_run_archival(self.controller.store.connection, new_run, now)
        self.assertEqual(
            {
                row["state"]
                for row in self.controller.store.rows(
                    "SELECT state FROM obligations WHERE kind = 'archive'"
                )
            },
            {"canceled"},
        )
        self.controller.store.execute(
            "UPDATE assignments SET stage = 'completed' WHERE id = ?",
            (overflow_assignment["id"],),
        )
        self.controller.store.execute(
            "UPDATE runs SET state = 'completed' WHERE id = ?", (overflow_run,)
        )
        schedule_run_archival(self.controller.store.connection, overflow_run, now)
        lineage_tasks = self.controller.store.rows(
            """SELECT * FROM tasks WHERE lineage_number = ?
               AND role IN ('weaver','executor','overseer')
               AND state NOT IN ('retired','archived') ORDER BY id""",
            (lineage,),
        )
        self.assertEqual(len(lineage_tasks), 5)
        self.assertEqual(
            {
                row["state"]
                for row in self.controller.store.rows(
                    "SELECT state FROM obligations WHERE kind = 'archive'"
                )
            },
            {"pending"},
        )
        self.assertEqual(
            self.controller.store.row(
                "SELECT COUNT(*) AS count FROM obligations WHERE kind = 'archive'"
            )["count"],
            5,
        )

        archive_runtime = ControlledArchiveRuntime(
            [str(task["native_thread_id"]) for task in lineage_tasks]
        )
        self.controller.runtime = archive_runtime  # type: ignore[assignment]
        with patch("fulcrum.controller.utc_now", return_value=now):
            await self.controller._archive_ready_tasks()
        self.assertEqual(archive_runtime.archived, [])
        with patch("fulcrum.controller.utc_now", return_value="2026-01-01T00:10:00Z"):
            await self.controller._archive_ready_tasks()
        self.assertCountEqual(
            archive_runtime.archived,
            [str(task["native_thread_id"]) for task in lineage_tasks],
        )

    async def test_all_completion_roles_archive_only_after_ten_idle_minutes(
        self,
    ) -> None:
        tasks = [self.executor, self.overseer]
        for role in ("weaver", "sage", "inquisitor"):
            tasks.append(
                self.controller.store.register_task(
                    native_thread_id=role,
                    role=role,
                    description=f"{role} completion",
                    model="sol",
                    reasoning_effort="high",
                    project_id="p",
                )
            )
        created_at = "2026-01-01T00:00:00Z"
        for task in tasks:
            self.controller.store.execute(
                """INSERT INTO obligations(
                       kind, identity, target, state, created_at, updated_at
                   ) VALUES ('archive', ?, ?, 'pending', ?, ?)""",
                (
                    f"completion:{task['id']}",
                    task["native_thread_id"],
                    created_at,
                    created_at,
                ),
            )
        runtime = ControlledArchiveRuntime(
            [str(task["native_thread_id"]) for task in tasks]
        )
        self.controller.runtime = runtime  # type: ignore[assignment]

        with patch("fulcrum.controller.utc_now", return_value=created_at):
            await self.controller._archive_ready_tasks()
        self.assertEqual(runtime.archived, [])
        self.assertEqual(
            {
                row["archive_eligible_at"]
                for row in self.controller.store.rows(
                    "SELECT archive_eligible_at FROM tasks WHERE id IN (?, ?, ?, ?, ?)",
                    tuple(task["id"] for task in tasks),
                )
            },
            {"2026-01-01T00:10:00Z"},
        )

        with patch("fulcrum.controller.utc_now", return_value="2026-01-01T00:09:59Z"):
            await self.controller._archive_ready_tasks()
        self.assertEqual(runtime.archived, [])

        with patch("fulcrum.controller.utc_now", return_value="2026-01-01T00:10:00Z"):
            await self.controller._archive_ready_tasks()
        self.assertCountEqual(
            runtime.archived,
            [str(task["native_thread_id"]) for task in tasks],
        )

    async def test_archive_idle_interval_restarts_after_activity(self) -> None:
        created_at = "2026-01-01T00:00:00Z"
        self.controller.store.execute(
            """INSERT INTO obligations(
                   kind, identity, target, state, created_at, updated_at
               ) VALUES ('archive', 'activity', 'executor', 'pending', ?, ?)""",
            (created_at, created_at),
        )
        runtime = ControlledArchiveRuntime(["executor"])
        self.controller.runtime = runtime  # type: ignore[assignment]

        with patch("fulcrum.controller.utc_now", return_value=created_at):
            await self.controller._archive_ready_tasks()
        runtime.statuses["executor"] = "active"
        with patch("fulcrum.controller.utc_now", return_value="2026-01-01T00:05:00Z"):
            await self.controller._archive_ready_tasks()
        self.assertIsNone(
            self.controller.store.row(
                "SELECT archive_eligible_at FROM tasks WHERE id = ?",
                (self.executor["id"],),
            )["archive_eligible_at"]
        )

        runtime.statuses["executor"] = "idle"
        with patch("fulcrum.controller.utc_now", return_value="2026-01-01T00:07:00Z"):
            await self.controller._archive_ready_tasks()
        self.assertEqual(
            self.controller.store.row(
                "SELECT archive_eligible_at FROM tasks WHERE id = ?",
                (self.executor["id"],),
            )["archive_eligible_at"],
            "2026-01-01T00:17:00Z",
        )

        runtime.turn_ids["executor"] = "executor-later-turn"
        with patch("fulcrum.controller.utc_now", return_value="2026-01-01T00:12:00Z"):
            await self.controller._archive_ready_tasks()
        self.assertEqual(
            self.controller.store.row(
                "SELECT archive_eligible_at FROM tasks WHERE id = ?",
                (self.executor["id"],),
            )["archive_eligible_at"],
            "2026-01-01T00:22:00Z",
        )

        with patch("fulcrum.controller.utc_now", return_value="2026-01-01T00:21:59Z"):
            await self.controller._archive_ready_tasks()
        self.assertEqual(runtime.archived, [])
        with patch("fulcrum.controller.utc_now", return_value="2026-01-01T00:22:00Z"):
            await self.controller._archive_ready_tasks()
        self.assertEqual(runtime.archived, ["executor"])

    async def test_archive_deadline_survives_restart_and_reconciliation(self) -> None:
        weaver = self.controller.store.register_task(
            native_thread_id="weaver-restart",
            role="weaver",
            description="Restarted intake",
            model="sol",
            reasoning_effort="high",
            project_id="p",
        )
        created_at = "2026-01-01T00:00:00Z"
        self.controller.store.execute(
            """INSERT INTO obligations(
                   kind, identity, target, state, created_at, updated_at
               ) VALUES ('archive', 'restart', 'weaver-restart', 'pending', ?, ?)""",
            (created_at, created_at),
        )
        runtime = ControlledArchiveRuntime(["weaver-restart"])
        self.controller.runtime = runtime  # type: ignore[assignment]
        with patch("fulcrum.controller.utc_now", return_value=created_at):
            await self.controller._archive_ready_tasks()

        self._restart_controller()
        self.controller.runtime = runtime  # type: ignore[assignment]
        with patch("fulcrum.controller.utc_now", return_value="2026-01-01T00:10:00Z"):
            await self.controller.reconcile()
            await self.controller._archive_ready_tasks()

        self.assertEqual(runtime.archived, ["weaver-restart"])
        self.assertEqual(
            self.controller.store.row(
                "SELECT state, archived, archive_eligible_at FROM tasks WHERE id = ?",
                (weaver["id"],),
            ),
            {"state": "archived", "archived": 1, "archive_eligible_at": None},
        )

    async def test_eligible_archive_failure_retains_retry_behavior(self) -> None:
        created_at = "2026-01-01T00:00:00Z"
        self.controller.store.execute(
            """INSERT INTO obligations(
                   kind, identity, target, state, created_at, updated_at
               ) VALUES ('archive', 'retry', 'executor', 'pending', ?, ?)""",
            (created_at, created_at),
        )
        runtime = ControlledArchiveRuntime(["executor"])
        runtime.failures_remaining["executor"] = 1
        self.controller.runtime = runtime  # type: ignore[assignment]
        with patch("fulcrum.controller.utc_now", return_value=created_at):
            await self.controller._archive_ready_tasks()
        with patch("fulcrum.controller.utc_now", return_value="2026-01-01T00:10:00Z"):
            await self.controller._archive_ready_tasks()

        failed = self.controller.store.row(
            "SELECT * FROM obligations WHERE identity = 'retry'"
        )
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["retry_count"], 1)
        self.assertIsNotNone(failed["next_attempt_at"])
        self.assertEqual(runtime.archive_attempts, ["executor"])

        with patch(
            "fulcrum.controller.utc_now", return_value=failed["next_attempt_at"]
        ):
            await self.controller._archive_ready_tasks()
        self.assertEqual(runtime.archive_attempts, ["executor", "executor"])
        self.assertEqual(runtime.archived, ["executor"])
        self.assertEqual(
            self.controller.store.row(
                "SELECT state FROM obligations WHERE identity = 'retry'"
            )["state"],
            "complete",
        )

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
        runtime = AsyncMock()
        runtime.read_thread.return_value = {
            "id": "lazy-executor",
            "status": {"type": "idle"},
            "archived": False,
            "turns": [],
        }
        self.controller.runtime = runtime
        provision = AsyncMock(side_effect=[executor, overseer])
        with patch.object(self.controller, "_provision_task", new=provision):
            await self.controller._ensure_pair(assignment)
            retained = self.controller.store.row(
                "SELECT * FROM assignments WHERE id = ?", (assignment["id"],)
            )
            self.assertEqual(retained["executor_task_id"], executor["id"])
            self.assertIsNone(retained["overseer_task_id"])
            provision.assert_awaited_once()
            self.assertNotIn("cwd", provision.await_args.kwargs)

            assignment.update(retained)
            await self.controller._ensure_pair(assignment, include_overseer=True)
        retained = self.controller.store.row(
            "SELECT * FROM assignments WHERE id = ?", (assignment["id"],)
        )
        self.assertEqual(retained["overseer_task_id"], overseer["id"])
        self.assertEqual(provision.await_count, 2)
        self.assertEqual(provision.await_args.kwargs["role"], "overseer")
        self.assertNotIn("cwd", provision.await_args.kwargs)

    async def test_worker_thread_is_created_at_the_canonical_project_root(
        self,
    ) -> None:
        runtime = AsyncMock()
        runtime.create_thread.return_value = {
            "thread": {
                "id": "canonical-executor",
                "projectId": "codex-p",
            }
        }
        self.controller.runtime = runtime
        project = self.controller.store.row(
            "SELECT * FROM projects WHERE project_id = 'p'"
        )

        await self.controller._provision_task(
            role="executor",
            description="Canonical creation context",
            project=project,
            model="sol",
            effort="high",
        )

        runtime.create_thread.assert_awaited_once_with(
            cwd=self.config.source_root,
            workspace_root=self.config.source_root,
            model="sol",
            project_id="codex-p",
        )
        operation = self.controller.store.row(
            """SELECT input_json FROM external_operations
               WHERE kind = 'thread_start' AND native_id = 'canonical-executor'"""
        )
        self.assertEqual(
            json.loads(operation["input_json"])["cwd"], self.config.source_root
        )

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
        self.assertIn("You are Overseer", prompt)
        self.assertIn("# Current action", prompt)
        self.assertNotIn("--section", prompt)
        self.assertEqual(start.await_args.kwargs["cwd"], self.config.source_root)
        self.assertIsNone(start.await_args.kwargs["developer_instructions"])

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
        now = utc_now()
        full_scope = (
            "Show an empty state for zero matches; verify matching results remain correct. "
            + ("authoritative detail " * 30)
            + "SCOPE-TAIL"
        )
        self.controller.store.execute(
            """INSERT INTO beads VALUES (
                   'p-2','proposal-p-2','p','Fix empty results',?,
                   'pending','sol','high','sol','high','default',NULL,NULL,
                   '["conflict:search-ui"]','complete',?,?
               )""",
            (
                full_scope,
                now,
                now,
            ),
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
        self.assertIn("p-2 (p) — Fix empty results", text)
        self.assertIn(
            "Show an empty state for zero matches; verify matching results remain correct.",
            text,
        )
        self.assertIn("Retained scope: scope:", text)
        self.assertNotIn("SCOPE-TAIL", text)
        self.assertNotIn('"scope":', text)
        self.assertIn("Capacity used:", text)
        self.assertIn("Existing p-1", text)
        self.assertNotIn("fulcrum instructions", text)
        self.assertIn("You are Archon", text)
        self.assertIn("# Current action", text)
        self.assertNotIn("Exact JSON", text)
        self.assertLess(len(text.split()), 500)
        action = self.controller.store.row(
            "SELECT * FROM actions WHERE task_id = ? AND kind = 'archon'",
            (
                self.controller.store.row(
                    "SELECT id FROM tasks WHERE native_thread_id = 'archon-inline'"
                )["id"],
            ),
        )
        payload = json.loads(action["payload"])
        reference = payload["batch_items"][0]["content"]["scope_reference"]
        retained = self.controller.store.row(
            "SELECT * FROM scope_references WHERE identity = ?", (reference,)
        )
        self.assertEqual(retained["scope_snapshot"], full_scope)
        self.assertNotIn("SCOPE-TAIL", action["payload"])

    async def test_completion_only_update_is_acknowledged_without_archon_turn(
        self,
    ) -> None:
        archon = self.controller.store.register_task(
            native_thread_id="archon-auto-completion",
            role="archon",
            description="Fleet",
            model="sol",
            reasoning_effort="high",
            project_id="p",
            state="idle",
        )
        self.controller._queue_archon_update(
            "completion:auto",
            {
                "kind": "assignment_completed",
                "assignment_id": self.assignment["id"],
                "bead_id": "p-1",
                "run_id": self.assignment["run_id"],
                "action_id": 30,
                "minor_fixes": [],
                "cost": {"action": {"attributed_display": "$3.13"}},
            },
        )
        update = self.controller.store.row(
            "SELECT * FROM updates WHERE identity = 'completion:auto'"
        )
        runtime = AsyncMock()
        runtime.ready = True
        self.controller.runtime = runtime

        await self.controller._deliver_update_batch()
        await self.controller._deliver_update_batch()

        runtime.start_turn.assert_not_awaited()
        self.assertEqual(
            self.controller.store.row(
                "SELECT state FROM updates WHERE id = ?", (update["id"],)
            )["state"],
            "processed",
        )
        events = self.controller.store.rows(
            """SELECT * FROM events WHERE kind = 'completion_acknowledged'
               AND entity_id = ?""",
            (update["id"],),
        )
        self.assertEqual(len(events), 1)
        self.assertIn("p-1 completed", events[0]["message"])
        self.assertIn("$3.13", events[0]["message"])
        self.assertEqual(events[0]["detail_json"].count("automatic"), 1)

    async def test_judgment_completion_dispatches_both_paths_in_action_and_context(
        self,
    ) -> None:
        archon = self.controller.store.register_task(
            native_thread_id="archon-judgment-completion",
            role="archon",
            description="Fleet",
            model="sol",
            reasoning_effort="high",
            project_id="p",
            state="idle",
        )
        self.controller._queue_archon_update(
            "completion:required-decision",
            {
                "kind": "assignment_completed",
                "assignment_id": self.assignment["id"],
                "bead_id": "p-1",
                "run_id": self.assignment["run_id"],
                "required_decision": "Choose the recovery route",
            },
        )
        self.controller._queue_archon_update(
            "completion:requires-decision",
            {
                "kind": "specialist_completed",
                "specialist": "sage",
                "occurrence_id": "judgment",
                "summary": "Human judgment remains",
                "finding_count": 1,
                "requires_decision": True,
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
                new=AsyncMock(return_value="archon-judgment-turn"),
            ) as start,
        ):
            await self.controller._deliver_update_batch()

        action = self.controller.store.row(
            "SELECT * FROM actions WHERE task_id = ? AND kind = 'archon'",
            (archon["id"],),
        )
        assert action is not None
        paths = self.controller._handoff_destinations(action)
        initial = start.call_args.args[1]
        self.assertIn("finish with decisions or deferred", initial)
        self.assertEqual(initial.count(paths["decisions"]), 2)
        self.assertEqual(initial.count(paths["deferred"]), 2)

        reminder = self.controller._build_action_message(
            {**action, "reminder_sent": 1}, archon, None
        )
        context = await self.controller.handle_request(
            {"command": "context", "thread_id": archon["native_thread_id"]}
        )
        for rendered in (reminder, context["context"]):
            self.assertEqual(rendered.count(paths["decisions"]), 2)
            self.assertEqual(rendered.count(paths["deferred"]), 2)

    async def test_archon_batch_and_prompt_size_are_bounded(self) -> None:
        archon = self.controller.store.register_task(
            native_thread_id="archon-bounded-batch",
            role="archon",
            description="Fleet",
            model="sol",
            reasoning_effort="high",
            project_id="p",
            state="idle",
        )
        for number in range(25):
            self.controller._queue_archon_update(
                f"exception:{number}",
                {
                    "kind": "new_exception",
                    "project": "p",
                    "decision_needed": f"resolve {number}",
                    "evidence": "large evidence " * 1000,
                },
            )
        runtime = AsyncMock()
        runtime.ready = True
        runtime.start_turn.return_value = "bounded-turn"
        self.controller.runtime = runtime
        with patch.object(
            self.controller,
            "_refresh_task",
            new=AsyncMock(
                return_value={
                    "can_start": True,
                    "last_turn_id": None,
                    "runtime_status": "idle",
                }
            ),
        ):
            await self.controller._deliver_update_batch()

        prompt = runtime.start_turn.await_args.args[1]
        states = {
            row["state"]: row["count"]
            for row in self.controller.store.rows(
                "SELECT state, COUNT(*) AS count FROM updates GROUP BY state"
            )
        }
        self.assertEqual(states, {"batched": 20, "retained": 5})
        self.assertLess(len(prompt), 20_000)
        self.assertIn("Update IDs:", prompt)
        self.assertEqual(
            len(
                self.controller.store.rows(
                    "SELECT * FROM batch_updates WHERE batch_id = (SELECT MAX(id) FROM batches)"
                )
            ),
            20,
        )

    async def test_assignment_recovery_prompt_bounds_untrusted_exception(self) -> None:
        self.controller.store.register_task(
            native_thread_id="archon-bounded-recovery",
            role="archon",
            description="Fleet",
            model="sol",
            reasoning_effort="high",
            project_id="p",
            state="idle",
        )
        condition = "recovery failed: " + ("unbounded-runtime-error " * 20_000)
        self.controller._queue_archon_update(
            "recovery:bounded",
            {
                "kind": "assignment_recovery",
                "assignment_id": self.assignment["id"],
                "run_id": self.assignment["run_id"],
                "bead_id": "p-1",
                "attempt": 2,
                "condition": condition,
                "next_attempt_at": "2026-01-01T00:00:00Z",
            },
        )
        runtime = AsyncMock()
        runtime.ready = True
        runtime.start_turn.return_value = "bounded-recovery-turn"
        self.controller.runtime = runtime
        with patch.object(
            self.controller,
            "_refresh_task",
            new=AsyncMock(
                return_value={
                    "can_start": True,
                    "last_turn_id": None,
                    "runtime_status": "idle",
                }
            ),
        ):
            await self.controller._deliver_update_batch()

        prompt = runtime.start_turn.await_args.args[1]
        self.assertLess(len(prompt), 5_000)
        self.assertIn(
            f"Assignment {self.assignment['id']} (p-1, run {self.assignment['run_id']}) needs recovery",
            prompt,
        )
        self.assertIn("recovery failed:", prompt)
        self.assertIn("…", prompt)
        self.assertNotIn("unbounded-runtime-error " * 20, prompt)
        self.assertIn("automatic retry scheduled", prompt)

    async def test_combined_maximum_archon_state_is_budgeted_and_dispatched(
        self,
    ) -> None:
        self.controller.store.register_task(
            native_thread_id="archon-combined-maximum",
            role="archon",
            description="Fleet",
            model="sol",
            reasoning_effort="high",
            project_id="p",
            state="idle",
        )
        now = "2026-01-01T00:00:00Z"
        large = "decision-relevant-controller-state " * 200
        for number in range(20):
            bead_id = f"p-max-proposal-{number:02d}"
            self.controller.store.execute(
                """INSERT INTO beads(
                       bead_id, intake_key, project_id, title, description,
                       activation, executor_model, executor_reasoning_effort,
                       overseer_model, overseer_reasoning_effort,
                       model_provenance, context_json, publication_state,
                       created_at, updated_at
                   ) VALUES (?, ?, 'p', ?, ?, 'pending', 'sol', 'high',
                             'sol', 'high', 'default', ?, 'complete', ?, ?)""",
                (
                    bead_id,
                    f"max-proposal-{number:02d}",
                    f"Maximum proposal {number:02d} {large}",
                    f"Authoritative retained scope {number:02d} {large}",
                    json.dumps([f"conflict:proposal-{number:02d}-{large}"]),
                    now,
                    now,
                ),
            )
            self.controller._queue_archon_update(
                f"proposal:{bead_id}",
                {
                    "kind": "proposal",
                    "bead_id": bead_id,
                    "project": "p",
                    "title": f"Maximum proposal {number:02d} {large}",
                    "scope": f"Authoritative retained scope {number:02d} {large}",
                    "dependencies": [f"dependency-{number:02d}-{large}"],
                    "context": [f"conflict:proposal-{number:02d}-{large}"],
                },
            )

        # The fixture already supplies one relevant unfinished assignment.
        for number in range(19):
            bead_id = f"p-max-active-{number:02d}"
            self.controller.store.execute(
                """INSERT INTO beads(
                       bead_id, intake_key, project_id, title, description,
                       activation, executor_model, executor_reasoning_effort,
                       overseer_model, overseer_reasoning_effort,
                       model_provenance, context_json, publication_state,
                       created_at, updated_at
                   ) VALUES (?, ?, 'p', ?, ?, 'pending', 'sol', 'high',
                             'sol', 'high', 'default', ?, 'complete', ?, ?)""",
                (
                    bead_id,
                    f"max-active-{number:02d}",
                    f"Maximum active work {number:02d}",
                    f"Active scope {number:02d}",
                    json.dumps([{"conflict_key": f"active-{number:02d}-{large}"}]),
                    now,
                    now,
                ),
            )
            run = self.controller.store.execute(
                """INSERT INTO runs(
                       project_id, authority, state, priority, created_at, updated_at
                   ) VALUES ('p', ?, 'active', ?, ?, ?)""",
                (f"maximum-state-{number:02d}", number, now, now),
            )
            self.controller.store.execute(
                """INSERT INTO assignments(
                       run_id, bead_id, stage, scope_snapshot, condition,
                       created_at, updated_at
                   ) VALUES (?, ?, 'implementing', ?, ?, ?, ?)""",
                (
                    run.lastrowid,
                    bead_id,
                    f"Active scope {number:02d}",
                    f"Active condition {number:02d} {large}",
                    now,
                    now,
                ),
            )

        for number in range(20):
            self.controller.store.execute(
                """INSERT INTO holds(
                       scope, target, reason, urgent, release_condition, created_at
                   ) VALUES ('project', 'p', ?, ?, ?, ?)""",
                (
                    f"Hold reason {number:02d} {large}",
                    number % 2,
                    f"Release condition {number:02d} {large}",
                    now,
                ),
            )
            self.controller.store.execute(
                """INSERT INTO external_operations(
                       kind, target, input_json, state, reconciliation_used,
                       condition, correlation_id, created_at, updated_at
                   ) VALUES ('maximum_test', ?, '{}', 'uncertain', 1, ?, ?, ?, ?)""",
                (
                    f"target-{number:02d}-{large}",
                    f"Uncertain condition {number:02d} {large}",
                    f"maximum-operation-{number:02d}",
                    now,
                    now,
                ),
            )

        runtime = AsyncMock()
        runtime.ready = True
        runtime.start_turn.return_value = "combined-maximum-turn"
        self.controller.runtime = runtime
        with patch.object(
            self.controller,
            "_refresh_task",
            new=AsyncMock(
                return_value={
                    "can_start": True,
                    "last_turn_id": None,
                    "runtime_status": "idle",
                }
            ),
        ):
            await self.controller._deliver_update_batch()

        prompt = runtime.start_turn.await_args.args[1]
        self.assertLessEqual(len(prompt), 32_000)
        action = self.controller.store.row(
            "SELECT * FROM actions WHERE kind = 'archon' ORDER BY id DESC LIMIT 1"
        )
        payload = json.loads(action["payload"])
        self.assertEqual(len(payload["batch_items"]), 20)
        for item in payload["batch_items"]:
            content = item["content"]
            self.assertIn(f"Update {item['update_id']}:", prompt)
            self.assertIn(content["bead_id"], prompt)
            self.assertIn(content["scope_reference"], prompt)
        self.assertIn("Required handled_update_ids:", prompt)
        self.assertIn("Approve only with the listed", prompt)
        self.assertIn("Finish decisions:", prompt)
        self.assertIn("Defer the whole batch only when it must wait", prompt)

        for label, total in (
            ("Active work", 20),
            ("Holds", 20),
            ("External operations", 20),
        ):
            match = re.search(
                rf"{label}: showing (\d+)/{total}; (\d+) additional", prompt
            )
            self.assertIsNotNone(match, label)
            shown, retained = (int(value) for value in match.groups())
            self.assertGreater(shown, 0, label)
            self.assertEqual(shown + retained, total, label)

    async def test_mandatory_escalation_budget_splits_exact_ordered_batches(
        self,
    ) -> None:
        archon = self.controller.store.register_task(
            native_thread_id="archon-mandatory-escalations",
            role="archon",
            description="Fleet",
            model="sol",
            reasoning_effort="high",
            project_id="p",
            state="idle",
        )
        large = "bounded-current-review-evidence " * 200
        for number in range(20):
            self.controller._queue_archon_update(
                f"review-escalation:maximum:{number:02d}",
                {
                    "kind": "review_failure_escalation",
                    "assignment_id": 1_000 + number,
                    "bead_id": f"p-escalation-{number:02d}",
                    "project": "p",
                    "review_failures": 3,
                    "condition": f"Third review failed {number:02d}: {large}",
                    "candidate_revision": {
                        "candidate_id": f"candidate-{number:02d}",
                        "source": f"source-{number:02d}-{large}",
                    },
                    "unresolved_findings": [
                        {
                            "problem": f"current problem {number:02d} {large}",
                            "required_change": f"current repair {number:02d} {large}",
                        }
                    ],
                    "prior_review_history": [
                        {
                            "classification": "history; resolution not inferred",
                            "findings": f"prior context {number:02d} {large}",
                        }
                    ],
                    "changes_since_prior_attempt": {
                        "evidence": f"candidate delta {number:02d} {large}"
                    },
                    "reviewer_recommendation": {
                        "resolution": "retry",
                        "reason": f"bounded recommendation {number:02d} {large}",
                    },
                    "action_id": 2_000 + number,
                    "review_action": "changes_requested",
                    "hold_id": 3_000 + number,
                    "required_decision": "retry, rescope, complete_non_code, or cancel",
                    "resolutions": [
                        "retry",
                        "rescope",
                        "complete_non_code",
                        "cancel",
                    ],
                },
            )

        original_ids = [
            row["id"]
            for row in self.controller.store.rows(
                "SELECT id FROM updates WHERE recipient_task_id = ? ORDER BY id",
                (archon["id"],),
            )
        ]
        runtime = AsyncMock()
        runtime.ready = True
        runtime.start_turn.side_effect = [
            f"mandatory-escalation-turn-{number}" for number in range(20)
        ]
        self.controller.runtime = runtime
        seen: list[int] = []
        batch_sizes: list[int] = []
        with patch.object(
            self.controller,
            "_refresh_task",
            new=AsyncMock(
                return_value={
                    "can_start": True,
                    "last_turn_id": None,
                    "runtime_status": "idle",
                }
            ),
        ):
            while len(seen) < len(original_ids):
                await self.controller._deliver_update_batch()
                action = self.controller.store.row(
                    """SELECT * FROM actions WHERE task_id = ? AND kind = 'archon'
                       ORDER BY id DESC LIMIT 1""",
                    (archon["id"],),
                )
                payload = json.loads(action["payload"])
                batch_ids = [item["update_id"] for item in payload["batch_items"]]
                self.assertEqual(
                    batch_ids,
                    original_ids[len(seen) : len(seen) + len(batch_ids)],
                )
                self.assertTrue(set(batch_ids).isdisjoint(seen))
                batch_sizes.append(len(batch_ids))
                seen.extend(batch_ids)

                prompt = runtime.start_turn.await_args.args[1]
                self.assertLessEqual(len(prompt), 32_000)
                for update_id in batch_ids:
                    self.assertIn(f"Update {update_id}:", prompt)
                for category in (
                    "candidate revision:",
                    "unresolved findings:",
                    "prior review history:",
                    "changes since prior attempt:",
                    "reviewer recommendation:",
                ):
                    self.assertEqual(prompt.count(category), len(batch_ids), category)
                self.assertEqual(
                    prompt.count("requires resolve_escalation"), len(batch_ids)
                )
                states = {
                    row["state"]: row["count"]
                    for row in self.controller.store.rows(
                        """SELECT state, COUNT(*) AS count FROM updates
                           WHERE recipient_task_id = ? GROUP BY state""",
                        (archon["id"],),
                    )
                }
                self.assertEqual(states.get("batched"), len(batch_ids))
                self.assertEqual(states.get("retained", 0), 20 - len(seen))

                self.controller.store.execute(
                    "UPDATE updates SET state = 'processed' WHERE state = 'batched'"
                )
                self.controller.store.execute(
                    "UPDATE batches SET state = 'processed' WHERE action_id = ?",
                    (action["id"],),
                )
                self.controller.store.execute(
                    "UPDATE actions SET state = 'processed' WHERE id = ?",
                    (action["id"],),
                )
                self.controller.store.execute(
                    """UPDATE tasks SET state = 'idle', runtime_status = 'idle',
                       last_turn_terminal = 1 WHERE id = ?""",
                    (archon["id"],),
                )

        self.assertGreater(len(batch_sizes), 1)
        self.assertLess(batch_sizes[0], 20)
        self.assertEqual(seen, original_ids)
        self.assertEqual(runtime.start_turn.await_count, len(batch_sizes))

    async def test_completion_is_finalized_before_unrelated_archon_batch_once(
        self,
    ) -> None:
        archon = self.controller.store.register_task(
            native_thread_id="archon-cost-mixed",
            role="archon",
            description="Fleet",
            model="gpt-5.6-sol",
            reasoning_effort="high",
            project_id="p",
            state="idle",
        )
        now = "2026-01-01T00:00:00Z"
        completed_action = int(
            self.controller.store.execute(
                """INSERT INTO actions(task_id, assignment_id, kind, payload,
                       state, created_at, updated_at)
                   VALUES (?, ?, 'correct', '{}', 'processed', ?, ?)""",
                (self.executor["id"], self.assignment["id"], now, now),
            ).lastrowid
        )
        concurrent_action = int(
            self.controller.store.execute(
                """INSERT INTO actions(task_id, kind, payload, state,
                       created_at, updated_at)
                   VALUES (?, 'review', '{}', 'processed', ?, ?)""",
                (self.overseer["id"], now, now),
            ).lastrowid
        )
        self.controller.store.link_bead_to_workflow("workflow-complete", "p-1")
        self.controller.store.link_action_to_workflow(
            "workflow-complete", completed_action, causal_role="executor"
        )
        self.controller.store.link_action_to_workflow(
            "workflow-concurrent", concurrent_action, causal_role="overseer"
        )
        self.controller.store.record_tool_cost(
            source_key="completed-cost",
            tool_name="test",
            quantity=1,
            unit="call",
            unit_rate="0.01",
            source_url="https://developers.openai.com/api/docs/pricing",
            action_id=completed_action,
        )
        self.controller.store.record_tool_cost(
            source_key="concurrent-cost",
            tool_name="test",
            quantity=1,
            unit="call",
            unit_rate="0.02",
            source_url="https://developers.openai.com/api/docs/pricing",
            action_id=concurrent_action,
        )
        self.controller.store.execute(
            "UPDATE assignments SET stage = 'completed' WHERE id = ?",
            (self.assignment["id"],),
        )
        self.controller.store.execute(
            """INSERT INTO beads VALUES (
                   'p-mixed','mixed-key','p','Unrelated work','Separate scope',
                   'pending','sol','high','sol','high','default',NULL,NULL,'[]',
                   'complete',?,?
               )""",
            (now, now),
        )
        self.controller._queue_archon_update(
            "completion:mixed",
            {
                "kind": "assignment_completed",
                "assignment_id": self.assignment["id"],
                "action_id": completed_action,
                "bead_id": "p-1",
                "run_id": self.assignment["run_id"],
                "workflow_id": "workflow-complete",
            },
        )
        self.controller._queue_archon_update(
            "proposal:mixed",
            {
                "kind": "proposal",
                "bead_id": "p-mixed",
                "project": "p",
                "title": "Unrelated work",
                "scope_summary": "Keep this concurrent workflow separate.",
                "workflow_id": "workflow-concurrent",
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
                new=AsyncMock(return_value="mixed-ack-turn"),
            ),
        ):
            await self.controller._deliver_update_batch()
        acknowledgement = self.controller.store.row(
            """SELECT * FROM actions WHERE task_id = ? AND kind = 'archon'
               ORDER BY id DESC LIMIT 1""",
            (archon["id"],),
        )
        self.controller.store.record_tool_cost(
            source_key="mixed-ack-cost",
            tool_name="test",
            quantity=1,
            unit="call",
            unit_rate="0.05",
            source_url="https://developers.openai.com/api/docs/pricing",
            action_id=int(acknowledgement["id"]),
        )
        self.controller.store.execute(
            "UPDATE actions SET state = 'processed' WHERE id = ?",
            (acknowledgement["id"],),
        )

        self.controller._finalize_completed_workflows_for_archon(
            int(acknowledgement["id"])
        )
        self.controller._finalize_completed_workflows_for_archon(
            int(acknowledgement["id"])
        )
        complete = self.controller.store.row("""SELECT * FROM workflow_cost_boundaries
               WHERE workflow_id = 'workflow-complete'""")
        concurrent = self.controller.store.row("""SELECT * FROM workflow_cost_boundaries
               WHERE workflow_id = 'workflow-concurrent'""")
        self.assertEqual(complete["state"], "closed")
        self.assertEqual(complete["frozen_amount"], "0.01")
        self.assertEqual(concurrent["state"], "open")
        links = self.controller.store.rows(
            """SELECT workflow_id, include_cost FROM workflow_cost_actions
               WHERE action_id = ? ORDER BY workflow_id""",
            (acknowledgement["id"],),
        )
        self.assertEqual([row["include_cost"] for row in links], [1])
        report = self.controller.store.cost_report(
            workflow_id="workflow-complete", group_by="workflow"
        )["groups"][0]
        self.assertEqual(report["attributed"]["amount"], "0.01")
        self.assertEqual(report["coverage"], "partial")
        self.assertNotIn(
            "mixed-workflow Archon response", " ".join(report["exclusions"])
        )
        self.assertEqual(
            self.controller.store.row("""SELECT COUNT(*) AS count FROM events
                   WHERE kind = 'workflow_cost_finalized'
                     AND entity_id = 'workflow-complete'""")["count"],
            1,
        )
        self._restart_controller()
        self.controller._finalize_completed_workflows_for_archon(
            int(acknowledgement["id"])
        )
        self.assertEqual(
            self.controller.store.row("""SELECT COUNT(*) AS count FROM events
                   WHERE kind = 'workflow_cost_finalized'
                     AND entity_id = 'workflow-complete'""")["count"],
            1,
        )

    def test_completion_with_same_workflow_recovery_includes_acknowledgement(
        self,
    ) -> None:
        archon = self.controller.store.register_task(
            native_thread_id="archon-cost-one-workflow",
            role="archon",
            description="Fleet",
            model="gpt-5.6-sol",
            reasoning_effort="high",
            project_id="p",
            state="idle",
        )
        now = "2026-01-01T00:00:00Z"
        source_action = int(
            self.controller.store.execute(
                """INSERT INTO actions(task_id, assignment_id, kind, payload,
                       state, created_at, updated_at)
                   VALUES (?, ?, 'correct', '{}', 'processed', ?, ?)""",
                (self.executor["id"], self.assignment["id"], now, now),
            ).lastrowid
        )
        payload = {
            "batch_items": [
                {
                    "content": {
                        "kind": "assignment_completed",
                        "assignment_id": self.assignment["id"],
                        "workflow_id": "workflow-one",
                    }
                },
                {
                    "content": {
                        "kind": "assignment_recovery",
                        "assignment_id": self.assignment["id"],
                        "workflow_id": "workflow-one",
                    }
                },
            ]
        }
        acknowledgement = int(
            self.controller.store.execute(
                """INSERT INTO actions(task_id, kind, payload, state,
                       created_at, updated_at)
                   VALUES (?, 'archon', ?, 'processed', ?, ?)""",
                (archon["id"], json.dumps(payload), now, now),
            ).lastrowid
        )
        self.controller.store.link_bead_to_workflow("workflow-one", "p-1")
        self.controller.store.link_action_to_workflow(
            "workflow-one", source_action, causal_role="executor"
        )
        self.controller.store.link_action_to_workflow(
            "workflow-one", acknowledgement, causal_role="archon"
        )
        for key, action_id, amount in (
            ("one-source", source_action, "0.01"),
            ("one-ack", acknowledgement, "0.05"),
        ):
            self.controller.store.record_tool_cost(
                source_key=key,
                tool_name="test",
                quantity=1,
                unit="call",
                unit_rate=amount,
                source_url="https://developers.openai.com/api/docs/pricing",
                action_id=action_id,
            )
        self.controller.store.execute(
            "UPDATE assignments SET stage = 'completed' WHERE id = ?",
            (self.assignment["id"],),
        )

        self.controller._finalize_completed_workflows_for_archon(acknowledgement)
        self.controller._finalize_completed_workflows_for_archon(acknowledgement)
        boundary = self.controller.store.row("""SELECT * FROM workflow_cost_boundaries
               WHERE workflow_id = 'workflow-one'""")
        self.assertEqual(boundary["state"], "closed")
        self.assertEqual(boundary["frozen_amount"], "0.06")
        event = self.controller.store.row(
            """SELECT detail_json FROM events WHERE kind = 'workflow_cost_finalized'
               AND entity_id = 'workflow-one'"""
        )
        self.assertTrue(
            json.loads(event["detail_json"])["includes_acknowledgement_action"]
        )

    def test_recovery_resolution_and_succession_retain_causal_workflows(self) -> None:
        archon = self.controller.store.register_task(
            native_thread_id="archon-causal",
            role="archon",
            description="Fleet",
            model="gpt-5.6-sol",
            reasoning_effort="high",
            project_id="p",
            state="idle",
        )
        self.controller.store.link_bead_to_workflow("workflow-causal", "p-1")
        self.controller._schedule_assignment_recovery(
            dict(self.assignment), "runtime disconnected"
        )
        self.controller._queue_operation_resolution(
            {"id": 91, "kind": "turn_start", "target": "assignment"},
            hold_id=8,
            condition="uncertain operation",
            target_type="assignment",
            target_id=int(self.assignment["id"]),
        )
        self.controller._queue_archon_update(
            "succession:causal",
            {
                "kind": "archon_succession_completed",
                "predecessor_task_id": 1,
                "successor_task_id": 2,
                "reason": "context limit",
            },
        )
        updates = self.controller.store.rows(
            "SELECT identity, content FROM updates WHERE recipient_task_id = ?",
            (archon["id"],),
        )
        by_identity = {row["identity"]: json.loads(row["content"]) for row in updates}
        recovery = next(
            content
            for identity, content in by_identity.items()
            if identity.startswith("recovery:")
        )
        self.assertEqual(recovery["workflow_id"], "workflow-causal")
        self.assertEqual(
            by_identity["operation-resolution:91"]["workflow_id"],
            "workflow-causal",
        )
        self.assertEqual(
            by_identity["succession:causal"]["workflow_id"],
            "workflow-causal",
        )

    async def test_third_review_failure_reaches_archon_and_cancel_resolves_hold(
        self,
    ) -> None:
        archon = self.controller.store.register_task(
            native_thread_id="archon-review-escalation",
            role="archon",
            description="Fleet",
            model="sol",
            reasoning_effort="high",
            project_id="p",
            state="idle",
        )
        self.controller.store.link_bead_to_workflow("workflow-review-escalation", "p-1")
        findings_path = Path(self.temporary.name) / "review-findings.json"
        self.controller.store.execute(
            """UPDATE assignments SET candidate_id = 'candidate-review-escalation',
               source_oid = 'review-source' WHERE id = ?""",
            (self.assignment["id"],),
        )
        implementation = self.controller.store.execute(
            """INSERT INTO actions(
                   task_id, assignment_id, kind, payload, state, outcome_kind,
                   outcome_payload, created_at, updated_at
               ) VALUES (?, ?, 'implement', '{}', 'processed',
                         'ready_for_review', ?, ?, ?)""",
            (
                self.executor["id"],
                self.assignment["id"],
                json.dumps({"evidence": "retained implementation"}),
                utc_now(),
                utc_now(),
            ),
        )
        self.controller.store.execute(
            """INSERT INTO handoffs(
                   assignment_id, source_action_id, kind, content_json, created_at
               ) VALUES (?, ?, 'implementation_evidence', ?, ?)""",
            (
                self.assignment["id"],
                implementation.lastrowid,
                json.dumps({"evidence": "retained implementation"}),
                utc_now(),
            ),
        )

        review_action_ids: list[int] = []
        for failure in range(1, 4):
            self.controller.store.execute(
                "UPDATE assignments SET stage = 'reviewing' WHERE id = ?",
                (self.assignment["id"],),
            )
            action = self.controller.store.execute(
                """INSERT INTO actions(
                       task_id, assignment_id, kind, payload, state, created_at,
                       updated_at
                   ) VALUES (?, ?, 'review', '{}', 'active', ?, ?)""",
                (
                    self.overseer["id"],
                    self.assignment["id"],
                    utc_now(),
                    utc_now(),
                ),
            )
            review_action_ids.append(int(action.lastrowid))
            findings_path.write_text(
                json.dumps(
                    {
                        "findings": [
                            {
                                "problem": f"substantive defect {failure}",
                                "evidence": f"review evidence {failure}",
                                "required_change": f"correct defect {failure}",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            accept_finish(
                self.controller.store,
                native_thread_id="overseer",
                outcome_kind="changes_requested",
                options={"input": json.loads(findings_path.read_text())},
            )
            observed = observe_action_terminal(
                self.controller.store, int(action.lastrowid)
            )
            self.assertTrue(observed["advanced"])

        escalation = self.controller.store.row(
            "SELECT * FROM updates WHERE recipient_task_id = ?", (archon["id"],)
        )
        self.assertIsNotNone(escalation)
        self.assertEqual(
            escalation["identity"],
            f"review-escalation:{self.assignment['id']}:{review_action_ids[-1]}",
        )
        hold_id = json.loads(escalation["content"])["hold_id"]

        runtime = AsyncMock()
        runtime.ready = True
        runtime.start_turn.return_value = "archon-escalation-turn"
        self.controller.runtime = runtime
        with patch.object(
            self.controller,
            "_refresh_task",
            new=AsyncMock(
                return_value={
                    "can_start": True,
                    "last_turn_id": None,
                    "runtime_status": "idle",
                }
            ),
        ):
            await self.controller._deliver_update_batch()
            await self.controller._deliver_update_batch()

        runtime.start_turn.assert_awaited_once()
        retained_escalation = self.controller.store.row(
            "SELECT content FROM updates WHERE id = ?", (escalation["id"],)
        )
        self.assertEqual(
            json.loads(retained_escalation["content"])["workflow_id"],
            "workflow-review-escalation",
        )
        prompt = runtime.start_turn.await_args.args[1]
        self.assertIn(
            f"Assignment {self.assignment['id']} reached 3 substantive review failures",
            prompt,
        )
        self.assertIn(f"review action id: {review_action_ids[-1]}", prompt)
        self.assertIn(f"hold id: {hold_id}", prompt)
        self.assertIn('resolutions: ["retry", "rescope", "cancel"]', prompt)
        self.assertIn("unresolved findings", prompt)
        self.assertIn("substantive defect 3", prompt)
        self.assertIn("candidate revision", prompt)
        self.assertIn("changes since prior attempt", prompt)
        self.assertIn("reviewer recommendation", prompt)
        self.assertLess(len(prompt), 7000)
        frozen_action = self.controller.store.row(
            """SELECT * FROM actions WHERE task_id = ? AND kind = 'archon'
               ORDER BY id DESC LIMIT 1""",
            (archon["id"],),
        )
        batch = self.controller.store.row(
            "SELECT * FROM batches WHERE action_id = ?", (frozen_action["id"],)
        )
        self.assertEqual(batch["state"], "frozen")
        self.assertEqual(
            self.controller.store.row(
                "SELECT state FROM updates WHERE id = ?", (escalation["id"],)
            )["state"],
            "batched",
        )

        decision_path = Path(
            self.controller._handoff_destinations(frozen_action)["decisions"]
        )
        decision_payload = {
            "decisions": [
                {
                    "decision": "resolve_escalation",
                    "assignment_id": self.assignment["id"],
                    "resolution": "cancel",
                    "reason": "Repeated substantive failures make the work unsuitable.",
                }
            ],
            "handled_update_ids": [escalation["id"]],
        }
        decision_path.write_text(json.dumps(decision_payload), encoding="utf-8")
        await self.controller.handle_request(
            {
                "command": "finish",
                "thread_id": "archon-review-escalation",
                "outcome": "decisions",
                "options": self._structured_options(decision_path, decision_payload),
            }
        )
        self.controller.store.execute(
            """UPDATE tasks SET last_turn_terminal = 1, helpers_terminal = 1
               WHERE id = ?""",
            (archon["id"],),
        )
        with patch.object(
            self.controller,
            "_refresh_task",
            new=AsyncMock(
                return_value={
                    "last_turn_id": "archon-escalation-turn",
                    "last_turn_status": "completed",
                    "last_turn_terminal": True,
                    "helpers_terminal": True,
                }
            ),
        ):
            handled = await self.controller._handle_runtime_event(
                "turn/completed",
                {
                    "threadId": "archon-review-escalation",
                    "turn": {
                        "id": "archon-escalation-turn",
                        "status": "completed",
                    },
                },
            )

        self.assertTrue(handled)
        assignment = self.controller.store.row(
            "SELECT * FROM assignments WHERE id = ?", (self.assignment["id"],)
        )
        self.assertEqual(assignment["stage"], "canceled")
        self.assertIsNone(assignment["operator_hold_id"])
        self.assertEqual(
            self.controller.store.row(
                "SELECT state FROM runs WHERE id = ?", (self.assignment["run_id"],)
            )["state"],
            "canceled",
        )
        self.assertIsNotNone(
            self.controller.store.row(
                "SELECT released_at FROM holds WHERE id = ?", (hold_id,)
            )["released_at"]
        )
        self.assertEqual(
            self.controller.store.row(
                "SELECT state FROM updates WHERE id = ?", (escalation["id"],)
            )["state"],
            "processed",
        )
        self.assertEqual(
            observe_action_terminal(self.controller.store, frozen_action["id"]),
            {"advanced": True, "reused": True},
        )
        self.assertEqual(
            len(
                self.controller.store.rows(
                    "SELECT * FROM updates WHERE identity = ?",
                    (escalation["identity"],),
                )
            ),
            1,
        )
        self.assertEqual(invariant_violations(self.controller.store), [])

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
            "projectId": "codex-p",
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

    async def test_runtime_loops_bound_noise_and_deduplicate_completion(
        self,
    ) -> None:
        decision = acquire_lease(
            self.controller.store,
            LeaseRequest(
                task_id=int(self.executor["id"]),
                assignment_id=int(self.assignment["id"]),
                kind="implement",
                payload={"purpose": "integration test"},
                project_ids=("p",),
                pair_id=int(self.assignment["run_id"]),
            ),
        )
        self.assertTrue(decision.admitted)
        assert decision.action is not None
        runtime = AsyncMock()
        runtime.ready = True
        runtime.start_turn.side_effect = ["turn-stream", "turn-reminder"]
        runtime.read_thread.return_value = {
            "id": "executor",
            "name": self.executor["title"],
            "projectId": "codex-p",
            "status": {"type": "idle"},
            "turns": [
                {
                    "id": "turn-stream",
                    "status": "completed",
                    "items": [],
                }
            ],
        }
        self.controller.runtime = runtime
        self.executor["runtime_status"] = "unmaterialized"
        self.assignment["worktree_path"] = str(self.worktree)
        await self.controller._dispatch_action(
            decision.action,
            task=self.executor,
            assignment=self.assignment,
        )
        turn_start = self.controller.store.row(
            "SELECT id FROM external_operations WHERE kind = 'turn_start' AND target = ?",
            (str(decision.action["id"]),),
        )
        assert turn_start is not None
        lifecycle_event = self.controller.store.row(
            "SELECT id FROM events WHERE kind = 'operation_started' AND entity_type = 'operation' AND entity_id = ?",
            (str(turn_start["id"]),),
        )
        assert lifecycle_event is not None

        reconcile = AsyncMock()
        advance = AsyncMock()
        advancement_done = asyncio.Event()
        advance.side_effect = advancement_done.set
        event_worker = asyncio.create_task(self.controller._event_loop())
        advancement_worker = asyncio.create_task(self.controller._advancement_loop())
        try:
            with (
                patch.object(self.controller, "reconcile", new=reconcile),
                patch.object(self.controller, "advance", new=advance),
            ):
                events_before_noise = self.controller.store.row(
                    "SELECT COUNT(*) AS count FROM events"
                )["count"]
                for sequence in range(10_000):
                    await self.controller._queue_runtime_event(
                        "item/agentMessage/delta",
                        {
                            "threadId": "executor",
                            "delta": f"token-{sequence}",
                        },
                    )
                for sequence in range(1_000):
                    await self.controller._queue_runtime_event(
                        "thread/tokenUsage/updated",
                        {
                            "threadId": "executor",
                            "turnId": "turn-stream",
                            "modelContextWindow": 128_000,
                            "tokenUsage": {
                                "last": {
                                    "inputTokens": 1,
                                    "cachedInputTokens": 0,
                                    "cacheWriteInputTokens": 0,
                                    "outputTokens": 1,
                                    "reasoningOutputTokens": 0,
                                    "totalTokens": 2,
                                },
                                "total": {
                                    "inputTokens": sequence,
                                    "cachedInputTokens": sequence // 2,
                                    "cacheWriteInputTokens": 0,
                                    "outputTokens": sequence,
                                    "reasoningOutputTokens": sequence // 3,
                                    "totalTokens": sequence * 2,
                                },
                            },
                        },
                    )
                for _ in range(1_000):
                    await self.controller._queue_runtime_event(
                        "thread/status/changed",
                        {"threadId": "executor", "status": {"type": "active"}},
                    )
                await asyncio.wait_for(self.controller.events.join(), timeout=3)

                events_after_noise = self.controller.store.row(
                    "SELECT COUNT(*) AS count FROM events"
                )["count"]
                self.assertEqual(events_after_noise, events_before_noise)
                self.assertFalse(self.controller.advance_requested.is_set())
                reconcile.assert_not_awaited()
                advance.assert_not_awaited()
                runtime.start_turn.assert_awaited_once()
                retained_usage = self.controller.store.rows(
                    "SELECT * FROM action_turn_usage WHERE native_turn_id = 'turn-stream'"
                )
                self.assertEqual(len(retained_usage), 1)
                self.assertEqual(retained_usage[0]["total_tokens"], 1998)
                status_action = next(
                    item
                    for item in self.controller.store.status()["actions"]
                    if item["id"] == decision.action["id"]
                )
                self.assertEqual(
                    set(status_action["usage"]),
                    {
                        "direct_total_tokens",
                        "attributed_total_tokens",
                        "coverage",
                        "turn_count",
                    },
                )
                self.assertNotIn("turns", status_action["usage"])

                await self.controller._queue_runtime_event(
                    "turn/completed",
                    {
                        "threadId": "executor",
                        "turn": {"id": "turn-stream", "status": "completed"},
                    },
                )
                await asyncio.wait_for(self.controller.events.join(), timeout=1)
                await asyncio.wait_for(advancement_done.wait(), timeout=1)

                self.assertEqual(reconcile.await_count, 1)
                self.assertEqual(advance.await_count, 1)
                self.assertEqual(runtime.start_turn.await_count, 2)
                self.assertEqual(
                    runtime.start_turn.await_args_list[-1].args[0], "executor"
                )
                self.assertIn(
                    "finish outcome",
                    runtime.start_turn.await_args_list[-1].args[1],
                )
                self.assertFalse(self.controller.advance_requested.is_set())

                events_after_completion = self.controller.store.row(
                    "SELECT COUNT(*) AS count FROM events"
                )["count"]
                await self.controller._queue_runtime_event(
                    "turn/completed",
                    {
                        "threadId": "executor",
                        "turn": {"id": "turn-stream", "status": "completed"},
                    },
                )
                await asyncio.wait_for(self.controller.events.join(), timeout=1)
                await asyncio.sleep(0)

                self.assertEqual(reconcile.await_count, 1)
                self.assertEqual(advance.await_count, 1)
                self.assertEqual(runtime.start_turn.await_count, 2)
                self.assertEqual(
                    self.controller.store.row("SELECT COUNT(*) AS count FROM events")[
                        "count"
                    ],
                    events_after_completion,
                )
                self.assertFalse(self.controller.advance_requested.is_set())

                # The loop itself also rejects a producer that bypasses the
                # queueing boundary, without taking the mutation lock or logging.
                await self.controller.events.put(
                    (
                        "item/agentMessage/delta",
                        {"threadId": "executor", "delta": "direct"},
                    )
                )
                await asyncio.wait_for(self.controller.events.join(), timeout=1)
                self.assertEqual(reconcile.await_count, 1)
        finally:
            event_worker.cancel()
            advancement_worker.cancel()
            await asyncio.gather(
                event_worker, advancement_worker, return_exceptions=True
            )

        fallback_intervals: list[int | float] = []
        fallback_done = asyncio.Event()
        fallback_reconcile = AsyncMock()

        async def wait_for_fallback(interval: int | float) -> None:
            fallback_intervals.append(interval)
            if len(fallback_intervals) > 1:
                await asyncio.Future()

        async def mark_fallback_advanced() -> None:
            fallback_done.set()

        with (
            patch("fulcrum.controller.asyncio.sleep", new=wait_for_fallback),
            patch.object(self.controller, "reconcile", new=fallback_reconcile),
            patch.object(
                self.controller,
                "advance",
                new=AsyncMock(side_effect=mark_fallback_advanced),
            ),
        ):
            fallback_worker = asyncio.create_task(self.controller._fallback_loop())
            try:
                await asyncio.wait_for(fallback_done.wait(), timeout=1)
                self.assertEqual(fallback_intervals[0], 30)
                self.assertEqual(fallback_reconcile.await_count, 1)
            finally:
                fallback_worker.cancel()
                await asyncio.gather(fallback_worker, return_exceptions=True)

        for _ in range(225):
            self.controller.store.event(
                "reconciliation_started", "bounded sample noise"
            )
            self.controller.store.event(
                "reconciliation_completed", "bounded sample noise"
            )
        scope = json.dumps({"global": False, "projects": ["p"]})
        occurrence = self.controller.store.execute(
            "INSERT INTO occurrences(kind, scope, authority, state, created_at, updated_at) VALUES ('sage', ?, 'test', 'queued', 'now', 'now')",
            (scope,),
        )
        retained = self.controller.store.row(
            "SELECT * FROM occurrences WHERE id = ?", (occurrence.lastrowid,)
        )
        assert retained is not None
        evidence = self.controller._specialist_evidence(
            json.loads(scope), occurrence=retained
        )
        self.assertLessEqual(len(evidence["recent_events"]), 200)
        self.assertIn(
            lifecycle_event["id"],
            [event["id"] for event in evidence["recent_events"]],
        )
        self.assertNotIn(
            "reconciliation_started",
            [event["kind"] for event in evidence["recent_events"]],
        )
        self.assertIn(
            {"kind": "reconciliation_started", "count": 225},
            evidence["event_counts"],
        )

    async def test_status_remains_available_during_blocked_approval_without_advancing(
        self,
    ) -> None:
        self.controller.store.execute(
            """UPDATE assignments SET stage = 'delivering',
               candidate_id = 'candidate-1', mandate_candidate_id = 'candidate-1',
               mandate_scope = scope_snapshot WHERE id = ?""",
            (self.assignment["id"],),
        )
        self.controller.store.execute(
            """UPDATE tasks SET runtime_status = 'idle', last_turn_terminal = 1,
               helpers_terminal = 1 WHERE id IN (?, ?)""",
            (self.executor["id"], self.overseer["id"]),
        )
        tollgate = BlockingApprovalTollgate()
        self.controller.tollgate = tollgate  # type: ignore[assignment]
        self.controller.beads = FakeBeads()  # type: ignore[assignment]
        runtime = AsyncMock()
        runtime.ready = True
        runtime.read_thread.return_value = {
            "projectId": "codex-p",
            "status": {"type": "idle"},
            "turns": [],
        }
        self.controller.runtime = runtime

        advance_count = 0
        second_advance_completed = asyncio.Event()

        async def deliver_once() -> None:
            nonlocal advance_count
            advance_count += 1
            assignment = self.controller.store.row(
                """SELECT a.*, r.project_id FROM assignments a
                   JOIN runs r ON r.id = a.run_id
                   WHERE a.id = ? AND a.stage = 'delivering'""",
                (self.assignment["id"],),
            )
            if assignment is not None:
                await self.controller._deliver(assignment)
            if advance_count == 2:
                second_advance_completed.set()

        reconcile = AsyncMock()
        worker = asyncio.create_task(self.controller._advancement_loop())
        self.paths.socket.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(
            self.controller._handle_client,
            path=self.paths.socket,
            limit=16 * 1024 * 1024,
        )
        try:
            with (
                patch.object(self.controller, "reconcile", new=reconcile),
                patch.object(self.controller, "advance", new=deliver_once),
            ):
                self.controller.advance_requested.set()
                approval_started = await asyncio.to_thread(
                    tollgate.approval_started.wait, 1
                )
                self.assertTrue(approval_started)
                self.assertTrue(self.controller.mutation_lock.locked())
                self.assertFalse(self.controller.advance_requested.is_set())

                boundaries_before = self.controller.store.row(
                    """SELECT COUNT(*) AS count FROM events
                       WHERE kind IN ('reconciliation_started',
                                      'reconciliation_completed')"""
                )["count"]
                started = time.monotonic()
                response = await request(
                    self.paths.socket,
                    {"command": "status", "events": 20},
                    timeout=1,
                )
                elapsed = time.monotonic() - started

                self.assertLess(elapsed, 1)
                operations = response["data"]["operations"]
                self.assertEqual(len(operations), 1)
                self.assertEqual(operations[0]["kind"], "tollgate_approve")
                self.assertEqual(operations[0]["state"], "sent")
                self.assertEqual(operations[0]["attempt_count"], 1)
                self.assertFalse(self.controller.advance_requested.is_set())
                boundaries_after = self.controller.store.row(
                    """SELECT COUNT(*) AS count FROM events
                       WHERE kind IN ('reconciliation_started',
                                      'reconciliation_completed')"""
                )["count"]
                self.assertEqual(boundaries_after, boundaries_before)

                # Signals coalesce while the claimed attempt is in flight. The
                # follow-up pass observes completed state instead of approving it
                # a second time.
                self.controller.advance_requested.set()
                self.controller.advance_requested.set()
                tollgate.release_approval.set()
                await asyncio.wait_for(second_advance_completed.wait(), timeout=1)
                self.assertEqual(advance_count, 2)
                self.assertEqual(tollgate.approved, ["candidate-1"])
                self.assertEqual(
                    self.controller.store.row(
                        """SELECT COUNT(*) AS count FROM operation_attempts oa
                           JOIN external_operations eo ON eo.id = oa.operation_id
                           WHERE eo.kind = 'tollgate_approve'"""
                    )["count"],
                    1,
                )
        finally:
            tollgate.release_approval.set()
            server.close()
            await server.wait_closed()
            self.paths.socket.unlink(missing_ok=True)
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_telemetry_path_attributes_collaboration_and_never_advances(
        self,
    ) -> None:
        now = "2026-01-01T00:00:00Z"
        action_id = int(
            self.controller.store.execute(
                """INSERT INTO actions(task_id, assignment_id, kind, payload,
                       state, native_turn_id, created_at, updated_at)
                   VALUES (?, ?, 'implement', '{}', 'active', 'parent-turn', ?, ?)""",
                (self.executor["id"], self.assignment["id"], now, now),
            ).lastrowid
        )
        self.controller.store.bind_action_turn(action_id, "executor", "parent-turn")
        events_before = self.controller.store.row(
            "SELECT COUNT(*) AS count FROM events"
        )["count"]

        await self.controller._queue_runtime_event(
            "item/completed",
            {
                "threadId": "executor",
                "turnId": "parent-turn",
                "item": {
                    "id": "collaboration-item",
                    "type": "collabToolCall",
                    "tool": "spawn_agent",
                    "senderThreadId": "executor",
                    "newThreadId": "helper-thread",
                },
            },
        )
        self.assertFalse(
            await self.controller._handle_runtime_event(
                "turn/started",
                {"threadId": "helper-thread", "turn": {"id": "helper-turn"}},
            )
        )
        await self.controller._queue_runtime_event(
            "thread/tokenUsage/updated",
            {
                "threadId": "helper-thread",
                "turnId": "helper-turn",
                "tokenUsage": {
                    "total": {
                        "inputTokens": 8,
                        "cachedInputTokens": 4,
                        "cacheWriteInputTokens": 0,
                        "outputTokens": 2,
                        "reasoningOutputTokens": 1,
                        "totalTokens": 10,
                    }
                },
            },
        )
        await self.controller._queue_runtime_event(
            "item/completed",
            {
                "threadId": "helper-thread",
                "turnId": "helper-turn",
                "item": {
                    "id": "helper-search",
                    "type": "webSearch",
                    "query": "helper query",
                },
            },
        )

        summary = self.controller.store.action_usage_summary(action_id)
        self.assertEqual(summary["direct_total_tokens"], None)
        self.assertEqual(summary["attributed_total_tokens"], 10)
        assignment_rollup = self.controller.store.usage_report(
            assignment_id=int(self.assignment["id"]), group_by="assignment"
        )["groups"][0]
        run_rollup = self.controller.store.usage_report(
            run_id=int(self.assignment["run_id"]), group_by="run"
        )["groups"][0]
        self.assertEqual(assignment_rollup["attributed"]["total_tokens"], 10)
        self.assertEqual(run_rollup["attributed"]["total_tokens"], 10)
        cost = self.controller.store.cost_report(action_id=action_id)["groups"][0]
        self.assertEqual(cost["direct"]["tool_count"], 0)
        self.assertEqual(cost["attributed"]["tool_count"], 1)
        self.assertEqual(
            self.controller.store.row("SELECT COUNT(*) AS count FROM events")["count"],
            events_before,
        )
        self.assertFalse(self.controller.advance_requested.is_set())
        self.assertFalse(self.controller.mutation_lock.locked())
        ownership = self.controller.store.row("""SELECT parent_turn_id,
                      collaboration_item_id, native_turn_id
               FROM telemetry_helper_threads
               WHERE native_thread_id = 'helper-thread'""")
        self.assertEqual(ownership["parent_turn_id"], "parent-turn")
        self.assertEqual(ownership["collaboration_item_id"], "collaboration-item")
        self.assertEqual(ownership["native_turn_id"], "helper-turn")

    async def test_protocol_reroutes_and_observable_tools_are_durable(self) -> None:
        now = "2026-01-01T00:00:00Z"
        self.controller.store.execute(
            "UPDATE tasks SET model = 'gpt-5.6-sol' WHERE id = ?",
            (self.executor["id"],),
        )
        action_id = int(
            self.controller.store.execute(
                """INSERT INTO actions(task_id, assignment_id, kind, payload,
                       state, native_turn_id, created_at, updated_at)
                   VALUES (?, ?, 'implement', '{}', 'active', 'priced-turn', ?, ?)""",
                (self.executor["id"], self.assignment["id"], now, now),
            ).lastrowid
        )
        self.controller.store.bind_action_turn(action_id, "executor", "priced-turn")
        reroute = {
            "threadId": "executor",
            "turnId": "priced-turn",
            "fromModel": "gpt-5.6-sol",
            "toModel": "gpt-5.6-luna",
            "reason": "capacity",
        }
        await self.controller._queue_runtime_event("model/rerouted", reroute)
        await self.controller._queue_runtime_event("model/rerouted", reroute)
        await self.controller._queue_runtime_event(
            "thread/tokenUsage/updated",
            {
                "threadId": "executor",
                "turnId": "priced-turn",
                "serviceTier": "standard",
                "responseId": "response-1",
                "tokenUsage": {
                    "last": {
                        "inputTokens": 10,
                        "cachedInputTokens": 4,
                        "cacheWriteInputTokens": 0,
                        "outputTokens": 2,
                        "reasoningOutputTokens": 1,
                        "totalTokens": 12,
                    },
                    "total": {
                        "inputTokens": 10,
                        "cachedInputTokens": 4,
                        "cacheWriteInputTokens": 0,
                        "outputTokens": 2,
                        "reasoningOutputTokens": 1,
                        "totalTokens": 12,
                    },
                },
            },
        )
        await self.controller._queue_runtime_event(
            "thread/tokenUsage/updated",
            {
                "threadId": "executor",
                "turnId": "priced-turn",
                "effectiveModel": "gpt-5.6-terra",
                "serviceTier": "standard",
                "responseId": "response-2",
                "tokenUsage": {
                    "last": {
                        "inputTokens": 10,
                        "cachedInputTokens": 4,
                        "cacheWriteInputTokens": 0,
                        "outputTokens": 2,
                        "reasoningOutputTokens": 1,
                        "totalTokens": 12,
                    },
                    "total": {
                        "inputTokens": 20,
                        "cachedInputTokens": 8,
                        "cacheWriteInputTokens": 0,
                        "outputTokens": 4,
                        "reasoningOutputTokens": 2,
                        "totalTokens": 24,
                    },
                },
            },
        )
        await self.controller._queue_runtime_event(
            "model/rerouted",
            {
                "threadId": "executor",
                "turnId": "priced-turn",
                "fromModel": "gpt-5.6-luna",
                "toModel": "gpt-6-astra",
                "reason": "capability",
            },
        )
        await self.controller._queue_runtime_event(
            "thread/tokenUsage/updated",
            {
                "threadId": "executor",
                "turnId": "priced-turn",
                "serviceTier": "standard",
                "responseId": "response-3",
                "tokenUsage": {
                    "last": {
                        "inputTokens": 10,
                        "cachedInputTokens": 4,
                        "cacheWriteInputTokens": 0,
                        "outputTokens": 2,
                        "reasoningOutputTokens": 1,
                        "totalTokens": 12,
                    },
                    "total": {
                        "inputTokens": 30,
                        "cachedInputTokens": 12,
                        "cacheWriteInputTokens": 0,
                        "outputTokens": 6,
                        "reasoningOutputTokens": 3,
                        "totalTokens": 36,
                    },
                },
            },
        )
        unassociated = {
            "threadId": "executor",
            "turnId": "priced-turn",
            "fromModel": "gpt-6-astra",
            "toModel": "gpt-5.6-terra",
            "reason": "late without response",
        }
        await self.controller._queue_runtime_event("model/rerouted", unassociated)
        await self.controller._queue_runtime_event("model/rerouted", unassociated)
        item = {
            "threadId": "executor",
            "turnId": "priced-turn",
            "item": {"id": "search-1", "type": "webSearch", "query": "x"},
        }
        await self.controller._queue_runtime_event("item/started", item)
        await self.controller._queue_runtime_event("item/completed", item)
        await self.controller._queue_runtime_event("item/completed", item)
        self.controller.store.execute("DELETE FROM api_tool_rate_cards")
        await self.controller._queue_runtime_event(
            "item/completed",
            {
                "threadId": "executor",
                "turnId": "priced-turn",
                "item": {"id": "search-2", "type": "webSearch", "query": "y"},
            },
        )
        self.controller.store.finalize_action_usage(action_id)
        await self.controller._queue_runtime_event(
            "item/completed",
            {
                "threadId": "executor",
                "turnId": "priced-turn",
                "item": {"id": "search-3", "type": "webSearch", "query": "z"},
            },
        )

        contributions = self.controller.store.rows(
            "SELECT * FROM cost_contributions WHERE action_id = ? ORDER BY id",
            (action_id,),
        )
        self.assertEqual(len(contributions), 6)
        self.assertEqual(contributions[0]["effective_model"], "gpt-5.6-luna")
        self.assertEqual(contributions[1]["effective_model"], "gpt-5.6-terra")
        self.assertEqual(contributions[2]["effective_model"], "gpt-6-astra")
        self.assertEqual(contributions[1]["coverage"], "complete")
        tools = [
            row for row in contributions if row["contribution_kind"] == "tool_call"
        ]
        self.assertEqual(len(tools), 3)
        self.assertEqual(tools[0]["amount"], "0.01")
        self.assertIn("one completed", " ".join(json.loads(tools[0]["applied_rules"])))
        self.assertEqual(tools[1]["coverage"], "partial")
        self.assertEqual(
            self.controller.store.row("SELECT COUNT(*) AS count FROM model_reroutes")[
                "count"
            ],
            3,
        )
        self.assertEqual(
            self.controller.store.row("""SELECT COUNT(*) AS count FROM model_reroutes
                   WHERE contribution_id IS NULL""")["count"],
            1,
        )
        reroutes = self.controller.store.rows(
            "SELECT contribution_id FROM model_reroutes ORDER BY id"
        )
        self.assertEqual(
            [row["contribution_id"] for row in reroutes],
            [contributions[0]["id"], contributions[2]["id"], None],
        )
        self.assertEqual(
            self.controller.store.row("""SELECT COUNT(*) AS count FROM events
                   WHERE kind = 'cost_late_contribution_incorporated'""")["count"],
            1,
        )
        report = self.controller.store.cost_report(action_id=action_id)["groups"][0]
        self.assertEqual(report["coverage"], "partial")
        self.assertEqual(report["attributed"]["response_count"], 3)
        self.assertEqual(
            {
                source["model"]
                for source in report["rate_card_provenance"]
                if source["model"] is not None
            },
            {"gpt-5.6-luna", "gpt-5.6-terra", "gpt-6-astra"},
        )
        web_rate = next(
            source
            for source in report["rate_card_provenance"]
            if source["tool"] == "web_search"
        )
        self.assertEqual(web_rate["unit_rate"], "0.01")
        self.assertEqual(web_rate["unit"], "call")
        self.assertFalse(self.controller.advance_requested.is_set())

    async def test_identical_consumed_reroute_is_retained_for_next_response(
        self,
    ) -> None:
        now = "2026-01-01T00:00:00Z"
        self.controller.store.execute(
            "UPDATE tasks SET model = 'gpt-5.6-sol' WHERE id = ?",
            (self.executor["id"],),
        )
        action_id = int(
            self.controller.store.execute(
                """INSERT INTO actions(task_id, assignment_id, kind, payload,
                       state, native_turn_id, created_at, updated_at)
                   VALUES (?, ?, 'implement', '{}', 'active',
                           'repeat-reroute-turn', ?, ?)""",
                (self.executor["id"], self.assignment["id"], now, now),
            ).lastrowid
        )
        self.controller.store.bind_action_turn(
            action_id, "executor", "repeat-reroute-turn"
        )
        reroute = {
            "threadId": "executor",
            "turnId": "repeat-reroute-turn",
            "fromModel": "gpt-5.6-sol",
            "toModel": "gpt-5.6-luna",
            "reason": "capacity",
        }

        def usage(response_id: str, multiple: int) -> dict[str, Any]:
            response = {
                "inputTokens": 10,
                "cachedInputTokens": 4,
                "cacheWriteInputTokens": 0,
                "outputTokens": 2,
                "reasoningOutputTokens": 1,
                "totalTokens": 12,
            }
            return {
                "threadId": "executor",
                "turnId": "repeat-reroute-turn",
                "serviceTier": "standard",
                "responseId": response_id,
                "tokenUsage": {
                    "last": response,
                    "total": {key: value * multiple for key, value in response.items()},
                },
            }

        first = usage("r1", 1)
        await self.controller._queue_runtime_event("model/rerouted", reroute)
        await self.controller._queue_runtime_event("model/rerouted", reroute)
        await self.controller._queue_runtime_event("thread/tokenUsage/updated", first)
        await self.controller._queue_runtime_event("model/rerouted", reroute)
        await self.controller._queue_runtime_event("thread/tokenUsage/updated", first)
        await self.controller._queue_runtime_event("model/rerouted", reroute)
        await self.controller._queue_runtime_event(
            "thread/tokenUsage/updated", usage("r2", 2)
        )

        contributions = self.controller.store.rows(
            """SELECT id, effective_model, coverage FROM cost_contributions
               WHERE action_id = ? AND contribution_kind = 'model_response'
               ORDER BY response_sequence""",
            (action_id,),
        )
        reroutes = self.controller.store.rows(
            "SELECT contribution_id FROM model_reroutes ORDER BY id"
        )
        self.assertEqual(len(contributions), 2)
        self.assertEqual(
            [row["effective_model"] for row in contributions],
            ["gpt-5.6-luna", "gpt-5.6-luna"],
        )
        self.assertEqual([row["coverage"] for row in contributions], ["complete"] * 2)
        self.assertEqual(
            [row["contribution_id"] for row in reroutes],
            [contributions[0]["id"], contributions[1]["id"]],
        )
        report = self.controller.store.cost_report(action_id=action_id)["groups"][0]
        self.assertEqual(report["coverage"], "complete")
        self.assertEqual(report["attributed"]["response_count"], 2)
        self.assertEqual(
            {source["model"] for source in report["rate_card_provenance"]},
            {"gpt-5.6-luna"},
        )
        self.assertFalse(self.controller.advance_requested.is_set())

    async def test_terminal_usage_summary_retains_disconnect_gap_and_numeric_counts(
        self,
    ) -> None:
        now = "2026-01-01T00:00:00Z"
        action_id = int(
            self.controller.store.execute(
                """INSERT INTO actions(task_id, assignment_id, kind, payload,
                       state, native_turn_id, created_at, updated_at)
                   VALUES (?, ?, 'implement', '{}', 'active', 'terminal-turn', ?, ?)""",
                (self.executor["id"], self.assignment["id"], now, now),
            ).lastrowid
        )
        self.controller.store.bind_action_turn(action_id, "executor", "terminal-turn")
        await self.controller._queue_runtime_event(
            "thread/tokenUsage/updated",
            {
                "threadId": "executor",
                "turnId": "terminal-turn",
                "tokenUsage": {
                    "total": {
                        "inputTokens": 12,
                        "cachedInputTokens": 6,
                        "cacheWriteInputTokens": 0,
                        "outputTokens": 3,
                        "reasoningOutputTokens": 2,
                        "totalTokens": 15,
                    }
                },
            },
        )
        await self.controller._handle_runtime_event(
            "fulcrum/runtime/disconnected", {"error": "socket lost"}
        )
        runtime = AsyncMock()
        runtime.read_thread.return_value = {
            "id": "executor",
            "name": self.executor["title"],
            "projectId": "codex-p",
            "status": {"type": "idle"},
            "turns": [{"id": "terminal-turn", "status": "completed", "items": []}],
        }
        self.controller.runtime = runtime
        with patch.object(self.controller, "_dispatch_action", new=AsyncMock()):
            changed = await self.controller._handle_runtime_event(
                "turn/completed",
                {
                    "threadId": "executor",
                    "turn": {"id": "terminal-turn", "status": "completed"},
                },
            )
        self.assertTrue(changed)
        usage = self.controller.store.row(
            "SELECT * FROM action_turn_usage WHERE native_turn_id = 'terminal-turn'"
        )
        self.assertEqual(usage["coverage"], "partial")
        self.assertIn("socket lost", usage["gap_reason"])
        event = self.controller.store.row(
            """SELECT * FROM events WHERE kind = 'action_usage_finalized'
               AND entity_id = ?""",
            (str(action_id),),
        )
        detail = json.loads(event["detail_json"])
        self.assertEqual(detail["direct_total_tokens"], 15)
        self.assertIsInstance(detail["direct_total_tokens"], int)

    async def _assert_reboot_finalizes_terminal_usage(
        self, mode: str, *, with_usage: bool
    ) -> None:
        now = "2026-01-01T00:00:00Z"
        action_id = int(
            self.controller.store.execute(
                """INSERT INTO actions(task_id, assignment_id, kind, payload,
                       state, native_turn_id, created_at, updated_at)
                   VALUES (?, ?, 'implement', '{}', 'active', 'reboot-turn', ?, ?)""",
                (self.executor["id"], self.assignment["id"], now, now),
            ).lastrowid
        )
        self.controller.store.bind_action_turn(action_id, "executor", "reboot-turn")
        if with_usage:
            self.controller.store.observe_turn_usage(
                {
                    "threadId": "executor",
                    "turnId": "reboot-turn",
                    "tokenUsage": {
                        "total": {
                            "inputTokens": 8,
                            "cachedInputTokens": 4,
                            "cacheWriteInputTokens": 0,
                            "outputTokens": 2,
                            "reasoningOutputTokens": 1,
                            "totalTokens": 10,
                        }
                    },
                }
            )

        executor_reads = 0

        async def read_thread(thread_id: str) -> dict[str, Any]:
            nonlocal executor_reads
            task = self.controller.store.row(
                "SELECT title FROM tasks WHERE native_thread_id = ?", (thread_id,)
            )
            assert task is not None
            if thread_id == "executor":
                executor_reads += 1
                running = mode == "hard" and executor_reads == 1
                return {
                    "id": thread_id,
                    "name": task["title"],
                    "projectId": "codex-p",
                    "status": {"type": "active" if running else "idle"},
                    "turns": [
                        {
                            "id": "reboot-turn",
                            "status": "inProgress" if running else "completed",
                            "items": [],
                        }
                    ],
                }
            return {
                "id": thread_id,
                "name": task["title"],
                "projectId": "codex-p",
                "status": {"type": "idle"},
                "turns": [],
            }

        runtime = AsyncMock()
        runtime.ready = True
        runtime.read_thread.side_effect = read_thread
        self.controller.runtime = runtime
        with (
            patch.object(
                self.controller,
                "_setup_initialize",
                new=AsyncMock(return_value={"created_tasks": []}),
            ),
            patch.object(self.controller, "_ensure_pair", new=AsyncMock()),
        ):
            result = await self.controller._begin_reboot(mode)
            repeated = await self.controller._begin_reboot(mode)

        self.assertTrue(result["complete"])
        self.assertTrue(repeated["complete"])
        action = self.controller.store.row(
            "SELECT state FROM actions WHERE id = ?", (action_id,)
        )
        self.assertEqual(action["state"], "canceled")
        usage = self.controller.store.row(
            "SELECT total_tokens, coverage, terminal_at FROM action_turn_usage WHERE action_id = ?",
            (action_id,),
        )
        self.assertEqual(usage["total_tokens"], 10 if with_usage else None)
        self.assertEqual(usage["coverage"], "complete" if with_usage else "unavailable")
        self.assertIsNotNone(usage["terminal_at"])
        events = self.controller.store.rows(
            """SELECT detail_json FROM events WHERE kind = 'action_usage_finalized'
               AND entity_id = ?""",
            (str(action_id),),
        )
        self.assertEqual(len(events), 1)
        detail = json.loads(events[0]["detail_json"])
        self.assertEqual(detail["direct_total_tokens"], 10 if with_usage else None)
        self.assertLess(len(events[0]["detail_json"]), 512)
        if mode == "hard":
            runtime.interrupt.assert_awaited_once_with("executor", "reboot-turn")
        else:
            runtime.interrupt.assert_not_awaited()

    async def test_soft_reboot_finalizes_observed_usage_once(self) -> None:
        await self._assert_reboot_finalizes_terminal_usage("soft", with_usage=True)

    async def test_hard_reboot_finalizes_unavailable_usage_once(self) -> None:
        await self._assert_reboot_finalizes_terminal_usage("hard", with_usage=False)

    async def test_refresh_repairs_idle_runtime_without_current_action(self) -> None:
        self.controller.store.execute(
            "UPDATE tasks SET state = 'active' WHERE id = ?", (self.executor["id"],)
        )
        runtime = AsyncMock()
        runtime.read_thread.return_value = {
            "id": "executor",
            "name": self.executor["title"],
            "projectId": "codex-p",
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

    async def test_context_recovers_full_current_action_after_delta_dispatch(
        self,
    ) -> None:
        earlier = self.controller.store.execute(
            """INSERT INTO actions(
                   task_id, assignment_id, kind, payload, state, created_at, updated_at
               ) VALUES (?, ?, 'implement', '{}', 'processed', 'earlier', 'earlier')""",
            (self.executor["id"], self.assignment["id"]),
        )
        self.controller.store.execute(
            "UPDATE actions SET kind = 'review', outcome_kind = 'changes_requested' WHERE id = ?",
            (earlier.lastrowid,),
        )
        self.controller.store.execute(
            """INSERT INTO handoffs(
                   assignment_id, source_action_id, kind, content_json, created_at
               ) VALUES (?, ?, 'review_findings', ?, 'earlier')""",
            (
                self.assignment["id"],
                earlier.lastrowid,
                json.dumps("repair the boundary check"),
            ),
        )
        current = self.controller.store.execute(
            """INSERT INTO actions(
                   task_id, assignment_id, kind, payload, state, created_at, updated_at
               ) VALUES (?, ?, 'correct', ?, 'active', 'now', 'now')""",
            (
                self.executor["id"],
                self.assignment["id"],
                "{}",
            ),
        )
        action = self.controller.store.row(
            "SELECT * FROM actions WHERE id = ?", (current.lastrowid,)
        )
        assignment = self.controller.store.row(
            "SELECT * FROM assignments WHERE id = ?", (self.assignment["id"],)
        )

        dispatched = self.controller._build_action_message(
            action, self.executor, assignment
        )
        self.controller.store.execute(
            "UPDATE actions SET reminder_sent = 1 WHERE id = ?",
            (current.lastrowid,),
        )
        recovered = await self.controller.handle_request(
            {"command": "context", "thread_id": "executor"}
        )

        self.assertNotIn("Scope", dispatched)
        self.assertIn("scope is unchanged", dispatched)
        self.assertEqual(recovered["role"], "executor")
        self.assertEqual(recovered["action_kind"], "correct")
        self.assertEqual(recovered["action_id"], current.lastrowid)
        self.assertIn("Scope", recovered["context"])
        self.assertIn("repair the boundary check", recovered["context"])
        self.assertNotIn("previous turn ended", recovered["context"])

    async def test_context_rejects_an_actionless_thread(self) -> None:
        with self.assertRaisesRegex(StoreError, "no current Fulcrum action"):
            await self.controller.handle_request(
                {"command": "context", "thread_id": "overseer"}
            )

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
        self.controller.store.event(
            "reconciliation_started",
            "housekeeping noise",
            now="2026-02-01T00:00:00Z",
        )
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
        self.assertIn(
            {"kind": "reconciliation_started", "count": 1},
            evidence["event_counts"],
        )
        self.assertNotIn("housekeeping noise", json.dumps(evidence["recent_events"]))
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
        self.controller._reactivate_deferred_batches(recovery_only=True)
        self.assertEqual(
            self.controller.store.row(
                "SELECT state FROM updates WHERE id = ?", (update.lastrowid,)
            )["state"],
            "batched",
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

    async def test_archon_succession_adopts_target_observed_thread_after_restart(
        self,
    ) -> None:
        old = self.controller.store.register_task(
            native_thread_id="observed-old-archon",
            role="archon",
            description="Fleet",
            model="sol",
            reasoning_effort="high",
            project_id="p",
        )
        self.controller.store.execute(
            "UPDATE tasks SET state = 'archived', archived = 1 WHERE id = ?",
            (old["id"],),
        )
        self.controller.store.execute(
            "INSERT INTO meta(key, value) VALUES ('archon_succession_request', ?)",
            (
                json.dumps(
                    {
                        "task_id": old["id"],
                        "successor_model": "sol",
                        "successor_reasoning_effort": "high",
                        "reason": "restart observation",
                    }
                ),
            ),
        )
        runtime = AsyncMock()
        runtime.ready = True
        runtime.create_thread.side_effect = AppServerError(
            "connection ended after successor creation"
        )
        self.controller.runtime = runtime

        with self.assertRaises(AppServerError):
            await self.controller._process_archon_succession()
        operation = self.controller.store.row(
            "SELECT * FROM external_operations WHERE kind = 'thread_start' AND target = 'archon'"
        )
        inputs = json.loads(operation["input_json"])
        self.assertEqual(inputs["succession_task_id"], old["id"])

        database = self.controller.store.path
        event_log = self.controller.store.event_log
        self.controller.store.close()
        self.controller.store = Store(database, event_log=event_log)
        created_at = datetime.fromisoformat(
            operation["created_at"].replace("Z", "+00:00")
        )
        runtime.list_threads.return_value = [
            {
                "id": "target-observed-successor",
                "model": "sol",
                "projectId": "codex-p",
                "createdAt": int(created_at.timestamp()),
                "turns": [],
            }
        ]
        runtime.set_name.return_value = None

        await self.controller.reconcile()
        completed = self.controller.store.row(
            "SELECT * FROM external_operations WHERE id = ?", (operation["id"],)
        )
        successor = self.controller.store.row(
            "SELECT * FROM tasks WHERE native_thread_id = 'target-observed-successor'"
        )
        self.assertEqual(completed["state"], "complete")
        self.assertEqual(completed["reconciliation_used"], 1)
        self.assertNotIn("operator_resolution", json.loads(completed["result_json"]))

        await self.controller.advance()

        self.assertIsNone(
            self.controller.store.row(
                "SELECT * FROM meta WHERE key = 'archon_succession_request'"
            )
        )
        completion = self.controller.store.row(
            "SELECT * FROM updates WHERE identity = ?",
            (f"succession:{old['id']}:{successor['id']}",),
        )
        self.assertIsNotNone(completion)
        self.assertEqual(completion["state"], "processed")
        self.assertEqual(
            json.loads(completion["content"])["successor_task_id"], successor["id"]
        )
        self.assertEqual(
            self.controller.store.row(
                """SELECT COUNT(*) AS count FROM events
                   WHERE kind = 'completion_acknowledged' AND entity_id = ?""",
                (completion["id"],),
            )["count"],
            1,
        )

    async def test_archon_succession_still_rejects_unrelated_current_archon(
        self,
    ) -> None:
        old = self.controller.store.register_task(
            native_thread_id="unrelated-old-archon",
            role="archon",
            description="Fleet",
            model="sol",
            reasoning_effort="high",
            project_id="p",
        )
        self.controller.store.execute(
            "UPDATE tasks SET state = 'archived', archived = 1 WHERE id = ?",
            (old["id"],),
        )
        self.controller.store.register_task(
            native_thread_id="unrelated-current-archon",
            role="archon",
            description="Unrelated",
            model="sol",
            reasoning_effort="high",
            project_id="p",
        )
        self.controller.store.execute(
            "INSERT INTO meta(key, value) VALUES ('archon_succession_request', ?)",
            (
                json.dumps(
                    {
                        "task_id": old["id"],
                        "successor_model": "sol",
                        "successor_reasoning_effort": "high",
                        "reason": "must remain exact",
                    }
                ),
            ),
        )

        with self.assertRaisesRegex(StoreError, "unexpected current Archon"):
            await self.controller._process_archon_succession()

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

    async def test_mismatched_tested_tree_reaches_review_with_exact_source_evidence(
        self,
    ) -> None:
        def git(*arguments: str) -> str:
            result = subprocess.run(
                ["git", *arguments],
                cwd=self.worktree,
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout.strip()

        git("init", "--quiet")
        git("config", "user.name", "Fulcrum Test")
        git("config", "user.email", "fulcrum@example.invalid")
        source_file = self.worktree / "controller.py"
        source_file.write_text("value = 'tested'\n", encoding="utf-8")
        git("add", "controller.py")
        git("commit", "--quiet", "-m", "tested revision")
        tested_oid = git("rev-parse", "HEAD")
        source_file.write_text("value = 'source'\n", encoding="utf-8")
        git("add", "controller.py")
        git("commit", "--quiet", "-m", "source revision")
        source_oid = git("rev-parse", "HEAD")
        self.assertNotEqual(
            git("rev-parse", f"{source_oid}^{{tree}}"),
            git("rev-parse", f"{tested_oid}^{{tree}}"),
        )

        action = self.controller.store.execute(
            """INSERT INTO actions(
                   task_id, assignment_id, kind, payload, state, native_turn_id,
                   created_at, updated_at
               ) VALUES (?, ?, 'implement', '{}', 'active', 'implement-turn',
                         'now', 'now')""",
            (self.executor["id"], self.assignment["id"]),
        )
        evidence_path = self.paths.state_root.parent / "implementation.md"
        evidence_path.write_text(
            "Implementation is committed; controller validation is required.\n",
            encoding="utf-8",
        )
        accept_finish(
            self.controller.store,
            native_thread_id="executor",
            outcome_kind="ready_for_review",
            options={"evidence": str(evidence_path)},
        )
        self.controller.tollgate = FakeCandidateTollgate(
            str(self.worktree),
            source_oid=source_oid,
            tested_oid=tested_oid,
        )  # type: ignore[assignment]

        captured = await self.controller._capture_submitted_candidate(
            int(self.assignment["id"])
        )

        self.assertTrue(captured)
        retained_action = self.controller.store.row(
            "SELECT * FROM actions WHERE id = ?", (action.lastrowid,)
        )
        exact_validation = json.loads(retained_action["outcome_payload"])[
            "exact_source_validation"
        ]
        self.assertEqual(exact_validation["command"], ["true"])
        self.assertEqual(exact_validation["exit_status"], 0)
        self.assertEqual(exact_validation["source_before"], source_oid)
        self.assertEqual(exact_validation["source_after"], source_oid)
        self.assertTrue(exact_validation["source_unchanged"])
        self.assertTrue(exact_validation["passed"])
        artifact_path = Path(exact_validation["artifact_path"])
        self.assertTrue(artifact_path.is_file())
        self.assertEqual(
            json.loads(artifact_path.read_text(encoding="utf-8"))["source_revision"],
            source_oid,
        )

        self.controller.store.execute(
            """UPDATE tasks SET last_turn_terminal = 1, helpers_terminal = 1
               WHERE id = ?""",
            (self.executor["id"],),
        )
        processed = observe_action_terminal(
            self.controller.store, int(action.lastrowid)
        )
        self.assertTrue(processed["advanced"])
        self.assertEqual(processed["stage"], "review_pending")
        handoff = self.controller.store.row(
            "SELECT * FROM handoffs WHERE source_action_id = ?", (action.lastrowid,)
        )
        retained_evidence = json.loads(handoff["content_json"])
        self.assertEqual(retained_evidence["exact_source_validation"], exact_validation)

        self.controller.store.execute(
            "UPDATE tasks SET runtime_status = 'unmaterialized' WHERE id = ?",
            (self.overseer["id"],),
        )
        runtime = AsyncMock()
        runtime.ready = True
        runtime.start_turn.return_value = "review-turn"
        self.controller.runtime = runtime
        assignment = self.controller.store.row(
            """SELECT a.*, r.project_id FROM assignments a JOIN runs r ON r.id = a.run_id
               WHERE a.id = ?""",
            (self.assignment["id"],),
        )

        await self.controller._start_assignment_action(assignment)

        review = self.controller.store.row(
            "SELECT * FROM actions WHERE assignment_id = ? AND kind = 'review'",
            (self.assignment["id"],),
        )
        self.assertEqual(review["state"], "active")
        self.assertEqual(review["native_turn_id"], "review-turn")
        review_prompt = runtime.start_turn.await_args.args[1]
        self.assertIn(str(artifact_path), review_prompt)
        self.assertIn(source_oid, review_prompt)
        self.assertIn(tested_oid, review_prompt)
        self.assertEqual(
            self.controller.store.rows(
                "SELECT id FROM actions WHERE assignment_id = ? AND kind = 'correct'",
                (self.assignment["id"],),
            ),
            [],
        )

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

    def test_delivery_classification_respects_the_irreversible_boundary(self) -> None:
        cases = [
            (
                {
                    "state": "promoted",
                    "remote_state": "synchronized",
                    "cleanup_state": "pending",
                    "certificate_id": "certificate-1",
                },
                True,
                DeliveryDisposition.POST_PROMOTION_PENDING,
            ),
            (
                {
                    "state": "promoted-local-push-pending",
                    "remote_state": "pushing",
                    "cleanup_state": "pending",
                    "certificate_id": "certificate-1",
                },
                True,
                DeliveryDisposition.POST_PROMOTION_PENDING,
            ),
            (
                {
                    "state": "externally-integrated",
                    "remote_state": "disabled",
                    "cleanup_state": "not-eligible",
                    "certificate_id": "certificate-1",
                },
                False,
                DeliveryDisposition.SATISFIED,
            ),
            (
                {"state": "failed"},
                False,
                DeliveryDisposition.SOURCE_FAILED,
            ),
        ]
        for candidate, remote_enabled, expected in cases:
            with self.subTest(state=candidate["state"]):
                classified = _classify_delivery_status(
                    candidate,
                    {"configuration": {"remote_enabled": remote_enabled}},
                )
                self.assertEqual(classified.disposition, expected)

    async def test_promoted_cleanup_pending_reconciles_without_duplicate_effects(
        self,
    ) -> None:
        pending = {
            "state": "promoted",
            "remote_state": "synchronized",
            "cleanup_state": "pending",
            "certificate_id": "certificate-1",
        }
        completed = {**pending, "cleanup_state": "completed"}
        tollgate = SequencedDeliveryTollgate(pending, pending, pending, completed)
        beads = FakeBeads()
        self.controller.tollgate = tollgate  # type: ignore[assignment]
        self.controller.beads = beads  # type: ignore[assignment]
        assignment = self._prepare_delivery()

        await self.controller._deliver(assignment)
        self.controller.runtime.ready = True
        self.controller.starts_enabled = True
        with (
            patch.object(self.controller, "_update_readiness"),
            patch.object(
                self.controller,
                "_refresh_delivery_pair",
                new=AsyncMock(),
            ),
        ):
            await self.controller.advance()
            await self.controller.advance()

        waiting = self._delivery_assignment()
        operation = self.controller.store.row(
            "SELECT * FROM external_operations WHERE kind = 'tollgate_approve'"
        )
        self.assertEqual(waiting["stage"], "delivering")
        self.assertIn("cleanup is pending", waiting["condition"])
        self.assertEqual(operation["state"], "complete")
        self.assertEqual(tollgate.approved, ["candidate-1"])
        self.assertEqual(beads.closed, [])
        self.assertEqual(
            self.controller.store.rows(
                "SELECT * FROM actions WHERE assignment_id = ? AND kind = 'correct'",
                (self.assignment["id"],),
            ),
            [],
        )

        archon = self.controller.store.register_task(
            native_thread_id="archon-delivery",
            role="archon",
            description="Fleet",
            model="sol",
            reasoning_effort="high",
        )
        await self.controller.reconcile()
        await self.controller.reconcile()

        completed_assignment = self._delivery_assignment()
        self.assertEqual(completed_assignment["stage"], "completed")
        self.assertEqual(tollgate.approved, ["candidate-1"])
        self.assertEqual(beads.closed, ["p-1"])
        self.assertEqual(
            self.controller.store.row(
                "SELECT COUNT(*) AS count FROM external_operations WHERE kind = 'tollgate_approve'"
            )["count"],
            1,
        )
        self.assertEqual(
            self.controller.store.row(
                "SELECT COUNT(*) AS count FROM external_operations WHERE kind = 'beads_close'"
            )["count"],
            1,
        )
        self.assertEqual(
            self.controller.store.row(
                "SELECT COUNT(*) AS count FROM updates WHERE recipient_task_id = ? AND identity = ?",
                (archon["id"], f"completion:{self.assignment['id']}"),
            )["count"],
            1,
        )
        self.assertEqual(
            self.controller.store.row(
                "SELECT COUNT(*) AS count FROM obligations WHERE kind = 'archive'"
            )["count"],
            2,
        )
        self.assertEqual(
            self.controller.store.row(
                """SELECT COUNT(*) AS count FROM state_transitions
                   WHERE entity_type = 'assignment' AND entity_id = ?
                     AND to_state = 'completed'""",
                (str(self.assignment["id"]),),
            )["count"],
            1,
        )

    async def test_successful_approval_survives_status_failures_and_restart(
        self,
    ) -> None:
        completed = {
            "state": "promoted",
            "remote_state": "synchronized",
            "cleanup_state": "completed",
            "certificate_id": "certificate-1",
        }
        tollgate = TransientStatusDeliveryTollgate(3, completed)
        beads = FakeBeads()
        self.controller.tollgate = tollgate  # type: ignore[assignment]
        self.controller.beads = beads  # type: ignore[assignment]
        assignment = self._prepare_delivery()
        archon = self.controller.store.register_task(
            native_thread_id="archon-status-recovery",
            role="archon",
            description="Fleet",
            model="sol",
            reasoning_effort="high",
        )

        await self.controller._deliver(assignment)

        operation = self.controller.store.row(
            "SELECT * FROM external_operations WHERE kind = 'tollgate_approve'"
        )
        attempt = self.controller.store.row(
            "SELECT * FROM operation_attempts WHERE operation_id = ?",
            (operation["id"],),
        )
        waiting = self._delivery_assignment()
        self.assertEqual(operation["state"], "complete")
        self.assertEqual(operation["attempt_count"], 1)
        self.assertIn('"approval"', operation["result_json"])
        self.assertEqual(attempt["state"], "complete")
        self.assertEqual(waiting["stage"], "delivering")
        self.assertIn("status is unavailable", waiting["condition"])
        self.assertEqual(tollgate.approved, ["candidate-1"])

        self.controller.runtime.ready = True
        self.controller.starts_enabled = True
        with (
            patch.object(self.controller, "_update_readiness"),
            patch.object(
                self.controller,
                "_refresh_delivery_pair",
                new=AsyncMock(),
            ),
        ):
            await self.controller.advance()
            await self.controller.advance()

        waiting = self._delivery_assignment()
        self.assertEqual(waiting["stage"], "delivering")
        self.assertIn("status is unavailable", waiting["condition"])
        self.assertEqual(tollgate.approved, ["candidate-1"])
        self.assertEqual(
            self.controller.store.row(
                "SELECT COUNT(*) AS count FROM external_operations WHERE kind = 'tollgate_approve'"
            )["count"],
            1,
        )

        self._restart_controller()
        self.controller.tollgate = tollgate  # type: ignore[assignment]
        self.controller.beads = beads  # type: ignore[assignment]
        self.controller.runtime.ready = True
        await self.controller.reconcile()
        await self.controller.reconcile()

        completed_assignment = self._delivery_assignment()
        self.assertEqual(completed_assignment["stage"], "completed")
        self.assertEqual(tollgate.approved, ["candidate-1"])
        self.assertEqual(tollgate.status_calls, 4)
        self.assertEqual(beads.closed, ["p-1"])
        self.assertEqual(
            self.controller.store.row(
                "SELECT COUNT(*) AS count FROM external_operations WHERE kind = 'tollgate_approve'"
            )["count"],
            1,
        )
        self.assertEqual(
            self.controller.store.row(
                "SELECT COUNT(*) AS count FROM external_operations WHERE kind = 'beads_close'"
            )["count"],
            1,
        )
        self.assertEqual(
            self.controller.store.row(
                "SELECT COUNT(*) AS count FROM updates WHERE recipient_task_id = ? AND identity = ?",
                (archon["id"], f"completion:{self.assignment['id']}"),
            )["count"],
            1,
        )
        self.assertEqual(
            self.controller.store.row(
                "SELECT COUNT(*) AS count FROM obligations WHERE kind = 'archive'"
            )["count"],
            2,
        )

    async def test_remote_and_certificate_pending_remain_in_delivery(self) -> None:
        cases = [
            (
                {
                    "state": "promoted",
                    "remote_state": "pushing",
                    "cleanup_state": "pending",
                    "certificate_id": "certificate-1",
                },
                "synchronization is pending",
            ),
            (
                {
                    "state": "promoted-local-push-pending",
                    "remote_state": "pushing",
                    "cleanup_state": "pending",
                    "certificate_id": "certificate-1",
                },
                "synchronization is pending",
            ),
            (
                {
                    "state": "promoted",
                    "remote_state": "synchronized",
                    "cleanup_state": "completed",
                    "certificate_id": None,
                },
                "certificate finalization is pending",
            ),
        ]
        for candidate, condition in cases:
            with self.subTest(condition=condition):
                assignment = self._prepare_delivery()
                self.controller.store.execute(
                    "DELETE FROM external_operations WHERE kind = 'tollgate_approve'"
                )
                tollgate = SequencedDeliveryTollgate(candidate)
                beads = FakeBeads()
                self.controller.tollgate = tollgate  # type: ignore[assignment]
                self.controller.beads = beads  # type: ignore[assignment]

                await self.controller._deliver(assignment)

                waiting = self._delivery_assignment()
                self.assertEqual(waiting["stage"], "delivering")
                self.assertIn(condition, waiting["condition"])
                self.assertEqual(tollgate.approved, ["candidate-1"])
                self.assertEqual(beads.closed, [])
                self.assertEqual(
                    self.controller.store.row(
                        "SELECT state FROM external_operations WHERE kind = 'tollgate_approve'"
                    )["state"],
                    "complete",
                )

    async def test_post_promotion_attention_holds_without_correction(self) -> None:
        cases = [
            {
                "state": "promoted",
                "remote_state": "synchronized",
                "cleanup_state": "needs-attention",
                "certificate_id": "certificate-1",
            },
            {
                "state": "promoted-local-push-pending",
                "remote_state": "push-blocked",
                "cleanup_state": "pending",
                "certificate_id": "certificate-1",
            },
        ]
        for candidate in cases:
            with self.subTest(candidate=candidate):
                assignment = self._prepare_delivery()
                self.controller.store.execute("DELETE FROM holds")
                self.controller.store.execute(
                    "DELETE FROM external_operations WHERE kind = 'tollgate_approve'"
                )
                tollgate = SequencedDeliveryTollgate(candidate, candidate)
                self.controller.tollgate = tollgate  # type: ignore[assignment]
                self.controller.beads = FakeBeads()  # type: ignore[assignment]

                await self.controller._deliver(assignment)
                await self.controller._reconcile_uncertain_operations()

                held = self._delivery_assignment()
                self.assertEqual(held["stage"], "recovering")
                self.assertIsNotNone(held["operator_hold_id"])
                self.assertEqual(
                    self.controller.store.row(
                        "SELECT COUNT(*) AS count FROM holds WHERE released_at IS NULL"
                    )["count"],
                    1,
                )
                self.assertEqual(tollgate.approved, ["candidate-1"])
                self.assertEqual(
                    self.controller.store.rows(
                        "SELECT * FROM actions WHERE assignment_id = ? AND kind = 'correct'",
                        (self.assignment["id"],),
                    ),
                    [],
                )

    async def test_terminal_delivery_states_retain_diagnosis_and_correct(self) -> None:
        for state in ("failed", "canceled", "merge-conflict"):
            with self.subTest(state=state):
                assignment = self._prepare_delivery()
                self.controller.store.execute(
                    "DELETE FROM external_operations WHERE kind = 'tollgate_approve'"
                )
                tollgate = TerminalDeliveryTollgate(state)
                self.controller.tollgate = tollgate  # type: ignore[assignment]

                await self.controller._deliver(assignment)

                correcting = self._delivery_assignment()
                operation = self.controller.store.row(
                    "SELECT * FROM external_operations WHERE kind = 'tollgate_approve'"
                )
                self.assertEqual(correcting["stage"], "correcting")
                self.assertEqual(
                    correcting["condition"], f"Tollgate candidate ended in {state}"
                )
                self.assertEqual(operation["state"], "failed")
                self.assertIn('"diagnosis": "retained"', operation["result_json"])
                self.assertEqual(tollgate.diagnosed, ["candidate-1"])

    async def test_restart_adopts_effected_approval_without_reauthorizing(self) -> None:
        assignment = self._prepare_delivery()
        operation_id = self.controller.store.create_operation(
            "tollgate_approve", "candidate-1", {"repository_id": "tg-p"}
        )
        self.controller.store.begin_operation_attempt(operation_id)
        pending = {
            "state": "promoted",
            "remote_state": "synchronized",
            "cleanup_state": "pending",
            "certificate_id": "certificate-1",
        }

        self._restart_controller()
        tollgate = SequencedDeliveryTollgate(pending)
        self.controller.tollgate = tollgate  # type: ignore[assignment]
        self.controller.beads = FakeBeads()  # type: ignore[assignment]
        self.controller._adopt_stranded_operations()
        await self.controller._reconcile_uncertain_operations()
        await self.controller._reconcile_uncertain_operations()

        retained = self.controller.store.row(
            "SELECT * FROM external_operations WHERE id = ?", (operation_id,)
        )
        waiting = self._delivery_assignment()
        self.assertEqual(retained["state"], "complete")
        self.assertEqual(retained["reconciliation_used"], 1)
        self.assertEqual(waiting["stage"], "delivering")
        self.assertIn("cleanup is pending", waiting["condition"])
        self.assertEqual(tollgate.approved, [])
        self.assertEqual(
            self.controller.store.rows(
                "SELECT * FROM actions WHERE assignment_id = ? AND kind = 'correct'",
                (self.assignment["id"],),
            ),
            [],
        )

    async def test_restart_repairs_legacy_cleanup_pending_misclassification(
        self,
    ) -> None:
        self._prepare_delivery()
        operation_id = self.controller.store.create_operation(
            "tollgate_approve", "candidate-1", {"repository_id": "tg-p"}
        )
        attempt = self.controller.store.begin_operation_attempt(operation_id)
        pending = {
            "id": "candidate-1",
            "state": "promoted",
            "remote_state": "synchronized",
            "cleanup_state": "pending",
            "certificate_id": "certificate-1",
        }
        self.controller.store.finish_operation_attempt(
            operation_id,
            attempt,
            state="failed",
            result={"status": {"candidate": {"item": pending}}},
            error="candidate worktree cleanup is incomplete",
            native_id="candidate-1",
        )
        self.controller.store.execute(
            """UPDATE assignments SET prior_stage = 'delivering', stage = 'correcting',
               condition = 'candidate worktree cleanup is incomplete' WHERE id = ?""",
            (self.assignment["id"],),
        )

        self._restart_controller()
        tollgate = SequencedDeliveryTollgate(pending)
        self.controller.tollgate = tollgate  # type: ignore[assignment]
        await self.controller._reconcile_uncertain_operations()

        operation = self.controller.store.row(
            "SELECT * FROM external_operations WHERE id = ?", (operation_id,)
        )
        assignment = self._delivery_assignment()
        self.assertEqual(operation["state"], "complete")
        self.assertEqual(assignment["stage"], "delivering")
        self.assertIn("cleanup is pending", assignment["condition"])
        self.assertEqual(tollgate.approved, [])
        self.assertEqual(
            self.controller.store.rows(
                "SELECT * FROM actions WHERE assignment_id = ? AND kind = 'correct'",
                (self.assignment["id"],),
            ),
            [],
        )

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

    async def test_completed_child_work_can_deliver_a_no_code_parent(self) -> None:
        approved_scope = (
            "File two independent copy-edit tasks; the child assignments own all "
            "repository changes."
        )
        self.controller.store.execute(
            "UPDATE beads SET description = ? WHERE bead_id = 'p-1'",
            (approved_scope,),
        )
        self.controller.store.execute(
            "UPDATE run_beads SET scope_snapshot = ? WHERE run_id = ?",
            (approved_scope, self.assignment["run_id"]),
        )
        self.controller.store.execute(
            "UPDATE assignments SET scope_snapshot = ? WHERE id = ?",
            (approved_scope, self.assignment["id"]),
        )
        subprocess.run(["git", "init", "-q"], cwd=self.worktree, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=self.worktree,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Fulcrum Test"],
            cwd=self.worktree,
            check=True,
        )
        (self.worktree / "README.md").write_text("baseline\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.worktree, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "test: establish baseline"],
            cwd=self.worktree,
            check=True,
        )
        parent_head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.worktree, text=True
        ).strip()

        beads = FakeBeads()
        self.controller.beads = beads  # type: ignore[assignment]
        children = file_graph(
            self.controller.store,
            beads,  # type: ignore[arg-type]
            {
                "project": "p",
                "tasks": [
                    {
                        "intake_key": "parent:first-copy-edit",
                        "title": "First copy edit",
                        "description": "Apply and verify the first independent copy edit.",
                    },
                    {
                        "intake_key": "parent:second-copy-edit",
                        "title": "Second copy edit",
                        "description": "Apply and verify the second independent copy edit.",
                    },
                ],
            },
            group_id="parent:children",
        )["bead_ids"]
        self.assertEqual(children, ["p-child-1", "p-child-2"])

        action = self.controller.store.execute(
            """INSERT INTO actions(
                   task_id, assignment_id, kind, payload, state, created_at, updated_at
               ) VALUES (?, ?, 'implement', '{}', 'active', ?, ?)""",
            (self.executor["id"], self.assignment["id"], utc_now(), utc_now()),
        )
        self.controller.store.execute(
            """INSERT INTO reservations(
                   action_id, pair_id, project_ids, state, created_at
               ) VALUES (?, ?, '["p"]', 'active', ?)""",
            (action.lastrowid, self.assignment["run_id"], utc_now()),
        )
        accept_finish(
            self.controller.store,
            native_thread_id="executor",
            outcome_kind="blocked",
            options={
                "reason": "The approved result is the two filed child tasks; no repository edit exists."
            },
        )
        observed = observe_action_terminal(self.controller.store, int(action.lastrowid))
        self.assertTrue(observed["advanced"])
        self.assertEqual(
            self.controller.store.row(
                "SELECT stage FROM assignments WHERE id = ?",
                (self.assignment["id"],),
            )["stage"],
            "recovering",
        )

        tollgate = FakeSuccessfulTollgate()
        self.controller.tollgate = tollgate  # type: ignore[assignment]
        for position, child_id in enumerate(children, start=1):
            child_run = apply_archon_decisions(
                self.controller.store,
                {
                    "decisions": [
                        {
                            "decision": "approve",
                            "project": "p",
                            "beads": [child_id],
                        }
                    ]
                },
            )["created_runs"][0]
            child_assignment = self.controller.store.row(
                "SELECT * FROM assignments WHERE run_id = ?", (child_run,)
            )
            candidate_id = f"child-candidate-{position}"
            self.controller.store.execute(
                """UPDATE assignments SET stage = 'delivering', candidate_id = ?,
                   mandate_candidate_id = ?, mandate_scope = scope_snapshot
                   WHERE id = ?""",
                (candidate_id, candidate_id, child_assignment["id"]),
            )
            deliverable = self.controller.store.row(
                """SELECT a.*, r.project_id FROM assignments a
                   JOIN runs r ON r.id = a.run_id WHERE a.id = ?""",
                (child_assignment["id"],),
            )
            await self.controller._deliver(deliverable)

        self.assertEqual(tollgate.approved, ["child-candidate-1", "child-candidate-2"])
        self.assertEqual(
            [
                self.controller.store.row(
                    "SELECT stage FROM assignments WHERE bead_id = ?", (child_id,)
                )["stage"]
                for child_id in children
            ],
            ["completed", "completed"],
        )
        self.assertEqual(
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=self.worktree, text=True
            ).strip(),
            parent_head,
        )
        self.assertEqual(
            subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=self.worktree, text=True
            ),
            "",
        )

        evidence = (
            "Filed p-child-1 and p-child-2; both child assignments completed "
            "after their candidates were promoted. The parent worktree remains clean."
        )
        apply_archon_decisions(
            self.controller.store,
            {
                "decisions": [
                    {
                        "decision": "resolve_escalation",
                        "assignment_id": self.assignment["id"],
                        "resolution": "complete_non_code",
                        "reason": "The parent scope was fulfilled by filing the children.",
                        "evidence": evidence,
                    }
                ]
            },
        )
        parent = self.controller.store.row(
            """SELECT a.*, r.project_id FROM assignments a
               JOIN runs r ON r.id = a.run_id WHERE a.id = ?""",
            (self.assignment["id"],),
        )
        self.assertEqual(parent["stage"], "delivering")
        self.assertEqual(parent["completion_kind"], "non_code")
        self.assertEqual(parent["completion_evidence"], evidence)
        self.assertIsNone(parent["candidate_id"])

        await self.controller._deliver(parent)
        await self.controller._deliver(
            self.controller.store.row(
                """SELECT a.*, r.project_id FROM assignments a
                   JOIN runs r ON r.id = a.run_id WHERE a.id = ?""",
                (self.assignment["id"],),
            )
        )

        completed_parent = self.controller.store.row(
            "SELECT * FROM assignments WHERE id = ?", (self.assignment["id"],)
        )
        self.assertEqual(completed_parent["stage"], "completed")
        self.assertEqual(
            self.controller.store.row(
                "SELECT state FROM runs WHERE id = ?", (self.assignment["run_id"],)
            )["state"],
            "completed",
        )
        self.assertEqual(beads.closed.count("p-1"), 1)
        self.assertEqual(tollgate.approved, ["child-candidate-1", "child-candidate-2"])
        parent_closes = self.controller.store.rows("""SELECT * FROM external_operations
               WHERE kind = 'beads_close' AND target = 'p-1'""")
        self.assertEqual(len(parent_closes), 1)
        self.assertEqual(
            json.loads(parent_closes[0]["input_json"]),
            {"completion_kind": "non_code", "evidence": evidence},
        )
        self.assertIn("without a repository candidate", beads.close_reasons[-1])
        self.assertEqual(
            self.controller.store.row(
                """SELECT COUNT(*) AS count FROM external_operations
                   WHERE kind = 'tollgate_candidate_create' AND target = ?""",
                (str(self.assignment["id"]),),
            )["count"],
            0,
        )
        self.assertEqual(invariant_violations(self.controller.store), [])

    async def test_canceling_recovery_does_not_claim_no_code_completion(self) -> None:
        hold = self.controller.store.execute(
            """INSERT INTO holds(scope, target, reason, urgent, release_condition, created_at)
               VALUES ('assignment', ?, 'work is not complete', 1,
                       'Archon chooses a recovery', ?)""",
            (str(self.assignment["id"]), utc_now()),
        )
        self.controller.store.execute(
            """UPDATE assignments SET prior_stage = stage, stage = 'recovering',
               operator_hold_id = ?, condition = 'work is not complete' WHERE id = ?""",
            (hold.lastrowid, self.assignment["id"]),
        )
        beads = FakeBeads()
        self.controller.beads = beads  # type: ignore[assignment]

        apply_archon_decisions(
            self.controller.store,
            {
                "decisions": [
                    {
                        "decision": "resolve_escalation",
                        "assignment_id": self.assignment["id"],
                        "resolution": "cancel",
                        "reason": "The requested work should not be performed.",
                    }
                ]
            },
        )

        canceled = self.controller.store.row(
            "SELECT * FROM assignments WHERE id = ?", (self.assignment["id"],)
        )
        self.assertEqual(canceled["stage"], "canceled")
        self.assertIsNone(canceled["completion_kind"])
        self.assertIsNone(canceled["completion_evidence"])
        self.assertEqual(
            self.controller.store.row(
                "SELECT state FROM runs WHERE id = ?", (self.assignment["run_id"],)
            )["state"],
            "canceled",
        )
        self.assertEqual(beads.closed, [])
        self.assertIsNone(
            self.controller.store.row(
                "SELECT id FROM external_operations WHERE kind = 'beads_close' AND target = 'p-1'"
            )
        )

    def test_no_code_completion_rejects_missing_evidence_or_a_candidate(self) -> None:
        hold = self.controller.store.execute(
            """INSERT INTO holds(scope, target, reason, urgent, release_condition, created_at)
               VALUES ('assignment', ?, 'needs a completion decision', 1,
                       'Archon chooses a recovery', ?)""",
            (str(self.assignment["id"]), utc_now()),
        )
        self.controller.store.execute(
            """UPDATE assignments SET prior_stage = stage, stage = 'recovering',
               operator_hold_id = ? WHERE id = ?""",
            (hold.lastrowid, self.assignment["id"]),
        )
        decision = {
            "decisions": [
                {
                    "decision": "resolve_escalation",
                    "assignment_id": self.assignment["id"],
                    "resolution": "complete_non_code",
                    "reason": "The approved result needs no source change.",
                }
            ]
        }

        with self.assertRaisesRegex(StoreError, "requires nonempty evidence"):
            apply_archon_decisions(self.controller.store, decision)
        retained = self.controller.store.row(
            "SELECT * FROM assignments WHERE id = ?", (self.assignment["id"],)
        )
        self.assertEqual(retained["stage"], "recovering")
        self.assertEqual(retained["operator_hold_id"], hold.lastrowid)
        self.assertIsNone(
            self.controller.store.row(
                "SELECT released_at FROM holds WHERE id = ?", (hold.lastrowid,)
            )["released_at"]
        )

        decision["decisions"][0]["evidence"] = "The requested investigation is done."
        self.controller.store.execute(
            "UPDATE assignments SET candidate_id = 'candidate-1', source_oid = 'abc' WHERE id = ?",
            (self.assignment["id"],),
        )
        with self.assertRaisesRegex(StoreError, "without a repository candidate"):
            apply_archon_decisions(self.controller.store, decision)
        self.assertEqual(
            self.controller.store.row(
                "SELECT stage FROM assignments WHERE id = ?",
                (self.assignment["id"],),
            )["stage"],
            "recovering",
        )

    def test_assignment_completion_is_one_transaction_at_every_mutation(self) -> None:
        archon = self.controller.store.register_task(
            native_thread_id="archon",
            role="archon",
            description="Fleet",
            model="sol",
            reasoning_effort="high",
        )
        self.controller.store.execute(
            "UPDATE assignments SET stage = 'delivering' WHERE id = ?",
            (self.assignment["id"],),
        )

        for crash_after in range(1, 6):
            with self.subTest(crash_after=crash_after):
                assignment = self.controller.store.row(
                    "SELECT * FROM assignments WHERE id = ?",
                    (self.assignment["id"],),
                )
                assert assignment is not None
                original_execute = self.controller.store.execute
                mutation_count = 0

                def execute_then_crash(
                    sql: str, parameters: tuple[Any, ...] = ()
                ) -> Any:
                    nonlocal mutation_count
                    cursor = original_execute(sql, parameters)
                    mutation_count += 1
                    if mutation_count == crash_after:
                        raise RuntimeError("simulated controller crash")
                    return cursor

                with (
                    patch.object(
                        self.controller.store,
                        "execute",
                        side_effect=execute_then_crash,
                    ),
                    self.assertRaisesRegex(RuntimeError, "simulated controller crash"),
                ):
                    self.controller._record_assignment_completed(assignment)

                self._restart_controller()
                self.assertEqual(
                    self.controller.store.row(
                        "SELECT stage FROM assignments WHERE id = ?",
                        (self.assignment["id"],),
                    ),
                    {"stage": "delivering"},
                )
                self.assertEqual(
                    self.controller.store.row(
                        "SELECT state FROM runs WHERE id = ?",
                        (self.assignment["run_id"],),
                    ),
                    {"state": "active"},
                )
                self.assertIsNone(
                    self.controller.store.row(
                        "SELECT 1 FROM updates WHERE recipient_task_id = ? AND identity = ?",
                        (archon["id"], f"completion:{self.assignment['id']}"),
                    )
                )
                self.assertEqual(
                    self.controller.store.rows(
                        "SELECT * FROM obligations WHERE kind = 'archive'"
                    ),
                    [],
                )

    async def test_restart_repairs_every_assignment_completion_crash_prefix(
        self,
    ) -> None:
        archon = self.controller.store.register_task(
            native_thread_id="archon",
            role="archon",
            description="Fleet",
            model="sol",
            reasoning_effort="high",
        )
        operation_id = self.controller.store.create_operation(
            "beads_close", self.assignment["bead_id"], {"candidate_id": "candidate-1"}
        )
        attempt = self.controller.store.begin_operation_attempt(operation_id)
        self.controller.store.finish_operation_attempt(
            operation_id,
            attempt,
            state="complete",
            result={"closed": True},
            native_id=self.assignment["bead_id"],
        )
        timestamp = "2026-01-02T00:00:00Z"
        pair = (self.executor, self.overseer)
        completion = json.dumps(
            {
                "kind": "assignment_completed",
                "assignment_id": self.assignment["id"],
                "run_id": self.assignment["run_id"],
                "bead_id": self.assignment["bead_id"],
                "candidate_id": None,
                "source_revision": None,
                "tested_revision": None,
            },
            sort_keys=True,
        )

        for completed_mutations in range(1, 6):
            with self.subTest(completed_mutations=completed_mutations):
                self.controller.store.execute(
                    "DELETE FROM obligations WHERE kind = 'archive'"
                )
                self.controller.store.execute(
                    "DELETE FROM updates WHERE identity = ?",
                    (f"completion:{self.assignment['id']}",),
                )
                self.controller.store.execute(
                    "UPDATE runs SET state = 'active' WHERE id = ?",
                    (self.assignment["run_id"],),
                )
                self.controller.store.execute(
                    "UPDATE assignments SET stage = 'delivering' WHERE id = ?",
                    (self.assignment["id"],),
                )
                self.controller.store.execute("DELETE FROM state_transitions")

                mutations = [
                    (
                        "UPDATE assignments SET stage = 'completed' WHERE id = ?",
                        (self.assignment["id"],),
                    ),
                    (
                        """INSERT INTO updates(
                               recipient_task_id, identity, content, actionable,
                               state, created_at, updated_at
                           ) VALUES (?, ?, ?, 1, 'retained', ?, ?)""",
                        (
                            archon["id"],
                            f"completion:{self.assignment['id']}",
                            completion,
                            timestamp,
                            timestamp,
                        ),
                    ),
                    (
                        "UPDATE runs SET state = 'completed' WHERE id = ?",
                        (self.assignment["run_id"],),
                    ),
                    (
                        """INSERT INTO obligations(
                               kind, identity, target, state, created_at, updated_at
                           ) VALUES ('archive', ?, ?, 'pending', ?, ?)""",
                        (
                            str(pair[0]["id"]),
                            pair[0]["native_thread_id"],
                            timestamp,
                            timestamp,
                        ),
                    ),
                    (
                        """INSERT INTO obligations(
                               kind, identity, target, state, created_at, updated_at
                           ) VALUES ('archive', ?, ?, 'pending', ?, ?)""",
                        (
                            str(pair[1]["id"]),
                            pair[1]["native_thread_id"],
                            timestamp,
                            timestamp,
                        ),
                    ),
                ]
                for sql, parameters in mutations[:completed_mutations]:
                    self.controller.store.execute(sql, parameters)

                before_repair = invariant_violations(self.controller.store)
                if completed_mutations < len(mutations):
                    self.assertTrue(before_repair)
                else:
                    self.assertEqual(before_repair, [])

                self._restart_controller()
                self.controller.runtime.ready = True
                await self.controller.reconcile()

                self.assertEqual(
                    self.controller.store.row(
                        "SELECT stage FROM assignments WHERE id = ?",
                        (self.assignment["id"],),
                    ),
                    {"stage": "completed"},
                )
                self.assertEqual(
                    self.controller.store.row(
                        "SELECT state FROM runs WHERE id = ?",
                        (self.assignment["run_id"],),
                    ),
                    {"state": "completed"},
                )
                self.assertEqual(
                    self.controller.store.row(
                        "SELECT COUNT(*) AS count FROM updates WHERE identity = ?",
                        (f"completion:{self.assignment['id']}",),
                    ),
                    {"count": 1},
                )
                self.assertEqual(
                    self.controller.store.rows(
                        """SELECT identity, target FROM obligations
                           WHERE kind = 'archive' ORDER BY identity"""
                    ),
                    [
                        {
                            "identity": str(self.executor["id"]),
                            "target": "executor",
                        },
                        {
                            "identity": str(self.overseer["id"]),
                            "target": "overseer",
                        },
                    ],
                )
                self.assertEqual(
                    self.controller.store.row(
                        """SELECT COUNT(*) AS count FROM state_transitions
                           WHERE entity_type = 'assignment' AND entity_id = ?
                             AND to_state = 'completed'""",
                        (str(self.assignment["id"]),),
                    ),
                    {"count": 1},
                )
                self.assertEqual(invariant_violations(self.controller.store), [])

    async def test_json_lines_approval_completes_delivery_without_reconciliation(
        self,
    ) -> None:
        fixture = (
            Path(__file__).parent
            / "fixtures"
            / "tollgate"
            / "approve-operation-13.jsonl"
        ).read_text(encoding="utf-8")
        documents = [json.loads(line) for line in fixture.splitlines()]
        candidate_id = documents[0]["item_id"]
        repository_id = documents[1]["item"]["repository_id"]
        self.controller.store.execute(
            "UPDATE projects SET tollgate_repo_id = ? WHERE project_id = 'p'",
            (repository_id,),
        )
        self.controller.store.execute(
            """UPDATE assignments SET stage = 'delivering', candidate_id = ?,
               mandate_candidate_id = ?, mandate_scope = scope_snapshot
               WHERE id = ?""",
            (candidate_id, candidate_id, self.assignment["id"]),
        )
        assignment = self.controller.store.row(
            """SELECT a.*, r.project_id FROM assignments a JOIN runs r ON r.id = a.run_id
               WHERE a.id = ?""",
            (self.assignment["id"],),
        )
        self.controller.tollgate = Tollgate("/usr/bin/tg")
        beads = FakeBeads()
        self.controller.beads = beads  # type: ignore[assignment]
        completed = [
            subprocess.CompletedProcess(
                args=[], returncode=0, stdout=fixture, stderr=""
            ),
            subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps(documents[-1]), stderr=""
            ),
        ]

        with patch("fulcrum.tollgate.subprocess.run", side_effect=completed):
            await self.controller._deliver(assignment)

        operation = self.controller.store.row(
            "SELECT * FROM external_operations WHERE kind = 'tollgate_approve'"
        )
        attempt = self.controller.store.row(
            "SELECT * FROM operation_attempts WHERE operation_id = ?",
            (operation["id"],),
        )
        self.assertEqual(operation["state"], "complete")
        self.assertEqual(operation["reconciliation_used"], 0)
        self.assertEqual(attempt["state"], "complete")
        self.assertIsNone(attempt["error"])
        self.assertEqual(beads.closed, ["p-1"])

    async def test_terminal_candidate_does_not_require_diagnosis(self) -> None:
        self.controller.store.execute(
            """UPDATE assignments SET stage = 'delivering', candidate_id = 'candidate-1',
               mandate_candidate_id = 'candidate-1', mandate_scope = scope_snapshot
               WHERE id = ?""",
            (self.assignment["id"],),
        )
        operation = self.controller.store.create_operation(
            "tollgate_approve",
            "candidate-1",
            {"repository_id": "tg-p"},
        )
        self.controller.store.execute(
            "UPDATE external_operations SET state = 'uncertain' WHERE id = ?",
            (operation,),
        )
        self.controller.tollgate = FakeTerminalTollgate()  # type: ignore[assignment]

        retained = self.controller.store.row(
            "SELECT * FROM external_operations WHERE id = ?", (operation,)
        )
        await self.controller._reconcile_tollgate_approve(retained)

        retained = self.controller.store.row(
            "SELECT state, result_json FROM external_operations WHERE id = ?",
            (operation,),
        )
        self.assertEqual(retained["state"], "failed")
        self.assertIn("diagnosis_unavailable", retained["result_json"])
        assignment = self.controller.store.row(
            "SELECT stage FROM assignments WHERE id = ?", (self.assignment["id"],)
        )
        self.assertEqual(assignment["stage"], "correcting")

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

    async def test_approved_action_with_stale_runtime_status_delivers_after_native_idle(
        self,
    ) -> None:
        now = "2026-01-01T00:00:00Z"
        self.controller.store.register_task(
            native_thread_id="archon",
            role="archon",
            description="",
            model="sol",
            reasoning_effort="high",
            project_id="p",
        )
        self.controller.store.execute(
            """UPDATE assignments SET stage = 'reviewing', candidate_id = 'candidate-1',
               source_oid = 'source-1', tested_oid = 'tested-1' WHERE id = ?""",
            (self.assignment["id"],),
        )
        implementation = self.controller.store.execute(
            """INSERT INTO actions(
                   task_id, assignment_id, kind, payload, state, outcome_kind,
                   outcome_payload, created_at, updated_at
               ) VALUES (?, ?, 'implement', '{}', 'processed', 'ready_for_review',
                         ?, ?, ?)""",
            (
                self.executor["id"],
                self.assignment["id"],
                json.dumps({"evidence": "/tmp/implementation-evidence.md"}),
                now,
                now,
            ),
        )
        self.controller.store.execute(
            """INSERT INTO handoffs(
                   assignment_id, source_action_id, kind, content_json, created_at
               ) VALUES (?, ?, 'implementation_evidence', ?, ?)""",
            (
                self.assignment["id"],
                implementation.lastrowid,
                json.dumps({"evidence": "/tmp/implementation-evidence.md"}),
                now,
            ),
        )
        review = self.controller.store.execute(
            """INSERT INTO actions(
                   task_id, assignment_id, kind, payload, state, native_turn_id,
                   outcome_kind, outcome_payload, created_at, updated_at
               ) VALUES (?, ?, 'review', '{}', 'active', 'review-turn', 'approved',
                         ?, ?, ?)""",
            (
                self.overseer["id"],
                self.assignment["id"],
                json.dumps(
                    {
                        "assessment": "candidate matches the approved scope",
                        "minor_fixes": [
                            {
                                "problem": "Nonblocking naming inconsistency",
                                "evidence": "module.py:10",
                                "requested_change": "Use the domain term in follow-up work",
                            }
                        ],
                        "repair_permissions": ["bounded_in_scope_ci_fix"],
                    }
                ),
                now,
                now,
            ),
        )
        self.controller.store.execute(
            """UPDATE tasks SET state = 'active', runtime_status = 'active',
               last_turn_terminal = 1, helpers_terminal = 1 WHERE id = ?""",
            (self.overseer["id"],),
        )
        self.controller.store.execute(
            """UPDATE tasks SET state = 'idle', runtime_status = 'idle',
               last_turn_terminal = 1, helpers_terminal = 1 WHERE id = ?""",
            (self.executor["id"],),
        )

        result = observe_action_terminal(self.controller.store, int(review.lastrowid))
        self.assertTrue(result["advanced"])
        approved = self.controller.store.row(
            "SELECT * FROM assignments WHERE id = ?", (self.assignment["id"],)
        )
        self.assertEqual(approved["stage"], "delivering")
        self.assertEqual(approved["mandate_candidate_id"], "candidate-1")
        self.assertEqual(approved["mandate_scope"], approved["scope_snapshot"])
        stale_task = self.controller.store.row(
            "SELECT state, runtime_status FROM tasks WHERE id = ?",
            (self.overseer["id"],),
        )
        self.assertEqual(stale_task, {"state": "idle", "runtime_status": "active"})

        native_status = {"executor": "idle", "overseer": "active"}

        async def read_thread(thread_id: str) -> dict[str, Any]:
            task = self.controller.store.row(
                "SELECT title FROM tasks WHERE native_thread_id = ?", (thread_id,)
            )
            assert task is not None
            return {
                "id": thread_id,
                "name": task["title"],
                "projectId": "codex-p",
                "status": {"type": native_status[thread_id]},
                "turns": [
                    {
                        "id": "review-turn" if thread_id == "overseer" else "impl-turn",
                        "status": "completed",
                        "items": [],
                    }
                ],
            }

        runtime = AsyncMock()
        runtime.ready = True
        runtime.read_thread.side_effect = read_thread
        self.controller.runtime = runtime
        tollgate = FakeSuccessfulTollgate()
        self.controller.tollgate = tollgate  # type: ignore[assignment]
        beads = FakeBeads()
        self.controller.beads = beads  # type: ignore[assignment]

        assignment = self.controller.store.row(
            """SELECT a.*, r.project_id FROM assignments a JOIN runs r ON r.id = a.run_id
               WHERE a.id = ?""",
            (self.assignment["id"],),
        )
        await self.controller._deliver(assignment)

        deferred = self.controller.store.row(
            "SELECT stage, operator_hold_id, mandate_candidate_id, mandate_scope FROM assignments WHERE id = ?",
            (self.assignment["id"],),
        )
        self.assertEqual(deferred["stage"], "delivering")
        self.assertIsNone(deferred["operator_hold_id"])
        self.assertEqual(deferred["mandate_candidate_id"], "candidate-1")
        self.assertEqual(deferred["mandate_scope"], approved["scope_snapshot"])
        self.assertEqual(tollgate.approved, [])
        self.assertIsNone(
            self.controller.store.row(
                "SELECT id FROM holds WHERE scope = 'assignment' AND target = ?",
                (str(self.assignment["id"]),),
            )
        )

        native_status["overseer"] = "idle"
        assignment = self.controller.store.row(
            """SELECT a.*, r.project_id FROM assignments a JOIN runs r ON r.id = a.run_id
               WHERE a.id = ?""",
            (self.assignment["id"],),
        )
        await self.controller._deliver(assignment)

        completed = self.controller.store.row(
            "SELECT stage, operator_hold_id, mandate_candidate_id, mandate_scope FROM assignments WHERE id = ?",
            (self.assignment["id"],),
        )
        self.assertEqual(completed["stage"], "completed")
        self.assertIsNone(completed["operator_hold_id"])
        self.assertEqual(completed["mandate_candidate_id"], "candidate-1")
        self.assertEqual(completed["mandate_scope"], approved["scope_snapshot"])
        self.assertEqual(tollgate.approved, ["candidate-1"])
        self.assertEqual(beads.closed, ["p-1"])
        completion = self.controller.store.row(
            "SELECT content FROM updates WHERE identity = ?",
            (f"completion:{self.assignment['id']}",),
        )
        self.assertEqual(
            json.loads(completion["content"])["minor_fixes"],
            [
                {
                    "problem": "Nonblocking naming inconsistency",
                    "evidence": "module.py:10",
                    "requested_change": "Use the domain term in follow-up work",
                }
            ],
        )

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
        self.controller.store.execute(
            "UPDATE tasks SET runtime_status = 'notLoaded' WHERE id IN (?, ?)",
            (self.executor["id"], self.overseer["id"]),
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
