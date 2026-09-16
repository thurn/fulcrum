"""Bounded structured diagnostics and durable operator projections."""

from __future__ import annotations

import base64
import json
import os
import re
import time
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fulcrum.configuration import ConfigurationManager, default_config
from fulcrum.contracts import CommandResult, CommandState, FulcrumError, ParsedRequest
from fulcrum.ledger import Ledger, LedgerFailure, OperationRecord, operation_view
from fulcrum.work import work_view

DEFAULT_LIMIT = 20
DEFAULT_CHUNK_BYTES = 256 * 1024
_SECRET_KEY: re.Pattern[str] = re.compile(
    r"(?:token|secret|password|passwd|credential|authorization|api[_-]?key|cookie)",
    re.IGNORECASE,
)
_BEARER: re.Pattern[str] = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _runtime_component(request: ParsedRequest) -> dict[str, Any]:
    from fulcrum.runtime_service import RuntimeService

    try:
        result = RuntimeService().capabilities(request).result
        facts = dict(result) if isinstance(result, Mapping) else {}
        available = bool(facts.get("available"))
        models = facts.get("models")
        evidence = {
            "endpoint": facts.get("endpoint"),
            "available": available,
            "methods": list(facts.get("methods") or []),
            "models": sorted(models) if isinstance(models, Mapping) else [],
            "gaps": list(facts.get("gaps") or []),
        }
        state = "healthy" if available else "unavailable"
    except FulcrumError as error:
        state = "unsupported" if error.code == "RUNTIME_UNSUPPORTED" else "unavailable"
        evidence = {"error": error.message}
    healthy = state == "healthy"
    return _health(
        "runtime",
        state,
        evidence=evidence,
        affected_commands=[] if healthy else ["enter", "task", "dispatch"],
        next_commands=(
            [] if healthy else [["fulcrum", "runtime", "capabilities", "--json"]]
        ),
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
        path = self._active_path(len(encoded))
        with path.open("ab") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        self.prune()
        return retained

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
                    if bead and value.get("bead_id") != bead:
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
                    if item.kind
                    not in {"control", "task", "memory", "analytics", "operation"}
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
            capacity = _capacity(request, ledger)
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
        return CommandResult.query(
            {
                "observed_at": observed_at,
                "instance": request.instance.to_dict(),
                "work": work,
                "work_next_cursor": work_next_cursor,
                "operations": operations,
                "capacity": capacity,
                "publication": publication,
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
                    ["automatic reconciliation"] if not socket_exists else []
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
        components.append(_runtime_component(request))
        loops = _loop_health(request, config, now)
        return CommandResult.query({"components": components, "loops": loops})

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
        if not isinstance(bead_id, str) or not bead_id:
            raise FulcrumError.invalid("INVALID_INPUT", "trace requires --bead")
        ledger = _ledger(request)
        record = ledger.show(bead_id)
        if record is None or record.kind != "work":
            raise FulcrumError.invalid("NOT_FOUND", f"unknown work {bead_id}")
        items: list[dict[str, Any]] = []
        for candidate in ledger.list_records(kind="operation", limit=0):
            operation = OperationRecord.from_record(candidate)
            fc = operation.operation
            if fc.get("bead_id") != bead_id and operation.id not in {
                (record.fc or {}).get("ownership_operation"),
                (record.fc or {}).get("last_transition"),
            }:
                continue
            result = fc.get("result") if isinstance(fc.get("result"), Mapping) else {}
            items.append(
                {
                    "time": fc.get("completed_at") or fc.get("created_at"),
                    "id": operation.id,
                    "bead_id": bead_id,
                    "task_id": fc.get("owner"),
                    "turn_id": None,
                    "operation_id": operation.id,
                    "transition": fc.get("step"),
                    "effect": fc.get("command"),
                    "outcome": fc.get("state"),
                    "evidence": result.get("evidence", []),
                    "source_oid": result.get("source_oid"),
                }
            )
        fc = record.fc or {}
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
        retained_log = DiagnosticLog.from_request(request)
        log_result = retained_log.read(bead=bead_id, limit=1)
        gaps = list(log_result["gaps"])
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
            {"items": selected, "next_cursor": next_cursor, "gaps": gaps}
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


def _loop_health(
    request: ParsedRequest, config: Mapping[str, Any] | None, now: datetime
) -> list[dict[str, Any]]:
    health_path = request.instance.instance_root / "service-health.json"
    retained: Mapping[str, Any] = {}
    try:
        value = json.loads(health_path.read_text(encoding="utf-8"))
        if isinstance(value, Mapping):
            retained = value
    except (OSError, json.JSONDecodeError):
        pass
    timing = config.get("timing", {}) if config else {}
    reconcile = (
        float(timing.get("reconcile_seconds", 15))
        if isinstance(timing, Mapping)
        else 15
    )
    external = (
        float(timing.get("external_timeout_seconds", 30))
        if isinstance(timing, Mapping)
        else 30
    )
    deadlines = {
        "intake": (
            float(timing.get("intake_idle_seconds", 10)) * 2 + external
            if isinstance(timing, Mapping)
            else 50
        ),
        "event": (
            float(timing.get("event_silence_seconds", 120))
            if isinstance(timing, Mapping)
            else 120
        ),
        "reconciliation": reconcile * 2 + external,
        "runner": reconcile * 2 + external,
    }
    result: list[dict[str, Any]] = []
    for name, deadline in deadlines.items():
        value = retained.get(name)
        last = value.get("last_success_at") if isinstance(value, Mapping) else None
        retained_state = value.get("state") if isinstance(value, Mapping) else None
        failures = (
            int(value.get("consecutive_failures", 0))
            if isinstance(value, Mapping)
            else 0
        )
        age: float | None = None
        if isinstance(last, str):
            try:
                age = (
                    now - datetime.fromisoformat(last.replace("Z", "+00:00"))
                ).total_seconds()
            except ValueError:
                age = None
        if retained_state == "unavailable" and failures:
            state = "unavailable"
        else:
            state = (
                "unknown" if age is None else ("stale" if age > deadline else "healthy")
            )
        result.append(
            _health(
                name,
                state,
                evidence={
                    "expected_interval_seconds": (
                        reconcile if name in {"reconciliation", "runner"} else None
                    ),
                    "deadline_seconds": deadline,
                    "age_seconds": age,
                    "path": str(health_path),
                    "consecutive_failures": failures,
                    "error": (
                        value.get("error") if isinstance(value, Mapping) else None
                    ),
                },
                affected_commands=(
                    ["automatic workflow progress"] if state != "healthy" else []
                ),
                next_commands=(
                    [["fulcrum", "reconcile", "--json"]] if state == "stale" else []
                ),
                last_success_at=last if isinstance(last, str) else None,
            )
        )
    return result


def _capacity(request: ParsedRequest, ledger: Ledger) -> dict[str, Any]:
    from fulcrum.leadership import capacity_snapshot

    manager = ConfigurationManager(request.instance.config_path)
    document, _ = manager.load()
    config = manager.effective(document)
    return capacity_snapshot(ledger, config)
