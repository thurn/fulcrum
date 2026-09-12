"""Destructive reset primitives with exact-root safety checks."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


class ResetError(RuntimeError):
    pass


def _run(command: list[str], *, cwd: Path) -> str:
    result = subprocess.run(
        command, cwd=cwd, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise ResetError(
            result.stderr.strip() or result.stdout.strip() or f"failed: {command[0]}"
        )
    return result.stdout.strip()


def reset_brain(
    brain_root: Path, *, source_root: Path, synchronize_remote: bool = True
) -> dict[str, Any]:
    """Clear selected brain content, Git history, and Beads history."""

    root = brain_root.resolve(strict=True)
    source = source_root.resolve(strict=True)
    home = Path.home().resolve(strict=True)
    if root in {source, home, Path("/")} or source.is_relative_to(root):
        raise ResetError(f"refusing unsafe brain reset target {root}")
    top = Path(_run(["git", "rev-parse", "--show-toplevel"], cwd=root)).resolve()
    if top != root:
        raise ResetError(f"brain root must be the exact Git repository root: {top}")
    branch = _run(["git", "symbolic-ref", "--short", "HEAD"], cwd=root)
    remote = None
    remote_result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if remote_result.returncode == 0:
        remote = remote_result.stdout.strip()
    dolt_remotes: list[dict[str, Any]] = []
    bd = shutil.which("bd")
    if bd and (root / ".beads").exists():
        listed = subprocess.run(
            [bd, "--json", "-C", str(root), "dolt", "remote", "list"],
            capture_output=True,
            text=True,
            check=False,
        )
        if listed.returncode == 0:
            try:
                raw = json.loads(listed.stdout)
                if isinstance(raw, list):
                    dolt_remotes = [item for item in raw if isinstance(item, dict)]
            except json.JSONDecodeError:
                pass
        try:
            _run([bd, "-C", str(root), "admin", "reset", "--force"], cwd=root)
        except ResetError as error:
            if "not yet supported in embedded mode" not in str(error):
                raise
            shutil.rmtree(root / ".beads")
    if not dolt_remotes and isinstance(remote, str):
        dolt_url = _dolt_url(remote)
        if dolt_url:
            dolt_remotes = [{"name": "origin", "url": dolt_url}]
    if remote:
        _run(["git", "remote", "remove", "origin"], cwd=root)
    _run(["git", "checkout", "--orphan", "fulcrum-reset"], cwd=root)
    for child in root.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink(missing_ok=True)
    (root / "plans").mkdir(mode=0o700)
    (root / "memory").mkdir(mode=0o700)
    (root / "reports").mkdir(mode=0o700)
    (root / "NEWS.md").write_text("# Fulcrum News\n", encoding="utf-8")
    _run(["git", "add", "-A"], cwd=root)
    _run(
        ["git", "commit", "--allow-empty", "-m", "chore: reset Fulcrum brain"], cwd=root
    )
    _run(["git", "branch", "-M", branch], cwd=root)
    if bd:
        _run(
            [
                bd,
                "init",
                "--non-interactive",
                "--prefix",
                root.name,
                "--skip-agents",
                "--skip-hooks",
            ],
            cwd=root,
        )
        for item in dolt_remotes:
            name = item.get("name")
            url = item.get("url")
            if isinstance(name, str) and isinstance(url, str):
                _run(
                    [bd, "-C", str(root), "dolt", "remote", "add", name, url], cwd=root
                )
        if dolt_remotes and synchronize_remote:
            _run([bd, "-C", str(root), "dolt", "push", "--force"], cwd=root)
    if remote:
        _run(["git", "remote", "add", "origin", remote], cwd=root)
    if remote and synchronize_remote:
        _run(
            ["git", "push", "--force", "origin", f"HEAD:refs/heads/{branch}"], cwd=root
        )
    _run(["git", "reflog", "expire", "--expire=now", "--all"], cwd=root)
    _run(["git", "gc", "--prune=now"], cwd=root)
    return {
        "brain_root": str(root),
        "branch": branch,
        "git_remote": remote,
        "dolt_remotes": dolt_remotes,
    }


def _dolt_url(git_remote: str) -> str | None:
    if git_remote.startswith("git+ssh://"):
        return git_remote
    if git_remote.startswith("git@") and ":" in git_remote:
        host, path = git_remote.split(":", 1)
        return f"git+ssh://{host}/{path}"
    if git_remote.startswith("ssh://"):
        return "git+" + git_remote
    return None
