from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
import uuid

from fulcrum.contracts import ActorContext
from fulcrum.desktop_protocol import DesktopProtocolService
from fulcrum.hooks import HookService
from fulcrum.observations import read_transcript
from tests.support import MemoryLedger, request


class ObservationTests(unittest.TestCase):
    def test_parser_retains_native_lifecycle_and_usage_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_bytes(
                b'{"type":"turn_completed","event_id":"e1","turn_id":"t1"}\n'
                b'{"type":"token_usage_record","response_id":"r1","usage":{"input_tokens":12,"output_tokens":4}}\n'
                b'{"type":"turn_started"'
            )
            page = read_transcript(path)
        self.assertEqual(page.lifecycle[0]["event_id"], "e1")
        self.assertEqual(page.usage[0]["response_id"], "r1")
        self.assertEqual(page.usage[0]["input_tokens"], 12)
        self.assertEqual(page.gaps[0]["kind"], "incomplete_trailing_line")


class HookTests(unittest.TestCase):
    def test_pre_tool_rejects_unclaimed_native_effect(self):
        ledger = MemoryLedger()
        desktop = DesktopProtocolService(ledger)
        desktop.register_standing(
            replace(
                request(("register", "standing")),
                input={
                    "role": "steward",
                    "task_id": "steward-1",
                    "session_id": "session-1",
                },
                request_id=str(uuid.uuid4()),
            )
        )
        hook_request = replace(
            request(("hook", "handle")),
            actor=ActorContext.parse("task:steward-1"),
            thread_id="steward-1",
            input={
                "hook_event_name": "PreToolUse",
                "tool_name": "mcp__codex_app__create_thread",
                "tool_input": {"prompt": "unclaimed"},
                "tool_use_id": "tool-1",
            },
            request_id=str(uuid.uuid4()),
        )
        result = HookService(ledger).handle(hook_request)
        self.assertFalse(result.result["continue"])
        self.assertEqual(
            result.result["hookSpecificOutput"]["permissionDecision"], "deny"
        )
