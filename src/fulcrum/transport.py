"""Small asynchronous adapter for the installed Codex app-server protocol."""

from __future__ import annotations

import asyncio
import json
import re
from collections import deque
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from websockets.asyncio.client import ClientConnection, connect


class AppServerError(RuntimeError):
    """The shared Codex runtime is unavailable or contradicted expectations."""

    def __init__(
        self,
        message: str,
        *,
        category: str = "unavailable",
        uncertain: bool = False,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.uncertain = uncertain


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class RuntimeEvent:
    method: str
    params: Mapping[str, Any]
    request_id: str | None
    observed_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "params": dict(self.params),
            "request_id": self.request_id,
            "observed_at": self.observed_at,
        }


EventHandler = Callable[[str, dict[str, Any]], Coroutine[Any, Any, None]]
MAX_APP_SERVER_FRAME_BYTES = 64 * 1024 * 1024


def _critical_event(event: RuntimeEvent) -> bool:
    if event.request_id is not None:
        return True
    method = event.method.lower()
    return any(
        token in method
        for token in (
            "completed",
            "failed",
            "error",
            "approval",
            "requestuserinput",
            "request_user_input",
            "disconnected",
            "requests-lost",
        )
    )


def _coalesce_key(event: RuntimeEvent) -> tuple[str, str, str] | None:
    method = event.method.lower()
    if not any(
        token in method for token in ("status", "updated", "tokenusage", "ratelimits")
    ):
        return None
    thread_id = event.params.get("threadId") or event.params.get("thread_id") or ""
    turn_id = event.params.get("turnId") or event.params.get("turn_id") or ""
    return event.method, str(thread_id), str(turn_id)


class _BoundedEventQueue:
    """Event-loop-local buffer that favors requests and terminal evidence."""

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("event capacity must be positive")
        self.capacity = capacity
        self.items: deque[RuntimeEvent] = deque()
        self.available = asyncio.Event()

    def put(self, event: RuntimeEvent) -> bool:
        """Enqueue and report whether an observation was discarded."""

        key = _coalesce_key(event)
        if key is not None:
            for index, retained in enumerate(self.items):
                if _coalesce_key(retained) == key:
                    del self.items[index]
                    break
        dropped = False
        if len(self.items) >= self.capacity:
            dropped = True
            if _critical_event(event):
                for index, retained in enumerate(self.items):
                    if not _critical_event(retained):
                        del self.items[index]
                        break
                else:
                    self.items.popleft()
            else:
                return True
        self.items.append(event)
        self.available.set()
        return dropped

    async def get(self) -> RuntimeEvent:
        while not self.items:
            self.available.clear()
            if self.items:
                break
            await self.available.wait()
        event = self.items.popleft()
        if not self.items:
            self.available.clear()
        return event


