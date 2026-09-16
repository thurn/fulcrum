"""Rerunnable Beads-only bootstrap for a Fulcrum2 installation."""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import subprocess
import tempfile
import uuid
import venv
from collections.abc import Mapping, MutableMapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from fulcrum.configuration import ConfigurationManager, default_config
from fulcrum.contracts import (
    CommandResult,
    CommandState,
    FulcrumError,
    InstanceContext,
    ParsedRequest,
)
from fulcrum.install import (
    InstallationError,
    _package_contents,
    fulcrum2_service_definitions,
    install_fulcrum2_service_definitions,
    installation_source_root,
    reconcile_fulcrum2_skills,
)
from fulcrum.installation_service import (
    ServiceService,
    _start_one,
    load_installed_services,
)
from fulcrum.install import inspect_service
from fulcrum.instance import DEFAULT_BRAIN, WriterLock
from fulcrum.leadership import ensure_leadership
from fulcrum.ledger import Ledger, LedgerFailure, operation_view
from fulcrum.runtime import AppServerRuntime
from fulcrum.source_refresh import ensure_recovery_environment
from fulcrum.tollgate import Tollgate, TollgateError
from fulcrum.publication import (
    DoltPublicationAdapter,
    DoltPublicationError,
    _git_transport,
)


def run_setup(request: ParsedRequest) -> CommandResult:
    """Create or repair exactly one selected installation."""

    try:
        return _run_setup(request)
    except InstallationError as error:
        raise FulcrumError(
            "INSTALLATION_FAILED",
            str(error),
            exit_code=4,
            retryable=False,
            request_id=request.request_id,
        ) from error


