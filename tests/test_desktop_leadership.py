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
