from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from fulcrum.diagnostics import _doctor_result, _loop_health
from tests.support import MemoryLedger, record, request


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


def test_recent_heartbeat_delivery_is_healthy():
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
            datetime(2026, 9, 17, 0, 45, tzinfo=timezone.utc),
        )
    assert health[0]["state"] == "healthy"
    assert health[0]["last_success_at"] == "2026-09-17T00:30:00Z"
