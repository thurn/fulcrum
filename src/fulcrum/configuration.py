"""Authoritative round-trip YAML configuration and project enrollment."""

from __future__ import annotations

import copy
import io
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML  # pyre-ignore[21]

from fulcrum.contracts import CommandResult, CommandState, FulcrumError, ParsedRequest
from fulcrum.ledger import Ledger, LedgerFailure, OperationRecord, operation_view
from fulcrum.tollgate import Tollgate, TollgateError, TollgateUncertainError

ROLES: tuple[str, ...] = (
    "vizier",
    "marshal",
    "weaver",
    "executor",
    "warden",
    "sage",
    "mason",
    "justiciar",
)
EFFORTS: set[str] = {"low", "medium", "high", "xhigh", "max", "ultra"}
TOP_LEVEL: set[str] = {
    "runtime",
    "delivery",
    "beads",
    "brain",
    "projects",
    "models",
    "policy",
    "knowledge",
    "source_watch_root",
    "timing",
    "diagnostics",
    "resources",
}
RESTART_FIELDS: tuple[str, ...] = (
    "runtime.endpoint",
    "runtime.executable",
    "beads.host",
    "beads.port",
    "beads.database",
    "beads.executable",
)


def default_config(brain_root: Path) -> dict[str, Any]:
    model_defaults = {
        role: {"model": "gpt-5.6-sol", "effort": "high"} for role in ROLES
    }
    return {
        "runtime": {
            "kind": "codex",
            "endpoint": "ws://127.0.0.1:4500",
            "executable": None,
        },
        "delivery": {"kind": "tollgate", "executable": shutil.which("tg")},
        "beads": {
            "executable": shutil.which("bd"),
            "host": "127.0.0.1",
            "port": 3309,
            "database": "fulcrum",
        },
        "brain": {
            "root": str(brain_root),
            "remote": "origin",
            "branch": None,
            "push_interval_seconds": 300,
        },
        "projects": {},
        "models": model_defaults,
        "policy": {
            "automatic_capacity": 4,
            "default_project_capacity": 4,
            "project_capacity": {},
            "paused_projects": [],
            "suspended_rules": {},
            "rationale": "Initial human-approved installation defaults.",
        },
        "knowledge": {
            "root": str(brain_root),
            "remote": "origin",
            "branch": None,
            "require_remote_sync": True,
        },
        "source_watch_root": None,
        "timing": {
            "intake_busy_seconds": 2,
            "intake_idle_seconds": 10,
            "reconcile_seconds": 15,
            "external_timeout_seconds": 30,
            "event_silence_seconds": 120,
            "checkpoint_seconds": 600,
            "stalled_seconds": 1800,
            "marshal_coalesce_seconds": 2,
            "archive_idle_seconds": 600,
        },
        "diagnostics": {
            "retention_days": 14,
            "max_bytes": 1073741824,
            "capture_bytes_per_stream": 262144,
        },
        "resources": {
            "fd_soft_limit": 4096,
            "pause_ratio": 0.85,
            "resume_ratio": 0.70,
        },
    }


