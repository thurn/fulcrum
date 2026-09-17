"""Trusted Codex command-hook handling and scoped transcript collection."""

from __future__ import annotations

import copy
import json
import os
import re
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fulcrum.configuration import ConfigurationManager
from fulcrum.contracts import CommandResult, FulcrumError, ParsedRequest
from fulcrum.coordination import coordinated
from fulcrum.desktop_protocol import _protocol, _with_protocol
from fulcrum.diagnostics import DiagnosticLog
from fulcrum.ledger import Ledger, LedgerRecord
from fulcrum.observations import read_transcript

HOOK_EVENTS = {
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Stop",
    "Interrupt",
}
MARKER: re.Pattern[str] = re.compile(
    r"^Fulcrum-Action: (?P<value>\{[^\n]+\})", re.MULTILINE
)


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
        executable: str | None = None
        try:
            manager = ConfigurationManager(request.instance.config_path)
            document, _ = manager.load()
            configured = manager.effective(document)["beads"].get("executable")
            executable = str(configured) if configured else None
        except FulcrumError:
            pass
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
            response = self._prompt(ledger, request, marker, bound)
        elif event_name == "PreToolUse":
            response = self._pre_tool(ledger, request, bound)
        elif event_name == "PostToolUse":
            self._post_tool(ledger, request, bound)
        elif bound is not None:
            self._lifecycle(ledger, request, bound, event_name)
        if bound is not None:
            self._collect_transcript(ledger, request, bound)
        self._log(request, event_name, task_id, response)
        return CommandResult.query(response)

    def _prompt(
        self,
        ledger: Ledger,
        request: ParsedRequest,
        marker: Mapping[str, Any],
        bound: tuple[LedgerRecord, str] | None,
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
            "task_id": request.actor.task_id or request.thread_id,
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
        record, _ = bound
        protocol = _protocol(record.fc or {})
        action, attempt = _issuing_action(protocol, request.input)
        if action is None or attempt is None:
            return _deny("native effect has no matching claimed Fulcrum action")
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
        record, _ = bound
        protocol = _protocol(record.fc or {})
        action, attempt = _issuing_action(protocol, request.input)
        if action is None or attempt is None:
            return
        result = request.input.get("tool_response") or request.input.get("toolResponse")
        attempt["post_hook_event_id"] = _event_id(request.input)
        attempt["hook_result"] = copy.deepcopy(result)
        if isinstance(result, Mapping) and isinstance(result.get("isError"), bool):
            outcome = "rejected" if result["isError"] else "succeeded"
            attempt["state"] = outcome
            attempt["outcome"] = outcome
            action["state"] = outcome
        _replace_attempt(protocol, str(action["action_id"]), attempt, action)
        ledger.update_fc(record.id, _with_protocol(record.fc or {}, protocol))

    def _lifecycle(
        self,
        ledger: Ledger,
        request: ParsedRequest,
        bound: tuple[LedgerRecord, str],
        event_name: str,
    ) -> None:
        record, _ = bound
        protocol = _protocol(record.fc or {})
        events = list(protocol.get("hook_events") or [])
        event = {
            "event_id": _event_id(request.input),
            "event": event_name,
            "session_id": request.input.get("session_id"),
            "turn_id": request.input.get("turn_id"),
            "reason": request.input.get("reason"),
        }
        if not any(item.get("event_id") == event["event_id"] for item in events):
            events.append(event)
            protocol["hook_events"] = events[-128:]
            ledger.update_fc(record.id, _with_protocol(record.fc or {}, protocol))

    def _collect_transcript(
        self,
        ledger: Ledger,
        request: ParsedRequest,
        bound: tuple[LedgerRecord, str],
    ) -> None:
        transcript = request.input.get("transcript_path") or request.input.get(
            "transcriptPath"
        )
        if not isinstance(transcript, str) or not Path(transcript).is_absolute():
            return
        record, _ = bound
        protocol = _protocol(record.fc or {})
        current = protocol.get("transcript")
        cursor = int(current.get("cursor", 0)) if isinstance(current, Mapping) else 0
        page = read_transcript(Path(transcript), cursor)
        observations = dict(protocol.get("observations") or {})
        lifecycle = dict(observations.get("lifecycle") or {})
        usage = dict(observations.get("usage") or {})
        for item in page.lifecycle:
            identity = item.get("event_id")
            if identity:
                lifecycle[str(identity)] = dict(item)
        for item in page.usage:
            identity = item.get("response_id") or item.get("event_id")
            if identity:
                usage[str(identity)] = dict(item)
        observations["lifecycle"] = lifecycle
        observations["usage"] = usage
        protocol["observations"] = observations
        protocol["transcript"] = {
            "path": transcript,
            "cursor": page.cursor,
            "gaps": [dict(item) for item in page.gaps][-20:],
        }
        ledger.update_fc(record.id, _with_protocol(record.fc or {}, protocol))

    def _log(
        self,
        request: ParsedRequest,
        event_name: str,
        task_id: str,
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
        assignment = _protocol(record.fc or {}).get("assignment")
        if isinstance(assignment, Mapping) and assignment.get("task_id") == task_id:
            return record, str(assignment.get("role") or "worker")
    return None


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


def _issuing_action(
    protocol: Mapping[str, Any], value: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    tool_use_id = value.get("tool_use_id") or value.get("toolUseId")
    tool_name = str(value.get("tool_name") or value.get("toolName") or "")
    tool_name = tool_name.rsplit("__", 1)[-1]
    for candidate in (protocol.get("actions") or {}).values():
        if not isinstance(candidate, Mapping) or candidate.get("state") != "issuing":
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
        return copy.deepcopy(dict(candidate)), copy.deepcopy(dict(attempt))
    return None, None


def _arguments_match(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> bool:
    # Prompts/text include the immutable marker in the issued action; compare all
    # other fields exactly and require the retained prompt as the suffix.
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
