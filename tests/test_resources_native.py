from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from fulcrum.config import InstallationConfig, ProjectConfig, RuntimePaths
from fulcrum.controller import Controller
from fulcrum.resources import (
    DESCRIPTOR_RESERVE,
    AppServerResourceProbe,
    ResourceSnapshot,
)
from fulcrum.runtime import AppServerError, CodexRuntime, thread_facts


async def _wait_for_baseline(
    probe: AppServerResourceProbe,
    baseline: ResourceSnapshot,
    *,
    deadline: float,
) -> ResourceSnapshot:
    last_observation: ResourceSnapshot | None = None
    while True:
        if time.monotonic() >= deadline:
            raise AssertionError(
                "native resources did not return to baseline within the cleanup "
                f"deadline: baseline={baseline}, last={last_observation}"
            )
        current = probe.snapshot()
        if current == baseline:
            return current
        last_observation = current
        await asyncio.sleep(0.25)


class ResourceBaselineDeadlineTest(unittest.IsolatedAsyncioTestCase):
    async def test_expired_deadline_rejects_even_a_baseline_snapshot(self) -> None:
        baseline = ResourceSnapshot(
            process_id=1,
            soft_limit=256,
            descriptor_count=10,
            child_count=0,
            descriptor_types={"REG": 10},
        )
        probe = Mock(spec=AppServerResourceProbe)
        probe.snapshot.return_value = baseline

        with self.assertRaisesRegex(AssertionError, "cleanup deadline"):
            await _wait_for_baseline(probe, baseline, deadline=time.monotonic() - 0.001)
        probe.snapshot.assert_not_called()


