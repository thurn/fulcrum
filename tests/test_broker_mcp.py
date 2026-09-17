from __future__ import annotations

import time
import unittest

from fulcrum.broker import Evaluation, PendingBroker
from fulcrum.mcp_server import McpServer, tool_descriptions


class BrokerTests(unittest.IsolatedAsyncioTestCase):
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
        names = {item["name"] for item in tool_descriptions()}
        self.assertIn("wait_for_instructions", names)
        self.assertIn("wait_for_ci_results", names)
        self.assertIn("claim_action", names)

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
