from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import threading
import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fulcrum.contracts import ActorContext, CommandResult, FulcrumError, ParsedRequest
from fulcrum.diagnostics import DiagnosticService
from fulcrum.instance import resolve_instance
from fulcrum.ledger import Ledger, operation_id
from fulcrum.runtime import ReleaseFacts, ResourceFacts, TaskFacts, TurnFacts
from fulcrum.supervision import ControllerSupervisor


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 14, 16, 0, tzinfo=timezone.utc)
        self.elapsed = 0.0

    def now(self) -> datetime:
        return self.value

    def monotonic(self) -> float:
        return self.elapsed

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)
        self.elapsed += seconds


class FakeRuntime:
    def __init__(self) -> None:
        self.facts: dict[str, TaskFacts] = {}
        self.sent: list[tuple[str, str, str]] = []
        self.released: list[str] = []
        self.fd_soft_limit = 100
        self.fd_usage = 10
        self.overloaded = False

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def inspect_task(self, thread_id: str) -> TaskFacts:
        return self.facts[thread_id]

    async def resources(self) -> ResourceFacts:
        return ResourceFacts(
            loaded_count=len(self.facts),
            active_count=sum(
                facts.active_turn is not None for facts in self.facts.values()
            ),
            loaded_ids=tuple(self.facts),
            fd_soft_limit=self.fd_soft_limit,
            fd_usage=self.fd_usage,
            overloaded=self.overloaded,
            observed_at="2026-09-14T16:00:00Z",
        )

    async def start_turn(self, thread_id: str, turn: Any) -> TurnFacts:
        self.sent.append((thread_id, turn.operation_id, turn.text))
        return TurnFacts(
            id=f"turn-{len(self.sent)}",
            thread_id=thread_id,
            state="inProgress",
            operation_id=turn.operation_id,
            completed=False,
            error=None,
            tools=(),
            usage=None,
            observed_at="2026-09-14T16:00:00Z",
        )

    async def release(self, thread_id: str) -> ReleaseFacts:
        self.released.append(thread_id)
        return ReleaseFacts(
            thread_id=thread_id,
            status="unsubscribed",
            active_terminals=(),
            observed_at="2026-09-14T16:00:00Z",
        )


class RetryApplication:
    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        self.failures: dict[str, int] = {}
        self.calls: dict[str, int] = {}
        self.effects: set[str] = set()
        self.blockers: dict[str, threading.Event] = {}
        self.started: dict[str, threading.Event] = {}

    def dispatch(self, request: ParsedRequest) -> CommandResult:
        assert request.request_id is not None
        receipt_id = operation_id(request.request_id)
        self.calls[receipt_id] = self.calls.get(receipt_id, 0) + 1
        remaining = self.failures.get(receipt_id, 0)
        if remaining:
            self.failures[receipt_id] = remaining - 1
            raise FulcrumError(
                "PROVED_TRANSIENT",
                "fixture send was transient",
                exit_code=4,
                retryable=True,
            )
        effect = str(request.input.get("effect"))
        self.started.setdefault(effect, threading.Event()).set()
        blocker = self.blockers.get(effect)
        if blocker is not None:
            if not blocker.wait(30):
                raise RuntimeError("fixture blocker timed out")
        self.effects.add(effect)
        record = self.ledger.show(receipt_id)
        assert record is not None
        settled = self.ledger.update_operation(
            receipt_id,
            state="completed",
            step="effect_observed",
            result={"effect": effect},
            next_action="No further action is required.",
        )
        return CommandResult.query({"operation": settled.id})


