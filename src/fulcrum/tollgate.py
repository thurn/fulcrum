"""Structured subprocess adapter for the native Tollgate `tg` CLI."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


class TollgateError(RuntimeError):
    """A native Tollgate operation failed or returned unusable evidence."""


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

    def run(
        self,
        arguments: list[str],
        *,
        repository_id: str | None = None,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        command = [self.executable, "--json", "--no-launch"]
        if repository_id is not None:
            command.extend(["--repository", repository_id])
        command.extend(arguments)
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
                f"Tollgate operation {arguments[0]} timed out; result is uncertain"
            ) from error
        except OSError as error:
            raise TollgateError(
                f"could not run Tollgate operation {arguments[0]}: {error}"
            ) from error
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "no output"
            raise TollgateError(f"Tollgate operation {arguments[0]} failed: {detail}")
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise TollgateError(
                f"Tollgate returned invalid JSON for {arguments[0]}"
            ) from error
        if not isinstance(result, dict):
            raise TollgateError(f"Tollgate returned a non-object for {arguments[0]}")
        return result

    def repositories(self) -> dict[str, Any]:
        return self.run(["repo", "list"])

    def status(
        self, repository_id: str, candidate_id: str | None = None
    ) -> dict[str, Any]:
        arguments = ["status"] + ([candidate_id] if candidate_id else [])
        return self.run(arguments, repository_id=repository_id)

    def create_worktree(self, repository_id: str, identity: str) -> dict[str, Any]:
        return self.run(["worktree", "create", identity], repository_id=repository_id)

    def remove_worktree(self, repository_id: str, path: str) -> dict[str, Any]:
        return self.run(["worktree", "remove", path], repository_id=repository_id)

    def submit_candidate(
        self, repository_id: str, revision: str = "HEAD"
    ) -> dict[str, Any]:
        return self.run(["candidate", revision], repository_id=repository_id)

    def approve(self, repository_id: str, candidate_id: str) -> dict[str, Any]:
        return self.run(
            ["approve", candidate_id, "--wait"], repository_id=repository_id
        )

    def diagnose(self, repository_id: str, candidate_id: str) -> dict[str, Any]:
        """Read retained failure evidence without requesting a replay."""

        return self.run(["diagnose", candidate_id], repository_id=repository_id)

    def cancel(self, repository_id: str, candidate_id: str) -> dict[str, Any]:
        return self.run(["cancel", candidate_id], repository_id=repository_id)
