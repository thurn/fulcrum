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
        from fulcrum.activation import activate

        config = _config(request)
        result = activate(
            request.instance.instance_root,
            request.instance.config_path,
            dict(config["source"]),
            maintenance=bool(request.arguments.get("maintenance")),
            retry=bool(request.arguments.get("retry")),
        )
        return CommandResult.query(result)


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
