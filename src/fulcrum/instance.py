"""Instance discovery and the single brain-root writer lock."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import Any, Mapping

import yaml

from fulcrum.contracts import FulcrumError, InstanceContext

DEFAULT_INSTANCE: Path = Path.home() / "Library" / "Application Support" / "Fulcrum"
DEFAULT_BRAIN: Path = Path.home() / "brain"


def _absolute(value: str | Path, field: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise FulcrumError.invalid(
            "INVALID_PATH",
            f"{field} must be an absolute path",
            details={"field": field},
        )
    return path.resolve(strict=False)


def _brain_from_config(path: Path) -> Path:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise FulcrumError(
            "CONFIG_NOT_FOUND",
            f"authoritative configuration does not exist: {path}",
            exit_code=4,
            next_command=("fulcrum", "config", "validate", "--config", str(path)),
        ) from error
    except (OSError, yaml.YAMLError) as error:
        raise FulcrumError(
            "CONFIG_INVALID",
            f"cannot read authoritative configuration {path}: {error}",
            exit_code=4,
            next_command=("fulcrum", "config", "validate", "--config", str(path)),
        ) from error
    if not isinstance(loaded, dict):
        raise FulcrumError(
            "CONFIG_INVALID",
            "authoritative configuration must be a YAML mapping",
            exit_code=4,
        )
    brain: Any = loaded.get("brain")
    root = brain.get("root") if isinstance(brain, dict) else loaded.get("brain_root")
    if not isinstance(root, str) or not root:
        raise FulcrumError(
            "CONFIG_INVALID",
            "authoritative configuration is missing brain.root",
            exit_code=4,
            details={"field": "brain.root"},
        )
    return _absolute(root, "brain.root")


def resolve_instance(
    *,
    instance: str | None,
    config: str | None,
    environ: Mapping[str, str] | None = None,
    allow_broken_config: bool = False,
) -> InstanceContext:
    env = os.environ if environ is None else environ
    raw_instance = instance or env.get("FULCRUM_INSTANCE")
    explicit = instance is not None or "FULCRUM_INSTANCE" in env or config is not None
    instance_root = _absolute(raw_instance or DEFAULT_INSTANCE, "instance")

    if config is not None:
        config_path = _absolute(config, "config")
    else:
        configured = instance_root / "config"
        if configured.exists() or configured.is_symlink():
            config_path = configured.resolve(strict=False)
        elif explicit:
            config_path = configured
        else:
            config_path = (DEFAULT_BRAIN / "fulcrum.yaml").resolve(strict=False)

    brain_root: Path | None
    try:
        brain_root = _brain_from_config(config_path)
    except FulcrumError:
        if not allow_broken_config:
            raise
        brain_root = None
    lock_path = brain_root / ".fulcrum-controller.lock" if brain_root else None
    return InstanceContext(
        instance_root=instance_root,
        config_path=config_path,
        brain_root=brain_root,
        socket_path=instance_root / "controller.sock",
        lock_path=lock_path,
        explicit_selection=explicit,
    )


class WriterLock:
    """An advisory lock whose open descriptor is retained for its lifetime."""

    def __init__(self, path: Path) -> None:
        self.path: Path = path.resolve(strict=False)
        self._descriptor: int | None = None

    def acquire(self) -> WriterLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(descriptor)
            raise FulcrumError(
                "WRITER_BUSY",
                f"another Fulcrum writer holds {self.path}",
                exit_code=4,
                retryable=True,
                next_command=("fulcrum", "service", "status", "--json"),
            ) from error
        self._descriptor = descriptor
        return self

    def release(self) -> None:
        descriptor = self._descriptor
        if descriptor is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            self._descriptor = None

    def __enter__(self) -> WriterLock:
        return self.acquire()

    def __exit__(self, *_: object) -> None:
        self.release()
