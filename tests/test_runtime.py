from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any

from websockets.asyncio.server import serve

from fulcrum.runtime import CodexRuntime, thread_facts


class RuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_protocol_handshake_methods_and_server_request_rejection(
        self,
    ) -> None:
        received: list[dict[str, Any]] = []
        rejection: asyncio.Future[dict[str, Any]] = (
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
                if message.get("id") == 99 and "error" in message:
                    rejection.set_result(message)
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
                elif method == "thread/name/set":
                    thread["name"] = message["params"]["name"]
                    await connection.send(json.dumps({"id": identifier, "result": {}}))
                elif method == "thread/read":
                    await connection.send(
                        json.dumps({"id": identifier, "result": {"thread": thread}})
                    )
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
            created = await runtime.create_thread(
                cwd="/tmp",
                model="sol",
                project_id="project",
                base_instructions="role",
            )
            self.assertEqual(created["thread"]["id"], "thread-1")
            await runtime.set_name("thread-1", "Canonical")
            self.assertEqual(
                (await runtime.read_thread("thread-1"))["name"], "Canonical"
            )
            turn = await runtime.start_turn(
                "thread-1",
                "brief",
                cwd="/tmp",
                model="sol",
                effort="high",
                correlation="operation-1",
            )
            self.assertEqual(turn, "turn-1")
            rejected = await asyncio.wait_for(rejection, 1)
            self.assertEqual(rejected["error"]["code"], -32601)
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


if __name__ == "__main__":
    unittest.main()
