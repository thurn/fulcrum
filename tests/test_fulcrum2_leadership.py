from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fulcrum.application import default_application
from fulcrum.configuration import ConfigurationManager
from fulcrum.contracts import ActorContext, CommandResult, ParsedRequest
from fulcrum.instance import resolve_instance
from fulcrum.leadership import (
    BRIEF_CHARACTER_LIMIT,
    AdmissionService,
    LeadershipService,
    capacity_snapshot,
    ensure_leadership,
    _create_or_recover_leader,
    marshal_context,
    normalize_native_intake,
)
from fulcrum.ledger import Ledger, LedgerRecord
from fulcrum.roles import LEADERSHIP_TITLES
from fulcrum.runtime import (
    AppServerError,
    ResourceFacts,
    TaskFacts,
    TaskSpec,
    TurnFacts,
)
from fulcrum.supervision import ControllerSupervisor


class ManualClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 14, 17, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.value

    def monotonic(self) -> float:
        return 0.0

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class FakeLeadershipRuntime:
    def __init__(self) -> None:
        self.tasks: dict[str, TaskFacts] = {}
        self.created: list[str] = []
        self.turns: list[str] = []
        self.rejected_creates = 0

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def find_tasks(self, creation_cwd: str) -> list[TaskFacts]:
        return [task for task in self.tasks.values() if task.cwd == creation_cwd]

    async def create_task(self, spec: Any) -> TaskFacts:
        if self.rejected_creates:
            self.rejected_creates -= 1
            raise AppServerError(
                "thread not found: 01a0a4b6-6fb7-7302-a990-0f248370e080",
                category="rejected",
            )
        thread_id = f"native-leader-{len(self.tasks) + 1}"
        task = TaskFacts(
            id=thread_id,
            title=spec.title,
            cwd=spec.creation_cwd,
            project_id=None,
            workspace_roots=spec.workspace_roots,
            archived=False,
            exists=True,
            loaded=True,
            runtime_status="idle",
            active_turn=None,
            last_turn=None,
            pending_requests=(),
            observed_at="2026-09-14T00:00:00Z",
        )
        self.tasks[thread_id] = task
        self.created.append(thread_id)
        return task

    async def inspect_task(self, thread_id: str) -> TaskFacts:
        return self.tasks[thread_id]

    async def start_turn(self, thread_id: str, turn_input: Any) -> TurnFacts:
        self.turns.append(thread_id)
        turn = TurnFacts(
            id=f"turn-{len(self.turns)}",
            thread_id=thread_id,
            state="inProgress",
            operation_id=turn_input.operation_id,
            completed=False,
            error=None,
            tools=(),
            usage=None,
            observed_at="2026-09-14T17:00:02Z",
        )
        task = self.tasks[thread_id]
        self.tasks[thread_id] = TaskFacts(
            id=task.id,
            title=task.title,
            cwd=task.cwd,
            project_id=task.project_id,
            workspace_roots=task.workspace_roots,
            archived=task.archived,
            exists=task.exists,
            loaded=task.loaded,
            runtime_status="inProgress",
            active_turn=turn.id,
            last_turn=turn.to_dict(),
            pending_requests=task.pending_requests,
            observed_at=turn.observed_at,
        )
        return turn

    async def resources(self) -> ResourceFacts:
        return ResourceFacts(
            loaded_count=len(self.tasks),
            active_count=sum(
                task.active_turn is not None for task in self.tasks.values()
            ),
            loaded_ids=tuple(self.tasks),
            fd_soft_limit=100,
            fd_usage=10,
            overloaded=False,
            observed_at="2026-09-14T00:00:00Z",
        )

    async def release(self, thread_id: str) -> None:
        return None


