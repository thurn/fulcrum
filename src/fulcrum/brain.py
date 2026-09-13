"""Non-destructive publication for the controller-owned brain repository."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class BrainPublicationError(RuntimeError):
    """Brain publication retained local work but could not synchronize it."""


@dataclass(frozen=True)
class BrainPublication:
    branch: str
    local_revision: str
    remote_revision: str | None
    merged_remote: bool


class BrainRepository:
    """Serialize Git publication without assuming a fast-forward remote."""

    def __init__(self, root: Path, *, timeout: int = 60) -> None:
        self.root: Path = root.resolve(strict=True)
        self.timeout: int = timeout
        top = Path(self._run(["rev-parse", "--show-toplevel"])).resolve(strict=True)
        if top != self.root:
            raise BrainPublicationError(
                f"brain root must be the exact Git repository root: {top}"
            )

    def publish(self, paths: list[Path], message: str) -> BrainPublication:
        if not message.strip():
            raise BrainPublicationError("brain publication requires a commit message")
        relative: list[str] = []
        for path in paths:
            resolved = path.resolve(strict=False)
            if not resolved.is_relative_to(self.root):
                raise BrainPublicationError(f"publication path escapes brain: {path}")
            relative.append(str(resolved.relative_to(self.root)))
        if relative:
            self._run(["add", "--", *relative])
        staged = subprocess.run(
            ["git", "-C", str(self.root), "diff", "--cached", "--quiet"],
            capture_output=True,
            text=True,
            check=False,
        )
        if staged.returncode not in {0, 1}:
            raise BrainPublicationError(
                staged.stderr.strip() or "could not inspect staged brain changes"
            )
        if staged.returncode == 1:
            self._run(["commit", "-m", message])
        return self.synchronize()

    def synchronize(self) -> BrainPublication:
        branch = self._run(["symbolic-ref", "--short", "HEAD"])
        local = self._run(["rev-parse", "HEAD"])
        remote_url = subprocess.run(
            ["git", "-C", str(self.root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
        )
        if remote_url.returncode != 0:
            return BrainPublication(branch, local, None, False)
        self._run(["fetch", "origin", branch])
        remote = self._run(["rev-parse", f"origin/{branch}"])
        merged = False
        if not self._is_ancestor(remote, local):
            try:
                self._run(
                    [
                        "merge",
                        "--no-edit",
                        "--no-ff",
                        "--allow-unrelated-histories",
                        f"origin/{branch}",
                    ]
                )
            except BrainPublicationError:
                subprocess.run(
                    ["git", "-C", str(self.root), "merge", "--abort"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                raise
            local = self._run(["rev-parse", "HEAD"])
            merged = True
        self._run(["push", "origin", f"HEAD:refs/heads/{branch}"])
        observed = self._run(["ls-remote", "origin", f"refs/heads/{branch}"])
        remote_revision = observed.split()[0] if observed else None
        if remote_revision != local:
            raise BrainPublicationError(
                "brain push returned without the expected remote revision"
            )
        return BrainPublication(branch, local, remote_revision, merged)

    def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "merge-base",
                "--is-ancestor",
                ancestor,
                descendant,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode not in {0, 1}:
            raise BrainPublicationError(
                result.stderr.strip() or "could not compare brain histories"
            )
        return result.returncode == 0

    def _run(self, arguments: list[str]) -> str:
        command = ["git", "-C", str(self.root), *arguments]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise BrainPublicationError(
                f"could not run brain Git operation {arguments[0]}: {error}"
            ) from error
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "no output"
            raise BrainPublicationError(
                f"brain Git operation {arguments[0]} failed: {detail}"
            )
        return result.stdout.strip()

    def evidence(self) -> dict[str, Any]:
        """Return bounded identities suitable for an operation result."""

        branch = self._run(["symbolic-ref", "--short", "HEAD"])
        return {"branch": branch, "revision": self._run(["rev-parse", "HEAD"])}
