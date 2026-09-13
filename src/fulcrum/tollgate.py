"""Structured subprocess adapter for the native Tollgate `tg` CLI."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable


class TollgateError(RuntimeError):
    """A native Tollgate operation failed or returned unusable evidence."""

    def __init__(
        self,
        message: str,
        *,
        stdout: str | None = None,
        stderr: str | None = None,
        returncode: int | None = None,
        duration_ms: int | None = None,
    ) -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.duration_ms = duration_ms


class TollgateUncertainError(TollgateError):
    """A mutating CLI operation timed out, so its effect is unknown."""


class Tollgate:
    def __init__(
        self, executable: str | Path | None = None, *, timeout: int = 60
    ) -> None:
        selected = str(executable) if executable is not None else shutil.which("tg")
        if not selected:
            bundled = Path("/Applications/Tollgate.app/Contents/MacOS/tg")
            selected = str(bundled) if bundled.is_file() else None
        if selected is None:
            raise TollgateError("Tollgate CLI `tg` was not found")
        self.executable: str = selected
        self.timeout = timeout

    def _run_json(
        self,
        arguments: list[str],
        *,
        repository_id: str | None = None,
        cwd: Path | None = None,
        mutating: bool = False,
        decoder: Callable[[str], Any] | None = None,
    ) -> Any:
        command = [self.executable, "--json", "--no-launch"]
        if repository_id is not None:
            command.extend(["--repository", repository_id])
        command.extend(arguments)
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise TollgateUncertainError(
                f"Tollgate operation {arguments[0]} timed out; result is uncertain",
                stdout=_text(error.stdout),
                stderr=_text(error.stderr),
                duration_ms=int((time.monotonic() - started) * 1000),
            ) from error
        except OSError as error:
            error_type = TollgateUncertainError if mutating else TollgateError
            raise error_type(
                f"could not run Tollgate operation {arguments[0]}: {error}",
                duration_ms=int((time.monotonic() - started) * 1000),
            ) from error
        duration_ms = int((time.monotonic() - started) * 1000)
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "no output"
            error_type = TollgateUncertainError if mutating else TollgateError
            raise error_type(
                f"Tollgate operation {arguments[0]} failed: {detail}",
                stdout=completed.stdout,
                stderr=completed.stderr,
                returncode=completed.returncode,
                duration_ms=duration_ms,
            )
        try:
            result = (
                json.loads(completed.stdout)
                if decoder is None
                else decoder(completed.stdout)
            )
        except ValueError as error:
            error_type = TollgateUncertainError if mutating else TollgateError
            output_kind = (
                "invalid JSON"
                if decoder is None
                else "invalid or ambiguous JSON stream"
            )
            raise error_type(
                f"Tollgate returned {output_kind} for {arguments[0]}; result is "
                + ("uncertain" if mutating else "unusable"),
                stdout=completed.stdout,
                stderr=completed.stderr,
                returncode=completed.returncode,
                duration_ms=duration_ms,
            ) from error
        return result

    def run(
        self,
        arguments: list[str],
        *,
        repository_id: str | None = None,
        cwd: Path | None = None,
        mutating: bool = False,
    ) -> dict[str, Any]:
        result = self._run_json(
            arguments,
            repository_id=repository_id,
            cwd=cwd,
            mutating=mutating,
        )
        if not isinstance(result, dict):
            error_type = TollgateUncertainError if mutating else TollgateError
            raise error_type(f"Tollgate returned a non-object for {arguments[0]}")
        return result

    def repositories(self) -> list[dict[str, Any]]:
        result = self._run_json(["repo", "list"])
        if not isinstance(result, list) or not all(
            isinstance(item, dict) for item in result
        ):
            raise TollgateError("Tollgate returned a non-list for repo")
        return result

    def status(
        self, repository_id: str, candidate_id: str | None = None
    ) -> dict[str, Any]:
        arguments = ["status"] + ([candidate_id] if candidate_id else [])
        return self.run(arguments, repository_id=repository_id)

    def create_worktree(self, repository_id: str, identity: str) -> dict[str, Any]:
        return self.run(
            ["worktree", "create", identity],
            repository_id=repository_id,
            mutating=True,
        )

    def remove_worktree(self, repository_id: str, path: str) -> dict[str, Any]:
        return self.run(
            ["worktree", "remove", path],
            repository_id=repository_id,
            mutating=True,
        )

    def submit_candidate(
        self,
        repository_id: str,
        revision: str = "HEAD",
        *,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        return self.run(
            ["candidate", revision],
            repository_id=repository_id,
            cwd=cwd,
            mutating=True,
        )

    def approve(self, repository_id: str, candidate_id: str) -> dict[str, Any]:
        result = self._run_json(
            ["approve", candidate_id, "--wait"],
            repository_id=repository_id,
            mutating=True,
            decoder=lambda output: _parse_approval_stream(
                output, repository_id, candidate_id
            ),
        )
        assert isinstance(result, dict)
        return result

    def diagnose(self, repository_id: str, candidate_id: str) -> dict[str, Any]:
        """Read retained failure evidence without requesting a replay."""

        return self.run(["diagnose", candidate_id], repository_id=repository_id)

    def cancel(self, repository_id: str, candidate_id: str) -> dict[str, Any]:
        return self.run(
            ["cancel", candidate_id], repository_id=repository_id, mutating=True
        )


def _text(value: bytes | str | None) -> str | None:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


_APPROVAL_SUCCESS_STATES: set[str] = {"promoted", "externally-integrated"}
_TERMINAL_STATES: set[str] = _APPROVAL_SUCCESS_STATES | {
    "failed",
    "merge-conflict",
    "dependency-failed",
    "canceled",
    "superseded",
    "infrastructure-exhausted",
    "check-passed",
    "check-failed",
}


def _parse_approval_stream(
    output: str, repository_id: str, candidate_id: str
) -> dict[str, Any]:
    """Decode the JSON Lines contract emitted by ``tg approve --wait``."""

    lines = output.splitlines()
    if len(lines) < 2:
        raise ValueError("approval stream omitted its authorization or wait status")

    documents: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise ValueError(f"approval stream line {line_number} is empty")
        try:
            document = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(
                f"approval stream line {line_number} is not JSON"
            ) from error
        if not isinstance(document, dict):
            raise ValueError(f"approval stream line {line_number} is not an object")
        documents.append(document)

    authorization = documents[0]
    already_authorized = authorization.get("already_authorized")
    authorized_item_ids = authorization.get("authorized_item_ids")
    if authorization.get("item_id") != candidate_id:
        raise ValueError("approval authorization identifies a different candidate")
    if not isinstance(already_authorized, bool):
        raise ValueError("approval authorization omitted its authorization state")
    if not isinstance(authorized_item_ids, list) or not all(
        isinstance(item_id, str) for item_id in authorized_item_ids
    ):
        raise ValueError("approval authorization omitted its candidate set")
    if not already_authorized and candidate_id not in authorized_item_ids:
        raise ValueError("approval authorization excludes the requested candidate")

    wait_statuses = documents[1:]
    for index, status in enumerate(wait_statuses, start=2):
        item = status.get("item")
        if not isinstance(item, dict):
            raise ValueError(f"approval wait status on line {index} omitted its item")
        if item.get("id") != candidate_id:
            raise ValueError(
                f"approval wait status on line {index} identifies a different candidate"
            )
        if item.get("repository_id") != repository_id:
            raise ValueError(
                f"approval wait status on line {index} identifies a different repository"
            )
        if not isinstance(item.get("state"), str):
            raise ValueError(f"approval wait status on line {index} omitted its state")
        if not isinstance(status.get("repository_execution_state"), str):
            raise ValueError(
                f"approval wait status on line {index} omitted repository execution state"
            )
        if not isinstance(status.get("block_reasons"), list):
            raise ValueError(
                f"approval wait status on line {index} omitted repository block reasons"
            )

    terminal = wait_statuses[-1]["item"]["state"]
    if terminal not in _APPROVAL_SUCCESS_STATES:
        raise ValueError(
            "successful approval stream did not end in a promoted candidate"
        )
    if any(
        status["item"]["state"] in _TERMINAL_STATES for status in wait_statuses[:-1]
    ):
        raise ValueError("approval stream continued after a terminal candidate status")

    return {
        "authorization": authorization,
        "wait_statuses": wait_statuses,
    }
