from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fulcrum.config import InstallationConfig, ProjectConfig, RuntimePaths
from fulcrum.controller import Controller, ResourceAdmissionPaused
from fulcrum.resources import (
    DESCRIPTOR_RESERVE,
    DESCRIPTOR_START_ALLOWANCE,
    ResourceSnapshot,
    _parse_lsof_descriptors,
    bounded_condition,
)
from fulcrum.runtime import AppServerError
from fulcrum.store import StoreError


class SequenceProbe:
    def __init__(self, *counts: int) -> None:
        self.counts = list(counts)

    def snapshot(self) -> ResourceSnapshot:
        count = self.counts.pop(0) if len(self.counts) > 1 else self.counts[0]
        return ResourceSnapshot(
            process_id=123,
            soft_limit=256,
            descriptor_count=count,
            child_count=8,
            descriptor_types={"PIPE": count // 2},
        )


class ParkingRuntime:
    def __init__(
        self, *, active: bool = False, successful_archives: int | None = None
    ) -> None:
        self.ready = True
        self.active = active
        self.successful_archives = successful_archives
        self.archived: list[str] = []

    async def set_name(self, _thread_id: str, _name: str) -> None:
        return None

    async def read_thread(
        self, thread_id: str, *, include_turns: bool = True
    ) -> dict[str, object]:
        return {
            "id": thread_id,
            "name": "[EXE0001] Resource worker",
            "projectId": "codex-p",
            "status": {"type": "active" if self.active else "idle"},
            "archived": thread_id in self.archived,
            "turns": [
                {
                    "id": "turn-1",
                    "status": "inProgress" if self.active else "completed",
                    "items": [],
                }
            ],
        }

    async def archive(self, thread_id: str) -> None:
        if (
            self.successful_archives is not None
            and len(self.archived) >= self.successful_archives
        ):
            raise AppServerError("native archive failed")
        self.archived.append(thread_id)


class ControllerResourceLifecycleTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        source = root / "source"
        source.mkdir()
        self.paths = RuntimePaths(
            brain_root=root / "brain",
            state_root=root / "state",
            config_file=root / "config.json",
            control_root=root / "control",
        )
        self.config = InstallationConfig(
            source_root=str(source),
            brain_root=str(self.paths.brain_root),
            state_root=str(self.paths.state_root),
            codex_bin="/bin/codex",
            desktop_executable="/Applications/ChatGPT.app/ChatGPT",
            projects=[ProjectConfig("p", str(source), codex_project_id="codex-p")],
        )
        self.controller = Controller(self.paths, self.config)
        self.controller._initialize_configuration()

    def tearDown(self) -> None:
        self.controller.store.close()
        if self.controller.lock_handle is not None:
            self.controller.lock_handle.close()
        self.temporary.cleanup()

    async def test_pressure_parks_safe_idle_worker_and_recovers_headroom(self) -> None:
        task = self.controller.store.register_task(
            native_thread_id="worker",
            role="executor",
            description="Resource worker",
            model="sol",
            reasoning_effort="high",
            project_id="p",
        )
        self.controller.store.execute(
            "UPDATE tasks SET resource_idle_since = '2026-01-01T00:00:00Z' WHERE id = ?",
            (task["id"],),
        )
        runtime = ParkingRuntime()
        self.controller.runtime = runtime  # type: ignore[assignment]
        self.controller.resource_probe = SequenceProbe(200, 180)  # type: ignore[assignment]

        await self.controller._maintain_resource_lifecycle()

        retained = self.controller.store.row(
            "SELECT * FROM tasks WHERE id = ?", (task["id"],)
        )
        self.assertEqual(runtime.archived, ["worker"])
        self.assertIsNotNone(retained["resource_reclaimed_at"])
        self.assertEqual(retained["state"], "idle")
        self.assertEqual(
            self.controller.store.row(
                "SELECT value FROM meta WHERE key = 'resource_admission_condition'"
            )["value"],
            "",
        )

    async def test_pressure_recovery_enforces_idle_limit_before_resuming(self) -> None:
        tasks = [
            self.controller.store.register_task(
                native_thread_id=f"worker-{index}",
                role="executor",
                description=f"Resource worker {index}",
                model="sol",
                reasoning_effort="high",
                project_id="p",
            )
            for index in range(6)
        ]
        runtime = ParkingRuntime()
        self.controller.runtime = runtime  # type: ignore[assignment]
        self.controller.resource_probe = SequenceProbe(200, 180)  # type: ignore[assignment]

        await self.controller._maintain_resource_lifecycle()

        self.assertEqual(runtime.archived, ["worker-0", "worker-1"])
        resident = self.controller.store.row(
            """SELECT COUNT(*) AS count FROM tasks WHERE state = 'idle'
               AND archived = 0 AND resource_reclaimed_at IS NULL"""
        )
        self.assertEqual(resident["count"], 4)
        self.assertEqual(
            sum(
                self.controller.store.row(
                    "SELECT resource_reclaimed_at FROM tasks WHERE id = ?",
                    (task["id"],),
                )["resource_reclaimed_at"]
                is not None
                for task in tasks
            ),
            2,
        )
        self.assertEqual(
            self.controller.store.row(
                "SELECT value FROM meta WHERE key = 'resource_admission_condition'"
            )["value"],
            "",
        )
        lifecycle_events = self.controller.store.rows(
            """SELECT kind FROM events WHERE kind IN (
                   'conversation_resources_reclaimed', 'resource_admission_resumed')
               ORDER BY id"""
        )
        self.assertEqual(
            [event["kind"] for event in lifecycle_events],
            [
                "conversation_resources_reclaimed",
                "conversation_resources_reclaimed",
                "resource_admission_resumed",
            ],
        )

    async def test_admission_stays_paused_when_idle_limit_parking_fails(self) -> None:
        for index in range(6):
            self.controller.store.register_task(
                native_thread_id=f"worker-{index}",
                role="executor",
                description=f"Resource worker {index}",
                model="sol",
                reasoning_effort="high",
                project_id="p",
            )
        runtime = ParkingRuntime(successful_archives=1)
        self.controller.runtime = runtime  # type: ignore[assignment]
        self.controller.resource_probe = SequenceProbe(200, 180)  # type: ignore[assignment]

        await self.controller._maintain_resource_lifecycle()

        self.assertEqual(runtime.archived, ["worker-0"])
        resident = self.controller.store.row(
            """SELECT COUNT(*) AS count FROM tasks WHERE state = 'idle'
               AND archived = 0 AND resource_reclaimed_at IS NULL"""
        )
        self.assertEqual(resident["count"], 5)
        retained_condition = self.controller.store.row(
            "SELECT value FROM meta WHERE key = 'resource_admission_condition'"
        )["value"]
        self.assertTrue(retained_condition)

        with self.assertRaisesRegex(
            ResourceAdmissionPaused,
            "5 eligible idle worker conversations remain resident",
        ):
            await self.controller._admit_runtime_start("turn start", "next")

        condition = self.controller.store.row(
            "SELECT value FROM meta WHERE key = 'resource_admission_condition'"
        )["value"]
        self.assertTrue(condition)
        self.assertIn("supported maximum of 4", condition)
        self.assertEqual(
            self.controller.store.row(
                "SELECT COUNT(*) AS count FROM events WHERE kind = 'resource_admission_resumed'"
            )["count"],
            0,
        )

    async def test_restart_reassociates_active_parked_thread_without_interrupting(
        self,
    ) -> None:
        task = self.controller.store.register_task(
            native_thread_id="worker",
            role="executor",
            description="Resource worker",
            model="sol",
            reasoning_effort="high",
            project_id="p",
        )
        self.controller.store.execute(
            "UPDATE tasks SET resource_reclaimed_at = '2026-01-01T00:00:00Z' WHERE id = ?",
            (task["id"],),
        )
        runtime = ParkingRuntime(active=True)
        self.controller.runtime = runtime  # type: ignore[assignment]
        self.controller.resource_probe = SequenceProbe(100)  # type: ignore[assignment]

        await self.controller._maintain_resource_lifecycle()

        retained = self.controller.store.row(
            "SELECT * FROM tasks WHERE id = ?", (task["id"],)
        )
        self.assertEqual(runtime.archived, [])
        self.assertIsNone(retained["resource_reclaimed_at"])
        self.assertEqual(retained["state"], "active")
        self.assertEqual(retained["last_turn_terminal"], 0)

    async def test_restart_tracks_active_retired_thread_until_terminal_cleanup(
        self,
    ) -> None:
        task = self.controller.store.register_task(
            native_thread_id="retired-worker",
            role="executor",
            description="Retired resource worker",
            model="sol",
            reasoning_effort="high",
            project_id="p",
            state="retired",
        )
        self.controller.store.execute(
            "UPDATE tasks SET archived = 1 WHERE id = ?", (task["id"],)
        )
        runtime = ParkingRuntime(active=True)
        self.controller.runtime = runtime  # type: ignore[assignment]
        self.controller.resource_probe = SequenceProbe(100)  # type: ignore[assignment]

        await self.controller._maintain_resource_lifecycle()

        retained = self.controller.store.row(
            "SELECT * FROM tasks WHERE id = ?", (task["id"],)
        )
        self.assertEqual(runtime.archived, [])
        self.assertEqual(retained["state"], "retired")
        self.assertEqual(retained["archived"], 0)
        self.assertEqual(retained["resource_orphan_active"], 1)
        self.assertEqual(retained["last_turn_terminal"], 0)
        for index, role in enumerate(("overseer", "sage", "inquisitor"), start=1):
            self.controller.store.register_task(
                native_thread_id=f"other-active-{index}",
                role=role,
                description=f"Other active {index}",
                model="sol",
                reasoning_effort="high",
                project_id="p",
                state="active",
            )
        with self.assertRaisesRegex(ResourceAdmissionPaused, "supported maximum of 4"):
            await self.controller._admit_runtime_start("turn start", "next")

        runtime.active = False
        await self.controller._maintain_resource_lifecycle()
        retained = self.controller.store.row(
            "SELECT * FROM tasks WHERE id = ?", (task["id"],)
        )
        self.assertEqual(runtime.archived, ["retired-worker"])
        self.assertEqual(retained["state"], "retired")
        self.assertEqual(retained["archived"], 1)
        self.assertEqual(retained["resource_orphan_active"], 0)

    async def test_restart_tracks_active_archived_thread_until_terminal_cleanup(
        self,
    ) -> None:
        task = self.controller.store.register_task(
            native_thread_id="archived-worker",
            role="sage",
            description="Archived resource worker",
            model="sol",
            reasoning_effort="high",
            project_id="p",
            state="archived",
        )
        self.controller.store.execute(
            "UPDATE tasks SET archived = 1 WHERE id = ?", (task["id"],)
        )
        runtime = ParkingRuntime(active=True)
        self.controller.runtime = runtime  # type: ignore[assignment]
        self.controller.resource_probe = SequenceProbe(100)  # type: ignore[assignment]

        await self.controller._maintain_resource_lifecycle()

        retained = self.controller.store.row(
            "SELECT * FROM tasks WHERE id = ?", (task["id"],)
        )
        self.assertEqual(runtime.archived, [])
        self.assertEqual(retained["state"], "archived")
        self.assertEqual(retained["archived"], 0)
        self.assertEqual(retained["resource_orphan_active"], 1)

        runtime.active = False
        await self.controller._maintain_resource_lifecycle()
        retained = self.controller.store.row(
            "SELECT * FROM tasks WHERE id = ?", (task["id"],)
        )
        self.assertEqual(runtime.archived, ["archived-worker"])
        self.assertEqual(retained["state"], "archived")
        self.assertEqual(retained["archived"], 1)
        self.assertEqual(retained["resource_orphan_active"], 0)

    async def test_idle_worker_is_parked_at_ten_minute_boundary(self) -> None:
        task = self.controller.store.register_task(
            native_thread_id="worker",
            role="executor",
            description="Resource worker",
            model="sol",
            reasoning_effort="high",
            project_id="p",
        )
        self.controller.store.execute(
            "UPDATE tasks SET resource_idle_since = '2026-01-01T00:00:00Z' WHERE id = ?",
            (task["id"],),
        )
        runtime = ParkingRuntime()
        self.controller.runtime = runtime  # type: ignore[assignment]
        self.controller.resource_probe = SequenceProbe(100)  # type: ignore[assignment]

        with patch("fulcrum.controller.utc_now", return_value="2026-01-01T00:09:59Z"):
            await self.controller._maintain_resource_lifecycle()
        self.assertEqual(runtime.archived, [])

        with patch("fulcrum.controller.utc_now", return_value="2026-01-01T00:10:00Z"):
            await self.controller._maintain_resource_lifecycle()
        self.assertEqual(runtime.archived, ["worker"])

    async def test_pressure_refuses_thread_creation_before_runtime_call(self) -> None:
        runtime = AsyncMock()
        runtime.ready = True
        self.controller.runtime = runtime
        self.controller.resource_probe = SequenceProbe(200)  # type: ignore[assignment]
        project = self.controller.store.row(
            "SELECT * FROM projects WHERE project_id = 'p'"
        )

        with self.assertRaisesRegex(
            ResourceAdmissionPaused, "resource admission paused"
        ):
            await self.controller._provision_task(
                role="sage",
                description="Blocked start",
                project=project,
                model="sol",
                effort="high",
            )

        runtime.create_thread.assert_not_awaited()
        condition = self.controller.store.row(
            "SELECT value FROM meta WHERE key = 'resource_admission_condition'"
        )["value"]
        self.assertLessEqual(len(condition), 500)
        self.assertIn("200/256 descriptors", condition)

    async def test_pressure_refuses_setup_smoke_before_thread_creation(self) -> None:
        runtime = AsyncMock()
        runtime.ready = True
        self.controller.runtime = runtime
        self.controller.resource_probe = SequenceProbe(200)  # type: ignore[assignment]

        with self.assertRaisesRegex(
            ResourceAdmissionPaused, "setup smoke thread start"
        ):
            await self.controller._setup_smoke_check()

        runtime.create_thread.assert_not_awaited()
        self.assertIsNone(
            self.controller.store.row(
                "SELECT id FROM external_operations WHERE kind = 'setup_runtime_smoke'"
            )
        )

    async def test_pressure_after_setup_thread_creation_reclaims_partial_thread(
        self,
    ) -> None:
        runtime = AsyncMock()
        runtime.ready = True
        runtime.create_thread.return_value = {"thread": {"id": "partial-smoke"}}
        runtime.archive.side_effect = AppServerError("no rollout found")
        self.controller.runtime = runtime
        self.controller.resource_probe = SequenceProbe(100, 200)  # type: ignore[assignment]

        with self.assertRaisesRegex(
            StoreError, "shared runtime visibility smoke check failed"
        ):
            await self.controller._setup_smoke_check()

        runtime.create_thread.assert_awaited_once()
        runtime.archive.assert_awaited_once_with("partial-smoke")
        runtime.unsubscribe.assert_awaited_once_with("partial-smoke")
        runtime.set_name.assert_not_awaited()
        runtime.start_turn.assert_not_awaited()
        operation = self.controller.store.row(
            "SELECT * FROM external_operations WHERE kind = 'setup_runtime_smoke'"
        )
        self.assertEqual(operation["state"], "failed")
        self.assertEqual(operation["native_id"], "partial-smoke")
        self.assertEqual(
            operation["result_json"],
            '{"partial_thread_archived": false, "partial_thread_unsubscribed": true}',
        )

    async def test_setup_reconciliation_requires_exact_completed_correlated_turn(
        self,
    ) -> None:
        operation_id = self.controller.store.create_operation(
            "setup_runtime_smoke",
            "p",
            {"cwd": self.config.source_root, "project_id": "codex-p"},
        )
        attempt = self.controller.store.begin_operation_attempt(operation_id)
        self.controller.store.finish_operation_attempt(
            operation_id,
            attempt,
            state="uncertain",
            native_id="smoke-thread",
            error="response lost",
        )
        runtime = AsyncMock()
        runtime.ready = True
        runtime.read_thread.return_value = {
            "id": "smoke-thread",
            "name": "Fulcrum setup visibility check",
            "projectId": "codex-p",
            "status": {"type": "idle"},
            "turns": [
                {
                    "id": "unrelated-turn",
                    "status": "completed",
                    "items": [{"type": "userMessage", "clientId": "other"}],
                }
            ],
        }
        self.controller.runtime = runtime
        operation = self.controller.store.row(
            "SELECT * FROM external_operations WHERE id = ?", (operation_id,)
        )

        await self.controller._reconcile_setup_runtime_smoke(operation)

        retained = self.controller.store.row(
            "SELECT * FROM external_operations WHERE id = ?", (operation_id,)
        )
        self.assertEqual(retained["state"], "uncertain")
        self.assertNotEqual(retained["state"], "complete")
        runtime.archive.assert_not_awaited()

    async def test_archive_reconciliation_retries_unsubscribe_before_completion(
        self,
    ) -> None:
        task = self.controller.store.register_task(
            native_thread_id="archived-but-loaded",
            role="executor",
            description="Archive reconciliation worker",
            model="sol",
            reasoning_effort="high",
            project_id="p",
        )
        self.controller.store.execute(
            """INSERT INTO obligations(kind, identity, target, state, created_at, updated_at)
               VALUES ('archive', 'archive-reconciliation', ?, 'pending',
                       '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')""",
            (task["native_thread_id"],),
        )
        operation_id = self.controller.store.create_operation(
            "thread_archive", str(task["native_thread_id"]), {}
        )
        attempt = self.controller.store.begin_operation_attempt(operation_id)
        self.controller.store.finish_operation_attempt(
            operation_id,
            attempt,
            state="uncertain",
            error="archive response was lost",
        )
        runtime = AsyncMock()
        runtime.ready = True
        runtime.read_thread.return_value = {
            "id": task["native_thread_id"],
            "archived": True,
            "status": {"type": "notLoaded"},
        }
        runtime.unsubscribe.side_effect = AppServerError("unsubscribe timed out")
        self.controller.runtime = runtime
        operation = self.controller.store.row(
            "SELECT * FROM external_operations WHERE id = ?", (operation_id,)
        )

        await self.controller._reconcile_thread_archive(operation)

        retained = self.controller.store.row(
            "SELECT * FROM external_operations WHERE id = ?", (operation_id,)
        )
        obligation = self.controller.store.row(
            "SELECT * FROM obligations WHERE kind = 'archive' AND target = ?",
            (task["native_thread_id"],),
        )
        retained_task = self.controller.store.row(
            "SELECT * FROM tasks WHERE id = ?", (task["id"],)
        )
        self.assertEqual(retained["state"], "uncertain")
        self.assertEqual(retained["reconciliation_used"], 0)
        self.assertIn("unsubscribe remains unconfirmed", retained["condition"])
        self.assertEqual(obligation["state"], "failed")
        self.assertNotEqual(retained_task["state"], "archived")

        runtime.unsubscribe.side_effect = None
        operation = self.controller.store.row(
            "SELECT * FROM external_operations WHERE id = ?", (operation_id,)
        )
        await self.controller._reconcile_thread_archive(operation)

        retained = self.controller.store.row(
            "SELECT * FROM external_operations WHERE id = ?", (operation_id,)
        )
        obligation = self.controller.store.row(
            "SELECT * FROM obligations WHERE kind = 'archive' AND target = ?",
            (task["native_thread_id"],),
        )
        retained_task = self.controller.store.row(
            "SELECT * FROM tasks WHERE id = ?", (task["id"],)
        )
        self.assertEqual(runtime.unsubscribe.await_count, 2)
        self.assertEqual(retained["state"], "complete")
        self.assertEqual(obligation["state"], "complete")
        self.assertEqual(retained_task["state"], "archived")

    async def test_supported_active_limit_refuses_another_start(self) -> None:
        for number, role in enumerate(
            ("executor", "overseer", "sage", "inquisitor"), start=1
        ):
            self.controller.store.register_task(
                native_thread_id=f"active-{number}",
                role=role,
                description=f"Active {number}",
                model="sol",
                reasoning_effort="high",
                project_id="p",
                state="active",
            )
        runtime = AsyncMock()
        runtime.ready = True
        self.controller.runtime = runtime
        self.controller.resource_probe = SequenceProbe(100)  # type: ignore[assignment]
        project = self.controller.store.row(
            "SELECT * FROM projects WHERE project_id = 'p'"
        )

        with self.assertRaisesRegex(ResourceAdmissionPaused, "supported maximum of 4"):
            await self.controller._provision_task(
                role="sage",
                description="Fifth active start",
                project=project,
                model="sol",
                effort="high",
            )

        runtime.create_thread.assert_not_awaited()


class ResourcePolicyTest(unittest.TestCase):
    def test_lsof_parser_counts_thread_writer_lock_paths_without_lock_status(
        self,
    ) -> None:
        descriptors, types, sessions, locked = _parse_lsof_descriptors(
            "\n".join(
                (
                    "p123",
                    "f7",
                    "tREG",
                    "n/Users/test/.codex/thread-writer-locks/thread-1.lock",
                    "f8",
                    "tREG",
                    "l ",
                    "n/tmp/unlocked.txt",
                    "f9",
                    "tREG",
                    "lW",
                    "n/tmp/kernel-locked.txt",
                    "f10",
                    "tREG",
                    "n/Users/test/.codex/sessions/2026/thread-1.jsonl",
                    "ftxt",
                    "tREG",
                    "n/Users/test/.codex/thread-writer-locks/not-an-fd.lock",
                )
            )
        )

        self.assertEqual(descriptors, {7, 8, 9, 10})
        self.assertEqual(types, {7: "REG", 8: "REG", 9: "REG", 10: "REG"})
        self.assertEqual(sessions, {10})
        self.assertEqual(locked, {7, 9})

    def test_boundary_requires_reserve_and_start_allowance(self) -> None:
        admitted = ResourceSnapshot(
            process_id=1,
            soft_limit=256,
            descriptor_count=256 - DESCRIPTOR_RESERVE - DESCRIPTOR_START_ALLOWANCE,
            child_count=8,
            descriptor_types={"PIPE": 32},
        )
        refused = ResourceSnapshot(
            process_id=1,
            soft_limit=256,
            descriptor_count=admitted.descriptor_count + 1,
            child_count=8,
            descriptor_types={"PIPE": 34},
        )

        self.assertTrue(admitted.admits_start())
        self.assertFalse(refused.admits_start())
        condition = bounded_condition("resource admission paused", refused.detail())
        self.assertLessEqual(len(condition), 500)
        self.assertIn("finish/archive unrelated Codex conversations", condition)

    def test_256_descriptor_stress_preserves_reserve_and_returns_to_baseline(
        self,
    ) -> None:
        script = textwrap.dedent(f"""
            import os
            import fcntl
            import resource
            import subprocess
            import sys
            import tempfile
            from pathlib import Path
            from fulcrum.resources import AppServerResourceProbe

            resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
            probe = AppServerResourceProbe(pid_provider=os.getpid)
            baseline = probe.snapshot()
            helpers = []
            pipes = []
            session_files = []
            temporary = tempfile.TemporaryDirectory()
            try:
                # Four active and four idle conversations are the documented
                # supported stress mix. Each helper owns three parent-side pipes,
                # a session file, and a writer lock.
                for number in range(8):
                    helpers.append(subprocess.Popen(
                        [sys.executable, "-c", "import time; time.sleep(30)"],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    ))
                    descriptor = os.open(
                        Path(temporary.name) / f"session-{{number}}.jsonl",
                        os.O_CREAT | os.O_RDWR,
                        0o600,
                    )
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    session_files.append(descriptor)
                while True:
                    snapshot = probe.snapshot()
                    if snapshot.available_descriptors <= {DESCRIPTOR_RESERVE + DESCRIPTOR_START_ALLOWANCE + 1}:
                        break
                    pipes.append(os.pipe())
                stressed = probe.snapshot()
                assert stressed.admits_start(), stressed
                assert stressed.child_count == baseline.child_count + 8, stressed
                for _ in range(100):
                    subprocess.run(
                        [sys.executable, "-c", "pass"],
                        check=True,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
            finally:
                for read_fd, write_fd in pipes:
                    os.close(read_fd)
                    os.close(write_fd)
                for descriptor in session_files:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                    os.close(descriptor)
                for helper in helpers:
                    helper.terminate()
                for helper in helpers:
                    helper.wait(timeout=5)
                    for stream in (helper.stdin, helper.stdout, helper.stderr):
                        if stream is not None:
                            stream.close()
                temporary.cleanup()
            final = probe.snapshot()
            assert final.descriptor_count == baseline.descriptor_count, (baseline, final)
            assert final.child_count == baseline.child_count, (baseline, final)
            """)
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_controller_reclamation_returns_helpers_and_descriptors_to_baseline(
        self,
    ) -> None:
        script = textwrap.dedent("""
            import asyncio
            import fcntl
            import os
            import resource
            import subprocess
            import sys
            import tempfile
            from pathlib import Path
            from fulcrum.config import InstallationConfig, ProjectConfig, RuntimePaths
            from fulcrum.controller import Controller
            from fulcrum.resources import AppServerResourceProbe

            class Runtime:
                ready = True
                def __init__(self, root):
                    self.root = root
                    self.resources = {}
                def add(self, thread_id):
                    child = subprocess.Popen(
                        [sys.executable, "-c", "import time; time.sleep(30)"],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    descriptor = os.open(
                        self.root / f"{thread_id}.jsonl",
                        os.O_CREAT | os.O_RDWR, 0o600,
                    )
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.resources[thread_id] = (child, descriptor)
                async def read_thread(self, thread_id, include_turns=True):
                    return {
                        "id": thread_id, "name": thread_id,
                        "projectId": "codex-p", "status": {"type": "idle"},
                        "archived": thread_id not in self.resources,
                        "turns": [{"id": "turn", "status": "completed", "items": []}],
                    }
                async def set_name(self, thread_id, name):
                    return None
                async def archive(self, thread_id):
                    child, descriptor = self.resources.pop(thread_id)
                    child.terminate()
                    child.wait(timeout=5)
                    for stream in (child.stdin, child.stdout, child.stderr):
                        if stream is not None:
                            stream.close()
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                    os.close(descriptor)
                def close_all(self):
                    for thread_id in list(self.resources):
                        asyncio.run(self.archive(thread_id))

            resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
            temporary = tempfile.TemporaryDirectory()
            root = Path(temporary.name)
            source = root / "source"
            source.mkdir()
            paths = RuntimePaths(
                brain_root=root / "brain", state_root=root / "state",
                config_file=root / "config.json", control_root=root / "control",
            )
            config = InstallationConfig(
                source_root=str(source), brain_root=str(paths.brain_root),
                state_root=str(paths.state_root), codex_bin="/bin/codex",
                desktop_executable="/Applications/ChatGPT.app/ChatGPT",
                projects=[ProjectConfig("p", str(source), codex_project_id="codex-p")],
            )
            controller = Controller(paths, config)
            controller._initialize_configuration()
            runtime = Runtime(root)
            probe = AppServerResourceProbe(pid_provider=os.getpid)
            baseline = probe.snapshot()
            try:
                roles = ("executor", "overseer", "sage", "inquisitor") * 2
                for number, role in enumerate(roles):
                    thread_id = f"worker-{number}"
                    task = controller.store.register_task(
                        native_thread_id=thread_id, role=role,
                        description=thread_id, model="sol",
                        reasoning_effort="high", project_id="p",
                    )
                    controller.store.execute(
                        "UPDATE tasks SET resource_idle_since = ? WHERE id = ?",
                        ("2026-01-01T00:00:00Z", task["id"]),
                    )
                    runtime.add(thread_id)
                controller.runtime = runtime
                controller.resource_probe = probe
                loaded = probe.snapshot()
                assert loaded.child_count == baseline.child_count + 8, (baseline, loaded)
                asyncio.run(controller._maintain_resource_lifecycle())
                reclaimed = probe.snapshot()
                assert reclaimed.child_count == baseline.child_count, (baseline, reclaimed)
                assert reclaimed.descriptor_count == baseline.descriptor_count, (baseline, reclaimed)
                assert controller.store.row(
                    "SELECT COUNT(*) AS count FROM tasks WHERE resource_reclaimed_at IS NOT NULL"
                )["count"] == 8
            finally:
                runtime.close_all()
                controller.store.close()
                if controller.lock_handle is not None:
                    controller.lock_handle.close()
                temporary.cleanup()
            """)
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)


if __name__ == "__main__":
    unittest.main()
