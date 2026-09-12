"""Small task-scoped context assembly with explicit partial-read errors."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, cast

from fulcrum.config import RuntimePaths, safe_child
from fulcrum.records import (
    AssignmentRecord,
    Hold,
    HoldsJobsRecord,
    Record,
    RoleRunRegistryRecord,
    load_record,
)
from fulcrum.state import selected_record_path

MEMORY_LIMIT = 2000


def _error(path: Path, error: Exception) -> dict[str, str]:
    return {"path": str(path), "error": str(error)}


def _read_optional_memory(path: Path, errors: list[dict[str, str]]) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        errors.append(_error(path, error))
        return None
    return text[:MEMORY_LIMIT]


def _matching_records(
    directory: Path,
    predicate: Callable[[Record], bool],
    errors: list[dict[str, str]],
) -> list[Record]:
    matches: list[Record] = []
    if not directory.exists():
        return matches
    for path in sorted(directory.glob("*.json")):
        try:
            record = load_record(path)
            if predicate(record):
                matches.append(record)
        except Exception as error:
            errors.append(_error(path, error))
    return matches


def _is_assignment_for_task(record: Record, task_id: str) -> bool:
    if record["record_kind"] != "assignment":
        return False
    assignment = cast(AssignmentRecord, record)
    return task_id in {assignment["executor_task_id"], assignment["overseer_task_id"]}


def read_task_context(paths: RuntimePaths, task_id: str) -> dict[str, Any]:
    """Return only context authorized by an exact registered task identity."""

    errors: list[dict[str, str]] = []
    registry_path = selected_record_path(paths, "role_run_registry", None)
    try:
        loaded_registry = load_record(registry_path)
        if loaded_registry["record_kind"] != "role_run_registry":
            raise ValueError("expected role_run_registry record")
        registry = cast(RoleRunRegistryRecord, loaded_registry)
    except Exception as error:
        errors.append(_error(registry_path, error))
        return {"task_id": task_id, "known": False, "errors": errors}

    role = next(
        (item for item in registry["roles"] if item["task_id"] == task_id), None
    )
    if role is None:
        return {"task_id": task_id, "known": False, "errors": errors}

    project_id = role["project_id"]
    assignments = _matching_records(
        safe_child(paths.state_root, "assignments"),
        lambda record: _is_assignment_for_task(record, task_id),
        errors,
    )

    holds_path = selected_record_path(paths, "holds_jobs", None)
    applicable_holds: list[Hold] = []
    try:
        loaded_holds = load_record(holds_path)
        if loaded_holds["record_kind"] != "holds_jobs":
            raise ValueError("expected holds_jobs record")
        holds_record = cast(HoldsJobsRecord, loaded_holds)
        scopes = {"global", f"project:{project_id}", f"task:{task_id}"}
        applicable_holds = [
            hold for hold in holds_record["holds"] if hold["scope"] in scopes
        ]
    except Exception as error:
        errors.append(_error(holds_path, error))

    role_name = role["role"]
    global_memory_path = safe_child(paths.brain_root, "memory", "global.md")
    role_memory_path = safe_child(paths.brain_root, "memory", role_name, "global.md")
    project_memory_path = safe_child(
        paths.brain_root, "memory", role_name, "projects", f"{project_id}.md"
    )
    memory = {
        "global": _read_optional_memory(global_memory_path, errors),
        "role": _read_optional_memory(role_memory_path, errors),
        "project": _read_optional_memory(project_memory_path, errors),
    }
    return {
        "task_id": task_id,
        "known": True,
        "role": role,
        "assignments": assignments,
        "holds": applicable_holds,
        "memory": memory,
        "errors": errors,
    }
