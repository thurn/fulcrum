"""Stock-Beads workflow ledger and receipt lifecycle.

Fulcrum invokes the public ``bd`` executable only. The classes in this module are
typed views over native issues; they are not a second persistence layer.
"""

from __future__ import annotations

from fulcrum.timing import timed

import json
import copy
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from fulcrum.coordination import ProcessLock, merge_change
from fulcrum.contracts import CommandResult, CommandState, FulcrumError, ParsedRequest

CAPTURE_BYTES = 256 * 1024
JSON_BYTES = 64 * 1024 * 1024
_PROCESS_SLOTS = threading.BoundedSemaphore(4)


class LedgerFailure(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        category: str,
        retryable: bool,
        uncertain: bool = False,
        stdout: str = "",
        stderr: str = "",
        returncode: int | None = None,
        duration_ms: int | None = None,
        truncated: bool = False,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.retryable = retryable
        self.uncertain = uncertain
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.duration_ms = duration_ms
        self.truncated = truncated


@dataclass(frozen=True)
class CommandObservation:
    value: Any
    duration_ms: int
    stdout: str
    stderr: str
    truncated: bool


@dataclass(frozen=True)
class LedgerRecord:
    id: str
    title: str
    status: str
    assignee: str | None
    labels: tuple[str, ...]
    metadata: Mapping[str, Any]
    native: Mapping[str, Any]

    @property
    def fc(self) -> Mapping[str, Any] | None:
        value = self.metadata.get("fc")
        return value if isinstance(value, dict) else None

    @property
    def kind(self) -> str | None:
        value = self.fc
        kind = value.get("kind") if value else None
        return kind if isinstance(kind, str) else None

    @classmethod
    def from_native(cls, issue: Mapping[str, Any]) -> LedgerRecord:
        metadata = issue.get("metadata")
        labels = issue.get("labels")
        assignee = issue.get("assignee", issue.get("owner"))
        return cls(
            id=str(issue.get("id", "")),
            title=str(issue.get("title", "")),
            status=str(issue.get("status", "")),
            assignee=str(assignee) if assignee else None,
            labels=(
                tuple(str(item) for item in labels) if isinstance(labels, list) else ()
            ),
            metadata=metadata if isinstance(metadata, dict) else {},
            native=dict(issue),
        )


@dataclass(frozen=True)
class OperationRecord(LedgerRecord):
    @property
    def operation(self) -> Mapping[str, Any]:
        return self.fc or {}

    @classmethod
    def from_record(cls, record: LedgerRecord) -> OperationRecord:
        if record.kind != "operation":
            raise FulcrumError.invalid(
                "WRONG_RECORD_KIND",
                f"{record.id} is not an operation receipt",
                details={"id": record.id, "kind": record.kind},
            )
        return cls(**record.__dict__)


def operation_id(request_id: str) -> str:
    try:
        parsed = uuid.UUID(request_id)
    except ValueError as error:
        raise FulcrumError.invalid(
            "INVALID_REQUEST_ID", "request ID must be a UUID"
        ) from error
    return "fc-" + parsed.hex


def random_record_id() -> str:
    return "fc-" + uuid.uuid4().hex[:8]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


_LEDGER_LOCK_GUARD = threading.RLock()
_LEDGER_RECORD_LOCKS: dict[tuple[str, str], threading.RLock] = {}


class Ledger:
    def __init__(
        self,
        workspace: Path,
        *,
        executable: str | None = None,
        actor: str = "fulcrum-controller",
        timeout: float = 30.0,
        dolt_auto_commit: str | None = None,
    ) -> None:
        requested = executable or "bd"
        resolved = (
            shutil.which(requested) if not Path(requested).is_absolute() else requested
        )
        if not resolved or not Path(resolved).is_file():
            raise LedgerFailure(
                f"stock Beads executable was not found: {requested}",
                category="unavailable",
                retryable=False,
            )
        self.executable = str(Path(resolved).resolve(strict=True))
        self.workspace: Path = workspace.resolve(strict=False)
        self.actor = actor
        self.timeout = timeout
        self.dolt_auto_commit = dolt_auto_commit
        self._observed: dict[str, dict[str, Any]] = {}

    def _lock_for(self, bead_id: str | None) -> ProcessLock:
        return ProcessLock(self.workspace / ".fulcrum-locks" / "state")

    @timed("ledger.run")
    def run(
        self,
        arguments: Sequence[str],
        *,
        mutating: bool = False,
        timeout: float | None = None,
    ) -> CommandObservation:
        argv = [
            self.executable,
            "--json",
            "--actor",
            self.actor,
            *(
                ["--dolt-auto-commit", self.dolt_auto_commit]
                if self.dolt_auto_commit is not None
                else []
            ),
            "-C",
            str(self.workspace),
            *[str(item) for item in arguments],
        ]
        started = time.monotonic()
        with (
            _PROCESS_SLOTS,
            tempfile.TemporaryFile() as stdout_file,
            tempfile.TemporaryFile() as stderr_file,
        ):
            try:
                completed = subprocess.run(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    timeout=timeout or self.timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                stdout, stdout_truncated = _read_capture(stdout_file)
                stderr, stderr_truncated = _read_capture(stderr_file)
                raise LedgerFailure(
                    "Beads command timed out",
                    category="uncertain" if mutating else "transient",
                    retryable=True,
                    uncertain=mutating,
                    stdout=stdout,
                    stderr=stderr,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    truncated=stdout_truncated or stderr_truncated,
                ) from error
            except OSError as error:
                raise LedgerFailure(
                    f"could not execute Beads: {error}",
                    category="unavailable",
                    retryable=True,
                    uncertain=False,
                    duration_ms=int((time.monotonic() - started) * 1000),
                ) from error
            stdout, stdout_truncated = _read_capture(stdout_file)
            stderr, stderr_truncated = _read_capture(stderr_file)
            stdout_json, stdout_json_truncated = _read_capture(
                stdout_file, limit=JSON_BYTES
            )
        duration = int((time.monotonic() - started) * 1000)
        truncated = stdout_truncated or stderr_truncated
        if completed.returncode != 0:
            category, retryable = _classify_failure(stdout, stderr)
            raise LedgerFailure(
                stderr.strip() or stdout.strip() or "Beads command failed",
                category=category,
                retryable=retryable,
                uncertain=False,
                stdout=stdout,
                stderr=stderr,
                returncode=completed.returncode,
                duration_ms=duration,
                truncated=truncated,
            )
        if not stdout.strip():
            value: Any = None
        else:
            if stdout_json_truncated:
                raise LedgerFailure(
                    "Beads JSON exceeded the supported response size",
                    category="unsupported",
                    retryable=False,
                    stdout=stdout,
                    stderr=stderr,
                    returncode=completed.returncode,
                    duration_ms=duration,
                    truncated=True,
                )
            try:
                value = json.loads(stdout_json)
            except json.JSONDecodeError as error:
                raise LedgerFailure(
                    "Beads returned malformed JSON",
                    category="uncertain" if mutating else "unsupported",
                    retryable=False,
                    uncertain=mutating,
                    stdout=stdout,
                    stderr=stderr,
                    returncode=completed.returncode,
                    duration_ms=duration,
                    truncated=truncated,
                ) from error
        return CommandObservation(value, duration, stdout, stderr, truncated)

    def show(self, record_id: str) -> LedgerRecord | None:
        try:
            value = self.run(("show", record_id)).value
        except LedgerFailure as error:
            detail = str(error).lower()
            if error.category == "rejected" and (
                "not found" in detail or "no issue found" in detail
            ):
                return None
            raise
        if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
            record = LedgerRecord.from_native(value[0])
            self._observed[record.id] = copy.deepcopy(dict(record.fc or {}))
            return record
        if isinstance(value, dict):
            record = LedgerRecord.from_native(value)
            self._observed[record.id] = copy.deepcopy(dict(record.fc or {}))
            return record
        return None

    def list_records(
        self, *, kind: str | None = None, limit: int = 20
    ) -> list[LedgerRecord]:
        arguments = ["list", "--all", "--flat", "--limit", str(limit)]
        if kind is not None:
            arguments.extend(("--label", f"fc:{kind}"))
        value = self.run(arguments).value
        records = (
            [LedgerRecord.from_native(item) for item in value if isinstance(item, dict)]
            if isinstance(value, list)
            else []
        )
        if kind is not None:
            records = [record for record in records if record.kind == kind]
        for record in records:
            self._observed[record.id] = copy.deepcopy(dict(record.fc or {}))
        return records

    def create_record(
        self,
        *,
        record_id: str,
        kind: str,
        title: str,
        description: str,
        owner: str,
        fc: Mapping[str, Any],
        issue_type: str = "chore",
        status: str = "open",
        priority: int = 2,
        acceptance: str | None = None,
        labels: Sequence[str] = (),
        parent: str | None = None,
        external_ref: str | None = None,
    ) -> LedgerRecord:
        if fc.get("kind") != kind:
            raise ValueError("record kind and metadata.fc.kind must agree")
        arguments = [
            "create",
            "--id",
            record_id,
            "--title",
            title,
            "--description",
            description,
            "--type",
            issue_type,
            "--priority",
            str(priority),
            "--assignee",
            owner,
            "--labels",
            ",".join((f"fc:{kind}", *labels)),
            "--metadata",
            json.dumps({"fc": dict(fc)}, separators=(",", ":")),
        ]
        if acceptance is not None:
            arguments.extend(("--acceptance", acceptance))
        if external_ref is not None:
            arguments.extend(("--external-ref", external_ref))
        if status != "open":
            arguments.extend(("--status", status))
        try:
            observation = self.run(arguments, mutating=True)
        except LedgerFailure as error:
            existing = (
                self.show(record_id)
                if error.uncertain or error.category == "rejected"
                else None
            )
            if existing is None:
                raise
            if existing.kind != kind or existing.fc != dict(fc):
                raise FulcrumError(
                    "REQUEST_CONFLICT",
                    f"existing record {record_id} does not match planned creation",
                    exit_code=5,
                    details={"id": record_id},
                ) from error
            created = existing
        else:
            created = _record_from_value(observation.value, record_id)
            if created is None or created.kind != kind or created.fc != dict(fc):
                created = self.show(record_id)
            elif created is not None:
                self._observed[record_id] = copy.deepcopy(dict(created.fc or {}))
        if created is None:
            raise LedgerFailure(
                f"created record {record_id} could not be observed",
                category="uncertain",
                retryable=True,
                uncertain=True,
            )
        if parent is not None:
            created = self.set_parent(record_id, parent)
        return created

    def set_parent(self, record_id: str, parent: str) -> LedgerRecord:
        if any(item.id == record_id for item in self.children(parent)):
            existing = self.show(record_id)
            if existing is not None:
                return existing
        try:
            self.run(("update", record_id, "--parent", parent), mutating=True)
        except LedgerFailure as error:
            if not error.uncertain or not any(
                item.id == record_id for item in self.children(parent)
            ):
                raise
        if not any(item.id == record_id for item in self.children(parent)):
            raise LedgerFailure(
                f"parent assignment for {record_id} could not be verified",
                category="uncertain",
                retryable=True,
                uncertain=True,
            )
        observed = self.show(record_id)
        if observed is None:
            raise LedgerFailure(
                f"parent assignment for {record_id} could not be observed",
                category="uncertain",
                retryable=True,
                uncertain=True,
            )
        return observed

    def update_fc(
        self,
        record_id: str,
        fc: Mapping[str, Any],
        *,
        assignee: str | None = None,
        status: str | None = None,
        title: str | None = None,
        priority: int | None = None,
        description: str | None = None,
        acceptance: str | None = None,
    ) -> LedgerRecord:
        with self._lock_for(record_id):
            base = self._observed.get(record_id)
            existing = self.show(record_id)
            if existing is None:
                raise FulcrumError.invalid(
                    "NOT_FOUND", f"unknown Beads record {record_id}"
                )
            if base is not None:
                for field in ("owner", "ownership_operation", "recovery_fence"):
                    if base.get(field) != (existing.fc or {}).get(field):
                        raise FulcrumError(
                            "OWNERSHIP_CONFLICT",
                            "ownership changed while an external effect was running",
                            exit_code=5,
                        )
                fc = merge_change(base, dict(fc), dict(existing.fc or {}))
            arguments = [
                "update",
                record_id,
                "--metadata",
                json.dumps({"fc": dict(fc)}, separators=(",", ":")),
            ]
            for flag, value in (
                ("--assignee", assignee),
                ("--status", status),
                ("--title", title),
                ("--priority", priority),
                ("--description", description),
                ("--acceptance", acceptance),
            ):
                if value is not None:
                    arguments.extend((flag, str(value)))
            observation = self.run(arguments, mutating=True)
            observed = _record_from_value(observation.value, record_id)
            if observed is None or observed.fc != dict(fc):
                observed = self.show(record_id)
            else:
                self._observed[record_id] = copy.deepcopy(dict(observed.fc or {}))
            if observed is None or observed.fc != dict(fc):
                raise LedgerFailure(
                    f"update of {record_id} could not be verified",
                    category="uncertain",
                    retryable=True,
                    uncertain=True,
                )
            return observed

    def add_labels(self, record_id: str, labels: Sequence[str]) -> LedgerRecord:
        observed: LedgerRecord | None = None
        if labels:
            observation = self.run(
                ("update", record_id, "--add-label", ",".join(labels)),
                mutating=True,
            )
            observed = _record_from_value(observation.value, record_id)
        if observed is None or any(label not in observed.labels for label in labels):
            observed = self.show(record_id)
        if observed is None or any(label not in observed.labels for label in labels):
            raise LedgerFailure(
                f"label update for {record_id} could not be verified",
                category="uncertain",
                retryable=True,
                uncertain=True,
            )
        return observed

    def dependencies(self, record_id: str) -> list[str]:
        value = self.run(("dep", "list", record_id, "--type", "blocks")).value
        if not isinstance(value, list):
            return []
        result: list[str] = []
        for item in value:
            if not isinstance(item, Mapping):
                continue
            identifier = (
                item.get("id") or item.get("depends_on_id") or item.get("dependency_id")
            )
            if isinstance(identifier, str):
                result.append(identifier)
        return result

    def children(self, record_id: str) -> list[LedgerRecord]:
        value = self.run(("children", record_id)).value
        return (
            [LedgerRecord.from_native(item) for item in value if isinstance(item, dict)]
            if isinstance(value, list)
            else []
        )

    @timed("ledger.cook")
    def cook(self, formula: str, variables: Mapping[str, str]) -> dict[str, str]:
        try:
            value = json.loads(Path(formula).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise LedgerFailure(
                f"role formula could not be read: {error}",
                category="unsupported",
                retryable=False,
            ) from error
        if not isinstance(value, Mapping):
            raise LedgerFailure(
                "role formula returned no object",
                category="unsupported",
                retryable=False,
            )
        declared = value.get("vars")
        if not isinstance(declared, Mapping) or any(
            isinstance(spec, Mapping) and spec.get("required") and key not in variables
            for key, spec in declared.items()
        ):
            raise LedgerFailure(
                "role formula is missing a required variable",
                category="unsupported",
                retryable=False,
            )
        steps = value.get("steps")
        if not isinstance(steps, list):
            raise LedgerFailure(
                "Beads formula returned no steps",
                category="unsupported",
                retryable=False,
            )
        work_steps = [
            step
            for step in steps
            if isinstance(step, Mapping) and step.get("id") == "work"
        ]
        if len(steps) != 1 or len(work_steps) != 1:
            raise LedgerFailure(
                "role formula must contain exactly one work step",
                category="unsupported",
                retryable=False,
            )
        title = work_steps[0].get("title")
        description = work_steps[0].get("description")
        if not isinstance(title, str) or not isinstance(description, str):
            raise LedgerFailure(
                "role formula work step has no title or description",
                category="unsupported",
                retryable=False,
            )
        token: re.Pattern[str] = re.compile(r"\{\{([A-Za-z0-9_]+)\}\}")

        def render(template: str) -> str:
            return token.sub(
                lambda match: variables.get(match.group(1), match.group(0)), template
            )

        return {"title": render(title), "description": render(description)}

    @timed("ledger.create_operation")
    def create_operation(
        self,
        request: ParsedRequest,
        *,
        bead_id: str | None = None,
        owner: str | None = None,
        planned: Mapping[str, Any] | None = None,
        next_action: str | None = None,
    ) -> tuple[OperationRecord, bool]:
        if request.request_id is None:
            raise ValueError("mutations require a request ID")
        record_id = operation_id(request.request_id)
        accepted = accepted_input(request)
        existing = self.show(record_id)
        if existing is not None:
            operation = OperationRecord.from_record(existing)
            fc = operation.operation
            if (
                fc.get("request_id") != request.request_id
                or fc.get("command") != request.command_name.replace(" ", ".")
                or fc.get("input") != accepted
            ):
                raise FulcrumError(
                    "REQUEST_CONFLICT",
                    f"request ID {request.request_id} was already used with different input",
                    exit_code=5,
                    request_id=request.request_id,
                    operation_id=record_id,
                    details={"operation_id": record_id},
                )
            return operation, True
        responsible = owner or request.thread_id or request.actor.task_id or "HUMAN"
        fc = {
            "kind": "operation",
            "owner": responsible,
            "request_id": request.request_id,
            "command": request.command_name.replace(" ", "."),
            "input": accepted,
            "bead_id": bead_id,
            "ownership_operation": request.ownership_operation,
            "state": "accepted",
            "step": "intent_recorded",
            "attempts": 0,
            "external": None,
            "planned": dict(planned or {}),
            "result": None,
            "error": None,
            "next_action": next_action,
            "created_at": utc_now(),
        }
        created = self.create_record(
            record_id=record_id,
            kind="operation",
            title=f"Operation: {request.command_name}",
            description=f"Receipt for {request.command_name} request {request.request_id}.",
            owner=responsible,
            fc=fc,
        )

        return OperationRecord.from_record(created), False

    @timed("ledger.update_operation")
    def update_operation(
        self,
        operation: OperationRecord | str,
        *,
        state: str | None = None,
        step: str | None = None,
        attempts: int | None = None,
        external: Mapping[str, Any] | None = None,
        result: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
        next_action: str | None = None,
        planned: Mapping[str, Any] | None = None,
    ) -> OperationRecord:
        record_id = (
            operation.id if isinstance(operation, OperationRecord) else operation
        )
        with self._lock_for(record_id):
            if isinstance(operation, OperationRecord):
                # Reuse the caller's snapshot as the merge baseline. update_fc still
                # reads fresh state under this same lock, rejects ownership changes,
                # merges unrelated updates, and verifies the resulting write.
                record = operation
                self._observed[record_id] = copy.deepcopy(dict(record.operation))
            else:
                current = self.show(record_id)
                if current is None:
                    raise FulcrumError.invalid(
                        "NOT_FOUND", f"unknown operation {record_id}"
                    )
                record = OperationRecord.from_record(current)
            fc = dict(record.operation)
            for key, value in (
                ("state", state),
                ("step", step),
                ("attempts", attempts),
                ("external", external),
                ("result", result),
                ("error", error),
                ("next_action", next_action),
                ("planned", planned),
            ):
                if value is not None:
                    fc[key] = value
            if state in {"completed", "failed", "cancelled"}:
                fc["completed_at"] = utc_now()
            updated = self.update_fc(record_id, fc)
            if (
                state in {"completed", "failed", "cancelled"}
                and updated.status != "closed"
            ):
                close_observation: CommandObservation | None = None
                try:
                    close_observation = self.run(
                        ("close", record_id, "--reason", f"operation {state}"),
                        mutating=True,
                    )
                except LedgerFailure:
                    observed = self.show(record_id)
                    if observed is None or observed.status != "closed":
                        raise
                    updated = observed
                if close_observation is not None:
                    updated = (
                        _record_from_value(close_observation.value, record_id)
                        or self.show(record_id)
                        or updated
                    )
            return OperationRecord.from_record(updated)


def _record_from_value(value: Any, record_id: str) -> LedgerRecord | None:
    candidates = value if isinstance(value, list) else [value]
    matches = [
        item
        for item in candidates
        if isinstance(item, Mapping) and str(item.get("id")) == record_id
    ]
    return LedgerRecord.from_native(matches[0]) if len(matches) == 1 else None


def accepted_input(request: ParsedRequest) -> dict[str, Any]:
    return {
        "arguments": dict(request.arguments),
        "input": dict(request.input),
        "project": request.project,
        "thread_id": request.thread_id,
        "actor": request.actor.to_dict(),
        "ownership_operation": request.ownership_operation,
    }


class OperationService:
    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    def show(self, request: ParsedRequest) -> CommandResult:
        record = self.ledger.show(str(request.arguments["id"]))
        if record is None:
            raise FulcrumError.invalid(
                "NOT_FOUND", f"unknown operation {request.arguments['id']}"
            )
        operation = OperationRecord.from_record(record)
        return CommandResult.query(operation_view(operation))

    def list(self, request: ParsedRequest) -> CommandResult:
        limit = int(request.arguments.get("limit", 20))
        records = self.ledger.list_records(kind="operation", limit=limit)
        return CommandResult.query(
            {
                "items": [
                    operation_view(OperationRecord.from_record(item))
                    for item in records
                ],
                "next_cursor": None,
            }
        )

    def wait(self, request: ParsedRequest) -> CommandResult:
        target = str(request.arguments["id"])
        deadline = time.monotonic() + request.timeout
        while True:
            record = self.ledger.show(target)
            if record is None:
                raise FulcrumError.invalid("NOT_FOUND", f"unknown operation {target}")
            operation = OperationRecord.from_record(record)
            state = operation.operation.get("state")
            if state in {"completed", "failed", "uncertain", "cancelled"}:
                return _operation_result(operation)
            if time.monotonic() >= deadline:
                raise FulcrumError(
                    "WAIT_TIMEOUT",
                    f"operation {target} did not settle before the client timeout",
                    exit_code=3,
                    state=(
                        CommandState(str(state))
                        if state in CommandState._value2member_map_
                        else CommandState.RUNNING
                    ),
                    operation_id=target,
                    details={"operation": operation_view(operation)},
                    next_command=("fulcrum", "operation", "show", target, "--json"),
                )
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))

    def cancel(self, request: ParsedRequest) -> CommandResult:
        target_id = str(request.arguments["id"])
        target = self.ledger.show(target_id)
        if target is None:
            raise FulcrumError.invalid("NOT_FOUND", f"unknown operation {target_id}")
        target_operation = OperationRecord.from_record(target)
        receipt, reused = self.ledger.create_operation(
            request,
            planned={"target_operation": target_id},
            next_action="Inspect the target operation and preserve any observed effect.",
        )
        if reused and receipt.operation.get("state") in {
            "completed",
            "failed",
            "cancelled",
        }:
            return _operation_result(receipt)
        target_state = target_operation.operation.get("state")
        external = target_operation.operation.get("external")
        if target_state in {"completed", "failed", "cancelled"}:
            result = {"target": operation_view(target_operation), "changed": False}
        elif external:
            receipt = self.ledger.update_operation(
                receipt,
                state="uncertain",
                step="external_effect_requires_inspection",
                result={"target": operation_view(target_operation), "changed": False},
                next_action="Inspect the recorded external locator before cancellation.",
            )
            return _operation_result(receipt)
        else:
            target_operation = self.ledger.update_operation(
                target_operation,
                state="cancelled",
                step="cancelled_before_external_effect",
                result={"cancelled_by": receipt.id},
                next_action="No further action is required.",
            )
            result = {"target": operation_view(target_operation), "changed": True}
        receipt = self.ledger.update_operation(
            receipt,
            state="completed",
            step="target_inspected",
            result=result,
            next_action="No further action is required.",
        )
        return _operation_result(receipt)

    def reconcile(self, request: ParsedRequest) -> CommandResult:
        target_id = str(request.arguments["id"])
        target = self.ledger.show(target_id)
        if target is None:
            raise FulcrumError.invalid("NOT_FOUND", f"unknown operation {target_id}")
        target_operation = OperationRecord.from_record(target)
        receipt, reused = self.ledger.create_operation(
            request,
            planned={"target_operation": target_id},
            next_action="Inspect the target operation's recorded postcondition.",
        )
        if reused and receipt.operation.get("state") in {
            "completed",
            "failed",
            "uncertain",
            "cancelled",
        }:
            return _operation_result(receipt)
        target_state = target_operation.operation.get("state")
        result = {
            "target": operation_view(target_operation),
            "settled": target_state in {"completed", "failed", "cancelled"},
        }
        state = "completed" if result["settled"] else "uncertain"
        receipt = self.ledger.update_operation(
            receipt,
            state=state,
            step="target_inspected",
            result=result,
            next_action=(
                "No further action is required."
                if result["settled"]
                else "Use the target receipt's next command to inspect its external postcondition."
            ),
        )
        return _operation_result(receipt)


def operation_reply(operation: OperationRecord) -> dict[str, Any]:
    """Agent-facing result; the full durable recovery receipt remains inspectable."""
    fc = operation.operation
    result = fc.get("result")
    return {
        "bead_id": fc.get("bead_id"),
        "ownership_operation": fc.get("ownership_operation"),
        **(dict(result) if isinstance(result, Mapping) else {}),
        "step": fc.get("step"),
        "next_action": fc.get("next_action"),
        "error": fc.get("error"),
        "next_commands": [["fulcrum", "operation", "show", operation.id, "--json"]],
    }


def operation_view(operation: OperationRecord) -> dict[str, Any]:
    fc = operation.operation
    return {
        "id": operation.id,
        "status": operation.status,
        "owner": fc.get("owner"),
        "request_id": fc.get("request_id"),
        "command": fc.get("command"),
        "input": fc.get("input"),
        "bead_id": fc.get("bead_id"),
        "ownership_operation": fc.get("ownership_operation"),
        "state": fc.get("state"),
        "step": fc.get("step"),
        "attempts": fc.get("attempts"),
        "external": fc.get("external"),
        "planned": fc.get("planned"),
        "result": fc.get("result"),
        "error": fc.get("error"),
        "next_action": fc.get("next_action"),
        "created_at": fc.get("created_at"),
        "completed_at": fc.get("completed_at"),
        "next_commands": [["fulcrum", "operation", "show", operation.id, "--json"]],
    }


def _operation_result(operation: OperationRecord) -> CommandResult:
    fc = operation.operation
    value = str(fc.get("state", "running"))
    state = (
        CommandState(value)
        if value in CommandState._value2member_map_
        else CommandState.RUNNING
    )
    ok = state not in {CommandState.FAILED, CommandState.UNCERTAIN}
    return CommandResult(
        ok=ok,
        state=state,
        operation_id=operation.id,
        request_id=str(fc.get("request_id")) if fc.get("request_id") else None,
        result=operation_view(operation),
    )


def _read_capture(handle: Any, *, limit: int = CAPTURE_BYTES) -> tuple[str, bool]:
    handle.flush()
    size = handle.tell()
    handle.seek(0)
    data = handle.read(limit)
    return data.decode("utf-8", errors="replace"), size > limit


def _classify_failure(stdout: str, stderr: str) -> tuple[str, bool]:
    detail = (stderr + "\n" + stdout).lower()
    if any(
        marker in detail
        for marker in (
            "connection refused",
            "server unavailable",
            "timed out",
            "no beads project found",
        )
    ):
        return "unavailable", True
    if any(
        marker in detail
        for marker in ("unsupported", "unknown command", "unknown flag")
    ):
        return "unsupported", False
    return "rejected", False
