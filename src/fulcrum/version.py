"""Version reporting with an optional source revision."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from fulcrum import __version__


def source_revision() -> str | None:
    """Return the installed source revision when it can be resolved safely."""

    supplied = os.environ.get("FULCRUM_BUILD_REVISION")
    if supplied:
        return supplied

    package_path = Path(__file__).resolve()
    for parent in package_path.parents:
        if not (parent / ".git").exists():
            continue
        try:
            result = subprocess.run(
                ["git", "-C", str(parent), "rev-parse", "--verify", "HEAD"],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError:
            return None
        if result.returncode == 0:
            revision = result.stdout.strip()
            return revision or None
    return None


def version_text() -> str:
    """Return human-readable package and source version information."""

    revision = source_revision()
    suffix = f" (revision {revision})" if revision else ""
    return f"fulcrum {__version__}{suffix}"
