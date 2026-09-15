"""Quiescent installed-package refresh and isolated recovery installation."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
import venv
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from fulcrum.configuration import ConfigurationManager
from fulcrum.contracts import CommandResult, FulcrumError, ParsedRequest
from fulcrum.install import (
    InstallationError,
    _package_contents,
    fulcrum2_service_definitions,
    install_fulcrum2_service_definitions,
    installation_source_root,
    inspect_service,
)
from fulcrum.installation_service import (
    ServiceService,
    _operation_result,
    _stop_one,
    load_installed_services,
)
from fulcrum.instance import WriterLock
from fulcrum.ledger import Ledger

MAINTENANCE_REQUEST = "source-refresh-request.json"
MAINTENANCE_READY = "source-refresh-ready.json"


def maintenance_path(instance_root: Path) -> Path:
    return instance_root / MAINTENANCE_REQUEST


def maintenance_ready_path(instance_root: Path) -> Path:
    return instance_root / MAINTENANCE_READY


def read_maintenance(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return dict(value) if isinstance(value, Mapping) else None


def write_maintenance(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    temporary.write_text(
        json.dumps(dict(value), sort_keys=True) + "\n", encoding="utf-8"
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def claim_maintenance(path: Path, value: Mapping[str, Any]) -> bool:
    """Atomically claim the single coalesced installed-refresh boundary."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = (json.dumps(dict(value), sort_keys=True) + "\n").encode()
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        current = read_maintenance(path)
        return current is not None and current.get("operation_id") == value.get(
            "operation_id"
        )
    with os.fdopen(descriptor, "wb") as output:
        output.write(encoded)
        output.flush()
        os.fsync(output.fileno())
    return True


def release_maintenance(path: Path, operation_id: str) -> None:
    current = read_maintenance(path)
    if current is not None and current.get("operation_id") == operation_id:
        path.unlink(missing_ok=True)


