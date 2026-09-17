from __future__ import annotations

import unittest
from unittest.mock import patch

from fulcrum.analytics import AnalyticsService
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


class AnalyticsTests(unittest.TestCase):
    def test_completion_correction_tracks_coverage_change(self):
        test_completion_correction_records_coverage_change_without_new_turns()