def _run_setup(request: ParsedRequest) -> CommandResult:

    from fulcrum.reset import refuse_unfinished_reset

    refuse_unfinished_reset(request.instance.instance_root)

    if request.actor.kind != "human":
        raise FulcrumError(
            "CONFIG_AUTHORITY_DENIED",
            "setup requires explicit human authority",
            exit_code=5,
            request_id=request.request_id,
        )
    supplied = dict(request.input)
    target, brain_root = _bootstrap_target(request, supplied)
    instance = InstanceContext(
        instance_root=request.instance.instance_root.resolve(strict=False),
        config_path=target.resolve(strict=False),
        brain_root=brain_root,
        socket_path=request.instance.instance_root / "resident.sock",
        lock_path=brain_root / ".fulcrum-locks" / "maintenance",
        explicit_selection=request.instance.explicit_selection,
    )
    setup_request = ParsedRequest(
        command=("setup",),
        arguments=request.arguments,
        input=supplied,
        actor=request.actor,
        instance=instance,
        request_id=request.request_id,
        timeout=request.timeout,
    )
    created: list[str] = []
    capabilities: dict[str, Any] = {}
    prior_services = load_installed_services(instance.instance_root)
    prior_controller = prior_services.get("controller")
    stopped_for_setup: str | None = None
    if prior_controller is not None and inspect_service(prior_controller.label).running:
        assert setup_request.request_id is not None
        stop_request = replace(
            setup_request,
            command=("service", "stop"),
            input={},
            arguments={},
            request_id=str(
                uuid.uuid5(uuid.UUID(setup_request.request_id), "setup-service-stop")
            ),
        )
        stopped_for_setup = ServiceService().stop(stop_request).operation_id
    lock_path = instance.lock_path
    assert lock_path is not None
    with WriterLock(lock_path):
        config, config_changed = _prepare_configuration(
            target,
            brain_root,
            supplied,
            non_interactive=bool(request.arguments.get("non_interactive", False)),
        )
        _prepare_brain(brain_root, created)
        ordinary_remote = _git_remote_url(brain_root, str(config["brain"]["remote"]))
        _install_discovery_link(instance.instance_root, target)
        controller, controller_changed = _install_controller_environment(
            instance.instance_root
        )
        definitions = fulcrum2_service_definitions(
            instance_root=instance.instance_root,
            config_path=target,
            brain_root=brain_root,
            config=config,
            controller_executable=controller,
            production=not instance.explicit_selection,
        )
        installed_services, changed_services = install_fulcrum2_service_definitions(
            definitions, instance.instance_root
        )
        # The dedicated ledger runtime is a permitted pre-ledger bootstrap
        # primitive. Every later external effect belongs to the setup receipt.
        (brain_root / ".beads" / "dolt").mkdir(parents=True, exist_ok=True, mode=0o700)
        _start_one(
            installed_services["dolt"],
            endpoint=f"tcp://{config['beads']['host']}:{config['beads']['port']}",
        )
        _initialize_beads(
            brain_root, config, setup_request.timeout, ordinary_remote=ordinary_remote
        )
        ledger = Ledger(
            brain_root,
            executable=str(config["beads"]["executable"]),
            timeout=setup_request.timeout,
        )
        assert setup_request.request_id is not None
        operation, reused = ledger.create_operation(
            setup_request,
            planned={
                "instance_root": str(instance.instance_root),
                "brain_root": str(brain_root),
                "config_path": str(target),
            },
            next_action="Install and verify owned assets and standing identities.",
        )
        if reused and operation.operation.get("state") in {
            "completed",
            "failed",
            "cancelled",
            "uncertain",
        }:
            return _setup_operation_result(operation)
        recovery = ensure_recovery_environment(
            instance.instance_root,
            installation_source_root(),
            operation.id,
            config_path=target,
            production=not instance.explicit_selection,
        )
        skills = reconcile_fulcrum2_skills(
            instance.instance_root,
            production=not instance.explicit_selection,
        )
        capabilities["brain_git"] = _brain_git_capability(
            ledger, config, ordinary_remote
        )
        capabilities["beads"] = _beads_capability(ledger, config)
        capabilities["assets"] = _asset_capability(controller)
        capabilities["delivery"] = _delivery_capability(config)
        runtime_capability, leadership = _runtime_and_leadership(
            setup_request, ledger, config, installed_services
        )
        capabilities["runtime"] = runtime_capability
        capabilities["models"] = _model_capability(config, runtime_capability)
        capabilities["projects"] = _project_capability(config, runtime_capability)
        required_failures = [
            name
            for name, value in capabilities.items()
            if isinstance(value, Mapping)
            and value.get("required")
            and not value.get("available")
        ]
        result = {
            "instance": str(instance.instance_root),
            "config": {
                "path": str(target),
                "changed": config_changed,
                "effective": config,
            },
            "assets": {
                "controller": str(controller),
                "controller_changed": controller_changed,
                "recovery": recovery,
                "skills": skills,
            },
            "services": {
                name: {
                    "label": service.label,
                    "definition": str(service.definition),
                    "changed": name in changed_services,
                }
                for name, service in installed_services.items()
            },
            "leadership": leadership,
            "capabilities": capabilities,
            "created": created,
            "service_started": False,
            "stopped_for_setup": stopped_for_setup,
        }
        if required_failures:
            operation = ledger.update_operation(
                operation,
                state="failed",
                step="required_capability_unavailable",
                result=result,
                error={
                    "code": "CAPABILITY_UNAVAILABLE",
                    "message": "required setup capabilities are unavailable: "
                    + ", ".join(required_failures),
                    "retryable": True,
                },
                next_action="Repair the named capabilities and repeat the setup request.",
            )
            return _setup_operation_result(operation)
        operation = ledger.update_operation(
            operation,
            state="accepted",
            step="installation_verified",
            result=result,
            next_action="Start the installed controller service.",
        )
    # The controller must acquire the same writer lock, so launch it only after
    # the bootstrap critical section. No Vizier turn is started by setup.
    assert setup_request.request_id is not None
    service_request = replace(
        setup_request,
        command=("service", "start"),
        input={},
        arguments={},
        timeout=max(120.0, setup_request.timeout),
        request_id=str(
            uuid.uuid5(uuid.UUID(setup_request.request_id), "setup-service-start")
        ),
    )
    try:
        service_result = ServiceService().start(service_request)
    except FulcrumError as error:
        ledger.update_operation(
            operation,
            state="failed",
            step="controller_start_failed",
            error={
                "code": error.code,
                "message": error.message,
                "retryable": error.retryable,
            },
            next_action="Repair the controller start failure and repeat setup.",
        )
        raise
    retained = dict(operation.operation.get("result") or {})
    retained["service_started"] = True
    retained["service_start"] = dict(service_result.result or {})
    operation = ledger.update_operation(
        operation,
        state="completed",
        step="controller_started",
        result=retained,
        next_action="No further action is required.",
    )
    return _setup_operation_result(operation)


