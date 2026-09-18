from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import uuid
import unittest
from unittest.mock import patch

from fulcrum.contracts import ActorContext, CommandResult, FulcrumError
from fulcrum.completion import settle_native_completion
from fulcrum.desktop_protocol import DesktopProtocolService, _worker_prompt, role_title
from tests.support import (
    MemoryLedger,
    observe_action_prompt,
    record,
    request,
    seed_action,
)


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
    action = seed_action(
        ledger,
        {
            "executor": "bootstrap",
            "tool": "create_thread",
            "arguments": {"prompt": "register steward"},
            "purpose": "bootstrap_steward",
        },
    )
    observe_action_prompt(ledger, action, task_id="steward-1", session_id="s1")
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
    service, ledger = registered_service()
    service.resume(mutation(("resume",), payload={"reason": "test"}))
    queued = seed_action(
        ledger,
        {
            "record_id": "fc-system",
            "executor": "steward",
            "tool": "send_message_to_thread",
            "arguments": {"threadId": "worker-1", "prompt": "continue"},
        },
    )
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


def test_message_success_requires_matching_target_identity():
    service, ledger = registered_service()
    service.resume(mutation(("resume",), payload={"reason": "test"}))
    action = seed_action(
        ledger,
        {
            "record_id": "fc-system",
            "executor": "steward",
            "tool": "send_message_to_thread",
            "arguments": {"threadId": "worker-1", "prompt": "continue"},
        },
    )
    service.claim_action(
        mutation(
            ("action", "claim"),
            actor="task:steward-1",
            arguments={"record_id": "fc-system", "action_id": action["action_id"]},
            payload={"attempt_id": "message-attempt"},
        )
    )
    with unittest.TestCase().assertRaises(FulcrumError) as raised:
        service.report_action_result(
            mutation(
                ("action", "result"),
                actor="task:steward-1",
                arguments={
                    "record_id": "fc-system",
                    "action_id": action["action_id"],
                },
                payload={
                    "attempt_id": "message-attempt",
                    "outcome": "succeeded",
                    "native_result": {"threadId": "different-worker"},
                },
            )
        )
    assert raised.exception.code == "RESULT_CONFLICT"


def test_automation_success_accepts_native_result_without_target_but_rejects_conflict():
    service, ledger = registered_service()
    service.resume(mutation(("resume",), payload={"reason": "test"}))
    action = seed_action(
        ledger,
        {
            "record_id": "fc-system",
            "executor": "steward",
            "tool": "automation_update",
            "arguments": {
                "id": "marshal-check",
                "mode": "update",
                "status": "PAUSED",
                "targetThreadId": "marshal-1",
            },
        },
    )
    service.claim_action(
        mutation(
            ("action", "claim"),
            actor="task:steward-1",
            arguments={"record_id": "fc-system", "action_id": action["action_id"]},
            payload={"attempt_id": "automation-attempt"},
        )
    )
    completed = service.report_action_result(
        mutation(
            ("action", "result"),
            actor="task:steward-1",
            arguments={"record_id": "fc-system", "action_id": action["action_id"]},
            payload={
                "attempt_id": "automation-attempt",
                "outcome": "succeeded",
                "native_result": {
                    "automationId": "marshal-check",
                    "mode": "update",
                    "status": "PAUSED",
                },
            },
        )
    )
    assert completed.result["state"] == "succeeded"

    conflict = seed_action(
        ledger,
        {
            "record_id": "fc-system",
            "executor": "steward",
            "tool": "automation_update",
            "arguments": {
                "id": "other-check",
                "mode": "update",
                "status": "PAUSED",
                "targetThreadId": "marshal-1",
            },
        },
    )
    service.claim_action(
        mutation(
            ("action", "claim"),
            actor="task:steward-1",
            arguments={"record_id": "fc-system", "action_id": conflict["action_id"]},
            payload={"attempt_id": "conflict-attempt"},
        )
    )
    with unittest.TestCase().assertRaises(FulcrumError) as raised:
        service.report_action_result(
            mutation(
                ("action", "result"),
                actor="task:steward-1",
                arguments={
                    "record_id": "fc-system",
                    "action_id": conflict["action_id"],
                },
                payload={
                    "attempt_id": "conflict-attempt",
                    "outcome": "succeeded",
                    "native_result": {
                        "automationId": "other-check",
                        "status": "PAUSED",
                        "targetThreadId": "different-marshal",
                    },
                },
            )
        )
    assert raised.exception.code == "RESULT_CONFLICT"


def test_rejected_worker_creation_releases_assignment_for_retry():
    work = record(
        "fc-work",
        owner="STEWARD",
        phase="ready",
        requested_role="warden",
        role="warden",
        ownership_operation="assignment-1",
        desktop={
            "assignment": {
                "assignment_token": "assignment-1",
                "role": "warden",
                "state": "reserved",
            }
        },
    )
    service, ledger = registered_service(work)
    service.resume(mutation(("resume",), payload={"reason": "test"}))
    action = seed_action(
        ledger,
        {
            "record_id": "fc-work",
            "executor": "steward",
            "tool": "create_thread",
            "arguments": {
                "prompt": "review",
                "target": {"type": "project", "projectId": "project-1"},
            },
            "assignment_token": "assignment-1",
            "purpose": "routine_dispatch",
        },
    )
    service.claim_action(
        mutation(
            ("action", "claim"),
            actor="task:steward-1",
            arguments={"record_id": "fc-work", "action_id": action["action_id"]},
            payload={"attempt_id": "dispatch-attempt"},
        )
    )
    service.report_action_result(
        mutation(
            ("action", "result"),
            actor="task:steward-1",
            arguments={"record_id": "fc-work", "action_id": action["action_id"]},
            payload={
                "attempt_id": "dispatch-attempt",
                "outcome": "rejected",
                "native_result": {"isError": True},
            },
        )
    )

    retained = ledger.show("fc-work").fc or {}
    protocol = retained["desktop"]
    assert "assignment" not in protocol
    assert protocol["assignment_history"][-1]["state"] == "dispatch_rejected"
    assert retained["role"] is None
    assert retained["ownership_operation"] is None

    protocol["assignment"] = {
        "assignment_token": "assignment-1",
        "role": "warden",
        "state": "reserved",
    }
    protocol["assignment_history"] = []
    ledger.update_fc(
        "fc-work",
        {
            **retained,
            "desktop": protocol,
            "role": "warden",
            "ownership_operation": "assignment-1",
        },
    )
    service.report_action_result(
        mutation(
            ("action", "result"),
            actor="task:steward-1",
            arguments={"record_id": "fc-work", "action_id": action["action_id"]},
            payload={
                "attempt_id": "dispatch-attempt",
                "outcome": "rejected",
                "native_result": {"isError": True},
            },
        )
    )
    repaired = ledger.show("fc-work").fc or {}
    assert "assignment" not in repaired["desktop"]
    assert repaired["desktop"]["assignment_history"][-1]["state"] == "dispatch_rejected"


