"""Repeatable installation from retained, certified Fulcrum source."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from fulcrum import __version__
from fulcrum.config import RuntimePaths
from fulcrum.hook_config import install_hook_source
from fulcrum.records import InstallationRecord, load_record, validate_record
from fulcrum.state import atomic_write_record

ROLES = (
    "archon",
    "executor",
    "inquisitor",
    "night-watchman",
    "overseer",
    "sage",
    "weaver",
)


class InstallationError(RuntimeError):
    """Installation inputs cannot produce a safe, repeatable installation."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _run_git(source_root: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(source_root), *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InstallationError(
            f"could not inspect source Git state: {error}"
        ) from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        raise InstallationError(f"could not inspect source Git state: {detail}")
    return result.stdout.strip()


def verify_certified_source(source_root: Path, revision: str) -> str:
    """Require an immutable revision in a retained checkout at release and origin."""

    root = source_root.resolve(strict=True)
    if ".worktrees" in root.parts:
        raise InstallationError("refusing to install from a disposable .worktrees path")
    actual = _run_git(root, "rev-parse", "HEAD")
    if actual != revision:
        raise InstallationError(f"source HEAD is {actual}; expected {revision}")
    for reference in ("refs/heads/release", "refs/remotes/origin/master"):
        resolved = _run_git(root, "rev-parse", "--verify", reference)
        if resolved != revision:
            raise InstallationError(
                f"certified source {revision} does not match {reference} ({resolved})"
            )
    return actual


