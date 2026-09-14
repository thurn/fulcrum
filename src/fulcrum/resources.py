"""Descriptor telemetry and admission policy for the shared Codex runtime."""

from __future__ import annotations

import resource
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fulcrum.install import APP_SERVER_LABEL, inspect_service

# A start is refused while it could reduce the app-server below this reserve.
# The stress regression exercises 100 sequential subprocess starts while only
# this reserve remains.  Eight additional descriptors cover the observed
# thread/turn-start request, response, and notification burst.
DESCRIPTOR_RESERVE = 64
DESCRIPTOR_START_ALLOWANCE = 8
MAX_ACTIVE_CONVERSATIONS = 4
MAX_IDLE_WORKER_CONVERSATIONS = 4


class ResourceProbeError(RuntimeError):
    """The app-server resource owner or its descriptor state is unavailable."""


@dataclass(frozen=True)
class ResourceSnapshot:
    """One bounded observation of the process that owns native conversations."""

    process_id: int
    soft_limit: int
    descriptor_count: int
    child_count: int
    descriptor_types: dict[str, int]
    session_descriptor_count: int = 0
    locked_descriptor_count: int = 0

    @property
    def available_descriptors(self) -> int:
        return max(0, self.soft_limit - self.descriptor_count)

    def admits_start(self) -> bool:
        return (
            self.available_descriptors
            >= DESCRIPTOR_RESERVE + DESCRIPTOR_START_ALLOWANCE
        )

    def detail(self) -> dict[str, Any]:
        return {
            "process_id": self.process_id,
            "soft_limit": self.soft_limit,
            "descriptor_count": self.descriptor_count,
            "available_descriptors": self.available_descriptors,
            "required_reserve": DESCRIPTOR_RESERVE,
            "start_allowance": DESCRIPTOR_START_ALLOWANCE,
            "child_count": self.child_count,
            "descriptor_types": dict(sorted(self.descriptor_types.items())),
            "session_descriptor_count": self.session_descriptor_count,
            "locked_descriptor_count": self.locked_descriptor_count,
        }


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _is_thread_writer_lock(path: str) -> bool:
    """Identify the per-thread lock files held by native Codex conversations."""

    normalized = path.removesuffix(" (deleted)").replace("\\", "/")
    return "/thread-writer-locks/" in normalized and normalized.endswith(".lock")


def _parse_lsof_descriptors(
    output: str,
) -> tuple[set[int], dict[int, str], set[int], set[int]]:
    """Parse lsof field output without relying on its optional lock-status field."""

    descriptors: set[int] = set()
    descriptor_types: dict[int, str] = {}
    session_descriptors: set[int] = set()
    locked_descriptors: set[int] = set()
    current: int | None = None
    for line in output.splitlines():
        if line.startswith("f"):
            current = int(line[1:]) if line[1:].isdecimal() else None
            if current is not None:
                descriptors.add(current)
        elif current is not None and line.startswith("t") and len(line) > 1:
            descriptor_types[current] = line[1:]
        elif current is not None and line.startswith("l") and line[1:].strip():
            locked_descriptors.add(current)
        elif current is not None and line.startswith("n"):
            path = line[1:]
            if "/sessions/" in path:
                session_descriptors.add(current)
            if _is_thread_writer_lock(path):
                locked_descriptors.add(current)
    return (
        descriptors,
        descriptor_types,
        session_descriptors,
        locked_descriptors,
    )


