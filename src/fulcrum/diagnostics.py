"""Bounded structured diagnostics and durable operator projections."""

from __future__ import annotations

import base64
import fcntl
import json
import os
import re
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fulcrum.configuration import ConfigurationManager, default_config
from fulcrum.contracts import (
    CommandResult,
    CommandState,
    ErrorInfo,
    FulcrumError,
    ParsedRequest,
)
from fulcrum.ledger import (
    Ledger,
    LedgerFailure,
    LedgerRecord,
    OperationRecord,
    operation_view,
)
from fulcrum.work import work_view

DEFAULT_LIMIT = 20
NON_FAILURE_HEALTH_STATES = {"healthy", "initializing", "running", "paused"}
DEFAULT_CHUNK_BYTES = 256 * 1024
_SECRET_KEY: re.Pattern[str] = re.compile(
    r"(?:token|secret|password|passwd|credential|authorization|api[_-]?key|cookie)",
    re.IGNORECASE,
)
_BEARER: re.Pattern[str] = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _diagnostic_health(instance_root: Path) -> dict[str, Any]:
    path = instance_root / "diagnostic-health.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return value
    except (OSError, json.JSONDecodeError):
        pass
    return {"state": "healthy", "dropped_events": 0}


def _duration_ms(start: Any, finish: Any) -> float | None:
    if not isinstance(start, str) or not isinstance(finish, str):
        return None
    try:
        start_at = datetime.fromisoformat(start.replace("Z", "+00:00"))
        finish_at = datetime.fromisoformat(finish.replace("Z", "+00:00"))
    except ValueError:
        return None
    return round((finish_at - start_at).total_seconds() * 1000, 3)


def _broker_component(request: ParsedRequest) -> dict[str, Any]:
    import asyncio
    from fulcrum.broker import broker_request

    try:
        facts = dict(
            asyncio.run(
                asyncio.wait_for(
                    broker_request(request.instance.socket_path, {"type": "health"}),
                    timeout=1,
                )
            )
        )
        available = facts.get("state") == "healthy"
        evidence = {
            "socket": str(request.instance.socket_path),
            "available": available,
            "pid": facts.get("pid"),
            "pending": list(facts.get("pending") or []),
            "completed": facts.get("completed"),
            "failures": facts.get("failures"),
        }
        state = "healthy" if available else "unavailable"
    except Exception as error:
        state = "unavailable"
        evidence = {"socket": str(request.instance.socket_path), "error": str(error)}
    healthy = state == "healthy"
    return _health(
        "broker",
        state,
        evidence=evidence,
        affected_commands=[] if healthy else ["instruction wait", "ci wait"],
        next_commands=([] if healthy else [["fulcrum", "service", "status", "--json"]]),
    )


