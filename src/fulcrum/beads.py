"""Thin Beads intake and brain synchronization helpers."""

from __future__ import annotations

import copy
import fcntl
import json
import re
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Literal, TypedDict, cast

from fulcrum.config import RuntimePaths
from fulcrum.records import ProgressRecord, PushObligation, validate_record
from fulcrum.state import atomic_write_record

COMMAND_TIMEOUT_SECONDS = 15
COMMIT_LOCK_TIMEOUT_SECONDS = 10
IDENTIFIER_PATTERN: re.Pattern[str] = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")


class BeadsError(RuntimeError):
    """A Beads or brain synchronization operation could not be completed."""


class BeadLabelFacts(TypedDict):
    project_id: str
    plan_id: str | None
    activation: Literal["queued", "future"] | None
    inherited_activation: bool


class PushAttempt(TypedDict):
    source: Literal["git", "beads"]
    command: list[str]
    ok: bool
    detail: str


@dataclass(frozen=True)
class BeadDraft:
    """Implementation-ready content for one idempotently created bead."""

    intake_key: str
    title: str
    project_id: str
    problem: str
    outcome: str
    bounded_scope: str
    context: str
    dependencies: tuple[str, ...]
    acceptance_criteria: str
    validation: str
    authorized_model_overrides: str = "None"
    plan_id: str | None = None
    activation: Literal["queued", "future"] | None = None
    issue_type: Literal["bug", "feature", "task", "epic", "chore", "decision"] = "task"
    priority: int = 2

    def __post_init__(self) -> None:
        _require_identifier(self.project_id, "project")
        if self.plan_id is not None:
            _require_identifier(self.plan_id, "plan")
            if self.activation is not None:
                raise BeadsError(
                    "plan beads inherit activation and cannot carry activation labels"
                )
        if not self.intake_key.strip():
            raise BeadsError("intake key must not be empty")
        if not 0 <= self.priority <= 4:
            raise BeadsError("priority must be between 0 and 4")
        for name, value in (
            ("title", self.title),
            ("problem", self.problem),
            ("outcome", self.outcome),
            ("bounded scope", self.bounded_scope),
            ("context", self.context),
            ("acceptance criteria", self.acceptance_criteria),
            ("validation", self.validation),
        ):
            if not value.strip():
                raise BeadsError(f"{name} must not be empty")

    @property
    def labels(self) -> tuple[str, ...]:
        labels = [f"project:{self.project_id}"]
        if self.plan_id is not None:
            labels.append(f"plan:{self.plan_id}")
        else:
            labels.append(f"activation:{self.activation or 'queued'}")
        return tuple(labels)

    @property
    def external_reference(self) -> str:
        return f"fulcrum-intake:{self.intake_key}"

    def description(self) -> str:
        dependencies = "\n".join(f"- {item}" for item in self.dependencies) or "- None"
        return (
            f"## Problem\n\n{self.problem}\n\n"
            f"## Outcome\n\n{self.outcome}\n\n"
            f"## Bounded scope\n\n{self.bounded_scope}\n\n"
            f"## Context\n\n{self.context}\n\n"
            f"## Dependencies\n\n{dependencies}\n\n"
            f"## Acceptance criteria\n\n{self.acceptance_criteria}\n\n"
            f"## Validation\n\n{self.validation}\n\n"
            "## Authorized model overrides\n\n"
            f"{self.authorized_model_overrides}"
        )


def _require_identifier(value: str, label: str) -> None:
    if not IDENTIFIER_PATTERN.fullmatch(value):
        raise BeadsError(
            f"invalid {label} identifier {value!r}; use lowercase letters, digits, and hyphens"
        )


