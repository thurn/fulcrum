"""Newline-delimited JSON transport between local CLI and controller."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any


class ControllerUnavailable(RuntimeError):
    pass


async def request(
    socket_path: Path, payload: dict[str, Any], *, timeout: float = 30
) -> dict[str, Any]:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(socket_path), timeout
        )
    except (OSError, TimeoutError) as error:
        raise ControllerUnavailable(
            f"Fulcrum controller is unavailable at {socket_path}: {error}"
        ) from error
    try:
        writer.write(json.dumps(payload, separators=(",", ":")).encode() + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout)
        if not line:
            raise ControllerUnavailable(
                "Fulcrum controller closed the request without a response"
            )
        response = json.loads(line)
        if not isinstance(response, dict):
            raise ControllerUnavailable(
                "Fulcrum controller returned an invalid response"
            )
        if response.get("ok") is False:
            raise ControllerUnavailable(
                str(response.get("error", "controller request failed"))
            )
        return response
    finally:
        writer.close()
        await writer.wait_closed()


def request_sync(
    socket_path: Path, payload: dict[str, Any], *, timeout: float = 30
) -> dict[str, Any]:
    return asyncio.run(request(socket_path, payload, timeout=timeout))
