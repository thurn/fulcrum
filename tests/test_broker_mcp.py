from __future__ import annotations

import json
import time
from pathlib import Path
import asyncio
import tempfile
import unittest
from unittest.mock import patch

from fulcrum.broker import BrokerServer, Evaluation, PendingBroker
from fulcrum.mcp_server import FreshCli, McpServer, tool_descriptions


class BrokerTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconstruction_loop_does_not_spin_after_snapshot(self):
        calls = 0

        async def runner(argv, stdin):
            nonlocal calls
            calls += 1
            return {
                "ok": True,
                "result": {"waits": [], "watch_paths": []},
            }

        broker = PendingBroker(runner)
        server = BrokerServer(
            Path("/tmp/fulcrum-test-broker.sock"),
            broker,
            snapshot_argv=("fulcrum", "transport", "snapshot"),
        )
        task = asyncio.create_task(server._reconstruct_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(calls, 1)

    async def test_reconstructed_durable_waits_are_visible_after_restart(self):
        broker = PendingBroker()
        broker.reconstruct(
            {
                "ok": True,
                "result": {
                    "waits": [
                        {
                            "record_id": "fc-a",
                            "wait_id": "wait-durable",
                            "kind": "ci",
                            "deadline": "2026-09-17T08:00:00Z",
                        }
                    ],
                    "watch_paths": ["/tmp/transcript.jsonl"],
                },
            }
        )
        health = broker.health()
        self.assertEqual(health["durable_waits"][0]["wait_id"], "wait-durable")
        self.assertEqual(health["watch_paths"], ["/tmp/transcript.jsonl"])

    async def test_mcp_wait_uses_policy_deadline_plus_wrapper_margin(self):
        captured = {}

        async def fresh(argv, stdin):
            return {
                "ok": True,
                "state": "running",
                "result": {
                    "transport_wait": {
                        "kind": "instruction",
                        "wait_id": "wait-policy",
                        "remaining_seconds": 7,
                    }
                },
            }

        async def broker(path, payload):
            captured.update(payload)
            return {"ok": True, "state": "completed"}

        cli = FreshCli(Path("/instance"), Path("/config"), Path("/fulcrum"))
        with (
            patch("fulcrum.broker.run_fresh", fresh),
            patch("fulcrum.mcp_server.broker_request", broker),
        ):
            await cli.run(
                "wait_for_instructions",
                {"request_id": "89170642-a734-4735-a272-7b0d41e006d8"},
            )
        self.assertEqual(captured["remaining_seconds"], 67)

    async def test_transcript_change_wakes_wait_before_timer(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "rollout.jsonl"
            transcript.write_text("first\n", encoding="utf-8")
            calls = 0

            async def runner(argv, stdin):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return {
                        "ok": True,
                        "state": "running",
                        "result": {
                            "transport_wait": {
                                "wait_id": "watched",
                                "watch_paths": [str(transcript)],
                            }
                        },
                    }
                return {"ok": True, "state": "completed", "result": {"calls": calls}}

            async def change_file():
                await asyncio.sleep(0.05)
                transcript.write_text("second\n", encoding="utf-8")

            changed = asyncio.create_task(change_file())
            started = time.monotonic()
            result = await PendingBroker(runner).wait(
                Evaluation(
                    argv=("fulcrum",),
                    stdin="{}",
                    interval_seconds=5,
                    deadline_monotonic=time.monotonic() + 6,
                    wait_id="watched",
                    kind="instruction",
                )
            )
            await changed
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(result["result"]["calls"], 2)

    async def test_each_evaluation_starts_from_current_operation_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            revision = root / "revision"
            revision.write_text("first", encoding="utf-8")
            calls = 0

            async def runner(argv, stdin):
                nonlocal calls
                calls += 1
                observed = revision.read_text(encoding="utf-8")
                if calls == 1:
                    revision.write_text("second", encoding="utf-8")
                    return {
                        "ok": True,
                        "state": "running",
                        "result": {"transport_wait": {"wait_id": "source-wait"}},
                    }
                return {
                    "ok": True,
                    "state": "completed",
                    "result": {"revision": observed},
                }

            result = await PendingBroker(runner).wait(
                Evaluation(
                    argv=("fulcrum",),
                    stdin="{}",
                    interval_seconds=0.01,
                    deadline_monotonic=time.monotonic() + 1,
                    wait_id="source-wait",
                    kind="instruction",
                )
            )
            self.assertEqual(result["result"]["revision"], "second")

    async def test_pending_wait_returns_only_terminal_result(self):
        calls = 0
        policy_revision = "old"

        async def runner(argv, stdin):
            nonlocal calls, policy_revision
            calls += 1
            if calls == 1:
                policy_revision = "new"
                return {
                    "ok": True,
                    "state": "running",
                    "result": {"transport_wait": {"wait_id": "wait-1"}},
                }
            return {
                "ok": True,
                "state": "completed",
                "result": {"kind": "action", "policy_revision": policy_revision},
            }

        broker = PendingBroker(runner)
        result = await broker.wait(
            Evaluation(
                argv=("fulcrum",),
                stdin="{}",
                interval_seconds=0.01,
                deadline_monotonic=time.monotonic() + 1,
                wait_id="wait-1",
                kind="instruction",
            )
        )
        self.assertEqual(result["result"]["kind"], "action")
        self.assertEqual(result["result"]["policy_revision"], "new")
        self.assertEqual(calls, 2)


class McpTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_advertises_blocking_protocol_tools(self):
        tools = {item["name"]: item for item in tool_descriptions()}
        names = set(tools)
        self.assertIn("wait_for_instructions", names)
        self.assertIn("wait_for_ci_results", names)
        self.assertIn("claim_action", names)
        claim = tools["claim_action"]["inputSchema"]
        self.assertNotIn("turn_id", claim["properties"])
        registration = tools["register_standing"]["inputSchema"]
        self.assertIn("role", registration["required"])
        self.assertIn("action_id", registration["required"])
        self.assertIn("task_id", registration["required"])
        self.assertIn("session_id", registration["required"])
        self.assertIn("turn_id", registration["properties"])
        finish = tools["finish"]["inputSchema"]
        self.assertIn("outcome", finish["required"])
        self.assertEqual(
            finish["properties"]["checks"]["items"]["required"],
            ["name", "status", "evidence"],
        )

        class Cli:
            async def run(self, name, arguments):
                return {
                    "ok": True,
                    "state": "completed",
                    "result": {"name": name},
                }

        response = await McpServer(Cli()).handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
        self.assertEqual(response["id"], 1)
        self.assertGreater(len(response["result"]["tools"]), 5)

    async def test_standing_registration_observes_current_session_identity(self):
        cli = FreshCli(Path("/instance"), Path("/config"), Path("/fulcrum"))
        with patch.dict(
            "os.environ",
            {
                "CODEX_THREAD_ID": "thread-1",
                "CODEX_SESSION_ID": "session-1",
            },
        ):
            argv, stdin = cli.invocation(
                "register_standing",
                {
                    "request_id": "89170642-a734-4735-a272-7b0d41e006d8",
                    "role": "steward",
                    "action_id": "action-1",
                },
            )

        self.assertIn("task:thread-1", argv)
        self.assertEqual(json.loads(stdin)["session_id"], "session-1")
