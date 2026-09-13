"""Structured subprocess adapter for the native Tollgate `tg` CLI."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any


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
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            error_type = TollgateUncertainError if mutating else TollgateError
            raise error_type(
                f"Tollgate returned invalid JSON for {arguments[0]}; result is "
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
        return self.run(
            ["approve", candidate_id, "--wait"],
            repository_id=repository_id,
            mutating=True,
        )

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
