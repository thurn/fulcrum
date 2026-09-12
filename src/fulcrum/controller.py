"""Single-writer controller for runtime events, scheduling, and delivery."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import signal
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from watchfiles import awatch

from fulcrum.beads import Beads, BeadsUncertainError
from dataclasses import replace

from fulcrum.config import InstallationConfig, RuntimePaths, save_installation
from fulcrum.intake import (
    file_graph,
    file_task,
    reconcile_beads_creation,
    task_from_payload,
)
from fulcrum.lifecycle import accept_finish, observe_action_terminal
from fulcrum.prompts import build_prompt, load_template
from fulcrum.reset import reset_brain
from fulcrum.runtime import AppServerError, CodexRuntime, thread_facts
from fulcrum.scheduling import (
    capacity,
    create_due_occurrences,
    next_cadence,
    ready_assignments,
)
from fulcrum.store import Store, StoreError, utc_now
from fulcrum.tollgate import Tollgate, TollgateError, TollgateUncertainError


class Controller:
    """Own all operational mutations for one configured environment."""

    def __init__(self, paths: RuntimePaths, config: InstallationConfig) -> None:
        self.paths = paths
        self.config = config
        self.store = Store(paths.database)
        self.runtime = CodexRuntime(
            config.app_server_endpoint, event_handler=self._queue_runtime_event
        )
        self.tollgate: Tollgate | None = None
        self.beads = Beads(paths.brain_root)
        self.events: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
        self.mutation_lock = asyncio.Lock()
        self.server: asyncio.AbstractServer | None = None
        self.stop_event = asyncio.Event()
        self.starts_enabled = False
        self.lock_handle: Any = None
        self._initialize_configuration()

    def _initialize_configuration(self) -> None:
        timestamp = utc_now()
        for project in self.config.projects:
            self.store.execute(
                """INSERT INTO projects(project_id, repo_path, codex_project_id, tollgate_repo_id,
                   validation_command, source_remote, enabled, condition) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                   ON CONFLICT(project_id) DO UPDATE SET repo_path = excluded.repo_path,
                   codex_project_id = excluded.codex_project_id, tollgate_repo_id = excluded.tollgate_repo_id,
                   validation_command = excluded.validation_command, source_remote = excluded.source_remote,
                   enabled = excluded.enabled""",
                (
                    project.project_id,
                    project.repo_path,
                    project.codex_project_id,
                    project.tollgate_repo_id,
                    json.dumps(project.validation_command),
                    project.source_remote,
                    int(project.enabled),
                ),
            )
        self.store.execute(
            "INSERT INTO meta(key, value) VALUES ('controller_state', 'starting') ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        )
        self.store.event("controller_starting", "controller is starting", now=timestamp)

    def acquire_process_lock(self) -> None:
        self.paths.control_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = self.paths.lock.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            handle.close()
            raise StoreError(
                f"another Fulcrum controller owns {self.paths.lock}"
            ) from error
        os.set_inheritable(handle.fileno(), True)
        self.lock_handle = handle

    async def start(self) -> None:
        self.acquire_process_lock()
        self.paths.socket.unlink(missing_ok=True)
        self.server = await asyncio.start_unix_server(
            self._handle_client, path=self.paths.socket
        )
        os.chmod(self.paths.socket, 0o600)
        try:
            self.tollgate = Tollgate()
        except TollgateError as error:
            self.store.event(
                "capability_failed",
                str(error),
                entity_type="capability",
                entity_id="tollgate",
            )
        await self._connect_runtime()
        await self.reconcile()
        self.starts_enabled = self.runtime.ready
        self.store.execute(
            "INSERT INTO meta(key, value) VALUES ('controller_state', 'ready') ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        )
        tasks = [
            asyncio.create_task(self._event_loop(), name="fulcrum-events"),
            asyncio.create_task(self._fallback_loop(), name="fulcrum-fallback"),
            asyncio.create_task(self._source_watch_loop(), name="fulcrum-source-watch"),
        ]
        try:
            await self.stop_event.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            server = self.server
            if server is not None:
                server.close()
                await server.wait_closed()
            await self.runtime.close()
            self.paths.socket.unlink(missing_ok=True)
            self.store.close()

    async def _connect_runtime(self) -> None:
        try:
            await self.runtime.connect()
        except AppServerError as error:
            self.starts_enabled = False
            self.store.event(
                "runtime_disconnected",
                str(error),
                entity_type="capability",
                entity_id="app_server",
            )

    async def _queue_runtime_event(self, method: str, params: dict[str, Any]) -> None:
        await self.events.put((method, params))

    async def _event_loop(self) -> None:
        while True:
            method, params = await self.events.get()
            try:
                async with self.mutation_lock:
                    await self._handle_runtime_event(method, params)
                    await self.advance()
            except Exception as error:
                self.store.event("event_error", f"{method}: {error}")

    async def _handle_runtime_event(self, method: str, params: dict[str, Any]) -> None:
        thread_id = params.get("threadId")
        if not isinstance(thread_id, str):
            return
        task = self.store.row(
            "SELECT * FROM tasks WHERE native_thread_id = ?", (thread_id,)
        )
        if task is None:
            return
        timestamp = utc_now()
        if method == "turn/started":
            turn = params.get("turn")
            turn_id = turn.get("id") if isinstance(turn, dict) else None
            self.store.execute(
                "UPDATE tasks SET state = 'active', runtime_status = 'active', last_turn_terminal = 0, updated_at = ? WHERE id = ?",
                (timestamp, task["id"]),
            )
            if isinstance(turn_id, str):
                self.store.execute(
                    "UPDATE actions SET state = 'active', native_turn_id = ?, updated_at = ? WHERE task_id = ? AND state IN ('starting','pending')",
                    (turn_id, timestamp, task["id"]),
                )
        elif method == "thread/status/changed":
            raw_status = params.get("status")
            status = (
                raw_status.get("type") if isinstance(raw_status, dict) else raw_status
            )
            self.store.execute(
                "UPDATE tasks SET runtime_status = ?, updated_at = ? WHERE id = ?",
                (status, timestamp, task["id"]),
            )
        elif method == "turn/completed":
            await self._refresh_task(task)
            turn = params.get("turn")
            turn_id = turn.get("id") if isinstance(turn, dict) else None
            status = (
                turn.get("status", "completed")
                if isinstance(turn, dict)
                else "completed"
            )
            action = (
                self.store.row(
                    "SELECT * FROM actions WHERE task_id = ? AND native_turn_id = ?",
                    (task["id"], turn_id),
                )
                if turn_id
                else None
            )
            if action is not None:
                if (
                    status == "completed"
                    and action["outcome_kind"]
                    in {"ready_for_review", "permitted_repair_complete"}
                    and action["assignment_id"] is not None
                ):
                    captured = await self._capture_submitted_candidate(
                        int(action["assignment_id"])
                    )
                    if not captured:
                        return
                result = observe_action_terminal(
                    self.store, int(action["id"]), runtime_state=str(status)
                )
                if result.get("reminder"):
                    action["payload"] = json.dumps(
                        {
                            "reminder": "Your previous turn ended without the required finish outcome. Report only the retained outcome now."
                        }
                    )
                    self.store.execute(
                        "UPDATE actions SET payload = ?, state = 'pending', updated_at = ? WHERE id = ?",
                        (action["payload"], timestamp, action["id"]),
                    )
                    await self._dispatch_action(action)
        elif method in {"thread/archived", "thread/unarchived"}:
            archived = int(method == "thread/archived")
            state = "archived" if archived else "idle"
            self.store.execute(
                "UPDATE tasks SET archived = ?, state = ?, updated_at = ? WHERE id = ?",
                (archived, state, timestamp, task["id"]),
            )

    async def _refresh_task(self, task: dict[str, Any]) -> dict[str, Any]:
        thread = await self.runtime.read_thread(task["native_thread_id"])
        if thread.get("name") != task["title"]:
            await self.runtime.set_name(task["native_thread_id"], task["title"])
        facts = thread_facts(thread)
        self.store.execute(
            "UPDATE tasks SET runtime_status = ?, last_turn_terminal = ?, helpers_terminal = ?, archived = ?, updated_at = ? WHERE id = ?",
            (
                facts["runtime_status"],
                int(facts["last_turn_terminal"]),
                int(facts["helpers_terminal"]),
                int(facts["archived"]),
                utc_now(),
                task["id"],
            ),
        )
        return facts

    async def _fallback_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            if self.mutation_lock.locked():
                continue
            async with self.mutation_lock:
                if not self.runtime.ready:
                    await self._connect_runtime()
                await self.reconcile()
                await self.advance()

    async def reconcile(self) -> None:
        if not self.runtime.ready:
            return
        await self._reconcile_uncertain_operations()
        for task in self.store.rows(
            "SELECT * FROM tasks WHERE state IN ('active','uncertain','provisioning')"
        ):
            try:
                facts = await self._refresh_task(task)
                action = self.store.row(
                    "SELECT * FROM actions WHERE task_id = ? AND state IN ('active','terminal') ORDER BY id DESC LIMIT 1",
                    (task["id"],),
                )
                if (
                    action is not None
                    and facts["last_turn_terminal"]
                    and action["native_turn_id"] == facts["last_turn_id"]
                ):
                    candidate_ready = True
                    if (
                        facts["last_turn_status"] == "completed"
                        and action["outcome_kind"]
                        in {"ready_for_review", "permitted_repair_complete"}
                        and action["assignment_id"] is not None
                    ):
                        candidate_ready = await self._capture_submitted_candidate(
                            int(action["assignment_id"])
                        )
                    if candidate_ready:
                        result = observe_action_terminal(
                            self.store,
                            int(action["id"]),
                            runtime_state=str(facts["last_turn_status"] or "completed"),
                        )
                        if result.get("reminder") and self.starts_enabled:
                            reminder_payload = json.dumps(
                                {
                                    "reminder": "Your previous turn ended without the required finish outcome. Report only the retained outcome now."
                                }
                            )
                            self.store.execute(
                                "UPDATE actions SET payload = ?, state = 'pending', updated_at = ? WHERE id = ?",
                                (reminder_payload, utc_now(), action["id"]),
                            )
                            action["payload"] = reminder_payload
                            await self._dispatch_action(action, task=task)
            except AppServerError as error:
                self.store.event(
                    "reconcile_failed",
                    str(error),
                    entity_type="task",
                    entity_id=task["id"],
                )
        await self._inspect_due_actions()
        self.store.execute(
            "INSERT INTO meta(key, value) VALUES ('last_reconciliation', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (utc_now(),),
        )
        create_due_occurrences(self.store)
        if self.paths.reboot_record.exists():
            record = json.loads(self.paths.reboot_record.read_text(encoding="utf-8"))
            if isinstance(record, dict) and record.get("mode") in {
                "soft",
                "hard",
                "reset",
            }:
                await self._begin_reboot(str(record["mode"]), existing=record)

    async def _inspect_due_actions(self) -> None:
        now = utc_now()
        for action in self.store.rows(
            "SELECT * FROM actions WHERE state IN ('active','uncertain') AND check_after IS NOT NULL AND check_after <= ?",
            (now,),
        ):
            task = self.store.row(
                "SELECT * FROM tasks WHERE id = ?", (action["task_id"],)
            )
            if task is None:
                continue
            facts = await self._refresh_task(task)
            condition = None
            if not facts["last_turn_terminal"] or not facts["helpers_terminal"]:
                condition = (
                    f"possible stall: {task['title']} remained active at its scheduled "
                    "liveness inspection; no interruption or replacement was attempted"
                )
                self.store.event(
                    "possible_stall",
                    condition,
                    entity_type="action",
                    entity_id=action["id"],
                )
                if task["role"] != "archon":
                    archon = self.store.row(
                        "SELECT id FROM tasks WHERE role = 'archon' AND state NOT IN ('retired','archived')"
                    )
                    if archon is not None:
                        self.store.execute(
                            "INSERT OR IGNORE INTO updates(recipient_task_id, identity, content, actionable, state, created_at, updated_at) VALUES (?, ?, ?, 1, 'retained', ?, ?)",
                            (
                                archon["id"],
                                f"stall:{action['id']}",
                                json.dumps({"condition": condition}),
                                now,
                                now,
                            ),
                        )
            self.store.execute(
                "UPDATE actions SET check_after = NULL, condition = COALESCE(?, condition), updated_at = ? WHERE id = ?",
                (condition, now, action["id"]),
            )

    async def _reconcile_uncertain_operations(self) -> None:
        for operation in self.store.rows(
            "SELECT * FROM external_operations WHERE state = 'uncertain' AND reconciliation_used = 0 ORDER BY id"
        ):
            if operation["kind"] == "turn_start":
                await self._reconcile_turn_start(operation)
            elif operation["kind"] == "thread_start":
                await self._reconcile_thread_start(operation)
            elif operation["kind"] == "tollgate_worktree_create":
                self._reconcile_worktree_create(operation)
            elif operation["kind"] == "tollgate_approve":
                await self._reconcile_tollgate_approve(operation)
            elif operation["kind"] == "thread_archive":
                await self._reconcile_thread_archive(operation)
            elif operation["kind"] == "beads_create":
                await asyncio.to_thread(self._reconcile_beads_create, operation)
            elif operation["kind"] == "beads_close":
                await asyncio.to_thread(self._reconcile_beads_close, operation)

    def _reconcile_beads_create(self, operation: dict[str, Any]) -> None:
        if reconcile_beads_creation(self.store, self.beads, operation):
            return
        condition = "Beads publication remains ambiguous after one exact external-reference lookup; operator must attach the actual bead or confirm it was not created"
        self._retain_uncertain_condition(operation, condition)
        self.store.execute(
            "UPDATE obligations SET state = 'uncertain', detail = ?, updated_at = ? WHERE kind = 'beads_publication' AND identity = ?",
            (condition, utc_now(), operation["target"]),
        )

    def _reconcile_beads_close(self, operation: dict[str, Any]) -> None:
        bead = self.beads.show(operation["target"])
        assignment = self.store.row(
            "SELECT * FROM assignments WHERE bead_id = ? AND stage = 'delivering'",
            (operation["target"],),
        )
        if bead and str(bead.get("status", "")).lower() in {
            "closed",
            "complete",
            "completed",
        }:
            self.store.execute(
                "UPDATE external_operations SET state = 'complete', reconciliation_used = 1, result_json = ?, condition = NULL, updated_at = ? WHERE id = ?",
                (json.dumps(bead), utc_now(), operation["id"]),
            )
            if assignment:
                self._record_assignment_completed(assignment)
            return
        condition = "Beads closure remains ambiguous after one exact issue read; operator must confirm closure before retry"
        self._retain_uncertain_condition(operation, condition)
        if assignment:
            self.store.execute(
                "UPDATE assignments SET condition = ?, updated_at = ? WHERE id = ?",
                (condition, utc_now(), assignment["id"]),
            )

    async def _reconcile_turn_start(self, operation: dict[str, Any]) -> None:
        inputs = json.loads(operation["input_json"])
        thread = await self.runtime.read_thread(inputs["thread_id"])
        turns = thread.get("turns") if isinstance(thread.get("turns"), list) else []
        baseline = inputs.get("baseline_turn_id")
        after_baseline = baseline is None
        matches: list[dict[str, Any]] = []
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            if not after_baseline:
                if turn.get("id") == baseline:
                    after_baseline = True
                continue
            for item in turn.get("items", []):
                if (
                    isinstance(item, dict)
                    and item.get("type") == "userMessage"
                    and item.get("clientId") == f"fulcrum-operation-{operation['id']}"
                ):
                    matches.append(turn)
                    break
        action = self.store.row(
            "SELECT * FROM actions WHERE id = ?", (int(operation["target"]),)
        )
        if len(matches) == 1 and action is not None:
            turn_id = matches[0]["id"]
            self.store.execute(
                "UPDATE external_operations SET state = 'complete', native_id = ?, reconciliation_used = 1, condition = NULL, updated_at = ? WHERE id = ?",
                (turn_id, utc_now(), operation["id"]),
            )
            self.store.execute(
                "UPDATE actions SET state = 'active', native_turn_id = ?, condition = NULL, updated_at = ? WHERE id = ?",
                (turn_id, utc_now(), action["id"]),
            )
            self.store.execute(
                "UPDATE reservations SET state = 'active' WHERE action_id = ?",
                (action["id"],),
            )
            return
        condition = (
            "multiple correlated turns matched the retained start"
            if len(matches) > 1
            else "turn start remains ambiguous after its one targeted history pass; operator must attach the actual turn or confirm no turn was created"
        )
        self._retain_uncertain_condition(operation, condition)

    async def _reconcile_thread_start(self, operation: dict[str, Any]) -> None:
        inputs = json.loads(operation["input_json"])
        if operation["native_id"]:
            thread = await self.runtime.read_thread(
                operation["native_id"], include_turns=False
            )
            await self._register_created_thread(operation, inputs, thread)
            return
        threads = await self.runtime.list_threads(
            cwd=inputs["cwd"], project_id=inputs.get("project_id")
        )
        created = datetime.fromisoformat(operation["created_at"].replace("Z", "+00:00"))
        candidates = [
            thread
            for thread in threads
            if thread.get("model") == inputs["model"]
            and isinstance(thread.get("createdAt"), int)
            and abs(datetime.fromtimestamp(thread["createdAt"], timezone.utc) - created)
            <= timedelta(minutes=2)
            and not thread.get("turns")
        ]
        if len(candidates) == 1:
            await self._register_created_thread(operation, inputs, candidates[0])
            return
        condition = (
            "multiple threads matched the retained creation intent"
            if len(candidates) > 1
            else "thread creation remains ambiguous after its one filtered discovery pass; operator must attach the actual thread or confirm no thread was created"
        )
        self._retain_uncertain_condition(operation, condition)

    def _reconcile_worktree_create(self, operation: dict[str, Any]) -> None:
        assignment = self.store.row(
            """SELECT a.*, p.repo_path FROM assignments a JOIN runs r ON r.id = a.run_id
               JOIN projects p ON p.project_id = r.project_id WHERE a.id = ?""",
            (int(operation["target"]),),
        )
        if assignment is None:
            self._retain_uncertain_condition(
                operation, "worktree assignment disappeared"
            )
            return
        name = json.loads(operation["input_json"])["name"]
        result = subprocess.run(
            ["git", "-C", assignment["repo_path"], "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
        )
        paths = [
            line.removeprefix("worktree ")
            for line in result.stdout.splitlines()
            if line.startswith("worktree ")
            and Path(line.removeprefix("worktree ")).name == name
        ]
        if result.returncode == 0 and len(paths) == 1:
            self.store.execute(
                "UPDATE assignments SET worktree_path = ?, stage = 'preparing', condition = NULL, updated_at = ? WHERE id = ?",
                (paths[0], utc_now(), assignment["id"]),
            )
            self.store.execute(
                "UPDATE external_operations SET state = 'complete', reconciliation_used = 1, result_json = ?, condition = NULL, updated_at = ? WHERE id = ?",
                (json.dumps({"worktree_path": paths[0]}), utc_now(), operation["id"]),
            )
            return
        condition = (
            "multiple worktrees matched the retained creation intent"
            if len(paths) > 1
            else "worktree creation remains ambiguous after its one exact Git inventory pass; operator must attach the actual worktree or confirm it was not created"
        )
        self._retain_uncertain_condition(operation, condition)
        self.store.execute(
            "UPDATE assignments SET condition = ?, updated_at = ? WHERE id = ?",
            (condition, utc_now(), assignment["id"]),
        )

    async def _reconcile_tollgate_approve(self, operation: dict[str, Any]) -> None:
        assignment = self.store.row(
            """SELECT a.*, r.project_id, p.tollgate_repo_id FROM assignments a
               JOIN runs r ON r.id = a.run_id JOIN projects p ON p.project_id = r.project_id
               WHERE a.candidate_id = ? AND a.stage NOT IN ('completed','canceled')""",
            (operation["target"],),
        )
        if assignment is None or self.tollgate is None:
            self._retain_uncertain_condition(
                operation, "cannot reconcile Tollgate approval against its assignment"
            )
            return
        observed = await asyncio.to_thread(
            self.tollgate.status,
            assignment["tollgate_repo_id"],
            assignment["candidate_id"],
        )
        candidate = _candidate_by_id(observed, assignment["candidate_id"])
        if candidate and candidate.get("promotion_authorized"):
            self.store.execute(
                "UPDATE external_operations SET state = 'complete', reconciliation_used = 1, result_json = ?, condition = NULL, updated_at = ? WHERE id = ?",
                (json.dumps(observed), utc_now(), operation["id"]),
            )
            self.store.execute(
                "UPDATE assignments SET stage = 'delivering', condition = NULL, updated_at = ? WHERE id = ?",
                (utc_now(), assignment["id"]),
            )
            return
        terminal = isinstance(candidate, dict) and candidate.get("state") not in {
            "queued",
            "running",
            "validated",
            "promoting",
        }
        if terminal and isinstance(candidate, dict):
            tollgate = self.tollgate
            assert tollgate is not None
            diagnosis = await asyncio.to_thread(
                tollgate.diagnose,
                assignment["tollgate_repo_id"],
                assignment["candidate_id"],
            )
            condition = f"Tollgate candidate ended in {candidate.get('state')}"
            self.store.execute(
                "UPDATE external_operations SET state = 'failed', reconciliation_used = 1, result_json = ?, condition = ?, updated_at = ? WHERE id = ?",
                (json.dumps(diagnosis), condition, utc_now(), operation["id"]),
            )
            self.store.execute(
                "UPDATE assignments SET stage = 'correcting', condition = ?, updated_at = ? WHERE id = ?",
                (condition, utc_now(), assignment["id"]),
            )
            return
        condition = "Tollgate approval remains ambiguous after one candidate-specific status read; operator must confirm authorization before retry"
        self._retain_uncertain_condition(operation, condition)
        self.store.execute(
            "UPDATE assignments SET condition = ?, updated_at = ? WHERE id = ?",
            (condition, utc_now(), assignment["id"]),
        )

    async def _reconcile_thread_archive(self, operation: dict[str, Any]) -> None:
        thread = await self.runtime.read_thread(
            operation["target"], include_turns=False
        )
        task = self.store.row(
            "SELECT * FROM tasks WHERE native_thread_id = ?", (operation["target"],)
        )
        obligation = self.store.row(
            "SELECT * FROM obligations WHERE kind = 'archive' AND target = ? AND state NOT IN ('complete','canceled') ORDER BY id LIMIT 1",
            (operation["target"],),
        )
        if thread.get("archived"):
            self.store.execute(
                "UPDATE external_operations SET state = 'complete', reconciliation_used = 1, condition = NULL, updated_at = ? WHERE id = ?",
                (utc_now(), operation["id"]),
            )
            if task:
                self.store.execute(
                    "UPDATE tasks SET state = 'archived', archived = 1, updated_at = ? WHERE id = ?",
                    (utc_now(), task["id"]),
                )
            if obligation:
                self.store.execute(
                    "UPDATE obligations SET state = 'complete', updated_at = ? WHERE id = ?",
                    (utc_now(), obligation["id"]),
                )
            return
        self.store.execute(
            "UPDATE external_operations SET state = 'failed', reconciliation_used = 1, condition = 'targeted read confirmed archive did not take effect', updated_at = ? WHERE id = ?",
            (utc_now(), operation["id"]),
        )
        if obligation:
            self.store.execute(
                "UPDATE obligations SET state = 'failed', detail = 'targeted read confirmed archive did not take effect', updated_at = ? WHERE id = ?",
                (utc_now(), obligation["id"]),
            )

    def _retain_uncertain_condition(
        self, operation: dict[str, Any], condition: str
    ) -> None:
        self.store.execute(
            "UPDATE external_operations SET reconciliation_used = 1, condition = ?, updated_at = ? WHERE id = ?",
            (condition, utc_now(), operation["id"]),
        )
        self.store.event(
            "operator_attention",
            condition,
            entity_type="operation",
            entity_id=operation["id"],
        )

    async def advance(self) -> None:
        if not self.starts_enabled or not self.runtime.ready:
            return
        for assignment in ready_assignments(self.store):
            if assignment["stage"] == "queued":
                await self._prepare_assignment(assignment)
            elif assignment["stage"] == "preparing":
                await self._resume_preparing_assignment(assignment)
            elif assignment["stage"] in {"review_pending", "correcting"}:
                await self._start_assignment_action(assignment)
        for assignment in self.store.rows(
            "SELECT a.*, r.project_id FROM assignments a JOIN runs r ON r.id = a.run_id WHERE a.stage = 'delivering'"
        ):
            await self._deliver(assignment)
        await self._publish_occurrences()
        await self._manage_interviews()
        await self._start_specialists()
        await self._queue_proposals()
        await self._deliver_update_batch()
        await self._archive_ready_tasks()
        self._update_readiness()

    async def _prepare_assignment(self, assignment: dict[str, Any]) -> None:
        project = self.store.row(
            "SELECT * FROM projects WHERE project_id = ?", (assignment["project_id"],)
        )
        if project is None or not project["tollgate_repo_id"] or self.tollgate is None:
            return
        timestamp = utc_now()
        self.store.execute(
            "UPDATE assignments SET stage = 'preparing', updated_at = ? WHERE id = ?",
            (timestamp, assignment["id"]),
        )
        operation = self.store.create_operation(
            "tollgate_worktree_create",
            str(assignment["id"]),
            {
                "repository_id": project["tollgate_repo_id"],
                "name": f"fulcrum-{assignment['id']}",
            },
        )
        self.store.execute(
            "UPDATE external_operations SET state = 'sent' WHERE id = ?", (operation,)
        )
        try:
            tollgate = self.tollgate
            assert tollgate is not None
            result = await asyncio.to_thread(
                tollgate.create_worktree,
                project["tollgate_repo_id"],
                f"fulcrum-{assignment['id']}",
            )
            path = _find_string(result, {"path", "worktree_path", "worktreePath"})
            if path is None:
                raise TollgateError("worktree create returned no path")
            self.store.execute(
                "UPDATE assignments SET worktree_path = ?, updated_at = ? WHERE id = ?",
                (path, utc_now(), assignment["id"]),
            )
            self.store.execute(
                "UPDATE external_operations SET state = 'complete', result_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(result), utc_now(), operation),
            )
            assignment["worktree_path"] = path
            await self._ensure_pair(assignment)
            assignment["stage"] = "implementing"
            self.store.execute(
                "UPDATE assignments SET stage = 'implementing', updated_at = ? WHERE id = ?",
                (utc_now(), assignment["id"]),
            )
            await self._start_assignment_action(assignment)
        except Exception as error:
            self._operation_failed(operation, error)
            self.store.execute(
                "UPDATE assignments SET prior_stage = 'preparing', stage = 'recovering', condition = ?, updated_at = ? WHERE id = ?",
                (str(error), utc_now(), assignment["id"]),
            )

    async def _resume_preparing_assignment(self, assignment: dict[str, Any]) -> None:
        if not assignment["worktree_path"]:
            self.store.execute(
                "UPDATE assignments SET stage = 'queued', updated_at = ? WHERE id = ?",
                (utc_now(), assignment["id"]),
            )
            return
        await self._ensure_pair(assignment)
        self.store.execute(
            "UPDATE assignments SET stage = 'implementing', condition = NULL, updated_at = ? WHERE id = ?",
            (utc_now(), assignment["id"]),
        )
        assignment["stage"] = "implementing"
        await self._start_assignment_action(assignment)

    async def _ensure_pair(self, assignment: dict[str, Any]) -> None:
        if assignment.get("executor_task_id") and assignment.get("overseer_task_id"):
            return
        bead = self.store.row(
            "SELECT * FROM beads WHERE bead_id = ?", (assignment["bead_id"],)
        )
        project = self.store.row(
            "SELECT * FROM projects WHERE project_id = ?", (assignment["project_id"],)
        )
        if bead is None or project is None:
            raise StoreError("assignment sources disappeared")
        executor = await self._provision_task(
            role="executor",
            description=bead["title"],
            project=project,
            model=bead["executor_model"],
            effort=bead["executor_reasoning_effort"],
            pair_id=int(assignment["id"]),
        )
        overseer = await self._provision_task(
            role="overseer",
            description=bead["title"],
            project=project,
            model=bead["overseer_model"],
            effort=bead["overseer_reasoning_effort"],
            pair_id=int(assignment["id"]),
        )
        self.store.execute(
            "UPDATE assignments SET executor_task_id = ?, overseer_task_id = ?, updated_at = ? WHERE id = ?",
            (executor["id"], overseer["id"], utc_now(), assignment["id"]),
        )
        assignment["executor_task_id"] = executor["id"]
        assignment["overseer_task_id"] = overseer["id"]

    async def _provision_task(
        self,
        *,
        role: str,
        description: str,
        project: dict[str, Any],
        model: str,
        effort: str,
        pair_id: int | None = None,
    ) -> dict[str, Any]:
        role_number, title = self.store.allocate_name(role, description)
        inputs = {
            "role": role,
            "description": description,
            "project_id": project["codex_project_id"],
            "local_project_id": project["project_id"],
            "cwd": project["repo_path"],
            "model": model,
            "effort": effort,
            "pair_id": pair_id,
            "role_number": role_number,
            "title": title,
        }
        operation = self.store.create_operation("thread_start", role, inputs)
        self.store.execute(
            "UPDATE external_operations SET state = 'sent' WHERE id = ?", (operation,)
        )
        try:
            result = await self.runtime.create_thread(
                cwd=project["repo_path"],
                model=model,
                project_id=project["codex_project_id"],
                base_instructions=load_template(
                    "implement"
                    if role == "executor"
                    else (
                        "review"
                        if role == "overseer"
                        else (
                            "specialist" if role in {"sage", "inquisitor"} else "archon"
                        )
                    )
                ),
            )
            thread = result["thread"]
            self.store.execute(
                "UPDATE external_operations SET native_id = ?, updated_at = ? WHERE id = ?",
                (thread["id"], utc_now(), operation),
            )
            task = self.store.register_task(
                native_thread_id=thread["id"],
                role=role,
                description=description,
                model=model,
                reasoning_effort=effort,
                project_id=project["project_id"],
                state="provisioning",
                pair_id=pair_id,
                role_number=role_number,
                title=title,
            )
            await self.runtime.set_name(thread["id"], task["title"])
            self.store.execute(
                "UPDATE tasks SET state = 'idle', runtime_status = 'unmaterialized', updated_at = ? WHERE id = ?",
                (utc_now(), task["id"]),
            )
            self.store.execute(
                "UPDATE external_operations SET state = 'complete', native_id = ?, result_json = ?, updated_at = ? WHERE id = ?",
                (thread["id"], json.dumps(result), utc_now(), operation),
            )
            task["state"] = "idle"
            task["runtime_status"] = "unmaterialized"
            return task
        except Exception as error:
            state = "uncertain" if isinstance(error, AppServerError) else "failed"
            self.store.execute(
                "UPDATE external_operations SET state = ?, condition = ?, updated_at = ? WHERE id = ?",
                (state, str(error), utc_now(), operation),
            )
            raise

    async def _register_created_thread(
        self,
        operation: dict[str, Any],
        inputs: dict[str, Any],
        thread: dict[str, Any],
    ) -> dict[str, Any]:
        thread_id = thread.get("id")
        if not isinstance(thread_id, str):
            raise AppServerError("discovered thread has no native ID")
        task = self.store.register_task(
            native_thread_id=thread_id,
            role=inputs["role"],
            description=inputs["description"],
            model=inputs["model"],
            reasoning_effort=inputs["effort"],
            project_id=inputs.get("local_project_id"),
            state="provisioning",
            pair_id=inputs.get("pair_id"),
            role_number=inputs.get("role_number"),
            title=inputs["title"],
        )
        await self.runtime.set_name(thread_id, inputs["title"])
        facts = thread_facts(thread)
        runtime_status = (
            str(facts["runtime_status"]) if thread.get("turns") else "unmaterialized"
        )
        self.store.execute(
            "UPDATE tasks SET state = 'idle', runtime_status = ?, updated_at = ? WHERE id = ?",
            (runtime_status, utc_now(), task["id"]),
        )
        self.store.execute(
            "UPDATE external_operations SET state = 'complete', native_id = ?, result_json = ?, reconciliation_used = 1, condition = NULL, updated_at = ? WHERE id = ?",
            (thread_id, json.dumps({"thread": thread}), utc_now(), operation["id"]),
        )
        pair_id = inputs.get("pair_id")
        if isinstance(pair_id, int) and inputs["role"] in {"executor", "overseer"}:
            column = (
                "executor_task_id"
                if inputs["role"] == "executor"
                else "overseer_task_id"
            )
            self.store.execute(
                f"UPDATE assignments SET {column} = ?, stage = 'preparing', condition = NULL, updated_at = ? WHERE id = ?",
                (task["id"], utc_now(), pair_id),
            )
        task["state"] = "idle"
        task["runtime_status"] = runtime_status
        return task

    async def _start_assignment_action(self, assignment: dict[str, Any]) -> None:
        kind = (
            "implement"
            if assignment["stage"] == "implementing"
            else "review" if assignment["stage"] == "review_pending" else "correct"
        )
        task_key = (
            "executor_task_id"
            if kind in {"implement", "correct"}
            else "overseer_task_id"
        )
        task = self.store.row(
            "SELECT * FROM tasks WHERE id = ?", (assignment[task_key],)
        )
        if task is None:
            return
        existing = self.store.row(
            "SELECT * FROM actions WHERE task_id = ? AND state NOT IN ('processed','canceled')",
            (task["id"],),
        )
        if existing is not None:
            return
        payload = {"purpose": kind, "bead_id": assignment["bead_id"]}
        timestamp = utc_now()
        cursor = self.store.execute(
            "INSERT INTO actions(task_id, assignment_id, kind, payload, state, check_after, created_at, updated_at) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)",
            (
                task["id"],
                assignment["id"],
                kind,
                json.dumps(payload),
                (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=self.config.turn_check_after_seconds)
                )
                .isoformat()
                .replace("+00:00", "Z"),
                timestamp,
                timestamp,
            ),
        )
        action = self.store.row(
            "SELECT * FROM actions WHERE id = ?", (cursor.lastrowid,)
        )
        assert action is not None
        self.store.execute(
            "INSERT INTO reservations(action_id, pair_id, global_slots, project_ids, state, created_at) VALUES (?, ?, 1, ?, 'reserved', ?)",
            (
                action["id"],
                assignment["id"],
                json.dumps([assignment["project_id"]]),
                timestamp,
            ),
        )
        next_stage = {
            "implement": "implementing",
            "review": "reviewing",
            "correct": "correcting",
        }[kind]
        self.store.execute(
            "UPDATE assignments SET stage = ?, updated_at = ? WHERE id = ?",
            (next_stage, timestamp, assignment["id"]),
        )
        assignment["stage"] = next_stage
        await self._dispatch_action(action, task=task, assignment=assignment)

    async def _dispatch_action(
        self,
        action: dict[str, Any],
        *,
        task: dict[str, Any] | None = None,
        assignment: dict[str, Any] | None = None,
    ) -> None:
        task = task or self.store.row(
            "SELECT * FROM tasks WHERE id = ?", (action["task_id"],)
        )
        if task is None:
            raise StoreError("action task is missing")
        if assignment is None and action.get("assignment_id") is not None:
            assignment = self.store.row(
                "SELECT a.*, r.project_id FROM assignments a JOIN runs r ON r.id = a.run_id WHERE a.id = ?",
                (action["assignment_id"],),
            )
        if task["archived"]:
            await self.runtime.unarchive(task["native_thread_id"])
            await self.runtime.resume_thread(task["native_thread_id"])
        facts = (
            {
                "last_turn_id": None,
                "can_start": True,
            }
            if task["runtime_status"] == "unmaterialized"
            else await self._refresh_task(task)
        )
        if not facts["can_start"]:
            raise StoreError(f"task {task['title']} is not ready for a new turn")
        if assignment and action["kind"] in {"implement", "correct"}:
            cwd = assignment["worktree_path"]
        else:
            project = self.store.row(
                "SELECT repo_path FROM projects WHERE project_id = ?",
                (task["project_id"],),
            )
            if project is None:
                raise StoreError("action project is missing")
            cwd = project["repo_path"]
        prompt = build_prompt(
            action_kind=action["kind"], task=task, action=action, assignment=assignment
        )
        operation = self.store.create_operation(
            "turn_start",
            str(action["id"]),
            {
                "thread_id": task["native_thread_id"],
                "prompt": prompt,
                "baseline_turn_id": facts["last_turn_id"],
            },
        )
        self.store.execute(
            "UPDATE actions SET state = 'starting', updated_at = ? WHERE id = ?",
            (utc_now(), action["id"]),
        )
        self.store.execute(
            "UPDATE external_operations SET state = 'sent' WHERE id = ?", (operation,)
        )
        try:
            turn_id = await self.runtime.start_turn(
                task["native_thread_id"],
                prompt,
                cwd=cwd,
                model=task["model"],
                effort=task["reasoning_effort"],
                correlation=f"fulcrum-operation-{operation}",
            )
            self.store.execute(
                "UPDATE external_operations SET state = 'complete', native_id = ?, updated_at = ? WHERE id = ?",
                (turn_id, utc_now(), operation),
            )
            self.store.execute(
                "UPDATE actions SET state = 'active', native_turn_id = ?, updated_at = ? WHERE id = ?",
                (turn_id, utc_now(), action["id"]),
            )
            self.store.execute(
                "UPDATE tasks SET state = 'active', runtime_status = 'active', last_turn_terminal = 0, updated_at = ? WHERE id = ?",
                (utc_now(), task["id"]),
            )
            self.store.execute(
                "UPDATE reservations SET state = 'active' WHERE action_id = ?",
                (action["id"],),
            )
        except AppServerError as error:
            self.store.execute(
                "UPDATE external_operations SET state = 'uncertain', condition = ?, updated_at = ? WHERE id = ?",
                (str(error), utc_now(), operation),
            )
            self.store.execute(
                "UPDATE actions SET state = 'uncertain', condition = ?, updated_at = ? WHERE id = ?",
                (str(error), utc_now(), action["id"]),
            )
            self.store.execute(
                "UPDATE reservations SET state = 'uncertain' WHERE action_id = ?",
                (action["id"],),
            )

    async def _capture_submitted_candidate(self, assignment_id: int) -> bool:
        assignment = self.store.row(
            "SELECT a.*, r.project_id FROM assignments a JOIN runs r ON r.id = a.run_id WHERE a.id = ?",
            (assignment_id,),
        )
        if assignment is None or self.tollgate is None:
            return False
        project = self.store.row(
            "SELECT * FROM projects WHERE project_id = ?", (assignment["project_id"],)
        )
        if project is None or not project["tollgate_repo_id"]:
            return False
        tollgate = self.tollgate
        assert tollgate is not None
        result = await asyncio.to_thread(
            tollgate.status, project["tollgate_repo_id"], None
        )
        candidate = _find_candidate(
            result,
            assignment["worktree_path"],
            exclude_id=assignment["candidate_id"],
        )
        if candidate is None:
            reason = (
                "submitted outcome has no new matching immutable Tollgate candidate"
            )
            self.store.execute(
                "UPDATE actions SET state = 'failed', condition = ?, updated_at = ? WHERE assignment_id = ? AND state NOT IN ('processed','canceled')",
                (reason, utc_now(), assignment_id),
            )
            self.store.execute(
                "UPDATE assignments SET prior_stage = stage, stage = 'recovering', condition = ?, updated_at = ? WHERE id = ?",
                (reason, utc_now(), assignment_id),
            )
            return False
        self.store.execute(
            "UPDATE assignments SET candidate_id = ?, source_oid = ?, tested_oid = ?, updated_at = ? WHERE id = ?",
            (
                candidate.get("id"),
                _oid(candidate.get("source_oid")),
                _oid(candidate.get("tested_oid")),
                utc_now(),
                assignment_id,
            ),
        )
        return True

    async def _deliver(self, assignment: dict[str, Any]) -> None:
        if self.tollgate is None or not assignment["candidate_id"]:
            return
        project = self.store.row(
            "SELECT * FROM projects WHERE project_id = ?", (assignment["project_id"],)
        )
        if project is None or not project["tollgate_repo_id"]:
            return
        previous = self.store.row(
            "SELECT * FROM external_operations WHERE kind = 'tollgate_approve' AND target = ? ORDER BY id DESC LIMIT 1",
            (assignment["candidate_id"],),
        )
        if previous and previous["state"] in {"sent", "uncertain"}:
            return
        if previous and previous["state"] == "complete":
            tollgate = self.tollgate
            assert tollgate is not None
            observed = await asyncio.to_thread(
                tollgate.status,
                project["tollgate_repo_id"],
                assignment["candidate_id"],
            )
            candidate = _candidate_by_id(observed, assignment["candidate_id"])
            failure = _delivery_contract_failure(candidate, observed)
            if failure is None:
                await self._close_delivered_assignment(assignment)
            elif candidate and candidate.get("state") not in {
                "queued",
                "running",
                "validated",
                "promoting",
            }:
                self.store.execute(
                    "UPDATE assignments SET prior_stage = 'delivering', stage = 'correcting', condition = ?, updated_at = ? WHERE id = ?",
                    (failure, utc_now(), assignment["id"]),
                )
            return
        authority_error = self._delivery_authority_error(assignment)
        if authority_error:
            self.store.execute(
                "UPDATE assignments SET prior_stage = 'delivering', stage = 'recovering', condition = ?, updated_at = ? WHERE id = ?",
                (authority_error, utc_now(), assignment["id"]),
            )
            return
        operation = self.store.create_operation(
            "tollgate_approve",
            assignment["candidate_id"],
            {"repository_id": project["tollgate_repo_id"]},
        )
        self.store.execute(
            "UPDATE external_operations SET state = 'sent' WHERE id = ?", (operation,)
        )
        try:
            tollgate = self.tollgate
            assert tollgate is not None
            result = await asyncio.to_thread(
                tollgate.approve,
                project["tollgate_repo_id"],
                assignment["candidate_id"],
            )
            observed = await asyncio.to_thread(
                tollgate.status,
                project["tollgate_repo_id"],
                assignment["candidate_id"],
            )
            candidate = _candidate_by_id(observed, assignment["candidate_id"])
            failure = _delivery_contract_failure(candidate, observed)
            if failure is not None:
                diagnosis: dict[str, Any] | None = None
                if candidate and candidate.get("state") not in {
                    "queued",
                    "running",
                    "validated",
                    "promoting",
                }:
                    diagnosis = await asyncio.to_thread(
                        tollgate.diagnose,
                        project["tollgate_repo_id"],
                        assignment["candidate_id"],
                    )
                detail = {
                    "approval": result,
                    "status": observed,
                    "diagnosis": diagnosis,
                }
                self.store.execute(
                    "UPDATE external_operations SET state = 'failed', result_json = ?, condition = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(detail), failure, utc_now(), operation),
                )
                self.store.execute(
                    "UPDATE assignments SET prior_stage = 'delivering', stage = 'correcting', condition = ?, updated_at = ? WHERE id = ?",
                    (failure, utc_now(), assignment["id"]),
                )
                return
            self.store.execute(
                "UPDATE external_operations SET state = 'complete', result_json = ?, updated_at = ? WHERE id = ?",
                (
                    json.dumps({"approval": result, "status": observed}),
                    utc_now(),
                    operation,
                ),
            )
            await self._close_delivered_assignment(assignment)
        except Exception as error:
            self._operation_failed(operation, error)
            self.store.execute(
                "UPDATE assignments SET prior_stage = 'delivering', stage = 'recovering', condition = ?, updated_at = ? WHERE id = ?",
                (str(error), utc_now(), assignment["id"]),
            )

    async def _close_delivered_assignment(self, assignment: dict[str, Any]) -> None:
        previous = self.store.row(
            "SELECT * FROM external_operations WHERE kind = 'beads_close' AND target = ? ORDER BY id DESC LIMIT 1",
            (assignment["bead_id"],),
        )
        if previous and previous["state"] in {"sent", "uncertain"}:
            return
        if not previous or previous["state"] != "complete":
            operation = self.store.create_operation(
                "beads_close",
                assignment["bead_id"],
                {"candidate_id": assignment["candidate_id"]},
            )
            self.store.execute(
                "UPDATE external_operations SET state = 'sent' WHERE id = ?",
                (operation,),
            )
            try:
                await asyncio.to_thread(
                    self.beads.close,
                    assignment["bead_id"],
                    f"Delivered candidate {assignment['candidate_id']}",
                )
                self.store.execute(
                    "UPDATE external_operations SET state = 'complete', updated_at = ? WHERE id = ?",
                    (utc_now(), operation),
                )
            except BeadsUncertainError as error:
                self.store.execute(
                    "UPDATE external_operations SET state = 'uncertain', condition = ?, updated_at = ? WHERE id = ?",
                    (str(error), utc_now(), operation),
                )
                self.store.execute(
                    "UPDATE assignments SET condition = ?, updated_at = ? WHERE id = ?",
                    (str(error), utc_now(), assignment["id"]),
                )
                return
        self._record_assignment_completed(assignment)

    def _record_assignment_completed(self, assignment: dict[str, Any]) -> None:
        self.store.execute(
            "UPDATE assignments SET stage = 'completed', condition = NULL, updated_at = ? WHERE id = ?",
            (utc_now(), assignment["id"]),
        )
        remaining = self.store.row(
            "SELECT 1 FROM assignments WHERE run_id = ? AND stage NOT IN ('completed','canceled')",
            (assignment["run_id"],),
        )
        if remaining is not None:
            return
        self.store.execute(
            "UPDATE runs SET state = 'completed', updated_at = ? WHERE id = ?",
            (utc_now(), assignment["run_id"]),
        )
        for key in ("executor_task_id", "overseer_task_id"):
            task_id = assignment.get(key)
            if task_id:
                task = self.store.row(
                    "SELECT native_thread_id FROM tasks WHERE id = ?", (task_id,)
                )
                if task:
                    self.store.execute(
                        "INSERT OR IGNORE INTO obligations(kind, identity, target, state, created_at, updated_at) VALUES ('archive', ?, ?, 'pending', ?, ?)",
                        (
                            str(task_id),
                            task["native_thread_id"],
                            utc_now(),
                            utc_now(),
                        ),
                    )

    def _delivery_authority_error(self, assignment: dict[str, Any]) -> str | None:
        if assignment["candidate_id"] != assignment["mandate_candidate_id"]:
            if (
                assignment["predecessor_candidate_id"]
                != assignment["mandate_candidate_id"]
            ):
                return "candidate is not linked to the exact retained review mandate"
            permissions = json.loads(assignment["repair_permissions"] or "[]")
            if assignment["repair_category"] not in permissions:
                return "replacement candidate has no matching permitted repair category"
            if not assignment["repair_rationale"] or not assignment["repair_evidence"]:
                return "replacement candidate lacks retained repair rationale or validation"
        if assignment["scope_snapshot"] != assignment["mandate_scope"]:
            return "assignment scope differs from the retained review mandate"
        active = self.store.row(
            "SELECT 1 FROM tasks WHERE id IN (?, ?) AND (last_turn_terminal = 0 OR helpers_terminal = 0 OR runtime_status != 'idle')",
            (assignment["executor_task_id"], assignment["overseer_task_id"]),
        )
        if active is not None:
            return "pair is not confirmed inactive at the delivery boundary"
        hold = self.store.row(
            """SELECT id FROM holds WHERE released_at IS NULL AND
               (scope = 'global' OR (scope = 'project' AND target = ?) OR
                (scope = 'run' AND target = ?)) LIMIT 1""",
            (assignment["project_id"], str(assignment["run_id"])),
        )
        if hold is not None:
            return f"delivery is blocked by hold {hold['id']}"
        return None

    async def _archive_ready_tasks(self) -> None:
        for obligation in self.store.rows(
            "SELECT * FROM obligations WHERE kind = 'archive' AND state IN ('pending','failed')"
        ):
            task = self.store.row(
                "SELECT * FROM tasks WHERE native_thread_id = ?",
                (obligation["target"],),
            )
            if (
                task is None
                or not task["last_turn_terminal"]
                or not task["helpers_terminal"]
            ):
                continue
            operation = self.store.create_operation(
                "thread_archive", obligation["target"], {}
            )
            try:
                await self.runtime.archive(obligation["target"])
                self.store.execute(
                    "UPDATE obligations SET state = 'complete', updated_at = ? WHERE id = ?",
                    (utc_now(), obligation["id"]),
                )
                self.store.execute(
                    "UPDATE tasks SET state = 'archived', archived = 1, updated_at = ? WHERE id = ?",
                    (utc_now(), task["id"]),
                )
                self.store.execute(
                    "UPDATE external_operations SET state = 'complete', updated_at = ? WHERE id = ?",
                    (utc_now(), operation),
                )
            except Exception as error:
                self._operation_failed(operation, error)
                self.store.execute(
                    "UPDATE obligations SET state = 'failed', detail = ?, updated_at = ? WHERE id = ?",
                    (str(error), utc_now(), obligation["id"]),
                )

    def _operation_failed(self, operation: int, error: Exception) -> None:
        state = (
            "uncertain"
            if isinstance(error, (TollgateUncertainError, AppServerError))
            else "failed"
        )
        self.store.execute(
            "UPDATE external_operations SET state = ?, condition = ?, updated_at = ? WHERE id = ?",
            (state, str(error), utc_now(), operation),
        )

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 30)
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise ValueError("request must be an object")
            async with self.mutation_lock:
                result = await self.handle_request(payload)
                await self.advance()
            response = {"ok": True, "data": result}
        except Exception as error:
            response = {"ok": False, "error": str(error)}
        writer.write(json.dumps(response, separators=(",", ":")).encode() + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def handle_request(self, request: dict[str, Any]) -> dict[str, Any]:
        command = request.get("command")
        if command == "status":
            status = self.store.status(event_limit=int(request.get("events", 20)))
            run_id = request.get("run")
            if isinstance(run_id, int):
                status["runs"] = [row for row in status["runs"] if row["id"] == run_id]
                status["assignments"] = [
                    row for row in status["assignments"] if row["run_id"] == run_id
                ]
            if request.get("view") == "queue":
                return {
                    "dispatch_enabled": status["dispatch_enabled"],
                    "assignments": status["assignments"],
                    "pending_updates": status["pending_updates"],
                    "holds": status["holds"],
                }
            if request.get("view") == "capabilities":
                return {
                    "controller_state": status["controller_state"],
                    "dispatch_enabled": status["dispatch_enabled"],
                    "projects": status["projects"],
                    "slot_usage": status["slot_usage"],
                    "policies": status["policies"],
                }
            return status
        if command == "finish":
            return accept_finish(
                self.store,
                native_thread_id=_thread_identity(request),
                outcome_kind=str(request.get("outcome")),
                options=(
                    request.get("options")
                    if isinstance(request.get("options"), dict)
                    else {}
                ),
            )
        if command == "intake":
            payload = request.get("task")
            if not isinstance(payload, dict):
                raise StoreError("intake requires a task object")
            if not payload.get("project"):
                payload["project"] = self._infer_project(request.get("thread_id"))
            return await asyncio.to_thread(
                file_task,
                self.store,
                self.beads,
                task_from_payload(payload, intake_key=request.get("intake_key")),
            )
        if command == "intake_graph":
            graph = request.get("graph")
            if not isinstance(graph, dict):
                raise StoreError("graph intake requires an object")
            if not graph.get("project"):
                graph["project"] = self._infer_project(request.get("thread_id"))
            return await asyncio.to_thread(
                file_graph,
                self.store,
                self.beads,
                graph,
                group_id=request.get("intake_key"),
            )
        if command == "weaver_register":
            return await self._register_weaver(request)
        if command == "instructions":
            thread = _thread_identity(request)
            action = self.store.current_action(thread)
            task = self.store.row(
                "SELECT * FROM tasks WHERE id = ?", (action["task_id"],)
            )
            return {
                "instructions": build_prompt(
                    action_kind=action["kind"], task=task or {}, action=action
                )
            }
        if command == "archon":
            task = self.store.row(
                "SELECT * FROM tasks WHERE role = 'archon' AND state NOT IN ('retired','archived')"
            )
            if task is None:
                raise StoreError("no current Archon; run ./scripts/setup")
            return {"thread_id": task["native_thread_id"], "title": task["title"]}
        if command == "specialist":
            return await self._request_specialist(request)
        if command == "setup_initialize":
            return await self._setup_initialize()
        if command == "reboot":
            return await self._begin_reboot(str(request.get("mode")))
        if command == "enable_dispatch":
            self.store.execute(
                "INSERT INTO meta(key, value) VALUES ('dispatch_enabled', '1') ON CONFLICT(key) DO UPDATE SET value = '1'"
            )
            return {"dispatch_enabled": True}
        raise StoreError(f"unknown controller command: {command!r}")

    def _infer_project(self, thread_id: object) -> str:
        if isinstance(thread_id, str):
            task = self.store.row(
                "SELECT project_id FROM tasks WHERE native_thread_id = ?", (thread_id,)
            )
            if task and task["project_id"]:
                return str(task["project_id"])
        projects = self.store.rows("SELECT project_id FROM projects WHERE enabled = 1")
        if len(projects) == 1:
            return str(projects[0]["project_id"])
        raise StoreError("project is ambiguous; pass --project")

    async def _register_weaver(self, request: dict[str, Any]) -> dict[str, Any]:
        thread_id = _thread_identity(request)
        project_id = request.get("project") or self._infer_project(thread_id)
        task = self.store.register_task(
            native_thread_id=thread_id,
            role="weaver",
            description=str(request.get("description") or "Task intake"),
            model=str(request.get("model") or "gpt-5.6-sol"),
            reasoning_effort=str(request.get("effort") or "high"),
            project_id=project_id,
            state="provisioning",
        )
        await self.runtime.set_name(thread_id, task["title"])
        existing: dict[str, Any] | None = None
        if request.get("writable", True):
            existing = self.store.row(
                "SELECT * FROM actions WHERE task_id = ? AND state NOT IN ('processed','canceled')",
                (task["id"],),
            )
            if existing is None:
                timestamp = utc_now()
                cursor = self.store.execute(
                    "INSERT INTO actions(task_id, kind, payload, state, created_at, updated_at) VALUES (?, 'weaver', ?, 'active', ?, ?)",
                    (
                        task["id"],
                        json.dumps({"mode": "intake", "project": project_id}),
                        timestamp,
                        timestamp,
                    ),
                )
                existing = self.store.row(
                    "SELECT * FROM actions WHERE id = ?", (cursor.lastrowid,)
                )
        self.store.execute(
            "UPDATE tasks SET state = 'active', updated_at = ? WHERE id = ?",
            (utc_now(), task["id"]),
        )
        return {
            "thread_id": thread_id,
            "title": task["title"],
            "role_number": task["role_number"],
            "instructions": build_prompt(
                action_kind="weaver", task=task, action=existing or {"payload": "{}"}
            ),
        }

    async def _request_specialist(self, request: dict[str, Any]) -> dict[str, Any]:
        kind = request.get("kind")
        if kind not in {"sage", "inquisitor"}:
            raise StoreError("specialist kind must be sage or inquisitor")
        raw_scope = request.get("scope")
        scope = json.loads(raw_scope) if isinstance(raw_scope, str) else raw_scope
        if not isinstance(scope, dict) or not isinstance(scope.get("projects"), list):
            raise StoreError("specialist scope is invalid")
        requested_projects = scope["projects"]
        if not requested_projects and kind == "inquisitor":
            requested_projects = [
                row["project_id"]
                for row in self.store.rows(
                    "SELECT project_id FROM projects WHERE enabled = 1 ORDER BY project_id"
                )
            ]
        if kind == "inquisitor" and not requested_projects:
            raise StoreError("global Inquisitor has no enabled projects to review")
        for project_id in requested_projects:
            project = self.store.row(
                "SELECT enabled FROM projects WHERE project_id = ?", (project_id,)
            )
            if project is None or not project["enabled"]:
                raise StoreError(f"specialist project {project_id!r} is unavailable")
        scope = {
            "global": not bool(scope["projects"]),
            "projects": requested_projects,
        }
        timestamp = utc_now()
        cursor = self.store.execute(
            "INSERT INTO occurrences(kind, scope, authority, prompt, state, created_at, updated_at) VALUES (?, ?, 'explicit-local-request', ?, 'queued', ?, ?)",
            (kind, json.dumps(scope), request.get("prompt"), timestamp, timestamp),
        )
        return {
            "request_id": int(cursor.lastrowid),
            "state": "queued",
            "approval_required": False,
        }

    async def _setup_initialize(self) -> dict[str, Any]:
        if not self.runtime.ready:
            raise StoreError("shared app-server is not connected")
        models = await self.runtime.list_models()
        supported = {str(item.get("model") or item.get("id")): item for item in models}
        if self.config.archon_model not in supported:
            raise StoreError(
                f"configured Archon model {self.config.archon_model!r} is unsupported"
            )
        model = supported[self.config.archon_model]
        efforts = {
            str(item.get("reasoningEffort") or item.get("effort"))
            for item in model.get("supportedReasoningEfforts", [])
            if isinstance(item, dict)
        }
        if efforts and self.config.archon_reasoning_effort not in efforts:
            raise StoreError(
                f"configured Archon effort {self.config.archon_reasoning_effort!r} is unsupported"
            )
        await self._verify_projects()
        await self._setup_smoke_check()
        archon = self.store.row(
            "SELECT * FROM tasks WHERE role = 'archon' AND state NOT IN ('retired','archived')"
        )
        if archon is None:
            context = {
                "project_id": self.config.projects[0].project_id,
                "repo_path": self.config.source_root,
                "codex_project_id": self.config.projects[0].codex_project_id,
            }
            archon = await self._provision_task(
                role="archon",
                description="",
                project=context,
                model=str(self.config.archon_model),
                effort=str(self.config.archon_reasoning_effort),
            )
        policies = self.store.rows("SELECT * FROM policies WHERE active = 1")
        capacities = self.store.row("SELECT value FROM meta WHERE key = 'global_limit'")
        if not policies or capacities is None:
            existing = self.store.row(
                "SELECT * FROM actions WHERE task_id = ? AND state NOT IN ('processed','canceled')",
                (archon["id"],),
            )
            if existing is None:
                timestamp = utc_now()
                payload = {
                    "purpose": "initial_policies",
                    "projects": [
                        project.project_id for project in self.config.projects
                    ],
                    "required": "Set explicit global/project capacity and recurring Sage/Inquisitor policies.",
                }
                cursor = self.store.execute(
                    "INSERT INTO actions(task_id, kind, payload, state, created_at, updated_at) VALUES (?, 'archon', ?, 'pending', ?, ?)",
                    (archon["id"], json.dumps(payload), timestamp, timestamp),
                )
                existing = self.store.row(
                    "SELECT * FROM actions WHERE id = ?", (cursor.lastrowid,)
                )
                assert existing is not None
                await self._dispatch_action(existing, task=archon)
            return {
                "ready": False,
                "archon": archon["native_thread_id"],
                "condition": "Archon policies pending",
            }
        self._update_readiness()
        return {"ready": True, "archon": archon["native_thread_id"]}

    async def _setup_smoke_check(self) -> None:
        if self.store.row("SELECT value FROM meta WHERE key = 'desktop_smoke_check'"):
            return
        project = self.store.row(
            "SELECT * FROM projects WHERE enabled = 1 ORDER BY project_id LIMIT 1"
        )
        if project is None:
            return
        operation = self.store.create_operation(
            "setup_runtime_smoke",
            project["project_id"],
            {
                "cwd": project["repo_path"],
                "project_id": project["codex_project_id"],
            },
        )
        self.store.execute(
            "UPDATE external_operations SET state = 'sent' WHERE id = ?", (operation,)
        )
        try:
            result = await self.runtime.create_thread(
                cwd=project["repo_path"],
                model=self.config.archon_model or "gpt-5.6-sol",
                project_id=project["codex_project_id"],
                base_instructions="Disposable Fulcrum installation visibility check. Do not start work.",
            )
            thread_id = result["thread"]["id"]
            self.store.execute(
                "UPDATE external_operations SET native_id = ?, updated_at = ? WHERE id = ?",
                (thread_id, utc_now(), operation),
            )
            await self.runtime.set_name(thread_id, "Fulcrum setup visibility check")
            turn_id = await self.runtime.start_turn(
                thread_id,
                "Reply with exactly: Fulcrum runtime check passed. Do not use tools or modify files.",
                cwd=project["repo_path"],
                model=self.config.archon_model or "gpt-5.6-sol",
                effort=self.config.archon_reasoning_effort or "medium",
                correlation=f"fulcrum-operation-{operation}",
            )
            deadline = asyncio.get_running_loop().time() + 180
            observed: dict[str, Any] | None = None
            while asyncio.get_running_loop().time() < deadline:
                try:
                    current = await self.runtime.read_thread(thread_id)
                except AppServerError:
                    await asyncio.sleep(0.25)
                    continue
                facts = thread_facts(current)
                if (
                    facts["last_turn_id"] == turn_id
                    and facts["last_turn_terminal"]
                    and facts["helpers_terminal"]
                    and facts["runtime_status"] == "idle"
                ):
                    observed = current
                    break
                await asyncio.sleep(0.25)
            if observed is None:
                raise AppServerError(
                    "runtime smoke turn did not finish within 180 seconds"
                )
            if observed.get("name") != "Fulcrum setup visibility check":
                raise AppServerError("runtime smoke task title was not retained")
            if observed.get("projectId") != project["codex_project_id"]:
                raise AppServerError(
                    "setup smoke task project binding was not retained"
                )
            await self.runtime.archive(thread_id)
            self.store.execute(
                "UPDATE external_operations SET state = 'complete', native_id = ?, result_json = ?, updated_at = ? WHERE id = ?",
                (thread_id, json.dumps(observed), utc_now(), operation),
            )
            self.store.execute(
                "INSERT INTO meta(key, value) VALUES ('desktop_smoke_check', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (utc_now(),),
            )
        except Exception as error:
            self._operation_failed(operation, error)
            raise StoreError(
                f"shared runtime visibility smoke check failed: {error}"
            ) from error

    async def _verify_projects(self) -> None:
        tollgate_repositories: Any = None
        if self.tollgate is not None:
            tollgate_repositories = await asyncio.to_thread(self.tollgate.repositories)
        codex_projects = await self.runtime.list_projects()
        resolved_projects = []
        for project in self.config.projects:
            codex_id = project.codex_project_id or _find_codex_project_id(
                codex_projects, project.repo_path
            )
            tollgate_id = project.tollgate_repo_id or _find_tollgate_repository_id(
                tollgate_repositories, project.repo_path
            )
            project = replace(
                project,
                codex_project_id=codex_id,
                tollgate_repo_id=tollgate_id,
            )
            resolved_projects.append(project)
            problems: list[str] = []
            root = Path(project.repo_path)
            if not (root / ".git").exists():
                problems.append("Git repository is unavailable")
            if not project.codex_project_id:
                problems.append("Codex project identity is unavailable")
            if not project.tollgate_repo_id:
                problems.append("Tollgate repository identity is unavailable")
            elif not _contains_id(tollgate_repositories, project.tollgate_repo_id):
                problems.append(
                    "Tollgate repository identity is unavailable or mismatched"
                )
            self.store.execute(
                "UPDATE projects SET codex_project_id = ?, tollgate_repo_id = ?, enabled = ?, condition = ? WHERE project_id = ?",
                (
                    codex_id,
                    tollgate_id,
                    int(not problems),
                    "; ".join(problems) or None,
                    project.project_id,
                ),
            )
        if resolved_projects != self.config.projects:
            self.config = replace(self.config, projects=resolved_projects)
            save_installation(self.paths.config_file, self.config)

    def _update_readiness(self) -> None:
        global_limit = self.store.row(
            "SELECT value FROM meta WHERE key = 'global_limit'"
        )
        projects = self.store.rows("SELECT * FROM projects")
        policies = self.store.rows("SELECT * FROM policies WHERE active = 1")
        archon = self.store.row(
            "SELECT * FROM tasks WHERE role = 'archon' AND state NOT IN ('retired','archived')"
        )
        ready = bool(
            global_limit
            and projects
            and all(row["enabled"] for row in projects)
            and policies
            and archon
            and self.runtime.ready
        )
        self.store.execute(
            "INSERT INTO meta(key, value) VALUES ('dispatch_enabled', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("1" if ready else "0",),
        )

    async def _queue_proposals(self) -> None:
        archon = self.store.row(
            "SELECT * FROM tasks WHERE role = 'archon' AND state NOT IN ('retired','archived')"
        )
        if archon is None:
            return
        beads = self.store.rows(
            """SELECT b.* FROM beads b WHERE b.activation = 'pending' AND b.publication_state = 'complete'
               AND NOT EXISTS (SELECT 1 FROM assignments a WHERE a.bead_id = b.bead_id AND a.stage NOT IN ('completed','canceled'))
               AND NOT EXISTS (SELECT 1 FROM intake_group_beads igb JOIN intake_groups ig ON ig.id = igb.group_id WHERE igb.bead_id = b.bead_id AND ig.state != 'complete')
               ORDER BY b.created_at, b.bead_id"""
        )
        timestamp = utc_now()
        for bead in beads:
            content = json.dumps(
                {
                    "bead_id": bead["bead_id"],
                    "project": bead["project_id"],
                    "title": bead["title"],
                    "scope": bead["description"],
                },
                sort_keys=True,
            )
            self.store.execute(
                """INSERT INTO updates(recipient_task_id, identity, content, actionable, state, created_at, updated_at)
                   VALUES (?, ?, ?, 1, 'retained', ?, ?)
                   ON CONFLICT(recipient_task_id, identity) DO UPDATE SET content = excluded.content,
                   updated_at = excluded.updated_at WHERE updates.state = 'retained'""",
                (
                    archon["id"],
                    f"proposal:{bead['bead_id']}",
                    content,
                    timestamp,
                    timestamp,
                ),
            )

    async def _deliver_update_batch(self) -> None:
        archon = self.store.row(
            "SELECT * FROM tasks WHERE role = 'archon' AND state = 'idle'"
        )
        if archon is None:
            return
        current = self.store.row(
            "SELECT 1 FROM actions WHERE task_id = ? AND state NOT IN ('processed','canceled')",
            (archon["id"],),
        )
        if current is not None:
            return
        updates = self.store.rows(
            "SELECT * FROM updates WHERE recipient_task_id = ? AND state = 'retained' AND actionable = 1 ORDER BY id",
            (archon["id"],),
        )
        if not updates:
            return
        timestamp = utc_now()
        payload = {
            "batch_items": [
                {
                    "update_id": row["id"],
                    "identity": row["identity"],
                    "content": json.loads(row["content"]),
                }
                for row in updates
            ]
        }
        with self.store.transaction() as connection:
            batch_cursor = connection.execute(
                "INSERT INTO batches(recipient_task_id, state, created_at, updated_at) VALUES (?, 'frozen', ?, ?)",
                (archon["id"], timestamp, timestamp),
            )
            action_cursor = connection.execute(
                "INSERT INTO actions(task_id, kind, payload, state, created_at, updated_at) VALUES (?, 'archon', ?, 'pending', ?, ?)",
                (archon["id"], json.dumps(payload), timestamp, timestamp),
            )
            connection.execute(
                "UPDATE batches SET action_id = ? WHERE id = ?",
                (action_cursor.lastrowid, batch_cursor.lastrowid),
            )
            for update in updates:
                connection.execute(
                    "INSERT INTO batch_updates(batch_id, update_id) VALUES (?, ?)",
                    (batch_cursor.lastrowid, update["id"]),
                )
                connection.execute(
                    "UPDATE updates SET state = 'batched', updated_at = ? WHERE id = ?",
                    (timestamp, update["id"]),
                )
        action = self.store.row(
            "SELECT * FROM actions WHERE id = ?", (action_cursor.lastrowid,)
        )
        assert action is not None
        await self._dispatch_action(action, task=archon)

    async def _manage_interviews(self) -> None:
        now = utc_now()
        for interview in self.store.rows(
            "SELECT * FROM interviews WHERE state = 'queued' AND deadline_at <= ?",
            (now,),
        ):
            action = self.store.row(
                "SELECT * FROM actions WHERE occurrence_id = ? AND task_id = ? AND kind = 'interview' AND state NOT IN ('processed','canceled')",
                (interview["occurrence_id"], interview["subject_task_id"]),
            )
            if action is not None and action["state"] in {
                "starting",
                "uncertain",
                "active",
            }:
                continue
            if action is not None:
                self.store.execute(
                    "DELETE FROM reservations WHERE action_id = ?", (action["id"],)
                )
                self.store.execute(
                    "UPDATE actions SET state = 'canceled', condition = 'interview deadline passed before start', updated_at = ? WHERE id = ?",
                    (now, action["id"]),
                )
            self.store.execute(
                "UPDATE interviews SET state = 'expired', updated_at = ? WHERE id = ?",
                (now, interview["id"]),
            )

        for interview in self.store.rows(
            "SELECT * FROM interviews WHERE state = 'queued' AND deadline_at > ? ORDER BY id",
            (now,),
        ):
            await self._start_interview(interview)

        for interview in self.store.rows(
            "SELECT * FROM interviews WHERE state IN ('answered','expired','failed') AND prior_archived = 1"
        ):
            task = self.store.row(
                "SELECT * FROM tasks WHERE id = ?", (interview["subject_task_id"],)
            )
            if (
                task
                and not task["archived"]
                and task["last_turn_terminal"]
                and task["helpers_terminal"]
            ):
                self.store.execute(
                    "INSERT OR IGNORE INTO obligations(kind, identity, target, state, created_at, updated_at) VALUES ('archive', ?, ?, 'pending', ?, ?)",
                    (
                        f"interview:{interview['id']}",
                        task["native_thread_id"],
                        now,
                        now,
                    ),
                )

        for occurrence in self.store.rows(
            "SELECT * FROM occurrences WHERE kind = 'sage' AND state = 'collecting' ORDER BY id"
        ):
            interviews = self.store.rows(
                "SELECT i.*, t.title FROM interviews i JOIN tasks t ON t.id = i.subject_task_id WHERE i.occurrence_id = ? ORDER BY i.id",
                (occurrence["id"],),
            )
            deadline_passed = bool(
                occurrence["deadline_at"] and occurrence["deadline_at"] <= now
            )
            resolved = all(
                interview["state"] in {"answered", "expired", "failed"}
                for interview in interviews
            )
            if resolved or deadline_passed:
                await self._resume_sage_with_interviews(occurrence, interviews)

    async def _start_interview(self, interview: dict[str, Any]) -> None:
        subject = self.store.row(
            "SELECT * FROM tasks WHERE id = ?", (interview["subject_task_id"],)
        )
        if subject is None or subject["state"] in {
            "active",
            "uncertain",
            "provisioning",
        }:
            return
        if subject["pair_id"] is not None and self.store.row(
            "SELECT 1 FROM reservations WHERE pair_id = ?", (subject["pair_id"],)
        ):
            return
        project_ids = [subject["project_id"]] if subject["project_id"] else []
        if not self._specialist_capacity_available(project_ids):
            return
        timestamp = utc_now()
        cursor = self.store.execute(
            "INSERT INTO actions(task_id, occurrence_id, kind, payload, state, check_after, created_at, updated_at) VALUES (?, ?, 'interview', ?, 'pending', ?, ?, ?)",
            (
                subject["id"],
                interview["occurrence_id"],
                json.dumps(
                    {
                        "interview_id": interview["id"],
                        "question": interview["request"],
                        "instruction": "Answer only this interview; do not resume prior implementation work.",
                    }
                ),
                (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=self.config.turn_check_after_seconds)
                )
                .isoformat()
                .replace("+00:00", "Z"),
                timestamp,
                timestamp,
            ),
        )
        action = self.store.row(
            "SELECT * FROM actions WHERE id = ?", (cursor.lastrowid,)
        )
        assert action is not None
        self.store.execute(
            "INSERT INTO reservations(action_id, pair_id, global_slots, project_ids, state, created_at) VALUES (?, ?, 1, ?, 'reserved', ?)",
            (
                action["id"],
                subject["pair_id"],
                json.dumps(project_ids),
                timestamp,
            ),
        )
        self.store.execute(
            "UPDATE interviews SET state = 'starting', updated_at = ? WHERE id = ?",
            (timestamp, interview["id"]),
        )
        try:
            await self._dispatch_action(action, task=subject)
            self.store.execute(
                "UPDATE interviews SET state = 'active', updated_at = ? WHERE id = ?",
                (utc_now(), interview["id"]),
            )
        except Exception as error:
            self.store.execute(
                "UPDATE interviews SET state = 'failed', updated_at = ? WHERE id = ?",
                (utc_now(), interview["id"]),
            )
            self.store.execute(
                "UPDATE actions SET state = 'failed', condition = ?, updated_at = ? WHERE id = ?",
                (str(error), utc_now(), action["id"]),
            )

    async def _resume_sage_with_interviews(
        self, occurrence: dict[str, Any], interviews: list[dict[str, Any]]
    ) -> None:
        prior = self.store.row(
            "SELECT task_id FROM actions WHERE occurrence_id = ? AND kind = 'specialist' ORDER BY id LIMIT 1",
            (occurrence["id"],),
        )
        if prior is None:
            return
        task = self.store.row("SELECT * FROM tasks WHERE id = ?", (prior["task_id"],))
        if task is None or task["state"] != "idle":
            return
        existing = self.store.row(
            "SELECT 1 FROM actions WHERE occurrence_id = ? AND kind = 'specialist' AND state NOT IN ('processed','canceled')",
            (occurrence["id"],),
        )
        if existing is not None:
            return
        scope = _occurrence_scope(occurrence["scope"])
        project_ids = scope.get("projects", []) if isinstance(scope, dict) else []
        if not self._specialist_capacity_available(project_ids):
            return
        answers = [
            {
                "subject": row["title"],
                "answer": json.loads(row["answer_json"]),
            }
            for row in interviews
            if row["state"] == "answered" and row["answer_json"]
        ]
        missing = [row["title"] for row in interviews if row["state"] != "answered"]
        timestamp = utc_now()
        cursor = self.store.execute(
            "INSERT INTO actions(task_id, occurrence_id, kind, payload, state, check_after, created_at, updated_at) VALUES (?, ?, 'specialist', ?, 'pending', ?, ?, ?)",
            (
                task["id"],
                occurrence["id"],
                json.dumps(
                    {
                        "continuation": "final report after the single interview round",
                        "answers": answers,
                        "missing_evidence": missing,
                    }
                ),
                (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=self.config.turn_check_after_seconds)
                )
                .isoformat()
                .replace("+00:00", "Z"),
                timestamp,
                timestamp,
            ),
        )
        action = self.store.row(
            "SELECT * FROM actions WHERE id = ?", (cursor.lastrowid,)
        )
        assert action is not None
        self.store.execute(
            "INSERT INTO reservations(action_id, global_slots, project_ids, state, created_at) VALUES (?, 1, ?, 'reserved', ?)",
            (action["id"], json.dumps(project_ids), timestamp),
        )
        self.store.execute(
            "UPDATE occurrences SET state = 'active', updated_at = ? WHERE id = ?",
            (timestamp, occurrence["id"]),
        )
        await self._dispatch_action(action, task=task)

    def _specialist_capacity_available(self, project_ids: list[str]) -> bool:
        limits = capacity(self.store)
        if (
            limits["global_limit"] is None
            or limits["global_usage"] >= limits["global_limit"]
        ):
            return False
        for project_id in project_ids:
            project = self.store.row(
                "SELECT enabled FROM projects WHERE project_id = ?", (project_id,)
            )
            project_limit = limits["project_limits"].get(project_id)
            if (
                project is None
                or not project["enabled"]
                or project_limit is None
                or limits["project_usage"].get(project_id, 0) >= project_limit
            ):
                return False
            if self.store.row(
                "SELECT 1 FROM holds WHERE released_at IS NULL AND (scope = 'global' OR (scope = 'project' AND target = ?))",
                (project_id,),
            ):
                return False
        return (
            self.store.row(
                "SELECT 1 FROM holds WHERE released_at IS NULL AND scope = 'global'"
            )
            is None
        )

    async def _start_specialists(self) -> None:
        for occurrence in self.store.rows(
            "SELECT * FROM occurrences WHERE state = 'queued' ORDER BY id"
        ):
            scope = (
                json.loads(occurrence["scope"])
                if occurrence["scope"] and str(occurrence["scope"]).startswith("{")
                else {
                    "global": occurrence["scope"] is None,
                    "projects": [occurrence["scope"]] if occurrence["scope"] else [],
                }
            )
            project_ids = scope.get("projects", [])
            if not self._specialist_capacity_available(project_ids):
                continue
            project_id = project_ids[0] if len(project_ids) == 1 else None
            context = (
                self.store.row(
                    "SELECT * FROM projects WHERE project_id = ?", (project_id,)
                )
                if project_id
                else self.store.row(
                    "SELECT * FROM projects ORDER BY project_id LIMIT 1"
                )
            )
            if context is None:
                continue
            task = await self._provision_task(
                role=occurrence["kind"],
                description=(
                    "Workflow postmortem"
                    if occurrence["kind"] == "sage"
                    else "Architecture review"
                ),
                project=context,
                model="gpt-5.6-sol",
                effort="high",
            )
            timestamp = utc_now()
            cursor = self.store.execute(
                "INSERT INTO actions(task_id, occurrence_id, kind, payload, state, check_after, created_at, updated_at) VALUES (?, ?, 'specialist', ?, 'pending', ?, ?, ?)",
                (
                    task["id"],
                    occurrence["id"],
                    json.dumps({"scope": scope, "prompt": occurrence["prompt"]}),
                    (
                        datetime.now(timezone.utc)
                        + timedelta(seconds=self.config.turn_check_after_seconds)
                    )
                    .isoformat()
                    .replace("+00:00", "Z"),
                    timestamp,
                    timestamp,
                ),
            )
            action = self.store.row(
                "SELECT * FROM actions WHERE id = ?", (cursor.lastrowid,)
            )
            if action is None:
                raise StoreError("specialist action was not retained")
            self.store.execute(
                "INSERT INTO reservations(action_id, global_slots, project_ids, state, created_at) VALUES (?, 1, ?, 'reserved', ?)",
                (action["id"], json.dumps(project_ids), timestamp),
            )
            self.store.execute(
                "UPDATE occurrences SET state = 'active', updated_at = ? WHERE id = ?",
                (timestamp, occurrence["id"]),
            )
            await self._dispatch_action(action, task=task)

    async def _publish_occurrences(self) -> None:
        for occurrence in self.store.rows(
            "SELECT * FROM occurrences WHERE state = 'publishing'"
        ):
            report = json.loads(occurrence["report_json"] or "{}")
            try:
                for position, finding in enumerate(report.get("findings", [])):
                    if finding.get("existing_bead_id"):
                        bead = self.store.row(
                            "SELECT bead_id FROM beads WHERE bead_id = ?",
                            (finding["existing_bead_id"],),
                        )
                        if bead is None:
                            raise StoreError(
                                f"finding names unknown bead {finding['existing_bead_id']}"
                            )
                        await asyncio.to_thread(
                            self.beads.comment,
                            finding["existing_bead_id"],
                            f"Specialist evidence: {finding['evidence']}\nExpected benefit: {finding['expected_benefit']}",
                        )
                        continue
                    task_payload = {
                        "project": finding["project"],
                        "title": finding.get("title")
                        or finding["problem"].splitlines()[0][:100],
                        "description": f"{finding['problem']}\n\nEvidence: {finding['evidence']}\n\nExpected benefit: {finding['expected_benefit']}\n\nAcceptance criteria: {finding['acceptance_criteria']}",
                    }
                    await asyncio.to_thread(
                        file_task,
                        self.store,
                        self.beads,
                        task_from_payload(
                            task_payload,
                            intake_key=f"finding:{occurrence['id']}:{position}",
                        ),
                    )
                self.store.execute(
                    "UPDATE occurrences SET state = 'complete', updated_at = ? WHERE id = ?",
                    (utc_now(), occurrence["id"]),
                )
                if occurrence["policy_id"] is not None:
                    policy = self.store.row(
                        "SELECT * FROM policies WHERE id = ?",
                        (occurrence["policy_id"],),
                    )
                    if policy and policy["anchor_at"] and policy["cadence_seconds"]:
                        anchor = datetime.fromisoformat(
                            policy["anchor_at"].replace("Z", "+00:00")
                        )
                        due = next_cadence(
                            anchor,
                            int(policy["cadence_seconds"]),
                            datetime.now(timezone.utc),
                        )
                        self.store.execute(
                            "UPDATE policies SET next_due_at = ? WHERE id = ?",
                            (
                                due.isoformat().replace("+00:00", "Z"),
                                policy["id"],
                            ),
                        )
                action = self.store.row(
                    "SELECT task_id FROM actions WHERE occurrence_id = ? ORDER BY id DESC LIMIT 1",
                    (occurrence["id"],),
                )
                if action:
                    task = self.store.row(
                        "SELECT native_thread_id FROM tasks WHERE id = ?",
                        (action["task_id"],),
                    )
                    if task:
                        self.store.execute(
                            "INSERT OR IGNORE INTO obligations(kind, identity, target, state, created_at, updated_at) VALUES ('archive', ?, ?, 'pending', ?, ?)",
                            (
                                str(action["task_id"]),
                                task["native_thread_id"],
                                utc_now(),
                                utc_now(),
                            ),
                        )
            except Exception as error:
                self.store.execute(
                    "UPDATE occurrences SET state = 'failed', updated_at = ? WHERE id = ?",
                    (utc_now(), occurrence["id"]),
                )
                self.store.execute(
                    "INSERT OR IGNORE INTO obligations(kind, identity, target, state, detail, created_at, updated_at) VALUES ('finding_publication', ?, ?, 'failed', ?, ?, ?)",
                    (
                        str(occurrence["id"]),
                        str(occurrence["id"]),
                        str(error),
                        utc_now(),
                        utc_now(),
                    ),
                )

    async def _begin_reboot(
        self, mode: str, *, existing: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if mode not in {"soft", "hard", "reset"}:
            raise StoreError("reboot mode must be soft, hard, or reset")
        record = existing or {
            "mode": mode,
            "state": "stopping",
            "started_at": utc_now(),
            "interrupts_sent": [],
        }
        self.paths.reboot_record.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.paths.reboot_record.write_text(
            json.dumps(record, indent=2) + "\n", encoding="utf-8"
        )
        self.starts_enabled = False
        self.store.execute(
            "INSERT INTO meta(key, value) VALUES ('dispatch_enabled', '0') ON CONFLICT(key) DO UPDATE SET value = '0'"
        )
        candidates = self.store.rows(
            "SELECT * FROM tasks WHERE state NOT IN ('retired','archived')"
        )
        for task in candidates:
            try:
                await self._refresh_task(task)
            except AppServerError:
                pass
        active = self.store.rows(
            "SELECT * FROM tasks WHERE state NOT IN ('retired','archived') AND (last_turn_terminal = 0 OR helpers_terminal = 0)"
        )
        if mode == "soft" and active:
            return {
                "complete": False,
                "mode": mode,
                "condition": "waiting for active turns and helpers",
                "active": [row["native_thread_id"] for row in active],
            }
        if mode in {"hard", "reset"}:
            sent = set(record.get("interrupts_sent", []))
            for task in active:
                action = self.store.row(
                    "SELECT native_turn_id FROM actions WHERE task_id = ? AND state IN ('starting','active','uncertain')",
                    (task["id"],),
                )
                if (
                    action
                    and action["native_turn_id"]
                    and action["native_turn_id"] not in sent
                ):
                    await self.runtime.interrupt(
                        task["native_thread_id"], action["native_turn_id"]
                    )
                    sent.add(action["native_turn_id"])
            record["interrupts_sent"] = sorted(sent)
            self.paths.reboot_record.write_text(
                json.dumps(record, indent=2) + "\n", encoding="utf-8"
            )
            for task in active:
                facts = await self._refresh_task(task)
                if not facts["last_turn_terminal"] or not facts["helpers_terminal"]:
                    return {
                        "complete": False,
                        "mode": mode,
                        "condition": "waiting for confirmed turn/helper termination",
                    }
        if mode == "reset":
            await self._reset_state()
        else:
            self.store.execute(
                "DELETE FROM reservations WHERE action_id IN (SELECT id FROM actions WHERE state NOT IN ('processed','canceled'))"
            )
            self.store.execute(
                "UPDATE actions SET state = 'canceled', condition = 'replaced by fleet reboot', updated_at = ? WHERE state NOT IN ('processed','canceled')",
                (utc_now(),),
            )
            for task in self.store.rows(
                "SELECT * FROM tasks WHERE state NOT IN ('retired','archived')"
            ):
                await self.runtime.archive(task["native_thread_id"])
                self.store.execute(
                    "UPDATE tasks SET state = 'retired', archived = 1, updated_at = ? WHERE id = ?",
                    (utc_now(), task["id"]),
                )
            self.store.execute(
                "UPDATE assignments SET executor_task_id = NULL, overseer_task_id = NULL, stage = CASE WHEN stage = 'reviewing' THEN 'review_pending' ELSE stage END, updated_at = ? WHERE stage NOT IN ('completed','canceled')",
                (utc_now(),),
            )
        initialized = await self._setup_initialize()
        if mode != "reset":
            for assignment in self.store.rows(
                "SELECT a.*, r.project_id FROM assignments a JOIN runs r ON r.id = a.run_id WHERE a.stage NOT IN ('queued','completed','canceled') ORDER BY a.id"
            ):
                await self._ensure_pair(assignment)
        self.paths.reboot_record.unlink(missing_ok=True)
        self.starts_enabled = True
        return {"complete": True, "mode": mode, **initialized}

    async def _reset_state(self) -> None:
        owned = self.store.rows(
            "SELECT a.worktree_path, p.tollgate_repo_id FROM assignments a JOIN runs r ON r.id = a.run_id JOIN projects p ON p.project_id = r.project_id WHERE a.worktree_path IS NOT NULL AND a.stage NOT IN ('completed','canceled')"
        )
        if owned and self.tollgate is None:
            raise StoreError(
                "reset incomplete; Tollgate is unavailable for owned worktree cleanup"
            )
        tollgate = self.tollgate
        for item in owned:
            assert tollgate is not None
            await asyncio.to_thread(
                tollgate.remove_worktree,
                item["tollgate_repo_id"],
                item["worktree_path"],
            )
        for task in self.store.rows(
            "SELECT * FROM tasks WHERE state NOT IN ('retired','archived')"
        ):
            await self.runtime.archive(task["native_thread_id"])
        await asyncio.to_thread(
            reset_brain,
            self.paths.brain_root,
            source_root=Path(self.config.source_root),
        )
        self.store.close()
        for suffix in ("", "-wal", "-shm"):
            Path(str(self.paths.database) + suffix).unlink(missing_ok=True)
        for child in (
            self.paths.logs_root,
            self.paths.state_root / "observations",
            self.paths.state_root / "assignments",
            self.paths.state_root / "progress",
            self.paths.state_root / "evidence",
            self.paths.state_root / "interviews",
            self.paths.state_root / "registry",
        ):
            if child.is_dir():
                import shutil

                shutil.rmtree(child)
        self.store = Store(self.paths.database)
        self._initialize_configuration()

    async def _source_watch_loop(self) -> None:
        source = Path(self.config.source_root) / "src" / "fulcrum"
        async for changes in awatch(source, debounce=250):
            if not any(str(path).endswith(".py") for _, path in changes):
                continue
            self.starts_enabled = False
            async with self.mutation_lock:
                self.store.event(
                    "source_reload",
                    "Python source changed; controller re-exec requested",
                )
                if os.environ.get("FULCRUM_DISABLE_REEXEC") == "1":
                    continue
                await self.runtime.close()
                server = self.server
                if server is not None:
                    server.close()
                    await server.wait_closed()
                self.store.close()
                os.execv(sys.executable, [sys.executable, "-m", "fulcrum.cli", "serve"])


def _thread_identity(request: dict[str, Any]) -> str:
    value = request.get("thread_id")
    if not isinstance(value, str) or not value.strip():
        raise StoreError("current Codex thread identity is unavailable")
    return value


def _find_string(value: Any, keys: set[str]) -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in keys and isinstance(child, str):
                return child
        for child in value.values():
            found = _find_string(child, keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_string(child, keys)
            if found is not None:
                return found
    return None


def _occurrence_scope(value: Any) -> dict[str, Any]:
    if isinstance(value, str) and value.startswith("{"):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    return {
        "global": value is None,
        "projects": [value] if isinstance(value, str) and value else [],
    }


def _find_candidate(
    value: Any, worktree_path: str | None, *, exclude_id: str | None = None
) -> dict[str, Any] | None:
    matches: list[dict[str, Any]] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            item = node.get("item")
            if isinstance(item, dict):
                metadata = item.get("metadata")
                if (
                    isinstance(metadata, dict)
                    and metadata.get("worktree_path") == worktree_path
                    and item.get("id") != exclude_id
                ):
                    merged = dict(item)
                    generation = node.get("generation")
                    if isinstance(generation, dict):
                        merged["tested_oid"] = generation.get("tested_oid")
                    matches.append(merged)
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    if not matches:
        return None
    return max(
        matches,
        key=lambda item: int(item.get("admission_sequence") or 0),
    )


def _oid(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("bytes"), str):
        return str(value["bytes"])
    return None


def _candidate_by_id(value: Any, candidate_id: str) -> dict[str, Any] | None:
    if isinstance(value, dict):
        item = value.get("item")
        if isinstance(item, dict) and item.get("id") == candidate_id:
            merged = dict(item)
            if isinstance(value.get("generation"), dict):
                merged["tested_oid"] = value["generation"].get("tested_oid")
            return merged
        if value.get("id") == candidate_id and "state" in value:
            return value
        for child in value.values():
            found = _candidate_by_id(child, candidate_id)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _candidate_by_id(child, candidate_id)
            if found is not None:
                return found
    return None


def _delivery_contract_failure(
    candidate: dict[str, Any] | None, status: dict[str, Any]
) -> str | None:
    if candidate is None:
        return "Tollgate no longer reports the retained candidate"
    if candidate.get("state") != "promoted":
        return f"Tollgate candidate ended in {candidate.get('state', 'unknown')}"
    configuration = status.get("configuration")
    remote_enabled = bool(
        isinstance(configuration, dict) and configuration.get("remote_enabled")
    )
    if remote_enabled and candidate.get("remote_state") != "synchronized":
        return "candidate was promoted but source synchronization is incomplete"
    if candidate.get("cleanup_state") not in {"completed", "not-eligible"}:
        return "candidate worktree cleanup is incomplete"
    if not candidate.get("certificate_id"):
        return "candidate has no retained Tollgate certificate"
    return None


def _find_codex_project_id(
    projects: list[dict[str, Any]], repo_path: str
) -> str | None:
    expected = str(Path(repo_path).resolve())
    matches = []
    for project in projects:
        roots = project.get("roots")
        if not isinstance(roots, list):
            continue
        if any(
            isinstance(root, dict)
            and isinstance(root.get("path"), str)
            and str(Path(root["path"]).resolve()) == expected
            for root in roots
        ):
            matches.append(project.get("id"))
    return matches[0] if len(matches) == 1 and isinstance(matches[0], str) else None


def _find_tollgate_repository_id(value: Any, repo_path: str) -> str | None:
    expected: str = str(Path(repo_path).resolve())
    matches: list[str] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            identifier = node.get("id")
            path = node.get("path")
            if (
                isinstance(identifier, str)
                and isinstance(path, str)
                and str(Path(path).resolve()) == expected
            ):
                matches.append(identifier)
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    unique = sorted(set(matches))
    return unique[0] if len(unique) == 1 else None


def _contains_id(value: Any, identifier: str) -> bool:
    if isinstance(value, dict):
        if value.get("id") == identifier:
            return True
        return any(_contains_id(child, identifier) for child in value.values())
    if isinstance(value, list):
        return any(_contains_id(child, identifier) for child in value)
    return False


async def run_controller(paths: RuntimePaths, config: InstallationConfig) -> None:
    controller = Controller(paths, config)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, controller.stop_event.set)
    await controller.start()
