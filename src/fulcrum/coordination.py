"""Cooperative Archon transfer and fail-closed enrollment from observations."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import cast

from fulcrum.config import RuntimePaths
from fulcrum.records import Project, RoleRunRegistryRecord
from fulcrum.state import OwnershipError, atomic_write_record, read_record


def transfer_archon(
    paths: RuntimePaths, previous: str, successor: str, evidence: str, now: str
) -> None:
    """Caller must verify relinquishment/inactivity through supported Codex tools."""
    if not evidence.strip() or not successor or successor == previous:
        raise ValueError("distinct successor and verified handover evidence required")
    registry = cast(RoleRunRegistryRecord, read_record(paths, "role_run_registry"))
    if registry["current_archon_task_id"] != previous:
        raise OwnershipError("Archon changed; reconcile current registration")
    updated = deepcopy(registry)
    updated.update(
        writer_id=successor, current_archon_task_id=successor, updated_at=now
    )
    atomic_write_record(paths, updated, handover_from=previous)


def enroll_project(
    project: Project,
    *,
    git_root: str | None,
    codex_id: str | None,
    codex_path: str | None,
    codex_host: str | None,
    codex_is_git: bool | None,
    tollgate_id: str | None,
    tollgate_path: str | None,
    tollgate_healthy: bool | None,
) -> tuple[Project, list[str]]:
    """Construct a registry entry; observations must come from current native tools."""
    reasons: list[str] = []
    root = Path(project["repo_path"]).expanduser().resolve()
    for label, observed in (
        ("Git", git_root),
        ("Codex", codex_path),
        ("Tollgate", tollgate_path),
    ):
        if observed is None or Path(observed).expanduser().resolve() != root:
            reasons.append(f"{label} repository path unavailable or mismatched")
    if (
        not codex_id
        or codex_id != project["codex_project_id"]
        or codex_host != project["host_id"]
        or codex_is_git is not True
    ):
        reasons.append("Codex project/host identity unavailable or mismatched")
    if not tollgate_id or tollgate_id != project["tollgate_repo_id"]:
        reasons.append("Tollgate repository identity unavailable or mismatched")
    if tollgate_healthy is not True:
        reasons.append("Tollgate configuration or execution health unavailable")
    result = deepcopy(project)
    result["enabled"] = not reasons
    return result, reasons
