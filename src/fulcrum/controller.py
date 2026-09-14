"""Single-writer controller for runtime events, scheduling, and delivery."""

from __future__ import annotations

import asyncio
import errno
import fcntl
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
import traceback
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from watchfiles import awatch

from fulcrum.beads import Beads
from fulcrum.brain import BrainRepository

from fulcrum.config import (
    InstallationConfig,
    RuntimePaths,
    safe_child,
    save_installation,
)
from fulcrum.intake import (
    file_graph,
    file_report,
    file_task,
    reconcile_beads_creation,
    report_task_from_payload,
    task_from_payload,
)
from fulcrum.install import (
    APP_SERVER_LABEL,
    CONTROLLER_LABEL,
    control_plane_source,
    controller_program_arguments,
    inspect_service,
    install_control_plane,
    provision_worktree_environment,
)
from fulcrum.ipc import MAX_MESSAGE_BYTES
from fulcrum.lifecycle import (
    accept_finish,
    apply_archon_decisions,
    observe_action_terminal,
    schedule_run_archival,
)
from fulcrum.operative import (
    authority_gate,
    journal_is_unfinished,
    read_journal,
    transition_journal,
    write_journal,
)
from fulcrum.outcomes import (
    ALLOWED,
    STRUCTURED_OUTCOME_FILENAMES,
    validate_outcome,
)
from fulcrum.kernel import (
    LeaseRequest,
    acquire_lease,
    invariant_violations,
    schedule_action_retry,
)
from fulcrum.prompts import (
    MAX_ARCHON_MESSAGE_CHARS,
    action_message,
    archon_action_fits,
    direct_sage_instructions,
    operative_instructions,
    role_instructions,
    weaver_instructions,
)
from fulcrum.readiness import progress_readiness, state_readiness
from fulcrum.reset import reset_brain
from fulcrum.resources import (
    AppServerResourceProbe,
    MAX_ACTIVE_CONVERSATIONS,
    MAX_IDLE_WORKER_CONVERSATIONS,
    ResourceProbeError,
    ResourceSnapshot,
    bounded_condition,
)
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
FALLBACK_RECONCILIATION_SECONDS = 30
COMPLETION_ARCHIVE_DELAY = timedelta(minutes=10)
RESOURCE_RECLAIM_DELAY = timedelta(minutes=10)
COMPLETION_ARCHIVE_ROLES: frozenset[str] = frozenset(
    {"weaver", "executor", "overseer", "sage", "inquisitor"}
)
RUNTIME_WORKFLOW_EVENTS: frozenset[str] = frozenset(
    {
        "fulcrum/runtime/disconnected",
        "thread/archived",
        "thread/status/changed",
        "thread/unarchived",
        "turn/completed",
        "turn/started",
    }
)
RUNTIME_TELEMETRY_EVENTS: frozenset[str] = frozenset(
    {
        "thread/tokenUsage/updated",
        "model/rerouted",
        "item/started",
        "item/completed",
    }
)
MAX_EXACT_SOURCE_ARTIFACT_BYTES = 1_000_000
ARCHON_SCOPE_SUMMARY_CHARS = 240
ARCHON_BATCH_LIMIT = 20
OPERATIVE_PASSIVE_COMMANDS: frozenset[str] = frozenset(
    {
        "status",
        "usage",
        "cost",
        "context",
        "archon",
        "operative_status",
        "operative_dossier",
    }
)
OPERATIVE_CONTROL_COMMANDS: frozenset[str] = frozenset(
    {
        "operative_register",
        "operative_finish",
        "operative_abort",
        "operative_recover",
        "operative_wind_down",
        "operative_reconcile",
        "operative_worktree",
        "operative_repair_check",
        "operative_reinstall",
        "operative_service_check",
    }
)


class DeliveryDisposition(StrEnum):
    SATISFIED = "satisfied"
    SOURCE_FAILED = "source-failed"
    POST_PROMOTION_PENDING = "post-promotion-pending"
    POST_PROMOTION_ATTENTION = "post-promotion-attention"
    UNRESOLVED = "unresolved"


class ResourceAdmissionPaused(StoreError):
    """A native thread/turn start was withheld to preserve descriptor reserve."""


@dataclass(frozen=True)
class DeliveryStatus:
    disposition: DeliveryDisposition
    condition: str | None = None


