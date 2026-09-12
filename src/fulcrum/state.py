"""Owned record paths and crash-durable atomic JSON I/O."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import cast

from fulcrum.config import ConfigurationError, RuntimePaths, safe_child
from fulcrum.records import (
    AssignmentRecord,
    ExecutorEvidenceRecord,
    InterviewRecord,
    ProgressRecord,
    Record,
    RecordValidationError,
    RoleRunRegistryRecord,
    load_record,
    validate_record,
)


class OwnershipError(PermissionError):
    """The record's declared writer does not own the selected state file."""


def record_path(paths: RuntimePaths, record: Record) -> Path:
    """Return the one allowed target path for a validated record."""

    kind = record["record_kind"]
    if kind == "installation":
        return paths.config_file
    if kind == "project_registry":
        return safe_child(paths.state_root, "registry", "projects.json")
    if kind == "role_run_registry":
        return safe_child(paths.state_root, "registry", "roles.json")
    if kind == "holds_jobs":
        return safe_child(paths.state_root, "registry", "holds-jobs.json")
    if kind == "assignment":
        assignment = cast(AssignmentRecord, record)
        return safe_child(
            paths.state_root, "assignments", f"{assignment['assignment_id']}.json"
        )
    if kind == "progress":
        progress = cast(ProgressRecord, record)
        return safe_child(
            paths.state_root, "progress", f"{progress['role_task_id']}.json"
        )
    if kind == "executor_evidence":
        evidence = cast(ExecutorEvidenceRecord, record)
        return safe_child(
            paths.state_root, "evidence", f"{evidence['executor_task_id']}.json"
        )
    if kind == "interview":
        interview = cast(InterviewRecord, record)
        return safe_child(paths.state_root, "interviews", f"{interview['run_id']}.json")
    raise RecordValidationError(kind, [f"$.record_kind: unknown kind {kind!r}"])


def selected_record_path(
    paths: RuntimePaths, kind: str, identifier: str | None
) -> Path:
    """Resolve a requested read without accepting arbitrary path fragments."""

    singleton = {
        "installation": paths.config_file,
        "project_registry": safe_child(paths.state_root, "registry", "projects.json"),
        "role_run_registry": safe_child(paths.state_root, "registry", "roles.json"),
        "holds_jobs": safe_child(paths.state_root, "registry", "holds-jobs.json"),
    }
    if kind in singleton:
        if identifier is not None:
            raise ConfigurationError(f"{kind} is a singleton and does not accept --id")
        return singleton[kind]
    collections = {
        "assignment": "assignments",
        "progress": "progress",
        "executor_evidence": "evidence",
        "interview": "interviews",
    }
    directory = collections.get(kind)
    if directory is None:
        raise ConfigurationError(f"unknown record kind: {kind!r}")
    if identifier is None:
        raise ConfigurationError(f"{kind} requires --id")
    return safe_child(paths.state_root, directory, f"{identifier}.json")


def _current_archon(paths: RuntimePaths) -> str:
    registry_path = selected_record_path(paths, "role_run_registry", None)
    registry = load_record(registry_path)
    if registry["record_kind"] != "role_run_registry":
        raise OwnershipError(f"{registry_path}: expected role_run_registry record")
    role_registry = cast(RoleRunRegistryRecord, registry)
    archon = role_registry["current_archon_task_id"]
    if archon is None:
        raise OwnershipError("role registry has no current Archon")
    return archon


def require_owner(
    paths: RuntimePaths, record: Record, *, handover_from: str | None = None
) -> None:
    """Enforce the record's cooperative single-writer contract."""

    kind = record["record_kind"]
    writer = record["writer_id"]
    expected: str | None
    if kind == "installation":
        if writer == "setup" or writer == "human" or writer.startswith("human:"):
            return
        expected = "setup or human"
    elif kind == "role_run_registry":
        registry_path = selected_record_path(paths, "role_run_registry", None)
        if registry_path.exists():
            previous = cast(
                RoleRunRegistryRecord, read_record(paths, "role_run_registry")
            )["current_archon_task_id"]
            incoming = cast(RoleRunRegistryRecord, record)["current_archon_task_id"]
            if incoming != previous and (previous is None or handover_from != previous):
                raise OwnershipError(
                    "Archon replacement requires verified cooperative handover"
                )
        expected = (
            cast(RoleRunRegistryRecord, record)["current_archon_task_id"] or "human"
        )
    elif kind in {"project_registry", "holds_jobs"}:
        expected = _current_archon(paths)
    elif kind == "assignment":
        expected = cast(AssignmentRecord, record)["overseer_task_id"]
    elif kind == "progress":
        expected = cast(ProgressRecord, record)["role_task_id"]
    elif kind == "executor_evidence":
        expected = cast(ExecutorEvidenceRecord, record)["executor_task_id"]
    elif kind == "interview":
        expected = cast(InterviewRecord, record)["sage_task_id"]
    else:
        raise OwnershipError(f"unsupported ownership contract for {kind!r}")
    if writer != expected:
        raise OwnershipError(
            f"{kind} writer mismatch: declared {writer!r}, expected {expected!r}"
        )


def read_record(
    paths: RuntimePaths, kind: str, identifier: str | None = None
) -> Record:
    """Read the selected record and verify its kind agrees with its path."""

    path = selected_record_path(paths, kind, identifier)
    record = load_record(path)
    if record["record_kind"] != kind:
        raise RecordValidationError(
            record["record_kind"],
            [
                f"{path}: path expects {kind!r}, record declares {record['record_kind']!r}"
            ],
        )
    return record


def atomic_write_record(
    paths: RuntimePaths, value: object, *, handover_from: str | None = None
) -> Path:
    """Validate and atomically replace one cooperatively owned record."""

    record = validate_record(value)
    require_owner(paths, record, handover_from=handover_from)
    target = record_path(paths, record)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(record, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise
    return target
