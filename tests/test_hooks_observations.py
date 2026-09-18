from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from fulcrum.contracts import ActorContext
from fulcrum.desktop_protocol import DesktopProtocolService
from fulcrum.hooks import HookService, _arguments_match
from fulcrum.observations import read_transcript
from tests.support import (
    MemoryLedger,
    observe_action_prompt,
    record,
    request,
    seed_action,
)


class ObservationTests(unittest.TestCase):
    def test_parser_retains_native_lifecycle_and_usage_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_bytes(
                b'{"type":"turn_context","event_id":"e1","turn_id":"t1","model":"gpt-5.6-luna"}\n'
                b'{"type":"token_usage_record","response_id":"r1","usage":{"input_tokens":12,"cache_write_input_tokens":3,"output_tokens":4,"reasoning_output_tokens":2}}\n'
                b'{"type":"event_msg","payload":{"type":"turn_aborted","turn_id":"t1","reason":"interrupted"}}\n'
                b'{"type":"event_msg","payload":{"type":"turn_future","turn_id":"t2"}}\n'
                b'{"type":"turn_started"'
            )
            page = read_transcript(path)
        self.assertEqual(page.lifecycle[0]["event_id"], "e1")
        self.assertEqual(page.lifecycle[0]["model"], "gpt-5.6-luna")
        self.assertEqual(page.lifecycle[1]["type"], "turn_aborted")
        self.assertEqual(page.lifecycle[1]["reason"], "interrupted")
        self.assertEqual(page.usage[0]["response_id"], "r1")
        self.assertEqual(page.usage[0]["input_tokens"], 12)
        self.assertEqual(page.usage[0]["cache_write_tokens"], 3)
        self.assertEqual(page.usage[0]["reasoning_tokens"], 2)
        self.assertEqual(page.gaps[0]["kind"], "unrecognized_lifecycle_event")
        self.assertEqual(page.gaps[0]["event_type"], "turn_future")
        self.assertEqual(page.gaps[1]["kind"], "incomplete_trailing_line")


