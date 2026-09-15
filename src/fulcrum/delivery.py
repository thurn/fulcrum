"""Typed worktree, validation, promotion, and source-publication adapter."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from fulcrum.tollgate import Tollgate, TollgateError, TollgateUncertainError

GIT_CAPTURE_BYTES = 256 * 1024
FAILED_STATES = {
    "failed",
    "merge-conflict",
    "dependency-failed",
    "canceled",
    "superseded",
    "infrastructure-exhausted",
    "check-failed",
}
PASSED_STATES = {
    "ready",
    "promoting",
    "promoted-local-push-pending",
    "promoted",
    "externally-integrated",
    "check-passed",
}
PROMOTED_STATES = {
    "promoted-local-push-pending",
    "promoted",
    "externally-integrated",
}


class DeliveryProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        category: str,
        possible_effect: bool = False,
        evidence: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.possible_effect = possible_effect
        self.evidence: dict[str, Any] = dict(evidence or {})


@dataclass(frozen=True)
class WorkRef:
    bead_id: str
    project_id: str
    project_root: str
    repository_id: str
    intended_path: str
    branch: str
    integration_branch: str
    operation_id: str
    prepare_argv: tuple[str, ...] = ()
    validate_argv: tuple[str, ...] = ()
    source_remote: str | None = None
    require_source_sync: bool = False
    actual_path: str | None = None
    base_oid: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _json_dict(asdict(self))


@dataclass(frozen=True)
class SourceRef:
    work: WorkRef
    oid: str

    def to_dict(self) -> dict[str, Any]:
        return {"work": self.work.to_dict(), "oid": self.oid}


@dataclass(frozen=True)
class WorkspaceFacts:
    path: str | None
    branch: str
    base_oid: str | None
    head_oid: str | None
    exists: bool
    owned: bool
    dirty: bool | None
    dirty_entries: tuple[str, ...]
    preparation: str
    ownership_evidence: Mapping[str, Any]
    observed_at: str
    gaps: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return _json_dict(asdict(self))


@dataclass(frozen=True)
class ValidationFacts:
    handle: str
    source_oid: str
    state: str
    checks: tuple[Mapping[str, Any], ...]
    evidence: Mapping[str, Any]
    observed_at: str
    gaps: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return _json_dict(asdict(self))


@dataclass(frozen=True)
class DeliveryFacts:
    handle: str
    source_oid: str
    validation: str
    promotion: str
    integration_oid: str | None
    synchronization: str
    cleanup: str
    evidence: Mapping[str, Any]
    observed_at: str
    gaps: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return _json_dict(asdict(self))


def _json_dict(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return the exact JSON-native shape that the Beads ledger will retain."""

    return cast(dict[str, Any], json.loads(json.dumps(dict(value))))


class Delivery(Protocol):
    async def prepare(self, work: WorkRef) -> WorkspaceFacts: ...
    async def inspect_workspace(self, work: WorkRef) -> WorkspaceFacts: ...
    async def submit(self, source: SourceRef) -> ValidationFacts: ...
    async def inspect(self, source: SourceRef, handle: str | None) -> DeliveryFacts: ...
    async def promote(self, source: SourceRef, handle: str) -> DeliveryFacts: ...
    async def cancel(self, source: SourceRef, handle: str) -> DeliveryFacts: ...
    async def synchronize(self, source: SourceRef, handle: str) -> DeliveryFacts: ...
    async def cleanup(self, work: WorkRef) -> WorkspaceFacts: ...


