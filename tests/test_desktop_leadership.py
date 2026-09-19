from __future__ import annotations

from dataclasses import replace
import unittest
import uuid
from unittest.mock import patch

from fulcrum.contracts import ActorContext, FulcrumError
from fulcrum.desktop_leadership import DesktopLeadershipService
from tests.support import (
    MemoryLedger,
    observe_action_prompt,
    record,
    request,
    seed_action,
)


def call(command, *, actor="human", arguments=None, payload=None):
    task = actor.removeprefix("task:") if actor.startswith("task:") else None
    return replace(
        request(command),
        actor=ActorContext.parse(actor),
        thread_id=task,
        arguments=arguments or {},
        input=payload or {},
        request_id=str(uuid.uuid4()),
    )


class LeadershipTests(unittest.TestCase):
    def setUp(self):
        self.work = record(
            "fc-a",
            phase="ready",
            priority=1,
            project="toy",
            workspace="/tmp/worktree",
            codex_project_id="project-1",
            requested_role="executor",
            scope={
                "summary": "Implement the approved change",
                "acceptance": ["The approved behavior is present"],
                "evidence": [],
                "implementation_notes": [],
                "finish_operation": "fc-op-scope",
            },
        )
        self.ledger = MemoryLedger(self.work)
        self.service = DesktopLeadershipService(self.ledger)
        for role, task in (("steward", "steward-1"), ("marshal", "marshal-1")):
            action = seed_action(
                self.ledger,
                {
                    "executor": "bootstrap",
                    "tool": "create_thread",
                    "arguments": {"prompt": f"register {role}"},
                    "purpose": f"bootstrap_{role}",
                },
            )
            observe_action_prompt(self.ledger, action, task_id=task, session_id=task)
            self.service.register_standing(
                call(
                    ("register", "standing"),
                    payload={
                        "role": role,
                        "task_id": task,
                        "session_id": task,
                        "action_id": action["action_id"],
                    },
                )
            )
        self.service.resume(call(("resume",), payload={"reason": "test setup"}))

    def _record_idle_steward(self, *, live=False):
        system = self.ledger.show("fc-system")
        desktop = dict(system.fc["desktop"])
        standing = dict(desktop["standing"])
        standing["steward"] = {
            **standing["steward"],
            "state": "registered",
            "turn_id": "idle-turn",
        }
        desktop["standing"] = standing
        desktop["instruction_waits"] = {
            "idle-wait": {
                "wait_id": "idle-wait",
                "task_id": "steward-1",
                "turn_id": "idle-turn",
                "state": "waiting" if live else "expired",
                **(
                    {}
                    if live
                    else {
                        "response": {
                            "kind": "stop",
                            "reason": "idle_deadline",
                            "retained_obligation": False,
                        }
                    }
                ),
            }
        }
        desktop["observations"] = {
            "lifecycle": {
                "idle-complete": {
                    "type": "task_complete",
                    "task_id": "steward-1",
                    "turn_id": "idle-turn",
                }
            }
        }
        self.ledger.update_fc("fc-system", {**system.fc, "desktop": desktop})

    def test_ready_work_compiles_without_marshal_approval(self):
        self.service.resume(call(("resume",), payload={"reason": "test"}))
        result = self.service.wait_for_instructions(
            call(
                ("instruction", "wait"),
                actor="task:steward-1",
                payload={"loop_id": "loop-1", "turn_id": "turn-1"},
            )
        )
        self.assertEqual(result.result["kind"], "action")
        self.assertEqual(result.result["action"]["tool"], "create_thread")
        assignment = self.ledger.show("fc-a").fc["desktop"]["assignment"]
        self.assertEqual(assignment["state"], "reserved")

    @patch("fulcrum.desktop_leadership._active_broker_wait_ids", return_value=set())
    def test_marshal_classifies_completed_idle_deadline_and_wakes_same_steward(
        self, _active_waits
    ):
        self._record_idle_steward()

        checked = self.service.marshal_check(
            call(
                ("marshal", "check"),
                actor="task:marshal-1",
                payload={"turn_id": "marshal-idle-turn"},
            )
        )

        self.assertEqual(checked.result["brief"]["steward_health"], "idle")
        action = checked.result["brief"]["recovery_action"]
        self.assertEqual(action["tool"], "send_message_to_thread")
        self.assertEqual(action["executor"], "marshal")
        self.assertEqual(action["arguments"]["threadId"], "steward-1")
        self.assertEqual(action["reporting"]["idle_turn_id"], "idle-turn")

    @patch("fulcrum.desktop_leadership._active_broker_wait_ids", return_value=set())
    def test_marshal_does_nothing_for_idle_steward_without_ready_work(
        self, _active_waits
    ):
        self._record_idle_steward()
        work = self.ledger.show("fc-a")
        self.ledger.update_fc("fc-a", work.fc, status="closed")

        checked = self.service.marshal_check(
            call(
                ("marshal", "check"),
                actor="task:marshal-1",
                payload={"turn_id": "marshal-idle-no-work"},
            )
        )

        self.assertEqual(checked.result["brief"]["steward_health"], "idle")
        self.assertIsNone(checked.result["brief"]["recovery_action"])

    @patch(
        "fulcrum.desktop_leadership._active_broker_wait_ids",
        return_value={"idle-wait"},
    )
    def test_marshal_preserves_healthy_steward_wait(self, _active_waits):
        self._record_idle_steward(live=True)

        checked = self.service.marshal_check(
            call(
                ("marshal", "check"),
                actor="task:marshal-1",
                payload={"turn_id": "marshal-live-wait"},
            )
        )

        self.assertEqual(checked.result["brief"]["steward_health"], "healthy_wait")
        self.assertIsNone(checked.result["brief"]["recovery_action"])

    @patch("fulcrum.desktop_leadership._active_broker_wait_ids", return_value=set())
    def test_marshal_does_not_wake_idle_steward_when_paused(self, _active_waits):
        self._record_idle_steward()
        self.service.pause(call(("pause",), payload={"reason": "test"}))

        checked = self.service.marshal_check(
            call(
                ("marshal", "check"),
                actor="task:marshal-1",
                payload={"turn_id": "marshal-paused"},
            )
        )

        self.assertEqual(checked.result["brief"]["steward_health"], "idle")
        self.assertIsNone(checked.result["brief"]["recovery_action"])

    @patch("fulcrum.desktop_leadership._active_broker_wait_ids", return_value=set())
    def test_marshal_does_not_retry_unresolved_idle_wake(self, _active_waits):
        self._record_idle_steward()
        work = self.ledger.show("fc-a")
        desktop = dict(work.fc.get("desktop") or {})
        desktop["actions"] = {
            "uncertain-wake": {
                "action_id": "uncertain-wake",
                "record_id": "fc-a",
                "executor": "weaver",
                "tool": "send_message_to_thread",
                "arguments": {"threadId": "steward-1", "prompt": "resume"},
                "reporting": {"idle_turn_id": "idle-turn"},
                "state": "uncertain",
                "attempts": [{"attempt_id": "wake-attempt", "state": "uncertain"}],
                "purpose": "recover_steward_loop",
            }
        }
        self.ledger.update_fc("fc-a", {**work.fc, "desktop": desktop})

        checked = self.service.marshal_check(
            call(
                ("marshal", "check"),
                actor="task:marshal-1",
                payload={"turn_id": "marshal-uncertain"},
            )
        )

        self.assertIsNone(checked.result["brief"]["recovery_action"])
        actions = [
            value
            for row in self.ledger.list_records(limit=0)
            for value in ((row.fc or {}).get("desktop", {}).get("actions", {})).values()
            if value.get("purpose") == "recover_steward_loop"
        ]
        self.assertEqual(len(actions), 1)

    @patch("fulcrum.desktop_leadership._active_broker_wait_ids", return_value=set())
    def test_marshal_replaces_missed_completed_weaver_wake_once(self, _active_waits):
        self._record_idle_steward()
        work = self.ledger.show("fc-a")
        desktop = dict(work.fc.get("desktop") or {})
        desktop["assignment"] = {
            "assignment_token": "weaver-token",
            "role": "weaver",
            "task_id": "weaver-1",
            "turn_id": "weaver-turn",
            "state": "active",
            "finish_operation": "weaver-finish",
        }
        desktop["observations"] = {
            "lifecycle": {
                "weaver-complete": {
                    "type": "task_complete",
                    "task_id": "weaver-1",
                    "turn_id": "weaver-turn",
                }
            }
        }
        desktop["actions"] = {
            "weaver-wake": {
                "action_id": "weaver-wake",
                "record_id": "fc-a",
                "executor": "weaver",
                "tool": "send_message_to_thread",
                "arguments": {"threadId": "steward-1", "prompt": "resume"},
                "expected_result": {"thread_id": "steward-1"},
                "reporting": {"idle_turn_id": "idle-turn"},
                "assignment_token": "weaver-token",
                "state": "pending",
                "attempts": [],
                "purpose": "recover_steward_loop",
            }
        }
        self.ledger.update_fc("fc-a", {**work.fc, "desktop": desktop})

        checked = self.service.marshal_check(
            call(
                ("marshal", "check"),
                actor="task:marshal-1",
                payload={"turn_id": "marshal-missed-wake"},
            )
        )

        self.assertEqual(
            self.ledger.show("fc-a").fc["desktop"]["actions"]["weaver-wake"]["state"],
            "superseded",
        )
        action = checked.result["brief"]["recovery_action"]
        self.assertEqual(action["executor"], "marshal")
        repeated_actions = [
            value
            for row in self.ledger.list_records(limit=0)
            for value in ((row.fc or {}).get("desktop", {}).get("actions", {})).values()
            if value.get("purpose") == "recover_steward_loop"
            and value.get("state") == "pending"
        ]
        self.assertEqual(len(repeated_actions), 1)

    def test_scheduled_marshal_delivery_revives_identity_and_records_health(self):
        system = self.ledger.show("fc-system")
        desktop = dict(system.fc["desktop"])
        standing = dict(desktop["standing"])
        standing["marshal"] = {**standing["marshal"], "state": "stopped"}
        desktop["standing"] = standing
        desktop["marshal_schedule"] = {
            "state": "succeeded",
            "status": "ACTIVE",
            "automation_id": "marshal-check",
            "target_task_id": "marshal-1",
            "activated_at": "2026-09-17T00:00:00Z",
        }
        desktop["observations"] = {
            "lifecycle": {
                "heartbeat-start": {
                    "type": "task_started",
                    "task_id": "marshal-1",
                    "turn_id": "heartbeat-turn",
                    "time": "2026-09-17T00:01:00Z",
                }
            }
        }
        self.ledger.update_fc("fc-system", {**system.fc, "desktop": desktop})

        checked = self.service.marshal_check(
            call(
                ("marshal", "check"),
                actor="task:marshal-1",
                payload={"trigger": "heartbeat"},
            )
        )

        retained = self.ledger.show("fc-system").fc["desktop"]
        self.assertEqual(retained["standing"]["marshal"]["state"], "registered")
        self.assertEqual(retained["marshal_schedule"]["delivery_count"], 1)
        self.assertTrue(
            retained["marshal_schedule"]["last_delivery_turn_id"].startswith(
                "heartbeat:"
            )
        )
        self.assertTrue(checked.result["decision"]["turn_id"].startswith("heartbeat:"))

        decided = self.service.marshal_decide(
            call(
                ("marshal", "apply"),
                actor="task:marshal-1",
                payload={
                    "turn_id": checked.result["decision"]["turn_id"],
                    "decision_id": checked.result["decision"]["decision_id"],
                    "decisions": [],
                },
            )
        )
        self.assertEqual(decided.result["decision"]["state"], "completed")
        retained = self.ledger.show("fc-system").fc["desktop"]
        self.assertEqual(retained["marshal_schedule"]["completed_cycle_count"], 1)
        self.assertEqual(
            retained["marshal_schedule"]["last_cycle_turn_id"],
            checked.result["decision"]["turn_id"],
        )

    def test_marshal_check_does_not_create_decision_without_turn_evidence(self):
        with self.assertRaisesRegex(FulcrumError, "native turn has not been observed"):
            self.service.marshal_check(
                call(("marshal", "check"), actor="task:marshal-1")
            )
        system = self.ledger.show("fc-system")
        self.assertNotIn("marshal_decision", system.fc["desktop"])

    def test_completed_turn_replaces_unsettled_marshal_decision_with_fresh_brief(self):
        first = self.service.marshal_check(
            call(
                ("marshal", "check"),
                actor="task:marshal-1",
                payload={"turn_id": "turn-1"},
            )
        )
        self.ledger.rows["fc-fresh"] = record(
            "fc-fresh", owner="marshal-1", phase="ready", priority=0
        )
        system = self.ledger.show("fc-system")
        desktop = dict(system.fc["desktop"])
        observations = dict(desktop.get("observations") or {})
        observations["lifecycle"] = {
            "turn-1-complete": {
                "type": "turn_complete",
                "task_id": "marshal-1",
                "turn_id": "turn-1",
            }
        }
        desktop["observations"] = observations
        self.ledger.update_fc("fc-system", {**system.fc, "desktop": desktop})

        second = self.service.marshal_check(
            call(
                ("marshal", "check"),
                actor="task:marshal-1",
                payload={"turn_id": "turn-2"},
            )
        )

        self.assertFalse(second.result["joined"])
        self.assertNotEqual(
            first.result["decision"]["decision_id"],
            second.result["decision"]["decision_id"],
        )
        self.assertEqual(second.result["brief"]["ready"][0]["bead"], "fc-fresh")
        history = self.ledger.show("fc-system").fc["desktop"][
            "marshal_decision_history"
        ]
        self.assertEqual(history[-1]["state"], "superseded")

    def test_same_turn_rejoins_unsettled_marshal_decision_with_its_brief(self):
        first = self.service.marshal_check(
            call(
                ("marshal", "check"),
                actor="task:marshal-1",
                payload={"turn_id": "turn-1"},
            )
        )

        second = self.service.marshal_check(
            call(
                ("marshal", "check"),
                actor="task:marshal-1",
                payload={"turn_id": "turn-1"},
            )
        )

        self.assertTrue(second.result["joined"])
        self.assertEqual(second.result["brief"], first.result["brief"])

    def test_next_heartbeat_replaces_unsettled_heartbeat_without_terminal_hook(self):
        system = self.ledger.show("fc-system")
        desktop = dict(system.fc["desktop"])
        desktop["marshal_schedule"] = {
            "state": "succeeded",
            "status": "ACTIVE",
            "automation_id": "marshal-check",
            "target_task_id": "marshal-1",
        }
        self.ledger.update_fc("fc-system", {**system.fc, "desktop": desktop})
        first = self.service.marshal_check(
            call(
                ("marshal", "check"),
                actor="task:marshal-1",
                payload={"trigger": "heartbeat"},
            )
        )

        second = self.service.marshal_check(
            call(
                ("marshal", "check"),
                actor="task:marshal-1",
                payload={"trigger": "heartbeat"},
            )
        )

        self.assertFalse(second.result["joined"])
        self.assertNotEqual(
            first.result["decision"]["decision_id"],
            second.result["decision"]["decision_id"],
        )
        history = self.ledger.show("fc-system").fc["desktop"][
            "marshal_decision_history"
        ]
        self.assertEqual(history[-1]["superseded_reason"], "next_serialized_heartbeat")

    def test_heartbeat_decision_is_stable_when_transcript_observes_current_turn(self):
        system = self.ledger.show("fc-system")
        desktop = dict(system.fc["desktop"])
        desktop["marshal_schedule"] = {
            "state": "succeeded",
            "status": "ACTIVE",
            "automation_id": "marshal-check",
            "target_task_id": "marshal-1",
        }
        self.ledger.update_fc("fc-system", {**system.fc, "desktop": desktop})
        checked = self.service.marshal_check(
            call(
                ("marshal", "check"),
                actor="task:marshal-1",
                payload={"trigger": "heartbeat"},
            )
        )
        system = self.ledger.show("fc-system")
        desktop = dict(system.fc["desktop"])
        desktop["observations"] = {
            "lifecycle": {
                "current-turn": {
                    "type": "turn_context",
                    "task_id": "marshal-1",
                    "turn_id": "newly-observed-native-turn",
                }
            }
        }
        self.ledger.update_fc("fc-system", {**system.fc, "desktop": desktop})

        decided = self.service.marshal_decide(
            call(
                ("marshal", "apply"),
                actor="task:marshal-1",
                payload={
                    "turn_id": checked.result["decision"]["turn_id"],
                    "decision_id": checked.result["decision"]["decision_id"],
                    "decisions": [],
                },
            )
        )

        self.assertEqual(decided.result["decision"]["state"], "completed")

    def test_closed_work_does_not_enter_recovery_brief_or_block_steward(self):
        self._seed_interrupted_creation(
            provider_truth={"state": "created_response_lost", "invoked": True},
            possible_task_locator={"threadId": "executor-1"},
        )
        work = self.ledger.show("fc-a")
        self.ledger.update_fc("fc-a", work.fc, status="closed")

        checked = self.service.marshal_check(
            call(
                ("marshal", "check"),
                actor="task:marshal-1",
                payload={"turn_id": "marshal-cleanup-turn"},
            )
        )

        self.assertEqual(checked.result["brief"]["incidents"], [])
        self.assertEqual(checked.result["brief"]["recoveries"], [])
        self.assertEqual(
            checked.result["brief"]["recovery_action"]["tool"],
            "send_message_to_thread",
        )

    def test_scheduled_marshal_delivery_uses_authenticated_request_identity_without_turn_hook(
        self,
    ):
        system = self.ledger.show("fc-system")
        desktop = dict(system.fc["desktop"])
        desktop["marshal_schedule"] = {
            "state": "succeeded",
            "status": "ACTIVE",
            "automation_id": "marshal-check",
            "target_task_id": "marshal-1",
        }
        self.ledger.update_fc("fc-system", {**system.fc, "desktop": desktop})
        heartbeat = call(
            ("marshal", "check"),
            actor="task:marshal-1",
            payload={"trigger": "heartbeat"},
        )

        checked = self.service.marshal_check(heartbeat)

        self.assertEqual(
            checked.result["decision"]["turn_id"],
            f"heartbeat:{heartbeat.request_id}",
        )

    def test_stale_marshal_row_does_not_overwrite_current_fact(self):
        checked = self.service.marshal_check(
            call(
                ("marshal", "check"),
                actor="task:marshal-1",
                payload={"turn_id": "turn-1"},
            )
        )
        result = self.service.marshal_decide(
            call(
                ("marshal", "apply"),
                actor="task:marshal-1",
                payload={
                    "decision_id": checked.result["decision"]["decision_id"],
                    "turn_id": "turn-1",
                    "decisions": [
                        {
                            "bead": "fc-a",
                            "expected": {"priority": 2},
                            "changes": {"priority": 0},
                        }
                    ],
                },
            )
        )
        self.assertEqual(result.result["accepted"], [])
        self.assertEqual(result.result["stale"][0]["reason"], "stale")
        self.assertEqual(self.ledger.show("fc-a").fc["priority"], 1)

    def test_marshal_dependency_decision_updates_native_dependency_edges(self):
        self.ledger.rows["fc-dependency"] = record("fc-dependency", phase="backlog")
        checked = self.service.marshal_check(
            call(
                ("marshal", "check"),
                actor="task:marshal-1",
                payload={"turn_id": "turn-dependencies"},
            )
        )
        result = self.service.marshal_decide(
            call(
                ("marshal", "apply"),
                actor="task:marshal-1",
                payload={
                    "decision_id": checked.result["decision"]["decision_id"],
                    "turn_id": "turn-dependencies",
                    "decisions": [
                        {
                            "bead": "fc-a",
                            "expected": {"priority": 1},
                            "changes": {"dependencies": ["fc-dependency"]},
                        }
                    ],
                },
            )
        )
        self.assertEqual(result.result["stale"], [])
        self.assertEqual(self.ledger.dependencies("fc-a"), ["fc-dependency"])
        self.assertNotIn("dependencies", self.ledger.show("fc-a").fc)

    def test_three_repairs_allow_one_recovery_slot(self):
        self.service.report_incident(
            call(
                ("incident", "report"),
                arguments={"bead": "fc-a"},
                payload={"incident_key": "ci", "scope": "repair CI"},
            )
        )
        for _ in range(3):
            self.service.record_repair(
                call(
                    ("repair", "record"),
                    arguments={"bead": "fc-a"},
                    payload={"incident_key": "ci", "outcome": "failed"},
                )
            )
        with patch("fulcrum.desktop_leadership.ConfigurationManager") as manager:
            manager.return_value.load.return_value = ({}, None)
            manager.return_value.effective.return_value = {
                "projects": {
                    "toy": {
                        "codex_project_id": "project-1",
                        "models": {},
                    }
                },
                "models": {"justiciar": {"model": "gpt-5.6-sol", "effort": "high"}},
            }
            prepared = self.service.recovery_prepare(
                call(
                    ("recovery", "prepare"),
                    actor="task:marshal-1",
                    arguments={"bead": "fc-a"},
                    payload={"incident_key": "ci", "scope": "repair CI"},
                )
            )
        self.assertEqual(prepared.result["action"]["executor"], "marshal")
        self.assertEqual(
            prepared.result["action"]["arguments"]["target"]["projectId"],
            "project-1",
        )
        self.assertEqual(prepared.result["action"]["arguments"]["model"], "gpt-5.6-sol")
        self.assertIn("$justiciar\n", prepared.result["action"]["arguments"]["prompt"])
        self.assertEqual(
            prepared.result["action"]["arguments"]["title"],
            "🔥 [jus] repair CI",
        )
        retained = self.ledger.show("fc-a").fc
        self.assertEqual(retained["recovery_fence"]["state"], "active")
        assignment = retained["desktop"]["assignment"]
        self.assertEqual(assignment["workspace"], "/tmp/worktree")
        observe_action_prompt(
            self.ledger,
            prepared.result["action"],
            task_id="justiciar-1",
            session_id="justiciar-session",
        )
        registered = self.service.register_worker(
            call(
                ("worker", "register"),
                actor="task:justiciar-1",
                arguments={"bead": "fc-a"},
                payload={
                    "assignment_token": assignment["assignment_token"],
                    "workspace": "/tmp/worktree",
                    "git_root": "/tmp/worktree",
                    "session_id": "justiciar-session",
                    "turn_id": "justiciar-turn",
                },
            )
        )
        self.assertEqual(registered.result["assignment"]["role"], "justiciar")
        with self.assertRaises(FulcrumError) as raised:
            self.service.recovery_prepare(
                call(
                    ("recovery", "prepare"),
                    actor="task:marshal-1",
                    arguments={"bead": "fc-a"},
                    payload={"incident_key": "ci", "scope": "again"},
                )
            )
        self.assertEqual(raised.exception.code, "RECOVERY_CAPACITY")

    def test_unassigned_task_cannot_mutate_incidents_or_repairs(self):
        for command, payload in (
            (("incident", "report"), {"incident_key": "ci"}),
            (("repair", "record"), {"incident_key": "ci", "outcome": "failed"}),
        ):
            with self.assertRaises(FulcrumError) as raised:
                getattr(
                    self.service,
                    "report_incident" if command[0] == "incident" else "record_repair",
                )(
                    call(
                        command,
                        actor="task:intruder",
                        arguments={"bead": "fc-a"},
                        payload=payload,
                    )
                )
            self.assertEqual(raised.exception.code, "AUTHORITY_MISMATCH")

    def test_incident_alert_is_recorded_once_for_marshal(self):
        first = self.service.report_incident(
            call(
                ("incident", "report"),
                arguments={"bead": "fc-a"},
                payload={"incident_key": "ci", "scope": "repair CI"},
            )
        )
        second = self.service.report_incident(
            call(
                ("incident", "report"),
                arguments={"bead": "fc-a"},
                payload={"incident_key": "ci", "scope": "repair CI"},
            )
        )
        self.assertEqual(first.result["alert"]["tool"], "send_message_to_thread")
        self.assertEqual(
            first.result["alert"]["action_id"], second.result["alert"]["action_id"]
        )

    def _seed_interrupted_creation(self, *, provider_truth, possible_task_locator=None):
        action = seed_action(
            self.ledger,
            {
                "record_id": "fc-a",
                "executor": "steward",
                "tool": "create_thread",
                "arguments": {
                    "prompt": "perform retained work",
                    "target": {"type": "project", "projectId": "project-1"},
                },
                "purpose": "routine_dispatch",
                "assignment_token": "assignment-1",
            },
        )
        work = self.ledger.show("fc-a")
        desktop = dict((work.fc or {})["desktop"])
        actions = dict(desktop["actions"])
        actions[action["action_id"]] = {
            **actions[action["action_id"]],
            "state": "uncertain",
            "attempts": [
                {
                    "attempt_id": "attempt-1",
                    "actor_task_id": "steward-1",
                    "state": "uncertain",
                }
            ],
            "provider_truth": provider_truth,
            "possible_task_locator": possible_task_locator,
        }
        desktop["actions"] = actions
        desktop["assignment"] = {
            "assignment_token": "assignment-1",
            "role": "executor",
            "state": "uncertain",
        }
        incident_id = "incident-1"
        desktop["incidents"] = {
            f"native-action:{action['action_id']}": {
                "incident_id": incident_id,
                "incident_key": f"native-action:{action['action_id']}",
                "action_id": action["action_id"],
                "state": "open",
                "scope": "uncertain native worker creation",
            }
        }
        self.ledger.update_fc("fc-a", {**work.fc, "desktop": desktop})
        system = self.ledger.show("fc-system")
        system_desktop = dict(system.fc["desktop"])
        standing = dict(system_desktop["standing"])
        standing["steward"] = {
            **standing["steward"],
            "state": "interrupt_observed",
        }
        system_desktop["standing"] = standing
        self.ledger.update_fc("fc-system", {**system.fc, "desktop": system_desktop})
        return action

    def test_marshal_adopts_exact_observed_task_and_resumes_same_steward(self):
        action = self._seed_interrupted_creation(
            provider_truth={"state": "created_response_lost", "invoked": True},
            possible_task_locator={"threadId": "executor-1"},
        )
        checked = self.service.marshal_check(
            call(
                ("marshal", "check"),
                actor="task:marshal-1",
                payload={"turn_id": "marshal-recovery-turn"},
            )
        )
        inspection = checked.result["brief"]["recovery_action"]
        self.assertEqual(inspection["tool"], "read_thread")
        self.assertEqual(inspection["executor"], "marshal")
        claimed = self.service.claim_action(
            call(
                ("action", "claim"),
                actor="task:marshal-1",
                arguments={
                    "record_id": "fc-a",
                    "action_id": inspection["action_id"],
                },
                payload={"attempt_id": "inspection-attempt"},
            )
        )
        self.assertTrue(claimed.result["invoke"])
        self.service.report_action_result(
            call(
                ("action", "result"),
                actor="task:marshal-1",
                arguments={
                    "record_id": "fc-a",
                    "action_id": inspection["action_id"],
                },
                payload={
                    "attempt_id": "inspection-attempt",
                    "outcome": "succeeded",
                    "native_result": {
                        "threadId": "executor-1",
                        "status": "idle",
                    },
                },
            )
        )
        decided = self.service.marshal_decide(
            call(
                ("marshal", "apply"),
                actor="task:marshal-1",
                payload={
                    "turn_id": "marshal-recovery-turn",
                    "decision_id": checked.result["decision"]["decision_id"],
                    "decisions": [
                        {
                            "bead": "fc-a",
                            "action_id": action["action_id"],
                            "expected_state": "succeeded",
                            "decision": "adopt_observed_task",
                        }
                    ],
                },
            )
        )
        resume = decided.result["recovery_action"]
        self.assertEqual(resume["tool"], "send_message_to_thread")
        self.assertEqual(resume["arguments"]["threadId"], "steward-1")
        retained = self.ledger.show("fc-a").fc["desktop"]
        self.assertEqual(retained["assignment"]["task_id"], "executor-1")
        worker_resume = next(
            item
            for item in retained["actions"].values()
            if item.get("purpose") == f"resume_reconciled_worker:{action['action_id']}"
        )
        self.assertEqual(worker_resume["executor"], "steward")
        self.assertEqual(worker_resume["arguments"]["threadId"], "executor-1")
        self.assertEqual(
            retained["incidents"][f"native-action:{action['action_id']}"]["state"],
            "resolved",
        )

    def test_marshal_retries_same_action_only_after_definite_absence(self):
        action = self._seed_interrupted_creation(
            provider_truth={"state": "definitely_not_created", "invoked": False}
        )
        checked = self.service.marshal_check(
            call(
                ("marshal", "check"),
                actor="task:marshal-1",
                payload={"turn_id": "marshal-retry-turn"},
            )
        )
        self.assertIsNone(checked.result["brief"]["recovery_action"])
        decided = self.service.marshal_decide(
            call(
                ("marshal", "apply"),
                actor="task:marshal-1",
                payload={
                    "turn_id": "marshal-retry-turn",
                    "decision_id": checked.result["decision"]["decision_id"],
                    "decisions": [
                        {
                            "bead": "fc-a",
                            "action_id": action["action_id"],
                            "expected_state": "uncertain",
                            "decision": "retry_same_action",
                        }
                    ],
                },
            )
        )
        retained = self.ledger.show("fc-a").fc["desktop"]
        self.assertEqual(retained["actions"][action["action_id"]]["state"], "pending")
        self.assertEqual(len(retained["actions"][action["action_id"]]["attempts"]), 1)
        self.assertEqual(retained["assignment"]["state"], "reserved")
        self.assertIsNotNone(decided.result["recovery_action"])