class ConfigurationManager:
    def __init__(
        self,
        path: Path,
        *,
        before_replace: Callable[[Path], None] | None = None,
    ) -> None:
        self.path: Path = path.resolve(strict=False)
        self.before_replace = before_replace

    @staticmethod
    def yaml() -> Any:
        parser = YAML(typ="rt")
        parser.allow_duplicate_keys = False
        parser.preserve_quotes = True
        parser.default_flow_style = False
        return parser

    def load(self) -> tuple[MutableMapping[str, Any], bytes]:
        try:
            raw = self.path.read_bytes()
            loaded = self.yaml().load(raw.decode("utf-8"))
        except FileNotFoundError as error:
            raise FulcrumError(
                "CONFIG_NOT_FOUND",
                f"authoritative configuration does not exist: {self.path}",
                exit_code=4,
            ) from error
        except Exception as error:
            raise FulcrumError(
                "CONFIG_INVALID",
                f"cannot parse authoritative configuration {self.path}: {error}",
                exit_code=4,
            ) from error
        if not isinstance(loaded, MutableMapping):
            raise FulcrumError(
                "CONFIG_INVALID",
                "authoritative configuration must be a YAML mapping",
                exit_code=4,
            )
        self.validate_document(loaded)
        return loaded, raw

    def effective(self, document: Mapping[str, Any]) -> dict[str, Any]:
        brain = document.get("brain")
        root_value = brain.get("root") if isinstance(brain, Mapping) else None
        brain_root = (
            _absolute(root_value, "brain.root")
            if isinstance(root_value, str)
            else self.path.parent.resolve(strict=False)
        )
        effective = default_config(brain_root)
        _merge(effective, _plain(document))
        return effective

    def validate_document(self, document: Mapping[str, Any]) -> None:
        unknown = sorted(set(document).difference(TOP_LEVEL))
        if unknown:
            raise _unknown(unknown)
        effective = self.effective(document)
        brain = _mapping(effective["brain"], "brain")
        brain_root = _absolute(brain["root"], "brain.root")
        if brain_root != self.path.parent.resolve(strict=False):
            raise FulcrumError.invalid(
                "CONFIG_ROOT_MISMATCH",
                "brain.root must match the authoritative configuration's parent",
                details={
                    "brain.root": str(brain_root),
                    "config_parent": str(self.path.parent.resolve(strict=False)),
                },
            )
        _known(brain, {"root", "remote", "branch", "push_interval_seconds"}, "brain")
        _positive_int(brain["push_interval_seconds"], "brain.push_interval_seconds")

        runtime = _mapping(effective["runtime"], "runtime")
        _known(runtime, {"kind", "endpoint", "executable"}, "runtime")
        if runtime["kind"] != "codex":
            raise _invalid("runtime.kind", "must be codex")
        _nonempty(runtime["endpoint"], "runtime.endpoint")
        _optional_absolute(runtime["executable"], "runtime.executable")

        delivery = _mapping(effective["delivery"], "delivery")
        _known(delivery, {"kind", "executable"}, "delivery")
        if delivery["kind"] != "tollgate":
            raise _invalid("delivery.kind", "must be tollgate")
        _optional_absolute(delivery.get("executable"), "delivery.executable")

        beads = _mapping(effective["beads"], "beads")
        _known(beads, {"executable", "host", "port", "database"}, "beads")
        _optional_absolute(beads["executable"], "beads.executable")
        if beads["host"] not in {"127.0.0.1", "localhost", "::1"}:
            raise _invalid("beads.host", "must be loopback")
        if (
            not isinstance(beads["port"], int)
            or isinstance(beads["port"], bool)
            or not 1 <= beads["port"] <= 65535
        ):
            raise _invalid("beads.port", "must be an integer from 1 through 65535")
        _nonempty(beads["database"], "beads.database")

        models = _mapping(effective["models"], "models")
        _known(models, set(ROLES), "models")
        for role in ROLES:
            selection = _mapping(models[role], f"models.{role}")
            _known(selection, {"model", "effort"}, f"models.{role}")
            _nonempty(selection["model"], f"models.{role}.model")
            if selection["effort"] not in EFFORTS:
                raise _invalid(
                    f"models.{role}.effort", f"must be one of {sorted(EFFORTS)}"
                )

        policy = _mapping(effective["policy"], "policy")
        _known(
            policy,
            {
                "automatic_capacity",
                "default_project_capacity",
                "project_capacity",
                "paused_projects",
                "suspended_rules",
                "rationale",
            },
            "policy",
        )
        _positive_int(policy["automatic_capacity"], "policy.automatic_capacity")
        _positive_int(
            policy["default_project_capacity"], "policy.default_project_capacity"
        )
        capacities = _mapping(policy["project_capacity"], "policy.project_capacity")
        for project_id, value in capacities.items():
            _positive_int(value, f"policy.project_capacity.{project_id}")
        _string_list(policy["paused_projects"], "policy.paused_projects")
        _mapping(policy["suspended_rules"], "policy.suspended_rules")
        _nonempty(policy["rationale"], "policy.rationale")

        projects = _mapping(effective["projects"], "projects")
        for project_id, value in projects.items():
            _validate_project(
                str(project_id), _mapping(value, f"projects.{project_id}")
            )

        knowledge = _mapping(effective["knowledge"], "knowledge")
        _known(
            knowledge,
            {"root", "remote", "branch", "require_remote_sync"},
            "knowledge",
        )
        _absolute(knowledge["root"], "knowledge.root")
        if not isinstance(knowledge["require_remote_sync"], bool):
            raise _invalid("knowledge.require_remote_sync", "must be boolean")
        source_watch = effective["source_watch_root"]
        if source_watch is not None:
            _absolute(source_watch, "source_watch_root")

        timing = _mapping(effective["timing"], "timing")
        _known(timing, set(default_config(brain_root)["timing"]), "timing")
        for key, value in timing.items():
            _positive_number(value, f"timing.{key}")
        diagnostics = _mapping(effective["diagnostics"], "diagnostics")
        _known(
            diagnostics, set(default_config(brain_root)["diagnostics"]), "diagnostics"
        )
        for key, value in diagnostics.items():
            _positive_int(value, f"diagnostics.{key}")
        resources = _mapping(effective["resources"], "resources")
        _known(resources, {"fd_soft_limit", "pause_ratio", "resume_ratio"}, "resources")
        _positive_int(resources["fd_soft_limit"], "resources.fd_soft_limit")
        for key in ("pause_ratio", "resume_ratio"):
            if (
                not isinstance(resources[key], (int, float))
                or isinstance(resources[key], bool)
                or not 0 < resources[key] < 1
            ):
                raise _invalid(
                    f"resources.{key}", "must be strictly between zero and one"
                )
        if resources["resume_ratio"] >= resources["pause_ratio"]:
            raise _invalid(
                "resources.resume_ratio", "must be below resources.pause_ratio"
            )

    def prepare_patch(
        self, patch: Mapping[str, Any], *, prefix: str | None = None
    ) -> tuple[MutableMapping[str, Any], bytes, list[str], dict[str, Any]]:
        document, original = self.load()
        before = self.effective(document)
        target: MutableMapping[str, Any] = document
        if prefix is not None:
            child = document.get(prefix)
            if child is None:
                child = {}
                document[prefix] = child
            if not isinstance(child, MutableMapping):
                raise _invalid(prefix, "must be a mapping")
            target = child
        _merge(target, patch)
        self.validate_document(document)
        after = self.effective(document)
        return document, original, _changed_paths(before, after), after

    def replace(self, document: MutableMapping[str, Any], expected: bytes) -> bytes:
        stream = io.StringIO()
        self.yaml().dump(document, stream)
        rendered = stream.getvalue().encode("utf-8")
        if self.before_replace is not None:
            self.before_replace(self.path)
        try:
            current = self.path.read_bytes()
        except OSError as error:
            raise FulcrumError(
                "CONFIG_CONFLICT",
                f"cannot re-read configuration before replacement: {error}",
                exit_code=5,
            ) from error
        if current != expected:
            raise FulcrumError(
                "CONFIG_CONFLICT",
                "configuration changed after it was read; the external edit was preserved",
                exit_code=5,
                details={"path": str(self.path)},
            )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(rendered)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
        return rendered


