from __future__ import annotations

import unittest
from unittest.mock import patch

from fulcrum.analytics import (
    AnalyticsService,
    _expected_workflow_gaps,
    record_desktop_usage,
)
from tests.support import MemoryLedger, record


def test_completion_correction_records_coverage_change_without_new_turns():
    prior_summary = record(
        "fc-prior",
        kind="analytics",
        subtype="completion_summary",
        included_native_turn_ids=["task:turn"],
        rate_card_ids=["rate-1"],
        coverage="partial",
        missing_reasons=["terminal_usage_observation_missing"],
        exclusions=[],
        currency="USD",
        priced_subtotal="1.00",
        total=None,
        components={},
    )
    root = record(
        "fc-root",
        status="closed",
        workflow_root="fc-root",
        completion_cost={"summary_bead": "fc-prior", "coverage": "partial"},
    )
    ledger = MemoryLedger(root, prior_summary)
    complete_report = {
        "included_native_turn_ids": ["task:turn"],
        "rate_card_ids": ["rate-1"],
        "coverage": "complete",
        "missing_reasons": [],
        "exclusions": [],
        "currency": "USD",
        "priced_subtotal": "1.00",
        "total": "1.00",
        "components": {},
    }
    with (
        patch("fulcrum.analytics._cost_report", return_value=complete_report),
        patch("fulcrum.analytics._expected_workflow_gaps", return_value=[]),
    ):
        correction = AnalyticsService().finalize_root(
            ledger,
            root,
            "fc-reconcile",
            correction=True,
        )

    assert correction["coverage"] == "complete"
    assert correction["prior_summary"] == "fc-prior"
    assert correction["summary_bead"] != "fc-prior"
    assert (ledger.show("fc-root").fc or {})["completion_cost"] == correction


def test_latest_open_turn_is_in_progress_but_superseded_turn_needs_terminal():
    control = record("fc-system", kind="control")
    ledger = MemoryLedger(control)
    usage = [
        {
            "task_id": "task-1",
            "turn_id": turn_id,
            "response_id": f"response-{turn_id}",
            "input_tokens": 1,
            "cached_input_tokens": 0,
            "cache_write_tokens": 0,
            "output_tokens": 1,
            "reasoning_tokens": 0,
        }
        for turn_id in ("turn-1", "turn-2")
    ]
    lifecycle = [
        {"type": "turn_context", "task_id": "task-1", "turn_id": "turn-1"},
        {"type": "turn_context", "task_id": "task-1", "turn_id": "turn-2"},
    ]
    record_desktop_usage(ledger, control, "steward", usage, lifecycle)
    rows = {
        (item.fc or {})["turn_id"]: item.fc or {}
        for item in ledger.list_records(kind="analytics", limit=0)
    }
    assert "terminal_lifecycle_missing" in rows["turn-1"]["missing_reasons"]
    assert rows["turn-2"]["coverage"] == "in_progress"
    assert "terminal_lifecycle_missing" not in rows["turn-2"]["missing_reasons"]


class AnalyticsTests(unittest.TestCase):
    def test_completion_correction_tracks_coverage_change(self):
        test_completion_correction_records_coverage_change_without_new_turns()

    def test_expected_gaps_use_native_lifecycle_turn(self):
        work = record(
            "fc-root",
            workflow_root="fc-root",
            desktop={
                "assignment_history": [
                    {
                        "task_id": "task-1",
                        "turn_id": "task-1",
                        "state": "finished",
                    }
                ],
                "observations": {
                    "lifecycle": {
                        "done": {
                            "task_id": "task-1",
                            "turn_id": "turn-1",
                            "type": "task_complete",
                        }
                    }
                },
            },
        )
        usage = record(
            "fc-usage",
            kind="analytics",
            subtype="turn",
            workflow_root="fc-root",
            thread_id="task-1",
            turn_id="turn-1",
            coverage="complete",
        )
        assert (
            _expected_workflow_gaps(MemoryLedger(work, usage), "fc-root", [usage]) == []
        )
