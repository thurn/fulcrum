from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from fulcrum.diagnostics import DiagnosticService, _doctor_result, _loop_health
from tests.support import MemoryLedger, record, request


def test_status_surfaces_accounting_gaps_in_overall_health():
    ledger = MemoryLedger(
        record(
            "fc-usage",
            kind="analytics",
            subtype="turn",
            thread_id="task-1",
            turn_id="turn-1",
            missing_reasons=["terminal_lifecycle_missing"],
        )
    )
    with (
        patch("fulcrum.diagnostics._ledger", return_value=ledger),
        patch("fulcrum.diagnostics._capacity", return_value={}),
    ):
        result = DiagnosticService().status(
            request(("status",), arguments={}, project=None)
        )
    assert result.result["health"]["state"] == "degraded"
    assert result.result["event_log_health"]["state"] == "healthy"
    assert result.result["gaps"] == [
        {
            "component": "desktop_protocol",
            "projection": "accounting",
            "record_id": "fc-usage",
            "task_id": "task-1",
            "turn_id": "turn-1",
            "missing_reasons": ["terminal_lifecycle_missing"],
        }
    ]


def test_trace_joins_action_task_lifecycle_and_registration_recovery():
    ledger = MemoryLedger(
        record(
            "fc-work",
            phase="ready",
            desktop={
                "actions": {
                    "action-create": {
                        "action_id": "action-create",
                        "tool": "create_thread",
                        "state": "succeeded",
                        "purpose": "routine_dispatch",
                        "assignment_token": "assignment-1",
                        "native_result": {"threadId": "worker-1"},
                        "attempts": [{"attempt_id": "attempt-1"}],
                    }
                },
                "assignment_history": [
                    {
                        "assignment_token": "assignment-1",
                        "creation_action_id": "action-create",
                        "task_id": "worker-1",
                        "role": "executor",
                        "state": "registration_failed",
                    }
                ],
                "observations": {
                    "lifecycle": {
                        "stop-1": {
                            "event_id": "stop-1",
                            "type": "turn_aborted",
                            "task_id": "worker-1",
                            "turn_id": "turn-1",
                            "reason": "interrupted",
                        }
                    },
                    "timings": {
                        "worker-1:turn-1:response:r1": {
                            "kind": "model_response",
                            "item_type": "ModelResponse",
                            "task_id": "worker-1",
                            "turn_id": "turn-1",
                            "response_id": "r1",
                            "time": "2026-09-17T00:00:02.000Z",
                            "started_at": "2026-09-17T00:00:00.500Z",
                            "completed_at": "2026-09-17T00:00:02.000Z",
                            "duration_ms": 1500.0,
                            "span_id": "worker-1:turn-1:response:r1",
                            "parent_span_id": "worker-1:turn-1:item:tool-0",
                            "correlation_id": "worker-1:turn-1",
                            "outcome": "completed",
                        }
                    },
                },
                "registration_failures": [
                    {
                        "task_id": "worker-1",
                        "role": "executor",
                        "assignment_token": "assignment-1",
                        "creation_action_id": "action-create",
                        "recorded_at": "2026-09-17T00:00:00Z",
                        "terminal_event": {
                            "type": "turn_aborted",
                            "turn_id": "turn-1",
                            "reason": "interrupted",
                        },
                    }
                ],
            },
        )
    )
    log = MagicMock()
    log.read.return_value = {"items": [], "gaps": [], "next_cursor": None}
    log.files.return_value = ["events.jsonl"]
    with (
        patch("fulcrum.diagnostics._ledger", return_value=ledger),
        patch("fulcrum.diagnostics.DiagnosticLog.from_request", return_value=log),
    ):
        result = DiagnosticService().trace(
            request(("trace",), arguments={"bead": "fc-work", "limit": 0})
        )
    rows = {item["id"]: item for item in result.result["items"]}
    assert rows["action-create"]["attempt_id"] == "attempt-1"
    assert rows["action-create"]["task_id"] == "worker-1"
    assert rows["stop-1"]["outcome"] == "interrupted"
    timing = rows["worker-1:turn-1:response:r1"]
    assert timing["transition"] == "native_timing"
    assert timing["duration_ms"] == 1500.0
    assert timing["parent_span_id"] == "worker-1:turn-1:item:tool-0"
    assert timing["correlation_id"] == "worker-1:turn-1"
    assert rows["fc-work:registration-failure:0"]["outcome"] == "capacity_released"
    assert result.result["gaps"] == []


def test_intentionally_paused_bootstrap_heartbeat_is_reported_paused():
    ledger = MemoryLedger(
        record(
            "fc-system",
            kind="control",
            desktop={
                "run_control": "paused",
                "marshal_schedule": {
                    "state": "succeeded",
                    "automation_id": "marshal-check",
                    "target_task_id": "marshal-1",
                },
            },
        )
    )
    with patch("fulcrum.diagnostics._ledger", return_value=ledger):
        health = _loop_health(
            request(("doctor",)), None, datetime(2026, 9, 17, tzinfo=timezone.utc)
        )
    assert health[0]["state"] == "paused"
    assert health[0]["evidence"]["intentionally_paused"] is True


