from __future__ import annotations

import unittest
from unittest.mock import patch

from fulcrum.analytics import (
    AnalyticsService,
    _expected_workflow_gaps,
    reconcile_descendant_attribution,
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


def test_usage_reconcile_backfills_one_observed_turn_model():
    control = record("fc-system", kind="control")
    ledger = MemoryLedger(control)
    usage = [
        {
            "task_id": "task-1",
            "turn_id": "turn-1",
            "response_id": "response-1",
            "model": "gpt-5.6-sol",
            "input_tokens": 10,
            "cached_input_tokens": 0,
            "cache_write_tokens": 0,
            "output_tokens": 2,
            "reasoning_tokens": 0,
        },
        {
            "task_id": "task-1",
            "turn_id": "turn-1",
            "response_id": "response-2",
            "model": None,
            "input_tokens": 12,
            "cached_input_tokens": 0,
            "cache_write_tokens": 0,
            "output_tokens": 3,
            "reasoning_tokens": 0,
        },
    ]
    lifecycle = [{"type": "task_complete", "task_id": "task-1", "turn_id": "turn-1"}]

    record_desktop_usage(ledger, control, "vizier", usage, lifecycle)

    analytics = ledger.list_records(kind="analytics", limit=0)[0]
    retained = analytics.fc or {}
    assert retained["model"]["effective"] == "gpt-5.6-sol"
    assert "effective_model_missing" not in retained["missing_reasons"]
    assert {
        response["effective_model"] for response in retained["response_records"]
    } == {"gpt-5.6-sol"}


def test_terminal_only_delta_updates_existing_turn_coverage():
    control = record("fc-system", kind="control")
    ledger = MemoryLedger(control)
    usage = [
        {
            "task_id": "task-1",
            "turn_id": "turn-1",
            "response_id": "response-1",
            "model": "gpt-5.6-sol",
            "input_tokens": 10,
            "cached_input_tokens": 0,
            "cache_write_tokens": 0,
            "output_tokens": 2,
            "reasoning_tokens": 0,
        }
    ]
    record_desktop_usage(ledger, control, "marshal", usage, [])
    analytics = ledger.list_records(kind="analytics", limit=0)[0]
    assert (analytics.fc or {})["coverage"] == "in_progress"

    record_desktop_usage(
        ledger,
        control,
        "marshal",
        [],
        [{"type": "turn_complete", "task_id": "task-1", "turn_id": "turn-1"}],
    )

    retained = ledger.show(analytics.id).fc or {}
    assert retained["terminal_state"] == "turn_complete"
    assert retained["coverage"] == "partial"
    assert retained["coverage"] != "in_progress"
    assert len(retained["raw_responses"]) == 1


def test_terminal_only_delta_without_prior_usage_remains_partial():
    control = record("fc-system", kind="control")
    ledger = MemoryLedger(control)

    record_desktop_usage(
        ledger,
        control,
        "marshal",
        [],
        [{"type": "turn_complete", "task_id": "task-1", "turn_id": "turn-1"}],
    )

    retained = ledger.list_records(kind="analytics", limit=0)[0].fc or {}
    assert retained["terminal_state"] == "turn_complete"
    assert retained["coverage"] == "partial"
    assert retained["missing_reasons"] == ["usage_observation_missing"]


def test_subagent_inherits_review_and_coordination_parent_components():
    usage = {
        "response_id": "response",
        "model": "gpt-5.6-sol",
        "input_tokens": 1,
        "cached_input_tokens": 0,
        "cache_write_tokens": 0,
        "output_tokens": 1,
        "reasoning_tokens": 0,
    }
    for record_id, role, expected in (
        ("fc-review", "warden", "review"),
        ("fc-system", "steward", "coordination"),
    ):
        parent_task = f"{role}-parent"
        child_task = f"{role}-child"
        retained = record(
            record_id,
            kind="control" if record_id == "fc-system" else "work",
            workflow_root="fc-root",
        )
        ledger = MemoryLedger(retained)
        record_desktop_usage(
            ledger,
            retained,
            role,
            [{**usage, "task_id": parent_task, "turn_id": "parent-turn"}],
            [
                {
                    "type": "turn_complete",
                    "task_id": parent_task,
                    "turn_id": "parent-turn",
                }
            ],
        )
        record_desktop_usage(
            ledger,
            retained,
            role,
            [{**usage, "task_id": child_task, "turn_id": "child-turn"}],
            [
                {
                    "type": "turn_complete",
                    "task_id": child_task,
                    "turn_id": "child-turn",
                }
            ],
            lineage={
                "parent_task_id": parent_task,
                "parent_turn_id": "parent-turn",
                "spawn_activity_id": "spawn",
                "lineage_depth": 1,
                "telemetry_semantics": "separate_child_response_counters",
            },
        )
        child = next(
            item.fc or {}
            for item in ledger.list_records(kind="analytics", limit=0)
            if (item.fc or {}).get("thread_id") == child_task
        )
        assert child["component"] == expected
        assert child["attributions"] == [{"workflow_root": "fc-root", "weight": "1"}]


def test_delayed_parent_attribution_reconciles_existing_child_turn():
    work = record("fc-root", workflow_root="fc-root")
    ledger = MemoryLedger(work)
    usage = {
        "response_id": "response",
        "model": "gpt-5.6-sol",
        "input_tokens": 1,
        "cached_input_tokens": 0,
        "cache_write_tokens": 0,
        "output_tokens": 1,
        "reasoning_tokens": 0,
    }
    lineage = {
        "task_id": "child",
        "parent_task_id": "parent",
        "parent_turn_id": "parent-turn",
        "lineage_depth": 1,
    }
    record_desktop_usage(
        ledger,
        work,
        "executor",
        [{**usage, "task_id": "child", "turn_id": "child-turn"}],
        [
            {
                "type": "turn_complete",
                "task_id": "child",
                "turn_id": "child-turn",
            }
        ],
        lineage=lineage,
    )
    child = next(
        item
        for item in ledger.list_records(kind="analytics", limit=0)
        if (item.fc or {}).get("thread_id") == "child"
    )
    assert "causal_parent_attribution_missing" in (child.fc or {})["missing_reasons"]
    record_desktop_usage(
        ledger,
        work,
        "executor",
        [{**usage, "task_id": "parent", "turn_id": "parent-turn"}],
        [
            {
                "type": "turn_complete",
                "task_id": "parent",
                "turn_id": "parent-turn",
            }
        ],
    )

    assert reconcile_descendant_attribution(ledger, lineage) is True
    updated = ledger.show(child.id).fc or {}
    assert updated["component"] == "direct"
    assert updated["attributions"] == [{"workflow_root": "fc-root", "weight": "1"}]
    assert "causal_parent_attribution_missing" not in updated["missing_reasons"]
    assert reconcile_descendant_attribution(ledger, lineage) is False


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
