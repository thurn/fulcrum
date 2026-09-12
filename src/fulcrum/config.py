"""Static installation configuration and resettable runtime paths."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping


class ConfigurationError(ValueError):
    """Installation configuration is incomplete or unsafe."""


@dataclass(frozen=True)
class ProjectConfig:
    """Native identities and delivery policy for one enrolled repository."""

    project_id: str
    repo_path: str
    codex_project_id: str | None = None
    tollgate_repo_id: str | None = None
    validation_command: list[str] = field(default_factory=list)
    source_remote: str | None = None
    enabled: bool = True


@dataclass(frozen=True)
class InstallationConfig:
    """Configuration retained across an operational reset."""

    source_root: str
    brain_root: str
    state_root: str
    codex_bin: str
    desktop_executable: str
    app_server_endpoint: str = "ws://127.0.0.1:4500"
    host_id: str = "local"
    archon_model: str | None = None
    archon_reasoning_effort: str | None = None
    turn_check_after_seconds: int = 1800
    projects: list[ProjectConfig] = field(default_factory=list)
    brain_remote: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RuntimePaths:
    """All paths selected for one Fulcrum environment."""

    brain_root: Path
    state_root: Path
    config_file: Path
    control_root: Path

    @property
    def database(self) -> Path:
        return self.state_root / "fulcrum.sqlite3"

    @property
    def socket(self) -> Path:
        return self.control_root / "controller.sock"

    @property
    def lock(self) -> Path:
        return self.control_root / "controller.lock"

    @property
    def reboot_record(self) -> Path:
        return self.control_root / "reboot.json"

    @property
    def logs_root(self) -> Path:
        return self.state_root / "logs"


def _expand_path(raw: str | Path, home: Path) -> Path:
    text = str(raw)
    if not text or "\x00" in text:
        raise ConfigurationError(f"invalid path value {text!r}")
    if text == "~":
        candidate = home
    elif text.startswith("~/"):
        candidate = home / text[2:]
    elif text.startswith("~"):
        raise ConfigurationError(f"named-home expansion is unsupported: {text!r}")
    else:
        candidate = Path(text)
    if not candidate.is_absolute():
        raise ConfigurationError(f"path must be absolute: {text!r}")
    return candidate.resolve(strict=False)


def safe_child(root: Path, *parts: str) -> Path:
    """Resolve a child path without permitting traversal in identifiers."""

    for part in parts:
        if not part or part in {".", ".."} or Path(part).name != part:
            raise ConfigurationError(f"unsafe path component: {part!r}")
    base = root.resolve(strict=False)
    child = base.joinpath(*parts).resolve(strict=False)
    if not child.is_relative_to(base):
        raise ConfigurationError(f"path escapes configured root: {child}")
    return child


def _default_config(home: Path, state_root: Path) -> InstallationConfig:
    return InstallationConfig(
        source_root=str(Path(__file__).resolve().parents[2]),
        brain_root=str(home / "brain"),
        state_root=str(state_root),
        codex_bin=str(home / ".local" / "bin" / "codex"),
        desktop_executable="/Applications/ChatGPT.app/Contents/MacOS/ChatGPT",
    )


def load_installation(path: Path, *, home: Path | None = None) -> InstallationConfig:
    """Load ordinary configuration, or return safe discoverable defaults."""

    actual_home = (home or Path.home()).resolve(strict=False)
    default_state = actual_home / "Library" / "Application Support" / "Fulcrum"
    defaults = _default_config(actual_home, default_state)
    if not path.is_file():
        return defaults
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError(
            f"cannot read configuration {path}: {error}"
        ) from error
    if not isinstance(raw, dict):
        raise ConfigurationError(f"configuration {path} must contain an object")
    if "record_kind" in raw or "schema_version" in raw:
        raise ConfigurationError(
            f"legacy configuration at {path} is unsupported; run fulcrum setup"
        )
    projects_raw = raw.pop("projects", [])
    if not isinstance(projects_raw, list):
        raise ConfigurationError("projects must be a list")
    try:
        projects = [ProjectConfig(**item) for item in projects_raw]
        return InstallationConfig(projects=projects, **raw)
    except (TypeError, ValueError) as error:
        raise ConfigurationError(f"invalid configuration {path}: {error}") from error


def save_installation(path: Path, config: InstallationConfig) -> None:
    """Durably replace static installation configuration."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(config.to_json(), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def resolve_paths(
    *,
    brain_override: str | Path | None = None,
    state_override: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    user_home: Path | None = None,
) -> RuntimePaths:
    """Resolve explicit overrides, environment, saved config, then defaults."""

    env = os.environ if environ is None else environ
    home = (user_home or Path.home()).resolve(strict=False)
    default_root = home / "Library" / "Application Support" / "Fulcrum"
    config_file = _expand_path(
        env.get("FULCRUM_CONFIG", default_root / "config.json"), home
    )
    config = load_installation(config_file, home=home)
    brain = brain_override or env.get("FULCRUM_BRAIN_ROOT") or config.brain_root
    state = state_override or env.get("FULCRUM_STATE_ROOT") or config.state_root
    state_root = _expand_path(state, home)
    control_override = env.get("FULCRUM_CONTROL_ROOT")
    control_root = (
        _expand_path(control_override, home)
        if control_override
        else config_file.parent / "control"
    )
    return RuntimePaths(
        brain_root=_expand_path(brain, home),
        state_root=state_root,
        config_file=config_file,
        control_root=control_root.resolve(strict=False),
    )
