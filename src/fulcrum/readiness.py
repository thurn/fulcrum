"""Deterministic evaluation of the documented infrastructure evidence matrix."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, TypedDict, cast


class ReadinessEntry(TypedDict):
    id: str
    requirement: str
    required: bool
    status: Literal["pass", "fail", "unsupported"]
    detail: str
    evidence: list[str]


class ReadinessError(ValueError):
    """The evidence matrix cannot support a readiness decision."""


def evaluate_readiness(value: object) -> dict[str, Any]:
    """Validate a matrix and fail closed on every required non-pass result."""

    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ReadinessError("readiness matrix must use schema_version 1")
    entries = value.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ReadinessError("readiness matrix requires at least one entry")
    checked: list[ReadinessEntry] = []
    seen: set[str] = set()
    for index, raw in enumerate(entries):
        if not isinstance(raw, dict):
            raise ReadinessError(f"entry {index} must be an object")
        identifier = raw.get("id")
        requirement = raw.get("requirement")
        required = raw.get("required")
        status = raw.get("status")
        detail = raw.get("detail")
        evidence = raw.get("evidence")
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise ReadinessError(f"entry {index} has a missing or duplicate id")
        if not isinstance(requirement, str) or not requirement:
            raise ReadinessError(f"entry {identifier} has no requirement")
        if not isinstance(required, bool):
            raise ReadinessError(f"entry {identifier} must declare required")
        if status not in {"pass", "fail", "unsupported"}:
            raise ReadinessError(f"entry {identifier} has invalid status")
        if required and status == "unsupported":
            raise ReadinessError(
                f"required entry {identifier} cannot be marked unsupported"
            )
        if not isinstance(detail, str) or not detail:
            raise ReadinessError(f"entry {identifier} has no detail")
        if (
            not isinstance(evidence, list)
            or not evidence
            or any(not isinstance(item, str) or not item for item in evidence)
        ):
            raise ReadinessError(f"entry {identifier} requires evidence references")
        seen.add(identifier)
        checked.append(cast(ReadinessEntry, raw))
    blockers = [
        entry for entry in checked if entry["required"] and entry["status"] != "pass"
    ]
    return {
        "ready": not blockers,
        "blockers": blockers,
        "unsupported_optional": [
            entry
            for entry in checked
            if not entry["required"] and entry["status"] == "unsupported"
        ],
        "entries": checked,
    }


def load_and_evaluate(path: Path) -> dict[str, Any]:
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReadinessError(
            f"could not read readiness matrix {path}: {error}"
        ) from error
    return evaluate_readiness(value)