class Fulcrum2LeadershipTest(unittest.TestCase):
    def test_absent_fresh_leader_is_retried_from_same_intent(self) -> None:
        runtime = FakeLeadershipRuntime()
        runtime.rejected_creates = 1
        spec = TaskSpec(
            creation_cwd=str(self.instance / "leaders" / "vizier-retry"),
            cwd=str(self.brain),
            project_id=None,
            workspace_roots=(str(self.brain),),
            title="Vizier retry",
            model="luna",
            effort="low",
        )

        task, created = asyncio.run(
            _create_or_recover_leader(runtime, spec, excluded_threads=set())
        )

        self.assertTrue(created)
        self.assertEqual(task.title, "Vizier retry")
        self.assertEqual(runtime.rejected_creates, 0)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name).resolve()
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
        self.ledger = Ledger(self.brain)
        self.marshal = "native-marshal"
        self.vizier = "native-vizier"
        self.ledger.create_record(
            record_id="fc-system",
            kind="control",
            title="Fulcrum control",
            description="Standing leadership identities.",
            owner=self.marshal,
            fc={
                "kind": "control",
                "owner": self.marshal,
                "vizier_thread": self.vizier,
                "marshal_thread": self.marshal,
                "active_takeover": None,
                "last_transition": None,
            },
        )
        self.ledger.create_record(
            record_id="fc-marshal-task",
            kind="task",
            title=f"Managed task: {LEADERSHIP_TITLES['marshal']}",
            description="Standing Marshal task.",
            owner=self.marshal,
            external_ref=f"fulcrum:thread:{self.marshal}",
            fc={
                "kind": "task",
                "owner": self.marshal,
                "thread_id": self.marshal,
                "role": "marshal",
                "work_bead": None,
                "ownership_operation": None,
                "creation_operation": "fc-bootstrap-marshal",
                "creation_cwd": str(self.instance / "leaders" / "marshal"),
                "model": "gpt-5.6-sol",
                "effort": "high",
                "last_observed": {
                    "runtime_status": "idle",
                    "active_turn": None,
                    "observed_at": "2026-09-14T00:00:00Z",
                },
                "last_turn": None,
                "deleted_at": None,
                "replaced_by": None,
            },
        )

    def invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        executable = Path(os.sys.executable).with_name("fulcrum")
        return subprocess.run(
            [str(executable), *arguments],
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )

    def tearDown(self) -> None:
        subprocess.run(
            ["bd", "-C", str(self.brain), "dolt", "stop"],
            capture_output=True,
            check=False,
            timeout=20,
        )
        self.temporary.cleanup()

    def request(
        self,
        command: tuple[str, ...],
        *,
        arguments: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        actor: ActorContext | None = None,
    ) -> ParsedRequest:
        selected_actor = actor or ActorContext(kind="human")
        return ParsedRequest(
            command=command,
            arguments=arguments or {},
            input=payload or {},
            actor=selected_actor,
            instance=self.context,
            request_id=str(uuid.uuid4()),
            project="toy",
            thread_id=(
                selected_actor.task_id if selected_actor.kind == "task" else None
            ),
            timeout=10,
            offline=True,
        )

    def create_work(
        self,
        bead_id: str,
        *,
        intake: dict[str, Any] | None = None,
        dispatch: dict[str, Any] | None = None,
        priority: int = 2,
    ) -> LedgerRecord:
        return self.ledger.create_record(
            record_id=bead_id,
            kind="work",
            title=f"Proposal {bead_id}",
            description=f"Produce the observable outcome for {bead_id}.",
            acceptance="The result is observable.",
            owner=self.marshal,
            priority=priority,
            fc={
                "kind": "work",
                "project": "toy",
                "owner": self.marshal,
                "role": "marshal",
                "ownership_operation": f"{bead_id}-acquisition",
                "phase": "backlog",
                "requested_role": "executor",
                "outcome": f"Produce the observable outcome for {bead_id}.",
                "acceptance": ["The result is observable."],
                "intake": intake,
                "size": "small",
                "overlap_tags": [],
                "context": [f"evidence:{bead_id}"],
                "waiting": None,
                "dispatch": dispatch,
                "next_action": "Marshal must choose the next responsibility.",
                "last_progress": None,
                "last_transition": f"{bead_id}-acquisition",
            },
        )

    def test_brief_is_separate_bounded_and_preserves_unknown_intake(self) -> None:
        for index in range(14):
            self.create_work(
                f"fc-proposal-{index:02d}", intake=None, priority=index % 5
            )
        self.create_work(
            "fc-actionable",
            intake={"benefit": "Reduce operator latency.", "uncertainties": []},
            priority=0,
        )
        service = LeadershipService()
        groom = service.marshal_brief(
            self.request(("marshal", "brief"), arguments={"kind": "groom"})
        ).result
        assert groom is not None
        self.assertEqual(groom["kind"], "groom")
        self.assertLessEqual(len(groom["rows"]), 12)
        self.assertGreater(groom["omitted_counts"]["groom"], 0)
        self.assertLessEqual(
            len(
                json.dumps(
                    groom, separators=(",", ":"), ensure_ascii=False, sort_keys=True
                )
            ),
            BRIEF_CHARACTER_LIMIT,
        )
        self.assertIn("benefit", groom["rows"][0]["unknowns"])
        self.assertNotIn("fc-actionable", {row["bead_id"] for row in groom["rows"]})
        dispatch = service.marshal_brief(
            self.request(("marshal", "brief"), arguments={"kind": "dispatch"})
        ).result
        assert dispatch is not None
        self.assertEqual(
            [row["bead_id"] for row in dispatch["rows"]], ["fc-actionable"]
        )

    def test_installed_leader_brief_backlog_and_no_decision_request(self) -> None:
        leader = self.invoke(
            "leader",
            "show",
            "marshal",
            "--instance",
            str(self.instance),
            "--offline",
            "--json",
        )
        self.assertEqual(leader.returncode, 0, leader.stderr + leader.stdout)
        self.assertEqual(json.loads(leader.stdout)["result"]["thread_id"], self.marshal)
        brief = self.invoke(
            "marshal",
            "brief",
            "--kind",
            "recover",
            "--instance",
            str(self.instance),
            "--offline",
            "--json",
        )
        self.assertEqual(brief.returncode, 0, brief.stderr + brief.stdout)
        self.assertFalse(json.loads(brief.stdout)["result"]["decision_required"])
        requested = self.invoke(
            "marshal",
            "request",
            "--kind",
            "recover",
            "--actor",
            f"task:{self.marshal}",
            "--thread-id",
            self.marshal,
            "--instance",
            str(self.instance),
            "--offline",
            "--json",
        )
        self.assertEqual(requested.returncode, 0, requested.stderr + requested.stdout)
        operation_id = json.loads(requested.stdout)["operation_id"]
        operation = self.ledger.show(operation_id)
        assert operation is not None and operation.fc
        self.assertEqual(operation.fc["step"], "no_decision_required")
        self.assertIsNone(operation.fc["result"]["turn"])
        backlog = self.invoke(
            "backlog",
            "list",
            "--include-deferred",
            "--instance",
            str(self.instance),
            "--offline",
            "--json",
        )
        self.assertEqual(backlog.returncode, 0, backlog.stderr + backlog.stdout)
        self.assertEqual(json.loads(backlog.stdout)["result"]["items"], [])

    def test_cli_briefs_keep_priority_overlap_dependencies_and_recovery_separate(
        self,
    ) -> None:
        prerequisite = self.create_work(
            "fc-cli-prerequisite",
            intake={"benefit": "Known", "uncertainties": []},
            priority=3,
        )
        prerequisite_fc = dict(prerequisite.fc or {})
        prerequisite_fc["phase"] = "working"
        self.ledger.update_fc(prerequisite.id, prerequisite_fc)
        dependent = self.create_work(
            "fc-cli-dependent",
            intake={"benefit": "Known", "uncertainties": []},
            priority=1,
        )
        dependent_fc = dict(dependent.fc or {})
        dependent_fc["overlap_tags"] = ["shared-database"]
        self.ledger.update_fc(dependent.id, dependent_fc)
        self.ledger.run(("dep", "add", dependent.id, prerequisite.id), mutating=True)
        urgent = self.create_work(
            "fc-cli-urgent",
            intake={"benefit": "Known", "uncertainties": []},
            priority=0,
        )
        urgent_fc = dict(urgent.fc or {})
        urgent_fc["overlap_tags"] = ["shared-database"]
        self.ledger.update_fc(urgent.id, urgent_fc)
        self.create_work("fc-cli-incomplete", intake=None, priority=0)
        recovery = self.create_work(
            "fc-cli-recovery",
            intake={"benefit": "Known", "uncertainties": []},
            priority=0,
        )
        recovery_fc = dict(recovery.fc or {})
        recovery_fc["phase"] = "recovering"
        recovery_fc["recovery"] = {
            "diagnosis": "The retained external effect is uncertain.",
            "scope": "Choose whether to inspect or retry the exact effect.",
        }
        self.ledger.update_fc(recovery.id, recovery_fc)

        def brief(kind: str) -> dict[str, Any]:
            completed = self.invoke(
                "marshal",
                "brief",
                "--kind",
                kind,
                "--instance",
                str(self.instance),
                "--offline",
                "--json",
            )
            self.assertEqual(
                completed.returncode, 0, completed.stderr + completed.stdout
            )
            return json.loads(completed.stdout)["result"]

        dispatch = brief("dispatch")
        self.assertEqual(
            [row["bead_id"] for row in dispatch["rows"]],
            [urgent.id, dependent.id],
        )
        dependent_row = dispatch["rows"][1]
        self.assertEqual(
            dependent_row["expected_ownership_operation"],
            dependent_fc["ownership_operation"],
        )
        self.assertEqual(dependent_row["expected_phase"], dependent_fc["phase"])
        self.assertEqual(
            dependent_row["decision_context"]["dependency_blockers"],
            [prerequisite.id],
        )
        self.assertEqual(
            dependent_row["decision_context"]["overlap_tags"],
            ["shared-database"],
        )
        self.assertEqual(
            [row["bead_id"] for row in brief("groom")["rows"]],
            ["fc-cli-incomplete"],
        )
        recover = brief("recover")
        self.assertEqual([row["bead_id"] for row in recover["rows"]], [recovery.id])
        self.assertIn("scoped recovery", recover["rows"][0]["decision_needed"])

    def test_automatic_dispatch_omits_plan_roots_and_dependency_blocked_work(
        self,
    ) -> None:
        root = self.create_work(
            "fc-plan-root",
            intake={"benefit": "Known", "uncertainties": []},
            priority=0,
        )
        root_fc = dict(root.fc or {})
        root_fc["plan"] = {
            "published_scope": {"summary": "Coordinate the published plan."},
            "children_by_key": {"child": "fc-plan-child"},
        }
        self.ledger.update_fc(root.id, root_fc)

        prerequisite = self.create_work(
            "fc-plan-prerequisite",
            intake={"benefit": "Known", "uncertainties": []},
        )
        prerequisite_fc = dict(prerequisite.fc or {})
        prerequisite_fc["phase"] = "working"
        self.ledger.update_fc(prerequisite.id, prerequisite_fc)
        child = self.create_work(
            "fc-plan-child",
            intake={"benefit": "Known", "uncertainties": []},
            priority=1,
        )
        self.ledger.run(("dep", "add", child.id, prerequisite.id), mutating=True)

        automatic = self.invoke(
            "marshal",
            "brief",
            "--kind",
            "auto",
            "--instance",
            str(self.instance),
            "--offline",
            "--json",
        )
        self.assertEqual(automatic.returncode, 0, automatic.stderr + automatic.stdout)
        automatic_result = json.loads(automatic.stdout)["result"]
        self.assertFalse(automatic_result["decision_required"])
        self.assertEqual(automatic_result["rows"], [])

        targeted = self.invoke(
            "marshal",
            "brief",
            "--kind",
            "dispatch",
            "--bead",
            child.id,
            "--instance",
            str(self.instance),
            "--offline",
            "--json",
        )
        self.assertEqual(targeted.returncode, 0, targeted.stderr + targeted.stdout)
        targeted_result = json.loads(targeted.stdout)["result"]
        self.assertEqual(
            [row["bead_id"] for row in targeted_result["rows"]], [child.id]
        )
        self.assertFalse(
            targeted_result["rows"][0]["decision_context"]["dependencies_ready"]
        )

    def test_bootstrap_records_intents_and_creates_idle_leaders_without_turns(
        self,
    ) -> None:
        root = Path(self.temporary.name).resolve()
        brain = root / "bootstrap-brain"
        instance = root / "bootstrap-instance"
        brain.mkdir()
        instance.mkdir()
        subprocess.run(["git", "init", "--quiet"], cwd=brain, check=True)
        subprocess.run(
            ["git", "config", "user.email", "fixture@example.com"],
            cwd=brain,
            check=True,
        )
        subprocess.run(["git", "config", "user.name", "Fixture"], cwd=brain, check=True)
        subprocess.run(
            ["git", "config", "beads.role", "maintainer"], cwd=brain, check=True
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
            cwd=brain,
            capture_output=True,
            check=True,
            timeout=30,
        )
        config_path = brain / "fulcrum.yaml"
        config_path.write_text(
            f"brain:\n  root: {brain}\nprojects: {{}}\n", encoding="utf-8"
        )
        (instance / "config").symlink_to(config_path)
        context = resolve_instance(instance=str(instance), config=None)
        request = ParsedRequest(
            command=("reconcile",),
            arguments={},
            input={},
            actor=ActorContext(kind="controller"),
            instance=context,
            request_id=str(uuid.uuid4()),
            timeout=10,
            offline=True,
        )
        manager = ConfigurationManager(config_path)
        document, _ = manager.load()
        config = manager.effective(document)
        ledger = Ledger(brain)
        runtime = FakeLeadershipRuntime()
        try:
            first = asyncio.run(ensure_leadership(request, ledger, runtime, config))
            second = asyncio.run(ensure_leadership(request, ledger, runtime, config))
            control = ledger.show("fc-system")
            assert control is not None and control.fc
            self.assertEqual(len(runtime.created), 2)
            self.assertEqual(runtime.turns, [])
            self.assertEqual({row["role"] for row in first}, {"vizier", "marshal"})
            self.assertTrue(all(row["created"] is False for row in second))
            self.assertEqual(control.fc["owner"], control.fc["marshal_thread"])
            self.assertEqual(control.fc["instance_root"], str(instance))
            operations = ledger.list_records(kind="operation", limit=0)
            self.assertEqual(len(operations), 2)
            self.assertTrue(
                all((item.fc or {}).get("state") == "completed" for item in operations)
            )
            self.assertTrue(
                all(
                    (item.fc or {}).get("result", {}).get("turn_started") is False
                    for item in operations
                )
            )
        finally:
            subprocess.run(
                ["bd", "-C", str(brain), "dolt", "stop"],
                capture_output=True,
                check=False,
                timeout=20,
            )

    def test_native_intake_normalizes_when_project_is_proved_and_retains_conflict(
        self,
    ) -> None:
        self.ledger.run(
            (
                "create",
                "--id",
                "fc-native-proved",
                "--title",
                "Native proved intake",
                "--description",
                "Preserve this native request.",
                "--type",
                "task",
                "--priority",
                "2",
                "--assignee",
                "executor",
                "--labels",
                "project:toy",
            ),
            mutating=True,
        )
        self.ledger.run(
            (
                "create",
                "--id",
                "fc-native-unknown-project",
                "--title",
                "Native unknown project",
                "--description",
                "Do not guess its project.",
                "--type",
                "task",
                "--priority",
                "2",
            ),
            mutating=True,
        )
        results = normalize_native_intake(self.request(("reconcile",)), self.ledger)
        by_id = {row["bead_id"]: row for row in results}
        self.assertTrue(by_id["fc-native-proved"]["adopted"])
        self.assertEqual(by_id["fc-native-unknown-project"]["code"], "SCOPE_CONFLICT")
        proved = self.ledger.show("fc-native-proved")
        unknown = self.ledger.show("fc-native-unknown-project")
        assert proved is not None and proved.fc and unknown is not None
        self.assertEqual(proved.fc["owner"], self.marshal)
        self.assertEqual(proved.fc["requested_role"], "executor")
        self.assertIn("native_snapshot", proved.fc["origin"])
        self.assertIsNone(unknown.fc)

    def test_automatic_judgment_coalesces_and_never_turns_vizier(self) -> None:
        self.create_work(
            "fc-auto-decision",
            intake=None,
            priority=0,
        )
        runtime = FakeLeadershipRuntime()
        for role, thread_id in (("vizier", self.vizier), ("marshal", self.marshal)):
            runtime.tasks[thread_id] = TaskFacts(
                id=thread_id,
                title=LEADERSHIP_TITLES[role],
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
                observed_at="2026-09-14T17:00:00Z",
            )
        clock = ManualClock()
        supervisor = ControllerSupervisor(
            self.request(("reconcile",)),
            default_application(),
            clock=clock,
            runtime=runtime,  # type: ignore[arg-type]
        )
        first = asyncio.run(supervisor.run_once())
        clock.advance(1)
        second = asyncio.run(supervisor.run_once())
        clock.advance(1)
        third = asyncio.run(supervisor.run_once())
        recovery = self.create_work(
            "fc-recovery-during-grooming",
            intake={"benefit": "Known", "uncertainties": []},
            priority=0,
        )
        recovery_fc = dict(recovery.fc or {})
        recovery_fc["phase"] = "recovering"
        self.ledger.update_fc(recovery.id, recovery_fc)
        clock.advance(30)
        fourth = asyncio.run(supervisor.run_once())
        self.assertEqual(
            [
                row["reason"]
                for row in first.next_actions
                if row["kind"] == "marshal_decision"
            ],
            ["coalescing compatible judgment events"],
        )
        self.assertEqual(
            [
                row["reason"]
                for row in second.next_actions
                if row["kind"] == "marshal_decision"
            ],
            ["coalescing compatible judgment events"],
        )
        self.assertTrue(
            next(
                row for row in third.next_actions if row["kind"] == "marshal_decision"
            )["started"]
        )
        self.assertFalse(
            next(
                row for row in fourth.next_actions if row["kind"] == "marshal_decision"
            )["started"]
        )
        operations = [
            record
            for record in self.ledger.list_records(kind="operation", limit=0)
            if record.fc and record.fc.get("command") == "marshal.request"
        ]
        self.assertEqual(len(operations), 1)
        self.assertEqual(runtime.turns, [self.marshal])
        self.assertEqual(operations[0].fc["external"]["thread_id"], self.marshal)

        self.ledger.update_operation(
            operations[0].id,
            state="completed",
            step="fixture_grooming_decided",
            result={"fixture": "decision applied"},
        )
        work = self.ledger.show("fc-auto-decision")
        assert work is not None and work.fc
        work_fc = dict(work.fc)
        work_fc["phase"] = "working"
        self.ledger.update_fc(work.id, work_fc)
        marshal = runtime.tasks[self.marshal]
        runtime.tasks[self.marshal] = TaskFacts(
            id=marshal.id,
            title=marshal.title,
            cwd=marshal.cwd,
            project_id=marshal.project_id,
            workspace_roots=marshal.workspace_roots,
            archived=marshal.archived,
            exists=marshal.exists,
            loaded=marshal.loaded,
            runtime_status="idle",
            active_turn=None,
            last_turn=marshal.last_turn,
            pending_requests=(),
            observed_at="2026-09-14T17:00:32Z",
        )
        recovery_first = asyncio.run(supervisor.run_once())
        self.assertEqual(
            next(
                row
                for row in recovery_first.next_actions
                if row["kind"] == "marshal_decision"
            )["reason"],
            "coalescing compatible judgment events",
        )
        clock.advance(2)
        recovery_started = asyncio.run(supervisor.run_once())
        self.assertTrue(
            next(
                row
                for row in recovery_started.next_actions
                if row["kind"] == "marshal_decision"
            )["started"]
        )
        self.assertEqual(runtime.turns, [self.marshal, self.marshal])

        latest_recovery = self.ledger.show(recovery.id)
        assert latest_recovery is not None and latest_recovery.fc
        settled_recovery_fc = dict(latest_recovery.fc)
        settled_recovery_fc["phase"] = "working"
        self.ledger.update_fc(recovery.id, settled_recovery_fc)
        active_marshal = runtime.tasks[self.marshal]
        runtime.tasks[self.marshal] = TaskFacts(
            id=active_marshal.id,
            title=active_marshal.title,
            cwd=active_marshal.cwd,
            project_id=active_marshal.project_id,
            workspace_roots=active_marshal.workspace_roots,
            archived=active_marshal.archived,
            exists=active_marshal.exists,
            loaded=active_marshal.loaded,
            runtime_status="idle",
            active_turn=None,
            last_turn=active_marshal.last_turn,
            pending_requests=(),
            observed_at="2026-09-14T17:00:34Z",
        )
        routine = asyncio.run(supervisor.run_once())
        self.assertFalse(
            any(row["kind"] == "marshal_decision" for row in routine.next_actions)
        )
        self.assertEqual(runtime.turns, [self.marshal, self.marshal])

    def test_future_plan_cannot_be_activated_by_dispatch_or_human_bypass(
        self,
    ) -> None:
        work = self.create_work(
            "fc-future-plan",
            intake={"benefit": "Known", "uncertainties": []},
            dispatch={
                "role": "executor",
                "reason": "Marshal authorization cannot activate it.",
                "decision_operation": "fc-plan-dispatch",
                "human_bypass": False,
                "reservation": None,
            },
        )
        fc = dict(work.fc or {})
        fc["plan"] = {
            "activation": "future",
            "approval_operation": "fc-plan-approval",
            "activation_authorization": None,
        }
        self.ledger.update_fc(work.id, fc)
        for human in (False, True):
            result = AdmissionService().dispatch(
                self.request(
                    ("dispatch",),
                    arguments={
                        "bead": work.id,
                        **({"human": True} if human else {}),
                    },
                )
            )
            operation = self.ledger.show(str(result.operation_id))
            assert operation is not None and operation.fc
            self.assertEqual(operation.fc["error"]["code"], "ACTIVATION_REQUIRED")
            self.assertEqual(operation.fc["state"], "failed")

    def test_changed_scope_rejects_only_stale_decision_and_context_survives(
        self,
    ) -> None:
        stale = self.create_work(
            "fc-stale",
            intake={"benefit": "A", "uncertainties": []},
            priority=0,
        )
        valid = self.create_work(
            "fc-valid",
            intake={"benefit": "B", "uncertainties": []},
            priority=1,
        )
        clarify = self.create_work(
            "fc-clarify",
            intake={"benefit": "C", "uncertainties": []},
            priority=2,
        )
        turn = TurnFacts(
            id="marshal-turn",
            thread_id=self.marshal,
            state="inProgress",
            operation_id=None,
            completed=False,
            error=None,
            tools=(),
            usage=None,
            observed_at="2026-09-14T00:00:01Z",
        )
        service = LeadershipService()
        with patch("fulcrum.runtime_service._runtime_call", return_value=turn):
            requested = service.marshal_request(
                self.request(
                    ("marshal", "request"),
                    arguments={"kind": "dispatch"},
                    actor=ActorContext(kind="task", task_id=self.marshal),
                )
            )
        decision_operation = requested.operation_id
        assert decision_operation is not None
        operation = self.ledger.show(decision_operation)
        assert operation is not None and operation.fc
        comparisons = operation.fc["planned"]["comparison_facts"]
        stale_fc = dict(stale.fc or {})
        stale_fc["outcome"] = "Changed after the decision turn started."
        self.ledger.update_fc(stale.id, stale_fc)
        decisions = [
            {
                "bead_id": bead_id,
                "expected_ownership_operation": comparisons[bead_id][
                    "ownership_operation"
                ],
                "expected_phase": comparisons[bead_id]["phase"],
                "action": "defer",
                "reason": f"Retain current rationale for {bead_id}.",
                "reconsider_when": [{"event": "priority_changed", "subject": bead_id}],
            }
            for bead_id in (stale.id, valid.id)
        ]
        decisions.append(
            {
                "bead_id": clarify.id,
                "expected_ownership_operation": comparisons[clarify.id][
                    "ownership_operation"
                ],
                "expected_phase": comparisons[clarify.id]["phase"],
                "action": "clarify",
                "reason": "Repository evidence is needed before implementation.",
                "question": "Which existing interface owns this behavior?",
                "expected_result": "Return the interface and evidence on this bead.",
            }
        )
        payload = {
            "decision_operation": decision_operation,
            "decisions": decisions,
        }
        decided = service.marshal_decide(
            self.request(
                ("marshal", "decide"),
                payload=payload,
                actor=ActorContext(kind="task", task_id=self.marshal),
            )
        )
        receipt = self.ledger.show(str(decided.operation_id))
        assert receipt is not None and receipt.fc
        rows = receipt.fc["result"]["rows"]
        self.assertEqual(rows[0]["code"], "STALE_DECISION")
        self.assertTrue(rows[1]["applied"])
        self.assertTrue(rows[2]["applied"])
        clarified = self.ledger.show(clarify.id)
        assert clarified is not None and clarified.fc
        self.assertEqual(clarified.fc["dispatch"]["role"], "weaver")
        self.assertEqual(
            clarified.fc["clarification"]["question"],
            "Which existing interface owns this behavior?",
        )
        current = marshal_context(
            self.request(("context",), arguments={"role": "marshal"})
        ).result
        assert current is not None
        retained = {row["bead_id"]: row for row in current["current_rows"]}
        self.assertEqual(
            retained[valid.id]["waiting"]["reasons"][0]["decision_operation"],
            decision_operation,
        )
        valid_record = self.ledger.show(valid.id)
        assert valid_record is not None and valid_record.fc
        irrelevant = dict(valid_record.fc)
        irrelevant["summary"] = "An unrelated summary observation changed."
        self.ledger.update_fc(valid.id, irrelevant)
        quiet = service.marshal_brief(
            self.request(
                ("marshal", "brief"),
                arguments={"kind": "dispatch", "bead": valid.id},
            )
        ).result
        assert quiet is not None
        self.assertFalse(quiet["decision_required"])
        self.ledger.update_fc(valid.id, irrelevant, priority=0)
        triggered = service.marshal_brief(
            self.request(
                ("marshal", "brief"),
                arguments={"kind": "dispatch", "bead": valid.id},
            )
        ).result
        assert triggered is not None
        self.assertEqual([row["bead_id"] for row in triggered["rows"]], [valid.id])

    def test_four_reservations_queue_fifth_then_freed_capacity_needs_no_judgment(
        self,
    ) -> None:
        authorization = {
            "role": "executor",
            "reason": "Already authorized by Marshal.",
            "decision_operation": "fc-decision",
            "authorized_at": "2026-09-14T00:00:00Z",
            "human_bypass": False,
            "reservation": None,
        }
        for index in range(6):
            self.create_work(
                f"fc-capacity-{index}",
                intake={"benefit": "Known", "uncertainties": []},
                dispatch=dict(authorization),
            )
        service = AdmissionService()
        entered = 0
        entered_lock = threading.Lock()
        four_started = threading.Event()
        five_started = threading.Event()
        release = threading.Event()

        def blocked_entry(_service: Any, _request: ParsedRequest) -> CommandResult:
            nonlocal entered
            with entered_lock:
                entered += 1
                if entered == 4:
                    four_started.set()
                if entered == 5:
                    five_started.set()
            self.assertTrue(release.wait(120))
            return CommandResult.query({"fixture": "started"})

        def start(index: int) -> CommandResult:
            return service.dispatch(
                self.request(("dispatch",), arguments={"bead": f"fc-capacity-{index}"})
            )

        with patch("fulcrum.leadership.RoleService.enter", new=blocked_entry):
            with ThreadPoolExecutor(max_workers=5) as pool:
                futures = [pool.submit(start, index) for index in range(4)]
                self.assertTrue(four_started.wait(120))
                fifth = start(4)
                fifth_record = self.ledger.show(str(fifth.operation_id))
                assert fifth_record is not None and fifth_record.fc
                self.assertFalse(fifth_record.fc["result"]["started"])
                self.assertTrue(fifth_record.fc["result"]["queued"])
                self.assertEqual(entered, 4)
                human_future = pool.submit(
                    lambda: service.dispatch(
                        self.request(
                            ("dispatch",),
                            arguments={"bead": "fc-capacity-5", "human": True},
                        )
                    )
                )
                self.assertTrue(five_started.wait(120))
                release.set()
                for future in futures:
                    self.assertTrue(future.result(timeout=120).ok)
                self.assertTrue(human_future.result(timeout=120).ok)
        fifth_work = self.ledger.show("fc-capacity-4")
        assert fifth_work is not None and fifth_work.fc
        self.assertIsNotNone(fifth_work.fc["dispatch"])
        self.assertEqual(
            fifth_work.fc["waiting"]["reasons"][0]["reconsider_when"][0]["event"],
            "capacity_available",
        )
        bypassed = self.ledger.show("fc-capacity-5")
        assert bypassed is not None and bypassed.fc
        self.assertTrue(bypassed.fc["dispatch"]["human_bypass"])

    def test_unknown_activity_counts_human_bypass_is_distinct_and_idle_leader_does_not(
        self,
    ) -> None:
        self.ledger.create_record(
            record_id="fc-unknown-task",
            kind="task",
            title="Managed unknown task",
            description="Unknown activity must retain a slot.",
            owner="native-unknown",
            external_ref="fulcrum:thread:native-unknown",
            fc={
                "kind": "task",
                "owner": "native-unknown",
                "thread_id": "native-unknown",
                "role": "executor",
                "work_bead": None,
                "last_observed": None,
                "last_turn": None,
                "deleted_at": None,
            },
        )
        work = self.create_work(
            "fc-human-bypass",
            intake={"benefit": "Known", "uncertainties": []},
            dispatch={
                "role": "executor",
                "reason": "Human bypass",
                "decision_operation": "fc-human-decision",
                "human_bypass": True,
                "reservation": {
                    "operation_id": "fc-human-reservation",
                    "state": "unknown",
                    "human_bypass": True,
                },
            },
        )
        duplicate = self.create_work(
            "fc-single-slot",
            intake={"benefit": "Known", "uncertainties": []},
            dispatch={
                "role": "executor",
                "reason": "Transitioning from reservation to managed task.",
                "decision_operation": "fc-single-slot-decision",
                "human_bypass": False,
                "reservation": {
                    "operation_id": "fc-single-slot-reservation",
                    "state": "in_flight",
                    "human_bypass": False,
                },
            },
        )
        self.ledger.create_record(
            record_id="fc-single-slot-task",
            kind="task",
            title="Single-count task",
            description="The task and its settling reservation occupy one slot.",
            owner="native-single-slot",
            external_ref="fulcrum:thread:native-single-slot",
            fc={
                "kind": "task",
                "owner": "native-single-slot",
                "thread_id": "native-single-slot",
                "role": "executor",
                "work_bead": duplicate.id,
                "last_observed": {
                    "runtime_status": "inProgress",
                    "active_turn": "turn-single-slot",
                    "observed_at": "2026-09-14T00:00:00Z",
                },
                "last_turn": None,
                "deleted_at": None,
            },
        )
        manager = ConfigurationManager(self.config)
        document, _ = manager.load()
        config = manager.effective(document)
        capacity = capacity_snapshot(self.ledger, config)
        self.assertIn("native-unknown", capacity["unknown_managed_ids"])
        self.assertIn("fc-human-reservation", capacity["human_bypasses"])
        self.assertNotIn(self.marshal, capacity["active_managed_ids"])
        self.assertIn("native-single-slot", capacity["active_managed_ids"])
        self.assertNotIn(
            "fc-single-slot-reservation", capacity["pending_start_reservations"]
        )
        self.assertEqual(capacity["occupied"], 3)
        self.assertEqual(work.id, "fc-human-bypass")


if __name__ == "__main__":
    unittest.main()
