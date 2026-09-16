"""Connection continuity, not application policy.

Do not import workflow code here. Every scheduled job starts fresh selected
Python code; ordinary activation must never replace this connection owner.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
import sys
from typing import Any

from fulcrum.bootstrap import launch_arguments, pinned_selection, selection
from fulcrum.transport import AppServerError, Transport


class Resident:
    def __init__(self, instance: Path, settings: dict[str, Any]) -> None:
        self.instance = instance
        self.settings = settings
        self.transport = Transport(settings["endpoint"])
        self.events: dict[str, dict[str, Any]] = {}
        self.sequence = 0
        self.update_requested = False
        self.reconcile_requested = True
        self.wake = asyncio.Event()
        self.stop = asyncio.Event()
        self.jobs: dict[str, asyncio.Task[None]] = {}
        self.errors: dict[str, str] = {}
        self.last_source_probe: dict[str, Any] | None = None

    async def dispatch(self, value: dict[str, Any]) -> dict[str, Any]:
        action = value["action"]
        if action == "health":
            return {
                "pid": os.getpid(),
                "connected": self.transport.ready,
                "pending": {
                    key: event.to_dict()
                    for key, event in self.transport.pending_server_requests.items()
                },
                "jobs": list(self.jobs),
                "errors": self.errors,
                "event_count": len(self.events),
                "last_source_probe": self.last_source_probe,
            }
        if action == "request":
            await self.transport.connect()
            result = await self.transport.request(
                value["method"],
                value["params"],
                timeout=float(value.get("timeout", 30)),
            )
            return {
                "result": result,
                "pending": {
                    key: event.to_dict()
                    for key, event in self.transport.pending_server_requests.items()
                },
            }

        if action == "respond":
            await self.transport.respond_server_request(
                value["request_id"], value["response"]
            )
            return {"ok": True}
        if action == "events":
            return {"events": list(self.events.items())[:128]}
        if action == "ack":
            for identifier in value["ids"]:
                self.events.pop(identifier, None)
            return {"ok": True}
        if action == "wake":
            self.update_requested = self.update_requested or bool(value.get("update"))
            self.reconcile_requested = True
            self.wake.set()
            return {"ok": True}
        raise AppServerError("unknown resident action", category="rejected")

    async def client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), 10)
            try:
                result = await self.dispatch(json.loads(raw))
            except Exception as error:
                result = {
                    "error": str(error),
                    "category": getattr(error, "category", "unavailable"),
                    "uncertain": getattr(error, "uncertain", False),
                }
            writer.write(json.dumps(result).encode() + b"\n")
            await writer.drain()
        except (ConnectionError, asyncio.TimeoutError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    async def collect(self) -> None:
        while not self.stop.is_set():
            try:
                await self.transport.connect()
                async for event in self.transport.events():
                    self.sequence += 1
                    # Pending requests remain in transport until explicitly resolved.
                    # Overflow is observable; reconciliation cannot infer success.
                    if len(self.events) >= 1024:
                        self.events.pop(next(iter(self.events)))
                        self.errors["events"] = (
                            "event overflow; reconciliation required"
                        )
                    self.events[str(self.sequence)] = event.to_dict()
                    self.reconcile_requested = True
                    self.wake.set()
            except AppServerError as error:
                self.errors["transport"] = str(error)
                await asyncio.sleep(2)

    async def job(self, kind: str) -> None:
        selected, lease = pinned_selection(self.instance)
        if selected is None:
            self.errors[kind] = "no selected source"
            return
        env = dict(
            os.environ,
            FULCRUM_SOURCE=selected["source"],
            FULCRUM_COMMIT=selected["commit"],
            PYTHONDONTWRITEBYTECODE="1",
        )
        env.pop("PYTHONPATH", None)
        command = launch_arguments(
            selected,
            "fulcrum.worker",
            [kind, str(self.instance), self.settings["config"]],
        )
        log = self.instance / "logs" / f"{kind}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("ab") as output:
            child = await asyncio.create_subprocess_exec(
                *command, env=env, stdout=output, stderr=output, start_new_session=True
            )
            code = await child.wait()
        if lease is not None:
            os.close(lease)
        if code:
            self.errors[kind] = f"job exited {code}"
        else:
            self.errors.pop(kind, None)

    def start_job(self, kind: str) -> bool:
        if kind in self.jobs and not self.jobs[kind].done():
            return False
        task = asyncio.create_task(self.job(kind))
        self.jobs[kind] = task
        return True

    async def published_source_changed(self) -> bool:
        source = self.settings.get("source")
        selected = selection(self.instance)
        if not isinstance(source, dict) or selected is None:
            return True
        repository = source.get("repository")
        remote = source.get("remote")
        branch = source.get("branch")
        if not all(
            isinstance(item, str) and item for item in (repository, remote, branch)
        ):
            self.errors["source_probe"] = "resident source probe is not configured"
            return True
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            repository,
            "ls-remote",
            remote,
            f"refs/heads/{branch}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            self.errors["source_probe"] = "published source probe timed out"
            return False
        if process.returncode != 0:
            message = stderr.decode(errors="replace").strip()
            self.errors["source_probe"] = message or "published source probe failed"
            return False
        fields = stdout.decode(errors="replace").strip().split()
        if not fields:
            self.errors["source_probe"] = "published source branch was not found"
            return False
        observed = fields[0]
        changed = observed != selected.get("commit")
        suppressed_state: str | None = None
        activation_path = self.instance / "activation.json"
        try:
            activation = json.loads(activation_path.read_text())
        except (OSError, json.JSONDecodeError):
            activation = {}
        if (
            changed
            and activation.get("observed_commit") == observed
            and activation.get("state") in {"maintenance_required", "rejected"}
        ):
            suppressed_state = str(activation["state"])
            changed = False
        self.last_source_probe = {
            "observed_commit": observed,
            "selected_commit": selected.get("commit"),
            "changed": changed,
            "suppressed_state": suppressed_state,
        }
        self.errors.pop("source_probe", None)
        return changed

    async def schedule(self) -> None:
        next_update = 0.0
        next_reconcile = 0.0
        reconcile_seconds = max(1.0, float(self.settings.get("reconcile_seconds", 15)))
        while not self.stop.is_set():
            self.wake.clear()
            now = asyncio.get_running_loop().time()
            if now >= next_reconcile or self.reconcile_requested:
                if self.start_job("background"):
                    self.reconcile_requested = False
                    next_reconcile = now + reconcile_seconds
            requested = self.update_requested
            if now >= next_update or requested:
                self.update_requested = False
                if requested or await self.published_source_changed():
                    if not self.start_job("update"):
                        self.update_requested = True
                next_update = now + (
                    30
                    if "update" in self.errors or "source_probe" in self.errors
                    else 5
                )
            try:
                await asyncio.wait_for(self.wake.wait(), 1)
            except asyncio.TimeoutError:
                pass

    async def run(self) -> None:
        path = self.instance / "resident.sock"
        path.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(
            self.client, path=str(path), limit=64 * 1024 * 1024
        )
        os.chmod(path, 0o600)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stop.set)
        tasks = [
            asyncio.create_task(self.collect()),
            asyncio.create_task(self.schedule()),
        ]
        async with server:
            await self.stop.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.transport.close()
        path.unlink(missing_ok=True)


def main() -> int:
    instance = Path(sys.argv[1])
    settings = json.loads((instance / "resident.json").read_text())
    asyncio.run(Resident(instance, settings).run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
