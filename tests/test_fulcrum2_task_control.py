from __future__ import annotations

import asyncio
import subprocess
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from fulcrum.configuration import ConfigurationManager
from fulcrum.contracts import ActorContext, FulcrumError, ParsedRequest
from fulcrum.instance import resolve_instance
from fulcrum.leadership import capacity_snapshot
from fulcrum.ledger import Ledger
from fulcrum.reviews import ReviewService
from fulcrum.runtime import (
    AppServerError,
    AppServerRuntime,
    ReleaseFacts,
    RuntimeCapabilities,
    TaskFacts,
    TurnFacts,
)
from fulcrum.runtime_service import TaskService


def facts(
    thread_id: str,
    *,
    status: str = "idle",
    active_turn: str | None = None,
    last_turn: dict[str, Any] | None = None,
    pending: tuple[dict[str, Any], ...] = (),
    cwd: str = "/work",
) -> TaskFacts:
    return TaskFacts(
        id=thread_id,
        title=f"Task {thread_id}",
        cwd=cwd,
        project_id="project-toy",
        workspace_roots=(cwd,),
        archived=False,
        exists=True,
        loaded=True,
        runtime_status=status,
        active_turn=active_turn,
        last_turn=last_turn,
        pending_requests=pending,
        observed_at="2026-09-14T19:00:00Z",
    )


