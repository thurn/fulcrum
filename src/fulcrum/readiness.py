"""Shared readiness rules for setup, the controller, and diagnostics."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fulcrum.kernel import invariant_violations
from fulcrum.operative import journal_is_unfinished, read_journal
from fulcrum.store import Store, StoreError


def state_readiness(
    store: Store,
    *,
    include_operative_fence: bool = True,
    operative_journal: Path | None = None,
) -> tuple[bool, list[str]]:
    """Return whether durable controller state can safely enable dispatch."""

    reasons: list[str] = []
    if include_operative_fence:
        journal_path = operative_journal or store.operative_journal_path
        journal_fenced = False
        if journal_path is not None:
            try:
                journal_fenced = journal_is_unfinished(read_journal(journal_path))
            except StoreError:
                journal_fenced = True
        if journal_fenced or store.unfinished_operative_takeover() is not None:
            reasons.append("operative takeover active")
    global_limit = store.row("SELECT value FROM meta WHERE key = 'global_limit'")
    if global_limit is None or not _positive_integer(global_limit["value"]):
        reasons.append("global capacity is missing")

    projects = store.rows("SELECT * FROM projects ORDER BY project_id")
    if not projects:
        reasons.append("no projects are enrolled")
    unavailable = [row["project_id"] for row in projects if not row["enabled"]]
    if unavailable:
        reasons.append("project integration is unavailable: " + ", ".join(unavailable))

    project_limits_row = store.row(
        "SELECT value FROM meta WHERE key = 'project_limits'"
    )
    project_limits: Any = None
    if project_limits_row is not None:
        try:
            project_limits = json.loads(project_limits_row["value"])
        except (TypeError, json.JSONDecodeError):
            project_limits = None
    missing_limits = [
        row["project_id"]
        for row in projects
        if row["enabled"]
        and (
            not isinstance(project_limits, dict)
            or not _positive_integer(project_limits.get(row["project_id"]))
        )
    ]
    if missing_limits:
        reasons.append("project capacity is missing: " + ", ".join(missing_limits))

    policies = {
        (row["kind"], row["scope"])
        for row in store.rows("SELECT kind, scope FROM policies")
    }
    required_policies = {("sage", None)} | {
        ("inquisitor", row["project_id"]) for row in projects if row["enabled"]
    }
    missing_policies = sorted(required_policies - policies, key=lambda item: str(item))
    if missing_policies:
        rendered = [
            "fleet Sage" if item == ("sage", None) else f"{item[1]} Inquisitor"
            for item in missing_policies
        ]
        reasons.append("recurring policy definition is missing: " + ", ".join(rendered))

    archon = store.row(
        "SELECT id, runtime_status FROM tasks WHERE role = 'archon' AND state NOT IN ('retired','archived')"
    )
    if archon is None:
        reasons.append("Archon is missing")
    elif archon["runtime_status"] == "unmaterialized":
        reasons.append("Archon has no materialized turn")
    return not reasons, reasons


def progress_readiness(
    store: Store,
    *,
    critical_workers: set[str],
    maximum_reconciliation_age_seconds: int = 90,
    now: datetime | None = None,
) -> tuple[bool, list[str]]:
    """Evaluate whether the workflow can make progress, not merely answer IPC."""

    reasons = invariant_violations(store)
    current = now or datetime.now(timezone.utc)
    reconciliation = store.row(
        "SELECT value FROM meta WHERE key = 'last_reconciliation'"
    )
    if reconciliation is None:
        reasons.append("reconciliation has never completed")
    else:
        try:
            observed = datetime.fromisoformat(
                str(reconciliation["value"]).replace("Z", "+00:00")
            )
            age = (current - observed).total_seconds()
            if age > maximum_reconciliation_age_seconds:
                reasons.append(f"reconciliation is stale by {int(age)} seconds")
        except (TypeError, ValueError):
            reasons.append("reconciliation timestamp is invalid")
    workers = {
        str(row["worker_name"]): str(row["state"])
        for row in store.rows("SELECT worker_name, state FROM worker_heartbeats")
    }
    for name in sorted(critical_workers):
        if workers.get(name) != "running":
            reasons.append(f"critical worker {name} is not running")
    return not reasons, reasons


def _positive_integer(value: Any) -> bool:
    try:
        return int(value) > 0 and not isinstance(value, bool)
    except (TypeError, ValueError):
        return False
