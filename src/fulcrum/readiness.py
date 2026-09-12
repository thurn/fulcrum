"""Deterministic evaluation of the documented infrastructure evidence matrix."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
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


def apply_doctor_evidence(
    value: object, doctor: object, *, observed_at: str | None = None
) -> dict[str, Any]:
    """Overlay live diagnostics on the runtime-dependent gate entries."""

    baseline = evaluate_readiness(value)
    if not isinstance(doctor, dict) or not isinstance(doctor.get("checks"), list):
        raise ReadinessError("doctor report must contain checks")
    checks: dict[str, dict[str, Any]] = {}
    for raw in doctor["checks"]:
        if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
            raise ReadinessError("doctor report contains an invalid check")
        name = cast(str, raw["name"])
        if name in checks:
            raise ReadinessError(f"doctor report contains duplicate check {name}")
        checks[name] = cast(dict[str, Any], raw)

    matrix = deepcopy(value)
    if not isinstance(matrix, dict) or not isinstance(matrix.get("entries"), list):
        raise ReadinessError("readiness matrix requires entries")

    def passed(*names: str) -> bool:
        return all(checks.get(name, {}).get("status") == "pass" for name in names)

    def details(*names: str) -> str:
        return "; ".join(
            f"{name}: {checks.get(name, {}).get('detail', 'missing')}" for name in names
        )

    updates = {
        "human-roles-schedule": (
            (
                "pass"
                if passed("human_role_enrollment", "watchman_hourly_schedule")
                else "fail"
            ),
            details("human_role_enrollment", "watchman_hourly_schedule"),
        ),
        "initial-project-registry": (
            "pass" if passed("initial_project_integrations") else "fail",
            details("initial_project_integrations"),
        ),
        "desktop-hooks": (
            "pass" if passed("desktop_hook_delivery") else "unsupported",
            details("desktop_hook_delivery"),
        ),
        "runtime-visibility": (
            "pass" if passed("runtime_observation") else "unsupported",
            details("runtime_observation"),
        ),
    }
    seen_updates: set[str] = set()
    for raw in matrix["entries"]:
        if not isinstance(raw, dict) or raw.get("id") not in updates:
            continue
        identifier = cast(str, raw["id"])
        status, detail = updates[identifier]
        raw["status"] = status
        raw["detail"] = detail
        evidence = raw.get("evidence")
        if isinstance(evidence, list):
            evidence.append("live fulcrum doctor report")
        seen_updates.add(identifier)
    missing = set(updates) - seen_updates
    if missing:
        raise ReadinessError(
            "readiness matrix is missing runtime entries: " + ", ".join(sorted(missing))
        )
    required_failures = doctor.get("required_failures")
    if not isinstance(required_failures, list):
        raise ReadinessError("doctor report must contain required_failures")
    doctor_ready = doctor.get("ready") is True and not required_failures
    matrix["entries"].append(
        {
            "id": "live-doctor",
            "requirement": "All current required Fulcrum diagnostics pass",
            "required": True,
            "status": "pass" if doctor_ready else "fail",
            "detail": (
                "all required doctor checks pass"
                if doctor_ready
                else "required doctor failures remain"
            ),
            "evidence": ["live fulcrum doctor report"],
        }
    )
    matrix["evaluated_at"] = observed_at or datetime.now(timezone.utc).isoformat()
    result = evaluate_readiness(matrix)
    result["baseline_ready"] = baseline["ready"]
    result["evaluated_at"] = matrix["evaluated_at"]
    return result


def load_with_doctor_evidence(
    matrix_path: Path, doctor_path: Path, *, observed_at: str | None = None
) -> dict[str, Any]:
    try:
        matrix: object = json.loads(matrix_path.read_text(encoding="utf-8"))
        doctor: object = json.loads(doctor_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReadinessError(f"could not read runtime evidence: {error}") from error
    return apply_doctor_evidence(matrix, doctor, observed_at=observed_at)
