"""One complete check, including cleanup, within a fixed wall-clock budget."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time

BUDGET_SECONDS = 55.0
CLEANUP_SECONDS = 2.0
ROOT = Path(__file__).resolve().parents[1]


def stop_group(process: subprocess.Popen[bytes]) -> None:
    # All children inherit this group, including type-check workers. Kill the
    # entire group even if its original parent already exited.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=CLEANUP_SECONDS)


def run_phase(name: str, command: list[str], deadline: float) -> int:
    started = time.monotonic()
    remaining = deadline - started - CLEANUP_SECONDS
    if remaining <= 0:
        print(f"{name}: check deadline exhausted", file=sys.stderr, flush=True)
        return 124
    print(f"\n{name}", flush=True)
    process = subprocess.Popen(command, cwd=ROOT, process_group=0)
    try:
        code = process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        print(f"{name}: check exceeded its wall-clock budget", file=sys.stderr)
        code = 124
    finally:
        stop_group(process)
        print(f"{name}: {time.monotonic() - started:.2f}s", flush=True)
    return code


def main() -> int:
    started = time.monotonic()
    deadline = started + BUDGET_SECONDS
    # Pin import resolution to this checkout even when using a prepared
    # environment shared with another worktree.
    os.environ["PYTHONPATH"] = os.pathsep.join((str(ROOT / "src"), str(ROOT)))
    os.environ["PATH"] = os.pathsep.join(
        (str(Path(sys.executable).parent), os.environ.get("PATH", ""))
    )
    phases = [
        (
            "Formatting",
            [
                sys.executable,
                "-m",
                "black",
                "--check",
                "src",
                "tests",
                "scripts/check.py",
                "scripts/run-tests.py",
            ],
        ),
        (
            "Types",
            [
                sys.executable,
                "-m",
                "pyre_check.client.pyre",
                "--noninteractive",
                "check",
            ],
        ),
        ("Tests", [sys.executable, str(ROOT / "scripts/run-tests.py")]),
    ]
    try:
        for name, command in phases:
            code = run_phase(name, command, deadline)
            if code:
                return code
        return 0
    finally:
        print(
            f"\nComplete check: {time.monotonic() - started:.2f}s / {BUDGET_SECONDS:.0f}s",
            flush=True,
        )


if __name__ == "__main__":
    raise SystemExit(main())
