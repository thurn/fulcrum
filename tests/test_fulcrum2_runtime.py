from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

from websockets.asyncio.server import serve

from fulcrum.runtime import (
    AppServerError,
    AppServerRuntime,
    CodexRuntime,
    RuntimeCapabilities,
    TaskFacts,
    TaskSpec,
    TURN_START_ACK_TIMEOUT_SECONDS,
    TurnFacts,
)
from fulcrum.runtime_service import (
    _interrupt_or_observe,
    _create_and_configure,
    _runtime_call,
    _start_or_recover,
    _wait_for_task,
)


def task_facts(
    identifier: str = "thread-1", *, project_id: str | None = "project-1"
) -> TaskFacts:
    return TaskFacts(
        id=identifier,
        title="Managed task",
        cwd="/work",
        project_id=project_id,
        workspace_roots=("/work",),
        archived=False,
        exists=True,
        loaded=True,
        runtime_status="idle",
        active_turn=None,
        last_turn=None,
        pending_requests=(),
        observed_at="2026-09-14T00:00:00Z",
    )


def task_spec() -> TaskSpec:
    return TaskSpec(
        creation_cwd="/instance/threads/fc-op-1",
        cwd="/work",
        project_id="project-1",
        workspace_roots=("/work",),
        title="Managed task",
        model="luna",
        effort="high",
    )


class RuntimeFailureFixtureTest(unittest.IsolatedAsyncioTestCase):
    def test_task_facts_exclude_unbounded_turn_items(self) -> None:
        facts = TaskFacts(
            id="thread-1",
            title="Managed task",
            cwd="/work",
            project_id="project-1",
            workspace_roots=("/work",),
            archived=False,
            exists=True,
            loaded=True,
            runtime_status="inProgress",
            active_turn="turn-1",
            last_turn={
                "id": "turn-1",
                "status": "inProgress",
                "items": [{"type": "agentMessage", "text": "x" * 1_000_000}],
            },
            pending_requests=(),
            observed_at="2026-09-14T00:00:00Z",
        )

        retained = facts.to_dict()

        self.assertNotIn("items", retained["last_turn"])
        self.assertEqual(retained["last_turn"]["item_count"], 1)
        self.assertLess(len(json.dumps(retained)), 1_000)
        self.assertIn("items", facts.last_turn)

    async def test_tool_rich_native_history_frame_is_observable(self) -> None:
        padding = "x" * (17 * 1024 * 1024)

        async def handler(connection: Any) -> None:
            async for raw in connection:
                message = json.loads(raw)
                if "id" not in message:
                    continue
                if message.get("method") == "initialize":
                    result: dict[str, Any] = {}
                elif message.get("method") == "model/list":
                    result = {"data": [{"model": "luna", "history": padding}]}
                else:
                    result = {}
                await connection.send(
                    json.dumps({"id": message["id"], "result": result})
                )

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            runtime = CodexRuntime(f"ws://127.0.0.1:{port}")
            await runtime.connect()
            models = await runtime.list_models()
            await runtime.close()

        self.assertEqual(models[0]["model"], "luna")
        self.assertEqual(len(models[0]["history"]), len(padding))

    async def test_malformed_response_fails_pending_call_as_uncertain(self) -> None:
        async def handler(connection: Any) -> None:
            async for raw in connection:
                message = json.loads(raw)
                if message.get("method") == "initialize":
                    await connection.send(
                        json.dumps({"id": message["id"], "result": {}})
                    )
                elif message.get("method") == "model/list":
                    await connection.send("{")

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            runtime = CodexRuntime(f"ws://127.0.0.1:{port}")
            await runtime.connect()
            with self.assertRaises(AppServerError) as captured:
                await runtime.list_models()
            self.assertTrue(captured.exception.uncertain)
            self.assertEqual(captured.exception.category, "uncertain")
            await runtime.close()

    async def test_unsupported_configuration_is_classified(self) -> None:
        async def handler(connection: Any) -> None:
            async for raw in connection:
                message = json.loads(raw)
                if message.get("method") == "initialize":
                    await connection.send(
                        json.dumps({"id": message["id"], "result": {}})
                    )
                elif message.get("method") == "thread/settings/update":
                    await connection.send(
                        json.dumps(
                            {
                                "id": message["id"],
                                "error": {"code": -32601, "message": "unsupported"},
                            }
                        )
                    )

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            runtime = CodexRuntime(f"ws://127.0.0.1:{port}")
            await runtime.connect()
            with self.assertRaises(AppServerError) as captured:
                await runtime.configure_thread(
                    "thread-1",
                    cwd="/work",
                    workspace_root="/work",
                    model="luna",
                    effort="high",
                )
            self.assertEqual(captured.exception.category, "unsupported")
            self.assertFalse(captured.exception.uncertain)
            await runtime.close()

    async def test_all_idempotent_unsubscribe_outcomes_are_successful(self) -> None:
        for status in ("unsubscribed", "notSubscribed", "notLoaded"):
            runtime = CodexRuntime("ws://unused")
            runtime.request = AsyncMock(return_value={"status": status})
            self.assertEqual(await runtime.unsubscribe("thread-1"), status)

    async def test_turn_start_uses_short_acceptance_timeout(self) -> None:
        runtime = CodexRuntime("ws://unused")
        runtime.configure_thread = AsyncMock()
        runtime.request = AsyncMock(return_value={"turn": {"id": "turn-1"}})

        turn_id = await runtime.start_turn(
            "thread-1",
            "Start retained work.",
            cwd="/work",
            workspace_root="/work",
            model="luna",
            effort="high",
            correlation="fc-operation",
        )

        self.assertEqual(turn_id, "turn-1")
        self.assertEqual(
            runtime.request.await_args.kwargs["timeout"],
            TURN_START_ACK_TIMEOUT_SECONDS,
        )


class RuntimeRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_lost_create_response_adopts_exact_cwd_match(self) -> None:
        native = task_facts()
        runtime = AsyncMock()
        runtime.capabilities.return_value = RuntimeCapabilities(
            available=True,
            endpoint="ws://runtime",
            methods=(),
            models={"luna": ("high",)},
        )
        runtime.find_tasks.side_effect = [[], [native]]
        runtime.create_task.side_effect = AppServerError(
            "connection lost", category="uncertain", uncertain=True
        )
        runtime.configure_task.return_value = native

        result = await _create_and_configure(runtime, task_spec(), None)

        self.assertEqual(result.id, "thread-1")
        self.assertEqual(
            runtime.find_tasks.await_args_list[0].args,
            ("/instance/threads/fc-op-1",),
        )
        self.assertEqual(runtime.find_tasks.await_count, 2)
        runtime.create_task.assert_awaited_once()

    async def test_multiple_creation_matches_require_recovery(self) -> None:
        runtime = AsyncMock()
        runtime.capabilities.return_value = RuntimeCapabilities(
            available=True,
            endpoint="ws://runtime",
            methods=(),
            models={"luna": ("high",)},
        )
        runtime.find_tasks.return_value = [task_facts("one"), task_facts("two")]

        with self.assertRaises(AppServerError) as captured:
            await _create_and_configure(runtime, task_spec(), None)

        self.assertTrue(captured.exception.uncertain)
        runtime.create_task.assert_not_awaited()

    async def test_lost_start_response_adopts_exact_input_marker(self) -> None:
        found = TurnFacts(
            id="turn-1",
            thread_id="thread-1",
            state="inProgress",
            operation_id="fc-op-1",
            completed=False,
            error=None,
            tools=(),
            usage=None,
            observed_at="2026-09-14T00:00:00Z",
        )
        runtime = AsyncMock()
        runtime.find_turn.side_effect = [None, found]
        runtime.start_turn.side_effect = AppServerError(
            "connection lost", category="uncertain", uncertain=True
        )

        result = await _start_or_recover(
            runtime,
            "thread-1",
            task_spec(),
            "fc-op-1",
            "Do the work.",
            ownership_operation="fc-owner-1",
        )

        self.assertEqual(result.id, "turn-1")
        runtime.start_turn.assert_awaited_once()
        turn_input = runtime.start_turn.await_args.args[1]
        self.assertEqual(turn_input.operation_id, "fc-op-1")
        self.assertEqual(turn_input.ownership_operation, "fc-owner-1")
        self.assertEqual(runtime.find_turn.await_count, 2)

    async def test_interrupt_race_accepts_observed_terminal_turn(self) -> None:
        completed = TurnFacts(
            id="turn-ended",
            thread_id="thread-1",
            state="completed",
            operation_id=None,
            completed=True,
            error=None,
            tools=(),
            usage=None,
            observed_at="2026-09-15T17:00:00Z",
        )
        runtime = AsyncMock()
        runtime.interrupt.side_effect = AppServerError(
            "no active turn to interrupt", category="rejected"
        )
        runtime.inspect_turn.return_value = completed

        result = await _interrupt_or_observe(runtime, "thread-1", "turn-ended")

        self.assertEqual(result, completed)
        runtime.inspect_turn.assert_awaited_once_with("thread-1", "turn-ended")

    async def test_find_turn_requires_exact_persisted_marker(self) -> None:
        transport = AsyncMock()
        transport.read_thread.return_value = {
            "id": "thread-1",
            "turns": [
                {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [
                        {
                            "type": "userMessage",
                            "text": "FULCRUM_OPERATION=fc-op-1 Do the work.",
                        }
                    ],
                },
                {
                    "id": "turn-near",
                    "status": "completed",
                    "items": [
                        {
                            "type": "userMessage",
                            "text": "FULCRUM_OPERATION=fc-op-10",
                        }
                    ],
                },
            ],
        }
        runtime = AppServerRuntime(
            "ws://unused", transport=cast(CodexRuntime, transport)
        )

        result = await runtime.find_turn("thread-1", "fc-op-1")

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.id, "turn-1")

    async def test_project_mismatch_is_uncertain_after_configuration(self) -> None:
        transport = AsyncMock()
        transport.read_thread.return_value = {
            "id": "thread-1",
            "name": "Managed task",
            "cwd": "/work",
            "projectId": "wrong-project",
            "runtimeWorkspaceRoots": ["/work"],
            "status": {"type": "idle"},
            "turns": [],
        }
        transport.loaded_threads.return_value = ["thread-1"]
        transport.pending_server_requests = {}
        transport.assign_thread_project.return_value = {
            "thread": transport.read_thread.return_value
        }
        runtime = AppServerRuntime(
            "ws://unused", transport=cast(CodexRuntime, transport)
        )

        with self.assertRaises(AppServerError) as captured:
            await runtime.configure_task("thread-1", task_spec())

        self.assertTrue(captured.exception.uncertain)

    async def test_project_creation_is_discovered_by_exact_root(self) -> None:
        transport = AsyncMock()
        transport.list_projects.side_effect = [
            [],
            [
                {
                    "id": "project-1",
                    "name": "toy",
                    "roots": [{"path": "/private/tmp/toy"}],
                }
            ],
        ]
        transport.create_project.side_effect = AppServerError(
            "connection lost", category="uncertain", uncertain=True
        )
        runtime = AppServerRuntime(
            "ws://unused", transport=cast(CodexRuntime, transport)
        )

        project = await runtime.ensure_project(
            name="toy", root="/private/tmp/toy", operation_id="fc-op-project"
        )

        self.assertEqual(project["id"], "project-1")
        transport.create_project.assert_awaited_once_with(
            name="toy",
            roots=("/private/tmp/toy",),
            idempotency_key="fc-op-project",
            metadata={"fulcrum_operation": "fc-op-project"},
        )

    async def test_project_deletion_is_verified_by_exact_id(self) -> None:
        transport = AsyncMock()
        transport.list_projects.side_effect = [
            [{"id": "project-1", "name": "toy"}],
            [{"id": "unrelated", "name": "other"}],
        ]
        runtime = AppServerRuntime(
            "ws://unused", transport=cast(CodexRuntime, transport)
        )

        result = await runtime.delete_project("project-1")

        self.assertEqual(result, {"id": "project-1", "exists": False, "deleted": True})
        transport.delete_project.assert_awaited_once_with("project-1")

    async def test_task_wait_reconnects_after_transient_disconnect(self) -> None:
        runtime = AsyncMock()
        terminal = TaskFacts(
            **{
                **task_facts().__dict__,
                "last_turn": {"id": "turn-1", "status": "completed"},
            }
        )
        runtime.inspect_task.side_effect = [
            AppServerError("app-server is not connected"),
            terminal,
        ]

        result = await _wait_for_task(
            runtime,
            "thread-1",
            turn_id=None,
            until="terminal",
            timeout=2,
        )

        self.assertEqual(result["task"]["id"], "thread-1")
        runtime.connect.assert_awaited_once()

    def test_command_timeout_is_forwarded_to_shared_runtime_bridge(self) -> None:
        observed: list[float] = []

        def submit(_action: Any, timeout: float) -> str:
            observed.append(timeout)
            return "completed"

        request = cast(
            Any,
            SimpleNamespace(runtime_submit=submit, timeout=300.0),
        )
        result = _runtime_call(request, lambda _runtime: AsyncMock())

        self.assertEqual(result, "completed")
        self.assertEqual(observed, [305.0])


if __name__ == "__main__":
    unittest.main()