class TollgateDelivery:
    def __init__(self, tollgate: Tollgate) -> None:
        self.tollgate = tollgate

    async def prepare(self, work: WorkRef) -> WorkspaceFacts:
        return await asyncio.to_thread(self._prepare, work)

    async def inspect_workspace(self, work: WorkRef) -> WorkspaceFacts:
        return await asyncio.to_thread(self._inspect_workspace, work)

    async def submit(self, source: SourceRef) -> ValidationFacts:
        return await asyncio.to_thread(self._submit, source)

    async def inspect(self, source: SourceRef, handle: str | None) -> DeliveryFacts:
        return await asyncio.to_thread(self._inspect, source, handle)

    async def promote(self, source: SourceRef, handle: str) -> DeliveryFacts:
        return await asyncio.to_thread(self._promote, source, handle)

    async def cancel(self, source: SourceRef, handle: str) -> DeliveryFacts:
        return await asyncio.to_thread(self._cancel, source, handle)

    async def synchronize(self, source: SourceRef, handle: str) -> DeliveryFacts:
        return await asyncio.to_thread(self._synchronize, source, handle)

    async def cleanup(self, work: WorkRef) -> WorkspaceFacts:
        return await asyncio.to_thread(self._cleanup, work)

    def _prepare(self, work: WorkRef) -> WorkspaceFacts:
        _verify_repository(Path(work.project_root))
        existing = self._inspect_workspace(work)
        if existing.exists:
            if not existing.owned:
                raise DeliveryProviderError(
                    "the recorded workspace exists without exact ownership evidence",
                    category="rejected",
                    evidence=existing.to_dict(),
                )
            return existing
        try:
            response = self.tollgate.create_worktree(work.repository_id, work.branch)
        except TollgateError as error:
            recovered = self._inspect_workspace(work)
            if recovered.exists and recovered.owned:
                response = {"recovered_after_error": _error_evidence(error)}
            else:
                raise _provider_error(error) from error
        path = _worktree_path(response)
        if path is None:
            recovered = self._inspect_workspace(work)
            if not recovered.exists or not recovered.owned:
                raise DeliveryProviderError(
                    "Tollgate worktree response did not identify a uniquely owned path",
                    category="uncertain",
                    possible_effect=True,
                    evidence={"response": response},
                )
            path = recovered.path
        assert path is not None
        actual = WorkRef(**{**work.__dict__, "actual_path": path})
        created = self._inspect_workspace(actual)
        if not created.exists or not created.owned:
            raise DeliveryProviderError(
                "created worktree failed exact Git ownership verification",
                category="uncertain",
                possible_effect=True,
                evidence=created.to_dict(),
            )
        preparation = "not_required"
        if work.prepare_argv:
            _run_argv(work.prepare_argv, Path(path))
            preparation = "passed"
        refreshed = self._inspect_workspace(actual)
        return WorkspaceFacts(
            **{
                **refreshed.__dict__,
                "preparation": preparation,
                "ownership_evidence": {
                    **dict(refreshed.ownership_evidence),
                    "provider_response": response,
                },
            }
        )

    def _inspect_workspace(self, work: WorkRef) -> WorkspaceFacts:
        root = Path(work.project_root).resolve(strict=True)
        _verify_repository(root)
        entries = _worktree_entries(root)
        expected_ref = f"refs/heads/{work.branch}"
        matches = [
            entry
            for entry in entries
            if entry.get("branch") == expected_ref
            or (
                work.actual_path is not None
                and entry.get("path")
                == str(Path(work.actual_path).resolve(strict=False))
            )
        ]
        if not matches:
            return WorkspaceFacts(
                path=work.actual_path,
                branch=work.branch,
                base_oid=None,
                head_oid=None,
                exists=False,
                owned=False,
                dirty=None,
                dirty_entries=(),
                preparation="absent",
                ownership_evidence={
                    "project_root": str(root),
                    "expected_branch_ref": expected_ref,
                    "operation_id": work.operation_id,
                    "matches": 0,
                },
                observed_at=_git_time(),
            )
        if len(matches) != 1:
            raise DeliveryProviderError(
                "multiple Git worktrees match the retained delivery identity",
                category="uncertain",
                evidence={"matches": matches, "branch": work.branch},
            )
        match = matches[0]
        path = Path(str(match["path"])).resolve(strict=False)
        exists = path.is_dir()
        common_root: str | None = None
        owned = False
        dirty_entries: tuple[str, ...] = ()
        if exists:
            common_root = str(
                Path(_git(path, ("rev-parse", "--show-toplevel"))).resolve(strict=True)
            )
            common_dir = Path(_git(path, ("rev-parse", "--git-common-dir")))
            if not common_dir.is_absolute():
                common_dir = (path / common_dir).resolve(strict=False)
            project_common = Path(_git(root, ("rev-parse", "--git-common-dir")))
            if not project_common.is_absolute():
                project_common = (root / project_common).resolve(strict=False)
            owned = (
                common_root == str(path)
                and common_dir.resolve(strict=False)
                == project_common.resolve(strict=False)
                and match.get("branch") == expected_ref
            )
            dirty_entries = tuple(
                item
                for item in _git(path, ("status", "--porcelain=v1", "-z")).split("\0")
                if item
            )
        return WorkspaceFacts(
            path=str(path),
            branch=work.branch,
            base_oid=(
                work.base_oid or (str(match.get("head")) if match.get("head") else None)
            ),
            head_oid=(
                _git(path, ("rev-parse", "HEAD")) if exists else match.get("head")
            ),
            exists=exists,
            owned=owned,
            dirty=bool(dirty_entries) if exists else None,
            dirty_entries=dirty_entries,
            preparation="observed" if exists else "absent",
            ownership_evidence={
                "project_root": str(root),
                "git_toplevel": common_root,
                "expected_branch_ref": expected_ref,
                "observed_branch_ref": match.get("branch"),
                "operation_id": work.operation_id,
                "matches": 1,
            },
            observed_at=_git_time(),
        )

    def _submit(self, source: SourceRef) -> ValidationFacts:
        path = _source_workspace(source)
        _verify_source(path, source.oid)
        if source.work.validate_argv:
            _run_argv(source.work.validate_argv, path)
        existing = self._find_candidates(source)
        if len(existing) > 1:
            raise DeliveryProviderError(
                "multiple Tollgate candidates match the exact source and worktree",
                category="uncertain",
                evidence={"candidate_ids": [_item(row).get("id") for row in existing]},
            )
        response: Mapping[str, Any]
        if existing:
            response = existing[0]
        else:
            try:
                response = self.tollgate.submit_candidate(
                    source.work.repository_id, source.oid, cwd=path
                )
            except TollgateError as error:
                recovered = self._find_candidates(source)
                if len(recovered) != 1:
                    raise DeliveryProviderError(
                        str(error),
                        category="uncertain",
                        possible_effect=True,
                        evidence={
                            "provider": _error_evidence(error),
                            "matches": len(recovered),
                        },
                    ) from error
                response = recovered[0]
        facts = _validation_facts(response)
        if facts.source_oid != source.oid:
            raise DeliveryProviderError(
                "Tollgate candidate does not retain the submitted source",
                category="uncertain",
                possible_effect=True,
                evidence=facts.to_dict(),
            )
        return facts

    def _inspect(self, source: SourceRef, handle: str | None) -> DeliveryFacts:
        if handle is None:
            matches = self._find_candidates(source)
            if len(matches) != 1:
                raise DeliveryProviderError(
                    "exact source/worktree lookup did not identify one candidate",
                    category="uncertain",
                    evidence={"matches": len(matches)},
                )
            response = matches[0]
        else:
            try:
                response = self.tollgate.status(source.work.repository_id, handle)
            except TollgateError as error:
                raise _provider_error(error) from error
        return _delivery_facts(response, source, handle)

    def _promote(self, source: SourceRef, handle: str) -> DeliveryFacts:
        before = self._inspect(source, handle)
        if before.source_oid != source.oid:
            raise DeliveryProviderError(
                "promotion handle belongs to a different immutable source",
                category="rejected",
                evidence=before.to_dict(),
            )
        if before.promotion == "promoted":
            return before
        try:
            self.tollgate.approve(source.work.repository_id, handle)
        except TollgateError as error:
            try:
                observed = self._inspect(source, handle)
            except DeliveryProviderError:
                raise _provider_error(error) from error
            if observed.promotion in {"pending", "promoted"}:
                return observed
            raise DeliveryProviderError(
                str(error),
                category="uncertain",
                possible_effect=True,
                evidence={
                    "provider": _error_evidence(error),
                    "observation": observed.to_dict(),
                },
            ) from error
        return self._inspect(source, handle)

    def _cancel(self, source: SourceRef, handle: str) -> DeliveryFacts:
        try:
            self.tollgate.cancel(source.work.repository_id, handle)
        except TollgateError as error:
            observed = self._inspect(source, handle)
            if observed.validation != "failed":
                raise DeliveryProviderError(
                    str(error),
                    category="uncertain",
                    possible_effect=True,
                    evidence={
                        "provider": _error_evidence(error),
                        "observation": observed.to_dict(),
                    },
                ) from error
        return self._inspect(source, handle)

    def _synchronize(self, source: SourceRef, handle: str) -> DeliveryFacts:
        observed = self._inspect(source, handle)
        if observed.promotion != "promoted" or observed.integration_oid is None:
            raise DeliveryProviderError(
                "source synchronization requires an observed promoted integration commit",
                category="rejected",
                evidence=observed.to_dict(),
            )
        if observed.synchronization == "complete":
            return observed
        if not source.work.require_source_sync:
            return DeliveryFacts(
                **{**observed.__dict__, "synchronization": "not_required"}
            )
        remote = source.work.source_remote
        if not remote:
            raise DeliveryProviderError(
                "required source synchronization has no configured remote",
                category="unsupported",
                evidence=observed.to_dict(),
            )
        root = Path(source.work.project_root)
        branch_ref = f"refs/heads/{source.work.integration_branch}"
        before = _remote_oid(root, remote, branch_ref)
        if before is not None and _remote_contains(
            root, remote, branch_ref, observed.integration_oid
        ):
            return DeliveryFacts(
                **{
                    **observed.__dict__,
                    "synchronization": "complete",
                    "evidence": {
                        **dict(observed.evidence),
                        "source_publication": {
                            "remote": remote,
                            "branch": source.work.integration_branch,
                            "remote_oid": before,
                            "contains_integration_oid": True,
                            "pushed": False,
                        },
                    },
                }
            )
        _git(
            root,
            (
                "push",
                remote,
                f"{observed.integration_oid}:{branch_ref}",
            ),
        )
        after = _remote_oid(root, remote, branch_ref)
        contained = after is not None and _remote_contains(
            root, remote, branch_ref, observed.integration_oid
        )
        if not contained:
            raise DeliveryProviderError(
                "source push returned without observed remote ancestry",
                category="uncertain",
                possible_effect=True,
                evidence={
                    "remote": remote,
                    "branch": source.work.integration_branch,
                    "integration_oid": observed.integration_oid,
                    "remote_oid": after,
                },
            )
        return DeliveryFacts(
            **{
                **observed.__dict__,
                "synchronization": "complete",
                "evidence": {
                    **dict(observed.evidence),
                    "source_publication": {
                        "remote": remote,
                        "branch": source.work.integration_branch,
                        "remote_oid": after,
                        "contains_integration_oid": True,
                        "pushed": True,
                    },
                },
            }
        )

    def _cleanup(self, work: WorkRef) -> WorkspaceFacts:
        observed = self._inspect_workspace(work)
        if not observed.exists:
            if _branch_exists(Path(work.project_root), work.branch):
                raise DeliveryProviderError(
                    "managed worktree is absent but its retained branch still exists",
                    category="uncertain",
                    evidence={
                        **observed.to_dict(),
                        "branch_ref": f"refs/heads/{work.branch}",
                        "branch_absent": False,
                    },
                )
            return WorkspaceFacts(
                **{
                    **observed.__dict__,
                    "preparation": "cleanup_complete",
                    "ownership_evidence": {
                        **dict(observed.ownership_evidence),
                        "branch_absent": True,
                    },
                }
            )
        if not observed.owned:
            raise DeliveryProviderError(
                "workspace cleanup lacks exact ownership evidence",
                category="rejected",
                evidence=observed.to_dict(),
            )
        if observed.dirty:
            raise DeliveryProviderError(
                "workspace cleanup refused to discard dirty evidence",
                category="rejected",
                evidence=observed.to_dict(),
            )
        assert observed.path is not None
        try:
            self.tollgate.remove_worktree(work.repository_id, observed.path)
        except TollgateError as error:
            absent = self._inspect_workspace(work)
            if absent.exists:
                raise _provider_error(error) from error
        absent = self._inspect_workspace(work)
        branch_exists = _branch_exists(Path(work.project_root), work.branch)
        if absent.exists or branch_exists:
            raise DeliveryProviderError(
                "workspace removal returned without observed worktree and branch absence",
                category="uncertain",
                possible_effect=True,
                evidence={
                    **absent.to_dict(),
                    "branch_ref": f"refs/heads/{work.branch}",
                    "branch_absent": not branch_exists,
                },
            )
        return WorkspaceFacts(
            **{
                **absent.__dict__,
                "preparation": "cleanup_complete",
                "ownership_evidence": {
                    **dict(absent.ownership_evidence),
                    "branch_absent": True,
                },
            }
        )

    def _find_candidates(self, source: SourceRef) -> list[Mapping[str, Any]]:
        observations: list[Any] = []
        try:
            observations.append(self.tollgate.queue(source.work.repository_id))
        except TollgateError:
            pass
        try:
            observations.append(self.tollgate.history(source.work.repository_id))
        except TollgateError:
            pass
        matches: dict[str, Mapping[str, Any]] = {}
        expected_path = str(_source_workspace(source).resolve(strict=False))
        for observation in observations:
            for candidate in _candidate_rows(observation):
                item = _item(candidate)
                handle = item.get("id")
                metadata = item.get("metadata")
                if (
                    isinstance(handle, str)
                    and _oid(item.get("source_oid")) == source.oid
                    and isinstance(metadata, Mapping)
                    and str(metadata.get("worktree_path")) == expected_path
                ):
                    matches[handle] = candidate
        return list(matches.values())


