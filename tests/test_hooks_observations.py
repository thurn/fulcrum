from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

from fulcrum.contracts import ActorContext
from fulcrum.coordination import ProcessLock, coordinated
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
                b'{"timestamp":"2026-09-18T00:00:00.000Z","type":"turn_started","event_id":"start","thread_id":"task-1","turn_id":"t1"}\n'
                b'{"timestamp":"2026-09-18T00:00:00.100Z","type":"turn_context","event_id":"e1","turn_id":"t1","model":"gpt-5.6-luna"}\n'
                b'{"timestamp":"2026-09-18T00:00:01.500Z","type":"token_usage_record","payload":{"thread_id":"task-1","turn_id":"t1","response_id":"r1","usage":{"input_tokens":12,"cache_write_input_tokens":3,"output_tokens":4,"reasoning_output_tokens":2}}}\n'
                b'{"timestamp":"2026-09-18T00:00:01.750Z","type":"event_msg","payload":{"type":"item_completed","thread_id":"task-1","turn_id":"t1","item":{"type":"McpToolCall","id":"tool-1","server":"fulcrum","tool":"register_worker","status":"completed","duration":{"secs":0,"nanos":125000000}},"started_at_ms":1789689601625,"completed_at_ms":1789689601750}}\n'
                b'{"type":"event_msg","payload":{"type":"turn_aborted","turn_id":"t1","reason":"interrupted"}}\n'
                b'{"type":"event_msg","payload":{"type":"turn_future","turn_id":"t2"}}\n'
                b'{"type":"turn_started"'
            )
            page = read_transcript(path)
        self.assertEqual(page.lifecycle[1]["event_id"], "e1")
        self.assertEqual(page.lifecycle[1]["model"], "gpt-5.6-luna")
        self.assertEqual(page.lifecycle[2]["type"], "turn_aborted")
        self.assertEqual(page.lifecycle[2]["reason"], "interrupted")
        self.assertEqual(page.usage[0]["response_id"], "r1")
        self.assertEqual(page.usage[0]["input_tokens"], 12)
        self.assertEqual(page.usage[0]["cache_write_tokens"], 3)
        self.assertEqual(page.usage[0]["reasoning_tokens"], 2)
        self.assertEqual(page.timings[0]["kind"], "model_response")
        self.assertEqual(page.timings[0]["duration_ms"], 1500.0)
        self.assertEqual(page.timings[0]["correlation_id"], "task-1:t1")
        self.assertEqual(page.timings[1]["kind"], "mcp_tool")
        self.assertEqual(page.timings[1]["duration_ms"], 125.0)
        self.assertEqual(page.timings[1]["parent_span_id"], page.timings[0]["span_id"])
        self.assertEqual(page.timings[1]["tool"], "register_worker")
        self.assertEqual(page.gaps[0]["kind"], "unrecognized_lifecycle_event")
        self.assertEqual(page.gaps[0]["event_type"], "turn_future")
        self.assertEqual(page.gaps[1]["kind"], "incomplete_trailing_line")

    def test_parser_carries_response_timing_across_incremental_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            first = (
                '{"timestamp":"2026-09-18T00:00:02.000Z","type":"response_item",'
                '"payload":{"type":"custom_tool_call_output"}}\n'
            )
            path.write_text(first, encoding="utf-8")
            initial = read_transcript(path)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    '{"timestamp":"2026-09-18T00:00:03.250Z",'
                    '"type":"token_usage_record","payload":{"thread_id":"task-1",'
                    '"turn_id":"turn-1","response_id":"response-1","usage":{}}}\n'
                )
            followup = read_transcript(
                path, initial.cursor, timing_state=initial.timing_state
            )

        self.assertEqual(len(followup.timings), 1)
        self.assertEqual(followup.timings[0]["duration_ms"], 1250.0)
        self.assertEqual(
            followup.timings[0]["span_id"], "task-1:turn-1:response:response-1"
        )


