"""Single-writer controller for runtime events, scheduling, and delivery."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import signal
import subprocess
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from watchfiles import awatch

from fulcrum.beads import Beads
from fulcrum.brain import BrainRepository
from dataclasses import replace

from fulcrum.config import InstallationConfig, RuntimePaths, save_installation
from fulcrum.intake import (
    file_graph,
    file_task,
    reconcile_beads_creation,
    task_from_payload,
)
from fulcrum.install import controller_program_arguments, install_control_plane
from fulcrum.lifecycle import accept_finish, observe_action_terminal
from fulcrum.kernel import (
    LeaseRequest,
    acquire_lease,
    invariant_violations,
    schedule_action_retry,
)
from fulcrum.prompts import (
    action_notice,
    build_context,
    role_instructions,
    weaver_instructions,
)
from fulcrum.readiness import progress_readiness, state_readiness
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

INHERITED_LOCK_FD_ENV = "FULCRUM_INHERITED_LOCK_FD"


class Controller:
    """Own all operational mutations for one configured environment."""

    def __init__(self, paths: RuntimePaths, config: InstallationConfig) -> None:
        self.paths = paths
        self.config = config
        self.lock_handle: Any = None
        self.acquire_process_lock()
        self.store = Store(paths.database, event_log=paths.logs_root / "workflow.jsonl")
        self.runtime = CodexRuntime(
            config.app_server_endpoint, event_handler=self._queue_runtime_event
        )
        self.tollgate: Tollgate | None = None
        self.beads = Beads(paths.brain_root)
        self.events: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
        self.mutation_lock = asyncio.Lock()
        self.server: asyncio.AbstractServer | None = None
        self.stop_event = asyncio.Event()
        self.advance_requested = asyncio.Event()
        self.starts_enabled = False
        self.critical_workers = {
            "events",
            "fallback",
            "advancement",
            "source-watch",
        }

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
            self.store.event(
                "project_reconciled",
                f"reconciled configured project {project.project_id}",
                entity_type="project",
                entity_id=project.project_id,
                detail={
                    "origin": "installation_config",
                    "cwd": project.repo_path,
                    "codex_project_id": project.codex_project_id,
                    "tollgate_repository_id": project.tollgate_repo_id,
                    "process_id": os.getpid(),
                },
                now=timestamp,
            )
        self.store.execute(
            "INSERT INTO meta(key, value) VALUES ('controller_state', 'starting') ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        )
        self.store.event("controller_starting", "controller is starting", now=timestamp)

    def acquire_process_lock(self) -> None:
        if self.lock_handle is not None:
            return
        self.paths.control_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        inherited_fd = os.environ.pop(INHERITED_LOCK_FD_ENV, None)
        if inherited_fd is not None:
            try:
                descriptor = int(inherited_fd)
                descriptor_stat = os.fstat(descriptor)
                lock_stat = self.paths.lock.stat()
                if (descriptor_stat.st_dev, descriptor_stat.st_ino) != (
                    lock_stat.st_dev,
                    lock_stat.st_ino,
                ):
                    raise ValueError("descriptor does not identify the controller lock")
                handle = os.fdopen(descriptor, "a+", closefd=True)
            except (OSError, TypeError, ValueError) as error:
                raise StoreError(
                    f"invalid inherited controller lock for {self.paths.lock}"
                ) from error
            os.set_inheritable(handle.fileno(), True)
            self.lock_handle = handle
            return
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
        self._initialize_configuration()
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
        workers = {
            "events": self._event_loop,
            "fallback": self._fallback_loop,
            "advancement": self._advancement_loop,
            "source-watch": self._source_watch_loop,
        }
        tasks = [
            asyncio.create_task(
                self._supervise_worker(name, worker), name=f"fulcrum-{name}"
            )
            for name, worker in workers.items()
        ]
        self.advance_requested.set()
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
            if self.lock_handle is not None:
                self.lock_handle.close()
                self.lock_handle = None

    async def _supervise_worker(self, name: str, worker: Any) -> None:
        """Restart a failed critical loop and make the failure durably visible."""

        while not self.stop_event.is_set():
            self.store.heartbeat(name, state="starting")
            try:
                self.store.heartbeat(name)
                await worker()
                if not self.stop_event.is_set():
                    raise RuntimeError(f"critical worker {name} returned unexpectedly")
            except asyncio.CancelledError:
                self.store.heartbeat(name, state="stopped")
                raise
            except BaseException as error:
                stack = traceback.format_exc()
                self.starts_enabled = False
                self.store.execute(
                    "INSERT INTO meta(key, value) VALUES ('dispatch_enabled', '0') ON CONFLICT(key) DO UPDATE SET value = '0'"
                )
                self.store.heartbeat(
                    name,
                    state="degraded",
                    error=str(error),
                    traceback_text=stack,
                )
                self.store.event(
                    "critical_worker_failed",
                    f"{name}: {error}",
                    entity_type="worker",
                    entity_id=name,
                    detail={"traceback": stack},
                )
                await asyncio.sleep(1)

    async def _connect_runtime(self) -> None:
        try:
            await self.runtime.connect()
        except Exception as error:
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
            self.store.heartbeat("events")
            method, params = await self.events.get()
            try:
                async with self.mutation_lock:
                    await self._handle_runtime_event(method, params)
                    self.advance_requested.set()
            except Exception as error:
                self.store.event(
                    "event_error",
                    f"{method}: {error}",
                    detail={"traceback": traceback.format_exc()},
                )

    async def _advancement_loop(self) -> None:
        while True:
            self.store.heartbeat("advancement")
            try:
                await asyncio.wait_for(self.advance_requested.wait(), timeout=5)
            except TimeoutError:
                pass
            self.advance_requested.clear()
            if self.mutation_lock.locked():
                self.advance_requested.set()
                continue
            async with self.mutation_lock:
                if not self.runtime.ready:
                    await self._connect_runtime()
                await self.reconcile()
                if await self._maybe_refresh_source():
                    continue
                await self.advance()

    async def _handle_runtime_event(self, method: str, params: dict[str, Any]) -> None:
        if method == "fulcrum/runtime/disconnected":
            self.starts_enabled = False
            self.store.execute(
                "INSERT INTO meta(key, value) VALUES ('dispatch_enabled', '0') ON CONFLICT(key) DO UPDATE SET value = '0'"
            )
            self.store.event(
                "runtime_disconnected",
                str(params.get("error") or "app-server connection lost"),
                entity_type="capability",
                entity_id="app_server",
            )
            self.advance_requested.set()
            return
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
                    action["reminder_sent"] = 1
                    self.store.execute(
                        "UPDATE actions SET state = 'pending', updated_at = ? WHERE id = ?",
                        (timestamp, action["id"]),
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
            self.store.heartbeat("fallback")
            if self.mutation_lock.locked():
                continue
            async with self.mutation_lock:
                if not self.runtime.ready:
                    await self._connect_runtime()
                await self.reconcile()
                if await self._maybe_refresh_source():
                    continue
                await self.advance()

    async def reconcile(self) -> None:
        started = time.monotonic()
        self.store.event("reconciliation_started", "reconciliation pass started")
        if not self.runtime.ready:
            self.store.event(
                "reconciliation_skipped",
                "runtime is unavailable",
                detail={"duration_ms": 0},
            )
            return
        self._adopt_stranded_operations()
        await self._reconcile_uncertain_operations()
        self._retry_recovering_assignments()
        await self._retry_pending_actions()
        task_ids = self.store.rows("""SELECT DISTINCT task_id FROM actions
               WHERE state IN ('starting','active','terminal','uncertain')""")
        for item in task_ids:
            task = self.store.row(
                "SELECT * FROM tasks WHERE id = ?", (item["task_id"],)
            )
            if task is None:
                continue
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
                            self.store.execute(
                                "UPDATE actions SET state = 'pending', updated_at = ? WHERE id = ?",
                                (utc_now(), action["id"]),
                            )
                            action["reminder_sent"] = 1
                            await self._dispatch_action(action, task=task)
            except Exception as error:
                self.store.execute(
                    "UPDATE tasks SET state = 'uncertain', updated_at = ? WHERE id = ?",
                    (utc_now(), task["id"]),
                )
                self.store.event(
                    "reconcile_failed",
                    str(error),
                    entity_type="task",
                    entity_id=task["id"],
                    detail={"traceback": traceback.format_exc()},
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
        violations = invariant_violations(self.store)
        if violations:
            self.starts_enabled = False
            self.store.execute(
                "INSERT INTO meta(key, value) VALUES ('dispatch_enabled', '0') ON CONFLICT(key) DO UPDATE SET value = '0'"
            )
            self.store.event(
                "invariant_violation",
                "workflow invariant check failed",
                detail={"violations": violations},
            )
        self.store.event(
            "reconciliation_completed",
            "reconciliation pass completed",
            detail={
                "duration_ms": int((time.monotonic() - started) * 1000),
                "violations": violations,
            },
        )

    async def _retry_pending_actions(self) -> None:
        now = utc_now()
        for action in self.store.rows(
            """SELECT * FROM actions WHERE state = 'pending'
               AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY id""",
            (now,),
        ):
            try:
                await self._dispatch_action(action)
            except Exception as error:
                schedule_action_retry(self.store, int(action["id"]), str(error))

    def _retry_recovering_assignments(self) -> None:
        now = utc_now()
        for assignment in self.store.rows(
            """SELECT * FROM assignments WHERE stage = 'recovering'
               AND next_attempt_at IS NOT NULL AND next_attempt_at <= ?
               AND operator_hold_id IS NULL ORDER BY id""",
            (now,),
        ):
            prior = assignment["prior_stage"]
            target = {
                "queued": "queued",
                "preparing": "preparing",
                "implementing": "preparing",
                "review_pending": "review_pending",
                "reviewing": "review_pending",
                "correcting": "correcting",
                "delivering": "delivering",
            }.get(str(prior), "queued")
            self.store.transition(
                "assignment",
                int(assignment["id"]),
                table="assignments",
                field="stage",
                to_state=target,
                reason="retry deadline reached",
                extra={"next_attempt_at": None, "condition": None},
            )

    def _adopt_stranded_operations(self) -> None:
        """Turn crash residue into explicit reconciliation or safe replay state."""

        timestamp = utc_now()
        for operation in self.store.rows(
            "SELECT * FROM external_operations WHERE state = 'sent' ORDER BY id"
        ):
            self.store.execute(
                """UPDATE external_operations SET state = 'uncertain',
                   condition = 'controller restarted after dispatch; effect requires observation',
                   updated_at = ? WHERE id = ?""",
                (timestamp, operation["id"]),
            )
            self.store.event(
                "operation_adopted",
                "controller adopted an in-flight operation as uncertain",
                entity_type="operation",
                entity_id=operation["id"],
            )
        for operation in self.store.rows(
            "SELECT * FROM external_operations WHERE state = 'intent' ORDER BY id"
        ):
            self.store.execute(
                """UPDATE external_operations SET state = 'canceled',
                   condition = 'confirmed unsent after controller restart', updated_at = ?
                   WHERE id = ?""",
                (timestamp, operation["id"]),
            )
            if operation["kind"] == "turn_start" and str(operation["target"]).isdigit():
                self.store.execute(
                    """UPDATE actions SET state = 'pending', next_attempt_at = ?,
                       condition = 'retrying confirmed-unsent turn start', updated_at = ?
                       WHERE id = ? AND state = 'starting'""",
                    (timestamp, timestamp, int(operation["target"])),
                )
            self.store.event(
                "operation_canceled_unsent",
                "confirmed-unsent intent released for safe replay",
                entity_type="operation",
                entity_id=operation["id"],
            )

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
            elif operation["kind"] == "tollgate_candidate_create":
                await self._reconcile_tollgate_candidate(operation)
            elif operation["kind"] == "thread_archive":
                await self._reconcile_thread_archive(operation)
            elif operation["kind"] == "beads_create":
                await asyncio.to_thread(self._reconcile_beads_create, operation)
            elif operation["kind"] == "beads_close":
                await asyncio.to_thread(self._reconcile_beads_close, operation)
            elif operation["kind"] == "setup_runtime_smoke":
                await self._reconcile_setup_runtime_smoke(operation)
            elif operation["kind"] == "brain_report_publish":
                await asyncio.to_thread(self._reconcile_brain_report, operation)
            elif operation["kind"] == "beads_finding_comment":
                await asyncio.to_thread(self._reconcile_finding_comment, operation)
            else:
                self._retain_uncertain_condition(
                    operation,
                    f"no automatic observer is available for {operation['kind']}; operator evidence is required",
                )

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

    def _reconcile_brain_report(self, operation: dict[str, Any]) -> None:
        inputs = json.loads(operation["input_json"])
        path = Path(str(inputs["path"]))
        publication = BrainRepository(self.paths.brain_root).publish(
            [path],
            f"docs: publish {inputs['kind']} report {operation['target']}",
        )
        result = {
            "branch": publication.branch,
            "local_revision": publication.local_revision,
            "remote_revision": publication.remote_revision,
            "merged_remote": publication.merged_remote,
        }
        timestamp = utc_now()
        self.store.execute(
            """UPDATE external_operations SET state = 'complete', result_json = ?,
               native_id = ?, reconciliation_used = 1, condition = NULL,
               completed_at = ?, updated_at = ? WHERE id = ?""",
            (
                json.dumps(result, sort_keys=True),
                publication.local_revision,
                timestamp,
                timestamp,
                operation["id"],
            ),
        )

    def _reconcile_finding_comment(self, operation: dict[str, Any]) -> None:
        inputs = json.loads(operation["input_json"])
        observed = self.beads.show(str(inputs["bead_id"]))
        marker = str(inputs["marker"])
        timestamp = utc_now()
        if observed is not None and marker in json.dumps(observed, sort_keys=True):
            self.store.execute(
                """UPDATE external_operations SET state = 'complete', result_json = ?,
                   native_id = ?, reconciliation_used = 1, condition = NULL,
                   completed_at = ?, updated_at = ? WHERE id = ?""",
                (
                    json.dumps({"bead_id": inputs["bead_id"], "marker": marker}),
                    inputs["bead_id"],
                    timestamp,
                    timestamp,
                    operation["id"],
                ),
            )
            self._record_existing_finding(
                str(operation["target"]),
                int(inputs["occurrence_id"]),
                str(inputs["bead_id"]),
                inputs["finding"],
            )
            return
        self.store.execute(
            """UPDATE external_operations SET state = 'failed', reconciliation_used = 1,
               condition = 'exact marker absent after targeted Beads observation',
               updated_at = ? WHERE id = ?""",
            (timestamp, operation["id"]),
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
            self._release_operator_hold("action", int(action["id"]))
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
            self._release_operator_hold("assignment", int(assignment["id"]))
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
        delivery_failure = _delivery_contract_failure(candidate, observed)
        if delivery_failure is None:
            self.store.execute(
                "UPDATE external_operations SET state = 'complete', reconciliation_used = 1, result_json = ?, condition = NULL, updated_at = ? WHERE id = ?",
                (json.dumps(observed), utc_now(), operation["id"]),
            )
            self.store.execute(
                "UPDATE assignments SET stage = 'delivering', condition = NULL, updated_at = ? WHERE id = ?",
                (utc_now(), assignment["id"]),
            )
            await self._close_delivered_assignment(assignment)
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

    async def _reconcile_tollgate_candidate(self, operation: dict[str, Any]) -> None:
        assignment = self.store.row(
            """SELECT a.*, r.project_id, p.tollgate_repo_id FROM assignments a
               JOIN runs r ON r.id = a.run_id JOIN projects p ON p.project_id = r.project_id
               WHERE a.id = ?""",
            (int(operation["target"]),),
        )
        if assignment is None or self.tollgate is None:
            self._retain_uncertain_condition(
                operation, "cannot reconcile candidate creation against its assignment"
            )
            return
        observed = await asyncio.to_thread(
            self.tollgate.status, assignment["tollgate_repo_id"], None
        )
        inputs = json.loads(operation["input_json"])
        candidate = _find_candidate(
            observed,
            inputs.get("worktree_path"),
            exclude_id=inputs.get("predecessor_candidate_id"),
        )
        if candidate is None or not isinstance(candidate.get("id"), str):
            condition = (
                "candidate creation remains ambiguous after an exact repository "
                "observation; operator must attach the candidate or confirm absence"
            )
            self._retain_uncertain_condition(operation, condition)
            self.store.execute(
                "UPDATE assignments SET condition = ?, updated_at = ? WHERE id = ?",
                (condition, utc_now(), assignment["id"]),
            )
            return
        self._record_candidate(assignment, candidate)
        timestamp = utc_now()
        self.store.execute(
            """UPDATE external_operations SET state = 'complete', native_id = ?,
               result_json = ?, reconciliation_used = 1, condition = NULL,
               completed_at = ?, updated_at = ? WHERE id = ?""",
            (
                candidate["id"],
                json.dumps(observed),
                timestamp,
                timestamp,
                operation["id"],
            ),
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

    async def _reconcile_setup_runtime_smoke(self, operation: dict[str, Any]) -> None:
        thread_id = operation["native_id"]
        if not thread_id:
            self._retain_uncertain_condition(
                operation,
                "runtime smoke thread identity was not checkpointed; operator cleanup is required",
            )
            return
        thread = await self.runtime.read_thread(str(thread_id))
        facts = thread_facts(thread)
        inputs = json.loads(operation["input_json"])
        if thread.get("name") != "Fulcrum setup visibility check" or thread.get(
            "projectId"
        ) != inputs.get("project_id"):
            self._retain_uncertain_condition(
                operation,
                "runtime smoke thread identity or project binding is inconsistent",
            )
            return
        if not (
            facts["last_turn_terminal"]
            and facts["helpers_terminal"]
            and facts["runtime_status"] == "idle"
        ):
            return
        await self.runtime.archive(str(thread_id))
        timestamp = utc_now()
        self.store.execute(
            """UPDATE external_operations SET state = 'complete', result_json = ?,
               reconciliation_used = 1, condition = NULL, completed_at = ?, updated_at = ?
               WHERE id = ?""",
            (json.dumps(thread), timestamp, timestamp, operation["id"]),
        )
        self.store.execute(
            "INSERT INTO meta(key, value) VALUES ('desktop_smoke_check', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (timestamp,),
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
        target_id: int | None = None
        target_type: str | None = None
        if operation["kind"] == "turn_start" and str(operation["target"]).isdigit():
            target_id = int(operation["target"])
            target_type = "action"
        elif (
            operation["kind"]
            in {
                "tollgate_worktree_create",
                "tollgate_candidate_create",
            }
            and str(operation["target"]).isdigit()
        ):
            target_id = int(operation["target"])
            target_type = "assignment"
        if target_id is None or target_type is None:
            return
        existing = self.store.row(
            "SELECT id FROM holds WHERE scope = ? AND target = ? AND released_at IS NULL",
            (target_type, str(target_id)),
        )
        if existing is None:
            cursor = self.store.execute(
                """INSERT INTO holds(scope, target, reason, urgent, release_condition, created_at)
                   VALUES (?, ?, ?, 1, 'operator resolves ambiguous external effect', ?)""",
                (target_type, str(target_id), condition, utc_now()),
            )
            hold_id = int(cursor.lastrowid)
        else:
            hold_id = int(existing["id"])
        if target_type == "action":
            self.store.execute(
                "UPDATE actions SET operator_hold_id = ?, condition = ?, updated_at = ? WHERE id = ?",
                (hold_id, condition, utc_now(), target_id),
            )
        else:
            assignment = self.store.row(
                "SELECT stage FROM assignments WHERE id = ?", (target_id,)
            )
            if assignment is not None:
                self.store.execute(
                    """UPDATE assignments SET prior_stage = CASE WHEN stage = 'recovering' THEN prior_stage ELSE stage END,
                       stage = 'recovering', operator_hold_id = ?, next_attempt_at = NULL,
                       condition = ?, updated_at = ? WHERE id = ?""",
                    (hold_id, condition, utc_now(), target_id),
                )

    async def advance(self) -> None:
        await self._run_advancement_step(
            "occurrence-publication", self._publish_occurrences
        )
        if self.runtime.ready:
            await self._run_advancement_step(
                "archon-succession", self._process_archon_succession
            )
            await self._run_advancement_step("archival", self._archive_ready_tasks)
            await self._run_advancement_step(
                "archon-succession", self._process_archon_succession
            )
        self._update_readiness()
        if not self.starts_enabled or not self.runtime.ready:
            return
        # Admission is deliberately one-at-a-time. Each started action commits a
        # lease before the next capacity snapshot is calculated.
        considered: set[int] = set()
        while True:
            ready = [
                item
                for item in ready_assignments(self.store)
                if int(item["id"]) not in considered
            ]
            if not ready:
                break
            assignment = ready[0]
            considered.add(int(assignment["id"]))
            if assignment["stage"] == "queued":
                try:
                    await self._prepare_assignment(assignment)
                except Exception as error:
                    self._schedule_assignment_recovery(assignment, str(error))
            elif assignment["stage"] == "preparing":
                try:
                    await self._resume_preparing_assignment(assignment)
                except Exception as error:
                    self._schedule_assignment_recovery(assignment, str(error))
            elif assignment["stage"] in {"review_pending", "correcting"}:
                try:
                    await self._start_assignment_action(assignment)
                except Exception as error:
                    self._schedule_assignment_recovery(assignment, str(error))
        for assignment in self.store.rows(
            "SELECT a.*, r.project_id FROM assignments a JOIN runs r ON r.id = a.run_id WHERE a.stage = 'delivering'"
        ):
            try:
                await self._deliver(assignment)
            except Exception as error:
                self._schedule_assignment_recovery(assignment, str(error))
        for name, step in (
            ("interviews", self._manage_interviews),
            ("specialists", self._start_specialists),
            ("proposals", self._queue_proposals),
            ("update-delivery", self._deliver_update_batch),
        ):
            await self._run_advancement_step(name, step)
        self._update_readiness()

    async def _run_advancement_step(self, name: str, step: Any) -> None:
        try:
            await step()
        except Exception as error:
            self.store.event(
                "advancement_step_failed",
                f"{name}: {error}",
                entity_type="advancement_step",
                entity_id=name,
                detail={"traceback": traceback.format_exc()},
            )

    async def _prepare_assignment(self, assignment: dict[str, Any]) -> None:
        project = self.store.row(
            "SELECT * FROM projects WHERE project_id = ?", (assignment["project_id"],)
        )
        if project is None or not project["tollgate_repo_id"] or self.tollgate is None:
            raise StoreError("Tollgate worktree capability is unavailable")
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
        attempt = self.store.begin_operation_attempt(operation)
        started = time.monotonic()
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
            self._ensure_worktree_environment(Path(path))
            self.store.finish_operation_attempt(
                operation,
                attempt,
                state="complete",
                result=result,
                duration_ms=int((time.monotonic() - started) * 1000),
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
            self._operation_failed(
                operation,
                error,
                attempt=attempt,
                started=started,
                mutation=True,
            )
            retained = self.store.row(
                "SELECT * FROM external_operations WHERE id = ?", (operation,)
            )
            if retained is not None:
                self._reconcile_worktree_create(retained)

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
        run = self.store.row("SELECT * FROM runs WHERE id = ?", (assignment["run_id"],))
        if run is None:
            raise StoreError("assignment run disappeared")
        if run.get("executor_task_id") and run.get("overseer_task_id"):
            self.store.execute(
                """UPDATE assignments SET executor_task_id = ?, overseer_task_id = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    run["executor_task_id"],
                    run["overseer_task_id"],
                    utc_now(),
                    assignment["id"],
                ),
            )
            assignment["executor_task_id"] = run["executor_task_id"]
            assignment["overseer_task_id"] = run["overseer_task_id"]
            return
        bead = self.store.row(
            "SELECT * FROM beads WHERE bead_id = ?", (assignment["bead_id"],)
        )
        project = self.store.row(
            "SELECT * FROM projects WHERE project_id = ?", (assignment["project_id"],)
        )
        if bead is None or project is None:
            raise StoreError("assignment sources disappeared")
        executor = self.store.row(
            "SELECT * FROM tasks WHERE pair_id = ? AND role = 'executor' ORDER BY id DESC LIMIT 1",
            (assignment["run_id"],),
        ) or await self._provision_task(
            role="executor",
            description=bead["title"],
            project=project,
            model=bead["executor_model"],
            effort=bead["executor_reasoning_effort"],
            pair_id=int(assignment["run_id"]),
        )
        overseer = self.store.row(
            "SELECT * FROM tasks WHERE pair_id = ? AND role = 'overseer' ORDER BY id DESC LIMIT 1",
            (assignment["run_id"],),
        ) or await self._provision_task(
            role="overseer",
            description=bead["title"],
            project=project,
            model=bead["overseer_model"],
            effort=bead["overseer_reasoning_effort"],
            pair_id=int(assignment["run_id"]),
        )
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE runs SET executor_task_id = ?, overseer_task_id = ?, state = 'active', updated_at = ? WHERE id = ?",
                (executor["id"], overseer["id"], utc_now(), assignment["run_id"]),
            )
            connection.execute(
                """UPDATE assignments SET executor_task_id = ?, overseer_task_id = ?, updated_at = ?
                   WHERE run_id = ?""",
                (executor["id"], overseer["id"], utc_now(), assignment["run_id"]),
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
        attempt = self.store.begin_operation_attempt(operation)
        started = time.monotonic()
        try:
            result = await self.runtime.create_thread(
                cwd=project["repo_path"],
                model=model,
                project_id=project["codex_project_id"],
                base_instructions=role_instructions(
                    (
                        "implement"
                        if role == "executor"
                        else (
                            "review"
                            if role == "overseer"
                            else (
                                "specialist"
                                if role in {"sage", "inquisitor"}
                                else "archon"
                            )
                        )
                    ),
                    role=role,
                ),
            )
            thread = result["thread"]
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
            self.store.finish_operation_attempt(
                operation,
                attempt,
                state="complete",
                result=result,
                native_id=str(thread["id"]),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            await self.runtime.set_name(thread["id"], task["title"])
            self.store.execute(
                "UPDATE tasks SET state = 'idle', runtime_status = 'unmaterialized', updated_at = ? WHERE id = ?",
                (utc_now(), task["id"]),
            )
            task["state"] = "idle"
            task["runtime_status"] = "unmaterialized"
            return task
        except Exception as error:
            current = self.store.row(
                "SELECT state FROM external_operations WHERE id = ?", (operation,)
            )
            if current is not None and current["state"] != "complete":
                self._operation_failed(
                    operation,
                    error,
                    attempt=attempt,
                    started=started,
                    mutation=True,
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
                f"UPDATE runs SET {column} = ?, updated_at = ? WHERE id = ?",
                (task["id"], utc_now(), pair_id),
            )
            self.store.execute(
                f"UPDATE assignments SET {column} = ?, condition = NULL, updated_at = ? WHERE run_id = ?",
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
            """SELECT * FROM actions WHERE task_id = ?
               AND state IN ('pending','starting','active','terminal','uncertain')""",
            (task["id"],),
        )
        if existing is not None:
            return
        handoffs = self.store.rows(
            """SELECT kind, content_json, source_action_id FROM handoffs
               WHERE assignment_id = ? ORDER BY id""",
            (assignment["id"],),
        )
        payload = {
            "purpose": kind,
            "bead_id": assignment["bead_id"],
            "predecessor_candidate_id": assignment.get("candidate_id"),
            "candidate": {
                "id": assignment.get("candidate_id"),
                "source_revision": assignment.get("source_oid"),
                "tested_revision": assignment.get("tested_oid"),
            },
            "handoffs": [
                {
                    "kind": row["kind"],
                    "source_action_id": row["source_action_id"],
                    "content": json.loads(row["content_json"]),
                }
                for row in handoffs
            ],
        }
        decision = acquire_lease(
            self.store,
            LeaseRequest(
                task_id=int(task["id"]),
                assignment_id=int(assignment["id"]),
                kind=kind,
                payload=payload,
                project_ids=(str(assignment["project_id"]),),
                pair_id=int(assignment["run_id"]),
                conflict_keys=self._assignment_conflict_keys(assignment),
                check_after=(
                    datetime.now(timezone.utc)
                    + timedelta(seconds=self.config.turn_check_after_seconds)
                )
                .isoformat()
                .replace("+00:00", "Z"),
            ),
        )
        if not decision.admitted or decision.action is None:
            self.store.event(
                "lease_deferred",
                "; ".join(decision.blockers),
                entity_type="assignment",
                entity_id=assignment["id"],
                detail={"blockers": decision.blockers},
            )
            return
        action = decision.action
        timestamp = utc_now()
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
        try:
            await self._dispatch_action(action, task=task, assignment=assignment)
        except Exception as error:
            schedule_action_retry(self.store, int(action["id"]), str(error))

    def _assignment_conflict_keys(self, assignment: dict[str, Any]) -> tuple[str, ...]:
        bead = self.store.row(
            "SELECT context_json FROM beads WHERE bead_id = ?",
            (assignment["bead_id"],),
        )
        if bead is None:
            return ()
        try:
            context = json.loads(bead["context_json"])
        except (TypeError, json.JSONDecodeError):
            return ()
        keys: set[str] = set()
        for item in context if isinstance(context, list) else []:
            if isinstance(item, str) and item.startswith("conflict:"):
                raw = item.removeprefix("conflict:").strip()
                if raw:
                    keys.add(raw)
            elif isinstance(item, dict):
                raw = item.get("conflict_key") or item.get("resource")
                if isinstance(raw, str) and raw.strip():
                    keys.add(raw.strip())
        return tuple(sorted(keys))

    def _build_action_context(
        self,
        action: dict[str, Any],
        task: dict[str, Any],
        assignment: dict[str, Any] | None,
        *,
        section: str = "context",
    ) -> str:
        """Read the current action facts separately from notices and role guidance."""

        constraints: list[str] = []
        if assignment is not None:
            bead = self.store.row(
                "SELECT context_json FROM beads WHERE bead_id = ?",
                (assignment["bead_id"],),
            )
            if bead is not None:
                try:
                    context = json.loads(bead["context_json"] or "[]")
                except json.JSONDecodeError:
                    context = []
                constraints.extend(
                    item if isinstance(item, str) else json.dumps(item, sort_keys=True)
                    for item in context
                    if isinstance(item, (str, dict))
                )
            dependencies = self.store.rows(
                "SELECT dependency_id FROM bead_dependencies WHERE bead_id = ? ORDER BY dependency_id",
                (assignment["bead_id"],),
            )
            constraints.extend(
                f"dependency {row['dependency_id']} is completed"
                for row in dependencies
            )
        if assignment is not None:
            payload = (
                json.loads(action["payload"])
                if isinstance(action["payload"], str)
                else dict(action["payload"])
            )
            payload["handoffs"] = [
                {
                    "kind": row["kind"],
                    "source_action_id": row["source_action_id"],
                    "content": json.loads(row["content_json"]),
                }
                for row in self.store.rows(
                    "SELECT kind, source_action_id, content_json FROM handoffs WHERE assignment_id = ? ORDER BY id",
                    (assignment["id"],),
                )
            ]
            if action["kind"] == "correct" and assignment.get("candidate_id"):
                operation = self.store.row(
                    "SELECT result_json, condition FROM external_operations WHERE kind = 'tollgate_approve' AND target = ? ORDER BY id DESC LIMIT 1",
                    (assignment["candidate_id"],),
                )
                if operation:
                    payload["delivery_failure"] = operation["condition"]
                    payload["delivery_evidence"] = json.loads(
                        operation["result_json"] or "{}"
                    )
            action = {**action, "payload": payload}
        return build_context(
            task=task,
            action=action,
            assignment=assignment,
            constraints=constraints,
            section=section,
        )

    def _ensure_worktree_environment(self, worktree: Path) -> None:
        """Expose the retained check environment in every managed worktree."""

        source_environment = Path(self.config.source_root) / ".venv"
        target = worktree / ".venv"
        if not source_environment.is_dir() or target.exists() or target.is_symlink():
            return
        target.symlink_to(source_environment, target_is_directory=True)
        self.store.event(
            "worktree_environment_ready",
            "linked the managed validation environment",
            entity_type="worktree",
            entity_id=str(worktree),
            detail={"environment": str(source_environment)},
        )

    def _schedule_assignment_recovery(
        self, assignment: dict[str, Any], reason: str
    ) -> None:
        attempts = int(assignment.get("retry_count") or 0) + 1
        due = (
            (
                datetime.now(timezone.utc)
                + timedelta(seconds=5 * (2 ** min(attempts - 1, 6)))
            )
            .isoformat()
            .replace("+00:00", "Z")
        )
        self.store.transition(
            "assignment",
            int(assignment["id"]),
            table="assignments",
            field="stage",
            to_state="recovering",
            reason=reason,
            extra={
                "prior_stage": assignment["stage"],
                "retry_count": attempts,
                "next_attempt_at": due,
                "condition": reason,
            },
        )
        self._queue_archon_update(
            f"recovery:{assignment['id']}:{attempts}",
            {
                "kind": "assignment_recovery",
                "assignment_id": assignment["id"],
                "run_id": assignment["run_id"],
                "bead_id": assignment["bead_id"],
                "attempt": attempts,
                "condition": reason,
                "next_attempt_at": due,
            },
        )

    def _hold_assignment(self, assignment: dict[str, Any], reason: str) -> None:
        timestamp = utc_now()
        with self.store.transaction() as connection:
            hold = connection.execute(
                """INSERT INTO holds(scope, target, reason, urgent, release_condition, created_at)
                   VALUES ('assignment', ?, ?, 1, 'operator supplies a specific recovery decision', ?)""",
                (str(assignment["id"]), reason, timestamp),
            )
            connection.execute(
                """UPDATE assignments SET prior_stage = stage, stage = 'recovering',
                   operator_hold_id = ?, next_attempt_at = NULL, condition = ?, updated_at = ?
                   WHERE id = ?""",
                (hold.lastrowid, reason, timestamp, assignment["id"]),
            )

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
        prompt = action_notice(action)
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
        attempt = self.store.begin_operation_attempt(operation)
        started = time.monotonic()
        try:
            turn_id = await self.runtime.start_turn(
                task["native_thread_id"],
                prompt,
                cwd=cwd,
                model=task["model"],
                effort=task["reasoning_effort"],
                correlation=f"fulcrum-operation-{operation}",
            )
            self.store.finish_operation_attempt(
                operation,
                attempt,
                state="complete",
                result={"turn_id": turn_id},
                native_id=turn_id,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            self.store.execute(
                "UPDATE actions SET state = 'active', native_turn_id = ?, next_attempt_at = NULL, condition = NULL, updated_at = ? WHERE id = ?",
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
        except Exception as error:
            self._operation_failed(
                operation,
                error,
                attempt=attempt,
                started=started,
                mutation=True,
            )
            self.store.execute(
                "UPDATE actions SET state = 'uncertain', condition = ?, updated_at = ? WHERE id = ?",
                (str(error), utc_now(), action["id"]),
            )
            self.store.execute(
                "UPDATE reservations SET state = 'uncertain' WHERE action_id = ?",
                (action["id"],),
            )
            retained = self.store.row(
                "SELECT * FROM external_operations WHERE id = ?", (operation,)
            )
            if retained is not None:
                await self._reconcile_turn_start(retained)

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
        action = self.store.row(
            """SELECT payload FROM actions WHERE assignment_id = ?
               AND state IN ('starting','active','terminal','uncertain') ORDER BY id DESC LIMIT 1""",
            (assignment_id,),
        )
        payload = json.loads(action["payload"] or "{}") if action else {}
        predecessor = payload.get("predecessor_candidate_id")
        if assignment["candidate_id"] and assignment["candidate_id"] != predecessor:
            return True
        result = await asyncio.to_thread(
            tollgate.status, project["tollgate_repo_id"], None
        )
        candidate = _find_candidate(
            result,
            assignment["worktree_path"],
            exclude_id=predecessor,
        )
        if candidate is None:
            operation = self.store.create_operation(
                "tollgate_candidate_create",
                str(assignment_id),
                {
                    "repository_id": project["tollgate_repo_id"],
                    "worktree_path": assignment["worktree_path"],
                    "revision": "HEAD",
                    "predecessor_candidate_id": predecessor,
                },
            )
            attempt = self.store.begin_operation_attempt(operation)
            started = time.monotonic()
            try:
                submitted = await asyncio.to_thread(
                    tollgate.submit_candidate,
                    project["tollgate_repo_id"],
                    "HEAD",
                    cwd=Path(str(assignment["worktree_path"])),
                )
                observed = await asyncio.to_thread(
                    tollgate.status, project["tollgate_repo_id"], None
                )
                candidate = _find_candidate(
                    observed,
                    assignment["worktree_path"],
                    exclude_id=predecessor,
                )
                if candidate is None:
                    raise TollgateUncertainError(
                        "Tollgate accepted candidate creation but the exact candidate "
                        "was not observable"
                    )
                self.store.finish_operation_attempt(
                    operation,
                    attempt,
                    state="complete",
                    result={"submission": submitted, "status": observed},
                    native_id=str(candidate.get("id")),
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            except Exception as error:
                self._operation_failed(
                    operation,
                    error,
                    attempt=attempt,
                    started=started,
                    mutation=True,
                )
                retained = self.store.row(
                    "SELECT * FROM external_operations WHERE id = ?", (operation,)
                )
                if retained is not None and retained["state"] == "uncertain":
                    await self._reconcile_tollgate_candidate(retained)
                return False
        self._record_candidate(assignment, candidate)
        return True

    def _record_candidate(
        self, assignment: dict[str, Any], candidate: dict[str, Any]
    ) -> None:
        candidate_id = candidate.get("id")
        if not isinstance(candidate_id, str):
            raise StoreError("Tollgate candidate has no immutable identity")
        source_oid = _oid(candidate.get("source_oid"))
        if source_oid is None:
            raise StoreError("Tollgate candidate has no immutable source revision")
        self.store.execute(
            "UPDATE assignments SET candidate_id = ?, source_oid = ?, tested_oid = ?, updated_at = ? WHERE id = ?",
            (
                candidate_id,
                source_oid,
                _oid(candidate.get("tested_oid")),
                utc_now(),
                assignment["id"],
            ),
        )
        self.store.event(
            "candidate_captured",
            f"captured immutable candidate {candidate_id}",
            entity_type="assignment",
            entity_id=assignment["id"],
            detail={
                "candidate_id": candidate_id,
                "source_oid": source_oid,
                "tested_oid": _oid(candidate.get("tested_oid")),
            },
        )
        self._release_operator_hold("assignment", int(assignment["id"]))

    def _release_operator_hold(self, entity_type: str, entity_id: int) -> None:
        timestamp = utc_now()
        self.store.execute(
            "UPDATE holds SET released_at = ? WHERE scope = ? AND target = ? AND released_at IS NULL",
            (timestamp, entity_type, str(entity_id)),
        )
        table = "actions" if entity_type == "action" else "assignments"
        self.store.execute(
            f"UPDATE {table} SET operator_hold_id = NULL, condition = NULL, updated_at = ? WHERE id = ?",
            (timestamp, entity_id),
        )

    async def _deliver(self, assignment: dict[str, Any]) -> None:
        if self.tollgate is None or not assignment["candidate_id"]:
            raise StoreError("Tollgate delivery capability or candidate is unavailable")
        project = self.store.row(
            "SELECT * FROM projects WHERE project_id = ?", (assignment["project_id"],)
        )
        if project is None or not project["tollgate_repo_id"]:
            raise StoreError("assignment has no Tollgate repository identity")
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
            self._hold_assignment(assignment, authority_error)
            return
        operation = self.store.create_operation(
            "tollgate_approve",
            assignment["candidate_id"],
            {"repository_id": project["tollgate_repo_id"]},
        )
        attempt = self.store.begin_operation_attempt(operation)
        started = time.monotonic()
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
                self.store.finish_operation_attempt(
                    operation,
                    attempt,
                    state="failed",
                    result=detail,
                    error=failure,
                    native_id=str(assignment["candidate_id"]),
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
                self.store.execute(
                    "UPDATE assignments SET prior_stage = 'delivering', stage = 'correcting', condition = ?, updated_at = ? WHERE id = ?",
                    (failure, utc_now(), assignment["id"]),
                )
                return
            self.store.finish_operation_attempt(
                operation,
                attempt,
                state="complete",
                result={"approval": result, "status": observed},
                native_id=str(assignment["candidate_id"]),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            await self._close_delivered_assignment(assignment)
        except Exception as error:
            self._operation_failed(
                operation,
                error,
                attempt=attempt,
                started=started,
                mutation=True,
            )
            retained = self.store.row(
                "SELECT * FROM external_operations WHERE id = ?", (operation,)
            )
            if retained is not None:
                await self._reconcile_tollgate_approve(retained)

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
            attempt = self.store.begin_operation_attempt(operation)
            started = time.monotonic()
            try:
                await asyncio.to_thread(
                    self.beads.close,
                    assignment["bead_id"],
                    f"Delivered candidate {assignment['candidate_id']}",
                )
                self.store.finish_operation_attempt(
                    operation,
                    attempt,
                    state="complete",
                    result={"closed": True},
                    native_id=str(assignment["bead_id"]),
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            except Exception as error:
                self._operation_failed(
                    operation,
                    error,
                    attempt=attempt,
                    started=started,
                    mutation=True,
                )
                self.store.execute(
                    "UPDATE assignments SET condition = ?, updated_at = ? WHERE id = ?",
                    (str(error), utc_now(), assignment["id"]),
                )
                retained = self.store.row(
                    "SELECT * FROM external_operations WHERE id = ?", (operation,)
                )
                if retained is not None:
                    await asyncio.to_thread(self._reconcile_beads_close, retained)
                return
        self._record_assignment_completed(assignment)

    def _record_assignment_completed(self, assignment: dict[str, Any]) -> None:
        self.store.execute(
            "UPDATE assignments SET stage = 'completed', condition = NULL, updated_at = ? WHERE id = ?",
            (utc_now(), assignment["id"]),
        )
        self._queue_archon_update(
            f"completion:{assignment['id']}",
            {
                "kind": "assignment_completed",
                "assignment_id": assignment["id"],
                "run_id": assignment["run_id"],
                "bead_id": assignment["bead_id"],
                "candidate_id": assignment.get("candidate_id"),
                "source_revision": assignment.get("source_oid"),
                "tested_revision": assignment.get("tested_oid"),
            },
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
        run = (
            self.store.row(
                "SELECT executor_task_id, overseer_task_id FROM runs WHERE id = ?",
                (assignment["run_id"],),
            )
            or {}
        )
        for key in ("executor_task_id", "overseer_task_id"):
            task_id = run.get(key)
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
        now = utc_now()
        for obligation in self.store.rows(
            """SELECT * FROM obligations WHERE kind = 'archive'
               AND state IN ('pending','failed')
               AND operator_hold_id IS NULL
               AND (next_attempt_at IS NULL OR next_attempt_at <= ?)""",
            (now,),
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
            attempt = self.store.begin_operation_attempt(operation)
            started = time.monotonic()
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
                self.store.finish_operation_attempt(
                    operation,
                    attempt,
                    state="complete",
                    result={"archived": True},
                    native_id=str(obligation["target"]),
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            except Exception as error:
                self._operation_failed(
                    operation,
                    error,
                    attempt=attempt,
                    started=started,
                    mutation=True,
                )
                retained = self.store.row(
                    "SELECT * FROM external_operations WHERE id = ?", (operation,)
                )
                if retained is not None:
                    try:
                        await self._reconcile_thread_archive(retained)
                    except AppServerError:
                        pass
                current = self.store.row(
                    "SELECT state FROM obligations WHERE id = ?", (obligation["id"],)
                )
                if current is not None and current["state"] != "complete":
                    retries = int(obligation["retry_count"] or 0) + 1
                    if retries >= 8:
                        hold = self.store.execute(
                            """INSERT INTO holds(scope, target, reason, urgent, release_condition, created_at)
                               VALUES ('obligation', ?, ?, 1, 'operator repairs or quarantines native thread', ?)""",
                            (str(obligation["id"]), str(error), utc_now()),
                        )
                        self.store.execute(
                            """UPDATE obligations SET state = 'failed', retry_count = ?,
                               operator_hold_id = ?, next_attempt_at = NULL, detail = ?, updated_at = ?
                               WHERE id = ?""",
                            (
                                retries,
                                hold.lastrowid,
                                str(error),
                                utc_now(),
                                obligation["id"],
                            ),
                        )
                    else:
                        due = (
                            (
                                datetime.now(timezone.utc)
                                + timedelta(seconds=5 * (2 ** min(retries - 1, 6)))
                            )
                            .isoformat()
                            .replace("+00:00", "Z")
                        )
                        self.store.execute(
                            """UPDATE obligations SET state = 'failed', retry_count = ?,
                               next_attempt_at = ?, detail = ?, updated_at = ? WHERE id = ?""",
                            (retries, due, str(error), utc_now(), obligation["id"]),
                        )

    def _operation_failed(
        self,
        operation: int,
        error: Exception,
        *,
        attempt: int | None = None,
        started: float | None = None,
        mutation: bool = False,
    ) -> None:
        state = (
            "uncertain"
            if mutation or isinstance(error, (TollgateUncertainError, AppServerError))
            else "failed"
        )
        current = self.store.row(
            "SELECT attempt_count FROM external_operations WHERE id = ?", (operation,)
        )
        actual_attempt = attempt or int(current["attempt_count"] if current else 0)
        if actual_attempt <= 0:
            actual_attempt = self.store.begin_operation_attempt(operation)
        self.store.finish_operation_attempt(
            operation,
            actual_attempt,
            state=state,
            stdout=getattr(error, "stdout", None),
            stderr=getattr(error, "stderr", None),
            error=str(error),
            duration_ms=(
                getattr(error, "duration_ms", None)
                if started is None
                else int((time.monotonic() - started) * 1000)
            ),
        )

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        correlation_id = f"request-{uuid.uuid4()}"
        started = time.monotonic()
        try:
            line = await asyncio.wait_for(reader.readline(), 30)
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise ValueError("request must be an object")
            self.store.event(
                "command_received",
                f"received {payload.get('command', 'unknown')}",
                entity_type="request",
                entity_id=correlation_id,
                detail={"correlation_id": correlation_id, "request": payload},
            )
            async with self.mutation_lock:
                result = await self.handle_request(payload)
            self.advance_requested.set()
            response = {"ok": True, "data": result}
            self.store.event(
                "command_succeeded",
                f"completed {payload.get('command', 'unknown')}",
                entity_type="request",
                entity_id=correlation_id,
                detail={
                    "correlation_id": correlation_id,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                },
            )
        except Exception as error:
            response = {"ok": False, "error": str(error)}
            self.store.event(
                "command_failed",
                str(error),
                entity_type="request",
                entity_id=correlation_id,
                detail={
                    "correlation_id": correlation_id,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "traceback": traceback.format_exc(),
                },
            )
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
            if task is None:
                raise StoreError("action task is missing")
            assignment = None
            if action.get("assignment_id") is not None:
                assignment = self.store.row(
                    """SELECT a.*, r.project_id FROM assignments a
                       JOIN runs r ON r.id = a.run_id WHERE a.id = ?""",
                    (action["assignment_id"],),
                )
            return {
                "instructions": self._build_action_context(
                    action,
                    task,
                    assignment,
                    section=str(request.get("section", "context")),
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
                """SELECT * FROM actions WHERE task_id = ?
                   AND state IN ('pending','starting','active','terminal','uncertain')""",
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
            "instructions": weaver_instructions(
                plan_mode=not bool(request.get("writable", True)),
                project=str(project_id),
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
        if not requested_projects:
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
        thread = await self.runtime.read_thread(archon["native_thread_id"])
        turns = thread.get("turns")
        materialized = isinstance(turns, list) and bool(turns)
        policies = self.store.rows("SELECT * FROM policies")
        capacities = self.store.row("SELECT value FROM meta WHERE key = 'global_limit'")
        needs_policies = not policies or capacities is None
        if needs_policies or not materialized:
            existing = self.store.row(
                """SELECT * FROM actions WHERE task_id = ?
                   AND state IN ('pending','starting','active','terminal','uncertain')""",
                (archon["id"],),
            )
            if existing is None:
                timestamp = utc_now()
                purpose = "initial_policies" if needs_policies else "materialize_archon"
                payload: dict[str, Any] = {
                    "purpose": purpose,
                    "projects": [
                        project.project_id for project in self.config.projects
                    ],
                    "required": (
                        "Set explicit global/project capacity and recurring Sage/Inquisitor policies."
                        if needs_policies
                        else "Confirm the retained fleet configuration by returning an empty decisions list unless a change is required."
                    ),
                }
                if not needs_policies:
                    payload["fleet_snapshot"] = self._fleet_snapshot()
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
                "condition": (
                    "Archon policies pending"
                    if needs_policies
                    else "Archon materialization pending"
                ),
            }
        facts = thread_facts(thread)
        if not facts["last_turn_terminal"] or not facts["helpers_terminal"]:
            return {
                "ready": False,
                "archon": archon["native_thread_id"],
                "condition": "Archon setup turn is still active",
            }
        if not await self.runtime.thread_is_listed(archon["native_thread_id"]):
            self.store.execute(
                "UPDATE tasks SET runtime_status = 'unmaterialized', updated_at = ? WHERE id = ?",
                (utc_now(), archon["id"]),
            )
            self._update_readiness()
            raise StoreError(
                "Archon thread is not discoverable through thread/list; setup cannot expose it in Codex"
            )
        self.store.execute(
            "UPDATE tasks SET runtime_status = ?, last_turn_terminal = 1, helpers_terminal = 1, updated_at = ? WHERE id = ?",
            (str(facts["runtime_status"]), utc_now(), archon["id"]),
        )
        self._update_readiness()
        return {
            "ready": self.starts_enabled,
            "archon": archon["native_thread_id"],
            "condition": None if self.starts_enabled else "controller is not ready",
        }

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
        attempt = self.store.begin_operation_attempt(operation)
        started = time.monotonic()
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
            self.store.finish_operation_attempt(
                operation,
                attempt,
                state="complete",
                result=observed,
                native_id=str(thread_id),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            self.store.execute(
                "INSERT INTO meta(key, value) VALUES ('desktop_smoke_check', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (utc_now(),),
            )
        except Exception as error:
            current = self.store.row(
                "SELECT state FROM external_operations WHERE id = ?", (operation,)
            )
            if current is not None and current["state"] != "complete":
                self._operation_failed(
                    operation,
                    error,
                    attempt=attempt,
                    started=started,
                    mutation=True,
                )
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
        state_ready, state_reasons = state_readiness(self.store)
        progress_ready, progress_reasons = progress_readiness(
            self.store, critical_workers=self.critical_workers
        )
        ready = state_ready and progress_ready and self.runtime.ready
        self.store.execute(
            "INSERT INTO meta(key, value) VALUES ('dispatch_enabled', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("1" if ready else "0",),
        )
        self.store.execute(
            "INSERT INTO meta(key, value) VALUES ('readiness_reasons', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (json.dumps(state_reasons + progress_reasons),),
        )
        self.starts_enabled = ready

    def _queue_archon_update(
        self, identity: str, content: dict[str, Any], *, actionable: bool = True
    ) -> None:
        archon = self.store.row(
            "SELECT id FROM tasks WHERE role = 'archon' AND state NOT IN ('retired','archived')"
        )
        if archon is None:
            return
        timestamp = utc_now()
        self.store.execute(
            """INSERT INTO updates(recipient_task_id, identity, content, actionable, state, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'retained', ?, ?)
               ON CONFLICT(recipient_task_id, identity) DO UPDATE SET
               content = excluded.content, actionable = excluded.actionable,
               updated_at = excluded.updated_at WHERE updates.state = 'retained'""",
            (
                archon["id"],
                identity,
                json.dumps(content, sort_keys=True),
                int(actionable),
                timestamp,
                timestamp,
            ),
        )

    async def _process_archon_succession(self) -> None:
        retained = self.store.row(
            "SELECT value FROM meta WHERE key = 'archon_succession_request'"
        )
        if retained is None:
            return
        request = json.loads(retained["value"])
        old_task_id = request.get("task_id")
        if not isinstance(old_task_id, int):
            return
        old = self.store.row("SELECT * FROM tasks WHERE id = ?", (old_task_id,))
        if old is not None and old["state"] not in {"retired", "archived"}:
            active = self.store.row(
                """SELECT 1 FROM actions WHERE task_id = ?
                   AND state IN ('pending','starting','active','terminal','uncertain')""",
                (old_task_id,),
            )
            if (
                active is not None
                or not old["last_turn_terminal"]
                or not old["helpers_terminal"]
            ):
                return
            self.store.execute(
                """INSERT OR IGNORE INTO obligations(
                       kind, identity, target, state, created_at, updated_at
                   ) VALUES ('archive', ?, ?, 'pending', ?, ?)""",
                (
                    f"archon-succession:{old_task_id}",
                    old["native_thread_id"],
                    utc_now(),
                    utc_now(),
                ),
            )
            return
        current = self.store.row(
            "SELECT 1 FROM tasks WHERE role = 'archon' AND state NOT IN ('retired','archived')"
        )
        if current is not None:
            raise StoreError("Archon succession found an unexpected current Archon")
        context = self.store.row(
            "SELECT * FROM projects WHERE enabled = 1 ORDER BY project_id LIMIT 1"
        )
        if context is None:
            raise StoreError("Archon succession has no enabled project context")
        successor = await self._provision_task(
            role="archon",
            description="",
            project=context,
            model=str(request["successor_model"]),
            effort=str(request["successor_reasoning_effort"]),
        )
        self.store.execute("DELETE FROM meta WHERE key = 'archon_succession_request'")
        self._queue_archon_update(
            f"succession:{old_task_id}:{successor['id']}",
            {
                "kind": "archon_succession_completed",
                "predecessor_task_id": old_task_id,
                "successor_task_id": successor["id"],
                "reason": request["reason"],
            },
            actionable=True,
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
            dependencies = [
                row["dependency_id"]
                for row in self.store.rows(
                    "SELECT dependency_id FROM bead_dependencies WHERE bead_id = ? ORDER BY dependency_id",
                    (bead["bead_id"],),
                )
            ]
            content = json.dumps(
                {
                    "kind": "proposal",
                    "bead_id": bead["bead_id"],
                    "project": bead["project_id"],
                    "title": bead["title"],
                    "scope": bead["description"],
                    "dependencies": dependencies,
                    "context": json.loads(bead["context_json"] or "[]"),
                    "models": {
                        "executor": [
                            bead["executor_model"],
                            bead["executor_reasoning_effort"],
                        ],
                        "overseer": [
                            bead["overseer_model"],
                            bead["overseer_reasoning_effort"],
                        ],
                        "provenance": bead["model_provenance"],
                    },
                    "plan": {
                        "id": bead["plan_id"],
                        "commit": bead["plan_commit"],
                    },
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
        self._reactivate_deferred_batches()
        archon = self.store.row(
            "SELECT * FROM tasks WHERE role = 'archon' AND state = 'idle'"
        )
        if archon is None:
            return
        current = self.store.row(
            """SELECT 1 FROM actions WHERE task_id = ?
               AND state IN ('pending','starting','active','terminal','uncertain')""",
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
            "fleet_snapshot": self._fleet_snapshot(),
            "batch_items": [
                {
                    "update_id": row["id"],
                    "identity": row["identity"],
                    "content": json.loads(row["content"]),
                }
                for row in updates
            ],
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

    def _fleet_snapshot(self) -> dict[str, Any]:
        return {
            "capacity": capacity(self.store),
            "unfinished_assignments": self.store.rows(
                """SELECT a.id, a.run_id, a.bead_id, a.stage, a.condition,
                          a.next_attempt_at, a.operator_hold_id, r.project_id, r.priority
                   FROM assignments a JOIN runs r ON r.id = a.run_id
                   WHERE a.stage NOT IN ('completed','canceled') ORDER BY r.priority DESC, a.run_id, a.id"""
            ),
            "approved_waiting": self.store.rows(
                """SELECT r.id AS run_id, r.project_id, r.priority, a.id AS assignment_id,
                          a.bead_id, a.stage
                   FROM runs r JOIN assignments a ON a.run_id = r.id
                   WHERE r.state IN ('approved','active') AND a.stage IN ('queued','preparing')
                   ORDER BY r.priority DESC, r.id, a.id"""
            ),
            "holds": self.store.rows(
                "SELECT id, scope, target, reason, urgent, release_condition FROM holds WHERE released_at IS NULL ORDER BY urgent DESC, id"
            ),
            "policies": self.store.rows(
                "SELECT id, kind, scope, cadence_seconds, next_due_at, active FROM policies ORDER BY id"
            ),
        }

    def _reactivate_deferred_batches(self) -> None:
        now = utc_now()
        for deferred in self.store.rows(
            "SELECT * FROM deferred_batches ORDER BY batch_id"
        ):
            condition = json.loads(deferred["reactivation_json"])
            ready = bool(deferred["next_check_at"] and deferred["next_check_at"] <= now)
            dependency = condition.get("dependency")
            if isinstance(dependency, str):
                ready = (
                    ready
                    or self.store.row(
                        "SELECT 1 FROM assignments WHERE bead_id = ? AND stage = 'completed'",
                        (dependency,),
                    )
                    is not None
                )
            hold_id = condition.get("hold")
            if isinstance(hold_id, int):
                ready = (
                    ready
                    or self.store.row(
                        "SELECT 1 FROM holds WHERE id = ? AND released_at IS NOT NULL",
                        (hold_id,),
                    )
                    is not None
                )
            if condition.get("capacity"):
                snapshot = capacity(self.store)
                ready = ready or bool(
                    snapshot["global_limit"] is not None
                    and snapshot["global_usage"] < snapshot["global_limit"]
                )
            if condition.get("operator_change"):
                ready = (
                    ready
                    or self.store.row(
                        """SELECT 1 FROM updates u JOIN batches b
                           ON b.recipient_task_id = u.recipient_task_id
                           WHERE b.id = ? AND u.state = 'retained'
                           AND u.actionable = 1 AND u.id NOT IN (
                             SELECT update_id FROM batch_updates WHERE batch_id = ?
                           ) LIMIT 1""",
                        (deferred["batch_id"], deferred["batch_id"]),
                    )
                    is not None
                )
            if not ready:
                continue
            with self.store.transaction() as connection:
                connection.execute(
                    """UPDATE updates SET state = 'retained', updated_at = ?
                       WHERE id IN (SELECT update_id FROM batch_updates WHERE batch_id = ?)""",
                    (now, deferred["batch_id"]),
                )
                connection.execute(
                    "UPDATE batches SET state = 'processed', updated_at = ? WHERE id = ?",
                    (now, deferred["batch_id"]),
                )
                connection.execute(
                    "DELETE FROM deferred_batches WHERE batch_id = ?",
                    (deferred["batch_id"],),
                )

    async def _manage_interviews(self) -> None:
        now = utc_now()
        for interview in self.store.rows(
            "SELECT * FROM interviews WHERE state = 'queued' AND deadline_at <= ?",
            (now,),
        ):
            action = self.store.row(
                """SELECT * FROM actions WHERE occurrence_id = ? AND task_id = ?
                   AND kind = 'interview'
                   AND state IN ('pending','starting','active','terminal','uncertain')""",
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
            """SELECT 1 FROM actions WHERE occurrence_id = ? AND kind = 'specialist'
               AND state IN ('pending','starting','active','terminal','uncertain')""",
            (occurrence["id"],),
        )
        if existing is not None:
            return
        scope = _occurrence_scope(occurrence["scope"])
        project_ids = [] if scope.get("global") else scope.get("projects", [])
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
                        "scope": scope,
                        "prompt": occurrence["prompt"],
                        "retained_evidence": json.loads(
                            occurrence["evidence_json"] or "{}"
                        ),
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
            reservation_projects = (
                []
                if occurrence["kind"] == "sage" and scope.get("global")
                else project_ids
            )
            if not self._specialist_capacity_available(reservation_projects):
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
            if not scope.get("projects"):
                scope["projects"] = [
                    row["project_id"]
                    for row in self.store.rows(
                        "SELECT project_id FROM projects WHERE enabled = 1 ORDER BY project_id"
                    )
                ]
                self.store.execute(
                    "UPDATE occurrences SET scope = ? WHERE id = ?",
                    (json.dumps(scope), occurrence["id"]),
                )
                occurrence["scope"] = json.dumps(scope)
            evidence = self._specialist_evidence(scope, occurrence=occurrence)
            self.store.execute(
                "UPDATE occurrences SET evidence_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(evidence, sort_keys=True), utc_now(), occurrence["id"]),
            )
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
                    json.dumps(
                        {
                            "scope": scope,
                            "prompt": occurrence["prompt"],
                            "retained_evidence": evidence,
                            "required_method": (
                                "reconstruct the event timeline and identify violated workflow invariants"
                                if occurrence["kind"] == "sage"
                                else "inspect each named source revision and report coverage plus reproducible defects"
                            ),
                        },
                        sort_keys=True,
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
            if action is None:
                raise StoreError("specialist action was not retained")
            self.store.execute(
                "INSERT INTO reservations(action_id, global_slots, project_ids, state, created_at) VALUES (?, 1, ?, 'reserved', ?)",
                (action["id"], json.dumps(reservation_projects), timestamp),
            )
            self.store.execute(
                "UPDATE occurrences SET state = 'active', updated_at = ? WHERE id = ?",
                (timestamp, occurrence["id"]),
            )
            await self._dispatch_action(action, task=task)

    def _specialist_evidence(
        self, scope: dict[str, Any], *, occurrence: dict[str, Any]
    ) -> dict[str, Any]:
        projects: list[dict[str, Any]] = []
        requested = scope.get("projects", [])
        for project in self.store.rows(
            "SELECT project_id, repo_path FROM projects WHERE enabled = 1 ORDER BY project_id"
        ):
            if requested and project["project_id"] not in requested:
                continue
            revision: str | None = None
            try:
                result = subprocess.run(
                    ["git", "-C", project["repo_path"], "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                if result.returncode == 0:
                    revision = result.stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                revision = None
            projects.append({**project, "source_revision": revision})
        cutoff = utc_now()
        prior = self.store.row(
            "SELECT evidence_json, updated_at FROM occurrences WHERE kind = ? AND scope IS ? AND state = 'complete' AND id != ? ORDER BY updated_at DESC LIMIT 1",
            (occurrence["kind"], occurrence["scope"], occurrence["id"]),
        )
        start = None
        if prior:
            start = (
                json.loads(prior["evidence_json"] or "{}").get("captured_at")
                or prior["updated_at"]
            )
        project_ids = [item["project_id"] for item in projects]
        placeholders = ",".join("?" for _ in project_ids) or "NULL"
        assignments = self.store.rows(
            f"""SELECT a.id, a.run_id, a.bead_id, a.stage, a.condition,
                      a.candidate_id, a.source_oid, a.tested_oid, r.project_id
               FROM assignments a JOIN runs r ON r.id = a.run_id
               WHERE r.project_id IN ({placeholders}) AND a.updated_at <= ?
                 AND (? IS NULL OR a.updated_at > ?)
               ORDER BY a.updated_at DESC, a.id DESC LIMIT 101""",
            (*project_ids, cutoff, start, start),
        )
        # Events without a project binding remain explicitly labelled fleet context.
        # Exclude events whose direct project/task/run/assignment binding is elsewhere.
        events = self.store.rows(
            f"""SELECT e.id, e.kind, e.entity_type, e.entity_id, e.message,
                       e.detail_json, e.created_at,
                       COALESCE(t.project_id, r.project_id, ar.project_id,
                         CASE WHEN e.entity_type = 'project' THEN e.entity_id END) AS project_id
                FROM events e
                LEFT JOIN tasks t ON e.entity_type = 'task' AND e.entity_id = CAST(t.id AS TEXT)
                LEFT JOIN runs r ON e.entity_type = 'run' AND e.entity_id = CAST(r.id AS TEXT)
                LEFT JOIN assignments a ON e.entity_type = 'assignment' AND e.entity_id = CAST(a.id AS TEXT)
                LEFT JOIN runs ar ON ar.id = a.run_id
                WHERE e.created_at <= ? AND (? IS NULL OR e.created_at > ?)
                  AND (COALESCE(t.project_id, r.project_id, ar.project_id,
                         CASE WHEN e.entity_type = 'project' THEN e.entity_id END) IS NULL
                       OR COALESCE(t.project_id, r.project_id, ar.project_id,
                         CASE WHEN e.entity_type = 'project' THEN e.entity_id END) IN ({placeholders}))
                ORDER BY e.id DESC LIMIT 201""",
            (cutoff, start, start, *project_ids),
        )
        reports = self.store.rows(
            """SELECT id, kind, scope, publication_revision, report_json, created_at
               FROM occurrences WHERE state = 'complete' AND report_json IS NOT NULL
                 AND kind = ? AND scope IS ? ORDER BY id DESC LIMIT 21""",
            (occurrence["kind"], occurrence["scope"]),
        )
        return {
            "captured_at": cutoff,
            "window": {
                "after": start,
                "through": cutoff,
                "basis": (
                    "prior matching completed report cutoff"
                    if start
                    else "available retained history; no prior matching report"
                ),
            },
            "projects": [
                {
                    **project,
                    "revision_basis": "captured repository HEAD; certification not established",
                }
                for project in projects
            ],
            "coverage": {
                "assignments": {
                    "limit": 100,
                    "truncated": len(assignments) > 100,
                    "selection": "updated in interval, selected projects",
                },
                "events": {
                    "limit": 200,
                    "truncated": len(events) > 200,
                    "selection": "interval events for selected projects plus unbound fleet context",
                },
                "prior_reports": {
                    "limit": 20,
                    "truncated": len(reports) > 20,
                    "selection": "same role and retained scope",
                },
                "missing": "Native Codex histories, complete Tollgate logs, token/latency telemetry and existing Beads are not automatically included; inspect relevant durable sources or report gaps.",
            },
            "assignments": assignments[:100],
            "recent_events": events[:200],
            "prior_reports": reports[:20],
        }

    async def _publish_occurrences(self) -> None:
        for occurrence in self.store.rows(
            "SELECT * FROM occurrences WHERE state = 'publishing'"
        ):
            obligation = self.store.row(
                """SELECT * FROM obligations WHERE kind = 'finding_publication'
                   AND identity = ? AND target = ?""",
                (str(occurrence["id"]), str(occurrence["id"])),
            )
            if obligation and (
                obligation["operator_hold_id"] is not None
                or (
                    obligation["next_attempt_at"]
                    and obligation["next_attempt_at"] > utc_now()
                )
            ):
                continue
            report = json.loads(occurrence["report_json"] or "{}")
            try:
                publication = await asyncio.to_thread(
                    self._publish_specialist_report, occurrence, report
                )
                for finding in report.get("findings", []):
                    if finding.get("existing_bead_id"):
                        bead = self.store.row(
                            "SELECT bead_id FROM beads WHERE bead_id = ? AND project_id = ?",
                            (finding["existing_bead_id"], finding["project"]),
                        )
                        if bead is None:
                            raise StoreError(
                                f"finding names unknown bead {finding['existing_bead_id']}"
                            )
                        await asyncio.to_thread(
                            self._publish_existing_finding,
                            occurrence,
                            finding,
                        )
                        continue
                    task_payload = {
                        "project": finding["project"],
                        "activation": finding.get("activation", "pending"),
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
                            intake_key=(
                                f"finding:{finding['project']}:{finding['identity']}"
                            ),
                        ),
                    )
                self.store.execute(
                    """UPDATE occurrences SET state = 'complete', publication_revision = ?,
                       updated_at = ? WHERE id = ?""",
                    (publication["local_revision"], utc_now(), occurrence["id"]),
                )
                if obligation:
                    self.store.execute(
                        """UPDATE obligations SET state = 'complete', detail = NULL,
                           next_attempt_at = NULL, updated_at = ? WHERE id = ?""",
                        (utc_now(), obligation["id"]),
                    )
                self._queue_archon_update(
                    f"specialist:{occurrence['id']}",
                    {
                        "kind": "specialist_completed",
                        "occurrence_id": occurrence["id"],
                        "specialist": occurrence["kind"],
                        "scope": _occurrence_scope(occurrence["scope"]),
                        "summary": report.get("summary"),
                        "finding_count": len(report.get("findings", [])),
                        "publication_revision": publication["local_revision"],
                    },
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
                self._defer_occurrence_publication(occurrence, obligation, str(error))

    def _publish_specialist_report(
        self, occurrence: dict[str, Any], report: dict[str, Any]
    ) -> dict[str, Any]:
        report_root = self.paths.brain_root / "reports" / str(occurrence["kind"])
        report_root.mkdir(parents=True, exist_ok=True)
        path = report_root / f"{occurrence['id']}.json"
        document = {
            "occurrence": {
                "id": occurrence["id"],
                "kind": occurrence["kind"],
                "scope": _occurrence_scope(occurrence["scope"]),
                "authority": occurrence["authority"],
                "created_at": occurrence["created_at"],
            },
            "reviewed_evidence": json.loads(occurrence["evidence_json"] or "{}"),
            "report": report,
        }
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(document, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        operation = self.store.create_operation(
            "brain_report_publish",
            str(occurrence["id"]),
            {"path": str(path), "kind": occurrence["kind"]},
        )
        attempt = self.store.begin_operation_attempt(operation)
        started = time.monotonic()
        try:
            result = BrainRepository(self.paths.brain_root).publish(
                [path], f"docs: publish {occurrence['kind']} report {occurrence['id']}"
            )
            evidence = {
                "branch": result.branch,
                "local_revision": result.local_revision,
                "remote_revision": result.remote_revision,
                "merged_remote": result.merged_remote,
            }
            self.store.finish_operation_attempt(
                operation,
                attempt,
                state="complete",
                result=evidence,
                native_id=result.local_revision,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            return evidence
        except Exception as error:
            self._operation_failed(
                operation,
                error,
                attempt=attempt,
                started=started,
                mutation=True,
            )
            raise

    def _publish_existing_finding(
        self, occurrence: dict[str, Any], finding: dict[str, Any]
    ) -> None:
        semantic_key = f"{finding['project']}:{finding['identity']}"
        bead_id = str(finding["existing_bead_id"])
        retained = self.store.row(
            "SELECT * FROM finding_publications WHERE semantic_key = ?",
            (semantic_key,),
        )
        if retained is not None:
            if retained["bead_id"] != bead_id:
                raise StoreError(
                    f"finding identity {semantic_key!r} names conflicting beads"
                )
            if retained["state"] == "complete":
                return
        marker = f"[fulcrum-finding:{semantic_key}]"
        observed = self.beads.show(bead_id)
        if observed is not None and marker in json.dumps(observed, sort_keys=True):
            self._record_existing_finding(
                semantic_key, int(occurrence["id"]), bead_id, finding
            )
            return
        content = (
            f"{marker}\nSpecialist evidence: {finding['evidence']}\n"
            f"Expected benefit: {finding['expected_benefit']}"
        )
        operation = self.store.create_operation(
            "beads_finding_comment",
            semantic_key,
            {
                "bead_id": bead_id,
                "content": content,
                "marker": marker,
                "occurrence_id": occurrence["id"],
                "finding": finding,
            },
        )
        attempt = self.store.begin_operation_attempt(operation)
        started = time.monotonic()
        try:
            self.beads.comment(bead_id, content)
            self.store.finish_operation_attempt(
                operation,
                attempt,
                state="complete",
                result={"bead_id": bead_id, "marker": marker},
                native_id=bead_id,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            self._record_existing_finding(
                semantic_key, int(occurrence["id"]), bead_id, finding
            )
        except Exception as error:
            self._operation_failed(
                operation,
                error,
                attempt=attempt,
                started=started,
                mutation=True,
            )
            raise

    def _record_existing_finding(
        self,
        semantic_key: str,
        occurrence_id: int,
        bead_id: str,
        finding: dict[str, Any],
    ) -> None:
        timestamp = utc_now()
        self.store.execute(
            """INSERT INTO finding_publications(
                   semantic_key, occurrence_id, bead_id, evidence_json, state,
                   created_at, updated_at
               ) VALUES (?, ?, ?, ?, 'complete', ?, ?)
               ON CONFLICT(semantic_key) DO UPDATE SET state = 'complete',
               evidence_json = excluded.evidence_json, updated_at = excluded.updated_at""",
            (
                semantic_key,
                occurrence_id,
                bead_id,
                json.dumps(finding, sort_keys=True),
                timestamp,
                timestamp,
            ),
        )

    def _defer_occurrence_publication(
        self,
        occurrence: dict[str, Any],
        obligation: dict[str, Any] | None,
        reason: str,
    ) -> None:
        retries = int(obligation["retry_count"] if obligation else 0) + 1
        timestamp = utc_now()
        hold_id: int | None = None
        next_attempt: str | None = None
        if retries >= 8:
            hold = self.store.execute(
                """INSERT INTO holds(scope, target, reason, urgent, release_condition, created_at)
                   VALUES ('obligation', ?, ?, 1,
                   'operator repairs brain or Beads publication and releases the hold', ?)""",
                (str(occurrence["id"]), reason, timestamp),
            )
            hold_id = int(hold.lastrowid)
        else:
            next_attempt = (
                (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=5 * (2 ** min(retries - 1, 6)))
                )
                .isoformat()
                .replace("+00:00", "Z")
            )
        self.store.execute(
            """INSERT INTO obligations(
                   kind, identity, target, state, detail, retry_count,
                   next_attempt_at, operator_hold_id, created_at, updated_at
               ) VALUES ('finding_publication', ?, ?, 'failed', ?, ?, ?, ?, ?, ?)
               ON CONFLICT(kind, identity, target) DO UPDATE SET state = 'failed',
               detail = excluded.detail, retry_count = excluded.retry_count,
               next_attempt_at = excluded.next_attempt_at,
               operator_hold_id = excluded.operator_hold_id,
               updated_at = excluded.updated_at""",
            (
                str(occurrence["id"]),
                str(occurrence["id"]),
                reason,
                retries,
                next_attempt,
                hold_id,
                timestamp,
                timestamp,
            ),
        )
        self.store.event(
            "specialist_publication_deferred",
            reason,
            entity_type="occurrence",
            entity_id=occurrence["id"],
            detail={"retry_count": retries, "next_attempt_at": next_attempt},
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
            reset_exceptions = await self._reset_state(record)
        else:
            reset_exceptions = []
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
            self.store.execute(
                """UPDATE runs SET executor_task_id = NULL, overseer_task_id = NULL,
                   updated_at = ? WHERE state IN ('approved','active','held')""",
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
        return {
            "complete": True,
            "mode": mode,
            "archive_exceptions": reset_exceptions,
            "replacement": initialized,
            **initialized,
        }

    async def _reset_state(self, record: dict[str, Any]) -> list[dict[str, str]]:
        owned = self.store.rows(
            """SELECT a.id, a.worktree_path, a.candidate_id, p.tollgate_repo_id
               FROM assignments a JOIN runs r ON r.id = a.run_id
               JOIN projects p ON p.project_id = r.project_id
               WHERE a.worktree_path IS NOT NULL AND a.stage NOT IN ('completed','canceled')
               ORDER BY a.id"""
        )
        requires_tollgate = any(
            item["candidate_id"] or Path(str(item["worktree_path"])).exists()
            for item in owned
        )
        if requires_tollgate and self.tollgate is None:
            raise StoreError(
                "reset incomplete; Tollgate is unavailable for owned worktree cleanup"
            )
        tollgate = self.tollgate
        cleanup_errors: list[str] = []
        completed_candidates = set(record.get("candidates_disposed", []))
        completed_worktrees = set(record.get("worktrees_removed", []))
        for item in owned:
            identity = str(item["id"])
            candidate_id = item["candidate_id"]
            try:
                if candidate_id and identity not in completed_candidates:
                    assert tollgate is not None
                    observed = await asyncio.to_thread(
                        tollgate.status, item["tollgate_repo_id"], candidate_id
                    )
                    candidate = _candidate_by_id(observed, candidate_id)
                    if candidate and candidate.get("state") in {
                        "queued",
                        "running",
                        "validated",
                        "promoting",
                    }:
                        await asyncio.to_thread(
                            tollgate.cancel, item["tollgate_repo_id"], candidate_id
                        )
                        confirmed = await asyncio.to_thread(
                            tollgate.status, item["tollgate_repo_id"], candidate_id
                        )
                        retained = _candidate_by_id(confirmed, candidate_id)
                        if retained and retained.get("state") in {
                            "queued",
                            "running",
                            "validated",
                            "promoting",
                        }:
                            raise StoreError(
                                f"candidate {candidate_id} remains active after cancellation"
                            )
                    completed_candidates.add(identity)
                    record["candidates_disposed"] = sorted(completed_candidates)
                    self._write_reboot_record(record)
                path = Path(str(item["worktree_path"]))
                if identity not in completed_worktrees and path.exists():
                    assert tollgate is not None
                    await asyncio.to_thread(
                        tollgate.remove_worktree,
                        item["tollgate_repo_id"],
                        item["worktree_path"],
                    )
                    if path.exists():
                        raise StoreError(
                            f"worktree {path} remains present after removal"
                        )
                completed_worktrees.add(identity)
                self.store.execute(
                    "UPDATE assignments SET worktree_path = NULL, updated_at = ? WHERE id = ?",
                    (utc_now(), item["id"]),
                )
                record["worktrees_removed"] = sorted(completed_worktrees)
                self._write_reboot_record(record)
            except Exception as error:
                cleanup_errors.append(f"assignment {identity}: {error}")
        archive_exceptions: list[dict[str, str]] = []
        archived_threads = set(record.get("threads_archived", []))
        for task in self.store.rows(
            "SELECT * FROM tasks WHERE state NOT IN ('retired','archived')"
        ):
            thread_id = str(task["native_thread_id"])
            if thread_id in archived_threads:
                continue
            try:
                await self.runtime.archive(thread_id)
                archived_threads.add(thread_id)
                self.store.execute(
                    "UPDATE tasks SET state = 'archived', archived = 1, updated_at = ? WHERE id = ?",
                    (utc_now(), task["id"]),
                )
            except Exception as error:
                archive_exceptions.append(
                    {"thread_id": thread_id, "condition": str(error)}
                )
                self.store.execute(
                    "UPDATE tasks SET state = 'uncertain', updated_at = ? WHERE id = ?",
                    (utc_now(), task["id"]),
                )
            record["threads_archived"] = sorted(archived_threads)
            record["archive_exceptions"] = archive_exceptions
            self._write_reboot_record(record)
        if cleanup_errors:
            record["cleanup_errors"] = cleanup_errors
            self._write_reboot_record(record)
            raise StoreError(
                "reset retained cleanup failures: " + "; ".join(cleanup_errors)
            )
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
        self.store = Store(
            self.paths.database, event_log=self.paths.logs_root / "workflow.jsonl"
        )
        self._initialize_configuration()
        if archive_exceptions:
            self.store.event(
                "reset_archive_exceptions",
                "reset completed with quarantined native thread archives",
                detail={"exceptions": archive_exceptions},
            )
        return archive_exceptions

    def _write_reboot_record(self, record: dict[str, Any]) -> None:
        temporary = self.paths.reboot_record.with_name(
            f".{self.paths.reboot_record.name}.{os.getpid()}.tmp"
        )
        temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.paths.reboot_record)

    async def _source_watch_loop(self) -> None:
        source = Path(self.config.source_root) / "src" / "fulcrum"
        async for changes in awatch(source, debounce=250):
            self.store.heartbeat("source-watch")
            if not any(str(path).endswith((".py", ".md")) for _, path in changes):
                continue
            self.starts_enabled = False
            async with self.mutation_lock:
                self.store.event(
                    "source_reload",
                    "Python source changed; quiescent controller refresh requested",
                )
                self.store.execute(
                    "INSERT INTO meta(key, value) VALUES ('source_refresh_pending', '1') ON CONFLICT(key) DO UPDATE SET value = '1'"
                )
            self.advance_requested.set()

    async def _maybe_refresh_source(self) -> bool:
        requested = self.store.row(
            "SELECT value FROM meta WHERE key = 'source_refresh_pending'"
        )
        if requested is None or requested["value"] != "1":
            return False
        in_flight = self.store.row(
            "SELECT id FROM external_operations WHERE state = 'sent' LIMIT 1"
        )
        if in_flight is not None:
            self.store.event(
                "source_refresh_deferred",
                "waiting for an in-flight external mutation to be observed",
                entity_type="operation",
                entity_id=in_flight["id"],
            )
            return True
        self.store.event(
            "source_refresh_quiescent",
            "all external mutations are observed; controller refresh may proceed",
        )
        if os.environ.get("FULCRUM_DISABLE_REEXEC") == "1":
            self.store.execute(
                "UPDATE meta SET value = '0' WHERE key = 'source_refresh_pending'"
            )
            self.starts_enabled = self.runtime.ready
            return False
        await self.runtime.close()
        server = self.server
        if server is not None:
            server.close()
            await server.wait_closed()
        install_control_plane(self.config, self.paths)
        arguments = controller_program_arguments(self.config, self.paths)
        self.store.execute(
            "UPDATE meta SET value = '0' WHERE key = 'source_refresh_pending'"
        )
        self.store.event(
            "source_refresh_installed",
            "control-plane snapshot installed; transferring controller ownership",
            detail={"program": arguments[0]},
        )
        if self.lock_handle is None:
            raise StoreError("controller lock disappeared before source refresh")
        os.environ[INHERITED_LOCK_FD_ENV] = str(self.lock_handle.fileno())
        self.store.close()
        os.execv(arguments[0], arguments)
        return True


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