def test_automation_update_rejects_a_different_native_identity():
    service, ledger = registered_service()
    service.resume(mutation(("resume",), payload={"reason": "test"}))
    action = seed_action(
        ledger,
        {
            "record_id": "fc-system",
            "executor": "steward",
            "tool": "automation_update",
            "arguments": {
                "id": "marshal-check",
                "mode": "update",
                "status": "ACTIVE",
                "targetThreadId": "marshal-1",
            },
        },
    )
    service.claim_action(
        mutation(
            ("action", "claim"),
            actor="task:steward-1",
            arguments={"record_id": "fc-system", "action_id": action["action_id"]},
            payload={"attempt_id": "identity-attempt"},
        )
    )
    with unittest.TestCase().assertRaises(FulcrumError) as raised:
        service.report_action_result(
            mutation(
                ("action", "result"),
                actor="task:steward-1",
                arguments={
                    "record_id": "fc-system",
                    "action_id": action["action_id"],
                },
                payload={
                    "attempt_id": "identity-attempt",
                    "outcome": "succeeded",
                    "native_result": {
                        "automationId": "different-check",
                        "status": "ACTIVE",
                    },
                },
            )
        )
    assert raised.exception.code == "RESULT_CONFLICT"


def test_claim_revalidates_dependency_after_reservation():
    work = record(
        "fc-a",
        phase="ready",
        project="toy",
        desktop={
            "assignment": {
                "assignment_token": "assignment-1",
                "state": "reserved",
                "role": "executor",
            }
        },
    )
    dependency = record("fc-dependency", phase="backlog")
    service, ledger = registered_service(work, dependency)
    service.resume(mutation(("resume",), payload={"reason": "test"}))
    ledger.edges["fc-a"] = ["fc-dependency"]
    action = seed_action(
        ledger,
        {
            "record_id": "fc-a",
            "executor": "steward",
            "tool": "create_thread",
            "arguments": {"prompt": "implement"},
            "assignment_token": "assignment-1",
            "purpose": "routine_dispatch",
        },
    )
    with unittest.TestCase().assertRaises(FulcrumError) as raised:
        service.claim_action(
            mutation(
                ("action", "claim"),
                actor="task:steward-1",
                arguments={"record_id": "fc-a", "action_id": action["action_id"]},
                payload={"attempt_id": "dependency-attempt"},
            )
        )
    assert raised.exception.code == "ACTION_SUPERSEDED"
    retained = (ledger.show("fc-a").fc or {})["desktop"]["actions"]
    assert retained[action["action_id"]]["state"] == "superseded"


def test_registration_requires_and_consumes_trusted_prompt_handshake():
    ledger = MemoryLedger()
    service = DesktopProtocolService(ledger)
    action = seed_action(
        ledger,
        {
            "executor": "bootstrap",
            "tool": "create_thread",
            "arguments": {"prompt": "register steward"},
            "purpose": "bootstrap_steward",
        },
    )
    registration = mutation(
        ("register", "standing"),
        payload={
            "role": "steward",
            "task_id": "steward-1",
            "session_id": "session-1",
            "action_id": action["action_id"],
        },
    )
    with unittest.TestCase().assertRaises(FulcrumError) as raised:
        service.register_standing(registration)
    assert raised.exception.code == "REGISTRATION_OBSERVATION_REQUIRED"
    observe_action_prompt(
        ledger,
        action,
        task_id="steward-1",
        session_id="session-1",
    )
    service.register_standing(registration)
    handshake = next(
        iter((ledger.show("fc-system").fc or {})["desktop"]["handshakes"].values())
    )
    assert handshake["disposition"] == "registered"
    assert handshake["consumed_at"]


def test_registration_accepts_matching_succeeded_creation_result():
    ledger = MemoryLedger()
    service = DesktopProtocolService(ledger)
    action = seed_action(
        ledger,
        {
            "executor": "bootstrap",
            "tool": "create_thread",
            "arguments": {"prompt": "register steward"},
            "purpose": "bootstrap_steward",
        },
    )
    service.claim_action(
        mutation(
            ("action", "claim"),
            arguments={"record_id": "fc-system", "action_id": action["action_id"]},
            payload={"attempt_id": "create-standing"},
        )
    )
    service.report_action_result(
        mutation(
            ("action", "result"),
            arguments={"record_id": "fc-system", "action_id": action["action_id"]},
            payload={
                "attempt_id": "create-standing",
                "outcome": "succeeded",
                "native_result": {"threadId": "steward-1", "hostId": "local"},
            },
        )
    )

    result = service.register_standing(
        mutation(
            ("register", "standing"),
            actor="task:steward-1",
            payload={
                "role": "steward",
                "task_id": "steward-1",
                "session_id": "session-1",
                "action_id": action["action_id"],
            },
        )
    )

    observation = result.result["standing"]["registration_observation"]
    assert observation["kind"] == "creation_result"
    assert observation["native_result"]["threadId"] == "steward-1"


