import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import subprocess
import unittest
from unittest.mock import AsyncMock, Mock, patch
import uuid

from fulcrum.completion import CompletionService
from fulcrum.contracts import ActorContext, CommandResult, CommandState, FulcrumError
from fulcrum.delivery import (
    DeliveryFacts,
    DeliveryProviderError,
    LocalCheckFacts,
    ValidationFacts,
    WorkRef,
)
from fulcrum.delivery_service import DeliveryService, normalized_delivery
from fulcrum.ledger import operation_id
from fulcrum.supervision import ControllerSupervisor
from fulcrum.work import WorkService
from tests.support import MemoryLedger, record, request

SOURCE = "a" * 40
REPAIRED_SOURCE = "b" * 40


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
            patch("fulcrum.completion._wake_controller") as wake,
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
        wake.assert_called_once_with(finish)
        work = self.ledger.show("fc-work")
        self.assertEqual(work.fc["phase"], "delivering")
        self.assertEqual(work.fc["delivery_finish"]["state"], "waiting_for_validation")
        self.assertIsNone(work.fc["delivery"]["approved_source"])

    def test_failed_validation_child_keeps_warden_in_review_without_sealing(self):
        provider = Mock()
        provider.validate = AsyncMock(
            return_value=LocalCheckFacts(
                SOURCE,
                "passed",
                ("scripts/check",),
                0,
                "all passed",
                "",
                "2026-09-16T00:00:00Z",
            )
        )
        provider.submit = AsyncMock(
            side_effect=DeliveryProviderError(
                "configured command failed: scripts/check",
                category="rejected",
                evidence={"returncode": 1, "stderr": "one test failed"},
            )
        )
        reference = WorkRef(
            "fc-work",
            "toy",
            "/unused/project",
            "repo",
            "/unused/worktree",
            "work",
            "main",
            "fc-prepare",
        )
        finish = finish_request()
        with (
            patch("fulcrum.completion._ledger", return_value=self.ledger),
            patch(
                "fulcrum.completion._inspect_clean_source",
                return_value={"owned": True, "dirty": False, "head_oid": SOURCE},
            ),
            patch(
                "fulcrum.delivery_service._context",
                return_value=(self.ledger, self.work, {}, provider),
            ),
            patch("fulcrum.delivery_service._work_ref", return_value=reference),
        ):
            failed = CompletionService().finish(finish)

        self.assertEqual(failed.result["step"], "warden_validation_unresolved")
        self.assertEqual(failed.state, CommandState.FAILED)
        self.assertFalse(failed.result["accepted"])
        self.assertTrue(failed.result["correctable"])
        work = self.ledger.show("fc-work")
        self.assertEqual(work.fc["phase"], "reviewing")
        self.assertNotIn("delivery_finish", work.fc)
        self.assertNotIn("finish_requested_operation", self.ledger.show("fc-task").fc)

        provider.submit = AsyncMock(
            return_value=ValidationFacts(
                "candidate",
                SOURCE,
                "pending",
                (),
                {},
                "2026-09-16T00:00:00Z",
            )
        )
        retry = replace(finish, request_id=str(uuid.uuid4()))
        with (
            patch("fulcrum.completion._ledger", return_value=self.ledger),
            patch(
                "fulcrum.completion._inspect_clean_source",
                return_value={"owned": True, "dirty": False, "head_oid": SOURCE},
            ),
            patch(
                "fulcrum.delivery_service._context",
                return_value=(self.ledger, self.ledger.show("fc-work"), {}, provider),
            ),
            patch("fulcrum.delivery_service._work_ref", return_value=reference),
        ):
            accepted = CompletionService().finish(retry)

        self.assertTrue(accepted.result["accepted"])
        self.assertEqual(accepted.result["step"], "warden_judgment_sealed")

    def test_new_source_supersedes_old_sealed_judgment(self):
        work = self.ledger.show("fc-work")
        self.ledger.update_fc(
            work.id,
            {
                **work.fc,
                "delivery": {
                    "source_oid": SOURCE,
                    "approved_source": {"oid": SOURCE},
                    "validation": {"state": "passed"},
                },
                "delivery_finish": {
                    "operation_id": "fc-old-finish",
                    "source_oid": SOURCE,
                    "summary": "Old judgment",
                },
            },
        )

        def repaired_validation(_request):
            current = self.ledger.show("fc-work")
            self.ledger.update_fc(
                current.id,
                {
                    **current.fc,
                    "delivery": {
                        "source_oid": REPAIRED_SOURCE,
                        "provider_handle": "repaired-candidate",
                        "validation": {"state": "pending"},
                        "approved_source": None,
                    },
                },
            )
            return CommandResult(
                ok=True,
                state=CommandState.COMPLETED,
                operation_id="fc-repaired-validation",
                result={"validation": {"state": "pending"}},
            )

        finish = replace(
            finish_request(),
            input={
                **finish_request().input,
                "source_oid": REPAIRED_SOURCE,
                "summary": "Repaired judgment",
            },
        )
        with (
            patch("fulcrum.completion._ledger", return_value=self.ledger),
            patch(
                "fulcrum.completion._inspect_clean_source",
                return_value={
                    "owned": True,
                    "dirty": False,
                    "head_oid": REPAIRED_SOURCE,
                },
            ),
            patch(
                "fulcrum.completion.DeliveryService.validation_start",
                side_effect=repaired_validation,
            ),
        ):
            result = CompletionService().finish(finish)

        self.assertTrue(result.result["accepted"])
        current = self.ledger.show("fc-work")
        self.assertEqual(current.fc["delivery_finish"]["source_oid"], REPAIRED_SOURCE)
        superseded = current.fc["failed_delivery_finishes"][-1]
        self.assertEqual(superseded["source_oid"], SOURCE)
        self.assertEqual(superseded["reason"], "workspace_source_changed")

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

    def test_controller_supersedes_finish_when_approval_observes_new_source(self):
        work = record(
            owner="warden",
            role="warden",
            phase="delivering",
            ownership_operation="fc-warden-entry",
            delivery={
                "source_oid": REPAIRED_SOURCE,
                "provider_handle": "candidate",
                "validation": {"state": "passed"},
                "approved_source": None,
            },
            delivery_finish={
                "operation_id": "fc-finish",
                "source_oid": SOURCE,
                "summary": "Judgment for old source.",
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

        def dispatch(value):
            if value.command == ("promotion", "show"):
                return CommandResult.query(
                    {
                        "delivery": {
                            "source_oid": REPAIRED_SOURCE,
                            "validation": {"state": "passed"},
                            "promotion": {"state": "not_started"},
                        }
                    }
                )
            if value.command == ("review", "approve"):
                raise FulcrumError(
                    "STALE_SOURCE",
                    "requested source differs from retained validation",
                    exit_code=5,
                    details={"requested": SOURCE, "validated": REPAIRED_SOURCE},
                )
            self.fail(f"unexpected command: {value.command}")

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
        supervisor.clock = Mock()
        supervisor.clock.now.return_value = datetime(2026, 9, 16, tzinfo=timezone.utc)

        result = asyncio.run(
            supervisor._advance_warden_delivery(task, work, Mock(id="warden"))
        )

        self.assertEqual(result["state"], "returned_to_review")
        self.assertEqual(result["reason"], "workspace_source_changed")
        reopened = ledger.show("fc-work")
        self.assertEqual(reopened.fc["phase"], "reviewing")
        self.assertNotIn("delivery_finish", reopened.fc)
        self.assertIsNone(reopened.fc["delivery"]["approved_source"])
        self.assertEqual(
            reopened.fc["failed_delivery_finishes"][-1]["source_oid"], SOURCE
        )
        retained = reopened.fc["failed_delivery_finishes"][-1]["evidence"]
        self.assertEqual(retained["retained_delivery"]["source_oid"], REPAIRED_SOURCE)

    def test_controller_recovers_sealed_finish_without_delivery_from_child_receipt(
        self,
    ):
        work = record(
            owner="warden",
            role="warden",
            phase="delivering",
            ownership_operation="fc-warden-entry",
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
        finish_operation = record(
            "fc-finish",
            kind="operation",
            owner="warden",
            state="completed",
            step="warden_judgment_sealed",
            planned={
                "children": {
                    "validation": {
                        "operation_id": "fc-validation",
                        "state": "failed",
                    }
                }
            },
        )
        validation_operation = record(
            "fc-validation",
            kind="operation",
            owner="warden",
            state="failed",
            step="validation_submit_unresolved",
            error={"code": "DELIVERY_REJECTED", "message": "check failed"},
        )
        ledger = MemoryLedger(work, task, finish_operation, validation_operation)
        application = Mock()
        application.dispatch.side_effect = FulcrumError.invalid(
            "DELIVERY_NOT_STARTED", "work has no retained validation submission"
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

        self.assertTrue(result["advanced"])
        self.assertEqual(result["state"], "returned_to_review")
        reopened = ledger.show("fc-work")
        self.assertEqual(reopened.fc["phase"], "reviewing")
        self.assertNotIn("delivery_finish", reopened.fc)
        evidence = reopened.fc["failed_delivery_finishes"][-1]["evidence"]
        self.assertEqual(evidence["child"]["id"], "fc-validation")
        self.assertEqual(evidence["child"]["state"], "failed")

    def test_warden_topology_is_correctable_before_single_provider_submission(self):
        base_oid = "0" * 40
        two_commit_source = "1" * 40
        workspace = {
            "path": "/managed/worktree",
            "base_oid": base_oid,
            "head_oid": two_commit_source,
            "owned": True,
            "dirty": False,
        }
        first_request = replace(
            finish_request(),
            input={**finish_request().input, "source_oid": two_commit_source},
        )
        with (
            patch("fulcrum.completion._ledger", return_value=self.ledger),
            patch("fulcrum.completion._inspect_clean_source", return_value=workspace),
            patch(
                "fulcrum.completion.subprocess.run",
                side_effect=[
                    subprocess.CompletedProcess([], 0, "", ""),
                    subprocess.CompletedProcess([], 0, "2\n", ""),
                ],
            ),
            patch("fulcrum.completion.DeliveryService.validation_start") as submit,
        ):
            rejected = CompletionService().finish(first_request)

        self.assertEqual(rejected.state, CommandState.FAILED)
        self.assertEqual(rejected.result["step"], "warden_source_topology_correctable")
        self.assertTrue(rejected.result["correctable"])
        submit.assert_not_called()
        self.assertNotIn("delivery_finish", self.ledger.show("fc-work").fc)

        repaired_source = "2" * 40

        def validate(_request):
            current = self.ledger.show("fc-work")
            self.ledger.update_fc(
                current.id,
                {
                    **current.fc,
                    "delivery": {
                        "source_oid": repaired_source,
                        "provider_handle": "candidate",
                        "validation": {"state": "pending"},
                        "approved_source": None,
                    },
                },
            )
            return CommandResult(
                ok=True,
                state=CommandState.COMPLETED,
                operation_id="fc-validation",
                result={"validation": {"state": "pending"}},
            )

        retry = replace(
            first_request,
            request_id=str(uuid.uuid4()),
            input={**first_request.input, "source_oid": repaired_source},
        )
        workspace = {**workspace, "head_oid": repaired_source}
        with (
            patch("fulcrum.completion._ledger", return_value=self.ledger),
            patch("fulcrum.completion._inspect_clean_source", return_value=workspace),
            patch(
                "fulcrum.completion.subprocess.run",
                side_effect=[
                    subprocess.CompletedProcess([], 0, "", ""),
                    subprocess.CompletedProcess([], 0, "1\n", ""),
                ],
            ),
            patch(
                "fulcrum.completion.DeliveryService.validation_start",
                side_effect=validate,
            ) as submit,
        ):
            accepted = CompletionService().finish(retry)

        self.assertTrue(accepted.result["accepted"])
        submit.assert_called_once()


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


class DeliveryStateNormalizationTests(unittest.TestCase):
    def test_provider_terminal_states_reconcile_and_close_exactly_once(self):
        work = record(
            owner="warden",
            role="warden",
            phase="delivering",
            ownership_operation="fc-warden-entry",
            delivery={
                "source_oid": SOURCE,
                "provider_handle": "candidate",
                "validation": {"state": "pending"},
                "promotion": {"state": "not_started"},
                "synchronization": {"state": "pending"},
                "cleanup": {"state": "pending"},
            },
        )
        ledger = MemoryLedger(work)
        provider = DeliveryFacts(
            "candidate",
            SOURCE,
            "passed",
            "promoted",
            SOURCE,
            "complete",
            "complete",
            {},
            "2026-09-16T00:00:00Z",
        )
        application = Mock()
        application.dispatch.return_value = CommandResult.query(
            {"delivery": normalized_delivery(provider)}
        )
        supervisor = object.__new__(ControllerSupervisor)
        supervisor.ledger = ledger
        supervisor.application = application
        supervisor.request = request()

        reconciled, action = asyncio.run(supervisor._reconcile_work_delivery(work))

        self.assertTrue(action["advanced"])
        self.assertEqual(reconciled.fc["delivery"]["promotion"]["state"], "observed")
        self.assertEqual(
            reconciled.fc["delivery"]["synchronization"]["state"], "observed"
        )
        self.assertEqual(reconciled.fc["delivery"]["cleanup"]["state"], "observed")
        close_request = request(
            ("work", "close"),
            arguments={
                "id": "fc-work",
                "outcome": "delivered",
                "summary": "Delivered.",
            },
            actor=ActorContext(kind="controller"),
        )
        with patch("fulcrum.work._ledger", return_value=ledger):
            first = WorkService().close(close_request)
            replay = WorkService().close(close_request)

        self.assertEqual(first.result["step"], "work_closed")
        self.assertEqual(replay.operation_id, first.operation_id)
        self.assertEqual(ledger.show("fc-work").status, "closed")


class ExactSourceLocalCheckTests(unittest.TestCase):
    def test_red_and_green_checks_run_once_per_source_oid(self):
        work = record(
            owner="warden",
            role="warden",
            phase="reviewing",
            ownership_operation="fc-warden-entry",
        )
        ledger = MemoryLedger(work)
        reference = WorkRef(
            "fc-work",
            "toy",
            "/unused/project",
            "repo",
            "/unused/worktree",
            "work",
            "main",
            "fc-prepare",
            validate_argv=("scripts/check",),
        )
        provider = Mock()

        async def validate(source):
            failed = source.oid == SOURCE
            return LocalCheckFacts(
                source.oid,
                "failed" if failed else "passed",
                ("scripts/check",),
                1 if failed else 0,
                "",
                "one test failed" if failed else "",
                "2026-09-16T00:00:00Z",
            )

        provider.validate = AsyncMock(side_effect=validate)
        provider.submit = AsyncMock(
            return_value=ValidationFacts(
                "candidate",
                REPAIRED_SOURCE,
                "pending",
                (),
                {},
                "2026-09-16T00:00:01Z",
            )
        )

        def invoke(command, source):
            with (
                patch(
                    "fulcrum.delivery_service._context",
                    return_value=(ledger, ledger.show("fc-work"), {}, provider),
                ),
                patch("fulcrum.delivery_service._work_ref", return_value=reference),
            ):
                return getattr(DeliveryService(), command)(
                    request(
                        tuple(command.split("_")),
                        arguments={"bead": "fc-work", "source": source},
                        actor=ActorContext(kind="controller"),
                    )
                )

        red = invoke("validation_check", SOURCE)
        red_reuse = invoke("validation_check", SOURCE)
        red_submission = invoke("validation_start", SOURCE)

        self.assertEqual(red.state, CommandState.FAILED)
        self.assertEqual(red_reuse.operation_id, red.operation_id)
        self.assertEqual(red_submission.state, CommandState.FAILED)
        self.assertEqual(provider.validate.await_count, 1)
        provider.submit.assert_not_awaited()

        green = invoke("validation_check", REPAIRED_SOURCE)
        green_submission = invoke("validation_start", REPAIRED_SOURCE)

        self.assertEqual(green.state, CommandState.COMPLETED)
        self.assertEqual(green_submission.state, CommandState.COMPLETED)
        self.assertEqual(provider.validate.await_count, 2)
        provider.submit.assert_awaited_once()
        retained = ledger.show("fc-work").fc
        self.assertEqual(retained["local_check"]["source_oid"], REPAIRED_SOURCE)
        self.assertEqual(retained["local_check_history"][-1]["source_oid"], SOURCE)
