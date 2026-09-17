"""Explicit non-destructive publication for the Fulcrum brain repository."""

from __future__ import annotations

from fulcrum.coordination import unlocked

import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable, Mapping, Sequence
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
        documents: list[dict[str, str]] = []
        for path in paths:
            resolved = path.resolve(strict=False)
            if not resolved.is_relative_to(self.root):
                raise BrainPublicationError(f"publication path escapes brain: {path}")
            try:
                content = resolved.read_text(encoding="utf-8")
            except OSError as error:
                raise BrainPublicationError(
                    f"could not read selected brain publication {path}: {error}"
                ) from error
            documents.append(
                {
                    "relative_path": resolved.relative_to(self.root).as_posix(),
                    "content": content,
                }
            )
        branch = self._run(["symbolic-ref", "--short", "HEAD"])
        remote = subprocess.run(
            ["git", "-C", str(self.root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
        )
        try:
            result = IsolatedGitPublisher(timeout=self.timeout).publish(
                {
                    "operation_id": "legacy-" + uuid.uuid4().hex,
                    "documents": documents,
                    "allow_live_selected": True,
                },
                PublicationDestination(
                    root=self.root,
                    worktree_root=(
                        self.root.parent / f".{self.root.name}-fulcrum-publications"
                    ),
                    remote="origin" if remote.returncode == 0 else None,
                    branch=branch,
                    require_remote_sync=remote.returncode == 0,
                ),
            )
        except KnowledgePublicationError as error:
            raise BrainPublicationError(str(error)) from error
        local = result.get("local")
        remote_result = result.get("remote")
        if not isinstance(local, Mapping) or not isinstance(local.get("commit"), str):
            raise BrainPublicationError(
                "isolated brain publication made no local commit"
            )
        if remote.returncode == 0 and (
            not isinstance(remote_result, Mapping)
            or remote_result.get("state") != "observed"
        ):
            detail = (
                remote_result.get("error")
                if isinstance(remote_result, Mapping)
                else "remote publication was not observed"
            )
            raise BrainPublicationError(str(detail))
        return BrainPublication(
            branch,
            str(local["commit"]),
            (
                str(remote_result["commit"])
                if isinstance(remote_result, Mapping)
                and isinstance(remote_result.get("commit"), str)
                else None
            ),
            bool(result.get("merged_remote")),
        )

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
        if self._is_ancestor(local, remote):
            return BrainPublication(branch, local, remote, False)
        if not self._is_ancestor(remote, local):
            raise BrainPublicationError(
                "brain histories diverged; use isolated publication repair"
            )
        self._run(["push", "origin", f"HEAD:refs/heads/{branch}"])
        observed = self._run(["ls-remote", "origin", f"refs/heads/{branch}"])
        remote_revision = observed.split()[0] if observed else None
        if remote_revision != local:
            raise BrainPublicationError(
                "brain push returned without the expected remote revision"
            )
        return BrainPublication(branch, local, remote_revision, False)

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

    @unlocked
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


class KnowledgePublicationError(RuntimeError):
    """A selected publication target is invalid or cannot be inspected safely."""


@dataclass(frozen=True)
class PublicationDestination:
    root: Path
    worktree_root: Path
    remote: str | None
    branch: str | None
    require_remote_sync: bool


class IsolatedGitPublisher:
    """Publish selected content without touching the source checkout's index."""

    def __init__(
        self,
        *,
        timeout: float = 60,
        after_commit: Callable[[str], None] | None = None,
        after_push: Callable[[str], None] | None = None,
    ) -> None:
        self.timeout = timeout
        self.after_commit = after_commit
        self.after_push = after_push

    def validate(
        self,
        documents: Sequence[Mapping[str, Any]],
        destination: PublicationDestination,
    ) -> dict[str, Any]:
        root = destination.root.resolve(strict=True)
        top = Path(self._run(root, ("rev-parse", "--show-toplevel"))).resolve(
            strict=True
        )
        if top != root:
            raise KnowledgePublicationError(
                f"publication root must be the exact Git repository root: {top}"
            )
        worktree_root = destination.worktree_root.resolve(strict=False)
        if worktree_root == root or worktree_root.is_relative_to(root):
            raise KnowledgePublicationError(
                "publication worktrees must be outside the destination repository"
            )
        selected: list[dict[str, str]] = []
        for document in documents:
            relative = document.get("relative_path")
            content = document.get("content")
            if not isinstance(relative, str) or not relative.strip():
                raise KnowledgePublicationError(
                    "publication relative_path must be nonempty"
                )
            if not isinstance(content, str):
                raise KnowledgePublicationError("publication content must be text")
            path = Path(relative)
            if (
                path.is_absolute()
                or not path.parts
                or ".." in path.parts
                or path.parts[0] in {".git", ".beads"}
            ):
                raise KnowledgePublicationError(
                    f"publication path escapes its destination: {relative}"
                )
            resolved = (root / path).resolve(strict=False)
            if not resolved.is_relative_to(root):
                raise KnowledgePublicationError(
                    f"publication path follows a symlink outside its destination: {relative}"
                )
            selected.append({"relative_path": path.as_posix(), "content": content})
        if not selected:
            raise KnowledgePublicationError("publication requires selected content")
        if len({item["relative_path"] for item in selected}) != len(selected):
            raise KnowledgePublicationError("publication paths must be unique")
        branch = destination.branch or self._branch(root)
        return {
            "root": str(root),
            "worktree_root": str(worktree_root),
            "remote": destination.remote,
            "branch": branch,
            "require_remote_sync": destination.require_remote_sync,
            "documents": selected,
        }

    def publish(
        self, record: Mapping[str, Any], destination: PublicationDestination
    ) -> dict[str, Any]:
        documents = record.get("documents")
        operation_id = record.get("operation_id")
        if not isinstance(documents, list) or not isinstance(operation_id, str):
            raise KnowledgePublicationError(
                "publication record requires operation_id and documents"
            )
        intent = self.validate(documents, destination)
        root = Path(str(intent["root"]))
        selected = [dict(item) for item in intent["documents"]]
        if not record.get("allow_live_selected"):
            for document in selected:
                dirty = self._run_optional(
                    root,
                    (
                        "status",
                        "--porcelain=v1",
                        "--untracked-files=all",
                        "--",
                        document["relative_path"],
                    ),
                )
                if dirty:
                    raise KnowledgePublicationError(
                        "selected publication path has live uncommitted content; "
                        f"preserved without import: {document['relative_path']}"
                    )
        worktree = Path(str(intent["worktree_root"])) / operation_id
        worktree.parent.mkdir(parents=True, exist_ok=True)
        response_loss: list[str] = []
        retained_conflict: str | None = None
        premerged_remote = False
        existing_worktree = (worktree / ".git").exists()
        if not existing_worktree:
            if worktree.exists() and any(worktree.iterdir()):
                raise KnowledgePublicationError(
                    f"publication worktree path is occupied: {worktree}"
                )
            base = self._base_commit(root)
            remote_name = str(intent["remote"]) if intent["remote"] else None
            remote_commit: str | None = None
            if remote_name is not None:
                fetched, _ = self._fetch(root, remote_name, str(intent["branch"]))
                if fetched:
                    remote_commit = self._remote_commit(
                        root, remote_name, str(intent["branch"])
                    )
            prior_commit = record.get("prior_publication_commit")
            if remote_commit is not None:
                retained_conflict = self._selected_history_conflict(
                    root,
                    base,
                    remote_commit,
                    str(prior_commit) if isinstance(prior_commit, str) else None,
                    selected,
                )
                if retained_conflict is None and self._is_ancestor(
                    root, base, remote_commit
                ):
                    base = remote_commit
            self._run(
                root,
                ("worktree", "add", "--detach", str(worktree), base),
            )
            if (
                remote_commit is not None
                and retained_conflict is None
                and not self._is_ancestor(worktree, remote_commit, base)
            ):
                merge = self._run_result(
                    worktree,
                    (
                        "-c",
                        "user.name=Fulcrum",
                        "-c",
                        "user.email=fulcrum@localhost",
                        "merge",
                        "--no-edit",
                        "--no-ff",
                        remote_commit,
                    ),
                )
                if merge.returncode != 0:
                    self._run_result(worktree, ("merge", "--abort"))
                    retained_conflict = (
                        merge.stderr.strip()
                        or merge.stdout.strip()
                        or "local and remote publication histories conflict"
                    )
                else:
                    premerged_remote = True
        else:
            remote_name = str(intent["remote"]) if intent["remote"] else None
            if remote_name is not None:
                fetched, _ = self._fetch(root, remote_name, str(intent["branch"]))
                remote_commit = (
                    self._remote_commit(root, remote_name, str(intent["branch"]))
                    if fetched
                    else None
                )
                worktree_head = self._run_optional(worktree, ("rev-parse", "HEAD"))
                if (
                    remote_commit is not None
                    and worktree_head is not None
                    and not self._is_ancestor(root, worktree_head, remote_commit)
                ):
                    prior_commit = record.get("prior_publication_commit")
                    retained_conflict = self._selected_history_conflict(
                        root,
                        self._base_commit(root),
                        remote_commit,
                        (str(prior_commit) if isinstance(prior_commit, str) else None),
                        selected,
                    )
        worktree_root = Path(
            self._run(worktree, ("rev-parse", "--show-toplevel"))
        ).resolve(strict=True)
        if worktree_root != worktree.resolve(strict=True):
            raise KnowledgePublicationError(
                f"publication worktree identity changed: {worktree_root}"
            )
        for document in selected:
            target = worktree / document["relative_path"]
            resolved_target = target.resolve(strict=False)
            if not resolved_target.is_relative_to(worktree_root):
                raise KnowledgePublicationError(
                    "publication path follows a worktree symlink outside its destination: "
                    + document["relative_path"]
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(document["content"], encoding="utf-8")
        paths = tuple(document["relative_path"] for document in selected)
        self._run(worktree, ("add", "--", *paths))
        changed = bool(
            self._run_optional(worktree, ("diff", "--cached", "--name-only", "--"))
        )
        if changed:
            self._run(
                worktree,
                (
                    "-c",
                    "user.name=Fulcrum",
                    "-c",
                    "user.email=fulcrum@localhost",
                    "commit",
                    "-m",
                    f"docs: publish Fulcrum knowledge ({operation_id})",
                    "--",
                    *paths,
                ),
            )
        local_commit = self._run(worktree, ("rev-parse", "HEAD"))
        try:
            if self.after_commit is not None:
                self.after_commit(local_commit)
        except Exception:
            recovered = self._recover_local(worktree, selected, operation_id)
            if recovered != local_commit:
                raise KnowledgePublicationError(
                    "lost commit response could not be reconciled to one local commit"
                )
            response_loss.append("commit")
        remote_result = (
            self._remote_failure(
                worktree,
                retained_conflict,
                require_remote=bool(intent["require_remote_sync"]),
                conflict=True,
            )
            if retained_conflict is not None
            else self._synchronize(
                worktree,
                remote=(str(intent["remote"]) if intent["remote"] else None),
                branch=str(intent["branch"]),
                local_commit=local_commit,
                require_remote=bool(intent["require_remote_sync"]),
                operation_id=operation_id,
            )
        )
        if remote_result.pop("push_response_lost", False):
            response_loss.append("push")
        if (
            premerged_remote
            and remote_result.get("remote", {}).get("state") == "observed"
        ):
            remote_result["merged_remote"] = True
        return {
            "destination_root": str(root),
            "selected_paths": list(paths),
            "worktree_path": str(worktree),
            "branch": intent["branch"],
            "remote_name": intent["remote"],
            "local": {
                "state": "observed",
                "commit": local_commit,
                "created_commit": changed,
            },
            **remote_result,
            "response_loss_reconciled": response_loss,
        }

    def inspect(self, receipt: Mapping[str, Any]) -> dict[str, Any]:
        worktree_value = receipt.get("worktree_path")
        local = receipt.get("local")
        remote = receipt.get("remote_name")
        branch = receipt.get("branch")
        if (
            not isinstance(worktree_value, str)
            or not isinstance(local, Mapping)
            or not isinstance(local.get("commit"), str)
            or not isinstance(branch, str)
        ):
            raise KnowledgePublicationError("publication receipt is incomplete")
        worktree = Path(worktree_value).resolve(strict=True)
        local_commit = str(local["commit"])
        local_observed = (
            self._run_optional(worktree, ("cat-file", "-t", local_commit)) == "commit"
        )
        if not isinstance(remote, str) or not remote:
            return {
                "local_observed": local_observed,
                "remote_commit": None,
                "remote_contains_local": False,
            }
        fetched, error = self._fetch(worktree, remote, branch)
        if not fetched:
            return {
                "local_observed": local_observed,
                "remote_commit": None,
                "remote_contains_local": False,
                "error": error,
            }
        remote_commit = self._remote_commit(worktree, remote, branch)
        return {
            "local_observed": local_observed,
            "remote_commit": remote_commit,
            "remote_contains_local": bool(
                remote_commit
                and self._is_ancestor(worktree, local_commit, remote_commit)
            ),
        }

    def _synchronize(
        self,
        worktree: Path,
        *,
        remote: str | None,
        branch: str,
        local_commit: str,
        require_remote: bool,
        operation_id: str,
    ) -> dict[str, Any]:
        if remote is None:
            return {
                "remote": {
                    "state": "failed" if require_remote else "not_required",
                    "commit": None,
                    "contains_local": False,
                    "error": "no publication remote is configured",
                },
                "pushed": False,
                "merged_remote": False,
                "repair": (
                    {
                        "kind": "publication_remote",
                        "reason": "no publication remote is configured",
                        "worktree_path": str(worktree),
                    }
                    if require_remote
                    else None
                ),
            }
        fetched, fetch_error = self._fetch(worktree, remote, branch)
        remote_commit = (
            self._remote_commit(worktree, remote, branch) if fetched else None
        )
        if fetch_error and remote_commit is None:
            return self._remote_failure(
                worktree, fetch_error, require_remote=require_remote
            )
        if remote_commit and self._is_ancestor(worktree, local_commit, remote_commit):
            return self._remote_success(remote_commit, pushed=False, merged=False)
        merged = False
        if remote_commit and not self._is_ancestor(
            worktree, remote_commit, self._run(worktree, ("rev-parse", "HEAD"))
        ):
            merge = self._run_result(
                worktree,
                (
                    "-c",
                    "user.name=Fulcrum",
                    "-c",
                    "user.email=fulcrum@localhost",
                    "merge",
                    "--no-edit",
                    "--no-ff",
                    remote_commit,
                ),
            )
            if merge.returncode != 0:
                self._run_result(worktree, ("merge", "--abort"))
                return self._remote_failure(
                    worktree,
                    merge.stderr.strip()
                    or merge.stdout.strip()
                    or "remote publication conflicts with selected local content",
                    require_remote=require_remote,
                    conflict=True,
                )
            merged = True
        push = self._run_result(worktree, ("push", remote, f"HEAD:refs/heads/{branch}"))
        push_response_lost = False
        if push.returncode == 0:
            try:
                if self.after_push is not None:
                    self.after_push(operation_id)
            except Exception:
                push_response_lost = True
        fetched, fetch_error = self._fetch(worktree, remote, branch)
        observed = self._remote_commit(worktree, remote, branch) if fetched else None
        if observed and self._is_ancestor(worktree, local_commit, observed):
            result = self._remote_success(observed, pushed=True, merged=merged)
            result["push_response_lost"] = push_response_lost
            return result
        detail = (
            fetch_error
            or push.stderr.strip()
            or push.stdout.strip()
            or "remote does not contain the recorded publication commit"
        )
        return self._remote_failure(
            worktree,
            detail,
            require_remote=require_remote,
            conflict=push.returncode != 0,
        )

    @staticmethod
    def _remote_success(
        remote_commit: str, *, pushed: bool, merged: bool
    ) -> dict[str, Any]:
        return {
            "remote": {
                "state": "observed",
                "commit": remote_commit,
                "contains_local": True,
                "error": None,
            },
            "pushed": pushed,
            "merged_remote": merged,
            "repair": None,
        }

    @staticmethod
    def _remote_failure(
        worktree: Path,
        detail: str,
        *,
        require_remote: bool,
        conflict: bool = False,
    ) -> dict[str, Any]:
        return {
            "remote": {
                "state": "failed" if require_remote else "unavailable",
                "commit": None,
                "contains_local": False,
                "error": detail,
            },
            "pushed": False,
            "merged_remote": False,
            "repair": {
                "kind": "publication_conflict" if conflict else "publication_remote",
                "reason": detail,
                "worktree_path": str(worktree),
            },
        }

    def _fetch(self, root: Path, remote: str, branch: str) -> tuple[bool, str | None]:
        if self._run_optional(root, ("remote", "get-url", remote)) is None:
            return False, f"publication remote {remote!r} is not configured"
        result = self._run_result(
            root,
            (
                "fetch",
                "--no-tags",
                remote,
                f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}",
            ),
        )
        if result.returncode == 0:
            return True, None
        detail = result.stderr.strip() or result.stdout.strip() or "Git fetch failed"
        # An empty remote has no branch yet and remains a valid push target.
        if "couldn't find remote ref" in detail.lower():
            return True, None
        return False, detail

    def _remote_commit(self, root: Path, remote: str, branch: str) -> str | None:
        return self._run_optional(
            root, ("rev-parse", "--verify", f"refs/remotes/{remote}/{branch}")
        )

    def _recover_local(
        self,
        worktree: Path,
        selected: Sequence[Mapping[str, str]],
        operation_id: str,
    ) -> str:
        commit = self._run(worktree, ("rev-parse", "HEAD"))
        message = self._run(worktree, ("show", "-s", "--format=%B", commit))
        if operation_id not in message:
            raise KnowledgePublicationError(
                "publication worktree HEAD is not the recorded operation commit"
            )
        for document in selected:
            if (worktree / document["relative_path"]).read_text(
                encoding="utf-8"
            ) != document["content"]:
                raise KnowledgePublicationError(
                    "publication worktree content differs from retained intent"
                )
        return commit

    def _selected_history_conflict(
        self,
        root: Path,
        local_commit: str,
        remote_commit: str,
        prior_commit: str | None,
        selected: Sequence[Mapping[str, str]],
    ) -> str | None:
        remote_contains_prior = bool(
            prior_commit
            and self._run_optional(root, ("cat-file", "-t", prior_commit)) == "commit"
            and self._is_ancestor(root, prior_commit, remote_commit)
        )
        for document in selected:
            path = document["relative_path"]
            local_content = self._show_path(root, local_commit, path)
            remote_content = self._show_path(root, remote_commit, path)
            if local_content == remote_content:
                continue
            prior_content = (
                self._show_path(root, prior_commit, path)
                if remote_contains_prior and prior_commit is not None
                else None
            )
            if (
                remote_contains_prior
                and remote_content == prior_content
                and local_content in {None, prior_content}
            ):
                continue
            if local_content is None and remote_content is None:
                continue
            return (
                "selected publication path changed outside the retained canonical "
                f"publication: {path}"
            )
        return None

    def _show_path(self, root: Path, commit: str, path: str) -> str | None:
        result = self._run_result(root, ("show", f"{commit}:{path}"))
        return result.stdout if result.returncode == 0 else None

    def _base_commit(self, root: Path) -> str:
        current = self._run_optional(root, ("rev-parse", "--verify", "HEAD"))
        if current is not None:
            return current
        tree = self._run_with_input(root, ("mktree",), "")
        return self._run_with_input(
            root,
            ("commit-tree", tree, "-m", "chore: initialize publication history"),
            "",
            identity=True,
        )

    def _branch(self, root: Path) -> str:
        value = self._run_optional(root, ("symbolic-ref", "--short", "HEAD"))
        return value or "main"

    def _is_ancestor(self, root: Path, ancestor: str, descendant: str) -> bool:
        result = self._run_result(
            root, ("merge-base", "--is-ancestor", ancestor, descendant)
        )
        if result.returncode not in {0, 1}:
            raise KnowledgePublicationError(
                result.stderr.strip() or "could not inspect publication ancestry"
            )
        return result.returncode == 0

    @unlocked
    def _run(self, root: Path, arguments: Sequence[str]) -> str:
        result = self._run_result(root, arguments)
        if result.returncode != 0:
            raise KnowledgePublicationError(
                result.stderr.strip()
                or result.stdout.strip()
                or f"Git {arguments[0]} failed"
            )
        return result.stdout.strip()

    def _run_optional(self, root: Path, arguments: Sequence[str]) -> str | None:
        result = self._run_result(root, arguments)
        return result.stdout.strip() if result.returncode == 0 else None

    @unlocked
    def _run_result(
        self, root: Path, arguments: Sequence[str]
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["git", "-C", str(root), *arguments],
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise KnowledgePublicationError(
                f"could not run Git {arguments[0]}: {error}"
            ) from error

    @unlocked
    def _run_with_input(
        self,
        root: Path,
        arguments: Sequence[str],
        input_text: str,
        *,
        identity: bool = False,
    ) -> str:
        command = ["git", "-C", str(root)]
        if identity:
            command.extend(
                (
                    "-c",
                    "user.name=Fulcrum",
                    "-c",
                    "user.email=fulcrum@localhost",
                )
            )
        command.extend(arguments)
        try:
            result = subprocess.run(
                command,
                input=input_text,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise KnowledgePublicationError(
                f"could not run Git {arguments[0]}: {error}"
            ) from error
        if result.returncode != 0:
            raise KnowledgePublicationError(
                result.stderr.strip()
                or result.stdout.strip()
                or f"Git {arguments[0]} failed"
            )
        return result.stdout.strip()