class HookTests(unittest.TestCase):
    def test_create_thread_allows_paraphrase_with_exact_action_marker(self):
        action = {
            "action_id": "action-1",
            "record_id": "fc-work",
            "assignment_token": "assignment-1",
            "tool": "create_thread",
        }
        expected = {
            "prompt": "Register and perform the retained assignment.",
            "title": "worker",
            "target": {"type": "project", "projectId": "project-1"},
        }
        actual = {
            **expected,
            "prompt": (
                'Fulcrum-Action: {"instance":"/instance","record_id":"fc-work",'
                '"action_id":"action-1","assignment_token":"assignment-1"}\n'
                "Register first, then carry out the assignment returned by Fulcrum."
            ),
        }

        self.assertTrue(
            _arguments_match(action, expected, actual, instance="/instance")
        )

    def test_create_thread_rejects_paraphrase_with_wrong_action_marker(self):
        action = {
            "action_id": "action-1",
            "record_id": "fc-work",
            "assignment_token": "assignment-1",
            "tool": "create_thread",
        }
        expected = {"prompt": "Register.", "title": "worker"}
        actual = {
            "prompt": (
                'Fulcrum-Action: {"instance":"/instance","record_id":"fc-work",'
                '"action_id":"action-other","assignment_token":"assignment-1"}\n'
                "Register."
            ),
            "title": "worker",
        }

        self.assertFalse(
            _arguments_match(action, expected, actual, instance="/instance")
        )

    def test_non_prompt_create_thread_arguments_remain_exact(self):
        action = {
            "action_id": "action-1",
            "record_id": "fc-work",
            "tool": "create_thread",
        }
        expected = {"prompt": "Register.", "title": "worker"}
        actual = {
            "prompt": (
                'Fulcrum-Action: {"instance":"/instance","record_id":"fc-work",'
                '"action_id":"action-1"}\nRegister.'
            ),
            "title": "different worker",
        }

        self.assertFalse(
            _arguments_match(action, expected, actual, instance="/instance")
        )

    def test_active_assignment_collection_releases_completed_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker.jsonl"
            rows = [
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "native-turn",
                    },
                },
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            ledger = MemoryLedger(
                record(
                    "fc-work",
                    owner="worker-1",
                    phase="reviewing",
                    role="executor",
                    ownership_operation="assignment-1",
                    desktop={
                        "assignment": {
                            "assignment_token": "assignment-1",
                            "role": "executor",
                            "task_id": "worker-1",
                            "turn_id": None,
                            "state": "active",
                            "finish_operation": "finish-1",
                        },
                        "transcripts": {
                            "worker-1": {"path": str(path), "cursor": 0, "gaps": []}
                        },
                    },
                )
            )

            watched = HookService(ledger).collect_active_assignments(request())

        retained = ledger.show("fc-work")
        protocol = (retained.fc or {})["desktop"]
        self.assertEqual(watched, [str(path)])
        self.assertNotIn("assignment", protocol)
        self.assertEqual(protocol["assignment_history"][-1]["state"], "finished")
        self.assertEqual(protocol["assignment_history"][-1]["turn_id"], "native-turn")
        self.assertEqual(retained.assignee, "STEWARD")

    def test_active_assignment_collection_omits_inactive_transcripts(self):
        with tempfile.TemporaryDirectory() as directory:
            active = Path(directory) / "active.jsonl"
            inactive = Path(directory) / "inactive.jsonl"
            active.write_text("", encoding="utf-8")
            inactive.write_text("", encoding="utf-8")
            ledger = MemoryLedger(
                record(
                    "fc-work",
                    owner="worker-1",
                    phase="working",
                    role="executor",
                    desktop={
                        "assignment": {
                            "assignment_token": "assignment-1",
                            "role": "executor",
                            "task_id": "worker-1",
                            "turn_id": "turn-1",
                            "state": "active",
                        },
                        "transcripts": {
                            "worker-1": {
                                "path": str(active),
                                "cursor": 0,
                                "gaps": [],
                            },
                            "worker-old": {
                                "path": str(inactive),
                                "cursor": 0,
                                "gaps": [],
                            },
                        },
                    },
                )
            )

            watched = HookService(ledger).collect_active_assignments(request())

        self.assertEqual(watched, [str(active)])

    def test_session_only_hook_identity_binds_same_task_weaver(self):
        ledger = MemoryLedger(
            record(
                "fc-work",
                owner="weaver-1",
                phase="working",
                role="weaver",
                desktop={
                    "assignment": {
                        "assignment_token": "operation-1",
                        "role": "weaver",
                        "task_id": "weaver-1",
                        "session_id": None,
                        "state": "active",
                    },
                    "actions": {
                        "action-title": {
                            "action_id": "action-title",
                            "record_id": "fc-work",
                            "executor": "weaver",
                            "tool": "set_thread_title",
                            "arguments": {
                                "threadId": "weaver-1",
                                "title": "Weaver title",
                            },
                            "assignment_token": "operation-1",
                            "state": "issuing",
                            "attempts": [{"attempt_id": "attempt-title"}],
                        }
                    },
                },
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_text(
                json.dumps({"type": "turn_context", "turn_id": "turn-1"}) + "\n"
            )
            HookService(ledger).handle(
                replace(
                    request(("hook", "handle")),
                    input={
                        "hook_event_name": "PostToolUse",
                        "session_id": "weaver-1",
                        "turn_id": "turn-1",
                        "transcript_path": str(path),
                        "tool_name": "mcp__codex_app__set_thread_title",
                        "tool_input": {
                            "threadId": "weaver-1",
                            "title": "Weaver title",
                        },
                        "tool_response": {
                            "isError": False,
                            "structuredContent": {
                                "threadId": "weaver-1",
                                "title": "Weaver title",
                            },
                        },
                    },
                    request_id=str(uuid.uuid4()),
                )
            )
        protocol = (ledger.show("fc-work").fc or {})["desktop"]
        self.assertEqual(protocol["assignment"]["turn_id"], "turn-1")
        self.assertEqual(protocol["actions"]["action-title"]["state"], "succeeded")
        self.assertIn("weaver-1", protocol["transcripts"])

    def test_unregistered_worker_is_blocked_then_released_on_stop(self):
        ledger = MemoryLedger(
            record(
                "fc-work",
                owner="STEWARD",
                phase="ready",
                requested_role="executor",
                desktop={
                    "assignment": {
                        "assignment_token": "assignment-1",
                        "role": "executor",
                        "task_id": "worker-1",
                        "creation_action_id": "action-create",
                        "state": "issuing",
                    },
                    "actions": {},
                },
            )
        )
        hook = HookService(ledger)
        blocked = hook.handle(
            replace(
                request(("hook", "handle")),
                actor=ActorContext.parse("task:worker-1"),
                thread_id="worker-1",
                input={
                    "hook_event_name": "PreToolUse",
                    "tool_name": "exec_command",
                    "tool_input": {"cmd": "git status"},
                    "tool_use_id": "tool-read",
                },
                request_id=str(uuid.uuid4()),
            )
        )
        self.assertEqual(
            blocked.result["hookSpecificOutput"]["permissionDecision"], "deny"
        )
        allowed = hook.handle(
            replace(
                request(("hook", "handle")),
                actor=ActorContext.parse("task:worker-1"),
                thread_id="worker-1",
                input={
                    "hook_event_name": "PreToolUse",
                    "tool_name": "mcp__fulcrum__register_worker",
                    "tool_input": {"bead": "fc-work"},
                    "tool_use_id": "tool-register",
                },
                request_id=str(uuid.uuid4()),
            )
        )
        self.assertNotIn("hookSpecificOutput", allowed.result)
        hook.handle(
            replace(
                request(("hook", "handle")),
                actor=ActorContext.parse("task:worker-1"),
                thread_id="worker-1",
                input={
                    "hook_event_name": "Stop",
                    "event_id": "stop-1",
                    "turn_id": "turn-1",
                    "reason": "interrupted",
                },
                request_id=str(uuid.uuid4()),
            )
        )
        retained = ledger.show("fc-work").fc or {}
        protocol = retained["desktop"]
        self.assertNotIn("assignment", protocol)
        self.assertEqual(
            protocol["assignment_history"][-1]["state"], "registration_failed"
        )
        self.assertEqual(retained["owner"], "STEWARD")
        archival = next(
            value
            for value in protocol["actions"].values()
            if value.get("purpose") == "archive_unregistered_task:worker-1"
        )
        self.assertEqual(archival["tool"], "set_thread_archived")

        work = ledger.show("fc-work")
        fc = dict(work.fc or {})
        protocol = dict(fc["desktop"])
        protocol["assignment"] = {
            "assignment_token": "assignment-2",
            "role": "executor",
            "task_id": "worker-2",
            "creation_action_id": "action-create-2",
            "state": "issuing",
        }
        fc["desktop"] = protocol
        fc["owner"] = "STEWARD"
        ledger.update_fc("fc-work", fc, assignee="STEWARD")
        hook.handle(
            replace(
                request(("hook", "handle")),
                actor=ActorContext.parse("task:worker-2"),
                thread_id="worker-2",
                input={
                    "hook_event_name": "Stop",
                    "event_id": "stop-2",
                    "turn_id": "turn-2",
                    "reason": "interrupted",
                },
                request_id=str(uuid.uuid4()),
            )
        )
        exhausted = ledger.show("fc-work").fc or {}
        self.assertEqual(exhausted["owner"], "HUMAN")
        self.assertEqual(
            exhausted["blocked"]["reason"],
            "worker_registration_failed_repeatedly",
        )

    def test_session_only_hook_identity_binds_registered_standing_task(self):
        ledger = MemoryLedger()
        desktop = DesktopProtocolService(ledger)
        action = seed_action(
            ledger,
            {
                "executor": "bootstrap",
                "tool": "create_thread",
                "arguments": {"prompt": "register marshal"},
                "purpose": "bootstrap_marshal",
            },
        )
        observe_action_prompt(
            ledger, action, task_id="marshal-1", session_id="session-1"
        )
        desktop.register_standing(
            replace(
                request(("register", "standing")),
                input={
                    "role": "marshal",
                    "task_id": "marshal-1",
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
            HookService(ledger).handle(
                replace(
                    request(("hook", "handle")),
                    input={
                        "hook_event_name": "SessionStart",
                        "session_id": "session-1",
                        "turn_id": "turn-1",
                        "transcript_path": str(path),
                    },
                    request_id=str(uuid.uuid4()),
                )
            )
        protocol = (ledger.show("fc-system").fc or {})["desktop"]
        self.assertIn("marshal-1", protocol["transcripts"])
        self.assertEqual(
            protocol["standing"]["marshal"]["turn_id"], "turn-registration"
        )

    def test_stop_callback_keeps_standing_identity_registered(self):
        ledger = MemoryLedger()
        desktop = DesktopProtocolService(ledger)
        action = seed_action(
            ledger,
            {
                "executor": "bootstrap",
                "tool": "create_thread",
                "arguments": {"prompt": "register steward"},
                "purpose": "bootstrap_steward",
            },
        )
        observe_action_prompt(
            ledger, action, task_id="steward-1", session_id="session-1"
        )
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
        HookService(ledger).handle(
            replace(
                request(("hook", "handle")),
                actor=ActorContext.parse("task:steward-1"),
                thread_id="steward-1",
                input={
                    "hook_event_name": "Stop",
                    "event_id": "stop-1",
                    "turn_id": "turn-1",
                    "session_id": "session-1",
                },
                request_id=str(uuid.uuid4()),
            )
        )
        binding = (ledger.show("fc-system").fc or {})["desktop"]["standing"]["steward"]
        self.assertEqual(binding["state"], "registered")
        self.assertEqual(binding["last_lifecycle_event"]["event"], "Stop")

    def test_steward_hook_claim_is_resolved_on_owning_work_record(self):
        ledger = MemoryLedger(record("fc-work"))
        desktop = DesktopProtocolService(ledger)
        registration = seed_action(
            ledger,
            {
                "executor": "bootstrap",
                "tool": "create_thread",
                "arguments": {"prompt": "register steward"},
                "purpose": "bootstrap_steward",
            },
        )
        observe_action_prompt(
            ledger,
            registration,
            task_id="steward-1",
            session_id="session-1",
        )
        desktop.register_standing(
            replace(
                request(("register", "standing")),
                input={
                    "role": "steward",
                    "task_id": "steward-1",
                    "session_id": "session-1",
                    "action_id": registration["action_id"],
                },
                request_id=str(uuid.uuid4()),
            )
        )
        desktop.resume(
            replace(
                request(("resume",)),
                input={"reason": "test"},
                request_id=str(uuid.uuid4()),
            )
        )
        action = seed_action(
            ledger,
            {
                "record_id": "fc-work",
                "executor": "steward",
                "tool": "send_message_to_thread",
                "arguments": {"threadId": "worker-1", "prompt": "continue"},
            },
        )
        desktop.claim_action(
            replace(
                request(("action", "claim")),
                actor=ActorContext.parse("task:steward-1"),
                thread_id="steward-1",
                arguments={
                    "record_id": "fc-work",
                    "action_id": action["action_id"],
                },
                input={"attempt_id": "attempt-1"},
                request_id=str(uuid.uuid4()),
            )
        )
        hook = HookService(ledger)
        common = {
            "tool_name": "mcp__codex_app__send_message_to_thread",
            "tool_input": {
                "threadId": "worker-1",
                "prompt": "Fulcrum-Action: {}\ncontinue",
            },
            "tool_use_id": "tool-1",
        }
        pre = hook.handle(
            replace(
                request(("hook", "handle")),
                actor=ActorContext.parse("task:steward-1"),
                thread_id="steward-1",
                input={"hook_event_name": "PreToolUse", **common},
                request_id=str(uuid.uuid4()),
            )
        )
        self.assertNotIn("hookSpecificOutput", pre.result)
        hook.handle(
            replace(
                request(("hook", "handle")),
                actor=ActorContext.parse("task:steward-1"),
                thread_id="steward-1",
                input={
                    "hook_event_name": "PostToolUse",
                    **common,
                    "tool_response": {
                        "isError": False,
                        "messageId": "message-1",
                        "threadId": "worker-1",
                    },
                },
                request_id=str(uuid.uuid4()),
            )
        )
        retained = (ledger.show("fc-work").fc or {})["desktop"]["actions"]
        self.assertEqual(retained[action["action_id"]]["state"], "succeeded")

    def test_registered_transcript_collects_delayed_writes(self):
        ledger = MemoryLedger()
        desktop = DesktopProtocolService(ledger)
        action = seed_action(
            ledger,
            {
                "executor": "bootstrap",
                "tool": "create_thread",
                "arguments": {"prompt": "register steward"},
                "purpose": "bootstrap_steward",
            },
        )
        observe_action_prompt(
            ledger, action, task_id="steward-1", session_id="session-1"
        )
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
                            "payload": {
                                "thread_id": "steward-1",
                                "turn_id": "turn-1",
                                "response_id": "response-delayed",
                                "usage": {"input_tokens": 3, "output_tokens": 1},
                            },
                        }
                    )
                    + "\n"
                )
            watched = hook.collect_registered(request())
            writes_after_change = len(ledger.writes)
            watched_again = hook.collect_registered(request())
            retained_paths = hook.registered_paths(ledger)
        protocol = (ledger.show("fc-system").fc or {})["desktop"]
        self.assertEqual(watched, [str(path)])
        self.assertEqual(watched_again, [str(path)])
        self.assertEqual(retained_paths, [str(path)])
        self.assertEqual(len(ledger.writes), writes_after_change)
        self.assertIn("response-delayed", protocol["observations"]["usage"])
        analytics = ledger.list_records(kind="analytics", limit=0)[0]
        self.assertEqual((analytics.fc or {})["coverage"], "in_progress")
        self.assertNotIn(
            "terminal_lifecycle_missing", (analytics.fc or {})["missing_reasons"]
        )

    def test_turn_aborted_cancels_retained_wait_without_interrupt_hook(self):
        ledger = MemoryLedger()
        desktop = DesktopProtocolService(ledger)
        action = seed_action(
            ledger,
            {
                "executor": "bootstrap",
                "tool": "create_thread",
                "arguments": {"prompt": "register steward"},
                "purpose": "bootstrap_steward",
            },
        )
        observe_action_prompt(
            ledger, action, task_id="steward-1", session_id="session-1"
        )
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
        wait_request = replace(
            request(("instruction", "wait")),
            actor=ActorContext.parse("task:steward-1"),
            thread_id="steward-1",
            input={"loop_id": "loop-1", "turn_id": "generated-wait-turn"},
            request_id=str(uuid.uuid4()),
        )
        waiting = desktop.wait_for_instructions(wait_request)
        self.assertEqual(waiting.state.value, "running")
        wait_id = waiting.result["transport_wait"]["wait_id"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "type": "turn_context",
                        "turn_id": "native-turn-1",
                        "model": "gpt-5.6-luna",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            hook = HookService(ledger)
            hook.handle(
                replace(
                    request(("hook", "handle")),
                    actor=ActorContext.parse("task:steward-1"),
                    thread_id="steward-1",
                    input={
                        "hook_event_name": "SessionStart",
                        "session_id": "session-1",
                        "turn_id": "native-turn-1",
                        "transcript_path": str(path),
                    },
                    request_id=str(uuid.uuid4()),
                )
            )
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "event_msg",
                            "timestamp": "2099-01-01T00:00:00Z",
                            "payload": {
                                "type": "turn_aborted",
                                "turn_id": "native-turn-1",
                                "reason": "interrupted",
                            },
                        }
                    )
                    + "\n"
                )
            hook.collect_registered(request())
        retained = (ledger.show("fc-system").fc or {})["desktop"]["instruction_waits"][
            wait_id
        ]
        self.assertEqual(retained["state"], "cancelled")
        self.assertEqual(retained["terminal_event"]["type"], "turn_aborted")
        replayed = desktop.wait_for_instructions(wait_request)
        self.assertEqual(replayed.result["reason"], "native_turn_interrupted")

    def test_standing_tasks_keep_independent_transcript_cursors(self):
        ledger = MemoryLedger()
        desktop = DesktopProtocolService(ledger)
        bindings = {}
        for role in ("steward", "marshal"):
            action = seed_action(
                ledger,
                {
                    "executor": "bootstrap",
                    "tool": "create_thread",
                    "arguments": {"prompt": f"register {role}"},
                    "purpose": f"bootstrap_{role}",
                },
            )
            task_id = f"{role}-1"
            observe_action_prompt(
                ledger,
                action,
                task_id=task_id,
                session_id=f"session-{role}",
            )
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
        self.assertEqual(protocol["standing"]["steward"]["state"], "registered")
        self.assertEqual(protocol["standing"]["marshal"]["state"], "registered")
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
        action = seed_action(
            ledger,
            {
                "executor": "bootstrap",
                "tool": "create_thread",
                "arguments": {"prompt": "register steward"},
                "purpose": "bootstrap_steward",
            },
        )
        observe_action_prompt(
            ledger, action, task_id="steward-1", session_id="session-1"
        )
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
