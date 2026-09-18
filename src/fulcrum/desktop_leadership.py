"""Marshal recovery, repair limits, and Vizier decision protocol."""

from __future__ import annotations

import copy
import json
import socket
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from fulcrum.configuration import ConfigurationManager
from fulcrum.contracts import CommandResult, FulcrumError, ParsedRequest
from fulcrum.coordination import coordinated, external_effect
from fulcrum.desktop_protocol import (
    DesktopProtocolService,
    _opaque,
    _protocol,
    _request_input,
    _result,
    _saved_request,
    _save_request,
    _utc_now,
    _with_protocol,
    action_marker,
    require_run_control,
    _positive_native_completion,
)


def _active_broker_wait_ids(request: ParsedRequest) -> set[str]:
    path = request.instance.instance_root / "broker.sock"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.25)
            client.connect(str(path))
            client.sendall(b'{"type":"health"}\n')
            raw = client.makefile("rb").readline(1024 * 1024)
        response = json.loads(raw)
    except (OSError, ValueError, json.JSONDecodeError):
        return set()
    pending = response.get("pending") if isinstance(response, Mapping) else None
    if not isinstance(pending, list):
        return set()
    return {
        str(item["wait_id"])
        for item in pending
        if isinstance(item, Mapping) and item.get("wait_id")
    }


def _current_native_turn(protocol: Mapping[str, Any], task_id: Any) -> str | None:
    """Return the newest observed nonterminal native turn for one task."""

    observations = protocol.get("observations")
    lifecycle = (
        observations.get("lifecycle") if isinstance(observations, Mapping) else None
    )
    if (
        not isinstance(task_id, str)
        or not task_id
        or not isinstance(lifecycle, Mapping)
    ):
        return None
    terminal_kinds = {
        "task_complete",
        "task_completed",
        "turn_complete",
        "turn_completed",
    }
    terminal = {
        str(event.get("turn_id"))
        for event in lifecycle.values()
        if isinstance(event, Mapping)
        and event.get("task_id") == task_id
        and event.get("type") in terminal_kinds
        and event.get("turn_id")
    }
    candidates = [
        event
        for event in lifecycle.values()
        if isinstance(event, Mapping)
        and event.get("task_id") == task_id
        and event.get("type") in {"task_started", "turn_context"}
        and isinstance(event.get("turn_id"), str)
        and event.get("turn_id") not in terminal
    ]
    if not candidates:
        return None
    newest = max(candidates, key=lambda event: str(event.get("time") or ""))
    return str(newest["turn_id"])


def _marshal_turn_id(
    protocol: Mapping[str, Any], binding: Mapping[str, Any], request: ParsedRequest
) -> str:
    supplied = request.input.get("turn_id")
    if supplied is not None and (not isinstance(supplied, str) or not supplied):
        raise FulcrumError.invalid(
            "IDENTITY_REQUIRED", "Marshal turn_id must be a nonempty string"
        )
    observed = _current_native_turn(protocol, binding.get("task_id"))
    if isinstance(supplied, str):
        if observed is not None and supplied != observed:
            raise FulcrumError(
                "STALE_DECISION",
                "Marshal turn_id does not match the active native turn",
                exit_code=5,
            )
        return supplied
    if observed is not None:
        return observed
    if request.input.get("trigger") == "heartbeat":
        return f"heartbeat:{request.request_id or _opaque('turn')}"
    raise FulcrumError(
        "IDENTITY_REQUIRED",
        "current Marshal native turn has not been observed yet",
        exit_code=5,
    )