class ConfigurationService:
    def show(self, request: ParsedRequest) -> CommandResult:
        manager = ConfigurationManager(request.instance.config_path)
        document, _ = manager.load()
        return CommandResult.query(
            {
                "path": str(manager.path),
                "config": _redact(manager.effective(document)),
                "authority": ["human", "current_vizier"],
            }
        )

    def validate(self, request: ParsedRequest) -> CommandResult:
        manager = ConfigurationManager(request.instance.config_path)
        document, _ = manager.load()
        effective = manager.effective(document)
        return CommandResult.query(
            {
                "path": str(manager.path),
                "valid": True,
                "config": _redact(effective),
                "capabilities": {
                    "models": "unverified",
                    "reason": "runtime capability adapter is not available yet",
                },
            }
        )

    def set(self, request: ParsedRequest) -> CommandResult:
        return self._mutate(request, request.input)

    def policy_show(self, request: ParsedRequest) -> CommandResult:
        manager = ConfigurationManager(request.instance.config_path)
        document, _ = manager.load()
        return CommandResult.query(
            {
                "policy": manager.effective(document)["policy"],
                "source": str(manager.path),
            }
        )

    def policy_set(self, request: ParsedRequest) -> CommandResult:
        return self._mutate(request, request.input, prefix="policy")

    def _mutate(
        self,
        request: ParsedRequest,
        patch: Mapping[str, Any],
        *,
        prefix: str | None = None,
    ) -> CommandResult:
        manager = ConfigurationManager(request.instance.config_path)
        current_document, _ = manager.load()
        current_effective = manager.effective(current_document)
        document, original, changed, effective = manager.prepare_patch(
            patch, prefix=prefix
        )
        ledger: Ledger | None = None
        operation: OperationRecord | None = None
        try:
            ledger = _authorized_ledger(request, current_effective)
            _require_config_authority(request, ledger)
            operation, reused = ledger.create_operation(
                request,
                planned={"config_path": str(manager.path), "changed_fields": changed},
                next_action="Atomically replace the validated authoritative YAML.",
            )
            if reused and operation.operation.get("state") in {
                "completed",
                "failed",
                "uncertain",
                "cancelled",
            }:
                return _operation_command_result(operation)
        except LedgerFailure as error:
            if not (request.offline and request.actor.kind == "human"):
                raise _ledger_public_error(error, request)
            manager.replace(document, original)
            return CommandResult(
                ok=True,
                state=CommandState.DEGRADED,
                request_id=request.request_id,
                result={
                    "path": str(manager.path),
                    "changed_fields": changed,
                    "restart_required": _restart_required(changed),
                    "receipt_persisted": False,
                    "degraded_reason": str(error),
                },
                warnings=(
                    "Beads was unavailable; the explicit offline human repair has no operation receipt.",
                ),
            )
        manager.replace(document, original)
        assert ledger is not None and operation is not None
        operation = ledger.update_operation(
            operation,
            state="completed",
            step="config_replaced",
            result={
                "path": str(manager.path),
                "changed_fields": changed,
                "restart_required": _restart_required(changed),
                "publication": {"state": "pending", "capability": "not_implemented"},
            },
            next_action="Publish the selected configuration path when the publication adapter is available.",
        )
        return _operation_command_result(operation)


