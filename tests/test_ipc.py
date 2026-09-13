from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from fulcrum.ipc import request


class IpcTest(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()