class DesktopLeadershipService(DesktopProtocolService):
    @coordinated
    def marshal_check(self, request: ParsedRequest) -> CommandResult:
        ledger = self._ledger(request)
        binding = self._standing_actor(ledger, "marshal", request)
        system = self._system(ledger)
        protocol = _protocol(system.fc or {})
        saved = _saved_request(protocol, request, ledger=ledger)
        if saved:
            from fulcrum.desktop_protocol import _replay

            return _replay(saved, request)
        turn_id = _marshal_turn_id(protocol, binding, request)
        if request.input.get("trigger") == "heartbeat":
            schedule = protocol.get("marshal_schedule")
            if (
                not isinstance(schedule, Mapping)
                or schedule.get("state") != "succeeded"
                or schedule.get("status") != "ACTIVE"
                or schedule.get("target_task_id") != binding.get("task_id")
            ):
                raise FulcrumError(
                    "HEARTBEAT_SCHEDULE_MISMATCH",
                    "scheduled Marshal delivery does not match the active retained heartbeat",
                    exit_code=5,
                )
            delivered_at = _utc_now()
            deliveries = list(schedule.get("deliveries") or [])
            deliveries.append(
                {
                    "delivered_at": delivered_at,
                    "task_id": binding.get("task_id"),
                    "turn_id": turn_id,
                }
            )
            protocol["marshal_schedule"] = {
                **dict(schedule),
                "last_delivery_at": delivered_at,
                "last_delivery_turn_id": turn_id,
                "delivery_count": int(schedule.get("delivery_count") or 0) + 1,
                "deliveries": deliveries[-20:],
            }
        current = protocol.get("marshal_decision")
        if isinstance(current, Mapping) and current.get("state") == "active":
            same_turn = turn_id == current.get("turn_id")
            accepted_input = current.get("accepted_input")
            accepted_payload = (
                accepted_input.get("input")
                if isinstance(accepted_input, Mapping)
                else None
            )
            serialized_heartbeat = (
                request.input.get("trigger") == "heartbeat"
                and isinstance(accepted_payload, Mapping)
                and accepted_payload.get("trigger") == "heartbeat"
            )
            if (
                not same_turn
                and not serialized_heartbeat
                and not _positive_native_completion(protocol, current)
            ):
                value = {
                    "decision": copy.deepcopy(dict(current)),
                    "joined": False,
                    "deferred": True,
                    "brief": {
                        "reason": "the prior Marshal turn is not positively complete",
                        "incidents": [],
                        "ready": [],
                        "omitted": {},
                    },
                }
                _save_request(protocol, request, value, ledger=ledger)
                ledger.update_fc(system.id, _with_protocol(system.fc or {}, protocol))
                return _result(request, value)
            if not same_turn:
                history = list(protocol.get("marshal_decision_history") or [])
                history.append(
                    {
                        **copy.deepcopy(dict(current)),
                        "state": "superseded",
                        "superseded_at": _utc_now(),
                        "superseded_reason": (
                            "next_serialized_heartbeat"
                            if serialized_heartbeat
                            else "native_turn_completed_without_decision"
                        ),
                    }
                )
                protocol["marshal_decision_history"] = history[-20:]
            else:
                value = {
                    "decision": copy.deepcopy(dict(current)),
                    "joined": True,
                    "brief": copy.deepcopy(
                        current.get("brief")
                        if isinstance(current.get("brief"), Mapping)
                        else {"incidents": [], "ready": [], "omitted": {}}
                    ),
                }
                _save_request(protocol, request, value, ledger=ledger)
                ledger.update_fc(system.id, _with_protocol(system.fc or {}, protocol))
                return _result(request, value)
        records = ledger.list_records(limit=0)
        incidents: list[dict[str, Any]] = []
        recoveries: list[dict[str, Any]] = []
        ready: list[dict[str, Any]] = []
        for record in records:
            fc = record.fc or {}
            desktop = _protocol(fc)
            assignment = desktop.get("assignment")
            ci_waiting = any(
                isinstance(wait, Mapping) and wait.get("state") == "waiting"
                for wait in (desktop.get("ci_waits") or {}).values()
            )
            if (
                isinstance(assignment, Mapping)
                and assignment.get("state") == "active"
                and not ci_waiting
            ):
                last = fc.get("last_progress_at") or assignment.get("registered_at")
                try:
                    observed = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                except (TypeError, ValueError):
                    observed = self.now() - timedelta(days=1)
                if observed.tzinfo is None:
                    observed = observed.replace(tzinfo=timezone.utc)
                if self.now() - observed >= timedelta(minutes=30):
                    retained = dict(desktop.get("incidents") or {})
                    incident = dict(retained.get("missing-progress") or {})
                    incident.update(
                        {
                            "incident_id": incident.get("incident_id")
                            or _opaque("incident"),
                            "incident_key": "missing-progress",
                            "state": "open",
                            "scope": "registered worker progress",
                            "required_decision": "Inspect the exact native task before recovery.",
                            "evidence": [
                                {
                                    "task_id": assignment.get("task_id"),
                                    "turn_id": assignment.get("turn_id"),
                                    "last_progress_at": last,
                                }
                            ],
                            "updated_at": _utc_now(),
                            "repair_cycles": int(incident.get("repair_cycles", 0)),
                            "justiciar_interventions": int(
                                incident.get("justiciar_interventions", 0)
                            ),
                        }
                    )
                    retained["missing-progress"] = incident
                    desktop["incidents"] = retained
                    ledger.update_fc(
                        record.id, _with_protocol(record.fc or {}, desktop)
                    )
            for incident in (desktop.get("incidents") or {}).values():
                if (
                    isinstance(incident, Mapping)
                    and incident.get("state") != "resolved"
                ):
                    incidents.append({"bead": record.id, **dict(incident)})
                    action_id = incident.get("action_id")
                    action = (desktop.get("actions") or {}).get(action_id)
                    if str(incident.get("incident_key") or "").startswith(
                        "native-action:"
                    ) and isinstance(action, Mapping):
                        assignment = desktop.get("assignment")
                        recoveries.append(
                            {
                                "bead": record.id,
                                "incident_id": incident.get("incident_id"),
                                "action_id": action_id,
                                "action_state": action.get("state"),
                                "attempts": len(action.get("attempts") or []),
                                "expected_result": copy.deepcopy(
                                    action.get("expected_result") or {}
                                ),
                                "possible_task_locator": copy.deepcopy(
                                    action.get("possible_task_locator")
                                ),
                                "provider_truth": copy.deepcopy(
                                    action.get("provider_truth")
                                ),
                                "assignment": (
                                    {
                                        "state": assignment.get("state"),
                                        "task_id": assignment.get("task_id"),
                                        "assignment_token": assignment.get(
                                            "assignment_token"
                                        ),
                                    }
                                    if isinstance(assignment, Mapping)
                                    else None
                                ),
                                "allowed_decisions": [
                                    "adopt_observed_task",
                                    "retry_same_action",
                                    "leave_uncertain",
                                ],
                            }
                        )
            if (
                record.status != "closed"
                and fc.get("owner") == binding["task_id"]
                and fc.get("phase") in {"ready", "implementation_ready"}
            ):
                ready.append(
                    {
                        "bead": record.id,
                        "priority": fc.get("priority", 2),
                        "holds": fc.get("holds") or [],
                    }
                )
        waits = protocol.get("instruction_waits") or {}
        with external_effect():
            active_wait_ids = _active_broker_wait_ids(request)
        healthy_wait = any(
            isinstance(value, Mapping)
            and value.get("state") == "waiting"
            and value.get("wait_id") in active_wait_ids
            for value in waits.values()
        )
        steward = (protocol.get("standing") or {}).get("steward")
        steward_health = (
            "healthy_wait"
            if healthy_wait
            else (
                "stopped"
                if isinstance(steward, Mapping)
                and steward.get("state")
                in {"stopped", "stop_observed", "interrupt_observed"}
                else "unknown"
            )
        )
        decision = {
            "decision_id": _opaque("decision"),
            "state": "active",
            "task_id": binding["task_id"],
            "turn_id": turn_id,
            "accepted_input": {
                **_request_input(request),
                "input": {**dict(request.input), "turn_id": turn_id},
            },
            "started_at": _utc_now(),
        }
        protocol["marshal_decision"] = decision
        recovery_action: dict[str, Any] | None = None
        actions = dict(protocol.get("actions") or {})
        unsettled = any(
            isinstance(action, Mapping)
            and action.get("state") in {"issuing", "uncertain"}
            for item in records
            for action in (
                (_protocol(item.fc or {}).get("actions") or {}).values()
                if isinstance(_protocol(item.fc or {}).get("actions"), Mapping)
                else []
            )
        )
        if steward_health == "stopped" and recoveries:
            current_recovery = recoveries[0]
            recovery_record = ledger.show(str(current_recovery["bead"]))
            if recovery_record is None:
                raise FulcrumError(
                    "RECOVERY_RECORD_MISSING",
                    "the bounded recovery record disappeared during Marshal check",
                    exit_code=5,
                )
            recovery_protocol = _protocol(recovery_record.fc or {})
            original = (recovery_protocol.get("actions") or {}).get(
                current_recovery["action_id"]
            )
            provider_truth = (
                original.get("provider_truth")
                if isinstance(original, Mapping)
                else None
            )
            if (
                isinstance(original, Mapping)
                and original.get("state") == "uncertain"
                and not (
                    isinstance(provider_truth, Mapping)
                    and provider_truth.get("state") == "definitely_not_created"
                )
            ):
                purpose = f"reconcile_action:{original['action_id']}"
                existing = next(
                    (
                        item
                        for item in (recovery_protocol.get("actions") or {}).values()
                        if isinstance(item, Mapping)
                        and item.get("purpose") == purpose
                        and item.get("state") == "pending"
                    ),
                    None,
                )
                if isinstance(existing, Mapping):
                    recovery_action = dict(existing)
                else:
                    locator = original.get("possible_task_locator")
                    task_id = (
                        locator.get("threadId")
                        if isinstance(locator, Mapping)
                        else None
                    )
                    if task_id:
                        recovery_action = self._append_action(
                            recovery_protocol,
                            record_id=recovery_record.id,
                            executor="marshal",
                            tool="read_thread",
                            arguments={"threadId": task_id},
                            purpose=purpose,
                            expected_result={"threadId": task_id},
                            reporting={"reconciles": original.get("action_id")},
                        )
                    else:
                        recovery_action = self._append_action(
                            recovery_protocol,
                            record_id=recovery_record.id,
                            executor="marshal",
                            tool="list_threads",
                            arguments={"limit": 100},
                            purpose=purpose,
                            expected_result={"action_id": original.get("action_id")},
                            reporting={"reconciles": original.get("action_id")},
                        )
                    ledger.update_fc(
                        recovery_record.id,
                        _with_protocol(recovery_record.fc or {}, recovery_protocol),
                    )
        if (
            steward_health == "stopped"
            and isinstance(steward, Mapping)
            and not healthy_wait
            and not unsettled
            and not recoveries
        ):
            existing = next(
                (
                    action
                    for action in actions.values()
                    if isinstance(action, Mapping)
                    and action.get("purpose") == "recover_steward_loop"
                    and action.get("state") not in {"rejected", "superseded"}
                ),
                None,
            )
            if isinstance(existing, Mapping):
                recovery_action = dict(existing)
            else:
                action_id = _opaque("action")
                recovery_action = {
                    "action_id": action_id,
                    "record_id": system.id,
                    "executor": "marshal",
                    "tool": "send_message_to_thread",
                    "arguments": {
                        "threadId": steward.get("task_id"),
                        "prompt": (
                            "Register as the existing Steward, reconcile retained "
                            "instruction/action state, then call wait_for_instructions."
                        ),
                    },
                    "expected_result": {"thread_id": steward.get("task_id")},
                    "reporting": {"purpose": "same_steward_resumption"},
                    "state": "pending",
                    "attempts": [],
                    "created_at": _utc_now(),
                    "purpose": "recover_steward_loop",
                }
                actions[action_id] = recovery_action
                protocol["actions"] = actions
        brief = {
            "purpose": "recovery" if incidents else "curation",
            "steward_health": steward_health,
            "incidents": incidents[:20],
            "recoveries": recoveries[:20],
            "ready": sorted(ready, key=lambda row: (row["priority"], row["bead"]))[:20],
            "omitted": {
                "incidents": max(0, len(incidents) - 20),
                "recoveries": max(0, len(recoveries) - 20),
                "ready": max(0, len(ready) - 20),
            },
            "recovery_action": (
                self._action_response(request, recovery_action)
                if recovery_action is not None
                else None
            ),
        }
        decision["brief"] = copy.deepcopy(brief)
        protocol["marshal_decision"] = decision
        value = {
            "decision": decision,
            "joined": False,
            "brief": brief,
        }
        _save_request(protocol, request, value, ledger=ledger)
        ledger.update_fc(system.id, _with_protocol(system.fc or {}, protocol))
        self._write_event(
            request,
            "marshal_check_started",
            task_id=binding["task_id"],
            operation_id=decision["decision_id"],
            outcome="active",
        )
        return _result(request, value)

    @coordinated
    def marshal_decide(self, request: ParsedRequest) -> CommandResult:
        ledger = self._ledger(request)
        binding = self._standing_actor(ledger, "marshal", request)
        system = self._system(ledger)
        system_protocol = _protocol(system.fc or {})
        saved = _saved_request(system_protocol, request, ledger=ledger)
        if saved:
            from fulcrum.desktop_protocol import _replay

            return _replay(saved, request)
        turn_id = _marshal_turn_id(system_protocol, binding, request)
        operation = system_protocol.get("marshal_decision")
        if not isinstance(operation, Mapping) or operation.get("state") != "active":
            raise FulcrumError(
                "DECISION_REQUIRED",
                "Marshal has no active decision operation",
                exit_code=5,
            )
        if request.input.get("decision_id") != operation.get("decision_id"):
            raise FulcrumError(
                "STALE_DECISION", "decision operation changed", exit_code=5
            )
        if turn_id != operation.get("turn_id"):
            raise FulcrumError(
                "STALE_DECISION",
                "decision belongs to another Marshal turn",
                exit_code=5,
            )
        decisions = request.input.get("decisions")
        if not isinstance(decisions, list):
            raise FulcrumError.invalid(
                "INVALID_DECISIONS", "decisions must be an array"
            )
        recovery_rows = request.input.get("recoveries")
        if not isinstance(recovery_rows, list):
            raise FulcrumError.invalid(
                "INVALID_RECOVERIES", "recoveries must be an array"
            )
        accepted: list[dict[str, Any]] = []
        stale: list[dict[str, Any]] = []
        allowed = {"priority", "holds", "dependencies", "disposition", "owner", "phase"}
        for row in decisions:
            if not isinstance(row, Mapping):
                continue
            bead = str(row.get("bead") or "")
            record = ledger.show(bead)
            if record is None:
                stale.append({"bead": bead, "reason": "missing"})
                continue
            fc = dict(record.fc or {})
            expected = row.get("expected")
            changes = row.get("changes")
            if not isinstance(expected, Mapping) or not isinstance(changes, Mapping):
                stale.append({"bead": bead, "reason": "invalid_targeted_change"})
                continue
            mismatch = {
                key: {"expected": value, "actual": fc.get(key)}
                for key, value in expected.items()
                if fc.get(key) != value
            }
            unknown = set(changes).difference(allowed)
            if mismatch or unknown:
                stale.append(
                    {
                        "bead": bead,
                        "reason": "stale" if mismatch else "unsupported_fields",
                        "facts": mismatch,
                        "fields": sorted(unknown),
                    }
                )
                continue
            dependency_change = changes.get("dependencies")
            if dependency_change is not None:
                if not isinstance(dependency_change, list) or not all(
                    isinstance(value, str) and value for value in dependency_change
                ):
                    stale.append({"bead": bead, "reason": "invalid_dependencies"})
                    continue
                from fulcrum.work import _reconcile_dependencies, _reject_cycle

                dependency_ids = sorted(set(dependency_change))
                if bead in dependency_ids or any(
                    ledger.show(value) is None for value in dependency_ids
                ):
                    stale.append({"bead": bead, "reason": "invalid_dependencies"})
                    continue
                _reject_cycle(ledger, bead, set(dependency_ids))
                _reconcile_dependencies(ledger, bead, dependency_ids)
            retained_changes = {
                key: value for key, value in changes.items() if key != "dependencies"
            }
            updated = {**fc, **copy.deepcopy(retained_changes)}
            ledger.update_fc(
                bead,
                updated,
                assignee=(
                    str(retained_changes["owner"])
                    if "owner" in retained_changes
                    else None
                ),
                priority=(
                    int(retained_changes["priority"])
                    if "priority" in retained_changes
                    else None
                ),
            )
            accepted.append({"bead": bead, "changes": dict(changes)})
        accepted_recoveries: list[dict[str, Any]] = []
        stale_recoveries: list[dict[str, Any]] = []
        for row in recovery_rows:
            if not isinstance(row, Mapping):
                continue
            bead = str(row.get("bead") or "")
            action_id = str(row.get("action_id") or "")
            recovery_decision = str(row.get("decision") or "")
            record = ledger.show(bead)
            if record is None:
                stale_recoveries.append(
                    {"bead": bead, "action_id": action_id, "reason": "missing"}
                )
                continue
            desktop = _protocol(record.fc or {})
            actions = dict(desktop.get("actions") or {})
            action_value = actions.get(action_id)
            if not isinstance(action_value, Mapping):
                stale_recoveries.append(
                    {
                        "bead": bead,
                        "action_id": action_id,
                        "reason": "action_missing",
                    }
                )
                continue
            action = dict(action_value)
            expected_state = row.get("expected_state")
            if expected_state != action.get("state"):
                stale_recoveries.append(
                    {
                        "bead": bead,
                        "action_id": action_id,
                        "reason": "stale",
                        "expected_state": expected_state,
                        "actual_state": action.get("state"),
                    }
                )
                continue
            incident_key = f"native-action:{action_id}"
            incidents = dict(desktop.get("incidents") or {})
            incident_value = incidents.get(incident_key)
            if (
                not isinstance(incident_value, Mapping)
                or incident_value.get("state") == "resolved"
            ):
                stale_recoveries.append(
                    {
                        "bead": bead,
                        "action_id": action_id,
                        "reason": "incident_not_open",
                    }
                )
                continue
            incident = dict(incident_value)
            assignment = desktop.get("assignment")
            if recovery_decision == "adopt_observed_task":
                if (
                    action.get("state") != "succeeded"
                    or not isinstance(assignment, Mapping)
                    or not assignment.get("task_id")
                ):
                    stale_recoveries.append(
                        {
                            "bead": bead,
                            "action_id": action_id,
                            "reason": "provider_task_not_reconciled",
                        }
                    )
                    continue
            elif recovery_decision == "retry_same_action":
                provider_truth = action.get("provider_truth")
                if (
                    action.get("state") != "uncertain"
                    or not isinstance(provider_truth, Mapping)
                    or provider_truth.get("state") != "definitely_not_created"
                ):
                    stale_recoveries.append(
                        {
                            "bead": bead,
                            "action_id": action_id,
                            "reason": "definite_absence_not_proven",
                        }
                    )
                    continue
                for field in (
                    "claimed_at",
                    "claimed_by",
                    "completed_at",
                    "native_result",
                    "scenario_fault",
                    "provider_truth",
                    "possible_task_locator",
                    "recovery_incident_id",
                ):
                    action.pop(field, None)
                action["state"] = "pending"
                action["retry_authorized_at"] = _utc_now()
                action["retry_authorized_by"] = operation.get("decision_id")
                actions[action_id] = action
                desktop["actions"] = actions
                if isinstance(assignment, Mapping):
                    retried_assignment = dict(assignment)
                    for field in (
                        "task_id",
                        "client_thread_id",
                        "created_at",
                        "uncertain_at",
                    ):
                        retried_assignment.pop(field, None)
                    retried_assignment["state"] = "reserved"
                    retried_assignment["retry_authorized_at"] = _utc_now()
                    desktop["assignment"] = retried_assignment
            elif recovery_decision == "leave_uncertain":
                accepted_recoveries.append(
                    {
                        "bead": bead,
                        "action_id": action_id,
                        "decision": recovery_decision,
                    }
                )
                continue
            else:
                stale_recoveries.append(
                    {
                        "bead": bead,
                        "action_id": action_id,
                        "reason": "unsupported_recovery_decision",
                    }
                )
                continue
            if recovery_decision == "adopt_observed_task" and isinstance(
                assignment, Mapping
            ):
                worker_task_id = str(assignment.get("task_id") or "")
                assignment_token = str(assignment.get("assignment_token") or "")
                purpose = f"resume_reconciled_worker:{action_id}"
                if worker_task_id and not any(
                    isinstance(item, Mapping)
                    and item.get("purpose") == purpose
                    and item.get("state") not in {"rejected", "superseded"}
                    for item in actions.values()
                ):
                    self._append_action(
                        desktop,
                        record_id=record.id,
                        executor="steward",
                        tool="send_message_to_thread",
                        arguments={
                            "threadId": worker_task_id,
                            "prompt": (
                                "Fulcrum reconciled your exact retained creation "
                                f"action for {record.id}. Retry register_worker once "
                                f"with assignment_token `{assignment_token}` and this "
                                "same task identity, then continue the original "
                                "assignment. Do not create or delegate another task."
                            ),
                        },
                        purpose=purpose,
                        expected_result={"thread_id": worker_task_id},
                        assignment_token=assignment_token or None,
                        reporting={
                            "kind": "reconciled_worker_resumption",
                            "reconciles": action_id,
                        },
                    )
            incident.update(
                {
                    "state": "resolved",
                    "resolved_at": _utc_now(),
                    "marshal_decision_id": operation.get("decision_id"),
                    "resolution": recovery_decision,
                }
            )
            incidents[incident_key] = incident
            desktop["incidents"] = incidents
            ledger.update_fc(record.id, _with_protocol(record.fc or {}, desktop))
            accepted_recoveries.append(
                {
                    "bead": bead,
                    "action_id": action_id,
                    "decision": recovery_decision,
                }
            )
        recovery_action = None
        standing = system_protocol.get("standing") or {}
        steward = standing.get("steward") if isinstance(standing, Mapping) else None
        unsettled = any(
            isinstance(action, Mapping)
            and action.get("state") in {"issuing", "uncertain"}
            for candidate in ledger.list_records(limit=0)
            for action in (
                (_protocol(candidate.fc or {}).get("actions") or {}).values()
                if isinstance(_protocol(candidate.fc or {}).get("actions"), Mapping)
                else []
            )
        )
        if (
            accepted_recoveries
            and all(row["decision"] != "leave_uncertain" for row in accepted_recoveries)
            and isinstance(steward, Mapping)
            and steward.get("state")
            in {"stopped", "stop_observed", "interrupt_observed"}
            and not unsettled
        ):
            system_actions = dict(system_protocol.get("actions") or {})
            existing_resume = next(
                (
                    item
                    for item in system_actions.values()
                    if isinstance(item, Mapping)
                    and item.get("purpose") == "recover_steward_loop"
                    and item.get("state") not in {"rejected", "superseded"}
                ),
                None,
            )
            if isinstance(existing_resume, Mapping):
                recovery_action = dict(existing_resume)
            else:
                recovery_action = self._append_action(
                    system_protocol,
                    record_id=system.id,
                    executor="marshal",
                    tool="send_message_to_thread",
                    arguments={
                        "threadId": steward.get("task_id"),
                        "prompt": (
                            "Resume as the existing Steward. Re-read durable Fulcrum "
                            "state, then call wait_for_instructions and execute only "
                            "the retained actions it returns."
                        ),
                    },
                    purpose="recover_steward_loop",
                    expected_result={"thread_id": steward.get("task_id")},
                    reporting={"purpose": "same_steward_resumption"},
                )
        completed = {
            **dict(operation),
            "state": "completed",
            "completed_at": _utc_now(),
            "accepted": accepted,
            "stale": stale,
            "accepted_recoveries": accepted_recoveries,
            "stale_recoveries": stale_recoveries,
        }
        system_protocol["marshal_decision"] = completed
        accepted_input = operation.get("accepted_input")
        accepted_payload = (
            accepted_input.get("input") if isinstance(accepted_input, Mapping) else None
        )
        if (
            isinstance(accepted_payload, Mapping)
            and accepted_payload.get("trigger") == "heartbeat"
        ):
            schedule = system_protocol.get("marshal_schedule")
            if isinstance(schedule, Mapping):
                cycles = list(schedule.get("completed_cycles") or [])
                cycle = {
                    "decision_id": completed["decision_id"],
                    "turn_id": completed["turn_id"],
                    "completed_at": completed["completed_at"],
                    "accepted_count": len(accepted),
                    "stale_count": len(stale),
                }
                cycles.append(cycle)
                system_protocol["marshal_schedule"] = {
                    **dict(schedule),
                    "last_cycle_completed_at": completed["completed_at"],
                    "last_cycle_turn_id": completed["turn_id"],
                    "completed_cycle_count": int(
                        schedule.get("completed_cycle_count") or 0
                    )
                    + 1,
                    "completed_cycles": cycles[-20:],
                }
        value = {
            "decision": completed,
            "accepted": accepted,
            "stale": stale,
            "accepted_recoveries": accepted_recoveries,
            "stale_recoveries": stale_recoveries,
            "recovery_action": (
                self._action_response(request, recovery_action)
                if recovery_action is not None
                else None
            ),
        }
        _save_request(system_protocol, request, value, ledger=ledger)
        ledger.update_fc(system.id, _with_protocol(system.fc or {}, system_protocol))
        self._write_event(
            request,
            "marshal_decision_completed",
            operation_id=completed["decision_id"],
            outcome="completed",
            accepted=accepted,
            stale=stale,
        )
        return _result(request, value)

    @coordinated
    def report_incident(self, request: ParsedRequest) -> CommandResult:
        ledger = self._ledger(request)
        bead = str(request.arguments.get("bead") or request.input.get("bead") or "")
        record = ledger.show(bead)
        if record is None:
            raise FulcrumError.invalid("NOT_FOUND", f"unknown work {bead}")
        self._authorize_work_actor(
            ledger, record, request, standing_roles={"marshal", "vizier"}
        )
        protocol = _protocol(record.fc or {})
        saved = _saved_request(protocol, request, ledger=ledger)
        if saved:
            from fulcrum.desktop_protocol import _replay

            return _replay(saved, request)
        incident_key = str(request.input.get("incident_key") or "")
        if not incident_key:
            raise FulcrumError.invalid(
                "INCIDENT_KEY_REQUIRED", "incident_key is required"
            )
        incidents = dict(protocol.get("incidents") or {})
        existing = incidents.get(incident_key)
        incident = {
            **(dict(existing) if isinstance(existing, Mapping) else {}),
            "incident_id": (
                existing.get("incident_id")
                if isinstance(existing, Mapping)
                else _opaque("incident")
            ),
            "incident_key": incident_key,
            "state": "open",
            "scope": request.input.get("scope"),
            "required_decision": request.input.get("required_decision"),
            "evidence": copy.deepcopy(request.input.get("evidence") or []),
            "updated_at": _utc_now(),
            "repair_cycles": (
                int((existing or {}).get("repair_cycles", 0))
                if isinstance(existing, Mapping)
                else 0
            ),
            "justiciar_interventions": (
                int((existing or {}).get("justiciar_interventions", 0))
                if isinstance(existing, Mapping)
                else 0
            ),
        }
        incidents[incident_key] = incident
        protocol["incidents"] = incidents
        alert = self._ensure_notice(
            ledger,
            record.id,
            protocol,
            target_role="marshal",
            purpose=f"incident_alert:{incident['incident_id']}",
            prompt=(
                f"Fulcrum incident {incident['incident_id']} affects {record.id}. "
                f"Scope: {incident.get('scope') or 'unspecified'}. Run marshal_check "
                "and act only on current retained facts."
            ),
        )
        decision = None
        if (
            incident.get("required_decision")
            and int(incident.get("justiciar_interventions", 0)) >= 1
        ):
            decision = self._ensure_human_decision(
                ledger, record.id, protocol, incident
            )
        value: dict[str, Any] = {
            "incident": incident,
            "coalesced": existing is not None,
        }
        value["alert"] = alert
        value["decision"] = decision
        _save_request(protocol, request, value, ledger=ledger)
        ledger.update_fc(record.id, _with_protocol(record.fc or {}, protocol))
        return _result(request, value)

    @coordinated
    def record_repair(self, request: ParsedRequest) -> CommandResult:
        ledger = self._ledger(request)
        bead = str(request.arguments.get("bead") or request.input.get("bead") or "")
        record = ledger.show(bead)
        if record is None:
            raise FulcrumError.invalid("NOT_FOUND", f"unknown work {bead}")
        self._authorize_work_actor(
            ledger, record, request, standing_roles={"marshal", "vizier"}
        )
        protocol = _protocol(record.fc or {})
        saved = _saved_request(protocol, request, ledger=ledger)
        if saved:
            from fulcrum.desktop_protocol import _replay

            return _replay(saved, request)
        key = str(request.input.get("incident_key") or "")
        incidents = dict(protocol.get("incidents") or {})
        current = incidents.get(key)
        if not isinstance(current, Mapping):
            raise FulcrumError.invalid("INCIDENT_NOT_FOUND", "unknown incident")
        incident = dict(current)
        outcome = str(request.input.get("outcome") or "")
        if outcome == "failed":
            cycles = int(incident.get("repair_cycles", 0)) + 1
            allowance = 3 + int(incident.get("additional_repair_cycles", 0))
            if cycles > allowance:
                raise FulcrumError(
                    "REPAIR_LIMIT",
                    "the authorized ordinary repair cycles are exhausted",
                    exit_code=5,
                )
            incident["repair_cycles"] = cycles
            if cycles == allowance:
                incident["repair_hold"] = True
                incident["required_decision"] = (
                    "Marshal may authorize one Justiciar intervention."
                )
            if int(incident.get("justiciar_interventions", 0)) >= 1:
                incident["repair_hold"] = True
                incident["required_decision"] = (
                    "Human direction is required after the scoped Justiciar intervention."
                )
        elif outcome == "succeeded":
            incident["state"] = "resolved"
            incident["resolved_at"] = _utc_now()
        else:
            raise FulcrumError.invalid(
                "INVALID_OUTCOME", "repair outcome must be failed or succeeded"
            )
        incidents[key] = incident
        protocol["incidents"] = incidents
        alert = None
        if incident.get("repair_hold"):
            alert = self._ensure_notice(
                ledger,
                record.id,
                protocol,
                target_role="marshal",
                purpose=f"repair_hold:{incident.get('incident_id')}",
                prompt=(
                    f"Repair allowance is exhausted for {record.id}, incident "
                    f"{incident.get('incident_id')}. Inspect current evidence before "
                    "authorizing any scoped recovery."
                ),
            )
        decision = None
        if outcome == "failed" and int(incident.get("justiciar_interventions", 0)) >= 1:
            decision = self._ensure_human_decision(
                ledger, record.id, protocol, incident
            )
        value = {"incident": incident, "alert": alert, "decision": decision}
        _save_request(protocol, request, value, ledger=ledger)
        ledger.update_fc(record.id, _with_protocol(record.fc or {}, protocol))
        return _result(request, value)

    def _ensure_notice(
        self,
        ledger: Any,
        record_id: str,
        protocol: dict[str, Any],
        *,
        target_role: str,
        purpose: str,
        prompt: str,
    ) -> Mapping[str, Any]:
        actions = dict(protocol.get("actions") or {})
        for action in actions.values():
            if (
                isinstance(action, Mapping)
                and action.get("purpose") == purpose
                and action.get("state") not in {"rejected", "superseded"}
            ):
                return dict(action)
        system = self._system(ledger)
        binding = (_protocol(system.fc or {}).get("standing") or {}).get(target_role)
        if not isinstance(binding, Mapping) or binding.get("state") != "registered":
            return {
                "state": "blocked",
                "reason": f"{target_role} is not registered",
                "purpose": purpose,
            }
        return self._append_action(
            protocol,
            record_id=record_id,
            executor="steward",
            tool="send_message_to_thread",
            arguments={"threadId": binding.get("task_id"), "prompt": prompt},
            purpose=purpose,
            expected_result={"thread_id": binding.get("task_id")},
            reporting={"kind": "recorded_notification", "target": target_role},
        )

    def _ensure_human_decision(
        self,
        ledger: Any,
        record_id: str,
        protocol: dict[str, Any],
        incident: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        decisions = dict(protocol.get("human_decisions") or {})
        incident_id = str(incident.get("incident_id") or "")
        existing = next(
            (
                value
                for value in decisions.values()
                if isinstance(value, Mapping)
                and value.get("incident_id") == incident_id
                and value.get("state") == "open"
            ),
            None,
        )
        if isinstance(existing, Mapping):
            return dict(existing)
        decision_id = _opaque("human-decision")
        decision = {
            "decision_id": decision_id,
            "incident_id": incident_id,
            "state": "open",
            "question": incident.get("required_decision"),
            "scope": incident.get("scope"),
            "evidence": copy.deepcopy(incident.get("evidence") or []),
            "created_at": _utc_now(),
        }
        decisions[decision_id] = decision
        protocol["human_decisions"] = decisions
        notice = self._ensure_notice(
            ledger,
            record_id,
            protocol,
            target_role="vizier",
            purpose=f"human_decision:{decision_id}",
            prompt=(
                f"Human decision {decision_id} is required for {record_id}: "
                f"{decision.get('question')}. Reread current Fulcrum state before "
                "presenting or recording a response."
            ),
        )
        decision["notice"] = copy.deepcopy(dict(notice))
        decisions[decision_id] = decision
        protocol["human_decisions"] = decisions
        return decision

    @coordinated
    def recovery_prepare(self, request: ParsedRequest) -> CommandResult:
        ledger = self._ledger(request)
        self._standing_actor(ledger, "marshal", request)
        require_run_control(ledger, "Justiciar creation")
        bead = str(request.arguments.get("bead") or request.input.get("bead") or "")
        record = ledger.show(bead)
        if record is None:
            raise FulcrumError.invalid("NOT_FOUND", f"unknown work {bead}")
        system = self._system(ledger)
        system_protocol = _protocol(system.fc or {})
        saved = _saved_request(system_protocol, request, ledger=ledger)
        if saved:
            from fulcrum.desktop_protocol import _replay

            return _replay(saved, request)
        slot = system_protocol.get("recovery_slot")
        matching_slot = (
            isinstance(slot, Mapping)
            and slot.get("state") in {"reserved", "issuing", "active", "uncertain"}
            and slot.get("request_id") == request.request_id
            and slot.get("accepted_input") == _request_input(request)
        )
        if (
            isinstance(slot, Mapping)
            and slot.get("state")
            in {
                "reserved",
                "issuing",
                "active",
                "uncertain",
            }
            and not matching_slot
        ):
            raise FulcrumError(
                "RECOVERY_CAPACITY",
                "the additional recovery slot is occupied",
                exit_code=5,
            )
        protocol = _protocol(record.fc or {})
        key = str(request.input.get("incident_key") or "")
        incidents = dict(protocol.get("incidents") or {})
        current = incidents.get(key)
        if not isinstance(current, Mapping):
            raise FulcrumError.invalid("INCIDENT_NOT_FOUND", "unknown incident")
        incident = dict(current)
        if matching_slot:
            action_id = str(slot.get("action_id") or "")
            retained_action = (protocol.get("actions") or {}).get(action_id)
            if isinstance(retained_action, Mapping) and incident.get(
                "recovery_id"
            ) == slot.get("recovery_id"):
                value = {
                    "recovery_id": slot.get("recovery_id"),
                    "bead": bead,
                    "incident_key": key,
                    "action_id": action_id,
                    "action": self._action_response(request, retained_action),
                }
                _save_request(system_protocol, request, value, ledger=ledger)
                ledger.update_fc(
                    system.id, _with_protocol(system.fc or {}, system_protocol)
                )
                return _result(request, value)
        assignment = protocol.get("assignment")
        if isinstance(assignment, Mapping) and assignment.get("state") in {
            "reserved",
            "issuing",
            "active",
            "uncertain",
        }:
            raise FulcrumError(
                "RECOVERY_CONFLICT",
                "work already has an active or unresolved writer",
                exit_code=5,
            )
        if int(incident.get("justiciar_interventions", 0)) >= 1:
            raise FulcrumError(
                "INTERVENTION_LIMIT",
                "this incident already used its Justiciar intervention",
                exit_code=5,
            )
        if int(incident.get("repair_cycles", 0)) > 0 and not incident.get(
            "repair_hold"
        ):
            raise FulcrumError(
                "RECOVERY_NOT_AUTHORIZED",
                "ordinary repair allowance is not exhausted",
                exit_code=5,
            )
        fc = dict(record.fc or {})
        delivery = fc.get("delivery")
        worktree = fc.get("worktree")
        workspace = fc.get("workspace")
        if not workspace and isinstance(delivery, Mapping):
            workspace = delivery.get("workspace")
        if not workspace and isinstance(worktree, Mapping):
            workspace = worktree.get("path")
        source = fc.get("source")
        if not source and isinstance(delivery, Mapping):
            source = delivery.get("source_oid")
        if not source and isinstance(worktree, Mapping):
            source = worktree.get("head_oid")
        branch = worktree.get("branch") if isinstance(worktree, Mapping) else None
        project = str(fc.get("project") or "")
        manager = ConfigurationManager(request.instance.config_path)
        document, _ = manager.load()
        config = manager.effective(document)
        project_config = (config.get("projects") or {}).get(project)
        if not isinstance(project_config, Mapping):
            raise FulcrumError(
                "PROJECT_NOT_CONFIGURED",
                "recovery requires the work's configured Desktop project",
                exit_code=5,
            )
        codex_project = project_config.get("codex_project_id")
        if not isinstance(workspace, str) or not workspace or not codex_project:
            raise FulcrumError(
                "RECOVERY_WORKSPACE_REQUIRED",
                "recovery requires the retained workspace and saved Desktop project",
                exit_code=5,
            )
        role_models = fc.get("models")
        model_config = (
            role_models.get("justiciar") if isinstance(role_models, Mapping) else None
        )
        project_models = project_config.get("models")
        if not isinstance(model_config, Mapping) and isinstance(
            project_models, Mapping
        ):
            model_config = project_models.get("justiciar")
        if not isinstance(model_config, Mapping):
            model_config = (config.get("models") or {}).get("justiciar")
        recovery_id = (
            str(slot.get("recovery_id")) if matching_slot else _opaque("recovery")
        )
        action_id = str(slot.get("action_id")) if matching_slot else _opaque("action")
        assignment_token = (
            str(slot.get("assignment_token"))
            if matching_slot
            else _opaque("assignment")
        )
        slot = {
            "recovery_id": recovery_id,
            "bead": bead,
            "incident_key": key,
            "action_id": action_id,
            "assignment_token": assignment_token,
            "request_id": request.request_id,
            "accepted_input": _request_input(request),
            "state": "reserved",
            "reserved_at": _utc_now(),
        }
        system_protocol["recovery_slot"] = slot
        ledger.update_fc(system.id, _with_protocol(system.fc or {}, system_protocol))
        scope = request.input.get("scope") or incident.get("scope")
        prompt = (
            f"$fulcrum-justiciar\nRegister as Justiciar for {bead} before editing. "
            f"Work only in {workspace}. Assignment token: {assignment_token}.\n"
            f"Incident: {incident.get('incident_id')}\nScope: {scope}\n"
            f"Current source: {source or 'not retained'}\n"
            "Act only within this recovery scope and reconcile every native effect."
        )
        native_arguments: dict[str, Any] = {
            "prompt": prompt,
            "title": f"⚖️ JUSTICIAR {bead} ⚖️",
            "target": {
                "type": "project",
                "projectId": codex_project,
                "environment": {"type": "local"},
            },
        }
        if isinstance(model_config, Mapping) and model_config.get("model"):
            native_arguments["model"] = model_config["model"]
        if isinstance(model_config, Mapping) and model_config.get("effort"):
            native_arguments["thinking"] = model_config["effort"]
        action = {
            "action_id": action_id,
            "record_id": bead,
            "executor": "marshal",
            "tool": "create_thread",
            "arguments": native_arguments,
            "expected_result": {"threadId": "native task identity"},
            "reporting": {"register": "register_worker"},
            "assignment_token": assignment_token,
            "state": "pending",
            "attempts": [],
            "created_at": _utc_now(),
            "purpose": "exceptional_recovery",
        }
        actions = dict(protocol.get("actions") or {})
        actions[action_id] = action
        protocol["actions"] = actions
        protocol["assignment"] = {
            "assignment_token": assignment_token,
            "role": "justiciar",
            "workspace": workspace,
            "source": source,
            "project": project,
            "branch": branch,
            "scope": scope,
            "capacity_class": "recovery",
            "state": "reserved",
            "recovery_id": recovery_id,
        }
        incident["justiciar_interventions"] = 1
        incident["recovery_id"] = recovery_id
        incident["repair_hold"] = True
        incidents[key] = incident
        protocol["incidents"] = incidents
        fc["recovery_fence"] = {
            "state": "active",
            "operation_id": recovery_id,
            "recovery_id": recovery_id,
            "incident_id": incident.get("incident_id"),
            "incident_key": key,
            "scope": scope,
            "workspace": workspace,
            "source": source,
            "created_at": _utc_now(),
        }
        ledger.update_fc(record.id, _with_protocol(fc, protocol), assignee="MARSHAL")
        value = {
            "recovery_id": recovery_id,
            "bead": bead,
            "incident_key": key,
            "action_id": action_id,
        }
        value["action"] = {
            **action,
            "arguments": {
                **dict(action["arguments"]),
                "prompt": action_marker(
                    str(request.instance.instance_root),
                    bead,
                    action_id,
                    assignment_token,
                )
                + "\n"
                + prompt,
            },
        }
        _save_request(system_protocol, request, value, ledger=ledger)
        ledger.update_fc(system.id, _with_protocol(system.fc or {}, system_protocol))
        return _result(request, value)

    @coordinated
    def decision_respond(self, request: ParsedRequest) -> CommandResult:
        ledger = self._ledger(request)
        if request.actor.kind != "human":
            raise FulcrumError(
                "HUMAN_DECISION_REQUIRED",
                "only an explicit human response can extend repair authority",
                exit_code=5,
            )
        bead = str(request.arguments.get("bead") or request.input.get("bead") or "")
        record = ledger.show(bead)
        if record is None:
            raise FulcrumError.invalid("NOT_FOUND", f"unknown work {bead}")
        protocol = _protocol(record.fc or {})
        saved = _saved_request(protocol, request, ledger=ledger)
        if saved:
            from fulcrum.desktop_protocol import _replay

            return _replay(saved, request)
        decisions = dict(protocol.get("human_decisions") or {})
        decision_id = str(request.input.get("decision_id") or "")
        current = decisions.get(decision_id)
        if not isinstance(current, Mapping) or current.get("state") == "resolved":
            raise FulcrumError(
                "DECISION_NOT_OPEN", "human decision is not open", exit_code=5
            )
        additional = int(request.input.get("additional_repair_cycles") or 0)
        if additional < 0:
            raise FulcrumError.invalid(
                "INVALID_REPAIR_ALLOWANCE",
                "additional_repair_cycles cannot be negative",
            )
        decision = {
            **dict(current),
            "state": "resolved",
            "answer": copy.deepcopy(request.input.get("answer")),
            "additional_repair_cycles": additional,
            "resolved_at": _utc_now(),
        }
        decisions[decision_id] = decision
        protocol["human_decisions"] = decisions
        incident_id = current.get("incident_id")
        incidents = dict(protocol.get("incidents") or {})
        matching_key = next(
            (
                key
                for key, value in incidents.items()
                if isinstance(value, Mapping)
                and value.get("incident_id") == incident_id
            ),
            None,
        )
        if matching_key is None:
            raise FulcrumError(
                "INCIDENT_NOT_FOUND",
                "the decision's incident is no longer retained",
                exit_code=5,
            )
        incident = dict(incidents[matching_key])
        incident["additional_repair_cycles"] = (
            int(incident.get("additional_repair_cycles", 0)) + additional
        )
        if additional > 0:
            incident["repair_hold"] = False
            incident["required_decision"] = None
        incident["human_decision_id"] = decision_id
        incident["updated_at"] = _utc_now()
        incidents[matching_key] = incident
        protocol["incidents"] = incidents
        value = {"decision": decision}
        _save_request(protocol, request, value, ledger=ledger)
        ledger.update_fc(record.id, _with_protocol(record.fc or {}, protocol))
        return _result(request, value)
