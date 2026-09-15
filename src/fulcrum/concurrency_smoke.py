"""Bounded public-CLI client for the native thirty-task concurrency smoke."""

from __future__ import annotations

import argparse
import json
import os
import resource
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, IO, Mapping, Sequence, TypeVar

from fulcrum.contracts import FulcrumError

T = TypeVar("T")


class SmokeFailure(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_default(value: Any) -> str:
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _nested(envelope: Mapping[str, Any]) -> dict[str, Any]:
    result = envelope.get("result")
    if isinstance(result, Mapping) and isinstance(result.get("result"), Mapping):
        return dict(result["result"])
    return dict(result) if isinstance(result, Mapping) else {}


def _task_native(row: Mapping[str, Any]) -> dict[str, Any]:
    native = row.get("native")
    return dict(native) if isinstance(native, Mapping) else {}


def _capacity_policy(workers: int, *, paused: bool) -> dict[str, Any]:
    return {
        "automatic_capacity": workers,
        "default_project_capacity": workers,
        "project_capacity": {"fixture": workers},
        "paused_projects": ["fixture"] if paused else [],
        "rationale": "Explicit disposable native concurrency smoke.",
    }


class ConcurrencySmoke:
    def __init__(self, namespace: argparse.Namespace, executable: Path) -> None:
        values = vars(namespace)
        self.workers: int = int(values.get("workers", 30))
        self.model: str = str(values.get("model", "gpt-5.6-luna"))
        self.effort: str = str(values.get("effort", "low"))
        self.budget: float = float(values.get("timeout", 600))
        self.command_timeout: float = float(values.get("command_timeout", 90))
        self.external_runtime: bool = "runtime_endpoint" in values
        self.runtime_endpoint: str = str(
            values.get("runtime_endpoint") or self._available_runtime_endpoint()
        )
        codex_value = values.get("codex_executable") or shutil.which("codex")
        tollgate_value = values.get("tollgate_executable") or shutil.which("tg")
        if not isinstance(codex_value, str) or not isinstance(tollgate_value, str):
            raise SmokeFailure("native smoke executables are unavailable")
        self.codex_executable: str = str(Path(codex_value).resolve(strict=True))
        self.tollgate_executable: str = str(Path(tollgate_value).resolve(strict=True))
        self.bootstrap_executable: Path = executable.resolve(strict=True)
        self.executable: Path = self.bootstrap_executable
        self.started: float = time.monotonic()
        self.deadline: float = self.started + self.budget
        self.cleanup_at: float = self.deadline - 60
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.report_path: Path = Path(
            str(values.get("report") or f"/tmp/fulcrum2-concurrency-{stamp}.json")
        ).resolve(strict=False)
        self.markdown_path: Path = self.report_path.with_suffix(".md")
        fixture_parent = str(values.get("fixture_parent", "/tmp"))
        self.fixture_parent: Path = Path(
            tempfile.mkdtemp(prefix="fulcrum2-concurrency-", dir=fixture_parent)
        ).resolve(strict=True)
        self.fixture_root: Path = self.fixture_parent / "fixture"
        self.runtime_process: subprocess.Popen[str] | None = None
        self.runtime_stdout: IO[str] | None = None
        self.runtime_stderr: IO[str] | None = None
        self.runtime_process_facts: dict[str, Any] = {
            "owned": not self.external_runtime,
            "endpoint": self.runtime_endpoint,
            "requested_fd_soft_limit": 4096,
        }
        self.instance: str | None = None
        self.fixture_id: str | None = None
        self.fixture: dict[str, Any] = {}
        self.barrier_name: str = "native-concurrency"
        self.participants: list[str] = [
            f"worker-{index:02d}" for index in range(self.workers)
        ]
        self.work: dict[str, str] = {}
        self.tasks: dict[str, str] = {}
        self.turns: dict[str, str] = {}
        self.references: list[dict[str, Any]] = []
        self.assertions: list[dict[str, Any]] = []
        self.observations: dict[str, Any] = {}
        self.cleanup: dict[str, Any] | None = None
        self.gaps: list[str] = []
        self._lock: threading.RLock = threading.RLock()
        source_status = self._git("status", "--porcelain")
        self.report: dict[str, Any] = {
            "schema": "fulcrum2-concurrency-report-v1",
            "invocation": list(sys.argv),
            "started_at": _now(),
            "source_commit": self._git("rev-parse", "HEAD"),
            "source_status": source_status,
            "state": "running",
            "budget_seconds": self.budget,
            "cleanup_reserve_seconds": 60,
            "requested_workers": self.workers,
            "model": self.model,
            "effort": self.effort,
            "runtime_endpoint": self.runtime_endpoint,
        }

    @staticmethod
    def _available_runtime_endpoint() -> str:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            port = int(listener.getsockname()[1])
        return f"ws://127.0.0.1:{port}"

    def prepare_runtime(self) -> None:
        if self.external_runtime:
            self.runtime_process_facts["state"] = "external"
            self.report["runtime_process"] = self.runtime_process_facts
            return
        port = int(self.runtime_endpoint.rsplit(":", 1)[1])
        stdout_path = self.report_path.with_suffix(".runtime.log")
        stderr_path = self.report_path.with_suffix(".runtime-error.log")
        stdout_handle = stdout_path.open("w", encoding="utf-8")
        stderr_handle = stderr_path.open("w", encoding="utf-8")
        self.runtime_stdout = stdout_handle
        self.runtime_stderr = stderr_handle

        def raise_file_limit() -> None:
            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            target = 4096 if hard == resource.RLIM_INFINITY else min(4096, hard)
            if soft < target:
                resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))

        process = subprocess.Popen(
            [
                self.codex_executable,
                "app-server",
                "--listen",
                self.runtime_endpoint,
            ],
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
            start_new_session=True,
            preexec_fn=raise_file_limit,
        )
        self.runtime_process = process
        deadline = time.monotonic() + 20
        connected = False
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                    connected = True
                    break
            except OSError:
                time.sleep(0.1)
        self.runtime_process_facts.update(
            {
                "pid": process.pid,
                "state": "listening" if connected else "failed",
                "stdout": str(stdout_path),
                "stderr": str(stderr_path),
            }
        )
        self.report["runtime_process"] = self.runtime_process_facts
        self.save()
        if not connected:
            stdout_handle.flush()
            stderr_handle.flush()
            raise SmokeFailure(
                "owned Codex app-server did not become reachable: "
                + stderr_path.read_text(encoding="utf-8", errors="replace")[-4000:]
            )

    def _git(self, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(Path.cwd()), *arguments],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if completed.returncode != 0:
            raise SmokeFailure(completed.stderr.strip() or "git inspection failed")
        return completed.stdout.strip()

    def remaining(self, *, cleanup: bool = False) -> float:
        remaining = (self.deadline if cleanup else self.cleanup_at) - time.monotonic()
        if remaining <= 0:
            raise SmokeFailure("concurrency smoke exhausted its work budget")
        return remaining

    def save(self) -> None:
        with self._lock:
            self.report.update(
                {
                    "updated_at": _now(),
                    "elapsed_seconds": round(time.monotonic() - self.started, 3),
                    "fixture": self.fixture,
                    "participants": [
                        {
                            "participant": participant,
                            "bead_id": self.work.get(participant),
                            "task_id": self.tasks.get(participant),
                            "turn_id": self.turns.get(participant),
                        }
                        for participant in self.participants
                    ],
                    "assertions": list(self.assertions),
                    "observations": self.observations,
                    "commands": list(self.references),
                    "observation_gaps": list(self.gaps),
                    "cleanup": self.cleanup,
                }
            )
            self.report_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.report_path.with_suffix(self.report_path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(self.report, indent=2, sort_keys=True, default=_json_default)
                + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.report_path)

    def save_markdown(self) -> None:
        lines = [
            "# Fulcrum2 native concurrency smoke",
            "",
            f"- State: **{self.report.get('state')}**",
            f"- Source commit: `{self.report.get('source_commit')}`",
            f"- Requested workers: `{self.workers}`",
            f"- Model/effort: `{self.model}` / `{self.effort}`",
            f"- Elapsed seconds: `{self.report.get('elapsed_seconds')}`",
            f"- JSON evidence: `{self.report_path}`",
            "",
            "## Assertions",
            "",
        ]
        lines.extend(
            f"- {'PASS' if row.get('passed') else 'FAIL'}: {row.get('name')}"
            for row in self.assertions
        )
        lines.extend(
            [
                "",
                "## Measured concurrency",
                "",
                "```json",
                json.dumps(
                    self.observations,
                    indent=2,
                    sort_keys=True,
                    default=_json_default,
                ),
                "```",
                "",
                "## Cleanup",
                "",
                "```json",
                json.dumps(
                    self.cleanup, indent=2, sort_keys=True, default=_json_default
                ),
                "```",
            ]
        )
        temporary = self.markdown_path.with_suffix(self.markdown_path.suffix + ".tmp")
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(temporary, self.markdown_path)

    def check(self, condition: bool, name: str, evidence: Any) -> None:
        row = {"name": name, "passed": bool(condition), "evidence": evidence}
        with self._lock:
            self.assertions.append(row)
        self.save()
        if not condition:
            raise SmokeFailure(f"assertion failed: {name}: {evidence}")

    def command(
        self,
        *arguments: str,
        payload: Mapping[str, Any] | None = None,
        executable: Path | None = None,
        timeout: float | None = None,
        cleanup: bool = False,
        expected: Sequence[int] = (0,),
    ) -> dict[str, Any]:
        argv = [str(executable or self.executable), *arguments]
        started = time.monotonic()
        completed = subprocess.run(
            argv,
            input=(json.dumps(payload) + "\n" if payload is not None else None),
            capture_output=True,
            text=True,
            check=False,
            timeout=min(
                timeout or self.command_timeout,
                max(1.0, self.remaining(cleanup=cleanup)),
            ),
            env={**os.environ, "CODEX_THREAD_ID": ""},
        )
        envelope: dict[str, Any] | None = None
        if completed.stdout.strip():
            try:
                candidate = json.loads(completed.stdout)
                envelope = candidate if isinstance(candidate, dict) else None
            except json.JSONDecodeError:
                envelope = None
        reference = {
            "argv": argv,
            "input": dict(payload) if payload is not None else None,
            "returncode": completed.returncode,
            "latency_seconds": round(time.monotonic() - started, 3),
            "state": envelope.get("state") if envelope else None,
            "operation_id": envelope.get("operation_id") if envelope else None,
            "request_id": envelope.get("request_id") if envelope else None,
            "error_code": (
                (envelope.get("error") or {}).get("code") if envelope else None
            ),
        }
        with self._lock:
            self.references.append(reference)
        if completed.returncode not in expected:
            raise SmokeFailure(
                f"command returned {completed.returncode}: {' '.join(argv)}\n"
                f"{completed.stdout}\n{completed.stderr}"
            )
        if envelope is None:
            raise SmokeFailure(f"command returned no JSON: {' '.join(argv)}")
        return envelope

    def fc(
        self,
        *arguments: str,
        payload: Mapping[str, Any] | None = None,
        offline: bool = False,
        timeout: float | None = None,
        cleanup: bool = False,
        expected: Sequence[int] = (0,),
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if self.instance is None:
            raise SmokeFailure("fixture instance is not established")
        argv = ["--instance", self.instance, *arguments]
        if "--timeout" not in arguments:
            process_timeout = timeout or self.command_timeout
            argv.extend(["--timeout", str(max(1.0, process_timeout - 5.0))])
        if offline:
            argv.append("--offline")
        if "--actor" not in arguments:
            argv.extend(["--actor", "human"])
        if request_id:
            argv.extend(["--request-id", request_id])
        if payload is not None:
            argv.extend(["--input", "-"])
        argv.append("--json")
        return self.command(
            *argv,
            payload=payload,
            timeout=timeout,
            cleanup=cleanup,
            expected=expected,
        )

    def parallel(self, function: Callable[[T], Any], items: Sequence[T]) -> list[Any]:
        results: list[Any] = [None] * len(items)
        with ThreadPoolExecutor(max_workers=min(32, max(1, len(items)))) as pool:
            futures = {
                pool.submit(function, item): index for index, item in enumerate(items)
            }
            for future in as_completed(futures):
                results[futures[future]] = future.result()
        return results

    def poll(
        self,
        description: str,
        read: Callable[[], T],
        ready: Callable[[T], bool],
        *,
        seconds: float,
    ) -> T:
        deadline = min(time.monotonic() + seconds, self.cleanup_at)
        last = read()
        while not ready(last):
            if time.monotonic() >= deadline:
                raise SmokeFailure(f"timed out waiting for {description}: {last}")
            time.sleep(1)
            last = read()
        return last

    def create_fixture(self) -> None:
        request_id = str(uuid.uuid4())
        self.instance = str(self.fixture_root / "instance")
        created = self.command(
            "fixture",
            "create",
            "--actor",
            "human",
            "--request-id",
            request_id,
            "--timeout",
            "180",
            "--input",
            "-",
            "--json",
            payload={
                "root": str(self.fixture_root),
                "runtime": {
                    "kind": "codex",
                    "endpoint": self.runtime_endpoint,
                    "executable": self.codex_executable,
                },
                "delivery": {
                    "kind": "tollgate",
                    "executable": self.tollgate_executable,
                },
                "model": self.model,
                "effort": self.effort,
                "capacity": 4,
                "source_sync": False,
            },
            executable=self.bootstrap_executable,
            timeout=240,
        )
        self.fixture = _nested(created)
        self.instance = str(self.fixture["instance"])
        self.fixture_id = str(self.fixture["fixture_id"])
        installed = Path(str(self.instance)) / "runtime" / "current" / "bin" / "fulcrum"
        self.check(
            installed.is_file(), "fixture installed executable exists", str(installed)
        )
        self.executable = installed.resolve(strict=True)

    def start_and_configure(self) -> None:
        started = self.fc("service", "start", offline=True, timeout=150)
        self.check(started.get("ok") is True, "fixture service starts", started)
        policy = self.fc(
            "policy",
            "set",
            payload=_capacity_policy(self.workers, paused=True),
        )
        self.check(policy.get("ok") is True, "fixture capacity is authorized", policy)
        status = self.fc("status")
        capacity = dict(status.get("result", {}).get("capacity") or {})
        self.check(
            capacity.get("global_limit") == self.workers
            and capacity.get("default_project_limit") == self.workers,
            "only the disposable fixture has the requested capacity",
            capacity,
        )
        self.check(
            capacity.get("paused_projects") == ["fixture"],
            "fixture admission is paused while authorizations are recorded",
            capacity,
        )
        self.observations["baseline"] = {
            "managed_active": capacity.get("occupied"),
            "status": status.get("result"),
            "runtime": self.fc("runtime", "status").get("result"),
            "observed_at": _now(),
        }

    def create_work(self) -> None:
        assert self.fixture_id is not None and self.instance is not None
        fixture_id: str = self.fixture_id
        instance: str = self.instance
        executable: str = str(self.executable)

        def create(participant: str) -> dict[str, Any]:
            arrive = " ".join(
                shlex.quote(item)
                for item in (
                    executable,
                    "--instance",
                    instance,
                    "fixture",
                    "barrier",
                    "arrive",
                    fixture_id,
                    "--name",
                    self.barrier_name,
                    "--participant",
                    participant,
                    "--wait",
                    "--timeout",
                    str(max(60, int(self.budget - 90))),
                    "--json",
                )
            )
            outcome = (
                f"Concurrency participant {participant}. Run exactly one shell tool "
                f"command first: `{arrive}`. Wait for its successful release response. "
                "Then finish this Weaver responsibility with outcome answered, a short "
                "summary, and evidence naming the barrier participant. The finish input "
                "must contain exactly a summary string and evidence as a one-element "
                "array of strings. Do not edit files or start another model turn."
            )
            return self.fc(
                "work",
                "create",
                "--project",
                "fixture",
                payload={
                    "title": f"Concurrency {participant}",
                    "outcome": outcome,
                    "acceptance": [
                        f"Barrier participant {participant} arrives through a shell tool.",
                        "The work closes with an answered disposition after release.",
                    ],
                    "requested_role": "weaver",
                    "project": "fixture",
                    "intake": {
                        "benefit": "Measure real simultaneous native task capacity.",
                        "uncertainties": [],
                    },
                },
            )

        created = self.parallel(create, self.participants)
        for participant, envelope in zip(self.participants, created):
            self.work[participant] = str(_nested(envelope)["bead_id"])
        self.check(
            len(set(self.work.values())) == self.workers,
            f"{self.workers} distinct Weaver work beads exist",
            self.work,
        )
        barrier = self.fc(
            "fixture",
            "barrier",
            "prepare",
            fixture_id,
            "--name",
            self.barrier_name,
            payload={"participants": self.participants},
        )
        expected = _nested(barrier).get("barrier", {}).get("expected")
        self.check(
            expected == sorted(self.participants),
            "barrier is prepared before native starts",
            _nested(barrier),
        )

    def dispatch_all(self) -> None:
        def dispatch(participant: str) -> dict[str, Any]:
            return self.fc(
                "dispatch",
                "--bead",
                self.work[participant],
                "--authorize",
                request_id=str(
                    uuid.uuid5(
                        uuid.UUID("0b7ae019-74d8-4a67-b7df-6d3acfe6fd93"),
                        participant,
                    )
                ),
                timeout=min(150, self.remaining()),
            )

        results = self.parallel(dispatch, self.participants)
        starts = [_nested(envelope) for envelope in results]
        self.check(
            all(
                row.get("started") is False and row.get("queued") is True
                for row in starts
            ),
            f"all {self.workers} starts are durably authorized while paused",
            starts,
        )
        policy = self.fc(
            "policy",
            "set",
            payload=_capacity_policy(self.workers, paused=False),
        )
        self.check(
            policy.get("ok") is True,
            "fixture admission is unpaused for controller reconciliation",
            policy,
        )

    def task_rows(self) -> dict[str, dict[str, Any]]:
        rows = (
            self.fc("task", "list", "--limit", "0").get("result", {}).get("items", [])
        )
        by_work = {
            str(row.get("work_bead")): dict(row)
            for row in rows
            if isinstance(row, Mapping) and row.get("role") == "weaver"
        }
        return {
            participant: by_work[self.work[participant]]
            for participant in self.participants
            if self.work.get(participant) in by_work
        }

    def observe_overlap(self) -> None:
        assert self.fixture_id is not None
        fixture_id: str = self.fixture_id

        def observation() -> dict[str, Any]:
            barrier = self.fc(
                "fixture",
                "barrier",
                "show",
                fixture_id,
                "--name",
                self.barrier_name,
            ).get("result", {})
            tasks = self.task_rows()
            active = {
                participant: row
                for participant, row in tasks.items()
                if _task_native(row).get("active_turn") is not None
            }
            return {"barrier": barrier, "tasks": tasks, "active": active}

        observed = self.poll(
            f"{self.workers} barrier arrivals and active native turns",
            observation,
            lambda row: len(row["barrier"].get("arrived", [])) == self.workers
            and len(row["active"]) == self.workers,
            seconds=min(360, self.remaining()),
        )
        observed_at = _now()
        for participant, row in observed["tasks"].items():
            native = _task_native(row)
            self.tasks[participant] = str(row["thread_id"])
            self.turns[participant] = str(native["active_turn"])
        runtime = self.fc("runtime", "status").get("result", {})
        status = self.fc("status").get("result", {})
        self.observations["peak"] = {
            "observed_at": observed_at,
            "managed_active": len(observed["active"]),
            "active_task_ids": sorted(self.tasks.values()),
            "active_turn_ids": sorted(self.turns.values()),
            "barrier": observed["barrier"],
            "status": status,
            "runtime": runtime,
        }
        resources = dict(runtime.get("resources") or {})
        if resources.get("fd_soft_limit") is None or resources.get("fd_usage") is None:
            self.gaps.append(
                "native runtime did not expose complete file-descriptor pressure facts"
            )
        self.check(
            len(set(self.tasks.values())) == self.workers
            and len(set(self.turns.values())) == self.workers,
            f"{self.workers} distinct native tasks and turns are active together",
            self.observations["peak"],
        )
        self.check(
            resources.get("overloaded") is not True,
            "shared runtime reports no supported overload condition",
            resources,
        )
        self.check(
            status.get("ok", True) is not False, "status remains responsive", status
        )

    def release_and_finish(self) -> None:
        assert self.fixture_id is not None
        fixture_id: str = self.fixture_id
        released = self.fc(
            "fixture",
            "barrier",
            "release",
            fixture_id,
            "--name",
            self.barrier_name,
        )
        self.observations["overlap_interval"] = {
            "start": self.observations["peak"]["observed_at"],
            "end": _now(),
            "active_task_ids": sorted(self.tasks.values()),
        }
        self.check(
            _nested(released).get("barrier", {}).get("released") is True,
            "fixture barrier releases after overlap observation",
            _nested(released),
        )

        def settle_participant(participant: str) -> dict[str, Any]:
            waited = self.fc(
                "task",
                "wait",
                self.tasks[participant],
                "--turn-id",
                self.turns[participant],
                "--until",
                "terminal",
                timeout=min(60, self.remaining()),
            )
            output = self.fc(
                "task",
                "output",
                self.tasks[participant],
                "--turn-id",
                self.turns[participant],
                "--limit",
                "0",
                "--max-bytes",
                "1048576",
            ).get("result", {})
            released = self.fc("task", "release", self.tasks[participant])
            return {"wait": waited, "output": output, "release": released}

        settlements = self.parallel(settle_participant, self.participants)
        self.check(
            all(_nested(row["wait"]).get("satisfied") is True for row in settlements),
            "all exact native turns are observed terminal",
            [_nested(row["wait"]) for row in settlements],
        )

        def work_rows() -> list[dict[str, Any]]:
            return self.parallel(
                lambda participant: dict(
                    self.fc("work", "show", self.work[participant]).get("result") or {}
                ),
                self.participants,
            )

        closed = self.poll(
            f"{self.workers} answered work beads to close",
            work_rows,
            lambda rows: all(row.get("status") == "closed" for row in rows),
            seconds=min(120, self.remaining()),
        )
        self.check(
            all(
                (row.get("fc", {}).get("disposition") or {}).get("outcome")
                == "answered"
                for row in closed
            ),
            f"all {self.workers} Weaver beads close answered",
            closed,
        )

        outputs = [row["output"] for row in settlements]
        tool_evidence: dict[str, Any] = {}
        for participant, output in zip(self.participants, outputs):
            serialized = json.dumps(output, sort_keys=True).lower()
            tool_evidence[participant] = {
                "task_id": self.tasks[participant],
                "turn_id": self.turns[participant],
                "command_execution": "commandexecution" in serialized,
                "participant_visible": participant.lower() in serialized,
            }
        self.observations["tool_evidence"] = tool_evidence
        self.check(
            all(
                row["command_execution"] and row["participant_visible"]
                for row in tool_evidence.values()
            ),
            "every native turn exposes its distinct shell barrier operation",
            tool_evidence,
        )

        self.check(
            all(row["release"].get("ok") is True for row in settlements),
            "all worker subscriptions release through the public CLI",
            [_nested(row["release"]) for row in settlements],
        )
        final_rows = self.task_rows()
        final_status = self.fc("status").get("result", {})
        final_runtime = self.fc("runtime", "status").get("result", {})
        self.observations["final"] = {
            "observed_at": _now(),
            "managed_active": sum(
                _task_native(row).get("active_turn") is not None
                for row in final_rows.values()
            ),
            "subscriptions": {
                participant: row.get("subscription")
                for participant, row in final_rows.items()
            },
            "status": final_status,
            "runtime": final_runtime,
        }
        self.check(
            len(final_rows) == self.workers
            and all(
                (row.get("subscription") or {}).get("state") == "released"
                for row in final_rows.values()
            ),
            "all worker subscription releases are durably observed",
            self.observations["final"],
        )
        self.check(
            self.observations["final"]["managed_active"] == 0,
            "no owned worker start remains active",
            self.observations["final"],
        )

    def cleanup_fixture(self) -> None:
        if self.instance is None or self.fixture_id is None:
            self.stop_runtime()
            return
        fixture_id: str = self.fixture_id
        try:
            try:
                self.fc(
                    "fixture",
                    "barrier",
                    "release",
                    fixture_id,
                    "--name",
                    self.barrier_name,
                    cleanup=True,
                    expected=(0, 2),
                )
            except Exception as error:
                self.gaps.append(f"bounded barrier release: {error}")
            try:
                rows = self.task_rows()
                for row in rows.values():
                    native = _task_native(row)
                    if native.get("active_turn"):
                        self.fc(
                            "task",
                            "interrupt",
                            str(row["thread_id"]),
                            "--turn-id",
                            str(native["active_turn"]),
                            cleanup=True,
                            expected=(0, 2, 4, 5),
                        )
            except Exception as error:
                self.gaps.append(f"bounded task interruption: {error}")
            cleanup = self.fc(
                "fixture",
                "cleanup",
                fixture_id,
                "--yes",
                offline=True,
                timeout=min(55, max(1, self.deadline - time.monotonic())),
                cleanup=True,
            )
            self.cleanup = _nested(cleanup)
        except Exception as error:
            self.cleanup = {"root_removed": False, "error": str(error)}
            self.gaps.append(f"fixture cleanup failed: {error}")
        finally:
            self.stop_runtime()
            try:
                if self.fixture_parent.exists() and not any(
                    self.fixture_parent.iterdir()
                ):
                    self.fixture_parent.rmdir()
            except OSError:
                pass

    def stop_runtime(self) -> None:
        process = self.runtime_process
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            self.runtime_process_facts.update(
                {"state": "stopped", "returncode": process.returncode}
            )
        for handle in (self.runtime_stdout, self.runtime_stderr):
            if handle is not None and not handle.closed:
                handle.close()
        cleanup = dict(self.cleanup or {})
        cleanup["owned_runtime"] = dict(self.runtime_process_facts)
        self.cleanup = cleanup

    def run(self) -> dict[str, Any]:
        try:
            self.check(
                not self.report["source_status"],
                "concurrency smoke runs from clean committed source",
                self.report["source_status"],
            )
            self.prepare_runtime()
            self.create_fixture()
            self.start_and_configure()
            self.create_work()
            self.dispatch_all()
            self.observe_overlap()
            self.release_and_finish()
            self.report["state"] = "passed"
        except Exception as error:
            self.report["state"] = "failed"
            self.report["error"] = {
                "type": type(error).__name__,
                "message": str(error),
            }
        finally:
            self.save()
            self.cleanup_fixture()
            if not self.cleanup or self.cleanup.get("root_removed") is not True:
                self.report["state"] = "failed"
            self.report["finished_at"] = _now()
            self.save()
            self.save_markdown()
        passed = self.report["state"] == "passed"
        if passed:
            return {
                "ok": True,
                "state": "completed",
                "operation_id": None,
                "request_id": None,
                "result": {
                    "state": "passed",
                    "workers": self.workers,
                    "report": str(self.report_path),
                    "markdown": str(self.markdown_path),
                    "elapsed_seconds": self.report.get("elapsed_seconds"),
                },
                "warnings": [],
                "error": None,
            }
        return {
            "ok": False,
            "state": "failed",
            "operation_id": None,
            "request_id": None,
            "result": {
                "state": "failed",
                "workers": self.workers,
                "report": str(self.report_path),
                "markdown": str(self.markdown_path),
                "elapsed_seconds": self.report.get("elapsed_seconds"),
            },
            "warnings": [],
            "error": {
                "code": "CONCURRENCY_SMOKE_FAILED",
                "message": str(
                    (self.report.get("error") or {}).get("message") or "smoke failed"
                ),
                "retryable": False,
                "next_command": None,
                "details": {"report": str(self.report_path)},
            },
        }


def run_concurrency_smoke(
    namespace: argparse.Namespace, *, executable: Path
) -> dict[str, Any]:
    values = vars(namespace)
    workers = int(values.get("workers", 30))
    timeout = float(values.get("timeout", 600))
    command_timeout = float(values.get("command_timeout", 90))
    if values.get("instance") is not None or values.get("config") is not None:
        raise FulcrumError.invalid(
            "SMOKE_SELECTION_CONFLICT",
            "smoke concurrency always creates and selects its own disposable fixture",
        )
    if workers < 1 or workers > 30:
        raise FulcrumError.invalid(
            "INVALID_WORKERS", "workers must be between 1 and 30"
        )
    if timeout <= 60 or timeout > 600:
        raise FulcrumError.invalid(
            "INVALID_TIMEOUT",
            "concurrency smoke timeout must be over 60 and at most 600 seconds",
        )
    if command_timeout <= 0:
        raise FulcrumError.invalid(
            "INVALID_TIMEOUT", "command timeout must be positive"
        )
    if not (values.get("codex_executable") or shutil.which("codex")):
        raise FulcrumError(
            "DEPENDENCY_UNAVAILABLE", "native Codex executable is required", exit_code=4
        )
    if not (values.get("tollgate_executable") or shutil.which("tg")):
        raise FulcrumError(
            "DEPENDENCY_UNAVAILABLE", "Tollgate executable is required", exit_code=4
        )
    return ConcurrencySmoke(namespace, executable).run()
