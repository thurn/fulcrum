"""Narrow, file-activated provider admission control for live validation.

The production command surface intentionally has no scenario controls.  A live
validation campaign may opt in by placing an exact, base-bound control document
in the instance root.  Unrelated repositories and source contents are ignored.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fulcrum.contracts import FulcrumError, ParsedRequest
from fulcrum.coordination import ProcessLock
from fulcrum.delivery import SourceRef
from fulcrum.ledger import Ledger, LedgerRecord, utc_now

CONTROL_NAME = "scenario-provider-admission.json"


def wait_for_scenario_admission(
    request: ParsedRequest,
    ledger: Ledger,
    work: LedgerRecord,
    source: SourceRef,
) -> dict[str, Any] | None:
    """Wait at an explicitly configured two-candidate provider admission gate."""

    path = request.instance.instance_root / CONTROL_NAME
    document = _read_optional(path)
    if document is None or document.get("enabled") is not True:
        return None
    if document.get("repository_id") != source.work.repository_id:
        return None
    lock_path = path.with_name(f".{path.name}.lock")
    repair = _retain_repair_release(path, lock_path, document, work, source)
    if repair is not None:
        return repair
    label = _candidate_label(document, source)
    if label is None:
        return None
    expected_base = _required_string(document, "expected_base_oid", path)
    parent_oid = _source_parent(source)
    if source.work.base_oid != expected_base or parent_oid != expected_base:
        raise _invalid(
            path,
            "scenario candidates must each be one commit atop the configured base",
            {
                "label": label,
                "expected_base_oid": expected_base,
                "retained_base_oid": source.work.base_oid,
                "source_parent_oid": parent_oid,
            },
        )
    order = _release_order(document, path)
    timeout = _positive_number(document.get("timeout_seconds", 480), path)
    poll = min(_positive_number(document.get("poll_seconds", 0.2), path), 2.0)
    deadline = time.monotonic() + timeout

    while True:
        with ProcessLock(lock_path):
            current = _read_required(path)
            _assert_same_configuration(document, current, path)
            state = dict(current.get("state") or {})
            arrivals = dict(state.get("arrivals") or {})
            retained = arrivals.get(label)
            arrival = {
                "bead_id": work.id,
                "source_oid": source.oid,
                "base_oid": expected_base,
                "source_parent_oid": parent_oid,
            }
            if isinstance(retained, Mapping):
                for key, value in arrival.items():
                    if retained.get(key) != value:
                        raise _invalid(
                            path,
                            f"candidate label {label} was already claimed by another source",
                            {"retained": dict(retained), "requested": arrival},
                        )
                arrival = dict(retained)
            else:
                arrival["arrived_at"] = utc_now()
                arrivals[label] = arrival
                state["arrivals"] = arrivals
                current["state"] = state
                _write(path, current)

            both_arrived = all(item in arrivals for item in order)
            releases = list(state.get("releases") or [])
            released = next(
                (
                    dict(item)
                    for item in releases
                    if isinstance(item, Mapping) and item.get("label") == label
                ),
                None,
            )
            alpha = dict(arrivals.get(order[0]) or {})
            snapshot = {
                "barrier_id": _required_string(current, "id", path),
                "label": label,
                "fixture_path": _required_string(current, "fixture_path", path),
                "expected_base_oid": expected_base,
                "source_parent_oid": parent_oid,
                "arrivals": arrivals,
            }
            if released is not None:
                return {**snapshot, "release": released}
            if both_arrived and label == order[0]:
                release = {
                    "label": label,
                    "bead_id": work.id,
                    "source_oid": source.oid,
                    "released_at": utc_now(),
                    "condition": "both_same_base_candidates_arrived",
                }
                releases.append(release)
                state["releases"] = releases
                current["state"] = state
                _write(path, current)
                return {**snapshot, "release": release}

        if both_arrived and label == order[1]:
            promotion = _observed_promotion(ledger, str(alpha.get("bead_id") or ""))
            if promotion is not None:
                with ProcessLock(lock_path):
                    current = _read_required(path)
                    _assert_same_configuration(document, current, path)
                    state = dict(current.get("state") or {})
                    releases = list(state.get("releases") or [])
                    existing = next(
                        (
                            dict(item)
                            for item in releases
                            if isinstance(item, Mapping) and item.get("label") == label
                        ),
                        None,
                    )
                    if existing is None:
                        existing = {
                            "label": label,
                            "bead_id": work.id,
                            "source_oid": source.oid,
                            "released_at": utc_now(),
                            "condition": "first_candidate_promotion_observed",
                            "first_candidate": {
                                "label": order[0],
                                "bead_id": alpha.get("bead_id"),
                                "source_oid": alpha.get("source_oid"),
                                "promotion": promotion,
                            },
                        }
                        releases.append(existing)
                        state["releases"] = releases
                        current["state"] = state
                        _write(path, current)
                    return {**snapshot, "release": existing}

        if time.monotonic() >= deadline:
            raise FulcrumError(
                "SCENARIO_BARRIER_TIMEOUT",
                "timed out waiting for the configured provider admission condition",
                exit_code=4,
                retryable=False,
                details={
                    "path": str(path),
                    "label": label,
                    "arrivals": arrivals,
                    "release_order": order,
                },
            )
        time.sleep(poll)


def _retain_repair_release(
    path: Path,
    lock_path: Path,
    initial: Mapping[str, Any],
    work: LedgerRecord,
    source: SourceRef,
) -> dict[str, Any] | None:
    """Do not reapply the initial same-base gate to a provider-requested repair."""

    with ProcessLock(lock_path):
        current = _read_required(path)
        _assert_same_configuration(initial, current, path)
        state = dict(current.get("state") or {})
        arrivals = dict(state.get("arrivals") or {})
        releases = list(state.get("releases") or [])
        matched: tuple[str, dict[str, Any], dict[str, Any]] | None = None
        for label, raw_arrival in arrivals.items():
            if not isinstance(label, str) or not isinstance(raw_arrival, Mapping):
                continue
            arrival = dict(raw_arrival)
            release = next(
                (
                    dict(item)
                    for item in releases
                    if isinstance(item, Mapping) and item.get("label") == label
                ),
                None,
            )
            if (
                arrival.get("bead_id") == work.id
                and arrival.get("source_oid") != source.oid
                and release is not None
            ):
                matched = (label, arrival, release)
                break
        if matched is None:
            return None
        label, arrival, initial_release = matched
        expected_repair = current.get("repair_line")
        if isinstance(expected_repair, str):
            observed_repair = _fixture_lines(current, source)
            if observed_repair != [expected_repair]:
                raise FulcrumError(
                    "SCENARIO_REPAIR_INCOMPLETE",
                    "the provider-conflict repair must preserve both candidate outcomes",
                    exit_code=4,
                    retryable=False,
                    details={
                        "path": str(path),
                        "label": label,
                        "fixture_path": _required_string(current, "fixture_path", path),
                        "expected_line": expected_repair,
                        "observed_lines": observed_repair,
                        "next_action": (
                            "Read the promoted release and original candidate with git show, "
                            "write the ordered union, create one commit atop the retained base, "
                            "and submit that new source."
                        ),
                    },
                )
        repairs = list(state.get("repairs") or [])
        repair = next(
            (
                dict(item)
                for item in repairs
                if isinstance(item, Mapping)
                and item.get("bead_id") == work.id
                and item.get("source_oid") == source.oid
            ),
            None,
        )
        if repair is None:
            repair = {
                "label": label,
                "bead_id": work.id,
                "source_oid": source.oid,
                "released_at": utc_now(),
                "condition": "initial_candidate_already_released_for_provider_repair",
            }
            repairs.append(repair)
            state["repairs"] = repairs
            current["state"] = state
            _write(path, current)
        return {
            "barrier_id": _required_string(current, "id", path),
            "label": label,
            "fixture_path": _required_string(current, "fixture_path", path),
            "expected_base_oid": _required_string(current, "expected_base_oid", path),
            "initial_arrival": arrival,
            "initial_release": initial_release,
            "release": repair,
        }


def _candidate_label(document: Mapping[str, Any], source: SourceRef) -> str | None:
    lines = _fixture_lines(document, source)
    candidates = document.get("candidate_lines")
    if not isinstance(candidates, Mapping):
        raise _invalid(Path(CONTROL_NAME), "candidate_lines must be a mapping")
    for label, expected in candidates.items():
        if isinstance(label, str) and isinstance(expected, str) and lines == [expected]:
            return label
    return None


def _fixture_lines(document: Mapping[str, Any], source: SourceRef) -> list[str]:
    path = Path(_required_string(document, "fixture_path", Path(CONTROL_NAME)))
    if path.is_absolute() or ".." in path.parts:
        raise _invalid(
            Path(CONTROL_NAME),
            "fixture_path must be a repository-relative path without parent traversal",
        )
    content = _git(source, "show", f"{source.oid}:{path.as_posix()}")
    return content.splitlines()


def _source_parent(source: SourceRef) -> str:
    return _git(source, "rev-parse", f"{source.oid}^").strip()


def _git(source: SourceRef, *arguments: str) -> str:
    workspace = Path(source.work.actual_path or source.work.intended_path)
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), *arguments],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise FulcrumError(
            "SCENARIO_BARRIER_UNAVAILABLE",
            f"could not inspect the scenario candidate: {error}",
            exit_code=4,
            retryable=True,
        ) from error
    if result.returncode != 0:
        raise FulcrumError(
            "SCENARIO_BARRIER_UNAVAILABLE",
            "Git could not inspect the scenario candidate",
            exit_code=4,
            retryable=True,
            details={"arguments": list(arguments), "stderr": result.stderr[-4000:]},
        )
    return result.stdout


def _observed_promotion(ledger: Ledger, bead_id: str) -> dict[str, Any] | None:
    if not bead_id:
        return None
    record = ledger.show(bead_id)
    delivery = (record.fc or {}).get("delivery") if record is not None else None
    promotion = delivery.get("promotion") if isinstance(delivery, Mapping) else None
    if isinstance(promotion, Mapping) and promotion.get("state") == "observed":
        return dict(promotion)
    return None


def _release_order(document: Mapping[str, Any], path: Path) -> tuple[str, str]:
    raw = document.get("release_order")
    candidates = document.get("candidate_lines")
    if (
        not isinstance(raw, list)
        or len(raw) != 2
        or len(set(raw)) != 2
        or not isinstance(candidates, Mapping)
        or any(not isinstance(item, str) or item not in candidates for item in raw)
    ):
        raise _invalid(path, "release_order must name exactly two distinct candidates")
    return raw[0], raw[1]


def _positive_number(value: Any, path: Path) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise _invalid(path, "barrier timing values must be positive numbers")
    return float(value)


def _assert_same_configuration(
    initial: Mapping[str, Any], current: Mapping[str, Any], path: Path
) -> None:
    for key in (
        "id",
        "enabled",
        "repository_id",
        "fixture_path",
        "expected_base_oid",
        "candidate_lines",
        "repair_line",
        "release_order",
    ):
        if current.get(key) != initial.get(key):
            raise _invalid(
                path, "barrier configuration changed while candidates waited"
            )


def _required_string(document: Mapping[str, Any], key: str, path: Path) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value:
        raise _invalid(path, f"{key} must be a nonempty string")
    return value


def _read_optional(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return _read_required(path)


def _read_required(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise _invalid(
            path, f"cannot read barrier control document: {error}"
        ) from error
    if not isinstance(value, dict):
        raise _invalid(path, "barrier control document must be a JSON object")
    return value


def _write(path: Path, document: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _invalid(
    path: Path, message: str, details: Mapping[str, Any] | None = None
) -> FulcrumError:
    return FulcrumError(
        "SCENARIO_BARRIER_INVALID",
        message,
        exit_code=4,
        retryable=False,
        details={"path": str(path), **dict(details or {})},
    )