def _validation_facts(response: Mapping[str, Any]) -> ValidationFacts:
    item = _item(response)
    handle = item.get("id")
    source_oid = _oid(item.get("source_oid"))
    if not isinstance(handle, str) or source_oid is None:
        raise DeliveryProviderError(
            "Tollgate candidate response omitted handle or source",
            category="uncertain",
            possible_effect=True,
            evidence={"response": dict(response)},
        )
    state = str(item.get("state") or "unknown")
    validation = (
        "failed"
        if state in FAILED_STATES
        else (
            "passed"
            if state in PASSED_STATES
            else "running" if state == "running" else "pending"
        )
    )
    buildset = response.get("buildset")
    checks = tuple(
        dict(row)
        for row in (
            buildset.get("step_results", []) if isinstance(buildset, Mapping) else []
        )
        if isinstance(row, Mapping)
    )
    return ValidationFacts(
        handle=handle,
        source_oid=source_oid,
        state=validation,
        checks=checks,
        evidence=_provider_evidence(response),
        observed_at=_git_time(),
    )


def _delivery_facts(
    response: Mapping[str, Any], source: SourceRef, handle: str | None
) -> DeliveryFacts:
    validation = _validation_facts(response)
    item = _item(response)
    if handle is not None and validation.handle != handle:
        raise DeliveryProviderError(
            "Tollgate status returned a different candidate",
            category="uncertain",
            evidence={"expected": handle, "observed": validation.handle},
        )
    state = str(item.get("state") or "unknown")
    authorized = item.get("promotion_authorized") is True
    promotion = (
        "promoted"
        if state in PROMOTED_STATES
        else (
            "failed"
            if state in FAILED_STATES and authorized
            else "pending" if authorized else "not_started"
        )
    )
    integration_oid = _integration_oid(response) if promotion == "promoted" else None
    remote_state = str(item.get("remote_state") or "unknown")
    synchronization = (
        "complete"
        if remote_state == "synchronized"
        else (
            "not_required"
            if remote_state == "disabled" and not source.work.require_source_sync
            else "failed" if remote_state in {"failed", "push-failed"} else "pending"
        )
    )
    cleanup_state = str(item.get("cleanup_state") or "pending")
    cleanup = (
        "complete"
        if cleanup_state == "completed"
        else "failed" if cleanup_state in {"failed", "blocked"} else "pending"
    )
    return DeliveryFacts(
        handle=validation.handle,
        source_oid=validation.source_oid,
        validation=validation.state,
        promotion=promotion,
        integration_oid=integration_oid,
        synchronization=synchronization,
        cleanup=cleanup,
        evidence=_provider_evidence(response),
        observed_at=_git_time(),
        gaps=(
            ("provider did not expose an integration commit",)
            if promotion == "promoted" and integration_oid is None
            else ()
        ),
    )