def test_running_admission_requires_active_heartbeat():
    ledger = MemoryLedger(
        record(
            "fc-system",
            kind="control",
            desktop={
                "run_control": "running",
                "marshal_schedule": {
                    "state": "succeeded",
                    "status": "PAUSED",
                    "automation_id": "marshal-check",
                    "target_task_id": "marshal-1",
                },
            },
        )
    )
    with patch("fulcrum.diagnostics._ledger", return_value=ledger):
        health = _loop_health(
            request(("doctor",)), None, datetime(2026, 9, 17, tzinfo=timezone.utc)
        )
    assert health[0]["state"] == "unavailable"
    assert health[0]["evidence"]["intentionally_paused"] is False


def test_active_heartbeat_is_initializing_until_first_delivery():
    ledger = MemoryLedger(
        record(
            "fc-system",
            kind="control",
            desktop={
                "run_control": "running",
                "marshal_schedule": {
                    "state": "succeeded",
                    "status": "ACTIVE",
                    "activated_at": "2026-09-17T00:00:00Z",
                    "automation_id": "marshal-check",
                    "target_task_id": "marshal-1",
                },
            },
        )
    )
    with patch("fulcrum.diagnostics._ledger", return_value=ledger):
        health = _loop_health(
            request(("doctor",)),
            None,
            datetime(2026, 9, 17, 0, 10, tzinfo=timezone.utc),
        )
    assert health[0]["state"] == "initializing"
    assert health[0]["last_success_at"] is None
    assert _doctor_result([], health).ok is True


def test_active_heartbeat_without_delivery_becomes_degraded():
    ledger = MemoryLedger(
        record(
            "fc-system",
            kind="control",
            desktop={
                "run_control": "running",
                "marshal_schedule": {
                    "state": "succeeded",
                    "status": "ACTIVE",
                    "activated_at": "2026-09-17T00:00:00Z",
                    "automation_id": "marshal-check",
                    "target_task_id": "marshal-1",
                },
            },
        )
    )
    with patch("fulcrum.diagnostics._ledger", return_value=ledger):
        health = _loop_health(
            request(("doctor",)),
            None,
            datetime(2026, 9, 17, 0, 21, tzinfo=timezone.utc),
        )
    assert health[0]["state"] == "degraded"
    assert health[0]["last_success_at"] is None


def test_retargeted_heartbeat_ignores_delivery_from_prior_configuration():
    ledger = MemoryLedger(
        record(
            "fc-system",
            kind="control",
            desktop={
                "run_control": "running",
                "marshal_schedule": {
                    "state": "succeeded",
                    "status": "ACTIVE",
                    "activated_at": "2026-09-17T00:30:00Z",
                    "last_delivery_at": "2026-09-17T00:20:00Z",
                    "automation_id": "marshal-check",
                    "target_task_id": "marshal-1",
                },
            },
        )
    )
    with patch("fulcrum.diagnostics._ledger", return_value=ledger):
        health = _loop_health(
            request(("doctor",)),
            None,
            datetime(2026, 9, 17, 0, 35, tzinfo=timezone.utc),
        )
    assert health[0]["state"] == "initializing"
    assert health[0]["last_success_at"] is None


def test_delivered_heartbeat_is_running_until_cycle_completes():
    ledger = MemoryLedger(
        record(
            "fc-system",
            kind="control",
            desktop={
                "run_control": "running",
                "marshal_schedule": {
                    "state": "succeeded",
                    "status": "ACTIVE",
                    "activated_at": "2026-09-17T00:00:00Z",
                    "last_delivery_at": "2026-09-17T00:30:00Z",
                    "automation_id": "marshal-check",
                    "target_task_id": "marshal-1",
                },
            },
        )
    )
    with patch("fulcrum.diagnostics._ledger", return_value=ledger):
        health = _loop_health(
            request(("doctor",)),
            None,
            datetime(2026, 9, 17, 0, 35, tzinfo=timezone.utc),
        )
    assert health[0]["state"] == "running"
    assert health[0]["last_success_at"] is None


def test_recent_completed_heartbeat_cycle_is_healthy():
    ledger = MemoryLedger(
        record(
            "fc-system",
            kind="control",
            desktop={
                "run_control": "running",
                "marshal_schedule": {
                    "state": "succeeded",
                    "status": "ACTIVE",
                    "activated_at": "2026-09-17T00:00:00Z",
                    "last_delivery_at": "2026-09-17T00:30:00Z",
                    "last_cycle_completed_at": "2026-09-17T00:31:00Z",
                    "automation_id": "marshal-check",
                    "target_task_id": "marshal-1",
                },
            },
        )
    )
    with patch("fulcrum.diagnostics._ledger", return_value=ledger):
        health = _loop_health(
            request(("doctor",)),
            None,
            datetime(2026, 9, 17, 0, 45, tzinfo=timezone.utc),
        )
    assert health[0]["state"] == "healthy"
    assert health[0]["last_success_at"] == "2026-09-17T00:31:00Z"