class AppServerResourceProbe:
    """Observe the launchd-owned app-server without opening files in that process."""

    def __init__(
        self,
        *,
        pid_provider: Callable[[], int | None] | None = None,
        runner: Runner = subprocess.run,
    ) -> None:
        self.pid_provider: Callable[[], int | None] = pid_provider or self._service_pid
        self.runner: Runner = runner

    @staticmethod
    def _service_pid() -> int | None:
        observation = inspect_service(APP_SERVER_LABEL)
        return observation.pid if observation.running else None

    def snapshot(self) -> ResourceSnapshot:
        try:
            pid = self.pid_provider()
            if pid is None:
                raise ResourceProbeError("configured app-server process is not running")
            soft_limit = self._soft_limit(pid)
            descriptors, types, sessions, locked = self._descriptors(pid)
            return ResourceSnapshot(
                process_id=pid,
                soft_limit=soft_limit,
                descriptor_count=len(descriptors),
                child_count=self._child_count(pid),
                descriptor_types=dict(Counter(types.values())),
                session_descriptor_count=len(sessions),
                locked_descriptor_count=len(locked),
            )
        except ResourceProbeError:
            raise
        except (OSError, subprocess.SubprocessError) as error:
            raise ResourceProbeError(
                f"cannot inspect app-server process resources: {error}"
            ) from error

    def _soft_limit(self, pid: int) -> int:
        limits = Path(f"/proc/{pid}/limits")
        if limits.is_file():
            for line in limits.read_text().splitlines():
                if line.startswith("Max open files"):
                    value = line.removeprefix("Max open files").split()[0]
                    if value.isdecimal():
                        return int(value)
        completed = self.runner(
            ["/bin/launchctl", "limit", "maxfiles"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if completed.returncode == 0:
            values = [value for value in completed.stdout.split() if value.isdecimal()]
            if values:
                return int(values[0])
        # This is accurate for the normal launchd topology and remains a safe
        # fallback for foreground diagnostics where launchctl is unavailable.
        return int(resource.getrlimit(resource.RLIMIT_NOFILE)[0])

    def _descriptors(
        self, pid: int
    ) -> tuple[set[int], dict[int, str], set[int], set[int]]:
        proc = Path(f"/proc/{pid}/fd")
        if proc.is_dir():
            descriptors = {
                int(item.name) for item in proc.iterdir() if item.name.isdecimal()
            }
            sessions: set[int] = set()
            locked: set[int] = set()
            for descriptor in descriptors:
                try:
                    target = (proc / str(descriptor)).readlink()
                except OSError:
                    continue
                path = str(target)
                if "/sessions/" in path:
                    sessions.add(descriptor)
                if _is_thread_writer_lock(path):
                    locked.add(descriptor)
            return (
                descriptors,
                {descriptor: "unknown" for descriptor in descriptors},
                sessions,
                locked,
            )
        completed = self.runner(
            ["/usr/sbin/lsof", "-nP", "-a", "-p", str(pid), "-Ffltn"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if completed.returncode != 0:
            raise ResourceProbeError(
                f"cannot inspect app-server descriptors: {completed.stderr.strip()}"
            )
        return _parse_lsof_descriptors(completed.stdout)

    def _child_count(self, pid: int) -> int:
        if Path("/proc").is_dir():
            count = 0
            for status in Path("/proc").glob("[0-9]*/status"):
                try:
                    parent = next(
                        line
                        for line in status.read_text().splitlines()
                        if line.startswith("PPid:")
                    )
                except (OSError, StopIteration):
                    continue
                if parent.split()[1] == str(pid):
                    count += 1
            return count
        completed = self.runner(
            ["/bin/ps", "-axo", "ppid="],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if completed.returncode != 0:
            raise ResourceProbeError(
                f"cannot inspect app-server children: {completed.stderr.strip()}"
            )
        return sum(
            1
            for value in completed.stdout.splitlines()
            if value.strip().isdecimal() and int(value) == pid
        )


def bounded_condition(prefix: str, detail: dict[str, Any]) -> str:
    """Return a stable actionable condition suitable for retries and status."""

    return (
        f"{prefix}: app-server uses {detail['descriptor_count']}/"
        f"{detail['soft_limit']} descriptors with {detail['child_count']} direct "
        f"children; keep {detail['required_reserve']} free plus "
        f"{detail['start_allowance']} for a start. Fulcrum is parking safe idle "
        "worker conversations; wait for reclamation or finish/archive unrelated "
        "Codex conversations."
    )