def bead_label_facts(labels: list[str] | tuple[str, ...]) -> BeadLabelFacts:
    """Validate Fulcrum labels and resolve the effective activation."""

    projects = [
        label.removeprefix("project:")
        for label in labels
        if label.startswith("project:")
    ]
    plans = [
        label.removeprefix("plan:") for label in labels if label.startswith("plan:")
    ]
    activations = [
        label.removeprefix("activation:")
        for label in labels
        if label.startswith("activation:")
    ]
    if len(projects) != 1:
        raise BeadsError("each bead must have exactly one project:<id> label")
    if len(plans) > 1:
        raise BeadsError("a bead may have at most one plan:<id> label")
    if len(activations) > 1:
        raise BeadsError("a bead may have at most one activation label")
    _require_identifier(projects[0], "project")
    if plans:
        _require_identifier(plans[0], "plan")
        if activations:
            raise BeadsError(
                "plan beads inherit activation and cannot carry activation labels"
            )
        return {
            "project_id": projects[0],
            "plan_id": plans[0],
            "activation": None,
            "inherited_activation": True,
        }
    activation = activations[0] if activations else "queued"
    if activation not in {"queued", "future"}:
        raise BeadsError(
            f"invalid activation {activation!r}; expected 'queued' or 'future'"
        )
    return {
        "project_id": projects[0],
        "plan_id": None,
        "activation": activation,
        "inherited_activation": False,
    }


def _command(
    command: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = COMMAND_TIMEOUT_SECONDS,
    allow_failure: bool = False,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BeadsError(f"could not run {command[0]!r}: {error}") from error
    if completed.returncode != 0 and not allow_failure:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no output"
        raise BeadsError(
            f"command failed ({completed.returncode}): {' '.join(command)}: {detail}"
        )
    return completed


def run_beads(
    brain_root: Path,
    arguments: list[str],
    *,
    timeout: int = COMMAND_TIMEOUT_SECONDS,
) -> Any:
    """Run one explicitly brain-scoped Beads command and parse its JSON output."""

    root = brain_root.resolve(strict=True)
    command = ["bd", "--directory", str(root), *arguments, "--json"]
    completed = _command(command, timeout=timeout)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise BeadsError(
            f"Beads returned invalid JSON for {' '.join(command)}: {error}"
        ) from error


def _issue_labels(issue: dict[str, Any]) -> list[str]:
    labels = issue.get("labels", [])
    if not isinstance(labels, list) or not all(
        isinstance(item, str) for item in labels
    ):
        raise BeadsError("Beads issue returned malformed labels")
    return cast(list[str], labels)


def _issue_id(issue: dict[str, Any]) -> str:
    issue_id = issue.get("id")
    if not isinstance(issue_id, str) or not issue_id:
        raise BeadsError("Beads issue response has no string id")
    return issue_id


def ensure_bead(brain_root: Path, draft: BeadDraft) -> str:
    """Create a bead once, or reconcile dependencies after an interrupted intake."""

    issues = run_beads(brain_root, ["list", "--all", "--limit", "0"])
    if not isinstance(issues, list):
        raise BeadsError("bd list returned an unexpected response")
    matches = [
        item
        for item in issues
        if isinstance(item, dict)
        and item.get("external_ref") == draft.external_reference
    ]
    if len(matches) > 1:
        raise BeadsError(f"duplicate beads already use {draft.external_reference!r}")
    if matches:
        issue = cast(dict[str, Any], matches[0])
        facts = bead_label_facts(_issue_labels(issue))
        if facts["project_id"] != draft.project_id or facts["plan_id"] != draft.plan_id:
            raise BeadsError(
                f"existing intake {draft.external_reference!r} has different project or plan labels"
            )
        issue_id = _issue_id(issue)
    else:
        arguments = [
            "create",
            draft.title,
            "--type",
            draft.issue_type,
            "--priority",
            str(draft.priority),
            "--labels",
            ",".join(draft.labels),
            "--description",
            draft.description(),
            "--acceptance",
            draft.acceptance_criteria,
            "--context",
            draft.context,
            "--external-ref",
            draft.external_reference,
            "--dolt-auto-commit",
            "batch",
        ]
        created = run_beads(brain_root, arguments)
        if not isinstance(created, dict):
            raise BeadsError("bd create returned an unexpected response")
        issue_id = _issue_id(cast(dict[str, Any], created))

    for dependency in draft.dependencies:
        run_beads(
            brain_root,
            [
                "dep",
                "add",
                issue_id,
                dependency,
                "--type",
                "blocks",
                "--dolt-auto-commit",
                "batch",
            ],
        )
    return issue_id


def _git_dir(brain_root: Path) -> Path:
    output = _command(
        ["git", "-C", str(brain_root), "rev-parse", "--absolute-git-dir"]
    ).stdout.strip()
    return Path(output)


@contextmanager
def brain_commit_lock(
    brain_root: Path, timeout: float = COMMIT_LOCK_TIMEOUT_SECONDS
) -> Iterator[None]:
    """Serialize only Git staging and committing in the shared brain."""

    lock_path = _git_dir(brain_root) / "fulcrum-commit.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise BeadsError(
                        f"timed out waiting for brain commit lock {lock_path}"
                    )
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _safe_brain_paths(brain_root: Path, paths: list[Path]) -> list[str]:
    root = brain_root.resolve(strict=True)
    relative: list[str] = []
    for path in paths:
        candidate = (
            (root / path).resolve(strict=False)
            if not path.is_absolute()
            else path.resolve(strict=False)
        )
        if not candidate.is_relative_to(root) or candidate == root:
            raise BeadsError(f"commit path is outside the brain: {path}")
        relative.append(candidate.relative_to(root).as_posix())
    if not relative:
        raise BeadsError("at least one Markdown path is required")
    return relative