class DiagnosticLog:
    def __init__(
        self,
        root: Path,
        *,
        retention_days: int = 14,
        max_bytes: int = 1024 * 1024 * 1024,
        capture_bytes: int = DEFAULT_CHUNK_BYTES,
    ) -> None:
        self.root: Path = root.resolve(strict=False)
        self.retention_days = retention_days
        self.max_bytes = max_bytes
        self.capture_bytes = capture_bytes

    @classmethod
    def from_request(cls, request: ParsedRequest) -> DiagnosticLog:
        values = default_config(
            request.instance.brain_root or request.instance.config_path.parent
        )["diagnostics"]
        try:
            manager = ConfigurationManager(request.instance.config_path)
            document, _ = manager.load()
            values = manager.effective(document)["diagnostics"]
        except FulcrumError:
            pass
        return cls(
            request.instance.instance_root / "logs",
            retention_days=int(values["retention_days"]),
            max_bytes=int(values["max_bytes"]),
            capture_bytes=int(values["capture_bytes_per_stream"]),
        )

    def append(self, event: Mapping[str, Any]) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        retained = redact(dict(event), capture_bytes=self.capture_bytes)
        retained.setdefault("time", utc_now())
        retained.setdefault("event_id", str(uuid.uuid4()))
        encoded = (
            json.dumps(retained, separators=(",", ":"), ensure_ascii=False) + "\n"
        ).encode("utf-8")
        if len(encoded) > self.capture_bytes * 2 + 65536:
            retained["diagnostic_truncated"] = True
            retained.pop("stdout", None)
            retained.pop("stderr", None)
            encoded = (
                json.dumps(retained, separators=(",", ":"), ensure_ascii=False) + "\n"
            ).encode("utf-8")
        lock_path = self.root / ".append.lock"
        with lock_path.open("ab") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            path = self._active_path(len(encoded))
            with path.open("ab") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            # Rotation is checked at most once a minute, never at every event.
            prune_stamp = self.root / ".last-prune"
            try:
                stale = time.time() - prune_stamp.stat().st_mtime >= 60
            except FileNotFoundError:
                stale = True
            if stale:
                self.prune()
                prune_stamp.touch()
        return retained

    @staticmethod
    def report_failure(instance_root: Path, error: BaseException) -> None:
        """Make a diagnostic sink failure visible without inviting effect retry."""

        message = f"fulcrum diagnostic log failure: {type(error).__name__}: {error}"
        print(message, file=sys.stderr)
        health = instance_root / "diagnostic-health.json"
        current: dict[str, Any] = {}
        try:
            loaded = json.loads(health.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                current = loaded
        except (OSError, json.JSONDecodeError):
            pass
        current.update(
            {
                "state": "degraded",
                "last_error": message,
                "last_error_at": utc_now(),
                "dropped_events": int(current.get("dropped_events", 0)) + 1,
            }
        )
        try:
            health.parent.mkdir(parents=True, exist_ok=True)
            temporary = health.with_name(f".{health.name}.{os.getpid()}")
            temporary.write_text(
                json.dumps(current, separators=(",", ":")) + "\n", encoding="utf-8"
            )
            os.replace(temporary, health)
        except OSError:
            print("fulcrum could not persist diagnostic health", file=sys.stderr)

    def _active_path(self, incoming: int) -> Path:
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        per_file = max(4096, min(64 * 1024 * 1024, self.max_bytes // 4))
        for index in range(100000):
            path = self.root / f"events-{day}-{index:05d}.jsonl"
            if not path.exists() or path.stat().st_size + incoming <= per_file:
                return path
        raise OSError("diagnostic log rotation slots exhausted")

    def files(self) -> list[Path]:
        if not self.root.exists():
            return []
        return sorted(self.root.glob("events-*.jsonl"))

    def prune(self) -> dict[str, Any]:
        files = self.files()
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        removed: list[str] = []
        removed_bytes = 0
        survivors: list[Path] = []
        for path in files:
            modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            if modified < cutoff:
                removed.append(path.name)
                removed_bytes += path.stat().st_size
                path.unlink(missing_ok=True)
            else:
                survivors.append(path)
        total = sum(path.stat().st_size for path in survivors)
        while len(survivors) > 1 and total > self.max_bytes:
            path = survivors.pop(0)
            size = path.stat().st_size
            path.unlink(missing_ok=True)
            removed.append(path.name)
            removed_bytes += size
            total -= size
        return {
            "removed_files": removed,
            "removed_bytes": removed_bytes,
            "retained_bytes": total,
            "retention_days": self.retention_days,
            "max_bytes": self.max_bytes,
        }

    def read(
        self,
        *,
        bead: str | None = None,
        operation: str | None = None,
        since: str | None = None,
        limit: int = DEFAULT_LIMIT,
        cursor: str | None = None,
        max_bytes: int = DEFAULT_CHUNK_BYTES,
    ) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        gaps: list[dict[str, Any]] = []
        for path in self.files():
            try:
                for line_number, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), start=1
                ):
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        gaps.append(
                            {
                                "file": path.name,
                                "line": line_number,
                                "reason": "invalid_json",
                            }
                        )
                        continue
                    if not isinstance(value, dict):
                        continue
                    associated = value.get("associated_beads")
                    if (
                        bead
                        and value.get("bead_id") != bead
                        and not (isinstance(associated, list) and bead in associated)
                    ):
                        continue
                    if operation and value.get("operation_id") != operation:
                        continue
                    if since and str(value.get("time", "")) < since:
                        continue
                    events.append(value)
            except (OSError, UnicodeError) as error:
                gaps.append({"file": path.name, "reason": str(error)})
        events.sort(key=_event_key)
        start = _cursor_start(events, cursor)
        selected: list[dict[str, Any]] = []
        used = 0
        available = events[start:]
        for event in available:
            size = len(json.dumps(event, ensure_ascii=False).encode("utf-8"))
            if selected and used + size > max_bytes:
                break
            if not selected and size > max_bytes:
                clipped = dict(event)
                clipped["event_truncated"] = True
                clipped.pop("stdout", None)
                clipped.pop("stderr", None)
                event = clipped
                size = len(json.dumps(event, ensure_ascii=False).encode("utf-8"))
            selected.append(event)
            used += size
            if limit and len(selected) >= limit:
                break
        consumed = start + len(selected)
        next_cursor = (
            encode_cursor(_event_key(selected[-1]))
            if selected and consumed < len(events)
            else None
        )
        return {
            "items": selected,
            "next_cursor": next_cursor,
            "cursor": encode_cursor(_event_key(selected[-1])) if selected else cursor,
            "gaps": gaps,
            "bytes": used,
        }


class DiagnosticService:
    def status(self, request: ParsedRequest) -> CommandResult:
        observed_at = utc_now()
        work: list[dict[str, Any]] = []
        operations: list[dict[str, Any]] = []
        gaps: list[dict[str, Any]] = []
        capacity: dict[str, Any] | None = None
        publication: dict[str, Any] | None = None
        desktop: dict[str, Any] | None = None
        work_next_cursor: str | None = None
        start = 0
        all_work_count = 0
        try:
            ledger = _ledger(request)
            target = request.arguments.get("bead")
            if target:
                record = ledger.show(str(target))
                if record is None or record.kind != "work":
                    raise FulcrumError.invalid("NOT_FOUND", f"unknown work {target}")
                records = [record]
            else:
                records = [
                    item
                    for item in ledger.list_records(limit=0)
                    if item.kind not in {"control", "analytics", "operation"}
                    and (
                        not request.project
                        or (item.fc and item.fc.get("project") == request.project)
                    )
                ]
                records.sort(
                    key=lambda item: (
                        str(
                            item.native.get("updated_at")
                            or item.native.get("created_at")
                            or ""
                        ),
                        item.id,
                    )
                )
                start = _cursor_start(
                    records,
                    _optional_string(request.arguments.get("cursor")),
                    key=lambda item: (
                        str(
                            item.native.get("updated_at")
                            or item.native.get("created_at")
                            or ""
                        ),
                        item.id,
                    ),
                )
                limit = _limit(request.arguments.get("limit"))
                all_work_count = len(records)
                records = (
                    records[start:] if limit == 0 else records[start : start + limit]
                )
            for record in records:
                view = work_view(ledger, record)
                view["native_observation"] = {
                    "observed_at": observed_at,
                    "available": True,
                    "status": record.status,
                    "assignee": record.assignee,
                }
                work.append(view)
            work_next_cursor = None
            if not target and records:
                last_record = records[-1]
                if start + len(records) < all_work_count:
                    work_next_cursor = encode_cursor(
                        (
                            str(
                                last_record.native.get("updated_at")
                                or last_record.native.get("created_at")
                                or ""
                            ),
                            last_record.id,
                        )
                    )
            all_operations = [
                OperationRecord.from_record(item)
                for item in ledger.list_records(kind="operation", limit=0)
            ]
            if target:
                all_operations = [
                    item
                    for item in all_operations
                    if item.operation.get("bead_id") == target
                ]
            operations = [operation_view(item) for item in all_operations[:20]]
            control = ledger.show("fc-system")
            if control and control.fc:
                publication_value = control.fc.get("publication")
                publication = (
                    dict(publication_value)
                    if isinstance(publication_value, Mapping)
                    else None
                )
                retained_desktop = control.fc.get("desktop")
                if isinstance(retained_desktop, Mapping):
                    actions = retained_desktop.get("actions") or {}
                    waits = retained_desktop.get("instruction_waits") or {}
                    desktop = {
                        "run_control": retained_desktop.get("run_control"),
                        "setup": retained_desktop.get("setup"),
                        "standing": retained_desktop.get("standing"),
                        "marshal_schedule": retained_desktop.get("marshal_schedule"),
                        "actions": [
                            {
                                "action_id": value.get("action_id"),
                                "tool": value.get("tool"),
                                "executor": value.get("executor"),
                                "state": value.get("state"),
                            }
                            for value in actions.values()
                            if isinstance(value, Mapping)
                            and value.get("state")
                            in {"pending", "issuing", "uncertain"}
                        ],
                        "waits": [
                            dict(value)
                            for value in waits.values()
                            if isinstance(value, Mapping)
                            and value.get("state") == "waiting"
                        ],
                    }
            all_records = ledger.list_records(limit=0)
            pending_actions: list[dict[str, Any]] = []
            incidents: list[dict[str, Any]] = []
            provider_jobs: list[dict[str, Any]] = []
            accounting_coverage: list[dict[str, Any]] = []
            observed_now = datetime.now(timezone.utc)
            for retained in all_records:
                retained_fc = retained.fc or {}
                retained_desktop = retained_fc.get("desktop")
                if isinstance(retained_desktop, Mapping):
                    for transcript_task, transcript in (
                        retained_desktop.get("transcripts") or {}
                    ).items():
                        if not isinstance(transcript, Mapping):
                            continue
                        for gap in transcript.get("gaps") or []:
                            if isinstance(gap, Mapping):
                                gaps.append(
                                    {
                                        "component": "desktop_protocol",
                                        "projection": "transcript",
                                        "record_id": retained.id,
                                        "task_id": transcript_task,
                                        **dict(gap),
                                    }
                                )
                    for action in (retained_desktop.get("actions") or {}).values():
                        if not isinstance(action, Mapping) or action.get(
                            "state"
                        ) not in {
                            "pending",
                            "issuing",
                            "uncertain",
                        }:
                            continue
                        created = _parse_time(action.get("created_at"))
                        pending_actions.append(
                            {
                                "record_id": retained.id,
                                "action_id": action.get("action_id"),
                                "tool": action.get("tool"),
                                "executor": action.get("executor"),
                                "state": action.get("state"),
                                "age_seconds": (
                                    max(
                                        0, int((observed_now - created).total_seconds())
                                    )
                                    if created is not None
                                    else None
                                ),
                            }
                        )
                    for incident in (retained_desktop.get("incidents") or {}).values():
                        if (
                            not isinstance(incident, Mapping)
                            or incident.get("state") == "resolved"
                        ):
                            continue
                        incidents.append(
                            {
                                "record_id": retained.id,
                                **dict(incident),
                                "ordinary_repair_allowance": 3
                                + int(incident.get("additional_repair_cycles") or 0),
                                "ordinary_repairs_used": int(
                                    incident.get("repair_cycles") or 0
                                ),
                                "justiciar_allowance": 1,
                                "justiciar_interventions_used": int(
                                    incident.get("justiciar_interventions") or 0
                                ),
                            }
                        )
                    candidate = retained_desktop.get("candidate")
                    if isinstance(candidate, Mapping):
                        provider_jobs.append(
                            {"record_id": retained.id, **dict(candidate)}
                        )
                if retained.kind == "analytics":
                    missing_reasons = retained_fc.get("missing_reasons") or []
                    accounting_coverage.append(
                        {
                            "record_id": retained.id,
                            "thread_id": retained_fc.get("thread_id"),
                            "role": retained_fc.get("role"),
                            "missing_reasons": missing_reasons,
                        }
                    )
                    if missing_reasons:
                        gaps.append(
                            {
                                "component": "desktop_protocol",
                                "projection": "accounting",
                                "record_id": retained.id,
                                "task_id": retained_fc.get("thread_id"),
                                "turn_id": retained_fc.get("turn_id"),
                                "missing_reasons": list(missing_reasons),
                            }
                        )
            desktop = dict(desktop or {})
            pending_actions.sort(
                key=lambda value: (
                    str(value.get("record_id")),
                    str(value.get("action_id")),
                )
            )
            incidents.sort(
                key=lambda value: (
                    str(value.get("record_id")),
                    str(value.get("incident_id")),
                )
            )
            provider_jobs.sort(
                key=lambda value: (
                    str(value.get("record_id")),
                    str(value.get("candidate_id")),
                )
            )
            accounting_coverage.sort(key=lambda value: str(value.get("record_id")))
            desktop["actions"] = pending_actions[:100]
            desktop["incidents"] = incidents[:100]
            desktop["provider_jobs"] = provider_jobs[:100]
            desktop["accounting_coverage"] = accounting_coverage[:100]
            desktop["omitted"] = {
                "actions": max(0, len(pending_actions) - 100),
                "incidents": max(0, len(incidents) - 100),
                "provider_jobs": max(0, len(provider_jobs) - 100),
                "accounting_coverage": max(0, len(accounting_coverage) - 100),
            }
            try:
                capacity = _capacity(request, ledger)
            except FulcrumError as error:
                gaps.append(
                    {
                        "component": "config",
                        "projection": "capacity",
                        "availability": "unavailable",
                        "reason": error.message,
                    }
                )
        except LedgerFailure as error:
            gaps.append(
                {
                    "component": "ledger",
                    "projection": "work",
                    "availability": "unavailable",
                    "reason": str(error),
                    "category": error.category,
                }
            )
        event_log_health = _diagnostic_health(request.instance.instance_root)
        overall_state = (
            "degraded"
            if gaps or event_log_health.get("state") != "healthy"
            else "healthy"
        )
        return CommandResult.query(
            {
                "observed_at": observed_at,
                "instance": request.instance.to_dict(),
                "work": work,
                "work_next_cursor": work_next_cursor,
                "operations": operations,
                "capacity": capacity,
                "publication": publication,
                "desktop": desktop,
                "health": {
                    "state": overall_state,
                    "gap_count": len(gaps),
                    "event_log_state": event_log_health.get("state"),
                },
                "event_log_health": event_log_health,
                "gaps": gaps,
            }
        )

    def doctor(self, request: ParsedRequest) -> CommandResult:
        now = datetime.now(timezone.utc)
        components: list[dict[str, Any]] = []
        config: dict[str, Any] | None = None
        try:
            manager = ConfigurationManager(request.instance.config_path)
            document, _ = manager.load()
            config = manager.effective(document)
            components.append(
                _health(
                    "config",
                    "healthy",
                    evidence={"path": str(request.instance.config_path)},
                    affected_commands=[],
                )
            )
        except FulcrumError as error:
            components.append(
                _health(
                    "config",
                    "unavailable",
                    evidence={"error": error.message},
                    affected_commands=["all workflow mutations"],
                    next_commands=[["fulcrum", "config", "validate", "--json"]],
                )
            )
        try:
            ledger = _ledger(request)
            ledger.list_records(limit=1)
            components.append(
                _health(
                    "ledger",
                    "healthy",
                    evidence={"root": str(request.instance.brain_root)},
                    affected_commands=[],
                )
            )
        except (FulcrumError, LedgerFailure) as error:
            components.append(
                _health(
                    "ledger",
                    "unavailable",
                    evidence={"error": str(error)},
                    affected_commands=["work", "operation", "status", "trace"],
                    next_commands=[["fulcrum", "ledger", "status", "--json"]],
                )
            )
        socket_exists = request.instance.socket_path.exists()
        components.append(
            _health(
                "service_socket",
                "healthy" if socket_exists else "unavailable",
                evidence={
                    "path": str(request.instance.socket_path),
                    "exists": socket_exists,
                    "execution": "local_process",
                },
                affected_commands=(
                    ["instruction wait", "ci wait"] if not socket_exists else []
                ),
                next_commands=(
                    [["fulcrum", "service", "start", "--json"]]
                    if not socket_exists
                    else []
                ),
            )
        )
        delivery = config.get("delivery") if config else None
        executable = (
            delivery.get("executable") if isinstance(delivery, Mapping) else None
        )
        components.append(
            _health(
                "delivery",
                (
                    "healthy"
                    if executable and Path(str(executable)).is_file()
                    else "unsupported"
                ),
                evidence={"executable": executable},
                affected_commands=["validation", "promotion", "source sync"],
                next_commands=[["fulcrum", "config", "show", "--json"]],
            )
        )
        components.append(_broker_component(request))
        components.append(_desktop_protocol_component(request, now))
        loops = _loop_health(request, config, now)
        return _doctor_result(components, loops)

    def logs(self, request: ParsedRequest) -> CommandResult:
        arguments = request.arguments
        result = DiagnosticLog.from_request(request).read(
            bead=_optional_string(arguments.get("bead")),
            operation=_optional_string(arguments.get("operation")),
            since=_optional_string(arguments.get("since")),
            limit=_limit(arguments.get("limit")),
            cursor=_optional_string(arguments.get("cursor")),
            max_bytes=int(arguments.get("max_bytes", DEFAULT_CHUNK_BYTES)),
        )
        result["follow"] = bool(arguments.get("follow", False))
        return CommandResult.query(result)

    def prune(self, request: ParsedRequest) -> CommandResult:
        log = DiagnosticLog.from_request(request)
        result = log.prune()
        if result["removed_files"]:
            log.append(
                {
                    "event": "logs_pruned",
                    "removed_files": result["removed_files"],
                    "removed_bytes": result["removed_bytes"],
                    "outcome": "completed",
                }
            )
        return CommandResult.query(result)

    def trace(self, request: ParsedRequest) -> CommandResult:
        bead_id = request.arguments.get("bead")
        ledger = _ledger(request)
        selectors = {
            name: request.arguments.get(name)
            for name in ("operation", "action", "task", "wait")
            if request.arguments.get(name)
        }
        if not isinstance(bead_id, str) or not bead_id:
            if len(selectors) != 1:
                raise FulcrumError.invalid(
                    "INVALID_INPUT",
                    "trace requires exactly one of --bead, --operation, --action, --task, or --wait",
                )
            selector_name, selector_value = next(iter(selectors.items()))
            matched: LedgerRecord | None = None
            if selector_name == "operation":
                operation_record = ledger.show(str(selector_value))
                operation_fc = (
                    operation_record.fc or {} if operation_record is not None else {}
                )
                operation = operation_fc.get("operation")
                candidate = (
                    operation.get("bead_id") if isinstance(operation, Mapping) else None
                )
                matched = ledger.show(str(candidate)) if candidate else operation_record
            else:
                for candidate in ledger.list_records(limit=0):
                    desktop = (candidate.fc or {}).get("desktop")
                    if not isinstance(desktop, Mapping):
                        continue
                    if selector_name == "action" and str(selector_value) in (
                        desktop.get("actions") or {}
                    ):
                        matched = candidate
                        break
                    if selector_name == "wait" and any(
                        str(selector_value) in (desktop.get(name) or {})
                        for name in ("instruction_waits", "ci_waits")
                    ):
                        matched = candidate
                        break
                    if selector_name == "task":
                        assignment = desktop.get("assignment")
                        history = desktop.get("assignment_history") or []
                        standing = desktop.get("standing") or {}
                        task_ids = {
                            str(value.get("task_id"))
                            for value in [assignment, *history, *standing.values()]
                            if isinstance(value, Mapping) and value.get("task_id")
                        }
                        if str(selector_value) in task_ids:
                            matched = candidate
                            break
            if matched is None:
                raise FulcrumError.invalid(
                    "NOT_FOUND", f"unknown {selector_name} {selector_value}"
                )
            if matched.kind != "work":
                desktop = (matched.fc or {}).get("desktop") or {}
                evidence: Any = {}
                if selector_name == "operation":
                    evidence = (matched.fc or {}).get("operation") or {}
                elif selector_name == "action":
                    evidence = (desktop.get("actions") or {}).get(
                        str(selector_value), {}
                    )
                elif selector_name == "wait":
                    evidence = next(
                        (
                            collection[str(selector_value)]
                            for collection in (
                                desktop.get("instruction_waits") or {},
                                desktop.get("ci_waits") or {},
                            )
                            if str(selector_value) in collection
                        ),
                        {},
                    )
                elif selector_name == "task":
                    evidence = next(
                        (
                            value
                            for value in (desktop.get("standing") or {}).values()
                            if isinstance(value, Mapping)
                            and value.get("task_id") == selector_value
                        ),
                        {},
                    )
                return CommandResult.query(
                    {
                        "items": [
                            {
                                "time": matched.native.get("updated_at")
                                or matched.native.get("created_at"),
                                "id": f"{matched.id}:{selector_name}:{selector_value}",
                                "bead_id": None,
                                "operation_id": (
                                    selector_value
                                    if selector_name == "operation"
                                    else None
                                ),
                                "task_id": (
                                    selector_value if selector_name == "task" else None
                                ),
                                "transition": "durable_control_observation",
                                "effect": selector_name,
                                "outcome": matched.status,
                                "evidence": evidence,
                            }
                        ],
                        "next_cursor": None,
                        "gaps": [],
                        "source_oids": [],
                    }
                )
            bead_id = matched.id
        record = ledger.show(bead_id)
        if record is None or record.kind != "work":
            raise FulcrumError.invalid("NOT_FOUND", f"unknown work {bead_id}")
        items: list[dict[str, Any]] = []
        durable_gaps: list[dict[str, Any]] = []
        operations = [
            OperationRecord.from_record(candidate)
            for candidate in ledger.list_records(kind="operation", limit=0)
        ]
        child_ids = {
            operation.id: _trace_child_operation_ids(operation.operation)
            for operation in operations
        }
        parent_ids: dict[str, list[str]] = {}
        for parent_id, children in child_ids.items():
            for child_id in children:
                parent_ids.setdefault(child_id, []).append(parent_id)
        related_ids = {
            operation.id
            for operation in operations
            if _trace_operation_mentions_bead(operation.operation, bead_id)
        }
        related_ids.update(
            str(identifier)
            for identifier in (
                (record.fc or {}).get("ownership_operation"),
                (record.fc or {}).get("last_transition"),
            )
            if identifier
        )
        changed = True
        while changed:
            before = len(related_ids)
            for operation_id in tuple(related_ids):
                related_ids.update(child_ids.get(operation_id, []))
                related_ids.update(parent_ids.get(operation_id, []))
            changed = len(related_ids) != before
        related_operations = [
            operation for operation in operations if operation.id in related_ids
        ]
        for operation in related_operations:
            fc = operation.operation
            result = fc.get("result") if isinstance(fc.get("result"), Mapping) else {}
            external = (
                fc.get("external") if isinstance(fc.get("external"), Mapping) else {}
            )
            source_oids = _trace_source_oids(fc)
            items.append(
                {
                    "time": fc.get("completed_at") or fc.get("created_at"),
                    "created_at": fc.get("created_at"),
                    "completed_at": fc.get("completed_at"),
                    "id": operation.id,
                    "bead_id": bead_id,
                    "task_id": fc.get("owner"),
                    "turn_id": external.get("turn_id"),
                    "operation_id": operation.id,
                    "transition": fc.get("step"),
                    "effect": fc.get("command"),
                    "outcome": fc.get("state"),
                    "evidence": result.get("evidence", []),
                    "source_oid": source_oids[-1] if source_oids else None,
                    "source_oids": source_oids,
                    "parent_operation_ids": sorted(parent_ids.get(operation.id, [])),
                    "child_operation_ids": child_ids.get(operation.id, []),
                    "provider_handles": _trace_provider_handles(fc),
                    "duration_ms": _duration_ms(
                        fc.get("created_at"), fc.get("completed_at")
                    ),
                }
            )
        fc = record.fc or {}
        dispatch = fc.get("dispatch")
        if isinstance(dispatch, Mapping):
            authorized_scope = dispatch.get("authorized_scope")
            items.append(
                {
                    "time": dispatch.get("authorized_at"),
                    "id": f"{record.id}:authorized-scope",
                    "bead_id": bead_id,
                    "task_id": None,
                    "turn_id": None,
                    "operation_id": dispatch.get("decision_operation"),
                    "transition": "scope_authorized",
                    "effect": dispatch.get("role"),
                    "outcome": "authorized",
                    "evidence": [],
                    "scope_revision": (
                        authorized_scope.get("finish_operation")
                        if isinstance(authorized_scope, Mapping)
                        else None
                    ),
                    "compiled_contract": {
                        "authorized_role": dispatch.get("role"),
                        "scope_revision": (
                            authorized_scope.get("finish_operation")
                            if isinstance(authorized_scope, Mapping)
                            else None
                        ),
                        "decision_operation": dispatch.get("decision_operation"),
                    },
                    "source_oid": None,
                    "source_oids": [],
                    "parent_operation_ids": [],
                    "child_operation_ids": [],
                    "provider_handles": [],
                }
            )
        fence = fc.get("recovery_fence")
        if isinstance(fence, Mapping):
            items.append(
                {
                    "time": fence.get("released_at") or fence.get("started_at"),
                    "id": f"{record.id}:recovery-fence",
                    "bead_id": bead_id,
                    "task_id": fence.get("owner_thread"),
                    "turn_id": None,
                    "operation_id": fence.get("operation_id"),
                    "transition": "recovery_fence",
                    "effect": fence.get("scope"),
                    "outcome": fence.get("state"),
                    "evidence": [],
                    "source_oid": None,
                    "source_oids": [],
                    "parent_operation_ids": [],
                    "child_operation_ids": [],
                    "provider_handles": [],
                }
            )
        for index, resolution in enumerate(fc.get("human_resolutions") or []):
            if not isinstance(resolution, Mapping):
                continue
            items.append(
                {
                    "time": resolution.get("resolved_at"),
                    "id": f"{record.id}:human-resolution:{index}",
                    "bead_id": bead_id,
                    "task_id": None,
                    "turn_id": None,
                    "operation_id": resolution.get("operation_id"),
                    "transition": "human_resolution",
                    "effect": resolution.get("reason_id"),
                    "outcome": resolution.get("resume_role"),
                    "evidence": [resolution.get("answer")],
                    "source_oid": None,
                    "source_oids": [],
                    "parent_operation_ids": [],
                    "child_operation_ids": [],
                    "provider_handles": [],
                }
            )
        desktop = fc.get("desktop")
        assignment_rows: list[Mapping[str, Any]] = []
        if isinstance(desktop, Mapping):
            history = desktop.get("assignment_history")
            if isinstance(history, list):
                assignment_rows.extend(
                    item for item in history if isinstance(item, Mapping)
                )
            active = desktop.get("assignment")
            if isinstance(active, Mapping):
                assignment_rows.append(active)
            assignments_by_token = {
                str(item.get("assignment_token")): item
                for item in assignment_rows
                if item.get("assignment_token")
            }
            lifecycle_rows: list[Mapping[str, Any]] = []
            observations = desktop.get("observations")
            lifecycle = (
                observations.get("lifecycle")
                if isinstance(observations, Mapping)
                else None
            )
            if isinstance(lifecycle, Mapping):
                lifecycle_rows.extend(
                    value for value in lifecycle.values() if isinstance(value, Mapping)
                )
            lifecycle_rows.extend(
                value
                for value in (desktop.get("hook_events") or [])
                if isinstance(value, Mapping)
            )
            lifecycle_tasks = {
                str(value.get("task_id"))
                for value in lifecycle_rows
                if value.get("task_id")
            }
            for index, event in enumerate(lifecycle_rows):
                items.append(
                    {
                        "time": event.get("time") or event.get("recorded_at"),
                        "id": event.get("event_id") or f"{record.id}:lifecycle:{index}",
                        "bead_id": bead_id,
                        "task_id": event.get("task_id"),
                        "turn_id": event.get("turn_id"),
                        "operation_id": None,
                        "action_id": event.get("creation_action_id"),
                        "assignment_token": event.get("assignment_token"),
                        "transition": "native_lifecycle",
                        "effect": event.get("type") or event.get("event"),
                        "outcome": event.get("reason") or event.get("type"),
                        "evidence": [],
                        "source_oid": None,
                        "source_oids": [],
                        "parent_operation_ids": [],
                        "child_operation_ids": [],
                        "provider_handles": [],
                    }
                )
            for action in (desktop.get("actions") or {}).values():
                if not isinstance(action, Mapping):
                    continue
                attempts = [
                    value
                    for value in (action.get("attempts") or [])
                    if isinstance(value, Mapping)
                ]
                latest = attempts[-1] if attempts else {}
                task_id = _trace_native_identifier(action.get("native_result"))
                assignment = assignments_by_token.get(
                    str(action.get("assignment_token") or "")
                )
                if not task_id and isinstance(assignment, Mapping):
                    task_id = assignment.get("task_id")
                items.append(
                    {
                        "time": action.get("completed_at")
                        or action.get("claimed_at")
                        or action.get("created_at"),
                        "id": action.get("action_id"),
                        "bead_id": bead_id,
                        "task_id": task_id,
                        "turn_id": (
                            assignment.get("turn_id")
                            if isinstance(assignment, Mapping)
                            else None
                        ),
                        "operation_id": None,
                        "action_id": action.get("action_id"),
                        "attempt_id": latest.get("attempt_id"),
                        "assignment_token": action.get("assignment_token"),
                        "transition": "native_action",
                        "effect": action.get("tool"),
                        "outcome": action.get("state"),
                        "evidence": action.get("purpose"),
                        "compiled_contract": (
                            assignment.get("scope")
                            if isinstance(assignment, Mapping)
                            else None
                        ),
                        "source_oid": None,
                        "source_oids": [],
                        "parent_operation_ids": [],
                        "child_operation_ids": [],
                        "provider_handles": [],
                    }
                )
                if action.get("tool") != "create_thread" or action.get("state") not in {
                    "succeeded",
                    "uncertain",
                }:
                    continue
                if not task_id:
                    durable_gaps.append(
                        {
                            "component": "native_task",
                            "action_id": action.get("action_id"),
                            "reason": "created task identity is not joined",
                        }
                    )
                elif str(task_id) not in lifecycle_tasks:
                    durable_gaps.append(
                        {
                            "component": "native_lifecycle",
                            "action_id": action.get("action_id"),
                            "task_id": task_id,
                            "reason": "created task lifecycle has not been observed",
                        }
                    )
            for index, failure in enumerate(
                value
                for value in (desktop.get("registration_failures") or [])
                if isinstance(value, Mapping)
            ):
                terminal = failure.get("terminal_event")
                items.append(
                    {
                        "time": failure.get("recorded_at"),
                        "id": f"{record.id}:registration-failure:{index}",
                        "bead_id": bead_id,
                        "task_id": failure.get("task_id"),
                        "turn_id": (
                            terminal.get("turn_id")
                            if isinstance(terminal, Mapping)
                            else None
                        ),
                        "operation_id": None,
                        "action_id": failure.get("creation_action_id"),
                        "assignment_token": failure.get("assignment_token"),
                        "transition": "registration_failed",
                        "effect": failure.get("role"),
                        "outcome": "capacity_released",
                        "evidence": terminal or {},
                        "source_oid": None,
                        "source_oids": [],
                        "parent_operation_ids": [],
                        "child_operation_ids": [],
                        "provider_handles": [],
                    }
                )
        for index, assignment in enumerate(assignment_rows):
            items.append(
                {
                    "time": assignment.get("registered_at")
                    or assignment.get("reserved_at"),
                    "id": f"{record.id}:assignment:{index}",
                    "bead_id": bead_id,
                    "task_id": assignment.get("task_id"),
                    "turn_id": assignment.get("turn_id"),
                    "operation_id": assignment.get("finish_operation"),
                    "transition": "task_observed",
                    "effect": assignment.get("role"),
                    "outcome": assignment.get("state"),
                    "evidence": [],
                    "source_oid": None,
                    "source_oids": [],
                    "parent_operation_ids": [],
                    "child_operation_ids": [],
                    "provider_handles": [],
                }
            )
        items.append(
            {
                "time": record.native.get("updated_at")
                or record.native.get("created_at"),
                "id": f"{record.id}:native",
                "bead_id": bead_id,
                "task_id": fc.get("owner"),
                "turn_id": None,
                "operation_id": fc.get("last_transition"),
                "transition": "native_observation",
                "effect": "work_observed",
                "outcome": record.status,
                "evidence": [],
                "source_oid": (
                    fc.get("delivery", {}).get("source_oid")
                    if isinstance(fc.get("delivery"), Mapping)
                    else None
                ),
                "source_oids": _trace_source_oids(fc),
                "parent_operation_ids": [],
                "child_operation_ids": [],
                "provider_handles": _trace_provider_handles(fc),
            }
        )
        retained_log = DiagnosticLog.from_request(request)
        log_result = retained_log.read(bead=bead_id, limit=0)
        event_gaps: list[dict[str, Any]] = []
        for event in log_result["items"]:
            items.append(
                {
                    "time": event.get("time"),
                    "id": event.get("event_id") or f"log:{len(items)}",
                    "bead_id": bead_id,
                    "task_id": event.get("task_id"),
                    "turn_id": event.get("turn_id"),
                    "operation_id": event.get("operation_id"),
                    "transition": event.get("event"),
                    "effect": event.get("command") or event.get("trigger"),
                    "outcome": event.get("outcome"),
                    "duration_ms": event.get("duration_ms"),
                    "evidence": event.get("result") or [],
                    "source_oid": event.get("source_oid"),
                    "source_oids": _trace_source_oids(event),
                    "parent_operation_ids": [],
                    "child_operation_ids": [],
                    "provider_handles": _trace_provider_handles(event),
                    "wait_reason": event.get("reason"),
                    "span_id": event.get("publication_span_id")
                    or event.get("pass_id")
                    or event.get("span_id"),
                    "correlation_id": event.get("correlation_id"),
                    "scope_revision": event.get("scope_revision"),
                    "compiled_contract": event.get("compiled_contract"),
                    "stages": event.get("stages"),
                }
            )
            retained_gaps = event.get("gaps")
            if isinstance(retained_gaps, list):
                for gap in retained_gaps:
                    if isinstance(gap, Mapping):
                        event_gaps.append(
                            {
                                **dict(gap),
                                "event_id": event.get("event_id"),
                                "pass_id": event.get("pass_id"),
                            }
                        )
        items.sort(key=lambda item: (str(item.get("time") or ""), str(item["id"])))
        cursor = _optional_string(request.arguments.get("cursor"))
        start = _cursor_start(
            items,
            cursor,
            key=lambda item: (str(item.get("time") or ""), str(item["id"])),
        )
        limit = _limit(request.arguments.get("limit"))
        selected = items[start:] if limit == 0 else items[start : start + limit]
        next_cursor = (
            encode_cursor(
                (str(selected[-1].get("time") or ""), str(selected[-1]["id"]))
            )
            if selected and start + len(selected) < len(items)
            else None
        )
        gaps = [*durable_gaps, *log_result["gaps"], *event_gaps]
        pruning = retained_log.read(limit=0)
        for event in pruning["items"]:
            if event.get("event") == "logs_pruned" and event.get("removed_files"):
                gaps.append(
                    {
                        "component": "logs",
                        "reason": "older diagnostic logs were pruned",
                        "removed_files": event["removed_files"],
                    }
                )
        if not retained_log.files():
            gaps.append(
                {
                    "component": "logs",
                    "reason": "no retained diagnostic logs; trace is durable-only",
                }
            )
        return CommandResult.query(
            {
                "items": selected,
                "next_cursor": next_cursor,
                "gaps": gaps,
                "source_oids": sorted(
                    {
                        source_oid
                        for item in items
                        for source_oid in item.get("source_oids", [])
                    }
                ),
            }
        )

    def wait(self, request: ParsedRequest) -> CommandResult:
        bead_id = str(request.arguments["bead"])
        expected = str(request.arguments["until"])
        deadline = time.monotonic() + request.timeout
        ledger = _ledger(request)
        while True:
            record = ledger.show(bead_id)
            if record is None or record.kind != "work":
                raise FulcrumError.invalid("NOT_FOUND", f"unknown work {bead_id}")
            view = work_view(ledger, record)
            if (expected == "closed" and record.status == "closed") or view.get(
                "phase"
            ) == expected:
                return CommandResult.query(
                    {
                        "bead_id": bead_id,
                        "until": expected,
                        "observed_at": utc_now(),
                        "work": view,
                    }
                )
            if time.monotonic() >= deadline:
                raise FulcrumError(
                    "WAIT_TIMEOUT",
                    f"work {bead_id} did not reach {expected} before the client timeout",
                    exit_code=3,
                    state=CommandState.RUNNING,
                    details={
                        "bead_id": bead_id,
                        "until": expected,
                        "observed_at": utc_now(),
                        "work": view,
                    },
                    next_command=("fulcrum", "work", "show", bead_id, "--json"),
                )
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))


