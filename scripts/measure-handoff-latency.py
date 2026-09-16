"""Measure durable finish-to-successor scheduling while publication is occupied.

This is an isolated live scheduler/stock-Beads benchmark. It exercises the real
resident socket, lane scheduler, process locks, and durable operation receipts.
The successor operation is a benchmark receipt; native-provider task scheduling
is deliberately reported separately and is not included in the latency claim.

Run with:
  .venv/bin/python scripts/measure-handoff-latency.py
"""

from __future__ import annotations

import argparse
import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import shutil
import statistics
import subprocess
import tempfile
import uuid

from fulcrum.contracts import ActorContext, InstanceContext, ParsedRequest
from fulcrum.ledger import Ledger, OperationRecord
from fulcrum.resident import Resident
from fulcrum.resident_client import exchange


@dataclass
class Trial:
    finish: OperationRecord
    completed: asyncio.Future[OperationRecord]


class BenchmarkResident(Resident):
    def __init__(
        self,
        instance: Path,
        settings: dict[str, object],
        ledger: Ledger,
        request: ParsedRequest,
        *,
        publication_hold_seconds: float,
    ) -> None:
        super().__init__(instance, settings)
        self.ledger = ledger
        self.request = request
        self.publication_hold_seconds = publication_hold_seconds
        self.pending: deque[Trial] = deque()
        self.publication_started = asyncio.Event()
        self.publication_completed = asyncio.Event()

    async def local_source_changed(self) -> bool:
        return False

    async def job(self, kind: str) -> None:
        if kind == "publication":
            self.publication_started.set()
            await asyncio.sleep(self.publication_hold_seconds)
            self.publication_completed.set()
            return
        if kind != "reconcile" or not self.pending:
            return
        trial = self.pending.popleft()
        try:
            successor_request = ParsedRequest(
                command=("benchmark", "successor"),
                arguments={},
                input={},
                actor=ActorContext(kind="controller"),
                instance=self.request.instance,
                request_id=str(uuid.uuid4()),
                timeout=5,
            )
            successor, _ = await asyncio.to_thread(
                self.ledger.create_operation,
                successor_request,
                planned={"parent_finish_operation": trial.finish.id},
                next_action="Benchmark successor creation observed.",
            )
            successor = await asyncio.to_thread(
                self.ledger.update_operation,
                successor,
                state="completed",
                step="benchmark_successor_created",
                result={"parent_finish_operation": trial.finish.id},
                next_action="No action is required.",
            )
        except BaseException as error:
            trial.completed.set_exception(error)
            raise
        trial.completed.set_result(successor)


def parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise RuntimeError(f"operation timestamp is absent: {value!r}")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def benchmark(trials: int, publication_hold_seconds: float) -> dict[str, object]:
    executable = shutil.which("bd")
    if executable is None:
        raise RuntimeError("stock Beads executable 'bd' is required")
    with tempfile.TemporaryDirectory(prefix="fc-hand-", dir="/tmp") as temporary:
        root = Path(temporary)
        brain = root / "brain"
        instance = root / "instance"
        brain.mkdir()
        instance.mkdir()
        subprocess.run(
            ["git", "-C", str(brain), "init", "-q"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                executable,
                "--json",
                "--actor",
                "fulcrum-benchmark",
                "init",
                "--server",
                "--prefix",
                "fc",
                "--skip-hooks",
                "--skip-agents",
                "--non-interactive",
            ],
            check=True,
            capture_output=True,
            cwd=brain,
        )
        context = InstanceContext(
            instance_root=instance,
            config_path=instance / "config",
            brain_root=brain,
            socket_path=instance / "controller.sock",
            lock_path=brain / ".lock",
            explicit_selection=True,
        )
        base = ParsedRequest(
            command=("benchmark", "finish"),
            arguments={},
            input={},
            actor=ActorContext(kind="controller"),
            instance=context,
            request_id=str(uuid.uuid4()),
            timeout=5,
        )
        ledger = Ledger(brain, executable=executable, actor="fulcrum-benchmark")
        resident = BenchmarkResident(
            instance,
            {
                "config": str(context.config_path),
                "endpoint": "ws://127.0.0.1:1",
                "reconcile_seconds": 3600,
                "publication_seconds": 3600,
            },
            ledger,
            base,
            publication_hold_seconds=publication_hold_seconds,
        )
        socket_path = instance / "resident.sock"
        server = await asyncio.start_unix_server(resident.client, path=str(socket_path))
        scheduler = asyncio.create_task(resident.schedule())
        latencies: list[float] = []
        rows: list[dict[str, object]] = []
        try:
            await asyncio.wait_for(resident.publication_started.wait(), timeout=5)
            for index in range(trials):
                finish_request = ParsedRequest(
                    command=("benchmark", "finish"),
                    arguments={},
                    input={},
                    actor=ActorContext(kind="task", task_id=f"trial-{index}"),
                    instance=context,
                    request_id=str(uuid.uuid4()),
                    timeout=5,
                )
                finish, _ = await asyncio.to_thread(
                    ledger.create_operation,
                    finish_request,
                    planned={"trial": index},
                    next_action="Wake successor scheduling.",
                )
                finish = await asyncio.to_thread(
                    ledger.update_operation,
                    finish,
                    state="completed",
                    step="benchmark_finish_received",
                    result={"trial": index},
                    next_action="Successor scheduling is required.",
                )
                publication_active = not resident.publication_completed.is_set()
                completed = asyncio.get_running_loop().create_future()
                resident.pending.append(Trial(finish, completed))
                await exchange(socket_path, {"action": "wake", "timeout": 2.0})
                successor = await asyncio.wait_for(completed, timeout=10.0)
                finish_at = parse_time(finish.operation.get("completed_at"))
                successor_at = parse_time(successor.operation.get("created_at"))
                latency = (successor_at - finish_at).total_seconds()
                latencies.append(latency)
                rows.append(
                    {
                        "trial": index,
                        "finish_operation": finish.id,
                        "successor_operation": successor.id,
                        "seconds": latency,
                        "publication_active": publication_active,
                    }
                )
                await asyncio.sleep(0)
            await resident.publication_completed.wait()
        finally:
            resident.stop.set()
            resident.wake.set()
            scheduler.cancel()
            await asyncio.gather(scheduler, return_exceptions=True)
            for job in resident.jobs.values():
                job.cancel()
            await asyncio.gather(*resident.jobs.values(), return_exceptions=True)
            server.close()
            await server.wait_closed()
            socket_path.unlink(missing_ok=True)
        ordered = sorted(latencies)
        p95_index = max(0, int(0.95 * len(ordered) + 0.999999) - 1)
        publication_overlapped = all(bool(row["publication_active"]) for row in rows)
        return {
            "benchmark": "durable_finish_to_successor_operation",
            "trials": trials,
            "publication_hold_seconds": publication_hold_seconds,
            "publication_overlapped_all_trials": publication_overlapped,
            "p95_seconds": ordered[p95_index],
            "median_seconds": statistics.median(ordered),
            "maximum_seconds": max(ordered),
            "threshold_seconds": 2.0,
            "passed": ordered[p95_index] < 2.0 and publication_overlapped,
            "native_provider_scheduling": "not measured; report separately",
            "rows": rows,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=20)
    parser.add_argument("--publication-hold-seconds", type=float, default=60.0)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    if arguments.trials < 1:
        parser.error("--trials must be positive")
    if arguments.publication_hold_seconds <= 0:
        parser.error("--publication-hold-seconds must be positive")
    report = asyncio.run(
        benchmark(arguments.trials, arguments.publication_hold_seconds)
    )
    rendered = json.dumps(report, indent=2) + "\n"
    if arguments.output is not None:
        arguments.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
