from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from fulcrum.scenario_native_action import (
    CONTROL_NAME,
    inject_claim_fault,
    inject_result_fault,
    reconciliation_held,
    record_alert_delivered,
    record_steward_interruption,
)
from tests.support import request


class ScenarioNativeActionTests(unittest.TestCase):
    def _fixture(self, directory: str, mode: str):
        root = Path(directory)
        control = root / CONTROL_NAME
        control.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "id": "scenario-4",
                    "mode": mode,
                    "project_id": "project-1",
                    "prompt_contains": "Recovery fixture",
                }
            ),
            encoding="utf-8",
        )
        base = request()
        supplied = replace(
            base,
            actor=replace(base.actor, kind="task", task_id="steward-1"),
            thread_id="steward-1",
            instance=replace(base.instance, instance_root=root),
        )
        action = {
            "action_id": "action-1",
            "record_id": "fc-a",
            "tool": "create_thread",
            "purpose": "routine_dispatch",
            "arguments": {
                "prompt": "Add a Recovery fixture sentence.",
                "target": {"type": "project", "projectId": "project-1"},
            },
        }
        return supplied, action, control

    def test_created_reply_is_obscured_once_and_boundary_is_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            supplied, action, control = self._fixture(
                directory, "created_response_lost"
            )
            outcome, native_result, fault = inject_result_fault(
                supplied,
                record_id="fc-a",
                action=action,
                outcome="succeeded",
                native_result={"threadId": "executor-1"},
            )
            self.assertEqual(outcome, "uncertain")
            self.assertEqual(
                native_result["possibleTaskLocator"], {"threadId": "executor-1"}
            )
            self.assertEqual(fault["provider_truth"]["state"], "created_response_lost")
            self.assertTrue(reconciliation_held(supplied, action=action))
            second = inject_result_fault(
                supplied,
                record_id="fc-a",
                action=action,
                outcome="succeeded",
                native_result={"threadId": "executor-2"},
            )
            self.assertEqual(second, ("succeeded", {"threadId": "executor-2"}, None))
            record_alert_delivered(
                supplied,
                action={
                    "purpose": "incident_alert:incident-1",
                    "reporting": {"recovery_action_id": "action-1"},
                },
            )
            state = json.loads(control.read_text(encoding="utf-8"))["state"]
            self.assertIn("alert_delivered_at", state)
            record_steward_interruption(supplied, task_id="steward-1")
            state = json.loads(control.read_text(encoding="utf-8"))["state"]
            self.assertFalse(state["hold_reconciliation"])
            self.assertIn("interrupted_at", state)

    def test_definite_absence_fault_prevents_native_invocation_once(self):
        with tempfile.TemporaryDirectory() as directory:
            supplied, action, _ = self._fixture(directory, "definitely_not_created")
            fault = inject_claim_fault(supplied, record_id="fc-a", action=action)
            self.assertEqual(
                fault["provider_truth"],
                {"state": "definitely_not_created", "invoked": False},
            )
            self.assertIsNone(
                inject_claim_fault(supplied, record_id="fc-a", action=action)
            )


if __name__ == "__main__":
    unittest.main()