class ProjectService:
    def list(self, request: ParsedRequest) -> CommandResult:
        effective = _effective(request)
        projects = effective["projects"]
        return CommandResult.query(
            {
                "items": [
                    {"id": key, **value, "provider_observation": "unverified"}
                    for key, value in projects.items()
                ],
                "next_cursor": None,
            }
        )

    def show(self, request: ParsedRequest) -> CommandResult:
        effective = _effective(request)
        project_id = str(request.arguments["id"])
        project = effective["projects"].get(project_id)
        if project is None:
            raise FulcrumError.invalid("NOT_FOUND", f"unknown project {project_id}")
        return CommandResult.query(
            {"id": project_id, **project, "provider_observation": "unverified"}
        )

    def add(self, request: ParsedRequest) -> CommandResult:
        project = dict(request.input)
        project_id = project.pop("id", None)
        if not isinstance(project_id, str) or not project_id:
            raise _invalid("id", "is required")
        effective = _effective(request)
        if project_id in effective["projects"]:
            raise FulcrumError.invalid(
                "PROJECT_EXISTS", f"project {project_id} is already enrolled"
            )
        project.setdefault("enabled", True)
        project.setdefault("prepare_argv", [])
        project.setdefault("validate_argv", [])
        project.setdefault("source_remote", None)
        project.setdefault("require_source_sync", False)
        project.setdefault("models", {})
        _validate_project(project_id, project)
        manager = ConfigurationManager(request.instance.config_path)
        document, original = manager.load()
        ledger = _authorized_ledger(request, effective)
        _require_config_authority(request, ledger)
        operation, reused = ledger.create_operation(
            request,
            planned={
                "project_id": project_id,
                "root": project["root"],
                "delivery": project.get("delivery"),
            },
            next_action="Enroll the exact project root with the shared Beads backend.",
        )
        if reused and operation.operation.get("state") in {
            "completed",
            "failed",
            "uncertain",
            "cancelled",
        }:
            return _operation_command_result(operation)
        if not project.get("codex_project_id"):
            from fulcrum.runtime_service import _runtime_call

            native_project = _runtime_call(
                request,
                lambda runtime: runtime.ensure_project(
                    name=project_id,
                    root=str(project["root"]),
                    operation_id=operation.id,
                ),
            )
            project["codex_project_id"] = str(native_project["id"])
            operation = ledger.update_operation(
                operation,
                step="codex_project_verified",
                external={
                    "adapter": "codex",
                    "project_id": project["codex_project_id"],
                },
                result={"codex_project": native_project},
                next_action="Enroll the project in the shared Beads backend.",
            )
        if effective["delivery"].get("kind") == "tollgate":
            try:
                delivery, registration = _ensure_tollgate_repository(
                    project_id, project, effective, timeout=request.timeout
                )
            except FulcrumError as error:
                state = (
                    "uncertain" if error.state == CommandState.UNCERTAIN else "failed"
                )
                operation = ledger.update_operation(
                    operation,
                    state=state,
                    step="delivery_repository_unresolved",
                    error={
                        "code": error.code,
                        "message": str(error),
                        "retryable": error.retryable,
                        "details": error.details,
                    },
                    result={"project_id": project_id, "root": project["root"]},
                    next_action="Inspect the exact Tollgate repository registration before retrying enrollment.",
                )
                return _operation_command_result(operation)
            project["delivery"] = delivery
            operation = ledger.update_operation(
                operation,
                step="delivery_repository_verified",
                external={
                    **dict(operation.operation.get("external") or {}),
                    "delivery_provider": "tollgate",
                    "repository_id": delivery["id"],
                },
                result={"delivery_registration": registration},
                next_action="Enroll the project in the shared Beads backend.",
            )
        _enroll_project_beads(project_id, project, effective, ledger.executable)
        projects = document.get("projects")
        if projects is None:
            projects = {}
            document["projects"] = projects
        if not isinstance(projects, MutableMapping):
            raise _invalid("projects", "must be a mapping")
        projects[project_id] = project
        manager.validate_document(document)
        manager.replace(document, original)
        operation = ledger.update_operation(
            operation,
            state="completed",
            step="project_enrolled",
            result={
                "project_id": project_id,
                "project": project,
                "publication": {"state": "pending"},
            },
            next_action="No further action is required for local enrollment.",
        )
        return _operation_command_result(operation)

    def enable(self, request: ParsedRequest) -> CommandResult:
        return self._set_enabled(request, True)

    def disable(self, request: ParsedRequest) -> CommandResult:
        return self._set_enabled(request, False)

    def _set_enabled(self, request: ParsedRequest, enabled: bool) -> CommandResult:
        project_id = str(request.arguments["id"])
        manager = ConfigurationManager(request.instance.config_path)
        document, original = manager.load()
        effective = manager.effective(document)
        projects = document.get("projects")
        if not isinstance(projects, MutableMapping) or project_id not in projects:
            raise FulcrumError.invalid("NOT_FOUND", f"unknown project {project_id}")
        ledger = _authorized_ledger(request, effective)
        _require_config_authority(request, ledger)
        operation, reused = ledger.create_operation(
            request,
            planned={"project_id": project_id, "enabled": enabled},
            next_action="Update the enrolled project configuration.",
        )
        if reused and operation.operation.get("state") in {
            "completed",
            "failed",
            "uncertain",
            "cancelled",
        }:
            return _operation_command_result(operation)
        project = projects[project_id]
        if not isinstance(project, MutableMapping):
            raise _invalid(f"projects.{project_id}", "must be a mapping")
        project["enabled"] = enabled
        if not enabled and request.arguments.get("reason"):
            project["disabled_reason"] = request.arguments["reason"]
        manager.validate_document(document)
        manager.replace(document, original)
        operation = ledger.update_operation(
            operation,
            state="completed",
            step="project_configuration_updated",
            result={"project_id": project_id, "enabled": enabled},
            next_action="No further action is required.",
        )
        return _operation_command_result(operation)

    def remove(self, request: ParsedRequest) -> CommandResult:
        project_id = str(request.arguments["id"])
        manager = ConfigurationManager(request.instance.config_path)
        document, original = manager.load()
        effective = manager.effective(document)
        projects = document.get("projects")
        if not isinstance(projects, MutableMapping) or project_id not in projects:
            raise FulcrumError.invalid("NOT_FOUND", f"unknown project {project_id}")
        ledger = _authorized_ledger(request, effective)
        _require_config_authority(request, ledger)
        open_work: list[str] = []
        for record in ledger.list_records(limit=0):
            if record.status == "closed" or record.kind in {
                "control",
                "task",
                "memory",
                "analytics",
                "operation",
            }:
                continue
            fc_project = record.fc.get("project") if record.fc else None
            native_project = record.native.get("created_by") == f"project:{project_id}"
            labelled_project = f"project:{project_id}" in record.labels
            if fc_project == project_id or native_project or labelled_project:
                open_work.append(record.id)
        if open_work:
            raise FulcrumError(
                "PROJECT_IN_USE",
                f"project {project_id} still has open work",
                exit_code=5,
                details={"work": open_work},
            )
        project = projects[project_id]
        if not isinstance(project, Mapping):
            raise _invalid(f"projects.{project_id}", "must be a mapping")
        remove_registrations = bool(
            request.arguments.get("remove_owned_registrations", False)
        )
        add_operation = _project_add_operation(ledger, project_id)
        delivery = project.get("delivery")
        planned_registrations = {
            "runtime_project": {
                "id": project.get("codex_project_id"),
                "ownership_operation": add_operation,
            },
            "delivery_repository": {
                "id": delivery.get("id") if isinstance(delivery, Mapping) else None,
                "ownership": (
                    delivery.get("registration")
                    if isinstance(delivery, Mapping)
                    else None
                ),
            },
        }
        operation, reused = ledger.create_operation(
            request,
            planned={
                "project_id": project_id,
                "remove_owned_registrations": remove_registrations,
                "registrations": planned_registrations,
            },
            next_action=(
                "Remove exact owned provider registrations, then authoritative YAML."
                if remove_registrations
                else "Remove the settled project from authoritative YAML."
            ),
        )
        if reused and operation.operation.get("state") in {
            "completed",
            "failed",
            "uncertain",
            "cancelled",
        }:
            return _operation_command_result(operation)
        provider_removals: list[dict[str, Any]] = []
        if remove_registrations:
            try:
                runtime_id = project.get("codex_project_id")
                if isinstance(runtime_id, str) and runtime_id and add_operation:
                    from fulcrum.runtime_service import _runtime_call

                    runtime_result = _runtime_call(
                        request,
                        lambda runtime: _remove_created_runtime_project(
                            runtime,
                            str(project["root"]),
                            runtime_id,
                            add_operation,
                        ),
                    )
                    provider_removals.append({"runtime_project": runtime_result})
                    operation = ledger.update_operation(
                        operation,
                        step="runtime_project_removal_observed",
                        result={"provider_removals": provider_removals},
                        next_action="Remove any exact owned delivery registration.",
                    )
                if (
                    isinstance(delivery, Mapping)
                    and delivery.get("registration") == "created"
                    and isinstance(delivery.get("id"), str)
                ):
                    tollgate = Tollgate(
                        effective["delivery"].get("executable"),
                        timeout=max(60, int(request.timeout)),
                    )
                    removed = tollgate.remove_repository(str(delivery["id"]))
                    provider_removals.append(
                        {"delivery_repository": removed or str(delivery["id"])}
                    )
                    operation = ledger.update_operation(
                        operation,
                        step="delivery_repository_removal_observed",
                        result={"provider_removals": provider_removals},
                        next_action="Remove the authoritative YAML enrollment.",
                    )
            except (FulcrumError, TollgateError) as error:
                operation = ledger.update_operation(
                    operation,
                    state=(
                        "uncertain"
                        if isinstance(error, TollgateUncertainError)
                        or (
                            isinstance(error, FulcrumError)
                            and error.state == CommandState.UNCERTAIN
                        )
                        else "failed"
                    ),
                    step="provider_registration_removal_failed",
                    error={
                        "code": getattr(error, "code", "PROVIDER_REMOVAL_FAILED"),
                        "message": str(error),
                        "retryable": getattr(error, "retryable", False),
                    },
                    result={"provider_removals": provider_removals},
                    next_action="Inspect the exact provider IDs before retrying removal.",
                )
                return _operation_command_result(operation)
        del projects[project_id]
        manager.validate_document(document)
        manager.replace(document, original)
        operation = ledger.update_operation(
            operation,
            state="completed",
            step="project_removed",
            result={
                "project_id": project_id,
                "owned_provider_registrations_removed": remove_registrations,
                "provider_removals": provider_removals,
            },
            next_action="No further action is required.",
        )
        return _operation_command_result(operation)


