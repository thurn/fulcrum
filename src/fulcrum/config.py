"""Configuration and path resolution for local Fulcrum data."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, cast

from fulcrum.records import InstallationRecord, load_record


class ConfigurationError(ValueError):
    """Configuration contains an unusable path or record."""


@dataclass(frozen=True)
class RuntimePaths:
    """Resolved paths used by one Fulcrum invocation."""

    brain_root: Path
    state_root: Path
    config_file: Path

    @property
    def logs_root(self) -> Path:
        return self.state_root / "logs"

    @property
    def observations_root(self) -> Path:
        return self.state_root / "observations"


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
    """Resolve a child path and reject separators or traversal in identifiers."""

    for part in parts:
        if not part or part in {".", ".."} or Path(part).name != part:
            raise ConfigurationError(f"unsafe path component: {part!r}")
    resolved_root = root.resolve(strict=False)
    candidate = resolved_root.joinpath(*parts).resolve(strict=False)
    if not candidate.is_relative_to(resolved_root):
        raise ConfigurationError(f"path escapes configured root: {candidate}")
    return candidate


def resolve_paths(
    *,
    brain_override: str | Path | None = None,
    state_override: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    user_home: Path | None = None,
) -> RuntimePaths:
    """Resolve CLI, environment, configuration, and default paths in order."""

    environment = os.environ if environ is None else environ
    home = (Path.home() if user_home is None else user_home).resolve(strict=False)
    default_state = home / "Library" / "Application Support" / "Fulcrum"
    config_file = _expand_path(
        environment.get("FULCRUM_CONFIG", default_state / "config.json"), home
    )

    configured: InstallationRecord | None = None
    if config_file.is_file():
        record = load_record(config_file)
        if record["record_kind"] != "installation":
            raise ConfigurationError(
                f"{config_file}: expected installation record, got "
                f"{record['record_kind']!r}"
            )
        configured = cast(InstallationRecord, record)

    configured_brain = (
        configured["brain_root"] if configured is not None else home / "brain"
    )
    configured_state = (
        configured["state_root"] if configured is not None else default_state
    )

    brain_value: str | Path = (
        brain_override or environment.get("FULCRUM_BRAIN_ROOT") or configured_brain
    )
    state_value: str | Path = (
        state_override or environment.get("FULCRUM_STATE_ROOT") or configured_state
    )
    return RuntimePaths(
        brain_root=_expand_path(brain_value, home),
        state_root=_expand_path(state_value, home),
        config_file=config_file,
    )