def sanitized_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        environment.pop(name, None)
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def probe_installed_environment(
    deployment: Path,
    *,
    config_path: Path | None,
    recovery: bool,
) -> dict[str, Any]:
    executable = deployment / "bin" / ("fulcrum-recover" if recovery else "fulcrum")
    python = deployment / "bin" / "python"
    if not executable.is_file() or not python.is_file():
        raise InstallationError(f"installed entry point is missing from {deployment}")
    environment = sanitized_environment()
    help_probe = subprocess.run(
        [str(executable), "--help"],
        cwd="/",
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if help_probe.returncode != 0:
        raise InstallationError(
            "installed help probe failed: "
            + (help_probe.stderr.strip() or help_probe.stdout.strip())
        )
    code = """
import json
import pathlib
import sys
import fulcrum
import fulcrum.recovery_entry
from fulcrum.configuration import ConfigurationManager

package = pathlib.Path(fulcrum.__file__).resolve().parent
result = {
    'prefix': str(pathlib.Path(sys.prefix).resolve()),
    'module': str(package),
    'formula_assets': (package / 'formulas').is_dir(),
    'fallback_assets': (package / 'role_fallbacks').is_dir(),
}
if len(sys.argv) > 1:
    manager = ConfigurationManager(pathlib.Path(sys.argv[1]))
    document, _ = manager.load()
    result['config_root'] = manager.effective(document)['brain']['root']
print(json.dumps(result))
"""
    command = [str(python), "-I", "-c", code]
    if config_path is not None:
        command.append(str(config_path))
    import_probe = subprocess.run(
        command,
        cwd="/",
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if import_probe.returncode != 0:
        raise InstallationError(
            "installed import/configuration probe failed: "
            + (import_probe.stderr.strip() or import_probe.stdout.strip())
        )
    try:
        facts = json.loads(import_probe.stdout)
    except ValueError as error:
        raise InstallationError(
            "installed import probe returned invalid JSON"
        ) from error
    resolved = deployment.resolve(strict=True)
    module = Path(str(facts.get("module"))).resolve(strict=True)
    prefix = Path(str(facts.get("prefix"))).resolve(strict=True)
    if (
        prefix != resolved
        or not module.is_relative_to(resolved)
        or not facts.get("formula_assets")
        or not facts.get("fallback_assets")
    ):
        raise InstallationError(
            f"installed probe escaped its private environment: {deployment}"
        )
    return {
        "deployment": str(resolved),
        "entry_point": str(executable),
        "module": str(module),
        "config_compatible": config_path is None or bool(facts.get("config_root")),
        "isolated": True,
    }


def build_installed_environment(
    source: Path,
    deployment: Path,
    *,
    config_path: Path | None,
    recovery: bool,
) -> dict[str, Any]:
    source = source.resolve(strict=True)
    if not (source / "pyproject.toml").is_file():
        raise InstallationError(f"installation source has no pyproject.toml: {source}")
    if deployment.exists() or deployment.is_symlink():
        try:
            return probe_installed_environment(
                deployment, config_path=config_path, recovery=recovery
            )
        except (InstallationError, OSError):
            if deployment.is_symlink() or not deployment.is_dir():
                deployment.unlink(missing_ok=True)
            else:
                shutil.rmtree(deployment)
    deployment.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        venv.EnvBuilder(with_pip=True).create(deployment)
        installed = subprocess.run(
            [
                str(deployment / "bin" / "pip"),
                "install",
                "--disable-pip-version-check",
                str(source),
            ],
            cwd="/",
            env=sanitized_environment(),
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
        if installed.returncode != 0:
            raise InstallationError(
                "private installation build failed: "
                + (installed.stderr.strip() or installed.stdout.strip())
            )
        if recovery:
            launcher = deployment / "bin" / "fulcrum-recover"
            launcher.write_text(
                "#!/bin/sh\n"
                "set -eu\n"
                "script=$0\n"
                'while [ -L "$script" ]; do\n'
                '  directory=$(CDPATH= cd -- "$(dirname -- "$script")" && pwd)\n'
                '  target=$(readlink "$script")\n'
                '  case "$target" in /*) script=$target ;; *) script=$directory/$target ;; esac\n'
                "done\n"
                'artifact=$(CDPATH= cd -- "$(dirname -- "$script")/.." && pwd)\n'
                'exec "$artifact/bin/python" -I -m fulcrum.recovery_entry "$@"\n',
                encoding="utf-8",
            )
            os.chmod(launcher, 0o700)
        return probe_installed_environment(
            deployment, config_path=config_path, recovery=recovery
        )
    except Exception:
        # Retain a bounded failure marker but never select a partial environment.
        try:
            (deployment / ".probe-failed").write_text(
                "candidate was not activated\n", encoding="utf-8"
            )
        except OSError:
            pass
        raise


def switch_installed_pointer(root: Path, deployment: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if deployment.parent.resolve(strict=True) != root.resolve(strict=True):
        raise InstallationError("pending installation is outside its owned root")
    current = root / "current"
    temporary = root / f".current.{os.getpid()}.{uuid.uuid4().hex}"
    temporary.symlink_to(deployment.name, target_is_directory=True)
    os.replace(temporary, current)
    return current.resolve(strict=True)


def install_recovery_link(instance_root: Path, *, production: bool) -> Path:
    target = (
        Path.home() / ".local" / "bin" / "fulcrum-recover"
        if production
        else instance_root / "bin" / "fulcrum-recover"
    )
    source = instance_root / "recovery" / "current" / "bin" / "fulcrum-recover"
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.is_symlink():
        resolved = target.resolve(strict=False)
        if resolved == source.resolve(strict=False):
            return target
        if not resolved.is_relative_to(instance_root.resolve(strict=False)):
            raise InstallationError(f"refusing to replace unrelated launcher {target}")
        target.unlink()
    elif target.exists():
        raise InstallationError(f"refusing to replace real user launcher {target}")
    temporary = target.with_name(f".{target.name}.{os.getpid()}")
    temporary.symlink_to(source)
    os.replace(temporary, target)
    return target


def ensure_recovery_environment(
    instance_root: Path,
    source: Path,
    operation_key: str,
    *,
    config_path: Path,
    production: bool,
) -> dict[str, Any]:
    selected = _selected(instance_root / "recovery")
    source_package = source / "src" / "fulcrum"
    if not source_package.is_dir():
        source_package = source / "fulcrum"
    if selected is not None:
        installed = next(selected.glob("lib/python*/site-packages/fulcrum"), None)
        if installed is not None and _package_contents(installed) == _package_contents(
            source_package
        ):
            probe = probe_installed_environment(
                selected, config_path=config_path, recovery=True
            )
            launcher = install_recovery_link(instance_root, production=production)
            return {
                **probe,
                "active": str(selected),
                "launcher": str(launcher),
                "changed": False,
            }
    token = operation_key.removeprefix("fc-").replace("/", "-")
    deployment = instance_root / "recovery" / f"deployment-{token}"
    probe = build_installed_environment(
        source, deployment, config_path=config_path, recovery=True
    )
    active = switch_installed_pointer(instance_root / "recovery", deployment)
    launcher = install_recovery_link(instance_root, production=production)
    return {
        **probe,
        "active": str(active),
        "launcher": str(launcher),
        "changed": True,
    }


class SourceRefreshService:
    def update(self, request: ParsedRequest) -> CommandResult:
        config = _config(request)
        ledger = _ledger(request, config)
        requested = request.arguments.get("source")
        source = (
            Path(str(requested))
            if requested is not None
            else Path(
                str(config.get("source_watch_root") or installation_source_root())
            )
        )
        if not source.is_absolute():
            raise FulcrumError.invalid(
                "INVALID_PATH", "service update source must be absolute"
            )
        source = source.resolve(strict=True)
        runtime_root = request.instance.instance_root / "runtime"
        recovery_root = request.instance.instance_root / "recovery"
        current_main = _selected(runtime_root)
        current_recovery = _selected(recovery_root)
        operation, reused = ledger.create_operation(
            request,
            planned={
                "source": str(source),
                "current_main": str(current_main) if current_main else None,
                "current_recovery": (
                    str(current_recovery) if current_recovery else None
                ),
            },
            next_action="Pause mutation admission and reach a quiescent controller boundary.",
        )
        if reused and operation.operation.get("state") in {"completed", "cancelled"}:
            return _operation_result(operation)
        planned = dict(operation.operation.get("planned") or {})
        source = Path(str(planned.get("source") or source)).resolve(strict=True)
        token = operation.id.removeprefix("fc-")
        pending_main = runtime_root / f"deployment-{token}"
        pending_recovery = recovery_root / f"deployment-{token}"
        planned.update(
            {
                "pending_main": str(pending_main),
                "pending_recovery": str(pending_recovery),
            }
        )
        operation = ledger.update_operation(
            operation,
            state="accepted",
            step="quiescence_requested",
            planned=planned,
            error={},
            next_action="Wait for the controller to reject new mutations and acknowledge quiescence.",
        )
        marker = maintenance_path(request.instance.instance_root)
        ready = maintenance_ready_path(request.instance.instance_root)
        claim = {
            "operation_id": operation.id,
            "source": str(source),
            "requested_at": time.time(),
        }
        if not claim_maintenance(marker, claim):
            current = read_maintenance(marker)
            operation = ledger.update_operation(
                operation,
                state="accepted",
                step="coalesced_behind_active_refresh",
                result={"active_refresh": current, "planned": planned},
                next_action="Repeat this request after the active installed refresh settles.",
            )
            return _operation_result(operation)
        activated = False
        controller_stopped = False
        try:
            quiescence = _wait_for_quiescence(request, operation.id, ready)
            if not quiescence["ready"]:
                operation = ledger.update_operation(
                    operation,
                    state="accepted",
                    step="waiting_for_quiescence",
                    result={"quiescence": quiescence, "planned": planned},
                    next_action="Repeat service update with the same request ID after in-flight mutations settle.",
                )
                release_maintenance(marker, operation.id)
                release_maintenance(ready, operation.id)
                return _operation_result(operation)
            main_probe: dict[str, Any] | None = None
            recovery_probe: dict[str, Any] | None = None
            for _attempt in range(3):
                before = _source_observation(source)
                main_probe = build_installed_environment(
                    source,
                    pending_main,
                    config_path=request.instance.config_path,
                    recovery=False,
                )
                recovery_probe = build_installed_environment(
                    source,
                    pending_recovery,
                    config_path=request.instance.config_path,
                    recovery=True,
                )
                if before == _source_observation(source):
                    break
                shutil.rmtree(pending_main, ignore_errors=True)
                shutil.rmtree(pending_recovery, ignore_errors=True)
                main_probe = None
                recovery_probe = None
            if main_probe is None or recovery_probe is None:
                raise InstallationError(
                    "installation source kept changing throughout the bounded build"
                )
            operation = ledger.update_operation(
                operation,
                step="activation_intent_retained",
                planned={
                    **planned,
                    "main_probe": main_probe,
                    "recovery_probe": recovery_probe,
                    "observed_main_before": str(_selected(runtime_root)),
                    "observed_recovery_before": str(_selected(recovery_root)),
                },
                next_action="Stop only the quiescent owned controller and atomically select both candidates.",
            )
            services = load_installed_services(request.instance.instance_root)
            controller = services.get("controller")
            if controller is not None:
                _stop_one(controller)
                controller_stopped = True
            lock_path = request.instance.lock_path
            if lock_path is None:
                raise FulcrumError(
                    "CONFIG_INVALID",
                    "source activation requires the brain writer lock",
                    exit_code=4,
                )
            with WriterLock(lock_path):
                observed_main = _selected(runtime_root)
                original_main = _path_or_none(planned.get("current_main"))
                if observed_main not in {
                    original_main,
                    pending_main.resolve(strict=False),
                }:
                    raise FulcrumError(
                        "ACTIVATION_CONFLICT",
                        "active main installation changed after update intent",
                        exit_code=5,
                        details={"observed": str(observed_main), "planned": planned},
                    )
                switch_installed_pointer(recovery_root, pending_recovery)
                active_main = switch_installed_pointer(runtime_root, pending_main)
                activated = True
                definitions = fulcrum2_service_definitions(
                    instance_root=request.instance.instance_root,
                    config_path=request.instance.config_path,
                    brain_root=request.instance.brain_root
                    or request.instance.config_path.parent,
                    config=dict(config),
                    controller_executable=active_main / "bin" / "fulcrum",
                    production=not request.instance.explicit_selection,
                )
                installed, changed = install_fulcrum2_service_definitions(
                    definitions, request.instance.instance_root
                )
                install_recovery_link(
                    request.instance.instance_root,
                    production=not request.instance.explicit_selection,
                )
            assert request.request_id is not None
            start_request = replace(
                request,
                command=("service", "start"),
                arguments={},
                input={},
                request_id=str(
                    uuid.uuid5(uuid.UUID(request.request_id), "updated-service-start")
                ),
            )
            started = ServiceService().start(start_request)
            release_maintenance(marker, operation.id)
            release_maintenance(ready, operation.id)
            operation = ledger.update_operation(
                operation,
                state="completed",
                step="updated_controller_started",
                external={
                    "active_main": str(_selected(runtime_root)),
                    "active_recovery": str(_selected(recovery_root)),
                },
                result={
                    "source": str(source),
                    "previous_main": planned.get("current_main"),
                    "active_main": str(_selected(runtime_root)),
                    "previous_recovery": planned.get("current_recovery"),
                    "active_recovery": str(_selected(recovery_root)),
                    "main_probe": main_probe,
                    "recovery_probe": recovery_probe,
                    "changed_service_definitions": changed,
                    "services": sorted(installed),
                    "start_operation": started.operation_id,
                    "identities_retained": True,
                },
                error={},
                next_action="No further action is required.",
            )
            return _operation_result(operation)
        except Exception as error:
            working_service_preserved = not controller_stopped
            if not activated:
                if controller_stopped and request.request_id is not None:
                    try:
                        restarted = ServiceService().start(
                            replace(
                                request,
                                command=("service", "start"),
                                arguments={},
                                input={},
                                request_id=str(
                                    uuid.uuid5(
                                        uuid.UUID(request.request_id),
                                        "failed-update-service-restart",
                                    )
                                ),
                            )
                        )
                        working_service_preserved = restarted.state.value == "completed"
                    except Exception:
                        working_service_preserved = False
                release_maintenance(marker, operation.id)
                release_maintenance(ready, operation.id)
            operation = ledger.update_operation(
                operation,
                state="uncertain" if activated else "failed",
                step=(
                    "activation_requires_reconciliation"
                    if activated
                    else "candidate_probe_failed"
                ),
                error={
                    "code": getattr(error, "code", type(error).__name__),
                    "message": str(error),
                    "retryable": True,
                },
                result={
                    "source": str(source),
                    "current_main": str(_selected(runtime_root)),
                    "pending_main": str(pending_main),
                    "current_recovery": str(_selected(recovery_root)),
                    "pending_recovery": str(pending_recovery),
                    "working_service_preserved": working_service_preserved,
                },
                next_action="Inspect the selected and pending paths, then repeat this exact request.",
            )
            return _operation_result(operation)


def _config(request: ParsedRequest) -> dict[str, Any]:
    manager = ConfigurationManager(request.instance.config_path)
    document, _ = manager.load()
    return manager.effective(document)


def _ledger(request: ParsedRequest, config: Mapping[str, Any]) -> Ledger:
    if request.instance.brain_root is None:
        raise FulcrumError(
            "LEDGER_UNAVAILABLE", "configuration has no brain root", exit_code=4
        )
    return Ledger(
        request.instance.brain_root,
        executable=str(config["beads"]["executable"]),
        timeout=request.timeout,
    )


def _selected(root: Path) -> Path | None:
    current = root / "current"
    try:
        return current.resolve(strict=True)
    except OSError:
        return None


def _path_or_none(value: Any) -> Path | None:
    return Path(str(value)).resolve(strict=False) if value else None


def _source_observation(source: Path) -> tuple[tuple[str, int, int], ...]:
    rows: list[tuple[str, int, int]] = []
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        if any(
            part in {".git", ".venv", "__pycache__", "build", "dist"}
            for part in relative.parts
        ):
            continue
        if not path.is_file():
            continue
        try:
            status = path.stat()
        except OSError:
            continue
        rows.append((str(relative), status.st_size, status.st_mtime_ns))
    return tuple(sorted(rows))


def _wait_for_quiescence(
    request: ParsedRequest, operation_id: str, ready: Path
) -> dict[str, Any]:
    controller = load_installed_services(request.instance.instance_root).get(
        "controller"
    )
    if controller is None or not inspect_service(controller.label).running:
        return {"ready": True, "controller_running": False, "acknowledged": False}
    deadline = time.monotonic() + request.timeout
    while time.monotonic() < deadline:
        value = read_maintenance(ready)
        if value is not None and value.get("operation_id") == operation_id:
            return {"ready": True, "controller_running": True, "acknowledged": True}
        time.sleep(0.05)
    return {"ready": False, "controller_running": True, "acknowledged": False}
