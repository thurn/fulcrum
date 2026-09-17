from __future__ import annotations

from dataclasses import replace
import unittest
import uuid

from fulcrum.contracts import ActorContext, FulcrumError
from fulcrum.desktop_leadership import DesktopLeadershipService
from tests.support import MemoryLedger, record, request


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
            self.service.register_standing(
                call(
                    ("register", "standing"),
                    payload={"role": role, "task_id": task, "session_id": task},
                )
            )

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
        prepared = self.service.recovery_prepare(
            call(
                ("recovery", "prepare"),
                actor="task:marshal-1",
                arguments={"bead": "fc-a"},
                payload={"incident_key": "ci", "scope": "repair CI"},
            )
        )
        self.assertEqual(prepared.result["action"]["executor"], "marshal")
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
