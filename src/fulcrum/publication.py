"""Native Beads/Dolt publication to the brain repository's dedicated Git ref."""

from __future__ import annotations

from fulcrum.coordination import unlocked

from fulcrum.coordination import coordinated

import json
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fulcrum.configuration import ConfigurationManager
from fulcrum.contracts import CommandResult, CommandState, FulcrumError, ParsedRequest
from fulcrum.ledger import Ledger, LedgerFailure, OperationRecord, operation_view

MAX_SENDS = 3
RETRY_DELAYS = (1, 5)


class DoltPublicationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        category: str,
        retryable: bool,
        uncertain: bool = False,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.retryable = retryable
        self.uncertain = uncertain


@dataclass(frozen=True)
class NativeStatus:
    branch: str
    commit: str


class DoltPublicationAdapter:
    """Use only stock ``bd`` and Git transport commands."""

    def __init__(
        self,
        ledger: Ledger,
        *,
        remote: str,
        timeout: float,
        after_push: Callable[[str], None] | None = None,
    ) -> None:
        self.ledger = ledger
        self.root: Path = ledger.workspace
        self.remote = remote
        self.timeout = timeout
        self.after_push = after_push

    def status(self) -> NativeStatus:
        value = self.ledger.run(("vc", "status")).value
        if not isinstance(value, Mapping):
            raise self._unsupported("bd vc status returned no JSON object")
        branch = value.get("branch")
        commit = value.get("commit")
        if not isinstance(branch, str) or not isinstance(commit, str):
            raise self._unsupported(
                "bd vc status did not expose distinct branch and native commit fields"
            )
        return NativeStatus(branch=branch, commit=commit)

    def ensure_remote(self) -> dict[str, Any]:
        return self._remote_configuration(add_missing=True)

    def inspect_remote(self) -> dict[str, Any]:
        return self._remote_configuration(add_missing=False)

    def _remote_configuration(self, *, add_missing: bool) -> dict[str, Any]:
        ordinary_url = self._ordinary_remote_url()
        expected = _git_transport(ordinary_url)
        value = self.ledger.run(("dolt", "remote", "list")).value
        if not isinstance(value, list):
            raise self._unsupported("bd dolt remote list returned no JSON array")
        matches = [
            item
            for item in value
            if isinstance(item, Mapping) and item.get("name") == self.remote
        ]
        if len(matches) > 1:
            raise self._unsupported(f"multiple Dolt remotes are named {self.remote}")
        if not matches and add_missing:
            self.ledger.run(
                ("dolt", "remote", "add", self.remote, expected), mutating=True
            )
            value = self.ledger.run(("dolt", "remote", "list")).value
            matches = (
                [
                    item
                    for item in value
                    if isinstance(item, Mapping) and item.get("name") == self.remote
                ]
                if isinstance(value, list)
                else []
            )
        if len(matches) != 1:
            raise DoltPublicationError(
                f"Dolt remote {self.remote} is not configured",
                category="configuration",
                retryable=False,
            )
        observed_url = matches[0].get("url") or matches[0].get("sql_url")
        if observed_url != expected:
            raise DoltPublicationError(
                f"Dolt remote {self.remote} points to {observed_url}, expected {expected}",
                category="configuration",
                retryable=False,
            )
        return {
            "name": self.remote,
            "ordinary_url": ordinary_url,
            "transport_url": expected,
            "status": matches[0].get("status"),
        }

    def diff(self, older: str, newer: str) -> list[dict[str, Any]]:
        try:
            value = self.ledger.run(("diff", older, newer)).value
        except LedgerFailure as error:
            raise DoltPublicationError(
                f"stock Beads cannot compare publication boundary {older}: {error}",
                category="capability" if not error.retryable else error.category,
                retryable=error.retryable,
            ) from error
        if not isinstance(value, list):
            raise self._unsupported("bd diff returned no JSON array")
        rows: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, Mapping) or not isinstance(
                item.get("IssueID"), str
            ):
                raise self._unsupported("bd diff omitted an issue identity")
            rows.append(dict(item))
        return rows

    def commit(self, message: str) -> NativeStatus:
        self._bd_text(("dolt", "commit", "-m", message), mutating=True)
        return self.status()

    @unlocked
    def push(self, target_commit: str) -> None:
        self._bd_text(("dolt", "push", "--remote", self.remote), mutating=True)
        if self.after_push is not None:
            try:
                self.after_push(target_commit)
            except Exception as error:
                raise DoltPublicationError(
                    f"push response was lost after the native send: {error}",
                    category="response_lost",
                    retryable=True,
                    uncertain=True,
                ) from error

    def remote_data_ref(self) -> str | None:
        url = self._ordinary_remote_url()
        completed = self._run(("git", "ls-remote", url, "refs/dolt/data"))
        line = completed.stdout.strip()
        if not line:
            return None
        oid, _, reference = line.partition("\t")
        if reference != "refs/dolt/data" or len(oid) < 7:
            raise self._unsupported(
                "Git did not return a valid refs/dolt/data identity"
            )
        return oid

    def remote_contains(self, target_commit: str, witness_issue: str) -> bool:
        """Prove a Dolt commit through a fresh stock-Beads remote bootstrap."""

        ordinary_url = self._ordinary_remote_url()
        transport = _git_transport(ordinary_url)
        with tempfile.TemporaryDirectory(prefix="fulcrum-dolt-inspect-") as directory:
            scratch = Path(directory)
            self._run(("git", "init", str(scratch)))
            self._run(
                ("git", "-C", str(scratch), "remote", "add", "origin", ordinary_url)
            )
            command = (
                self.ledger.executable,
                "--json",
                "--actor",
                "fulcrum-publication-inspector",
                "init",
                "--remote",
                transport,
                "--prefix",
                "fc",
                "--non-interactive",
                "--skip-hooks",
                "--skip-agents",
            )
            self._run(command, cwd=scratch)
            history = self._run(
                (
                    self.ledger.executable,
                    "--json",
                    "--actor",
                    "fulcrum-publication-inspector",
                    "-C",
                    str(scratch),
                    "history",
                    witness_issue,
                    "--limit",
                    "0",
                )
            )
            try:
                value = json.loads(history.stdout)
            except json.JSONDecodeError as error:
                raise self._unsupported(
                    "bd history returned malformed JSON during remote inspection"
                ) from error
        return bool(
            isinstance(value, list)
            and any(
                isinstance(item, Mapping) and item.get("CommitHash") == target_commit
                for item in value
            )
        )

    def _ordinary_remote_url(self) -> str:
        completed = self._run(
            ("git", "-C", str(self.root), "remote", "get-url", self.remote),
            allow_failure=True,
        )
        if completed.returncode == 0 and completed.stdout.strip():
            return completed.stdout.strip()
        if _looks_like_remote(self.remote):
            return self.remote
        raise DoltPublicationError(
            f"brain Git remote {self.remote} has no configured URL",
            category="configuration",
            retryable=False,
        )

    def _bd_text(self, arguments: Sequence[str], *, mutating: bool) -> str:
        command = [
            self.ledger.executable,
            "--actor",
            self.ledger.actor,
            "--dolt-auto-commit",
            "batch",
            "-C",
            str(self.root),
            *arguments,
        ]
        completed = self._run(tuple(command), allow_failure=True, uncertain=mutating)
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            lower = detail.lower()
            divergence = any(
                word in lower
                for word in ("conflict", "non-fast-forward", "diverge", "overwrite")
            )
            transient = any(
                word in lower
                for word in (
                    "connection refused",
                    "could not resolve",
                    "timed out",
                    "timeout",
                    "unavailable",
                    "authentication",
                    "permission denied",
                    "could not be accessed",
                )
            )
            raise DoltPublicationError(
                detail or "stock Beads Dolt command failed",
                category=(
                    "divergence"
                    if divergence
                    else "transient" if transient else "rejected"
                ),
                retryable=transient,
            )
        return completed.stdout.strip()

    @unlocked
    def _run(
        self,
        command: Sequence[str],
        *,
        allow_failure: bool = False,
        uncertain: bool = False,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            completed = subprocess.run(
                list(command),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
                cwd=cwd,
            )
        except subprocess.TimeoutExpired as error:
            raise DoltPublicationError(
                f"command timed out: {command[0]}",
                category="timeout",
                retryable=True,
                uncertain=uncertain,
            ) from error
        except OSError as error:
            raise DoltPublicationError(
                f"could not execute {command[0]}: {error}",
                category="unavailable",
                retryable=True,
                uncertain=False,
            ) from error
        if completed.returncode != 0 and not allow_failure:
            raise DoltPublicationError(
                completed.stderr.strip()
                or completed.stdout.strip()
                or f"{command[0]} failed",
                category="unavailable",
                retryable=True,
                uncertain=False,
            )
        return completed

    @staticmethod
    def _unsupported(message: str) -> DoltPublicationError:
        return DoltPublicationError(
            message, category="unsupported", retryable=False, uncertain=False
        )


class LedgerPublicationService:
    def __init__(
        self,
        *,
        now: Callable[[], datetime] | None = None,
        adapter_factory: Callable[..., DoltPublicationAdapter] = DoltPublicationAdapter,
    ) -> None:
        self.now: Callable[[], datetime] = now or (lambda: datetime.now(timezone.utc))
        self.adapter_factory = adapter_factory

    @coordinated
    def status(self, request: ParsedRequest) -> CommandResult:
        try:
            ledger, config, adapter = self._components(request)
            return CommandResult.query(self.inspect(ledger, config, adapter))
        except DoltPublicationError as error:
            raise _public_error(error, request) from error

    @coordinated
    def sync(self, request: ParsedRequest) -> CommandResult:
        try:
            ledger, config, adapter = self._components(request)
            return self.synchronize(request, ledger, config, adapter, explicit=True)
        except DoltPublicationError as error:
            raise _public_error(error, request) from error

    @coordinated
    def reconcile(
        self, request: ParsedRequest, target: OperationRecord
    ) -> CommandResult:
        if target.operation.get("command") != "ledger.sync":
            raise FulcrumError.invalid(
                "WRONG_OPERATION_KIND", f"{target.id} is not a ledger.sync receipt"
            )
        ledger, config, adapter = self._components(request)
        publication = _publication(ledger)
        if publication.get("operation_id") != target.id:
            raise FulcrumError(
                "REQUEST_CONFLICT",
                "the requested ledger publication is no longer the unsettled batch",
                exit_code=5,
                operation_id=target.id,
            )
        self._grant_retry(ledger, target, request.request_id, "operation.reconcile")
        sync_request = replace(
            request, command=("ledger", "sync"), input={}, arguments={}
        )
        return self.synchronize(sync_request, ledger, config, adapter, explicit=False)

    @coordinated
    def synchronize(
        self,
        request: ParsedRequest,
        ledger: Ledger,
        config: Mapping[str, Any],
        adapter: DoltPublicationAdapter,
        *,
        explicit: bool,
    ) -> CommandResult:
        now = self.now()
        adapter.ensure_remote()
        _ensure_publication_control(ledger, request)
        publication = _publication(ledger)
        operation = _publication_operation(ledger, publication)
        if operation is not None and operation.operation.get("state") in {
            "failed",
            "uncertain",
        }:
            if explicit:
                operation = self._grant_retry(
                    ledger, operation, request.request_id, "ledger.sync"
                )
            elif operation.operation.get("state") == "uncertain":
                return _operation_result(operation)
            else:
                return _operation_result(operation)

        if operation is None or operation.operation.get("state") == "completed":
            status = adapter.status()
            pending, changed = self._pending(
                ledger, adapter, publication, status.commit
            )
            if not pending:
                operation, reused = ledger.create_operation(
                    request,
                    planned={"native_commit": status.commit, "changes": []},
                    next_action="No native ledger publication is pending.",
                )
                if not reused or operation.operation.get("state") not in {
                    "completed",
                    "failed",
                    "cancelled",
                }:
                    operation = ledger.update_operation(
                        operation,
                        state="completed",
                        step="no_native_changes",
                        result=self.inspect(ledger, config, adapter),
                        next_action="No further action is required.",
                    )
                return _operation_result(operation)
            operation, reused = ledger.create_operation(
                request,
                planned={
                    "changes": changed,
                    "grant": 1,
                    "grant_attempts": 0,
                    "total_sends": 0,
                    "retry_grants": [],
                },
                next_action="Commit and publish the retained native Beads batch.",
            )
            if reused and operation.operation.get("state") in {
                "completed",
                "cancelled",
            }:
                return _operation_result(operation)
            publication.update(
                {
                    "pending_since": publication.get("pending_since")
                    or _format_time(now),
                    "operation_id": operation.id,
                    "failure": None,
                }
            )
            _write_publication(ledger, publication)
            target = adapter.commit(f"fulcrum: publish ledger batch {operation.id}")
            witness = operation.id
            target_control = _normalized_control(ledger.show("fc-system"))
            planned = dict(operation.operation.get("planned") or {})
            planned.update(
                {
                    "target_dolt_commit": target.commit,
                    "target_branch": target.branch,
                    "witness_issue": witness,
                    "boundary_control": target_control,
                }
            )
            try:
                before_ref = adapter.remote_data_ref()
            except DoltPublicationError:
                before_ref = None
            operation = ledger.update_operation(
                operation,
                state="running",
                step="native_commit_retained",
                planned=planned,
                external={"remote": adapter.remote, "remote_data_ref": before_ref},
            )

        planned = dict(operation.operation.get("planned") or {})
        target_commit = planned.get("target_dolt_commit")
        witness = planned.get("witness_issue")
        if not isinstance(target_commit, str) or not isinstance(witness, str):
            raise FulcrumError(
                "PUBLICATION_RECEIPT_INVALID",
                "ledger publication receipt has no native commit boundary",
                exit_code=5,
                operation_id=operation.id,
            )
        grant_attempts = int(planned.get("grant_attempts") or 0)
        if grant_attempts >= MAX_SENDS:
            return _operation_result(operation)
        grant_attempts += 1
        planned["grant_attempts"] = grant_attempts
        planned["total_sends"] = int(planned.get("total_sends") or 0) + 1
        operation = ledger.update_operation(
            operation,
            state="running",
            step="push_started",
            attempts=grant_attempts,
            planned=planned,
        )
        error: DoltPublicationError | None = None
        try:
            adapter.push(target_commit)
        except DoltPublicationError as caught:
            error = caught
        if error is None or error.uncertain:
            try:
                contained = adapter.remote_contains(target_commit, witness)
            except DoltPublicationError as inspect_error:
                if error is None:
                    error = inspect_error
                contained = False
            if contained:
                completed = self._complete(
                    ledger, config, adapter, operation, target_commit, planned, now
                )

                return completed
        assert error is not None
        return self._fail(ledger, operation, publication, planned, error, now)

    def inspect(
        self,
        ledger: Ledger,
        config: Mapping[str, Any],
        adapter: DoltPublicationAdapter,
        *,
        mark_pending: bool = False,
    ) -> dict[str, Any]:
        status = adapter.status()
        publication = _publication(ledger)
        pending, changes = self._pending(ledger, adapter, publication, status.commit)
        configuration = self._configuration_status(ledger, config)
        overall_pending = pending or bool(configuration["configuration_pending"])
        if (
            overall_pending
            and publication.get("pending_since") is None
            and mark_pending
        ):
            publication["pending_since"] = _format_time(self.now())
            _write_publication(ledger, publication)
        pending_since = _parse_optional_time(publication.get("pending_since"))
        operation = _publication_operation(ledger, publication)
        remote: dict[str, Any]
        try:
            remote_config = adapter.inspect_remote()
            remote = {**remote_config, "data_ref": adapter.remote_data_ref()}
        except DoltPublicationError as error:
            remote = {
                "name": adapter.remote,
                "data_ref": publication.get("remote_data_ref"),
                "error": str(error),
                "category": error.category,
            }
        operation_view_value = None
        if operation is not None:
            planned = operation.operation.get("planned")
            operation_view_value = {
                "id": operation.id,
                "state": operation.operation.get("state"),
                "attempts": operation.operation.get("attempts"),
                "grant": planned.get("grant") if isinstance(planned, Mapping) else None,
                "total_sends": (
                    planned.get("total_sends") if isinstance(planned, Mapping) else None
                ),
                "next_retry_at": (
                    planned.get("next_retry_at")
                    if isinstance(planned, Mapping)
                    else None
                ),
                "exhausted": operation.operation.get("state") == "failed",
                "error": operation.operation.get("error"),
            }
        return {
            "local": {"branch": status.branch, "commit": status.commit},
            "remote": remote,
            "publication": publication,
            "pending": pending,
            "overall_pending": overall_pending,
            "pending_changes": changes,
            "pending_age_seconds": (
                max(0.0, (self.now() - pending_since).total_seconds())
                if overall_pending and pending_since is not None
                else None
            ),
            "mode": "explicit",
            "operation": operation_view_value,
            "ordinary_git": configuration,
        }

    def _configuration_status(
        self, ledger: Ledger, config: Mapping[str, Any]
    ) -> dict[str, Any]:
        path = ledger.workspace / "fulcrum.yaml"
        control = ledger.show("fc-system")
        publication = (
            (control.fc or {}).get("config_publication")
            if control is not None
            else None
        )
        local = publication.get("local") if isinstance(publication, Mapping) else None
        published_commit = local.get("commit") if isinstance(local, Mapping) else None
        published_content: str | None = None
        if isinstance(published_commit, str):
            completed = subprocess.run(
                [
                    "git",
                    "-C",
                    str(ledger.workspace),
                    "show",
                    f"{published_commit}:fulcrum.yaml",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=ledger.timeout,
            )
            if completed.returncode == 0:
                published_content = completed.stdout
        try:
            current = path.read_text(encoding="utf-8")
        except OSError:
            current = None
        remote = publication.get("remote") if isinstance(publication, Mapping) else None
        remote_ready = not bool(config["knowledge"]["require_remote_sync"]) or (
            isinstance(remote, Mapping)
            and remote.get("state") == "observed"
            and remote.get("contains_local") is True
        )
        return {
            "configuration_pending": current is not None
            and (published_content != current or not remote_ready),
            "path": str(path),
            "published_commit": published_commit,
            "remote_ready": remote_ready,
        }

    def _pending(
        self,
        ledger: Ledger,
        adapter: DoltPublicationAdapter,
        publication: Mapping[str, Any],
        current: str,
    ) -> tuple[bool, list[dict[str, Any]]]:
        boundary = publication.get("last_dolt_commit")
        if not isinstance(boundary, str):
            return True, [{"issue_id": None, "reason": "no_published_boundary"}]
        if boundary == current:
            return False, []
        rows = adapter.diff(boundary, current)
        retained = _publication_operation(ledger, publication)
        boundary_control = None
        if retained is not None:
            planned = retained.operation.get("planned")
            if isinstance(planned, Mapping):
                boundary_control = planned.get("boundary_control")
        changes: list[dict[str, Any]] = []
        for row in rows:
            issue_id = str(row["IssueID"])
            record = ledger.show(issue_id)
            if (
                record is not None
                and record.kind == "operation"
                and (record.fc or {}).get("command") == "ledger.sync"
            ):
                continue
            if issue_id == "fc-system":
                if boundary_control is None:
                    raise DoltPublicationError(
                        "installed stock Beads cannot expose historical metadata needed to distinguish publication bookkeeping",
                        category="unsupported",
                        retryable=False,
                    )
                if _normalized_control(record) == boundary_control:
                    continue
            changes.append({"issue_id": issue_id, "diff_type": row.get("DiffType")})
        return bool(changes), changes

    def _complete(
        self,
        ledger: Ledger,
        config: Mapping[str, Any],
        adapter: DoltPublicationAdapter,
        operation: OperationRecord,
        target_commit: str,
        planned: Mapping[str, Any],
        now: datetime,
    ) -> CommandResult:
        remote_ref = adapter.remote_data_ref()
        current = adapter.status()
        publication = _publication(ledger)
        later_pending, _ = (
            (False, [])
            if current.commit == target_commit
            else self._pending(
                ledger,
                adapter,
                {**publication, "last_dolt_commit": target_commit},
                current.commit,
            )
        )
        publication.update(
            {
                "pending_since": _format_time(now) if later_pending else None,
                "last_success_at": _format_time(now),
                "operation_id": operation.id,
                "last_dolt_commit": target_commit,
                "remote_data_ref": remote_ref,
                "failure": None,
            }
        )
        _write_publication(ledger, publication)
        result = {
            "local": {"branch": current.branch, "commit": current.commit},
            "target_dolt_commit": target_commit,
            "remote_data_ref": remote_ref,
            "remote_contains_target": True,
            "pending_after_boundary": later_pending,
            "mode": "explicit",
        }
        completed_planned = dict(planned)
        completed_planned["capability_state"] = "healthy"
        operation = ledger.update_operation(
            operation,
            state="completed",
            step="remote_native_commit_observed",
            planned=completed_planned,
            result=result,
            error={},
            next_action="No further action is required.",
        )
        return _operation_result(operation)

    def _fail(
        self,
        ledger: Ledger,
        operation: OperationRecord,
        publication: dict[str, Any],
        planned: dict[str, Any],
        error: DoltPublicationError,
        now: datetime,
    ) -> CommandResult:
        attempts = int(planned.get("grant_attempts") or 0)
        if error.retryable:
            planned["capability_state"] = "failed"
        if error.uncertain:
            state = "uncertain"
            step = "push_outcome_unproved"
            next_action = f"Run fulcrum operation reconcile {operation.id} --json."
        elif error.retryable and attempts < MAX_SENDS:
            state = "accepted"
            step = "push_retry_wait"
            delay = RETRY_DELAYS[min(attempts - 1, len(RETRY_DELAYS) - 1)]
            planned["next_retry_at"] = _format_time(now + timedelta(seconds=delay))
            next_action = (
                "Retry the same retained native commit after the recorded delay."
            )
        else:
            state = "failed"
            step = "automatic_push_retries_exhausted"
            planned.pop("next_retry_at", None)
            next_action = f"Run fulcrum ledger sync --json or reconcile {operation.id}."
        failure = {
            "category": error.category,
            "message": str(error),
            "retryable": error.retryable,
            "uncertain": error.uncertain,
            "attempt": attempts,
            "at": _format_time(now),
        }
        publication.update(
            {
                "pending_since": publication.get("pending_since") or _format_time(now),
                "operation_id": operation.id,
                "failure": failure,
            }
        )
        _write_publication(ledger, publication)
        operation = ledger.update_operation(
            operation,
            state=state,
            step=step,
            attempts=attempts,
            planned=planned,
            error={
                "code": error.category,
                "message": str(error),
                "retryable": error.retryable,
            },
            next_action=next_action,
        )
        return _operation_result(operation)

    def _grant_retry(
        self,
        ledger: Ledger,
        operation: OperationRecord,
        request_id: str | None,
        source: str,
    ) -> OperationRecord:
        planned = dict(operation.operation.get("planned") or {})
        grants = list(planned.get("retry_grants") or [])
        grants.append(
            {
                "grant": int(planned.get("grant") or 1) + 1,
                "request_id": request_id,
                "source": source,
                "at": _format_time(self.now()),
            }
        )
        planned.update(
            {
                "grant": grants[-1]["grant"],
                "grant_attempts": 0,
                "retry_grants": grants,
            }
        )
        planned.pop("next_retry_at", None)
        return ledger.update_operation(
            operation,
            state="accepted",
            step="explicit_retry_granted",
            attempts=0,
            planned=planned,
            next_action="Retry the same retained native commit.",
        )

    def _components(
        self, request: ParsedRequest
    ) -> tuple[Ledger, dict[str, Any], DoltPublicationAdapter]:
        if request.instance.brain_root is None:
            raise FulcrumError(
                "LEDGER_UNAVAILABLE", "a valid brain root is required", exit_code=4
            )
        manager = ConfigurationManager(request.instance.config_path)
        document, _ = manager.load()
        config = manager.effective(document)
        beads = config["beads"]
        ledger = Ledger(
            request.instance.brain_root,
            executable=(
                str(beads.get("executable")) if beads.get("executable") else None
            ),
            timeout=request.timeout,
            dolt_auto_commit="batch",
        )
        remote = str(config["brain"]["remote"])
        adapter = self.adapter_factory(ledger, remote=remote, timeout=request.timeout)
        return ledger, config, adapter


def _publication(ledger: Ledger) -> dict[str, Any]:
    control = ledger.show("fc-system")
    if control is None or not control.fc or control.kind != "control":
        raise FulcrumError(
            "CONTROL_NOT_FOUND",
            "fc-system must exist before native ledger publication",
            exit_code=4,
        )
    value = control.fc.get("publication")
    publication = dict(value) if isinstance(value, Mapping) else {}
    for key in (
        "pending_since",
        "last_success_at",
        "operation_id",
        "last_dolt_commit",
        "remote_data_ref",
        "failure",
    ):
        publication.setdefault(key, None)
    return publication


def _ensure_publication_control(ledger: Ledger, request: ParsedRequest) -> None:
    if ledger.show("fc-system") is not None:
        return
    ledger.create_record(
        record_id="fc-system",
        kind="control",
        title="Fulcrum control",
        description="Standing leadership identities and installation binding.",
        owner="HUMAN",
        fc={
            "kind": "control",
            "owner": "HUMAN",
            "instance_root": str(request.instance.instance_root),
            "brain_root": (
                str(request.instance.brain_root)
                if request.instance.brain_root is not None
                else None
            ),
            "desktop": {
                "run_control": "paused",
                "standing": {},
                "requests": {},
                "instruction_waits": {},
            },
            "last_transition": None,
        },
    )


def _write_publication(ledger: Ledger, publication: Mapping[str, Any]) -> None:
    control = ledger.show("fc-system")
    if control is None or not control.fc:
        raise FulcrumError("CONTROL_NOT_FOUND", "fc-system is unavailable", exit_code=4)
    fc = dict(control.fc)
    fc["publication"] = dict(publication)
    ledger.update_fc(control.id, fc)


def _publication_operation(
    ledger: Ledger, publication: Mapping[str, Any]
) -> OperationRecord | None:
    identifier = publication.get("operation_id")
    if not isinstance(identifier, str):
        return None
    record = ledger.show(identifier)
    if record is None or record.kind != "operation":
        return None
    operation = OperationRecord.from_record(record)
    if operation.operation.get("command") != "ledger.sync":
        return None
    return operation


def _normalized_control(record: Any) -> dict[str, Any] | None:
    if record is None or not record.fc:
        return None
    fc = dict(record.fc)
    fc.pop("publication", None)
    native = {
        key: value
        for key, value in record.native.items()
        if key
        not in {
            "metadata",
            "updated_at",
            "dependent_count",
            "dependency_count",
            "comment_count",
        }
    }
    return {"native": native, "fc": fc}


def _operation_result(operation: OperationRecord) -> CommandResult:
    value = str(operation.operation.get("state") or "running")
    state = (
        CommandState(value)
        if value in CommandState._value2member_map_
        else CommandState.RUNNING
    )
    return CommandResult(
        ok=state not in {CommandState.FAILED, CommandState.UNCERTAIN},
        state=state,
        operation_id=operation.id,
        request_id=operation.operation.get("request_id"),
        result=operation_view(operation),
    )


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_optional_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _looks_like_remote(value: str) -> bool:
    return "://" in value or value.startswith("git@") or Path(value).is_absolute()


def _git_transport(value: str) -> str:
    if value.startswith("git+"):
        return value
    if value.startswith("git@") and ":" in value:
        host, path = value.split(":", 1)
        return f"git+ssh://{host}/{path}"
    if value.startswith("ssh://"):
        return "git+" + value
    if (
        value.startswith("https://")
        or value.startswith("http://")
        or value.startswith("file://")
    ):
        return "git+" + value
    path = Path(value)
    if path.is_absolute():
        return "git+file://" + str(path)
    raise DoltPublicationError(
        f"unsupported brain Git remote URL: {value}",
        category="configuration",
        retryable=False,
    )


def _public_error(error: DoltPublicationError, request: ParsedRequest) -> FulcrumError:
    return FulcrumError(
        (
            "PUBLICATION_CAPABILITY_UNSUPPORTED"
            if error.category == "unsupported"
            else "PUBLICATION_UNAVAILABLE"
        ),
        str(error),
        exit_code=4,
        retryable=error.retryable,
        state=CommandState.UNCERTAIN if error.uncertain else CommandState.FAILED,
        request_id=request.request_id,
        details={"category": error.category},
    )