def commit_markdown(brain_root: Path, paths: list[Path], message: str) -> str:
    """Stage only declared paths and create one Git commit under the narrow lock."""

    root = brain_root.resolve(strict=True)
    relative = _safe_brain_paths(root, paths)
    with brain_commit_lock(root):
        staged = _command(
            ["git", "-C", str(root), "diff", "--cached", "--name-only", "-z"]
        ).stdout
        if staged:
            names = ", ".join(item for item in staged.split("\0") if item)
            raise BeadsError(f"brain already has staged content: {names}")
        _command(["git", "-C", str(root), "add", "--", *relative])
        selected = {
            item
            for item in _command(
                ["git", "-C", str(root), "diff", "--cached", "--name-only", "-z"]
            ).stdout.split("\0")
            if item
        }
        if selected != set(relative):
            raise BeadsError(
                "staged paths differ from the intended brain change: "
                f"expected {sorted(relative)}, got {sorted(selected)}"
            )
        _command(["git", "-C", str(root), "commit", "-m", message])
        return _command(["git", "-C", str(root), "rev-parse", "HEAD"]).stdout.strip()


def _push_attempt(
    source: Literal["git", "beads"], command: list[str], *, cwd: Path | None = None
) -> PushAttempt:
    completed = _command(command, cwd=cwd, allow_failure=True)
    detail = completed.stderr.strip() or completed.stdout.strip()
    if not detail:
        detail = (
            "push completed"
            if completed.returncode == 0
            else "push failed without output"
        )
    return {
        "source": source,
        "command": command,
        "ok": completed.returncode == 0,
        "detail": detail,
    }


def commit_and_push_markdown(
    brain_root: Path, paths: list[Path], message: str
) -> tuple[str, PushAttempt]:
    """Commit Markdown under the lock, release it, then immediately push Git."""

    root = brain_root.resolve(strict=True)
    commit_oid = commit_markdown(root, paths, message)
    attempt = _push_attempt("git", ["git", "-C", str(root), "push"])
    return commit_oid, attempt


def commit_and_push_beads(brain_root: Path, message: str) -> PushAttempt:
    """Commit Beads' Dolt working set, then immediately attempt its own push."""

    root = brain_root.resolve(strict=True)
    _command(["bd", "--directory", str(root), "dolt", "commit", "--message", message])
    return _push_attempt(
        "beads", ["bd", "--directory", str(root), "dolt", "push", "--json"]
    )


def record_push_obligations(
    paths: RuntimePaths,
    progress: ProgressRecord,
    attempts: list[PushAttempt],
    *,
    updated_at: str,
) -> Path:
    """Persist only failed pushes in the responsible role's owned progress record."""

    obligations: list[PushObligation] = []
    for attempt in attempts:
        if not attempt["ok"]:
            obligations.append(
                {
                    "source": attempt["source"],
                    "command": attempt["command"],
                    "detail": attempt["detail"],
                }
            )
    value = copy.deepcopy(progress)
    value["updated_at"] = updated_at
    value["push_obligations"] = obligations
    validated = validate_record(value)
    if validated["record_kind"] != "progress":
        raise BeadsError("push obligations require a progress record")
    return atomic_write_record(paths, cast(ProgressRecord, validated))