class Transport:
    """One initialized JSON-RPC client with an always-running event reader."""

    def __init__(
        self,
        endpoint: str,
        *,
        event_handler: EventHandler | None = None,
        event_capacity: int = 1024,
        reconnect_waits: Sequence[float] = (2.0, 10.0, 30.0),
    ) -> None:
        self.endpoint = endpoint
        self.event_handler = event_handler
        self.websocket: ClientConnection | None = None
        self.reader_task: asyncio.Task[None] | None = None
        self.pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.request_id = 0
        self.ready = False
        self.initialize_result: dict[str, Any] | None = None
        self.event_queue = _BoundedEventQueue(event_capacity)
        self.handler_queue = _BoundedEventQueue(event_capacity)
        self.pending_server_requests: dict[str, RuntimeEvent] = {}
        self.event_overflowed = False
        self.handler_worker: asyncio.Task[None] | None = None
        self.reconnect_task: asyncio.Task[None] | None = None
        self.connect_lock = asyncio.Lock()
        self.closing = False
        if not reconnect_waits or any(value < 0 for value in reconnect_waits):
            raise ValueError("reconnect waits must be nonnegative and nonempty")
        self.reconnect_waits: tuple[float, ...] = tuple(reconnect_waits)

    async def connect(self) -> None:
        self.closing = False
        async with self.connect_lock:
            if self.websocket is not None and self.ready:
                return
            try:
                self.websocket = await connect(
                    self.endpoint,
                    open_timeout=10,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=MAX_APP_SERVER_FRAME_BYTES,
                )
            except Exception as error:
                self.websocket = None
                raise AppServerError(
                    f"cannot connect to shared app-server at {self.endpoint}: {error}"
                ) from error
            self.reader_task = asyncio.create_task(
                self._reader(), name="codex-runtime-reader"
            )
            if self.event_handler is not None and self.handler_worker is None:
                self.handler_worker = asyncio.create_task(
                    self._dispatch_handlers(), name="codex-runtime-event-handler"
                )
            try:
                self.initialize_result = await self.request(
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
                websocket = self.websocket
                self.websocket = None
                if websocket is not None:
                    await websocket.close()
                raise
            self.ready = True

    async def close(self) -> None:
        self.closing = True
        self.ready = False
        reconnect_task = self.reconnect_task
        self.reconnect_task = None
        if reconnect_task is not None and reconnect_task is not asyncio.current_task():
            reconnect_task.cancel()
            await asyncio.gather(reconnect_task, return_exceptions=True)
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
        handler_worker = self.handler_worker
        self.handler_worker = None
        if handler_worker is not None:
            handler_worker.cancel()
            await asyncio.gather(handler_worker, return_exceptions=True)
        for future in self.pending.values():
            if not future.done():
                future.set_exception(
                    AppServerError(
                        "app-server disconnected while a result may be pending",
                        category="uncertain",
                        uncertain=True,
                    )
                )
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
        try:
            await websocket.send(
                json.dumps(
                    {"id": identifier, "method": method, "params": params},
                    separators=(",", ":"),
                )
            )
            return await asyncio.wait_for(future, timeout)
        except TimeoutError as error:
            raise AppServerError(
                f"app-server request {method} timed out; result is uncertain",
                category="uncertain",
                uncertain=True,
            ) from error
        except AppServerError:
            raise
        except Exception as error:
            raise AppServerError(
                f"app-server request {method} lost its connection; result is uncertain",
                category="uncertain",
                uncertain=True,
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
                        rpc_error = message["error"]
                        code = (
                            rpc_error.get("code")
                            if isinstance(rpc_error, dict)
                            else None
                        )
                        category = "unsupported" if code == -32601 else "rejected"
                        future.set_exception(
                            AppServerError(
                                f"app-server error: {rpc_error}", category=category
                            )
                        )
                    else:
                        result = message.get("result")
                        future.set_result(
                            result if isinstance(result, dict) else {"value": result}
                        )
                elif identifier is not None and isinstance(method, str):
                    params = message.get("params")
                    event = RuntimeEvent(
                        method=method,
                        params=params if isinstance(params, dict) else {},
                        request_id=str(identifier),
                        observed_at=_now(),
                    )
                    self.pending_server_requests[str(identifier)] = event
                    self._enqueue(event)
                elif isinstance(method, str):
                    params = message.get("params")
                    if method == "serverRequest/resolved" and isinstance(params, dict):
                        resolved_id = params.get("requestId")
                        if resolved_id is not None:
                            self.pending_server_requests.pop(str(resolved_id), None)
                    self._enqueue(
                        RuntimeEvent(
                            method=method,
                            params=params if isinstance(params, dict) else {},
                            request_id=None,
                            observed_at=_now(),
                        )
                    )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            reason = str(error)
        else:
            reason = "app-server connection closed"
        self.ready = False
        if self.websocket is websocket:
            self.websocket = None
        if not self.closing:
            lost = sorted(self.pending_server_requests)
            self.pending_server_requests.clear()
            if lost:
                self._enqueue(
                    RuntimeEvent(
                        method="fulcrum/runtime/requests-lost",
                        params={"request_ids": lost, "reconcile_required": True},
                        request_id=None,
                        observed_at=_now(),
                    )
                )
            self._enqueue(
                RuntimeEvent(
                    method="fulcrum/runtime/disconnected",
                    params={"error": reason},
                    request_id=None,
                    observed_at=_now(),
                )
            )
        for future in self.pending.values():
            if not future.done():
                future.set_exception(
                    AppServerError(
                        f"app-server connection lost: {reason}; result is uncertain",
                        category="uncertain",
                        uncertain=True,
                    )
                )
        if not self.closing and (
            self.reconnect_task is None or self.reconnect_task.done()
        ):
            self.reconnect_task = asyncio.create_task(
                self._reconnect(), name="codex-runtime-reconnect"
            )

    def _enqueue(self, event: RuntimeEvent) -> None:
        self.event_overflowed = self.event_queue.put(event) or self.event_overflowed
        if self.event_handler is not None:
            self.event_overflowed = (
                self.handler_queue.put(event) or self.event_overflowed
            )

    async def _dispatch_handlers(self) -> None:
        handler = self.event_handler
        assert handler is not None
        while True:
            event = await self.handler_queue.get()
            try:
                await handler(event.method, dict(event.params))
            except asyncio.CancelledError:
                raise
            except Exception:
                self.event_overflowed = True

    async def _reconnect(self) -> None:
        attempt = 0
        while not self.closing and self.websocket is None:
            await asyncio.sleep(
                self.reconnect_waits[min(attempt, len(self.reconnect_waits) - 1)]
            )
            if self.closing or self.websocket is not None:
                return
            try:
                await self.connect()
            except AppServerError:
                attempt += 1
                continue
            self._enqueue(
                RuntimeEvent(
                    method="fulcrum/runtime/reconnected",
                    params={"attempts": attempt + 1},
                    request_id=None,
                    observed_at=_now(),
                )
            )
            return

    async def events(self) -> AsyncIterator[RuntimeEvent]:
        while True:
            if self.event_overflowed:
                self.event_overflowed = False
                yield RuntimeEvent(
                    method="fulcrum/runtime/event-overflow",
                    params={"reconcile_required": True},
                    request_id=None,
                    observed_at=_now(),
                )
            yield await self.event_queue.get()

    async def respond_server_request(
        self, request_id: str, response: Mapping[str, Any]
    ) -> None:
        event = self.pending_server_requests.get(request_id)
        websocket = self.websocket
        if event is None:
            raise AppServerError(
                f"native request {request_id} is not pending", category="rejected"
            )
        if websocket is None:
            raise AppServerError("app-server is not connected")
        native_id: int | str = int(request_id) if request_id.isdigit() else request_id
        await websocket.send(
            json.dumps(
                {"id": native_id, "result": dict(response)}, separators=(",", ":")
            )
        )
        self.pending_server_requests.pop(request_id, None)