def _bootstrap_target(
    request: ParsedRequest, supplied: Mapping[str, Any]
) -> tuple[Path, Path]:
    selected = request.instance.config_path
    if selected.is_file():
        manager = ConfigurationManager(selected)
        document, _ = manager.load()
        effective = manager.effective(document)
        return (
            selected.resolve(strict=True),
            Path(effective["brain"]["root"]).resolve(strict=False),
        )
    brain = supplied.get("brain")
    root = brain.get("root") if isinstance(brain, Mapping) else None
    if isinstance(root, str) and root:
        brain_root = Path(root).expanduser()
        if not brain_root.is_absolute():
            raise FulcrumError.invalid(
                "INVALID_PATH", "brain.root must be an absolute path"
            )
        brain_root = brain_root.resolve(strict=False)
    elif request.instance.explicit_selection:
        raise FulcrumError.invalid(
            "MISSING_SETUP_FIELDS",
            "explicit setup requires brain.root when configuration is missing",
            details={"fields": ["brain.root"]},
        )
    else:
        brain_root = DEFAULT_BRAIN.resolve(strict=False)
    return brain_root / "fulcrum.yaml", brain_root


def _prepare_configuration(
    target: Path,
    brain_root: Path,
    supplied: Mapping[str, Any],
    *,
    non_interactive: bool,
) -> tuple[dict[str, Any], bool]:
    manager = ConfigurationManager(target)
    if target.is_file():
        document, _ = manager.load()
        if not supplied:
            effective = manager.effective(document)
            _resolve_missing_executables(effective)
            return effective, False
        document, _original, changed, effective = manager.prepare_patch(supplied)
        if not changed:
            _resolve_missing_executables(effective)
            return effective, False
        manager.replace(document, _original)
        _resolve_missing_executables(effective)
        return effective, True
    if target.exists() and target.is_dir():
        raise FulcrumError.invalid(
            "CONFIG_PATH_COLLISION", f"configuration path is a directory: {target}"
        )
    config = default_config(brain_root)
    _merge(config, supplied)
    if "source" not in supplied:
        config["source"]["repository"] = str(
            installation_source_root().resolve(strict=True)
        )
    _resolve_missing_executables(config)
    missing = _missing_required(config)
    if missing and not non_interactive:
        _prompt_missing(config, missing)
        missing = _missing_required(config)
    if missing:
        raise FulcrumError.invalid(
            "MISSING_SETUP_FIELDS",
            "setup is missing required values: " + ", ".join(missing),
            details={"fields": missing},
        )
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    manager.validate_document(config)
    stream = io.StringIO()
    manager.yaml().dump(config, stream)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(stream.getvalue())
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return manager.effective(config), True


