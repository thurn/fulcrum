import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import unittest
from unittest.mock import AsyncMock, Mock, patch
import uuid

from fulcrum.completion import CompletionService
from fulcrum.contracts import ActorContext, CommandResult, CommandState
from fulcrum.ledger import operation_id
from fulcrum.supervision import ControllerSupervisor
from tests.support import MemoryLedger, record, request

SOURCE = "a" * 40


def finish_request(*, request_id=None):
    return request(
        ("finish",),
        arguments={"bead": "fc-work", "outcome": "approved"},
        input={
            "summary": "Reviewed exact source.",
            "source_oid": SOURCE,
            "checks": [{"name": "scripts/check", "status": "passed", "evidence": "ok"}],
            "evidence": ["commit:" + SOURCE],
        },
        actor=ActorContext(kind="task", task_id="warden"),
        thread_id="warden",
        ownership_operation="fc-warden-entry",
        request_id=request_id or str(uuid.uuid4()),
    )


class WardenDeliveryOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.work = record(
            owner="warden",
            role="warden",
            phase="reviewing",
            ownership_operation="fc-warden-entry",
            project="toy",
        )
        self.task = record(
            "fc-task",
            kind="task",
            owner="warden",
            thread_id="warden",
            role="warden",
            work_bead="fc-work",
            ownership_operation="fc-warden-entry",
        )
        self.ledger = MemoryLedger(self.work, self.task)
        self.validation = Mock(side_effect=self._start_validation)

    def _start_validation(self, _request):
        work = self.ledger.show("fc-work")
        self.ledger.update_fc(
            work.id,
            {
                **work.fc,
                "delivery": {
                    "source_oid": SOURCE,
                    "provider_handle": "candidate",
                    "validation": {"state": "pending"},
                    "approved_source": None,
                    "promotion": {"state": "not_started"},
                },
            },
        )
        return CommandResult(
            ok=True,
            state=CommandState.COMPLETED,
            operation_id="fc-validation",
            result={"validation": {"state": "pending"}},
        )

    def test_one_warden_finish_seals_judgment_and_leaves_delivery_to_controller(self):
        finish = finish_request()
        with (
            patch("fulcrum.completion._ledger", return_value=self.ledger),
            patch(
                "fulcrum.completion._inspect_clean_source",
                return_value={"owned": True, "dirty": False, "head_oid": SOURCE},
            ),
            patch(
                "fulcrum.completion.DeliveryService.validation_start",
                self.validation,
            ),
        ):
            first = CompletionService().finish(finish)
            replay = CompletionService().finish(finish)
            duplicate = CompletionService().finish(
                replace(finish, request_id=str(uuid.uuid4()))
            )

        self.assertEqual(first.result["step"], "warden_judgment_sealed")
        self.assertEqual(replay.operation_id, first.operation_id)
        self.assertEqual(duplicate.operation_id, first.operation_id)
        self.validation.assert_called_once()
        work = self.ledger.show("fc-work")
        self.assertEqual(work.fc["phase"], "delivering")
        self.assertEqual(work.fc["delivery_finish"]["state"], "waiting_for_validation")
        self.assertIsNone(work.fc["delivery"]["approved_source"])

    def test_controller_completes_approved_delivery_without_another_warden_finish(self):
        work = record(
            owner="warden",
            role="warden",
            phase="delivering",
            ownership_operation="fc-warden-entry",
            delivery={
                "source_oid": SOURCE,
                "provider_handle": "candidate",
                "validation": {"state": "passed"},
                "approved_source": None,
            },
            delivery_finish={
                "operation_id": "fc-finish",
                "source_oid": SOURCE,
                "summary": "Reviewed exact source.",
            },
        )
        task = record(
            "fc-task",
            kind="task",
            owner="warden",
            thread_id="warden",
            role="warden",
            work_bead="fc-work",
            ownership_operation="fc-warden-entry",
        )
        ledger = MemoryLedger(work, task)
        application = Mock()
        calls = []

        def dispatch(value):
            calls.append(value.command)
            if value.command == ("promotion", "show"):
                promoted = calls.count(("promotion", "show")) > 1
                return CommandResult.query(
                    {
                        "delivery": {
                            "validation": "passed",
                            "promotion": "promoted" if promoted else "not_started",
                        }
                    }
                )
            return CommandResult(
                ok=True,
                state=CommandState.COMPLETED,
                operation_id="fc-" + "-".join(value.command),
                result={},
            )

        application.dispatch.side_effect = dispatch
        release = Mock(active_terminals=())
        release.to_dict.return_value = {"released": True, "active_terminals": []}
        runtime = Mock()
        runtime.terminals = AsyncMock(return_value={"items": []})
        runtime.release = AsyncMock(return_value=release)
        supervisor = object.__new__(ControllerSupervisor)
        supervisor.ledger = ledger
        supervisor.application = application
        supervisor.runtime = runtime
        supervisor.request = request()
        facts = Mock(id="warden")

        result = asyncio.run(supervisor._advance_warden_delivery(task, work, facts))

        self.assertTrue(result["advanced"])
        self.assertEqual(result["state"], "closed")
        self.assertEqual(
            calls,
            [
                ("promotion", "show"),
                ("review", "approve"),
                ("promotion", "start"),
                ("promotion", "show"),
                ("source", "sync"),
                ("worktree", "cleanup"),
                ("work", "close"),
            ],
        )

    def test_failed_async_validation_reopens_review_without_sealing_next_finish(self):
        work = record(
            owner="warden",
            role="warden",
            phase="delivering",
            ownership_operation="fc-warden-entry",
            delivery={"source_oid": SOURCE, "provider_handle": "candidate"},
            delivery_finish={
                "operation_id": "fc-finish",
                "source_oid": SOURCE,
                "summary": "Reviewed exact source.",
            },
        )
        task = record(
            "fc-task",
            kind="task",
            owner="warden",
            thread_id="warden",
            role="warden",
            work_bead="fc-work",
            ownership_operation="fc-warden-entry",
        )
        ledger = MemoryLedger(work, task)
        application = Mock()
        application.dispatch.return_value = CommandResult.query(
            {"delivery": {"validation": "failed", "promotion": "not_started"}}
        )
        supervisor = object.__new__(ControllerSupervisor)
        supervisor.ledger = ledger
        supervisor.application = application
        supervisor.runtime = Mock()
        supervisor.request = request()
        supervisor.clock = Mock()
        supervisor.clock.now.return_value = datetime(2026, 9, 16, tzinfo=timezone.utc)

        result = asyncio.run(
            supervisor._advance_warden_delivery(task, work, Mock(id="warden"))
        )

        self.assertEqual(result["state"], "returned_to_review")
        reopened = ledger.show("fc-work")
        self.assertEqual(reopened.fc["phase"], "reviewing")
        self.assertNotIn("delivery_finish", reopened.fc)
        self.assertEqual(
            reopened.fc["failed_delivery_finishes"][0]["operation_id"], "fc-finish"
        )
        supervisor.runtime.terminals.assert_not_called()


