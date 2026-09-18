from __future__ import annotations

from dataclasses import replace
import uuid
import unittest
from unittest.mock import MagicMock, patch

from fulcrum.contracts import ActorContext, FulcrumError
from fulcrum.completion import CompletionService
from fulcrum.desktop_protocol import DesktopProtocolService
from fulcrum.work import WorkService, _work_spec
from tests.support import MemoryLedger, record, request


def report_request(report_key: str, **changes):
    payload = {
        "report_key": report_key,
        "title": "Follow-up",
        "problem": "Observed a durable workflow issue",
        "observed_evidence": "event-1",
        "required_change": "Repair the exact boundary",
        "acceptance_checks": ["The boundary is repaired"],
        "implementation_ready": True,
        **changes,
    }
    return replace(request(("report",)), input=payload, request_id=str(uuid.uuid4()))


def test_report_key_replays_equal_input_and_rejects_changed_input():
    ledger = MemoryLedger()
    with (
        patch("fulcrum.work._ledger", return_value=ledger),
        patch("fulcrum.work._project_from_request", return_value="toy"),
    ):
        first = WorkService().report(report_request("finding-1"))
        replay = WorkService().report(report_request("finding-1"))
        assert first.result["bead_id"] == replay.result["bead_id"]
        assert replay.result["filing_state"] == "replayed"
        bead = ledger.show(first.result["bead_id"])
        assert bead.fc["phase"] == "ready"
        assert bead.fc["requested_role"] == "executor"
        with unittest.TestCase().assertRaises(FulcrumError) as raised:
            WorkService().report(
                report_request("finding-1", required_change="Different change")
            )
        assert raised.exception.code == "REQUEST_CONFLICT"


def test_weaver_entry_binds_invoking_task_without_native_creation():
    ledger = MemoryLedger()
    manager = MagicMock()
    manager.load.return_value = ({}, None)
    manager.effective.return_value = {"projects": {"toy": {"root": "/tmp/toy"}}}
    entered = replace(
        request(("enter",)),
        arguments={"role": "weaver"},
        input={"description": "Add newline to README.md"},
        actor=ActorContext.parse("task:human-task"),
        thread_id="human-task",
        request_id=str(uuid.uuid4()),
    )
    with (
        patch("fulcrum.work._ledger", return_value=ledger),
        patch("fulcrum.work._project_from_request", return_value="toy"),
        patch("fulcrum.work.ConfigurationManager", return_value=manager),
    ):
        result = WorkService().enter(entered)
    payload = result.result["result"]
    assert payload["native_task_created"] is False
    assert payload["title_action"]["tool"] == "set_thread_title"
    assert payload["title_action"]["arguments"] == {
        "threadId": "human-task",
        "title": f"🧵 [wvr-{payload['bead_id'].removeprefix('fc-')}] Add newline to README.md",
    }
    work = ledger.show(payload["bead_id"])
    assignment = work.fc["desktop"]["assignment"]
    assert assignment["entry_mode"] == "same_task"
    assert assignment["task_id"] == "human-task"
    assert work.fc["role"] == "weaver"
    assert work.fc["requested_role"] == "executor"
    action = work.fc["desktop"]["actions"][payload["title_action"]["action_id"]]
    assert action["executor"] == "weaver"
    assert action["state"] == "pending"

    ledger.rows["fc-system"] = record(
        "fc-system", kind="system", desktop={"run_control": "active"}
    )
    claim = replace(
        entered,
        command=("action", "claim"),
        arguments={
            "record_id": payload["bead_id"],
            "action_id": payload["title_action"]["action_id"],
        },
        input={"attempt_id": "title-attempt"},
        ownership_operation=None,
        request_id=str(uuid.uuid4()),
    )
    claimed = DesktopProtocolService(ledger).claim_action(claim)
    assert claimed.result["invoke"] is True

    finished = replace(
        entered,
        command=("finish",),
        arguments={"bead": payload["bead_id"], "outcome": "ready"},
        input={
            "bead": payload["bead_id"],
            "outcome": "ready",
            "summary": "README.md ends with a newline.",
            "acceptance": ["The final byte of README.md is a newline."],
            "evidence": ["README.md is the requested file."],
        },
        ownership_operation=payload["ownership_operation"],
        request_id=str(uuid.uuid4()),
    )
    with patch("fulcrum.completion._ledger", return_value=ledger):
        with unittest.TestCase().assertRaises(FulcrumError) as raised:
            CompletionService().finish(finished)
    assert raised.exception.code == "ACTION_RESULT_REQUIRED"

    reported = replace(
        entered,
        command=("action", "result"),
        arguments={
            "record_id": payload["bead_id"],
            "action_id": payload["title_action"]["action_id"],
        },
        input={
            "attempt_id": "title-attempt",
            "outcome": "succeeded",
            "native_result": payload["title_action"]["expected_result"],
        },
        ownership_operation=None,
        request_id=str(uuid.uuid4()),
    )
    DesktopProtocolService(ledger).report_action_result(reported)

    with patch("fulcrum.completion._ledger", return_value=ledger):
        completion = CompletionService().finish(finished)
    retained = ledger.show(payload["bead_id"])
    assignment = retained.fc["desktop"]["assignment"]
    assert assignment["finish_operation"] == completion.operation_id


def test_work_creation_cannot_request_weaver_dispatch():
    with unittest.TestCase().assertRaises(FulcrumError) as raised:
        _work_spec(
            {
                "title": "Bad dispatch",
                "outcome": "Do not create another Weaver",
                "requested_role": "weaver",
            },
            project="toy",
            key="root",
        )
    assert raised.exception.code == "WEAVER_TASK_FORBIDDEN"


def test_downstream_work_view_withholds_raw_intake():
    ledger = MemoryLedger(
        record(
            "fc-work",
            owner="executor-1",
            role="executor",
            outcome="RAW $weaver intake",
            context=["raw transcript"],
            desktop={
                "assignment": {
                    "task_id": "executor-1",
                    "role": "executor",
                    "state": "active",
                    "scope": {
                        "behavioral_outcome": "Add newline to README.md",
                        "acceptance": ["README.md ends in a newline"],
                        "evidence": ["README.md lacks the newline"],
                    },
                }
            },
        )
    )
    show = replace(
        request(("work", "show")),
        arguments={"id": "fc-work"},
        actor=ActorContext.parse("task:executor-1"),
        thread_id="executor-1",
    )
    with patch("fulcrum.work._ledger", return_value=ledger):
        result = WorkService().show(show)
    assert "RAW" not in str(result.result)
    assert "$weaver" not in str(result.result)
    assert "raw transcript" not in str(result.result)
    assert result.result["fc"]["outcome"] == "Add newline to README.md"


class WorkReportTests(unittest.TestCase):
    def test_report_replay(self):
        test_report_key_replays_equal_input_and_rejects_changed_input()

    def test_weaver_entry_is_same_task(self):
        test_weaver_entry_binds_invoking_task_without_native_creation()

    def test_weaver_dispatch_is_rejected(self):
        test_work_creation_cannot_request_weaver_dispatch()

    def test_raw_intake_is_withheld_downstream(self):
        test_downstream_work_view_withholds_raw_intake()