def _trace_operation_mentions_bead(operation: Mapping[str, Any], bead_id: str) -> bool:
    if operation.get("bead_id") == bead_id:
        return True

    def visit(value: Any, key: str | None = None) -> bool:
        if isinstance(value, Mapping):
            return any(
                visit(child, str(child_key)) for child_key, child in value.items()
            )
        if isinstance(value, (list, tuple)):
            return any(visit(child, key) for child in value)
        return (
            isinstance(value, str)
            and value == bead_id
            and key
            in {
                "bead",
                "bead_id",
                "work_bead",
                "associated_beads",
                "selected_ids",
                "changes",
                "id",
            }
        )

    return visit(operation)


def _trace_native_identifier(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    pending: list[Mapping[str, Any]] = [value]
    seen: set[int] = set()
    while pending:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        for field in ("threadId", "thread_id", "id", "clientThreadId"):
            candidate = current.get(field)
            if isinstance(candidate, str) and candidate:
                return candidate
        for child in current.values():
            if isinstance(child, Mapping):
                pending.append(child)
    return None


def _trace_child_operation_ids(operation: Mapping[str, Any]) -> list[str]:
    retained: list[str] = []

    def visit(value: Any, *, within_children: bool = False) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                child_context = within_children or str(key) == "children"
                if (
                    child_context
                    and str(key) == "operation_id"
                    and isinstance(child, str)
                    and child.startswith("fc-")
                    and child not in retained
                ):
                    retained.append(child)
                visit(child, within_children=child_context)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child, within_children=within_children)

    visit(operation.get("planned"))
    visit(operation.get("result"))
    return retained


