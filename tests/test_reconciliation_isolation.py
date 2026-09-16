import asyncio
from datetime import datetime, timezone
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fulcrum.contracts import CommandResult, FulcrumError
from fulcrum.runtime import TaskFacts
from fulcrum.supervision import ControllerSupervisor
from tests.support import MemoryLedger, record, request


class ReconciliationIsolationTests(unittest.TestCase):
    def supervisor(self, *, fail_task_reconciliation=True):
        poison = record(
            "fc-poison",
            owner="warden-a",
            role="warden",
            phase="delivering",
            ownership_operation="fc-warden-entry",
            delivery={
                "source_oid": "b" * 40,
                "provider_handle": "candidate",
                "validation": {"state": "passed"},
                "promotion": {"state": "not_started"},
                "approved_source": None,
            },
            delivery_finish={
                "operation_id": "fc-finish",
                "source_oid": "a" * 40,
                "summary": "stale judgment",
            },
        )
        target = record(
            "fc-target",
            owner="marshal",
            role="marshal",
            phase="backlog",
            ownership_operation="fc-weaver-finish",
            dispatch=None,
        )
        task = record(
            "fc-task-poison",
            kind="task",
            owner="warden-a",
            thread_id="warden-a",
            role="warden",
            work_bead="fc-poison",
            ownership_operation="fc-warden-entry",
        )
        supervisor = object.__new__(ControllerSupervisor)
        supervisor.ledger = MemoryLedger(poison, target, task)
        supervisor.request = request()
        supervisor.config = {
            "timing": {
                "archive_idle_seconds": 60,
                "checkpoint_seconds": 3600,
                "stalled_seconds": 7200,
            }
        }
        supervisor.runtime = Mock()
        supervisor.runtime.connect = AsyncMock()
        supervisor.runtime.close = AsyncMock()
        supervisor.runtime.terminals = AsyncMock(return_value={"items": []})
        released = Mock(active_terminals=())
        released.to_dict.return_value = {"active_terminals": []}
        supervisor.runtime.release = AsyncMock(return_value=released)
        supervisor.health = Mock()
        supervisor.bead_locks = {}
        supervisor.analytics = Mock()
        supervisor.clock = Mock()
        supervisor.clock.now.return_value = datetime(2026, 9, 16, tzinfo=timezone.utc)
        supervisor._loop = None
        facts = TaskFacts(
            "warden-a",
            "Warden",
            "/work",
            "toy",
            ("/work",),
            False,
            True,
            True,
            "idle",
            None,
            {"id": "turn-a", "status": "completed"},
            (),
            "2026-09-16T00:00:00Z",
        )
        supervisor._inspect_tasks = AsyncMock(return_value={"warden-a": facts})
        supervisor._resource_pressure = AsyncMock(
            return_value={"paused": False, "reason": None}
        )
        supervisor._reconcile_work_delivery = AsyncMock(
            side_effect=lambda work: (work, None)
        )
        if fail_task_reconciliation:
            supervisor._ordered_task_reconcile = AsyncMock(
                side_effect=FulcrumError(
                    "STALE_SOURCE",
                    "approval source differs from retained validation",
                    exit_code=5,
                )
            )
        supervisor._complete_plan_roots = AsyncMock(return_value=([], set()))
        supervisor._request_marshal_judgment = AsyncMock(
            return_value={
                "kind": "marshal_decision",
                "bead_id": "fc-target",
                "started": True,
            }
        )
        supervisor._start_authorized_work = AsyncMock(return_value=[])
        supervisor._record_reconciliation = Mock()
        return supervisor

    def test_exact_stale_approval_shape_recovers_and_reaches_marshal(self):
        supervisor = self.supervisor(fail_task_reconciliation=False)

        def dispatch(value):
            if value.command == ("promotion", "show"):
                return CommandResult.query(
                    {
                        "delivery": {
                            "source_oid": "b" * 40,
                            "validation": {"state": "passed"},
                            "promotion": {"state": "not_started"},
                        }
                    }
                )
            if value.command == ("review", "approve"):
                raise FulcrumError(
                    "STALE_SOURCE",
                    "approval source differs from retained validation",
                    exit_code=5,
                    details={"requested": "a" * 40, "validated": "b" * 40},
                )
            self.fail(f"unexpected dispatch: {value.command}")

        supervisor.application = Mock()
        supervisor.application.dispatch.side_effect = dispatch
        with (
            patch(
                "fulcrum.supervision.ensure_leadership", new=AsyncMock(return_value=[])
            ),
            patch("fulcrum.supervision.normalize_native_intake", return_value=[]),
            patch(
                "fulcrum.supervision.reconcile_archive_once",
                new=AsyncMock(return_value=[]),
            ),
            patch("fulcrum.supervision.active_recovery_fence", return_value=None),
        ):
            summary = asyncio.run(supervisor._run_once(pass_id="incident-pass"))

        supervisor._request_marshal_judgment.assert_awaited_once()
        self.assertTrue(
            any(
                action.get("kind") == "warden_delivery"
                and action.get("state") == "returned_to_review"
                for action in summary.next_actions
            )
        )
        self.assertTrue(
            any(
                action.get("kind") == "marshal_decision"
                and action.get("bead_id") == "fc-target"
                for action in summary.next_actions
            )
        )
        reopened = supervisor.ledger.show("fc-poison")
        self.assertEqual(reopened.fc["phase"], "reviewing")
        self.assertNotIn("delivery_finish", reopened.fc)
        self.assertEqual(
            reopened.fc["failed_delivery_finishes"][-1]["evidence"][
                "retained_delivery"
            ]["source_oid"],
            "b" * 40,
        )

    def test_repeated_failure_is_backed_off_while_other_backlog_stays_live(self):
        supervisor = self.supervisor()
        with (
            patch(
                "fulcrum.supervision.ensure_leadership", new=AsyncMock(return_value=[])
            ),
            patch("fulcrum.supervision.normalize_native_intake", return_value=[]),
            patch(
                "fulcrum.supervision.reconcile_archive_once",
                new=AsyncMock(return_value=[]),
            ),
            patch("fulcrum.supervision.active_recovery_fence", return_value=None),
        ):
            summary = asyncio.run(supervisor._run_once(pass_id="pass-one"))

        supervisor._request_marshal_judgment.assert_awaited_once()
        self.assertTrue(
            any(
                action.get("kind") == "marshal_decision"
                and action.get("bead_id") == "fc-target"
                for action in summary.next_actions
            )
        )
        self.assertTrue(summary.gaps)
        self.assertEqual(summary.gaps[0]["bead_id"], "fc-poison")
        retained = supervisor.ledger.show("fc-task-poison").fc["reconciliation_failure"]
        self.assertEqual(retained["attempts"], 1)
        self.assertEqual(retained["code"], "STALE_SOURCE")
        self.assertEqual(retained["pass_id"], "pass-one")
        supervisor.health.failure.assert_called_once()
        event = supervisor._record_reconciliation.call_args.args[0]
        self.assertEqual(event["event"], "task_reconciliation_failed")
        self.assertEqual(event["associated_beads"], ["fc-poison"])

        supervisor._request_marshal_judgment.reset_mock()
        supervisor._ordered_task_reconcile.reset_mock()
        with (
            patch(
                "fulcrum.supervision.ensure_leadership", new=AsyncMock(return_value=[])
            ),
            patch("fulcrum.supervision.normalize_native_intake", return_value=[]),
            patch(
                "fulcrum.supervision.reconcile_archive_once",
                new=AsyncMock(return_value=[]),
            ),
            patch("fulcrum.supervision.active_recovery_fence", return_value=None),
        ):
            second = asyncio.run(supervisor._run_once(pass_id="pass-two"))

        supervisor._ordered_task_reconcile.assert_not_awaited()
        supervisor._request_marshal_judgment.assert_awaited_once()
        self.assertTrue(
            any(
                action.get("kind") == "task_reconciliation_suppressed"
                for action in second.next_actions
            )
        )
        self.assertEqual(
            supervisor.ledger.show("fc-task-poison").fc["reconciliation_failure"][
                "attempts"
            ],
            1,
        )


if __name__ == "__main__":
    unittest.main()