class FakeTaskRuntime:
    def __init__(self) -> None:
        self.tasks: dict[str, TaskFacts] = {}
        self.creation_paths: dict[str, str] = {}
        self.turns: dict[tuple[str, str], TurnFacts] = {}
        self.terminal_rows: dict[str, list[dict[str, Any]]] = {}
        self.output_rows: dict[str, list[dict[str, Any]]] = {}
        self.responses: list[tuple[str, str, dict[str, Any]]] = []
        self.terminated: list[str] = []
        self.unsupported_termination = False
        self.response_loss = False
        self.start_response_loss = False

    async def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            available=True,
            endpoint="fake://runtime",
            methods=(),
            models={"luna": ("high",)},
        )

    async def find_tasks(self, creation_cwd: str) -> list[TaskFacts]:
        return [
            self.tasks[thread_id]
            for thread_id, path in self.creation_paths.items()
            if path == creation_cwd
        ]

    async def create_task(self, spec: Any) -> TaskFacts:
        thread_id = f"review-thread-{len(self.tasks) + 1}"
        created = facts(thread_id, cwd=spec.creation_cwd)
        self.tasks[thread_id] = created
        self.creation_paths[thread_id] = spec.creation_cwd
        return created

    async def configure_task(self, thread_id: str, spec: Any) -> TaskFacts:
        configured = facts(thread_id, cwd=spec.cwd)
        self.tasks[thread_id] = configured
        return configured

    async def inspect_task(self, thread_id: str) -> TaskFacts:
        return self.tasks[thread_id]

    async def find_turn(self, thread_id: str, operation_id: str) -> TurnFacts | None:
        return self.turns.get((thread_id, operation_id))

    async def inspect_turn(self, thread_id: str, turn_id: str) -> TurnFacts | None:
        return next(
            (
                turn
                for (candidate, _), turn in self.turns.items()
                if candidate == thread_id and turn.id == turn_id
            ),
            None,
        )

    async def start_turn(self, thread_id: str, turn_input: Any) -> TurnFacts:
        turn = TurnFacts(
            id=f"turn-{len(self.turns) + 1}",
            thread_id=thread_id,
            state="inProgress",
            operation_id=turn_input.operation_id,
            completed=False,
            error=None,
            tools=(),
            usage=None,
            observed_at="2026-09-14T19:00:01Z",
        )
        self.turns[(thread_id, turn_input.operation_id)] = turn
        current = self.tasks[thread_id]
        self.tasks[thread_id] = TaskFacts(
            **{
                **current.__dict__,
                "runtime_status": "inProgress",
                "active_turn": turn.id,
                "last_turn": turn.to_dict(),
            }
        )
        if self.start_response_loss:
            self.start_response_loss = False
            raise AppServerError(
                "turn response connection lost",
                category="uncertain",
                uncertain=True,
            )
        return turn

    async def output(self, thread_id: str, **arguments: Any) -> dict[str, Any]:
        rows = self.output_rows.get(thread_id, [])
        limit = int(arguments["limit"])
        selected = rows if limit == 0 else rows[:limit]
        return {
            "thread_id": thread_id,
            "turn_id": arguments.get("turn_id"),
            "items": selected,
            "next_cursor": None,
            "observed_at": "2026-09-14T19:00:02Z",
            "gaps": [],
            "bytes": 100,
        }

    async def terminals(self, thread_id: str, **arguments: Any) -> dict[str, Any]:
        return {
            "thread_id": thread_id,
            "items": list(self.terminal_rows.get(thread_id, [])),
            "next_cursor": None,
            "observed_at": "2026-09-14T19:00:02Z",
            "gaps": [],
        }

    async def terminate_terminal(
        self, thread_id: str, process_id: str
    ) -> dict[str, Any]:
        self.terminated.append(process_id)
        if self.unsupported_termination:
            raise AppServerError("precise stop unsupported", category="unsupported")
        self.terminal_rows[thread_id] = [
            row
            for row in self.terminal_rows.get(thread_id, [])
            if row.get("terminal_id") != process_id
        ]
        return {
            "thread_id": thread_id,
            "terminal_id": process_id,
            "terminated": True,
            "still_running": False,
            "observed_at": "2026-09-14T19:00:03Z",
        }

    async def respond(
        self, thread_id: str, request_id: str, response: dict[str, Any]
    ) -> None:
        self.responses.append((thread_id, request_id, response))
        current = self.tasks[thread_id]
        retained = tuple(
            item
            for item in current.pending_requests
            if str(item.get("request_id")) != request_id
        )
        self.tasks[thread_id] = TaskFacts(
            **{**current.__dict__, "pending_requests": retained}
        )
        if self.response_loss:
            self.response_loss = False
            raise AppServerError(
                "response connection lost", category="uncertain", uncertain=True
            )

    async def release(self, thread_id: str) -> ReleaseFacts:
        return ReleaseFacts(
            thread_id=thread_id,
            status="unsubscribed",
            active_terminals=(),
            observed_at="2026-09-14T19:00:04Z",
        )

    async def archive(self, thread_id: str) -> TaskFacts:
        current = self.tasks[thread_id]
        archived = TaskFacts(**{**current.__dict__, "archived": True})
        self.tasks[thread_id] = archived
        return archived

    async def unarchive(self, thread_id: str) -> TaskFacts:
        current = self.tasks[thread_id]
        restored = TaskFacts(**{**current.__dict__, "archived": False})
        self.tasks[thread_id] = restored
        return restored

    async def delete(self, thread_id: str) -> TaskFacts:
        current = self.tasks[thread_id]
        deleted = TaskFacts(
            **{
                **current.__dict__,
                "exists": False,
                "loaded": False,
                "runtime_status": "deleted",
            }
        )
        self.tasks[thread_id] = deleted
        return deleted


