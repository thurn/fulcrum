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

    async def test_slow_publication_does_not_occupy_reconciliation_lane(self):
        resident = Resident(
            Path("/unused"),
            {
                "endpoint": "ws://unused",
                "config": "/unused",
                "reconcile_seconds": 15,
                "publication_seconds": 15,
            },
        )
        publication_release = asyncio.Event()
        started: dict[str, float] = {}

        async def job(kind):
            started[kind] = asyncio.get_running_loop().time()
            if kind == "publication":
                await publication_release.wait()

        resident.job = job
        scheduled = asyncio.create_task(resident.schedule())
        deadline = asyncio.get_running_loop().time() + 1
        while not {"reconcile", "publication"}.issubset(started):
            self.assertLess(asyncio.get_running_loop().time(), deadline)
            await asyncio.sleep(0)

        self.assertFalse(resident.jobs["publication"].done())
        self.assertLess(abs(started["reconcile"] - started["publication"]), 0.05)
        publication_release.set()
        resident.stop.set()
        resident.wake.set()
        await scheduled
        await asyncio.gather(*resident.jobs.values())

    async def test_source_probe_skips_full_update_until_local_commit_changes(self):
        class Process:
            returncode = 0

            def __init__(self, oid):
                self.oid = oid

            async def communicate(self):
                return (
                    f"{self.oid}\trefs/heads/master\n".encode(),
                    b"",
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(
                root / "selected.json",
                {"source": "/selected", "python": sys.executable, "commit": "same"},
            )
            resident = Resident(
                root,
                {
                    "endpoint": "ws://unused",
                    "config": "/unused",
                    "source": {
                        "repository": "/repo",
                        "remote": "origin",
                        "branch": "master",
                    },
                },
            )
            with patch(
                "fulcrum.resident.asyncio.create_subprocess_exec",
                return_value=Process("same"),
            ):
                self.assertFalse(await resident.local_source_changed())
            with patch(
                "fulcrum.resident.asyncio.create_subprocess_exec",
                return_value=Process("new"),
            ):
                self.assertTrue(await resident.local_source_changed())
            self.assertEqual(resident.last_source_probe["observed_commit"], "new")


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
                    patch("fulcrum.activation.git", side_effect=["new", "new"]),
                    patch("fulcrum.activation.snapshot", side_effect=self.materialize),
                    patch("fulcrum.activation.preflight") as preflight,
                    patch("fulcrum.activation.subprocess.run") as process,
                    patch(
                        "fulcrum.install.reconcile_fulcrum2_skills"
                    ) as reconcile_skills,
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
                reconcile_skills.assert_called_once_with(root, production=False)
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
                patch("fulcrum.activation.git", side_effect=["new"]),
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

    def test_unchanged_local_commit_ignores_worktree_edits(self):
        from fulcrum.activation import activate

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(
                root / "selected.json",
                {"source": str(root), "python": sys.executable, "commit": "same"},
            )
            (root / "dirty.py").write_text("broken source")
            with (
                patch("fulcrum.activation.git", side_effect=["same"]),
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
                    with patch(
                        "fulcrum.install.master_source_root",
                        return_value=Path(__file__).parents[1],
                    ):
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


class MaintenanceBoundaryTests(unittest.TestCase):
    def test_shared_gate_allows_worker_threads_and_excludes_maintenance(self):
        import concurrent.futures

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "maintenance"

            def shared():
                with ProcessLock(path, shared=True, blocking=False):
                    return True

            def exclusive():
                with ProcessLock(path, blocking=False):
                    return True

            with (
                ProcessLock(path, shared=True),
                concurrent.futures.ThreadPoolExecutor() as pool,
            ):
                self.assertTrue(pool.submit(shared).result(timeout=1))
                with self.assertRaises(FulcrumError):
                    pool.submit(exclusive).result(timeout=1)


class AssetSelectionTests(unittest.TestCase):
    def test_user_skills_always_link_directly_to_master(self):
        from fulcrum.install import HUMAN_SKILLS
        from fulcrum.activation import select_candidate

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master = root / "fulcrum"
            (master / "skills").mkdir(parents=True)
            (master / "pyproject.toml").write_text("[project]\nname='fulcrum'\n")
            for skill in HUMAN_SKILLS:
                asset = master / "skills" / skill
                (asset / "agents").mkdir(parents=True)
                (asset / "SKILL.md").write_text("master")
                (asset / "agents/openai.yaml").write_text("interface: {}")
            legacy = root / "skills-current"
            legacy.symlink_to(root / "sources/old", target_is_directory=True)
            for name in ("old", "new"):
                source = root / "sources" / name
                source.mkdir(parents=True)
                with patch("fulcrum.install.master_source_root", return_value=master):
                    select_candidate(
                        root,
                        {
                            "source": str(source),
                            "python": sys.executable,
                            "commit": name,
                        },
                    )
            cleanup_sources(root, {"source": str(root / "sources/new")})
            for skill in HUMAN_SKILLS:
                link = root / "codex/skills" / skill
                self.assertEqual((link / "SKILL.md").read_text(), "master")
                self.assertTrue(link.readlink().is_absolute())
                self.assertEqual(link.readlink(), master / "skills" / skill)
            self.assertFalse(legacy.is_symlink())


class ImmediateMasterTests(unittest.TestCase):
    def test_new_launcher_definition_never_replaces_running_resident(self):
        from fulcrum.install import InstalledService, ServiceObservation
        from fulcrum.installation_service import _start_one

        service = InstalledService("controller", "test.resident", Path("/unused"))
        observation = ServiceObservation(
            "test.resident", True, "running", 42, "/old", ("/old",), "/", ""
        )
        with (
            patch(
                "fulcrum.installation_service.inspect_service", return_value=observation
            ),
            patch(
                "fulcrum.installation_service._program_arguments", return_value=["/new"]
            ),
            patch("fulcrum.installation_service._stop_one") as stop,
            patch("fulcrum.installation_service.subprocess.run") as process,
        ):
            self.assertEqual(_start_one(service, endpoint=None)["pid"], 42)
        stop.assert_not_called()
        process.assert_not_called()

    def test_commit_is_visible_to_next_command_without_remote_or_update(self):
        # The outer test permits exactly this local Python process. Its fixture
        # owns its Git repository and child interpreters; no provider is involved.
        code = (Path(__file__).parent / "fixtures/local_master_probe.py").read_text()
        command = [sys.executable, "-B", "-c", code, str(Path(__file__).parents[1])]
        with local_python(command):
            result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(json.loads(result.stdout)["commit_to_command_seconds"], 2)

    def test_reconciliation_worker_refreshes_before_application_dispatch(self):
        from fulcrum.worker import main

        with (
            patch.dict(os.environ, {"FULCRUM_WORKER_SELECTED": ""}),
            patch("sys.argv", ["worker", "reconcile", "/instance", "/config"]),
            patch("fulcrum.bootstrap.main", return_value=0) as launch,
        ):
            self.assertEqual(main(), 0)
        launch.assert_called_once_with(
            "fulcrum.worker",
            ["reconcile", "/instance", "/config"],
            Path("/instance"),
            Path("/config"),
        )

    def test_preparation_failure_cannot_fall_back_to_old_code(self):
        from fulcrum.bootstrap import fresh_selection

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "sources/old"
            old.mkdir(parents=True)
            write_json(
                root / "selected.json",
                {
                    "source": str(old),
                    "commit": "old",
                    "python": sys.executable,
                },
            )
            with (
                patch("fulcrum.bootstrap.local_commit", return_value="new"),
                patch(
                    "fulcrum.bootstrap.subprocess.run",
                    return_value=subprocess.CompletedProcess(
                        [], 1, "", "rejected import"
                    ),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "new: rejected import"):
                    fresh_selection(root, root / "config")
