"""Bounded newline-delimited JSON transport for the local controller."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fulcrum.contracts import CommandResult, FulcrumError, ParsedRequest

MAX_MESSAGE_BYTES = 4 * 1024 * 1024
# Thirty live workers can simultaneously hold a barrier request, a task wait, and
# an admission/reconciliation request while operators still need responsive
# status and release controls.  Keep that fanout explicitly bounded, with room
# for the three 30-wide request classes plus ordinary control traffic.
IPC_HANDLER_LIMIT = 128
_IPC_HANDLERS = ThreadPoolExecutor(
    max_workers=IPC_HANDLER_LIMIT, thread_name_prefix="fulcrum-ipc"
)


class ControllerUnavailable(RuntimeError):
    pass


class ControllerTimedOut(RuntimeError):
    pass


class ControllerRejected(RuntimeError):
    pass


async def request(
    socket_path: Path, payload: Mapping[str, Any], *, timeout: float = 30
) -> dict[str, Any]:
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise FulcrumError.invalid(
            "INPUT_TOO_LARGE",
            "serialized request exceeds the 4 MiB IPC limit",
            details={"bytes": len(encoded), "limit": MAX_MESSAGE_BYTES},
        )
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(socket_path, limit=MAX_MESSAGE_BYTES + 1),
            timeout,
        )
    except (OSError, TimeoutError) as error:
        raise ControllerUnavailable(
            f"Fulcrum controller is unavailable at {socket_path}: {error}"
        ) from error
    try:
        writer.write(encoded)
        await writer.drain()
        try:
            line = await asyncio.wait_for(reader.readline(), timeout)
        except TimeoutError as error:
            raise ControllerTimedOut(
                "controller request is still running after the client deadline"
            ) from error
        except (ValueError, asyncio.LimitOverrunError) as error:
            raise ControllerUnavailable(
                "controller response exceeded the IPC limit"
            ) from error
        if not line:
            raise ControllerUnavailable(
                "Fulcrum controller closed the request without a response"
            )
        try:
            response = json.loads(line)
        except json.JSONDecodeError as error:
            raise ControllerUnavailable("controller returned invalid JSON") from error
        if not isinstance(response, dict):
            raise ControllerUnavailable("controller returned a non-object response")
        return response
    finally:
        writer.close()
        await writer.wait_closed()


def request_sync(
    socket_path: Path, payload: Mapping[str, Any], *, timeout: float = 30
) -> dict[str, Any]:
    return asyncio.run(request(socket_path, payload, timeout=timeout))


class IpcServer:
    def __init__(
        self,
        socket_path: Path,
        handler: Callable[[ParsedRequest], CommandResult],
        *,
        once: bool = False,
    ) -> None:
        self.socket_path = socket_path
        self.handler = handler
        self.once = once
        self._server: asyncio.AbstractServer | None = None
        self._handled = asyncio.Event()

    async def run(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(
            self._handle,
            path=self.socket_path,
            limit=MAX_MESSAGE_BYTES + 1,
        )
        self._server = server
        try:
            if self.once:
                await self._handled.wait()
            else:
                async with server:
                    await server.serve_forever()
        finally:
            server.close()
            await server.wait_closed()
            self.socket_path.unlink(missing_ok=True)

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        response: dict[str, Any]
        try:
            try:
                line = await reader.readline()
            except (ValueError, asyncio.LimitOverrunError) as error:
                raise FulcrumError.invalid(
                    "INPUT_TOO_LARGE", "request exceeds the 4 MiB IPC limit"
                ) from error
            if not line or len(line) > MAX_MESSAGE_BYTES:
                raise FulcrumError.invalid(
                    "INPUT_TOO_LARGE", "request exceeds the 4 MiB IPC limit"
                )
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as error:
                raise FulcrumError.invalid(
                    "INVALID_JSON", "request is not valid JSON"
                ) from error
            if not isinstance(payload, dict):
                raise FulcrumError.invalid(
                    "INVALID_REQUEST", "request must be an object"
                )
            parsed = ParsedRequest.from_wire(payload)
            response = (
                await asyncio.get_running_loop().run_in_executor(
                    _IPC_HANDLERS, self.handler, parsed
                )
            ).to_dict()
        except FulcrumError as error:
            response = error.to_result().to_dict()
        except Exception as error:  # pragma: no cover - last-resort protocol boundary
            response = (
                FulcrumError("INTERNAL_ERROR", str(error), exit_code=2)
                .to_result()
                .to_dict()
            )
        try:
            writer.write(json.dumps(response, separators=(",", ":")).encode() + b"\n")
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
            if self.once:
                self._handled.set()
