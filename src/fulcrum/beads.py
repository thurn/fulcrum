"""Narrow native Beads adapter for task intake and completion."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class BeadsError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        stdout: str | None = None,
        stderr: str | None = None,
        returncode: int | None = None,
        duration_ms: int | None = None,
    ) -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.duration_ms = duration_ms


class BeadsUncertainError(BeadsError):
    pass


@dataclass(frozen=True)
class IntakeTask:
    intake_key: str
    project: str
    title: str
    description: str
    activation: str = "pending"
    dependencies: tuple[str, ...] = ()
    context: tuple[str, ...] = ()
    executor_model: str = "gpt-5.6-sol"
    executor_reasoning_effort: str = "high"
    overseer_model: str = "gpt-5.6-sol"
    overseer_reasoning_effort: str = "high"
    model_provenance: str = "default"
    plan_id: str | None = None
    plan_commit: str | None = None

    def validate(self) -> None:
        for name, value in (
            ("intake key", self.intake_key),
            ("project", self.project),
            ("title", self.title),
            ("description", self.description),
        ):
            if not value.strip():
                raise BeadsError(f"{name} is required")
        if self.activation not in {"pending", "future"}:
            raise BeadsError("activation must be pending or future")
        if self.plan_id is None and self.plan_commit is not None:
            raise BeadsError("plan commit requires a plan ID")


class Beads:
    def __init__(
        self, brain_root: Path, executable: str | None = None, *, timeout: int = 30
    ) -> None:
        self.brain_root = brain_root
        self.executable: str = executable or shutil.which("bd") or "bd"
        self.timeout = timeout

    def run(
        self,
        arguments: list[str],
        *,
        allow_failure: bool = False,
        mutating: bool = False,
    ) -> Any:
        command = [self.executable, "--json", "-C", str(self.brain_root), *arguments]
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise BeadsUncertainError(
                "Beads command timed out; result is uncertain",
                stdout=_text(error.stdout),
                stderr=_text(error.stderr),
                duration_ms=int((time.monotonic() - started) * 1000),
            ) from error
        except OSError as error:
            error_type = BeadsUncertainError if mutating else BeadsError
            raise error_type(
                f"could not run Beads: {error}",
                duration_ms=int((time.monotonic() - started) * 1000),
            ) from error
        duration_ms = int((time.monotonic() - started) * 1000)
        if completed.returncode != 0 and not allow_failure:
            detail = completed.stderr.strip() or completed.stdout.strip() or "no output"
            error_type = BeadsUncertainError if mutating else BeadsError
            raise error_type(
                f"Beads command failed: {detail}",
                stdout=completed.stdout,
                stderr=completed.stderr,
                returncode=completed.returncode,
                duration_ms=duration_ms,
            )
        if not completed.stdout.strip():
            return {"ok": completed.returncode == 0}
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            error_type = BeadsUncertainError if mutating else BeadsError
            raise error_type(
                "Beads returned invalid JSON"
                + ("; result is uncertain" if mutating else ""),
                stdout=completed.stdout,
                stderr=completed.stderr,
                returncode=completed.returncode,
                duration_ms=duration_ms,
            ) from error

    def create(self, task: IntakeTask) -> str:
        task.validate()
        metadata = {
            "fulcrum": {
                "intake_key": task.intake_key,
                "activation": task.activation,
                "executor_model": task.executor_model,
                "executor_reasoning_effort": task.executor_reasoning_effort,
                "overseer_model": task.overseer_model,
                "overseer_reasoning_effort": task.overseer_reasoning_effort,
                "model_provenance": task.model_provenance,
                "plan_id": task.plan_id,
                "plan_commit": task.plan_commit,
                "context": list(task.context),
            }
        }
        labels = [f"project:{task.project}", f"activation:{task.activation}"]
        arguments = [
            "create",
            "--title",
            task.title,
            "--description",
            task.description,
            "--external-ref",
            f"fulcrum-intake:{task.intake_key}",
            "--labels",
            ",".join(labels),
            "--metadata",
            json.dumps(metadata, separators=(",", ":")),
        ]
        if task.dependencies:
            arguments.extend(["--deps", ",".join(task.dependencies)])
        result = self.run(arguments, mutating=True)
        if isinstance(result, dict):
            identifier = result.get("id") or result.get("issue", {}).get("id")
        elif (
            isinstance(result, list)
            and len(result) == 1
            and isinstance(result[0], dict)
        ):
            identifier = result[0].get("id")
        else:
            identifier = None
        if not isinstance(identifier, str) or not identifier:
            raise BeadsError("Beads create returned no issue ID")
        return identifier

    def list_pending(self, project: str) -> list[dict[str, Any]]:
        result = self.run(
            [
                "list",
                "--all",
                "--label",
                f"project:{project}",
                "--label",
                "activation:pending",
                "--limit",
                "0",
            ]
        )
        return (
            [item for item in result if isinstance(item, dict)]
            if isinstance(result, list)
            else []
        )

    def find_intake(self, intake_key: str) -> list[dict[str, Any]]:
        result = self.run(["list", "--all", "--limit", "0"])
        if not isinstance(result, list):
            return []
        expected = f"fulcrum-intake:{intake_key}"
        return [
            item
            for item in result
            if isinstance(item, dict)
            and (item.get("external_ref") or item.get("externalRef")) == expected
        ]

    def close(self, bead_id: str, reason: str) -> None:
        self.run(["close", bead_id, "--reason", reason], mutating=True)

    def show(self, bead_id: str) -> dict[str, Any] | None:
        result = self.run(["show", bead_id])
        if (
            isinstance(result, list)
            and len(result) == 1
            and isinstance(result[0], dict)
        ):
            return result[0]
        return result if isinstance(result, dict) else None

    def comment(self, bead_id: str, content: str) -> None:
        self.run(["comment", bead_id, content], mutating=True)

    def status(self) -> dict[str, Any]:
        result = self.run(["status"])
        if not isinstance(result, dict):
            raise BeadsError("Beads status returned no object")
        return result

    def bootstrap(self) -> dict[str, Any]:
        result = self.run(["bootstrap"], mutating=True)
        return result if isinstance(result, dict) else {"result": result}


def _text(value: bytes | str | None) -> str | None:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value