def _merge(target: MutableMapping[str, Any], patch: Mapping[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, Mapping) and isinstance(target.get(key), MutableMapping):
            _merge(target[key], value)
        else:
            target[key] = value


def _resolve_missing_executables(config: MutableMapping[str, Any]) -> None:
    beads = config["beads"]
    runtime = config["runtime"]
    delivery = config["delivery"]
    if not beads.get("executable"):
        beads["executable"] = shutil.which("bd")
    if runtime.get("kind") == "codex" and not runtime.get("executable"):
        runtime["executable"] = shutil.which("codex")
    if delivery.get("kind") == "tollgate" and not delivery.get("executable"):
        delivery["executable"] = shutil.which("tg")


def _missing_required(config: Mapping[str, Any]) -> list[str]:
    missing: list[str] = []
    for field, value in (
        ("beads.executable", config["beads"].get("executable")),
        ("runtime.endpoint", config["runtime"].get("endpoint")),
    ):
        if not value:
            missing.append(field)
    if config["runtime"].get("kind") == "codex" and not config["runtime"].get(
        "executable"
    ):
        missing.append("runtime.executable")
    if config["delivery"].get("kind") == "tollgate" and not config["delivery"].get(
        "executable"
    ):
        missing.append("delivery.executable")
    return missing


def _prompt_missing(config: MutableMapping[str, Any], fields: list[str]) -> None:
    for field in fields:
        section, name = field.split(".", 1)
        value = input(f"{field}: ").strip()
        if value:
            config[section][name] = value


def _prepare_brain(root: Path, created: list[str]) -> None:
    if root.exists() and not root.is_dir():
        raise FulcrumError.invalid(
            "BRAIN_PATH_COLLISION", f"brain root is not a directory: {root}"
        )
    if not root.exists():
        root.mkdir(parents=True, mode=0o700)
        created.append(str(root))
    if not (root / ".git").is_dir():
        completed = subprocess.run(
            ["git", "init", str(root)], capture_output=True, text=True, check=False
        )
        if completed.returncode != 0:
            raise FulcrumError(
                "GIT_UNAVAILABLE",
                completed.stderr.strip() or "could not initialize brain Git repository",
                exit_code=4,
            )


def _git_remote_url(root: Path, name: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), "remote", "get-url", name],
        capture_output=True,
        text=True,
        check=False,
    )
    value = completed.stdout.strip()
    if completed.returncode != 0 or not value:
        raise FulcrumError(
            "BRAIN_REMOTE_INVALID",
            f"brain Git remote {name!r} is unavailable at {root}",
            exit_code=4,
            details={"remote": name, "brain_root": str(root)},
        )
    return value


def _install_discovery_link(instance_root: Path, target: Path) -> None:
    instance_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    link = instance_root / "config"
    if not (
        link.is_symlink() and link.resolve(strict=False) == target.resolve(strict=False)
    ):
        if link.exists() or link.is_symlink():
            raise FulcrumError.invalid(
                "CONFIG_LINK_CONFLICT",
                f"refusing to replace non-owned or mismatched discovery path {link}",
            )
        temporary = link.with_name(f".{link.name}.{os.getpid()}")
        temporary.symlink_to(target.resolve(strict=False))
        os.replace(temporary, link)
    brain_root = target.parent.resolve(strict=False)
    lock_target = brain_root / ".fulcrum-locks" / "maintenance"
    lock_link = instance_root / "controller.lock"
    if lock_link.is_symlink() and lock_link.resolve(strict=False) == lock_target:
        return
    if lock_link.exists() or lock_link.is_symlink():
        raise FulcrumError.invalid(
            "LOCK_LINK_CONFLICT",
            f"refusing to replace non-owned or mismatched lock discovery path {lock_link}",
        )
    temporary_lock = lock_link.with_name(f".{lock_link.name}.{os.getpid()}")
    temporary_lock.symlink_to(lock_target)
    os.replace(temporary_lock, lock_link)


