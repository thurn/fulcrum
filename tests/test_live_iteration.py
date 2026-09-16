"""Architectural promises of a system developed using itself."""

import ast
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from tests import local_python
from fulcrum.activation import cleanup_sources, write_json
from fulcrum.bootstrap import launch_arguments, pin, pinned_selection
from fulcrum.coordination import ProcessLock, merge_change, transition, external_effect
from fulcrum.contracts import FulcrumError
from fulcrum.resident import Resident
from fulcrum.resident_client import ResidentTransport
from fulcrum.transport import RuntimeEvent


class LiveIterationTests(unittest.TestCase):
    def test_resident_import_boundary(self):
        allowed = {"fulcrum.bootstrap", "fulcrum.transport"}
        for name in ("resident", "transport", "bootstrap"):
            source = Path(__file__).parents[1] / "src/fulcrum" / f"{name}.py"
            tree = ast.parse(source.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                    "fulcrum."
                ):
                    if name == "bootstrap" and node.module == "fulcrum.cli":
                        continue  # Only the unselected development launcher branch.
                    self.assertIn(node.module, allowed)

    def test_snapshot_pin_survives_selection_and_delayed_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("before", "after"):
                source = root / "sources" / name
                (source / "src").mkdir(parents=True)
                (source / "src" / "behavior.py").write_text(f"VALUE = '{name}'")
                (source / "asset").write_text(name)
            old = {
                "source": str(root / "sources/before"),
                "python": sys.executable,
                "commit": "before",
            }
            new = {**old, "source": str(root / "sources/after"), "commit": "after"}
            write_json(root / "selected.json", old)
            selected, lease = pinned_selection(root)
            self.assertEqual(selected, old)
            write_json(root / "selected.json", new)
            cleanup_sources(root, new)
            self.assertTrue(Path(old["source"]).exists())
            code = "import sys,pathlib;sys.path.insert(0,sys.argv[1]+'/src');import behavior; print(behavior.VALUE, pathlib.Path(sys.argv[1]+'/asset').read_text())"
            command = [sys.executable, "-B", "-c", code, old["source"]]
            with local_python(command):
                result = subprocess.run(
                    command, capture_output=True, text=True, check=True
                )
            self.assertEqual(result.stdout.strip(), "before before")
            os.close(lease)
            cleanup_sources(root, new)
            self.assertFalse(Path(old["source"]).exists())

    def test_process_lock_and_crash_release(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lock"
            code = "from pathlib import Path;from fulcrum.coordination import ProcessLock;import sys;\nwith ProcessLock(Path(sys.argv[1]),blocking=False): print('acquired')"
            command = [sys.executable, "-B", "-c", code, str(path)]
            with ProcessLock(path), local_python(command):
                result = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            with local_python(command):
                result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.stdout.strip(), "acquired")

    def test_external_effect_yields_state_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code = "from pathlib import Path;from fulcrum.coordination import ProcessLock;import sys;\nwith ProcessLock(Path(sys.argv[1]),blocking=False): print('acquired')"
            command = [
                sys.executable,
                "-B",
                "-c",
                code,
                str(root / ".fulcrum-locks/state"),
            ]
            with transition(root), external_effect(), local_python(command):
                result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.stdout.strip(), "acquired")

    def test_field_updates_preserve_unrelated_concurrent_changes(self):
        self.assertEqual(
            merge_change(
                {"phase": "a", "count": 1},
                {"phase": "b", "count": 1},
                {"phase": "a", "count": 2},
            ),
            {"phase": "b", "count": 2},
        )
        with self.assertRaises(FulcrumError):
            merge_change({"phase": "a"}, {"phase": "b"}, {"phase": "c"})


class ResidentContinuityTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_close_preserves_pending_approval_and_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            resident = Resident(
                Path(directory), {"endpoint": "ws://unused", "config": "/unused"}
            )
            request = RuntimeEvent("approval", {"threadId": "task"}, "5", "now")
            resident.transport.pending_server_requests["5"] = request
            resident.transport.ready = True
            before = await resident.dispatch({"action": "health"})
            client = ResidentTransport(Path(directory) / "resident.sock")
            await client.close()
            after = await resident.dispatch({"action": "health"})
            self.assertEqual(before, after)
            self.assertIn("5", after["pending"])

    async def test_events_remain_until_acknowledged(self):
        resident = Resident(
            Path("/unused"), {"endpoint": "ws://unused", "config": "/unused"}
        )
        resident.events["1"] = {"method": "turn/completed"}
        first = await resident.dispatch({"action": "events"})
        self.assertEqual(first, await resident.dispatch({"action": "events"}))
        await resident.dispatch({"action": "ack", "ids": ["1"]})
        self.assertEqual((await resident.dispatch({"action": "events"}))["events"], [])


