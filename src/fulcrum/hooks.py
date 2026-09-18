"""Trusted Codex command-hook handling and scoped transcript collection."""

from __future__ import annotations

import copy
import json
import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from fulcrum.configuration import ConfigurationManager
from fulcrum.contracts import CommandResult, FulcrumError, ParsedRequest
from fulcrum.coordination import coordinated
from fulcrum.desktop_protocol import (
    DesktopProtocolService,
    _protocol,
    _utc_now,
    _validated_action_outcome,
    _with_protocol,
)
from fulcrum.diagnostics import DiagnosticLog
from fulcrum.ledger import Ledger, LedgerRecord
from fulcrum.observations import TERMINAL_LIFECYCLE_TYPES, read_transcript

HOOK_EVENTS = {
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Stop",
}
MARKER: re.Pattern[str] = re.compile(
    r"^Fulcrum-Action: (?P<value>\{[^\n]+\})", re.MULTILINE
)
MANAGED_NATIVE_TOOLS = {
    "automation_update",
    "create_thread",
    "list_projects",
    "list_threads",
    "read_thread",
    "send_message_to_thread",
    "set_thread_archived",
    "set_thread_title",
}


class HookService:
    def __init__(self, ledger: Ledger | None = None) -> None:
        self._ledger_override = ledger

    def _ledger(self, request: ParsedRequest) -> Ledger:
        if self._ledger_override is not None:
            return self._ledger_override
        if request.instance.brain_root is None:
            raise FulcrumError(
                "LEDGER_UNAVAILABLE", "hook handling requires Beads", exit_code=4
            )
        manager = ConfigurationManager(request.instance.config_path)
        document, _ = manager.load()
        configured = manager.effective(document)["beads"].get("executable")
        executable = str(configured) if configured else None
        return Ledger(
            request.instance.brain_root,
            executable=executable,
            timeout=request.timeout,
        )

    @coordinated
    def handle(self, request: ParsedRequest) -> CommandResult:
        event_name = str(
            request.input.get("hook_event_name")
            or request.input.get("event_name")
            or request.input.get("hookEventName")
            or ""
        )
        if event_name not in HOOK_EVENTS:
            return CommandResult.query({"continue": True})
        ledger = self._ledger(request)
        task_id = str(
            request.input.get("thread_id")
            or request.input.get("task_id")
            or request.actor.task_id
            or request.thread_id
            or ""
        )
        bound = _binding_for_task(ledger, task_id) if task_id else None
        if bound is None:
            session_id = request.input.get("session_id")
            if isinstance(session_id, str) and session_id:
                # Codex hook payloads expose the native thread identity as the
                # session id, even when they omit thread_id/task_id. Same-task
                # Weaver entry intentionally records that native thread as the
                # assignment task before a session-bound hook has run.
                bound = _binding_for_task(ledger, session_id)
                if bound is None:
                    bound = _binding_for_session(ledger, session_id)
                if bound is not None:
                    task_id = _task_id_for_binding(bound)
        marker = _parse_marker(request.input)
        if bound is None and marker is not None:
            bound = _prospective_binding(ledger, request, marker)
        response: dict[str, Any] = (
            {} if event_name == "PreToolUse" else {"continue": True}
        )
        if event_name == "SessionStart" and bound is not None:
            response["hookSpecificOutput"] = {
                "hookEventName": "SessionStart",
                "additionalContext": _context(bound),
            }
        elif event_name == "UserPromptSubmit" and marker is not None:
            response = self._prompt(ledger, request, marker, bound, task_id)
        elif event_name == "UserPromptSubmit" and bound is not None:
            self._record_manual_task_activity(ledger, request, bound, task_id)
        elif event_name == "PreToolUse":
            response = self._pre_tool(ledger, request, bound)
        elif event_name == "PostToolUse":
            self._post_tool(ledger, request, bound)
        elif bound is not None:
            self._lifecycle(ledger, request, bound, event_name)
        if bound is not None:
            bound = _binding_for_task(ledger, task_id) or bound
            self._collect_transcript(ledger, request, bound, task_id)
        self._log(request, event_name, task_id, bound is not None, response)
        return CommandResult.query(response)

    def _record_manual_task_activity(
        self,
        ledger: Ledger,
        request: ParsedRequest,
        bound: tuple[LedgerRecord, str],
        task_id: str,
    ) -> None:
        record, role = bound
        if role in {"steward", "marshal", "vizier"}:
            protocol = _protocol(record.fc or {})
            standing = dict(protocol.get("standing") or {})
            binding = standing.get(role)
            if isinstance(binding, Mapping):
                standing[role] = {
                    **dict(binding),
                    "state": "registered",
                    "session_id": request.input.get("session_id"),
                    "turn_id": request.input.get("turn_id"),
                    "last_prompt_at": _utc_now(),
                }
                protocol["standing"] = standing
                ledger.update_fc(record.id, _with_protocol(record.fc or {}, protocol))
            return
        protocol = _protocol(record.fc or {})
        assignment = protocol.get("assignment")
        if isinstance(assignment, Mapping) and assignment.get("task_id") == task_id:
            return
        native_tasks = dict(protocol.get("native_tasks") or {})
        current = native_tasks.get(task_id)
        if not isinstance(current, Mapping) or not current.get("archived"):
            return
        native_tasks[task_id] = {
            **dict(current),
            "archived": False,
            "manual_unarchive": True,
            "manual_activity_at": _utc_now(),
        }
        protocol["native_tasks"] = native_tasks
        ledger.update_fc(record.id, _with_protocol(record.fc or {}, protocol))

    def _prompt(
        self,
        ledger: Ledger,
        request: ParsedRequest,
        marker: Mapping[str, Any],
        bound: tuple[LedgerRecord, str] | None,
        task_id: str,
    ) -> dict[str, Any]:
        record = ledger.show(str(marker.get("record_id") or ""))
        if record is None:
            return _deny("unknown Fulcrum action record")
        protocol = _protocol(record.fc or {})
        action_id = str(marker.get("action_id") or "")
        actions = dict(protocol.get("actions") or {})
        action = actions.get(action_id)
        if not isinstance(action, Mapping):
            return _deny("unknown Fulcrum action")
        if marker.get("instance") != str(request.instance.instance_root):
            return _deny("Fulcrum action belongs to another instance")
        if action.get("assignment_token") and marker.get(
            "assignment_token"
        ) != action.get("assignment_token"):
            return _deny("Fulcrum assignment marker does not match")
        event_id = _event_id(request.input)
        handshakes = dict(protocol.get("handshakes") or {})
        accepted = {
            "event_id": event_id,
            "kind": "prompt",
            "action_id": action_id,
            "task_id": task_id,
            "session_id": request.input.get("session_id"),
            "turn_id": request.input.get("turn_id"),
        }
        _retain_callback(handshakes, event_id, accepted)
        protocol["handshakes"] = handshakes
        ledger.update_fc(record.id, _with_protocol(record.fc or {}, protocol))
        return {"continue": True}

    def _pre_tool(
        self,
        ledger: Ledger,
        request: ParsedRequest,
        bound: tuple[LedgerRecord, str] | None,
    ) -> dict[str, Any]:
        if bound is None:
            return {}
        tool_name = _tool_name(request.input)
        bound_record, _ = bound
        assignment = _protocol(bound_record.fc or {}).get("assignment")
        task_id = request.actor.task_id or request.thread_id
        if (
            bound_record.id != "fc-system"
            and isinstance(assignment, Mapping)
            and assignment.get("state") in {"reserved", "issuing", "uncertain"}
            and (not assignment.get("task_id") or assignment.get("task_id") == task_id)
            and tool_name != "register_worker"
        ):
            return _deny(
                "worker registration must succeed before any repository or native tool use"
            )
        if tool_name not in MANAGED_NATIVE_TOOLS:
            return {}
        located = _issuing_action_for_actor(ledger, bound, request.input)
        if located is None:
            return _deny("native effect has no matching claimed Fulcrum action")
        record, action, attempt = located
        protocol = _protocol(record.fc or {})
        expected = dict(action.get("arguments") or {})
        actual = request.input.get("tool_input") or request.input.get("toolInput")
        if not isinstance(actual, Mapping) or not _arguments_match(expected, actual):
            return _deny("native tool arguments differ from the claimed action")
        tool_use_id = request.input.get("tool_use_id") or request.input.get("toolUseId")
        attempt["native_tool_use_id"] = tool_use_id
        attempt["pre_hook_event_id"] = _event_id(request.input)
        _replace_attempt(protocol, str(action["action_id"]), attempt)
        ledger.update_fc(record.id, _with_protocol(record.fc or {}, protocol))
        return {}

    def _post_tool(
        self,
        ledger: Ledger,
        request: ParsedRequest,
        bound: tuple[LedgerRecord, str] | None,
    ) -> None:
        if bound is None:
            return
        tool_name = _tool_name(request.input)
        if tool_name not in MANAGED_NATIVE_TOOLS:
            return
        located = _issuing_action_for_actor(ledger, bound, request.input)
        if located is None:
            return
        record, action, attempt = located
        result = request.input.get("tool_response") or request.input.get("toolResponse")
        observed = "uncertain"
        if isinstance(result, Mapping) and isinstance(result.get("isError"), bool):
            observed = "rejected" if result["isError"] else "succeeded"
        try:
            observed = _validated_action_outcome(action, result, observed)
        except FulcrumError:
            observed = "uncertain"
        event_id = _event_id(request.input)
        DesktopProtocolService(ledger).report_action_result(
            replace(
                request,
                command=("action", "result"),
                arguments={
                    "record_id": record.id,
                    "action_id": str(action["action_id"]),
                },
                input={
                    "record_id": record.id,
                    "action_id": str(action["action_id"]),
                    "attempt_id": str(attempt["attempt_id"]),
                    "outcome": observed,
                    "evidence": {
                        "post_hook_event_id": event_id,
                        "source": "PostToolUse",
                    },
                    "native_result": copy.deepcopy(result),
                },
                request_id=str(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"hook-result:{event_id}")
                ),
            )
        )

    def _lifecycle(
        self,
        ledger: Ledger,
        request: ParsedRequest,
        bound: tuple[LedgerRecord, str],
        event_name: str,
    ) -> None:
        record, role = bound
        protocol = _protocol(record.fc or {})
        events = list(protocol.get("hook_events") or [])
        event = {
            "event_id": _event_id(request.input),
            "event": event_name,
            "session_id": request.input.get("session_id"),
            "turn_id": request.input.get("turn_id"),
            "reason": request.input.get("reason"),
        }
        assignment = protocol.get("assignment")
        if isinstance(assignment, Mapping):
            observed_task = str(
                request.input.get("thread_id")
                or request.input.get("task_id")
                or request.actor.task_id
                or request.thread_id
                or ""
            )
            if (
                assignment.get("task_id") == observed_task
                and assignment.get("turn_id")
                in {None, assignment.get("creation_action_id")}
                and event.get("turn_id")
            ):
                assignment = {
                    **dict(assignment),
                    "turn_id": event.get("turn_id"),
                    "turn_bound_at": _utc_now(),
                }
                protocol["assignment"] = assignment
            event.update(
                {
                    "assignment_token": assignment.get("assignment_token"),
                    "creation_action_id": assignment.get("creation_action_id"),
                    "task_id": assignment.get("task_id"),
                    "role": assignment.get("role"),
                }
            )
        if not any(item.get("event_id") == event["event_id"] for item in events):
            events.append(event)
            protocol["hook_events"] = events[-128:]
            standing = dict(protocol.get("standing") or {})
            binding = standing.get(role)
            if isinstance(binding, Mapping):
                standing[role] = {
                    **dict(binding),
                    "turn_id": event.get("turn_id") or binding.get("turn_id"),
                    "last_lifecycle_event": event,
                    "last_turn_ended_at": _utc_now(),
                }
                protocol["standing"] = standing
            ledger.update_fc(record.id, _with_protocol(record.fc or {}, protocol))
        if event_name == "Stop":
            _release_unregistered_assignment(
                ledger,
                task_id=str(event.get("task_id") or request.actor.task_id or ""),
                terminal_event=event,
            )

    def _collect_transcript(
        self,
        ledger: Ledger,
        request: ParsedRequest,
        bound: tuple[LedgerRecord, str],
        task_id: str,
    ) -> None:
        transcript = request.input.get("transcript_path") or request.input.get(
            "transcriptPath"
        )
        if not isinstance(transcript, str) or not Path(transcript).is_absolute():
            return
        record, role = bound
        self._collect_transcript_path(
            ledger,
            request,
            record,
            role,
            task_id,
            transcript,
            request.input.get("turn_id"),
        )

    def collect_registered(self, request: ParsedRequest) -> list[str]:
        """Collect every retained transcript and return its watched path."""

        ledger = self._ledger(request)
        paths: list[str] = []
        for record in ledger.list_records(limit=0):
            protocol = _protocol(record.fc or {})
            transcripts = protocol.get("transcripts")
            if not isinstance(transcripts, Mapping):
                continue
            assignment = protocol.get("assignment")
            standing = protocol.get("standing") or {}
            for task_id, retained in transcripts.items():
                if not isinstance(retained, Mapping):
                    continue
                transcript = retained.get("path")
                if (
                    not isinstance(transcript, str)
                    or not Path(transcript).is_absolute()
                ):
                    continue
                role = "worker"
                turn_id = None
                if (
                    isinstance(assignment, Mapping)
                    and assignment.get("task_id") == task_id
                ):
                    role = str(assignment.get("role") or role)
                    turn_id = assignment.get("turn_id")
                else:
                    for standing_role, binding in standing.items():
                        if (
                            isinstance(binding, Mapping)
                            and binding.get("task_id") == task_id
                        ):
                            role = str(standing_role)
                            turn_id = binding.get("turn_id")
                            break
                self._collect_transcript_path(
                    ledger,
                    request,
                    record,
                    role,
                    str(task_id),
                    transcript,
                    turn_id,
                )
                paths.append(transcript)
        return sorted(set(paths))

    @staticmethod
    def registered_paths(ledger: Ledger) -> list[str]:
        """Return retained transcript paths without parsing transcript content."""

        paths: set[str] = set()
        for record in ledger.list_records(limit=0):
            transcripts = _protocol(record.fc or {}).get("transcripts")
            if not isinstance(transcripts, Mapping):
                continue
            for retained in transcripts.values():
                path = retained.get("path") if isinstance(retained, Mapping) else None
                if isinstance(path, str) and Path(path).is_absolute():
                    paths.add(path)
        return sorted(paths)

    def _collect_transcript_path(
        self,
        ledger: Ledger,
        request: ParsedRequest,
        record: LedgerRecord,
        role: str,
        task_id: str,
        transcript: str,
        turn_id: Any,
    ) -> None:
        protocol = _protocol(record.fc or {})
        transcripts = dict(protocol.get("transcripts") or {})
        current = transcripts.get(task_id)
        cursor = int(current.get("cursor", 0)) if isinstance(current, Mapping) else 0
        page = read_transcript(Path(transcript), cursor)
        retained_gaps = (
            list(current.get("gaps") or []) if isinstance(current, Mapping) else []
        )
        page_gaps = [
            *(
                dict(item)
                for item in retained_gaps
                if isinstance(item, Mapping)
                and item.get("kind") != "incomplete_trailing_line"
            ),
            *(dict(item) for item in page.gaps),
        ][-20:]
        if page.cursor == cursor and page_gaps == retained_gaps:
            return
        observations = dict(protocol.get("observations") or {})
        lifecycle = dict(observations.get("lifecycle") or {})
        usage = dict(observations.get("usage") or {})
        models = {
            str(value.get("turn_id")): str(value["model"])
            for value in lifecycle.values()
            if isinstance(value, Mapping)
            and value.get("type") == "turn_context"
            and value.get("turn_id")
            and value.get("model")
        }
        assignment = protocol.get("assignment")
        if (
            isinstance(assignment, Mapping)
            and assignment.get("task_id") == task_id
            and assignment.get("turn_id")
            in {None, assignment.get("creation_action_id")}
        ):
            current_turn = next(
                (
                    item.get("turn_id")
                    for item in reversed(page.lifecycle)
                    if item.get("type") == "turn_context" and item.get("turn_id")
                ),
                None,
            )
            if current_turn:
                protocol["assignment"] = {
                    **dict(assignment),
                    "turn_id": current_turn,
                    "turn_bound_at": _utc_now(),
                }
        for item in page.lifecycle:
            normalized = dict(item)
            normalized["task_id"] = normalized.get("task_id") or task_id
            normalized["turn_id"] = normalized.get("turn_id") or turn_id
            if normalized.get("type") == "turn_context" and normalized.get("model"):
                models[str(normalized.get("turn_id"))] = str(normalized["model"])
            identity = normalized.get("event_id") or ":".join(
                str(normalized.get(field) or "unknown")
                for field in ("task_id", "turn_id", "type")
            )
            lifecycle[str(identity)] = normalized
        for item in page.usage:
            normalized = dict(item)
            normalized["task_id"] = normalized.get("task_id") or task_id
            normalized["turn_id"] = normalized.get("turn_id") or turn_id
            normalized["model"] = normalized.get("model") or models.get(
                str(normalized.get("turn_id"))
            )
            identity = normalized.get("response_id") or normalized.get("event_id")
            if identity:
                usage[str(identity)] = normalized
        observations["lifecycle"] = lifecycle
        observations["usage"] = usage
        protocol["observations"] = observations
        transcripts[task_id] = {
            "path": transcript,
            "cursor": page.cursor,
            "gaps": page_gaps,
        }
        protocol["transcripts"] = transcripts
        ledger.update_fc(record.id, _with_protocol(record.fc or {}, protocol))
        self._cancel_waits_for_terminal_events(ledger, task_id, page.lifecycle)
        task_usage = [
            value
            for value in usage.values()
            if isinstance(value, Mapping) and value.get("task_id") == task_id
        ]
        if task_usage:
            from fulcrum.analytics import record_desktop_usage

            record_desktop_usage(
                ledger,
                record,
                role,
                task_usage,
                [value for value in lifecycle.values() if isinstance(value, Mapping)],
            )
        from fulcrum.completion import settle_native_completion

        settle_native_completion(request, ledger, record.id)

    def _cancel_waits_for_terminal_events(
        self,
        ledger: Ledger,
        task_id: str,
        lifecycle_events: tuple[Mapping[str, Any], ...],
    ) -> None:
        terminal = next(
            (
                event
                for event in reversed(lifecycle_events)
                if event.get("type") in TERMINAL_LIFECYCLE_TYPES
            ),
            None,
        )
        if terminal is None:
            return
        _release_unregistered_assignment(
            ledger, task_id=task_id, terminal_event=terminal
        )
        interrupted = terminal.get("type") in {
            "turn_interrupted",
            "turn_aborted",
            "interrupted",
        }
        for candidate in ledger.list_records(limit=0):
            protocol = _protocol(candidate.fc or {})
            changed = False
            for collection_name in ("instruction_waits", "ci_waits"):
                collection = dict(protocol.get(collection_name) or {})
                for wait_id, retained in list(collection.items()):
                    if (
                        not isinstance(retained, Mapping)
                        or retained.get("state") != "waiting"
                        or retained.get("task_id") != task_id
                    ):
                        continue
                    terminal_time = terminal.get("time")
                    registered_at = retained.get("registered_at")
                    if (
                        isinstance(terminal_time, str)
                        and isinstance(registered_at, str)
                        and terminal_time < registered_at
                    ):
                        continue
                    response = {
                        "kind": "stop",
                        "reason": (
                            "native_turn_interrupted"
                            if interrupted
                            else "native_turn_ended"
                        ),
                        "wait_id": wait_id,
                        "retained_obligation": True,
                    }
                    collection[wait_id] = {
                        **dict(retained),
                        "state": "cancelled",
                        "resolved_at": _utc_now(),
                        "response": response,
                        "terminal_event": dict(terminal),
                    }
                    changed = True
                if changed:
                    protocol[collection_name] = collection
            if changed:
                ledger.update_fc(
                    candidate.id, _with_protocol(candidate.fc or {}, protocol)
                )

    def _log(
        self,
        request: ParsedRequest,
        event_name: str,
        task_id: str,
        bound: bool,
        response: Mapping[str, Any],
    ) -> None:
        if self._ledger_override is not None:
            return
        try:
            DiagnosticLog.from_request(request).append(
                {
                    "event": "hook_handled",
                    "component": "hook",
                    "process_id": os.getpid(),
                    "source_commit": os.environ.get("FULCRUM_COMMIT"),
                    "hook_event": event_name,
                    "task_id": task_id,
                    "session_id": request.input.get("session_id"),
                    "bound": bound,
                    "turn_id": request.input.get("turn_id"),
                    "tool_use_id": request.input.get("tool_use_id"),
                    "outcome": (
                        "rejected"
                        if (
                            isinstance(response.get("hookSpecificOutput"), Mapping)
                            and response["hookSpecificOutput"].get("permissionDecision")
                            == "deny"
                        )
                        else "allowed"
                    ),
                }
            )
        except OSError as error:
            DiagnosticLog.report_failure(request.instance.instance_root, error)