class Fulcrum2TaskControlReviewTest(unittest.TestCase):
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
                    "models:",
                    "  weaver:",
                    "    model: luna",
                    "    effort: high",
                    "projects:",
                    "  toy:",
                    f"    root: {self.project}",
                    "    enabled: true",
                    "    codex_project_id: project-toy",
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
        self.runtime = FakeTaskRuntime()

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
        thread_id: str | None = None,
        ownership: str | None = None,
    ) -> ParsedRequest:
        return ParsedRequest(
            command=command,
            arguments=arguments or {},
            input=payload or {},
            actor=actor or ActorContext(kind="human"),
            instance=self.context,
            request_id=str(uuid.uuid4()),
            project="toy",
            thread_id=thread_id,
            ownership_operation=ownership,
            timeout=10,
            offline=True,
        )

    def call(self, handler: Any, request: ParsedRequest) -> Any:
        async def run() -> Any:
            loop = asyncio.get_running_loop()

            def submit(action: Any) -> Any:
                return asyncio.run_coroutine_threadsafe(
                    action(self.runtime), loop
                ).result(timeout=15)

            return await asyncio.to_thread(
                handler, replace(request, runtime_submit=submit)
            )

        return asyncio.run(run())

    def create_root(self) -> Any:
        draft = {
            "text": "Implement the candidate architecture.",
            "tasks": [
                {
                    "key": "implementation",
                    "title": "Implement the candidate",
                    "outcome": "Implement it.",
                    "acceptance": ["The behavior passes."],
                    "depends_on": [],
                }
            ],
            "summary": "One implementation task.",
            "publication": None,
            "validation": {"summary": "Run focused tests.", "checks": []},
        }
        return self.ledger.create_record(
            record_id="fc-plan-root",
            kind="work",
            title="Plan root",
            description="ORIGINAL REQUIREMENT SENTINEL",
            acceptance="Original acceptance sentinel.",
            owner="author-thread",
            fc={
                "kind": "work",
                "project": "toy",
                "owner": "author-thread",
                "role": "weaver",
                "ownership_operation": "author-acquisition",
                "phase": "working",
                "outcome": "ORIGINAL REQUIREMENT SENTINEL",
                "acceptance": ["Original acceptance sentinel."],
                "intake": {"benefit": "Original benefit", "uncertainties": []},
                "context": ["Original discussion sentinel."],
                "plan": {"draft": draft, "reviews": {}},
            },
        )

    def create_managed_task(self) -> Any:
        work = self.ledger.create_record(
            record_id="fc-controlled-work",
            kind="work",
            title="Controlled work",
            description="Control the native task.",
            owner="native-controlled",
            fc={
                "kind": "work",
                "project": "toy",
                "owner": "native-controlled",
                "ownership_operation": "controlled-acquisition",
                "phase": "working",
            },
        )
        task = self.ledger.create_record(
            record_id="fc-controlled-task",
            kind="task",
            title="Controlled task",
            description="Native controlled task.",
            owner="native-controlled",
            external_ref="fulcrum:thread:native-controlled",
            fc={
                "kind": "task",
                "owner": "native-controlled",
                "thread_id": "native-controlled",
                "role": "executor",
                "project": "toy",
                "work_bead": work.id,
                "ownership_operation": "controlled-acquisition",
                "creation_operation": "controlled-create",
                "model": "luna",
                "effort": "high",
                "last_observed": None,
                "deleted_at": None,
            },
        )
        return work, task

    def test_independent_reviews_preserve_author_and_reject_stale_or_wrong_task(
        self,
    ) -> None:
        root = self.create_root()
        service = ReviewService()
        starts = []
        for perspective in ("cold_reader", "requirements"):
            starts.append(
                self.call(
                    service.start,
                    self.request(
                        ("plan", "review", "start"),
                        arguments={"bead": root.id, "perspective": perspective},
                    ),
                )
            )
        with self.assertRaises(FulcrumError) as duplicate:
            self.call(
                service.start,
                self.request(
                    ("plan", "review", "start"),
                    arguments={"bead": root.id, "perspective": "cold_reader"},
                ),
            )
        self.assertEqual(duplicate.exception.code, "REVIEW_IN_PROGRESS")
        self.assertEqual(len(self.runtime.tasks), 2)
        current = self.ledger.show(root.id)
        assert current is not None and current.fc
        self.assertEqual(current.fc["owner"], "author-thread")
        reviews = current.fc["plan"]["reviews"]
        self.assertNotEqual(
            reviews["cold_reader"]["thread_id"],
            reviews["requirements"]["thread_id"],
        )
        cold_operation = self.ledger.show(str(starts[0].operation_id))
        requirements_operation = self.ledger.show(str(starts[1].operation_id))
        assert cold_operation is not None and cold_operation.fc
        assert requirements_operation is not None and requirements_operation.fc
        cold_input = cold_operation.fc["planned"]["review_input"]
        requirements_input = requirements_operation.fc["planned"]["review_input"]
        self.assertNotIn("ORIGINAL REQUIREMENT SENTINEL", cold_input)
        self.assertIn("ORIGINAL REQUIREMENT SENTINEL", requirements_input)
        manager = ConfigurationManager(self.config)
        document, _ = manager.load()
        self.assertEqual(
            capacity_snapshot(self.ledger, manager.effective(document))["occupied"],
            2,
        )

        cold_task = self.ledger.show(reviews["cold_reader"]["task_record_id"])
        assert cold_task is not None and cold_task.fc
        with self.assertRaises(FulcrumError) as wrong:
            service.finish(
                self.request(
                    ("plan", "review", "finish"),
                    arguments={"task": cold_task.id},
                    payload={
                        "review_operation": starts[0].operation_id,
                        "summary": "Wrong task.",
                        "findings": [],
                    },
                    actor=ActorContext(kind="task", task_id="other-thread"),
                    thread_id="other-thread",
                )
            )
        self.assertEqual(wrong.exception.code, "OWNERSHIP_CONFLICT")
        finished = service.finish(
            self.request(
                ("plan", "review", "finish"),
                arguments={"task": cold_task.id},
                payload={
                    "review_operation": starts[0].operation_id,
                    "summary": "One concrete issue.",
                    "findings": [
                        {
                            "problem": "Acceptance omits failure behavior.",
                            "required_change": "Add one failure case.",
                            "evidence": "The validation checks are empty.",
                        }
                    ],
                },
                actor=ActorContext(kind="task", task_id=str(cold_task.fc["thread_id"])),
                thread_id=str(cold_task.fc["thread_id"]),
            )
        )
        self.assertTrue(finished.ok)
        self.assertEqual(self.ledger.show(root.id).fc["owner"], "author-thread")

        changed = self.ledger.show(root.id)
        assert changed is not None and changed.fc
        changed_fc = dict(changed.fc)
        changed_plan = dict(changed_fc["plan"])
        changed_draft = dict(changed_plan["draft"])
        changed_draft["text"] = "Substantively changed candidate."
        changed_plan["draft"] = changed_draft
        changed_fc["plan"] = changed_plan
        self.ledger.update_fc(changed.id, changed_fc)
        requirements_task = self.ledger.show(reviews["requirements"]["task_record_id"])
        assert requirements_task is not None and requirements_task.fc
        stale_request = self.request(
            ("plan", "review", "finish"),
            arguments={"task": requirements_task.id},
            payload={
                "review_operation": starts[1].operation_id,
                "summary": "Historical findings.",
                "findings": [],
            },
            actor=ActorContext(
                kind="task", task_id=str(requirements_task.fc["thread_id"])
            ),
            thread_id=str(requirements_task.fc["thread_id"]),
        )
        with self.assertRaises(FulcrumError) as stale:
            service.finish(stale_request)
        self.assertEqual(stale.exception.code, "STALE_REVIEW")
        stale_receipt = self.ledger.show(str(stale.exception.operation_id))
        assert stale_receipt is not None and stale_receipt.fc
        self.assertEqual(stale_receipt.fc["step"], "stale_review_retained")

    def test_output_wait_requests_response_and_response_loss(self) -> None:
        _, task = self.create_managed_task()
        pending = {
            "request_id": "request-1",
            "method": "item/tool/requestUserInput",
            "params": {"threadId": "native-controlled", "questions": []},
            "observed_at": "2026-09-14T19:00:00Z",
        }
        self.runtime.tasks["native-controlled"] = facts(
            "native-controlled",
            last_turn={"id": "turn-done", "status": "completed"},
            pending=(pending,),
            cwd=str(self.project),
        )
        self.runtime.output_rows["native-controlled"] = [
            {"turnId": "turn-done", "item": {"type": "agentMessage", "text": "done"}}
        ]
        self.runtime.turns[("native-controlled", "completed-fixture")] = TurnFacts(
            id="turn-done",
            thread_id="native-controlled",
            state="completed",
            operation_id="completed-fixture",
            completed=True,
            error=None,
            tools=(),
            usage=None,
            observed_at="2026-09-14T19:00:02Z",
        )
        service = TaskService()
        output = self.call(
            service.output,
            self.request(
                ("task", "output"),
                arguments={
                    "id": task.id,
                    "turn_id": "turn-done",
                    "limit": 20,
                    "max_bytes": 262144,
                },
            ),
        ).result
        assert output is not None
        self.assertEqual(output["items"][0]["item"]["text"], "done")
        waited = self.call(
            service.wait,
            self.request(
                ("task", "wait"),
                arguments={"id": task.id, "turn_id": "turn-done", "until": "terminal"},
            ),
        ).result
        assert waited is not None
        self.assertTrue(waited["satisfied"])
        requests = self.call(
            service.requests,
            self.request(("task", "requests"), arguments={"id": task.id}),
        ).result
        assert requests is not None
        self.assertEqual(requests["items"][0]["method"], "item/tool/requestUserInput")
        with self.assertRaises(FulcrumError) as invalid:
            self.call(
                service.respond,
                self.request(
                    ("task", "respond"),
                    arguments={"id": task.id, "request": "request-1"},
                    payload={"response": {"decision": "accept"}},
                ),
            )
        self.assertEqual(invalid.exception.code, "INVALID_NATIVE_RESPONSE")
        responded = self.call(
            service.respond,
            self.request(
                ("task", "respond"),
                arguments={"id": task.id, "request": "request-1"},
                payload={"response": {"answers": {"scope": {"answers": ["yes"]}}}},
            ),
        )
        self.assertTrue(responded.ok)
        self.assertEqual(self.runtime.responses[0][1], "request-1")

        with self.assertRaises(FulcrumError) as lost:
            self.call(
                service.respond,
                self.request(
                    ("task", "respond"),
                    arguments={"id": task.id, "request": "request-1"},
                    payload={"response": {"answers": {"scope": {"answers": ["yes"]}}}},
                ),
            )
        self.assertEqual(lost.exception.code, "REQUEST_IDENTITY_LOST")
        current = self.runtime.tasks["native-controlled"]
        self.runtime.tasks["native-controlled"] = TaskFacts(
            **{
                **current.__dict__,
                "pending_requests": ({**pending, "request_id": "request-2"},),
            }
        )
        self.runtime.response_loss = True
        uncertain = self.call(
            service.respond,
            self.request(
                ("task", "respond"),
                arguments={"id": task.id, "request": "request-2"},
                payload={"response": {"answers": {"scope": {"answers": ["yes"]}}}},
            ),
        )
        self.assertEqual(uncertain.state.value, "uncertain")

        current = self.runtime.tasks["native-controlled"]
        self.runtime.tasks["native-controlled"] = TaskFacts(
            **{
                **current.__dict__,
                "pending_requests": (),
                "active_turn": None,
                "runtime_status": "idle",
            }
        )
        self.runtime.start_response_loss = True
        sent = self.call(
            service.send,
            self.request(
                ("task", "send"),
                arguments={"id": task.id},
                payload={"text": "Continue with the retained task."},
            ),
        )
        self.assertTrue(sent.ok)
        self.assertEqual(
            len(
                [
                    turn
                    for (thread_id, _), turn in self.runtime.turns.items()
                    if thread_id == "native-controlled"
                    and turn.operation_id == sent.operation_id
                ]
            ),
            1,
        )

    def test_precise_terminal_stop_never_falls_back_and_lifecycle_is_guarded(
        self,
    ) -> None:
        work, task = self.create_managed_task()
        self.runtime.tasks["native-controlled"] = facts(
            "native-controlled", cwd=str(self.project)
        )
        self.runtime.terminal_rows["native-controlled"] = [
            {
                "terminal_id": "process-1",
                "processId": "process-1",
                "itemId": "item-1",
                "status": "running",
                "output_reference": "item-1",
            },
            {
                "terminal_id": "process-2",
                "processId": "process-2",
                "itemId": "item-2",
                "status": "running",
                "output_reference": "item-2",
            },
        ]
        service = TaskService()
        self.runtime.unsupported_termination = True
        unsupported = self.call(
            service.terminal_stop,
            self.request(
                ("task", "terminal", "stop"),
                arguments={
                    "id": task.id,
                    "terminal": "process-1",
                    "reason": "Stop only one exact terminal.",
                },
            ),
        )
        self.assertFalse(unsupported.ok)
        self.assertEqual(self.runtime.terminated, ["process-1"])
        self.assertEqual(len(self.runtime.terminal_rows["native-controlled"]), 2)
        self.runtime.unsupported_termination = False
        stopped = self.call(
            service.terminal_stop,
            self.request(
                ("task", "terminal", "stop"),
                arguments={
                    "id": task.id,
                    "all_owned": True,
                    "reason": "Stop every owned terminal.",
                },
            ),
        )
        self.assertTrue(stopped.ok)
        self.assertEqual(self.runtime.terminal_rows["native-controlled"], [])
        active = self.runtime.tasks["native-controlled"]
        self.runtime.tasks["native-controlled"] = TaskFacts(
            **{
                **active.__dict__,
                "active_turn": "turn-active",
                "runtime_status": "inProgress",
                "last_turn": {"id": "turn-active", "status": "inProgress"},
            }
        )
        with self.assertRaises(FulcrumError) as archive:
            self.call(
                service.archive,
                self.request(("task", "archive"), arguments={"id": task.id}),
            )
        self.assertEqual(archive.exception.code, "TASK_NOT_IDLE")
        self.runtime.tasks["native-controlled"] = facts(
            "native-controlled",
            last_turn={"id": "turn-done", "status": "completed"},
            cwd=str(self.project),
        )
        with self.assertRaises(FulcrumError) as deletion:
            self.call(
                service.delete,
                self.request(
                    ("task", "delete"), arguments={"id": task.id, "yes": True}
                ),
            )
        self.assertEqual(deletion.exception.code, "OWNERSHIP_CONFLICT")
        self.ledger.run(("close", work.id, "--reason", "fixture done"), mutating=True)
        deleted = self.call(
            service.delete,
            self.request(("task", "delete"), arguments={"id": task.id, "yes": True}),
        )
        self.assertTrue(deleted.ok)


