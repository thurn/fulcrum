"""Small asynchronous adapter for the installed Codex app-server protocol."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from websockets.asyncio.client import ClientConnection, connect


class AppServerError(RuntimeError):
    """The shared Codex runtime is unavailable or contradicted expectations."""


EventHandler = Callable[[str, dict[str, Any]], Awaitable[None]]


class CodexRuntime:
    """One initialized JSON-RPC client with an always-running event reader."""

    def __init__(
        self, endpoint: str, *, event_handler: EventHandler | None = None
    ) -> None:
        self.endpoint = endpoint
        self.event_handler = event_handler
        self.websocket: ClientConnection | None = None
        self.reader_task: asyncio.Task[None] | None = None
        self.pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.request_id = 0
        self.ready = False

    async def connect(self) -> None:
        if self.websocket is not None:
            return
        try:
            self.websocket = await connect(
                self.endpoint,
                open_timeout=10,
                ping_interval=20,
                ping_timeout=20,
                max_size=16 * 1024 * 1024,
            )
        except Exception as error:
            raise AppServerError(
                f"cannot connect to shared app-server at {self.endpoint}: {error}"
            ) from error
        self.reader_task = asyncio.create_task(
            self._reader(), name="codex-runtime-reader"
        )
        try:
            await self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "fulcrum",
                        "title": "Fulcrum Controller",
                        "version": "local",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            )
            await self.notify("initialized", {})
        except Exception:
            await self.close()
            raise
        self.ready = True

    async def close(self) -> None:
        self.ready = False
        if self.websocket is not None:
            await self.websocket.close()
            self.websocket = None
        if (
            self.reader_task is not None
            and self.reader_task is not asyncio.current_task()
        ):
            reader_task = self.reader_task
            reader_task.cancel()
            await asyncio.gather(reader_task, return_exceptions=True)
        self.reader_task = None
        for future in self.pending.values():
            if not future.done():
                future.set_exception(AppServerError("app-server disconnected"))
        self.pending.clear()

    async def request(
        self, method: str, params: dict[str, Any], *, timeout: float = 30
    ) -> dict[str, Any]:
        websocket = self.websocket
        if websocket is None:
            raise AppServerError("app-server is not connected")
        self.request_id += 1
        identifier = self.request_id
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self.pending[identifier] = future
        await websocket.send(
            json.dumps(
                {"id": identifier, "method": method, "params": params},
                separators=(",", ":"),
            )
        )
        try:
            return await asyncio.wait_for(future, timeout)
        except TimeoutError as error:
            raise AppServerError(
                f"app-server request {method} timed out; result is uncertain"
            ) from error
        finally:
            self.pending.pop(identifier, None)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        websocket = self.websocket
        if websocket is None:
            raise AppServerError("app-server is not connected")
        await websocket.send(
            json.dumps({"method": method, "params": params}, separators=(",", ":"))
        )

    async def _reader(self) -> None:
        websocket = self.websocket
        assert websocket is not None
        try:
            async for raw in websocket:
                message = json.loads(raw)
                if not isinstance(message, dict):
                    continue
                identifier = message.get("id")
                method = message.get("method")
                if identifier is not None and method is None:
                    future = self.pending.get(int(identifier))
                    if future is None or future.done():
                        continue
                    if "error" in message:
                        future.set_exception(
                            AppServerError(f"app-server error: {message['error']}")
                        )
                    else:
                        result = message.get("result")
                        future.set_result(
                            result if isinstance(result, dict) else {"value": result}
                        )
                elif identifier is not None and isinstance(method, str):
                    await self._reject_server_request(identifier, method)
                elif isinstance(method, str) and self.event_handler is not None:
                    params = message.get("params")
                    handler = self.event_handler
                    assert handler is not None
                    await handler(method, params if isinstance(params, dict) else {})
        except asyncio.CancelledError:
            raise
        except Exception as error:
            reason = str(error)
        else:
            reason = "app-server connection closed"
        self.ready = False
        self.websocket = None
        if self.event_handler is not None:
            await self.event_handler("fulcrum/runtime/disconnected", {"error": reason})
        for future in self.pending.values():
            if not future.done():
                future.set_exception(
                    AppServerError(f"app-server connection lost: {reason}")
                )

    async def _reject_server_request(self, identifier: object, method: str) -> None:
        assert self.websocket is not None
        await self.websocket.send(
            json.dumps(
                {
                    "id": identifier,
                    "error": {
                        "code": -32601,
                        "message": f"Fulcrum does not support server request {method}; use configured non-interactive permissions",
                    },
                },
                separators=(",", ":"),
            )
        )

    async def list_models(self) -> list[dict[str, Any]]:
        result = await self.request("model/list", {"includeHidden": True})
        data = result.get("data")
        return (
            [item for item in data if isinstance(item, dict)]
            if isinstance(data, list)
            else []
        )

    async def list_projects(self) -> list[dict[str, Any]]:
        result = await self.request("project/list", {"limit": 100})
        data = result.get("data")
        return (
            [item for item in data if isinstance(item, dict)]
            if isinstance(data, list)
            else []
        )

    async def create_thread(
        self,
        *,
        cwd: str,
        workspace_root: str,
        model: str,
        project_id: str | None,
        base_instructions: str,
        permissions: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "cwd": cwd,
            "model": model,
            "projectId": project_id,
            "baseInstructions": base_instructions,
            "runtimeWorkspaceRoots": [workspace_root],
        }
        if permissions is not None:
            params["permissions"] = permissions
        result = await self.request("thread/start", params)
        thread = result.get("thread")
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
            raise AppServerError("thread/start returned no native thread ID")
        return result

    async def list_threads(
        self, *, cwd: str, project_id: str | None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "cwd": [cwd],
            "limit": 20,
            "sortKey": "created_at",
            "sortDirection": "desc",
            "useStateDbOnly": True,
        }
        if project_id is not None:
            params["projectId"] = project_id
        result = await self.request("thread/list", params)
        data = result.get("data")
        return (
            [thread for thread in data if isinstance(thread, dict)]
            if isinstance(data, list)
            else []
        )

    async def thread_is_listed(self, thread_id: str) -> bool:
        """Return whether an active thread is discoverable by history clients."""

        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            params: dict[str, Any] = {
                "limit": 100,
                "sortKey": "created_at",
                "sortDirection": "desc",
                "archived": False,
            }
            if cursor is not None:
                params["cursor"] = cursor
            result = await self.request("thread/list", params)
            data = result.get("data")
            if isinstance(data, list) and any(
                isinstance(thread, dict) and thread.get("id") == thread_id
                for thread in data
            ):
                return True
            next_cursor = result.get("nextCursor")
            if (
                not isinstance(next_cursor, str)
                or not next_cursor
                or next_cursor in seen_cursors
            ):
                return False
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    async def set_name(self, thread_id: str, name: str) -> None:
        await self.request("thread/name/set", {"threadId": thread_id, "name": name})

    async def read_thread(
        self, thread_id: str, *, include_turns: bool = True
    ) -> dict[str, Any]:
        for attempt in range(5):
            try:
                result = await self.request(
                    "thread/read",
                    {"threadId": thread_id, "includeTurns": include_turns},
                )
                break
            except AppServerError as error:
                if "rollout at" not in str(error) or "is empty" not in str(error):
                    raise
                if attempt == 4:
                    raise
                await asyncio.sleep(0.05 * (attempt + 1))
        thread = result.get("thread")
        if not isinstance(thread, dict):
            raise AppServerError(f"thread/read returned no thread for {thread_id}")
        return thread

    async def resume_thread(self, thread_id: str) -> dict[str, Any]:
        return await self.request(
            "thread/resume", {"threadId": thread_id, "excludeTurns": True}
        )

    async def configure_thread(
        self,
        thread_id: str,
        *,
        cwd: str,
        workspace_root: str,
        model: str,
        effort: str,
    ) -> None:
        await self.request(
            "thread/settings/update",
            {
                "threadId": thread_id,
                "cwd": cwd,
                "runtimeWorkspaceRoots": [workspace_root],
                "model": model,
                "effort": effort,
                "summary": "concise",
                "collaborationMode": {
                    "mode": "default",
                    "settings": {
                        "model": model,
                        "reasoning_effort": effort,
                        "developer_instructions": None,
                    },
                },
            },
        )

    async def start_turn(
        self,
        thread_id: str,
        prompt: str,
        *,
        cwd: str,
        workspace_root: str,
        model: str,
        effort: str,
        correlation: str | None = None,
    ) -> str:
        await self.configure_thread(
            thread_id,
            cwd=cwd,
            workspace_root=workspace_root,
            model=model,
            effort=effort,
        )
        result = await self.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
                "cwd": cwd,
                "runtimeWorkspaceRoots": [workspace_root],
                "model": model,
                "effort": effort,
                "summary": "concise",
                "turnTrigger": "fulcrum",
                "clientUserMessageId": correlation,
            },
        )
        turn = result.get("turn")
        if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
            raise AppServerError("turn/start returned no native turn ID")
        return str(turn["id"])

    async def interrupt(self, thread_id: str, turn_id: str) -> None:
        await self.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    async def archive(self, thread_id: str) -> None:
        await self.request("thread/archive", {"threadId": thread_id})

    async def unarchive(self, thread_id: str) -> None:
        await self.request("thread/unarchive", {"threadId": thread_id})


def thread_facts(thread: dict[str, Any]) -> dict[str, Any]:
    """Derive dispatch facts without assuming notification ordering."""

    turns = thread.get("turns")
    turn_list = turns if isinstance(turns, list) else []
    last = turn_list[-1] if turn_list and isinstance(turn_list[-1], dict) else None
    status = thread.get("status")
    status_name = status.get("type") if isinstance(status, dict) else status
    helpers_terminal = True
    if last is not None:
        for item in (
            last.get("items", []) if isinstance(last.get("items"), list) else []
        ):
            if not isinstance(item, dict) or item.get("type") != "collabAgentToolCall":
                continue
            states = item.get("agentsStates") or item.get("agents_states") or {}
            if isinstance(states, dict):
                helpers_terminal = all(
                    isinstance(value, dict)
                    and value.get("status")
                    in {"completed", "errored", "interrupted", "shutdown", "notFound"}
                    for value in states.values()
                )
    terminal = last is None or last.get("status") in {
        "completed",
        "failed",
        "interrupted",
    }
    return {
        "runtime_status": status_name,
        "last_turn_id": last.get("id") if last else None,
        "last_turn_status": last.get("status") if last else None,
        "last_turn_terminal": terminal,
        "helpers_terminal": helpers_terminal,
        "archived": bool(thread.get("archived")),
        "can_start": terminal and status_name == "idle" and helpers_terminal,
    }
