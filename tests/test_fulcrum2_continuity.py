from __future__ import annotations

import asyncio
import subprocess
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fulcrum.application import Application
from fulcrum.continuity import fleet_admission_pause, reconcile_archive_once
from fulcrum.contracts import ActorContext, CommandState, ParsedRequest
from fulcrum.instance import resolve_instance
from fulcrum.ledger import Ledger
from fulcrum.runtime import (
    AppServerError,
    ReleaseFacts,
    RuntimeCapabilities,
    TaskFacts,
    TurnFacts,
)


def task_facts(
    thread_id: str,
    root: Path,
    *,
    active_turn: str | None = None,
    archived: bool = False,
) -> TaskFacts:
    return TaskFacts(
        id=thread_id,
        title=f"Task {thread_id}",
        cwd=str(root),
        project_id="toy-native",
        workspace_roots=(str(root),),
        archived=archived,
        exists=True,
        loaded=not archived,
        runtime_status="inProgress" if active_turn else "idle",
        active_turn=active_turn,
        last_turn=(
            {"id": active_turn, "status": "inProgress"}
            if active_turn
            else {"id": "turn-done", "status": "completed"}
        ),
        pending_requests=(),
        observed_at="2026-09-14T20:00:00Z",
    )


class ContinuityRuntime:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.tasks: dict[str, TaskFacts] = {}
        self.creation_paths: dict[str, str] = {}
        self.terminals_by_thread: dict[str, list[dict[str, Any]]] = {}
        self.archive_calls: list[str] = []
        self.release_calls: list[str] = []
        self.interrupt_calls: list[tuple[str, str]] = []
        self.create_calls = 0
        self.archive_loss: str | None = None
        self.unavailable = False

    async def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            available=True,
            endpoint="fake://continuity",
            methods=(),
            models={"luna": ("high",)},
        )

    async def inspect_task(self, thread_id: str) -> TaskFacts:
        if self.unavailable:
            raise AppServerError("runtime unavailable", category="unavailable")
        return self.tasks[thread_id]

    async def find_tasks(self, creation_cwd: str) -> list[TaskFacts]:
        return [
            self.tasks[thread_id]
            for thread_id, path in self.creation_paths.items()
            if path == creation_cwd
        ]

    async def create_task(self, spec: Any) -> TaskFacts:
        self.create_calls += 1
        thread_id = f"successor-{self.create_calls}"
        created = task_facts(thread_id, self.root)
        self.tasks[thread_id] = created
        self.creation_paths[thread_id] = spec.creation_cwd
        return created

    async def configure_task(self, thread_id: str, spec: Any) -> TaskFacts:
        current = self.tasks[thread_id]
        configured = TaskFacts(
            **{
                **current.__dict__,
                "title": spec.title,
                "cwd": spec.cwd,
                "workspace_roots": spec.workspace_roots,
            }
        )
        self.tasks[thread_id] = configured
        return configured

    async def interrupt(self, thread_id: str, turn_id: str) -> TurnFacts:
        self.interrupt_calls.append((thread_id, turn_id))
        current = self.tasks[thread_id]
        self.tasks[thread_id] = TaskFacts(
            **{
                **current.__dict__,
                "runtime_status": "idle",
                "active_turn": None,
                "last_turn": {"id": turn_id, "status": "interrupted"},
            }
        )
        return TurnFacts(
            id=turn_id,
            thread_id=thread_id,
            state="interrupted",
            operation_id=None,
            completed=True,
            error=None,
            tools=(),
            usage=None,
            observed_at="2026-09-14T20:00:01Z",
        )

    async def terminals(self, thread_id: str, **arguments: Any) -> dict[str, Any]:
        return {
            "thread_id": thread_id,
            "items": list(self.terminals_by_thread.get(thread_id, [])),
            "next_cursor": None,
            "observed_at": "2026-09-14T20:00:02Z",
            "gaps": [],
        }

    async def terminate_terminal(
        self, thread_id: str, terminal_id: str
    ) -> dict[str, Any]:
        self.terminals_by_thread[thread_id] = [
            item
            for item in self.terminals_by_thread.get(thread_id, [])
            if item.get("terminal_id") != terminal_id
        ]
        return {
            "thread_id": thread_id,
            "terminal_id": terminal_id,
            "terminated": True,
            "still_running": False,
        }

    async def release(self, thread_id: str) -> ReleaseFacts:
        self.release_calls.append(thread_id)
        return ReleaseFacts(
            thread_id=thread_id,
            status="unsubscribed",
            active_terminals=(),
            observed_at="2026-09-14T20:00:03Z",
        )

    async def archive(self, thread_id: str) -> TaskFacts:
        self.archive_calls.append(thread_id)
        if self.archive_loss == "not_applied":
            self.archive_loss = None
            raise AppServerError(
                "archive response lost before observation",
                category="uncertain",
                uncertain=True,
            )
        current = self.tasks[thread_id]
        archived = TaskFacts(
            **{
                **current.__dict__,
                "archived": True,
                "loaded": False,
                "runtime_status": "notLoaded",
            }
        )
        self.tasks[thread_id] = archived
        if self.archive_loss == "applied":
            self.archive_loss = None
            raise AppServerError(
                "archive response lost after application",
                category="uncertain",
                uncertain=True,
            )
        return archived

    async def unarchive(self, thread_id: str) -> TaskFacts:
        current = self.tasks[thread_id]
        restored = TaskFacts(
            **{
                **current.__dict__,
                "archived": False,
                "loaded": True,
                "runtime_status": "idle",
            }
        )
        self.tasks[thread_id] = restored
        return restored