def _provider_evidence(response: Mapping[str, Any]) -> dict[str, Any]:
    item = _item(response)
    generation = response.get("generation")
    buildset = response.get("buildset")
    certificate = response.get("certificate")
    return {
        "item": dict(item),
        "generation": dict(generation) if isinstance(generation, Mapping) else None,
        "buildset": (
            {
                key: buildset.get(key)
                for key in (
                    "id",
                    "state",
                    "tested_oid",
                    "expected_parent_oid",
                    "step_results",
                )
            }
            if isinstance(buildset, Mapping)
            else None
        ),
        "certificate": (
            {
                key: certificate.get(key)
                for key in ("id", "tested_oid", "tree_oid", "expected_parent_oid")
            }
            if isinstance(certificate, Mapping)
            else None
        ),
    }


def _integration_oid(response: Mapping[str, Any]) -> str | None:
    generation = response.get("generation")
    if isinstance(generation, Mapping):
        prefix = generation.get("prefix_oids")
        if isinstance(prefix, list) and prefix:
            value = _oid(prefix[-1])
            if value is not None:
                return value
        value = _oid(generation.get("tested_oid"))
        if value is not None:
            return value
    return None


def _candidate_rows(value: Any) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    if isinstance(value, Mapping):
        item = value.get("item")
        if isinstance(item, Mapping) and isinstance(item.get("id"), str):
            rows.append(value)
        else:
            normalized = _item(value)
            if (
                isinstance(normalized.get("id"), str)
                and normalized.get("source_oid") is not None
            ):
                rows.append({"item": normalized})
        for key in ("queue", "checks", "items", "data"):
            child = value.get(key)
            if isinstance(child, (Mapping, list)):
                rows.extend(_candidate_rows(child))
    elif isinstance(value, list):
        for child in value:
            rows.extend(_candidate_rows(child))
    return rows