def _effective(request: ParsedRequest) -> dict[str, Any]:
    manager = ConfigurationManager(request.instance.config_path)
    document, _ = manager.load()
    return manager.effective(document)


def _authorized_ledger(request: ParsedRequest, effective: Mapping[str, Any]) -> Ledger:
    if request.instance.brain_root is None:
        raise FulcrumError(
            "LEDGER_UNAVAILABLE", "configuration has no brain root", exit_code=4
        )
    beads = _mapping(effective["beads"], "beads")
    executable = beads.get("executable")
    return Ledger(
        request.instance.brain_root,
        executable=str(executable) if executable else None,
        timeout=request.timeout,
    )


def _require_config_authority(request: ParsedRequest, ledger: Ledger) -> None:
    if request.actor.kind == "human":
        return
    if request.actor.kind == "task" and request.actor.task_id:
        control = ledger.show("fc-system")
        fc = control.fc if control else None
        if (
            fc
            and fc.get("kind") == "control"
            and fc.get("vizier_thread") == request.actor.task_id
        ):
            return
    raise FulcrumError(
        "CONFIG_AUTHORITY_DENIED",
        "only the human or verifiable current Vizier may modify authoritative YAML",
        exit_code=5,
        request_id=request.request_id,
        details={"actor": request.actor.to_dict()},
    )