@unittest.skipUnless(
    os.environ.get("FULCRUM_NATIVE_APP_SERVER_TEST") == "1",
    "set FULCRUM_NATIVE_APP_SERVER_TEST=1 for the disposable native regression",
)
class NativeAppServerResourceIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_supported_mix_reclaims_native_resources_at_256_limit(self) -> None:
        if sys.platform != "darwin":
            self.skipTest(
                "native descriptor-type assertions currently require macOS lsof"
            )
        codex = os.environ.get("FULCRUM_TEST_CODEX_BIN") or shutil.which("codex")
        if codex is None:
            self.skipTest("Codex executable is unavailable")
        source_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        auth = source_home / "auth.json"
        if not auth.is_file():
            self.skipTest("an authenticated Codex installation is required")
        resources = Path("/Applications/ChatGPT.app/Contents/Resources")
        node_repl = resources / "cua_node/bin/node_repl"
        node = resources / "cua_node/bin/node"
        node_modules = resources / "cua_node/lib/node_modules"
        if not node_repl.is_file() or not node.is_file():
            self.skipTest("the native Node REPL helper is unavailable")

        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            codex_home = root / "codex-home"
            codex_home.mkdir()
            shutil.copy2(auth, codex_home / "auth.json")
            (codex_home / "config.toml").write_text(
                "\n".join(
                    [
                        "[mcp_servers.node_repl]",
                        f"command = {json.dumps(str(node_repl))}",
                        "startup_timeout_sec = 30",
                        "",
                        "[mcp_servers.node_repl.env]",
                        'NODE_REPL_NATIVE_PIPE_CONNECT_TIMEOUT_MS = "1000"',
                        f"NODE_REPL_NODE_MODULE_DIRS = {json.dumps(str(node_modules))}",
                        f"NODE_REPL_NODE_PATH = {json.dumps(str(node))}",
                        f"NODE_REPL_TRUSTED_CODE_PATHS = {json.dumps(str(codex_home))}",
                        f"CODEX_HOME = {json.dumps(str(codex_home))}",
                        f"CODEX_CLI_PATH = {json.dumps(codex)}",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = int(reservation.getsockname()[1])
            endpoint = f"ws://127.0.0.1:{port}"
            environment = dict(os.environ)
            environment["CODEX_HOME"] = str(codex_home)
            server = subprocess.Popen(
                [
                    "/bin/zsh",
                    "-c",
                    'ulimit -n 256; exec "$@"',
                    "fulcrum-native-app-server",
                    codex,
                    "app-server",
                    "--listen",
                    endpoint,
                ],
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            runtime = CodexRuntime(endpoint)
            controller: Controller | None = None
            thread_ids: list[str] = []
            try:
                await self._connect(runtime, server)
                probe = AppServerResourceProbe(pid_provider=lambda: server.pid)
                startup = await self._wait_stable(probe)

                # Warm the supported concurrency width before defining the
                # baseline so lazy global database/network pools are not
                # mistaken for per-conversation resources. Model transport
                # sockets are transient, so do not freeze the warmed baseline
                # until they return to the stable pre-model count.
                warm_ids = []
                for index in range(8):
                    warm = await runtime.create_thread(
                        cwd=str(root),
                        workspace_root=str(root),
                        model="gpt-5.6-sol",
                        project_id=None,
                    )
                    warm_id = str(warm["thread"]["id"])
                    warm_ids.append(warm_id)
                    await runtime.set_name(
                        warm_id, f"Fulcrum native resource warmup {index}"
                    )
                warm_turns = await asyncio.gather(
                    *(
                        runtime.start_turn(
                            warm_id,
                            "Reply with exactly: OK. Do not use tools or modify files.",
                            cwd=str(root),
                            workspace_root=str(root),
                            model="gpt-5.6-sol",
                            effort="low",
                            correlation=f"fulcrum-native-resource-warmup-{index}",
                        )
                        for index, warm_id in enumerate(warm_ids)
                    )
                )
                await asyncio.gather(
                    *(
                        self._wait_terminal(runtime, warm_id, warm_turn)
                        for warm_id, warm_turn in zip(warm_ids, warm_turns, strict=True)
                    )
                )
                for warm_id in warm_ids:
                    await runtime.archive(warm_id)
                baseline = await self._wait_stable(
                    probe,
                    maximum_ipv4=startup.descriptor_types.get("IPv4", 0),
                    timeout=30,
                )

                source = root / "source"
                source.mkdir()
                paths = RuntimePaths(
                    brain_root=root / "brain",
                    state_root=root / "state",
                    config_file=root / "config.json",
                    control_root=root / "control",
                )
                config = InstallationConfig(
                    source_root=str(source),
                    brain_root=str(paths.brain_root),
                    state_root=str(paths.state_root),
                    codex_bin=codex,
                    desktop_executable="/Applications/ChatGPT.app/ChatGPT",
                    projects=[ProjectConfig("p", str(source))],
                )
                controller = Controller(paths, config)
                controller._initialize_configuration()
                controller.runtime = runtime
                controller.resource_probe = probe

                roles = (
                    "executor",
                    "overseer",
                    "sage",
                    "inquisitor",
                    "executor",
                    "overseer",
                    "sage",
                    "inquisitor",
                )
                tasks = []
                for index, role in enumerate(roles):
                    result = await runtime.create_thread(
                        cwd=str(source),
                        workspace_root=str(source),
                        model="gpt-5.6-sol",
                        project_id=None,
                    )
                    thread_id = str(result["thread"]["id"])
                    thread_ids.append(thread_id)
                    task = controller.store.register_task(
                        native_thread_id=thread_id,
                        role=role,
                        description=f"Native resource worker {index}",
                        model="gpt-5.6-sol",
                        reasoning_effort="low",
                        project_id="p",
                        state="active" if index < 4 else "idle",
                    )
                    tasks.append(task)
                    await runtime.set_name(thread_id, str(task["title"]))

                helper_peak = probe.snapshot()
                turn_ids = await asyncio.gather(
                    *(
                        runtime.start_turn(
                            thread_id,
                            "Reply with exactly: OK. Do not use tools or modify files.",
                            cwd=str(source),
                            workspace_root=str(source),
                            model="gpt-5.6-sol",
                            effort="low",
                            correlation=f"fulcrum-native-resource-{index}",
                        )
                        for index, thread_id in enumerate(thread_ids)
                    )
                )
                session_peak = probe.snapshot()
                self.assertEqual(helper_peak.soft_limit, 256)
                self.assertGreater(helper_peak.child_count, baseline.child_count)
                self.assertGreater(
                    helper_peak.descriptor_types.get("PIPE", 0),
                    baseline.descriptor_types.get("PIPE", 0),
                )
                self.assertGreater(
                    helper_peak.descriptor_types.get("IPv4", 0),
                    baseline.descriptor_types.get("IPv4", 0),
                )
                self.assertGreater(
                    session_peak.session_descriptor_count,
                    baseline.session_descriptor_count,
                )
                self.assertGreater(
                    session_peak.locked_descriptor_count,
                    baseline.locked_descriptor_count,
                )
                self.assertGreaterEqual(
                    min(
                        helper_peak.available_descriptors,
                        session_peak.available_descriptors,
                    ),
                    DESCRIPTOR_RESERVE,
                )

                await asyncio.gather(
                    *(
                        self._wait_terminal(runtime, thread_id, turn_id)
                        for thread_id, turn_id in zip(thread_ids, turn_ids, strict=True)
                    )
                )
                for index in range(100):
                    await runtime.read_thread(
                        thread_ids[index % len(thread_ids)], include_turns=False
                    )
                loaded = await runtime.request(
                    "thread/loaded/list", {"cursor": None, "limit": 100}
                )
                self.assertTrue(set(thread_ids).issubset(set(loaded.get("data", []))))

                controller.store.execute(
                    """UPDATE tasks SET resource_idle_since = '2026-01-01T00:00:00Z'
                       WHERE state = 'idle'"""
                )
                cleanup_deadline = time.monotonic() + 10
                await controller._maintain_resource_lifecycle()
                parked = controller.store.row(
                    "SELECT COUNT(*) AS count FROM tasks WHERE resource_reclaimed_at IS NOT NULL"
                )
                self.assertEqual(parked["count"], 4)

                controller.store.execute(
                    """UPDATE tasks SET state = 'idle', runtime_status = 'idle',
                       resource_idle_since = '2026-01-01T00:00:00Z'
                       WHERE state = 'active'"""
                )
                await controller._maintain_resource_lifecycle()
                parked = controller.store.row(
                    "SELECT COUNT(*) AS count FROM tasks WHERE resource_reclaimed_at IS NOT NULL"
                )
                self.assertEqual(parked["count"], 8)
                loaded = await runtime.request(
                    "thread/loaded/list", {"cursor": None, "limit": 100}
                )
                self.assertTrue(set(thread_ids).isdisjoint(set(loaded.get("data", []))))

                final = await _wait_for_baseline(
                    probe, baseline, deadline=cleanup_deadline
                )
                self.assertEqual(final.child_count, baseline.child_count)
                self.assertEqual(
                    final.descriptor_count,
                    baseline.descriptor_count,
                    (baseline, final),
                )
                self.assertEqual(final.descriptor_types, baseline.descriptor_types)
                self.assertEqual(
                    final.session_descriptor_count, baseline.session_descriptor_count
                )
                self.assertEqual(
                    final.locked_descriptor_count, baseline.locked_descriptor_count
                )
            finally:
                if controller is not None:
                    controller.store.close()
                    if controller.lock_handle is not None:
                        controller.lock_handle.close()
                for thread_id in thread_ids:
                    try:
                        await runtime.archive(thread_id)
                    except AppServerError:
                        pass
                await runtime.close()
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)

    async def _connect(
        self, runtime: CodexRuntime, server: subprocess.Popen[bytes]
    ) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if server.poll() is not None:
                self.fail(f"disposable app-server exited with {server.returncode}")
            try:
                await runtime.connect()
                return
            except AppServerError:
                await asyncio.sleep(0.1)
        self.fail("disposable app-server did not become ready")

    async def _wait_terminal(
        self, runtime: CodexRuntime, thread_id: str, turn_id: str
    ) -> None:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                facts = thread_facts(await runtime.read_thread(thread_id))
            except AppServerError:
                await asyncio.sleep(0.1)
                continue
            if (
                facts["last_turn_id"] == turn_id
                and facts["last_turn_terminal"]
                and facts["can_start"]
            ):
                return
            await asyncio.sleep(0.1)
        await runtime.interrupt(thread_id, turn_id)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            facts = thread_facts(await runtime.read_thread(thread_id))
            if (
                facts["last_turn_id"] == turn_id
                and facts["last_turn_terminal"]
                and facts["can_start"]
            ):
                return
            await asyncio.sleep(0.1)
        self.fail(f"native turn {turn_id} did not settle after bounded interrupt")

    async def _wait_stable(
        self,
        probe: AppServerResourceProbe,
        *,
        maximum_ipv4: int | None = None,
        timeout: float = 10,
    ) -> ResourceSnapshot:
        previous = probe.snapshot()
        stable = 0
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(0.25)
            current = probe.snapshot()
            within_socket_baseline = (
                maximum_ipv4 is None
                or current.descriptor_types.get("IPv4", 0) <= maximum_ipv4
            )
            if within_socket_baseline and current == previous:
                stable += 1
                if stable == 4:
                    return current
            else:
                stable = 0
                previous = current
        self.fail(
            f"native app-server did not reach a stable baseline within {timeout:g} "
            "seconds"
        )


if __name__ == "__main__":
    unittest.main()