def _item(response: Mapping[str, Any]) -> dict[str, Any]:
    item = response.get("item")
    normalized = dict(item) if isinstance(item, Mapping) else dict(response)
    if not isinstance(normalized.get("id"), str) and isinstance(
        normalized.get("item_id"), str
    ):
        normalized["id"] = normalized["item_id"]
    return normalized


def _oid(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping) and isinstance(value.get("bytes"), str):
        return str(value["bytes"])
    return None


def _worktree_path(response: Mapping[str, Any]) -> str | None:
    for value in (
        response.get("path"),
        response.get("worktree_path"),
        (
            (response.get("worktree") or {}).get("path")
            if isinstance(response.get("worktree"), Mapping)
            else None
        ),
        (
            (response.get("state") or {}).get("path")
            if isinstance(response.get("state"), Mapping)
            else None
        ),
    ):
        if isinstance(value, str) and Path(value).is_absolute():
            return str(Path(value).resolve(strict=False))
    return None


def _worktree_entries(root: Path) -> list[dict[str, str]]:
    output = _git(root, ("worktree", "list", "--porcelain"))
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in output.splitlines() + [""]:
        if not line:
            if current:
                entries.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current["path"] = str(Path(value).resolve(strict=False))
        elif key == "HEAD":
            current["head"] = value
        elif key == "branch":
            current["branch"] = value
    return entries