def test_replacement_steward_registration_cancels_previous_instruction_wait():
    service, ledger = registered_service()
    system = ledger.show("fc-system")
    fc = dict(system.fc or {})
    desktop = dict(fc.get("desktop") or {})
    standing = dict(desktop.get("standing") or {})
    standing["steward"] = {
        **standing["steward"],
        "state": "replacement_pending",
    }
    desktop["standing"] = standing
    desktop["instruction_waits"] = {
        "wait-old": {
            "wait_id": "wait-old",
            "request_id": "old-request",
            "accepted_input": {"loop_id": "old-loop", "turn_id": "old-turn"},
            "task_id": "steward-1",
            "state": "waiting",
        }
    }
    fc["desktop"] = desktop
    ledger.update_fc(system.id, fc)
    action = seed_action(
        ledger,
        {
            "executor": "bootstrap",
            "tool": "create_thread",
            "arguments": {"prompt": "replace steward"},
            "purpose": "recover_steward",
        },
    )
    observe_action_prompt(
        ledger,
        action,
        task_id="steward-2",
        session_id="s2",
    )

    service.register_standing(
        mutation(
            ("register", "standing"),
            actor="task:steward-2",
            payload={
                "role": "steward",
                "task_id": "steward-2",
                "session_id": "s2",
                "action_id": action["action_id"],
            },
        )
    )

    retained = (ledger.show("fc-system").fc or {})["desktop"]
    assert retained["standing"]["steward"]["task_id"] == "steward-2"
    cancelled = retained["instruction_waits"]["wait-old"]
    assert cancelled["state"] == "cancelled"
    assert cancelled["response"]["reason"] == "standing_replaced"


def test_new_steward_wait_recovers_action_granted_after_previous_turn_ended():
    service, ledger = registered_service(record("fc-a", phase="ready"))
    action = seed_action(
        ledger,
        {
            "record_id": "fc-a",
            "executor": "steward",
            "tool": "create_thread",
            "arguments": {"prompt": "authorized work"},
            "purpose": "routine_dispatch",
        },
    )
    work = ledger.show("fc-a")
    fc = dict(work.fc or {})
    desktop = dict(fc.get("desktop") or {})
    actions = dict(desktop.get("actions") or {})
    actions[action["action_id"]] = {
        **actions[action["action_id"]],
        "granted_wait_id": "wait-old",
    }
    desktop["actions"] = actions
    fc["desktop"] = desktop
    ledger.update_fc(work.id, fc)
    system = ledger.show("fc-system")
    system_fc = dict(system.fc or {})
    system_desktop = dict(system_fc.get("desktop") or {})
    system_desktop["instruction_waits"] = {
        "wait-old": {
            "wait_id": "wait-old",
            "task_id": "steward-1",
            "state": "resolved",
        }
    }
    system_fc["desktop"] = system_desktop
    ledger.update_fc(system.id, system_fc)
    service.resume(mutation(("resume",), payload={"reason": "test"}))

    result = service.wait_for_instructions(
        mutation(
            ("instruction", "wait"),
            actor="task:steward-1",
            payload={"loop_id": "new-loop", "turn_id": "new-turn"},
        )
    )

    assert result.result["kind"] == "action"
    assert result.result["action"]["action_id"] == action["action_id"]
    retained = (ledger.show("fc-a").fc or {})["desktop"]["actions"]
    assert retained[action["action_id"]]["granted_wait_id"] != "wait-old"


def test_production_service_has_no_action_injection_api():
    service, _ = registered_service()
    assert not hasattr(service, "queue_action")


def test_successful_creation_does_not_schedule_unobserved_title_normalization():
    service, ledger = registered_service()
    service.resume(mutation(("resume",), payload={"reason": "test"}))
    action = seed_action(
        ledger,
        {
            "executor": "steward",
            "tool": "create_thread",
            "arguments": {"prompt": "work", "title": "EXECUTOR fc-a"},
            "purpose": "test_creation",
        },
    )
    service.claim_action(
        mutation(
            ("action", "claim"),
            actor="task:steward-1",
            arguments={"record_id": "fc-system", "action_id": action["action_id"]},
            payload={"attempt_id": "attempt-create"},
        )
    )
    service.report_action_result(
        mutation(
            ("action", "result"),
            actor="task:steward-1",
            arguments={"record_id": "fc-system", "action_id": action["action_id"]},
            payload={
                "attempt_id": "attempt-create",
                "outcome": "succeeded",
                "native_result": {"threadId": "worker-1"},
            },
        )
    )
    service.resume(mutation(("resume",), payload={"reason": "test"}))
    result = service.wait_for_instructions(
        mutation(
            ("instruction", "wait"),
            actor="task:steward-1",
            payload={"loop_id": "titles", "turn_id": "turn-titles"},
        )
    )
    assert result.result["transport_wait"]["state"] == "waiting"
    actions = (ledger.show("fc-system").fc or {})["desktop"]["actions"]
    assert not any(item.get("tool") == "set_thread_title" for item in actions.values())


def test_observed_title_mismatch_schedules_one_correction():
    service, ledger = registered_service()
    service.resume(mutation(("resume",), payload={"reason": "test"}))
    action = seed_action(
        ledger,
        {
            "executor": "steward",
            "tool": "create_thread",
            "arguments": {"prompt": "work", "title": "Expected title"},
            "purpose": "test_creation",
        },
    )
    service.claim_action(
        mutation(
            ("action", "claim"),
            actor="task:steward-1",
            arguments={"record_id": "fc-system", "action_id": action["action_id"]},
            payload={"attempt_id": "attempt-create"},
        )
    )
    service.report_action_result(
        mutation(
            ("action", "result"),
            actor="task:steward-1",
            arguments={"record_id": "fc-system", "action_id": action["action_id"]},
            payload={
                "attempt_id": "attempt-create",
                "outcome": "succeeded",
                "native_result": {"threadId": "worker-1", "title": "Wrong title"},
            },
        )
    )
    correction = next(
        value
        for value in (ledger.show("fc-system").fc or {})["desktop"]["actions"].values()
        if value.get("tool") == "set_thread_title"
    )
    assert correction["arguments"] == {
        "threadId": "worker-1",
        "title": "Expected title",
    }