def _skill_revision(source_root: Path) -> str:
    digest = hashlib.sha256()
    for role in ROLES:
        root = source_root / "skills" / role
        skill = root / "SKILL.md"
        if not skill.is_file():
            raise InstallationError(f"missing role skill: {skill}")
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            digest.update(str(path.relative_to(source_root)).encode())
            digest.update(path.read_bytes())
    shared = source_root / "skills" / "shared"
    if not shared.is_dir():
        raise InstallationError(f"missing shared skill references: {shared}")
    for path in sorted(item for item in shared.rglob("*") if item.is_file()):
        digest.update(str(path.relative_to(source_root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _atomic_copy(
    source: Path, target: Path, *, rewrite_shared_links: bool = False
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=target.parent, prefix=f".{target.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            data = source.read_bytes()
            if rewrite_shared_links and source.suffix == ".md":
                data = data.replace(b"../shared/", b"../fulcrum-shared/")
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def install_skills(source_root: Path, skills_root: Path) -> str:
    """Update only Fulcrum-owned role directories and keep unrelated skills."""

    revision = _skill_revision(source_root)
    for role in ROLES:
        source = source_root / "skills" / role
        target = skills_root / f"fulcrum-{role}"
        copied: set[Path] = set()
        for path in sorted(item for item in source.rglob("*") if item.is_file()):
            relative = path.relative_to(source)
            _atomic_copy(path, target / relative, rewrite_shared_links=True)
            copied.add(relative)
        manifest = target / ".fulcrum-files.json"
        previous: list[str] = []
        if manifest.is_file():
            try:
                value = json.loads(manifest.read_text(encoding="utf-8"))
                if isinstance(value, list):
                    previous = [item for item in value if isinstance(item, str)]
            except (OSError, json.JSONDecodeError):
                previous = []
        for name in previous:
            stale = target / name
            if stale.is_file() and Path(name) not in copied:
                stale.unlink()
        manifest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        manifest.write_text(
            json.dumps(sorted(str(path) for path in copied), indent=2) + "\n",
            encoding="utf-8",
        )
    shared_source = source_root / "skills" / "shared"
    shared_target = skills_root / "fulcrum-shared"
    for path in sorted(item for item in shared_source.rglob("*") if item.is_file()):
        _atomic_copy(path, shared_target / path.relative_to(shared_source))
    return revision


def _migration_backup(config_file: Path, original: bytes, now: str) -> Path:
    stamp = now.replace(":", "").replace("-", "")
    target = config_file.parent / "backups" / f"config-schema-v0-{stamp}.json"
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not target.exists():
        target.write_bytes(original)
        os.chmod(target, 0o600)
    return target


def _load_installation(
    config_file: Path, now: str
) -> tuple[InstallationRecord | None, Path | None]:
    if not config_file.is_file():
        return None, None
    original = config_file.read_bytes()
    try:
        raw: object = json.loads(original)
    except json.JSONDecodeError as error:
        raise InstallationError(
            f"invalid installation record; preserved: {error}"
        ) from error
    if not isinstance(raw, dict) or raw.get("record_kind") != "installation":
        raise InstallationError(
            "unsupported installation record; preserved without changes"
        )
    version = raw.get("schema_version")
    if version == 1:
        loaded = load_record(config_file)
        return cast(InstallationRecord, loaded), None
    if version != 0:
        raise InstallationError(
            f"unsupported installation schema {version!r}; preserved without changes"
        )
    required = {"brain_root", "state_root", "host_id"}
    if not required.issubset(raw):
        raise InstallationError("schema 0 installation is incomplete; preserved")
    backup = _migration_backup(config_file, original, now)
    converted = {
        "record_kind": "installation",
        "schema_version": 1,
        "writer_id": "setup",
        "updated_at": now,
        "brain_root": raw["brain_root"],
        "state_root": raw["state_root"],
        "host_id": raw["host_id"],
        "configured_services": raw.get("configured_services", []),
        "observations": raw.get("observations", {}),
    }
    return cast(InstallationRecord, validate_record(converted)), backup


def install_runtime(
    *,
    paths: RuntimePaths,
    source_root: Path,
    certified_revision: str,
    skills_root: Path,
    hooks_config: Path,
    hook_command: Path,
    host_id: str,
    expected_brain_remote: str,
    sage_anchor: str,
    codex_projects_verified_at: str | None = None,
    watchman_schedule_id: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Install assets and config without touching the brain or active run records."""

    observed_at = now or utc_now()
    verified_revision = verify_certified_source(source_root, certified_revision)
    current, backup = _load_installation(paths.config_file, observed_at)
    if current is not None:
        if Path(current["brain_root"]).resolve() != paths.brain_root.resolve():
            raise InstallationError("update would change the configured brain root")
        if Path(current["state_root"]).resolve() != paths.state_root.resolve():
            raise InstallationError("update would change the configured state root")
    skill_revision = install_skills(source_root, skills_root)
    install_hook_source(hooks_config, hook_command)
    observations = dict(current["observations"]) if current is not None else {}
    observations.update(
        {
            "package_version": __version__,
            "source_revision": verified_revision,
            "skill_revision": skill_revision,
            "installed_source_root": str(source_root.resolve()),
            "brain_remote": expected_brain_remote,
            "sage_cadence_anchor": sage_anchor,
            "hook_source": str(hooks_config.resolve()),
            "hook_trust": "review_required",
        }
    )
    if codex_projects_verified_at is not None:
        observations["codex_projects_verified_at"] = codex_projects_verified_at
    if watchman_schedule_id is not None:
        observations["watchman_schedule"] = "ready"
        observations["watchman_schedule_id"] = watchman_schedule_id
    services = set(current["configured_services"] if current is not None else [])
    services.update({"beads", "codex", "hooks", "tollgate"})
    record = cast(
        InstallationRecord,
        validate_record(
            {
                "record_kind": "installation",
                "schema_version": 1,
                "writer_id": "setup",
                "updated_at": observed_at,
                "brain_root": str(paths.brain_root),
                "state_root": str(paths.state_root),
                "host_id": host_id,
                "configured_services": sorted(services),
                "observations": observations,
            }
        ),
    )
    atomic_write_record(paths, record)
    return {
        "ok": True,
        "package_version": __version__,
        "source_revision": verified_revision,
        "skill_revision": skill_revision,
        "skills_installed": list(ROLES),
        "hook_source": str(hooks_config.resolve()),
        "hook_retrust_required": True,
        "migration_backup": str(backup) if backup is not None else None,
        "database_restarted": False,
    }
