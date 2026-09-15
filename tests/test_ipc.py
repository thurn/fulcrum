from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, Mock

from fulcrum.contracts import (
    ActorContext,
    CommandResult,
    InstanceContext,
    ParsedRequest,
)
from fulcrum.ipc import IPC_HANDLER_LIMIT, ControllerTimedOut, IpcServer, request


class IpcTest(unittest.IsolatedAsyncioTestCase):
    async def test_waiting_handlers_leave_control_headroom_at_thirty_tasks(
        self,
    ) -> None:
        parsed = ParsedRequest(
            command=("task", "wait"),
            arguments={},
            input={},
            actor=ActorContext(kind="task", task_id="fixture-task"),
            instance=InstanceContext(
                instance_root=Path("/tmp/instance"),
                config_path=Path("/tmp/config"),
                brain_root=Path("/tmp/brain"),
                socket_path=Path("/tmp/controller.sock"),
                lock_path=Path("/tmp/controller.lock"),
                explicit_selection=True,
            ),
            request_id=None,
        )
        entered = 0
        entered_lock = threading.Lock()
        release = threading.Event()

        def wait_at_barrier(_request: ParsedRequest) -> CommandResult:
            nonlocal entered
            with entered_lock:
                entered += 1
            if not release.wait(10):
                raise RuntimeError("IPC fixture barrier timed out")
            return CommandResult.query({"released": True})

        server = IpcServer(Path("/tmp/not-created.sock"), wait_at_barrier)
        handlers = []
        writers = []
        # Keep control traffic responsive with three concurrent requests per
        # worker, plus a status/release control lane.
        expected_concurrent_requests = 30 * 3 + 2
        self.assertGreaterEqual(IPC_HANDLER_LIMIT, expected_concurrent_requests)
        for _ in range(expected_concurrent_requests):
            reader = AsyncMock()
            reader.readline.return_value = (
                json.dumps(parsed.to_wire(), separators=(",", ":")).encode() + b"\n"
            )
            writer = Mock()
            writer.drain = AsyncMock()
            writer.wait_closed = AsyncMock()
            writers.append(writer)
            handlers.append(asyncio.create_task(server._handle(reader, writer)))
        deadline = asyncio.get_running_loop().time() + 5
        while (
            entered < expected_concurrent_requests
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.01)
        self.assertEqual(entered, expected_concurrent_requests)
        release.set()
        await asyncio.wait_for(asyncio.gather(*handlers), 10)
        self.assertTrue(all(writer.write.called for writer in writers))

    async def test_request_classifies_a_controller_response_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "controller.sock"
            release = asyncio.Event()

            async def do_not_respond(
                reader: asyncio.StreamReader, writer: asyncio.StreamWriter
            ) -> None:
                await reader.readline()
                await release.wait()
                writer.close()
                await writer.wait_closed()

            server = await asyncio.start_unix_server(do_not_respond, path=socket_path)
            async with server:
                with self.assertRaises(ControllerTimedOut):
                    await request(socket_path, {"command": "slow"}, timeout=0.01)
                release.set()

    async def test_request_accepts_large_controller_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "controller.sock"

            async def respond(
                reader: asyncio.StreamReader, writer: asyncio.StreamWriter
            ) -> None:
                await reader.readline()
                response = {"ok": True, "data": {"detail": "x" * 200_000}}
                writer.write(json.dumps(response).encode() + b"\n")
                await writer.drain()
                writer.close()
                await writer.wait_closed()

            server = await asyncio.start_unix_server(respond, path=socket_path)
            async with server:
                response = await request(socket_path, {"command": "status"})

            self.assertEqual(len(response["data"]["detail"]), 200_000)

    async def test_server_tolerates_client_disconnect_before_response(self) -> None:
        parsed = ParsedRequest(
            command=("status",),
            arguments={},
            input={},
            actor=ActorContext(kind="human"),
            instance=InstanceContext(
                instance_root=Path("/tmp/instance"),
                config_path=Path("/tmp/config"),
                brain_root=Path("/tmp/brain"),
                socket_path=Path("/tmp/controller.sock"),
                lock_path=Path("/tmp/controller.lock"),
                explicit_selection=True,
            ),
            request_id=None,
        )
        reader = AsyncMock()
        reader.readline.return_value = (
            json.dumps(parsed.to_wire(), separators=(",", ":")).encode() + b"\n"
        )
        writer = Mock()
        writer.drain = AsyncMock(side_effect=ConnectionResetError("client left"))
        writer.wait_closed = AsyncMock()
        server = IpcServer(
            Path("/tmp/not-created.sock"),
            lambda _request: CommandResult.query({"ready": True}),
        )

        await server._handle(reader, writer)

        writer.close.assert_called_once()
        writer.wait_closed.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