def _binding_for_task(ledger: Ledger, task_id: str) -> tuple[LedgerRecord, str] | None:
    system = ledger.show("fc-system")
    if system is not None:
        standing = _protocol(system.fc or {}).get("standing")
        if isinstance(standing, Mapping):
            for role, binding in standing.items():
                if isinstance(binding, Mapping) and binding.get("task_id") == task_id:
                    return system, str(role)
    for record in ledger.list_records(limit=0):
        protocol = _protocol(record.fc or {})
        assignment = protocol.get("assignment")
        if isinstance(assignment, Mapping) and assignment.get("task_id") == task_id:
            return record, str(assignment.get("role") or "worker")
        history = protocol.get("assignment_history")
        if isinstance(history, list):
            for retained in reversed(history):
                if isinstance(retained, Mapping) and retained.get("task_id") == task_id:
                    return record, str(retained.get("role") or "worker")
    return None


def _release_unregistered_assignment(
    ledger: Ledger,
    *,
    task_id: str,
    terminal_event: Mapping[str, Any],
) -> None:
    """Release capacity when a created worker terminates before registration."""

    if not task_id:
        return
    for record in ledger.list_records(kind="work", limit=0):
        fc = dict(record.fc or {})
        protocol = _protocol(fc)
        assignment = protocol.get("assignment")
        if (
            not isinstance(assignment, Mapping)
            or assignment.get("task_id") != task_id
            or assignment.get("state") not in {"reserved", "issuing", "uncertain"}
        ):
            continue
        released = {
            **dict(assignment),
            "state": "registration_failed",
            "released_at": _utc_now(),
            "terminal_event": copy.deepcopy(dict(terminal_event)),
        }
        history = list(protocol.get("assignment_history") or [])
        history.append(released)
        protocol["assignment_history"] = history[-20:]
        protocol.pop("assignment", None)
        failures = list(protocol.get("registration_failures") or [])
        failures.append(
            {
                "task_id": task_id,
                "role": assignment.get("role"),
                "assignment_token": assignment.get("assignment_token"),
                "creation_action_id": assignment.get("creation_action_id"),
                "terminal_event": copy.deepcopy(dict(terminal_event)),
                "recorded_at": _utc_now(),
            }
        )
        protocol["registration_failures"] = failures[-20:]
        exhausted = len(failures) >= 2
        incidents = dict(protocol.get("incidents") or {})
        incident_key = f"registration:{assignment.get('assignment_token')}"
        incidents[incident_key] = {
            "incident_id": incident_key,
            "incident_key": incident_key,
            "state": "open" if exhausted else "resolved",
            "scope": "downstream worker registration",
            "required_decision": (
                "Inspect repeated worker startup failures before another dispatch."
                if exhausted
                else None
            ),
            "evidence": {
                "task_id": task_id,
                "role": assignment.get("role"),
                "assignment_token": assignment.get("assignment_token"),
                "creation_action_id": assignment.get("creation_action_id"),
                "terminal_event": copy.deepcopy(dict(terminal_event)),
            },
            "recovery": {
                "assignment_released": True,
                "capacity_released": True,
                "task_archival_queued": True,
                "automatic_retry_available": not exhausted,
            },
            "updated_at": _utc_now(),
        }
        protocol["incidents"] = incidents
        actions = dict(protocol.get("actions") or {})
        purpose = f"archive_unregistered_task:{task_id}"
        if not any(
            isinstance(value, Mapping) and value.get("purpose") == purpose
            for value in actions.values()
        ):
            action_id = f"action-{uuid.uuid4()}"
            actions[action_id] = {
                "action_id": action_id,
                "record_id": record.id,
                "executor": "steward",
                "tool": "set_thread_archived",
                "arguments": {"threadId": task_id, "archived": True},
                "expected_result": {"threadId": task_id, "archived": True},
                "reporting": {"registration_failure": True},
                "assignment_token": assignment.get("assignment_token"),
                "state": "pending",
                "attempts": [],
                "created_at": _utc_now(),
                "purpose": purpose,
            }
        protocol["actions"] = actions
        fc.update(
            {
                "desktop": protocol,
                "owner": "HUMAN" if exhausted else "STEWARD",
                "role": None,
                "ownership_operation": None,
                "next_action": (
                    "Inspect repeated worker startup failures before authorizing another dispatch."
                    if exhausted
                    else "Archive the unregistered task, then retry downstream dispatch once."
                ),
            }
        )
        if exhausted:
            fc["blocked"] = {
                "reason": "worker_registration_failed_repeatedly",
                "incident_id": incident_key,
                "failure_count": len(failures),
            }
        ledger.update_fc(record.id, fc, assignee=str(fc["owner"]))