def _trace_source_oids(value: Any) -> list[str]:
    retained: list[str] = []

    def visit(item: Any, key: str | None = None) -> None:
        if isinstance(item, Mapping):
            for child_key, child in item.items():
                visit(child, str(child_key))
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child, key)
        elif (
            isinstance(item, str)
            and re.fullmatch(r"[0-9a-f]{40}", item)
            and key is not None
            and (key == "oid" or key.endswith("source_oid"))
            and item not in retained
        ):
            retained.append(item)

    visit(value)
    return retained


def _trace_provider_handles(value: Any) -> list[str]:
    retained: list[str] = []

    def visit(item: Any, key: str | None = None) -> None:
        if isinstance(item, Mapping):
            for child_key, child in item.items():
                visit(child, str(child_key))
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child, key)
        elif (
            isinstance(item, str)
            and key in {"provider_handle", "provider_id", "submission_handle"}
            and item not in retained
        ):
            retained.append(item)

    visit(value)
    return retained


def redact(value: Any, *, capture_bytes: int = DEFAULT_CHUNK_BYTES) -> Any:
    secrets: list[str] = [
        secret
        for key, secret in os.environ.items()
        if _SECRET_KEY.search(key) and len(secret) >= 8
    ]

    def visit(item: Any, key: str | None = None) -> Any:
        if key and _SECRET_KEY.search(key):
            return "[REDACTED]"
        if isinstance(item, Mapping):
            return {
                str(child_key): visit(child, str(child_key))
                for child_key, child in item.items()
            }
        if isinstance(item, (list, tuple)):
            return [visit(child) for child in item]
        if isinstance(item, str):
            text = _BEARER.sub(r"\1[REDACTED]", item)
            for secret in secrets:
                text = text.replace(secret, "[REDACTED]")
            encoded = text.encode("utf-8")
            if key in {"stdout", "stderr"} and len(encoded) > capture_bytes:
                text = encoded[:capture_bytes].decode("utf-8", errors="ignore")
                return {
                    "text": text,
                    "truncated": True,
                    "captured_bytes": capture_bytes,
                }
            return text
        return item

    return visit(value)