def _branch_exists(root: Path, branch: str) -> bool:
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    raise DeliveryProviderError(
        "could not inspect the retained Git branch",
        category="unavailable",
        evidence={
            "branch": branch,
            "returncode": completed.returncode,
            "stderr": _bounded_text(completed.stderr),
        },
    )


def _verify_repository(root: Path) -> None:
    observed = Path(_git(root, ("rev-parse", "--show-toplevel"))).resolve(strict=True)
    if observed != root.resolve(strict=True):
        raise DeliveryProviderError(
            f"project root is not the exact Git repository root: {observed}",
            category="rejected",
        )


def _source_workspace(source: SourceRef) -> Path:
    value = source.work.actual_path or source.work.intended_path
    path = Path(value).resolve(strict=False)
    if not path.is_dir():
        raise DeliveryProviderError(
            "the recorded source workspace is absent",
            category="rejected",
            evidence={"path": str(path)},
        )
    return path


def _verify_source(path: Path, oid: str) -> None:
    if len(oid) != 40 or any(character not in "0123456789abcdef" for character in oid):
        raise DeliveryProviderError(
            "source must be a full lowercase commit OID", category="rejected"
        )
    observed = _git(path, ("rev-parse", f"{oid}^{{commit}}"))
    if observed != oid:
        raise DeliveryProviderError(
            "source does not resolve to the exact retained commit",
            category="rejected",
            evidence={"requested": oid, "observed": observed},
        )