class Controller:
    """Own all operational mutations for one configured environment."""

    def __init__(self, paths: RuntimePaths, config: InstallationConfig) -> None:
        self.paths = paths
        self.config = config
        self.lock_handle: Any = None
        self.acquire_process_lock()
        self.store = Store(
            paths.database,
            event_log=paths.logs_root / "workflow.jsonl",
            operative_journal=paths.operative_journal,
        )
        self.operative_journal: dict[str, Any] | None = None
        self.operative_journal_error: str | None = None
        self._reconcile_operative_authority()
        self.store.mark_open_usage_gap(
            "controller restarted before terminal usage confirmation"
        )
        self.runtime = CodexRuntime(
            config.app_server_endpoint, event_handler=self._queue_runtime_event
        )
        self.tollgate: Tollgate | None = None
        self.beads = Beads(paths.brain_root)
        self.events: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
        self.mutation_lock = asyncio.Lock()
        self.telemetry_lock = asyncio.Lock()
        self.server: asyncio.AbstractServer | None = None
        self.stop_event = asyncio.Event()
        self.advance_requested = asyncio.Event()
        self.starts_enabled = False
        # Unit callers construct Controller without starting its external
        # services.  The live process installs the probe in start().
        self.resource_probe: AppServerResourceProbe | None = None
        self.last_resource_snapshot: ResourceSnapshot | None = None
        retained_resource_condition = self.store.row(
            "SELECT value FROM meta WHERE key = 'resource_admission_condition'"
        )
        self.resource_admission_condition: str | None = (
            str(retained_resource_condition["value"])
            if retained_resource_condition and retained_resource_condition["value"]
            else None
        )
        self.critical_workers = {
            "events",
            "fallback",
            "advancement",
            "source-watch",
        }

    def _reconcile_operative_authority(self) -> None:
        """Reconcile journal/store combinations protectively while holding the OS lock."""

        try:
            journal = read_journal(self.paths.operative_journal)
        except StoreError as error:
            self.operative_journal_error = str(error)
            self.store.execute(
                """INSERT INTO meta(key, value) VALUES ('dispatch_enabled', '0')
                   ON CONFLICT(key) DO UPDATE SET value = '0'"""
            )
            self.store.event(
                "operative_journal_unreadable",
                str(error),
                entity_type="operative_takeover",
                entity_id="unknown",
            )
            return
        mirror = self.store.unfinished_operative_takeover()
        if journal is None and mirror is not None:
            journal = self.store.operative_journal_from_mirror(mirror)
            write_journal(self.paths.operative_journal, journal)
            self.store.event(
                "operative_journal_reconstructed",
                "reconstructed missing journal from unfinished SQLite mirror",
                entity_type="operative_takeover",
                entity_id=mirror["takeover_id"],
            )
        elif (
            journal is not None
            and mirror is not None
            and (journal["takeover_id"] != mirror["takeover_id"])
        ):
            self.operative_journal_error = "operative journal conflicts with a different unfinished SQLite takeover"
            self.store.execute(
                """INSERT INTO meta(key, value) VALUES ('dispatch_enabled', '0')
                   ON CONFLICT(key) DO UPDATE SET value = '0'"""
            )
            return
        if journal is not None:
            self.store.mirror_operative_journal(journal)
            if journal_is_unfinished(journal):
                task = self.store.row(
                    "SELECT * FROM tasks WHERE native_thread_id = ? AND role = 'operative'",
                    (journal["native_thread_id"],),
                )
                action = (
                    self.store.row(
                        """SELECT * FROM actions WHERE task_id = ? AND kind = 'operative'
                           AND (state IN ('pending','starting','active','terminal','uncertain')
                                OR (? = 'closing' AND state = 'processed'))
                           ORDER BY id DESC LIMIT 1""",
                        (task["id"], journal["state"]),
                    )
                    if task is not None
                    else None
                )
                if task is None or action is None:
                    if (
                        journal.get("state") == "acquiring"
                        and journal.get("operative_identity") is None
                    ):
                        self.operative_journal = journal
                        self.store.execute(
                            """INSERT INTO meta(key, value) VALUES ('dispatch_enabled', '0')
                               ON CONFLICT(key) DO UPDATE SET value = '0'"""
                        )
                        return
                    try:
                        task, action = self.store.reconstruct_operative_binding(journal)
                    except StoreError as error:
                        self.operative_journal_error = str(error)
                        self.store.execute(
                            """INSERT INTO meta(key, value) VALUES ('dispatch_enabled', '0')
                               ON CONFLICT(key) DO UPDATE SET value = '0'"""
                        )
                        return
                    effects = list(journal["completed_effects"])
                    if "sqlite_authority_reconstructed" not in effects:
                        effects.append("sqlite_authority_reconstructed")
                    journal = dict(journal)
                    journal.update(
                        {
                            "task_id": task["id"],
                            "action_id": action["id"],
                            "completed_effects": effects,
                            "updated_at": utc_now(),
                        }
                    )
                    write_journal(self.paths.operative_journal, journal)
                    self.store.mirror_operative_journal(journal)
                self.store.execute(
                    """UPDATE tasks SET state = 'active', updated_at = ?
                       WHERE id = ? AND state NOT IN ('retired','archived')""",
                    (utc_now(), task["id"]),
                )
        self.operative_journal = journal
        if journal_is_unfinished(journal):
            self.store.execute(
                """INSERT INTO meta(key, value) VALUES ('dispatch_enabled', '0')
                   ON CONFLICT(key) DO UPDATE SET value = '0'"""
            )

    def _operative_fenced(self) -> bool:
        return self.operative_journal_error is not None or journal_is_unfinished(
            self.operative_journal
        )

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
        if self._operative_fenced():
            self.store.execute(
                """INSERT INTO meta(key, value) VALUES ('controller_state', 'operative_only')
                   ON CONFLICT(key) DO UPDATE SET value = 'operative_only'"""
            )
            self.store.event(
                "controller_starting",
                "controller is starting in operative-only mode",
            )
        else:
            self._initialize_configuration()
        self.paths.socket.unlink(missing_ok=True)
        self.server = await asyncio.start_unix_server(
            self._handle_client, path=self.paths.socket, limit=MAX_MESSAGE_BYTES
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
        # A substituted runtime (tests or an embedding) cannot safely be paired
        # with telemetry from an unrelated launchd-owned app-server.
        if isinstance(self.runtime, CodexRuntime):
            self.resource_probe = AppServerResourceProbe()
        if self.runtime.ready and not self._operative_fenced():
            await self._verify_projects()
            await self._repair_task_project_bindings()
        await self.reconcile()
        self.starts_enabled = self.runtime.ready and not self._operative_fenced()
        self.store.execute(
            """INSERT INTO meta(key, value) VALUES ('controller_state', ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            ("operative_only" if self._operative_fenced() else "ready",),
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
        # Telemetry is durable but never enters the workflow queue, takes the
        # workflow mutation lock, logs stream samples, or requests advancement.
        if method in RUNTIME_TELEMETRY_EVENTS:
            async with self.telemetry_lock:
                self._handle_telemetry_event(method, params)
            return
        # Other observational notifications do not mutate workflow state.
        if method not in RUNTIME_WORKFLOW_EVENTS:
            return
        await self.events.put((method, params))

    def _handle_telemetry_event(self, method: str, params: dict[str, Any]) -> None:
        if method == "thread/tokenUsage/updated":
            self.store.observe_turn_usage(params)
            return
        if method == "model/rerouted":
            self.store.observe_model_reroute(params)
            return
        item = params.get("item")
        if not isinstance(item, dict):
            return
        parent_thread = item.get("senderThreadId") or params.get("threadId")
        parent_turn = params.get("turnId")
        collaboration_item = item.get("id")
        if not isinstance(parent_thread, str):
            return
        pricing = item.get("publicPricing")
        if (
            method == "item/completed"
            and isinstance(item.get("id"), str)
            and isinstance(parent_turn, str)
        ):
            owner = self.store.row(
                """SELECT action_id, attributed_action_id
                   FROM action_turn_usage
                   WHERE native_thread_id = ? AND native_turn_id = ?""",
                (parent_thread, parent_turn),
            )
            owner_id = int(owner["action_id"]) if owner and owner["action_id"] else None
            attributed_owner_id = (
                int(owner["attributed_action_id"])
                if owner and owner["attributed_action_id"]
                else owner_id
            )
            source_key = f"tool-call:{parent_thread}:{parent_turn}:{item['id']}"
            if item.get("type") == "webSearch":
                self.store.record_observed_tool(
                    source_key=source_key,
                    tool_name="web_search",
                    native_thread_id=parent_thread,
                    native_turn_id=parent_turn,
                    action_id=owner_id,
                    attributed_action_id=attributed_owner_id,
                )
            elif isinstance(pricing, dict):
                self.store.record_tool_cost(
                    source_key=source_key,
                    tool_name=str(item.get("tool") or item.get("name") or item["type"]),
                    quantity=pricing.get("quantity", 1),
                    unit=str(pricing.get("unit") or "call"),
                    unit_rate=(
                        str(pricing["unitRate"])
                        if pricing.get("unitRate") is not None
                        else None
                    ),
                    source_url=(
                        str(pricing["sourceUrl"])
                        if pricing.get("sourceUrl") is not None
                        else None
                    ),
                    native_thread_id=parent_thread,
                    native_turn_id=parent_turn,
                    action_id=owner_id,
                    attributed_action_id=attributed_owner_id,
                )
        if item.get("type") not in {"collabToolCall", "collabAgentToolCall"}:
            return
        children: set[str] = set()
        new_thread = item.get("newThreadId")
        if isinstance(new_thread, str):
            children.add(new_thread)
        receiver = item.get("receiverThreadId")
        if isinstance(receiver, str) and self.store.row(
            "SELECT 1 FROM telemetry_helper_threads WHERE native_thread_id = ?",
            (receiver,),
        ):
            children.add(receiver)
        legacy_states = item.get("agentsStates") or item.get("agents_states")
        if isinstance(legacy_states, dict):
            children.update(str(key) for key in legacy_states if isinstance(key, str))
        for child in children:
            self.store.observe_helper_thread(
                parent_thread_id=parent_thread,
                parent_turn_id=parent_turn if isinstance(parent_turn, str) else None,
                native_thread_id=child,
                collaboration_item_id=(
                    collaboration_item if isinstance(collaboration_item, str) else None
                ),
            )

    async def _event_loop(self) -> None:
        while True:
            self.store.heartbeat("events")
            method, params = await self.events.get()
            try:
                # Keep the worker boundary defensive for tests and any future
                # producer that writes directly to the queue.
                if method not in RUNTIME_WORKFLOW_EVENTS:
                    continue
                async with self.mutation_lock:
                    if await self._handle_runtime_event(method, params):
                        self.advance_requested.set()
            except Exception as error:
                self.store.event(
                    "event_error",
                    f"{method}: {error}",
                    detail={"traceback": traceback.format_exc()},
                )
            finally:
                self.events.task_done()

    async def _advancement_loop(self) -> None:
        while True:
            self.store.heartbeat("advancement")
            # Periodic recovery belongs to _fallback_loop. This worker only
            # consumes coalesced requests caused by actionable state changes.
            await self.advance_requested.wait()
            self.advance_requested.clear()
            async with self.mutation_lock:
                if not self.runtime.ready:
                    await self._connect_runtime()
                await self.reconcile()
                if await self._maybe_refresh_source():
                    continue
                await self.advance()

    async def _handle_runtime_event(self, method: str, params: dict[str, Any]) -> bool:
        """Apply a relevant runtime event and report whether workflow may advance.

        Codex also emits high-volume item and token notifications. Those events do
        not change Fulcrum state and must not wake reconciliation, or an active
        agent turn creates a read/event/reconcile feedback loop.
        """
        if method == "fulcrum/runtime/disconnected":
            self.starts_enabled = False
            self.store.mark_open_usage_gap(
                str(params.get("error") or "app-server disconnected during turn")
            )
            self.store.execute(
                "INSERT INTO meta(key, value) VALUES ('dispatch_enabled', '0') ON CONFLICT(key) DO UPDATE SET value = '0'"
            )
            self.store.event(
                "runtime_disconnected",
                str(params.get("error") or "app-server connection lost"),
                entity_type="capability",
                entity_id="app_server",
            )
            return True
        thread_id = params.get("threadId")
        if not isinstance(thread_id, str):
            return False
        task = self.store.row(
            "SELECT * FROM tasks WHERE native_thread_id = ?", (thread_id,)
        )
        if task is None:
            if method in {"turn/started", "turn/completed"}:
                turn = params.get("turn")
                turn_id = turn.get("id") if isinstance(turn, dict) else None
                if isinstance(turn_id, str):
                    self.store.observe_helper_turn_started(thread_id, turn_id)
                    if method == "turn/completed":
                        self.store.finalize_native_turn_usage(thread_id, turn_id)
            return False
        if self._operative_fenced():
            return await self._handle_takeover_runtime_event(method, params, task)
        if task["archived"] and method not in {"thread/archived", "thread/unarchived"}:
            return False
        timestamp = utc_now()
        if method == "turn/started":
            self.store.execute(
                """UPDATE tasks SET archive_eligible_at = NULL,
                   archive_idle_turn_id = NULL, resource_idle_since = NULL,
                   resource_reclaimed_at = NULL WHERE id = ?""",
                (task["id"],),
            )
            turn = params.get("turn")
            turn_id = turn.get("id") if isinstance(turn, dict) else None
            action = self.store.row(
                """SELECT id FROM actions WHERE task_id = ?
                   AND state IN ('starting','pending') ORDER BY id DESC LIMIT 1""",
                (task["id"],),
            )
            if action is None:
                return False
            self.store.execute(
                """UPDATE tasks SET state = 'active', runtime_status = 'active',
                   last_turn_terminal = 0, archive_eligible_at = NULL,
                   archive_idle_turn_id = NULL, resource_idle_since = NULL,
                   resource_reclaimed_at = NULL, updated_at = ? WHERE id = ?""",
                (timestamp, task["id"]),
            )
            if isinstance(turn_id, str):
                self.store.execute(
                    "UPDATE actions SET state = 'active', native_turn_id = ?, updated_at = ? WHERE id = ?",
                    (turn_id, timestamp, action["id"]),
                )
                self.store.bind_action_turn(int(action["id"]), thread_id, turn_id)
        elif method == "thread/status/changed":
            raw_status = params.get("status")
            status = (
                raw_status.get("type") if isinstance(raw_status, dict) else raw_status
            )
            if task["runtime_status"] == status:
                return False
            self.store.execute(
                """UPDATE tasks SET runtime_status = ?,
                   archive_eligible_at = CASE WHEN ? = 'idle'
                       THEN archive_eligible_at ELSE NULL END,
                   archive_idle_turn_id = CASE WHEN ? = 'idle'
                       THEN archive_idle_turn_id ELSE NULL END,
                   resource_idle_since = CASE WHEN ? = 'idle'
                       THEN COALESCE(resource_idle_since, ?) ELSE NULL END,
                   updated_at = ? WHERE id = ?""",
                (status, status, status, status, timestamp, timestamp, task["id"]),
            )
        elif method == "turn/completed":
            facts: dict[str, Any] | None = None
            turn = params.get("turn")
            turn_id = turn.get("id") if isinstance(turn, dict) else None
            if not isinstance(turn_id, str):
                return False
            status = (
                turn.get("status", "completed")
                if isinstance(turn, dict)
                else "completed"
            )
            action = self.store.row(
                """SELECT * FROM actions WHERE task_id = ? AND native_turn_id = ?
                   ORDER BY id DESC LIMIT 1""",
                (task["id"], turn_id),
            )
            if action is None:
                facts = await self._refresh_task(task)
                action = self._adopt_unbound_weaver_turn(
                    int(task["id"]), facts, event_turn_id=turn_id
                )
                if action is None:
                    if facts["last_turn_id"] is not None:
                        return False
                    return self._retain_unbound_weaver_completion(
                        int(task["id"]), turn_id
                    )
            if action["state"] in {"processed", "canceled"}:
                return False
            if action["state"] == "failed" and not (
                status == "completed"
                and action["outcome_kind"] is not None
                and str(action["condition"] or "").startswith("runtime turn ")
            ):
                return False
            if action["native_turn_id"] != turn_id:
                return False
            if facts is None:
                facts = await self._refresh_task(task)
            if facts["last_turn_id"] != turn_id:
                return False
            observed_status = (
                str(facts["last_turn_status"])
                if facts["last_turn_terminal"] and facts["last_turn_status"] is not None
                else str(status)
            )
            if (
                observed_status == "completed"
                and action["outcome_kind"]
                in {"ready_for_review", "permitted_repair_complete"}
                and action["assignment_id"] is not None
            ):
                captured = await self._capture_submitted_candidate(
                    int(action["assignment_id"])
                )
                if not captured:
                    return True
            self._finalize_action_usage(int(action["id"]))
            result = observe_action_terminal(
                self.store, int(action["id"]), runtime_state=observed_status
            )
            self._finalize_completed_workflows_for_archon(int(action["id"]))
            if result.get("reminder"):
                action["reminder_sent"] = 1
                self.store.execute(
                    "UPDATE actions SET state = 'pending', updated_at = ? WHERE id = ?",
                    (timestamp, action["id"]),
                )
                await self._dispatch_action(action)
            return bool(
                result.get("advanced")
                or result.get("reminder")
                or result.get("condition")
            )
        elif method in {"thread/archived", "thread/unarchived"}:
            archived = int(method == "thread/archived")
            if method == "thread/archived" and task["resource_orphan_active"]:
                try:
                    await self.runtime.unsubscribe(thread_id)
                except AppServerError as error:
                    self.store.event(
                        "resource_reconciliation_deferred",
                        str(error)[:500],
                        entity_type="task",
                        entity_id=task["id"],
                    )
                    return False
                self.store.execute(
                    """UPDATE tasks SET archived = 1, resource_orphan_active = 0,
                       runtime_status = 'notLoaded', resource_idle_since = NULL,
                       resource_reclaimed_at = NULL, updated_at = ? WHERE id = ?""",
                    (timestamp, task["id"]),
                )
                return True
            if method == "thread/archived" and task["resource_reclaimed_at"]:
                self.store.execute(
                    """UPDATE tasks SET runtime_status = 'notLoaded',
                       resource_idle_since = NULL, updated_at = ? WHERE id = ?""",
                    (timestamp, task["id"]),
                )
                return True
            if int(task["archived"]) == archived:
                return False
            state = "archived" if archived else "idle"
            self.store.execute(
                """UPDATE tasks SET archived = ?, state = ?,
                   archive_eligible_at = NULL, archive_idle_turn_id = NULL,
                   resource_reclaimed_at = NULL,
                   resource_idle_since = CASE WHEN ? = 0 THEN ? ELSE NULL END,
                   updated_at = ? WHERE id = ?""",
                (archived, state, archived, timestamp, timestamp, task["id"]),
            )
        else:
            return False
        return True

    async def _handle_takeover_runtime_event(
        self, method: str, params: dict[str, Any], task: dict[str, Any]
    ) -> bool:
        """Retain runtime truth during takeover without applying ordinary authority."""

        journal: dict[str, Any] | None = self.operative_journal
        if journal is None:
            return False
        turn = params.get("turn")
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        evidence_key = ":".join(
            ["runtime", method, str(task["native_thread_id"]), str(turn_id or "-")]
        )
        self.store.record_operative_evidence(
            str(journal["takeover_id"]),
            evidence_key,
            "runtime_event",
            coverage="observed",
            target_type="task",
            target_id=task["id"],
            detail={
                "method": method,
                "native_thread_id": task["native_thread_id"],
                "native_turn_id": turn_id,
                "status": (
                    turn.get("status")
                    if isinstance(turn, dict)
                    else params.get("status")
                ),
                "quarantined": task["role"] != "operative",
            },
        )
        if method == "turn/completed" and isinstance(turn_id, str):
            self.store.finalize_native_turn_usage(
                str(task["native_thread_id"]), turn_id
            )
        try:
            facts = await self._observe_task(task)
        except Exception as error:
            self.store.record_operative_evidence(
                str(journal["takeover_id"]),
                evidence_key + ":refresh",
                "runtime_observation",
                coverage="unavailable",
                target_type="task",
                target_id=task["id"],
                detail={"error": str(error)},
            )
            return False
        if task["role"] != "operative":
            return False
        action = self.store.row(
            """SELECT * FROM actions WHERE task_id = ? AND kind = 'operative'
               AND state IN ('pending','starting','active','terminal','uncertain')
               ORDER BY id DESC LIMIT 1""",
            (task["id"],),
        )
        if action is not None and action["native_turn_id"] is None:
            action = self._adopt_unbound_operative_turn(
                int(task["id"]), facts, event_turn_id=turn_id
            )
        if (
            action is not None
            and facts["last_turn_terminal"]
            and action.get("native_turn_id") == facts.get("last_turn_id")
        ):
            self._finalize_action_usage(int(action["id"]))
            if journal.get("state") == "closing":
                await self._maybe_finalize_operative_closeout(task, facts)
        return False

    def _finalize_action_usage(self, action_id: int) -> dict[str, Any]:
        """Finalize one action snapshot and emit its bounded summary once."""

        usage_summary = self.store.finalize_action_usage(action_id)
        if usage_summary.pop("_newly_finalized", False):
            self.store.event(
                "action_usage_finalized",
                "retained terminal token-usage summary",
                entity_type="action",
                entity_id=action_id,
                detail=usage_summary,
            )
            cost_summary = self.store.action_cost_summary(action_id)
            self.store.event(
                (
                    "action_cost_computed"
                    if cost_summary["coverage"] == "complete"
                    else "action_cost_partial"
                ),
                "froze terminal API-equivalent cost estimate",
                entity_type="action",
                entity_id=action_id,
                detail=cost_summary,
            )
        return usage_summary

    def _finalize_completed_workflows_for_archon(self, action_id: int) -> None:
        action = self.store.row(
            "SELECT kind, payload, state FROM actions WHERE id = ?", (action_id,)
        )
        if (
            action is None
            or action["kind"] != "archon"
            or action["state"] != "processed"
        ):
            return
        payload = json.loads(action["payload"] or "{}")
        completion_workflows: set[str] = set()
        for item in payload.get("batch_items", []):
            content = item.get("content", {}) if isinstance(item, dict) else {}
            if not isinstance(content, dict) or content.get("kind") not in {
                "assignment_completed",
                "specialist_completed",
            }:
                continue
            completion_workflows.update(self._workflow_ids_for_update(content))
        if not completion_workflows:
            return
        workflows = self.store.rows(
            """SELECT workflow_id, include_cost, exclusion_reason
               FROM workflow_cost_actions WHERE action_id = ?""",
            (action_id,),
        )
        linked = {str(item["workflow_id"]): item for item in workflows}
        for workflow_id in sorted(completion_workflows):
            item = linked.get(workflow_id)
            if item is None:
                continue
            unfinished = self.store.row(
                """SELECT 1 FROM workflow_cost_beads bead
                   WHERE bead.workflow_id = ? AND NOT EXISTS (
                     SELECT 1 FROM assignments assignment
                     WHERE assignment.bead_id = bead.bead_id
                       AND assignment.stage = 'completed') LIMIT 1""",
                (workflow_id,),
            )
            if unfinished is not None:
                continue
            report = self.store.finalize_workflow_cost(workflow_id)
            if not report.pop("_newly_finalized", False):
                continue
            group = report["groups"][0] if report["groups"] else {}
            includes_acknowledgement = bool(item["include_cost"])
            self.store.event(
                "workflow_cost_finalized",
                "froze all-in API-equivalent workflow cost after acknowledgement",
                entity_type="workflow",
                entity_id=workflow_id,
                detail={
                    "amount": report.get("frozen_amount"),
                    "display": (group.get("attributed") or {}).get("display"),
                    "coverage": group.get("coverage", "partial"),
                    "acknowledgement_action_id": action_id,
                    "includes_acknowledgement_action": includes_acknowledgement,
                    "acknowledgement_exclusion": (
                        None
                        if includes_acknowledgement
                        else item.get("exclusion_reason")
                    ),
                },
            )

    def _adopt_unbound_weaver_turn(
        self,
        task_id: int,
        facts: dict[str, Any],
        *,
        event_turn_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Bind the turn already running when a human entry skill registered."""

        task = self.store.row("SELECT role FROM tasks WHERE id = ?", (task_id,))
        kind = "specialist" if task and task["role"] == "sage" else "weaver"
        return self._adopt_unbound_human_turn(
            task_id, facts, kind=kind, event_turn_id=event_turn_id
        )

    def _adopt_unbound_operative_turn(
        self,
        task_id: int,
        facts: dict[str, Any],
        *,
        event_turn_id: str | None = None,
    ) -> dict[str, Any] | None:
        return self._adopt_unbound_human_turn(
            task_id, facts, kind="operative", event_turn_id=event_turn_id
        )

    def _adopt_unbound_human_turn(
        self,
        task_id: int,
        facts: dict[str, Any],
        *,
        kind: str,
        event_turn_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Bind the current native turn for a human-created managed action.

        Human-created actions are registered from inside their native turn,
        so the controller can miss turn/started.  Adoption is limited to the one
        active, unbound action and an authoritative current/just-completed
        last turn.  The registration marker covers restart before finish; an
        accepted outcome covers actions created before the marker existed.
        """

        turn_id = facts.get("last_turn_id")
        if not isinstance(turn_id, str) or (
            event_turn_id is not None and event_turn_id != turn_id
        ):
            return None
        last_status = facts.get("last_turn_status")
        current_shape = bool(
            (last_status == "inProgress" and facts.get("runtime_status") == "active")
            or (
                facts.get("last_turn_terminal")
                and facts.get("runtime_status") == "idle"
            )
        )
        if not current_shape:
            return None
        action = self.store.row(
            """SELECT * FROM actions WHERE task_id = ? AND kind = ?
               AND state = 'active' AND native_turn_id IS NULL""",
            (task_id, kind),
        )
        if action is None:
            return None
        try:
            payload = json.loads(action["payload"])
        except (TypeError, json.JSONDecodeError):
            payload = {}
        allowed_by_registration = payload.get("adopt_current_turn") is True
        legacy_finished_action = bool(
            "adopt_current_turn" not in payload and action["outcome_kind"] is not None
        )
        if not (allowed_by_registration or legacy_finished_action):
            return None
        retained_completion = payload.get("unbound_completion_turn_id")
        if retained_completion is not None and retained_completion != turn_id:
            return None
        timestamp = utc_now()
        cursor = self.store.execute(
            """UPDATE actions SET native_turn_id = ?, updated_at = ?
               WHERE id = ? AND state = 'active' AND native_turn_id IS NULL""",
            (turn_id, timestamp, action["id"]),
        )
        if cursor.rowcount != 1:
            return None
        task = self.store.row(
            "SELECT native_thread_id FROM tasks WHERE id = ?", (task_id,)
        )
        assert task is not None
        self.store.bind_action_turn(
            int(action["id"]), task["native_thread_id"], turn_id
        )
        event_kind = {
            "weaver": "weaver_turn_adopted",
            "specialist": "human_sage_turn_adopted",
        }.get(kind, f"{kind}_turn_adopted")
        self.store.event(
            event_kind,
            f"adopted native turn {turn_id}",
            entity_type="action",
            entity_id=action["id"],
            detail={"native_turn_id": turn_id},
        )
        return self.store.row("SELECT * FROM actions WHERE id = ?", (action["id"],))

    def _retain_unbound_weaver_completion(self, task_id: int, turn_id: str) -> bool:
        """Pin an unmaterialized completion event for later confirmation."""

        action = self.store.row(
            """SELECT * FROM actions WHERE task_id = ?
               AND kind IN ('weaver','specialist')
               AND state = 'active' AND native_turn_id IS NULL""",
            (task_id,),
        )
        if action is None:
            return False
        try:
            payload = json.loads(action["payload"])
        except (TypeError, json.JSONDecodeError):
            payload = {}
        allowed_by_registration = payload.get("adopt_current_turn") is True
        legacy_finished_action = bool(
            "adopt_current_turn" not in payload and action["outcome_kind"] is not None
        )
        if not (allowed_by_registration or legacy_finished_action):
            return False
        retained_completion = payload.get("unbound_completion_turn_id")
        if retained_completion is not None:
            return retained_completion == turn_id
        payload["unbound_completion_turn_id"] = turn_id
        timestamp = utc_now()
        cursor = self.store.execute(
            """UPDATE actions SET payload = ?, updated_at = ?
               WHERE id = ? AND state = 'active' AND native_turn_id IS NULL""",
            (json.dumps(payload, sort_keys=True), timestamp, action["id"]),
        )
        if cursor.rowcount != 1:
            return False
        event_kind = (
            "weaver_completion_retained"
            if action["kind"] == "weaver"
            else "human_sage_completion_retained"
        )
        self.store.event(
            event_kind,
            f"retained unmaterialized native turn {turn_id}",
            entity_type="action",
            entity_id=action["id"],
            detail={"native_turn_id": turn_id},
        )
        return True

    async def _refresh_task(self, task: dict[str, Any]) -> dict[str, Any]:
        thread = await self.runtime.read_thread(task["native_thread_id"])
        thread = await self._ensure_task_project(task, thread)
        if thread.get("name") != task["title"]:
            await self.runtime.set_name(task["native_thread_id"], task["title"])
        facts = thread_facts(thread)
        safely_idle = bool(
            facts["runtime_status"] == "idle"
            and facts["last_turn_terminal"]
            and facts["helpers_terminal"]
        )
        resource_parked = bool(task.get("resource_reclaimed_at"))
        logical_archived = bool(
            task["archived"] or (facts["archived"] and not resource_parked)
        )
        timestamp = utc_now()
        self.store.execute(
            """UPDATE tasks SET runtime_status = ?, last_turn_terminal = ?,
               helpers_terminal = ?, archived = ?,
               archive_eligible_at = CASE WHEN ? = 1
                   THEN archive_eligible_at ELSE NULL END,
               archive_idle_turn_id = CASE WHEN ? = 1
                   THEN archive_idle_turn_id ELSE NULL END,
               resource_idle_since = CASE WHEN ? = 1 AND ? = 0
                   THEN COALESCE(resource_idle_since, ?) ELSE NULL END,
               updated_at = ? WHERE id = ?""",
            (
                facts["runtime_status"],
                int(facts["last_turn_terminal"]),
                int(facts["helpers_terminal"]),
                int(logical_archived),
                int(safely_idle),
                int(safely_idle),
                int(safely_idle),
                int(resource_parked),
                timestamp,
                timestamp,
                task["id"],
            ),
        )
        if (
            facts["can_start"]
            and not task["archived"]
            and self.store.row(
                """SELECT 1 FROM actions WHERE task_id = ?
                   AND state IN ('pending','starting','active','terminal','uncertain')""",
                (task["id"],),
            )
            is None
        ):
            self.store.execute(
                """UPDATE tasks SET state = 'idle',
                   resource_idle_since = COALESCE(resource_idle_since, ?),
                   updated_at = ? WHERE id = ?""",
                (timestamp, timestamp, task["id"]),
            )
        return facts

    async def _observe_task(self, task: dict[str, Any]) -> dict[str, Any]:
        """Observe runtime truth without repairing names, projects, or workflow state."""

        thread = await self.runtime.read_thread(task["native_thread_id"])
        facts = thread_facts(thread)
        self.store.execute(
            """UPDATE tasks SET runtime_status = ?, last_turn_terminal = ?,
               helpers_terminal = ?, archived = ?, updated_at = ? WHERE id = ?""",
            (
                facts["runtime_status"],
                int(facts["last_turn_terminal"]),
                int(facts["helpers_terminal"]),
                int(facts["archived"]),
                utc_now(),
                task["id"],
            ),
        )
        facts["thread_name"] = thread.get("name")
        facts["project_id"] = thread.get("projectId")
        return facts

    async def _ensure_task_project(
        self, task: dict[str, Any], thread: dict[str, Any]
    ) -> dict[str, Any]:
        if task.get("project_id"):
            project = self.store.row(
                "SELECT codex_project_id FROM projects WHERE project_id = ?",
                (task["project_id"],),
            )
            expected_project_id = project["codex_project_id"] if project else None
            if expected_project_id and thread.get("projectId") != expected_project_id:
                result = await self.runtime.assign_thread_project(
                    task["native_thread_id"], expected_project_id
                )
                assigned = result["thread"]
                if not isinstance(assigned.get("turns"), list) or (
                    not assigned["turns"] and thread.get("turns")
                ):
                    assigned["turns"] = thread.get("turns", [])
                thread = assigned
                self.store.event(
                    "task_project_repaired",
                    f"attached {task['title']} to its Codex project",
                    entity_type="task",
                    entity_id=task["id"],
                    detail={"codex_project_id": expected_project_id},
                )
        return thread

    async def _repair_task_project_bindings(self) -> None:
        for task in self.store.rows(
            """SELECT t.* FROM tasks t JOIN projects p ON p.project_id = t.project_id
               WHERE t.archived = 0 AND p.enabled = 1
                 AND t.state NOT IN ('retired','archived')
                 AND p.codex_project_id IS NOT NULL ORDER BY t.id"""
        ):
            thread = await self._read_thread_for_project_repair(task)
            if thread is None:
                continue
            await self._ensure_task_project(task, thread)

    async def _read_thread_for_project_repair(
        self, task: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Load a startup task or retire a provably inert missing identity."""

        thread_id = str(task["native_thread_id"])
        try:
            thread = await self.runtime.read_thread(thread_id, include_turns=False)
        except AppServerError as error:
            if "thread not loaded" not in str(error).lower():
                if self._retire_missing_terminal_task(task, error):
                    return None
                raise
            if not self._terminal_task_has_no_open_action(task):
                raise
            try:
                await self.runtime.resume_thread(thread_id)
                thread = await self.runtime.read_thread(thread_id, include_turns=False)
            except AppServerError as resume_error:
                if self._retire_missing_terminal_task(task, resume_error):
                    return None
                raise
        status = thread.get("status")
        status_name = status.get("type") if isinstance(status, dict) else status
        if status_name != "notLoaded" or not self._terminal_task_has_no_open_action(
            task
        ):
            return thread
        try:
            await self.runtime.resume_thread(thread_id)
            return await self.runtime.read_thread(thread_id, include_turns=False)
        except AppServerError as error:
            if self._retire_missing_terminal_task(task, error):
                return None
            raise

    def _terminal_task_has_no_open_action(self, task: dict[str, Any]) -> bool:
        return bool(
            task["role"] in COMPLETION_ARCHIVE_ROLES
            and not task["archived"]
            and task["last_turn_terminal"]
            and task["helpers_terminal"]
            and self.store.row(
                """SELECT 1 FROM actions WHERE task_id = ?
                   AND state IN ('pending','starting','active','terminal','uncertain')
                   LIMIT 1""",
                (task["id"],),
            )
            is None
        )

    def _retire_missing_terminal_task(
        self, task: dict[str, Any], error: AppServerError
    ) -> bool:
        condition = str(error)
        normalized = condition.lower()
        if not (
            self._terminal_task_has_no_open_action(task)
            and (
                "thread not loaded" in normalized
                or "no rollout found" in normalized
                or ("rollout" in normalized and "not found" in normalized)
            )
        ):
            return False
        timestamp = utc_now()
        assignment_column = (
            f"{task['role']}_task_id"
            if task["role"] in {"executor", "overseer"}
            else None
        )
        with self.store.transaction() as connection:
            detached_assignments = (
                [
                    int(row["id"])
                    for row in connection.execute(
                        f"""SELECT id FROM assignments WHERE {assignment_column} = ?
                            AND stage NOT IN ('completed','canceled') ORDER BY id""",
                        (task["id"],),
                    ).fetchall()
                ]
                if assignment_column is not None
                else []
            )
            detached_runs = (
                [
                    int(row["id"])
                    for row in connection.execute(
                        f"""SELECT id FROM runs WHERE {assignment_column} = ?
                            AND state NOT IN ('completed','canceled') ORDER BY id""",
                        (task["id"],),
                    ).fetchall()
                ]
                if assignment_column is not None
                else []
            )
            if assignment_column is not None:
                connection.execute(
                    f"""UPDATE assignments SET {assignment_column} = NULL,
                        updated_at = ? WHERE {assignment_column} = ?
                        AND stage NOT IN ('completed','canceled')""",
                    (timestamp, task["id"]),
                )
                connection.execute(
                    f"""UPDATE runs SET {assignment_column} = NULL, updated_at = ?
                        WHERE {assignment_column} = ?
                        AND state NOT IN ('completed','canceled')""",
                    (timestamp, task["id"]),
                )
            connection.execute(
                """UPDATE tasks SET state = 'retired', runtime_status = 'notFound',
                   pair_id = NULL, archive_eligible_at = NULL,
                   archive_idle_turn_id = NULL,
                   updated_at = ? WHERE id = ?""",
                (timestamp, task["id"]),
            )
            connection.execute(
                """UPDATE obligations SET state = 'canceled',
                   detail = 'native conversation rollout is unavailable', updated_at = ?
                   WHERE kind = 'archive' AND target = ?
                     AND state IN ('pending','failed')""",
                (timestamp, task["native_thread_id"]),
            )
            self.store.event(
                "stale_terminal_task_retired",
                f"retired unavailable terminal {task['role']} conversation",
                entity_type="task",
                entity_id=task["id"],
                detail={
                    "native_thread_id": task["native_thread_id"],
                    "condition": condition,
                    "prior_state": task["state"],
                    "prior_runtime_status": task["runtime_status"],
                    "last_turn_terminal": bool(task["last_turn_terminal"]),
                    "helpers_terminal": bool(task["helpers_terminal"]),
                    "recovery": "retired",
                    "detached_assignment_ids": detached_assignments,
                    "detached_run_ids": detached_runs,
                },
                now=timestamp,
            )
        return True

    async def _fallback_loop(self) -> None:
        while True:
            await asyncio.sleep(FALLBACK_RECONCILIATION_SECONDS)
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
        if self._operative_fenced():
            await self._reconcile_operative_mode()
            self.store.execute(
                """INSERT INTO meta(key, value) VALUES ('last_reconciliation', ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (utc_now(),),
            )
            self.store.event(
                "reconciliation_completed",
                "operative-only reconciliation pass completed",
                detail={
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "operative_takeover": True,
                },
            )
            return
        self._reconcile_assignment_completions()
        self._repair_action_usage_bindings()
        if not self.runtime.ready:
            self.store.event(
                "reconciliation_skipped",
                "runtime is unavailable",
                detail={"duration_ms": 0},
            )
            return
        self._adopt_stranded_operations()
        await self._reconcile_uncertain_operations()
        await self._reconcile_delivery_boundary_holds()
        self._retry_recovering_assignments()
        await self._retry_pending_actions()
        task_ids = self.store.rows("""SELECT DISTINCT task_id FROM actions
               WHERE state IN ('starting','active','terminal','uncertain')
               OR (state = 'failed' AND outcome_kind IS NOT NULL
                   AND condition LIKE 'runtime turn %')""")
        for item in task_ids:
            task = self.store.row(
                "SELECT * FROM tasks WHERE id = ?", (item["task_id"],)
            )
            if task is None:
                continue
            try:
                facts = await self._refresh_task(task)
                action = self.store.row(
                    """SELECT * FROM actions WHERE task_id = ?
                       AND (state IN ('active','terminal') OR
                            (state = 'failed' AND outcome_kind IS NOT NULL
                             AND condition LIKE 'runtime turn %'))
                       ORDER BY id DESC LIMIT 1""",
                    (task["id"],),
                )
                if action is not None and action["native_turn_id"] is None:
                    action = (
                        self._adopt_unbound_weaver_turn(int(task["id"]), facts)
                        or action
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
                        self._finalize_action_usage(int(action["id"]))
                        result = observe_action_terminal(
                            self.store,
                            int(action["id"]),
                            runtime_state=str(facts["last_turn_status"] or "completed"),
                        )
                        self._finalize_completed_workflows_for_archon(int(action["id"]))
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
        self._normalize_unowned_tasks()
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

    async def _reconcile_operative_mode(self) -> None:
        """Refresh observations without applying ordinary workflow authority."""

        self.starts_enabled = False
        self.store.execute(
            """INSERT INTO meta(key, value) VALUES ('dispatch_enabled', '0')
               ON CONFLICT(key) DO UPDATE SET value = '0'"""
        )
        journal = self.operative_journal
        if journal is None:
            return
        self.store.mirror_operative_journal(journal)
        if journal.get("state") == "closing" and self._close_completed_in_store(
            journal
        ):
            closed_at = utc_now()
            closed = transition_journal(
                self.paths.operative_journal,
                journal,
                "closed",
                now=closed_at,
                next_step="ordinary controller readiness determines dispatch",
                completed_effect="operative_archived_and_fence_released",
                closed_at=closed_at,
            )
            self.operative_journal = closed
            self.store.mirror_operative_journal(closed)
            self.store.execute(
                """INSERT INTO meta(key, value) VALUES ('controller_state', 'ready')
                   ON CONFLICT(key) DO UPDATE SET value = 'ready'"""
            )
            self._update_readiness()
            return
        if not self.runtime.ready:
            return
        for task in self.store.rows(
            "SELECT * FROM tasks WHERE archived = 0 ORDER BY id LIMIT 200"
        ):
            try:
                facts = await self._observe_task(task)
            except Exception as error:
                self.store.record_operative_evidence(
                    str(journal["takeover_id"]),
                    f"runtime-unavailable:{task['id']}",
                    "runtime_observation",
                    coverage="unavailable",
                    target_type="task",
                    target_id=task["id"],
                    detail={"error": str(error)},
                )
                continue
            action = self.store.row(
                """SELECT * FROM actions WHERE task_id = ?
                   AND state IN ('pending','starting','active','terminal','uncertain')
                   ORDER BY id DESC LIMIT 1""",
                (task["id"],),
            )
            if task["role"] == "operative":
                if action is not None and action["native_turn_id"] is None:
                    self._adopt_unbound_operative_turn(int(task["id"]), facts)
                if journal.get("state") == "closing":
                    await self._maybe_finalize_operative_closeout(task, facts)
                continue
            if (
                action is not None
                and facts["last_turn_terminal"]
                and action.get("native_turn_id") == facts.get("last_turn_id")
            ):
                self._finalize_action_usage(int(action["id"]))
                self.store.record_operative_evidence(
                    str(journal["takeover_id"]),
                    f"late-terminal:{action['id']}:{facts.get('last_turn_status')}",
                    "late_runtime_outcome",
                    coverage="observed",
                    target_type="action",
                    target_id=action["id"],
                    detail={
                        "native_turn_id": facts.get("last_turn_id"),
                        "runtime_state": facts.get("last_turn_status"),
                        "quarantined": True,
                    },
                )

    def _close_completed_in_store(self, journal: dict[str, Any]) -> bool:
        action = self.store.row(
            "SELECT state FROM actions WHERE id = ?", (journal.get("action_id"),)
        )
        task = self.store.row(
            "SELECT state, archived FROM tasks WHERE id = ?", (journal.get("task_id"),)
        )
        operation = self.store.row(
            """SELECT state FROM operative_operations
               WHERE correlation_id = ?""",
            (f"operative-close-archive:{journal['takeover_id']}",),
        )
        return bool(
            "closeout_revalidated" in journal.get("completed_effects", [])
            and action is not None
            and action["state"] == "processed"
            and task is not None
            and task["state"] == "archived"
            and bool(task["archived"])
            and operation is not None
            and operation["state"] == "complete"
        )

    def _repair_action_usage_bindings(self) -> None:
        """Restore exact active action/turn usage links after interruption."""

        actions = self.store.rows("""SELECT a.id, a.native_turn_id, t.native_thread_id
               FROM actions a JOIN tasks t ON t.id = a.task_id
               WHERE a.native_turn_id IS NOT NULL
                 AND (a.state IN ('starting','active','terminal','uncertain') OR
                      (a.state = 'failed' AND a.outcome_kind IS NOT NULL
                       AND a.condition LIKE 'runtime turn %'))
                 AND NOT EXISTS (
                   SELECT 1 FROM action_turn_usage usage
                   WHERE usage.native_thread_id = t.native_thread_id
                     AND usage.native_turn_id = a.native_turn_id
                     AND usage.action_id = a.id
                 )""")
        for action in actions:
            self.store.bind_action_turn(
                int(action["id"]),
                str(action["native_thread_id"]),
                str(action["native_turn_id"]),
            )

    def _normalize_unowned_tasks(self) -> None:
        """Return terminal tasks without a current action to their idle state."""

        self.store.execute(
            """UPDATE tasks SET state = 'idle',
               resource_idle_since = COALESCE(resource_idle_since, ?), updated_at = ?
               WHERE archived = 0 AND state IN ('provisioning','active','uncertain')
               AND last_turn_terminal = 1 AND helpers_terminal = 1
               AND NOT EXISTS (
                   SELECT 1 FROM actions
                   WHERE actions.task_id = tasks.id
                   AND actions.state IN ('pending','starting','active','terminal','uncertain')
               )""",
            (utc_now(), utc_now()),
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
            if (
                facts["runtime_status"] == "active"
                or not facts["last_turn_terminal"]
                or not facts["helpers_terminal"]
            ):
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
        for operation in self.store.rows("""SELECT operation.*
               FROM external_operations operation
               WHERE (operation.state = 'uncertain'
                      AND operation.reconciliation_used = 0)
                  OR (operation.kind = 'tollgate_approve'
                      AND operation.state = 'complete'
                      AND EXISTS (
                          SELECT 1 FROM assignments assignment
                          WHERE assignment.candidate_id = operation.target
                            AND assignment.stage IN ('delivering','recovering')
                      ))
                  OR (operation.kind = 'tollgate_approve'
                      AND operation.state = 'failed'
                      AND operation.condition IN (
                          'Tollgate candidate ended in promoted',
                          'candidate was promoted but source synchronization is incomplete',
                          'candidate worktree cleanup is incomplete',
                          'candidate has no retained Tollgate certificate'
                      )
                      AND EXISTS (
                          SELECT 1 FROM assignments assignment
                          WHERE assignment.candidate_id = operation.target
                            AND assignment.stage NOT IN ('completed','canceled')
                      ))
               ORDER BY operation.id"""):
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
            elif operation["kind"] == "thread_park":
                await self._reconcile_thread_park(operation)
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
            self.store.bind_action_turn(
                int(action["id"]), str(inputs["thread_id"]), str(turn_id)
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

        def candidates_in(
            items: list[dict[str, Any]], *, model: str, created_at: datetime
        ) -> list[dict[str, Any]]:
            return [
                thread
                for thread in items
                if thread.get("model") == model
                and isinstance(thread.get("createdAt"), int)
                and abs(
                    datetime.fromtimestamp(thread["createdAt"], timezone.utc)
                    - created_at
                )
                <= timedelta(minutes=2)
                and not thread.get("turns")
            ]

        candidates = candidates_in(
            threads, model=str(inputs["model"]), created_at=created
        )
        if not candidates and inputs.get("project_id"):
            candidates = candidates_in(
                await self.runtime.list_threads(cwd=inputs["cwd"], project_id=None),
                model=str(inputs["model"]),
                created_at=created,
            )
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
        await self._observe_delivery_operation(
            assignment,
            operation,
            reconciled=True,
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
        revision = inputs.get("revision")
        source_oid = (
            revision
            if isinstance(revision, str) and revision != "HEAD"
            else _worktree_head(inputs.get("worktree_path"))
        )
        candidate = _find_candidate(
            observed,
            inputs.get("worktree_path"),
            exclude_id=inputs.get("predecessor_candidate_id"),
            source_oid=source_oid,
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
            try:
                await self.runtime.unsubscribe(str(operation["target"]))
            except AppServerError as error:
                timestamp = utc_now()
                condition = (
                    "native archive is confirmed but thread unsubscribe remains "
                    f"unconfirmed: {error}"
                )
                self.store.execute(
                    """UPDATE external_operations SET state = 'uncertain',
                       reconciliation_used = 0, condition = ?, updated_at = ?
                       WHERE id = ?""",
                    (condition, timestamp, operation["id"]),
                )
                if obligation:
                    self.store.execute(
                        """UPDATE obligations SET state = 'failed', detail = ?,
                           next_attempt_at = NULL, updated_at = ? WHERE id = ?""",
                        (condition, timestamp, obligation["id"]),
                    )
                self.store.event(
                    "archive_unsubscribe_deferred",
                    condition,
                    entity_type="operation",
                    entity_id=operation["id"],
                    detail={"native_thread_id": operation["target"]},
                )
                return
            timestamp = utc_now()
            self.store.execute(
                """UPDATE external_operations SET state = 'complete',
                   reconciliation_used = 1, condition = NULL, completed_at = ?,
                   updated_at = ? WHERE id = ?""",
                (timestamp, timestamp, operation["id"]),
            )
            if task:
                self.store.execute(
                    "UPDATE tasks SET state = 'archived', archived = 1, archive_eligible_at = NULL, archive_idle_turn_id = NULL, updated_at = ? WHERE id = ?",
                    (timestamp, task["id"]),
                )
            if obligation:
                self.store.execute(
                    "UPDATE obligations SET state = 'complete', updated_at = ? WHERE id = ?",
                    (timestamp, obligation["id"]),
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

    async def _reconcile_thread_park(self, operation: dict[str, Any]) -> None:
        inputs = json.loads(operation["input_json"])
        task = self.store.row(
            "SELECT * FROM tasks WHERE id = ?", (int(operation["target"]),)
        )
        thread_id = inputs.get("thread_id")
        if task is None or not isinstance(thread_id, str):
            self._retain_uncertain_condition(
                operation, "parked thread cannot be reconciled to its managed task"
            )
            return
        thread = await self.runtime.read_thread(thread_id)
        facts = thread_facts(thread)
        timestamp = utc_now()
        if facts["archived"]:
            await self.runtime.unsubscribe(thread_id)
            self.store.execute(
                """UPDATE tasks SET resource_reclaimed_at = COALESCE(resource_reclaimed_at, ?),
                   resource_idle_since = NULL, runtime_status = 'notLoaded',
                   updated_at = ? WHERE id = ?""",
                (timestamp, timestamp, task["id"]),
            )
            self.store.execute(
                """UPDATE external_operations SET state = 'complete', native_id = ?,
                   result_json = ?, reconciliation_used = 1, condition = NULL,
                   completed_at = ?, updated_at = ? WHERE id = ?""",
                (
                    thread_id,
                    json.dumps({"parked": True}, sort_keys=True),
                    timestamp,
                    timestamp,
                    operation["id"],
                ),
            )
            return
        if (
            facts["runtime_status"] == "active"
            or not facts["last_turn_terminal"]
            or not facts["helpers_terminal"]
        ):
            self.store.execute(
                """UPDATE tasks SET resource_reclaimed_at = NULL,
                   archived = 0, state = 'active', runtime_status = ?,
                   last_turn_terminal = ?, helpers_terminal = ?,
                   updated_at = ? WHERE id = ?""",
                (
                    facts["runtime_status"],
                    int(
                        facts["last_turn_terminal"]
                        and facts["runtime_status"] != "active"
                    ),
                    int(facts["helpers_terminal"]),
                    timestamp,
                    task["id"],
                ),
            )
            self.store.execute(
                """UPDATE external_operations SET state = 'failed',
                   reconciliation_used = 1,
                   condition = 'active turn retained without interruption',
                   updated_at = ? WHERE id = ?""",
                (timestamp, operation["id"]),
            )
            return
        self.store.execute(
            """UPDATE external_operations SET state = 'failed',
               reconciliation_used = 1,
               condition = 'targeted read confirmed idle thread was not parked',
               updated_at = ? WHERE id = ?""",
            (timestamp, operation["id"]),
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
        turns = thread.get("turns") if isinstance(thread.get("turns"), list) else []
        correlation = f"fulcrum-operation-{operation['id']}"
        matches = [
            turn
            for turn in turns
            if isinstance(turn, dict)
            and any(
                isinstance(item, dict)
                and item.get("type") == "userMessage"
                and item.get("clientId") == correlation
                for item in (
                    turn.get("items", []) if isinstance(turn.get("items"), list) else []
                )
            )
        ]
        if not matches and facts["archived"]:
            await self.runtime.unsubscribe(str(thread_id))
            timestamp = utc_now()
            self.store.execute(
                """UPDATE external_operations SET state = 'failed',
                   reconciliation_used = 1,
                   condition = 'archived partial smoke thread had no correlated turn',
                   completed_at = ?, updated_at = ? WHERE id = ?""",
                (timestamp, timestamp, operation["id"]),
            )
            return
        expected_turn_id = inputs.get("smoke_turn_id")
        if (
            len(matches) != 1
            or expected_turn_id is not None
            and matches[0].get("id") != expected_turn_id
            or matches[0].get("id") != facts["last_turn_id"]
        ):
            self._retain_uncertain_condition(
                operation,
                "runtime smoke thread does not contain exactly one matching latest correlated turn",
            )
            return
        if not facts["last_turn_terminal"]:
            return
        if (
            facts["last_turn_status"] != "completed"
            or not facts["helpers_terminal"]
            or facts["runtime_status"] != "idle"
        ):
            if facts["helpers_terminal"] and facts["runtime_status"] == "idle":
                await self.runtime.archive(str(thread_id))
                timestamp = utc_now()
                self.store.execute(
                    """UPDATE external_operations SET state = 'failed', result_json = ?,
                       reconciliation_used = 1,
                       condition = 'correlated runtime smoke turn did not complete successfully',
                       completed_at = ?, updated_at = ? WHERE id = ?""",
                    (json.dumps(thread), timestamp, timestamp, operation["id"]),
                )
            return
        if not facts["archived"]:
            await self.runtime.archive(str(thread_id))
        else:
            await self.runtime.unsubscribe(str(thread_id))
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
        timestamp = utc_now()
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
        elif operation["kind"] == "tollgate_approve":
            assignment = self.store.row(
                "SELECT id FROM assignments WHERE candidate_id = ? AND stage NOT IN ('completed','canceled')",
                (operation["target"],),
            )
            if assignment is not None:
                target_id = int(assignment["id"])
                target_type = "assignment"
        elif operation["kind"] == "beads_close":
            assignment = self.store.row(
                "SELECT id FROM assignments WHERE bead_id = ? AND stage NOT IN ('completed','canceled')",
                (operation["target"],),
            )
            if assignment is not None:
                target_id = int(assignment["id"])
                target_type = "assignment"
        elif operation["kind"] == "thread_start":
            inputs = json.loads(operation["input_json"])
            pair_id = inputs.get("pair_id")
            if isinstance(pair_id, int):
                assignment = self.store.row(
                    "SELECT id FROM assignments WHERE run_id = ? AND stage NOT IN ('completed','canceled') ORDER BY id LIMIT 1",
                    (pair_id,),
                )
                if assignment is not None:
                    target_id = int(assignment["id"])
                    target_type = "assignment"
        elif operation["kind"] == "beads_create":
            obligation = self.store.row(
                "SELECT id FROM obligations WHERE kind = 'beads_publication' AND identity = ? AND state NOT IN ('complete','canceled') ORDER BY id LIMIT 1",
                (operation["target"],),
            )
            if obligation is not None:
                target_id = int(obligation["id"])
                target_type = "obligation"
        with self.store.transaction() as connection:
            retained = connection.execute(
                "SELECT operator_hold_id FROM external_operations WHERE id = ?",
                (operation["id"],),
            ).fetchone()
            hold_id = retained["operator_hold_id"] if retained else None
            if hold_id is None:
                cursor = connection.execute(
                    """INSERT INTO holds(scope, target, reason, urgent, release_condition, created_at)
                       VALUES ('operation', ?, ?, 1, 'operator resolves ambiguous external effect', ?)""",
                    (str(operation["id"]), condition, timestamp),
                )
                hold_id = int(cursor.lastrowid)
            connection.execute(
                """UPDATE external_operations SET reconciliation_used = 1,
                   operator_hold_id = ?, condition = ?, updated_at = ? WHERE id = ?""",
                (hold_id, condition, timestamp, operation["id"]),
            )
            if target_type == "action":
                connection.execute(
                    "UPDATE actions SET operator_hold_id = ?, condition = ?, updated_at = ? WHERE id = ?",
                    (hold_id, condition, timestamp, target_id),
                )
            elif target_type == "assignment":
                connection.execute(
                    """UPDATE assignments SET prior_stage = CASE WHEN stage = 'recovering' THEN prior_stage ELSE stage END,
                       stage = 'recovering', operator_hold_id = ?, next_attempt_at = NULL,
                       condition = ?, updated_at = ? WHERE id = ?""",
                    (hold_id, condition, timestamp, target_id),
                )
            elif target_type == "obligation":
                connection.execute(
                    """UPDATE obligations SET state = 'uncertain', operator_hold_id = ?,
                       next_attempt_at = NULL, detail = ?, updated_at = ? WHERE id = ?""",
                    (hold_id, condition, timestamp, target_id),
                )
        self.store.event(
            "operator_attention",
            condition,
            entity_type="operation",
            entity_id=operation["id"],
        )
        self._queue_operation_resolution(
            operation,
            hold_id=int(hold_id),
            condition=condition,
            target_type=target_type,
            target_id=target_id,
        )

    def _queue_operation_resolution(
        self,
        operation: dict[str, Any],
        *,
        hold_id: int,
        condition: str,
        target_type: str | None = None,
        target_id: int | None = None,
    ) -> None:
        affected_archon = False
        if target_type == "action" and target_id is not None:
            owner = self.store.row(
                """SELECT tasks.role FROM actions JOIN tasks ON tasks.id = actions.task_id
                   WHERE actions.id = ?""",
                (target_id,),
            )
            affected_archon = owner is not None and owner["role"] == "archon"
        if not affected_archon:
            self._queue_archon_update(
                f"operation-resolution:{operation['id']}",
                {
                    "kind": "operation_resolution",
                    "operation_id": operation["id"],
                    "operation_kind": operation["kind"],
                    "target": operation["target"],
                    "condition": condition,
                    "hold_id": hold_id,
                    "target_type": target_type,
                    "target_id": target_id,
                    "required_decision": "resolve_operation",
                    "resolutions": [
                        "observed_success",
                        "observed_failure",
                        "confirmed_unsent",
                    ],
                },
            )

    async def advance(self) -> None:
        if self._operative_fenced():
            self.starts_enabled = False
            return
        await self._run_advancement_step(
            "occurrence-publication", self._publish_occurrences
        )
        if self.runtime.ready:
            if self.resource_probe is not None:
                await self._run_advancement_step(
                    "resource-lifecycle", self._maintain_resource_lifecycle
                )
            await self._run_advancement_step(
                "archon-succession", self._process_archon_succession
            )
            await self._run_advancement_step("archival", self._archive_ready_tasks)
            await self._run_advancement_step(
                "archon-succession", self._process_archon_succession
            )
        self._update_readiness()
        if not self.runtime.ready:
            return
        if not self.starts_enabled:
            # Exhausted mutations intentionally disable ordinary dispatch, but the
            # dedicated Archon action that can resolve them must remain reachable.
            # This path batches only operation-resolution updates and acquires no
            # assignment/capacity lease.
            await self._run_advancement_step(
                "operation-recovery-delivery",
                lambda: self._deliver_update_batch(recovery_only=True),
            )
            return
        # Admission remains one-at-a-time. Each started action commits its lease
        # and passes the resource gate before the next capacity snapshot.
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

    async def _observe_app_server_resources(self) -> ResourceSnapshot | None:
        probe = self.resource_probe
        if probe is None:
            return None
        try:
            snapshot = await asyncio.to_thread(probe.snapshot)
        except ResourceProbeError as error:
            condition = (
                "resource admission paused: app-server descriptor telemetry is "
                f"unavailable ({str(error)[:240]}); restore the supervised app-server "
                "or inspect its process ownership"
            )
            self._set_resource_admission_condition(condition)
            return None
        prior = self.last_resource_snapshot
        self.last_resource_snapshot = snapshot
        detail = snapshot.detail()
        self.store.execute(
            """INSERT INTO meta(key, value) VALUES ('app_server_resources', ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (json.dumps(detail, sort_keys=True),),
        )
        if prior is None or (
            prior.process_id,
            prior.soft_limit,
            prior.descriptor_count,
            prior.child_count,
        ) != (
            snapshot.process_id,
            snapshot.soft_limit,
            snapshot.descriptor_count,
            snapshot.child_count,
        ):
            self.store.event(
                "app_server_resources_observed",
                "observed app-server descriptor and direct-child counts",
                entity_type="capability",
                entity_id="app_server",
                detail=detail,
            )
        return snapshot

    def _set_resource_admission_condition(self, condition: str | None) -> None:
        condition = condition[:500] if condition else None
        if condition == self.resource_admission_condition:
            return
        prior = self.resource_admission_condition
        self.resource_admission_condition = condition
        self.store.execute(
            """INSERT INTO meta(key, value) VALUES ('resource_admission_condition', ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (condition or "",),
        )
        self.store.event(
            "resource_admission_paused" if condition else "resource_admission_resumed",
            condition or "app-server descriptor reserve restored",
            entity_type="capability",
            entity_id="app_server",
            detail={"previous_condition": prior} if not condition and prior else None,
        )

    def _resident_idle_worker_candidates(self) -> list[dict[str, Any]]:
        return self.store.rows(
            """SELECT * FROM tasks WHERE state = 'idle' AND archived = 0
               AND resource_reclaimed_at IS NULL
               AND role IN ('executor','overseer','sage','inquisitor')
               AND NOT EXISTS (
                 SELECT 1 FROM actions WHERE actions.task_id = tasks.id
                   AND actions.state IN ('pending','starting','active','terminal','uncertain')
               )
               ORDER BY COALESCE(resource_idle_since, updated_at), id"""
        )

    async def _admit_runtime_start(self, kind: str, target: str) -> None:
        resident_idle_count = len(self._resident_idle_worker_candidates())
        if resident_idle_count > MAX_IDLE_WORKER_CONVERSATIONS:
            condition = (
                f"resource admission paused before {kind} for {target}: "
                f"{resident_idle_count} eligible idle worker conversations remain "
                f"resident above the supported maximum of "
                f"{MAX_IDLE_WORKER_CONVERSATIONS}; wait for lifecycle reclamation "
                "or inspect resource_reclamation_failed events and restore native "
                "archive/unsubscribe"
            )[:500]
            self._set_resource_admission_condition(condition)
            raise ResourceAdmissionPaused(condition)
        if self.resource_probe is None:
            return
        snapshot = await self._observe_app_server_resources()
        if snapshot is None:
            raise ResourceAdmissionPaused(
                self.resource_admission_condition
                or "resource admission paused because telemetry is unavailable"
            )
        active_row = self.store.row("""SELECT COUNT(*) AS count FROM tasks
               WHERE (state = 'active' OR resource_orphan_active = 1)
                 AND archived = 0""")
        assert active_row is not None
        active_count = int(active_row["count"])
        if active_count >= MAX_ACTIVE_CONVERSATIONS:
            condition = (
                f"resource admission paused before {kind} for {target}: "
                f"{active_count} active conversations already use the supported "
                f"maximum of {MAX_ACTIVE_CONVERSATIONS}; wait for an active turn "
                "to finish"
            )
            self._set_resource_admission_condition(condition)
            raise ResourceAdmissionPaused(condition)
        if snapshot.admits_start():
            self._set_resource_admission_condition(None)
            return
        condition = bounded_condition(
            f"resource admission paused before {kind} for {target}", snapshot.detail()
        )
        self._set_resource_admission_condition(condition)
        raise ResourceAdmissionPaused(condition)

    async def _maintain_resource_lifecycle(self) -> None:
        """Park idle workers and reconcile parked/retired threads after restart."""

        now = utc_now()
        snapshot = await self._observe_app_server_resources()
        pressured = snapshot is not None and not snapshot.admits_start()
        if pressured and snapshot is not None:
            self._set_resource_admission_condition(
                bounded_condition(
                    "resource admission paused while reclaiming idle workers",
                    snapshot.detail(),
                )
            )

        for task in self.store.rows(
            """SELECT * FROM tasks WHERE resource_reclaimed_at IS NOT NULL
               OR resource_orphan_active = 1
               OR state IN ('retired','archived') ORDER BY id"""
        ):
            try:
                thread = await self.runtime.read_thread(task["native_thread_id"])
                facts = thread_facts(thread)
            except AppServerError as error:
                self.store.event(
                    "resource_reconciliation_deferred",
                    str(error)[:500],
                    entity_type="task",
                    entity_id=task["id"],
                )
                continue
            if (
                facts["runtime_status"] == "active"
                or not facts["last_turn_terminal"]
                or not facts["helpers_terminal"]
            ):
                if task["resource_reclaimed_at"]:
                    self.store.execute(
                        """UPDATE tasks SET resource_reclaimed_at = NULL,
                           resource_orphan_active = 0, state = 'active', archived = 0,
                           runtime_status = ?,
                           last_turn_terminal = ?, helpers_terminal = ?,
                           updated_at = ? WHERE id = ?""",
                        (
                            facts["runtime_status"],
                            int(
                                facts["last_turn_terminal"]
                                and facts["runtime_status"] != "active"
                            ),
                            int(facts["helpers_terminal"]),
                            now,
                            task["id"],
                        ),
                    )
                    self.store.event(
                        "resource_reconciliation_reassociated",
                        "parked thread was active after restart; retained without interruption",
                        entity_type="task",
                        entity_id=task["id"],
                    )
                elif not task["resource_orphan_active"]:
                    self.store.execute(
                        """UPDATE tasks SET resource_orphan_active = 1,
                           archived = 0, runtime_status = ?,
                           last_turn_terminal = ?, helpers_terminal = ?,
                           resource_idle_since = NULL, updated_at = ? WHERE id = ?""",
                        (
                            facts["runtime_status"],
                            int(
                                facts["last_turn_terminal"]
                                and facts["runtime_status"] != "active"
                            ),
                            int(facts["helpers_terminal"]),
                            now,
                            task["id"],
                        ),
                    )
                    self.store.event(
                        "resource_reconciliation_reassociated",
                        "retired or archived thread was active after restart; tracking until terminal cleanup",
                        entity_type="task",
                        entity_id=task["id"],
                    )
                continue
            if facts["archived"]:
                try:
                    await self.runtime.unsubscribe(str(task["native_thread_id"]))
                except Exception as error:
                    self.store.event(
                        "resource_reconciliation_deferred",
                        str(error)[:500],
                        entity_type="task",
                        entity_id=task["id"],
                    )
                    continue
                if task["resource_orphan_active"]:
                    self.store.execute(
                        """UPDATE tasks SET resource_orphan_active = 0, archived = 1,
                           runtime_status = 'notLoaded', resource_idle_since = NULL,
                           resource_reclaimed_at = NULL, updated_at = ? WHERE id = ?""",
                        (now, task["id"]),
                    )
                continue
            try:
                await self._park_task(task, now=now, reconciliation=True)
                if task["resource_orphan_active"]:
                    self.store.execute(
                        """UPDATE tasks SET resource_orphan_active = 0, archived = 1,
                           state = ?, resource_reclaimed_at = NULL,
                           updated_at = ? WHERE id = ?""",
                        (task["state"], now, task["id"]),
                    )
            except Exception as error:
                self.store.event(
                    "resource_reconciliation_deferred",
                    str(error)[:500],
                    entity_type="task",
                    entity_id=task["id"],
                )

        candidates = self._resident_idle_worker_candidates()
        excess_idle = max(0, len(candidates) - MAX_IDLE_WORKER_CONVERSATIONS)
        for task in candidates:
            try:
                facts = await self._refresh_task(task)
            except AppServerError:
                continue
            if not (
                facts["runtime_status"] == "idle"
                and facts["last_turn_terminal"]
                and facts["helpers_terminal"]
            ):
                continue
            retained = self.store.row("SELECT * FROM tasks WHERE id = ?", (task["id"],))
            assert retained is not None
            idle_since = retained["resource_idle_since"]
            due = False
            if idle_since:
                due = datetime.fromisoformat(
                    idle_since.replace("Z", "+00:00")
                ) + RESOURCE_RECLAIM_DELAY <= datetime.fromisoformat(
                    now.replace("Z", "+00:00")
                )
            if not pressured and not due and excess_idle == 0:
                continue
            try:
                await self._park_task(retained, now=now)
            except Exception as error:
                self.store.event(
                    "resource_reclamation_failed",
                    str(error)[:500],
                    entity_type="task",
                    entity_id=task["id"],
                )
                continue
            if excess_idle > 0:
                excess_idle -= 1
            if pressured:
                snapshot = await self._observe_app_server_resources()
                pressured = snapshot is None or not snapshot.admits_start()
                if not pressured and excess_idle == 0:
                    self._set_resource_admission_condition(None)
        active_row = self.store.row("""SELECT COUNT(*) AS count FROM tasks
               WHERE (state = 'active' OR resource_orphan_active = 1)
                 AND archived = 0""")
        assert active_row is not None
        if (
            snapshot is not None
            and not pressured
            and excess_idle == 0
            and int(active_row["count"]) < MAX_ACTIVE_CONVERSATIONS
        ):
            self._set_resource_admission_condition(None)

    async def _park_task(
        self, task: dict[str, Any], *, now: str, reconciliation: bool = False
    ) -> None:
        operation = self.store.create_operation(
            "thread_park", str(task["id"]), {"thread_id": task["native_thread_id"]}
        )
        attempt = self.store.begin_operation_attempt(operation)
        started = time.monotonic()
        try:
            await self.runtime.archive(task["native_thread_id"])
            self.store.execute(
                """UPDATE tasks SET resource_reclaimed_at = ?,
                   resource_idle_since = NULL, runtime_status = 'notLoaded',
                   updated_at = ? WHERE id = ?""",
                (now, now, task["id"]),
            )
            self.store.finish_operation_attempt(
                operation,
                attempt,
                state="complete",
                result={"parked": True},
                native_id=str(task["native_thread_id"]),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            self.store.event(
                "conversation_resources_reclaimed",
                "archived safely idle worker conversation to release native resources",
                entity_type="task",
                entity_id=task["id"],
                detail={
                    "thread_id": task["native_thread_id"],
                    "reconciliation": reconciliation,
                },
            )
        except Exception as error:
            self._operation_failed(
                operation, error, attempt=attempt, started=started, mutation=True
            )
            raise

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
        assignment["stage"] = "preparing"
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
            self.store.finish_operation_attempt(
                operation,
                attempt,
                state="complete",
                result=result,
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
                self._reconcile_worktree_create(retained)
            return
        assignment["worktree_path"] = path
        await self._ensure_worktree_environment(Path(path))
        await self._ensure_pair(assignment)
        assignment["stage"] = "implementing"
        self.store.execute(
            "UPDATE assignments SET stage = 'implementing', updated_at = ? WHERE id = ?",
            (utc_now(), assignment["id"]),
        )
        await self._start_assignment_action(assignment)

    async def _resume_preparing_assignment(self, assignment: dict[str, Any]) -> None:
        if not assignment["worktree_path"]:
            self.store.execute(
                "UPDATE assignments SET stage = 'queued', updated_at = ? WHERE id = ?",
                (utc_now(), assignment["id"]),
            )
            return
        await self._ensure_worktree_environment(Path(assignment["worktree_path"]))
        await self._ensure_pair(assignment)
        self.store.execute(
            "UPDATE assignments SET stage = 'implementing', condition = NULL, updated_at = ? WHERE id = ?",
            (utc_now(), assignment["id"]),
        )
        assignment["stage"] = "implementing"
        await self._start_assignment_action(assignment)

    async def _ensure_pair(
        self, assignment: dict[str, Any], *, include_overseer: bool = False
    ) -> None:
        run = self.store.row("SELECT * FROM runs WHERE id = ?", (assignment["run_id"],))
        if run is None:
            raise StoreError("assignment run disappeared")
        bead = self.store.row(
            "SELECT * FROM beads WHERE bead_id = ?", (assignment["bead_id"],)
        )
        project = self.store.row(
            "SELECT * FROM projects WHERE project_id = ?", (assignment["project_id"],)
        )
        if bead is None or project is None:
            raise StoreError("assignment sources disappeared")
        executor = (
            self.store.row(
                "SELECT * FROM tasks WHERE id = ?", (run["executor_task_id"],)
            )
            if run.get("executor_task_id")
            else None
        )
        overseer = (
            self.store.row(
                "SELECT * FROM tasks WHERE id = ?", (run["overseer_task_id"],)
            )
            if run.get("overseer_task_id")
            else None
        )
        executor = await self._lineage_task_for_assignment(
            assignment=assignment,
            run=run,
            current=executor,
            role="executor",
            description=bead["title"],
            project=project,
            model=bead["executor_model"],
            effort=bead["executor_reasoning_effort"],
        )
        if include_overseer:
            overseer = await self._lineage_task_for_assignment(
                assignment=assignment,
                run=run,
                current=overseer,
                role="overseer",
                description=bead["title"],
                project=project,
                model=bead["overseer_model"],
                effort=bead["overseer_reasoning_effort"],
            )
        self.store.execute(
            "UPDATE runs SET executor_task_id = ?, overseer_task_id = ?, state = 'active', updated_at = ? WHERE id = ?",
            (
                executor["id"],
                overseer["id"] if overseer else None,
                utc_now(),
                assignment["run_id"],
            ),
        )
        self.store.execute(
            """UPDATE assignments SET executor_task_id = ?, overseer_task_id = ?, updated_at = ?
               WHERE id = ?""",
            (
                executor["id"],
                overseer["id"] if overseer else None,
                utc_now(),
                assignment["id"],
            ),
        )
        assignment["executor_task_id"] = executor["id"]
        assignment["overseer_task_id"] = overseer["id"] if overseer else None

    async def _lineage_task_for_assignment(
        self,
        *,
        assignment: dict[str, Any],
        run: dict[str, Any],
        current: dict[str, Any] | None,
        role: str,
        description: str,
        project: dict[str, Any],
        model: str,
        effort: str,
    ) -> dict[str, Any]:
        assigned_id = assignment.get(f"{role}_task_id")
        candidates: list[dict[str, Any]] = []
        if assigned_id is not None:
            bound = self.store.row("SELECT * FROM tasks WHERE id = ?", (assigned_id,))
            if bound is not None:
                candidates.append(bound)
        if current is not None:
            candidates.append(current)
        lineage_number = run.get("lineage_number")
        if isinstance(lineage_number, int):
            candidates.extend(
                self.store.rows(
                    """SELECT * FROM tasks
                       WHERE role = ? AND lineage_number = ?
                       ORDER BY CASE WHEN lineage_suffix = '' THEN 0 ELSE 1 END,
                                length(lineage_suffix), lineage_suffix, id""",
                    (role, lineage_number),
                )
            )
        seen: set[int] = set()
        for task in candidates:
            task_id = int(task["id"])
            if task_id in seen:
                continue
            seen.add(task_id)
            compatible_action = self._lineage_task_has_compatible_action(
                task, int(assignment["id"])
            )
            if self._lineage_task_is_usable(
                task,
                assignment_id=int(assignment["id"]),
                project_id=str(project["project_id"]),
                model=model,
                effort=effort,
                compatible_action=compatible_action,
            ) and await self._lineage_task_is_available(
                task, allow_active=compatible_action
            ):
                self._claim_lineage_task(task, int(assignment["run_id"]), role)
                task["pair_id"] = int(assignment["run_id"])
                task["state"] = "idle"
                return task
        with self.store.transaction() as connection:
            column = f"{role}_task_id"
            connection.execute(
                f"UPDATE runs SET {column} = NULL, updated_at = ? WHERE id = ?",
                (utc_now(), assignment["run_id"]),
            )
            connection.execute(
                "UPDATE tasks SET pair_id = NULL WHERE pair_id = ? AND role = ?",
                (assignment["run_id"], role),
            )
        provisioned = await self._provision_task(
            role=role,
            description=description,
            project=project,
            model=model,
            effort=effort,
            pair_id=int(assignment["run_id"]),
            lineage_number=lineage_number if isinstance(lineage_number, int) else None,
        )
        self._claim_lineage_task(provisioned, int(assignment["run_id"]), role)
        provisioned["pair_id"] = int(assignment["run_id"])
        return provisioned

    def _lineage_task_is_usable(
        self,
        task: dict[str, Any],
        *,
        assignment_id: int,
        project_id: str,
        model: str,
        effort: str,
        compatible_action: bool,
    ) -> bool:
        if (
            task["state"] in {"retired", "archived", "uncertain"}
            or task["archived"]
            or task["project_id"] != project_id
            or task["model"] != model
            or task["reasoning_effort"] != effort
            or (
                not compatible_action
                and (
                    not task["last_turn_terminal"]
                    or not task["helpers_terminal"]
                    or task["runtime_status"] == "active"
                )
            )
        ):
            return False
        if compatible_action:
            return True
        if (
            self.store.row(
                """SELECT 1 FROM actions WHERE task_id = ?
               AND state IN ('pending','starting','active','terminal','uncertain')""",
                (task["id"],),
            )
            is not None
        ):
            return False
        column = f"{task['role']}_task_id"
        return (
            self.store.row(
                f"""SELECT 1 FROM assignments WHERE {column} = ? AND id != ?
                AND stage NOT IN ('completed','canceled') LIMIT 1""",
                (task["id"], assignment_id),
            )
            is None
        )

    def _lineage_task_has_compatible_action(
        self, task: dict[str, Any], assignment_id: int
    ) -> bool:
        action = self.store.row(
            """SELECT assignment_id FROM actions WHERE task_id = ?
               AND state IN ('pending','starting','active','terminal','uncertain')""",
            (task["id"],),
        )
        return action is not None and action["assignment_id"] == assignment_id

    async def _lineage_task_is_available(
        self, task: dict[str, Any], *, allow_active: bool = False
    ) -> bool:
        if task.get("resource_reclaimed_at"):
            return bool(task["last_turn_terminal"] and task["helpers_terminal"])
        try:
            thread = await self.runtime.read_thread(
                str(task["native_thread_id"]), include_turns=False
            )
        except AppServerError as error:
            condition = str(error).lower()
            if not any(
                marker in condition
                for marker in (
                    "not found",
                    "no thread",
                    "unknown thread",
                    "does not exist",
                )
            ):
                raise
            timestamp = utc_now()
            self.store.execute(
                """UPDATE tasks SET state = 'retired', archive_eligible_at = NULL,
                   archive_idle_turn_id = NULL, updated_at = ? WHERE id = ?""",
                (timestamp, task["id"]),
            )
            self.store.event(
                "lineage_conversation_unavailable",
                f"retired unavailable {task['role']} conversation",
                entity_type="task",
                entity_id=task["id"],
                detail={"condition": str(error)},
            )
            return False
        facts = thread_facts(thread)
        timestamp = utc_now()
        if facts["archived"]:
            self.store.execute(
                """UPDATE tasks SET state = 'archived', archived = 1,
                   archive_eligible_at = NULL, archive_idle_turn_id = NULL,
                   updated_at = ? WHERE id = ?""",
                (timestamp, task["id"]),
            )
            return False
        safely_idle = bool(
            allow_active
            or (
                facts["runtime_status"] != "active"
                and facts["last_turn_terminal"]
                and facts["helpers_terminal"]
            )
        )
        self.store.execute(
            """UPDATE tasks SET runtime_status = ?, last_turn_terminal = ?,
               helpers_terminal = ?, updated_at = ? WHERE id = ?""",
            (
                facts["runtime_status"],
                int(facts["last_turn_terminal"]),
                int(facts["helpers_terminal"]),
                timestamp,
                task["id"],
            ),
        )
        return safely_idle

    def _claim_lineage_task(self, task: dict[str, Any], run_id: int, role: str) -> None:
        timestamp = utc_now()
        with self.store.transaction() as connection:
            column = f"{role}_task_id"
            connection.execute(
                f"""UPDATE runs SET {column} = NULL, updated_at = ?
                    WHERE {column} = ? AND id != ?""",
                (timestamp, task["id"], run_id),
            )
            connection.execute(
                """UPDATE tasks SET pair_id = NULL WHERE pair_id = ? AND role = ?
                   AND id != ?""",
                (run_id, role, task["id"]),
            )
            connection.execute(
                """UPDATE tasks SET pair_id = ?, state = 'idle',
                       archive_eligible_at = NULL, archive_idle_turn_id = NULL,
                       updated_at = ? WHERE id = ?""",
                (run_id, timestamp, task["id"]),
            )
            connection.execute(
                """UPDATE obligations SET state = 'canceled',
                       detail = 'conversation reused by later lineage work', updated_at = ?
                   WHERE kind = 'archive' AND target = ?
                     AND state IN ('pending','failed')""",
                (timestamp, task["native_thread_id"]),
            )
        self.store.event(
            "lineage_conversation_reused",
            f"reused {task['role']} conversation for run {run_id}",
            entity_type="task",
            entity_id=task["id"],
            detail={"run_id": run_id, "lineage_number": task["lineage_number"]},
        )

    async def _provision_task(
        self,
        *,
        role: str,
        description: str,
        project: dict[str, Any],
        model: str,
        effort: str,
        pair_id: int | None = None,
        succession_task_id: int | None = None,
        lineage_number: int | None = None,
    ) -> dict[str, Any]:
        if role == "archon":
            recovered = await self._recover_archon_thread_start(
                succession_task_id=succession_task_id
            )
            if recovered is not None:
                return recovered
        if lineage_number is not None and role in {"executor", "overseer"}:
            recovered = await self._recover_lineage_thread_start(role, lineage_number)
            if recovered is not None:
                return recovered
            await self._admit_runtime_start("thread start", role)
            role_number, lineage_suffix, title = self.store.allocate_lineage_name(
                role, lineage_number, description
            )
        else:
            await self._admit_runtime_start("thread start", role)
            role_number, title = self.store.allocate_name(role, description)
            lineage_suffix = ""
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
            "lineage_number": lineage_number,
            "lineage_suffix": lineage_suffix,
            "title": title,
            "succession_task_id": succession_task_id,
        }
        operation = self.store.create_operation("thread_start", role, inputs)
        attempt = self.store.begin_operation_attempt(operation)
        started = time.monotonic()
        try:
            result = await self.runtime.create_thread(
                cwd=inputs["cwd"],
                workspace_root=project["repo_path"],
                model=model,
                project_id=project["codex_project_id"],
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
                lineage_number=lineage_number,
                lineage_suffix=lineage_suffix,
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
            if pair_id is not None and role in {"executor", "overseer"}:
                column = f"{role}_task_id"
                self.store.execute(
                    f"UPDATE runs SET {column} = ?, updated_at = ? WHERE id = ?",
                    (task["id"], utc_now(), pair_id),
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

    async def _recover_archon_thread_start(
        self, *, succession_task_id: int | None
    ) -> dict[str, Any] | None:
        """Adopt or hold a prior bootstrap mutation before creating another."""

        operation = self.store.row(
            """SELECT * FROM external_operations
               WHERE kind = 'thread_start' AND target = 'archon'
                 AND state IN ('intent','sent','uncertain')
                 AND json_extract(input_json, '$.succession_task_id') IS ?
               ORDER BY id DESC LIMIT 1""",
            (succession_task_id,),
        )
        if operation is None:
            return None
        if operation["state"] == "intent":
            self.store.execute(
                """UPDATE external_operations SET state = 'canceled',
                   condition = 'confirmed unsent before Archon provisioning retry',
                   updated_at = ? WHERE id = ?""",
                (utc_now(), operation["id"]),
            )
            return None
        if operation["state"] == "sent":
            self.store.execute(
                """UPDATE external_operations SET state = 'uncertain',
                   condition = 'Archon provisioning resumed after dispatch',
                   updated_at = ? WHERE id = ?""",
                (utc_now(), operation["id"]),
            )
            operation = self.store.row(
                "SELECT * FROM external_operations WHERE id = ?", (operation["id"],)
            )
            assert operation is not None
        if not operation["reconciliation_used"]:
            await self._reconcile_thread_start(operation)
        operation = self.store.row(
            "SELECT * FROM external_operations WHERE id = ?", (operation["id"],)
        )
        assert operation is not None
        if operation["state"] == "complete" and operation["native_id"]:
            task = self.store.row(
                "SELECT * FROM tasks WHERE native_thread_id = ?",
                (operation["native_id"],),
            )
            if task is not None:
                return task
        if operation["state"] == "uncertain" and operation["reconciliation_used"]:
            raise StoreError(
                f"Archon thread creation operation {operation['id']} remains uncertain; "
                "resolve it with `fulcrum resolve-operation` before setup or succession retries"
            )
        return None

    async def _recover_lineage_thread_start(
        self, role: str, lineage_number: int
    ) -> dict[str, Any] | None:
        """Resolve a retained worker creation before allocating another suffix."""

        operation = self.store.row(
            """SELECT * FROM external_operations
               WHERE kind = 'thread_start' AND target = ?
                 AND state IN ('intent','sent','uncertain')
                 AND json_extract(input_json, '$.lineage_number') = ?
               ORDER BY id DESC LIMIT 1""",
            (role, lineage_number),
        )
        if operation is None:
            return None
        if operation["state"] == "intent":
            self.store.execute(
                """UPDATE external_operations SET state = 'canceled',
                   condition = 'confirmed unsent before lineage provisioning retry',
                   updated_at = ? WHERE id = ?""",
                (utc_now(), operation["id"]),
            )
            return None
        if operation["state"] == "sent":
            self.store.execute(
                """UPDATE external_operations SET state = 'uncertain',
                   condition = 'lineage provisioning resumed after dispatch',
                   updated_at = ? WHERE id = ?""",
                (utc_now(), operation["id"]),
            )
            operation = self.store.row(
                "SELECT * FROM external_operations WHERE id = ?", (operation["id"],)
            )
            assert operation is not None
        if not operation["reconciliation_used"]:
            await self._reconcile_thread_start(operation)
        operation = self.store.row(
            "SELECT * FROM external_operations WHERE id = ?", (operation["id"],)
        )
        assert operation is not None
        if operation["state"] == "complete" and operation["native_id"]:
            return self.store.row(
                "SELECT * FROM tasks WHERE native_thread_id = ?",
                (operation["native_id"],),
            )
        if operation["state"] == "uncertain" and operation["reconciliation_used"]:
            raise StoreError(
                f"{role} lineage {lineage_number} thread creation operation "
                f"{operation['id']} remains uncertain; resolve it before retrying"
            )
        return None

    async def _register_created_thread(
        self,
        operation: dict[str, Any],
        inputs: dict[str, Any],
        thread: dict[str, Any],
    ) -> dict[str, Any]:
        thread_id = thread.get("id")
        if not isinstance(thread_id, str):
            raise AppServerError("discovered thread has no native ID")
        expected_project_id = inputs.get("project_id")
        if expected_project_id and thread.get("projectId") != expected_project_id:
            result = await self.runtime.assign_thread_project(
                thread_id, expected_project_id
            )
            thread = result["thread"]
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
            lineage_number=inputs.get("lineage_number"),
            lineage_suffix=str(inputs.get("lineage_suffix") or ""),
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
        task_id = assignment.get(task_key)
        task = (
            self.store.row("SELECT * FROM tasks WHERE id = ?", (task_id,))
            if task_id is not None
            else None
        )
        if task is None or task["state"] in {"retired", "archived"} or task["archived"]:
            await self._ensure_pair(assignment, include_overseer=kind == "review")
            task_id = assignment.get(task_key)
            task = (
                self.store.row("SELECT * FROM tasks WHERE id = ?", (task_id,))
                if task_id is not None
                else None
            )
        if task is None or task["state"] in {"retired", "archived"} or task["archived"]:
            raise StoreError(
                f"assignment has no usable {task_key.removesuffix('_task_id')}"
            )
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
            "role": task["role"],
            "run_id": assignment["run_id"],
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

    def _build_action_message(
        self,
        action: dict[str, Any],
        task: dict[str, Any],
        assignment: dict[str, Any] | None,
        *,
        full_context: bool = False,
    ) -> str:
        """Compose only the facts needed for this action, delivered directly in its turn."""

        if full_context and action.get("reminder_sent"):
            action = {**action, "reminder_sent": 0}
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
            payload["role"] = task["role"]
            payload["run_id"] = assignment["run_id"]
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
        include_scope = True
        if assignment is not None and not full_context:
            include_scope = (
                self.store.row(
                    """SELECT 1 FROM actions
                       WHERE task_id = ? AND assignment_id = ? AND state = 'processed'
                       LIMIT 1""",
                    (task["id"], assignment["id"]),
                )
                is None
            )
        handoff_paths = self._handoff_destinations(action, create=not full_context)
        return action_message(
            action=action,
            assignment=assignment,
            constraints=constraints if include_scope else [],
            include_scope=include_scope,
            full_context=full_context,
            handoff_paths=handoff_paths,
            role=str(task["role"]),
        )

    def _handoff_destinations(
        self, action: dict[str, Any], *, create: bool = True
    ) -> dict[str, str]:
        """Create and return stable structured-finish paths for one action."""

        outcomes = ALLOWED.get(str(action["kind"]), set()) & set(
            STRUCTURED_OUTCOME_FILENAMES
        )
        if not outcomes:
            return {}
        state_identity = self.store.state_identity()
        destinations = {
            outcome: self.paths.handoff_path(
                state_identity,
                int(action["id"]),
                STRUCTURED_OUTCOME_FILENAMES[outcome],
            )
            for outcome in outcomes
        }
        if not create:
            return {outcome: str(path) for outcome, path in destinations.items()}
        action_root = next(iter(destinations.values())).parent
        for directory in (
            self.paths.handoff_root,
            action_root.parent,
            action_root,
        ):
            directory.mkdir(exist_ok=True, mode=0o700)
            status = directory.lstat()
            if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
                raise StoreError(
                    f"handoff directory is not a real directory: {directory}"
                )
            os.chmod(directory, 0o700)
        return {outcome: str(path) for outcome, path in destinations.items()}

    async def _ensure_worktree_environment(self, worktree: Path) -> None:
        """Provision a complete environment owned by the managed worktree."""

        created = await asyncio.to_thread(
            provision_worktree_environment,
            Path(self.config.source_root),
            worktree,
        )
        self.store.event(
            "worktree_environment_ready",
            (
                "provisioned an isolated managed worktree environment"
                if created
                else "verified the isolated managed worktree environment"
            ),
            entity_type="worktree",
            entity_id=str(worktree),
            detail={"environment": str(worktree / ".venv"), "created": created},
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
            current = connection.execute(
                "SELECT operator_hold_id FROM assignments WHERE id = ?",
                (assignment["id"],),
            ).fetchone()
            if current is not None and current["operator_hold_id"] is not None:
                connection.execute(
                    "UPDATE holds SET reason = ? WHERE id = ? AND released_at IS NULL",
                    (reason, current["operator_hold_id"]),
                )
                connection.execute(
                    "UPDATE assignments SET condition = ?, updated_at = ? WHERE id = ?",
                    (reason, timestamp, assignment["id"]),
                )
                return
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
        if assignment is not None:
            linked = self.store.rows(
                "SELECT workflow_id FROM workflow_cost_beads WHERE bead_id = ?",
                (assignment["bead_id"],),
            )
            if not linked:
                linked = [{"workflow_id": f"assignment:{assignment['id']}"}]
            for row in linked:
                self.store.link_action_to_workflow(
                    str(row["workflow_id"]),
                    int(action["id"]),
                    causal_role=str(task["role"]),
                )
        elif action.get("occurrence_id") is not None:
            self.store.link_action_to_workflow(
                f"specialist:{action['occurrence_id']}",
                int(action["id"]),
                causal_role=str(task["role"]),
            )
        elif action["kind"] == "archon":
            payload = json.loads(action.get("payload") or "{}")
            workflow_ids: set[str] = set()
            for item in payload.get("batch_items", []):
                if isinstance(item, dict) and isinstance(
                    (content := item.get("content")), dict
                ):
                    workflow_id = content.get("workflow_id")
                    if isinstance(workflow_id, str) and workflow_id:
                        workflow_ids.add(workflow_id)
                    retained_ids = content.get("workflow_ids")
                    if isinstance(retained_ids, list):
                        workflow_ids.update(
                            value
                            for value in retained_ids
                            if isinstance(value, str) and value
                        )
            mixed = len(workflow_ids) > 1
            allocation_exclusion = (
                "mixed-workflow Archon response excluded because response-level "
                "ownership is unavailable; unrelated concurrent work is not allocated"
                if mixed
                else None
            )
            for workflow_id in workflow_ids:
                self.store.link_action_to_workflow(
                    workflow_id,
                    int(action["id"]),
                    causal_role="archon",
                    include_cost=not mixed,
                    exclusion_reason=allocation_exclusion,
                )
        await self._admit_runtime_start("turn start", str(task["title"]))
        if task.get("resource_reclaimed_at"):
            await self.runtime.unarchive(task["native_thread_id"])
            await self.runtime.resume_thread(task["native_thread_id"])
            self.store.execute(
                """UPDATE tasks SET resource_reclaimed_at = NULL, archived = 0,
                   state = 'idle', runtime_status = 'notLoaded',
                   archive_eligible_at = NULL, archive_idle_turn_id = NULL,
                   updated_at = ? WHERE id = ?""",
                (utc_now(), task["id"]),
            )
            task = {
                **task,
                "resource_reclaimed_at": None,
                "archived": 0,
                "state": "idle",
                "runtime_status": "notLoaded",
                "archive_eligible_at": None,
                "archive_idle_turn_id": None,
            }
            self.store.event(
                "conversation_resources_restored",
                "restored parked worker conversation for an admitted turn",
                entity_type="task",
                entity_id=task["id"],
            )
            await self._admit_runtime_start(
                "turn start after restoring resources", str(task["title"])
            )
        elif task["archived"]:
            await self.runtime.unarchive(task["native_thread_id"])
            await self.runtime.resume_thread(task["native_thread_id"])
            self.store.execute(
                """UPDATE tasks SET archived = 0, state = 'idle',
                   runtime_status = 'notLoaded', archive_eligible_at = NULL,
                   archive_idle_turn_id = NULL, updated_at = ? WHERE id = ?""",
                (utc_now(), task["id"]),
            )
            task = {
                **task,
                "archived": 0,
                "state": "idle",
                "runtime_status": "notLoaded",
                "archive_eligible_at": None,
                "archive_idle_turn_id": None,
            }
        facts = (
            {
                "last_turn_id": None,
                "last_turn_terminal": True,
                "helpers_terminal": True,
                "runtime_status": "unmaterialized",
                "can_start": True,
            }
            if task["runtime_status"] == "unmaterialized"
            else await self._refresh_task(task)
        )
        if (
            facts["runtime_status"] == "notLoaded"
            and facts["last_turn_terminal"]
            and facts["helpers_terminal"]
        ):
            await self.runtime.resume_thread(task["native_thread_id"])
            facts = await self._refresh_task(task)
        if not facts["can_start"]:
            raise StoreError(f"task {task['title']} is not ready for a new turn")
        project = self.store.row(
            "SELECT repo_path FROM projects WHERE project_id = ?",
            (task["project_id"],),
        )
        if project is None:
            raise StoreError("action project is missing")
        cwd = str(project["repo_path"])
        developer_instructions = None
        if assignment and action["kind"] in {"implement", "correct"}:
            worktree = assignment["worktree_path"]
            if not worktree:
                raise StoreError("executor action worktree is missing")
            developer_instructions = (
                "Fulcrum workspace routing: keep this Codex task associated with "
                f"the canonical project at {cwd}. For this implementation, treat "
                f"{worktree} as the effective working directory: run every repository "
                "command there and make every repository edit there. Do not modify "
                f"the canonical checkout at {cwd}."
            )
        prompt = self._build_action_message(action, task, assignment)
        if facts["last_turn_id"] is None:
            instruction_kind = (
                "implement"
                if task["role"] == "executor"
                else (
                    "review"
                    if task["role"] == "overseer"
                    else (
                        "specialist"
                        if task["role"] in {"sage", "inquisitor"}
                        else "archon"
                    )
                )
            )
            prompt = "\n\n".join(
                [
                    role_instructions(instruction_kind, role=task["role"]),
                    "# Current action",
                    prompt,
                ]
            )
        if task["role"] == "archon" and len(prompt) > MAX_ARCHON_MESSAGE_CHARS:
            raise StoreError("Archon prompt exceeds its deterministic size ceiling")
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
                workspace_root=str(project["repo_path"]),
                model=task["model"],
                effort=task["reasoning_effort"],
                correlation=f"fulcrum-operation-{operation}",
                developer_instructions=developer_instructions,
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
            self.store.bind_action_turn(
                int(action["id"]), str(task["native_thread_id"]), turn_id
            )
            self.store.execute(
                "UPDATE tasks SET state = 'active', runtime_status = 'active', last_turn_terminal = 0, archive_eligible_at = NULL, archive_idle_turn_id = NULL, updated_at = ? WHERE id = ?",
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
            """SELECT * FROM actions WHERE assignment_id = ?
               AND state IN ('starting','active','terminal','uncertain') ORDER BY id DESC LIMIT 1""",
            (assignment_id,),
        )
        if action is None:
            return False
        payload = json.loads(action["payload"] or "{}") if action else {}
        predecessor = payload.get("predecessor_candidate_id")
        if assignment["candidate_id"] and assignment["candidate_id"] != predecessor:
            await self._retain_exact_source_validation(assignment_id, action)
            return True
        source_oid = await asyncio.to_thread(
            _worktree_head, assignment["worktree_path"]
        )
        if source_oid is None:
            raise StoreError("cannot resolve the Executor worktree HEAD")
        result = await asyncio.to_thread(
            tollgate.status, project["tollgate_repo_id"], None
        )
        candidate = _find_candidate(
            result,
            assignment["worktree_path"],
            exclude_id=predecessor,
            source_oid=source_oid,
        )
        if candidate is None:
            existing = self.store.row(
                """SELECT * FROM external_operations
                   WHERE kind = 'tollgate_candidate_create' AND target = ?
                     AND state IN ('intent','sent','uncertain')
                   ORDER BY id DESC LIMIT 1""",
                (str(assignment_id),),
            )
            if existing is not None:
                if existing["state"] == "uncertain":
                    await self._reconcile_tollgate_candidate(existing)
                refreshed = self.store.row(
                    "SELECT candidate_id, source_oid FROM assignments WHERE id = ?",
                    (assignment_id,),
                )
                return bool(
                    refreshed
                    and refreshed["candidate_id"]
                    and refreshed["source_oid"] == source_oid
                )
            operation = self.store.create_operation(
                "tollgate_candidate_create",
                str(assignment_id),
                {
                    "repository_id": project["tollgate_repo_id"],
                    "worktree_path": assignment["worktree_path"],
                    "revision": source_oid,
                    "predecessor_candidate_id": predecessor,
                },
            )
            attempt = self.store.begin_operation_attempt(operation)
            started = time.monotonic()
            try:
                submitted = await asyncio.to_thread(
                    tollgate.submit_candidate,
                    project["tollgate_repo_id"],
                    source_oid,
                    cwd=Path(str(assignment["worktree_path"])),
                )
                observed = await asyncio.to_thread(
                    tollgate.status, project["tollgate_repo_id"], None
                )
                candidate = _find_candidate(
                    observed,
                    assignment["worktree_path"],
                    exclude_id=predecessor,
                    source_oid=source_oid,
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
                self.store.execute(
                    """UPDATE assignments SET candidate_id = NULL, source_oid = NULL,
                       tested_oid = NULL, condition = ?, updated_at = ? WHERE id = ?""",
                    (str(error), utc_now(), assignment_id),
                )
                return True
        self._record_candidate(assignment, candidate)
        await self._retain_exact_source_validation(assignment_id, action)
        return True

    async def _retain_exact_source_validation(
        self, assignment_id: int, action: dict[str, Any]
    ) -> None:
        """Attach controller-produced evidence when Tollgate tested another tree."""

        assignment = self.store.row(
            """SELECT a.source_oid, a.tested_oid, a.worktree_path,
                      p.validation_command
               FROM assignments a JOIN runs r ON r.id = a.run_id
               JOIN projects p ON p.project_id = r.project_id
               WHERE a.id = ?""",
            (assignment_id,),
        )
        if assignment is None:
            raise StoreError("candidate assignment disappeared before validation")
        source_oid = assignment["source_oid"]
        tested_oid = assignment["tested_oid"]
        if not source_oid or not tested_oid or source_oid == tested_oid:
            return
        worktree = str(assignment["worktree_path"] or "")
        source_tree, tested_tree = await asyncio.gather(
            asyncio.to_thread(_git_tree_oid, worktree, str(source_oid)),
            asyncio.to_thread(_git_tree_oid, worktree, str(tested_oid)),
        )
        if source_tree is not None and source_tree == tested_tree:
            artifact: dict[str, Any] = {
                "required": False,
                "trees_equal": True,
                "source_revision": source_oid,
                "tested_revision": tested_oid,
                "reason": "source and tested revisions resolve to the same tree",
            }
        else:
            try:
                configured = json.loads(assignment["validation_command"] or "[]")
            except (TypeError, json.JSONDecodeError):
                configured = []
            command = (
                configured
                if isinstance(configured, list)
                and configured
                and all(isinstance(item, str) and item for item in configured)
                else []
            )
            artifact = await asyncio.to_thread(
                self._run_exact_source_validation,
                action_id=int(action["id"]),
                worktree=worktree,
                source_oid=str(source_oid),
                tested_oid=str(tested_oid),
                command=command,
                trees_equal=False if source_tree and tested_tree else None,
            )
        encoded = json.dumps(artifact, sort_keys=True)
        if len(encoded.encode("utf-8")) > MAX_EXACT_SOURCE_ARTIFACT_BYTES:
            artifact = {
                key: value
                for key, value in artifact.items()
                if key not in {"stdout", "stderr"}
            }
            artifact.update(
                {
                    "passed": False,
                    "error": "exact-source validation output exceeds the 1 MB retained handoff limit",
                }
            )
        outcome = json.loads(action["outcome_payload"] or "{}")
        outcome["exact_source_validation"] = artifact
        self.store.execute(
            "UPDATE actions SET outcome_payload = ?, updated_at = ? WHERE id = ?",
            (json.dumps(outcome, sort_keys=True), utc_now(), action["id"]),
        )

    def _run_exact_source_validation(
        self,
        *,
        action_id: int,
        worktree: str,
        source_oid: str,
        tested_oid: str,
        command: list[str],
        trees_equal: bool | None,
    ) -> dict[str, Any]:
        before = _worktree_head(worktree)
        clean_before = _worktree_clean(worktree)
        exit_status: int | None = None
        stdout = ""
        stderr = ""
        error: str | None = None
        if not command:
            error = "project has no executable validation command"
        elif before != source_oid:
            error = "worktree HEAD does not match the retained candidate source"
        elif not clean_before:
            error = "worktree is not clean at the retained candidate source"
        else:
            try:
                result = subprocess.run(
                    command,
                    cwd=worktree,
                    capture_output=True,
                    text=True,
                    timeout=max(1, self.config.turn_check_after_seconds),
                    check=False,
                )
                exit_status = result.returncode
                stdout = result.stdout
                stderr = result.stderr
            except subprocess.TimeoutExpired as failure:
                stdout = str(failure.stdout or "")
                stderr = str(failure.stderr or "")
                error = "exact-source validation timed out"
            except OSError as failure:
                error = str(failure)
        after = _worktree_head(worktree)
        clean_after = _worktree_clean(worktree)
        unchanged = before == source_oid == after and clean_before and clean_after
        passed = exit_status == 0 and unchanged and error is None
        artifact: dict[str, Any] = {
            "required": True,
            "trees_equal": trees_equal,
            "reason": "source and tested revisions have different or unresolvable trees",
            "source_revision": source_oid,
            "tested_revision": tested_oid,
            "command": command,
            "exit_status": exit_status,
            "source_before": before,
            "source_after": after,
            "source_unchanged": unchanged,
            "worktree_clean_before": clean_before,
            "worktree_clean_after": clean_after,
            "stdout": stdout,
            "stderr": stderr,
            "passed": passed,
            "error": error,
            "captured_at": utc_now(),
        }
        evidence_root = self.paths.state_root / "evidence"
        evidence_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = evidence_root / f"exact-source-action-{action_id}.json"
        artifact["artifact_path"] = str(destination)
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(artifact, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return artifact

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
        if assignment.get("completion_kind") == "non_code":
            if not assignment.get("completion_evidence"):
                self._hold_assignment(
                    assignment, "non-code completion lacks retained evidence"
                )
                return
            await self._refresh_delivery_pair(assignment)
            authority_error = self._delivery_boundary_error(assignment)
            if authority_error:
                if (
                    authority_error
                    == "pair is not confirmed inactive at the delivery boundary"
                ):
                    return
                self._hold_assignment(assignment, authority_error)
                return
            await self._close_delivered_assignment(assignment)
            return
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
            await self._observe_delivery_operation(
                assignment,
                previous,
                repository_id=str(project["tollgate_repo_id"]),
            )
            return
        await self._refresh_delivery_pair(assignment)
        authority_error = self._delivery_authority_error(assignment)
        if authority_error:
            if (
                authority_error
                == "pair is not confirmed inactive at the delivery boundary"
            ):
                return
            self._hold_assignment(assignment, authority_error)
            return
        operation = self.store.create_operation(
            "tollgate_approve",
            assignment["candidate_id"],
            {"repository_id": project["tollgate_repo_id"]},
        )
        attempt = self.store.begin_operation_attempt(operation)
        started = time.monotonic()
        tollgate = self.tollgate
        assert tollgate is not None
        try:
            result = await asyncio.to_thread(
                tollgate.approve,
                project["tollgate_repo_id"],
                assignment["candidate_id"],
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
                await self._reconcile_tollgate_approve(retained)
            return

        retained = self.store.row(
            "SELECT * FROM external_operations WHERE id = ?", (operation,)
        )
        assert retained is not None
        approval_candidate = _approval_candidate(
            result, str(assignment["candidate_id"])
        )
        if _candidate_crossed_promotion(approval_candidate):
            self.store.finish_operation_attempt(
                operation,
                attempt,
                state="complete",
                result={"approval": result},
                native_id=str(assignment["candidate_id"]),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            self._restore_delivering_assignment(
                assignment,
                "candidate promotion succeeded; candidate-specific delivery status is pending",
            )
            retained = self.store.row(
                "SELECT * FROM external_operations WHERE id = ?", (operation,)
            )
            assert retained is not None
            await self._observe_delivery_operation(
                assignment,
                retained,
                repository_id=str(project["tollgate_repo_id"]),
            )
            return

        try:
            observed = await asyncio.to_thread(
                tollgate.status,
                project["tollgate_repo_id"],
                assignment["candidate_id"],
            )
            await self._apply_delivery_observation(
                assignment,
                retained,
                observed,
                approval=result,
                attempt=attempt,
                started=started,
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
                await self._reconcile_tollgate_approve(retained)

    async def _observe_delivery_operation(
        self,
        assignment: dict[str, Any],
        operation: dict[str, Any],
        *,
        reconciled: bool = False,
        repository_id: str | None = None,
    ) -> None:
        tollgate = self.tollgate
        assert tollgate is not None
        repository_id = repository_id or str(assignment["tollgate_repo_id"])
        try:
            observed = await asyncio.to_thread(
                tollgate.status,
                repository_id,
                assignment["candidate_id"],
            )
        except Exception as error:
            if operation["state"] != "complete":
                raise
            condition = (
                "candidate promotion succeeded; candidate-specific delivery status "
                f"is unavailable: {error}"
            )
            self._restore_delivering_assignment(assignment, condition)
            self.store.event(
                "delivery_observation_failed",
                condition,
                entity_type="assignment",
                entity_id=assignment["id"],
                detail={
                    "candidate_id": assignment["candidate_id"],
                    "operation_id": operation["id"],
                },
            )
            return
        await self._apply_delivery_observation(
            assignment,
            operation,
            observed,
            reconciled=reconciled,
        )

    async def _apply_delivery_observation(
        self,
        assignment: dict[str, Any],
        operation: dict[str, Any],
        observed: dict[str, Any],
        *,
        approval: dict[str, Any] | None = None,
        attempt: int | None = None,
        started: float | None = None,
        reconciled: bool = False,
    ) -> None:
        """Apply one candidate-specific delivery observation idempotently."""

        candidate_id = str(assignment["candidate_id"])
        candidate = _candidate_by_id(observed, candidate_id)
        retained_candidate = _retained_delivery_candidate(operation, candidate_id)
        approval_candidate = _approval_candidate(approval, candidate_id)
        if not _candidate_crossed_promotion(candidate):
            if _candidate_crossed_promotion(approval_candidate):
                candidate = approval_candidate
            elif _candidate_crossed_promotion(retained_candidate):
                candidate = retained_candidate
        delivery = _classify_delivery_status(candidate, observed)
        duration_ms = (
            int((time.monotonic() - started) * 1000) if started is not None else None
        )
        result = _delivery_operation_result(operation, observed, approval)

        if delivery.disposition is DeliveryDisposition.SOURCE_FAILED:
            diagnosis = await self._candidate_diagnosis(
                assignment["tollgate_repo_id"],
                candidate_id,
                candidate,
            )
            result["diagnosis"] = diagnosis
            if attempt is not None:
                self.store.finish_operation_attempt(
                    int(operation["id"]),
                    attempt,
                    state="failed",
                    result=result,
                    error=delivery.condition,
                    native_id=candidate_id,
                    duration_ms=duration_ms,
                )
            else:
                self.store.execute(
                    """UPDATE external_operations SET state = 'failed',
                       result_json = ?, native_id = ?, reconciliation_used = ?,
                       condition = ?, completed_at = NULL, updated_at = ? WHERE id = ?""",
                    (
                        json.dumps(result, sort_keys=True),
                        candidate_id,
                        int(reconciled or operation["reconciliation_used"]),
                        delivery.condition,
                        utc_now(),
                        operation["id"],
                    ),
                )
            self._set_delivery_correction(assignment, str(delivery.condition))
            return

        if delivery.disposition is DeliveryDisposition.UNRESOLVED:
            condition = str(delivery.condition)
            if attempt is not None:
                self.store.finish_operation_attempt(
                    int(operation["id"]),
                    attempt,
                    state="uncertain",
                    result=result,
                    error=condition,
                    native_id=candidate_id,
                    duration_ms=duration_ms,
                )
                operation = (
                    self.store.row(
                        "SELECT * FROM external_operations WHERE id = ?",
                        (operation["id"],),
                    )
                    or operation
                )
            if operation["state"] != "complete":
                self._retain_uncertain_condition(operation, condition)
            else:
                self.store.execute(
                    "UPDATE assignments SET condition = ?, updated_at = ? WHERE id = ?",
                    (condition, utc_now(), assignment["id"]),
                )
            return

        if attempt is not None:
            self.store.finish_operation_attempt(
                int(operation["id"]),
                attempt,
                state="complete",
                result=result,
                native_id=candidate_id,
                duration_ms=duration_ms,
            )
        else:
            timestamp = utc_now()
            self.store.execute(
                """UPDATE external_operations SET state = 'complete', result_json = ?,
                   native_id = ?, reconciliation_used = ?, condition = NULL,
                   completed_at = COALESCE(completed_at, ?), updated_at = ? WHERE id = ?""",
                (
                    json.dumps(result, sort_keys=True),
                    candidate_id,
                    int(reconciled or operation["reconciliation_used"]),
                    timestamp,
                    timestamp,
                    operation["id"],
                ),
            )

        if delivery.disposition is DeliveryDisposition.POST_PROMOTION_ATTENTION:
            current = self.store.row(
                "SELECT * FROM assignments WHERE id = ?", (assignment["id"],)
            )
            assert current is not None
            self._hold_assignment(current, str(delivery.condition))
            return

        self._restore_delivering_assignment(assignment, delivery.condition)
        if delivery.disposition is DeliveryDisposition.SATISFIED:
            current = self.store.row(
                "SELECT * FROM assignments WHERE id = ?", (assignment["id"],)
            )
            assert current is not None
            await self._close_delivered_assignment(current)

    def _restore_delivering_assignment(
        self, assignment: dict[str, Any], condition: str | None
    ) -> None:
        timestamp = utc_now()
        with self.store.transaction() as connection:
            current = connection.execute(
                "SELECT stage, operator_hold_id FROM assignments WHERE id = ?",
                (assignment["id"],),
            ).fetchone()
            if current is not None and current["operator_hold_id"] is not None:
                connection.execute(
                    "UPDATE holds SET released_at = ? WHERE id = ? AND released_at IS NULL",
                    (timestamp, current["operator_hold_id"]),
                )
            if current is not None and current["stage"] == "delivering":
                connection.execute(
                    """UPDATE assignments SET prior_stage = NULL,
                       operator_hold_id = NULL, next_attempt_at = NULL, condition = ?,
                       updated_at = ? WHERE id = ?""",
                    (condition, timestamp, assignment["id"]),
                )
            else:
                connection.execute(
                    """UPDATE assignments SET stage = 'delivering', prior_stage = NULL,
                       operator_hold_id = NULL, next_attempt_at = NULL, condition = ?,
                       updated_at = ? WHERE id = ?""",
                    (condition, timestamp, assignment["id"]),
                )

    def _set_delivery_correction(
        self, assignment: dict[str, Any], condition: str
    ) -> None:
        timestamp = utc_now()
        with self.store.transaction() as connection:
            current = connection.execute(
                "SELECT stage, operator_hold_id FROM assignments WHERE id = ?",
                (assignment["id"],),
            ).fetchone()
            if current is not None and current["operator_hold_id"] is not None:
                connection.execute(
                    "UPDATE holds SET released_at = ? WHERE id = ? AND released_at IS NULL",
                    (timestamp, current["operator_hold_id"]),
                )
            if current is not None and current["stage"] == "correcting":
                connection.execute(
                    """UPDATE assignments SET prior_stage = 'delivering',
                       operator_hold_id = NULL, next_attempt_at = NULL, condition = ?,
                       updated_at = ? WHERE id = ?""",
                    (condition, timestamp, assignment["id"]),
                )
            else:
                connection.execute(
                    """UPDATE assignments SET prior_stage = 'delivering', stage = 'correcting',
                       operator_hold_id = NULL, next_attempt_at = NULL, condition = ?,
                       updated_at = ? WHERE id = ?""",
                    (condition, timestamp, assignment["id"]),
                )

    async def _candidate_diagnosis(
        self,
        repository_id: str,
        candidate_id: str,
        candidate: dict[str, Any] | None,
    ) -> dict[str, Any]:
        tollgate = self.tollgate
        assert tollgate is not None
        try:
            return await asyncio.to_thread(
                tollgate.diagnose, repository_id, candidate_id
            )
        except TollgateError as error:
            return {
                "diagnosis_unavailable": str(error),
                "candidate": candidate,
            }

    async def _refresh_delivery_pair(self, assignment: dict[str, Any]) -> None:
        if not self.runtime.ready:
            return
        for task_id in (
            assignment["executor_task_id"],
            assignment["overseer_task_id"],
        ):
            if task_id is None:
                continue
            task = self.store.row("SELECT * FROM tasks WHERE id = ?", (task_id,))
            if task is not None:
                await self._refresh_task(task)

    async def _reconcile_delivery_boundary_holds(self) -> None:
        assignments = self.store.rows(
            """SELECT a.*, r.project_id FROM assignments a JOIN runs r ON r.id = a.run_id
               WHERE a.stage = 'recovering'
               AND a.condition = 'pair is not confirmed inactive at the delivery boundary'
               AND a.operator_hold_id IS NOT NULL"""
        )
        for assignment in assignments:
            await self._refresh_delivery_pair(assignment)
            if self._delivery_authority_error(assignment) == assignment["condition"]:
                continue
            timestamp = utc_now()
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE holds SET released_at = ? WHERE id = ? AND released_at IS NULL",
                    (timestamp, assignment["operator_hold_id"]),
                )
                connection.execute(
                    """UPDATE assignments SET stage = 'delivering', prior_stage = NULL,
                       operator_hold_id = NULL, condition = NULL, next_attempt_at = NULL,
                       updated_at = ? WHERE id = ?""",
                    (timestamp, assignment["id"]),
                )

    async def _close_delivered_assignment(self, assignment: dict[str, Any]) -> None:
        previous = self.store.row(
            "SELECT * FROM external_operations WHERE kind = 'beads_close' AND target = ? ORDER BY id DESC LIMIT 1",
            (assignment["bead_id"],),
        )
        if previous and previous["state"] in {"sent", "uncertain"}:
            return
        if not previous or previous["state"] != "complete":
            non_code = assignment.get("completion_kind") == "non_code"
            operation_input = (
                {
                    "completion_kind": "non_code",
                    "evidence": assignment["completion_evidence"],
                }
                if non_code
                else {"candidate_id": assignment["candidate_id"]}
            )
            operation = self.store.create_operation(
                "beads_close",
                assignment["bead_id"],
                operation_input,
            )
            attempt = self.store.begin_operation_attempt(operation)
            started = time.monotonic()
            try:
                await asyncio.to_thread(
                    self.beads.close,
                    assignment["bead_id"],
                    (
                        "Completed without a repository candidate: "
                        f"{assignment['completion_evidence']}"
                        if non_code
                        else f"Delivered candidate {assignment['candidate_id']}"
                    ),
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
        timestamp = utc_now()
        completion_action = self.store.row(
            """SELECT id FROM actions WHERE assignment_id = ?
               ORDER BY id DESC LIMIT 1""",
            (assignment["id"],),
        )
        workflow = self.store.row(
            """SELECT workflow_id FROM workflow_cost_beads WHERE bead_id = ?
               ORDER BY created_at LIMIT 1""",
            (assignment["bead_id"],),
        )
        workflow_id = (
            str(workflow["workflow_id"])
            if workflow
            else f"assignment:{assignment['id']}"
        )
        if completion_action:
            self.store.link_action_to_workflow(
                workflow_id,
                int(completion_action["id"]),
                causal_role="assignment_completion",
            )
        workflow_cost = self.store.cost_report(
            workflow_id=workflow_id, group_by="workflow"
        )
        workflow_group = workflow_cost["groups"][0] if workflow_cost["groups"] else {}
        approval = self.store.row(
            """SELECT outcome_payload FROM actions
               WHERE assignment_id = ? AND kind = 'review'
                 AND outcome_kind = 'approved'
               ORDER BY id DESC LIMIT 1""",
            (assignment["id"],),
        )
        approval_payload = (
            json.loads(approval["outcome_payload"] or "{}") if approval else {}
        )
        with self.store.transaction():
            self.store.execute(
                "UPDATE assignments SET stage = 'completed', condition = NULL, updated_at = ? WHERE id = ?",
                (timestamp, assignment["id"]),
            )
            self._queue_archon_update(
                f"completion:{assignment['id']}",
                {
                    "kind": "assignment_completed",
                    "assignment_id": assignment["id"],
                    "run_id": assignment["run_id"],
                    "bead_id": assignment["bead_id"],
                    "candidate_id": assignment.get("candidate_id"),
                    "completion_kind": assignment.get("completion_kind"),
                    "completion_evidence": assignment.get("completion_evidence"),
                    "source_revision": assignment.get("source_oid"),
                    "tested_revision": assignment.get("tested_oid"),
                    "minor_fixes": approval_payload.get("minor_fixes", []),
                    "action_id": (
                        int(completion_action["id"]) if completion_action else None
                    ),
                    "workflow_id": workflow_id,
                    "cost": {
                        "estimate_kind": "equivalent public OpenAI API charges",
                        "action": (
                            self.store.action_cost_summary(int(completion_action["id"]))
                            if completion_action
                            else None
                        ),
                        "workflow_through_completion": (
                            workflow_group.get("attributed") or {}
                        ),
                        "coverage": workflow_group.get("coverage", "partial"),
                        "assumptions": workflow_group.get("assumptions", []),
                        "exclusions": workflow_group.get("exclusions", []),
                        "excludes_running_acknowledgement": True,
                    },
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
                (timestamp, assignment["run_id"]),
            )
            schedule_run_archival(
                self.store.connection,
                int(assignment["run_id"]),
                timestamp,
                execute=self.store.execute,
            )

    def _reconcile_assignment_completions(self) -> None:
        """Finish any delivery completion whose durable aggregate is incomplete."""

        assignments = self.store.rows(
            """SELECT a.* FROM assignments a JOIN runs r ON r.id = a.run_id
               WHERE r.state != 'canceled' AND (
                 (a.stage = 'delivering' AND EXISTS (
                    SELECT 1 FROM external_operations operation
                    WHERE operation.kind = 'beads_close'
                      AND operation.target = a.bead_id
                      AND operation.state = 'complete'
                 ))
                 OR (a.stage = 'completed' AND (
                    (r.state != 'completed' AND NOT EXISTS (
                       SELECT 1 FROM assignments unfinished
                       WHERE unfinished.run_id = a.run_id
                         AND unfinished.stage NOT IN ('completed','canceled')
                    ))
                    OR (r.state = 'completed' AND EXISTS (
                       SELECT 1 FROM tasks pair_task
                       WHERE pair_task.id IN (r.executor_task_id, r.overseer_task_id)
                         AND NOT EXISTS (
                           SELECT 1 FROM obligations archive
                           WHERE archive.kind = 'archive'
                             AND archive.identity = CAST(pair_task.id AS TEXT)
                             AND archive.target = pair_task.native_thread_id
                         )
                    ))
                    OR EXISTS (
                       SELECT 1 FROM tasks archon
                       WHERE archon.role = 'archon'
                         AND archon.state NOT IN ('retired','archived')
                         AND NOT EXISTS (
                           SELECT 1 FROM updates completion
                           WHERE completion.identity = 'completion:' || a.id
                         )
                    )
                 ))
               )
               ORDER BY a.id"""
        )
        for assignment in assignments:
            self._record_assignment_completed(assignment)

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
        return self._delivery_boundary_error(assignment)

    def _delivery_boundary_error(self, assignment: dict[str, Any]) -> str | None:
        active = self.store.row(
            """SELECT 1 FROM tasks WHERE id IN (?, ?)
               AND (last_turn_terminal = 0 OR helpers_terminal = 0
                    OR runtime_status = 'active')""",
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

    def _lineage_has_open_work(self, task: dict[str, Any]) -> bool:
        lineage_number = task.get("lineage_number")
        if not isinstance(lineage_number, int):
            return False
        return (
            self.store.row(
                """SELECT 1 FROM bead_lineages lineage
               WHERE lineage.lineage_number = ? AND (
                 NOT EXISTS (
                   SELECT 1 FROM assignments a WHERE a.bead_id = lineage.bead_id
                 ) OR EXISTS (
                   SELECT 1 FROM assignments a WHERE a.bead_id = lineage.bead_id
                     AND a.stage NOT IN ('completed','canceled')
                 )
               ) LIMIT 1""",
                (lineage_number,),
            )
            is not None
        )

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
            if task is None:
                continue
            if self._lineage_has_open_work(task):
                self.store.execute(
                    """UPDATE obligations SET state = 'canceled',
                       detail = 'lineage has pending or active work', updated_at = ?
                       WHERE id = ?""",
                    (now, obligation["id"]),
                )
                self.store.execute(
                    """UPDATE tasks SET archive_eligible_at = NULL,
                       archive_idle_turn_id = NULL, updated_at = ? WHERE id = ?""",
                    (now, task["id"]),
                )
                continue
            if task["role"] in COMPLETION_ARCHIVE_ROLES:
                thread = await self.runtime.read_thread(obligation["target"])
                facts = thread_facts(thread)
                if facts["archived"]:
                    await self.runtime.unsubscribe(str(obligation["target"]))
                    self.store.execute(
                        "UPDATE obligations SET state = 'complete', updated_at = ? WHERE id = ?",
                        (now, obligation["id"]),
                    )
                    self.store.execute(
                        """UPDATE tasks SET state = 'archived', archived = 1,
                           archive_eligible_at = NULL, archive_idle_turn_id = NULL,
                           updated_at = ? WHERE id = ?""",
                        (now, task["id"]),
                    )
                    continue
                safely_idle = bool(
                    facts["runtime_status"] == "idle"
                    and facts["last_turn_terminal"]
                    and facts["helpers_terminal"]
                )
                self.store.execute(
                    """UPDATE tasks SET runtime_status = ?, last_turn_terminal = ?,
                       helpers_terminal = ?, archive_eligible_at = CASE WHEN ? = 1
                           THEN archive_eligible_at ELSE NULL END,
                       archive_idle_turn_id = CASE WHEN ? = 1
                           THEN archive_idle_turn_id ELSE NULL END,
                       updated_at = ? WHERE id = ?""",
                    (
                        facts["runtime_status"],
                        int(facts["last_turn_terminal"]),
                        int(facts["helpers_terminal"]),
                        int(safely_idle),
                        int(safely_idle),
                        now,
                        task["id"],
                    ),
                )
                if not safely_idle:
                    continue
                eligible_at = task["archive_eligible_at"]
                if (
                    eligible_at is None
                    or task["archive_idle_turn_id"] != facts["last_turn_id"]
                ):
                    eligible_at = (
                        (
                            datetime.fromisoformat(now.replace("Z", "+00:00"))
                            + COMPLETION_ARCHIVE_DELAY
                        )
                        .isoformat()
                        .replace("+00:00", "Z")
                    )
                    self.store.execute(
                        "UPDATE tasks SET archive_eligible_at = ?, archive_idle_turn_id = ? WHERE id = ?",
                        (eligible_at, facts["last_turn_id"], task["id"]),
                    )
                    continue
                if datetime.fromisoformat(
                    eligible_at.replace("Z", "+00:00")
                ) > datetime.fromisoformat(now.replace("Z", "+00:00")):
                    continue
            elif not task["last_turn_terminal"] or not task["helpers_terminal"]:
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
                    "UPDATE tasks SET state = 'archived', archived = 1, archive_eligible_at = NULL, archive_idle_turn_id = NULL, updated_at = ? WHERE id = ?",
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
            if isinstance(error, (TollgateUncertainError, AppServerError))
            or (mutation and not isinstance(error, TollgateError))
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
            if payload.get("command") == "status":
                # Status is a synchronous projection of the authoritative store.
                # It cannot interleave with another coroutine while its queries
                # run, and must remain observable while a native effect is
                # awaiting completion under mutation_lock.
                result = await self.handle_request(payload)
            else:
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

    @staticmethod
    def _handoff_identity(value: os.stat_result) -> dict[str, int]:
        return {
            "device": value.st_dev,
            "inode": value.st_ino,
            "size": value.st_size,
            "mtime_ns": value.st_mtime_ns,
            "ctime_ns": value.st_ctime_ns,
        }

    @staticmethod
    def _same_handoff_file(observed: dict[str, int], accepted: dict[str, int]) -> bool:
        # Rename may update ctime; device/inode bind the file and size/mtime bind
        # the accepted snapshot.
        return all(
            observed[key] == accepted[key]
            for key in ("device", "inode", "size", "mtime_ns")
        )

    def _expected_handoff_path(self, action: dict[str, Any], outcome_kind: str) -> Path:
        filename = STRUCTURED_OUTCOME_FILENAMES.get(outcome_kind)
        if filename is None or outcome_kind not in ALLOWED.get(
            str(action["kind"]), set()
        ):
            raise StoreError(
                f"{outcome_kind!r} is not a structured outcome for this action"
            )
        return self.paths.handoff_path(
            self.store.state_identity(), int(action["id"]), filename
        )

    def _read_bound_handoff(
        self,
        expected: Path,
        options: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, dict[str, int] | None]:
        supplied = options.get("input_path")
        if not isinstance(supplied, str) or not Path(supplied).is_absolute():
            raise StoreError("structured finish requires canonical absolute input_path")
        if supplied != str(expected):
            raise StoreError(
                f"structured finish input path does not match this action: expected {expected}"
            )
        if Path(supplied).resolve(strict=False) != expected:
            raise StoreError("structured finish input path is not canonical")
        if options.get("input_missing") is True:
            if "input" in options or "input_identity" in options:
                raise StoreError("missing-file retry must not include input contents")
            if expected.exists() or expected.is_symlink():
                raise StoreError(
                    "structured finish input appeared after a missing-file retry"
                )
            return None, None
        claimed = options.get("input_identity")
        snapshot = options.get("input")
        if not isinstance(claimed, dict) or not isinstance(snapshot, dict):
            raise StoreError(
                "structured finish requires input payload and file identity"
            )
        identity_keys = {"device", "inode", "size", "mtime_ns", "ctime_ns"}
        if set(claimed) != identity_keys or any(
            not isinstance(claimed[key], int) or isinstance(claimed[key], bool)
            for key in identity_keys
        ):
            raise StoreError("structured finish file identity is malformed")
        try:
            path_status = expected.lstat()
        except OSError as error:
            raise StoreError(
                f"cannot inspect structured finish input: {error}"
            ) from error
        if stat.S_ISLNK(path_status.st_mode) or not stat.S_ISREG(path_status.st_mode):
            raise StoreError(
                "structured finish input must be a non-symlink regular file"
            )
        if self._handoff_identity(path_status) != claimed:
            raise StoreError(
                "structured finish input identity changed before acceptance"
            )
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(expected, flags)
            before = os.fstat(descriptor)
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = None
                raw = handle.read()
                after = os.fstat(handle.fileno())
        except OSError as error:
            raise StoreError(f"cannot read structured finish input: {error}") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
        before_identity = self._handoff_identity(before)
        after_identity = self._handoff_identity(after)
        if before_identity != claimed or after_identity != claimed:
            raise StoreError("structured finish input changed while being read")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise StoreError(
                f"structured finish input is not valid JSON: {error}"
            ) from error
        if not isinstance(payload, dict):
            raise StoreError("structured finish input must contain a JSON object")
        if payload != snapshot:
            raise StoreError(
                "structured finish payload does not match the file snapshot"
            )
        return payload, after_identity

    @staticmethod
    def _accepted_finish_options(action: dict[str, Any]) -> dict[str, Any]:
        payload = json.loads(action["outcome_payload"] or "{}")
        if action["outcome_kind"] == "deferred":
            return {"reason": payload["reason"], "input": payload["reactivation"]}
        return {"input": payload}

    @staticmethod
    def _validate_missing_retry_options(
        action: dict[str, Any], options: dict[str, Any]
    ) -> None:
        if action["outcome_kind"] == "deferred" and options.get("reason") != json.loads(
            action["outcome_payload"] or "{}"
        ).get("reason"):
            raise StoreError(
                "structured finish retry reason differs from accepted outcome"
            )

    def _cleanup_handoff(
        self, action: dict[str, Any], path: Path, identity: dict[str, int]
    ) -> dict[str, Any]:
        quarantine_root: Path | None = None
        quarantine: Path | None = None
        try:
            current = path.lstat()
            if stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode):
                raise StoreError("accepted handoff was replaced by a non-regular file")
            if self._handoff_identity(current) != identity:
                raise StoreError("accepted handoff identity changed before cleanup")
            for _attempt in range(10):
                candidate = path.with_name(f".cleanup-{uuid.uuid4().hex}")
                try:
                    candidate.mkdir(mode=0o700)
                except FileExistsError:
                    continue
                quarantine_root = candidate
                break
            if quarantine_root is None:
                raise StoreError("cannot allocate a private handoff quarantine")
            quarantine = quarantine_root / path.name
            os.rename(path, quarantine)
            moved = quarantine.lstat()
            if stat.S_ISLNK(moved.st_mode) or not stat.S_ISREG(moved.st_mode):
                raise StoreError("quarantined handoff is not a regular file")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(quarantine, flags)
            try:
                moved_identity = self._handoff_identity(os.fstat(descriptor))
            finally:
                os.close(descriptor)
            if not self._same_handoff_file(
                self._handoff_identity(moved), identity
            ) or not self._same_handoff_file(moved_identity, identity):
                raise StoreError(
                    "handoff pathname changed before quarantine; retained without deletion"
                )
            quarantine.unlink()
        except Exception as error:
            diagnostic = str(error)[:500]
            retained = (
                quarantine
                if quarantine is not None
                and (quarantine.exists() or quarantine.is_symlink())
                else path
            )
            if (
                quarantine_root is not None
                and quarantine_root.is_dir()
                and (quarantine is None or not quarantine.exists())
            ):
                try:
                    quarantine_root.rmdir()
                except OSError:
                    pass
            self.store.event(
                "handoff_cleanup_failed",
                diagnostic,
                entity_type="action",
                entity_id=action["id"],
                detail={
                    "path": str(path),
                    "retained_path": str(retained),
                    "outcome": action["outcome_kind"],
                },
            )
            return {
                "removed": False,
                "error": diagnostic,
                "retained_path": str(retained),
            }
        directory_errors: list[str] = []
        assert quarantine_root is not None
        try:
            quarantine_root.rmdir()
        except OSError as error:
            directory_errors.append(str(error)[:250])
        for directory in (path.parent, path.parent.parent):
            try:
                directory.rmdir()
            except OSError as error:
                if error.errno != errno.ENOTEMPTY:
                    directory_errors.append(str(error)[:250])
        result: dict[str, Any] = {"removed": True}
        if directory_errors:
            result["directory_cleanup_errors"] = directory_errors
        self.store.event(
            "handoff_removed",
            "removed accepted structured finish input",
            entity_type="action",
            entity_id=action["id"],
            detail={"path": str(path), "outcome": action["outcome_kind"]},
        )
        return result

    def _record_handoff_cleanup(self, action_id: int, cleanup: dict[str, Any]) -> None:
        self.store.execute(
            "UPDATE actions SET outcome_input_cleanup = ?, updated_at = ? WHERE id = ?",
            (
                json.dumps(cleanup, sort_keys=True, separators=(",", ":")),
                utc_now(),
                action_id,
            ),
        )

    @staticmethod
    def _retained_handoff_cleanup(action: dict[str, Any]) -> dict[str, Any]:
        encoded = action.get("outcome_input_cleanup")
        if isinstance(encoded, str):
            cleanup = json.loads(encoded)
            if isinstance(cleanup, dict):
                return cleanup
        return {"removed": True, "already_missing": True}

    def _processed_handoff_retry(
        self,
        native_thread_id: str,
        outcome_kind: str,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        supplied = options.get("input_path")
        if not isinstance(supplied, str):
            raise StoreError("thread has no current Fulcrum action")
        action = self.store.row(
            """SELECT a.*, t.native_thread_id, t.role, t.title
               FROM actions a JOIN tasks t ON t.id = a.task_id
               WHERE t.native_thread_id = ? AND a.state = 'processed'
                 AND a.outcome_kind = ? AND a.outcome_input_path = ?
               ORDER BY a.id DESC LIMIT 1""",
            (native_thread_id, outcome_kind, supplied),
        )
        if action is None:
            raise StoreError("no accepted action matches this structured finish retry")
        expected = self._expected_handoff_path(action, outcome_kind)
        payload, identity = self._read_bound_handoff(expected, options)
        if payload is None:
            self._validate_missing_retry_options(action, options)
            return {
                "ok": True,
                "action_id": action["id"],
                "outcome": outcome_kind,
                "reused": True,
                "cleanup": self._retained_handoff_cleanup(action),
            }
        submitted = dict(options)
        submitted["input"] = payload
        validated = validate_outcome(str(action["kind"]), outcome_kind, submitted)
        encoded = json.dumps(validated, sort_keys=True, separators=(",", ":"))
        if encoded != action["outcome_payload"]:
            raise StoreError(
                "structured finish retry payload differs from accepted outcome"
            )
        assert identity is not None
        result = {
            "ok": True,
            "action_id": action["id"],
            "outcome": outcome_kind,
            "reused": True,
        }
        cleanup = self._cleanup_handoff(action, expected, identity)
        self._record_handoff_cleanup(int(action["id"]), cleanup)
        result["cleanup"] = cleanup
        return result

    def _accept_finish_request(
        self,
        native_thread_id: str,
        outcome_kind: str,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            action = self.store.current_action(native_thread_id)
        except StoreError:
            if outcome_kind in STRUCTURED_OUTCOME_FILENAMES:
                return self._processed_handoff_retry(
                    native_thread_id, outcome_kind, options
                )
            raise
        if outcome_kind not in STRUCTURED_OUTCOME_FILENAMES:
            return accept_finish(
                self.store,
                native_thread_id=native_thread_id,
                outcome_kind=outcome_kind,
                options=options,
            )
        expected = self._expected_handoff_path(action, outcome_kind)
        payload, identity = self._read_bound_handoff(expected, options)
        if payload is None:
            if action["outcome_kind"] != outcome_kind or action.get(
                "outcome_input_path"
            ) != str(expected):
                raise StoreError("structured finish input is missing before acceptance")
            self._validate_missing_retry_options(action, options)
            submitted = self._accepted_finish_options(action)
            stored_identity = json.loads(action["outcome_input_identity"] or "null")
            result = accept_finish(
                self.store,
                native_thread_id=native_thread_id,
                outcome_kind=outcome_kind,
                options=submitted,
                input_path=str(expected),
                input_identity=stored_identity,
            )
            result["cleanup"] = self._retained_handoff_cleanup(action)
            return result
        submitted = dict(options)
        submitted["input"] = payload
        assert identity is not None
        result = accept_finish(
            self.store,
            native_thread_id=native_thread_id,
            outcome_kind=outcome_kind,
            options=submitted,
            input_path=str(expected),
            input_identity=identity,
        )
        accepted = self.store.row("SELECT * FROM actions WHERE id = ?", (action["id"],))
        assert accepted is not None
        cleanup = self._cleanup_handoff(accepted, expected, identity)
        self._record_handoff_cleanup(int(action["id"]), cleanup)
        result["cleanup"] = cleanup
        return result

    async def handle_request(self, request: dict[str, Any]) -> dict[str, Any]:
        command = request.get("command")
        self._enforce_operative_operation_matrix(request)
        if command == "status":
            status = self.store.status(event_limit=int(request.get("events", 20)))
            status["operative_journal_state"] = (
                self.operative_journal.get("state")
                if self.operative_journal is not None
                else None
            )
            status["operative_journal_error"] = self.operative_journal_error
            run_id = request.get("run")
            if isinstance(run_id, int):
                status["runs"] = [row for row in status["runs"] if row["id"] == run_id]
                status["assignments"] = [
                    row for row in status["assignments"] if row["run_id"] == run_id
                ]
            if request.get("view") == "queue":
                return {
                    "dispatch_enabled": status["dispatch_enabled"],
                    "operative_takeover": status["operative_takeover"],
                    "operative_journal_state": status["operative_journal_state"],
                    "resource_admission_condition": status[
                        "resource_admission_condition"
                    ],
                    "assignments": status["assignments"],
                    "pending_updates": status["pending_updates"],
                    "holds": status["holds"],
                }
            if request.get("view") == "capabilities":
                return {
                    "controller_state": status["controller_state"],
                    "dispatch_enabled": status["dispatch_enabled"],
                    "operative_takeover": status["operative_takeover"],
                    "operative_journal_state": status["operative_journal_state"],
                    "app_server_resources": status["app_server_resources"],
                    "resource_admission_condition": status[
                        "resource_admission_condition"
                    ],
                    "projects": status["projects"],
                    "slot_usage": status["slot_usage"],
                    "policies": status["policies"],
                }
            return status
        if command == "usage":
            return self.store.usage_report(
                action_id=_optional_int(request.get("action_id")),
                task_id=_optional_int(request.get("task_id")),
                assignment_id=_optional_int(request.get("assignment_id")),
                run_id=_optional_int(request.get("run_id")),
                role=(
                    request.get("role")
                    if isinstance(request.get("role"), str)
                    else None
                ),
                project_id=(
                    request.get("project_id")
                    if isinstance(request.get("project_id"), str)
                    else None
                ),
                group_by=str(request.get("group_by") or "action"),
            )
        if command == "cost":
            return self.store.cost_report(
                action_id=_optional_int(request.get("action_id")),
                task_id=_optional_int(request.get("task_id")),
                assignment_id=_optional_int(request.get("assignment_id")),
                run_id=_optional_int(request.get("run_id")),
                role=(
                    request.get("role")
                    if isinstance(request.get("role"), str)
                    else None
                ),
                project_id=(
                    request.get("project_id")
                    if isinstance(request.get("project_id"), str)
                    else None
                ),
                workflow_id=(
                    request.get("workflow_id")
                    if isinstance(request.get("workflow_id"), str)
                    else None
                ),
                group_by=str(request.get("group_by") or "action"),
            )
        if command == "context":
            thread_id = _thread_identity(request)
            action = self.store.current_action(thread_id)
            task = self.store.row(
                "SELECT * FROM tasks WHERE id = ?", (action["task_id"],)
            )
            if task is None:
                raise StoreError("current action task is missing")
            assignment = None
            if action.get("assignment_id") is not None:
                assignment = self.store.row(
                    """SELECT a.*, r.project_id FROM assignments a
                       JOIN runs r ON r.id = a.run_id WHERE a.id = ?""",
                    (action["assignment_id"],),
                )
                if assignment is None:
                    raise StoreError("current action assignment is missing")
            return {
                "action_id": action["id"],
                "action_kind": action["kind"],
                "role": task["role"],
                "context": self._build_action_message(
                    action, task, assignment, full_context=True
                ),
            }
        if command == "finish":
            return self._accept_finish_request(
                _thread_identity(request),
                str(request.get("outcome")),
                (
                    request.get("options")
                    if isinstance(request.get("options"), dict)
                    else {}
                ),
            )
        if command == "resolve_operation":
            decision = request.get("decision")
            if not isinstance(decision, dict):
                raise StoreError("resolve_operation requires a decision object")
            decision = dict(decision)
            decision["decision"] = "resolve_operation"
            operative_authorized = bool(
                self._operative_fenced()
                and isinstance(request.get("thread_id"), str)
                and self._is_bound_operative(str(request["thread_id"]))
            )
            bypass = None
            if operative_authorized:
                journal = self._require_bound_operative(_thread_identity(request))
                bypass = self.store.create_operative_operation(
                    str(journal["takeover_id"]),
                    "resolve_external_operation",
                    str(decision.get("operation_id")),
                    {
                        "operation": self.store.row(
                            "SELECT * FROM external_operations WHERE id = ?",
                            (decision.get("operation_id"),),
                        ),
                        "decision": decision,
                    },
                    correlation_id=(
                        f"operative-resolve:{journal['takeover_id']}:"
                        f"{decision.get('operation_id')}:{decision.get('resolution')}"
                    ),
                )
                if bypass["state"] == "complete":
                    retained_result = json.loads(bypass["result_json"] or "{}")
                    if not isinstance(retained_result, dict):
                        raise StoreError(
                            "retained operative operation result is invalid"
                        )
                    return retained_result
                if bypass["state"] in {"sent", "uncertain", "failed"}:
                    raise StoreError(
                        f"operative operation {bypass['id']} is {bypass['state']}; "
                        "exact observation is required"
                    )
                self.store.mark_operative_operation_sent(int(bypass["id"]))
            try:
                result = apply_archon_decisions(
                    self.store,
                    {"decisions": [decision]},
                    operative_authorized=operative_authorized,
                )
            except Exception as error:
                if bypass is not None:
                    self.store.finish_operative_operation(
                        int(bypass["id"]),
                        state="failed",
                        result={"error": str(error)},
                        after={
                            "operation": self.store.row(
                                "SELECT * FROM external_operations WHERE id = ?",
                                (decision.get("operation_id"),),
                            )
                        },
                    )
                raise
            if decision.get("resolution") == "observed_success":
                operation = self.store.row(
                    "SELECT kind, target FROM external_operations WHERE id = ?",
                    (decision.get("operation_id"),),
                )
                if operation is not None and operation["kind"] == "turn_start":
                    action = self.store.row(
                        """SELECT a.id, a.native_turn_id, t.native_thread_id
                           FROM actions a JOIN tasks t ON t.id = a.task_id
                           WHERE a.id = ?""",
                        (int(operation["target"]),),
                    )
                    if action is not None and isinstance(action["native_turn_id"], str):
                        self.store.bind_action_turn(
                            int(action["id"]),
                            str(action["native_thread_id"]),
                            str(action["native_turn_id"]),
                        )
            if bypass is not None:
                self.store.finish_operative_operation(
                    int(bypass["id"]),
                    state="complete",
                    result=result,
                    after={
                        "operation": self.store.row(
                            "SELECT * FROM external_operations WHERE id = ?",
                            (decision.get("operation_id"),),
                        )
                    },
                )
            self._update_readiness()
            return result
        if command == "intake":
            weaver = self._require_registered_weaver(request.get("thread_id"))
            payload = request.get("task")
            if not isinstance(payload, dict):
                raise StoreError("intake requires a task object")
            if not payload.get("project"):
                payload["project"] = self._infer_project(request.get("thread_id"))
            result = await asyncio.to_thread(
                file_task,
                self.store,
                self.beads,
                task_from_payload(payload, intake_key=request.get("intake_key")),
                weaver_task_id=int(weaver["id"]) if weaver else None,
            )
            self._link_weaver_intake_workflow(
                request.get("thread_id"), [result["bead_id"]]
            )
            return result
        if command == "intake_graph":
            weaver = self._require_registered_weaver(request.get("thread_id"))
            graph = request.get("graph")
            if not isinstance(graph, dict):
                raise StoreError("graph intake requires an object")
            if not graph.get("project"):
                graph["project"] = self._infer_project(request.get("thread_id"))
            result = await asyncio.to_thread(
                file_graph,
                self.store,
                self.beads,
                graph,
                group_id=request.get("intake_key"),
                weaver_task_id=int(weaver["id"]) if weaver else None,
            )
            self._link_weaver_intake_workflow(
                request.get("thread_id"), list(result.get("bead_ids", []))
            )
            return result
        if command == "report":
            payload = request.get("report")
            if not isinstance(payload, dict):
                raise StoreError("report requires an object")
            provenance = self._report_provenance(request.get("thread_id"))
            raw_project = payload.get("project")
            if raw_project is not None and (
                not isinstance(raw_project, str) or not raw_project.strip()
            ):
                raise StoreError("report project must be a nonempty string")
            project = (
                raw_project.strip()
                if isinstance(raw_project, str)
                else self._infer_project(
                    request.get("thread_id"),
                    guidance="include project in the report input",
                )
            )
            if (
                self.store.row(
                    "SELECT 1 FROM projects WHERE project_id = ? AND enabled = 1",
                    (project,),
                )
                is None
            ):
                raise StoreError(f"unknown or disabled report project {project!r}")
            result = await asyncio.to_thread(
                file_report,
                self.store,
                self.beads,
                report_task_from_payload(
                    payload, project=project, provenance=provenance
                ),
            )
            return result
        if command == "weaver_register":
            return await self._register_weaver(request)
        if command == "sage_register":
            return await self._register_sage(request)
        if command == "operative_register":
            return await self._register_operative(request)
        if command == "operative_status":
            return self._operative_status(request)
        if command == "operative_dossier":
            self._require_bound_operative(_thread_identity(request))
            return await self._build_operative_dossier()
        if command == "operative_wind_down":
            return await self._wind_down_operative_targets(request)
        if command == "operative_reconcile":
            return await self._operative_reconcile_control(request)
        if command == "operative_worktree":
            return self._operative_worktree_control(request)
        if command == "operative_repair_check":
            return self._operative_repair_check(request)
        if command == "operative_reinstall":
            return self._operative_reinstall(request)
        if command == "operative_service_check":
            return self._operative_service_check(request)
        if command == "operative_finish":
            return await self._request_operative_finish(request)
        if command == "operative_abort":
            return self._abort_operative(request)
        if command == "operative_recover":
            return await self._recover_operative(request)
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

    def _enforce_operative_operation_matrix(self, request: dict[str, Any]) -> None:
        if not self._operative_fenced():
            return
        command = str(request.get("command") or "")
        if (
            command in OPERATIVE_PASSIVE_COMMANDS
            or command in OPERATIVE_CONTROL_COMMANDS
        ):
            return
        # A late ordinary finish is admitted only far enough for accept_finish to
        # quarantine its exact payload as evidence before rejecting it.
        if command == "finish":
            return
        thread_id = request.get("thread_id")
        if (
            command == "resolve_operation"
            and isinstance(thread_id, str)
            and self._is_bound_operative(thread_id)
        ):
            return
        raise StoreError("operative takeover active")

    def _is_bound_operative(self, thread_id: str) -> bool:
        journal = self.operative_journal
        if journal is None or journal.get("native_thread_id") != thread_id:
            return False
        bound = self.store.row(
            """SELECT 1 FROM tasks t JOIN actions a ON a.task_id = t.id
               WHERE t.native_thread_id = ? AND t.role = 'operative'
                 AND a.kind = 'operative'
                 AND a.state IN ('pending','starting','active','terminal','uncertain')""",
            (thread_id,),
        )
        return bound is not None

    def _require_bound_operative(self, thread_id: str) -> dict[str, Any]:
        if not self._is_bound_operative(thread_id):
            raise StoreError("current thread is not the bound Operative")
        assert self.operative_journal is not None
        return self.operative_journal

    def _require_registered_weaver(self, thread_id: object) -> dict[str, Any] | None:
        if not isinstance(thread_id, str):
            return None
        authorized = self.store.row(
            """SELECT t.* FROM tasks t JOIN actions a ON a.task_id = t.id
               WHERE t.native_thread_id = ? AND t.role = 'weaver'
               AND a.kind = 'weaver'
               AND a.state IN ('pending','starting','active','terminal','uncertain')""",
            (thread_id,),
        )
        if authorized is None:
            raise StoreError(
                "Codex task intake requires `fulcrum weaver register` first"
            )
        return authorized

    def _report_provenance(self, thread_id: object) -> dict[str, Any]:
        """Permit unmanaged reports and post-finish managed session reports."""

        if not isinstance(thread_id, str):
            return {"source": "unregistered-codex-task"}
        task = self.store.row(
            "SELECT * FROM tasks WHERE native_thread_id = ?", (thread_id,)
        )
        if task is None:
            return {
                "source": "unregistered-codex-task",
                "source_thread_id": thread_id,
            }
        action = self.store.row(
            "SELECT * FROM actions WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task["id"],),
        )
        if action is None or action["outcome_kind"] is None:
            raise StoreError(
                "managed-session follow-up reports are allowed only after this "
                "action has an accepted `fulcrum finish` outcome; finish the "
                "assigned work first, then retry the unchanged report"
            )
        provenance: dict[str, Any] = {
            "source": "managed-fulcrum-session",
            "source_thread_id": thread_id,
            "source_task_id": int(task["id"]),
            "source_role": str(task["role"]),
            "source_action_id": int(action["id"]),
            "source_outcome": str(action["outcome_kind"]),
        }
        if action["assignment_id"] is not None:
            assignment = self.store.row(
                "SELECT id, run_id, bead_id FROM assignments WHERE id = ?",
                (action["assignment_id"],),
            )
            if assignment is not None:
                provenance.update(
                    {
                        "source_assignment_id": int(assignment["id"]),
                        "source_run_id": int(assignment["run_id"]),
                        "source_bead_id": str(assignment["bead_id"]),
                    }
                )
        return provenance

    def _link_weaver_intake_workflow(
        self, thread_id: object, bead_ids: list[str]
    ) -> None:
        if not isinstance(thread_id, str) or not bead_ids:
            return
        action = self.store.row(
            """SELECT a.id FROM actions a JOIN tasks t ON t.id = a.task_id
               WHERE t.native_thread_id = ? AND a.kind = 'weaver'
               ORDER BY a.id DESC LIMIT 1""",
            (thread_id,),
        )
        if action is None:
            return
        workflow_id = f"weaver-action:{action['id']}"
        self.store.link_action_to_workflow(
            workflow_id,
            int(action["id"]),
            causal_role="weaver_intake",
            origin_action_id=int(action["id"]),
        )
        for bead_id in bead_ids:
            self.store.link_bead_to_workflow(workflow_id, bead_id)

    def _infer_project(
        self, thread_id: object, *, guidance: str = "pass --project"
    ) -> str:
        if isinstance(thread_id, str):
            task = self.store.row(
                "SELECT project_id FROM tasks WHERE native_thread_id = ?", (thread_id,)
            )
            if task and task["project_id"]:
                return str(task["project_id"])
        projects = self.store.rows("SELECT project_id FROM projects WHERE enabled = 1")
        if len(projects) == 1:
            return str(projects[0]["project_id"])
        raise StoreError(f"project is ambiguous; {guidance}")

    async def _register_weaver(self, request: dict[str, Any]) -> dict[str, Any]:
        thread_id = _thread_identity(request)
        raw_description = request.get("description")
        if not isinstance(raw_description, str) or not raw_description.strip():
            raise StoreError("weaver registration requires a nonempty description")
        description = raw_description.strip()
        project_id = request.get("project") or self._infer_project(thread_id)
        task = self.store.register_task(
            native_thread_id=thread_id,
            role="weaver",
            description=description,
            model=str(request.get("model") or "gpt-5.6-sol"),
            reasoning_effort=str(request.get("effort") or "high"),
            project_id=project_id,
            state="provisioning",
        )
        await self.runtime.set_name(thread_id, task["title"])
        facts = await self._refresh_task(task)
        existing: dict[str, Any] | None = None
        if request.get("writable", True):
            existing = self.store.row(
                """SELECT * FROM actions WHERE task_id = ?
                   AND state IN ('pending','starting','active','terminal','uncertain')""",
                (task["id"],),
            )
            if existing is None:
                timestamp = utc_now()
                payload = {
                    "mode": "intake",
                    "project": project_id,
                    "adopt_current_turn": bool(
                        facts["runtime_status"] == "active"
                        and facts["last_turn_id"] is None
                    ),
                }
                cursor = self.store.execute(
                    """INSERT INTO actions(
                           task_id, kind, payload, state, native_turn_id,
                           created_at, updated_at
                       ) VALUES (?, 'weaver', ?, 'active', ?, ?, ?)""",
                    (
                        task["id"],
                        json.dumps(payload),
                        facts["last_turn_id"],
                        timestamp,
                        timestamp,
                    ),
                )
                existing = self.store.row(
                    "SELECT * FROM actions WHERE id = ?", (cursor.lastrowid,)
                )
                if isinstance(facts["last_turn_id"], str):
                    self.store.bind_action_turn(
                        int(cursor.lastrowid), thread_id, facts["last_turn_id"]
                    )
            elif existing["native_turn_id"] is None:
                self._adopt_unbound_weaver_turn(int(task["id"]), facts)
        self.store.execute(
            "UPDATE tasks SET state = 'active', archive_eligible_at = NULL, archive_idle_turn_id = NULL, updated_at = ? WHERE id = ?",
            (utc_now(), task["id"]),
        )
        self.store.execute(
            """UPDATE obligations SET state = 'canceled',
               detail = 'new Weaver turn retained the lineage conversation', updated_at = ?
               WHERE kind = 'archive' AND target = ? AND state IN ('pending','failed')""",
            (utc_now(), thread_id),
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

    def _save_operative_journal(self, **changes: Any) -> dict[str, Any]:
        journal = self.operative_journal
        if journal is None:
            raise StoreError("operative takeover state is unavailable")
        updated = dict(journal)
        updated.update(changes)
        updated["updated_at"] = utc_now()
        write_journal(self.paths.operative_journal, updated)
        self.store.mirror_operative_journal(updated)
        self.operative_journal = updated
        return updated

    def _operative_registration_result(
        self, task: dict[str, Any], *, reused: bool
    ) -> dict[str, Any]:
        journal = self.operative_journal
        assert journal is not None
        return {
            "takeover_id": journal["takeover_id"],
            "state": journal["state"],
            "thread_id": task["native_thread_id"],
            "title": task["title"],
            "role_number": task["role_number"],
            "dispatch_enabled": False,
            "reused": reused,
            "instructions": operative_instructions(),
            "dossier": journal.get("dossier"),
        }

    def _provisional_operative_registration(
        self, error: AppServerError, *, reused: bool
    ) -> dict[str, Any]:
        """Retain a fenced acquisition when exact caller verification is offline."""

        journal = self.operative_journal
        if journal is None or journal.get("state") != "acquiring":
            raise StoreError("operative acquisition is not resumable") from error
        thread_id = str(journal["native_thread_id"])
        task = self.store.row(
            "SELECT * FROM tasks WHERE native_thread_id = ?", (thread_id,)
        )
        if task is not None and task["role"] != "operative":
            raise StoreError("provisional operative identity is unavailable") from error
        retained_verification = journal.get("caller_verification")
        if (
            isinstance(retained_verification, dict)
            and retained_verification.get("coverage") == "observed"
            and retained_verification.get("method") == "exact App Server thread/read"
            and retained_verification.get("native_thread_id") == thread_id
        ):
            verification = retained_verification
        else:
            verification = {
                "coverage": "unavailable",
                "method": "controller-retained environment identity pending exact App Server thread/read",
                "native_thread_id": thread_id,
                "managed_agent": False,
                "reason": str(error),
            }
        effects = list(journal.get("completed_effects") or [])
        if "app_server_verification_deferred" not in effects:
            effects.append("app_server_verification_deferred")
        retained = self._save_operative_journal(
            caller_verification=verification,
            completed_effects=effects,
            next_step="repair service/store, then run reconcile",
        )
        result = {
            "takeover_id": retained["takeover_id"],
            "state": "acquiring",
            "thread_id": thread_id,
            "dispatch_enabled": False,
            "reused": reused,
            "authority": "provisional_local_repair",
            "caller_verification": verification,
            "database": {
                "coverage": "observed",
                "state": "healthy",
                "path": str(self.paths.database),
            },
            "quarantine": None,
            "next_step": retained["next_step"],
            "instructions": operative_instructions(),
            "dossier": retained.get("dossier"),
        }
        if task is not None:
            result.update(title=task["title"], role_number=task["role_number"])
        return result

    async def _resume_operative_registration(
        self, request: dict[str, Any], *, reused: bool
    ) -> dict[str, Any]:
        try:
            return await self._resume_operative_acquisition(request)
        except AppServerError as error:
            return self._provisional_operative_registration(error, reused=reused)

    async def _register_operative(self, request: dict[str, Any]) -> dict[str, Any]:
        """Acquire installation-wide authority from a human-created native turn."""

        if self.operative_journal_error is not None:
            raise StoreError(self.operative_journal_error)
        thread_id = _thread_identity(request)
        raw_description = request.get("description")
        if not isinstance(raw_description, str) or not raw_description.strip():
            raise StoreError("operative registration requires a nonempty description")
        existing_task = self.store.row(
            "SELECT * FROM tasks WHERE native_thread_id = ?", (thread_id,)
        )
        if existing_task is not None and existing_task["role"] != "operative":
            raise StoreError("managed agents cannot register as Operative")

        journal: dict[str, Any] | None = self.operative_journal
        if journal_is_unfinished(journal):
            assert journal is not None
            if journal["native_thread_id"] != thread_id:
                raise StoreError("operative takeover active")
            if journal["state"] == "aborted":
                raise StoreError(
                    "aborted operative takeover requires explicit recovery"
                )
            task = existing_task
            if task is None:
                task = self.store.row(
                    "SELECT * FROM tasks WHERE id = ?", (journal.get("task_id"),)
                )
            if task is None:
                return await self._resume_operative_registration(request, reused=True)
            if journal["state"] == "acquiring":
                return await self._resume_operative_registration(request, reused=True)
            return self._operative_registration_result(task, reused=True)
        if existing_task is not None:
            raise StoreError(
                "a closed Operative thread cannot acquire a new takeover; use a new human task"
            )

        try:
            with authority_gate(self.paths.authority_lock, blocking=False):
                try:
                    disk_journal = read_journal(self.paths.operative_journal)
                except StoreError as error:
                    self.operative_journal_error = str(error)
                    raise
                if journal_is_unfinished(disk_journal):
                    raise StoreError("operative takeover active")
                timestamp = utc_now()
                dispatch = self.store.row(
                    "SELECT value FROM meta WHERE key = 'dispatch_enabled'"
                )
                prior_dispatch = bool(dispatch is not None and dispatch["value"] == "1")
                journal = {
                    "takeover_id": str(uuid.uuid4()),
                    "state": "acquiring",
                    "native_thread_id": thread_id,
                    "superseded_thread_ids": [],
                    "scope": raw_description.strip(),
                    "prior_dispatch_enabled": prior_dispatch,
                    "completed_effects": ["authority_fence_written"],
                    "next_step": "mirror operative task and action",
                    "caller_verification": {
                        "coverage": "observed",
                        "method": "controller-bound current thread identity",
                        "native_thread_id": thread_id,
                        "managed_agent": False,
                    },
                    "created_at": timestamp,
                    "updated_at": timestamp,
                }
                write_journal(self.paths.operative_journal, journal)
                self.operative_journal = journal
                self.starts_enabled = False
                self.store.execute(
                    """INSERT INTO meta(key, value) VALUES ('dispatch_enabled', '0')
                       ON CONFLICT(key) DO UPDATE SET value = '0'"""
                )
                self.store.mirror_operative_journal(journal)
                self.store.event(
                    "operative_acquiring",
                    "operative authority fence acquired",
                    entity_type="operative_takeover",
                    entity_id=str(journal["takeover_id"]),
                    detail={"prior_dispatch_enabled": prior_dispatch},
                )
        except StoreError as error:
            if str(error) == "Fulcrum authority mutation is already in progress":
                raise StoreError("setup mutation active") from error
            raise
        return await self._resume_operative_registration(request, reused=False)

    async def _resume_operative_acquisition(
        self, request: dict[str, Any]
    ) -> dict[str, Any]:
        journal = self.operative_journal
        if journal is None or journal.get("state") != "acquiring":
            raise StoreError("operative acquisition is not resumable")
        thread_id = str(journal["native_thread_id"])
        thread = await self.runtime.read_thread(thread_id)
        if thread.get("id") != thread_id:
            raise AppServerError(
                "App Server returned a different native thread identity"
            )
        effects = list(journal.get("completed_effects") or [])
        if "exact_caller_verified" not in effects:
            effects.append("exact_caller_verified")
        journal = self._save_operative_journal(
            caller_verification={
                "coverage": "observed",
                "method": "exact App Server thread/read",
                "native_thread_id": thread_id,
                "managed_agent": False,
            },
            completed_effects=effects,
        )
        task = self.store.row(
            "SELECT * FROM tasks WHERE native_thread_id = ?", (thread_id,)
        )
        if task is not None and task["role"] != "operative":
            raise StoreError("managed agents cannot register as Operative")
        if task is None:
            task = self.store.register_task(
                native_thread_id=thread_id,
                role="operative",
                description=str(journal["scope"]),
                model=str(request.get("model") or "gpt-6-astra"),
                reasoning_effort=str(request.get("effort") or "high"),
                project_id=None,
                state="provisioning",
            )
        facts = thread_facts(thread)
        action = self.store.row(
            """SELECT * FROM actions WHERE task_id = ? AND kind = 'operative'
               AND state IN ('pending','starting','active','terminal','uncertain')""",
            (task["id"],),
        )
        if action is None:
            current_turn = (
                facts["last_turn_id"]
                if facts["runtime_status"] == "active"
                and facts["last_turn_status"] == "inProgress"
                else None
            )
            timestamp = utc_now()
            cursor = self.store.execute(
                """INSERT INTO actions(
                     task_id, kind, payload, state, native_turn_id, created_at, updated_at
                   ) VALUES (?, 'operative', ?, 'active', ?, ?, ?)""",
                (
                    task["id"],
                    json.dumps(
                        {
                            "takeover_id": journal["takeover_id"],
                            "scope": journal["scope"],
                            "adopt_current_turn": current_turn is None,
                        },
                        sort_keys=True,
                    ),
                    current_turn,
                    timestamp,
                    timestamp,
                ),
            )
            action = self.store.row(
                "SELECT * FROM actions WHERE id = ?", (cursor.lastrowid,)
            )
            assert action is not None
            if isinstance(current_turn, str):
                self.store.bind_action_turn(int(action["id"]), thread_id, current_turn)
        effects = list(journal["completed_effects"])
        if "sqlite_authority_mirrored" not in effects:
            effects.append("sqlite_authority_mirrored")
        journal = self._save_operative_journal(
            task_id=task["id"],
            action_id=action["id"],
            operative_identity={
                "role_number": task["role_number"],
                "title": task["title"],
                "model": task["model"],
                "reasoning_effort": task["reasoning_effort"],
            },
            operative_action={
                "payload": json.loads(action["payload"]),
                "native_turn_id": action["native_turn_id"],
            },
            completed_effects=effects,
            next_step="name and bind invoking native turn",
        )
        await self._ensure_operative_name(task, thread)
        facts = await self._observe_task(task)
        if action["native_turn_id"] is None:
            action = (
                self._adopt_unbound_operative_turn(int(task["id"]), facts) or action
            )
        effects = list(journal["completed_effects"])
        if "native_identity_bound" not in effects:
            effects.append("native_identity_bound")
        self._save_operative_journal(
            operative_action={
                "payload": json.loads(action["payload"]),
                "native_turn_id": action["native_turn_id"],
            },
            completed_effects=effects,
            next_step="reconcile in-flight observations and publish emergency dossier",
        )
        await self._reconcile_operative_mode()
        dossier = await self._build_operative_dossier()
        self.store.record_operative_evidence(
            str(journal["takeover_id"]),
            "initial-dossier",
            "dossier",
            coverage="observed",
            target_type="operative_takeover",
            target_id=journal["takeover_id"],
            detail=dossier,
        )
        activated = utc_now()
        active = transition_journal(
            self.paths.operative_journal,
            self.operative_journal or journal,
            "active",
            now=activated,
            next_step="operative resolves the stated emergency",
            completed_effect="initial_dossier_published",
            activated_at=activated,
            dossier={
                "coverage": "observed",
                "evidence_key": "initial-dossier",
            },
        )
        self.operative_journal = active
        self.store.mirror_operative_journal(active)
        self.store.execute(
            "UPDATE tasks SET state = 'active', updated_at = ? WHERE id = ?",
            (activated, task["id"]),
        )
        self.store.execute(
            """INSERT INTO meta(key, value) VALUES ('controller_state', 'operative_only')
               ON CONFLICT(key) DO UPDATE SET value = 'operative_only'"""
        )
        return self._operative_registration_result(task, reused=False)

    async def _ensure_operative_name(
        self, task: dict[str, Any], observed_thread: dict[str, Any]
    ) -> None:
        """Name the bound thread with exact durable send/observation recovery."""

        journal = self.operative_journal
        if journal is None:
            raise StoreError("operative takeover state is unavailable")
        takeover_id = str(journal["takeover_id"])
        target = str(task["native_thread_id"])
        desired = str(task["title"])
        current_name = observed_thread.get("name")
        prior = self.store.rows(
            """SELECT * FROM operative_operations
               WHERE takeover_id = ? AND kind = 'thread_name' AND target = ?
               ORDER BY id""",
            (takeover_id, target),
        )
        latest = prior[-1] if prior else None
        if latest is not None and latest["state"] == "complete":
            if current_name != desired:
                raise StoreError(
                    "completed operative naming effect contradicts current observation"
                )
            return
        if latest is not None and latest["state"] in {"sent", "uncertain"}:
            if current_name == desired:
                self.store.finish_operative_operation(
                    int(latest["id"]),
                    state="complete",
                    result={"reconciled": True},
                    after={"name": desired, "coverage": "observed"},
                )
                return
            self.store.finish_operative_operation(
                int(latest["id"]),
                state="failed",
                result={"reconciled": True, "observed_unsent": True},
                after={"name": current_name, "coverage": "observed"},
            )
        attempt = len(prior) + 1
        operation = self.store.create_operative_operation(
            takeover_id,
            "thread_name",
            target,
            {"name": current_name, "coverage": "observed"},
            correlation_id=f"operative-name:{takeover_id}:{target}:{attempt}",
        )
        if operation["state"] != "intent":
            raise StoreError("operative naming intent is not sendable")
        if current_name == desired:
            self.store.finish_operative_operation(
                int(operation["id"]),
                state="complete",
                result={"observed_preexisting": True, "sent": False},
                after={"name": desired, "coverage": "observed"},
            )
            return
        self.store.mark_operative_operation_sent(int(operation["id"]))
        try:
            await self.runtime.set_name(target, desired)
        except Exception as error:
            try:
                after_error = await self.runtime.read_thread(target)
            except Exception as observation_error:
                self.store.finish_operative_operation(
                    int(operation["id"]),
                    state="uncertain",
                    result={
                        "error": str(error),
                        "observation_error": str(observation_error),
                    },
                    after={"coverage": "unavailable"},
                )
            else:
                accepted = after_error.get("name") == desired
                self.store.finish_operative_operation(
                    int(operation["id"]),
                    state="complete" if accepted else "failed",
                    result={"error": str(error), "reconciled": True},
                    after={
                        "name": after_error.get("name"),
                        "coverage": "observed",
                    },
                )
            raise
        try:
            after = await self.runtime.read_thread(target)
        except Exception as error:
            self.store.finish_operative_operation(
                int(operation["id"]),
                state="uncertain",
                result={"accepted": True, "observation_error": str(error)},
                after={"coverage": "unavailable"},
            )
            raise
        if after.get("name") != desired:
            self.store.finish_operative_operation(
                int(operation["id"]),
                state="failed",
                result={"accepted": True, "observed_unsent": True},
                after={"name": after.get("name"), "coverage": "observed"},
            )
            raise StoreError("operative thread naming was not observed")
        self.store.finish_operative_operation(
            int(operation["id"]),
            state="complete",
            result={"accepted": True},
            after={"name": desired, "coverage": "observed"},
        )

    def _operative_status(self, request: dict[str, Any]) -> dict[str, Any]:
        journal = self._require_bound_operative(_thread_identity(request))
        mirror = self.store.operative_takeover(str(journal["takeover_id"]))
        return {
            "takeover_id": journal["takeover_id"],
            "state": journal["state"],
            "thread_id": journal["native_thread_id"],
            "scope": journal["scope"],
            "prior_dispatch_enabled": journal["prior_dispatch_enabled"],
            "next_step": journal["next_step"],
            "completed_effects": journal["completed_effects"],
            "sqlite_mirror": "observed" if mirror is not None else "unavailable",
            "dispatch_enabled": False,
        }

    @staticmethod
    def _operative_input(request: dict[str, Any]) -> dict[str, Any]:
        value = request.get("input")
        if not isinstance(value, dict):
            raise StoreError("operative control requires a JSON object input file")
        return value

    @staticmethod
    def _retained_operative_result(operation: dict[str, Any]) -> dict[str, Any]:
        try:
            value = json.loads(operation.get("result_json") or "{}")
        except json.JSONDecodeError as error:
            raise StoreError(
                "retained operative operation result is invalid"
            ) from error
        if not isinstance(value, dict):
            raise StoreError("retained operative operation result is invalid")
        return value

    async def _wind_down_operative_targets(
        self, request: dict[str, Any]
    ) -> dict[str, Any]:
        """Stop exact managed turns, prove helpers terminal, then retire them.

        The notice is deliberately a steer on the retained active turn.  It grants
        no action, asks for no finish, and is followed by an exact interrupt.
        """

        journal = self._require_bound_operative(_thread_identity(request))
        supplied = self._operative_input(request)
        targets = supplied.get("task_ids")
        notice = supplied.get("notice")
        operation_key = supplied.get("operation_key")
        dispositions = supplied.get("dispositions", {})
        if (
            not isinstance(targets, list)
            or not targets
            or not all(
                isinstance(item, int) and not isinstance(item, bool) for item in targets
            )
        ):
            raise StoreError("operative wind-down requires exact integer task_ids")
        if not isinstance(notice, str) or not notice.strip():
            raise StoreError("operative wind-down requires a nonempty notice")
        if not isinstance(operation_key, str) or not operation_key.strip():
            raise StoreError("operative wind-down requires a stable operation_key")
        if not isinstance(dispositions, dict):
            raise StoreError("operative wind-down dispositions must be an object")
        notice_text = (
            notice.strip()
            + "\n\nDo not perform more work, call finish, publish, deliver, clean a "
            "worktree, or send a follow-up. This is a no-follow-up emergency "
            "wind-down notice."
        )
        results: list[dict[str, Any]] = []
        blockers: list[str] = []
        takeover_id = str(journal["takeover_id"])
        for task_id in targets:
            task = self.store.row("SELECT * FROM tasks WHERE id = ?", (task_id,))
            if task is None:
                raise StoreError(f"unknown operative wind-down task {task_id}")
            if task["role"] == "operative":
                raise StoreError("the bound Operative cannot wind down itself")
            thread_id = str(task["native_thread_id"])
            if task["state"] in {"retired", "archived"} and task["archived"]:
                results.append(
                    {"task_id": task_id, "state": "archived", "reused": True}
                )
                continue
            try:
                thread = await self.runtime.read_thread(thread_id)
            except Exception as error:
                self.store.record_operative_evidence(
                    takeover_id,
                    f"wind-down-unavailable:{operation_key}:{task_id}",
                    "wind_down",
                    coverage="unavailable",
                    target_type="task",
                    target_id=task_id,
                    detail={"error": str(error)},
                    artifact_path=request.get("input_path"),
                )
                blockers.append(f"task {task_id} runtime state is unavailable: {error}")
                continue
            facts = thread_facts(thread)
            active_turn = (
                str(facts["last_turn_id"])
                if facts.get("runtime_status") == "active"
                and facts.get("last_turn_status") == "inProgress"
                and isinstance(facts.get("last_turn_id"), str)
                else None
            )
            idle_notice_correlation = (
                f"operative-wind-down:{takeover_id}:{operation_key}:"
                f"{task_id}:idle-notice"
            )
            prior_idle_notice = self.store.row(
                "SELECT * FROM operative_operations WHERE correlation_id = ?",
                (idle_notice_correlation,),
            )
            prior_notice_result = (
                self._retained_operative_result(prior_idle_notice)
                if prior_idle_notice is not None
                else {}
            )
            active_is_idle_notice = bool(
                active_turn is not None
                and prior_notice_result.get("turn_id") == active_turn
            )
            if active_turn is not None:
                steer_id: int | None = None
                if not active_is_idle_notice:
                    action = self.store.row(
                        """SELECT * FROM actions WHERE task_id = ?
                           AND state IN ('starting','active','terminal','uncertain')
                           ORDER BY id DESC LIMIT 1""",
                        (task_id,),
                    )
                    if action is None or action.get("native_turn_id") != active_turn:
                        blockers.append(
                            f"task {task_id} active turn contradicts the retained exact turn"
                        )
                        continue
                    steer_correlation = (
                        f"operative-wind-down:{takeover_id}:{operation_key}:"
                        f"{task_id}:steer:{active_turn}"
                    )
                    steer = self.store.create_operative_operation(
                        takeover_id,
                        "turn_steer",
                        f"{thread_id}:{active_turn}",
                        {"thread": thread, "notice": notice_text},
                        correlation_id=steer_correlation,
                    )
                    steer_id = int(steer["id"])
                    if steer["state"] == "intent":
                        self.store.mark_operative_operation_sent(steer_id)
                        try:
                            await self.runtime.steer(
                                thread_id,
                                active_turn,
                                notice_text,
                                correlation=steer_correlation,
                            )
                        except Exception as error:
                            self.store.finish_operative_operation(
                                steer_id,
                                state="uncertain",
                                result={
                                    "error": str(error),
                                    "accepted": "unavailable",
                                },
                                after={
                                    "turn_id": active_turn,
                                    "coverage": "unavailable",
                                },
                            )
                        else:
                            self.store.finish_operative_operation(
                                steer_id,
                                state="complete",
                                result={"accepted": True, "no_follow_up": True},
                                after={"turn_id": active_turn, "notice_sent": True},
                            )
                interrupt_correlation = (
                    f"operative-wind-down:{takeover_id}:{operation_key}:"
                    f"{task_id}:interrupt:{active_turn}"
                )
                interrupt = self.store.create_operative_operation(
                    takeover_id,
                    "turn_interrupt",
                    f"{thread_id}:{active_turn}",
                    {
                        "turn_id": active_turn,
                        "steer_operation_id": steer_id,
                        "idle_notification_turn": active_is_idle_notice,
                    },
                    correlation_id=interrupt_correlation,
                )
                if interrupt["state"] == "intent":
                    self.store.mark_operative_operation_sent(int(interrupt["id"]))
                    try:
                        await self.runtime.interrupt(thread_id, active_turn)
                    except Exception as error:
                        self.store.finish_operative_operation(
                            int(interrupt["id"]),
                            state="uncertain",
                            result={"error": str(error), "accepted": "unavailable"},
                            after={"turn_id": active_turn, "coverage": "unavailable"},
                        )
            else:
                idle_notice = self.store.create_operative_operation(
                    takeover_id,
                    "idle_notification_turn",
                    thread_id,
                    {
                        "thread": thread,
                        "notice": notice_text,
                        "normal_workflow_authority_created": False,
                    },
                    correlation_id=idle_notice_correlation,
                )
                if idle_notice["state"] == "intent":
                    project = self.store.row(
                        "SELECT * FROM projects WHERE project_id = ?",
                        (task.get("project_id"),),
                    )
                    cwd = (
                        str(project["repo_path"])
                        if project is not None
                        else self.config.source_root
                    )
                    self.store.mark_operative_operation_sent(int(idle_notice["id"]))
                    try:
                        notice_turn = await self.runtime.start_turn(
                            thread_id,
                            notice_text,
                            cwd=cwd,
                            workspace_root=cwd,
                            model=str(task["model"]),
                            effort=str(task["reasoning_effort"]),
                            correlation=idle_notice_correlation,
                        )
                    except Exception as error:
                        self.store.finish_operative_operation(
                            int(idle_notice["id"]),
                            state="uncertain",
                            result={"error": str(error)},
                            after={"coverage": "unavailable"},
                        )
                        blockers.append(
                            f"task {task_id} idle notification is uncertain: {error}"
                        )
                        continue
                    self.store.execute(
                        """UPDATE operative_operations SET result_json = ?,
                           updated_at = ? WHERE id = ?""",
                        (
                            json.dumps(
                                {
                                    "turn_id": notice_turn,
                                    "normal_workflow_authority_created": False,
                                },
                                sort_keys=True,
                            ),
                            utc_now(),
                            idle_notice["id"],
                        ),
                    )

            # An accepted interrupt is not proof.  This targeted read is the
            # authority for both the exact parent and its native helpers.
            observed = await self.runtime.read_thread(thread_id)
            after_facts = thread_facts(observed)
            helper_observations: list[dict[str, Any]] = []
            helpers_proven_terminal = True
            for helper in self.store.rows(
                """SELECT DISTINCT helper.native_thread_id
                   FROM telemetry_helper_threads AS helper
                   JOIN actions ON actions.id = helper.attributed_action_id
                   WHERE actions.task_id = ? ORDER BY helper.native_thread_id""",
                (task_id,),
            ):
                helper_thread_id = str(helper["native_thread_id"])
                try:
                    helper_thread = await self.runtime.read_thread(helper_thread_id)
                    helper_facts = thread_facts(helper_thread)
                except Exception as error:
                    helper_observations.append(
                        {
                            "thread_id": helper_thread_id,
                            "coverage": "unavailable",
                            "error": str(error),
                        }
                    )
                    helpers_proven_terminal = False
                    continue
                helper_observations.append(
                    {
                        "thread_id": helper_thread_id,
                        "coverage": "observed",
                        "facts": helper_facts,
                    }
                )
                helpers_proven_terminal = helpers_proven_terminal and bool(
                    helper_facts["last_turn_terminal"]
                    and helper_facts["helpers_terminal"]
                )
            if not (
                after_facts["last_turn_terminal"]
                and after_facts["helpers_terminal"]
                and helpers_proven_terminal
            ):
                blockers.append(
                    f"task {task_id} parent turn or native helpers are not terminal"
                )
                self.store.record_operative_evidence(
                    takeover_id,
                    f"wind-down-blocked:{operation_key}:{task_id}",
                    "wind_down",
                    coverage="observed",
                    target_type="task",
                    target_id=task_id,
                    detail={
                        "thread": observed,
                        "facts": after_facts,
                        "helper_threads": helper_observations,
                    },
                    artifact_path=request.get("input_path"),
                )
                continue
            if active_turn is None:
                idle_notice = self.store.row(
                    "SELECT * FROM operative_operations WHERE correlation_id = ?",
                    (
                        f"operative-wind-down:{takeover_id}:{operation_key}:"
                        f"{task_id}:idle-notice",
                    ),
                )
                if idle_notice is not None and idle_notice["state"] != "complete":
                    self.store.finish_operative_operation(
                        int(idle_notice["id"]),
                        state="complete",
                        result={
                            **self._retained_operative_result(idle_notice),
                            "terminal_observed": True,
                            "normal_workflow_authority_created": False,
                        },
                        after={
                            "thread": observed,
                            "facts": after_facts,
                            "helper_threads": helper_observations,
                        },
                    )
            if active_turn is not None:
                interrupt = self.store.row(
                    "SELECT * FROM operative_operations WHERE correlation_id = ?",
                    (
                        f"operative-wind-down:{takeover_id}:{operation_key}:"
                        f"{task_id}:interrupt:{active_turn}",
                    ),
                )
                if interrupt is not None and interrupt["state"] != "complete":
                    self.store.finish_operative_operation(
                        int(interrupt["id"]),
                        state="complete",
                        result={"terminal_observed": True},
                        after={
                            "thread": observed,
                            "facts": after_facts,
                            "helper_threads": helper_observations,
                        },
                    )
            archive_correlation = (
                f"operative-wind-down:{takeover_id}:{operation_key}:"
                f"{task_id}:archive"
            )
            archive = self.store.create_operative_operation(
                takeover_id,
                "thread_archive",
                thread_id,
                {
                    "thread": observed,
                    "facts": after_facts,
                    "helper_threads": helper_observations,
                },
                correlation_id=archive_correlation,
            )
            if archive["state"] == "intent":
                self.store.mark_operative_operation_sent(int(archive["id"]))
                try:
                    await self.runtime.archive(thread_id)
                    archived = await self.runtime.read_thread(thread_id)
                except Exception as error:
                    self.store.finish_operative_operation(
                        int(archive["id"]),
                        state="uncertain",
                        result={"error": str(error)},
                        after={"coverage": "unavailable"},
                    )
                    blockers.append(f"task {task_id} archive is uncertain: {error}")
                    continue
                if not archived.get("archived"):
                    self.store.finish_operative_operation(
                        int(archive["id"]),
                        state="failed",
                        result={"accepted": True},
                        after={"thread": archived},
                    )
                    blockers.append(f"task {task_id} archive was not observed")
                    continue
                self.store.finish_operative_operation(
                    int(archive["id"]),
                    state="complete",
                    result={"accepted": True},
                    after={"thread": archived},
                )
            elif archive["state"] != "complete":
                blockers.append(
                    f"task {task_id} archive operation {archive['id']} requires reconciliation"
                )
                continue

            timestamp = utc_now()
            disposition = dispositions.get(str(task_id), "quarantine")
            if disposition not in {"quarantine", "cancel"}:
                raise StoreError(
                    f"task {task_id} disposition must be quarantine or cancel"
                )
            with self.store.transaction() as connection:
                connection.execute(
                    "DELETE FROM reservations WHERE action_id IN (SELECT id FROM actions WHERE task_id = ?)",
                    (task_id,),
                )
                connection.execute(
                    """UPDATE actions SET state = 'canceled',
                       condition = 'superseded by operative emergency takeover',
                       updated_at = ? WHERE task_id = ?
                       AND state NOT IN ('processed','canceled')""",
                    (timestamp, task_id),
                )
                connection.execute(
                    """UPDATE tasks SET state = 'retired', archived = 1,
                       runtime_status = 'idle', last_turn_terminal = 1,
                       helpers_terminal = 1, updated_at = ? WHERE id = ?""",
                    (timestamp, task_id),
                )
                if disposition == "cancel":
                    connection.execute(
                        """UPDATE assignments SET stage = 'canceled',
                           condition = 'canceled by operative emergency takeover',
                           updated_at = ? WHERE executor_task_id = ? OR overseer_task_id = ?""",
                        (timestamp, task_id, task_id),
                    )
                else:
                    connection.execute(
                        """UPDATE assignments SET condition =
                           'quarantined for operative disposition', updated_at = ?
                           WHERE executor_task_id = ? OR overseer_task_id = ?""",
                        (timestamp, task_id, task_id),
                    )
            results.append(
                {
                    "task_id": task_id,
                    "thread_id": thread_id,
                    "state": "retired",
                    "disposition": disposition,
                    "parent_terminal": True,
                    "helpers_terminal": True,
                    "helper_threads": helper_observations,
                }
            )
        return {"ok": not blockers, "results": results, "blockers": blockers}

    async def _operative_reconcile_control(
        self, request: dict[str, Any]
    ) -> dict[str, Any]:
        journal = self._require_bound_operative(_thread_identity(request))
        supplied = self._operative_input(request)
        target_kind = supplied.get("target_kind")
        if target_kind == "external_operation":
            return await self.handle_request(
                {
                    "command": "resolve_operation",
                    "thread_id": journal["native_thread_id"],
                    "decision": {
                        key: value
                        for key, value in supplied.items()
                        if key
                        in {
                            "operation_id",
                            "resolution",
                            "evidence",
                            "native_id",
                            "result",
                        }
                    },
                }
            )
        if target_kind != "operative_operation":
            raise StoreError(
                "operative reconciliation target_kind must be external_operation or operative_operation"
            )
        operation_id = supplied.get("operation_id")
        resolution = supplied.get("resolution")
        if not isinstance(operation_id, int) or resolution not in {
            "observed_success",
            "observed_failure",
            "confirmed_unsent",
        }:
            raise StoreError(
                "operative reconciliation requires an exact operation and resolution"
            )
        operation = self.store.row(
            "SELECT * FROM operative_operations WHERE id = ? AND takeover_id = ?",
            (operation_id, journal["takeover_id"]),
        )
        if operation is None:
            raise StoreError(f"unknown operative operation {operation_id}")
        evidence_path, evidence = self._read_operative_evidence(
            supplied.get("evidence")
        )
        retained = self.store.record_operative_evidence(
            str(journal["takeover_id"]),
            f"operative-reconcile:{operation_id}:{resolution}",
            "operation_reconciliation",
            coverage="observed",
            target_type="operative_operation",
            target_id=operation_id,
            detail={"resolution": resolution, "evidence": evidence},
            artifact_path=evidence_path,
        )
        state = "complete" if resolution != "observed_failure" else "failed"
        self.store.finish_operative_operation(
            operation_id,
            state=state,
            result={"resolution": resolution, "evidence": evidence},
            after=(
                supplied.get("after")
                if isinstance(supplied.get("after"), dict)
                else {"coverage": "observed"}
            ),
            evidence_id=int(retained["id"]),
        )
        return {"ok": True, "operation_id": operation_id, "resolution": resolution}

    def _operative_worktree_control(self, request: dict[str, Any]) -> dict[str, Any]:
        journal = self._require_bound_operative(_thread_identity(request))
        supplied = self._operative_input(request)
        action = supplied.get("action")
        path_value = supplied.get("path")
        operation_key = supplied.get("operation_key")
        if action not in {"adopt", "release"}:
            raise StoreError("operative worktree action must be adopt or release")
        if not isinstance(path_value, str) or not Path(path_value).is_absolute():
            raise StoreError("operative worktree requires an exact absolute path")
        if not isinstance(operation_key, str) or not operation_key:
            raise StoreError("operative worktree requires a stable operation_key")
        path = Path(path_value).resolve(strict=True)
        project = next(
            (
                item
                for item in self.config.projects
                if path
                in {
                    Path(line.removeprefix("worktree ")).resolve(strict=True)
                    for line in subprocess.run(
                        ["git", "worktree", "list", "--porcelain"],
                        cwd=item.repo_path,
                        capture_output=True,
                        text=True,
                        check=False,
                    ).stdout.splitlines()
                    if line.startswith("worktree ")
                }
            ),
            None,
        )
        if project is None:
            raise StoreError("path is not an enrolled repository worktree")
        snapshot = self._observe_git_root(
            project.project_id,
            path,
            snapshot_id="worktree-control",
            changes_section="worktree-control-changes",
        )
        adopted = dict(journal.get("adopted_worktrees") or {})
        if action == "release" and str(path) not in adopted:
            raise StoreError("worktree is not adopted by this takeover")
        correlation = f"operative-worktree:{journal['takeover_id']}:{operation_key}:{action}:{path}"
        operation = self.store.create_operative_operation(
            str(journal["takeover_id"]),
            f"worktree_{action}",
            str(path),
            snapshot,
            correlation_id=correlation,
        )
        if operation["state"] == "complete":
            return self._retained_operative_result(operation)
        if operation["state"] != "intent":
            raise StoreError(
                f"operative operation {operation['id']} is {operation['state']}; reconcile it"
            )
        if action == "adopt":
            adopted[str(path)] = {
                "project_id": project.project_id,
                "adopted_at": utc_now(),
                "before": snapshot,
            }
        else:
            adopted.pop(str(path), None)
        self._save_operative_journal(adopted_worktrees=adopted)
        result = {"ok": True, "action": action, "path": str(path), "snapshot": snapshot}
        self.store.finish_operative_operation(
            int(operation["id"]), state="complete", result=result, after=snapshot
        )
        return result

    def _operative_repair_check(self, request: dict[str, Any]) -> dict[str, Any]:
        self._require_bound_operative(_thread_identity(request))
        source = Path(self.config.source_root)
        checks: list[dict[str, Any]] = []
        completed = subprocess.run(
            ["git", "status", "--porcelain=v1"],
            cwd=source,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        checks.append(
            {
                "name": "git_status",
                "ok": completed.returncode == 0,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }
        )
        syntax_errors = []
        for path in (source / "src" / "fulcrum").rglob("*.py"):
            try:
                compile(path.read_text(encoding="utf-8"), str(path), "exec")
            except (OSError, SyntaxError) as error:
                syntax_errors.append({"path": str(path), "error": str(error)})
        checks.append(
            {
                "name": "python_syntax",
                "ok": not syntax_errors,
                "errors": syntax_errors,
            }
        )
        return {"ok": all(item["ok"] for item in checks), "checks": checks}

    def _operative_reinstall(self, request: dict[str, Any]) -> dict[str, Any]:
        journal = self._require_bound_operative(_thread_identity(request))
        supplied = self._operative_input(request)
        if supplied.get("confirm") != "reinstall retained recovery and control plane":
            raise StoreError(
                "operative reinstall requires the exact confirmation phrase"
            )
        operation_key = supplied.get("operation_key")
        if not isinstance(operation_key, str) or not operation_key:
            raise StoreError("operative reinstall requires a stable operation_key")
        from fulcrum.install import install_recovery_artifact

        correlation = f"operative-reinstall:{journal['takeover_id']}:{operation_key}"
        operation = self.store.create_operative_operation(
            str(journal["takeover_id"]),
            "reinstall",
            str(self.paths.control_root),
            {
                "recovery_launcher": str(self.paths.recovery_launcher),
                "control_plane": str(control_plane_source(self.paths)),
            },
            correlation_id=correlation,
        )
        if operation["state"] == "complete":
            return self._retained_operative_result(operation)
        if operation["state"] != "intent":
            raise StoreError(
                f"operative reinstall operation {operation['id']} requires reconciliation"
            )
        self.store.mark_operative_operation_sent(int(operation["id"]))
        try:
            recovery, recovery_updated = install_recovery_artifact(
                self.config, self.paths
            )
            control_plane, control_updated = install_control_plane(
                self.config, self.paths
            )
        except Exception as error:
            self.store.finish_operative_operation(
                int(operation["id"]),
                state="uncertain",
                result={"error": str(error)},
                after={
                    "recovery_launcher": str(self.paths.recovery_launcher),
                    "control_plane": str(control_plane_source(self.paths)),
                },
            )
            raise
        result = {
            "ok": True,
            "recovery_launcher": str(recovery),
            "recovery_updated": recovery_updated,
            "control_plane": str(control_plane),
            "control_plane_updated": control_updated,
        }
        self.store.finish_operative_operation(
            int(operation["id"]), state="complete", result=result, after=result
        )
        return result

    def _operative_service_check(self, request: dict[str, Any]) -> dict[str, Any]:
        self._require_bound_operative(_thread_identity(request))
        controller, app_server = self._observe_operative_services()
        return {
            "ok": bool(controller.get("complete") and app_server.get("complete")),
            "controller": controller,
            "app_server": app_server,
        }

    def _dossier_section(
        self,
        name: str,
        items: list[dict[str, Any]] | dict[str, Any] | None,
        *,
        snapshot_id: str,
        unavailable: str | None = None,
        not_applicable: str | None = None,
    ) -> dict[str, Any]:
        if unavailable is not None:
            return {
                "coverage": "unavailable",
                "reason": unavailable,
                "complete": False,
                "truncated": False,
            }
        if not_applicable is not None:
            return {
                "coverage": "not_applicable",
                "reason": not_applicable,
                "complete": True,
                "truncated": False,
            }
        if isinstance(items, dict):
            return {
                "coverage": "observed",
                "items": items,
                "count": 1,
                "complete": True,
                "truncated": False,
            }
        values = items or []
        limit = 200
        section = {
            "coverage": "observed",
            "items": values[:limit],
            "count": len(values),
            "complete": len(values) <= limit,
            "truncated": len(values) > limit,
        }
        if len(values) > limit:
            section["snapshot_id"] = snapshot_id
            section["overflow_section"] = name
            section["overflow_artifact"] = self._write_dossier_overflow(
                snapshot_id, name, values[limit:]
            )
        return section

    def _create_dossier_snapshot(self) -> str:
        journal = self.operative_journal
        if journal is None:
            raise StoreError("no operative takeover is available")
        try:
            root = safe_child(
                self.paths.control_root / "operative-evidence",
                str(journal["takeover_id"]),
                "dossiers",
            )
        except ValueError as error:
            raise StoreError(
                "operative takeover has an unsafe artifact identity"
            ) from error
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for directory_path in (root, root.parent, root.parent.parent):
            directory = os.open(directory_path, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        for _attempt in range(10):
            snapshot_id = str(uuid.uuid4())
            snapshot_root = safe_child(root, snapshot_id)
            try:
                snapshot_root.mkdir(mode=0o700)
            except FileExistsError:
                continue
            directory = os.open(root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return snapshot_id
        raise StoreError("cannot allocate an immutable operative dossier snapshot")

    def _write_dossier_overflow(
        self,
        snapshot_id: str,
        section: str,
        omitted: list[dict[str, Any]],
    ) -> str:
        journal = self.operative_journal
        if journal is None:
            raise StoreError("no operative takeover is available")
        safe_section = "".join(
            character if character.isalnum() or character in {"-", "_"} else "_"
            for character in section
        )
        try:
            root = safe_child(
                self.paths.control_root / "operative-evidence",
                str(journal["takeover_id"]),
                "dossiers",
                snapshot_id,
            )
            path = safe_child(root, f"{safe_section}-overflow.json")
        except ValueError as error:
            raise StoreError("operative dossier artifact identity is unsafe") from error
        if not root.is_dir() or path.exists():
            raise StoreError("operative dossier artifact identity is not immutable")
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        payload = {
            "takeover_id": journal["takeover_id"],
            "snapshot_id": snapshot_id,
            "section": section,
            "coverage": "observed",
            "items": [self.store.redacted(item) for item in omitted],
        }
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory = os.open(root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
        return str(path.resolve())

    async def _build_operative_dossier(self) -> dict[str, Any]:
        """Build a bounded, redacted, explicitly covered emergency inventory."""

        journal = self.operative_journal
        if journal is None:
            raise StoreError("no operative takeover is available")
        snapshot_id: str = self._create_dossier_snapshot()
        tasks = self.store.rows(
            """SELECT id, native_thread_id, role, role_number, title, project_id,
                      state, runtime_status, last_turn_terminal, helpers_terminal,
                      archived, updated_at
               FROM tasks WHERE archived = 0 ORDER BY id"""
        )
        actions = self.store.rows(
            """SELECT actions.id, actions.task_id, actions.assignment_id,
                      actions.occurrence_id, actions.kind, actions.state,
                      actions.native_turn_id, actions.outcome_kind,
                      actions.condition, actions.created_at, actions.updated_at,
                      tasks.role
               FROM actions JOIN tasks ON tasks.id = actions.task_id
               WHERE actions.state NOT IN ('processed','canceled')
               ORDER BY actions.id"""
        )
        native_turns = self.store.rows(
            """SELECT usage.native_thread_id, usage.native_turn_id,
                      usage.action_id, usage.attributed_action_id, usage.is_helper,
                      usage.coverage, usage.gap_reason, usage.first_observed_at,
                      usage.last_observed_at, usage.terminal_at,
                      CASE WHEN usage.terminal_at IS NOT NULL THEN 'terminal'
                           WHEN usage.coverage = 'unavailable' THEN 'unavailable'
                           ELSE 'active' END AS runtime_state
               FROM action_turn_usage AS usage
               ORDER BY usage.native_thread_id, usage.native_turn_id"""
        )
        helpers = self.store.rows(
            """SELECT helper.id, helper.native_thread_id, helper.parent_thread_id,
                      helper.parent_turn_id,
                      helper.native_turn_id, helper.attributed_action_id,
                      helper.first_observed_at, helper.last_observed_at,
                      usage.coverage, usage.gap_reason, usage.terminal_at,
                      CASE WHEN usage.terminal_at IS NOT NULL THEN 'terminal'
                           WHEN usage.native_turn_id IS NULL THEN 'unavailable'
                           ELSE 'active' END AS runtime_state
               FROM telemetry_helper_threads AS helper
               LEFT JOIN action_turn_usage AS usage
                 ON usage.native_thread_id = helper.native_thread_id
                AND usage.native_turn_id = helper.native_turn_id
               ORDER BY id"""
        )
        interviews = self.store.rows(
            """SELECT id, occurrence_id, subject_task_id, prior_archived, state,
                      deadline_at, created_at, updated_at
               FROM interviews
               WHERE state NOT IN ('answered','expired') ORDER BY id"""
        )
        git_items: list[dict[str, Any]] = []
        worktree_items: list[dict[str, Any]] = []
        for project_index, project in enumerate(self.config.projects):
            root = Path(project.repo_path)
            if not (root / ".git").exists():
                git_items.append(
                    {
                        "project_id": project.project_id,
                        "path": str(root),
                        "coverage": "unavailable",
                        "reason": "Git repository is unavailable",
                    }
                )
                continue
            git_items.append(
                self._observe_git_root(
                    project.project_id,
                    root,
                    snapshot_id=snapshot_id,
                    changes_section=f"git-changes-{project_index}",
                )
            )
            try:
                observed = subprocess.run(
                    ["git", "worktree", "list", "--porcelain"],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
                if observed.returncode == 0:
                    paths = [
                        line.removeprefix("worktree ")
                        for line in observed.stdout.splitlines()
                        if line.startswith("worktree ")
                    ]
                    for worktree_index, path in enumerate(paths):
                        worktree_items.append(
                            self._observe_git_root(
                                project.project_id,
                                Path(path),
                                snapshot_id=snapshot_id,
                                changes_section=(
                                    f"worktree-changes-{project_index}-{worktree_index}"
                                ),
                            )
                        )
                else:
                    worktree_items.append(
                        {
                            "project_id": project.project_id,
                            "path": str(root),
                            "coverage": "unavailable",
                            "reason": "git worktree inventory failed",
                        }
                    )
            except (OSError, subprocess.SubprocessError):
                worktree_items.append(
                    {
                        "project_id": project.project_id,
                        "path": str(root),
                        "coverage": "unavailable",
                        "reason": "git worktree inventory failed",
                    }
                )
        integrity = self.store.row("PRAGMA quick_check")
        controller_service, app_server = self._observe_operative_services()
        try:
            recovery_smoke = subprocess.run(
                [str(self.paths.recovery_launcher), "--smoke-test"],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as error:
            recovery_launcher = {
                "coverage": "unavailable",
                "complete": False,
                "path": str(self.paths.recovery_launcher),
                "reason": str(error),
                "truncated": False,
            }
        else:
            recovery_mode = (
                stat.S_IMODE(self.paths.recovery_launcher.stat().st_mode)
                if self.paths.recovery_launcher.exists()
                else None
            )
            recovery_launcher = {
                "coverage": "observed",
                "complete": recovery_smoke.returncode == 0 and recovery_mode == 0o700,
                "path": str(self.paths.recovery_launcher),
                "mode": oct(recovery_mode) if recovery_mode is not None else None,
                "result": recovery_smoke.stdout.strip(),
                "error": recovery_smoke.stderr.strip(),
                "truncated": False,
            }
        state_ok, state_reasons = state_readiness(
            self.store, include_operative_fence=False
        )
        progress_ok, progress_reasons = progress_readiness(
            self.store, critical_workers=self.critical_workers
        )
        sent = self.store.rows(
            """SELECT id, kind, target, state, native_id, correlation_id, condition,
                      created_at, updated_at
               FROM external_operations
               WHERE state IN ('intent','sent','uncertain','failed')
               ORDER BY id"""
        )
        candidates = self.store.rows(
            """SELECT assignments.id AS assignment_id, runs.project_id, worktree_path, candidate_id,
                      source_oid, tested_oid, stage, condition
               FROM assignments JOIN runs ON runs.id = assignments.run_id
               WHERE assignments.stage NOT IN ('completed','canceled')
               ORDER BY assignments.id"""
        )
        takeover_identity = {
            key: journal.get(key)
            for key in (
                "takeover_id",
                "state",
                "native_thread_id",
                "superseded_thread_ids",
                "scope",
                "prior_dispatch_enabled",
                "caller_verification",
                "next_step",
                "completed_effects",
                "created_at",
                "updated_at",
                "offline_operations",
                "adopted_worktrees",
                "store_quarantine",
            )
        }

        def section(
            name: str,
            items: list[dict[str, Any]] | dict[str, Any] | None,
            *,
            unavailable: str | None = None,
            not_applicable: str | None = None,
        ) -> dict[str, Any]:
            return self._dossier_section(
                name,
                items,
                snapshot_id=snapshot_id,
                unavailable=unavailable,
                not_applicable=not_applicable,
            )

        dossier = {
            "snapshot_id": snapshot_id,
            "takeover": {
                "coverage": "observed",
                "value": takeover_identity,
                "complete": True,
                "truncated": False,
            },
            "managed_tasks": section("managed-tasks", tasks),
            "native_turns": section("native-turns", native_turns),
            "helpers": section("helpers", helpers),
            "interviews": section("interviews", interviews),
            "actions": section("actions", actions),
            "reservations": section(
                "reservations",
                self.store.rows("SELECT * FROM reservations ORDER BY id"),
            ),
            "runs": section(
                "runs",
                self.store.rows(
                    "SELECT * FROM runs WHERE state NOT IN ('completed','canceled') ORDER BY id"
                ),
            ),
            "assignments": section(
                "assignments",
                self.store.rows(
                    "SELECT * FROM assignments WHERE stage NOT IN ('completed','canceled') ORDER BY id"
                ),
            ),
            "holds": section(
                "holds",
                self.store.rows(
                    "SELECT * FROM holds WHERE released_at IS NULL ORDER BY id"
                ),
            ),
            "occurrences": section(
                "occurrences",
                self.store.rows(
                    "SELECT * FROM occurrences WHERE state NOT IN ('complete','skipped') ORDER BY id"
                ),
            ),
            "updates": section(
                "updates",
                self.store.rows(
                    """SELECT id, recipient_task_id, identity, actionable, state,
                              created_at, updated_at FROM updates
                       WHERE state IN ('retained','batched') ORDER BY id"""
                ),
            ),
            "obligations": section(
                "obligations",
                self.store.rows(
                    "SELECT * FROM obligations WHERE state NOT IN ('complete','canceled') ORDER BY id"
                ),
            ),
            "worktrees": section("worktrees", worktree_items),
            "git": section("git", git_items),
            "candidates": section("candidates", candidates),
            "tollgate": section(
                "tollgate",
                None,
                unavailable=(
                    "Tollgate status requires exact candidate-specific observation"
                    if candidates
                    else None
                ),
                not_applicable="no unfinished candidates" if not candidates else None,
            ),
            "beads_publication": section(
                "beads-publication",
                self.store.rows(
                    """SELECT bead_id, project_id, activation, publication_state,
                              updated_at FROM beads WHERE publication_state != 'complete'
                       ORDER BY bead_id"""
                ),
            ),
            "uncertain_external_operations": section(
                "uncertain-external-operations", sent
            ),
            "operative_operations": section(
                "operative-operations",
                self.store.rows(
                    """SELECT id, kind, target, state, correlation_id, evidence_id,
                              created_at, updated_at FROM operative_operations
                       WHERE takeover_id = ? ORDER BY id""",
                    (journal["takeover_id"],),
                ),
            ),
            "controller_service": controller_service,
            "app_server": app_server,
            "recovery_launcher": recovery_launcher,
            "store_integrity": {
                "coverage": "observed" if integrity is not None else "unavailable",
                "quick_check": next(iter(integrity.values())) if integrity else None,
                "complete": integrity is not None,
                "truncated": False,
            },
            "source_snapshot": section("source-snapshot", git_items),
            "editable_environment": {
                "coverage": "observed",
                "source_root": self.config.source_root,
                "python": os.path.realpath(sys.executable),
                "complete": True,
                "truncated": False,
            },
            "readiness": {
                "coverage": "observed",
                "ready_without_takeover_fence": state_ok
                and progress_ok
                and self.runtime.ready,
                "failures": state_reasons
                + progress_reasons
                + ([] if self.runtime.ready else ["App Server is unavailable"]),
                "complete": True,
                "truncated": False,
            },
            "evidence": section(
                "evidence",
                self.store.rows(
                    """SELECT id, evidence_key, kind, coverage, target_type,
                              target_id, artifact_path, created_at
                       FROM operative_evidence WHERE takeover_id = ?
                       ORDER BY id""",
                    (journal["takeover_id"],),
                ),
            ),
            "bounds": {"maximum_items_per_section": 200},
        }
        return self.store.redacted(dossier)

    def _observe_operative_services(
        self,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            controller = inspect_service(CONTROLLER_LABEL)
        except Exception as error:
            controller_detail: dict[str, Any] = {"observation_error": str(error)}
        else:
            controller_detail = {
                "launchd_loaded": controller.loaded,
                "launchd_state": controller.state,
                "launchd_pid": controller.pid,
                "launchd_executable": controller.executable_path,
            }
        controller_result = {
            "coverage": "observed" if self.lock_handle is not None else "unavailable",
            "pid": os.getpid(),
            "lock_path": str(self.paths.lock),
            "lock_owned": self.lock_handle is not None,
            "ownership": "controller_lock" if self.lock_handle is not None else None,
            "mode": "operative_only",
            "complete": self.lock_handle is not None,
            "truncated": False,
            **controller_detail,
        }
        try:
            app = inspect_service(APP_SERVER_LABEL)
        except Exception as error:
            app_result = {
                "coverage": "unavailable",
                "connected": self.runtime.ready,
                "ownership": "unavailable",
                "reason": str(error),
                "complete": False,
                "truncated": False,
            }
        else:
            owned = bool(app.running)
            app_result = {
                "coverage": (
                    "observed" if self.runtime.ready and owned else "unavailable"
                ),
                "connected": self.runtime.ready,
                "ownership": "launchd" if owned else "unavailable",
                "launchd_loaded": app.loaded,
                "launchd_state": app.state,
                "launchd_pid": app.pid,
                "launchd_executable": app.executable_path,
                "complete": self.runtime.ready and owned,
                "truncated": False,
            }
        return controller_result, app_result

    def _observe_git_root(
        self,
        project_id: str,
        root: Path,
        *,
        snapshot_id: str,
        changes_section: str,
    ) -> dict[str, Any]:
        def git(*arguments: str) -> tuple[bool, str]:
            try:
                result = subprocess.run(
                    ["git", *arguments],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
            except (OSError, subprocess.SubprocessError):
                return False, ""
            return result.returncode == 0, result.stdout.strip()

        head_ok, head = git("rev-parse", "HEAD")
        branch_ok, branch = git("branch", "--show-current")
        dirty_ok, dirty = git("status", "--porcelain=v1", "--untracked-files=normal")
        if not (head_ok and branch_ok and dirty_ok):
            return {
                "project_id": project_id,
                "path": str(root),
                "coverage": "unavailable",
                "reason": "Git observation failed",
            }
        lines = dirty.splitlines()
        return {
            "project_id": project_id,
            "path": str(root),
            "coverage": "observed",
            "head": head,
            "branch": branch or None,
            "dirty": bool(lines),
            "changes": self._dossier_section(
                changes_section,
                [{"path": line[3:]} for line in lines],
                snapshot_id=snapshot_id,
            ),
        }

    @staticmethod
    def _read_operative_evidence(path_value: object) -> tuple[str, str]:
        if not isinstance(path_value, str) or not path_value.strip():
            raise StoreError("operative evidence requires an absolute readable file")
        path = Path(path_value)
        if not path.is_absolute() or not path.is_file():
            raise StoreError("operative evidence requires an absolute readable file")
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as error:
            raise StoreError(f"cannot read operative evidence: {error}") from error
        if not content.strip():
            raise StoreError("operative evidence must not be empty")
        if len(content.encode("utf-8")) > MAX_EXACT_SOURCE_ARTIFACT_BYTES:
            raise StoreError("operative evidence exceeds the retained artifact bound")
        return str(path.resolve()), content

    @classmethod
    def _read_closeout_evidence(
        cls, path_value: object
    ) -> tuple[str, dict[str, Any], str]:
        path, content = cls._read_operative_evidence(path_value)
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as error:
            raise StoreError(
                "operative closeout evidence must be valid JSON"
            ) from error
        if not isinstance(payload, dict):
            raise StoreError("operative closeout evidence must contain a JSON object")
        return path, payload, content

    def _closeout_artifact_path(self, takeover_id: str) -> Path:
        try:
            return safe_child(
                self.paths.control_root / "operative-evidence",
                takeover_id,
                "closeout.json",
            )
        except ValueError as error:
            raise StoreError(
                "operative takeover has an unsafe artifact identity"
            ) from error

    def _retain_closeout_evidence(self, takeover_id: str, content: str) -> str:
        """Atomically retain the exact accepted bytes under controller ownership."""

        path = self._closeout_artifact_path(takeover_id)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
        return str(path.resolve())

    def _read_retained_closeout_evidence(
        self, journal: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        expected = self._closeout_artifact_path(str(journal["takeover_id"])).resolve()
        retained = journal.get("evidence_path")
        if not isinstance(retained, str):
            raise StoreError("retained operative closeout evidence is unavailable")
        try:
            actual = Path(retained).resolve(strict=True)
        except OSError as error:
            raise StoreError(
                "retained operative closeout evidence is unavailable"
            ) from error
        if actual != expected:
            raise StoreError("retained operative closeout evidence identity is invalid")
        path, payload, _ = self._read_closeout_evidence(str(actual))
        return path, payload

    @staticmethod
    def _section_items(dossier: dict[str, Any], name: str) -> list[dict[str, Any]]:
        section = dossier.get(name)
        if not isinstance(section, dict) or section.get("coverage") != "observed":
            return []
        items = section.get("items")
        return items if isinstance(items, list) else []

    def _complete_section_items(
        self, dossier: dict[str, Any], name: str
    ) -> list[dict[str, Any]] | None:
        section = dossier.get(name)
        if not isinstance(section, dict) or section.get("coverage") != "observed":
            return None
        inline = section.get("items")
        if not isinstance(inline, list):
            return None
        if not section.get("truncated"):
            return inline if section.get("complete") else None
        artifact_value = section.get("overflow_artifact")
        if not isinstance(artifact_value, str):
            return None
        artifact = Path(artifact_value)
        evidence_root = (self.paths.control_root / "operative-evidence").resolve()
        try:
            resolved = artifact.resolve(strict=True)
            resolved.relative_to(evidence_root)
            payload = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        overflow = payload.get("items") if isinstance(payload, dict) else None
        if (
            not isinstance(overflow, list)
            or payload.get("takeover_id")
            != (self.operative_journal or {}).get("takeover_id")
            or payload.get("snapshot_id") != section.get("snapshot_id")
            or payload.get("section") != section.get("overflow_section")
            or len(inline) + len(overflow) != section.get("count")
        ):
            return None
        return inline + overflow

    def _validate_closeout_evidence(
        self, evidence: dict[str, Any], dossier: dict[str, Any]
    ) -> list[str]:
        failures: list[str] = []
        scalar_fields = ("emergency_outcome", "root_cause")
        list_fields = ("repairs", "commits", "validation", "limitations")
        object_fields = ("source", "store", "services")
        for field in scalar_fields:
            if not isinstance(evidence.get(field), str) or not evidence[field].strip():
                failures.append(f"closeout evidence missing {field}")
        if evidence.get("emergency_outcome") != "complete":
            failures.append("closeout evidence emergency_outcome is not complete")
        for field in list_fields:
            value = evidence.get(field)
            if not isinstance(value, list) or not all(
                isinstance(item, str) and item.strip() for item in value
            ):
                failures.append(f"closeout evidence missing {field}")
            elif field != "limitations" and not value:
                failures.append(f"closeout evidence {field} must not be empty")
        for field in object_fields:
            value = evidence.get(field)
            if not isinstance(value, dict) or value.get("status") != "observed":
                failures.append(f"closeout evidence missing observed {field}")
        dispositions = evidence.get("dispositions")
        if not isinstance(dispositions, dict):
            failures.append("closeout evidence missing dispositions")
            return failures
        for section_name in (
            "managed_tasks",
            "actions",
            "uncertain_external_operations",
            "operative_operations",
            "worktrees",
            "candidates",
            "beads_publication",
            "obligations",
        ):
            if self._complete_section_items(dossier, section_name) is None:
                failures.append(
                    f"closeout evidence inventory is incomplete for {section_name}"
                )
        managed_tasks = self._complete_section_items(dossier, "managed_tasks") or []
        actions = self._complete_section_items(dossier, "actions") or []
        external_operations = (
            self._complete_section_items(dossier, "uncertain_external_operations") or []
        )
        operative_operations = (
            self._complete_section_items(dossier, "operative_operations") or []
        )
        worktrees = self._complete_section_items(dossier, "worktrees") or []
        candidates = self._complete_section_items(dossier, "candidates") or []
        publications = self._complete_section_items(dossier, "beads_publication") or []
        obligations = self._complete_section_items(dossier, "obligations") or []
        ordinary_tasks = [
            item for item in managed_tasks if item.get("role") != "operative"
        ]
        ordinary_task_ids = [str(item["id"]) for item in ordinary_tasks]
        ordinary_action_ids = [
            str(item["id"]) for item in actions if item.get("role") != "operative"
        ]
        effect_ids = [f"external:{item['id']}" for item in external_operations] + [
            f"operative:{item['id']}"
            for item in operative_operations
            if item.get("state") in {"sent", "uncertain", "failed"}
        ]
        worktree_ids = [
            str(item["path"]) for item in worktrees if item.get("path") is not None
        ]
        candidate_ids = [
            str(item.get("candidate_id") or item.get("assignment_id"))
            for item in candidates
        ]
        publication_ids = [f"bead:{item['bead_id']}" for item in publications] + [
            f"obligation:{item['id']}" for item in obligations
        ]
        expected = {
            "agents": ordinary_task_ids,
            "actions": ordinary_action_ids,
            "effects": effect_ids,
            "worktrees": worktree_ids,
            "candidates": candidate_ids,
            "publication_delivery": publication_ids,
        }
        for category, identifiers in expected.items():
            disposition = dispositions.get(category)
            if not isinstance(disposition, dict):
                failures.append(f"closeout evidence missing {category} disposition")
                continue
            status = disposition.get("status")
            ids = disposition.get("ids")
            if status not in {"resolved", "not_applicable"} or not isinstance(
                ids, list
            ):
                failures.append(f"closeout evidence invalid {category} disposition")
                continue
            if sorted(str(item) for item in ids) != sorted(identifiers):
                failures.append(
                    f"closeout evidence {category} disposition does not cover current IDs"
                )
            if identifiers and status != "resolved":
                failures.append(
                    f"closeout evidence {category} disposition is unresolved"
                )
        return failures

    def _operative_closeout_failures(
        self, dossier: dict[str, Any], evidence: dict[str, Any]
    ) -> list[str]:
        failures = self._validate_closeout_evidence(evidence, dossier)
        journal = self.operative_journal
        if journal is None:
            return ["operative journal is unavailable"]
        if not journal.get("prior_dispatch_enabled"):
            failures.append("dispatch was not permitted before takeover")
        state_ok, state_reasons = state_readiness(
            self.store, include_operative_fence=False
        )
        if not state_ok:
            failures.extend(state_reasons)
        progress_ok, progress_reasons = progress_readiness(
            self.store, critical_workers=self.critical_workers
        )
        if not progress_ok:
            failures.extend(progress_reasons)
        if not self.runtime.ready:
            failures.append("App Server is unavailable")
        recovery = self.paths.recovery_launcher
        try:
            smoke = subprocess.run(
                [str(recovery), "--smoke-test"],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            failures.append("independent recovery launcher is unavailable")
        else:
            if smoke.returncode != 0:
                failures.append("independent recovery launcher smoke test failed")
        active = self.store.rows(
            """SELECT id, title FROM tasks WHERE role != 'operative' AND archived = 0
               AND (last_turn_terminal = 0 OR helpers_terminal = 0 OR runtime_status = 'active')
               ORDER BY id LIMIT 20"""
        )
        if active:
            failures.append(
                "implicated managed tasks remain active: "
                + ", ".join(str(item["id"]) for item in active)
            )
        unfinished_actions = self.store.rows(
            """SELECT actions.id FROM actions JOIN tasks ON tasks.id = actions.task_id
               WHERE tasks.role != 'operative'
                 AND actions.state IN ('pending','starting','active','terminal','uncertain')
               ORDER BY actions.id LIMIT 20"""
        )
        if unfinished_actions:
            failures.append(
                "ordinary actions remain unfinished: "
                + ", ".join(str(item["id"]) for item in unfinished_actions)
            )
        unfinished_assignments = self.store.rows(
            """SELECT id FROM assignments WHERE stage NOT IN ('completed','canceled')
               ORDER BY id LIMIT 20"""
        )
        if unfinished_assignments:
            failures.append(
                "assignments remain unfinished: "
                + ", ".join(str(item["id"]) for item in unfinished_assignments)
            )
        unresolved = self.store.rows("""SELECT id FROM external_operations
               WHERE state IN ('intent','sent','uncertain') ORDER BY id LIMIT 20""")
        if unresolved:
            failures.append(
                "external operations remain unresolved: "
                + ", ".join(str(item["id"]) for item in unresolved)
            )
        unresolved_operative = self.store.rows(
            """SELECT id FROM operative_operations WHERE takeover_id = ?
               AND state IN ('intent','sent','uncertain') ORDER BY id LIMIT 20""",
            (journal["takeover_id"],),
        )
        if unresolved_operative:
            failures.append(
                "operative effects remain unresolved: "
                + ", ".join(str(item["id"]) for item in unresolved_operative)
            )
        for section in (
            "reservations",
            "runs",
            "assignments",
            "holds",
            "occurrences",
            "updates",
            "obligations",
            "interviews",
            "candidates",
            "beads_publication",
        ):
            items = self._complete_section_items(dossier, section)
            if items is None:
                failures.append(f"{section} inventory is incomplete")
            elif items:
                failures.append(f"{section} remain unresolved")
        if any(
            item.get("role") != "operative"
            for item in (self._complete_section_items(dossier, "actions") or [])
        ):
            failures.append("ordinary actions remain unresolved")
        git_items = self._complete_section_items(dossier, "git")
        if git_items is None:
            failures.append("source inventory is incomplete")
            git_items = []
        for item in git_items:
            if item.get("coverage") != "observed":
                failures.append("source state is unavailable")
            elif item.get("dirty"):
                failures.append(f"source worktree remains dirty: {item.get('path')}")
            changes = item.get("changes")
            if (
                not isinstance(changes, dict)
                or self._complete_section_items({"changes": changes}, "changes") is None
            ):
                failures.append(
                    f"source change inventory is incomplete: {item.get('path')}"
                )
        worktree_items = self._complete_section_items(dossier, "worktrees")
        if worktree_items is None:
            failures.append("worktree inventory is incomplete")
            worktree_items = []
        for item in worktree_items:
            path = item.get("path")
            if not isinstance(path, str) or not path:
                failures.append("worktree is unaccounted")
            if item.get("coverage") != "observed":
                failures.append(f"worktree state is unavailable: {path}")
                continue
            if (
                not isinstance(item.get("head"), str)
                or not item.get("head")
                or "branch" not in item
                or not isinstance(item.get("dirty"), bool)
            ):
                failures.append(f"worktree observation is incomplete: {path}")
            elif item["dirty"]:
                failures.append(f"worktree remains dirty: {path}")
            changes = item.get("changes")
            if (
                not isinstance(changes, dict)
                or self._complete_section_items({"changes": changes}, "changes") is None
            ):
                failures.append(f"worktree change inventory is incomplete: {path}")
        store_integrity = dossier.get("store_integrity")
        if not isinstance(store_integrity, dict) or (
            store_integrity.get("coverage") != "observed"
            or store_integrity.get("quick_check") != "ok"
        ):
            failures.append("store integrity is not observed healthy")
        controller_service = dossier.get("controller_service")
        if not isinstance(controller_service, dict) or not controller_service.get(
            "complete", False
        ):
            failures.append("controller ownership is unavailable")
        app_server = dossier.get("app_server")
        if not isinstance(app_server, dict) or not app_server.get("complete", False):
            failures.append("App Server ownership/connectivity is unavailable")
        recovery_launcher = dossier.get("recovery_launcher")
        if not isinstance(recovery_launcher, dict) or not recovery_launcher.get(
            "complete", False
        ):
            failures.append("independent recovery launcher is unavailable")
        readiness = dossier.get("readiness")
        if not isinstance(readiness, dict) or not readiness.get(
            "ready_without_takeover_fence", False
        ):
            failures.append("current readiness does not pass")
        return failures

    async def _request_operative_finish(
        self, request: dict[str, Any]
    ) -> dict[str, Any]:
        journal = self._require_bound_operative(_thread_identity(request))
        if journal["state"] == "closing":
            return {
                "ok": True,
                "takeover_id": journal["takeover_id"],
                "state": "closing",
                "reused": True,
            }
        if journal["state"] != "active":
            raise StoreError(
                f"cannot finish an operative takeover in {journal['state']}"
            )
        source_evidence_path, evidence, evidence_content = self._read_closeout_evidence(
            request.get("evidence")
        )
        dossier = await self._build_operative_dossier()
        failures = self._operative_closeout_failures(dossier, evidence)
        if failures:
            self.store.record_operative_evidence(
                str(journal["takeover_id"]),
                f"closeout-refused:{utc_now()}",
                "closeout_readiness",
                coverage="observed",
                target_type="operative_takeover",
                target_id=journal["takeover_id"],
                detail={"failures": failures},
                artifact_path=source_evidence_path,
            )
            raise StoreError("operative closeout refused: " + "; ".join(failures))
        evidence_path = self._retain_closeout_evidence(
            str(journal["takeover_id"]), evidence_content
        )
        retained = self.store.record_operative_evidence(
            str(journal["takeover_id"]),
            "successful-closeout-request",
            "closeout",
            coverage="observed",
            target_type="operative_takeover",
            target_id=journal["takeover_id"],
            detail={"contract": evidence},
            artifact_path=evidence_path,
        )
        closing_at = utc_now()
        closing = transition_journal(
            self.paths.operative_journal,
            journal,
            "closing",
            now=closing_at,
            next_step="wait for the operative turn and helpers to become terminal",
            completed_effect="closeout_evidence_retained",
            closing_at=closing_at,
            evidence_path=evidence_path,
            result={"evidence_id": retained["id"]},
        )
        self.operative_journal = closing
        self.store.mirror_operative_journal(closing)
        self.store.execute(
            "UPDATE actions SET outcome_kind = 'complete', outcome_payload = ?, updated_at = ? WHERE id = ?",
            (json.dumps({"evidence": evidence_path}), closing_at, journal["action_id"]),
        )
        return {
            "ok": True,
            "takeover_id": journal["takeover_id"],
            "state": "closing",
            "reused": False,
        }

    async def _maybe_finalize_operative_closeout(
        self, task: dict[str, Any], facts: dict[str, Any]
    ) -> bool:
        journal = self.operative_journal
        if journal is None or journal.get("state") != "closing":
            return False
        action = self.store.row(
            "SELECT * FROM actions WHERE id = ?", (journal.get("action_id"),)
        )
        if action is None or action.get("native_turn_id") != facts.get("last_turn_id"):
            return False
        if not facts["last_turn_terminal"] or not facts["helpers_terminal"]:
            return False
        retained_path, retained_contract = self._read_retained_closeout_evidence(
            journal
        )
        evidence_record = self.store.row(
            """SELECT detail_json FROM operative_evidence
               WHERE id = ? AND takeover_id = ? AND kind = 'closeout'""",
            (
                (journal.get("result") or {}).get("evidence_id"),
                journal["takeover_id"],
            ),
        )
        if evidence_record is None:
            recovered = self.store.record_operative_evidence(
                str(journal["takeover_id"]),
                "successful-closeout-request",
                "closeout",
                coverage="observed",
                target_type="operative_takeover",
                target_id=journal["takeover_id"],
                detail={"contract": retained_contract},
                artifact_path=retained_path,
            )
            journal = self._save_operative_journal(
                result={"evidence_id": recovered["id"]}
            )
            evidence_record = {
                "detail_json": json.dumps({"contract": retained_contract})
            }
        try:
            retained_detail = json.loads(evidence_record["detail_json"])
            closeout_evidence = retained_detail["contract"]
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise StoreError(
                "retained operative closeout evidence is invalid"
            ) from error
        if not isinstance(closeout_evidence, dict):
            raise StoreError("retained operative closeout evidence is invalid")
        if closeout_evidence != retained_contract:
            raise StoreError("retained operative closeout evidence is mismatched")
        dossier = await self._build_operative_dossier()
        failures = self._operative_closeout_failures(dossier, closeout_evidence)
        if failures:
            active = transition_journal(
                self.paths.operative_journal,
                journal,
                "active",
                now=utc_now(),
                next_step="resolve current closeout readiness failures",
                closeout_failures=failures,
            )
            self.operative_journal = active
            self.store.mirror_operative_journal(active)
            return False
        correlation = f"operative-close-archive:{journal['takeover_id']}"
        operation = self.store.create_operative_operation(
            str(journal["takeover_id"]),
            "thread_archive",
            str(task["native_thread_id"]),
            {"archived": bool(facts["archived"]), "terminal": True},
            correlation_id=correlation,
        )
        if operation["state"] in {"sent", "uncertain"} and not facts["archived"]:
            return False
        if facts["archived"] and operation["state"] != "complete":
            self.store.finish_operative_operation(
                int(operation["id"]),
                state="complete",
                result={"observed": True},
                after={"archived": True},
            )
        if not facts["archived"] and operation["state"] != "complete":
            self.store.mark_operative_operation_sent(int(operation["id"]))
            try:
                await self.runtime.archive(str(task["native_thread_id"]))
            except Exception as error:
                self.store.finish_operative_operation(
                    int(operation["id"]),
                    state="uncertain",
                    result={"error": str(error)},
                    after={"archived": "unavailable"},
                )
                return False
            try:
                observed = thread_facts(
                    await self.runtime.read_thread(str(task["native_thread_id"]))
                )
            except Exception as error:
                self.store.finish_operative_operation(
                    int(operation["id"]),
                    state="uncertain",
                    result={"accepted": True, "observation_error": str(error)},
                    after={"archived": "unavailable"},
                )
                return False
            if not observed["archived"]:
                self.store.finish_operative_operation(
                    int(operation["id"]),
                    state="uncertain",
                    result={"accepted": True},
                    after={"archived": False},
                )
                return False
            self.store.finish_operative_operation(
                int(operation["id"]),
                state="complete",
                result={"accepted": True},
                after={"archived": True},
            )
        final_dossier = await self._build_operative_dossier()
        final_failures = self._operative_closeout_failures(
            final_dossier, closeout_evidence
        )
        if final_failures:
            active = transition_journal(
                self.paths.operative_journal,
                journal,
                "active",
                now=utc_now(),
                next_step="resolve current closeout readiness failures",
                closeout_failures=final_failures,
            )
            self.operative_journal = active
            self.store.mirror_operative_journal(active)
            return False
        effects = list(journal["completed_effects"])
        if "closeout_revalidated" not in effects:
            effects.append("closeout_revalidated")
        journal = self._save_operative_journal(
            completed_effects=effects,
            next_step="finalize durable Operative archival and close the fence",
        )
        closed_at = utc_now()
        self.store.execute(
            "UPDATE actions SET state = 'processed', updated_at = ? WHERE id = ?",
            (closed_at, action["id"]),
        )
        self.store.execute(
            """UPDATE tasks SET state = 'archived', archived = 1,
               runtime_status = 'idle', last_turn_terminal = 1, helpers_terminal = 1,
               updated_at = ? WHERE id = ?""",
            (closed_at, task["id"]),
        )
        closed = transition_journal(
            self.paths.operative_journal,
            journal,
            "closed",
            now=closed_at,
            next_step="ordinary controller readiness determines dispatch",
            completed_effect="operative_archived_and_fence_released",
            closed_at=closed_at,
        )
        self.operative_journal = closed
        self.store.mirror_operative_journal(closed)
        self.store.execute(
            """INSERT INTO meta(key, value) VALUES ('controller_state', 'ready')
               ON CONFLICT(key) DO UPDATE SET value = 'ready'"""
        )
        self._update_readiness()
        self.advance_requested.set()
        return True

    def _abort_operative(self, request: dict[str, Any]) -> dict[str, Any]:
        journal = self._require_bound_operative(_thread_identity(request))
        if journal["state"] == "aborted":
            return {
                "ok": False,
                "takeover_id": journal["takeover_id"],
                "state": "aborted",
                "reused": True,
            }
        if journal["state"] != "active":
            raise StoreError(
                f"cannot abort an operative takeover in {journal['state']}"
            )
        reason = request.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise StoreError("operative abort requires an explicit human reason")
        evidence_path, evidence = self._read_operative_evidence(request.get("evidence"))
        self.store.record_operative_evidence(
            str(journal["takeover_id"]),
            "abort",
            "abort",
            coverage="observed",
            target_type="operative_takeover",
            target_id=journal["takeover_id"],
            detail={"reason": reason.strip(), "content": evidence},
            artifact_path=evidence_path,
        )
        aborted_at = utc_now()
        aborted = transition_journal(
            self.paths.operative_journal,
            journal,
            "aborted",
            now=aborted_at,
            next_step="explicit human recovery of this same takeover",
            completed_effect="abort_evidence_retained",
            aborted_at=aborted_at,
            evidence_path=evidence_path,
            result={"reason": reason.strip()},
        )
        self.operative_journal = aborted
        self.store.mirror_operative_journal(aborted)
        return {
            "ok": False,
            "takeover_id": journal["takeover_id"],
            "state": "aborted",
            "reused": False,
        }

    async def _recover_operative(self, request: dict[str, Any]) -> dict[str, Any]:
        journal = self.operative_journal
        if journal is None or journal.get("state") != "aborted":
            raise StoreError("no aborted operative takeover is available for recovery")
        if request.get("takeover_id") != journal["takeover_id"]:
            raise StoreError("operative recovery requires the exact takeover ID")
        thread_id = _thread_identity(request)
        existing = self.store.row(
            "SELECT * FROM tasks WHERE native_thread_id = ?", (thread_id,)
        )
        if existing is not None and existing["role"] != "operative":
            raise StoreError("managed agents cannot recover as Operative")
        evidence_path, evidence = self._read_operative_evidence(request.get("evidence"))
        old_thread = str(journal["native_thread_id"])
        self.store.record_operative_evidence(
            str(journal["takeover_id"]),
            f"recovery:{thread_id}",
            "successor_recovery",
            coverage="observed",
            target_type="thread",
            target_id=old_thread,
            detail={"successor_thread_id": thread_id, "content": evidence},
            artifact_path=evidence_path,
        )
        if thread_id != old_thread:
            self.store.execute(
                """UPDATE actions SET state = 'canceled', condition = ?, updated_at = ?
                   WHERE id = ? AND state IN ('pending','starting','active','terminal','uncertain')""",
                (
                    "superseded by explicit operative recovery",
                    utc_now(),
                    journal["action_id"],
                ),
            )
            self.store.execute(
                "UPDATE tasks SET state = 'retired', updated_at = ? WHERE id = ?",
                (utc_now(), journal["task_id"]),
            )
        superseded = list(journal["superseded_thread_ids"])
        if thread_id != old_thread and old_thread not in superseded:
            superseded.append(old_thread)
        acquiring = transition_journal(
            self.paths.operative_journal,
            journal,
            "acquiring",
            now=utc_now(),
            next_step="bind the explicitly authorized recovery thread",
            completed_effect="explicit_recovery_authorized",
            native_thread_id=thread_id,
            superseded_thread_ids=superseded,
            task_id=None if thread_id != old_thread else journal.get("task_id"),
            action_id=None if thread_id != old_thread else journal.get("action_id"),
            caller_verification={
                "coverage": "observed",
                "method": "explicit recovery command with exact takeover ID",
                "native_thread_id": thread_id,
                "managed_agent": False,
            },
        )
        self.operative_journal = acquiring
        self.store.mirror_operative_journal(acquiring)
        return await self._resume_operative_acquisition(request)

    def _resolve_direct_sage_target(self, item_id: str) -> dict[str, Any]:
        bead = self.store.row("SELECT * FROM beads WHERE bead_id = ?", (item_id,))
        if bead is None:
            raise StoreError(
                f"Sage target {item_id!r} is not a retained Bead; pass the exact "
                "work-item ID shown by Fulcrum"
            )
        workflows = self.store.rows(
            """SELECT boundary.* FROM workflow_cost_beads bead
               JOIN workflow_cost_boundaries boundary
                 ON boundary.workflow_id = bead.workflow_id
               WHERE bead.bead_id = ? ORDER BY boundary.workflow_id""",
            (item_id,),
        )
        if len(workflows) > 1:
            identities = ", ".join(str(row["workflow_id"]) for row in workflows)
            raise StoreError(
                f"Sage target {item_id!r} is ambiguous across retained causal "
                f"workflows: {identities}; repair the workflow relation first"
            )
        assignment = self.store.row(
            """SELECT assignment.*, run.project_id, run.state AS run_state
               FROM assignments assignment JOIN runs run ON run.id = assignment.run_id
               WHERE assignment.bead_id = ?
               ORDER BY CASE
                   WHEN assignment.stage = 'completed' THEN 0
                   WHEN assignment.stage != 'canceled' THEN 1
                   ELSE 2 END,
                   assignment.id DESC LIMIT 1""",
            (item_id,),
        )
        if assignment is None:
            raise StoreError(
                f"Sage target {item_id!r} has no retained Executor/Overseer "
                "assignment relation"
            )
        pair: dict[str, dict[str, Any]] = {}
        for role, column in (
            ("executor", "executor_task_id"),
            ("overseer", "overseer_task_id"),
        ):
            task_id = assignment.get(column)
            if task_id is None:
                raise StoreError(
                    f"Sage target {item_id!r} assignment {assignment['id']} "
                    f"has no retained {role.title()} relation"
                )
            task = self.store.row("SELECT * FROM tasks WHERE id = ?", (task_id,))
            if task is None or task["role"] != role:
                raise StoreError(
                    f"Sage target {item_id!r} assignment {assignment['id']} "
                    f"has an invalid {role.title()} relation"
                )
            if task["pair_id"] != assignment["run_id"]:
                raise StoreError(
                    f"Sage target {item_id!r} assignment {assignment['id']} "
                    f"has a mismatched {role.title()} workflow relation"
                )
            pair[role] = task
        return {
            "bead": bead,
            "workflow": workflows[0] if workflows else None,
            "assignment": assignment,
            **pair,
        }

    def _direct_sage_evidence(self, target: dict[str, Any]) -> dict[str, Any]:
        """Freeze a bounded snapshot selected by exact retained task relations."""

        item_id = str(target["bead"]["bead_id"])
        workflow = target.get("workflow")
        workflow_id = str(workflow["workflow_id"]) if workflow is not None else None
        if workflow_id is not None:
            action_rows = self.store.rows(
                """SELECT action.*, task.role, task.title, task.native_thread_id,
                          task.role_number, task.project_id, task.model,
                          task.reasoning_effort, causal.causal_role,
                          causal.include_cost, causal.exclusion_reason
                   FROM workflow_cost_actions causal
                   JOIN actions action ON action.id = causal.action_id
                   JOIN tasks task ON task.id = action.task_id
                   WHERE causal.workflow_id = ? ORDER BY action.id LIMIT 201""",
                (workflow_id,),
            )
        else:
            action_rows = self.store.rows(
                """SELECT action.*, task.role, task.title, task.native_thread_id,
                          task.role_number, task.project_id, task.model,
                          task.reasoning_effort, task.role AS causal_role,
                          1 AS include_cost, NULL AS exclusion_reason
                   FROM actions action JOIN tasks task ON task.id = action.task_id
                   WHERE action.assignment_id = ? ORDER BY action.id LIMIT 201""",
                (target["assignment"]["id"],),
            )
        selected_actions = action_rows[:200]
        action_ids = [int(row["id"]) for row in selected_actions]
        action_records: list[dict[str, Any]] = []
        all_turns: dict[tuple[str, str], dict[str, Any]] = {}
        usage_by_action: list[dict[str, Any]] = []
        for row in selected_actions:
            dispatch = self.store.row(
                """SELECT id, input_json, state, condition, attempt_count,
                          created_at, completed_at
                   FROM external_operations WHERE kind = 'turn_start' AND target = ?
                   ORDER BY id DESC LIMIT 1""",
                (str(row["id"]),),
            )
            dispatched_prompt = None
            if dispatch is not None:
                inputs = _json_object(dispatch.get("input_json"))
                prompt = inputs.get("prompt")
                if isinstance(prompt, str):
                    dispatched_prompt = {
                        "source": "retained historical turn_start prompt",
                        **_bounded_excerpt(prompt, 6000),
                    }
            action_records.append(
                {
                    "id": row["id"],
                    "kind": row["kind"],
                    "role": row["role"],
                    "causal_role": row["causal_role"],
                    "task": row["title"],
                    "assignment_id": row["assignment_id"],
                    "occurrence_id": row["occurrence_id"],
                    "state": row["state"],
                    "native_turn_id": row["native_turn_id"],
                    "outcome_kind": row["outcome_kind"],
                    "payload": _bounded_json(row["payload"], 4000),
                    "outcome": _bounded_json(row["outcome_payload"], 4000),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "cost_included": bool(row["include_cost"]),
                    "cost_exclusion": row["exclusion_reason"],
                    "dispatched_prompt": dispatched_prompt,
                }
            )
            report = self.store.usage_report(
                action_id=int(row["id"]), group_by="action"
            )
            summary = (
                report["groups"][0]
                if report["groups"]
                else {
                    "group": row["id"],
                    "action_ids": [row["id"]],
                    "direct": _empty_token_totals(),
                    "attributed": _empty_token_totals(),
                    "coverage": "unknown",
                    "direct_turn_count": 0,
                    "attributed_turn_count": 0,
                    "helper_turn_count": 0,
                    "unobserved_helper_count": 0,
                }
            )
            summary = {
                **summary,
                "gap_reasons": sorted(
                    {
                        str(turn["gap_reason"])
                        for turn in report["turns"]
                        if turn.get("gap_reason")
                    }
                ),
            }
            usage_by_action.append(summary)
            for turn in report["turns"]:
                all_turns[(turn["native_thread_id"], turn["native_turn_id"])] = turn

        if workflow_id is not None:
            workflow_beads = self.store.rows(
                """SELECT bead.bead_id, bead.intake_key, bead.title, bead.project_id,
                          bead.activation, bead.publication_state
                   FROM workflow_cost_beads causal JOIN beads bead
                     ON bead.bead_id = causal.bead_id
                   WHERE causal.workflow_id = ? ORDER BY bead.bead_id LIMIT 101""",
                (workflow_id,),
            )
            assignment_rows = self.store.rows(
                """SELECT assignment.*, run.project_id, run.state AS run_state
                   FROM assignments assignment JOIN runs run ON run.id = assignment.run_id
                   JOIN workflow_cost_beads causal ON causal.bead_id = assignment.bead_id
                   WHERE causal.workflow_id = ? ORDER BY assignment.id LIMIT 101""",
                (workflow_id,),
            )
        else:
            workflow_beads = [target["bead"]]
            assignment_rows = [target["assignment"]]
        assignment_ids = sorted(
            {int(row["id"]) for row in assignment_rows[:100]}
            | {int(target["assignment"]["id"])}
        )
        handoffs: list[dict[str, Any]] = []
        handoff_row_count = 0
        if assignment_ids:
            marks = ",".join("?" for _ in assignment_ids)
            retained = self.store.rows(
                f"""SELECT * FROM handoffs WHERE assignment_id IN ({marks})
                    ORDER BY id LIMIT 101""",
                assignment_ids,
            )
            handoff_row_count = len(retained)
            handoffs = [
                {
                    **{
                        key: row[key]
                        for key in (
                            "id",
                            "assignment_id",
                            "source_action_id",
                            "kind",
                            "created_at",
                        )
                    },
                    "content": _bounded_json(row["content_json"], 4000),
                }
                for row in retained[:100]
            ]

        selected_beads = workflow_beads[:100]
        if not any(row["bead_id"] == item_id for row in selected_beads):
            selected_beads = [
                *workflow_beads[:99],
                {
                    key: target["bead"].get(key)
                    for key in (
                        "bead_id",
                        "intake_key",
                        "title",
                        "project_id",
                        "activation",
                        "publication_state",
                    )
                },
            ]
        selected_assignments = assignment_rows[:100]
        target_assignment_id = int(target["assignment"]["id"])
        if not any(
            int(row["id"]) == target_assignment_id for row in selected_assignments
        ):
            selected_assignments = [*assignment_rows[:99], target["assignment"]]
        candidate_ids = sorted(
            {
                str(row["candidate_id"])
                for row in selected_assignments
                if row.get("candidate_id")
            }
        )
        task_thread_ids = sorted(
            {
                str(value)
                for value in (
                    *[row.get("native_thread_id") for row in selected_actions],
                    target["executor"].get("native_thread_id"),
                    target["overseer"].get("native_thread_id"),
                )
                if value
            }
        )
        operation_targets_by_kind = {
            "beads_close": sorted({str(row["bead_id"]) for row in selected_beads}),
            "beads_create": sorted({str(row["intake_key"]) for row in selected_beads}),
            "tollgate_approve": candidate_ids,
            "tollgate_candidate_create": [str(value) for value in assignment_ids],
            "tollgate_worktree_create": [str(value) for value in assignment_ids],
            "thread_archive": task_thread_ids,
            "turn_start": [str(value) for value in action_ids],
        }
        operation_clauses: list[str] = []
        operation_values: list[Any] = []
        for kind, targets in operation_targets_by_kind.items():
            if not targets:
                continue
            marks = ",".join("?" for _ in targets)
            operation_clauses.append(f"(kind = ? AND target IN ({marks}))")
            operation_values.extend([kind, *targets])
        if task_thread_ids:
            marks = ",".join("?" for _ in task_thread_ids)
            operation_clauses.append(
                f"(kind = 'thread_start' AND native_id IN ({marks}))"
            )
            operation_values.extend(task_thread_ids)
        operations = (
            self.store.rows(
                f"""SELECT * FROM external_operations
                    WHERE {' OR '.join(operation_clauses)} ORDER BY id LIMIT 101""",
                operation_values,
            )
            if operation_clauses
            else []
        )
        selected_operations = operations[:100]
        operation_ids = [int(row["id"]) for row in selected_operations]
        attempts: dict[int, list[dict[str, Any]]] = {}
        attempt_rows: list[dict[str, Any]] = []
        if operation_ids:
            marks = ",".join("?" for _ in operation_ids)
            attempt_rows = self.store.rows(
                f"""SELECT * FROM operation_attempts
                    WHERE operation_id IN ({marks}) ORDER BY id LIMIT 201""",
                operation_ids,
            )
            for row in attempt_rows[:200]:
                attempts.setdefault(int(row["operation_id"]), []).append(
                    {
                        "attempt": row["attempt"],
                        "state": row["state"],
                        "duration_ms": row["duration_ms"],
                        "error": _bounded_plain(row["error"], 1000),
                        "started_at": row["started_at"],
                        "finished_at": row["finished_at"],
                    }
                )
        operation_records = [
            {
                "id": row["id"],
                "kind": row["kind"],
                "target": row["target"],
                "state": row["state"],
                "condition": _bounded_plain(row["condition"], 1000),
                "attempt_count": row["attempt_count"],
                "native_id": row["native_id"],
                "created_at": row["created_at"],
                "completed_at": row["completed_at"],
                "attempts": attempts.get(int(row["id"]), []),
            }
            for row in selected_operations
        ]

        events = self._direct_sage_events(
            item_id=item_id,
            workflow_id=workflow_id,
            action_ids=action_ids,
            assignment_ids=assignment_ids,
            task_ids=sorted({int(row["task_id"]) for row in selected_actions}),
            run_ids=sorted({int(row["run_id"]) for row in selected_assignments}),
            operation_ids=operation_ids,
        )
        turns = list(all_turns.values())
        action_roles = {int(row["id"]): str(row["role"]) for row in selected_actions}
        usage_by_role = []
        for role in sorted(set(action_roles.values())):
            role_action_ids = {
                action_id for action_id, owner in action_roles.items() if owner == role
            }
            direct = [row for row in turns if row.get("action_id") in role_action_ids]
            attributed = [
                row
                for row in turns
                if row.get("attributed_action_id") in role_action_ids
            ]
            usage_by_role.append(
                {
                    "role": role,
                    "action_ids": sorted(role_action_ids),
                    "direct": _raw_token_totals(direct),
                    "attributed": _raw_token_totals(attributed),
                    "direct_turn_count": len(direct),
                    "attributed_turn_count": len(attributed),
                    "helper_turn_count": sum(
                        int(row["is_helper"]) for row in attributed
                    ),
                    "coverage": _raw_usage_coverage(attributed or direct),
                    "gap_reasons": sorted(
                        {
                            str(row["gap_reason"])
                            for row in attributed or direct
                            if row.get("gap_reason")
                        }
                    ),
                }
            )
        turn_records = [
            {
                key: row.get(key)
                for key in (
                    "native_thread_id",
                    "native_turn_id",
                    "action_id",
                    "attributed_action_id",
                    "is_helper",
                    "model",
                    "reasoning_effort",
                    "total_input_tokens",
                    "total_cached_input_tokens",
                    "total_cache_write_input_tokens",
                    "total_output_tokens",
                    "total_reasoning_output_tokens",
                    "total_tokens",
                    "coverage",
                    "gap_reason",
                    "terminal_at",
                )
            }
            for row in turns[:200]
        ]
        existing_beads = self.store.rows(
            """SELECT bead_id, title, description, activation, publication_state
               FROM beads WHERE project_id = ? ORDER BY updated_at DESC LIMIT 51""",
            (target["bead"]["project_id"],),
        )
        existing_bead_records = [
            {
                "bead_id": row["bead_id"],
                "title": row["title"],
                "description": _bounded_plain(row["description"], 1000),
                "activation": row["activation"],
                "publication_state": row["publication_state"],
            }
            for row in existing_beads[:50]
        ]
        frozen = (
            self.store.cost_report(workflow_id=workflow_id, group_by="workflow")
            if workflow_id is not None
            else self.store.usage_report(
                assignment_id=int(target["assignment"]["id"]),
                group_by="assignment",
            )
        )
        return {
            "target": {
                "item": item_id,
                "title": target["bead"]["title"],
                "project": target["bead"]["project_id"],
                "workflow_id": workflow_id,
                "workflow_state": workflow.get("state") if workflow else None,
                "selection_mode": (
                    "causal_workflow" if workflow_id is not None else "assignment"
                ),
                "assignment_id": target["assignment"]["id"],
                "assignment_stage": target["assignment"]["stage"],
                "run_id": target["assignment"]["run_id"],
                "run_state": target["assignment"]["run_state"],
                "executor": _task_identity(target["executor"]),
                "overseer": _task_identity(target["overseer"]),
            },
            "captured_at": utc_now(),
            "coverage": {
                "selection": (
                    "exact workflow_cost_beads/workflow_cost_actions causal joins"
                    if workflow_id is not None
                    else "exact Bead assignment and Executor/Overseer task relations"
                ),
                "project_time_window_used": False,
                "workflow_beads": {
                    "count": len(workflow_beads[:100]),
                    "limit": 100,
                    "truncated": len(workflow_beads) > 100,
                },
                "actions": {
                    "count": len(selected_actions),
                    "limit": 200,
                    "truncated": len(action_rows) > 200,
                },
                "handoffs": {
                    "limit": 100,
                    "truncated": handoff_row_count > 100,
                },
                "external_operations": {
                    "count": len(selected_operations),
                    "limit": 100,
                    "truncated": len(operations) > 100,
                    "selection": "operation-kind-specific retained causal identities",
                },
                "operation_attempts": {
                    "count": len(attempt_rows[:200]),
                    "limit": 200,
                    "truncated": len(attempt_rows) > 200,
                },
                "candidate_evidence": {
                    "count": len(selected_assignments),
                    "limit": 100,
                    "truncated": len(assignment_rows) > 100,
                    "selection": (
                        "assignments joined through causal workflow Beads"
                        if workflow_id is not None
                        else "exact retained assignment for the named Bead"
                    ),
                },
                "controller_events": {
                    "limit": 200,
                    "truncated": len(events) > 200,
                },
                "token_turns": {
                    "count": len(turn_records),
                    "limit": 200,
                    "truncated": len(turns) > 200,
                },
                "existing_beads": {
                    "limit": 50,
                    "truncated": len(existing_beads) > 50,
                    "selection": "same affected project, newest retained first",
                },
                "native_history": (
                    "only retained historical dispatch prompts are included; full native "
                    "task histories are unavailable unless separately supplied"
                ),
                "current_prompt": (
                    "the invoking human prompt may supplement evidence only when labelled current"
                ),
                "unknown_is_not_zero": True,
            },
            "workflow_beads": selected_beads,
            "workflow_actions": action_records,
            "handoffs": handoffs,
            "external_operations": operation_records,
            "controller_events": events[:200],
            "candidate_evidence": [
                {
                    key: row.get(key)
                    for key in (
                        "id",
                        "run_id",
                        "bead_id",
                        "stage",
                        "prior_stage",
                        "candidate_id",
                        "source_oid",
                        "tested_oid",
                        "review_failures",
                        "retry_count",
                        "completion_kind",
                        "completion_evidence",
                        "condition",
                        "created_at",
                        "updated_at",
                    )
                }
                for row in selected_assignments
            ],
            "token_usage": {
                "by_action": usage_by_action,
                "by_role": usage_by_role,
                "contributing_turns": turn_records,
            },
            "frozen_accounting": {
                "boundary": (
                    {
                        key: workflow.get(key)
                        for key in (
                            "workflow_id",
                            "state",
                            "origin_action_id",
                            "frozen_amount",
                            "currency",
                            "frozen_summary",
                            "coverage",
                            "assumptions",
                            "exclusions",
                            "closed_at",
                        )
                    }
                    if workflow is not None
                    else None
                ),
                "cost_report": frozen,
                "limitation": (
                    None
                    if workflow is not None
                    else "no workflow cost boundary was retained for this assignment"
                ),
            },
            "existing_beads": existing_bead_records,
        }

    def _direct_sage_events(
        self,
        *,
        item_id: str,
        workflow_id: str | None,
        action_ids: list[int],
        assignment_ids: list[int],
        task_ids: list[int],
        run_ids: list[int],
        operation_ids: list[int],
    ) -> list[dict[str, Any]]:
        relations: list[str] = []
        values: list[Any] = []
        for entity_type, identifiers in (
            ("action", action_ids),
            ("assignment", assignment_ids),
            ("task", task_ids),
            ("run", run_ids),
            ("operation", operation_ids),
        ):
            if not identifiers:
                continue
            marks = ",".join("?" for _ in identifiers)
            relations.append(f"(entity_type = ? AND entity_id IN ({marks}))")
            values.extend([entity_type, *(str(value) for value in identifiers)])
        if workflow_id is not None:
            relations.extend(
                [
                    "(entity_type = 'workflow' AND entity_id = ?)",
                    "detail_json LIKE ?",
                ]
            )
            values.extend([workflow_id, f"%{workflow_id}%"])
        relations.append("detail_json LIKE ?")
        values.append(f"%{item_id}%")
        rows = self.store.rows(
            f"""SELECT id, kind, entity_type, entity_id, message, detail_json,
                       created_at FROM events WHERE {' OR '.join(relations)}
                ORDER BY id LIMIT 201""",
            values,
        )
        return [
            {
                "id": row["id"],
                "kind": row["kind"],
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "message": _bounded_plain(row["message"], 1000),
                "detail": _bounded_json(row["detail_json"], 2000),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    async def _register_sage(self, request: dict[str, Any]) -> dict[str, Any]:
        thread_id = _thread_identity(request)
        raw_item = request.get("item")
        if not isinstance(raw_item, str) or not raw_item.strip():
            raise StoreError("Sage registration requires an exact nonempty --item")
        item_id = raw_item.strip()
        if not re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+", item_id):
            raise StoreError(
                "Sage registration --item must be an exact Bead ID containing "
                "letters, numbers, and hyphens"
            )
        raw_description = request.get("description")
        if not isinstance(raw_description, str):
            raise StoreError("Sage registration requires a safe 3-8 word description")
        description = " ".join(raw_description.split())
        if not (
            3 <= len(description.split()) <= 8
            and re.fullmatch(r"[A-Za-z0-9]+(?:[ -][A-Za-z0-9]+)*", description)
        ):
            raise StoreError(
                "Sage registration description must be 3-8 words using only "
                "letters, numbers, spaces, and hyphens"
            )

        existing_task = self.store.row(
            "SELECT * FROM tasks WHERE native_thread_id = ?", (thread_id,)
        )
        if existing_task is not None and existing_task["role"] != "sage":
            raise StoreError("thread is already bound with different authority")
        existing_occurrence = None
        if existing_task is not None:
            existing_occurrence = self.store.row(
                """SELECT occurrence.* FROM occurrences occurrence
                   JOIN actions action ON action.occurrence_id = occurrence.id
                   WHERE action.task_id = ? AND occurrence.authority = 'human-skill'
                   ORDER BY occurrence.id LIMIT 1""",
                (existing_task["id"],),
            )
            if existing_occurrence is not None:
                retained_scope = _occurrence_scope(existing_occurrence["scope"])
                if retained_scope.get("item") != item_id:
                    raise StoreError(
                        "this native task is already registered to Sage target "
                        f"{retained_scope.get('item')!r}"
                    )

        # Resolve every required relationship before task allocation or native rename.
        target = self._resolve_direct_sage_target(item_id)
        project_id = str(target["bead"]["project_id"])
        task = self.store.register_task(
            native_thread_id=thread_id,
            role="sage",
            description=description,
            model="gpt-5.6-sol",
            reasoning_effort="high",
            project_id=project_id,
            state="provisioning",
        )
        await self.runtime.set_name(thread_id, task["title"])
        facts = await self._refresh_task(task)
        evidence = self._direct_sage_evidence(target)
        occurrence = existing_occurrence
        action = None
        if occurrence is not None:
            action = self.store.row(
                """SELECT * FROM actions WHERE occurrence_id = ?
                   AND kind = 'specialist' ORDER BY id LIMIT 1""",
                (occurrence["id"],),
            )
        if occurrence is None or action is None:
            timestamp = utc_now()
            scope = {
                "global": False,
                "projects": [project_id],
                "direct_item": True,
                "item": item_id,
                "workflow_id": (
                    target["workflow"]["workflow_id"]
                    if target["workflow"] is not None
                    else None
                ),
                "assignment_id": target["assignment"]["id"],
                "executor_task_id": target["executor"]["id"],
                "overseer_task_id": target["overseer"]["id"],
            }
            with self.store.transaction() as connection:
                if occurrence is None:
                    cursor = connection.execute(
                        """INSERT INTO occurrences(
                               kind, scope, authority, prompt, state, evidence_json,
                               created_at, updated_at
                           ) VALUES ('sage', ?, 'human-skill', ?, 'active', ?, ?, ?)""",
                        (
                            json.dumps(scope, sort_keys=True),
                            f"Investigate retained work item {item_id}: {description}",
                            json.dumps(evidence, sort_keys=True),
                            timestamp,
                            timestamp,
                        ),
                    )
                    occurrence = connection.execute(
                        "SELECT * FROM occurrences WHERE id = ?",
                        (cursor.lastrowid,),
                    ).fetchone()
                assert occurrence is not None
                if action is None:
                    payload = {
                        "direct_item": True,
                        "scope": scope,
                        "prompt": (
                            f"Investigate only {item_id} and its exact retained task "
                            "relations. Use a causal workflow when retained, otherwise "
                            "use its assignment and Executor/Overseer pair. The invoking "
                            "human request is current context, not historical evidence."
                        ),
                        "retained_evidence": evidence,
                        "required_interviews": {
                            "executor": _task_identity(target["executor"]),
                            "overseer": _task_identity(target["overseer"]),
                        },
                        "adopt_current_turn": bool(
                            facts["runtime_status"] == "active"
                            and facts["last_turn_id"] is None
                        ),
                    }
                    cursor = connection.execute(
                        """INSERT INTO actions(
                               task_id, occurrence_id, kind, payload, state,
                               native_turn_id, created_at, updated_at
                           ) VALUES (?, ?, 'specialist', ?, 'active', ?, ?, ?)""",
                        (
                            task["id"],
                            occurrence["id"],
                            json.dumps(payload, sort_keys=True),
                            facts["last_turn_id"],
                            timestamp,
                            timestamp,
                        ),
                    )
                    action = connection.execute(
                        "SELECT * FROM actions WHERE id = ?", (cursor.lastrowid,)
                    ).fetchone()
        occurrence = self.store.row(
            "SELECT * FROM occurrences WHERE id = ?", (occurrence["id"],)
        )
        action = self.store.row("SELECT * FROM actions WHERE id = ?", (action["id"],))
        assert occurrence is not None and action is not None
        if isinstance(facts["last_turn_id"], str) and action["native_turn_id"] is None:
            self._adopt_unbound_weaver_turn(int(task["id"]), facts)
            action = self.store.row(
                "SELECT * FROM actions WHERE id = ?", (action["id"],)
            )
            if action is None:
                raise StoreError("human Sage action disappeared during turn adoption")
        if isinstance(action["native_turn_id"], str):
            self.store.bind_action_turn(
                int(action["id"]), thread_id, str(action["native_turn_id"])
            )
        self.store.link_action_to_workflow(
            f"specialist:{occurrence['id']}",
            int(action["id"]),
            causal_role="sage",
            origin_action_id=int(action["id"]),
        )
        self.store.execute(
            """UPDATE tasks SET state = 'active', archive_eligible_at = NULL,
               archive_idle_turn_id = NULL, updated_at = ? WHERE id = ?""",
            (utc_now(), task["id"]),
        )
        return {
            "thread_id": thread_id,
            "title": task["title"],
            "role_number": task["role_number"],
            "target": {
                "item": item_id,
                "project": project_id,
                "workflow_id": (
                    target["workflow"]["workflow_id"]
                    if target["workflow"] is not None
                    else None
                ),
                "assignment_id": target["assignment"]["id"],
                "executor": _task_identity(target["executor"]),
                "overseer": _task_identity(target["overseer"]),
            },
            "occurrence_id": occurrence["id"],
            "action_id": action["id"],
            "instructions": direct_sage_instructions(action),
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
        try:
            await self._setup_smoke_check()
        except StoreError as error:
            await self._deliver_update_batch(recovery_only=True)
            return {
                "ready": False,
                "archon": archon["native_thread_id"],
                "condition": str(error),
            }
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
        previous = self.store.row("""SELECT * FROM external_operations
               WHERE kind = 'setup_runtime_smoke' ORDER BY id DESC LIMIT 1""")
        if previous is not None and previous["state"] == "complete":
            self.store.execute(
                "INSERT INTO meta(key, value) VALUES ('desktop_smoke_check', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (previous["completed_at"] or previous["updated_at"],),
            )
            return
        if previous is not None and previous["state"] in {
            "intent",
            "sent",
            "uncertain",
        }:
            if previous["state"] == "intent":
                self.store.execute(
                    """UPDATE external_operations SET state = 'canceled',
                       condition = 'confirmed unsent before setup smoke retry', updated_at = ?
                       WHERE id = ?""",
                    (utc_now(), previous["id"]),
                )
            else:
                if previous["state"] == "sent":
                    self.store.execute(
                        """UPDATE external_operations SET state = 'uncertain',
                           condition = 'setup resumed after dispatch; effect requires observation',
                           updated_at = ? WHERE id = ?""",
                        (utc_now(), previous["id"]),
                    )
                retained = self.store.row(
                    "SELECT * FROM external_operations WHERE id = ?", (previous["id"],)
                )
                assert retained is not None
                if not retained["reconciliation_used"]:
                    try:
                        await self._reconcile_setup_runtime_smoke(retained)
                    except AppServerError:
                        pass
                retained = self.store.row(
                    "SELECT * FROM external_operations WHERE id = ?", (previous["id"],)
                )
                assert retained is not None
                if retained["state"] == "complete":
                    self.store.execute(
                        "INSERT INTO meta(key, value) VALUES ('desktop_smoke_check', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (retained["completed_at"] or retained["updated_at"],),
                    )
                    return
                if retained["reconciliation_used"] and retained["operator_hold_id"]:
                    self._queue_operation_resolution(
                        retained,
                        hold_id=int(retained["operator_hold_id"]),
                        condition=str(
                            retained["condition"]
                            or "setup runtime smoke remains ambiguous"
                        ),
                    )
                raise StoreError(
                    f"setup runtime smoke operation {retained['id']} remains uncertain; "
                    "resolve it before another smoke mutation"
                )
        project = self.store.row(
            "SELECT * FROM projects WHERE enabled = 1 ORDER BY project_id LIMIT 1"
        )
        if project is None:
            return
        await self._admit_runtime_start(
            "setup smoke thread start", str(project["project_id"])
        )
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
                workspace_root=project["repo_path"],
                model=self.config.archon_model or "gpt-5.6-sol",
                project_id=project["codex_project_id"],
            )
            thread_id = result["thread"]["id"]
            self.store.execute(
                "UPDATE external_operations SET native_id = ?, updated_at = ? WHERE id = ?",
                (thread_id, utc_now(), operation),
            )
            try:
                await self._admit_runtime_start(
                    "setup smoke turn start", str(thread_id)
                )
            except ResourceAdmissionPaused as error:
                try:
                    await self.runtime.archive(thread_id)
                    partial_archived = True
                except Exception as archive_error:
                    try:
                        await self.runtime.unsubscribe(thread_id)
                    except Exception as cleanup_error:
                        raise AppServerError(
                            "resource admission paused after setup thread creation and "
                            "partial-thread archive/unsubscribe cleanup failed: "
                            f"archive={archive_error}; unsubscribe={cleanup_error}"
                        ) from cleanup_error
                    partial_archived = False
                self.store.finish_operation_attempt(
                    operation,
                    attempt,
                    state="failed",
                    result={
                        "partial_thread_archived": partial_archived,
                        "partial_thread_unsubscribed": True,
                    },
                    error=str(error),
                    native_id=str(thread_id),
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
                raise
            await self.runtime.set_name(thread_id, "Fulcrum setup visibility check")
            turn_id = await self.runtime.start_turn(
                thread_id,
                "Disposable Fulcrum installation visibility check. Do not start work.\n\n"
                "Reply with exactly: Fulcrum runtime check passed. Do not use tools or modify files.",
                cwd=project["repo_path"],
                workspace_root=project["repo_path"],
                model=self.config.archon_model or "gpt-5.6-sol",
                effort=self.config.archon_reasoning_effort or "medium",
                correlation=f"fulcrum-operation-{operation}",
            )
            smoke_inputs = {
                "cwd": project["repo_path"],
                "project_id": project["codex_project_id"],
                "smoke_turn_id": turn_id,
            }
            self.store.execute(
                "UPDATE external_operations SET input_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(smoke_inputs, sort_keys=True), utc_now(), operation),
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
            if current is not None and current["state"] in {
                "intent",
                "sent",
                "uncertain",
            }:
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
                    try:
                        await self._reconcile_setup_runtime_smoke(retained)
                    except AppServerError:
                        pass
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
            codex_id = _find_codex_project_id(
                codex_projects,
                project.repo_path,
                preferred_id=project.codex_project_id,
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
        ready = (
            state_ready
            and progress_ready
            and self.runtime.ready
            and not self._operative_fenced()
        )
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
        content = self._with_workflow_context(content)
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

    def _workflow_ids_for_update(self, content: dict[str, Any]) -> set[str]:
        """Resolve durable causal identities for one controller-to-Archon fact."""

        workflow_ids: set[str] = set()
        workflow_id = content.get("workflow_id")
        if isinstance(workflow_id, str) and workflow_id:
            workflow_ids.add(workflow_id)
        retained_ids = content.get("workflow_ids")
        if isinstance(retained_ids, list):
            workflow_ids.update(
                item for item in retained_ids if isinstance(item, str) and item
            )

        bead_id = content.get("bead_id")
        if isinstance(bead_id, str):
            workflow_ids.update(
                str(row["workflow_id"])
                for row in self.store.rows(
                    "SELECT workflow_id FROM workflow_cost_beads WHERE bead_id = ?",
                    (bead_id,),
                )
            )

        assignment_id = content.get("assignment_id")
        if isinstance(assignment_id, int) and not isinstance(assignment_id, bool):
            workflow_ids.update(
                str(row["workflow_id"])
                for row in self.store.rows(
                    """SELECT workflow.workflow_id FROM assignments assignment
                       JOIN workflow_cost_beads workflow
                         ON workflow.bead_id = assignment.bead_id
                       WHERE assignment.id = ?""",
                    (assignment_id,),
                )
            )
            workflow_ids.update(
                str(row["workflow_id"])
                for row in self.store.rows(
                    """SELECT DISTINCT workflow.workflow_id
                       FROM actions action JOIN workflow_cost_actions workflow
                         ON workflow.action_id = action.id
                       WHERE action.assignment_id = ?""",
                    (assignment_id,),
                )
            )

        action_id = content.get("action_id")
        if isinstance(action_id, int) and not isinstance(action_id, bool):
            workflow_ids.update(
                str(row["workflow_id"])
                for row in self.store.rows(
                    "SELECT workflow_id FROM workflow_cost_actions WHERE action_id = ?",
                    (action_id,),
                )
            )

        target_type = content.get("target_type")
        target_id = content.get("target_id")
        if (
            target_type == "action"
            and isinstance(target_id, int)
            and not isinstance(target_id, bool)
        ):
            workflow_ids.update(
                str(row["workflow_id"])
                for row in self.store.rows(
                    "SELECT workflow_id FROM workflow_cost_actions WHERE action_id = ?",
                    (target_id,),
                )
            )
        elif (
            target_type == "assignment"
            and isinstance(target_id, int)
            and not isinstance(target_id, bool)
        ):
            workflow_ids.update(
                self._workflow_ids_for_update({"assignment_id": target_id})
            )

        if content.get("kind") == "archon_succession_completed":
            workflow_ids.update(
                str(row["workflow_id"])
                for row in self.store.rows(
                    """SELECT workflow_id FROM workflow_cost_boundaries
                       WHERE state = 'open' ORDER BY workflow_id"""
                )
            )
        return workflow_ids

    def _with_workflow_context(self, content: dict[str, Any]) -> dict[str, Any]:
        retained = dict(content)
        workflow_ids = sorted(self._workflow_ids_for_update(retained))
        if len(workflow_ids) == 1:
            retained["workflow_id"] = workflow_ids[0]
            retained.pop("workflow_ids", None)
        elif workflow_ids:
            retained["workflow_ids"] = workflow_ids
        return retained

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
            "SELECT * FROM tasks WHERE role = 'archon' AND state NOT IN ('retired','archived')"
        )
        if current is not None:
            recovered = self.store.row(
                """SELECT 1 FROM external_operations
                   WHERE kind = 'thread_start' AND target = 'archon'
                     AND state = 'complete' AND native_id = ?
                     AND json_extract(input_json, '$.succession_task_id') = ?
                   ORDER BY id DESC LIMIT 1""",
                (current["native_thread_id"], old_task_id),
            )
            if recovered is None:
                raise StoreError("Archon succession found an unexpected current Archon")
            successor = current
        else:
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
                succession_task_id=old_task_id,
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
            workflow = self.store.row(
                """SELECT workflow_id FROM workflow_cost_beads
                   WHERE bead_id = ? ORDER BY created_at LIMIT 1""",
                (bead["bead_id"],),
            )
            content = json.dumps(
                {
                    "kind": "proposal",
                    "bead_id": bead["bead_id"],
                    "project": bead["project_id"],
                    "title": bead["title"],
                    "scope_summary": self._scope_summary(bead["description"]),
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
                    "workflow_id": workflow["workflow_id"] if workflow else None,
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

    @staticmethod
    def _scope_summary(scope: Any) -> str:
        rendered = " ".join(str(scope).split())
        if len(rendered) <= ARCHON_SCOPE_SUMMARY_CHARS:
            return rendered
        return rendered[: ARCHON_SCOPE_SUMMARY_CHARS - 1].rstrip() + "…"

    def _freeze_proposal_scope(
        self, update: dict[str, Any], content: dict[str, Any], connection: Any
    ) -> dict[str, Any]:
        """Replace inline proposal scope with an action-stable controller reference."""

        if content.get("kind") != "proposal":
            return content
        bead_id = content.get("bead_id")
        project = content.get("project")
        if not isinstance(bead_id, str) or not isinstance(project, str):
            raise StoreError("proposal update is missing bead or project identity")
        bead = connection.execute(
            "SELECT project_id, description FROM beads WHERE bead_id = ?",
            (bead_id,),
        ).fetchone()
        if bead is None or bead["project_id"] != project:
            raise StoreError(
                f"proposal {bead_id} does not match retained project {project}"
            )
        reference = f"scope:{update['id']}"
        connection.execute(
            """INSERT OR IGNORE INTO scope_references(
                   identity, update_id, bead_id, project_id, scope_snapshot, created_at
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                reference,
                update["id"],
                bead_id,
                project,
                bead["description"],
                utc_now(),
            ),
        )
        retained = connection.execute(
            "SELECT * FROM scope_references WHERE identity = ?", (reference,)
        ).fetchone()
        if (
            retained is None
            or retained["update_id"] != update["id"]
            or retained["bead_id"] != bead_id
            or retained["project_id"] != project
        ):
            raise StoreError(f"scope reference {reference} has stale proposal identity")
        prepared = dict(content)
        prepared.pop("scope", None)
        prepared["scope_reference"] = reference
        prepared["scope_summary"] = self._scope_summary(retained["scope_snapshot"])
        return prepared

    @staticmethod
    def _completion_needs_judgment(content: dict[str, Any]) -> bool:
        return bool(
            content.get("required_decision") or content.get("requires_decision")
        )

    def _acknowledge_completion_updates(self) -> None:
        """Consume judgment-free completion facts without spending an Archon turn."""

        updates = self.store.rows(
            """SELECT * FROM updates WHERE state = 'retained' AND actionable = 1
               AND json_extract(content, '$.kind') IN (
                   'assignment_completed', 'specialist_completed',
                   'archon_succession_completed'
               ) ORDER BY id"""
        )
        for update in updates:
            content = json.loads(update["content"])
            if self._completion_needs_judgment(content):
                continue
            timestamp = utc_now()
            with self.store.transaction() as connection:
                changed = connection.execute(
                    """UPDATE updates SET state = 'processed', updated_at = ?
                       WHERE id = ? AND state = 'retained'""",
                    (timestamp, update["id"]),
                ).rowcount
                if changed == 1:
                    self._report_automatic_completion(int(update["id"]), content)
            if changed != 1:
                continue
        # Finalization is independently replayable: a crash after the update/report
        # commit cannot strand the workflow boundary.
        processed_completions = self.store.rows(
            """SELECT id, content FROM updates WHERE state = 'processed'
               AND json_extract(content, '$.kind') IN (
                   'assignment_completed', 'specialist_completed'
               ) ORDER BY id"""
        )
        for update in processed_completions:
            self._finalize_automatically_acknowledged_workflows(
                int(update["id"]), json.loads(update["content"])
            )

    def _report_automatic_completion(
        self, update_id: int, content: dict[str, Any]
    ) -> None:
        kind = str(content.get("kind"))
        if kind == "assignment_completed":
            subject = str(content.get("bead_id") or content.get("assignment_id"))
            message = f"{subject} completed"
        elif kind == "specialist_completed":
            subject = (
                f"{content.get('specialist')} report {content.get('occurrence_id')}"
            )
            message = (
                f"{subject} completed with {content.get('finding_count', 0)} findings"
            )
        else:
            subject = f"Archon succession {content.get('successor_task_id')}"
            message = f"{subject} completed"
        cost = content.get("cost")
        action_cost = cost.get("action") if isinstance(cost, dict) else None
        display = (
            action_cost.get("attributed_display")
            if isinstance(action_cost, dict)
            else None
        )
        if isinstance(display, str):
            message += f" at estimated API cost of {display}"
        self.store.event(
            "completion_acknowledged",
            message + ".",
            entity_type="update",
            entity_id=update_id,
            detail={
                "kind": kind,
                "subject": subject,
                "minor_fixes": content.get("minor_fixes", []),
                "automatic": True,
            },
        )

    def _finalize_automatically_acknowledged_workflows(
        self, update_id: int, content: dict[str, Any]
    ) -> None:
        if content.get("kind") not in {"assignment_completed", "specialist_completed"}:
            return
        for workflow_id in sorted(self._workflow_ids_for_update(content)):
            unfinished = self.store.row(
                """SELECT 1 FROM workflow_cost_beads bead
                   WHERE bead.workflow_id = ? AND NOT EXISTS (
                     SELECT 1 FROM assignments assignment
                     WHERE assignment.bead_id = bead.bead_id
                       AND assignment.stage = 'completed') LIMIT 1""",
                (workflow_id,),
            )
            if unfinished is not None:
                continue
            report = self.store.finalize_workflow_cost(workflow_id)
            if not report.pop("_newly_finalized", False):
                continue
            group = report["groups"][0] if report["groups"] else {}
            self.store.event(
                "workflow_cost_finalized",
                "froze all-in API-equivalent workflow cost after automatic completion acknowledgement",
                entity_type="workflow",
                entity_id=workflow_id,
                detail={
                    "amount": report.get("frozen_amount"),
                    "display": (group.get("attributed") or {}).get("display"),
                    "coverage": group.get("coverage", "partial"),
                    "acknowledgement_update_id": update_id,
                    "includes_acknowledgement_action": False,
                    "acknowledgement_exclusion": None,
                },
            )

    async def _deliver_update_batch(self, *, recovery_only: bool = False) -> None:
        self._acknowledge_completion_updates()
        self._reactivate_deferred_batches(recovery_only=recovery_only)
        archon = self.store.row(
            "SELECT * FROM tasks WHERE role = 'archon' AND state = 'idle'"
        )
        if archon is None:
            return
        current = self.store.row(
            """SELECT * FROM actions WHERE task_id = ?
               AND state IN ('pending','starting','active','terminal','uncertain')""",
            (archon["id"],),
        )
        if current is not None:
            if (
                recovery_only
                and current["state"] == "pending"
                and self.store.row(
                    """SELECT 1 FROM batches b JOIN batch_updates bu ON bu.batch_id = b.id
                       JOIN updates u ON u.id = bu.update_id
                       WHERE b.action_id = ?
                         AND json_extract(u.content, '$.kind') = 'operation_resolution'
                         AND NOT EXISTS (
                           SELECT 1 FROM batch_updates other_bu
                           JOIN updates other_u ON other_u.id = other_bu.update_id
                           WHERE other_bu.batch_id = b.id
                             AND (json_extract(other_u.content, '$.kind') IS NULL
                               OR json_extract(other_u.content, '$.kind') != 'operation_resolution')
                         )
                       LIMIT 1""",
                    (current["id"],),
                )
                is not None
            ):
                await self._dispatch_action(current, task=archon)
            return
        updates = self.store.rows(
            """SELECT * FROM updates WHERE recipient_task_id = ?
               AND state = 'retained' AND actionable = 1
               AND (? = 0 OR json_extract(content, '$.kind') = 'operation_resolution')
               ORDER BY id LIMIT ?""",
            (archon["id"], int(recovery_only), ARCHON_BATCH_LIMIT),
        )
        if not updates:
            return
        timestamp = utc_now()
        snapshot = self._fleet_snapshot()
        with self.store.transaction() as connection:
            prepared_updates: list[tuple[dict[str, Any], dict[str, Any]]] = []
            for row in updates:
                connection.execute("SAVEPOINT archon_update_admission")
                content = self._freeze_proposal_scope(
                    row,
                    self._with_workflow_context(json.loads(row["content"])),
                    connection,
                )
                candidate_updates = [*prepared_updates, (row, content)]
                candidate_payload = {
                    "fleet_snapshot": snapshot,
                    "batch_items": [
                        {
                            "update_id": candidate_row["id"],
                            "identity": candidate_row["identity"],
                            "content": candidate_content,
                        }
                        for candidate_row, candidate_content in candidate_updates
                    ],
                }
                if not archon_action_fits(candidate_payload):
                    connection.execute("ROLLBACK TO SAVEPOINT archon_update_admission")
                    connection.execute("RELEASE SAVEPOINT archon_update_admission")
                    break
                connection.execute("RELEASE SAVEPOINT archon_update_admission")
                prepared_updates.append((row, content))
            if not prepared_updates:
                raise StoreError(
                    "one bounded Archon update exceeds the mandatory action budget"
                )
            payload = {
                "fleet_snapshot": snapshot,
                "batch_items": [
                    {
                        "update_id": row["id"],
                        "identity": row["identity"],
                        "content": content,
                    }
                    for row, content in prepared_updates
                ],
            }
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
            for update, content in prepared_updates:
                connection.execute(
                    "INSERT INTO batch_updates(batch_id, update_id) VALUES (?, ?)",
                    (batch_cursor.lastrowid, update["id"]),
                )
                connection.execute(
                    """UPDATE updates SET content = ?, state = 'batched',
                       updated_at = ? WHERE id = ?""",
                    (json.dumps(content, sort_keys=True), timestamp, update["id"]),
                )
        action = self.store.row(
            "SELECT * FROM actions WHERE id = ?", (action_cursor.lastrowid,)
        )
        assert action is not None
        await self._dispatch_action(action, task=archon)

    def _fleet_snapshot(self) -> dict[str, Any]:
        unfinished = self.store.rows(
            """SELECT a.id, a.run_id, a.bead_id, a.stage, a.condition,
                      a.next_attempt_at, a.operator_hold_id, r.project_id, r.priority
               FROM assignments a JOIN runs r ON r.id = a.run_id
               WHERE a.stage NOT IN ('completed','canceled')
               ORDER BY r.priority DESC, a.run_id, a.id"""
        )
        for assignment in unfinished:
            assignment["conflict_keys"] = list(
                self._assignment_conflict_keys(assignment)
            )
        return {
            "capacity": capacity(self.store),
            "unfinished_assignments": unfinished,
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
            "uncertain_operations": self.store.rows(
                """SELECT id, kind, target, condition, operator_hold_id
                   FROM external_operations WHERE state = 'uncertain'
                     AND reconciliation_used = 1 ORDER BY id"""
            ),
            "policies": self.store.rows(
                "SELECT id, kind, scope, cadence_seconds, next_due_at, active FROM policies ORDER BY id"
            ),
        }

    def _reactivate_deferred_batches(self, *, recovery_only: bool = False) -> None:
        now = utc_now()
        for deferred in self.store.rows(
            "SELECT * FROM deferred_batches ORDER BY batch_id"
        ):
            if (
                recovery_only
                and self.store.row(
                    """SELECT 1 FROM batch_updates bu JOIN updates u ON u.id = bu.update_id
                   WHERE bu.batch_id = ?
                     AND (json_extract(u.content, '$.kind') IS NULL
                       OR json_extract(u.content, '$.kind') != 'operation_resolution')
                   LIMIT 1""",
                    (deferred["batch_id"],),
                )
                is not None
            ):
                continue
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
                """SELECT i.*, t.title, t.role, t.native_thread_id
                   FROM interviews i JOIN tasks t ON t.id = i.subject_task_id
                   WHERE i.occurrence_id = ? ORDER BY i.id""",
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
                "role": row["role"],
                "answer": json.loads(row["answer_json"]),
            }
            for row in interviews
            if row["state"] == "answered" and row["answer_json"]
        ]
        missing = [
            {"subject": row["title"], "role": row["role"], "state": row["state"]}
            for row in interviews
            if row["state"] != "answered"
        ]
        timestamp = utc_now()
        cursor = self.store.execute(
            "INSERT INTO actions(task_id, occurrence_id, kind, payload, state, check_after, created_at, updated_at) VALUES (?, ?, 'specialist', ?, 'pending', ?, ?, ?)",
            (
                task["id"],
                occurrence["id"],
                json.dumps(
                    {
                        "continuation": "final report after the single interview round",
                        "direct_item": bool(scope.get("direct_item")),
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
                  AND e.kind NOT IN ('reconciliation_started', 'reconciliation_completed',
                                     'command_received', 'command_succeeded')
                  AND (COALESCE(t.project_id, r.project_id, ar.project_id,
                         CASE WHEN e.entity_type = 'project' THEN e.entity_id END) IS NULL
                       OR COALESCE(t.project_id, r.project_id, ar.project_id,
                         CASE WHEN e.entity_type = 'project' THEN e.entity_id END) IN ({placeholders}))
                ORDER BY e.id DESC LIMIT 201""",
            (cutoff, start, start, *project_ids),
        )
        event_counts = self.store.rows(
            """SELECT kind, COUNT(*) AS count FROM events
               WHERE created_at <= ? AND (? IS NULL OR created_at > ?)
               GROUP BY kind ORDER BY count DESC, kind LIMIT 50""",
            (cutoff, start, start),
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
                "missing": "Native Codex histories, complete Tollgate logs, latency telemetry and existing Beads are not automatically included; token usage is included when observed, with explicit coverage gaps.",
            },
            "assignments": assignments[:100],
            "event_counts": event_counts,
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
                        "description": (
                            f"{finding['problem']}\n\n"
                            + (
                                f"Implementation scope: {finding['implementation_scope']}\n\n"
                                if finding.get("implementation_scope")
                                else ""
                            )
                            + f"Evidence: {finding['evidence']}\n\n"
                            + f"Expected benefit: {finding['expected_benefit']}\n\n"
                            + f"Acceptance criteria: {finding['acceptance_criteria']}"
                        ),
                    }
                    filed = await asyncio.to_thread(
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
                    if isinstance(filed.get("bead_id"), str):
                        self.store.link_bead_to_workflow(
                            f"specialist:{occurrence['id']}", filed["bead_id"]
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
                workflow_id = f"specialist:{occurrence['id']}"
                workflow_cost = self.store.cost_report(
                    workflow_id=workflow_id, group_by="workflow"
                )
                workflow_group = (
                    workflow_cost["groups"][0] if workflow_cost["groups"] else {}
                )
                specialist_action = self.store.row(
                    """SELECT id FROM actions WHERE occurrence_id = ?
                       AND kind = 'specialist' ORDER BY id DESC LIMIT 1""",
                    (occurrence["id"],),
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
                        "action_id": (
                            int(specialist_action["id"]) if specialist_action else None
                        ),
                        "workflow_id": workflow_id,
                        "cost": {
                            "estimate_kind": "equivalent public OpenAI API charges",
                            "action": (
                                self.store.action_cost_summary(
                                    int(specialist_action["id"])
                                )
                                if specialist_action
                                else None
                            ),
                            "workflow_through_completion": (
                                workflow_group.get("attributed") or {}
                            ),
                            "coverage": workflow_group.get("coverage", "partial"),
                            "assumptions": workflow_group.get("assumptions", []),
                            "exclusions": workflow_group.get("exclusions", []),
                            "excludes_running_acknowledgement": True,
                        },
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
        confirmed_terminal_turns: dict[int, str] = {}
        for task in candidates:
            try:
                facts = await self._refresh_task(task)
                if (
                    facts["last_turn_terminal"]
                    and facts["helpers_terminal"]
                    and isinstance(facts["last_turn_id"], str)
                ):
                    confirmed_terminal_turns[int(task["id"])] = facts["last_turn_id"]
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
                if isinstance(facts["last_turn_id"], str):
                    confirmed_terminal_turns[int(task["id"])] = facts["last_turn_id"]
        if mode == "reset":
            reset_exceptions = await self._reset_state(record)
        else:
            reset_exceptions = []
            for action in self.store.rows(
                """SELECT id, task_id, native_turn_id FROM actions
                   WHERE state NOT IN ('processed','canceled')
                     AND native_turn_id IS NOT NULL"""
            ):
                if confirmed_terminal_turns.get(int(action["task_id"])) == action.get(
                    "native_turn_id"
                ):
                    self._finalize_action_usage(int(action["id"]))
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
        self.store.execute(
            """INSERT INTO meta(key, value) VALUES ('controller_state', 'ready')
               ON CONFLICT(key) DO UPDATE SET value = excluded.value"""
        )
        self._update_readiness()
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
            self.paths.database,
            event_log=self.paths.logs_root / "workflow.jsonl",
            operative_journal=self.paths.operative_journal,
        )
        self._initialize_configuration()
        for worker_name in self.critical_workers:
            self.store.heartbeat(worker_name)
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
            if self._operative_fenced():
                journal = self.operative_journal
                if journal is not None:
                    self.store.record_operative_evidence(
                        str(journal["takeover_id"]),
                        f"source-change:{utc_now()}",
                        "source_observation",
                        coverage="observed",
                        target_type="source",
                        target_id=str(source),
                        detail={"change_count": len(changes)},
                    )
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
        if self._operative_fenced():
            return False
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
    value: Any,
    worktree_path: str | None,
    *,
    exclude_id: str | None = None,
    source_oid: str | None = None,
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
                    and (
                        source_oid is None or _oid(item.get("source_oid")) == source_oid
                    )
                    and item.get("state") not in {"canceled", "failed", "promoted"}
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


def _worktree_head(worktree_path: str | None) -> str | None:
    if not worktree_path:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", worktree_path, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _worktree_clean(worktree_path: str | None) -> bool:
    if not worktree_path:
        return False
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                worktree_path,
                "status",
                "--porcelain",
                "--untracked-files=all",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and not result.stdout.strip()


def _git_tree_oid(worktree_path: str | None, revision: str) -> str | None:
    if not worktree_path:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", worktree_path, "rev-parse", f"{revision}^{{tree}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


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


_PROMOTION_EFFECTED_STATES = {
    "promoted-local-push-pending",
    "promoted",
    "externally-integrated",
}
_SOURCE_FAILURE_STATES = {"canceled", "failed", "merge-conflict"}
_REMOTE_PENDING_STATES = {"preflight-pending", "ready", "pushing"}


def _classify_delivery_status(
    candidate: dict[str, Any] | None, status: dict[str, Any]
) -> DeliveryStatus:
    if candidate is None:
        return DeliveryStatus(
            DeliveryDisposition.UNRESOLVED,
            "Tollgate no longer reports the retained candidate; candidate-specific observation is required",
        )
    state = str(candidate.get("state", "unknown"))
    if state in _SOURCE_FAILURE_STATES:
        return DeliveryStatus(
            DeliveryDisposition.SOURCE_FAILED,
            f"Tollgate candidate ended in {state}",
        )
    if state not in _PROMOTION_EFFECTED_STATES:
        return DeliveryStatus(
            DeliveryDisposition.UNRESOLVED,
            f"Tollgate approval is not yet observable for candidate in {state}",
        )

    configuration = status.get("configuration")
    remote_enabled = bool(
        isinstance(configuration, dict) and configuration.get("remote_enabled")
    )
    remote_state = candidate.get("remote_state")
    remote_required = remote_enabled or state == "promoted-local-push-pending"
    if remote_required and remote_state in {"abandoned", "push-blocked"}:
        return DeliveryStatus(
            DeliveryDisposition.POST_PROMOTION_ATTENTION,
            f"candidate was promoted but source synchronization requires attention (remote_state={remote_state})",
        )
    if remote_required and remote_state != "synchronized":
        if remote_state is None or remote_state in _REMOTE_PENDING_STATES:
            return DeliveryStatus(
                DeliveryDisposition.POST_PROMOTION_PENDING,
                f"candidate was promoted; source synchronization is pending (remote_state={remote_state or 'unknown'})",
            )
        return DeliveryStatus(
            DeliveryDisposition.POST_PROMOTION_ATTENTION,
            f"candidate was promoted but source synchronization cannot complete automatically (remote_state={remote_state})",
        )
    if state == "promoted-local-push-pending":
        return DeliveryStatus(
            DeliveryDisposition.POST_PROMOTION_PENDING,
            "candidate was locally promoted; Tollgate has not finalized remote synchronization",
        )

    cleanup_state = candidate.get("cleanup_state")
    if cleanup_state == "needs-attention":
        return DeliveryStatus(
            DeliveryDisposition.POST_PROMOTION_ATTENTION,
            "candidate was promoted but worktree cleanup requires operator attention",
        )
    if cleanup_state not in {"completed", "not-eligible"}:
        if cleanup_state is None or cleanup_state in {"pending", "running"}:
            return DeliveryStatus(
                DeliveryDisposition.POST_PROMOTION_PENDING,
                f"candidate was promoted; worktree cleanup is pending (cleanup_state={cleanup_state or 'unknown'})",
            )
        return DeliveryStatus(
            DeliveryDisposition.POST_PROMOTION_ATTENTION,
            f"candidate was promoted but worktree cleanup cannot complete automatically (cleanup_state={cleanup_state})",
        )
    if not candidate.get("certificate_id"):
        if state == "externally-integrated":
            return DeliveryStatus(
                DeliveryDisposition.POST_PROMOTION_ATTENTION,
                "candidate was externally integrated without a retained Tollgate certificate; delivery recovery is required",
            )
        return DeliveryStatus(
            DeliveryDisposition.POST_PROMOTION_PENDING,
            "candidate was promoted; Tollgate certificate finalization is pending",
        )
    return DeliveryStatus(DeliveryDisposition.SATISFIED)


def _candidate_crossed_promotion(candidate: dict[str, Any] | None) -> bool:
    return bool(candidate and candidate.get("state") in _PROMOTION_EFFECTED_STATES)


def _approval_candidate(
    approval: dict[str, Any] | None, candidate_id: str
) -> dict[str, Any] | None:
    if not isinstance(approval, dict):
        return None
    wait_statuses = approval.get("wait_statuses")
    if isinstance(wait_statuses, list):
        for status in reversed(wait_statuses):
            candidate = _candidate_by_id(status, candidate_id)
            if candidate is not None:
                return candidate
    return _candidate_by_id(approval, candidate_id)


def _retained_delivery_candidate(
    operation: dict[str, Any], candidate_id: str
) -> dict[str, Any] | None:
    if not operation.get("result_json"):
        return None
    try:
        result = json.loads(operation["result_json"])
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(result, dict):
        return None
    for key in ("latest_status", "status"):
        candidate = _candidate_by_id(result.get(key), candidate_id)
        if _candidate_crossed_promotion(candidate):
            return candidate
    approval = result.get("approval")
    return _approval_candidate(
        approval if isinstance(approval, dict) else None, candidate_id
    ) or _candidate_by_id(result, candidate_id)


def _delivery_operation_result(
    operation: dict[str, Any],
    observed: dict[str, Any],
    approval: dict[str, Any] | None,
) -> dict[str, Any]:
    retained: dict[str, Any] = {}
    if operation.get("result_json"):
        try:
            decoded = json.loads(operation["result_json"])
        except (TypeError, json.JSONDecodeError):
            decoded = None
        if isinstance(decoded, dict):
            retained.update(decoded)
    if approval is not None:
        retained["approval"] = approval
    retained["latest_status"] = observed
    return retained


def _find_codex_project_id(
    projects: list[dict[str, Any]],
    repo_path: str,
    *,
    preferred_id: str | None = None,
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
            identifier = project.get("id")
            if isinstance(identifier, str):
                matches.append(identifier)
    if preferred_id in matches:
        return preferred_id
    return matches[0] if len(matches) == 1 else None


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


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _task_identity(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": int(task["id"]),
        "thread_id": str(task["native_thread_id"]),
        "title": str(task["title"]),
        "role": str(task["role"]),
    }


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _bounded_excerpt(value: str, limit: int) -> dict[str, Any]:
    if len(value) <= limit:
        return {"text": value, "truncated": False, "original_characters": len(value)}
    return {
        "text": value[:limit],
        "truncated": True,
        "original_characters": len(value),
        "omitted_characters": len(value) - limit,
    }


def _bounded_plain(value: Any, limit: int) -> Any:
    if not isinstance(value, str):
        return value
    return _bounded_excerpt(value, limit) if len(value) > limit else value


def _bounded_json(value: Any, limit: int) -> Any:
    if value is None:
        return None
    decoded: Any = value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            decoded = value
    rendered = json.dumps(decoded, ensure_ascii=False, sort_keys=True)
    if len(rendered) <= limit:
        return decoded
    return {
        "excerpt": rendered[:limit],
        "truncated": True,
        "original_characters": len(rendered),
        "omitted_characters": len(rendered) - limit,
    }


_RAW_TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def _empty_token_totals() -> dict[str, None]:
    return {field: None for field in _RAW_TOKEN_FIELDS}


def _raw_token_totals(rows: list[dict[str, Any]]) -> dict[str, int | None]:
    totals: dict[str, int | None] = {}
    for field in _RAW_TOKEN_FIELDS:
        column = "total_tokens" if field == "total_tokens" else f"total_{field}"
        known = [int(row[column]) for row in rows if row.get(column) is not None]
        totals[field] = sum(known) if known else None
    return totals


def _raw_usage_coverage(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "unknown"
    states = {str(row.get("coverage") or "unknown") for row in rows}
    if states == {"complete"}:
        return "complete"
    if states == {"observed"}:
        return "observed"
    if states == {"unavailable"}:
        return "unavailable"
    if states == {"unknown"}:
        return "unknown"
    return "partial"


async def run_controller(paths: RuntimePaths, config: InstallationConfig) -> None:
    controller = Controller(paths, config)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, controller.stop_event.set)
    await controller.start()
