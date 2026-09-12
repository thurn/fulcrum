"""Shared readiness rules for setup, the controller, and diagnostics."""

from __future__ import annotations

import json
from typing import Any

from fulcrum.store import Store


def state_readiness(store: Store) -> tuple[bool, list[str]]:
    """Return whether durable controller state can safely enable dispatch."""

    reasons: list[str] = []
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
        for row in store.rows("SELECT kind, scope FROM policies WHERE active = 1")
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
        reasons.append("recurring policy is missing: " + ", ".join(rendered))

    archon = store.row(
        "SELECT id FROM tasks WHERE role = 'archon' AND state NOT IN ('retired','archived')"
    )
    if archon is None:
        reasons.append("Archon is missing")
    return not reasons, reasons


def _positive_integer(value: Any) -> bool:
    try:
        return int(value) > 0 and not isinstance(value, bool)
    except (TypeError, ValueError):
        return False