class HookTests(unittest.TestCase):
    def test_fulcrum_mcp_hooks_do_not_read_the_ledger(self):
        class FailingLedger(MemoryLedger):
            def show(self, record_id):
                raise AssertionError("Fulcrum MCP hook must not read the ledger")

            def list_records(self, *, kind=None, limit=0, assignee=None):
                raise AssertionError("Fulcrum MCP hook must not scan the ledger")

        hook = HookService(FailingLedger())
        for event_name, expected in (
            ("PreToolUse", {}),
            ("PostToolUse", {"continue": True}),
        ):
            result = hook.handle(
                replace(
                    request(("hook", "handle")),
                    input={
                        "hook_event_name": event_name,
                        "session_id": "task-1",
                        "tool_name": "mcp__fulcrum__enter_weaver",
                        "tool_use_id": "tool-1",
                    },
                )
            )
            self.assertEqual(result.result, expected)

    def test_transport_snapshot_does_not_hold_global_transition_lock(self):
        started = threading.Event()
        release = threading.Event()

        class SlowLedger(MemoryLedger):
            def list_records(self, *, kind=None, limit=0, assignee=None):
                started.set()
                self.assert_released(release)
                return []

            @staticmethod
            def assert_released(event):
                if not event.wait(timeout=2):
                    raise AssertionError("test did not release transport snapshot")

        ledger = SlowLedger()
        snapshot_request = request(("transport", "snapshot"))
        thread = threading.Thread(
            target=DesktopProtocolService(ledger).transport_snapshot,
            args=(snapshot_request,),
        )
        thread.start()
        self.assertTrue(started.wait(timeout=1))
        try:
            state_lock = snapshot_request.instance.brain_root / ".fulcrum-locks/state"
            with ProcessLock(state_lock, blocking=False):
                pass
        finally:
            release.set()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())

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

    def test_collection_retries_completion_without_new_transcript_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker.jsonl"
            row = {
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "native-turn"},
            }
            content = json.dumps(row) + "\n"
            path.write_text(content, encoding="utf-8")
            ledger = MemoryLedger(
                record(
                    "fc-work",
                    owner="STEWARD",
                    phase="ready",
                    role=None,
                    ownership_operation="assignment-1",
                    desktop={
                        "assignment": {
                            "assignment_token": "assignment-1",
                            "role": "weaver",
                            "task_id": "worker-1",
                            "turn_id": "native-turn",
                            "state": "active",
                            "finish_operation": "finish-1",
                        },
                        "actions": {
                            "title-1": {
                                "action_id": "title-1",
                                "executor": "weaver",
                                "assignment_token": "assignment-1",
                                "state": "succeeded",
                            }
                        },
                        "observations": {
                            "lifecycle": {
                                "done": {
                                    "type": "task_complete",
                                    "task_id": "worker-1",
                                    "turn_id": "native-turn",
                                }
                            }
                        },
                        "transcripts": {
                            "worker-1": {
                                "path": str(path),
                                "cursor": len(content.encode()),
                                "gaps": [],
                            }
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

    def test_active_assignment_discovers_missing_native_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = (
                Path(directory)
                / ".codex"
                / "sessions"
                / "2026"
                / "09"
                / "18"
                / "rollout-worker-native.jsonl"
            )
            transcript.parent.mkdir(parents=True)
            transcript.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-09-18T00:00:00Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "turn_id": "native-turn",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            ledger = MemoryLedger(
                record(
                    "fc-work",
                    owner="worker-native",
                    phase="reviewing",
                    role="warden",
                    ownership_operation="assignment-1",
                    desktop={
                        "assignment": {
                            "assignment_token": "assignment-1",
                            "role": "warden",
                            "task_id": "worker-native",
                            "turn_id": None,
                            "state": "active",
                            "finish_operation": "finish-1",
                        }
                    },
                )
            )

            with patch("fulcrum.hooks.Path.home", return_value=Path(directory)):
                watched = HookService(ledger).collect_active_assignments(request())

        retained = ledger.show("fc-work")
        protocol = (retained.fc or {})["desktop"]
        self.assertEqual(watched, [str(transcript.resolve())])
        self.assertNotIn("assignment", protocol)
        self.assertEqual(protocol["assignment_history"][-1]["turn_id"], "native-turn")

    def test_transport_snapshot_reconciles_terminal_transcript_before_watch(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "worker-terminal.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-09-18T00:00:00Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "turn_id": "native-turn",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            ledger = MemoryLedger(
                record(
                    "fc-work",
                    owner="worker-native",
                    phase="reviewing",
                    role="warden",
                    ownership_operation="assignment-1",
                    desktop={
                        "assignment": {
                            "assignment_token": "assignment-1",
                            "role": "warden",
                            "task_id": "worker-native",
                            "turn_id": None,
                            "state": "active",
                            "finish_operation": "finish-1",
                        },
                        "transcripts": {
                            "worker-native": {
                                "path": str(transcript),
                                "cursor": 0,
                                "gaps": [],
                            }
                        },
                    },
                )
            )

            result = DesktopProtocolService(ledger).transport_snapshot(
                request(("transport", "snapshot"))
            )

        retained = ledger.show("fc-work")
        protocol = (retained.fc or {})["desktop"]
        self.assertNotIn("assignment", protocol)
        self.assertEqual(protocol["assignment_history"][-1]["turn_id"], "native-turn")
        self.assertEqual(result.result["watch_paths"], [])
        analytics = ledger.list_records(kind="analytics", limit=0)
        self.assertEqual(len(analytics), 1)
        self.assertEqual((analytics[0].fc or {})["turn_id"], "native-turn")
        self.assertEqual((analytics[0].fc or {})["terminal_state"], "task_complete")

    def test_transport_snapshot_discovers_and_collects_standing_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "steward.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "event_msg",
                        "timestamp": "2026-09-18T00:00:00Z",
                        "payload": {
                            "type": "turn_aborted",
                            "turn_id": "steward-turn",
                            "reason": "interrupted",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            ledger = MemoryLedger(
                record(
                    "fc-system",
                    kind="control",
                    desktop={
                        "standing": {
                            "steward": {
                                "role": "steward",
                                "task_id": "steward-1",
                                "state": "registered",
                            }
                        }
                    },
                )
            )
            with patch(
                "fulcrum.hooks._discover_native_transcript",
                return_value=str(transcript),
            ):
                result = DesktopProtocolService(ledger).transport_snapshot(
                    request(("transport", "snapshot"))
                )

        desktop = (ledger.show("fc-system").fc or {})["desktop"]
        self.assertEqual(desktop["standing"]["steward"]["state"], "interrupt_observed")
        self.assertEqual(result.result["watch_paths"], [str(transcript)])

    def test_standing_transcript_discovery_reads_only_a_bounded_recent_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "steward.jsonl"
            prefix = (
                json.dumps({"type": "response_item", "payload": "x" * 1000}) + "\n"
            ) * 700
            terminal = (
                json.dumps(
                    {
                        "type": "event_msg",
                        "timestamp": "2026-09-18T00:00:00Z",
                        "payload": {
                            "type": "turn_aborted",
                            "turn_id": "steward-turn",
                            "reason": "interrupted",
                        },
                    }
                )
                + "\n"
            )
            transcript.write_text(prefix + terminal, encoding="utf-8")
            ledger = MemoryLedger(
                record(
                    "fc-system",
                    kind="control",
                    desktop={
                        "standing": {
                            "steward": {
                                "role": "steward",
                                "task_id": "steward-1",
                                "state": "registered",
                            }
                        }
                    },
                )
            )
            with patch(
                "fulcrum.hooks._discover_native_transcript",
                return_value=str(transcript),
            ):
                DesktopProtocolService(ledger).transport_snapshot(
                    request(("transport", "snapshot"))
                )

        desktop = (ledger.show("fc-system").fc or {})["desktop"]
        retained = desktop["transcripts"]["steward-1"]
        self.assertEqual(desktop["standing"]["steward"]["state"], "interrupt_observed")
        self.assertEqual(retained["history_scope"], "recent_tail")
        self.assertGreater(retained["history_start_cursor"], 0)
        self.assertEqual(retained["cursor"], len((prefix + terminal).encode()))

    def test_active_assignment_paths_omit_completed_and_standing_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            active = Path(directory) / "active.jsonl"
            completed = Path(directory) / "completed.jsonl"
            standing = Path(directory) / "standing.jsonl"
            for path in (active, completed, standing):
                path.write_text("", encoding="utf-8")
            ledger = MemoryLedger(
                record(
                    "fc-system",
                    kind="control",
                    desktop={
                        "standing": {"steward": {"task_id": "steward-1"}},
                        "transcripts": {
                            "steward-1": {"path": str(standing), "cursor": 0}
                        },
                    },
                ),
                record(
                    "fc-active",
                    desktop={
                        "assignment": {"task_id": "worker-1", "state": "active"},
                        "transcripts": {"worker-1": {"path": str(active), "cursor": 0}},
                    },
                ),
                record(
                    "fc-completed",
                    status="closed",
                    phase="done",
                    desktop={
                        "assignment_history": [{"task_id": "worker-old"}],
                        "transcripts": {
                            "worker-old": {"path": str(completed), "cursor": 0}
                        },
                    },
                ),
            )

            watched = HookService.active_assignment_paths(ledger)

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
        self.assertFalse(
            any(
                value.get("purpose") == "archive_unregistered_task:worker-1"
                for value in protocol["actions"].values()
            )
        )
        incident = protocol["incidents"]["registration:assignment-1"]
        self.assertTrue(
            incident["recovery"]["task_archival_deferred_until_workflow_cleanup"]
        )

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

    def test_uncertain_created_worker_stop_retains_exact_assignment_for_marshal(self):
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
                        "state": "uncertain",
                    },
                    "actions": {
                        "action-create": {
                            "action_id": "action-create",
                            "record_id": "fc-work",
                            "executor": "steward",
                            "tool": "create_thread",
                            "purpose": "routine_dispatch",
                            "state": "uncertain",
                        }
                    },
                    "incidents": {
                        "native-action:action-create": {
                            "incident_id": "incident-1",
                            "incident_key": "native-action:action-create",
                            "action_id": "action-create",
                            "state": "open",
                            "evidence": {},
                        }
                    },
                },
            )
        )

        HookService(ledger).handle(
            replace(
                request(("hook", "handle")),
                actor=ActorContext.parse("task:worker-1"),
                thread_id="worker-1",
                input={
                    "hook_event_name": "Stop",
                    "event_id": "stop-uncertain",
                    "turn_id": "turn-1",
                    "reason": "completed",
                },
                request_id=str(uuid.uuid4()),
            )
        )

        desktop = (ledger.show("fc-work").fc or {})["desktop"]
        self.assertEqual(desktop["assignment"]["task_id"], "worker-1")
        self.assertEqual(desktop["assignment"]["state"], "uncertain")
        self.assertTrue(desktop["assignment"]["awaiting_native_action_recovery"])
        self.assertNotIn("registration_failures", desktop)
        evidence = desktop["incidents"]["native-action:action-create"]["evidence"]
        self.assertEqual(
            evidence["registration_terminal_event"]["event_id"], "stop-uncertain"
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
        paused_stop = HookService(ledger).handle(
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
        self.assertNotIn("decision", paused_stop.result)
        binding = (ledger.show("fc-system").fc or {})["desktop"]["standing"]["steward"]
        self.assertEqual(binding["state"], "registered")
        self.assertEqual(binding["last_lifecycle_event"]["event"], "Stop")

        system = ledger.show("fc-system")
        fc = dict(system.fc or {})
        protocol = dict(fc["desktop"])
        protocol["run_control"] = "running"
        fc["desktop"] = protocol
        ledger.update_fc("fc-system", fc)
        corrected = HookService(ledger).handle(
            replace(
                request(("hook", "handle")),
                actor=ActorContext.parse("task:steward-1"),
                thread_id="steward-1",
                input={
                    "hook_event_name": "Stop",
                    "event_id": "stop-2",
                    "turn_id": "turn-2",
                    "session_id": "session-1",
                    "stop_hook_active": False,
                },
                request_id=str(uuid.uuid4()),
            )
        )
        self.assertEqual(corrected.result["decision"], "block")
        self.assertIn("wait_for_instructions", corrected.result["reason"])

        repeated = HookService(ledger).handle(
            replace(
                request(("hook", "handle")),
                actor=ActorContext.parse("task:steward-1"),
                thread_id="steward-1",
                input={
                    "hook_event_name": "Stop",
                    "event_id": "stop-3",
                    "turn_id": "turn-2",
                    "session_id": "session-1",
                    "stop_hook_active": True,
                },
                request_id=str(uuid.uuid4()),
            )
        )
        self.assertNotIn("decision", repeated.result)

        system = ledger.show("fc-system")
        fc = dict(system.fc or {})
        protocol = dict(fc["desktop"])
        protocol["instruction_waits"] = {
            "wait-idle": {
                "wait_id": "wait-idle",
                "task_id": "steward-1",
                "turn_id": "turn-idle",
                "state": "expired",
                "response": {
                    "kind": "stop",
                    "reason": "idle_deadline",
                    "retained_obligation": False,
                },
            }
        }
        fc["desktop"] = protocol
        ledger.update_fc("fc-system", fc)
        idle_stop = HookService(ledger).handle(
            replace(
                request(("hook", "handle")),
                actor=ActorContext.parse("task:steward-1"),
                thread_id="steward-1",
                input={
                    "hook_event_name": "Stop",
                    "event_id": "stop-idle",
                    "turn_id": "turn-idle",
                    "session_id": "session-1",
                    "stop_hook_active": False,
                },
                request_id=str(uuid.uuid4()),
            )
        )
        self.assertNotIn("decision", idle_stop.result)

        system = ledger.show("fc-system")
        fc = dict(system.fc or {})
        protocol = dict(fc["desktop"])
        protocol["instruction_waits"] = {
            f"wait-{index}": {
                "wait_id": f"wait-{index}",
                "task_id": "steward-1",
                "turn_id": "turn-max",
                "state": "resolved",
                "response": {"kind": "action"},
            }
            for index in range(32)
        }
        fc["desktop"] = protocol
        ledger.update_fc("fc-system", fc)
        bounded_stop = HookService(ledger).handle(
            replace(
                request(("hook", "handle")),
                actor=ActorContext.parse("task:steward-1"),
                thread_id="steward-1",
                input={
                    "hook_event_name": "Stop",
                    "event_id": "stop-max",
                    "turn_id": "turn-max",
                    "session_id": "session-1",
                    "stop_hook_active": False,
                },
                request_id=str(uuid.uuid4()),
            )
        )
        self.assertNotIn("decision", bounded_stop.result)

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
        self.assertIn(
            "steward-1:turn-1:response-delayed",
            protocol["observations"]["usage"],
        )
        analytics = ledger.list_records(kind="analytics", limit=0)[0]
        self.assertEqual((analytics.fc or {})["coverage"], "in_progress")
        self.assertNotIn(
            "terminal_lifecycle_missing", (analytics.fc or {})["missing_reasons"]
        )

    def test_long_standing_transcript_append_reconciles_only_affected_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "marshal.jsonl"
            historical = []
            observations = {"lifecycle": {}, "usage": {}}
            for index in range(100):
                turn_id = f"turn-{index}"
                context = {
                    "type": "turn_context",
                    "turn_id": turn_id,
                    "model": "gpt-5.6-sol",
                }
                usage = {
                    "type": "token_usage_record",
                    "payload": {
                        "thread_id": "marshal-1",
                        "turn_id": turn_id,
                        "response_id": f"response-{index}",
                        "usage": {"input_tokens": index + 1, "output_tokens": 1},
                    },
                }
                historical.extend((context, usage))
                observations["lifecycle"][f"marshal-1:{turn_id}:turn_context"] = {
                    **context,
                    "task_id": "marshal-1",
                }
                observations["usage"][f"marshal-1:{turn_id}:response-{index}"] = {
                    "task_id": "marshal-1",
                    "turn_id": turn_id,
                    "response_id": f"response-{index}",
                    "model": "gpt-5.6-sol",
                    "input_tokens": index + 1,
                    "output_tokens": 1,
                }
            prefix = "".join(json.dumps(row) + "\n" for row in historical)
            path.write_text(prefix, encoding="utf-8")
            ledger = MemoryLedger(
                record(
                    "fc-system",
                    kind="control",
                    desktop={
                        "standing": {
                            "marshal": {
                                "role": "marshal",
                                "task_id": "marshal-1",
                                "turn_id": "turn-99",
                                "state": "registered",
                            }
                        },
                        "observations": observations,
                        "transcripts": {
                            "marshal-1": {
                                "path": str(path),
                                "cursor": len(prefix.encode()),
                                "gaps": [],
                            }
                        },
                    },
                )
            )
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "token_usage_record",
                            "payload": {
                                "thread_id": "marshal-1",
                                "turn_id": "turn-99",
                                "response_id": "response-new",
                                "usage": {"input_tokens": 5, "output_tokens": 2},
                            },
                        }
                    )
                    + "\n"
                )

            HookService(ledger).collect_standing(request())

        analytics_writes = [
            record_id
            for record_id in ledger.writes
            if (ledger.show(record_id).fc or {}).get("kind") == "analytics"
        ]
        self.assertEqual(len(analytics_writes), 1)
        analytics = ledger.show(analytics_writes[0]).fc or {}
        self.assertEqual(analytics["turn_id"], "turn-99")
        self.assertEqual(len(analytics["raw_responses"]), 1)

    def test_transport_snapshot_releases_state_lock_during_transcript_scan(self):
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()

        class MarshalProbe:
            @coordinated
            def marshal_check(self, _request):
                completed.set()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "marshal.jsonl"
            path.write_text("", encoding="utf-8")
            ledger = MemoryLedger(
                record(
                    "fc-system",
                    kind="control",
                    desktop={
                        "standing": {
                            "marshal": {
                                "role": "marshal",
                                "task_id": "marshal-1",
                                "state": "registered",
                            }
                        },
                        "transcripts": {
                            "marshal-1": {"path": str(path), "cursor": 0, "gaps": []}
                        },
                    },
                )
            )
            original = read_transcript

            def slow_read(*args, **kwargs):
                entered.set()
                self.assertTrue(release.wait(2))
                return original(*args, **kwargs)

            snapshot_request = replace(request(("transport", "snapshot")), arguments={})
            marshal_request = replace(request(("marshal", "check")), arguments={})
            snapshot = threading.Thread(
                target=DesktopProtocolService(ledger).transport_snapshot,
                args=(snapshot_request,),
            )
            with patch("fulcrum.hooks.read_transcript", side_effect=slow_read):
                snapshot.start()
                try:
                    self.assertTrue(entered.wait(1))
                    MarshalProbe().marshal_check(marshal_request)
                    self.assertTrue(completed.is_set())
                    self.assertTrue(snapshot.is_alive())
                finally:
                    release.set()
                    snapshot.join(2)

        self.assertFalse(snapshot.is_alive())

    def test_transport_snapshot_refreshes_state_after_unlocked_scan(self):
        listed = threading.Event()
        release = threading.Event()
        result = []

        class CachedLedger(MemoryLedger):
            def __init__(self, *records):
                super().__init__(*records)
                self._listed_records = None
                self._blocked_once = False

            def list_records(self, *, kind=None, limit=0):
                if self._listed_records is None:
                    self._listed_records = super().list_records(limit=0)
                    if not self._blocked_once:
                        self._blocked_once = True
                        listed.set()
                        self.assert_release()
                return [
                    row
                    for row in self._listed_records
                    if kind is None or row.kind == kind
                ]

            @staticmethod
            def assert_release():
                if not release.wait(2):
                    raise AssertionError("snapshot scan was not released")

        ledger = CachedLedger(record("fc-system", kind="control", desktop={}))
        snapshot_request = replace(request(("transport", "snapshot")), arguments={})
        snapshot = threading.Thread(
            target=lambda: result.append(
                DesktopProtocolService(ledger).transport_snapshot(snapshot_request)
            )
        )
        snapshot.start()
        try:
            self.assertTrue(listed.wait(1))
            ledger.create_record(
                record_id="fc-new-wait",
                kind="work",
                title="new wait",
                description="created during transcript reconstruction",
                owner="worker",
                fc={
                    "kind": "work",
                    "owner": "worker",
                    "desktop": {
                        "instruction_waits": {
                            "wait-new": {
                                "wait_id": "wait-new",
                                "state": "waiting",
                                "deadline": "2099-01-01T00:00:00Z",
                            }
                        }
                    },
                },
            )
        finally:
            release.set()
            snapshot.join(2)

        self.assertFalse(snapshot.is_alive())
        self.assertEqual(result[0].result["waits"][0]["wait_id"], "wait-new")

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
        standing = (ledger.show("fc-system").fc or {})["desktop"]["standing"]
        self.assertEqual(standing["steward"]["state"], "interrupt_observed")
        replayed = desktop.wait_for_instructions(wait_request)
        self.assertEqual(replayed.result["reason"], "native_turn_interrupted")

    def test_standing_tasks_keep_independent_transcript_cursors(self):
        ledger = MemoryLedger()
        desktop = DesktopProtocolService(ledger)
        bindings = {}
        paths = {}
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
                        "ordinal": 7,
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
                paths[role] = path
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
            with paths["steward"].open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "ordinal": 59,
                            "type": "token_usage_record",
                            "payload": {
                                "thread_id": bindings["steward"],
                                "turn_id": "turn-steward",
                                "response_id": "response-steward-late",
                                "usage": {"input_tokens": 12, "output_tokens": 3},
                            },
                        }
                    )
                    + "\n"
                )
            HookService(ledger).collect_registered(request())
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
            self.assertNotIn(
                "effective_model_missing", (analytics.fc or {})["missing_reasons"]
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