def encode_cursor(key: tuple[str, str]) -> str:
    raw = json.dumps(list(key), separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(value: str) -> tuple[str, str]:
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if not isinstance(decoded, list) or len(decoded) != 2:
            raise ValueError
        return str(decoded[0]), str(decoded[1])
    except Exception as error:
        raise FulcrumError.invalid("INVALID_CURSOR", "cursor is not valid") from error


def _event_key(event: Mapping[str, Any]) -> tuple[str, str]:
    return str(event.get("time") or ""), str(event.get("event_id") or "")


def _cursor_start(
    items: Sequence[Any],
    cursor: str | None,
    *,
    key: Any = _event_key,
) -> int:
    if not cursor:
        return 0
    target = decode_cursor(cursor)
    for index, item in enumerate(items):
        if key(item) > target:
            return index
    return len(items)


def _optional_string(value: Any) -> str | None:
    return str(value) if value is not None else None


def _limit(value: Any) -> int:
    result = int(value) if value is not None else DEFAULT_LIMIT
    if result < 0:
        raise FulcrumError.invalid("INVALID_LIMIT", "limit cannot be negative")
    return result


def _ledger(request: ParsedRequest) -> Ledger:
    if request.instance.brain_root is None:
        raise LedgerFailure(
            "a valid brain root is required",
            category="unavailable",
            retryable=False,
        )
    return Ledger(request.instance.brain_root, timeout=request.timeout)


def _health(
    name: str,
    state: str,
    *,
    evidence: Mapping[str, Any],
    affected_commands: Sequence[str],
    next_commands: Sequence[Sequence[str]] = (),
    last_success_at: str | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "state": state,
        "last_success_at": last_success_at,
        "evidence": dict(evidence),
        "affected_commands": list(affected_commands),
        "next_commands": [list(command) for command in next_commands],
    }


def _doctor_result(
    components: Sequence[Mapping[str, Any]], loops: Sequence[Mapping[str, Any]]
) -> CommandResult:
    result = {
        "components": [dict(item) for item in components],
        "loops": [dict(item) for item in loops],
    }
    issues = [
        {"kind": kind, "name": item.get("name"), "state": item.get("state")}
        for kind, items in (("component", components), ("loop", loops))
        for item in items
        if item.get("state") not in NON_FAILURE_HEALTH_STATES
    ]
    if not issues:
        return CommandResult.query(result)
    return CommandResult(
        ok=False,
        state=CommandState.FAILED,
        result=result,
        error=ErrorInfo(
            code="HEALTH_CHECK_FAILED",
            message="one or more Fulcrum components or loops are not healthy",
            retryable=True,
            details={"issues": issues},
        ),
    )


def _desktop_protocol_component(
    request: ParsedRequest, now: datetime
) -> dict[str, Any]:
    try:
        ledger = _ledger(request)
        pending = 0
        uncertain = 0
        stale = 0
        incidents = 0
        provider_jobs = 0
        accounting_gaps = 0
        for record in ledger.list_records(limit=0):
            fc = record.fc or {}
            desktop = fc.get("desktop")
            if isinstance(desktop, Mapping):
                for action in (desktop.get("actions") or {}).values():
                    if not isinstance(action, Mapping) or action.get("state") not in {
                        "pending",
                        "issuing",
                        "uncertain",
                    }:
                        continue
                    pending += 1
                    uncertain += int(action.get("state") == "uncertain")
                    created = _parse_time(action.get("created_at"))
                    stale += int(
                        created is not None and now - created >= timedelta(minutes=30)
                    )
                incidents += sum(
                    1
                    for value in (desktop.get("incidents") or {}).values()
                    if isinstance(value, Mapping) and value.get("state") != "resolved"
                )
                candidate = desktop.get("candidate")
                provider_jobs += int(
                    isinstance(candidate, Mapping)
                    and candidate.get("state") in {"pending", "running", "queued"}
                )
            if record.kind == "analytics" and fc.get("missing_reasons"):
                accounting_gaps += 1
        state = "degraded" if uncertain or stale or accounting_gaps else "healthy"
        return _health(
            "desktop_protocol",
            state,
            evidence={
                "pending_actions": pending,
                "uncertain_actions": uncertain,
                "actions_older_than_30m": stale,
                "open_incidents": incidents,
                "active_provider_jobs": provider_jobs,
                "accounting_gaps": accounting_gaps,
            },
            affected_commands=(
                ["native action dispatch", "recovery"] if state != "healthy" else []
            ),
            next_commands=(
                [["fulcrum", "status", "--json"]] if state != "healthy" else []
            ),
        )
    except (FulcrumError, LedgerFailure) as error:
        return _health(
            "desktop_protocol",
            "unknown",
            evidence={"error": str(error)},
            affected_commands=["native action dispatch", "recovery"],
        )


def _loop_health(
    request: ParsedRequest, config: Mapping[str, Any] | None, now: datetime
) -> list[dict[str, Any]]:
    try:
        system = _ledger(request).show("fc-system")
        desktop = (system.fc or {}).get("desktop") if system else None
        schedule = (
            desktop.get("marshal_schedule") if isinstance(desktop, Mapping) else None
        )
        run_control = (
            str(desktop.get("run_control") or "paused")
            if isinstance(desktop, Mapping)
            else "paused"
        )
        schedule_status = (
            schedule.get("status") if isinstance(schedule, Mapping) else None
        )
        intentionally_paused = (
            run_control == "paused"
            and isinstance(schedule, Mapping)
            and schedule.get("state") == "succeeded"
            and schedule_status in {None, "PAUSED"}
        )
        activated_at = _parse_health_time(
            schedule.get("activated_at") if isinstance(schedule, Mapping) else None
        )
        last_delivery_at = _parse_health_time(
            schedule.get("last_delivery_at") if isinstance(schedule, Mapping) else None
        )
        last_cycle_completed_at = _parse_health_time(
            schedule.get("last_cycle_completed_at")
            if isinstance(schedule, Mapping)
            else None
        )
        if (
            activated_at is not None
            and last_delivery_at is not None
            and last_delivery_at < activated_at
        ):
            last_delivery_at = None
        if (
            activated_at is not None
            and last_cycle_completed_at is not None
            and last_cycle_completed_at < activated_at
        ):
            last_cycle_completed_at = None
        interval = timedelta(minutes=15)
        first_delivery_grace = timedelta(minutes=20)
        cycle_grace = timedelta(minutes=10)
        stale_after = timedelta(minutes=35)
        if intentionally_paused:
            state = "paused"
        elif not (
            isinstance(schedule, Mapping)
            and schedule.get("state") == "succeeded"
            and schedule_status == "ACTIVE"
        ):
            state = "unavailable"
        elif last_delivery_at is not None and (
            last_cycle_completed_at is None
            or last_cycle_completed_at < last_delivery_at
        ):
            state = "running" if now - last_delivery_at <= cycle_grace else "degraded"
        elif last_cycle_completed_at is not None:
            state = (
                "healthy"
                if now - last_cycle_completed_at <= stale_after
                else "degraded"
            )
        elif activated_at is not None and now - activated_at <= first_delivery_grace:
            state = "initializing"
        else:
            state = "degraded"
        return [
            _health(
                "marshal_heartbeat",
                state,
                evidence={
                    "interval_seconds": int(interval.total_seconds()),
                    "first_delivery_grace_seconds": int(
                        first_delivery_grace.total_seconds()
                    ),
                    "cycle_grace_seconds": int(cycle_grace.total_seconds()),
                    "stale_after_seconds": int(stale_after.total_seconds()),
                    "schedule": schedule,
                    "run_control": run_control,
                    "intentionally_paused": intentionally_paused,
                },
                affected_commands=(
                    [] if state in NON_FAILURE_HEALTH_STATES else ["scheduled recovery"]
                ),
                next_commands=(
                    []
                    if state in NON_FAILURE_HEALTH_STATES
                    else [["fulcrum", "bootstrap", "--json"]]
                ),
                last_success_at=(
                    last_cycle_completed_at.isoformat().replace("+00:00", "Z")
                    if last_cycle_completed_at is not None
                    else None
                ),
            )
        ]
    except (FulcrumError, LedgerFailure) as error:
        return [
            _health(
                "marshal_heartbeat",
                "unknown",
                evidence={"error": str(error)},
                affected_commands=["scheduled recovery"],
            )
        ]


def _parse_health_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _capacity(request: ParsedRequest, ledger: Ledger) -> dict[str, Any]:
    manager = ConfigurationManager(request.instance.config_path)
    document, _ = manager.load()
    config = manager.effective(document)
    limit = int(config["policy"]["automatic_capacity"])
    active: list[dict[str, Any]] = []
    recovery: list[dict[str, Any]] = []
    same_task_entries: list[dict[str, Any]] = []
    for record in ledger.list_records(limit=0):
        desktop = (record.fc or {}).get("desktop")
        assignment = desktop.get("assignment") if isinstance(desktop, Mapping) else None
        if not isinstance(assignment, Mapping) or assignment.get("state") not in {
            "reserved",
            "issuing",
            "active",
            "uncertain",
        }:
            continue
        row = {"bead": record.id, **dict(assignment)}
        capacity_class = assignment.get("capacity_class")
        if capacity_class == "recovery":
            recovery.append(row)
        elif capacity_class == "entry":
            same_task_entries.append(row)
        else:
            active.append(row)
    return {
        "limit": limit,
        "used": len(active),
        "available": max(0, limit - len(active)),
        "active": active,
        "same_task_entries": same_task_entries,
        "recovery_slots": {"limit": 1, "used": len(recovery), "active": recovery},
    }
