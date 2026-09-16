"""Replaceable runtime adapter; closing a client never closes the native session."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Mapping

from fulcrum.runtime import CodexRuntime
from fulcrum.transport import AppServerError, RuntimeEvent


async def exchange(socket: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    try:
        reader, writer = await asyncio.open_unix_connection(
            str(socket), limit=64 * 1024 * 1024
        )
        try:
            writer.write(json.dumps(dict(payload)).encode() + b"\n")
            await writer.drain()
            raw = await asyncio.wait_for(
                reader.readline(), float(payload.get("timeout", 30)) + 5
            )
            result = json.loads(raw)
            if "error" in result:
                raise AppServerError(
                    result["error"],
                    category=result.get("category", "unavailable"),
                    uncertain=result.get("uncertain", False),
                )
            return result
        finally:
            writer.close()
            await writer.wait_closed()
    except (OSError, ValueError, asyncio.TimeoutError) as error:
        raise AppServerError(
            f"resident connection is unavailable: {error}",
            uncertain=payload.get("action") == "request",
        ) from error


class ResidentTransport(CodexRuntime):
    def __init__(self, socket: Path) -> None:
        super().__init__("resident")
        self.socket = socket

    async def connect(self) -> None:
        facts = await exchange(self.socket, {"action": "health"})
        self.pending_server_requests = {
            key: RuntimeEvent(**value) for key, value in facts["pending"].items()
        }
        self.ready = True

    async def close(self) -> None:
        # Native subscriptions and approvals belong to the resident, not this CLI.
        pass

    async def request(
        self, method: str, params: Mapping[str, Any], *, timeout: float = 30.0
    ) -> dict[str, Any]:
        result = await exchange(
            self.socket,
            {
                "action": "request",
                "method": method,
                "params": dict(params),
                "timeout": timeout,
            },
        )
        self.pending_server_requests = {
            key: RuntimeEvent(**value)
            for key, value in result.get("pending", {}).items()
        }
        return result["result"]

    async def respond_server_request(
        self, request_id: str, response: Mapping[str, Any]
    ) -> None:
        await exchange(
            self.socket,
            {"action": "respond", "request_id": request_id, "response": dict(response)},
        )
        self.pending_server_requests.pop(request_id, None)
