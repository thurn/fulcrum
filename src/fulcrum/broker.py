"""Policy-free pending-response broker for stock Codex Desktop.

The broker owns connections and clocks only. Every evaluation executes the
source-following ``fulcrum`` launcher, so a commit to local master changes the
next operation without replacing this process or its pending clients.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import signal
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from watchfiles import awatch

MAX_MESSAGE_BYTES = 4 * 1024 * 1024


def _log(event: str, **fields: Any) -> None:
    print(
        json.dumps(
            {"event": event, "component": "broker", "pid": os.getpid(), **fields},
            separators=(",", ":"),
        ),
        file=sys.stderr,
        flush=True,
    )


@dataclass(frozen=True)
class Evaluation:
    argv: tuple[str, ...]
    stdin: str
    interval_seconds: float
    deadline_monotonic: float
    wait_id: str
    kind: str


Runner = Callable[[Sequence[str], str], Awaitable[Mapping[str, Any]]]


async def run_fresh(argv: Sequence[str], stdin: str) -> Mapping[str, Any]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    stdout, stderr = await process.communicate(stdin.encode("utf-8"))
    if not stdout:
        raise RuntimeError(
            stderr.decode("utf-8", errors="replace").strip()
            or "fresh Fulcrum operation returned no result"
        )
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("fresh Fulcrum operation returned invalid JSON") from error
    if not isinstance(value, Mapping):
        raise RuntimeError("fresh Fulcrum operation returned a non-object")
    return value


class PendingBroker:
    """Own pending client responses while policy processes come and go."""

    def __init__(self, runner: Runner = run_fresh) -> None:
        self.runner = runner
        self._wake = asyncio.Event()
        self._active: dict[str, Evaluation] = {}
        self.started_monotonic: float = time.monotonic()
        self.completed: int = 0
        self.failures: int = 0

    def signal(self) -> None:
        self._wake.set()

    async def wait(self, evaluation: Evaluation) -> Mapping[str, Any]:
        if evaluation.wait_id in self._active:
            raise RuntimeError(f"wait {evaluation.wait_id} already has a connection")
        self._active[evaluation.wait_id] = evaluation
        started = time.monotonic()
        provider_errors = 0
        _log("broker_wait_started", wait_id=evaluation.wait_id, kind=evaluation.kind)
        try:
            while True:
                try:
                    result = await self.runner(evaluation.argv, evaluation.stdin)
                    provider_errors = 0
                except Exception as error:
                    if evaluation.kind != "ci":
                        raise
                    remaining = evaluation.deadline_monotonic - time.monotonic()
                    if remaining <= 0:
                        raise
                    delays = (60.0, 120.0, 300.0)
                    delay = min(
                        delays[min(provider_errors, len(delays) - 1)], remaining
                    )
                    provider_errors += 1
                    _log(
                        "broker_ci_observation_failed",
                        wait_id=evaluation.wait_id,
                        retry_in_seconds=delay,
                        error=str(error),
                    )
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=delay)
                        self._wake.clear()
                    except TimeoutError:
                        pass
                    continue
                if not _is_transport_wait(result):
                    self.completed += 1
                    _log(
                        "broker_wait_completed",
                        wait_id=evaluation.wait_id,
                        kind=evaluation.kind,
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )
                    return result
                watch_paths = _watch_paths(result)
                remaining = evaluation.deadline_monotonic - time.monotonic()
                if remaining <= 0:
                    result = await self.runner(evaluation.argv, evaluation.stdin)
                    if _is_transport_wait(result):
                        raise RuntimeError(
                            f"fresh policy did not settle expired {evaluation.kind} wait"
                        )
                    self.completed += 1
                    _log(
                        "broker_wait_completed",
                        wait_id=evaluation.wait_id,
                        kind=evaluation.kind,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        deadline=True,
                    )
                    return result
                try:
                    await self._wait_for_change(
                        watch_paths,
                        min(evaluation.interval_seconds, remaining),
                    )
                except TimeoutError:
                    pass
        except BaseException:
            self.failures += 1
            _log(
                "broker_wait_failed",
                wait_id=evaluation.wait_id,
                kind=evaluation.kind,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            raise
        finally:
            self._active.pop(evaluation.wait_id, None)
        raise AssertionError("pending wait loop exited without a result")

    async def _wait_for_change(
        self, watch_paths: tuple[Path, ...], timeout: float
    ) -> None:
        signal_task = asyncio.create_task(self._wake.wait())
        tasks: set[asyncio.Task[Any]] = {signal_task}
        if watch_paths:
            tasks.add(asyncio.create_task(_wait_for_files(watch_paths)))
        try:
            done, pending = await asyncio.wait(
                tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                raise TimeoutError
            self._wake.clear()
            for task in done:
                task.result()
            for task in pending:
                task.cancel()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def health(self) -> dict[str, Any]:
        return {
            "state": "healthy",
            "pid": os.getpid(),
            "uptime_seconds": round(time.monotonic() - self.started_monotonic, 3),
            "pending": [
                {
                    "wait_id": value.wait_id,
                    "kind": value.kind,
                    "deadline_monotonic": value.deadline_monotonic,
                }
                for value in self._active.values()
            ],
            "completed": self.completed,
            "failures": self.failures,
        }


def _is_transport_wait(result: Mapping[str, Any]) -> bool:
    payload = result.get("result")
    return (
        result.get("ok") is True
        and result.get("state") == "running"
        and isinstance(payload, Mapping)
        and isinstance(payload.get("transport_wait"), Mapping)
    )


def _watch_paths(result: Mapping[str, Any]) -> tuple[Path, ...]:
    payload = result.get("result")
    wait = payload.get("transport_wait") if isinstance(payload, Mapping) else None
    values = wait.get("watch_paths") if isinstance(wait, Mapping) else None
    if not isinstance(values, list):
        return ()
    return tuple(
        path
        for value in values
        if isinstance(value, str)
        and (path := Path(value)).is_absolute()
        and path.exists()
    )


async def _wait_for_files(paths: tuple[Path, ...]) -> None:
    async for _ in awatch(
        *paths,
        recursive=False,
        debounce=50,
        step=50,
    ):
        return


class BrokerServer:
    def __init__(self, socket_path: Path, broker: PendingBroker | None = None) -> None:
        self.socket_path: Path = socket_path.resolve(strict=False)
        self.broker: PendingBroker = broker or PendingBroker()
        self._server: asyncio.AbstractServer | None = None

    async def serve(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.socket_path.with_suffix(".lock")
        lock = lock_path.open("a+b")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            lock.close()
            raise RuntimeError("another broker owns this instance") from error
        self.socket_path.unlink(missing_ok=True)
        try:
            server = await asyncio.start_unix_server(
                self._client, path=str(self.socket_path)
            )
            self._server = server
            os.chmod(self.socket_path, 0o600)
            _log("broker_started", socket=str(self.socket_path))
            async with server:
                await server.serve_forever()
        finally:
            self.socket_path.unlink(missing_ok=True)
            lock.close()

    async def _client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw = await reader.readline()
            if len(raw) > MAX_MESSAGE_BYTES:
                raise ValueError("broker request exceeds the 4 MiB limit")
            value = json.loads(raw)
            if not isinstance(value, Mapping):
                raise ValueError("broker request must be an object")
            kind = value.get("type")
            if kind == "health":
                response: Mapping[str, Any] = self.broker.health()
            elif kind == "signal":
                self.broker.signal()
                response = {"ok": True}
            elif kind == "wait":
                request_argv = value.get("argv")
                if not isinstance(request_argv, list) or not all(
                    isinstance(item, str) for item in request_argv
                ):
                    raise ValueError("wait argv must be a string array")
                response = await self.broker.wait(
                    Evaluation(
                        argv=tuple(request_argv),
                        stdin=str(value.get("stdin") or "{}"),
                        interval_seconds=max(
                            0.05, float(value.get("interval_seconds") or 15)
                        ),
                        deadline_monotonic=time.monotonic()
                        + max(0.05, float(value.get("remaining_seconds") or 3600)),
                        wait_id=str(value.get("wait_id") or uuid.uuid4()),
                        kind=str(value.get("kind") or "instruction"),
                    )
                )
            else:
                raise ValueError("unknown broker request type")
        except BaseException as error:
            response = {
                "ok": False,
                "state": "failed",
                "error": {
                    "code": "BROKER_ERROR",
                    "message": str(error),
                    "retryable": True,
                },
            }
        try:
            writer.write(
                json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n"
            )
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass


async def broker_request(
    socket_path: Path, value: Mapping[str, Any]
) -> Mapping[str, Any]:
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    writer.write(json.dumps(value, separators=(",", ":")).encode("utf-8") + b"\n")
    await writer.drain()
    raw = await reader.readline()
    writer.close()
    await writer.wait_closed()
    result = json.loads(raw)
    if not isinstance(result, Mapping):
        raise RuntimeError("broker returned a non-object")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fulcrum-broker")
    parser.add_argument("--instance", required=True)
    args = parser.parse_args(argv)
    instance = Path(args.instance).expanduser()
    if not instance.is_absolute():
        parser.error("--instance must be absolute")
    socket_path: Path = instance / "broker.sock"

    async def run() -> None:
        loop = asyncio.get_running_loop()
        task = asyncio.create_task(BrokerServer(socket_path).serve())
        for name in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(name, lambda: task.cancel())
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
