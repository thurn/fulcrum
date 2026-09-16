from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fulcrum.configuration import ConfigurationManager, default_config
from fulcrum.contracts import ActorContext, FulcrumError
from fulcrum.leadership import _apply_decision
from fulcrum.recovery_service import HumanService, RecoveryService
from tests.support import MemoryLedger, record, request


def human_work():
    return record(
        "fc-work",
        project="toy",
        phase="backlog",
        role="marshal",
        ownership_operation="fc-marshal",
        outcome="Preserve the behavioral result",
        acceptance=["Behavior remains observable"],
        scope={
            "summary": "Preserve the behavioral result",
            "acceptance": ["Behavior remains observable"],
            "evidence": ["retained-evidence"],
            "implementation_notes": ["old/path may be stale"],
            "finish_operation": "fc-scope",
        },
    )


class RecoveryPolicyTests(unittest.TestCase):
    def test_human_escalation_requires_structured_irreducibility(self):
        ledger = MemoryLedger(human_work())
        supplied = {
            "action": "human",
            "reason": "Need a decision.",
            "question": "What should happen?",
            "required_action": "Answer the question.",
        }
        with self.assertRaises(FulcrumError) as missing:
            _apply_decision(ledger, ledger.show("fc-work"), supplied, "fc-decision")
        self.assertEqual(missing.exception.code, "INVALID_DECISION")

        updated = _apply_decision(
            ledger,
            ledger.show("fc-work"),
            {
                **supplied,
                "irreducibility": {
                    "kind": "intent",
                    "detail": "Two user-visible outcomes are equally plausible.",
                },
            },
            "fc-decision",
        )
        reason = updated.fc["waiting"]["reasons"][0]
        self.assertEqual(reason["irreducibility"]["kind"], "intent")
        self.assertEqual(updated.fc["owner"], "HUMAN")

    def test_human_reply_resolves_once_and_preserves_scope_for_marshal(self):
        ledger = MemoryLedger(
            record("fc-system", kind="system", marshal_thread="marshal"),
            human_work(),
        )
        escalated = _apply_decision(
            ledger,
            ledger.show("fc-work"),
            {
                "action": "human",
                "reason": "Need intent.",
                "question": "Keep the behavior unchanged?",
                "required_action": "Confirm intended behavior.",
                "irreducibility": {
                    "kind": "intent",
                    "detail": "Only the user can choose the visible result.",
                },
            },
            "fc-decision",
        )
        scope = escalated.fc["scope"]
        reason_id = escalated.fc["waiting"]["reasons"][0]["id"]
        resolve = request(
            ("human", "resolve"),
            arguments={"id": "fc-work"},
            input={
                "reason_id": reason_id,
                "answer": "Yes, keep the behavior unchanged.",
                "resume_role": "executor",
            },
        )
        with patch("fulcrum.recovery_service._ledger", return_value=ledger):
            result = HumanService().resolve(resolve)
            replay = HumanService().resolve(resolve)

        self.assertTrue(result.ok)
        self.assertEqual(replay.operation_id, result.operation_id)
        resumed = ledger.show("fc-work")
        self.assertEqual(resumed.fc["scope"], scope)
        self.assertEqual(resumed.fc["owner"], "marshal")
        self.assertEqual(resumed.fc["role"], "marshal")
        self.assertEqual(resumed.fc["phase"], "backlog")
        self.assertEqual(resumed.fc["requested_role"], "executor")
        self.assertIsNone(resumed.fc["waiting"])
        self.assertEqual(len(resumed.fc["human_resolutions"]), 1)

    def test_vizier_can_correct_mistaken_gate_without_changing_scope(self):
        ledger = MemoryLedger(
            record("fc-system", kind="system", marshal_thread="marshal"),
            record("fc-vizier", kind="task", thread_id="vizier", role="vizier"),
            human_work(),
        )
        escalated = _apply_decision(
            ledger,
            ledger.show("fc-work"),
            {
                "action": "human",
                "reason": "Mistaken escalation.",
                "question": "Can routine recovery continue?",
                "required_action": "Clear the mistaken gate.",
                "irreducibility": {
                    "kind": "authority",
                    "detail": "Recorded as authority-dependent before diagnosis.",
                },
            },
            "fc-decision",
        )
        reason_id = escalated.fc["waiting"]["reasons"][0]["id"]
        scope = escalated.fc["scope"]
        resolve = request(
            ("human", "resolve"),
            arguments={"id": "fc-work"},
            input={
                "reason_id": reason_id,
                "answer": "The gate was mistaken; continue mechanical routing.",
                "resume_role": "executor",
            },
            actor=ActorContext(kind="task", task_id="vizier"),
            thread_id="vizier",
        )
        with patch("fulcrum.recovery_service._ledger", return_value=ledger):
            result = HumanService().resolve(resolve)

        self.assertTrue(result.ok)
        resumed = ledger.show("fc-work")
        self.assertEqual(resumed.fc["scope"], scope)
        self.assertEqual(resumed.fc["owner"], "marshal")
        self.assertEqual(resumed.fc["requested_role"], "executor")

    def test_missing_controller_socket_does_not_block_offline_justiciar_takeover(self):
        ledger = MemoryLedger(
            record(
                "fc-system",
                kind="control",
                marshal_thread="marshal",
                active_takeover=None,
            ),
            record(
                "fc-marshal",
                kind="task",
                thread_id="marshal",
                role="marshal",
                purpose="leadership",
                work_bead=None,
                associated_beads=[],
                ownership_operation="fc-leadership",
                last_observed={"runtime_status": "idle", "active_turn": None},
            ),
            human_work(),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config_path = root / "fulcrum.yaml"
            config = default_config(root)
            config["source"] = {
                "repository": str(root),
                "remote": "origin",
                "branch": "master",
            }
            config["projects"] = {"toy": {"root": str(root), "enabled": True}}
            with config_path.open("w", encoding="utf-8") as stream:
                ConfigurationManager.yaml().dump(config, stream)
            takeover = request(
                ("recover", "takeover"),
                arguments={
                    "scope": "bead:fc-work",
                    "reason": "Repair the mismatched managed role and state.",
                },
            )
            takeover = replace(
                takeover,
                instance=replace(
                    takeover.instance,
                    instance_root=root / "instance",
                    config_path=config_path,
                    brain_root=root,
                    socket_path=root / "instance" / "resident.sock",
                    lock_path=root / ".fulcrum-locks" / "maintenance",
                ),
            )
            self.assertFalse(takeover.instance.socket_path.exists())
            with patch("fulcrum.recovery_service._ledger", return_value=ledger):
                result = RecoveryService(object()).takeover(takeover)

        self.assertTrue(result.ok)
        self.assertEqual(result.result["result"]["justiciar_thread"], "marshal")
        recovered = ledger.show("fc-work")
        self.assertEqual(recovered.fc["role"], "justiciar")
        self.assertEqual(recovered.fc["owner"], "marshal")
        self.assertEqual(recovered.fc["recovery_fence"]["state"], "active")


if __name__ == "__main__":
    unittest.main()
