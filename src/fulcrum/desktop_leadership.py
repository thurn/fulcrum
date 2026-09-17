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
                    "turn_id": request.input.get("turn_id"),
                }
            )
            protocol["marshal_schedule"] = {
                **dict(schedule),
                "last_delivery_at": delivered_at,
                "last_delivery_turn_id": request.input.get("turn_id"),
                "delivery_count": int(schedule.get("delivery_count") or 0) + 1,
                "deliveries": deliveries[-20:],
            }
        current = protocol.get("marshal_decision")
        if isinstance(current, Mapping) and current.get("state") == "active":
            same_turn = request.input.get("turn_id") == current.get("turn_id")
            if not same_turn and not _positive_native_completion(protocol, current):
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
                history.append(copy.deepcopy(dict(current)))
                protocol["marshal_decision_history"] = history[-20:]
                current = {
                    **dict(current),
                    "turn_id": request.input.get("turn_id"),
                    "recovered_at": _utc_now(),
                }
                protocol["marshal_decision"] = current
            value = {
                "decision": copy.deepcopy(dict(current)),
                "joined": True,
                "brief": {"incidents": [], "ready": [], "omitted": {}},
            }
            _save_request(protocol, request, value, ledger=ledger)
            ledger.update_fc(system.id, _with_protocol(system.fc or {}, protocol))
            return _result(request, value)
        records = ledger.list_records(limit=0)
        incidents: list[dict[str, Any]] = []
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
            if record.status != "closed" and fc.get("phase") in {
                "ready",
                "implementation_ready",
            }:
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
                if isinstance(steward, Mapping) and steward.get("state") == "stopped"
                else "unknown"
            )
        )
        decision = {
            "decision_id": _opaque("decision"),
            "state": "active",
            "task_id": binding["task_id"],
            "turn_id": request.input.get("turn_id"),
            "accepted_input": _request_input(request),
            "started_at": _utc_now(),
        }
        protocol["marshal_decision"] = decision
        recovery_action: dict[str, Any] | None = None
        actions = dict(protocol.get("actions") or {})
        unsettled = any(
            isinstance(action, Mapping)
            and action.get("state") in {"issuing", "uncertain"}
            for action in actions.values()
        )
        if (
            steward_health == "stopped"
            and isinstance(steward, Mapping)
            and not healthy_wait
            and not unsettled
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
        value = {
            "decision": decision,
            "joined": False,
            "brief": {
                "purpose": "recovery" if incidents else "curation",
                "steward_health": steward_health,
                "incidents": incidents[:20],
                "ready": sorted(ready, key=lambda row: (row["priority"], row["bead"]))[
                    :20
                ],
                "omitted": {
                    "incidents": max(0, len(incidents) - 20),
                    "ready": max(0, len(ready) - 20),
                },
                "recovery_action": (
                    self._action_response(request, recovery_action)
                    if recovery_action is not None
                    else None
                ),
            },
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
        self._standing_actor(ledger, "marshal", request)
        system = self._system(ledger)
        system_protocol = _protocol(system.fc or {})
        saved = _saved_request(system_protocol, request, ledger=ledger)
        if saved:
            from fulcrum.desktop_protocol import _replay

            return _replay(saved, request)
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
        if request.input.get("turn_id") != operation.get("turn_id"):
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
        completed = {
            **dict(operation),
            "state": "completed",
            "completed_at": _utc_now(),
            "accepted": accepted,
            "stale": stale,
        }
        system_protocol["marshal_decision"] = completed
        value = {"decision": completed, "accepted": accepted, "stale": stale}
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