class NativeOutputBoundTest(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_output_pages_without_resuming_and_truncates_one_item(
        self,
    ) -> None:
        runtime = AppServerRuntime("fake://runtime")
        runtime.transport.thread_items_page = AsyncMock(
            side_effect=[
                {
                    "items": [{"turnId": "turn-1", "item": {"text": "x" * 400}}],
                    "next_cursor": "after-large",
                }
            ]
        )
        result = await runtime.output(
            "thread-1",
            turn_id="turn-1",
            limit=20,
            cursor=None,
            max_bytes=128,
        )
        self.assertTrue(result["items"][0]["truncated"])
        self.assertEqual(result["next_cursor"], "after-large")
        self.assertTrue(result["gaps"])
        self.assertLessEqual(result["bytes"], 128)
        runtime.transport.thread_items_page.assert_awaited_once()

    async def test_limit_zero_reads_all_native_terminal_pages(self) -> None:
        runtime = AppServerRuntime("fake://runtime")
        runtime.transport.background_terminals_page = AsyncMock(
            side_effect=[
                {
                    "items": [{"processId": "process-1", "itemId": "item-1"}],
                    "next_cursor": "page-2",
                },
                {
                    "items": [{"processId": "process-2", "itemId": "item-2"}],
                    "next_cursor": None,
                },
            ]
        )
        result = await runtime.terminals("thread-1", limit=0, cursor=None)
        self.assertEqual(
            [item["terminal_id"] for item in result["items"]],
            ["process-1", "process-2"],
        )
        self.assertIsNone(result["next_cursor"])
        self.assertEqual(
            runtime.transport.background_terminals_page.await_args_list[1].kwargs[
                "cursor"
            ],
            "page-2",
        )

    async def test_release_never_cleans_a_still_running_terminal(self) -> None:
        runtime = AppServerRuntime("fake://runtime")
        runtime.inspect_task = AsyncMock(return_value=facts("thread-1"))
        runtime.transport.background_terminals = AsyncMock(
            return_value=[{"processId": "process-1", "itemId": "item-1"}]
        )
        runtime.transport.clean_background_terminals = AsyncMock()
        runtime.transport.unsubscribe = AsyncMock(return_value="unsubscribed")
        released = await runtime.release("thread-1")
        self.assertEqual(released.active_terminals[0]["processId"], "process-1")
        runtime.transport.clean_background_terminals.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