def _run_argv(arguments: Sequence[str], cwd: Path) -> None:
    if not arguments or any(
        not isinstance(item, str) or not item for item in arguments
    ):
        raise DeliveryProviderError(
            "configured argv contains an invalid argument", category="unsupported"
        )
    try:
        result = subprocess.run(
            list(arguments),
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except FileNotFoundError as error:
        raise DeliveryProviderError(
            f"configured executable is unavailable: {arguments[0]}",
            category="unavailable",
        ) from error
    except subprocess.TimeoutExpired as error:
        raise DeliveryProviderError(
            f"configured command timed out: {arguments[0]}",
            category="transient",
            evidence={
                "stdout": _bounded_text(error.stdout),
                "stderr": _bounded_text(error.stderr),
            },
        ) from error
    stdout = _bounded_text(result.stdout)
    stderr = _bounded_text(result.stderr)
    if result.returncode != 0:
        raise DeliveryProviderError(
            f"configured command failed: {arguments[0]}",
            category="rejected",
            evidence={
                "returncode": result.returncode,
                "stdout": stdout,
                "stderr": stderr,
            },
        )


def _git(root: Path, arguments: Sequence[str]) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *arguments],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DeliveryProviderError(
            f"Git operation {arguments[0]} was unavailable: {error}",
            category="unavailable",
        ) from error
    if result.returncode != 0:
        raise DeliveryProviderError(
            f"Git operation {arguments[0]} failed",
            category="rejected",
            evidence={
                "returncode": result.returncode,
                "stdout": _bounded_text(result.stdout),
                "stderr": _bounded_text(result.stderr),
            },
        )
    return result.stdout.strip()


def _remote_oid(root: Path, remote: str, branch_ref: str) -> str | None:
    output = _git(root, ("ls-remote", remote, branch_ref))
    return output.split()[0] if output else None


def _remote_contains(root: Path, remote: str, branch_ref: str, oid: str) -> bool:
    _git(root, ("fetch", "--no-tags", remote, branch_ref))
    result = subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", oid, "FETCH_HEAD"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode not in {0, 1}:
        raise DeliveryProviderError(
            "remote ancestry inspection failed",
            category="unavailable",
            evidence={"stderr": _bounded_text(result.stderr)},
        )
    return result.returncode == 0


def _provider_error(error: TollgateError) -> DeliveryProviderError:
    return DeliveryProviderError(
        str(error),
        category=error.category,
        possible_effect=error.possible_effect
        or isinstance(error, TollgateUncertainError),
        evidence=_error_evidence(error),
    )


def _error_evidence(error: TollgateError) -> dict[str, Any]:
    return {
        "category": error.category,
        "possible_effect": error.possible_effect,
        "returncode": error.returncode,
        "duration_ms": error.duration_ms,
        "stdout": error.stdout,
        "stderr": error.stderr,
        "truncated": error.truncated,
    }


def _bounded_text(value: bytes | str | None) -> str:
    if isinstance(value, bytes):
        value = value.decode(errors="replace")
    encoded = (value or "").encode("utf-8")
    return encoded[:GIT_CAPTURE_BYTES].decode("utf-8", errors="replace")


def _git_time() -> str:
    from fulcrum.ledger import utc_now

    return utc_now()