def _enroll_project_beads(
    project_id: str,
    project: Mapping[str, Any],
    effective: Mapping[str, Any],
    executable: str,
) -> None:
    root = _absolute(project["root"], f"projects.{project_id}.root")
    if not root.is_dir():
        raise FulcrumError.invalid(
            "PROJECT_ROOT_MISSING", f"project root does not exist: {root}"
        )
    beads_dir = root / ".beads"
    if not beads_dir.exists():
        backend = _mapping(effective["beads"], "beads")
        command = [
            executable,
            "init",
            "--server",
            "--external",
            "--server-host",
            str(backend["host"]),
            "--server-port",
            str(backend["port"]),
            "--database",
            str(backend["database"]),
            "--prefix",
            "fc",
            "--non-interactive",
            "--skip-agents",
            "--skip-hooks",
        ]
        completed = subprocess.run(
            command, cwd=root, capture_output=True, text=True, check=False, timeout=30
        )
        if completed.returncode != 0:
            raise FulcrumError(
                "PROJECT_ENROLLMENT_FAILED",
                completed.stderr.strip()
                or completed.stdout.strip()
                or "Beads project initialization failed",
                exit_code=4,
                retryable=True,
            )
    for key, value in (
        ("actor", f"project:{project_id}"),
        ("export.auto", "false"),
        ("export.git-add", "false"),
    ):
        completed = subprocess.run(
            [executable, "--json", "-C", str(root), "config", "set", key, value],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if completed.returncode != 0:
            raise FulcrumError(
                "PROJECT_ENROLLMENT_FAILED",
                completed.stderr.strip() or f"could not set project Beads {key}",
                exit_code=4,
            )
    exclude = root / ".git" / "info" / "exclude"
    if exclude.parent.is_dir():
        current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if ".beads/" not in {line.strip() for line in current.splitlines()}:
            with exclude.open("a", encoding="utf-8") as handle:
                if current and not current.endswith("\n"):
                    handle.write("\n")
                handle.write(".beads/\n")


def _ensure_tollgate_repository(
    project_id: str,
    project: Mapping[str, Any],
    effective: Mapping[str, Any],
    *,
    timeout: float = 60,
) -> tuple[dict[str, Any], dict[str, Any]]:
    root = _absolute(project["root"], f"projects.{project_id}.root")
    delivery_config = _mapping(effective["delivery"], "delivery")
    try:
        tollgate = Tollgate(
            delivery_config.get("executable"), timeout=max(60, int(timeout))
        )
        repositories = tollgate.repositories()
    except TollgateError as error:
        raise FulcrumError(
            "DELIVERY_UNAVAILABLE",
            str(error),
            exit_code=4,
            retryable=error.category in {"transient", "unavailable"},
            details={"category": error.category},
        ) from error
    requested = project.get("delivery")
    requested_id = requested.get("id") if isinstance(requested, Mapping) else None
    exact_path = [
        row
        for row in repositories
        if _tollgate_repository_path(row) == root.resolve(strict=True)
    ]
    if requested_id:
        matches = [
            row for row in repositories if _tollgate_repository_id(row) == requested_id
        ]
        if len(matches) != 1 or _tollgate_repository_path(matches[0]) != root:
            raise FulcrumError(
                "PROJECT_PROVIDER_MISMATCH",
                "configured Tollgate repository does not identify the exact project root",
                exit_code=5,
                details={"repository_id": requested_id, "root": str(root)},
            )
        delivery = dict(requested)
        delivery["id"] = str(requested_id)
        delivery.setdefault("registration", "supplied")
        return delivery, {
            "state": "verified",
            "ownership": delivery["registration"],
            "repository": matches[0],
        }
    if len(exact_path) > 1:
        raise FulcrumError(
            "DELIVERY_UNCERTAIN",
            "multiple Tollgate repositories identify the exact project root",
            exit_code=4,
            state=CommandState.UNCERTAIN,
            details={
                "root": str(root),
                "repository_ids": [_tollgate_repository_id(row) for row in exact_path],
            },
        )
    if exact_path:
        repository_id = _tollgate_repository_id(exact_path[0])
        assert repository_id is not None
        return {
            "id": repository_id,
            "registration": "discovered",
        }, {
            "state": "verified",
            "ownership": "discovered",
            "repository": exact_path[0],
        }
    try:
        created = tollgate.add_repository(root)
    except TollgateError as error:
        try:
            recovered = [
                row
                for row in tollgate.repositories()
                if _tollgate_repository_path(row) == root
            ]
        except TollgateError:
            recovered = []
        if len(recovered) != 1:
            raise FulcrumError(
                "DELIVERY_UNCERTAIN",
                "Tollgate repository creation response was lost and exact recovery is inconclusive",
                exit_code=4,
                retryable=isinstance(error, TollgateUncertainError),
                state=CommandState.UNCERTAIN,
                details={
                    "root": str(root),
                    "matches": len(recovered),
                    "provider_error": str(error),
                },
            ) from error
        created = recovered[0]
    repository_id = _tollgate_repository_id(created)
    if repository_id is None:
        recovered = [
            row
            for row in tollgate.repositories()
            if _tollgate_repository_path(row) == root
        ]
        if len(recovered) != 1:
            raise FulcrumError(
                "DELIVERY_UNCERTAIN",
                "Tollgate repository creation returned no verifiable repository ID",
                exit_code=4,
                state=CommandState.UNCERTAIN,
                details={"root": str(root), "response": created},
            )
        created = recovered[0]
        repository_id = _tollgate_repository_id(created)
    assert repository_id is not None
    return {"id": repository_id, "registration": "created"}, {
        "state": "created",
        "ownership": "created",
        "repository": created,
    }


def _project_add_operation(ledger: Ledger, project_id: str) -> str | None:
    matches: list[OperationRecord] = []
    for record in ledger.list_records(kind="operation", limit=0):
        operation = OperationRecord.from_record(record)
        if operation.operation.get("command") != "project.add":
            continue
        planned = operation.operation.get("planned")
        if isinstance(planned, Mapping) and planned.get("project_id") == project_id:
            matches.append(operation)
    if not matches:
        return None
    matches.sort(
        key=lambda item: (
            str(item.native.get("updated_at") or item.native.get("updatedAt") or ""),
            item.id,
        )
    )
    return matches[-1].id


async def _remove_created_runtime_project(
    runtime: Any,
    root: str,
    project_id: str,
    creation_operation: str,
) -> dict[str, Any]:
    matches = await runtime.find_projects(root)
    exact = [
        project
        for project in matches
        if str(project.get("id") or project.get("projectId") or "") == project_id
    ]
    if not exact:
        return {
            "id": project_id,
            "exists": False,
            "deleted": False,
            "ownership": "previously_removed",
        }
    if len(exact) != 1:
        raise FulcrumError(
            "PROJECT_PROVIDER_UNCERTAIN",
            "multiple native projects match the exact configured provider ID",
            exit_code=4,
            state=CommandState.UNCERTAIN,
            details={"project_id": project_id, "matches": len(exact)},
        )
    metadata = exact[0].get("metadata")
    if (
        not isinstance(metadata, Mapping)
        or metadata.get("fulcrum_operation") != creation_operation
    ):
        return {
            "id": project_id,
            "exists": True,
            "deleted": False,
            "ownership": "discovered",
        }
    removed = await runtime.delete_project(project_id)
    return {**dict(removed), "ownership": "created"}


def _tollgate_repository_state(value: Mapping[str, Any]) -> Mapping[str, Any]:
    state = value.get("state")
    return state if isinstance(state, Mapping) else value


def _tollgate_repository_id(value: Mapping[str, Any]) -> str | None:
    identifier = _tollgate_repository_state(value).get("id")
    return str(identifier) if isinstance(identifier, str) and identifier else None


def _tollgate_repository_path(value: Mapping[str, Any]) -> Path | None:
    path = _tollgate_repository_state(value).get("path")
    return (
        Path(path).resolve(strict=False)
        if isinstance(path, str) and Path(path).is_absolute()
        else None
    )


def _validate_project(project_id: str, project: Mapping[str, Any]) -> None:
    _known(
        project,
        {
            "root",
            "codex_project_id",
            "delivery",
            "integration_branch",
            "prepare_argv",
            "validate_argv",
            "source_remote",
            "require_source_sync",
            "models",
            "enabled",
            "disabled_reason",
        },
        f"projects.{project_id}",
    )
    _absolute(project.get("root"), f"projects.{project_id}.root")
    if "enabled" in project and not isinstance(project["enabled"], bool):
        raise _invalid(f"projects.{project_id}.enabled", "must be boolean")
    for field in ("prepare_argv", "validate_argv"):
        if field in project:
            _string_list(project[field], f"projects.{project_id}.{field}")
    if project.get("require_source_sync") and not project.get("source_remote"):
        raise _invalid(
            f"projects.{project_id}.source_remote",
            "is required when source synchronization is required",
        )
    if "delivery" in project:
        delivery = _mapping(project["delivery"], f"projects.{project_id}.delivery")
        _known(
            delivery,
            {"id", "registration"},
            f"projects.{project_id}.delivery",
        )
        if "id" in delivery and (
            not isinstance(delivery["id"], str) or not delivery["id"]
        ):
            raise _invalid(f"projects.{project_id}.delivery.id", "must be a string")
        if delivery.get("registration") not in {
            None,
            "supplied",
            "discovered",
            "created",
        }:
            raise _invalid(
                f"projects.{project_id}.delivery.registration",
                "must be supplied, discovered, or created",
            )
    models = project.get("models", {})
    if not isinstance(models, Mapping):
        raise _invalid(f"projects.{project_id}.models", "must be a mapping")
    _known(models, set(ROLES), f"projects.{project_id}.models")
    for role, selection in models.items():
        selected = _mapping(selection, f"projects.{project_id}.models.{role}")
        _known(selected, {"model", "effort"}, f"projects.{project_id}.models.{role}")


def _operation_command_result(operation: OperationRecord) -> CommandResult:
    state_value = str(operation.operation.get("state", "running"))
    state = (
        CommandState(state_value)
        if state_value in CommandState._value2member_map_
        else CommandState.RUNNING
    )
    return CommandResult(
        ok=state not in {CommandState.FAILED, CommandState.UNCERTAIN},
        state=state,
        operation_id=operation.id,
        request_id=str(operation.operation.get("request_id")),
        result=operation_view(operation),
    )


def _ledger_public_error(error: LedgerFailure, request: ParsedRequest) -> FulcrumError:
    return FulcrumError(
        "LEDGER_UNCERTAIN" if error.uncertain else "LEDGER_UNAVAILABLE",
        str(error),
        exit_code=4,
        retryable=error.retryable,
        state=CommandState.UNCERTAIN if error.uncertain else CommandState.FAILED,
        request_id=request.request_id,
        details={"category": error.category},
    )


def _restart_required(changed: Sequence[str]) -> bool:
    return any(
        any(
            path == prefix or path.startswith(prefix + ".") for prefix in RESTART_FIELDS
        )
        for path in changed
    )


def _merge(target: MutableMapping[str, Any], patch: Mapping[str, Any]) -> None:
    for key, value in patch.items():
        existing = target.get(key)
        if isinstance(existing, MutableMapping) and isinstance(value, Mapping):
            _merge(existing, value)
        elif isinstance(value, Mapping):
            child: MutableMapping[str, Any] = {}
            _merge(child, value)
            target[key] = child
        else:
            target[key] = copy.deepcopy(value)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_plain(child) for child in value]
    return value