class Fulcrum2ContinuityTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.brain = root / "brain"
        self.project = root / "project"
        self.instance_root = root / "instance"
        for path in (self.brain, self.project, self.instance_root):
            path.mkdir()
        for path in (self.brain, self.project):
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
                ["git", "config", "beads.role", "maintainer"],
                cwd=path,
                check=True,
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
            cwd=self.brain,
            capture_output=True,
            check=True,
            timeout=30,
        )
        self.config = self.brain / "fulcrum.yaml"
        self.config.write_text(
            "\n".join(
                (
                    "runtime:",
                    "  kind: deterministic",
                    "  endpoint: ws://127.0.0.1:1",
                    "brain:",
                    f"  root: {self.brain}",
                    "projects:",
                    "  toy:",
                    f"    root: {self.project}",
                    "    codex_project_id: toy-native",
                    "    enabled: true",
                    "models:",
                    "  executor:",
                    "    model: luna",
                    "    effort: high",
                    "  vizier:",
                    "    model: luna",
                    "    effort: high",
                    "  marshal:",
                    "    model: luna",
                    "    effort: high",
                    "",
                )
            ),
            encoding="utf-8",
        )
        (self.instance_root / "config").symlink_to(self.config)
        self.instance = resolve_instance(instance=str(self.instance_root), config=None)
        self.ledger = Ledger(self.brain)
        self.runtime = ContinuityRuntime(self.project)
        self.application = Application()
        self.ledger.create_record(
            record_id="fc-system",
            kind="control",
            title="Fulcrum control",
            description="Standing identities",
            owner="marshal-old",
            fc={
                "kind": "control",
                "owner": "marshal-old",
                "vizier_thread": "vizier-old",
                "marshal_thread": "marshal-old",
                "active_takeover": None,
                "fleet_replacement": None,
                "last_transition": None,
            },
        )

    def tearDown(self) -> None:
        subprocess.run(
            ["bd", "-C", str(self.brain), "dolt", "stop"],
            capture_output=True,
            check=False,
            timeout=20,
        )
        self.temporary.cleanup()

    def runtime_submit(self, action: Any, _timeout: float) -> Any:
        return asyncio.run(action(self.runtime))

    def request(
        self,
        command: tuple[str, ...],
        *,
        arguments: dict[str, Any],
        project: str | None = None,
        request_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> ParsedRequest:
        return ParsedRequest(
            command=command,
            arguments=arguments,
            input=payload or {},
            actor=ActorContext(kind="human"),
            instance=self.instance,
            request_id=request_id or str(uuid.uuid4()),
            project=project,
            timeout=10,
            offline=True,
            runtime_submit=self.runtime_submit,
        )

    def work(self, identifier: str, *, future: bool = False) -> None:
        self.ledger.create_record(
            record_id=identifier,
            kind="work",
            title=f"Work {identifier}",
            description="Preserved implementation state",
            owner="worker-old",
            fc={
                "kind": "work",
                "owner": "worker-old",
                "project": "toy",
                "role": "executor",
                "phase": "backlog" if future else "working",
                "ownership_operation": "fc-old-acquisition",
                "worktree": {"path": str(self.project), "dirty": True},
                "cost": {"total_usd": "1.25"},
                "activation": {"mode": "future"} if future else None,
                "next_action": "Continue exact retained work",
            },
        )

    def task(
        self,
        identifier: str,
        thread_id: str,
        *,
        role: str = "executor",
        purpose: str = "work",
        work_bead: str | None = None,
        associated: list[str] | None = None,
    ) -> None:
        observed = task_facts(thread_id, self.project)
        self.runtime.tasks[thread_id] = observed
        self.ledger.create_record(
            record_id=identifier,
            kind="task",
            title=f"Managed task: {thread_id}",
            description="Retained task context",
            owner=thread_id,
            external_ref=f"fulcrum:thread:{thread_id}",
            fc={
                "kind": "task",
                "owner": thread_id,
                "thread_id": thread_id,
                "role": role,
                "purpose": purpose,
                "project": "toy" if purpose != "leadership" else None,
                "work_bead": work_bead,
                "associated_beads": associated or [],
                "ownership_operation": (
                    "fc-old-acquisition" if work_bead is not None else None
                ),
                "creation_operation": f"fc-create-{identifier}",
                "creation_cwd": str(self.instance_root / "threads" / identifier),
                "model": "luna",
                "effort": "high",
                "memory_refs": ["fc-memory-policy"],
                "review_result_operation": "fc-review-result",
                "archive_state": "leadership" if purpose == "leadership" else "pending",
                "archive_due_at": None,
                "archive_operation": None,
                "replaced_by": None,
                "last_observed": observed.to_dict(),
            },
        )

    async def archive_pass(self, now: datetime) -> list[dict[str, Any]]:
        tasks = self.ledger.list_records(kind="task", limit=0)
        facts = {
            str((task.fc or {})["thread_id"]): self.runtime.tasks[
                str((task.fc or {})["thread_id"])
            ]
            for task in tasks
        }
        result = await reconcile_archive_once(
            self.ledger,
            self.runtime,
            tasks,
            facts,
            now=now,
            request=self.request(("reconcile",), arguments={}),
            archive_idle_seconds=600,
        )
        return [dict(item) for item in result]

    async def test_archive_once_survives_loss_restart_and_manual_unarchive(
        self,
    ) -> None:
        self.work("fc-finished")
        self.ledger.run(
            ("close", "fc-finished", "--reason", "fixture complete"), mutating=True
        )
        self.task("fc-finished-task", "worker-old", work_bead="fc-finished")
        started = datetime(2026, 9, 14, 20, 0, tzinfo=timezone.utc)
        pending = await self.archive_pass(started)
        self.assertEqual(pending[0]["state"], "pending")
        self.assertEqual(self.runtime.release_calls, ["worker-old"])
        self.runtime.archive_loss = "applied"
        done = await self.archive_pass(started + timedelta(minutes=10))
        self.assertEqual(done[0]["state"], "done")
        self.assertEqual(self.runtime.archive_calls, ["worker-old"])
        await self.archive_pass(started + timedelta(minutes=11))
        self.assertEqual(self.runtime.archive_calls, ["worker-old"])

        await self.runtime.unarchive("worker-old")
        suppressed = await self.archive_pass(started + timedelta(minutes=12))
        self.assertEqual(suppressed[0]["state"], "suppressed")
        await self.archive_pass(started + timedelta(hours=1))
        self.assertEqual(self.runtime.archive_calls, ["worker-old"])

    async def test_lost_unapplied_archive_response_is_never_resent(self) -> None:
        self.work("fc-unapplied")
        self.ledger.run(
            ("close", "fc-unapplied", "--reason", "fixture complete"), mutating=True
        )
        self.task("fc-unapplied-task", "worker-old", work_bead="fc-unapplied")
        started = datetime(2026, 9, 14, 20, 0, tzinfo=timezone.utc)
        await self.archive_pass(started)
        self.runtime.archive_loss = "not_applied"
        uncertain = await self.archive_pass(started + timedelta(minutes=10))
        self.assertEqual(uncertain[0]["state"], "uncertain")
        self.assertEqual(self.runtime.archive_calls, ["worker-old"])
        suppressed = await self.archive_pass(started + timedelta(minutes=11))
        self.assertEqual(suppressed[0]["state"], "suppressed")
        await self.archive_pass(started + timedelta(hours=2))
        self.assertEqual(self.runtime.archive_calls, ["worker-old"])

    async def test_future_only_is_eligible_but_active_association_retains_author(
        self,
    ) -> None:
        self.work("fc-future", future=True)
        self.work("fc-active")
        self.task(
            "fc-author-task",
            "worker-old",
            work_bead="fc-future",
            associated=["fc-active"],
        )
        started = datetime(2026, 9, 14, 20, 0, tzinfo=timezone.utc)
        self.assertEqual(await self.archive_pass(started), [])
        self.ledger.run(
            ("close", "fc-active", "--reason", "child complete"), mutating=True
        )
        pending = await self.archive_pass(started)
        self.assertEqual(pending[0]["state"], "pending")

    async def test_drain_replacement_waits_then_resumes_one_successor(self) -> None:
        self.work("fc-owned")
        self.task("fc-worker-task", "worker-old", work_bead="fc-owned")
        self.task(
            "fc-vizier-task",
            "vizier-old",
            role="vizier",
            purpose="leadership",
        )
        self.task(
            "fc-marshal-task",
            "marshal-old",
            role="marshal",
            purpose="leadership",
        )
        self.runtime.tasks["worker-old"] = task_facts(
            "worker-old", self.project, active_turn="turn-active"
        )
        request_id = str(uuid.uuid4())
        pending = await asyncio.to_thread(
            self.application.dispatch,
            self.request(
                ("fleet", "replace"),
                arguments={"mode": "drain", "reason": "refresh project fleet"},
                project="toy",
                request_id=request_id,
            ),
        )
        self.assertEqual(pending.state, CommandState.ACCEPTED)
        self.assertEqual(self.runtime.create_calls, 0)
        self.assertIsNotNone(
            (self.ledger.show("fc-system").fc or {})["fleet_replacement"]
        )
        self.runtime.tasks["worker-old"] = task_facts("worker-old", self.project)
        completed = await asyncio.to_thread(
            self.application.dispatch,
            self.request(
                ("fleet", "replace"),
                arguments={"mode": "drain", "reason": "refresh project fleet"},
                project="toy",
                request_id=request_id,
            ),
        )
        self.assertEqual(completed.state, CommandState.COMPLETED)
        self.assertEqual(self.runtime.create_calls, 1)
        work = self.ledger.show("fc-owned")
        old = self.ledger.show("fc-worker-task")
        assert work is not None and work.fc and old is not None and old.fc
        self.assertEqual(work.fc["owner"], "successor-1")
        self.assertEqual(work.fc["worktree"]["dirty"], True)
        self.assertEqual(work.fc["cost"]["total_usd"], "1.25")
        self.assertIsNotNone(old.fc["replaced_by"])
        successor = self.ledger.show(str(old.fc["replaced_by"]))
        assert successor is not None and successor.fc
        self.assertEqual(successor.fc["review_result_operation"], "fc-review-result")
        self.assertEqual(
            (self.ledger.show("fc-system").fc or {})["vizier_thread"],
            "vizier-old",
        )
        again = await asyncio.to_thread(
            self.application.dispatch,
            self.request(
                ("fleet", "replace"),
                arguments={"mode": "drain", "reason": "refresh project fleet"},
                project="toy",
                request_id=request_id,
            ),
        )
        self.assertEqual(again.state, CommandState.COMPLETED)
        self.assertEqual(self.runtime.create_calls, 1)

    async def test_interrupt_and_leader_replacement_preserve_context(self) -> None:
        self.task(
            "fc-vizier-task",
            "vizier-old",
            role="vizier",
            purpose="leadership",
        )
        self.task(
            "fc-marshal-task",
            "marshal-old",
            role="marshal",
            purpose="leadership",
        )
        self.runtime.tasks["vizier-old"] = task_facts(
            "vizier-old", self.project, active_turn="vizier-turn"
        )
        request_id = str(uuid.uuid4())
        result = await asyncio.to_thread(
            self.application.dispatch,
            self.request(
                ("leader", "replace"),
                arguments={"role": "vizier", "reason": "replace broken leader"},
                request_id=request_id,
            ),
        )
        self.assertEqual(result.state, CommandState.ACCEPTED)
        self.assertEqual(self.runtime.interrupt_calls, [])
        self.runtime.tasks["vizier-old"] = task_facts("vizier-old", self.project)
        completed = await asyncio.to_thread(
            self.application.dispatch,
            self.request(
                ("leader", "replace"),
                arguments={"role": "vizier", "reason": "replace broken leader"},
                request_id=request_id,
            ),
        )
        self.assertEqual(completed.state, CommandState.COMPLETED)
        control = self.ledger.show("fc-system")
        old = self.ledger.show("fc-vizier-task")
        assert control is not None and control.fc and old is not None and old.fc
        self.assertEqual(control.fc["vizier_thread"], "successor-1")
        successor = self.ledger.show(str(old.fc["replaced_by"]))
        assert successor is not None and successor.fc
        self.assertEqual(successor.fc["memory_refs"], ["fc-memory-policy"])

        marshal = await asyncio.to_thread(
            self.application.dispatch,
            self.request(
                ("leader", "replace"),
                arguments={"role": "marshal", "reason": "refresh current context"},
            ),
        )
        self.assertEqual(marshal.state, CommandState.COMPLETED)
        marshal_old = self.ledger.show("fc-marshal-task")
        assert marshal_old is not None and marshal_old.fc
        marshal_successor = self.ledger.show(str(marshal_old.fc["replaced_by"]))
        assert marshal_successor is not None and marshal_successor.fc
        self.assertIn("continuity_context", marshal_successor.fc)

        self.work("fc-interrupt")
        self.task("fc-interrupt-task", "worker-old", work_bead="fc-interrupt")
        self.runtime.tasks["worker-old"] = task_facts(
            "worker-old", self.project, active_turn="worker-turn"
        )
        self.runtime.terminals_by_thread["worker-old"] = [
            {"terminal_id": "terminal-owned"}
        ]
        interrupted = await asyncio.to_thread(
            self.application.dispatch,
            self.request(
                ("fleet", "replace"),
                arguments={"mode": "interrupt", "reason": "explicit interruption"},
                project="toy",
            ),
        )
        self.assertEqual(interrupted.state, CommandState.COMPLETED)
        self.assertEqual(
            self.runtime.interrupt_calls[-1], ("worker-old", "worker-turn")
        )
        self.assertEqual(self.runtime.terminals_by_thread["worker-old"], [])

    async def test_runtime_outage_keeps_old_owner_and_admission_paused(self) -> None:
        self.work("fc-outage")
        self.task("fc-outage-task", "worker-old", work_bead="fc-outage")
        self.runtime.unavailable = True
        request_id = str(uuid.uuid4())
        pending = await asyncio.to_thread(
            self.application.dispatch,
            self.request(
                ("fleet", "replace"),
                arguments={"mode": "interrupt", "reason": "runtime repair"},
                project="toy",
                request_id=request_id,
            ),
        )
        self.assertEqual(pending.state, CommandState.ACCEPTED)
        work = self.ledger.show("fc-outage")
        control = self.ledger.show("fc-system")
        assert work is not None and work.fc and control is not None and control.fc
        self.assertEqual(work.fc["owner"], "worker-old")
        self.assertEqual(control.fc["fleet_replacement"]["state"], "active")
        self.assertIsNotNone(fleet_admission_pause(self.ledger, "toy"))
        self.assertEqual(self.runtime.create_calls, 0)

        self.runtime.unavailable = False
        completed = await asyncio.to_thread(
            self.application.dispatch,
            self.request(
                ("fleet", "replace"),
                arguments={"mode": "interrupt", "reason": "runtime repair"},
                project="toy",
                request_id=request_id,
            ),
        )
        self.assertEqual(completed.state, CommandState.COMPLETED)
        self.assertEqual(self.runtime.create_calls, 1)

    async def test_recovery_replace_thread_executes_exact_retained_successor(
        self,
    ) -> None:
        self.work("fc-repair-thread")
        self.task("fc-repair-thread-task", "worker-old", work_bead="fc-repair-thread")
        control = self.ledger.show("fc-system")
        assert control is not None and control.fc
        control_fc = dict(control.fc)
        control_fc["active_takeover"] = {
            "operation_id": "fc-recovery-parent",
            "scope": "bead:fc-repair-thread",
            "reason": "replace damaged native thread",
            "state": "active",
            "bead_ids": ["fc-repair-thread"],
            "project_ids": ["toy"],
            "owner_thread": None,
        }
        self.ledger.update_fc(control.id, control_fc)
        result = await asyncio.to_thread(
            self.application.dispatch,
            self.request(
                ("recover", "repair"),
                arguments={"scope": "bead:fc-repair-thread"},
                payload={
                    "actions": [
                        {
                            "action": "replace_thread",
                            "target": "fc-repair-thread-task",
                            "arguments": {"mode": "drain"},
                            "reason": "replace the exact stopped managed thread",
                        }
                    ]
                },
            ),
        )
        self.assertEqual(result.state, CommandState.COMPLETED)
        old = self.ledger.show("fc-repair-thread-task")
        work = self.ledger.show("fc-repair-thread")
        assert old is not None and old.fc and work is not None and work.fc
        successor = self.ledger.show(str(old.fc["replaced_by"]))
        assert successor is not None and successor.fc
        self.assertEqual(work.fc["owner"], successor.fc["thread_id"])
        operation = self.ledger.show(str(result.operation_id))
        assert operation is not None and operation.fc
        progress = operation.fc["planned"]["progress"]
        self.assertEqual(
            progress["evidence"][0]["effect"]["new_task_record_id"],
            successor.id,
        )
