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

from fulcrum.transport import AppServerError, RuntimeEvent, Transport, _now


@dataclass(frozen=True)
class RuntimeCapabilities:
    available: bool
    endpoint: str
    methods: tuple[str, ...]
    models: Mapping[str, tuple[str, ...]]
    gaps: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "endpoint": self.endpoint,
            "methods": list(self.methods),
            "models": {model: list(efforts) for model, efforts in self.models.items()},
            "gaps": list(self.gaps),
        }


@dataclass(frozen=True)
class TaskSpec:
    creation_cwd: str
    cwd: str
    project_id: str | None
    workspace_roots: tuple[str, ...]
    title: str
    model: str
    effort: str
    permissions: str | None = None
    developer_instructions: str | None = None


@dataclass(frozen=True)
class TaskFacts:
    id: str
    title: str | None
    cwd: str | None
    project_id: str | None
    workspace_roots: tuple[str, ...]
    archived: bool
    exists: bool
    loaded: bool | None
    runtime_status: str | None
    active_turn: str | None
    last_turn: Mapping[str, Any] | None
    pending_requests: tuple[Mapping[str, Any], ...]
    observed_at: str
    gaps: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        last_turn = dict(self.last_turn) if self.last_turn else None
        if last_turn is not None:
            items = last_turn.pop("items", None)
            if isinstance(items, list):
                last_turn["item_count"] = len(items)
        return {
            "id": self.id,
            "title": self.title,
            "cwd": self.cwd,
            "project_id": self.project_id,
            "workspace_roots": list(self.workspace_roots),
            "archived": self.archived,
            "exists": self.exists,
            "loaded": self.loaded,
            "runtime_status": self.runtime_status,
            "active_turn": self.active_turn,
            # Full turn items are native output, not task-state facts. They can
            # grow without bound and are available through ``task output``.
            "last_turn": last_turn,
            "pending_requests": [dict(item) for item in self.pending_requests],
            "observed_at": self.observed_at,
            "gaps": list(self.gaps),
        }


@dataclass(frozen=True)
class TurnInput:
    text: str
    cwd: str
    workspace_roots: tuple[str, ...]
    model: str
    effort: str
    operation_id: str
    ownership_operation: str | None
    developer_instructions: str | None = None


@dataclass(frozen=True)
class TurnFacts:
    id: str
    thread_id: str
    state: str
    operation_id: str | None
    completed: bool
    error: Mapping[str, Any] | None
    tools: tuple[Mapping[str, Any], ...]
    usage: Mapping[str, Any] | None
    observed_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "thread_id": self.thread_id,
            "state": self.state,
            "operation_id": self.operation_id,
            "completed": self.completed,
            "error": dict(self.error) if self.error else None,
            "tools": [dict(item) for item in self.tools],
            "usage": dict(self.usage) if self.usage else None,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True)
class ReleaseFacts:
    thread_id: str
    status: str
    active_terminals: tuple[Mapping[str, Any], ...]
    observed_at: str
    gaps: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "status": self.status,
            "active_terminals": [dict(item) for item in self.active_terminals],
            "observed_at": self.observed_at,
            "gaps": list(self.gaps),
        }


@dataclass(frozen=True)
class ResourceFacts:
    loaded_count: int | None
    active_count: int | None
    loaded_ids: tuple[str, ...]
    fd_soft_limit: int | None
    fd_usage: int | None
    overloaded: bool | None
    observed_at: str
    gaps: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "loaded_count": self.loaded_count,
            "active_count": self.active_count,
            "loaded_ids": list(self.loaded_ids),
            "fd_soft_limit": self.fd_soft_limit,
            "fd_usage": self.fd_usage,
            "overloaded": self.overloaded,
            "observed_at": self.observed_at,
            "gaps": list(self.gaps),
        }


class Runtime(Protocol):
    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def capabilities(self) -> RuntimeCapabilities: ...
    async def find_projects(self, root: str) -> list[dict[str, Any]]: ...
    async def ensure_project(
        self, *, name: str, root: str, operation_id: str
    ) -> dict[str, Any]: ...
    async def delete_project(self, project_id: str) -> dict[str, Any]: ...
    async def create_task(self, spec: TaskSpec) -> TaskFacts: ...
    async def find_tasks(self, creation_cwd: str) -> list[TaskFacts]: ...
    async def inspect_task(self, thread_id: str) -> TaskFacts: ...
    async def configure_task(self, thread_id: str, spec: TaskSpec) -> TaskFacts: ...
    async def start_turn(self, thread_id: str, input: TurnInput) -> TurnFacts: ...
    async def find_turn(
        self, thread_id: str, operation_id: str
    ) -> TurnFacts | None: ...
    async def inspect_turn(self, thread_id: str, turn_id: str) -> TurnFacts | None: ...
    async def interrupt(self, thread_id: str, turn_id: str) -> TurnFacts: ...
    async def respond(
        self, thread_id: str, request_id: str, response: dict[str, Any]
    ) -> None: ...
    async def output(
        self,
        thread_id: str,
        *,
        turn_id: str | None,
        limit: int,
        cursor: str | None,
        max_bytes: int,
    ) -> Mapping[str, Any]: ...
    async def terminals(
        self, thread_id: str, *, limit: int, cursor: str | None
    ) -> Mapping[str, Any]: ...
    async def terminate_terminal(
        self, thread_id: str, process_id: str
    ) -> Mapping[str, Any]: ...
    async def release(self, thread_id: str) -> ReleaseFacts: ...
    async def archive(self, thread_id: str) -> TaskFacts: ...
    async def unarchive(self, thread_id: str) -> TaskFacts: ...
    async def delete(self, thread_id: str) -> TaskFacts: ...
    async def resources(self) -> ResourceFacts: ...
    def events(self) -> AsyncIterator[RuntimeEvent]: ...


