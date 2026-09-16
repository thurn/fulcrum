"""Measure local commit-to-behavior without providers or production mutations.

Run with .venv/bin/python scripts/measure-live-iteration.py.
Uses a disposable Git repo, real preparation/preflight and fresh interpreters,
and a local WebSocket with a pending approval throughout all trials.
"""

import asyncio
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

from websockets.asyncio.server import serve

from fulcrum.configuration import ConfigurationManager, default_config
from fulcrum.resident import Resident

PROJECT = Path(__file__).resolve().parents[1]


async def main():
    connections = []

    async def native(socket):
        connections.append(socket)
        async for raw in socket:
            message = json.loads(raw)
            if "method" not in message or "id" not in message:
                continue
            await socket.send(json.dumps({"id": message["id"], "result": {}}))
            if message["method"] == "turn/start":
                await socket.send(
                    json.dumps(
                        {
                            "id": 77,
                            "method": "approval",
                            "params": {"threadId": "working"},
                        }
                    )
                )

    async with serve(native, "127.0.0.1", 0) as server:
        with tempfile.TemporaryDirectory(prefix="fulcrum-local-master-") as directory:
            root = Path(directory)
            repo, instance = root / "repo", root / "instance"
            shutil.copytree(
                PROJECT / "src",
                repo / "src",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            shutil.copy(PROJECT / "pyproject.toml", repo / "pyproject.toml")
            instance.mkdir()
            config = instance / "config"
            with config.open("w") as output:
                ConfigurationManager.yaml().dump(default_config(instance), output)
            endpoint = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            settings = {
                "endpoint": endpoint,
                "config": str(config),
                "source": {
                    "repository": str(repo),
                    "remote": "origin",
                    "branch": "master",
                },
            }
            (instance / "resident.json").write_text(json.dumps(settings))
            resident = Resident(instance, settings)
            await resident.transport.connect()
            await resident.transport.request("turn/start", {})
            for _ in range(100):
                if "77" in resident.transport.pending_server_requests:
                    break
                await asyncio.sleep(0.001)
            socket = resident.transport.websocket
            listener = await asyncio.start_unix_server(
                resident.client, path=str(instance / "resident.sock")
            )

            def git(*args):
                return (
                    subprocess.check_output(
                        [
                            "git",
                            "-C",
                            str(repo),
                            "-c",
                            "user.name=Measurement",
                            "-c",
                            "user.email=measurement@example.invalid",
                            "-c",
                            "core.hooksPath=/dev/null",
                            *args,
                        ],
                        stderr=subprocess.DEVNULL,
                    )
                    .decode()
                    .strip()
                )

            git("init", "-b", "master")
            rows = []

            def trial(index):
                package = repo / "src/fulcrum"
                (package / "iteration_probe.py").write_text(
                    "from fulcrum.cli import main as cli_main\n"
                    "from pathlib import Path\n"
                    "def main():\n"
                    f" print({index}, (Path(__file__).parent / 'iteration_asset').read_text()); return 0\n"
                )
                (package / "iteration_asset").write_text(str(index))
                git("add", ".")
                git("commit", "-m", f"test: measure behavior {index}")
                code = (
                    "import sys;sys.path.insert(0,sys.argv.pop(1));"
                    "from fulcrum.bootstrap import main;"
                    "raise SystemExit(main('fulcrum.iteration_probe'))"
                )
                started = time.perf_counter()
                result = subprocess.run(
                    [
                        sys.executable,
                        "-B",
                        "-c",
                        code,
                        str(repo / "src"),
                        "--instance",
                        str(instance),
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                    env={
                        k: v
                        for k, v in os.environ.items()
                        if not k.startswith("FULCRUM_")
                    },
                )
                elapsed = time.perf_counter() - started
                assert result.stdout.strip() == f"{index} {index}", result
                status = json.loads((instance / "activation.json").read_text())
                stages = status["last_activation"]["timings"]
                return {
                    "total": elapsed,
                    **stages,
                    "launch_and_resolution": elapsed - stages["local_activation"],
                }

            try:
                for index in range(20):
                    rows.append(await asyncio.to_thread(trial, index))
                    assert resident.transport.websocket is socket
                    assert "77" in resident.transport.pending_server_requests
                    assert len(connections) == 1
            finally:
                listener.close()
                await listener.wait_closed()
                await resident.transport.close()
            report = {
                "trials": len(rows),
                "p95_seconds": sorted(row["total"] for row in rows)[18],
                "median_seconds": statistics.median(row["total"] for row in rows),
                "stages_mean": {
                    key: statistics.mean(row[key] for row in rows) for key in rows[0]
                },
                "connections": len(connections),
                "pending_approval_preserved": True,
                "remote_configured": False,
                "rows": rows,
            }
            print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