def test_closed_settled_worker_archives_only_after_ten_minutes():
    now = datetime.now(timezone.utc)
    work = record(
        "fc-a",
        status="closed",
        phase="done",
        desktop={
            "assignment_history": [
                {
                    "task_id": "worker-1",
                    "role": "executor",
                    "state": "finished",
                    "released_at": (now - timedelta(minutes=9)).isoformat(),
                }
            ]
        },
    )
    service, ledger = registered_service(work)
    service.now = lambda: now
    service.resume(mutation(("resume",), payload={"reason": "test"}))
    wait_request = mutation(
        ("instruction", "wait"),
        actor="task:steward-1",
        payload={"loop_id": "archive", "turn_id": "turn-archive"},
    )
    waiting = service.wait_for_instructions(wait_request)
    assert waiting.result["transport_wait"]["state"] == "waiting"
    retained = ledger.show("fc-a")
    fc = dict(retained.fc or {})
    protocol = dict(fc["desktop"])
    history = list(protocol["assignment_history"])
    history[0] = {
        **history[0],
        "released_at": (now - timedelta(minutes=10, seconds=1)).isoformat(),
    }
    protocol["assignment_history"] = history
    fc["desktop"] = protocol
    ledger.update_fc("fc-a", fc)
    result = service.wait_for_instructions(wait_request)
    assert result.result["action"]["tool"] == "set_thread_archived"
    assert result.result["action"]["arguments"]["threadId"] == "worker-1"
    assert result.result["action"]["arguments"]["archived"] is True