EventHandler = Callable[[str, dict[str, Any]], Coroutine[Any, Any, None]]
SOURCE_KINDS: tuple[str, ...] = (
    "cli",
    "vscode",
    "exec",
    "appServer",
    "subAgent",
    "subAgentReview",
    "subAgentCompact",
    "subAgentThreadSpawn",
    "subAgentOther",
    "unknown",
)
MAX_APP_SERVER_FRAME_BYTES = 64 * 1024 * 1024
# Desktop may keep turn/start open while the accepted turn is already running.
# Bound only that acknowledgement; callers recover by the exact operation marker.
TURN_START_ACK_TIMEOUT_SECONDS = 2.0


class CodexRuntime(Transport):
    """Replaceable typed methods over the resident raw connection."""

    async def list_models(self) -> list[dict[str, Any]]:
        return await self._paged("model/list", {"includeHidden": True})

    async def list_projects(self) -> list[dict[str, Any]]:
        return await self._paged("project/list", {})

    async def create_project(
        self,
        *,
        name: str,
        roots: Sequence[str],
        idempotency_key: str,
        metadata: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        return await self.request(
            "project/create",
            {
                "name": name,
                "roots": [{"path": root} for root in roots],
                "idempotencyKey": idempotency_key,
                "metadata": dict(metadata) if metadata is not None else None,
            },
        )

    async def delete_project(self, project_id: str) -> dict[str, Any]:
        return await self.request("project/delete", {"projectId": project_id})

    async def _paged(
        self, method: str, params: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        cursor: str | None = None
        seen: set[str] = set()
        items: list[dict[str, Any]] = []
        while True:
            page = {**params, "limit": 100}
            if cursor is not None:
                page["cursor"] = cursor
            result = await self.request(method, page)
            data = result.get("data")
            if isinstance(data, list):
                items.extend(item for item in data if isinstance(item, dict))
            next_cursor = result.get("nextCursor")
            if (
                not isinstance(next_cursor, str)
                or not next_cursor
                or next_cursor in seen
            ):
                return items
            seen.add(next_cursor)
            cursor = next_cursor

    async def create_thread(
        self,
        *,
        cwd: str,
        workspace_root: str,
        model: str,
        project_id: str | None,
        permissions: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "cwd": cwd,
            "model": model,
            "projectId": project_id,
            "runtimeWorkspaceRoots": [workspace_root],
        }
        if permissions is not None:
            params["permissions"] = permissions
        result = await self.request("thread/start", params)
        thread = result.get("thread")
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
            raise AppServerError("thread/start returned no native thread ID")
        if project_id is not None and thread.get("projectId") != project_id:
            updated = await self.assign_thread_project(thread["id"], project_id)
            result["thread"] = updated["thread"]
        return result

    async def assign_thread_project(
        self, thread_id: str, project_id: str
    ) -> dict[str, Any]:
        result = await self.request(
            "thread/metadata/update",
            {"threadId": thread_id, "projectId": project_id},
        )
        thread = result.get("thread")
        if not isinstance(thread, dict) or thread.get("projectId") != project_id:
            raise AppServerError(
                f"thread {thread_id} did not retain Codex project {project_id}"
            )
        return result

    async def list_threads(
        self, *, cwd: str, project_id: str | None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "cwd": [cwd],
            "sortKey": "created_at",
            "sortDirection": "desc",
            "useStateDbOnly": True,
            "sourceKinds": list(SOURCE_KINDS),
        }
        if project_id is not None:
            params["projectId"] = project_id
        return await self._paged("thread/list", params)

    async def list_all_threads(self, *, archived: bool = False) -> list[dict[str, Any]]:
        return await self._paged(
            "thread/list",
            {
                "archived": archived,
                "sortKey": "created_at",
                "sortDirection": "desc",
                "useStateDbOnly": True,
                "sourceKinds": list(SOURCE_KINDS),
            },
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
        try:
            await self.request("thread/name/set", {"threadId": thread_id, "name": name})
        except AppServerError as error:
            if not _unloaded_thread_error(error):
                raise
            await self.resume_thread(thread_id)
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
                if not _empty_history_error(error):
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
        developer_instructions: str | None = None,
    ) -> None:
        params = {
            "threadId": thread_id,
            "cwd": cwd,
            "model": model,
            "effort": effort,
            "summary": "concise",
            "collaborationMode": {
                "mode": "default",
                "settings": {
                    "model": model,
                    "reasoning_effort": effort,
                    "developer_instructions": developer_instructions,
                },
            },
        }
        try:
            await self.request("thread/settings/update", params)
        except AppServerError as error:
            if not _unloaded_thread_error(error):
                raise
            await self.resume_thread(thread_id)
            await self.request("thread/settings/update", params)

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
        developer_instructions: str | None = None,
    ) -> str:
        await self.configure_thread(
            thread_id,
            cwd=cwd,
            workspace_root=workspace_root,
            model=model,
            effort=effort,
            developer_instructions=developer_instructions,
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
            timeout=TURN_START_ACK_TIMEOUT_SECONDS,
        )
        turn = result.get("turn")
        if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
            raise AppServerError("turn/start returned no native turn ID")
        return str(turn["id"])

    async def interrupt(self, thread_id: str, turn_id: str) -> None:
        await self.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    async def steer(
        self, thread_id: str, turn_id: str, notice: str, *, correlation: str
    ) -> None:
        """Append an emergency notice only to the exact still-active turn."""

        await self.request(
            "turn/steer",
            {
                "threadId": thread_id,
                "expectedTurnId": turn_id,
                "input": [{"type": "text", "text": notice}],
                "clientUserMessageId": correlation,
            },
        )

    async def archive(self, thread_id: str) -> None:
        await self.request("thread/archive", {"threadId": thread_id})
        await self.unsubscribe(thread_id)

    async def unsubscribe(self, thread_id: str) -> str:
        result = await self.request("thread/unsubscribe", {"threadId": thread_id})
        if result.get("status") not in {
            "notLoaded",
            "notSubscribed",
            "unsubscribed",
        }:
            raise AppServerError(
                f"thread/unsubscribe returned an invalid status for {thread_id}"
            )
        return str(result["status"])

    async def unarchive(self, thread_id: str) -> None:
        await self.request("thread/unarchive", {"threadId": thread_id})

    async def delete(self, thread_id: str) -> None:
        await self.request("thread/delete", {"threadId": thread_id})

    async def loaded_threads(self) -> list[str]:
        result = await self.request("thread/loaded/list", {})
        data = result.get("data")
        return [str(item) for item in data] if isinstance(data, list) else []

    async def background_terminals(self, thread_id: str) -> list[dict[str, Any]]:
        cursor: str | None = None
        terminals: list[dict[str, Any]] = []
        while True:
            result = await self.background_terminals_page(
                thread_id, limit=100, cursor=cursor
            )
            terminals.extend(result["items"])
            cursor = result["next_cursor"]
            if cursor is None:
                return terminals

    async def background_terminals_page(
        self, thread_id: str, *, limit: int, cursor: str | None
    ) -> dict[str, Any]:
        params = {"threadId": thread_id, "limit": limit, "cursor": cursor}
        try:
            result = await self.request("thread/backgroundTerminals/list", params)
        except AppServerError as error:
            if not _unloaded_thread_error(error):
                raise
            await self.resume_thread(thread_id)
            result = await self.request("thread/backgroundTerminals/list", params)
        data = result.get("data") or result.get("terminals")
        return {
            "items": (
                [dict(item) for item in data if isinstance(item, Mapping)]
                if isinstance(data, list)
                else []
            ),
            "next_cursor": (
                str(result["nextCursor"])
                if isinstance(result.get("nextCursor"), str)
                and result.get("nextCursor")
                else None
            ),
        }

    async def terminate_background_terminal(
        self, thread_id: str, process_id: str
    ) -> bool:
        result = await self.request(
            "thread/backgroundTerminals/terminate",
            {"threadId": thread_id, "processId": process_id},
        )
        if not isinstance(result.get("terminated"), bool):
            raise AppServerError(
                "thread/backgroundTerminals/terminate returned no termination fact",
                category="uncertain",
                uncertain=True,
            )
        return bool(result["terminated"])

    async def clean_background_terminals(self, thread_id: str) -> None:
        await self.request("thread/backgroundTerminals/clean", {"threadId": thread_id})

    async def thread_items_page(
        self,
        thread_id: str,
        *,
        turn_id: str | None,
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        result = await self.request(
            "thread/items/list",
            {
                "threadId": thread_id,
                "turnId": turn_id,
                "limit": limit,
                "cursor": cursor,
                "sortDirection": "asc",
            },
        )
        data = result.get("data")
        return {
            "items": (
                [dict(item) for item in data if isinstance(item, Mapping)]
                if isinstance(data, list)
                else []
            ),
            "next_cursor": (
                str(result["nextCursor"])
                if isinstance(result.get("nextCursor"), str)
                and result.get("nextCursor")
                else None
            ),
        }


class AppServerRuntime:
    """Typed policy-free view over one initialized app-server transport."""

    METHODS: tuple[str, ...] = (
        "model/list",
        "project/list",
        "project/create",
        "thread/start",
        "thread/list",
        "thread/read",
        "thread/resume",
        "thread/name/set",
        "thread/metadata/update",
        "thread/settings/update",
        "thread/loaded/list",
        "turn/start",
        "turn/interrupt",
        "thread/unsubscribe",
        "thread/archive",
        "thread/unarchive",
        "thread/delete",
        "thread/backgroundTerminals/list",
        "thread/backgroundTerminals/terminate",
        "thread/backgroundTerminals/clean",
        "thread/items/list",
    )

    def __init__(self, endpoint: str, *, transport: CodexRuntime | None = None) -> None:
        self.endpoint = endpoint
        self.transport: CodexRuntime = transport or CodexRuntime(endpoint)
        self.project_evidence: dict[str, str] = {}

    async def connect(self) -> None:
        await self.transport.connect()

    async def close(self) -> None:
        await self.transport.close()

    async def capabilities(self) -> RuntimeCapabilities:
        models = await self.transport.list_models()
        supported: dict[str, tuple[str, ...]] = {}
        for model in models:
            identifier = model.get("model") or model.get("id")
            if not isinstance(identifier, str):
                continue
            efforts = model.get("supportedReasoningEfforts")
            supported[identifier] = (
                tuple(
                    str(item.get("reasoningEffort"))
                    for item in efforts
                    if isinstance(item, Mapping) and item.get("reasoningEffort")
                )
                if isinstance(efforts, list)
                else ()
            )
        return RuntimeCapabilities(
            available=True,
            endpoint=self.endpoint,
            methods=self.METHODS,
            models=supported,
        )

    async def find_projects(self, root: str) -> list[dict[str, Any]]:
        expected = _canonical_path(root)
        matches: list[dict[str, Any]] = []
        for project in await self.transport.list_projects():
            roots = project.get("roots")
            if not isinstance(roots, list):
                continue
            if any(
                isinstance(item, Mapping) and _same_path(item.get("path"), expected)
                for item in roots
            ):
                matches.append(project)
        return matches

    async def ensure_project(
        self, *, name: str, root: str, operation_id: str
    ) -> dict[str, Any]:
        matches = await self.find_projects(root)
        if len(matches) > 1:
            raise AppServerError(
                f"multiple Codex projects contain exact root {_canonical_path(root)}",
                category="uncertain",
                uncertain=True,
            )
        if matches:
            return matches[0]
        try:
            result = await self.transport.create_project(
                name=name,
                roots=(_canonical_path(root),),
                idempotency_key=operation_id,
                metadata={"fulcrum_operation": operation_id},
            )
        except AppServerError as error:
            if not error.uncertain and error.category not in {
                "unavailable",
                "transient",
            }:
                raise
            matches = await self.find_projects(root)
            if len(matches) != 1:
                raise
            return matches[0]
        project = result.get("project")
        if not isinstance(project, dict) or not isinstance(project.get("id"), str):
            raise AppServerError(
                "project/create returned no native project ID",
                category="uncertain",
                uncertain=True,
            )
        roots = project.get("roots")
        if not isinstance(roots, list) or not any(
            isinstance(item, Mapping)
            and _same_path(item.get("path"), _canonical_path(root))
            for item in roots
        ):
            raise AppServerError(
                "project/create returned an unverified root",
                category="uncertain",
                uncertain=True,
            )
        return project

    async def delete_project(self, project_id: str) -> dict[str, Any]:
        matches = [
            project
            for project in await self.transport.list_projects()
            if str(project.get("id") or project.get("projectId") or "") == project_id
        ]
        if not matches:
            return {"id": project_id, "exists": False, "deleted": False}
        await self.transport.delete_project(project_id)
        retained = [
            project
            for project in await self.transport.list_projects()
            if str(project.get("id") or project.get("projectId") or "") == project_id
        ]
        if retained:
            raise AppServerError(
                f"project {project_id} still exists after project/delete",
                category="uncertain",
                uncertain=True,
            )
        return {"id": project_id, "exists": False, "deleted": True}

    async def create_task(self, spec: TaskSpec) -> TaskFacts:
        result = await self.transport.create_thread(
            cwd=_canonical_path(spec.creation_cwd),
            workspace_root=spec.workspace_roots[0],
            model=spec.model,
            project_id=spec.project_id,
            permissions=spec.permissions,
        )
        thread = dict(result["thread"])
        thread_id = str(thread["id"])
        returned_project = thread.get("projectId")
        if isinstance(returned_project, str) and returned_project:
            self.project_evidence[thread_id] = returned_project
        await self.transport.set_name(thread_id, spec.title)
        thread["name"] = spec.title
        loaded = set(await self.transport.loaded_threads())
        return _task_facts(
            thread,
            loaded=thread_id in loaded,
            pending=self._pending_for(thread_id),
        )

    async def find_tasks(self, creation_cwd: str) -> list[TaskFacts]:
        canonical_cwd = _canonical_path(creation_cwd)
        active = await self.transport.list_threads(cwd=canonical_cwd, project_id=None)
        loaded_ids = await self.transport.loaded_threads()
        loaded_threads: list[dict[str, Any]] = []
        for thread_id in loaded_ids:
            try:
                thread = await self.transport.read_thread(
                    thread_id, include_turns=False
                )
            except AppServerError:
                continue
            if _same_path(thread.get("cwd"), canonical_cwd):
                loaded_threads.append(thread)
        archived = [
            item
            for item in await self.transport.list_all_threads(archived=True)
            if _same_path(item.get("cwd"), canonical_cwd)
        ]
        loaded = set(loaded_ids)
        indexed: dict[str, dict[str, Any]] = {}
        for item in active + loaded_threads + archived:
            identifier = item.get("id")
            if isinstance(identifier, str) and _same_path(
                item.get("cwd"), canonical_cwd
            ):
                indexed[identifier] = item
        return [
            _task_facts(
                item,
                loaded=item.get("id") in loaded,
                pending=self._pending_for(str(item.get("id"))),
            )
            for item in indexed.values()
        ]

    async def inspect_task(self, thread_id: str) -> TaskFacts:
        gaps: tuple[str, ...] = ()
        try:
            thread = await self.transport.read_thread(thread_id)
        except AppServerError as error:
            if _empty_history_error(error):
                thread = await self.transport.read_thread(
                    thread_id, include_turns=False
                )
                gaps = ("native task has no first rollout",)
            elif not _unloaded_thread_error(error):
                raise
            else:
                # A missing in-memory task is not proof its saved history is
                # gone. Check both inventories before reporting absence.
                for archived in (False, True):
                    listed = await self.transport.list_all_threads(archived=archived)
                    if any(item.get("id") == thread_id for item in listed):
                        raise error
                return TaskFacts(
                    id=thread_id,
                    title=None,
                    cwd=None,
                    project_id=None,
                    workspace_roots=(),
                    archived=False,
                    exists=False,
                    loaded=None,
                    runtime_status=None,
                    active_turn=None,
                    last_turn=None,
                    pending_requests=(),
                    observed_at=_now(),
                    gaps=(str(error),),
                )
        loaded = set(await self.transport.loaded_threads())
        facts = _task_facts(
            thread,
            loaded=thread_id in loaded,
            pending=self._pending_for(thread_id),
        )
        return TaskFacts(**{**facts.__dict__, "gaps": facts.gaps + gaps})

    async def configure_task(self, thread_id: str, spec: TaskSpec) -> TaskFacts:
        await self.transport.configure_thread(
            thread_id,
            cwd=spec.cwd,
            workspace_root=spec.workspace_roots[0],
            model=spec.model,
            effort=spec.effort,
            developer_instructions=spec.developer_instructions,
        )
        thread = await self.transport.read_thread(thread_id, include_turns=False)
        observed_project = thread.get("projectId")
        if spec.project_id is not None and observed_project != spec.project_id:
            if self.project_evidence.get(thread_id) != spec.project_id:
                try:
                    updated = await self.transport.assign_thread_project(
                        thread_id, spec.project_id
                    )
                except AppServerError as error:
                    raise AppServerError(
                        f"thread {thread_id} project attachment is not recoverably verified: {error}",
                        category="uncertain",
                        uncertain=True,
                    ) from error
                assigned = updated.get("thread")
                if isinstance(assigned, Mapping):
                    thread = dict(assigned)
        loaded = set(await self.transport.loaded_threads())
        facts = _task_facts(
            thread,
            loaded=thread_id in loaded,
            pending=self._pending_for(thread_id),
        )
        retained_project = facts.project_id or self.project_evidence.get(thread_id)
        if spec.project_id is not None and retained_project != spec.project_id:
            raise AppServerError(
                f"thread {thread_id} project attachment could not be verified",
                category="uncertain",
                uncertain=True,
            )
        if facts.project_id is None and retained_project is not None:
            facts = TaskFacts(
                **{
                    **facts.__dict__,
                    "project_id": retained_project,
                    "gaps": facts.gaps
                    + (
                        "project attachment is verified by thread/start until the first rollout",
                    ),
                }
            )
        expected_cwd = _canonical_path(spec.cwd)
        if not _same_path(facts.cwd, expected_cwd):
            raise AppServerError(
                f"thread {thread_id} work cwd could not be verified",
                category="uncertain",
                uncertain=True,
            )
        retained_roots = {_canonical_path(item) for item in facts.workspace_roots}
        expected_roots = {_canonical_path(item) for item in spec.workspace_roots}
        if not expected_roots.issubset(retained_roots):
            raise AppServerError(
                f"thread {thread_id} workspace roots could not be verified",
                category="uncertain",
                uncertain=True,
            )
        return facts

    async def start_turn(self, thread_id: str, input: TurnInput) -> TurnFacts:
        marker = _operation_marker(input.operation_id, input.ownership_operation)
        turn_id = await self.transport.start_turn(
            thread_id,
            marker + "\n\n" + input.text,
            cwd=input.cwd,
            workspace_root=input.workspace_roots[0],
            model=input.model,
            effort=input.effort,
            correlation=input.operation_id,
            developer_instructions=input.developer_instructions,
        )
        return TurnFacts(
            id=turn_id,
            thread_id=thread_id,
            state="inProgress",
            operation_id=input.operation_id,
            completed=False,
            error=None,
            tools=(),
            usage=None,
            observed_at=_now(),
        )

    async def find_turn(self, thread_id: str, operation_id: str) -> TurnFacts | None:
        try:
            thread = await self.transport.read_thread(thread_id, include_turns=True)
        except AppServerError as error:
            if _empty_history_error(error):
                return None
            raise
        turns = thread.get("turns")
        if not isinstance(turns, list):
            raise AppServerError(
                f"thread {thread_id} history is unavailable",
                category="uncertain",
                uncertain=True,
            )
        marker = f"FULCRUM_OPERATION={operation_id}"
        for turn in reversed(turns):
            if isinstance(turn, Mapping) and _contains_marker(turn, marker):
                return _turn_facts(thread_id, turn, operation_id=operation_id)
        return None

    async def inspect_turn(self, thread_id: str, turn_id: str) -> TurnFacts | None:
        try:
            thread = await self.transport.read_thread(thread_id, include_turns=True)
        except AppServerError as error:
            if _empty_history_error(error):
                return None
            raise
        turns = thread.get("turns")
        if not isinstance(turns, list):
            raise AppServerError(
                f"thread {thread_id} history is unavailable",
                category="uncertain",
                uncertain=True,
            )
        for turn in turns:
            if isinstance(turn, Mapping) and str(turn.get("id")) == turn_id:
                return _turn_facts(thread_id, turn, operation_id=None)
        return None

    async def interrupt(self, thread_id: str, turn_id: str) -> TurnFacts:
        await self.transport.interrupt(thread_id, turn_id)
        thread = await self.transport.read_thread(thread_id, include_turns=True)
        turns = thread.get("turns")
        if isinstance(turns, list):
            for turn in reversed(turns):
                if isinstance(turn, Mapping) and turn.get("id") == turn_id:
                    return _turn_facts(thread_id, turn, operation_id=None)
        return TurnFacts(
            id=turn_id,
            thread_id=thread_id,
            state="unknown",
            operation_id=None,
            completed=False,
            error=None,
            tools=(),
            usage=None,
            observed_at=_now(),
        )

    async def respond(
        self, thread_id: str, request_id: str, response: dict[str, Any]
    ) -> None:
        event = self.transport.pending_server_requests.get(request_id)
        if event is None or event.params.get("threadId") != thread_id:
            raise AppServerError(
                f"request {request_id} is not pending for {thread_id}",
                category="rejected",
            )
        await self.transport.respond_server_request(request_id, response)

    async def output(
        self,
        thread_id: str,
        *,
        turn_id: str | None,
        limit: int,
        cursor: str | None,
        max_bytes: int,
    ) -> Mapping[str, Any]:
        items: list[dict[str, Any]] = []
        gaps: list[str] = []
        used = 2
        current = cursor
        remaining = limit
        resolved_turn_id = turn_id
        observed_task: TaskFacts | None = None
        if resolved_turn_id is None:
            observed_task = await self.inspect_task(thread_id)
            if observed_task.active_turn is not None:
                resolved_turn_id = observed_task.active_turn
            elif isinstance(observed_task.last_turn, Mapping) and isinstance(
                observed_task.last_turn.get("id"), str
            ):
                resolved_turn_id = str(observed_task.last_turn["id"])
            else:
                return {
                    "thread_id": thread_id,
                    "turn_id": None,
                    "items": [],
                    "next_cursor": None,
                    "observed_at": _now(),
                    "gaps": ["native task has no observable turn"],
                    "bytes": used,
                }

        def append(row: dict[str, Any], next_cursor: str | None) -> bool:
            nonlocal current, remaining, used
            encoded = json.dumps(
                row, separators=(",", ":"), ensure_ascii=False, sort_keys=True
            ).encode("utf-8")
            delimiter = 1 if items else 0
            if used + delimiter + len(encoded) > max_bytes:
                available = max(0, max_bytes - used - delimiter)
                prefix_size = available
                truncated: dict[str, Any]
                while True:
                    preview = encoded[:prefix_size].decode("utf-8", errors="ignore")
                    truncated = {
                        "truncated": True,
                        "original_bytes": len(encoded),
                        "json_prefix": preview,
                    }
                    truncated_size = len(
                        json.dumps(
                            truncated,
                            separators=(",", ":"),
                            ensure_ascii=False,
                            sort_keys=True,
                        ).encode("utf-8")
                    )
                    if truncated_size <= available or prefix_size == 0:
                        break
                    prefix_size = max(0, prefix_size - (truncated_size - available))
                if truncated_size <= available:
                    items.append(truncated)
                    used += delimiter + truncated_size
                    gaps.append("one native output item was truncated at the byte cap")
                else:
                    gaps.append(
                        "one native output item could not fit at the byte cap and was skipped"
                    )
                current = next_cursor
                return False
            items.append(row)
            used += delimiter + len(encoded)
            current = next_cursor
            if remaining > 0:
                remaining -= 1
            return remaining != 0

        while remaining != 0:
            page = await self.transport.thread_items_page(
                thread_id,
                turn_id=resolved_turn_id,
                limit=1,
                cursor=current,
            )
            rows = page["items"]
            next_cursor = page["next_cursor"]
            if not rows:
                if current is None and not items:
                    if observed_task is None:
                        observed_task = await self.inspect_task(thread_id)
                    last_turn = observed_task.last_turn
                    history = (
                        last_turn.get("items")
                        if isinstance(last_turn, Mapping)
                        and last_turn.get("id") == resolved_turn_id
                        else None
                    )
                    if isinstance(history, list) and history:
                        gaps.append(
                            "native item pagination was empty; returned lossy thread history"
                        )
                        for history_item in history:
                            if not isinstance(history_item, Mapping):
                                continue
                            if not append(
                                {
                                    "turnId": resolved_turn_id,
                                    "item": dict(history_item),
                                },
                                None,
                            ):
                                break
                current = next_cursor
                break
            if not append(dict(rows[0]), next_cursor):
                break
            if current is None:
                break
        return {
            "thread_id": thread_id,
            "turn_id": resolved_turn_id,
            "items": items,
            "next_cursor": current,
            "observed_at": _now(),
            "gaps": gaps,
            "bytes": used,
        }

    async def terminals(
        self, thread_id: str, *, limit: int, cursor: str | None
    ) -> Mapping[str, Any]:
        if limit == 0:
            current = cursor
            rows: list[dict[str, Any]] = []
            while True:
                page = await self.transport.background_terminals_page(
                    thread_id, limit=100, cursor=current
                )
                rows.extend(page["items"])
                current = page["next_cursor"]
                if current is None:
                    break
            next_cursor = None
        else:
            page = await self.transport.background_terminals_page(
                thread_id, limit=limit, cursor=cursor
            )
            rows = page["items"]
            next_cursor = page["next_cursor"]
        return {
            "thread_id": thread_id,
            "items": [
                {
                    **dict(item),
                    "terminal_id": item.get("processId"),
                    "status": "running",
                    "output_reference": item.get("itemId"),
                }
                for item in rows
            ],
            "next_cursor": next_cursor,
            "observed_at": _now(),
            "gaps": [],
        }

    async def terminate_terminal(
        self, thread_id: str, process_id: str
    ) -> Mapping[str, Any]:
        terminals = await self.transport.background_terminals(thread_id)
        owned = [item for item in terminals if str(item.get("processId")) == process_id]
        if len(owned) != 1:
            raise AppServerError(
                f"terminal {process_id} is not an exact running terminal owned by {thread_id}",
                category="rejected",
            )
        try:
            terminated = await self.transport.terminate_background_terminal(
                thread_id, process_id
            )
        except AppServerError as error:
            if not error.uncertain:
                raise
            current = await self.transport.background_terminals(thread_id)
            if any(str(item.get("processId")) == process_id for item in current):
                raise
            terminated = True
        current = await self.transport.background_terminals(thread_id)
        still_running = any(
            str(item.get("processId")) == process_id for item in current
        )
        return {
            "thread_id": thread_id,
            "terminal_id": process_id,
            "terminated": terminated and not still_running,
            "still_running": still_running,
            "observed_at": _now(),
        }

    async def release(self, thread_id: str) -> ReleaseFacts:
        task = await self.inspect_task(thread_id)
        if task.active_turn is not None or task.runtime_status not in {
            "idle",
            "completed",
            "failed",
        }:
            raise AppServerError(
                f"thread {thread_id} is not observably idle",
                category="rejected",
            )
        terminals: list[dict[str, Any]] = []
        gaps: list[str] = []
        try:
            terminals = await self.transport.background_terminals(thread_id)
        except AppServerError as error:
            if error.category != "unsupported":
                gaps.append(str(error))
        if not terminals:
            try:
                await self.transport.clean_background_terminals(thread_id)
            except AppServerError as error:
                if error.category != "unsupported":
                    gaps.append(str(error))
        status = await self.transport.unsubscribe(thread_id)
        return ReleaseFacts(
            thread_id=thread_id,
            status=status,
            active_terminals=tuple(terminals),
            observed_at=_now(),
            gaps=tuple(gaps),
        )

    async def archive(self, thread_id: str) -> TaskFacts:
        await self.transport.archive(thread_id)
        facts = await self.inspect_task(thread_id)
        return TaskFacts(**{**facts.__dict__, "archived": True, "loaded": False})

    async def unarchive(self, thread_id: str) -> TaskFacts:
        await self.transport.unarchive(thread_id)
        return await self.inspect_task(thread_id)

    async def delete(self, thread_id: str) -> TaskFacts:
        await self.transport.delete(thread_id)
        return TaskFacts(
            id=thread_id,
            title=None,
            cwd=None,
            project_id=None,
            workspace_roots=(),
            archived=False,
            exists=False,
            loaded=False,
            runtime_status="deleted",
            active_turn=None,
            last_turn=None,
            pending_requests=(),
            observed_at=_now(),
        )

    async def resources(self) -> ResourceFacts:
        loaded = await self.transport.loaded_threads()
        active = 0
        gaps: list[str] = []
        for thread_id in loaded:
            try:
                facts = await self.inspect_task(thread_id)
                if facts.runtime_status not in {"idle", "notLoaded", None}:
                    active += 1
            except AppServerError as error:
                gaps.append(str(error))
        return ResourceFacts(
            loaded_count=len(loaded),
            active_count=active,
            loaded_ids=tuple(loaded),
            fd_soft_limit=None,
            fd_usage=None,
            overloaded=None,
            observed_at=_now(),
            gaps=tuple(gaps),
        )

    async def events(self) -> AsyncIterator[RuntimeEvent]:
        async for event in self.transport.events():
            yield event

    def _pending_for(self, thread_id: str) -> tuple[Mapping[str, Any], ...]:
        return tuple(
            {
                "request_id": event.request_id,
                "method": event.method,
                "params": dict(event.params),
                "observed_at": event.observed_at,
            }
            for event in self.transport.pending_server_requests.values()
            if event.params.get("threadId") == thread_id
        )


def _operation_marker(operation_id: str, ownership_operation: str | None) -> str:
    return (
        f"FULCRUM_OPERATION={operation_id} "
        f"FULCRUM_OWNERSHIP_OPERATION={ownership_operation or 'none'}"
    )


def _canonical_path(value: str) -> str:
    return str(Path(value).resolve(strict=False))


def _same_path(value: Any, expected: str) -> bool:
    return isinstance(value, str) and _canonical_path(value) == expected


def _empty_history_error(error: AppServerError) -> bool:
    message = str(error)
    return ("rollout at" in message and "is empty" in message) or (
        "list_turns is not supported yet" in message
    )


def _unloaded_thread_error(error: AppServerError) -> bool:
    return error.category == "rejected" and any(
        message in str(error).lower()
        for message in ("thread not found:", "thread not loaded:")
    )


def _contains_marker(value: Any, marker: str) -> bool:
    if isinstance(value, str):
        return re.search(re.escape(marker) + r"(?=$|\s)", value) is not None
    if isinstance(value, Mapping):
        return any(_contains_marker(item, marker) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(_contains_marker(item, marker) for item in value)
    return False


def _task_facts(
    thread: Mapping[str, Any],
    *,
    loaded: bool | None,
    pending: tuple[Mapping[str, Any], ...],
) -> TaskFacts:
    thread_id = str(thread.get("id") or "")
    turns = thread.get("turns")
    turn_list = (
        [item for item in turns if isinstance(item, Mapping)]
        if isinstance(turns, list)
        else []
    )
    last = turn_list[-1] if turn_list else None
    status = thread.get("status")
    runtime_status = status.get("type") if isinstance(status, Mapping) else status
    active_turn = None
    if last and str(last.get("status")) not in {"completed", "failed", "interrupted"}:
        active_turn = str(last.get("id")) if last.get("id") else None
    roots = thread.get("runtimeWorkspaceRoots")
    effective_cwd = thread.get("cwd")
    environments = thread.get("environments")
    if isinstance(environments, list):
        for environment in environments:
            if not isinstance(environment, Mapping):
                continue
            environment_cwd = environment.get("cwd")
            if isinstance(environment_cwd, str):
                effective_cwd = environment_cwd
            candidate = environment.get("runtimeWorkspaceRoots")
            if isinstance(candidate, list):
                roots = candidate
            break
    return TaskFacts(
        id=thread_id,
        title=(str(thread.get("name")) if thread.get("name") is not None else None),
        cwd=str(effective_cwd) if effective_cwd is not None else None,
        project_id=(
            str(thread.get("projectId"))
            if thread.get("projectId") is not None
            else None
        ),
        workspace_roots=(
            tuple(str(item) for item in roots) if isinstance(roots, list) else ()
        ),
        archived=bool(thread.get("archived")),
        exists=bool(thread_id),
        loaded=loaded,
        runtime_status=str(runtime_status) if runtime_status is not None else None,
        active_turn=active_turn,
        last_turn=dict(last) if last else None,
        pending_requests=pending,
        observed_at=_now(),
    )


def _turn_facts(
    thread_id: str, turn: Mapping[str, Any], *, operation_id: str | None
) -> TurnFacts:
    status = str(turn.get("status") or "unknown")
    items = turn.get("items")
    tools = (
        tuple(
            dict(item)
            for item in items
            if isinstance(item, Mapping)
            and item.get("type")
            in {
                "commandExecution",
                "mcpToolCall",
                "dynamicToolCall",
                "collabToolCall",
                "collabAgentToolCall",
            }
        )
        if isinstance(items, list)
        else ()
    )
    error = turn.get("error")
    usage = turn.get("usage")
    return TurnFacts(
        id=str(turn.get("id") or ""),
        thread_id=thread_id,
        state=status,
        operation_id=operation_id,
        completed=status in {"completed", "failed", "interrupted"},
        error=dict(error) if isinstance(error, Mapping) else None,
        tools=tools,
        usage=dict(usage) if isinstance(usage, Mapping) else None,
        observed_at=_now(),
    )


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
            if not isinstance(item, dict) or item.get("type") not in {
                "collabToolCall",
                "collabAgentToolCall",
            }:
                continue
            states = item.get("agentsStates") or item.get("agents_states") or {}
            if isinstance(states, dict):
                if states:
                    helpers_terminal = helpers_terminal and all(
                        isinstance(value, dict)
                        and value.get("status")
                        in {
                            "completed",
                            "errored",
                            "interrupted",
                            "shutdown",
                            "notFound",
                        }
                        for value in states.values()
                    )
                else:
                    agent_status = item.get("agentStatus")
                    if isinstance(agent_status, str):
                        helpers_terminal = helpers_terminal and agent_status in {
                            "completed",
                            "errored",
                            "interrupted",
                            "shutdown",
                            "notFound",
                        }
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
