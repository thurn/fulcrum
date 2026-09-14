from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any
from unittest.mock import AsyncMock, call, patch

from websockets.asyncio.server import serve

from fulcrum.runtime import (
    SOURCE_KINDS,
    AppServerError,
    CodexRuntime,
    RuntimeEvent,
    thread_facts,
)


class RuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_archive_unsubscribes_thread_resources(self) -> None:
        runtime = CodexRuntime("ws://unused")
        runtime.request = AsyncMock(side_effect=[{}, {"status": "unsubscribed"}])

        await runtime.archive("thread-1")

        self.assertEqual(
            runtime.request.await_args_list,
            [
                call("thread/archive", {"threadId": "thread-1"}),
                call("thread/unsubscribe", {"threadId": "thread-1"}),
            ],
        )

    async def test_read_thread_retries_temporarily_empty_rollout(self) -> None:
        runtime = CodexRuntime("ws://unused")
        runtime.request = AsyncMock(
            side_effect=[
                AppServerError("rollout at /tmp/new.jsonl is empty"),
                {"thread": {"id": "thread-1"}},
            ]
        )

        with patch("fulcrum.runtime.asyncio.sleep", new=AsyncMock()) as sleep:
            thread = await runtime.read_thread("thread-1")

        self.assertEqual(thread["id"], "thread-1")
        sleep.assert_awaited_once_with(0.05)

    async def test_protocol_handshake_methods_and_server_request_queue(
        self,
    ) -> None:
        received: list[dict[str, Any]] = []
        server_response: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        thread = {
            "id": "thread-1",
            "name": None,
            "status": {"type": "idle"},
            "archived": False,
            "turns": [],
        }

        async def handler(connection: Any) -> None:
            async for raw in connection:
                message = json.loads(raw)
                received.append(message)
                if message.get("id") == 99 and "result" in message:
                    server_response.set_result(message)
                    continue
                method = message.get("method")
                identifier = message.get("id")
                if method == "initialize":
                    await connection.send(
                        json.dumps(
                            {
                                "id": identifier,
                                "result": {"serverInfo": {"name": "test"}},
                            }
                        )
                    )
                elif method == "initialized":
                    await connection.send(
                        json.dumps(
                            {"id": 99, "method": "unsupported/request", "params": {}}
                        )
                    )
                elif method == "model/list":
                    await connection.send(
                        json.dumps(
                            {"id": identifier, "result": {"data": [{"model": "sol"}]}}
                        )
                    )
                elif method == "project/list":
                    await connection.send(
                        json.dumps(
                            {
                                "id": identifier,
                                "result": {"data": [{"id": "project"}]},
                            }
                        )
                    )
                elif method == "thread/list":
                    await connection.send(
                        json.dumps({"id": identifier, "result": {"data": [thread]}})
                    )
                elif method == "thread/start":
                    await connection.send(
                        json.dumps({"id": identifier, "result": {"thread": thread}})
                    )
                elif method == "thread/metadata/update":
                    thread["projectId"] = message["params"]["projectId"]
                    await connection.send(
                        json.dumps({"id": identifier, "result": {"thread": thread}})
                    )
                elif method == "thread/name/set":
                    thread["name"] = message["params"]["name"]
                    await connection.send(json.dumps({"id": identifier, "result": {}}))
                elif method == "thread/read":
                    await connection.send(
                        json.dumps({"id": identifier, "result": {"thread": thread}})
                    )
                elif method == "thread/settings/update":
                    await connection.send(json.dumps({"id": identifier, "result": {}}))
                elif method == "turn/start":
                    await connection.send(
                        json.dumps(
                            {"id": identifier, "result": {"turn": {"id": "turn-1"}}}
                        )
                    )
                elif identifier is not None:
                    await connection.send(json.dumps({"id": identifier, "result": {}}))

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            runtime = CodexRuntime(f"ws://127.0.0.1:{port}")
            await runtime.connect()
            self.assertEqual(await runtime.list_models(), [{"model": "sol"}])
            self.assertEqual(await runtime.list_projects(), [{"id": "project"}])
            self.assertEqual(
                (await runtime.list_threads(cwd="/tmp", project_id="project"))[0]["id"],
                "thread-1",
            )
            self.assertTrue(await runtime.thread_is_listed("thread-1"))
            listing_requests = [
                item for item in received if item.get("method") == "thread/list"
            ]
            self.assertTrue(
                any(
                    item["params"].get("sourceKinds") == list(SOURCE_KINDS)
                    for item in listing_requests
                )
            )
            created = await runtime.create_thread(
                cwd="/tmp",
                workspace_root="/workspace",
                model="sol",
                project_id="project",
            )
            self.assertEqual(created["thread"]["id"], "thread-1")
            self.assertEqual(created["thread"]["projectId"], "project")
            create_request = next(
                item for item in received if item.get("method") == "thread/start"
            )
            self.assertEqual(
                create_request["params"],
                {
                    "cwd": "/tmp",
                    "model": "sol",
                    "projectId": "project",
                    "runtimeWorkspaceRoots": ["/workspace"],
                },
            )
            metadata_request = next(
                item
                for item in received
                if item.get("method") == "thread/metadata/update"
            )
            self.assertEqual(
                metadata_request["params"],
                {"threadId": "thread-1", "projectId": "project"},
            )
            await runtime.set_name("thread-1", "Canonical")
            self.assertEqual(
                (await runtime.read_thread("thread-1"))["name"], "Canonical"
            )
            turn = await runtime.start_turn(
                "thread-1",
                "brief",
                cwd="/tmp",
                workspace_root="/workspace",
                model="sol",
                effort="high",
                correlation="operation-1",
                developer_instructions="Use the assigned worktree.",
            )
            self.assertEqual(turn, "turn-1")
            settings_request = next(
                item
                for item in received
                if item.get("method") == "thread/settings/update"
            )
            self.assertEqual(
                settings_request["params"],
                {
                    "threadId": "thread-1",
                    "cwd": "/tmp",
                    "model": "sol",
                    "effort": "high",
                    "summary": "concise",
                    "collaborationMode": {
                        "mode": "default",
                        "settings": {
                            "model": "sol",
                            "reasoning_effort": "high",
                            "developer_instructions": "Use the assigned worktree.",
                        },
                    },
                },
            )
            turn_request = next(
                item for item in received if item.get("method") == "turn/start"
            )
            self.assertEqual(turn_request["params"]["summary"], "concise")
            self.assertEqual(
                turn_request["params"]["runtimeWorkspaceRoots"], ["/workspace"]
            )
            self.assertLess(
                received.index(settings_request), received.index(turn_request)
            )
            event = await anext(runtime.events())
            self.assertEqual(event.request_id, "99")
            self.assertEqual(event.method, "unsupported/request")
            await runtime.respond_server_request("99", {"accepted": False})
            response = await asyncio.wait_for(server_response, 1)
            self.assertEqual(response["result"], {"accepted": False})
            self.assertTrue(all("jsonrpc" not in item for item in received))
            await runtime.close()

    def test_readiness_requires_helper_terminal_state(self) -> None:
        thread = {
            "status": {"type": "idle"},
            "turns": [
                {
                    "id": "turn",
                    "status": "completed",
                    "items": [
                        {
                            "type": "collabAgentToolCall",
                            "agentsStates": {"helper": {"status": "running"}},
                        }
                    ],
                }
            ],
        }
        facts = thread_facts(thread)
        self.assertFalse(facts["helpers_terminal"])
        self.assertFalse(facts["can_start"])

    async def test_connection_close_is_reported_and_allows_reconnect(self) -> None:
        disconnected: asyncio.Future[tuple[str, dict[str, Any]]] = (
            asyncio.get_running_loop().create_future()
        )

        async def event_handler(method: str, params: dict[str, Any]) -> None:
            if method == "fulcrum/runtime/disconnected" and not disconnected.done():
                disconnected.set_result((method, params))

        async def handler(connection: Any) -> None:
            async for raw in connection:
                message = json.loads(raw)
                if message.get("method") == "initialize":
                    await connection.send(
                        json.dumps({"id": message["id"], "result": {}})
                    )
                elif message.get("method") == "initialized":
                    await connection.close()

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            runtime = CodexRuntime(
                f"ws://127.0.0.1:{port}", event_handler=event_handler
            )
            await runtime.connect()
            method, params = await asyncio.wait_for(disconnected, 1)
            self.assertEqual(method, "fulcrum/runtime/disconnected")
            self.assertIn("closed", params["error"])
            self.assertFalse(runtime.ready)
            self.assertIsNone(runtime.websocket)
            await runtime.connect()
            await runtime.close()

    async def test_connection_loss_reconnects_with_backoff(self) -> None:
        connections = 0
        reconnected = asyncio.Event()

        async def event_handler(method: str, params: dict[str, Any]) -> None:
            if method == "fulcrum/runtime/reconnected":
                reconnected.set()

        async def handler(connection: Any) -> None:
            nonlocal connections
            connections += 1
            this_connection = connections
            async for raw in connection:
                message = json.loads(raw)
                if message.get("method") == "initialize":
                    await connection.send(
                        json.dumps({"id": message["id"], "result": {}})
                    )
                elif message.get("method") == "initialized" and this_connection == 1:
                    await connection.close()

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            runtime = CodexRuntime(
                f"ws://127.0.0.1:{port}",
                event_handler=event_handler,
                reconnect_waits=(0.01,),
            )
            await runtime.connect()
            await asyncio.wait_for(reconnected.wait(), 1)
            self.assertGreaterEqual(connections, 2)
            self.assertTrue(runtime.ready)
            await runtime.close()

    async def test_event_handler_can_await_rpc_without_deadlocking_reader(self) -> None:
        handled = asyncio.Event()
        runtime: CodexRuntime

        async def event_handler(method: str, params: dict[str, Any]) -> None:
            if method == "thread/status/changed":
                self.assertEqual(await runtime.list_models(), [{"model": "luna"}])
                handled.set()

        async def handler(connection: Any) -> None:
            async for raw in connection:
                message = json.loads(raw)
                if message.get("method") == "initialize":
                    await connection.send(
                        json.dumps({"id": message["id"], "result": {}})
                    )
                elif message.get("method") == "initialized":
                    await connection.send(
                        json.dumps(
                            {
                                "method": "thread/status/changed",
                                "params": {"threadId": "thread-1"},
                            }
                        )
                    )
                elif message.get("method") == "model/list":
                    await connection.send(
                        json.dumps(
                            {
                                "id": message["id"],
                                "result": {"data": [{"model": "luna"}]},
                            }
                        )
                    )

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            runtime = CodexRuntime(
                f"ws://127.0.0.1:{port}", event_handler=event_handler
            )
            await runtime.connect()
            await asyncio.wait_for(handled.wait(), 1)
            await runtime.close()

    async def test_bounded_events_retain_native_request_and_require_reconcile(
        self,
    ) -> None:
        runtime = CodexRuntime("ws://unused", event_capacity=2)
        for index in range(2):
            runtime._enqueue(
                RuntimeEvent(
                    method="item/agentMessage/delta",
                    params={"index": index},
                    request_id=None,
                    observed_at="now",
                )
            )
        runtime._enqueue(
            RuntimeEvent(
                method="item/tool/requestUserInput",
                params={"threadId": "thread-1"},
                request_id="42",
                observed_at="now",
            )
        )

        overflow = await anext(runtime.events())
        retained = [await anext(runtime.events()), await anext(runtime.events())]

        self.assertEqual(overflow.method, "fulcrum/runtime/event-overflow")
        self.assertIn("42", {item.request_id for item in retained})


if __name__ == "__main__":
    unittest.main()
