"""Narrow, file-activated native-action faults for live validation.

The production command surface intentionally exposes no fault controls.  A live
validation campaign may opt in by placing an exact project/native-action-bound control
document in the instance root.  The one-shot document records the injected
boundary so provider truth and recovery timing remain auditable.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fulcrum.contracts import ParsedRequest
from fulcrum.coordination import ProcessLock
from fulcrum.ledger import utc_now

CONTROL_NAME = "scenario-native-action-fault.json"


def inject_claim_fault(
    request: ParsedRequest,
    *,
    record_id: str,
    action: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Return one definite-not-created fault before native invocation."""

    return _trigger(
        request,
        record_id=record_id,
        action=action,
        mode="definitely_not_created",
        provider_truth={"state": "definitely_not_created", "invoked": False},
    )


def inject_result_fault(
    request: ParsedRequest,
    *,
    record_id: str,
    action: Mapping[str, Any],
    outcome: str,
    native_result: Any,
) -> tuple[str, Any, dict[str, Any] | None]:
    """Convert one successful creation reply into a retained uncertainty."""

    if outcome != "succeeded":
        return outcome, native_result, None
    locator = _locator(native_result)
    fault = _trigger(
        request,
        record_id=record_id,
        action=action,
        mode="created_response_lost",
        provider_truth={
            "state": "created_response_lost",
            "invoked": True,
            "possible_task_locator": locator,
        },
    )
    if fault is None:
        return outcome, native_result, None
    obscured = {
        "responseLost": True,
        "possibleTaskLocator": locator,
        "scenarioFault": fault,
    }
    return "uncertain", obscured, fault


def record_alert_delivered(
    request: ParsedRequest, *, action: Mapping[str, Any]
) -> None:
    purpose = str(action.get("purpose") or "")
    if not purpose.startswith("incident_alert:"):
        return
    reporting = action.get("reporting")
    recovery_action_id = (
        reporting.get("recovery_action_id") if isinstance(reporting, Mapping) else None
    )
    _update_matching_state(
        request,
        predicate=lambda state: state.get("action_id") == recovery_action_id,
        changes={"alert_delivered_at": utc_now()},
    )


def record_steward_interruption(request: ParsedRequest, *, task_id: str) -> None:
    _update_matching_state(
        request,
        predicate=lambda state: state.get("steward_task_id") == task_id,
        changes={
            "interrupted_at": utc_now(),
            "hold_reconciliation": False,
        },
    )


def reconciliation_held(request: ParsedRequest, *, action: Mapping[str, Any]) -> bool:
    document = _read_optional(request.instance.instance_root / CONTROL_NAME)
    state = document.get("state") if isinstance(document, Mapping) else None
    return bool(
        isinstance(document, Mapping)
        and document.get("enabled") is True
        and isinstance(state, Mapping)
        and state.get("action_id") == action.get("action_id")
        and state.get("hold_reconciliation") is True
        and not state.get("interrupted_at")
    )


def _trigger(
    request: ParsedRequest,
    *,
    record_id: str,
    action: Mapping[str, Any],
    mode: str,
    provider_truth: Mapping[str, Any],
) -> dict[str, Any] | None:
    path = request.instance.instance_root / CONTROL_NAME
    initial = _read_optional(path)
    if not _matches(initial, record_id=record_id, action=action, mode=mode):
        return None
    with ProcessLock(path.with_name(f".{path.name}.lock")):
        document = _read_optional(path)
        if not _matches(document, record_id=record_id, action=action, mode=mode):
            return None
        if document is None:
            return None
        state = document.get("state")
        if isinstance(state, Mapping) and state.get("action_id"):
            return None
        fault = {
            "control_id": document.get("id"),
            "mode": mode,
            "provider_truth": dict(provider_truth),
        }
        document["state"] = {
            "action_id": action.get("action_id"),
            "record_id": record_id,
            "steward_task_id": request.actor.task_id or request.thread_id,
            "triggered_at": utc_now(),
            "provider_truth": dict(provider_truth),
            "hold_reconciliation": True,
        }
        _write(path, document)
        return fault


def _matches(
    document: Any,
    *,
    record_id: str,
    action: Mapping[str, Any],
    mode: str,
) -> bool:
    if not isinstance(document, Mapping) or document.get("enabled") is not True:
        return False
    if document.get("mode") != mode:
        return False
    if document.get("record_id") not in {None, record_id}:
        return False
    if (
        action.get("tool") != "create_thread"
        or action.get("purpose") != "routine_dispatch"
    ):
        return False
    arguments = action.get("arguments")
    if not isinstance(arguments, Mapping):
        return False
    target = arguments.get("target")
    project_id = target.get("projectId") if isinstance(target, Mapping) else None
    if document.get("project_id") != project_id:
        return False
    criteria: list[bool] = []
    prompt_contains = document.get("prompt_contains")
    if isinstance(prompt_contains, str) and prompt_contains:
        prompt = arguments.get("prompt")
        criteria.append(isinstance(prompt, str) and prompt_contains in prompt)
    title_contains = document.get("title_contains")
    if isinstance(title_contains, str) and title_contains:
        title = arguments.get("title")
        criteria.append(isinstance(title, str) and title_contains in title)
    return bool(criteria) and all(criteria)


def _update_matching_state(
    request: ParsedRequest,
    *,
    predicate: Any,
    changes: Mapping[str, Any],
) -> None:
    path = request.instance.instance_root / CONTROL_NAME
    if not path.exists():
        return
    with ProcessLock(path.with_name(f".{path.name}.lock")):
        document = _read_optional(path)
        state = document.get("state") if isinstance(document, Mapping) else None
        if not isinstance(state, Mapping) or not predicate(state):
            return
        if document is None:
            return
        document["state"] = {**dict(state), **dict(changes)}
        _write(path, document)


def _locator(native_result: Any) -> dict[str, str] | None:
    if not isinstance(native_result, Mapping):
        return None
    for key in ("threadId", "thread_id", "id"):
        value = native_result.get(key)
        if isinstance(value, str) and value:
            return {"threadId": value}
    for key in ("clientThreadId", "client_thread_id"):
        value = native_result.get(key)
        if isinstance(value, str) and value:
            return {"clientThreadId": value}
    content = native_result.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, Mapping):
                continue
            text = item.get("text")
            if not isinstance(text, str):
                continue
            try:
                nested = json.loads(text)
            except json.JSONDecodeError:
                continue
            locator = _locator(nested)
            if locator is not None:
                return locator
    return None


def _read_optional(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return dict(value) if isinstance(value, Mapping) else None


def _write(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)