def _binding_for_session(
    ledger: Ledger, session_id: str
) -> tuple[LedgerRecord, str] | None:
    system = ledger.show("fc-system")
    if system is not None:
        standing = _protocol(system.fc or {}).get("standing")
        if isinstance(standing, Mapping):
            for role, binding in standing.items():
                if (
                    isinstance(binding, Mapping)
                    and binding.get("session_id") == session_id
                ):
                    return system, str(role)
    for record in ledger.list_records(limit=0):
        protocol = _protocol(record.fc or {})
        assignment = protocol.get("assignment")
        if (
            isinstance(assignment, Mapping)
            and assignment.get("session_id") == session_id
        ):
            return record, str(assignment.get("role") or "worker")
        history = protocol.get("assignment_history")
        if isinstance(history, list):
            for retained in reversed(history):
                if (
                    isinstance(retained, Mapping)
                    and retained.get("session_id") == session_id
                ):
                    return record, str(retained.get("role") or "worker")
    return None


def _task_id_for_binding(bound: tuple[LedgerRecord, str]) -> str:
    record, role = bound
    protocol = _protocol(record.fc or {})
    if record.id == "fc-system":
        standing = protocol.get("standing")
        binding = standing.get(role) if isinstance(standing, Mapping) else None
    else:
        binding = protocol.get("assignment")
        if not isinstance(binding, Mapping) or binding.get("role") != role:
            history = protocol.get("assignment_history")
            binding = (
                next(
                    (
                        retained
                        for retained in reversed(history)
                        if isinstance(retained, Mapping)
                        and retained.get("role") == role
                    ),
                    None,
                )
                if isinstance(history, list)
                else None
            )
    task_id = binding.get("task_id") if isinstance(binding, Mapping) else None
    return str(task_id or "")