class FinishReplayTests(unittest.TestCase):
    def test_older_finish_is_cancelled_when_a_later_finish_advanced_work(self):
        finish = replace(
            finish_request(),
            arguments={"bead": "fc-work", "outcome": "ready_for_review"},
            actor=ActorContext(kind="task", task_id="executor"),
            thread_id="executor",
            ownership_operation="fc-executor-entry",
        )
        work = record(
            owner="warden",
            role="warden",
            phase="reviewing",
            ownership_operation="fc-warden-entry",
        )
        ledger = MemoryLedger(work)
        stale, _ = ledger.create_operation(finish, bead_id="fc-work")
        current = ledger.show("fc-work")
        ledger.update_fc(
            current.id,
            {
                **current.fc,
                "finish": {
                    "operation_id": "fc-later",
                    "source_oid": SOURCE,
                    "summary": "Later accepted finish",
                },
            },
        )

        with patch("fulcrum.completion._ledger", return_value=ledger):
            result = CompletionService().finish(finish)

        self.assertEqual(result.operation_id, stale.id)
        self.assertEqual(result.state, CommandState.CANCELLED)
        self.assertEqual(result.result["step"], "finish_superseded")
        self.assertEqual(result.result["superseded_by"], "fc-later")
        self.assertEqual(stale.id, operation_id(finish.request_id))
