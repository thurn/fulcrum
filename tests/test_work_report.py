from __future__ import annotations

from dataclasses import replace
import uuid
import unittest
from unittest.mock import patch

from fulcrum.contracts import FulcrumError
from fulcrum.work import WorkService
from tests.support import MemoryLedger, request


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