class ActivationTests(unittest.TestCase):
    def materialize(self, _repo, commit, destination):
        destination.mkdir(parents=True)
        (destination / "pyproject.toml").write_text(
            '[project]\nrequires-python=">=3.12"\ndependencies=[]\n'
        )
        (destination / "src/fulcrum").mkdir(parents=True)
        for name in ("resident.py", "transport.py", "bootstrap.py"):
            (destination / "src/fulcrum" / name).write_text("# fixed resident\n")
        (destination / "behavior").write_text(commit)

    def test_activation_changes_source_without_restarting_or_installing(self):
        from fulcrum.activation import activate

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "sources/old"
            self.materialize(None, "old", old)
            selected = {"source": str(old), "python": sys.executable, "commit": "old"}
            write_json(root / "selected.json", selected)
            lease = pin(old)
            try:
                with (
                    patch("fulcrum.activation.git", side_effect=["", "new", "new"]),
                    patch("fulcrum.activation.snapshot", side_effect=self.materialize),
                    patch("fulcrum.activation.preflight") as preflight,
                    patch("fulcrum.activation.subprocess.run") as process,
                    patch(
                        "fulcrum.resident_client.exchange",
                        new=AsyncMock(return_value={"ok": True}),
                    ),
                ):
                    result = activate(
                        root,
                        root / "config",
                        {
                            "repository": str(root),
                            "remote": "origin",
                            "branch": "master",
                        },
                    )
                self.assertEqual(result["state"], "activated")
                self.assertEqual(result["selected"]["python"], sys.executable)
                self.assertTrue(old.exists())
                preflight.assert_called_once()
                process.assert_not_called()  # No install, restart, or native interruption.
            finally:
                os.close(lease)

    def test_failed_preflight_keeps_selected_source(self):
        from fulcrum.activation import activate

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "sources/old"
            self.materialize(None, "old", old)
            selected = {"source": str(old), "python": sys.executable, "commit": "old"}
            write_json(root / "selected.json", selected)
            with (
                patch("fulcrum.activation.git", side_effect=["", "new"]),
                patch("fulcrum.activation.snapshot", side_effect=self.materialize),
                patch(
                    "fulcrum.activation.preflight", side_effect=ValueError("bad import")
                ),
            ):
                with self.assertRaisesRegex(ValueError, "bad import"):
                    activate(
                        root,
                        root / "config",
                        {
                            "repository": str(root),
                            "remote": "origin",
                            "branch": "master",
                        },
                    )
            self.assertEqual(json.loads((root / "selected.json").read_text()), selected)
            self.assertEqual(
                json.loads((root / "activation.json").read_text())["state"], "rejected"
            )

    def test_unchanged_published_commit_ignores_worktree_edits(self):
        from fulcrum.activation import activate

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(
                root / "selected.json",
                {"source": str(root), "python": sys.executable, "commit": "same"},
            )
            (root / "dirty.py").write_text("broken source")
            with (
                patch("fulcrum.activation.git", side_effect=["", "same"]),
                patch("fulcrum.activation.snapshot") as build,
            ):
                result = activate(
                    root,
                    root / "config",
                    {"repository": str(root), "remote": "origin", "branch": "master"},
                )
            self.assertEqual(result["state"], "current")
            build.assert_not_called()


class TransportAcrossActivationTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_connection_and_pending_request_survive_source_selection(self):
        from websockets.asyncio.server import serve
        from fulcrum.activation import select_candidate
        from fulcrum.resident_client import exchange

        connections = []
        responses = []

        async def native(socket):
            connections.append(socket)
            async for raw in socket:
                message = json.loads(raw)
                if "method" not in message:
                    responses.append(message)
                    continue
                if "id" not in message:
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
            port = server.sockets[0].getsockname()[1]
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                resident = Resident(
                    root, {"endpoint": f"ws://127.0.0.1:{port}", "config": "/unused"}
                )
                listener = await asyncio.start_unix_server(
                    resident.client, path=str(root / "resident.sock")
                )
                try:
                    await exchange(
                        root / "resident.sock",
                        {"action": "request", "method": "turn/start", "params": {}},
                    )
                    for _ in range(100):
                        if resident.transport.pending_server_requests:
                            break
                        await asyncio.sleep(0.001)
                    self.assertIn("77", resident.transport.pending_server_requests)
                    socket = resident.transport.websocket
                    select_candidate(
                        root,
                        {
                            "source": "new source",
                            "python": sys.executable,
                            "commit": "new",
                        },
                    )
                    await ResidentTransport(root / "resident.sock").close()
                    await exchange(
                        root / "resident.sock",
                        {
                            "action": "respond",
                            "request_id": "77",
                            "response": {"decision": "accept"},
                        },
                    )
                    self.assertIs(resident.transport.websocket, socket)
                    self.assertEqual(len(connections), 1)
                    self.assertNotIn("77", resident.transport.pending_server_requests)
                finally:
                    listener.close()
                    await listener.wait_closed()
                    await resident.transport.close()


class PreparationIsolationTests(unittest.TestCase):
    def test_preparation_lock_does_not_block_source_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with ProcessLock(root / "update.lock"):
                self.assertEqual(pinned_selection(root), (None, None))
