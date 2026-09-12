"""Bounded resource observations and verified pause/resume helpers."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, NotRequired, TypedDict

from fulcrum.records import Hold, HoldException, HoldsJobsRecord

MAX_COMMAND_OUTPUT = 256 * 1024
COMMAND_TIMEOUT_SECONDS = 3.0


class CommandResult(TypedDict):
    available: bool
    returncode: int | None
    stdout: str
    error: str | None
    truncated: bool


class Measurement(TypedDict):
    available: bool
    value: float | int | None
    unit: str
    error: str | None


class ProcessObservation(TypedDict):
    pid: int
    parent_pid: int
    cpu_percent: float
    rss_bytes: int
    command: str
    owned: bool


class ProcessSummary(TypedDict):
    available: bool
    processes: list[ProcessObservation]
    error: str | None
    truncated: bool


class TollgateSummary(TypedDict):
    available: bool
    repository_id: str | None
    execution_state: str | None
    queue_items: int | None
    active_runs: int | None
    queued_runs: int | None
    error: str | None
    truncated: bool


class ResourceObservation(TypedDict):
    observed_at: str
    cpu_load_1m: Measurement
    logical_cpus: Measurement
    memory_free_percent: Measurement
    relevant_processes: ProcessSummary
    tollgate: TollgateSummary


class CheckpointReport(TypedDict):
    assignment_id: str
    requested_at: str
    recorded_at: str
    inventory_due_at: str
    preservation_due_at: str
    dirty_paths: list[str]
    retained_commits: list[str]
    candidate_id: str | None
    candidate_validated: bool
    candidate_reconciled: bool
    intended_changes_preserved: bool
    owned_process_ids: list[int]
    notes: str


class QuietReport(TypedDict):
    quiet: bool
    reasons: list[str]
    live_process_ids: list[int]


class ResumeReport(TypedDict):
    resumable: bool
    reasons: list[str]


RunCommand = Callable[[Sequence[str], float, int], CommandResult]


def utc_text(value: datetime) -> str:
    """Return a stable UTC timestamp."""

    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def run_bounded(
    command: Sequence[str],
    timeout: float = COMMAND_TIMEOUT_SECONDS,
    output_limit: int = MAX_COMMAND_OUTPUT,
) -> CommandResult:
    """Run a diagnostic command with a hard duration and retained-output bound."""

    try:
        completed = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as error:
        return {
            "available": False,
            "returncode": None,
            "stdout": "",
            "error": str(error),
            "truncated": False,
        }
    combined = completed.stdout
    if completed.returncode != 0 and not combined:
        combined = completed.stderr
    encoded = combined.encode("utf-8", errors="replace")
    truncated = len(encoded) > output_limit
    if truncated:
        combined = encoded[:output_limit].decode("utf-8", errors="ignore")
    return {
        "available": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": combined,
        "error": None if completed.returncode == 0 else f"exit {completed.returncode}",
        "truncated": truncated,
    }


def _memory_pressure(run: RunCommand) -> Measurement:
    result = run(["memory_pressure", "-Q"], COMMAND_TIMEOUT_SECONDS, MAX_COMMAND_OUTPUT)
    if result["available"]:
        match = re.search(
            r"free percentage:\s*([0-9]+(?:\.[0-9]+)?)%", result["stdout"]
        )
        if match:
            return {
                "available": True,
                "value": float(match.group(1)),
                "unit": "percent",
                "error": None,
            }
    return {
        "available": False,
        "value": None,
        "unit": "percent",
        "error": result["error"] or "memory pressure output was not recognized",
    }


def _processes(run: RunCommand, owned_pids: set[int]) -> ProcessSummary:
    result = run(
        ["ps", "-axo", "pid=,ppid=,pcpu=,rss=,command="],
        COMMAND_TIMEOUT_SECONDS,
        MAX_COMMAND_OUTPUT,
    )
    if not result["available"]:
        return {
            "available": False,
            "processes": [],
            "error": result["error"] or "process observation failed",
            "truncated": result["truncated"],
        }
    processes: list[ProcessObservation] = []
    for line in result["stdout"].splitlines():
        parts = line.strip().split(None, 4)
        if len(parts) != 5:
            continue
        try:
            pid, parent_pid = int(parts[0]), int(parts[1])
            cpu_percent, rss_bytes = float(parts[2]), int(parts[3]) * 1024
        except ValueError:
            continue
        command = parts[4]
        if pid not in owned_pids and not _relevant_command(command):
            continue
        processes.append(
            {
                "pid": pid,
                "parent_pid": parent_pid,
                "cpu_percent": cpu_percent,
                "rss_bytes": rss_bytes,
                "command": command[:1024],
                "owned": pid in owned_pids,
            }
        )
    return {
        "available": True,
        "processes": processes,
        "error": None,
        "truncated": result["truncated"],
    }


def _relevant_command(command: str) -> bool:
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    names = [Path(word).name.lower() for word in words if not word.startswith("-")]
    for name in names:
        if name == "tg" or name.startswith("tollgate"):
            return True
        if name in {"pytest", "pyre", "black", "scripts/check"}:
            return True
        if name.startswith(("cargo", "xcodebuild", "gradle")):
            return True
    return False


def _tollgate(run: RunCommand, repository_id: str | None) -> TollgateSummary:
    prefix = ["tg", "--no-launch"]
    if repository_id is not None:
        prefix.extend(["--repository", repository_id])
    queue_result = run(
        [*prefix, "--json", "queue"], COMMAND_TIMEOUT_SECONDS, MAX_COMMAND_OUTPUT
    )
    slot_result = run(
        [*prefix, "--json", "slot", "list"],
        COMMAND_TIMEOUT_SECONDS,
        MAX_COMMAND_OUTPUT,
    )
    if not queue_result["available"] or not slot_result["available"]:
        return {
            "available": False,
            "repository_id": repository_id,
            "execution_state": None,
            "queue_items": None,
            "active_runs": None,
            "queued_runs": None,
            "error": queue_result["error"]
            or slot_result["error"]
            or "Tollgate observation failed",
            "truncated": queue_result["truncated"] or slot_result["truncated"],
        }
    try:
        queue = json.loads(queue_result["stdout"])
        slots = json.loads(slot_result["stdout"])
        if not isinstance(queue, list) or not isinstance(slots, list):
            raise ValueError("missing queue array")
        active_runs = sum(
            isinstance(slot, dict) and slot.get("state") not in {"idle", "unhealthy"}
            for slot in slots
        )
        return {
            "available": True,
            "repository_id": repository_id,
            "execution_state": None,
            "queue_items": len(queue),
            "active_runs": active_runs,
            "queued_runs": len(queue),
            "error": None,
            "truncated": queue_result["truncated"] or slot_result["truncated"],
        }
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        return {
            "available": False,
            "repository_id": repository_id,
            "execution_state": None,
            "queue_items": None,
            "active_runs": None,
            "queued_runs": None,
            "error": f"invalid Tollgate status: {error}",
            "truncated": queue_result["truncated"] or slot_result["truncated"],
        }


def collect_resources(
    *,
    repository_id: str | None = None,
    owned_pids: Iterable[int] = (),
    now: datetime | None = None,
    run: RunCommand = run_bounded,
) -> ResourceObservation:
    """Collect local-only host/process facts without making scheduling decisions."""

    observed = datetime.now(timezone.utc) if now is None else now
    cpu_load: Measurement
    try:
        load = os.getloadavg()[0]
        cpu_load = {
            "available": True,
            "value": load,
            "unit": "load",
            "error": None,
        }
    except OSError as error:
        cpu_load = {
            "available": False,
            "value": None,
            "unit": "load",
            "error": str(error),
        }
    cpu_count = os.cpu_count()
    logical_cpus: Measurement = {
        "available": cpu_count is not None,
        "value": cpu_count,
        "unit": "count",
        "error": None if cpu_count is not None else "logical CPU count unavailable",
    }
    return {
        "observed_at": utc_text(observed),
        "cpu_load_1m": cpu_load,
        "logical_cpus": logical_cpus,
        "memory_free_percent": _memory_pressure(run),
        "relevant_processes": _processes(run, set(owned_pids)),
        "tollgate": _tollgate(run, repository_id),
    }


def hold_scope(kind: str, identifier: str | None = None) -> str:
    """Build one of the supported fleet/host/project/plan/assignment scopes."""

    if kind == "fleet" and identifier is None:
        return "fleet"
    if kind not in {"host", "project", "plan", "assignment"} or not identifier:
        raise ValueError(
            "hold scope must be fleet or a named host/project/plan/assignment"
        )
    if ":" in identifier or not re.fullmatch(r"[A-Za-z0-9._-]+", identifier):
        raise ValueError(f"invalid hold scope identifier {identifier!r}")
    return f"{kind}:{identifier}"


def new_hold(
    hold_id: str,
    *,
    scope: str,
    reason: str,
    release_condition: str,
    owner_id: str,
    release_mode: Literal["human", "evidence"],
    exceptions: Iterable[tuple[str, Iterable[str]]] = (),
) -> Hold:
    """Create an active hold with explicit ownership and recovery boundaries."""

    if not hold_id or not reason or not release_condition or not owner_id:
        raise ValueError(
            "hold identity, reason, release condition, and owner are required"
        )
    if scope != "fleet":
        kind, separator, identifier = scope.partition(":")
        if not separator or hold_scope(kind, identifier) != scope:
            raise ValueError(f"unsupported hold scope {scope!r}")
    structured: list[HoldException] = []
    for recovery_scope, resources in exceptions:
        structured.append(
            {
                "recovery_scope": recovery_scope,
                "permitted_resources": sorted(set(resources)),
            }
        )
    if any(not item["recovery_scope"] for item in structured):
        raise ValueError("recovery exception scope must not be empty")
    return {
        "hold_id": hold_id,
        "scope": scope,
        "reason": reason,
        "release_condition": release_condition,
        "permitted_exceptions": [item["recovery_scope"] for item in structured],
        "owner_id": owner_id,
        "release_mode": release_mode,
        "exceptions": structured,
        "released_at": None,
        "released_by": None,
        "release_evidence": None,
    }


def active_holds(record: HoldsJobsRecord, scopes: Iterable[str]) -> list[Hold]:
    """Return every unreleased matching hold; independent holds always compose."""

    selected = set(scopes)
    return [
        hold
        for hold in record["holds"]
        if hold["scope"] in selected and hold.get("released_at") is None
    ]


def hold_blocks(
    hold: Hold,
    *,
    scopes: Iterable[str],
    recovery_scope: str | None = None,
    requested_resources: Iterable[str] = (),
) -> bool:
    """Apply an active hold unless one structured recovery exception covers it."""

    if hold["scope"] not in set(scopes) or hold.get("released_at") is not None:
        return False
    requested = set(requested_resources)
    if recovery_scope is not None:
        for exception in hold.get("exceptions", []):
            if exception["recovery_scope"] == recovery_scope and requested.issubset(
                set(exception["permitted_resources"])
            ):
                return False
    return True


def release_hold(
    record: HoldsJobsRecord,
    hold_id: str,
    *,
    released_at: str,
    actor_id: str,
    evidence: str | None,
) -> HoldsJobsRecord:
    """Explicitly release exactly one hold while preserving overlapping holds."""

    found = False
    holds: list[Hold] = []
    for hold in record["holds"]:
        updated = Hold(**hold)
        if hold["hold_id"] == hold_id:
            found = True
            if hold.get("released_at") is not None:
                raise ValueError(f"hold {hold_id!r} is already released")
            if hold.get("release_mode") == "human" and hold.get("owner_id") != actor_id:
                raise ValueError("human hold requires explicit release by its owner")
            if hold.get("release_mode") == "evidence" and not evidence:
                raise ValueError("evidence-released hold requires release evidence")
            updated["released_at"] = released_at
            updated["released_by"] = actor_id
            updated["release_evidence"] = evidence
        holds.append(updated)
    if not found:
        raise ValueError(f"unknown hold {hold_id!r}")
    result = HoldsJobsRecord(**record)
    result["holds"] = holds
    result["updated_at"] = released_at
    return result


def checkpoint_report(
    assignment_id: str,
    *,
    requested_at: datetime,
    recorded_at: datetime,
    dirty_paths: Iterable[str] = (),
    retained_commits: Iterable[str] = (),
    candidate_id: str | None = None,
    candidate_validated: bool = False,
    candidate_reconciled: bool = False,
    intended_changes_preserved: bool = False,
    owned_process_ids: Iterable[int] = (),
    notes: str = "",
) -> CheckpointReport:
    """Record bounded pause evidence without turning a checkpoint into a candidate."""

    return {
        "assignment_id": assignment_id,
        "requested_at": utc_text(requested_at),
        "recorded_at": utc_text(recorded_at),
        "inventory_due_at": utc_text(requested_at + timedelta(minutes=5)),
        "preservation_due_at": utc_text(requested_at + timedelta(minutes=10)),
        "dirty_paths": sorted(set(dirty_paths)),
        "retained_commits": sorted(set(retained_commits)),
        "candidate_id": candidate_id,
        "candidate_validated": candidate_validated,
        "candidate_reconciled": candidate_reconciled,
        "intended_changes_preserved": intended_changes_preserved,
        "owned_process_ids": sorted(set(owned_process_ids)),
        "notes": notes,
    }


def checkpoint_is_candidate(checkpoint: CheckpointReport) -> bool:
    """Only an explicitly validated candidate may be described as one."""

    return checkpoint["candidate_id"] is not None and checkpoint["candidate_validated"]


def quiet_report(observation: ResourceObservation) -> QuietReport:
    """Require observed process exit and Tollgate drainage before declaring quiet."""

    reasons: list[str] = []
    process_summary = observation["relevant_processes"]
    live = [process["pid"] for process in process_summary["processes"]]
    if not process_summary["available"]:
        reasons.append("relevant process state is unavailable")
    elif live:
        reasons.append("relevant processes are still running")
    tollgate = observation["tollgate"]
    if not tollgate["available"]:
        reasons.append("Tollgate drainage state is unavailable")
    elif any(
        value is None or value > 0
        for value in (
            tollgate["queue_items"],
            tollgate["active_runs"],
            tollgate["queued_runs"],
        )
    ):
        reasons.append("Tollgate work has not drained")
    return {"quiet": not reasons, "reasons": reasons, "live_process_ids": live}


def resume_report(
    *,
    applicable: Sequence[Hold],
    worktree_matches: bool | None,
    contract_matches: bool | None,
    candidate_reconciled: bool | None,
    base_matches: bool | None,
) -> ResumeReport:
    """Explain all required identity and hold checks before resuming."""

    reasons: list[str] = []
    if applicable:
        reasons.append("applicable holds remain active")
    for value, label in (
        (worktree_matches, "worktree identity"),
        (contract_matches, "assignment contract"),
        (candidate_reconciled, "candidate state"),
        (base_matches, "certified base"),
    ):
        if value is not True:
            reasons.append(f"{label} is {'unknown' if value is None else 'mismatched'}")
    return {"resumable": not reasons, "reasons": reasons}
