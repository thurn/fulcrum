from __future__ import annotations

from dataclasses import replace
import json
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
                b'{"type":"turn_context","event_id":"e1","turn_id":"t1","model":"gpt-5.6-luna"}\n'
                b'{"type":"token_usage_record","response_id":"r1","usage":{"input_tokens":12,"cache_write_input_tokens":3,"output_tokens":4,"reasoning_output_tokens":2}}\n'
                b'{"type":"turn_started"'
            )
            page = read_transcript(path)
        self.assertEqual(page.lifecycle[0]["event_id"], "e1")
        self.assertEqual(page.lifecycle[0]["model"], "gpt-5.6-luna")
        self.assertEqual(page.usage[0]["response_id"], "r1")
        self.assertEqual(page.usage[0]["input_tokens"], 12)
        self.assertEqual(page.usage[0]["cache_write_tokens"], 3)
        self.assertEqual(page.usage[0]["reasoning_tokens"], 2)
        self.assertEqual(page.gaps[0]["kind"], "incomplete_trailing_line")


class HookTests(unittest.TestCase):
    def test_registered_transcript_collects_delayed_writes(self):
        ledger = MemoryLedger()
        desktop = DesktopProtocolService(ledger)
        action = desktop.queue_action(
            replace(
                request(("action", "queue")),
                input={
                    "executor": "bootstrap",
                    "tool": "create_thread",
                    "arguments": {"prompt": "register steward"},
                    "purpose": "bootstrap_steward",
                },
                request_id=str(uuid.uuid4()),
            )
        ).result["action"]
        desktop.register_standing(
            replace(
                request(("register", "standing")),
                input={
                    "role": "steward",
                    "task_id": "steward-1",
                    "session_id": "session-1",
                    "action_id": action["action_id"],
                },
                request_id=str(uuid.uuid4()),
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_text(
                json.dumps({"type": "turn_context", "turn_id": "turn-1"}) + "\n"
            )
            hook = HookService(ledger)
            hook.handle(
                replace(
                    request(("hook", "handle")),
                    actor=ActorContext.parse("task:steward-1"),
                    thread_id="steward-1",
                    input={
                        "hook_event_name": "SessionStart",
                        "thread_id": "steward-1",
                        "turn_id": "turn-1",
                        "transcript_path": str(path),
                    },
                    request_id=str(uuid.uuid4()),
                )
            )
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "token_usage_record",
                            "response_id": "response-delayed",
                            "usage": {"input_tokens": 3, "output_tokens": 1},
                        }
                    )
                    + "\n"
                )
            watched = hook.collect_registered(request())
        protocol = (ledger.show("fc-system").fc or {})["desktop"]
        self.assertEqual(watched, [str(path)])
        self.assertIn("response-delayed", protocol["observations"]["usage"])

    def test_standing_tasks_keep_independent_transcript_cursors(self):
        ledger = MemoryLedger()
        desktop = DesktopProtocolService(ledger)
        bindings = {}
        for role in ("steward", "marshal"):
            action = desktop.queue_action(
                replace(
                    request(("action", "queue")),
                    input={
                        "executor": "bootstrap",
                        "tool": "create_thread",
                        "arguments": {"prompt": f"register {role}"},
                        "purpose": f"bootstrap_{role}",
                    },
                    request_id=str(uuid.uuid4()),
                )
            ).result["action"]
            task_id = f"{role}-1"
            desktop.register_standing(
                replace(
                    request(("register", "standing")),
                    input={
                        "role": role,
                        "task_id": task_id,
                        "session_id": f"session-{role}",
                        "action_id": action["action_id"],
                    },
                    request_id=str(uuid.uuid4()),
                )
            )
            bindings[role] = task_id
        with tempfile.TemporaryDirectory() as directory:
            for role, task_id in bindings.items():
                turn_id = f"turn-{role}"
                path = Path(directory) / f"{role}.jsonl"
                rows = [
                    {
                        "type": "turn_context",
                        "turn_id": turn_id,
                        "model": "gpt-5.6-luna",
                    },
                    {
                        "type": "token_usage_record",
                        "payload": {
                            "thread_id": task_id,
                            "turn_id": turn_id,
                            "response_id": f"response-{role}",
                            "usage": {"input_tokens": 10, "output_tokens": 2},
                        },
                    },
                    {
                        "type": "event_msg",
                        "payload": {"type": "task_complete", "turn_id": turn_id},
                    },
                ]
                path.write_text("".join(json.dumps(row) + "\n" for row in rows))
                HookService(ledger).handle(
                    replace(
                        request(("hook", "handle")),
                        actor=ActorContext.parse(f"task:{task_id}"),
                        thread_id=task_id,
                        input={
                            "hook_event_name": "SessionStart",
                            "thread_id": task_id,
                            "turn_id": turn_id,
                            "transcript_path": str(path),
                        },
                        request_id=str(uuid.uuid4()),
                    )
                )
        protocol = (ledger.show("fc-system").fc or {})["desktop"]
        self.assertEqual(set(protocol["transcripts"]), {"steward-1", "marshal-1"})
        observed_roles = {}
        for analytics in ledger.list_records(kind="analytics", limit=0):
            observed_roles[(analytics.fc or {})["thread_id"]] = (analytics.fc or {})[
                "role"
            ]
            self.assertNotIn(
                "terminal_lifecycle_missing", (analytics.fc or {})["missing_reasons"]
            )
        self.assertEqual(
            observed_roles,
            {"steward-1": "steward", "marshal-1": "marshal"},
        )

    def test_pre_tool_rejects_unclaimed_native_effect(self):
        ledger = MemoryLedger()
        desktop = DesktopProtocolService(ledger)
        action = desktop.queue_action(
            replace(
                request(("action", "queue")),
                input={
                    "executor": "bootstrap",
                    "tool": "create_thread",
                    "arguments": {"prompt": "register steward"},
                    "purpose": "bootstrap_steward",
                },
                request_id=str(uuid.uuid4()),
            )
        ).result["action"]
        desktop.register_standing(
            replace(
                request(("register", "standing")),
                input={
                    "role": "steward",
                    "task_id": "steward-1",
                    "session_id": "session-1",
                    "action_id": action["action_id"],
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
        self.assertEqual(
            result.result["hookSpecificOutput"]["permissionDecision"], "deny"
        )
