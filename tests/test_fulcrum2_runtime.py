from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

from websockets.asyncio.server import serve

from fulcrum.ledger import Ledger
from fulcrum.runtime import (
    AppServerError,
    AppServerRuntime,
    CodexRuntime,
    RuntimeCapabilities,
    TaskFacts,
    TaskSpec,
    TurnFacts,
)
from fulcrum.runtime_service import _create_and_configure, _start_or_recover


def task_facts(
    identifier: str = "thread-1", *, project_id: str | None = "project-1"
) -> TaskFacts:
    return TaskFacts(
        id=identifier,
        title="Managed task",
        cwd="/work",
        project_id=project_id,
        workspace_roots=("/work",),
        archived=False,
        exists=True,
        loaded=True,
        runtime_status="idle",
        active_turn=None,
        last_turn=None,
        pending_requests=(),
        observed_at="2026-09-14T00:00:00Z",
    )


def task_spec() -> TaskSpec:
    return TaskSpec(
        creation_cwd="/instance/threads/fc-op-1",
        cwd="/work",
        project_id="project-1",
        workspace_roots=("/work",),
        title="Managed task",
        model="luna",
        effort="high",
    )


class RuntimeFailureFixtureTest(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_response_fails_pending_call_as_uncertain(self) -> None:
        async def handler(connection: Any) -> None:
            async for raw in connection:
                message = json.loads(raw)
                if message.get("method") == "initialize":
                    await connection.send(
                        json.dumps({"id": message["id"], "result": {}})
                    )
                elif message.get("method") == "model/list":
                    await connection.send("{")

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            runtime = CodexRuntime(f"ws://127.0.0.1:{port}")
            await runtime.connect()
            with self.assertRaises(AppServerError) as captured:
                await runtime.list_models()
            self.assertTrue(captured.exception.uncertain)
            self.assertEqual(captured.exception.category, "uncertain")
            await runtime.close()

    async def test_unsupported_configuration_is_classified(self) -> None:
        async def handler(connection: Any) -> None:
            async for raw in connection:
                message = json.loads(raw)
                if message.get("method") == "initialize":
                    await connection.send(
                        json.dumps({"id": message["id"], "result": {}})
                    )
                elif message.get("method") == "thread/settings/update":
                    await connection.send(
                        json.dumps(
                            {
                                "id": message["id"],
                                "error": {"code": -32601, "message": "unsupported"},
                            }
                        )
                    )

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            runtime = CodexRuntime(f"ws://127.0.0.1:{port}")
            await runtime.connect()
            with self.assertRaises(AppServerError) as captured:
                await runtime.configure_thread(
                    "thread-1",
                    cwd="/work",
                    workspace_root="/work",
                    model="luna",
                    effort="high",
                )
            self.assertEqual(captured.exception.category, "unsupported")
            self.assertFalse(captured.exception.uncertain)
            await runtime.close()

    async def test_all_idempotent_unsubscribe_outcomes_are_successful(self) -> None:
        for status in ("unsubscribed", "notSubscribed", "notLoaded"):
            runtime = CodexRuntime("ws://unused")
            runtime.request = AsyncMock(return_value={"status": status})
            self.assertEqual(await runtime.unsubscribe("thread-1"), status)


class RuntimeRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_lost_create_response_adopts_exact_cwd_match(self) -> None:
        native = task_facts()
        runtime = AsyncMock()
        runtime.capabilities.return_value = RuntimeCapabilities(
            available=True,
            endpoint="ws://runtime",
            methods=(),
            models={"luna": ("high",)},
        )
        runtime.find_tasks.side_effect = [[], [native]]
        runtime.create_task.side_effect = AppServerError(
            "connection lost", category="uncertain", uncertain=True
        )
        runtime.configure_task.return_value = native

        result = await _create_and_configure(runtime, task_spec(), None)

        self.assertEqual(result.id, "thread-1")
        self.assertEqual(
            runtime.find_tasks.await_args_list[0].args,
            ("/instance/threads/fc-op-1",),
        )
        self.assertEqual(runtime.find_tasks.await_count, 2)
        runtime.create_task.assert_awaited_once()

    async def test_multiple_creation_matches_require_recovery(self) -> None:
        runtime = AsyncMock()
        runtime.capabilities.return_value = RuntimeCapabilities(
            available=True,
            endpoint="ws://runtime",
            methods=(),
            models={"luna": ("high",)},
        )
        runtime.find_tasks.return_value = [task_facts("one"), task_facts("two")]

        with self.assertRaises(AppServerError) as captured:
            await _create_and_configure(runtime, task_spec(), None)

        self.assertTrue(captured.exception.uncertain)
        runtime.create_task.assert_not_awaited()

    async def test_installed_cli_starts_tracked_task_before_native_turn(self) -> None:
        threads: dict[str, dict[str, Any]] = {}
        terminals: dict[str, list[dict[str, Any]]] = {}
        thread_count = 0
        turn_count = 0
        captured_prompt: str | None = None
        responded: list[dict[str, Any]] = []

        async def handler(connection: Any) -> None:
            nonlocal thread_count, turn_count, captured_prompt
            async for raw in connection:
                message = json.loads(raw)
                identifier = message.get("id")
                method = message.get("method")
                params = message.get("params", {})
                if method is None and identifier == "native-request-1":
                    responded.append(dict(message.get("result") or {}))
                    continue
                if method == "initialize":
                    result: dict[str, Any] = {}
                elif method == "model/list":
                    result = {
                        "data": [
                            {
                                "model": "luna",
                                "supportedReasoningEfforts": [
                                    {"reasoningEffort": "high"}
                                ],
                            }
                        ]
                    }
                elif method == "thread/list":
                    requested = params.get("cwd")
                    requested_cwds = (
                        requested if isinstance(requested, list) else [requested]
                    )
                    data = [
                        thread
                        for thread in threads.values()
                        if requested is None or thread["cwd"] in requested_cwds
                    ]
                    result = {"data": data, "nextCursor": None}
                elif method == "thread/loaded/list":
                    result = {"data": list(threads)}
                elif method == "thread/start":
                    thread_count += 1
                    thread_id = f"native-thread-{thread_count}"
                    thread = {
                        "id": thread_id,
                        "name": None,
                        "cwd": params["cwd"],
                        "environments": [
                            {
                                "environmentId": "local",
                                "cwd": params["cwd"],
                                "runtimeWorkspaceRoots": params[
                                    "runtimeWorkspaceRoots"
                                ],
                            }
                        ],
                        "projectId": params["projectId"],
                        "status": {"type": "idle"},
                        "archived": False,
                        "turns": [],
                    }
                    threads[thread_id] = thread
                    terminals[thread_id] = []
                    result = {"thread": thread}
                elif method == "thread/name/set":
                    thread = threads[params["threadId"]]
                    thread["name"] = params["name"]
                    result = {}
                elif method == "thread/settings/update":
                    thread = threads[params["threadId"]]
                    thread["environments"][0]["cwd"] = params["cwd"]
                    result = {}
                elif method == "thread/read":
                    thread = threads[params["threadId"]]
                    result = {"thread": thread}
                elif method == "turn/start":
                    thread = threads[params["threadId"]]
                    turn_count += 1
                    captured_prompt = params["input"][0]["text"]
                    turn = {
                        "id": f"native-turn-{turn_count}",
                        "status": "completed",
                        "items": [{"type": "userMessage", "text": captured_prompt}],
                    }
                    thread["turns"] = [turn]
                    thread["status"] = {"type": "idle"}
                    result = {"turn": turn}
                elif method == "thread/items/list":
                    thread = threads[params["threadId"]]
                    result = {
                        "data": [
                            {
                                "turnId": thread["turns"][-1]["id"],
                                "item": {
                                    "type": "agentMessage",
                                    "text": "bounded fixture output",
                                },
                            }
                        ],
                        "nextCursor": None,
                    }
                elif method == "thread/backgroundTerminals/list":
                    result = {
                        "data": list(terminals.get(params["threadId"], [])),
                        "nextCursor": None,
                    }
                elif method == "thread/backgroundTerminals/terminate":
                    current = terminals.get(params["threadId"], [])
                    before = len(current)
                    terminals[params["threadId"]] = [
                        item
                        for item in current
                        if item.get("processId") != params["processId"]
                    ]
                    result = {"terminated": len(terminals[params["threadId"]]) < before}
                elif method == "thread/backgroundTerminals/clean":
                    result = {}
                elif method == "thread/unsubscribe":
                    result = {"status": "notSubscribed"}
                else:
                    continue
                await connection.send(json.dumps({"id": identifier, "result": result}))
                if method == "initialize" and "native-thread-1" in threads:
                    await connection.send(
                        json.dumps(
                            {
                                "id": "native-request-1",
                                "method": "item/tool/requestUserInput",
                                "params": {
                                    "threadId": "native-thread-1",
                                    "turnId": "native-turn-1",
                                    "itemId": "input-item-1",
                                    "questions": [
                                        {
                                            "id": "scope",
                                            "header": "Scope",
                                            "question": "Continue?",
                                            "options": [],
                                        }
                                    ],
                                },
                            }
                        )
                    )

        async def run(
            *arguments: str, payload: dict[str, Any] | None = None
        ) -> tuple[int, str, str]:
            executable = Path(os.sys.executable).with_name("fulcrum")
            process = await asyncio.create_subprocess_exec(
                str(executable),
                *arguments,
                stdin=asyncio.subprocess.PIPE if payload is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(
                    json.dumps(payload).encode() if payload is not None else None
                ),
                30,
            )
            return process.returncode or 0, stdout.decode(), stderr.decode()

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                brain, instance, project = (
                    root / "brain",
                    root / "instance",
                    root / "project",
                )
                for path in (brain, instance, project):
                    path.mkdir()
                for path in (brain, project):
                    subprocess.run(["git", "init", "--quiet"], cwd=path, check=True)
                    subprocess.run(
                        ["git", "config", "user.email", "fixture@example.com"],
                        cwd=path,
                        check=True,
                    )
                    subprocess.run(
                        ["git", "config", "user.name", "Fixture"],
                        cwd=path,
                        check=True,
                    )
                    subprocess.run(
                        ["git", "config", "beads.role", "maintainer"],
                        cwd=path,
                        check=True,
                    )
                subprocess.run(
                    [
                        "bd",
                        "init",
                        "--prefix",
                        "fc",
                        "--non-interactive",
                        "--skip-agents",
                        "--skip-hooks",
                    ],
                    cwd=brain,
                    capture_output=True,
                    check=True,
                    timeout=30,
                )
                config = brain / "fulcrum.yaml"
                config.write_text(
                    "\n".join(
                        (
                            "runtime:",
                            "  kind: codex",
                            f"  endpoint: ws://127.0.0.1:{port}",
                            "brain:",
                            f"  root: {brain}",
                            "models:",
                            "  executor:",
                            "    model: luna",
                            "    effort: high",
                            "  weaver:",
                            "    model: luna",
                            "    effort: high",
                            "projects:",
                            "  toy:",
                            f"    root: {project}",
                            "    enabled: true",
                            "    codex_project_id: fixture-project",
                            "    delivery:",
                            "      id: fixture-delivery",
                            "",
                        )
                    ),
                    encoding="utf-8",
                )
                (instance / "config").symlink_to(config)
                request_id = str(uuid.uuid4())
                code, stdout, stderr = await run(
                    "work",
                    "create",
                    "--instance",
                    str(instance),
                    "--offline",
                    "--actor",
                    "human",
                    "--request-id",
                    request_id,
                    "--input",
                    "-",
                    "--json",
                    payload={
                        "title": "Runtime boundary",
                        "outcome": "Start one tracked native task",
                        "project": "toy",
                        "acceptance": ["The native turn is correlated"],
                    },
                )
                self.assertEqual(code, 0, stderr + stdout)
                bead_id = json.loads(stdout)["result"]["result"]["bead_id"]
                start_request = str(uuid.uuid4())
                instructions = "Perform the fixture responsibility completely."
                code, stdout, stderr = await run(
                    "task",
                    "start",
                    "--bead",
                    bead_id,
                    "--role",
                    "executor",
                    "--instance",
                    str(instance),
                    "--offline",
                    "--actor",
                    "human",
                    "--request-id",
                    start_request,
                    "--input",
                    "-",
                    "--json",
                    payload={"instructions": instructions},
                )
                self.assertEqual(code, 0, stderr + stdout)
                envelope = json.loads(stdout)
                result = envelope["result"]["result"]
                self.assertEqual(result["thread_id"], "native-thread-1")
                self.assertEqual(result["turn_id"], "native-turn-1")
                self.assertIn(instructions, captured_prompt or "")
                self.assertIn(
                    f"FULCRUM_OPERATION={envelope['operation_id']}",
                    captured_prompt or "",
                )
                self.assertEqual(
                    threads["native-thread-1"]["projectId"], "fixture-project"
                )
                self.assertEqual(
                    threads["native-thread-1"]["environments"][0]["cwd"],
                    str(project.resolve()),
                )
                code, shown, stderr = await run(
                    "task",
                    "show",
                    result["task_record_id"],
                    "--instance",
                    str(instance),
                    "--offline",
                    "--json",
                )
                self.assertEqual(code, 0, stderr + shown)
                creation_cwd = json.loads(shown)["result"]["creation_cwd"]
                self.assertIn(envelope["operation_id"], creation_cwd)

                code, output, stderr = await run(
                    "task",
                    "output",
                    "native-thread-1",
                    "--turn-id",
                    "native-turn-1",
                    "--max-bytes",
                    "256",
                    "--instance",
                    str(instance),
                    "--offline",
                    "--json",
                )
                self.assertEqual(code, 0, stderr + output)
                output_result = json.loads(output)["result"]
                self.assertEqual(
                    output_result["items"][0]["item"]["text"],
                    "bounded fixture output",
                )
                self.assertLessEqual(output_result["bytes"], 256)
                code, waited, stderr = await run(
                    "task",
                    "wait",
                    "native-thread-1",
                    "--turn-id",
                    "native-turn-1",
                    "--until",
                    "terminal",
                    "--instance",
                    str(instance),
                    "--offline",
                    "--json",
                )
                self.assertEqual(code, 0, stderr + waited)
                self.assertTrue(json.loads(waited)["result"]["satisfied"])
                code, requests, stderr = await run(
                    "task",
                    "requests",
                    "native-thread-1",
                    "--instance",
                    str(instance),
                    "--offline",
                    "--json",
                )
                self.assertEqual(code, 0, stderr + requests)
                self.assertEqual(
                    json.loads(requests)["result"]["items"][0]["request_id"],
                    "native-request-1",
                )
                code, response, stderr = await run(
                    "task",
                    "respond",
                    "native-thread-1",
                    "--request",
                    "native-request-1",
                    "--instance",
                    str(instance),
                    "--offline",
                    "--actor",
                    "human",
                    "--input",
                    "-",
                    "--json",
                    payload={"response": {"answers": {"scope": {"answers": ["yes"]}}}},
                )
                self.assertEqual(code, 0, stderr + response)
                self.assertEqual(
                    responded[-1], {"answers": {"scope": {"answers": ["yes"]}}}
                )
                terminals["native-thread-1"] = [
                    {"processId": "process-1", "itemId": "terminal-item-1"}
                ]
                code, listed, stderr = await run(
                    "task",
                    "terminals",
                    "native-thread-1",
                    "--instance",
                    str(instance),
                    "--offline",
                    "--json",
                )
                self.assertEqual(code, 0, stderr + listed)
                self.assertEqual(
                    json.loads(listed)["result"]["items"][0]["terminal_id"],
                    "process-1",
                )
                code, stopped, stderr = await run(
                    "task",
                    "terminal",
                    "stop",
                    "native-thread-1",
                    "--terminal",
                    "process-1",
                    "--reason",
                    "fixture cleanup",
                    "--instance",
                    str(instance),
                    "--offline",
                    "--actor",
                    "human",
                    "--json",
                )
                self.assertEqual(code, 0, stderr + stopped)
                self.assertEqual(terminals["native-thread-1"], [])
                code, stdout, stderr = await run(
                    "task",
                    "release",
                    "native-thread-1",
                    "--instance",
                    str(instance),
                    "--offline",
                    "--actor",
                    "human",
                    "--json",
                )
                self.assertEqual(code, 0, stderr + stdout)
                self.assertEqual(
                    json.loads(stdout)["result"]["result"]["facts"]["status"],
                    "notSubscribed",
                )

                ledger = Ledger(brain)
                root_record = ledger.show(bead_id)
                assert root_record is not None and root_record.fc
                root_fc = dict(root_record.fc)
                root_fc["plan"] = {
                    "draft": {
                        "text": "Implement the reviewed fixture.",
                        "tasks": [
                            {
                                "key": "fixture",
                                "title": "Deliver the reviewed fixture",
                                "outcome": "Deliver the fixture.",
                                "acceptance": ["The fixture is observed."],
                                "depends_on": [],
                            }
                        ],
                        "summary": "One reviewed fixture task.",
                        "publication": None,
                        "validation": {
                            "summary": "Observe the installed CLI.",
                            "checks": [],
                        },
                    },
                    "reviews": {},
                }
                ledger.update_fc(bead_id, root_fc)
                author = root_fc["owner"]
                for perspective in ("cold_reader", "requirements"):
                    code, started, stderr = await run(
                        "plan",
                        "review",
                        "start",
                        "--bead",
                        bead_id,
                        "--perspective",
                        perspective,
                        "--instance",
                        str(instance),
                        "--offline",
                        "--actor",
                        "human",
                        "--json",
                    )
                    self.assertEqual(code, 0, stderr + started)
                    review = json.loads(started)["result"]["result"]
                    code, finished, stderr = await run(
                        "plan",
                        "review",
                        "finish",
                        "--task",
                        review["task_record_id"],
                        "--instance",
                        str(instance),
                        "--offline",
                        "--actor",
                        f"task:{review['thread_id']}",
                        "--thread-id",
                        review["thread_id"],
                        "--input",
                        "-",
                        "--json",
                        payload={
                            "review_operation": review["review_operation"],
                            "summary": f"{perspective} fixture review complete.",
                            "findings": [],
                        },
                    )
                    self.assertEqual(code, 0, stderr + finished)
                reviewed_root = ledger.show(bead_id)
                assert reviewed_root is not None and reviewed_root.fc
                self.assertEqual(reviewed_root.fc["owner"], author)
                self.assertEqual(
                    {
                        key: value["state"]
                        for key, value in reviewed_root.fc["plan"]["reviews"].items()
                    },
                    {"cold_reader": "completed", "requirements": "completed"},
                )
                subprocess.run(
                    ["bd", "-C", str(brain), "dolt", "stop"],
                    capture_output=True,
                    check=False,
                    timeout=20,
                )

    async def test_installed_cli_reports_live_capabilities_and_resources(self) -> None:
        async def handler(connection: Any) -> None:
            async for raw in connection:
                message = json.loads(raw)
                identifier = message.get("id")
                method = message.get("method")
                if method == "initialize":
                    result: dict[str, Any] = {}
                elif method == "model/list":
                    result = {
                        "data": [
                            {
                                "model": "luna",
                                "supportedReasoningEfforts": [
                                    {"reasoningEffort": "high"}
                                ],
                            }
                        ]
                    }
                elif method == "thread/loaded/list":
                    result = {"data": []}
                else:
                    continue
                await connection.send(json.dumps({"id": identifier, "result": result}))

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                brain = root / "brain"
                instance = root / "instance"
                brain.mkdir()
                instance.mkdir()
                config = brain / "fulcrum.yaml"
                config.write_text(
                    "\n".join(
                        (
                            "runtime:",
                            "  kind: codex",
                            f"  endpoint: ws://127.0.0.1:{port}",
                            "brain:",
                            f"  root: {brain}",
                            "projects: {}",
                            "",
                        )
                    ),
                    encoding="utf-8",
                )
                (instance / "config").symlink_to(config)
                executable = Path(os.sys.executable).with_name("fulcrum")
                process = await asyncio.create_subprocess_exec(
                    str(executable),
                    "runtime",
                    "status",
                    "--instance",
                    str(instance),
                    "--offline",
                    "--json",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(process.communicate(), 10)

        self.assertEqual(process.returncode, 0, stderr.decode() + stdout.decode())
        result = json.loads(stdout)["result"]
        self.assertTrue(result["available"])
        self.assertEqual(result["capabilities"]["models"], {"luna": ["high"]})
        self.assertEqual(result["resources"]["loaded_count"], 0)

    async def test_lost_start_response_adopts_exact_input_marker(self) -> None:
        found = TurnFacts(
            id="turn-1",
            thread_id="thread-1",
            state="inProgress",
            operation_id="fc-op-1",
            completed=False,
            error=None,
            tools=(),
            usage=None,
            observed_at="2026-09-14T00:00:00Z",
        )
        runtime = AsyncMock()
        runtime.find_turn.side_effect = [None, found]
        runtime.start_turn.side_effect = AppServerError(
            "connection lost", category="uncertain", uncertain=True
        )

        result = await _start_or_recover(
            runtime, "thread-1", task_spec(), "fc-op-1", "Do the work."
        )

        self.assertEqual(result.id, "turn-1")
        runtime.start_turn.assert_awaited_once()
        self.assertEqual(runtime.find_turn.await_count, 2)

    async def test_find_turn_requires_exact_persisted_marker(self) -> None:
        transport = AsyncMock()
        transport.read_thread.return_value = {
            "id": "thread-1",
            "turns": [
                {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [
                        {
                            "type": "userMessage",
                            "text": "FULCRUM_OPERATION=fc-op-1 Do the work.",
                        }
                    ],
                },
                {
                    "id": "turn-near",
                    "status": "completed",
                    "items": [
                        {
                            "type": "userMessage",
                            "text": "FULCRUM_OPERATION=fc-op-10",
                        }
                    ],
                },
            ],
        }
        runtime = AppServerRuntime(
            "ws://unused", transport=cast(CodexRuntime, transport)
        )

        result = await runtime.find_turn("thread-1", "fc-op-1")

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.id, "turn-1")

    async def test_project_mismatch_is_uncertain_after_configuration(self) -> None:
        transport = AsyncMock()
        transport.read_thread.return_value = {
            "id": "thread-1",
            "name": "Managed task",
            "cwd": "/work",
            "projectId": "wrong-project",
            "runtimeWorkspaceRoots": ["/work"],
            "status": {"type": "idle"},
            "turns": [],
        }
        transport.loaded_threads.return_value = ["thread-1"]
        transport.pending_server_requests = {}
        transport.assign_thread_project.return_value = {
            "thread": transport.read_thread.return_value
        }
        runtime = AppServerRuntime(
            "ws://unused", transport=cast(CodexRuntime, transport)
        )

        with self.assertRaises(AppServerError) as captured:
            await runtime.configure_task("thread-1", task_spec())

        self.assertTrue(captured.exception.uncertain)

    async def test_project_creation_is_discovered_by_exact_root(self) -> None:
        transport = AsyncMock()
        transport.list_projects.side_effect = [
            [],
            [
                {
                    "id": "project-1",
                    "name": "toy",
                    "roots": [{"path": "/private/tmp/toy"}],
                }
            ],
        ]
        transport.create_project.side_effect = AppServerError(
            "connection lost", category="uncertain", uncertain=True
        )
        runtime = AppServerRuntime(
            "ws://unused", transport=cast(CodexRuntime, transport)
        )

        project = await runtime.ensure_project(
            name="toy", root="/private/tmp/toy", operation_id="fc-op-project"
        )

        self.assertEqual(project["id"], "project-1")
        transport.create_project.assert_awaited_once_with(
            name="toy",
            roots=("/private/tmp/toy",),
            idempotency_key="fc-op-project",
            metadata={"fulcrum_operation": "fc-op-project"},
        )

    async def test_project_deletion_is_verified_by_exact_id(self) -> None:
        transport = AsyncMock()
        transport.list_projects.side_effect = [
            [{"id": "project-1", "name": "toy"}],
            [{"id": "unrelated", "name": "other"}],
        ]
        runtime = AppServerRuntime(
            "ws://unused", transport=cast(CodexRuntime, transport)
        )

        result = await runtime.delete_project("project-1")

        self.assertEqual(result, {"id": "project-1", "exists": False, "deleted": True})
        transport.delete_project.assert_awaited_once_with("project-1")


if __name__ == "__main__":
    unittest.main()