def _changed_paths(before: Any, after: Any, prefix: str = "") -> list[str]:
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        paths: list[str] = []
        for key in sorted(set(before).union(after)):
            child = f"{prefix}.{key}" if prefix else str(key)
            if key not in before or key not in after:
                paths.append(child)
            else:
                paths.extend(_changed_paths(before[key], after[key], child))
        return paths
    return [] if before == after else [prefix]


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): (
                "<redacted>"
                if any(
                    marker in str(key).lower()
                    for marker in ("password", "token", "secret", "credential")
                )
                else _redact(child)
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact(child) for child in value]
    return value


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _invalid(field, "must be a mapping")
    return value


def _known(value: Mapping[str, Any], allowed: set[str], prefix: str) -> None:
    unknown = sorted(str(key) for key in set(value).difference(allowed))
    if unknown:
        raise _unknown([f"{prefix}.{key}" for key in unknown])


def _unknown(fields: Sequence[str]) -> FulcrumError:
    return FulcrumError.invalid(
        "UNKNOWN_FIELDS",
        "configuration contains unknown fields",
        details={"fields": list(fields)},
    )


def _invalid(field: str, reason: str) -> FulcrumError:
    return FulcrumError.invalid(
        "CONFIG_INVALID", f"{field} {reason}", details={"field": field}
    )


def _absolute(value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise _invalid(field, "must be an absolute path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise _invalid(field, "must be an absolute path")
    return path.resolve(strict=False)


def _optional_absolute(value: Any, field: str) -> None:
    if value is not None:
        _absolute(value, field)


def _nonempty(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value:
        raise _invalid(field, "must be a nonempty string")


def _positive_int(value: Any, field: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise _invalid(field, "must be a positive integer")


def _positive_number(value: Any, field: str) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise _invalid(field, "must be positive")


def _string_list(value: Any, field: str) -> None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _invalid(field, "must be an array of strings")
