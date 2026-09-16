"""Local-master preparation and permanent source-following launchers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from collections.abc import Mapping

from fulcrum.configuration import ConfigurationManager
from fulcrum.contracts import CommandResult, FulcrumError, ParsedRequest
from fulcrum.install import InstallationError
from fulcrum.ledger import Ledger


def ensure_launchers(instance_root: Path) -> tuple[Path, Path, bool]:
    """Provision tiny entry points, never a copied installation of application code.

    These launchers always enter the checkout's selector. They do not need to be
    regenerated after a behavior change, and recovery does not need the host.
    """
    import shlex
    from fulcrum.install import master_source_root

    source = master_source_root()
    python = source / ".venv/bin/python"
    if not python.is_file():
        raise InstallationError(f"dependency interpreter is unavailable: {python}")
    root = instance_root / "bin"
    root.mkdir(parents=True, exist_ok=True)
    changed = False
    for name, flag in (("fulcrum", ""), ("fulcrum-recover", " --fulcrum-recovery")):
        launcher = root / name
        contents = (
            "#!/bin/sh\nexec "
            + shlex.quote(str(python))
            + " -B "
            + shlex.quote(str(source / "src/fulcrum/bootstrap.py"))
            + flag
            + ' "$@"\n'
        )
        if (
            launcher.is_symlink()
            or not launcher.exists()
            or launcher.read_text() != contents
        ):
            temporary = launcher.with_name("." + name + ".tmp")
            temporary.write_text(contents)
            temporary.chmod(0o755)
            os.replace(temporary, launcher)
            changed = True
    return root / "fulcrum", root / "fulcrum-recover", changed


def install_recovery_link(instance_root: Path, *, production: bool) -> Path:
    _, source, _ = ensure_launchers(instance_root)
    if not production:
        return source
    target = Path.home() / ".local/bin/fulcrum-recover"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        resolved = target.resolve(strict=False)
        if resolved == source.resolve(strict=False):
            return target
        if not resolved.is_relative_to(instance_root.resolve(strict=False)):
            raise InstallationError(f"refusing to replace unrelated launcher {target}")
    elif target.exists():
        raise InstallationError(f"refusing to replace real user launcher {target}")
    temporary = target.with_name(f".{target.name}.{os.getpid()}")
    temporary.symlink_to(source)
    os.replace(temporary, target)
    return target


def ensure_recovery_launcher(
    instance_root: Path,
    source: Path,
    operation_key: str,
    *,
    config_path: Path,
    production: bool,
) -> dict[str, Any]:
    _, executable, changed = ensure_launchers(instance_root)
    launcher = install_recovery_link(instance_root, production=production)
    return {
        "entry_point": str(executable),
        "active": str(source),
        "launcher": str(launcher),
        "changed": changed,
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