def _prospective_binding(
    ledger: Ledger, request: ParsedRequest, marker: Mapping[str, Any]
) -> tuple[LedgerRecord, str] | None:
    if marker.get("instance") != str(request.instance.instance_root):
        return None
    record = ledger.show(str(marker.get("record_id") or ""))
    if record is None:
        return None
    action = (_protocol(record.fc or {}).get("actions") or {}).get(
        marker.get("action_id")
    )
    if not isinstance(action, Mapping):
        return None
    return record, str(action.get("executor") or "prospective")


def _parse_marker(value: Mapping[str, Any]) -> Mapping[str, Any] | None:
    text = value.get("prompt") or value.get("user_prompt") or value.get("userPrompt")
    if not isinstance(text, str):
        return None
    matched = MARKER.search(text)
    if not matched:
        return None
    try:
        parsed = json.loads(matched.group("value"))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _context(bound: tuple[LedgerRecord, str]) -> str:
    record, role = bound
    protocol = _protocol(record.fc or {})
    value = {
        "role": role,
        "record_id": record.id,
        "run_control": protocol.get("run_control"),
        "assignment": protocol.get("assignment"),
        "outstanding_actions": [
            {
                "action_id": action.get("action_id"),
                "state": action.get("state"),
                "tool": action.get("tool"),
            }
            for action in (protocol.get("actions") or {}).values()
            if isinstance(action, Mapping)
            and action.get("state") in {"pending", "issuing", "uncertain"}
        ],
    }
    return "Fulcrum managed context:\n" + json.dumps(
        value, separators=(",", ":"), ensure_ascii=False
    )