class Fulcrum2SupervisionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.brain = root / "brain"
        self.instance = root / "instance"
        self.project = root / "project"
        for path in (self.brain, self.instance, self.project):
            path.mkdir()
        subprocess.run(["git", "init", "--quiet"], cwd=self.brain, check=True)
        subprocess.run(
            ["git", "config", "user.email", "fixture@example.com"],
            cwd=self.brain,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Fixture"],
            cwd=self.brain,
            check=True,
        )
        subprocess.run(
            ["git", "config", "beads.role", "maintainer"],
            cwd=self.brain,
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
                    "    enabled: true",
                    "    delivery:",
                    "      id: fixture",
                    "",
                )
            ),
            encoding="utf-8",
        )
        (self.instance / "config").symlink_to(self.config)
        self.context = resolve_instance(instance=str(self.instance), config=None)
        self.clock = FakeClock()
        self.runtime = FakeRuntime()
        self.ledger = Ledger(self.brain)
        self.ledger.create_record(
            record_id="fc-system",
            kind="control",
            title="Fulcrum control",
            description="Standing leadership identities.",
            owner="native-marshal-leader",
            fc={
                "kind": "control",
                "owner": "native-marshal-leader",
                "vizier_thread": "native-vizier-leader",
                "marshal_thread": "native-marshal-leader",
                "active_takeover": None,
                "last_transition": None,
            },
        )
        for role in ("vizier", "marshal"):
            thread_id = f"native-{role}-leader"
            facts = TaskFacts(
                id=thread_id,
                title=f"standing {role}",
                cwd=str(self.brain),
                project_id=None,
                workspace_roots=(str(self.brain),),
                archived=False,
                exists=True,
                loaded=True,
                runtime_status="idle",
                active_turn=None,
                last_turn=None,
                pending_requests=(),
                observed_at="2026-09-14T16:00:00Z",
            )
            self.runtime.facts[thread_id] = facts
            self.ledger.create_record(
                record_id=f"fc-{role}-leader-task",
                kind="task",
                title=f"Managed task: standing {role}",
                description=f"Standing {role} task.",
                owner=thread_id,
                external_ref=f"fulcrum:thread:{thread_id}",
                fc={
                    "kind": "task",
                    "owner": thread_id,
                    "thread_id": thread_id,
                    "role": role,
                    "purpose": "leadership",
                    "work_bead": None,
                    "creation_operation": f"fc-{role}-bootstrap",
                    "last_observed": facts.to_dict(),
                    "last_turn": None,
                    "deleted_at": None,
                    "replaced_by": None,
                },
            )
        self.request = ParsedRequest(
            command=("reconcile",),
            arguments={},
            input={},
            actor=ActorContext(kind="human"),
            instance=self.context,
            request_id=str(uuid.uuid4()),
            timeout=10,
            offline=True,
        )

    def tearDown(self) -> None:
        subprocess.run(
            ["bd", "-C", str(self.brain), "dolt", "stop"],
            capture_output=True,
            check=False,
            timeout=20,
        )
        self.temporary.cleanup()

    def create_operation(self, effect: str) -> str:
        request = ParsedRequest(
            command=("test", "effect"),
            arguments={},
            input={"effect": effect},
            actor=ActorContext(kind="human"),
            instance=self.context,
            request_id=str(uuid.uuid4()),
            timeout=10,
            offline=True,
        )
        operation, _ = self.ledger.create_operation(
            request,
            planned={"effect": effect},
            next_action="Apply the fixture effect.",
        )
        return operation.id

    async def test_retry_schedule_reuses_receipt_and_exhaustion_survives_ticks(
        self,
    ) -> None:
        application = RetryApplication(self.ledger)
        operation_id = self.create_operation("event-a")
        application.failures[operation_id] = 2
        supervisor = ControllerSupervisor(
            self.request, application, clock=self.clock, runtime=self.runtime  # type: ignore[arg-type]
        )

        first = await supervisor.run_once(operation_id=operation_id)
        self.assertEqual(application.calls[operation_id], 1)
        self.assertFalse(first.operations[0]["advanced"])
        self.clock.advance(1)
        await supervisor.run_once(operation_id=operation_id)
        self.assertEqual(application.calls[operation_id], 1)
        self.clock.advance(1)
        await supervisor.run_once(operation_id=operation_id)
        self.assertEqual(application.calls[operation_id], 2)
        self.clock.advance(10)
        await supervisor.run_once(operation_id=operation_id)
        self.assertEqual(application.calls[operation_id], 3)
        self.assertEqual(application.effects, {"event-a"})
        settled = self.ledger.show(operation_id)
        assert settled is not None and settled.fc
        self.assertEqual(settled.fc["state"], "completed")
        self.assertEqual(settled.fc["attempts"], 3)

        exhausted_id = self.create_operation("event-b")
        application.failures[exhausted_id] = 4
        for delay in (0, 2, 10):
            self.clock.advance(delay)
            await supervisor.run_once(operation_id=exhausted_id)
        exhausted = self.ledger.show(exhausted_id)
        assert exhausted is not None and exhausted.fc
        self.assertEqual(exhausted.fc["state"], "failed")
        self.assertEqual(exhausted.fc["attempts"], 3)
        calls = application.calls[exhausted_id]
        self.clock.advance(1000)
        await supervisor.run_once(operation_id=exhausted_id)
        self.assertEqual(application.calls[exhausted_id], calls)

    async def test_crash_reconciliation_and_uncertain_effect_do_not_duplicate(
        self,
    ) -> None:
        application = RetryApplication(self.ledger)
        operation_id = self.create_operation("already-applied")
        application.effects.add("already-applied")
        supervisor = ControllerSupervisor(
            self.request, application, clock=self.clock, runtime=self.runtime  # type: ignore[arg-type]
        )
        await supervisor.run_once(operation_id=operation_id)
        self.assertEqual(application.effects, {"already-applied"})
        self.assertEqual(application.calls[operation_id], 1)

        uncertain_id = self.create_operation("never-replay")
        self.ledger.update_operation(
            uncertain_id,
            state="uncertain",
            step="effect_unknown",
            next_action="Inspect the postcondition.",
        )
        result = await supervisor.run_once(operation_id=uncertain_id)
        self.assertNotIn(uncertain_id, application.calls)
        self.assertTrue(result.operations[0]["recovery_required"])

    async def test_clock_drives_one_reminder_checkpoint_and_recovery_request(
        self,
    ) -> None:
        work_id = "fc-supervised-work"
        acquisition = "fc-supervised-acquisition"
        thread_id = "native-supervised"
        started = self.clock.now().isoformat().replace("+00:00", "Z")
        work_record = self.ledger.create_record(
            record_id=work_id,
            kind="work",
            title="Supervised work",
            description="Compiled role instructions.",
            owner=thread_id,
            fc={
                "kind": "work",
                "owner": thread_id,
                "project": "toy",
                "phase": "working",
                "role": "executor",
                "ownership_operation": acquisition,
                "next_action": "Finish with evidence.",
                "last_progress_at": started,
                "waiting": None,
            },
        )
        self.ledger.update_fc(work_id, work_record.fc or {}, status="in_progress")
        self.ledger.create_record(
            record_id="fc-supervised-task",
            kind="task",
            title="Supervised task",
            description="Native supervised task.",
            owner=thread_id,
            fc={
                "kind": "task",
                "owner": thread_id,
                "thread_id": thread_id,
                "role": "executor",
                "work_bead": work_id,
                "ownership_operation": acquisition,
                "model": "luna",
                "effort": "high",
                "last_runtime_event_at": started,
                "last_substantive_progress_at": started,
            },
        )
        self.runtime.facts[thread_id] = TaskFacts(
            id=thread_id,
            title="Supervised task",
            cwd=str(self.project),
            project_id=None,
            workspace_roots=(str(self.project),),
            archived=False,
            exists=True,
            loaded=True,
            runtime_status="inProgress",
            active_turn="active-turn",
            last_turn={
                "id": "active-turn",
                "status": "inProgress",
                "items": [{"type": "commandExecution", "output": "new evidence"}],
            },
            pending_requests=(),
            observed_at=started,
        )
        supervisor = ControllerSupervisor(
            self.request,
            RetryApplication(self.ledger),
            clock=self.clock,
            runtime=self.runtime,  # type: ignore[arg-type]
        )

        self.clock.advance(1800)
        live = await supervisor.run_once(bead_id=work_id)
        self.assertNotIn(
            "recovery_request", [item["kind"] for item in live.next_actions]
        )
        self.assertEqual(self.runtime.sent, [])
        self.runtime.facts[thread_id] = replace(
            self.runtime.facts[thread_id],
            runtime_status="idle",
            active_turn=None,
            last_turn={"id": "finished-turn", "status": "completed"},
        )
        first = await supervisor.run_once(bead_id=work_id)
        reminders = [
            item[2] for item in self.runtime.sent if "ended without" in item[2]
        ]
        self.assertEqual(len(reminders), 1)
        self.assertIn(f"--bead {work_id}", reminders[0])
        self.assertIn(f"--ownership-operation {acquisition}", reminders[0])
        self.assertIn("finish_reminder", [item["kind"] for item in first.next_actions])
        await supervisor.run_once(bead_id=work_id)
        self.assertEqual(len(self.runtime.sent), 1)

        self.clock.advance(600)
        checkpoint = await supervisor.run_once(bead_id=work_id)
        self.assertIn("checkpoint", [item["kind"] for item in checkpoint.next_actions])
        self.assertEqual(len(self.runtime.sent), 2)
        self.clock.advance(1200)
        recovery = await supervisor.run_once(bead_id=work_id)
        self.assertIn(
            "recovery_request", [item["kind"] for item in recovery.next_actions]
        )
        await supervisor.run_once(bead_id=work_id)
        self.assertEqual(len(self.runtime.sent), 2)
        work = self.ledger.show(work_id)
        assert work is not None and work.fc
        self.assertEqual(
            work.fc["waiting"]["reason"], "stalled without substantive progress"
        )

        health_path = self.instance / "service-health.json"
        health = json.loads(health_path.read_text(encoding="utf-8"))
        health["runner"]["state"] = "unavailable"
        health["runner"]["consecutive_failures"] = 3
        health["runner"]["error"] = "runner fixture failure"
        health_path.write_text(json.dumps(health), encoding="utf-8")
        self.context.socket_path.touch()
        doctor = DiagnosticService().doctor(self.request).result or {}
        loops = {item["name"]: item for item in doctor["loops"]}
        self.assertEqual(loops["runner"]["state"], "unavailable")
        self.assertTrue(self.context.socket_path.exists())

    async def test_independent_effect_runs_while_another_external_call_waits(
        self,
    ) -> None:
        application = RetryApplication(self.ledger)
        blocked_id = self.create_operation("blocked-effect")
        independent_id = self.create_operation("independent-effect")
        release = threading.Event()
        application.blockers["blocked-effect"] = release
        application.started["blocked-effect"] = threading.Event()
        application.started["independent-effect"] = threading.Event()
        supervisor = ControllerSupervisor(
            self.request,
            application,
            clock=self.clock,
            runtime=self.runtime,  # type: ignore[arg-type]
        )
        running = asyncio.create_task(supervisor.run_once())
        await asyncio.wait_for(
            asyncio.gather(
                asyncio.to_thread(application.started["blocked-effect"].wait),
                asyncio.to_thread(application.started["independent-effect"].wait),
            ),
            20,
        )
        await asyncio.sleep(0)
        self.assertIn("independent-effect", application.effects)
        self.assertNotIn("blocked-effect", application.effects)
        release.set()
        await asyncio.wait_for(running, 10)
        independent = self.ledger.show(independent_id)
        blocked = self.ledger.show(blocked_id)
        assert independent is not None and independent.fc
        assert blocked is not None and blocked.fc
        self.assertEqual(independent.fc["state"], "completed")
        self.assertEqual(blocked.fc["state"], "completed")

    async def test_active_recovery_fence_pauses_ordinary_dispatch(self) -> None:
        control = self.ledger.show("fc-system")
        assert control is not None and control.fc
        control_fc = dict(control.fc)
        control_fc["active_takeover"] = {
            "operation_id": "fc-recovery-fixture",
            "scope": "instance",
            "state": "active",
            "bead_ids": [],
            "project_ids": ["toy"],
            "owner_thread": "native-justiciar",
        }
        self.ledger.update_fc(control.id, control_fc)
        supervisor = ControllerSupervisor(
            self.request,
            RetryApplication(self.ledger),
            clock=self.clock,
            runtime=self.runtime,  # type: ignore[arg-type]
        )

        summary = await supervisor.run_once()

        self.assertTrue(summary.pressure["paused"])
        self.assertEqual(summary.pressure["reason"], "active recovery fence")
        self.assertEqual(self.runtime.sent, [])
        self.assertIn(
            "ordinary_dispatch_paused",
            [
                action.get("effect")
                for action in summary.next_actions
                if action.get("kind") == "recovery_fence"
            ],
        )

    async def test_pressure_releases_only_idle_completed_subscription(self) -> None:
        thread_id = "native-completed"
        work = self.ledger.create_record(
            record_id="fc-completed-work",
            kind="work",
            title="Completed work",
            description="Done.",
            owner=thread_id,
            fc={
                "kind": "work",
                "owner": thread_id,
                "project": "toy",
                "phase": "done",
                "ownership_operation": "completed-acquisition",
            },
        )
        self.ledger.run(
            ("close", work.id, "--reason", "fixture complete"), mutating=True
        )
        self.ledger.create_record(
            record_id="fc-completed-task",
            kind="task",
            title="Completed task",
            description="Native completed task.",
            owner=thread_id,
            fc={
                "kind": "task",
                "owner": thread_id,
                "thread_id": thread_id,
                "role": "executor",
                "work_bead": work.id,
                "ownership_operation": "completed-acquisition",
                "model": "luna",
                "effort": "high",
            },
        )
        self.runtime.facts[thread_id] = TaskFacts(
            id=thread_id,
            title="Completed task",
            cwd=str(self.project),
            project_id=None,
            workspace_roots=(str(self.project),),
            archived=False,
            exists=True,
            loaded=True,
            runtime_status="idle",
            active_turn=None,
            last_turn={"id": "done", "status": "completed"},
            pending_requests=(),
            observed_at="2026-09-14T16:00:00Z",
        )
        self.runtime.fd_usage = 90
        supervisor = ControllerSupervisor(
            self.request,
            RetryApplication(self.ledger),
            clock=self.clock,
            runtime=self.runtime,  # type: ignore[arg-type]
        )
        result = await supervisor.run_once()
        self.assertTrue(result.pressure["paused"])
        self.assertEqual(result.pressure["released_idle_subscriptions"], [thread_id])

    async def test_review_gets_one_finish_reminder_then_recovery_and_release(
        self,
    ) -> None:
        root = self.ledger.create_record(
            record_id="fc-review-root",
            kind="work",
            title="Plan under review",
            description="Review this candidate.",
            owner="native-author",
            fc={
                "kind": "work",
                "owner": "native-author",
                "project": "toy",
                "phase": "planning",
                "ownership_operation": "author-acquisition",
                "plan": {
                    "reviews": {
                        "cold_reader": {
                            "state": "running",
                            "review_operation": "fc-review-start",
                        }
                    }
                },
            },
        )
        thread_id = "native-review-task"
        task = self.ledger.create_record(
            record_id="fc-review-task",
            kind="task",
            title="Independent review",
            description="Cold-reader review.",
            owner=thread_id,
            external_ref=f"fulcrum:thread:{thread_id}",
            fc={
                "kind": "task",
                "owner": thread_id,
                "thread_id": thread_id,
                "role": "weaver",
                "purpose": "plan_review",
                "perspective": "cold_reader",
                "project": "toy",
                "work_bead": None,
                "associated_beads": [root.id],
                "review_operation": "fc-review-start",
                "model": "luna",
                "effort": "high",
            },
        )
        self.runtime.facts[thread_id] = TaskFacts(
            id=thread_id,
            title="Independent review",
            cwd=str(self.project),
            project_id=None,
            workspace_roots=(str(self.project),),
            archived=False,
            exists=True,
            loaded=True,
            runtime_status="idle",
            active_turn=None,
            last_turn={"id": "review-turn", "status": "completed"},
            pending_requests=(),
            observed_at="2026-09-14T16:00:00Z",
        )
        supervisor = ControllerSupervisor(
            self.request,
            RetryApplication(self.ledger),
            clock=self.clock,
            runtime=self.runtime,  # type: ignore[arg-type]
        )

        first = await supervisor.run_once(bead_id=root.id)
        reminders = [
            item
            for item in first.next_actions
            if item["kind"] == "review_finish_reminder"
        ]
        self.assertEqual(len(reminders), 1)
        self.assertEqual(len(self.runtime.sent), 1)
        reminder_turn = str(reminders[0]["turn_id"])
        self.runtime.facts[thread_id] = replace(
            self.runtime.facts[thread_id],
            last_turn={"id": reminder_turn, "status": "completed"},
        )

        second = await supervisor.run_once(bead_id=root.id)
        self.assertEqual(len(self.runtime.sent), 1)
        self.assertIn(
            "review_recovery_request", [item["kind"] for item in second.next_actions]
        )
        current_root = self.ledger.show(root.id)
        assert current_root is not None and current_root.fc
        self.assertEqual(
            current_root.fc["plan"]["reviews"]["cold_reader"]["state"],
            "recovery_required",
        )

        current_task = self.ledger.show(task.id)
        assert current_task is not None and current_task.fc
        task_fc = dict(current_task.fc)
        task_fc["review_result_operation"] = "fc-review-finish"
        self.ledger.update_fc(task.id, task_fc)
        third = await supervisor.run_once(bead_id=root.id)
        self.assertIn(thread_id, self.runtime.released)
        self.assertIn("review_release", [item["kind"] for item in third.next_actions])

    async def test_three_critical_loop_failures_exit_and_remain_observable(
        self,
    ) -> None:
        supervisor = ControllerSupervisor(
            self.request,
            RetryApplication(self.ledger),
            clock=self.clock,
            runtime=self.runtime,  # type: ignore[arg-type]
        )

        async def fail() -> None:
            raise RuntimeError("persistent fixture failure")

        with patch("fulcrum.supervision.RETRY_DELAYS", (0.0, 0.0)):
            with self.assertRaisesRegex(RuntimeError, "failed 3 times"):
                await supervisor._supervise("runner", fail)
        retained = json.loads(
            (self.instance / "service-health.json").read_text(encoding="utf-8")
        )
        self.assertEqual(retained["runner"]["consecutive_failures"], 3)
        self.assertEqual(retained["runner"]["state"], "unavailable")

    async def test_installed_serve_once_returns_bounded_pass(self) -> None:
        executable = Path(os.sys.executable).with_name("fulcrum")
        process = await asyncio.create_subprocess_exec(
            str(executable),
            "serve",
            "--once",
            "--instance",
            str(self.instance),
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={
                key: value
                for key, value in os.environ.items()
                if key != "CODEX_THREAD_ID"
            },
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
        self.assertEqual(process.returncode, 0, stderr.decode() + stdout.decode())
        result = json.loads(stdout)["result"]
        self.assertIn("operations", result)
        self.assertIn("next_actions", result)
        self.assertFalse(self.context.socket_path.exists())


if __name__ == "__main__":
    unittest.main()
