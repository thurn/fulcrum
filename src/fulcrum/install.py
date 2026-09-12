"""Repeatable installation from retained, certified Fulcrum source."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

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
SETUP_SKILLS = ("fulcrum-setup",)
SHARED_SKILLS = ("fulcrum-shared",)
LINKED_SKILLS: tuple[str, ...] = (
    tuple(f"fulcrum-{role}" for role in ROLES) + SETUP_SKILLS + SHARED_SKILLS
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
    git_root = Path(_run_git(root, "rev-parse", "--show-toplevel")).resolve()
    if git_root != root:
        raise InstallationError(
            f"source root must be the retained Git repository root ({git_root})"
        )
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


def _install_directory_link(source: Path, target: Path) -> None:
    source = source.resolve(strict=True)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.is_symlink():
        if target.resolve(strict=False) == source:
            return
        raise InstallationError(f"refusing to replace unrelated symlink: {target}")
    if target.exists():
        raise InstallationError(
            f"link target already exists and is not the expected symlink: {target}"
        )
    temporary = target.parent / f".{target.name}.fulcrum-link-{os.getpid()}"
    temporary.unlink(missing_ok=True)
    try:
        temporary.symlink_to(source, target_is_directory=True)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_link_target(source: Path, target: Path) -> None:
    expected = source.resolve(strict=True)
    if target.is_symlink() and target.resolve(strict=False) != expected:
        raise InstallationError(f"refusing to replace unrelated symlink: {target}")
    if target.exists() and not target.is_symlink():
        raise InstallationError(
            f"link target already exists and is not the expected symlink: {target}"
        )


def install_links(
    source_root: Path, skills_root: Path, hooks_config: Path
) -> tuple[list[Path], Path, Path]:
    """Link Codex-visible Fulcrum assets directly to the retained Git checkout."""

    codex_root = skills_root.parent.expanduser().absolute()
    if hooks_config.parent.resolve(strict=False) != codex_root.resolve(strict=False):
        raise InstallationError("skills and hooks must use the same Codex home")
    links: list[tuple[Path, Path]] = []
    skill_links: list[Path] = []
    for name in LINKED_SKILLS:
        source = source_root / "skills" / name
        if not source.is_dir():
            raise InstallationError(f"missing Fulcrum skill directory: {source}")
        if name != "fulcrum-shared" and not (source / "SKILL.md").is_file():
            raise InstallationError(f"missing Fulcrum skill: {source / 'SKILL.md'}")
        target = skills_root / name
        links.append((source, target))
        skill_links.append(target)

    hook_source = source_root / "hooks"
    hook_command = hook_source / "fulcrum-hook"
    if not hook_command.is_file() or not os.access(hook_command, os.X_OK):
        raise InstallationError(f"missing executable Fulcrum hook: {hook_command}")
    hook_link = codex_root / "hooks" / "fulcrum"
    links.append((hook_source, hook_link))
    for source, target in links:
        _validate_link_target(source, target)
    for source, target in links:
        _install_directory_link(source, target)
    return skill_links, hook_link, hook_link / "fulcrum-hook"


def _load_installation(config_file: Path) -> InstallationRecord | None:
    if not config_file.is_file():
        return None
    try:
        raw: object = json.loads(config_file.read_bytes())
    except json.JSONDecodeError as error:
        raise InstallationError(
            f"invalid installation record; preserved: {error}"
        ) from error
    if not isinstance(raw, dict) or raw.get("record_kind") != "installation":
        raise InstallationError(
            "unsupported installation record; preserved without changes"
        )
    version = raw.get("schema_version")
    if version != 1:
        raise InstallationError(
            f"unsupported installation schema {version!r}; preserved without changes"
        )
    loaded = load_record(config_file)
    return cast(InstallationRecord, loaded)


def install_runtime(
    *,
    paths: RuntimePaths,
    source_root: Path,
    certified_revision: str,
    skills_root: Path,
    hooks_config: Path,
    host_id: str,
    expected_brain_remote: str,
    sage_anchor: str,
    codex_projects_verified_at: str | None = None,
    watchman_schedule_id: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Install assets and config without touching the brain or active run records."""

    observed_at = now or utc_now()
    verify_certified_source(source_root, certified_revision)
    current = _load_installation(paths.config_file)
    if current is not None:
        if Path(current["brain_root"]).resolve() != paths.brain_root.resolve():
            raise InstallationError("update would change the configured brain root")
        if Path(current["state_root"]).resolve() != paths.state_root.resolve():
            raise InstallationError("update would change the configured state root")
    skill_links, hook_link, hook_command = install_links(
        source_root, skills_root, hooks_config
    )
    previous_hook_config = hooks_config.read_bytes() if hooks_config.is_file() else None
    install_hook_source(hooks_config, hook_command)
    hook_config_changed = previous_hook_config != hooks_config.read_bytes()
    observations = dict(current["observations"]) if current is not None else {}
    observations.pop("skill_revision", None)
    observations.pop("package_version", None)
    observations.pop("source_revision", None)
    observations.update(
        {
            "installed_source_root": str(source_root.resolve()),
            "skills_root": str(skills_root.resolve(strict=False)),
            "hook_link": str(hook_link),
            "brain_remote": expected_brain_remote,
            "sage_cadence_anchor": sage_anchor,
            "hook_source": str(hooks_config.resolve()),
        }
    )
    if hook_config_changed:
        observations["hook_trust"] = "review_required"
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
        "skill_links": [str(path) for path in skill_links],
        "hook_link": str(hook_link),
        "hook_source": str(hooks_config.resolve()),
        "hook_retrust_required": hook_config_changed,
        "database_restarted": False,
    }