def _event_id(value: Mapping[str, Any]) -> str:
    return str(
        value.get("event_id")
        or value.get("hook_event_id")
        or value.get("tool_use_id")
        or uuid.uuid4()
    )


def _retain_callback(
    callbacks: dict[str, Any], event_id: str, value: Mapping[str, Any]
) -> None:
    existing = callbacks.get(event_id)
    if existing is not None and existing != value:
        raise FulcrumError(
            "HOOK_CONFLICT",
            "hook callback ID was replayed with different input",
            exit_code=5,
        )
    callbacks[event_id] = copy.deepcopy(dict(value))


def _tool_name(value: Mapping[str, Any]) -> str:
    name = str(value.get("tool_name") or value.get("toolName") or "")
    return name.rsplit("__", 1)[-1]


def _issuing_action_for_actor(
    ledger: Ledger,
    bound: tuple[LedgerRecord, str],
    value: Mapping[str, Any],
) -> tuple[LedgerRecord, dict[str, Any], dict[str, Any]] | None:
    tool_use_id = value.get("tool_use_id") or value.get("toolUseId")
    tool_name = _tool_name(value)
    bound_record, role = bound
    records = [bound_record]
    if bound_record.id == "fc-system":
        records = ledger.list_records(limit=0)
    for record in records:
        protocol = _protocol(record.fc or {})
        for candidate in (protocol.get("actions") or {}).values():
            if (
                not isinstance(candidate, Mapping)
                or candidate.get("state") != "issuing"
                or candidate.get("executor") != role
            ):
                continue
            if str(candidate.get("tool")) != tool_name:
                continue
            attempts = candidate.get("attempts")
            if not isinstance(attempts, list) or not attempts:
                continue
            attempt = attempts[-1]
            if not isinstance(attempt, Mapping):
                continue
            claimed_tool_use = attempt.get("native_tool_use_id")
            if claimed_tool_use and tool_use_id and claimed_tool_use != tool_use_id:
                continue
            return (
                record,
                copy.deepcopy(dict(candidate)),
                copy.deepcopy(dict(attempt)),
            )
    return None


def _arguments_match(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> bool:
    # One-shot task/message prompts include the immutable marker in the issued
    # action. Persistent automation prompts do not, so exact equality also passes.
    for key, value in expected.items():
        observed = actual.get(key)
        if key in {"prompt", "text"} and isinstance(value, str):
            if not isinstance(observed, str) or not observed.endswith(value):
                return False
        elif observed != value:
            return False
    return set(actual).issuperset(expected)


def _replace_attempt(
    protocol: dict[str, Any],
    action_id: str,
    attempt: Mapping[str, Any],
    action_override: Mapping[str, Any] | None = None,
) -> None:
    actions = dict(protocol.get("actions") or {})
    current = actions[action_id]
    action = copy.deepcopy(
        dict(action_override) if action_override is not None else dict(current)
    )
    attempts = list(action.get("attempts") or [])
    attempts[-1] = copy.deepcopy(dict(attempt))
    action["attempts"] = attempts
    actions[action_id] = action
    protocol["actions"] = actions


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    }
