from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from fulcrum.diagnostics import _loop_health
from tests.support import MemoryLedger, record, request


def test_intentionally_paused_bootstrap_heartbeat_is_healthy():
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
    assert health[0]["state"] == "healthy"
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
