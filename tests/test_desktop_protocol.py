from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import uuid
import unittest
from unittest.mock import patch

from fulcrum.contracts import ActorContext, CommandResult, FulcrumError
from fulcrum.completion import settle_native_completion
from fulcrum.desktop_protocol import DesktopProtocolService
from tests.support import MemoryLedger, record, request


def mutation(command, *, actor="human", arguments=None, payload=None, request_id=None):
    task = actor.removeprefix("task:") if actor.startswith("task:") else None
    return replace(
        request(command),
        actor=ActorContext.parse(actor),
        thread_id=task,
        arguments=arguments or {},
        input=payload or {},
        request_id=request_id or str(uuid.uuid4()),
    )


def registered_service(*work):
    ledger = MemoryLedger(*work)
    service = DesktopProtocolService(
        ledger, now=lambda: datetime(2026, 9, 16, tzinfo=timezone.utc)
    )
    action = service.queue_action(
        mutation(
            ("action", "queue"),
            payload={
                "executor": "bootstrap",
                "tool": "create_thread",
                "arguments": {"prompt": "register steward"},
                "purpose": "bootstrap_steward",
            },
        )
    ).result["action"]
    service.register_standing(
        mutation(
            ("register", "standing"),
            payload={
                "role": "steward",
                "task_id": "steward-1",
                "session_id": "s1",
                "action_id": action["action_id"],
            },
        )
    )
    return service, ledger


def test_equal_action_claim_replays_without_authorizing_second_invocation():
    service, _ = registered_service()
    queued = service.queue_action(
        mutation(
            ("action", "queue"),
            payload={
                "record_id": "fc-system",
                "executor": "steward",
                "tool": "send_message_to_thread",
                "arguments": {"threadId": "worker-1", "prompt": "continue"},
            },
        )
    ).result["action"]
    request_id = str(uuid.uuid4())
    claim = mutation(
        ("action", "claim"),
        actor="task:steward-1",
        arguments={"record_id": "fc-system", "action_id": queued["action_id"]},
        payload={"attempt_id": "attempt-1"},
        request_id=request_id,
    )
    first = service.claim_action(claim)
    replay = service.claim_action(claim)
    assert first.result["invoke"] is True
    assert replay.result == first.result
    changed = replace(claim, input={"attempt_id": "attempt-2"})
    try:
        service.claim_action(changed)
    except FulcrumError as error:
        assert error.code == "REQUEST_CONFLICT"
    else:
        raise AssertionError("changed request input was accepted")


def test_steward_selects_ready_action_without_marshal_and_pause_holds_it():
    work = record("fc-a", phase="ready", priority=1)
    service, ledger = registered_service(work)
    service.queue_action(
        mutation(
            ("action", "queue"),
            payload={
                "record_id": "fc-a",
                "executor": "steward",
                "tool": "create_thread",
                "arguments": {"prompt": "implement"},
                "assignment_token": "assignment-1",
            },
        )
    )
    wait_request = mutation(
        ("instruction", "wait"),
        actor="task:steward-1",
        payload={"loop_id": "loop-1", "turn_id": "turn-1"},
    )
    waiting = service.wait_for_instructions(wait_request)
    assert waiting.state.value == "running"
    service.resume(mutation(("resume",), payload={"reason": "acceptance"}))
    resolved = service.wait_for_instructions(wait_request)
    assert resolved.result["kind"] == "action"
    assert resolved.result["action"]["record_id"] == "fc-a"
    assert resolved.result["action"]["arguments"]["prompt"].startswith(
        "Fulcrum-Action:"
    )
    assert "fc-a" in ledger.writes


def test_ci_wait_is_transport_state_and_keeps_assignment_active():
    work = record(
        "fc-a",
        desktop={
            "assignment": {
                "assignment_token": "assignment-1",
                "task_id": "warden-1",
                "role": "warden",
                "state": "active",
            },
            "candidate": {
                "candidate_id": "candidate-1",
                "source": "abc",
                "provider_run_id": "run-1",
                "state": "running",
                "deadline": "2026-09-16T00:30:00Z",
            },
        },
    )
    service = DesktopProtocolService(MemoryLedger(work))
    with patch(
        "fulcrum.delivery_service.DeliveryService.validation_show",
        return_value=CommandResult.query({"state": "running"}),
    ):
        waiting = service.wait_for_ci_results(
            mutation(
                ("ci", "wait"),
                actor="task:warden-1",
                arguments={"bead": "fc-a"},
                payload={
                    "candidate_id": "candidate-1",
                    "turn_id": "turn-1",
                    "assignment_token": "assignment-1",
                },
            )
        )
    assert waiting.state.value == "running"
    assert waiting.result["transport_wait"]["kind"] == "ci"


def test_assignment_releases_only_after_exact_native_completion():
    assignment = {
        "assignment_token": "assignment-1",
        "task_id": "executor-1",
        "turn_id": "turn-1",
        "role": "executor",
        "state": "active",
        "finish_operation": "fc-op-finish",
    }
    ledger = MemoryLedger(
        record(
            "fc-a",
            owner="executor-1",
            phase="ready",
            desktop={"assignment": assignment, "observations": {"lifecycle": {}}},
        )
    )
    assert not settle_native_completion(request(), ledger, "fc-a")
    assert (ledger.show("fc-a").fc or {})["desktop"]["assignment"] == assignment

    work = ledger.show("fc-a")
    fc = dict(work.fc or {})
    desktop = dict(fc["desktop"])
    desktop["observations"] = {
        "lifecycle": {
            "done": {
                "event_id": "done",
                "type": "turn_completed",
                "task_id": "executor-1",
                "turn_id": "turn-1",
            }
        }
    }
    fc["desktop"] = desktop
    ledger.update_fc("fc-a", fc)

    assert settle_native_completion(request(), ledger, "fc-a")
    settled = ledger.show("fc-a")
    settled_desktop = (settled.fc or {})["desktop"]
    assert "assignment" not in settled_desktop
    assert settled_desktop["assignment_history"][-1]["state"] == "finished"


class DesktopProtocolTests(unittest.TestCase):
    def test_equal_action_claim_is_idempotent(self):
        test_equal_action_claim_replays_without_authorizing_second_invocation()

    def test_steward_selects_without_marshal(self):
        test_steward_selects_ready_action_without_marshal_and_pause_holds_it()

    def test_ci_wait_is_transport_state(self):
        test_ci_wait_is_transport_state_and_keeps_assignment_active()

    def test_native_completion_releases_assignment(self):
        test_assignment_releases_only_after_exact_native_completion()
