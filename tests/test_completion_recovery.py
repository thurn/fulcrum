from __future__ import annotations

from dataclasses import replace
import unittest
import uuid

from fulcrum.completion import CompletionService
from fulcrum.contracts import ActorContext, FulcrumError
from tests.support import MemoryLedger, record, request


class CompletionRecoveryTests(unittest.TestCase):
    def test_changed_justiciar_source_requires_settled_delivery(self):
        source = "a" * 40
        work = record(
            "fc-a",
            role="justiciar",
            phase="repairing",
            worktree={"path": "/managed/worktree"},
            recovery_fence={
                "state": "active",
                "operation_id": "recovery-1",
                "incident_key": "ci",
            },
            desktop={
                "assignment": {
                    "task_id": "justiciar-1",
                    "role": "justiciar",
                    "state": "active",
                    "assignment_token": "assignment-1",
                }
            },
        )
        ledger = MemoryLedger(work)
        call = replace(
            request(("finish",)),
            actor=ActorContext.parse("task:justiciar-1"),
            thread_id="justiciar-1",
            arguments={"bead": "fc-a"},
            input={
                "summary": "repair complete",
                "changes": ["change.txt"],
                "waived_requirements": [],
                "known_defects": [],
                "evidence": ["commit"],
                "source_oid": source,
            },
            ownership_operation="assignment-1",
            request_id=str(uuid.uuid4()),
        )
        with self.assertRaises(FulcrumError) as raised:
            CompletionService()._justiciar_repaired(call, ledger, work)
        self.assertEqual(raised.exception.code, "DELIVERY_NOT_SETTLED")
        self.assertEqual(ledger.show("fc-a").status, "open")

    def test_settled_exact_source_allows_justiciar_finish_after_cleanup(self):
        source = "b" * 40
        work = record(
            "fc-a",
            role="justiciar",
            phase="repairing",
            recovery_fence={
                "state": "active",
                "operation_id": "recovery-1",
                "incident_key": "ci",
                "scope": "repair CI",
            },
            delivery={
                "source_oid": source,
                "validation": {"state": "passed"},
                "approved_source": {"oid": source},
                "promotion": {
                    "state": "observed",
                    "integration_oid": "c" * 40,
                },
                "synchronization": {
                    "state": "observed",
                    "integration_oid": "c" * 40,
                },
                "cleanup": {"state": "observed"},
            },
            desktop={
                "assignment": {
                    "task_id": "justiciar-1",
                    "role": "justiciar",
                    "state": "active",
                    "assignment_token": "assignment-1",
                },
                "incidents": {"ci": {"incident_id": "incident-1", "state": "open"}},
            },
        )
        ledger = MemoryLedger(work)
        call = replace(
            request(("finish",)),
            actor=ActorContext.parse("task:justiciar-1"),
            thread_id="justiciar-1",
            arguments={"bead": "fc-a"},
            input={
                "summary": "repair delivered",
                "changes": ["source repair"],
                "waived_requirements": [],
                "known_defects": [],
                "evidence": ["exact delivery"],
                "source_oid": source,
            },
            ownership_operation="assignment-1",
            request_id=str(uuid.uuid4()),
        )
        CompletionService()._justiciar_repaired(call, ledger, work)
        retained = ledger.show("fc-a")
        self.assertEqual(retained.status, "closed")
        self.assertEqual((retained.fc or {})["phase"], "done")
        self.assertEqual(
            (retained.fc or {})["desktop"]["incidents"]["ci"]["state"],
            "resolved",
        )

    def test_failed_justiciar_intervention_retains_human_decision_and_notice(self):
        system = record(
            "fc-system",
            kind="control",
            desktop={
                "standing": {"vizier": {"task_id": "vizier-1", "state": "registered"}}
            },
        )
        work = record(
            "fc-a",
            role="justiciar",
            phase="repairing",
            recovery_fence={
                "state": "active",
                "operation_id": "recovery-1",
                "incident_key": "ci",
                "scope": "repair CI",
            },
            desktop={
                "assignment": {
                    "task_id": "justiciar-1",
                    "role": "justiciar",
                    "state": "active",
                    "assignment_token": "assignment-1",
                },
                "incidents": {
                    "ci": {
                        "incident_id": "incident-1",
                        "justiciar_interventions": 1,
                    }
                },
            },
        )
        ledger = MemoryLedger(system, work)
        call = replace(
            request(("finish",)),
            actor=ActorContext.parse("task:justiciar-1"),
            thread_id="justiciar-1",
            arguments={"bead": "fc-a"},
            input={
                "summary": "could not repair",
                "blocker": "provider invariant failed",
                "attempts": ["reproduced", "isolated"],
                "required_action": "Choose whether to abandon or expand scope",
            },
            ownership_operation="assignment-1",
            request_id=str(uuid.uuid4()),
        )
        result = CompletionService()._justiciar_blocked(call, ledger, work)
        self.assertEqual(result.result["notice"]["executor"], "justiciar")
        retained = ledger.show("fc-a")
        self.assertEqual(retained.status, "blocked")
        self.assertEqual((retained.fc or {})["recovery_fence"]["state"], "failed")
        decisions = (retained.fc or {})["desktop"]["human_decisions"]
        self.assertEqual(next(iter(decisions.values()))["state"], "open")