def _install_controller_environment(instance_root: Path) -> tuple[Path, bool]:
    source = installation_source_root().resolve(strict=True)
    root = instance_root / "runtime"
    current = root / "current"
    source_package = source / "src" / "fulcrum" if (source / "src").is_dir() else source
    if current.is_dir():
        installed = next(current.glob("lib/python*/site-packages/fulcrum"), None)
        executable = current / "bin" / "fulcrum"
        if (
            installed is not None
            and executable.is_file()
            and _package_contents(installed) == _package_contents(source_package)
        ):
            return executable, False
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    deployment = root / f"deployment-{uuid.uuid4().hex}"
    try:
        venv.EnvBuilder(with_pip=True).create(deployment)
        completed = subprocess.run(
            [str(deployment / "bin" / "pip"), "install", str(source)],
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
        if completed.returncode != 0:
            raise InstallationError(
                "controller package installation failed: "
                + (completed.stderr.strip() or completed.stdout.strip())
            )
        executable = deployment / "bin" / "fulcrum"
        probe = subprocess.run(
            [str(executable), "--help"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if probe.returncode != 0:
            raise InstallationError(
                "installed controller probe failed: "
                + (probe.stderr.strip() or probe.stdout.strip())
            )
        temporary = root / f".current.{os.getpid()}"
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(deployment.name, target_is_directory=True)
        os.replace(temporary, current)
        return current / "bin" / "fulcrum", True
    except Exception:
        shutil.rmtree(deployment, ignore_errors=True)
        raise


def _initialize_beads(
    brain_root: Path,
    config: Mapping[str, Any],
    timeout: float,
    *,
    ordinary_remote: str,
) -> None:
    beads = config["beads"]
    executable = str(beads["executable"])
    beads_config = brain_root / ".beads" / "config.yaml"
    if beads_config.is_file():
        status = subprocess.run(
            [executable, "--json", "-C", str(brain_root), "status"],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        if status.returncode == 0:
            return
        raise FulcrumError(
            "BEADS_UNAVAILABLE",
            status.stderr.strip() or "configured Beads ledger is unreadable",
            exit_code=4,
            retryable=True,
        )
    (brain_root / ".beads" / "dolt").mkdir(parents=True, exist_ok=True, mode=0o700)
    command = [
        executable,
        "-C",
        str(brain_root),
        "init",
        "--server",
        "--external",
        "--server-host",
        str(beads["host"]),
        "--server-port",
        str(beads["port"]),
        "--database",
        str(beads["database"]),
        "--prefix",
        "fc",
        "--remote",
        _git_transport(ordinary_remote),
        "--init-if-missing",
        "--non-interactive",
        "--skip-agents",
        "--skip-hooks",
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=max(30.0, timeout),
    )
    if completed.returncode != 0:
        raise FulcrumError(
            "BEADS_INIT_FAILED",
            completed.stderr.strip() or completed.stdout.strip() or "Beads init failed",
            exit_code=4,
            retryable=True,
        )
    verified = subprocess.run(
        [executable, "--json", "-C", str(brain_root), "status"],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if verified.returncode != 0:
        raise FulcrumError(
            "BEADS_UNAVAILABLE",
            verified.stderr.strip() or "initialized Beads ledger is unreadable",
            exit_code=4,
            retryable=True,
        )


def _brain_git_capability(
    ledger: Ledger, config: Mapping[str, Any], ordinary_remote: str
) -> dict[str, Any]:
    name = str(config["brain"]["remote"])
    try:
        remote = DoltPublicationAdapter(
            ledger, remote=name, timeout=ledger.timeout
        ).ensure_remote()
        return {
            "required": True,
            "available": True,
            "ordinary_remote": ordinary_remote,
            "dolt_remote": remote,
        }
    except (DoltPublicationError, LedgerFailure) as error:
        return {
            "required": True,
            "available": False,
            "ordinary_remote": ordinary_remote,
            "error": str(error),
        }


def _beads_capability(ledger: Ledger, config: Mapping[str, Any]) -> dict[str, Any]:
    try:
        observation = ledger.run(("status",))
        return {
            "required": True,
            "available": True,
            "executable": ledger.executable,
            "database": config["beads"]["database"],
            "duration_ms": observation.duration_ms,
        }
    except LedgerFailure as error:
        return {"required": True, "available": False, "error": str(error)}


def _asset_capability(controller: Path) -> dict[str, Any]:
    package = controller.parent.parent
    expected = [package / "lib", package / "bin" / "fulcrum"]
    missing = [str(path) for path in expected if not path.exists()]
    return {"required": True, "available": not missing, "missing": missing}


def _delivery_capability(config: Mapping[str, Any]) -> dict[str, Any]:
    delivery = config["delivery"]
    executable = delivery.get("executable")
    available = isinstance(executable, str) and Path(executable).is_file()
    invalid: list[str] = []
    if available:
        try:
            repositories = Tollgate(str(executable)).repositories()
            for project_id, project in config["projects"].items():
                delivery_config = project.get("delivery")
                expected = (
                    delivery_config.get("id")
                    if isinstance(delivery_config, Mapping)
                    else None
                )
                if expected and not any(
                    _provider_id(item) == str(expected) for item in repositories
                ):
                    invalid.append(f"projects.{project_id}.delivery.id")
        except TollgateError as error:
            available = False
            invalid.append(str(error))
    return {
        "required": True,
        "available": available and not invalid,
        "kind": "tollgate",
        "executable": executable,
        "error": None if available else "configured Tollgate executable is unavailable",
        "invalid": invalid,
    }


def _provider_id(value: Mapping[str, Any]) -> str | None:
    nested = value.get("state")
    if not isinstance(nested, Mapping):
        nested = value.get("repository")
    row = nested if isinstance(nested, Mapping) else value
    identifier = row.get("id") or row.get("repository_id")
    return str(identifier) if identifier else None


def _runtime_and_leadership(
    request: ParsedRequest,
    ledger: Ledger,
    config: Mapping[str, Any],
    services: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    runtime_config: Mapping[str, Any] = config["runtime"]
    runtime_service = services.get("runtime")
    if runtime_service is not None:
        _start_one(runtime_service, endpoint=str(runtime_config["endpoint"]))
    runtime: AppServerRuntime = AppServerRuntime(str(runtime_config["endpoint"]))

    async def inspect() -> tuple[dict[str, Any], list[dict[str, Any]]]:
        try:
            await runtime.connect()
            capabilities = await runtime.capabilities()
            projects: dict[str, Any] = {}
            for project_id, project in config["projects"].items():
                matches = await runtime.find_projects(str(project["root"]))
                expected = project.get("codex_project_id")
                matching_ids = [
                    str(item.get("id") or item.get("projectId"))
                    for item in matches
                    if item.get("id") or item.get("projectId")
                ]
                projects[str(project_id)] = {
                    "configured_id": expected,
                    "observed_ids": matching_ids,
                    "available": (
                        bool(matching_ids)
                        if not expected
                        else str(expected) in matching_ids
                    ),
                }
            leadership = await ensure_leadership(
                request,
                ledger,
                runtime,
                config,
                send_initial_requests=True,
            )
            return (
                {"required": True, **capabilities.to_dict(), "projects": projects},
                leadership,
            )
        except Exception as error:
            return (
                {
                    "required": True,
                    "available": False,
                    "endpoint": runtime_config["endpoint"],
                    "error": str(error),
                },
                [],
            )
        finally:
            await runtime.close()

    return asyncio.run(inspect())


def _model_capability(
    config: Mapping[str, Any], runtime: Mapping[str, Any]
) -> dict[str, Any]:
    models = runtime.get("models")
    if not runtime.get("available") or not isinstance(models, Mapping):
        return {
            "required": True,
            "available": False,
            "unverified": sorted(config["models"]),
        }
    invalid: list[str] = []
    for role, selection in config["models"].items():
        model = selection["model"]
        effort = selection["effort"]
        efforts = models.get(model)
        if not isinstance(efforts, (list, tuple)) or effort not in efforts:
            invalid.append(f"models.{role}={model}/{effort}")
    return {"required": True, "available": not invalid, "invalid": invalid}


def _project_capability(
    config: Mapping[str, Any], runtime: Mapping[str, Any]
) -> dict[str, Any]:
    invalid: list[str] = []
    observed = runtime.get("projects")
    for project_id, project in config["projects"].items():
        root = Path(str(project["root"]))
        if not root.is_dir():
            invalid.append(f"projects.{project_id}.root")
        if project.get("require_source_sync") and not project.get("source_remote"):
            invalid.append(f"projects.{project_id}.source_remote")
        runtime_project = (
            observed.get(project_id) if isinstance(observed, Mapping) else None
        )
        if not isinstance(runtime_project, Mapping) or not runtime_project.get(
            "available"
        ):
            invalid.append(f"projects.{project_id}.codex_project_id")
    return {"required": True, "available": not invalid, "invalid": invalid}


def _setup_operation_result(operation: Any) -> CommandResult:
    value = str(operation.operation.get("state", "running"))
    state = (
        CommandState(value)
        if value in CommandState._value2member_map_
        else CommandState.RUNNING
    )
    return CommandResult(
        ok=state not in {CommandState.FAILED, CommandState.CANCELLED},
        state=state,
        operation_id=operation.id,
        request_id=str(operation.operation.get("request_id")),
        result=operation_view(operation),
    )
