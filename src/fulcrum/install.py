"""Checkout-backed assets and macOS service configuration."""

from __future__ import annotations

import json
import importlib.metadata
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fulcrum.config import InstallationConfig, RuntimePaths


class InstallationError(RuntimeError):
    pass


HUMAN_SKILLS = (
    "fulcrum-vizier",
    "fulcrum-marshal",
    "fulcrum-weaver",
    "fulcrum-executor",
    "fulcrum-warden",
    "fulcrum-sage",
    "fulcrum-mason",
    "fulcrum-justiciar",
    "fulcrum-bead",
)
REMOVED_SKILLS = (
    "fulcrum-setup",
    "fulcrum-archon",
    "fulcrum-operative",
    "fulcrum-overseer",
    "fulcrum-inquisitor",
    "fulcrum-night-watchman",
    "fulcrum-shared",
)
APP_SERVER_LABEL = "dev.fulcrum.codex-app-server"
CONTROLLER_LABEL = "dev.fulcrum.controller"
SYSTEM_EXECUTABLE_PATHS = (
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
    "/usr/local/sbin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)
USER_EXECUTABLE_PATHS = (".local/bin", "bin")
WORKTREE_RUNTIME_IMPORTS = ("jsonschema", "yaml", "watchfiles", "websockets")
WORKTREE_DEVELOPMENT_COMMANDS = ("black", "pyre", "fulcrum")
RECOVERY_DISTRIBUTIONS = (
    "anyio",
    "attrs",
    "idna",
    "jsonschema",
    "jsonschema-specifications",
    "referencing",
    "rpds-py",
    "PyYAML",
    "watchfiles",
    "websockets",
    "typing_extensions",
)


@dataclass(frozen=True)
class ServiceObservation:
    """Typed evidence about one launchd job, not merely its plist on disk."""

    label: str
    loaded: bool
    state: str | None
    pid: int | None
    executable_path: str | None
    program_arguments: tuple[str, ...]
    working_directory: str | None
    detail: str

    @property
    def running(self) -> bool:
        return self.loaded and self.state == "running" and self.pid is not None


@dataclass(frozen=True)
class InstalledService:
    """One instance-owned launch service and its exact definition."""

    name: str
    label: str
    definition: Path


def installation_source_root() -> Path:
    """Return the retained source when present, otherwise the installed package."""

    package = package_root()
    candidate = package.parents[1]
    if (candidate / "pyproject.toml").is_file() and (candidate / "skills").is_dir():
        return candidate
    return package


def owned_skills_source() -> Path:
    source = installation_source_root()
    checkout_skills = source / "skills"
    if checkout_skills.is_dir():
        return checkout_skills
    packaged = Path(sys.prefix) / "share" / "fulcrum" / "skills"
    return packaged if packaged.is_dir() else package_root() / "skills"


def reconcile_fulcrum2_skills(
    instance_root: Path,
    *,
    production: bool,
    skills_root: Path | None = None,
) -> dict[str, Any]:
    """Repair only the nine human-invoked Fulcrum2 skill links."""

    source = owned_skills_source().resolve(strict=True)
    root = (
        skills_root
        or (
            Path.home() / ".codex" / "skills"
            if production
            else instance_root / "codex" / "skills"
        )
    ).resolve(strict=False)
    installed: list[str] = []
    unchanged: list[str] = []
    for name in HUMAN_SKILLS:
        skill = source / name
        if (
            not (skill / "SKILL.md").is_file()
            or not (skill / "agents" / "openai.yaml").is_file()
        ):
            raise InstallationError(f"missing packaged skill assets for {name}")
        target = root / name
        if target.exists() and not target.is_symlink():
            raise InstallationError(
                f"refusing to replace real user skill directory {target}"
            )
        if target.is_symlink() and target.resolve(strict=False) == skill.resolve(
            strict=False
        ):
            unchanged.append(str(target))
            continue
        _replace_owned_link(skill, target)
        installed.append(str(target))
    removed: list[str] = []
    for name in REMOVED_SKILLS:
        target = root / name
        if not target.is_symlink():
            continue
        resolved = target.resolve(strict=False)
        if resolved.is_relative_to(source) or not resolved.exists():
            target.unlink()
            removed.append(str(target))
    return {
        "root": str(root),
        "installed": installed,
        "unchanged": unchanged,
        "removed": removed,
        "implicit_invocation": False,
    }


def _service_identity(instance_root: Path, brain_root: Path) -> str:
    identity_path = instance_root / "service-identity"
    expected_binding = (
        f"{instance_root.resolve(strict=False)}\n{brain_root.resolve(strict=False)}\n"
    )
    if identity_path.exists() and identity_path.is_dir():
        raise InstallationError(
            f"service identity path is a user directory: {identity_path}"
        )
    if identity_path.is_file():
        lines = identity_path.read_text(encoding="utf-8").splitlines()
        if len(lines) != 3 or "\n".join(lines[:2]) + "\n" != expected_binding:
            raise InstallationError(
                "installed service identity is bound to a different instance or brain"
            )
        token = lines[2]
        if re.fullmatch(r"[a-f0-9]{32}", token):
            return token
        raise InstallationError(f"invalid installed service identity: {identity_path}")
    instance_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = uuid.uuid4().hex
    temporary = identity_path.with_name(f".{identity_path.name}.{os.getpid()}")
    temporary.write_text(expected_binding + token + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, identity_path)
    return token


def fulcrum2_service_definitions(
    *,
    instance_root: Path,
    config_path: Path,
    brain_root: Path,
    config: dict[str, Any],
    controller_executable: Path,
    production: bool,
) -> dict[str, dict[str, Any]]:
    """Build exact per-instance LaunchAgent definitions."""

    token = _service_identity(instance_root, brain_root)
    prefix = f"dev.fulcrum.{token}"
    logs = instance_root / "logs"
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    environment = {
        "PATH": service_executable_path(),
        "FULCRUM_INSTANCE": str(instance_root.resolve(strict=False)),
    }
    beads = dict(config["beads"])
    dolt = shutil.which("dolt")
    if not dolt:
        raise InstallationError("required Dolt executable is unavailable")
    definitions: dict[str, dict[str, Any]] = {
        "dolt": {
            "Label": f"{prefix}.dolt",
            "ProgramArguments": [
                str(Path(dolt).resolve(strict=True)),
                "sql-server",
                "-H",
                str(beads["host"]),
                "-P",
                str(beads["port"]),
                "--data-dir",
                str(brain_root / ".beads" / "dolt"),
            ],
            "WorkingDirectory": str(brain_root),
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(logs / "dolt.log"),
            "StandardErrorPath": str(logs / "dolt-error.log"),
            "EnvironmentVariables": environment,
        },
        "controller": {
            "Label": f"{prefix}.controller",
            "ProgramArguments": [
                str(controller_executable.resolve(strict=True)),
                "serve",
                "--instance",
                str(instance_root.resolve(strict=False)),
                "--config",
                str(config_path.resolve(strict=False)),
            ],
            "WorkingDirectory": str(instance_root),
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(logs / "controller.log"),
            "StandardErrorPath": str(logs / "controller-error.log"),
            "EnvironmentVariables": environment,
        },
    }
    runtime = dict(config["runtime"])
    if production and runtime["kind"] == "codex":
        executable = runtime.get("executable")
        if not isinstance(executable, str) or not executable:
            raise InstallationError("runtime.executable is required for production")
        definitions["runtime"] = {
            "Label": f"{prefix}.runtime",
            "ProgramArguments": [
                str(Path(executable).resolve(strict=True)),
                "app-server",
                "--listen",
                str(runtime["endpoint"]),
            ],
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(logs / "runtime.log"),
            "StandardErrorPath": str(logs / "runtime-error.log"),
            "EnvironmentVariables": environment,
            "SoftResourceLimits": {
                "NumberOfFiles": int(config["resources"]["fd_soft_limit"])
            },
        }
    return definitions


def install_fulcrum2_service_definitions(
    definitions: dict[str, dict[str, Any]], instance_root: Path
) -> tuple[dict[str, InstalledService], list[str]]:
    root = instance_root / "services"
    if root.exists() and not root.is_dir():
        raise InstallationError(f"service asset path is not a directory: {root}")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    installed: dict[str, InstalledService] = {}
    changed: list[str] = []
    for name, definition in definitions.items():
        label = str(definition["Label"])
        path = root / f"{name}.plist"
        encoded = plistlib.dumps(definition, fmt=plistlib.FMT_XML, sort_keys=True)
        if not path.is_file() or path.read_bytes() != encoded:
            temporary = path.with_name(f".{path.name}.{os.getpid()}")
            temporary.write_bytes(encoded)
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            changed.append(name)
        installed[name] = InstalledService(name, label, path)
    return installed, changed


def service_executable_path(*, user_home: Path | None = None) -> str:
    """Return a deterministic PATH suitable for processes started by launchd."""

    home = (user_home or Path.home()).resolve(strict=False)
    entries = [str(home / relative) for relative in USER_EXECUTABLE_PATHS]
    entries.extend(SYSTEM_EXECUTABLE_PATHS)
    return os.pathsep.join(entries)


def package_root() -> Path:
    return Path(__file__).resolve().parent


def control_plane_source(paths: RuntimePaths) -> Path:
    return paths.control_root / "runtime" / "current" / "fulcrum"


def _package_contents(package: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(package): path.read_bytes()
        for path in package.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }


def install_control_plane(
    config: InstallationConfig, paths: RuntimePaths
) -> tuple[Path, bool]:
    """Atomically install an immutable controller snapshot outside managed source."""

    source = Path(config.source_root).resolve(strict=True) / "src" / "fulcrum"
    runtime_root = paths.control_root / "runtime"
    runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    current = runtime_root / "current"
    installed = current / "fulcrum"
    previous = _package_contents(installed) if installed.is_dir() else {}
    deployment: Path | None = None
    copied: dict[Path, bytes] = {}
    for _attempt in range(3):
        before = _package_contents(source)
        candidate = runtime_root / f"deployment-{os.getpid()}-{uuid.uuid4().hex}"
        try:
            shutil.copytree(
                source.parent,
                candidate,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            after = _package_contents(source)
            copied = _package_contents(candidate / "fulcrum")
        except Exception:
            shutil.rmtree(candidate, ignore_errors=True)
            raise
        if before == after == copied and before:
            deployment = candidate
            break
        shutil.rmtree(candidate, ignore_errors=True)
    if deployment is None:
        raise InstallationError(
            "controller source changed throughout snapshot installation; retry when stable"
        )
    temporary = runtime_root / f".current.{os.getpid()}.tmp"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(deployment.name, target_is_directory=True)
    os.replace(temporary, current)
    active = current.resolve(strict=True)
    for child in runtime_root.glob("deployment-*"):
        if child.resolve(strict=False) != active and child.is_dir():
            shutil.rmtree(child)
    return current / "fulcrum", previous != copied


def _artifact_contents(root: Path) -> dict[Path, bytes | str]:
    if not root.is_dir():
        return {}
    result: dict[Path, bytes | str] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts:
            continue
        if path.is_symlink():
            result[relative] = f"symlink:{os.readlink(path)}"
        elif path.is_file():
            result[relative] = path.read_bytes()
    return result


def _copy_installed_distribution(name: str, target: Path) -> None:
    """Copy one installed wheel payload without retaining environment links."""

    try:
        distribution = importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError as error:
        raise InstallationError(
            f"recovery dependency is unavailable in the setup environment: {name}"
        ) from error
    root = Path(str(distribution.locate_file(""))).resolve(strict=True)
    files = distribution.files or []
    for entry in files:
        source = Path(str(distribution.locate_file(entry)))
        try:
            resolved = source.resolve(strict=True)
        except OSError:
            continue
        if not resolved.is_file() or not resolved.is_relative_to(root):
            continue
        relative = resolved.relative_to(root)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(resolved, destination)


def install_recovery_artifact(
    config: InstallationConfig, paths: RuntimePaths
) -> tuple[Path, bool]:
    """Atomically install the checkout-independent break-glass runtime."""

    source_package = Path(config.source_root).resolve(strict=True) / "src" / "fulcrum"
    recovery_root = paths.recovery_root
    recovery_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    current = recovery_root / "current"
    previous = _artifact_contents(current.resolve()) if current.is_dir() else {}
    deployment = recovery_root / f"deployment-{os.getpid()}-{uuid.uuid4().hex}"
    site = (
        deployment
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    binary = deployment / "bin"
    try:
        site.mkdir(parents=True, mode=0o700)
        binary.mkdir(parents=True, mode=0o700)
        shutil.copytree(
            source_package,
            site / "fulcrum",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        for name in RECOVERY_DISTRIBUTIONS:
            _copy_installed_distribution(name, site)
        base_python = Path(sys.executable).resolve(strict=True)
        (binary / "python").symlink_to(base_python)
        (deployment / "pyvenv.cfg").write_text(
            "\n".join(
                [
                    f"home = {base_python.parent}",
                    "include-system-site-packages = false",
                    f"version = {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        launcher = binary / "operative-recovery"
        launcher.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            'artifact=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)\n'
            'exec "$artifact/bin/python" -I -m fulcrum.recovery "$@"\n',
            encoding="utf-8",
        )
        os.chmod(launcher, 0o700)
        smoke = subprocess.run(
            [str(launcher), "--smoke-test"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        try:
            smoke_detail = json.loads(smoke.stdout)
        except json.JSONDecodeError:
            smoke_detail = None
        if not (
            smoke.returncode == 0
            and isinstance(smoke_detail, dict)
            and smoke_detail.get("isolated") is True
            and isinstance(smoke_detail.get("module"), str)
            and Path(str(smoke_detail["module"]))
            .resolve(strict=True)
            .is_relative_to(deployment.resolve(strict=True))
        ):
            raise InstallationError(
                "recovery artifact smoke test failed: "
                + (smoke.stderr.strip() or smoke.stdout.strip() or "no diagnostic")
            )
        copied = _artifact_contents(deployment)
        if not copied:
            raise InstallationError("recovery artifact is empty")
        temporary = recovery_root / f".current.{os.getpid()}.tmp"
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(deployment.name, target_is_directory=True)
        os.replace(temporary, current)
        active = current.resolve(strict=True)
        for child in recovery_root.glob("deployment-*"):
            if child.resolve(strict=False) != active and child.is_dir():
                shutil.rmtree(child)
        return paths.recovery_launcher, previous != copied
    except Exception:
        shutil.rmtree(deployment, ignore_errors=True)
        raise


def controller_program_arguments(
    config: InstallationConfig, paths: RuntimePaths
) -> list[str]:
    python = Path(config.source_root) / ".venv" / "bin" / "python"
    runtime_root = control_plane_source(paths).parent
    launcher = (
        "import sys; "
        f"sys.path.insert(0, {str(runtime_root)!r}); "
        "from fulcrum.cli import main; raise SystemExit(main())"
    )
    return [str(python), "-I", "-c", launcher, "serve"]


def _remove_environment_path(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _worktree_environment_is_ready(
    environment: Path, worktree: Path, source_environment: Path
) -> bool:
    """Validate the boundary and installed tools without importing Fulcrum."""

    if environment.is_symlink() or not environment.is_dir():
        return False
    python = environment / "bin" / "python"
    if not os.access(python, os.X_OK):
        return False
    inspection = """
import importlib.metadata
import json
import pathlib
import site
import sys

for name in sys.argv[1].split(','):
    __import__(name)
distribution = importlib.metadata.distribution('fulcrum')
print(json.dumps({
    'prefix': sys.prefix,
    'base_prefix': sys.base_prefix,
    'site_packages': site.getsitepackages(),
    'distribution_root': str(distribution.locate_file('')),
    'direct_url': distribution.read_text('direct_url.json'),
    'version': list(sys.version_info[:2]),
}))
"""
    try:
        completed = subprocess.run(
            [
                str(python),
                "-I",
                "-c",
                inspection,
                ",".join(WORKTREE_RUNTIME_IMPORTS),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if completed.returncode != 0:
            return False
        observed = json.loads(completed.stdout)
        direct_url = json.loads(observed["direct_url"])
        editable = direct_url.get("dir_info", {}).get("editable") is True
        editable_url = urllib.parse.urlparse(direct_url["url"])
        if editable_url.scheme != "file":
            return False
        installed_source = Path(
            urllib.request.url2pathname(urllib.parse.unquote(editable_url.path))
        )
        environment_root = environment.resolve(strict=True)
        source_root = source_environment.resolve(strict=True)
        site_packages = [
            Path(item).resolve(strict=True) for item in observed["site_packages"]
        ]
        if not (
            observed["version"] == [3, 12]
            and Path(observed["prefix"]).resolve(strict=True) == environment_root
            and Path(observed["base_prefix"]).resolve(strict=True) != environment_root
            and environment_root != source_root
            and all(path.is_relative_to(environment_root) for path in site_packages)
            and all(not path.is_relative_to(source_root) for path in site_packages)
            and Path(observed["distribution_root"])
            .resolve(strict=True)
            .is_relative_to(environment_root)
            and editable
            and installed_source.resolve(strict=True) == worktree.resolve(strict=True)
        ):
            return False
        for name in WORKTREE_DEVELOPMENT_COMMANDS:
            command = environment / "bin" / name
            if not os.access(command, os.X_OK) or not command.resolve(
                strict=True
            ).is_relative_to(environment_root):
                return False
        return True
    except (
        KeyError,
        OSError,
        ValueError,
        json.JSONDecodeError,
        subprocess.TimeoutExpired,
    ):
        return False


def _run_environment_install(command: list[str], *, worktree: Path) -> None:
    completed = subprocess.run(
        command,
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode == 0:
        return
    detail = completed.stderr.strip() or completed.stdout.strip() or "no output"
    raise InstallationError(
        f"managed worktree environment command failed ({' '.join(command)}): {detail}"
    )


def provision_worktree_environment(source_root: Path, worktree: Path) -> bool:
    """Create or verify a worktree-owned Python development environment.

    Pip's ordinary cache remains enabled, but no files from the controller's
    environment are linked into the managed worktree.
    """

    source = source_root.resolve(strict=True)
    managed = worktree.resolve(strict=True)
    if managed == source:
        raise InstallationError("managed worktree must differ from the source checkout")
    source_environment = source / ".venv"
    source_python = source_environment / "bin" / "python"
    requirements = managed / "requirements-dev.lock"
    project = managed / "pyproject.toml"
    if not os.access(source_python, os.X_OK):
        raise InstallationError(
            f"controller Python environment is unavailable: {source_environment}"
        )
    if not requirements.is_file() or not project.is_file():
        raise InstallationError(
            f"managed worktree lacks pyproject.toml or requirements-dev.lock: {managed}"
        )

    target = managed / ".venv"
    if _worktree_environment_is_ready(target, managed, source_environment):
        return False

    displaced = managed / f".venv.invalid-{os.getpid()}-{uuid.uuid4().hex}"
    if target.exists() or target.is_symlink():
        os.replace(target, displaced)
    try:
        _run_environment_install(
            [str(source_python), "-m", "venv", str(target)], worktree=managed
        )
        python = target / "bin" / "python"
        _run_environment_install(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--requirement",
                str(requirements),
            ],
            worktree=managed,
        )
        _run_environment_install(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-deps",
                "--editable",
                str(managed),
            ],
            worktree=managed,
        )
        if not _worktree_environment_is_ready(target, managed, source_environment):
            raise InstallationError(
                f"managed worktree environment failed validation: {target}"
            )
    except Exception:
        _remove_environment_path(target)
        if displaced.exists() or displaced.is_symlink():
            os.replace(displaced, target)
        raise
    _remove_environment_path(displaced)
    return True


def verify_editable_source(source_root: Path) -> None:
    root = source_root.resolve(strict=True)
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or Path(result.stdout.strip()).resolve() != root:
        raise InstallationError("source root must be the retained Git checkout root")
    if ".worktrees" in root.parts:
        raise InstallationError("source root cannot be a disposable worktree")
    if package_root() != root / "src" / "fulcrum":
        raise InstallationError(
            f"Fulcrum imports from {package_root()}, not editable source {root}"
        )


def _replace_owned_link(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.is_symlink():
        if target.resolve(strict=False) == source.resolve(strict=False):
            return
        target.unlink()
    elif target.exists():
        raise InstallationError(f"refusing to replace non-symlink asset {target}")
    temporary = target.with_name(f".{target.name}.{os.getpid()}")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(source.absolute(), target_is_directory=source.is_dir())
    os.replace(temporary, target)


def reconcile_skill_links(
    source_root: Path, *, skills_root: Path | None = None
) -> list[str]:
    """Point every installed Fulcrum skill at the retained source checkout."""

    source = source_root.resolve(strict=True)
    root = skills_root or Path.home() / ".codex" / "skills"
    skill_sources = {name: source / "skills" / name for name in HUMAN_SKILLS}
    for name, skill in skill_sources.items():
        if not (skill / "SKILL.md").is_file():
            raise InstallationError(f"missing human entry skill {skill}")
        target = root / name
        if target.exists() and not target.is_symlink():
            raise InstallationError(f"refusing to replace non-symlink asset {target}")

    installed: list[str] = []
    for name, skill in skill_sources.items():
        target = root / name
        _replace_owned_link(skill, target)
        installed.append(str(target))
    return installed


def install_links(
    config: InstallationConfig, *, codex_root: Path | None = None
) -> dict[str, Any]:
    source = Path(config.source_root).resolve(strict=True)
    root = codex_root or Path.home() / ".codex"
    installed = reconcile_skill_links(source, skills_root=root / "skills")
    removed: list[str] = []
    for name in REMOVED_SKILLS:
        target = root / "skills" / name
        if target.is_symlink():
            resolved = target.resolve(strict=False)
            if resolved.is_relative_to(source) or not resolved.exists():
                target.unlink()
                removed.append(str(target))
    hook_source = source / "hooks" / "fulcrum-hook"
    cli_source = source / ".venv" / "bin" / "fulcrum"
    if not os.access(hook_source, os.X_OK) or not os.access(cli_source, os.X_OK):
        raise InstallationError("editable CLI and hook executables must exist")
    hook_target = root / "hooks" / "fulcrum-hook"
    cli_target = root / "bin" / "fulcrum"
    obsolete_hook = root / "hooks" / "fulcrum"
    if obsolete_hook.is_symlink() and obsolete_hook.resolve(
        strict=False
    ).is_relative_to(source):
        obsolete_hook.unlink()
        removed.append(str(obsolete_hook))
    _replace_owned_link(hook_source, hook_target)
    _replace_owned_link(cli_source, cli_target)
    install_hook_config(root / "hooks.json", hook_target)
    return {
        "skills": installed,
        "removed": removed,
        "hook": str(hook_target),
        "cli": str(cli_target),
    }


def install_hook_config(path: Path, hook_command: Path) -> None:
    """Preserve unrelated hooks and install only one marked compact handler."""

    existing: Any = (
        json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    )
    if not isinstance(existing, dict) or not isinstance(
        existing.get("hooks", {}), dict
    ):
        raise InstallationError(f"Codex hook configuration is invalid: {path}")
    hooks = dict(existing.get("hooks", {}))
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        retained = []
        for group in groups:
            if not isinstance(group, dict):
                retained.append(group)
                continue
            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                retained.append(group)
                continue
            remaining = [
                item
                for item in handlers
                if not (
                    isinstance(item, dict)
                    and str(item.get("statusMessage", "")).startswith("Fulcrum:")
                )
            ]
            if remaining:
                retained.append({**group, "hooks": remaining})
        if retained:
            hooks[event] = retained
        else:
            hooks.pop(event, None)
    hooks["SessionStart"] = [
        *hooks.get("SessionStart", []),
        {
            "matcher": "^compact$",
            "hooks": [
                {
                    "type": "command",
                    "command": shlex.quote(str(hook_command.absolute())),
                    "timeout": 2,
                    "additionalContextLimit": 5000,
                    "statusMessage": "Fulcrum: restoring current action",
                }
            ],
        },
    ]
    existing["hooks"] = hooks
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    temporary.write_text(
        json.dumps(existing, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def service_definitions(
    config: InstallationConfig,
    paths: RuntimePaths,
    *,
    user_home: Path | None = None,
) -> dict[str, dict[str, Any]]:
    source = Path(config.source_root)
    logs = paths.logs_root
    executable_path = service_executable_path(user_home=user_home)
    return {
        APP_SERVER_LABEL: {
            "Label": APP_SERVER_LABEL,
            "ProgramArguments": [
                config.codex_bin,
                "app-server",
                "--listen",
                config.app_server_endpoint,
            ],
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(logs / "app-server.log"),
            "StandardErrorPath": str(logs / "app-server-error.log"),
            "EnvironmentVariables": {"PATH": executable_path},
        },
        CONTROLLER_LABEL: {
            "Label": CONTROLLER_LABEL,
            "ProgramArguments": controller_program_arguments(config, paths),
            "WorkingDirectory": str(paths.control_root),
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(logs / "controller.log"),
            "StandardErrorPath": str(logs / "controller-error.log"),
            "EnvironmentVariables": {
                "FULCRUM_CONFIG": str(paths.config_file),
                "PATH": executable_path,
            },
        },
    }


def install_services(
    config: InstallationConfig,
    paths: RuntimePaths,
    *,
    launch_agents: Path | None = None,
    user_home: Path | None = None,
) -> tuple[dict[str, str], frozenset[str]]:
    target_root = launch_agents or Path.home() / "Library" / "LaunchAgents"
    target_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths.logs_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    installed: dict[str, str] = {}
    updated: set[str] = set()
    for label, definition in service_definitions(
        config, paths, user_home=user_home
    ).items():
        target = target_root / f"{label}.plist"
        encoded = plistlib.dumps(definition, fmt=plistlib.FMT_XML, sort_keys=True)
        if not target.is_file() or target.read_bytes() != encoded:
            temporary = target.with_name(f".{target.name}.{os.getpid()}")
            temporary.write_bytes(encoded)
            os.replace(temporary, target)
            updated.add(label)
        installed[label] = str(target)
    wrapper = paths.control_root / "open-codex-with-fulcrum"
    wrapper.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    script = f'#!/bin/sh\nexec env CODEX_APP_SERVER_WS_URL={json.dumps(config.app_server_endpoint)} {json.dumps(config.desktop_executable)} "$@"\n'
    if not wrapper.is_file() or wrapper.read_text(encoding="utf-8") != script:
        temporary = wrapper.with_name(f".{wrapper.name}.{os.getpid()}")
        temporary.write_text(script, encoding="utf-8")
        os.chmod(temporary, 0o700)
        os.replace(temporary, wrapper)
    installed["desktop_wrapper"] = str(wrapper)
    return installed, frozenset(updated)


def _installed_service_path(definition: str) -> str | None:
    try:
        with Path(definition).open("rb") as handle:
            service = plistlib.load(handle)
        value = service.get("EnvironmentVariables", {}).get("PATH")
        return value if isinstance(value, str) else None
    except (OSError, plistlib.InvalidFileException, AttributeError):
        return None


def _launchctl_text(output: bytes | str | None) -> str:
    return (
        output.decode(errors="replace") if isinstance(output, bytes) else output or ""
    )


def _loaded_service_path(output: bytes | str | None) -> str | None:
    text = _launchctl_text(output)
    in_environment = False
    for line in text.splitlines():
        if line == "\tenvironment = {":
            in_environment = True
        elif in_environment and line == "\t}":
            return None
        elif in_environment and line.startswith("\t\tPATH => "):
            return line.removeprefix("\t\tPATH => ")
    return None


def _service_observation_from_result(
    label: str, result: subprocess.CompletedProcess[Any]
) -> ServiceObservation:
    text = _launchctl_text(result.stdout if result.returncode == 0 else result.stderr)
    state: str | None = None
    pid: int | None = None
    working_directory: str | None = None
    arguments: list[str] = []
    in_arguments = False
    for line in text.splitlines():
        if line == "\targuments = {":
            in_arguments = True
            continue
        if in_arguments:
            if line == "\t}":
                in_arguments = False
            elif line.startswith("\t\t"):
                arguments.append(line[2:])
            continue
        if line.startswith("\t\t") or not line.startswith("\t"):
            continue
        field = line[1:]
        if field.startswith("state = ") and state is None:
            state = field.removeprefix("state = ")
        elif field.startswith("pid = ") and pid is None:
            try:
                pid = int(field.removeprefix("pid = "))
            except ValueError:
                pid = None
        elif field.startswith("working directory = "):
            working_directory = field.removeprefix("working directory = ")
    return ServiceObservation(
        label=label,
        loaded=result.returncode == 0,
        state=state,
        pid=pid,
        executable_path=_loaded_service_path(result.stdout),
        program_arguments=tuple(arguments),
        working_directory=working_directory,
        detail=text.strip(),
    )


def inspect_service(label: str) -> ServiceObservation:
    domain = f"gui/{os.getuid()}"
    result = subprocess.run(
        ["launchctl", "print", f"{domain}/{label}"],
        capture_output=True,
        check=False,
    )
    return _service_observation_from_result(label, result)


def _endpoint_is_ready(endpoint: str) -> bool:
    ready_url = (
        endpoint.replace("ws://", "http://", 1)
        .replace("wss://", "https://", 1)
        .rstrip("/")
        + "/readyz"
    )
    try:
        with urllib.request.urlopen(ready_url, timeout=1) as response:
            return response.status == 200
    except Exception:
        return False


def _wait_for_running_service(label: str, *, timeout: float = 10) -> ServiceObservation:
    deadline = time.monotonic() + timeout
    consecutive = 0
    latest = inspect_service(label)
    while time.monotonic() < deadline:
        latest = inspect_service(label)
        if latest.running:
            consecutive += 1
            if consecutive >= 2:
                return latest
        else:
            consecutive = 0
        time.sleep(0.2)
    return latest


def _wait_for_unloaded_service(
    label: str, *, timeout: float = 10
) -> ServiceObservation:
    """Wait until launchd has finished removing a booted-out job."""

    deadline = time.monotonic() + timeout
    latest = inspect_service(label)
    while latest.loaded and time.monotonic() < deadline:
        time.sleep(0.2)
        latest = inspect_service(label)
    return latest


def verify_runtime_ownership_or_availability(endpoint: str) -> ServiceObservation:
    """Reject a ready app-server endpoint not owned by the configured launchd job."""

    observation = inspect_service(APP_SERVER_LABEL)
    if _endpoint_is_ready(endpoint) and not observation.running:
        raise InstallationError(
            f"refusing to accept an unmanaged listener at {endpoint}; "
            f"{APP_SERVER_LABEL} is not the running owner"
        )
    return observation


def start_services(
    definitions: dict[str, str],
    *,
    updated: frozenset[str],
    app_server_endpoint: str | None = None,
) -> None:
    domain = f"gui/{os.getuid()}"
    initial_app_server = (
        verify_runtime_ownership_or_availability(app_server_endpoint)
        if app_server_endpoint is not None
        else inspect_service(APP_SERVER_LABEL)
    )
    for label in (APP_SERVER_LABEL, CONTROLLER_LABEL):
        observation = (
            initial_app_server if label == APP_SERVER_LABEL else inspect_service(label)
        )
        loaded = observation.loaded
        expected_path = _installed_service_path(definitions[label])
        loaded_path = observation.executable_path
        reloading = loaded and (
            label in updated
            or (expected_path is not None and loaded_path != expected_path)
        )
        if reloading:
            result = subprocess.run(
                ["launchctl", "bootout", f"{domain}/{label}"],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                raise InstallationError(
                    f"could not unload stale {label}: "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )
            stopped = _wait_for_unloaded_service(label)
            if stopped.loaded:
                raise InstallationError(
                    f"configured service {label} did not finish unloading; "
                    f"domain={domain}; state={stopped.state!r}; "
                    f"pid={stopped.pid!r}; detail={stopped.detail!r}"
                )
            loaded = False
            if (
                label == APP_SERVER_LABEL
                and app_server_endpoint is not None
                and _endpoint_is_ready(app_server_endpoint)
            ):
                raise InstallationError(
                    f"{APP_SERVER_LABEL} stopped but {app_server_endpoint} remains "
                    "occupied; refusing to start competing runtimes"
                )
        if not loaded:
            result = subprocess.run(
                ["launchctl", "bootstrap", domain, definitions[label]],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                observed = subprocess.run(
                    ["launchctl", "print", f"{domain}/{label}"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if observed.returncode != 0:
                    retried = subprocess.run(
                        ["launchctl", "bootstrap", domain, definitions[label]],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                else:
                    retried = observed
                if retried.returncode != 0:
                    raise InstallationError(
                        "could not establish configured service "
                        f"{label}; domain={domain}; plist={definitions[label]}; "
                        f"first_stdout={result.stdout.strip()!r}; "
                        f"first_stderr={result.stderr.strip()!r}; "
                        f"observed_state={observed.stdout.strip() or observed.stderr.strip()!r}; "
                        f"retry_stdout={retried.stdout.strip()!r}; "
                        f"retry_stderr={retried.stderr.strip()!r}"
                    )
        else:
            result = subprocess.run(
                ["launchctl", "kickstart", f"{domain}/{label}"],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                raise InstallationError(
                    f"could not kickstart {label}: "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )
        verified = _wait_for_running_service(label)
        if not verified.running:
            raise InstallationError(
                f"configured service {label} is not stably running after setup; "
                f"domain={domain}; plist={definitions[label]}; "
                f"loaded={verified.loaded}; state={verified.state!r}; "
                f"pid={verified.pid!r}; detail={verified.detail!r}"
            )
        verified_path = verified.executable_path
        if expected_path is not None and verified_path != expected_path:
            raise InstallationError(
                f"configured service {label} loaded with an unexpected PATH; "
                f"expected={expected_path!r}; observed={verified_path!r}"
            )
