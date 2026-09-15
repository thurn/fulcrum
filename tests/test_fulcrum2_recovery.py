from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fulcrum.application import Application
from fulcrum.contracts import ActorContext, CommandState, FulcrumError, ParsedRequest
from fulcrum.delivery import DeliveryProviderError, SourceRef, WorkRef
from fulcrum.ledger import Ledger, random_record_id
from fulcrum.recovery_service import RecoveryService
from fulcrum.instance import resolve_instance
from fulcrum.runtime import ReleaseFacts, TaskFacts


class IdleRuntime:
    def __init__(self, facts: dict[str, TaskFacts]) -> None:
        self.facts = facts
        self.released: list[str] = []

    async def inspect_task(self, thread_id: str) -> TaskFacts:
        return self.facts[thread_id]

    async def release(self, thread_id: str) -> ReleaseFacts:
        self.released.append(thread_id)
        return ReleaseFacts(
            thread_id=thread_id,
            status="unsubscribed",
            active_terminals=(),
            observed_at="2026-09-14T18:00:00Z",
        )


class UncertainDelivery:
    async def cancel(self, source: SourceRef, handle: str) -> object:
        raise DeliveryProviderError(
            "cancellation response was lost",
            category="uncertain",
            possible_effect=True,
        )


class Fulcrum2RecoveryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name).resolve()
        cls.brain = cls.root / "brain"
        cls.instance_root = cls.root / "instance"
        cls.project = cls.root / "project"
        for path in (cls.brain, cls.instance_root, cls.project):
            path.mkdir()
        for path in (cls.brain, cls.project):
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
            cwd=cls.brain,
            capture_output=True,
            check=True,
            timeout=60,
        )
        cls.config = cls.brain / "fulcrum.yaml"
        cls.config.write_text(
            f"brain:\n  root: {cls.brain}\nprojects:\n  toy:\n    root: {cls.project}\n    enabled: true\n",
            encoding="utf-8",
        )
        (cls.instance_root / "config").symlink_to(cls.config)
        cls.instance = resolve_instance(instance=str(cls.instance_root), config=None)
        cls.ledger = Ledger(cls.brain)
        cls.application = Application()
        cls.marshal_thread = "marshal-fixture-thread"
        cls.control = cls.ledger.create_record(
            record_id="fc-system",
            kind="control",
            title="Fulcrum control",
            description="Fixture control",
            owner=cls.marshal_thread,
            fc={
                "kind": "control",
                "owner": cls.marshal_thread,
                "instance_root": str(cls.instance_root),
                "brain_root": str(cls.brain),
                "vizier_thread": None,
                "marshal_thread": cls.marshal_thread,
                "active_takeover": None,
                "last_transition": None,
            },
        )
        cls.marshal_task = cls.task(
            cls.marshal_thread,
            role="marshal",
            purpose="leadership",
            work_bead=None,
            runtime_status="idle",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        subprocess.run(
            ["bd", "-C", str(cls.brain), "dolt", "stop"],
            capture_output=True,
            check=False,
            timeout=20,
        )
        cls.temporary.cleanup()

    def setUp(self) -> None:
        control = self.ledger.show("fc-system")
        assert control is not None and control.fc
        control_fc = dict(control.fc)
        control_fc["active_takeover"] = None
        self.ledger.update_fc(control.id, control_fc)
        marshal = self.ledger.show(self.marshal_task)
        assert marshal is not None and marshal.fc
        marshal_fc = dict(marshal.fc)
        marshal_fc["role"] = "marshal"
        marshal_fc["purpose"] = "leadership"
        marshal_fc["recovery_operation"] = None
        self.ledger.update_fc(marshal.id, marshal_fc)

    @classmethod
    def task(
        cls,
        thread_id: str,
        *,
        role: str,
        purpose: str,
        work_bead: str | None,
        runtime_status: str,
        active_turn: str | None = None,
    ) -> str:
        identifier = random_record_id()
        cls.ledger.create_record(
            record_id=identifier,
            kind="task",
            title=f"{role} fixture",
            description="Managed task fixture",
            owner=thread_id,
            fc={
                "kind": "task",
                "owner": thread_id,
                "thread_id": thread_id,
                "work_bead": work_bead,
                "associated_beads": [work_bead] if work_bead else [],
                "role": role,
                "purpose": purpose,
                "ownership_operation": "fixture-ownership",
                "last_observed": {
                    "id": thread_id,
                    "runtime_status": runtime_status,
                    "active_turn": active_turn,
                    "last_turn": (
                        {"id": active_turn, "status": "inProgress"}
                        if active_turn
                        else None
                    ),
                    "observed_at": "2026-09-14T18:00:00Z",
                },
            },
            external_ref=f"fulcrum:thread:{thread_id}",
        )
        return identifier

    def work(self, title: str) -> str:
        identifier = random_record_id()
        self.ledger.create_record(
            record_id=identifier,
            kind="work",
            title=title,
            description=title,
            owner="writer-fixture",
            fc={
                "kind": "work",
                "owner": "writer-fixture",
                "role": "executor",
                "ownership_operation": "fixture-ownership",
                "phase": "working",
                "workflow_root": identifier,
                "project": "toy",
                "completion_cost": None,
                "waiting": None,
                "disposition": None,
                "next_action": "Implement fixture",
            },
        )
        return identifier

    def request(
        self,
        command: tuple[str, ...],
        *,
        arguments: dict[str, object] | None = None,
        payload: dict[str, object] | None = None,
        actor: ActorContext | None = None,
        thread_id: str | None = None,
        ownership_operation: str | None = None,
        request_id: str | None = None,
        runtime_submit: Any | None = None,
    ) -> ParsedRequest:
        return ParsedRequest(
            command=command,
            arguments=arguments or {},
            input=payload or {},
            actor=actor or ActorContext(kind="human"),
            instance=self.instance,
            request_id=request_id or str(uuid.uuid4()),
            thread_id=thread_id,
            ownership_operation=ownership_operation,
            offline=True,
            runtime_submit=runtime_submit,
        )

    def test_conflicting_writer_fences_before_transfer_then_reuses_receipt(
        self,
    ) -> None:
        root = self.work("Conflicting writer")
        writer_task = self.task(
            "writer-active-thread",
            role="executor",
            purpose="work",
            work_bead=root,
            runtime_status="active",
            active_turn="turn-active",
        )
        request_id = str(uuid.uuid4())
        request = self.request(
            ("recover", "takeover"),
            arguments={"scope": f"bead:{root}", "reason": "Writer is stuck"},
            request_id=request_id,
        )
        first = self.application.dispatch(request)
        self.assertEqual(first.state, CommandState.ACCEPTED)
        fenced = self.ledger.show(root)
        assert fenced is not None and fenced.fc
        self.assertEqual(fenced.fc["owner"], "writer-fixture")
        self.assertEqual(fenced.fc["recovery_fence"]["state"], "stopping")

        task = self.ledger.show(writer_task)
        assert task is not None and task.fc
        task_fc = dict(task.fc)
        task_fc["last_observed"] = {
            "id": "writer-active-thread",
            "runtime_status": "idle",
            "active_turn": None,
            "last_turn": {"id": "turn-active", "status": "interrupted"},
            "observed_at": "2026-09-14T18:01:00Z",
        }
        self.ledger.update_fc(task.id, task_fc)
        second = self.application.dispatch(request)
        self.assertEqual(second.state, CommandState.COMPLETED)
        self.assertEqual(second.operation_id, first.operation_id)
        acquired = self.ledger.show(root)
        assert acquired is not None and acquired.fc
        self.assertEqual(acquired.fc["owner"], self.marshal_thread)
        self.assertEqual(acquired.fc["role"], "justiciar")
        marshal = self.ledger.show(self.marshal_task)
        assert marshal is not None and marshal.fc
        self.assertEqual(marshal.fc["role"], "justiciar")

        stale = self.request(
            ("recover", "repair"),
            arguments={"scope": f"bead:{root}"},
            payload={"actions": []},
            actor=ActorContext(kind="task", task_id="stale-thread"),
            thread_id="stale-thread",
        )
        with self.assertRaises(FulcrumError) as raised:
            self.application.dispatch(stale)
        self.assertEqual(raised.exception.code, "RECOVERY_AUTHORITY_REQUIRED")

        conflicting = self.request(
            ("recover", "takeover"),
            arguments={"scope": f"bead:{root}", "reason": "Duplicate caller"},
        )
        with self.assertRaises(FulcrumError) as raised:
            self.application.dispatch(conflicting)
        self.assertEqual(raised.exception.code, "RECOVERY_SCOPE_CONFLICT")

    def test_typed_repairs_reject_before_effect_retain_failure_and_close_reduced_scope(
        self,
    ) -> None:
        root = self.work("Typed repair")
        takeover = self.application.dispatch(
            self.request(
                ("recover", "takeover"),
                arguments={"scope": f"bead:{root}", "reason": "Typed repair fixture"},
            )
        )
        self.assertEqual(takeover.state, CommandState.COMPLETED)
        operation_count = len(self.ledger.list_records(kind="operation", limit=0))
        invalid = self.request(
            ("recover", "repair"),
            arguments={"scope": f"bead:{root}"},
            payload={
                "actions": [
                    {
                        "action": "invented_action",
                        "target": root,
                        "arguments": {},
                        "reason": "Should never execute",
                    }
                ]
            },
        )
        with self.assertRaises(FulcrumError) as raised:
            self.application.dispatch(invalid)
        self.assertEqual(raised.exception.code, "INVALID_REPAIR")
        self.assertEqual(
            len(self.ledger.list_records(kind="operation", limit=0)),
            operation_count,
        )

        denied = self.application.dispatch(
            self.request(
                ("recover", "repair"),
                arguments={"scope": f"bead:{root}"},
                payload={
                    "actions": [
                        {
                            "action": "quarantine",
                            "target": str(self.config),
                            "arguments": {},
                            "reason": "Attempt an unauthorized YAML mutation",
                        }
                    ]
                },
            )
        )
        self.assertEqual(denied.state, CommandState.FAILED)
        self.assertTrue(self.config.exists())
        failed_control = self.ledger.show("fc-system")
        assert failed_control is not None and failed_control.fc
        self.assertEqual(failed_control.fc["active_takeover"]["state"], "failed")

        bad_git = self.application.dispatch(
            self.request(
                ("recover", "repair"),
                arguments={"scope": f"bead:{root}"},
                payload={
                    "actions": [
                        {
                            "action": "git",
                            "target": str(self.project),
                            "arguments": {"argv": ["not-a-real-subcommand"]},
                            "reason": "Exercise failed exact repair",
                        }
                    ]
                },
            )
        )
        self.assertEqual(bad_git.state, CommandState.FAILED)
        self.assertEqual(
            (self.ledger.show("fc-system").fc or {})["active_takeover"]["state"],
            "failed",
        )

        source = SourceRef(
            work=WorkRef(
                bead_id=root,
                project_id="toy",
                project_root=str(self.project),
                repository_id="toy",
                intended_path=str(self.project),
                branch="codex/recovery",
                integration_branch="main",
                operation_id="workspace-fixture",
            ),
            oid="source-fixture",
        )
        with (
            patch(
                "fulcrum.recovery_service.delivery_context",
                return_value=(
                    self.ledger,
                    self.ledger.show(root),
                    {},
                    UncertainDelivery(),
                ),
            ),
            patch(
                "fulcrum.recovery_service._retained_delivery_source",
                return_value=(source, "provider-fixture"),
            ),
        ):
            uncertain = self.application.dispatch(
                self.request(
                    ("recover", "repair"),
                    arguments={"scope": f"bead:{root}"},
                    payload={
                        "actions": [
                            {
                                "action": "cancel_delivery",
                                "target": root,
                                "arguments": {
                                    "source_oid": "source-fixture",
                                    "provider_handle": "provider-fixture",
                                },
                                "reason": "Cancel an uncertain provider submission",
                            }
                        ]
                    },
                )
            )
        self.assertEqual(uncertain.state, CommandState.UNCERTAIN)
        self.assertEqual(
            (self.ledger.show("fc-system").fc or {})["active_takeover"]["state"],
            "failed",
        )

        repaired = self.application.dispatch(
            self.request(
                ("recover", "repair"),
                arguments={"scope": f"bead:{root}"},
                payload={
                    "actions": [
                        {
                            "action": "set_disposition",
                            "target": root,
                            "arguments": {
                                "outcome": "reduced_scope",
                                "summary": "Recovered the observable safe subset",
                                "new_scope": {"outcome": "Safe subset only"},
                                "waived_requirements": [
                                    "Deferred unavailable integration"
                                ],
                                "known_defects": ["Integration remains unavailable"],
                                "evidence": ["git status clean"],
                            },
                            "reason": "Close only the verified reduced scope",
                        }
                    ]
                },
            )
        )
        self.assertEqual(repaired.state, CommandState.COMPLETED)
        closed = self.ledger.show(root)
        assert closed is not None and closed.fc
        self.assertEqual(closed.status, "closed")
        self.assertEqual(closed.fc["disposition"]["outcome"], "reduced_scope")
        self.assertEqual(closed.fc["completion_cost"]["state"], "finalized")

        released = self.application.dispatch(
            self.request(
                ("recover", "release"),
                arguments={
                    "scope": f"bead:{root}",
                    "summary": "Reduced-scope result and retained defect reconciled",
                },
            )
        )
        self.assertEqual(released.state, CommandState.COMPLETED)
        restored = self.ledger.show(self.marshal_task)
        assert restored is not None and restored.fc
        self.assertEqual(restored.fc["role"], "marshal")
        self.assertIsNone((self.ledger.show("fc-system").fc or {})["active_takeover"])

    def test_full_slots_release_idle_tasks_before_marshal_transition(self) -> None:
        roots: list[str] = []
        facts: dict[str, TaskFacts] = {}
        worker_threads: list[str] = []
        for index in range(4):
            root = self.work(f"Occupied slot {index}")
            thread_id = f"idle-slot-{uuid.uuid4()}"
            self.task(
                thread_id,
                role="executor",
                purpose="work",
                work_bead=root,
                runtime_status="idle",
            )
            roots.append(root)
            worker_threads.append(thread_id)
            facts[thread_id] = TaskFacts(
                id=thread_id,
                title="idle worker",
                cwd=str(self.project),
                project_id=None,
                workspace_roots=(str(self.project),),
                archived=False,
                exists=True,
                loaded=True,
                runtime_status="idle",
                active_turn=None,
                last_turn={"id": f"turn-{index}", "status": "completed"},
                pending_requests=(),
                observed_at="2026-09-14T18:00:00Z",
            )
        runtime = IdleRuntime(facts)

        def submit(action: Any) -> Any:
            return asyncio.run(action(runtime))

        scope = "beads:" + ",".join(roots)
        takeover = self.application.dispatch(
            self.request(
                ("recover", "takeover"),
                arguments={"scope": scope, "reason": "All four slots are occupied"},
                runtime_submit=submit,
            )
        )
        self.assertEqual(takeover.state, CommandState.COMPLETED)
        self.assertEqual(set(runtime.released), set(worker_threads))
        marshal = self.ledger.show(self.marshal_task)
        assert marshal is not None and marshal.fc
        self.assertEqual(marshal.fc["role"], "justiciar")
        released = self.application.dispatch(
            self.request(
                ("recover", "release"),
                arguments={"scope": scope, "summary": "Idle slots were released"},
            )
        )
        self.assertEqual(released.state, CommandState.COMPLETED)

    def test_human_resolution_preserves_other_reason_and_installed_inspection(
        self,
    ) -> None:
        root = self.work("Human intervention")
        work = self.ledger.show(root)
        assert work is not None and work.fc
        fc = dict(work.fc)
        fc["owner"] = "HUMAN"
        fc["role"] = None
        fc["phase"] = "human"
        fc["waiting"] = {
            "reasons": [
                {
                    "id": "human:first",
                    "kind": "human",
                    "reason": "Choose the supported environment",
                    "required_action": "Name the environment",
                },
                {
                    "id": "human:second",
                    "kind": "human",
                    "reason": "Provide external credential",
                    "required_action": "Install the credential",
                },
            ]
        }
        self.ledger.update_fc(root, fc, assignee="HUMAN", status="blocked")
        listed = self.application.dispatch(
            self.request(("human", "list"), request_id=None)
        )
        self.assertIn(root, [item["bead_id"] for item in listed.result["items"]])
        resolved = self.application.dispatch(
            self.request(
                ("human", "resolve"),
                arguments={"id": root},
                payload={
                    "reason_id": "human:first",
                    "answer": "Use the toy fixture environment",
                    "scope_change": {"summary": "Toy environment selected"},
                    "resume_role": "executor",
                },
            )
        )
        self.assertEqual(resolved.state, CommandState.COMPLETED)
        current = self.ledger.show(root)
        assert current is not None and current.fc
        self.assertEqual(current.fc["owner"], self.marshal_thread)
        self.assertEqual(current.fc["requested_role"], "executor")
        self.assertEqual(
            [item["id"] for item in current.fc["waiting"]["reasons"]],
            ["human:second"],
        )

        executable = Path(os.sys.executable).with_name("fulcrum")
        inspected = subprocess.run(
            [
                str(executable),
                "recover",
                "inspect",
                "--scope",
                f"bead:{root}",
                "--instance",
                str(self.instance_root),
                "--json",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        self.assertEqual(inspected.returncode, 0, inspected.stderr + inspected.stdout)
        envelope = json.loads(inspected.stdout)
        self.assertEqual(envelope["result"]["work"][0]["id"], root)

    def test_inspect_degrades_without_ledger_and_justiciar_finish_is_scoped(
        self,
    ) -> None:
        service = RecoveryService(self.application)
        with patch(
            "fulcrum.recovery_service._ledger",
            side_effect=FulcrumError(
                "LEDGER_UNAVAILABLE", "fixture outage", exit_code=4
            ),
        ):
            result = service.inspect(
                self.request(
                    ("recover", "inspect"),
                    arguments={"scope": "bead:fc-missing"},
                    request_id=None,
                )
            )
        self.assertEqual(result.state, CommandState.DEGRADED)
        self.assertFalse(result.result["durable_receipt"])

        root = self.work("Justiciar finish")
        takeover = self.application.dispatch(
            self.request(
                ("recover", "takeover"),
                arguments={"scope": f"bead:{root}", "reason": "Repair completion"},
            )
        )
        self.assertEqual(takeover.state, CommandState.COMPLETED)
        finished = self.application.dispatch(
            self.request(
                ("finish",),
                arguments={"bead": root, "outcome": "repaired"},
                payload={
                    "summary": "Scoped repair is complete",
                    "changes": ["Reconciled retained metadata"],
                    "waived_requirements": [],
                    "known_defects": [],
                    "evidence": ["Beads state observed"],
                },
                actor=ActorContext(kind="task", task_id=self.marshal_thread),
                thread_id=self.marshal_thread,
                ownership_operation=takeover.operation_id,
            )
        )
        self.assertEqual(finished.state, CommandState.COMPLETED)
        closed = self.ledger.show(root)
        assert closed is not None and closed.fc
        self.assertEqual(closed.fc["disposition"]["outcome"], "repaired")
        self.assertIsNotNone(closed.fc["completion_cost"]["summary_bead"])

    def test_standalone_investigation_files_reports_and_closes(self) -> None:
        root = self.work("Standalone Mason investigation")
        thread_id = "mason-standalone-thread"
        work = self.ledger.show(root)
        assert work is not None and work.fc
        work_fc = dict(work.fc)
        work_fc.update(
            {
                "owner": thread_id,
                "role": "mason",
                "requested_role": "mason",
                "ownership_operation": "fixture-ownership",
                "phase": "working",
            }
        )
        self.ledger.update_fc(root, work_fc, assignee=thread_id)
        self.task(
            thread_id,
            role="mason",
            purpose="work",
            work_bead=root,
            runtime_status="idle",
        )

        finished = self.application.dispatch(
            self.request(
                ("finish",),
                arguments={"bead": root, "outcome": "findings"},
                payload={
                    "summary": "One duplicated responsibility was observed.",
                    "findings": [
                        {
                            "title": "Consolidate duplicate ordering helpers",
                            "problem": "Two modules sort the same integer input.",
                            "observed_evidence": "ordering.py and legacy_ordering.py",
                            "required_change": "Retain one ordering responsibility.",
                            "acceptance_checks": [
                                "Only one production ordering helper remains."
                            ],
                        }
                    ],
                },
                actor=ActorContext(kind="task", task_id=thread_id),
                thread_id=thread_id,
                ownership_operation="fixture-ownership",
            )
        )

        self.assertEqual(finished.state, CommandState.COMPLETED)
        closed = self.ledger.show(root)
        assert closed is not None and closed.fc
        self.assertEqual(closed.status, "closed")
        self.assertEqual(closed.fc["disposition"]["outcome"], "findings")
        reports = closed.fc["investigations"][-1]["reports"]
        self.assertEqual(len(reports), 1)
        report = self.ledger.show(reports[0])
        assert report is not None and report.fc
        self.assertEqual(report.fc["caused_by"], root)

    def test_leadership_completion_closes_request_not_standing_task(self) -> None:
        root = self.work("Explicit Vizier request")
        thread_id = "vizier-standing-thread"
        work = self.ledger.show(root)
        assert work is not None and work.fc
        work_fc = dict(work.fc)
        work_fc.update(
            {
                "owner": thread_id,
                "role": "vizier",
                "requested_role": "vizier",
                "ownership_operation": "fixture-ownership",
                "phase": "working",
            }
        )
        self.ledger.update_fc(root, work_fc, assignee=thread_id)
        task_id = self.task(
            thread_id,
            role="vizier",
            purpose="leadership",
            work_bead=root,
            runtime_status="idle",
        )

        finished = self.application.dispatch(
            self.request(
                ("finish",),
                arguments={"bead": root, "outcome": "completed"},
                payload={"summary": "The requested fixture policy is retained."},
                actor=ActorContext(kind="task", task_id=thread_id),
                thread_id=thread_id,
                ownership_operation="fixture-ownership",
            )
        )

        self.assertEqual(finished.state, CommandState.COMPLETED)
        closed = self.ledger.show(root)
        task = self.ledger.show(task_id)
        assert closed is not None and closed.fc and task is not None and task.fc
        self.assertEqual(closed.fc["disposition"]["outcome"], "answered")
        self.assertEqual(task.fc["finish_operation"], finished.operation_id)
        self.assertNotEqual(task.status, "closed")


if __name__ == "__main__":
    unittest.main()