def test_steward_selects_ready_action_without_marshal_and_pause_holds_it():
    work = record("fc-a", phase="ready", priority=1)
    service, ledger = registered_service(work)
    seed_action(
        ledger,
        {
            "record_id": "fc-a",
            "executor": "steward",
            "tool": "create_thread",
            "arguments": {"prompt": "implement"},
            "assignment_token": "assignment-1",
        },
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
    service = DesktopProtocolService(
        MemoryLedger(work), now=lambda: datetime(2026, 9, 16, tzinfo=timezone.utc)
    )
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


def test_ci_wait_recovers_candidate_created_by_finish_validation():
    work = record(
        "fc-a",
        delivery={
            "source_oid": "abc",
            "provider_handle": "provider-1",
            "ci_deadline": "2026-09-16T00:30:00Z",
            "validation": {"state": "pending", "facts": {"handle": "provider-1"}},
        },
        desktop={
            "assignment": {
                "assignment_token": "assignment-1",
                "task_id": "warden-1",
                "role": "warden",
                "state": "active",
            }
        },
    )
    ledger = MemoryLedger(work)
    service = DesktopProtocolService(
        ledger, now=lambda: datetime(2026, 9, 16, tzinfo=timezone.utc)
    )
    with patch(
        "fulcrum.delivery_service.DeliveryService.validation_show",
        return_value=CommandResult.query(
            {
                "state": "passed",
                "delivery": {
                    "source_oid": "abc",
                    "provider_handle": "provider-1",
                    "validation": {
                        "state": "passed",
                        "facts": {
                            "handle": "provider-1",
                            "validation": "passed",
                        },
                    },
                },
            }
        ),
    ):
        completed = service.wait_for_ci_results(
            mutation(
                ("ci", "wait"),
                actor="task:warden-1",
                arguments={"bead": "fc-a"},
                payload={
                    "candidate_id": "provider-1",
                    "assignment_token": "assignment-1",
                },
            )
        )
    assert completed.result["status"] == "passed"
    candidate = (ledger.show("fc-a").fc or {})["desktop"]["candidate"]
    assert candidate["candidate_id"] == "provider-1"
    assert candidate["recovered_from_delivery"] is True
    assert (ledger.show("fc-a").fc or {})["delivery"]["validation"]["state"] == "passed"


def test_ci_wait_repairs_terminal_candidate_with_stale_delivery():
    work = record(
        "fc-a",
        delivery={
            "source_oid": "abc",
            "provider_handle": "provider-1",
            "validation": {"state": "pending"},
        },
        desktop={
            "assignment": {
                "assignment_token": "assignment-1",
                "task_id": "warden-1",
                "role": "warden",
                "state": "active",
            },
            "candidate": {
                "candidate_id": "provider-1",
                "source": "abc",
                "state": "passed",
                "deadline": "2026-09-16T00:30:00Z",
            },
        },
    )
    ledger = MemoryLedger(work)
    service = DesktopProtocolService(ledger)
    with patch(
        "fulcrum.delivery_service.DeliveryService.validation_show",
        return_value=CommandResult.query(
            {
                "state": "passed",
                "delivery": {
                    "source_oid": "abc",
                    "provider_handle": "provider-1",
                    "validation": {"state": "passed"},
                },
            }
        ),
    ) as validation_show:
        result = service.wait_for_ci_results(
            mutation(
                ("ci", "wait"),
                actor="task:warden-1",
                arguments={"bead": "fc-a"},
                payload={
                    "candidate_id": "provider-1",
                    "assignment_token": "assignment-1",
                },
            )
        )
    assert result.result["status"] == "passed"
    validation_show.assert_called_once()
    assert (ledger.show("fc-a").fc or {})["delivery"]["validation"]["state"] == "passed"


def test_steward_dispatches_executor_from_retained_worktree_path():
    archived = record(
        "fc-archived",
        status="closed",
        phase="done",
        priority=0,
        desktop={
            "actions": {
                "action-archive": {
                    "action_id": "action-archive",
                    "record_id": "fc-archived",
                    "executor": "steward",
                    "tool": "set_thread_archived",
                    "arguments": {"threadId": "old-worker", "archived": True},
                    "state": "pending",
                    "purpose": "archive_task:old-worker",
                }
            }
        },
    )
    work = record(
        "fc-cdf5657c",
        phase="ready",
        requested_role="executor",
        worktree={"path": "/tmp/managed-worktree", "branch": "fc-a"},
        codex_project_id="project-1",
        models={"executor": {"model": "gpt-6-astra", "effort": "xhigh"}},
        outcome="RAW INTAKE $weaver must never reach the Executor",
        context=["```$fulcrum-warden``` raw Weaver transcript"],
        scope={
            "summary": "Add newline to README.md",
            "acceptance": ["README.md ends with a newline"],
            "evidence": ["README.md currently lacks a final newline"],
            "implementation_notes": ["Inspect README.md before editing"],
            "finish_operation": "fc-op-scope",
        },
    )
    service, _ = registered_service(archived, work)
    service.resume(mutation(("resume",), payload={"reason": "acceptance"}))
    result = service.wait_for_instructions(
        mutation(
            ("instruction", "wait"),
            actor="task:steward-1",
            payload={"loop_id": "dispatch", "turn_id": "turn-dispatch"},
        )
    )
    assert result.result["kind"] == "action"
    assert result.result["action"]["record_id"] == "fc-cdf5657c"
    arguments = result.result["action"]["arguments"]
    prompt = arguments["prompt"]
    assert prompt.startswith("Fulcrum-Action:")
    assert "/tmp/managed-worktree" not in prompt
    assert "AUTHORIZED_CONTRACT_JSON" not in prompt
    assert "Add newline to README.md" not in prompt
    assert "RAW INTAKE" not in prompt
    assert "raw Weaver transcript" not in prompt
    assert "$weaver" not in prompt
    assert "$fulcrum-executor" not in prompt
    assert "its scope is the complete authorized contract" in prompt
    assert "checks proportional to the change" in prompt
    assert "exactly one task commit" in prompt
    assert "task_id set to the exact CODEX_THREAD_ID" in prompt
    assert "session_id set to the exact CODEX_SESSION_ID" in prompt
    assert "nonempty top-level evidence" in prompt
    assert "Copy assignment.assignment_token" in prompt
    assert "never retype or reconstruct that token" in prompt
    assert arguments["title"] == "⚒️ [exe-cdf5657c] Add newline to README.md"
    assert arguments["model"] == "gpt-6-astra"
    assert arguments["thinking"] == "xhigh"
    assert "$weaver" not in role_title("executor", "fc-a", "$weaver `run`")


def test_warden_prompt_requires_finish_after_passing_ci():
    prompt = _worker_prompt(
        role="warden",
        record=record("fc-review"),
        assignment_token="assignment-review",
    )

    assert "passing wait_for_ci_results response is not completion" in prompt
    assert "derives the exact current HEAD" in prompt
    assert "do not supply or retype a source OID" in prompt
    assert "Copy candidate.candidate_id" in prompt
    assert "must be finish with outcome approved" in prompt
    assert "a nonempty top-level evidence array" in prompt
    assert "and nonempty checks" in prompt
    assert "Do not send a final answer before finish returns accepted" in prompt
    assert "already in the Fulcrum Warden task" in prompt
    assert "do not create or delegate to another task" in prompt
    assert "source_thread_id in delegation metadata identifies the Steward" in prompt
    assert "use only the exact CODEX_THREAD_ID as task_id" in prompt


def test_warden_submission_derives_source_from_assigned_worktree():
    work = record(
        "fc-review",
        desktop={
            "assignment": {
                "assignment_token": "assignment-review",
                "task_id": "warden-1",
                "role": "warden",
                "state": "active",
            }
        },
    )
    service = DesktopProtocolService(MemoryLedger(work))
    source = "a" * 40
    with (
        patch(
            "fulcrum.delivery_service.DeliveryService.worktree_inspect",
            return_value=CommandResult.query({"workspace": {"head_oid": source}}),
        ),
        patch(
            "fulcrum.delivery_service.DeliveryService.validation_start",
            return_value=CommandResult.query({"state": "pending"}),
        ) as validation_start,
    ):
        result = service.submit_candidate(
            mutation(
                ("candidate", "submit"),
                actor="task:warden-1",
                arguments={"bead": "fc-review"},
                payload={"assignment_token": "assignment-review"},
            )
        )

    submitted = validation_start.call_args.args[0]
    assert submitted.arguments == {"bead": "fc-review", "source": source}
    assert result.result["candidate"]["source"] == source


def test_released_same_task_weaver_does_not_block_executor_dispatch():
    work = record(
        "fc-entry-complete",
        phase="ready",
        requested_role="executor",
        worktree={"path": "/tmp/managed-worktree", "branch": "fc-entry-complete"},
        codex_project_id="project-1",
        scope={
            "summary": "Update README.md",
            "acceptance": ["README.md is updated"],
            "finish_operation": "fc-op-scope",
        },
        desktop={
            "assignment_history": [
                {
                    "assignment_token": "entry-assignment",
                    "task_id": "weaver-1",
                    "turn_id": "logical-protocol-turn",
                    "role": "weaver",
                    "state": "finished",
                    "capacity_class": "entry",
                    "entry_mode": "same_task",
                    "released_at": "2026-09-16T00:01:00Z",
                }
            ],
            "observations": {
                "lifecycle": {
                    "done": {
                        "type": "task_complete",
                        "task_id": "weaver-1",
                        "turn_id": "native-codex-turn",
                    }
                }
            },
        },
    )
    service, _ = registered_service(work)
    service.resume(mutation(("resume",), payload={"reason": "test"}))

    result = service.wait_for_instructions(
        mutation(
            ("instruction", "wait"),
            actor="task:steward-1",
            payload={"loop_id": "entry-finished", "turn_id": "steward-turn"},
        )
    )

    assert result.result["kind"] == "action"
    assert result.result["action"]["tool"] == "create_thread"
    assert result.result["action"]["record_id"] == "fc-entry-complete"


def test_worker_registration_derives_native_identity_from_creation_result():
    work = record(
        "fc-derived",
        owner="STEWARD",
        phase="executing",
        requested_role="executor",
        desktop={
            "assignment": {
                "assignment_token": "assignment-derived",
                "role": "executor",
                "workspace": "/tmp/managed-worktree",
                "source": None,
                "project": "toy",
                "branch": "codex/fc-derived",
                "state": "reserved",
            },
            "actions": {
                "action-derived": {
                    "action_id": "action-derived",
                    "record_id": "fc-derived",
                    "executor": "steward",
                    "tool": "create_thread",
                    "assignment_token": "assignment-derived",
                    "state": "succeeded",
                    "native_result": {
                        "threadId": "worker-derived",
                        "hostId": "local",
                    },
                }
            },
        },
    )
    service, ledger = registered_service(work)

    result = service.register_worker(
        mutation(
            ("worker", "register"),
            arguments={"bead": "fc-derived"},
            payload={
                "assignment_token": "assignment-derived",
            },
        )
    )

    assignment = result.result["assignment"]
    assert assignment["task_id"] == "worker-derived"
    assert assignment["session_id"] == "worker-derived"
    assert assignment["turn_id"] is None
    assert assignment["host_id"] == "local"
    assert assignment["workspace"] == "/tmp/managed-worktree"
    assert assignment["git_root"] == "/tmp/managed-worktree"
    assert assignment["branch"] == "codex/fc-derived"
    assert ledger.show("fc-derived").assignee == "worker-derived"


def test_steward_never_dispatches_weaver_work():
    work = record(
        "fc-no-weaver",
        phase="backlog",
        requested_role="weaver",
        workspace="/tmp/project",
        codex_project_id="project-1",
    )
    service, ledger = registered_service(work)
    service.resume(mutation(("resume",), payload={"reason": "test"}))
    result = service.wait_for_instructions(
        mutation(
            ("instruction", "wait"),
            actor="task:steward-1",
            payload={"loop_id": "no-weaver", "turn_id": "turn-no-weaver"},
        )
    )
    assert result.result["transport_wait"]["state"] == "waiting"
    retained = ledger.show("fc-no-weaver").fc or {}
    assert not (retained.get("desktop") or {}).get("assignment")
    assert not any(
        value.get("tool") == "create_thread"
        for value in ((retained.get("desktop") or {}).get("actions") or {}).values()
    )


def test_stale_persisted_weaver_creation_is_superseded_before_claim():
    work = record(
        "fc-stale-weaver",
        owner="STEWARD",
        phase="backlog",
        requested_role="weaver",
        desktop={
            "assignment": {
                "assignment_token": "assignment-weaver",
                "role": "weaver",
                "state": "reserved",
            }
        },
    )
    service, ledger = registered_service(work)
    service.resume(mutation(("resume",), payload={"reason": "test"}))
    action = seed_action(
        ledger,
        {
            "record_id": "fc-stale-weaver",
            "executor": "steward",
            "tool": "create_thread",
            "arguments": {"prompt": "$weaver"},
            "assignment_token": "assignment-weaver",
            "purpose": "routine_dispatch",
        },
    )
    with unittest.TestCase().assertRaises(FulcrumError) as raised:
        service.claim_action(
            mutation(
                ("action", "claim"),
                actor="task:steward-1",
                arguments={
                    "record_id": "fc-stale-weaver",
                    "action_id": action["action_id"],
                },
                payload={"attempt_id": "attempt-stale-weaver"},
            )
        )
    assert raised.exception.code == "ACTION_SUPERSEDED"
    retained = (ledger.show("fc-stale-weaver").fc or {})["desktop"]["actions"]
    assert retained[action["action_id"]]["state"] == "superseded"


def test_stale_persisted_weaver_assignment_is_retired_and_archived():
    work = record(
        "fc-stale-weaver",
        owner="STEWARD",
        phase="backlog",
        requested_role="weaver",
        role="weaver",
        ownership_operation="assignment-weaver",
        desktop={
            "assignment": {
                "assignment_token": "assignment-weaver",
                "task_id": "worker-weaver",
                "role": "weaver",
                "state": "issuing",
                "capacity_class": "ordinary",
            }
        },
    )
    service, ledger = registered_service(work)
    service.resume(mutation(("resume",), payload={"reason": "test"}))
    result = service.wait_for_instructions(
        mutation(
            ("instruction", "wait"),
            actor="task:steward-1",
            payload={"loop_id": "retire-weaver", "turn_id": "turn-retire-weaver"},
        )
    )

    assert result.result["kind"] == "action"
    action = result.result["action"]
    assert action["tool"] == "set_thread_archived"
    assert action["arguments"] == {"threadId": "worker-weaver", "archived": True}
    assert action["reporting"]["forbidden_weaver_migration"] is True

    retained = ledger.show("fc-stale-weaver")
    assert retained.assignee == "HUMAN"
    fc = retained.fc or {}
    assert fc["owner"] == "HUMAN"
    assert fc["role"] is None
    assert fc["ownership_operation"] is None
    assert fc["requested_role"] == "executor"
    assert "$weaver" not in fc["next_action"]
    desktop = fc["desktop"]
    assert "assignment" not in desktop
    assert desktop["assignment_history"][-1]["state"] == "retired_forbidden_role"
    assert desktop["role_migrations"][-1] == {
        "role": "weaver",
        "task_id": "worker-weaver",
        "assignment_token": "assignment-weaver",
        "state": "retired",
        "capacity_released": True,
        "recorded_at": desktop["role_migrations"][-1]["recorded_at"],
    }


def test_same_task_weaver_assignment_is_preserved():
    work = record(
        "fc-same-task-weaver",
        owner="human-task",
        phase="working",
        requested_role="executor",
        role="weaver",
        ownership_operation="assignment-weaver",
        desktop={
            "assignment": {
                "assignment_token": "assignment-weaver",
                "task_id": "human-task",
                "role": "weaver",
                "state": "active",
                "capacity_class": "entry",
                "entry_mode": "same_task",
            }
        },
    )
    service, ledger = registered_service(work)
    service.resume(mutation(("resume",), payload={"reason": "test"}))
    result = service.wait_for_instructions(
        mutation(
            ("instruction", "wait"),
            actor="task:steward-1",
            payload={"loop_id": "preserve-weaver", "turn_id": "turn-preserve"},
        )
    )

    assert result.result["transport_wait"]["state"] == "waiting"
    retained = ledger.show("fc-same-task-weaver")
    assert retained.assignee == "human-task"
    assert retained.fc["desktop"]["assignment"] == work.fc["desktop"]["assignment"]
    assert "assignment_history" not in retained.fc["desktop"]


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

    assert settle_native_completion(
        replace(request(), input={"turn_id": "steward-wait-turn"}), ledger, "fc-a"
    )
    settled = ledger.show("fc-a")
    settled_desktop = (settled.fc or {})["desktop"]
    assert "assignment" not in settled_desktop
    assert settled_desktop["assignment_history"][-1]["state"] == "finished"
    assert settled_desktop["assignment_history"][-1]["turn_id"] == "turn-1"


def test_assignment_history_uses_completing_native_turn():
    ledger = MemoryLedger(
        record(
            "fc-a",
            owner="warden-1",
            phase="ready",
            desktop={
                "assignment": {
                    "assignment_token": "assignment-1",
                    "task_id": "warden-1",
                    "turn_id": "registration-placeholder",
                    "role": "warden",
                    "state": "active",
                    "finish_operation": "fc-op-finish",
                },
                "observations": {
                    "lifecycle": {
                        "done": {
                            "type": "task_complete",
                            "task_id": "warden-1",
                            "turn_id": "turn-real",
                        }
                    }
                },
            },
        )
    )
    assert not settle_native_completion(
        replace(request(), input={"turn_id": "turn-real"}), ledger, "fc-a"
    )
    assert "assignment_history" not in (ledger.show("fc-a").fc or {})["desktop"]


def test_legacy_creation_action_turn_placeholder_settles_from_task_completion():
    ledger = MemoryLedger(
        record(
            "fc-a",
            owner="warden-1",
            phase="awaiting_native_completion",
            desktop={
                "assignment": {
                    "assignment_token": "assignment-1",
                    "task_id": "warden-1",
                    "turn_id": "action-create",
                    "creation_action_id": "action-create",
                    "role": "warden",
                    "state": "active",
                    "finish_operation": "fc-op-finish",
                },
                "observations": {
                    "lifecycle": {
                        "done": {
                            "type": "task_complete",
                            "task_id": "warden-1",
                            "turn_id": "turn-real",
                        }
                    }
                },
            },
        )
    )

    assert settle_native_completion(request(), ledger, "fc-a")
    assert "assignment" not in (ledger.show("fc-a").fc or {})["desktop"]


def test_recovery_slot_releases_after_finish_and_native_completion():
    recovery_id = "recovery-1"
    ledger = MemoryLedger(
        record(
            "fc-system",
            kind="control",
            desktop={
                "recovery_slot": {
                    "recovery_id": recovery_id,
                    "bead": "fc-a",
                    "state": "active",
                }
            },
        ),
        record(
            "fc-a",
            owner="justiciar-1",
            phase="done",
            desktop={
                "assignment": {
                    "assignment_token": "assignment-1",
                    "task_id": "justiciar-1",
                    "turn_id": "turn-1",
                    "role": "justiciar",
                    "state": "active",
                    "capacity_class": "recovery",
                    "recovery_id": recovery_id,
                    "finish_operation": "fc-op-finish",
                },
                "observations": {
                    "lifecycle": {
                        "done": {
                            "type": "turn_completed",
                            "task_id": "justiciar-1",
                            "turn_id": "turn-1",
                        }
                    }
                },
            },
        ),
    )
    assert settle_native_completion(request(), ledger, "fc-a")
    slot = (ledger.show("fc-system").fc or {})["desktop"]["recovery_slot"]
    assert slot["state"] == "released"
    assert slot["release_reason"] == "accepted outcome and native completion"


def test_transition_limit_fences_new_mutation():
    retained = {
        str(uuid.uuid4()): {"state": "completed", "result": {}} for _ in range(128)
    }
    ledger = MemoryLedger(
        record(
            "fc-system",
            kind="control",
            desktop={"run_control": "running", "requests": retained},
        )
    )
    service = DesktopProtocolService(ledger)
    with unittest.TestCase().assertRaises(FulcrumError) as raised:
        service.pause(mutation(("pause",), payload={"reason": "test"}))
    assert raised.exception.code == "TRANSITION_STORAGE_BLOCKED"


def test_projected_request_journal_prunes_locally_and_replays_durably():
    ledger = MemoryLedger(
        record(
            "fc-system",
            kind="control",
            desktop={"run_control": "running", "requests": {}},
        )
    )
    service = DesktopProtocolService(ledger)
    first = mutation(("pause",), payload={"reason": "cycle-0"})
    expected = service.pause(first)
    for index in range(1, 140):
        service.pause(mutation(("pause",), payload={"reason": f"cycle-{index}"}))
    protocol = (ledger.show("fc-system").fc or {})["desktop"]
    assert len(protocol["requests"]) <= 33
    assert first.request_id not in protocol["requests"]
    replayed = service.pause(first)
    assert replayed.result == expected.result


def test_steward_repeats_unclaimed_grant_on_next_wait():
    service, ledger = registered_service()
    service.resume(mutation(("resume",), payload={"reason": "test"}))
    seed_action(
        ledger,
        {
            "record_id": "fc-system",
            "executor": "steward",
            "tool": "send_message_to_thread",
            "arguments": {"threadId": "worker-1", "prompt": "continue"},
        },
    )
    first = service.wait_for_instructions(
        mutation(
            ("instruction", "wait"),
            actor="task:steward-1",
            payload={"loop_id": "loop-1", "turn_id": "turn-1"},
        )
    )
    repeated = service.wait_for_instructions(
        mutation(
            ("instruction", "wait"),
            actor="task:steward-1",
            payload={"loop_id": "loop-2", "turn_id": "turn-2"},
        )
    )
    assert repeated.result["kind"] == "action"
    assert repeated.result["action"]["action_id"] == first.result["action"]["action_id"]


def test_create_result_requires_native_identity_evidence():
    service, ledger = registered_service()
    service.resume(mutation(("resume",), payload={"reason": "test"}))
    action = seed_action(
        ledger,
        {
            "record_id": "fc-system",
            "executor": "steward",
            "tool": "create_thread",
            "arguments": {"prompt": "work"},
        },
    )
    service.claim_action(
        mutation(
            ("action", "claim"),
            actor="task:steward-1",
            arguments={"record_id": "fc-system", "action_id": action["action_id"]},
            payload={"attempt_id": "attempt-invalid-result"},
        )
    )
    with unittest.TestCase().assertRaises(FulcrumError) as raised:
        service.report_action_result(
            mutation(
                ("action", "result"),
                actor="task:steward-1",
                arguments={
                    "record_id": "fc-system",
                    "action_id": action["action_id"],
                },
                payload={
                    "attempt_id": "attempt-invalid-result",
                    "outcome": "succeeded",
                    "native_result": {},
                },
            )
        )
    assert raised.exception.code == "RESULT_EVIDENCE_REQUIRED"


def test_dispatch_blocks_overlapping_active_work():
    active = record(
        "fc-active",
        project="toy",
        overlap_tags=["repository"],
        desktop={
            "assignment": {
                "assignment_token": "active-token",
                "state": "active",
                "capacity_class": "ordinary",
            }
        },
    )
    candidate = record(
        "fc-candidate",
        phase="ready",
        requested_role="executor",
        project="toy",
        overlap_tags=["repository"],
        workspace="/tmp/worktree",
        codex_project_id="project-1",
    )
    service, _ = registered_service(active, candidate)
    service.resume(mutation(("resume",), payload={"reason": "test"}))
    result = service.wait_for_instructions(
        mutation(
            ("instruction", "wait"),
            actor="task:steward-1",
            payload={"loop_id": "overlap", "turn_id": "turn-overlap"},
        )
    )
    assert result.state.value == "running"
    assert result.result["transport_wait"]["kind"] == "instruction"


def test_dispatch_blocks_disabled_project():
    candidate = record(
        "fc-candidate",
        phase="ready",
        requested_role="executor",
        project="toy",
        workspace="/tmp/worktree",
        codex_project_id="project-1",
    )
    service, _ = registered_service(candidate)
    service.resume(mutation(("resume",), payload={"reason": "test"}))
    config = {
        "policy": {
            "automatic_capacity": 4,
            "default_project_capacity": 4,
            "project_capacity": {},
            "paused_projects": [],
        },
        "models": {},
        "projects": {"toy": {"enabled": False}},
    }
    with patch("fulcrum.desktop_protocol.ConfigurationManager") as manager:
        manager.return_value.load.return_value = ({}, None)
        manager.return_value.effective.return_value = config
        result = service.wait_for_instructions(
            mutation(
                ("instruction", "wait"),
                actor="task:steward-1",
                payload={"loop_id": "disabled", "turn_id": "turn-disabled"},
            )
        )
    assert result.state.value == "running"


def test_claim_supersedes_dispatch_when_admission_is_paused():
    work = record(
        "fc-a",
        phase="ready",
        project="toy",
        desktop={
            "assignment": {
                "assignment_token": "assignment-1",
                "state": "reserved",
            }
        },
    )
    service, ledger = registered_service(work)
    action = seed_action(
        ledger,
        {
            "record_id": "fc-a",
            "executor": "steward",
            "tool": "create_thread",
            "arguments": {"prompt": "work"},
            "assignment_token": "assignment-1",
            "purpose": "routine_dispatch",
        },
    )
    with unittest.TestCase().assertRaises(FulcrumError) as raised:
        service.claim_action(
            mutation(
                ("action", "claim"),
                actor="task:steward-1",
                arguments={"record_id": "fc-a", "action_id": action["action_id"]},
                payload={"attempt_id": "attempt-1"},
            )
        )
    assert raised.exception.code == "ACTION_SUPERSEDED"
    retained = (ledger.show("fc-a").fc or {})["desktop"]["actions"]
    assert retained[action["action_id"]]["state"] == "superseded"


class DesktopProtocolTests(unittest.TestCase):
    def test_registration_requires_trusted_handshake(self):
        test_registration_requires_and_consumes_trusted_prompt_handshake()

    def test_registration_accepts_matching_creation_result(self):
        test_registration_accepts_matching_succeeded_creation_result()

    def test_managed_task_cannot_inject_action(self):
        test_production_service_has_no_action_injection_api()

    def test_creation_normalizes_title(self):
        test_successful_creation_does_not_schedule_unobserved_title_normalization()

    def test_observed_title_mismatch_is_corrected(self):
        test_observed_title_mismatch_schedules_one_correction()

    def test_settled_worker_archives(self):
        test_closed_settled_worker_archives_only_after_ten_minutes()

    def test_equal_action_claim_is_idempotent(self):
        test_equal_action_claim_replays_without_authorizing_second_invocation()

    def test_steward_selects_without_marshal(self):
        test_steward_selects_ready_action_without_marshal_and_pause_holds_it()

    def test_ci_wait_is_transport_state(self):
        test_ci_wait_is_transport_state_and_keeps_assignment_active()

    def test_ci_wait_recovers_finish_candidate(self):
        test_ci_wait_recovers_candidate_created_by_finish_validation()

    def test_ci_wait_repairs_stale_delivery(self):
        test_ci_wait_repairs_terminal_candidate_with_stale_delivery()

    def test_dispatch_uses_retained_worktree(self):
        test_steward_dispatches_executor_from_retained_worktree_path()

    def test_weaver_is_never_dispatched(self):
        test_steward_never_dispatches_weaver_work()

    def test_stale_weaver_creation_is_rejected(self):
        test_stale_persisted_weaver_creation_is_superseded_before_claim()

    def test_stale_weaver_assignment_is_retired(self):
        test_stale_persisted_weaver_assignment_is_retired_and_archived()

    def test_same_task_weaver_assignment_is_preserved(self):
        test_same_task_weaver_assignment_is_preserved()

    def test_native_completion_releases_assignment(self):
        test_assignment_releases_only_after_exact_native_completion()

    def test_native_completion_rejects_another_turn(self):
        test_assignment_history_uses_completing_native_turn()

    def test_native_completion_releases_recovery_slot(self):
        test_recovery_slot_releases_after_finish_and_native_completion()

    def test_transition_limit_blocks_new_mutation(self):
        test_transition_limit_fences_new_mutation()

    def test_projected_journal_prunes_and_replays(self):
        test_projected_request_journal_prunes_locally_and_replays_durably()

    def test_steward_repeats_unclaimed_grant(self):
        test_steward_repeats_unclaimed_grant_on_next_wait()

    def test_create_result_requires_identity(self):
        test_create_result_requires_native_identity_evidence()

    def test_dispatch_blocks_overlapping_work(self):
        test_dispatch_blocks_overlapping_active_work()

    def test_dispatch_blocks_disabled_project(self):
        test_dispatch_blocks_disabled_project()

    def test_claim_revalidates_pause(self):
        test_claim_supersedes_dispatch_when_admission_is_paused()

    def test_message_result_validates_target(self):
        test_message_success_requires_matching_target_identity()

    def test_claim_revalidates_dependencies(self):
        test_claim_revalidates_dependency_after_reservation()
