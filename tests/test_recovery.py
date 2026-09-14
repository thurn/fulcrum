from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from fulcrum.config import InstallationConfig, RuntimePaths, save_installation
from fulcrum.install import InstallationError, install_recovery_artifact
from fulcrum.operative import read_journal, transition_journal, write_journal
from fulcrum.recovery import (
    RecoveryError,
    _controller_control,
    _reconcile,
    _run,
    _store_repair,
    acquire,
    build_parser,
    probe,
)
from fulcrum.store import Store, utc_now


class RecoveryArtifactTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        shutil.copytree(Path(__file__).parents[1] / "src", self.source / "src")
        self.paths = RuntimePaths(
            brain_root=self.root / "brain",
            state_root=self.root / "state",
            config_file=self.root / "config.json",
            control_root=self.root / "control",
        )
        self.config = InstallationConfig(
            source_root=str(self.source),
            brain_root=str(self.paths.brain_root),
            state_root=str(self.paths.state_root),
            codex_bin="/bin/false",
            desktop_executable="/Applications/ChatGPT.app/ChatGPT",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_install_is_private_executable_atomic_and_survives_broken_source(
        self,
    ) -> None:
        launcher, updated = install_recovery_artifact(self.config, self.paths)
        deployment = launcher.parents[1].resolve()

        self.assertTrue(updated)
        self.assertEqual(stat.S_IMODE(launcher.stat().st_mode), 0o700)
        completed = subprocess.run(
            [str(launcher), "--smoke-test"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        observed = json.loads(completed.stdout)
        self.assertTrue(observed["isolated"])
        self.assertTrue(Path(observed["module"]).is_relative_to(deployment))
        self.assertFalse(Path(observed["module"]).is_relative_to(self.source))

        (self.source / "src" / "fulcrum" / "recovery.py").write_text(
            "this is broken Python !!!\n", encoding="utf-8"
        )
        with self.assertRaises(InstallationError):
            install_recovery_artifact(self.config, self.paths)
        self.assertEqual(launcher.parents[1].resolve(), deployment)
        retained = subprocess.run(
            [str(launcher), "--smoke-test"], capture_output=True, check=False
        )
        self.assertEqual(retained.returncode, 0)

    def test_process_race_retains_one_takeover_identity(self) -> None:
        launcher, _ = install_recovery_artifact(self.config, self.paths)
        save_installation(self.paths.config_file, self.config)
        input_path = self.root / "operative-input.json"
        input_path.write_text(
            json.dumps({"description": "Recover concurrent fallback race"}),
            encoding="utf-8",
        )
        input_path.chmod(0o600)
        environment = {
            **os.environ,
            "FULCRUM_CONFIG": str(self.paths.config_file),
            "FULCRUM_STATE_ROOT": str(self.paths.state_root),
            "FULCRUM_CONTROL_ROOT": str(self.paths.control_root),
            "CODEX_THREAD_ID": "race-operative",
        }
        processes = [
            subprocess.Popen(
                [str(launcher), "register", "--input", str(input_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
            )
            for _ in range(2)
        ]
        outputs = [process.communicate(timeout=20) for process in processes]

        successful = [
            json.loads(stdout)
            for process, (stdout, _stderr) in zip(processes, outputs, strict=True)
            if process.returncode == 0
        ]
        self.assertTrue(successful, outputs)
        identities = {item["takeover_id"] for item in successful}
        self.assertEqual(len(identities), 1)
        journal = read_journal(self.paths.operative_journal)
        self.assertEqual(journal["takeover_id"], next(iter(identities)))
        self.assertEqual(journal["native_thread_id"], "race-operative")


class OfflineRecoveryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.root = root
        source = root / "source"
        source.mkdir()
        (source / "README.md").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=source, check=True)
        subprocess.run(["git", "add", "README.md"], cwd=source, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Fulcrum Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qm",
                "test: seed",
            ],
            cwd=source,
            check=True,
        )
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
            codex_bin="/bin/false",
            desktop_executable="/Applications/ChatGPT.app/ChatGPT",
            app_server_endpoint="ws://127.0.0.1:1",
        )
        save_installation(self.paths.config_file, self.config)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    async def _unavailable_runtime(
        _config: object, thread_id: str
    ) -> tuple[None, dict[str, object]]:
        return None, {
            "coverage": "unavailable",
            "method": "test unavailable runtime",
            "native_thread_id": thread_id,
        }

    class _RecoveredRuntime:
        ready = True
        name = "temporary"

        async def close(self) -> None:
            return None

        async def read_thread(
            self, thread_id: str, *, include_turns: bool = True
        ) -> dict[str, object]:
            return {
                "id": thread_id,
                "name": self.name,
                "projectId": None,
                "status": {"type": "active"},
                "turns": (
                    [{"id": "operative-turn", "status": "inProgress", "items": []}]
                    if include_turns
                    else []
                ),
                "archived": False,
            }

        async def set_name(self, _thread_id: str, _name: str) -> None:
            self.name = _name

    def _seed_bound_repair(self) -> dict[str, Any]:
        now = utc_now()
        with Store(self.paths.database) as store:
            store.execute(
                "INSERT INTO meta(key, value) VALUES ('dispatch_enabled', '0') ON CONFLICT(key) DO UPDATE SET value = '0'"
            )
            task = store.register_task(
                native_thread_id="human-operative",
                role="operative",
                description="repair one exact store row",
                model="gpt-6-astra",
                reasoning_effort="high",
                state="active",
            )
            cursor = store.execute(
                """INSERT INTO actions(task_id, kind, payload, state, native_turn_id, created_at, updated_at)
                   VALUES (?, 'operative', '{}', 'active', 'operative-turn', ?, ?)""",
                (task["id"], now, now),
            )
            action_id = int(cursor.lastrowid)
        journal: dict[str, Any] = {
            "takeover_id": "repair-takeover",
            "state": "active",
            "task_id": int(task["id"]),
            "action_id": action_id,
            "native_thread_id": "human-operative",
            "superseded_thread_ids": [],
            "scope": "repair one exact store row",
            "prior_dispatch_enabled": False,
            "completed_effects": [
                "authority_fence_written",
                "sqlite_authority_mirrored",
                "native_identity_bound",
            ],
            "next_step": "repair store",
            "caller_verification": {
                "coverage": "observed",
                "method": "exact App Server thread/read",
                "native_thread_id": "human-operative",
            },
            "operative_identity": {
                "role_number": task["role_number"],
                "title": task["title"],
                "model": task["model"],
                "reasoning_effort": task["reasoning_effort"],
            },
            "operative_action": {
                "payload": {},
                "native_turn_id": "operative-turn",
            },
            "created_at": now,
            "updated_at": now,
        }
        write_journal(self.paths.operative_journal, journal)
        return journal

    async def test_missing_store_and_app_server_create_idempotent_provisional_fence(
        self,
    ) -> None:
        request = {"description": "Recover compound failure"}
        with (
            patch.dict(os.environ, {"CODEX_THREAD_ID": "human-operative"}, clear=False),
            patch(
                "fulcrum.recovery._verified_runtime",
                side_effect=self._unavailable_runtime,
            ),
        ):
            first = await acquire(self.paths, request, "/tmp/input.json")
            second = await acquire(self.paths, request, "/tmp/input.json")

        self.assertEqual(first["authority"], "provisional_local_repair")
        self.assertEqual(first["takeover_id"], second["takeover_id"])
        self.assertEqual(first["state"], "acquiring")
        self.assertFalse(self.paths.database.exists())
        journal = read_journal(self.paths.operative_journal)
        self.assertEqual(journal["native_thread_id"], "human-operative")
        self.assertEqual(journal["caller_verification"]["coverage"], "unavailable")

    async def test_corrupt_store_is_copied_once_and_never_overwritten(self) -> None:
        self.paths.state_root.mkdir(parents=True)
        original = b"not a sqlite database"
        self.paths.database.write_bytes(original)
        request = {"description": "Recover corrupt state"}
        with (
            patch.dict(os.environ, {"CODEX_THREAD_ID": "human-operative"}, clear=False),
            patch(
                "fulcrum.recovery._verified_runtime",
                side_effect=self._unavailable_runtime,
            ),
        ):
            first = await acquire(self.paths, request, "/tmp/input.json")
            second = await acquire(self.paths, request, "/tmp/input.json")

        self.assertEqual(self.paths.database.read_bytes(), original)
        self.assertEqual(first["quarantine"]["path"], second["quarantine"]["path"])
        copied = first["quarantine"]["copies"]
        self.assertEqual(len(copied), 1)
        self.assertEqual(Path(copied[0]["copy"]).read_bytes(), original)

    async def test_missing_store_reconciles_after_exact_caller_verification(
        self,
    ) -> None:
        request = {"description": "Recover missing state"}
        with (
            patch.dict(os.environ, {"CODEX_THREAD_ID": "human-operative"}, clear=False),
            patch(
                "fulcrum.recovery._verified_runtime",
                side_effect=self._unavailable_runtime,
            ),
        ):
            await acquire(self.paths, request, "/tmp/input.json")
        runtime = self._RecoveredRuntime()

        async def verified(
            _config: object, thread_id: str
        ) -> tuple[OfflineRecoveryTest._RecoveredRuntime, dict[str, object]]:
            return runtime, {
                "coverage": "observed",
                "method": "exact App Server thread/read",
                "native_thread_id": thread_id,
            }

        with (
            patch.dict(os.environ, {"CODEX_THREAD_ID": "human-operative"}, clear=False),
            patch("fulcrum.recovery._verified_runtime", side_effect=verified),
        ):
            result = await _reconcile(self.paths, {}, None)

        self.assertTrue(result["store_reconstructed_from_journal"])
        self.assertTrue(self.paths.database.is_file())
        journal = read_journal(self.paths.operative_journal)
        assert journal is not None
        self.assertEqual(journal["state"], "active")
        self.assertEqual(journal["caller_verification"]["coverage"], "observed")
        self.assertEqual(
            journal["caller_verification"]["method"], "exact App Server thread/read"
        )
        self.assertIn("exact_caller_verified", journal["completed_effects"])
        with Store(self.paths.database, readonly=True) as store:
            task = store.row(
                "SELECT role FROM tasks WHERE native_thread_id = 'human-operative'"
            )
        self.assertEqual(task["role"], "operative")

    async def test_no_thread_identity_returns_probe_without_creating_authority(
        self,
    ) -> None:
        self.paths.control_root.parent.mkdir(parents=True, exist_ok=True)
        with patch.dict(
            os.environ,
            {
                key: ""
                for key in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_TASK_ID")
            },
            clear=False,
        ):
            result = await acquire(
                self.paths, {"description": "Must remain read only"}, None
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "read_only")
        self.assertFalse(self.paths.operative_journal.exists())

    async def test_provisional_store_repair_is_rejected_before_database_mutation(
        self,
    ) -> None:
        with Store(self.paths.database) as store:
            store.execute("INSERT INTO meta(key, value) VALUES ('repair_target', '0')")
        now = utc_now()
        write_journal(
            self.paths.operative_journal,
            {
                "takeover_id": "provisional-repair",
                "state": "acquiring",
                "native_thread_id": "human-operative",
                "superseded_thread_ids": [],
                "scope": "must verify before store repair",
                "prior_dispatch_enabled": False,
                "completed_effects": ["authority_fence_written"],
                "next_step": "verify caller",
                "caller_verification": {"coverage": "unavailable"},
                "created_at": now,
                "updated_at": now,
            },
        )
        request = self.root / "provisional-store-repair.json"
        request.write_text(
            json.dumps(
                {
                    "operation_key": "must-not-run",
                    "reason": "prove provisional authority cannot mutate workflow state",
                    "statements": [
                        {
                            "sql": "UPDATE meta SET value = '1' WHERE key = 'repair_target'"
                        }
                    ],
                    "observations": [
                        {"sql": "SELECT value FROM meta WHERE key = 'repair_target'"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        args = build_parser().parse_args(["store-repair", "--input", str(request)])
        with patch.dict(
            os.environ, {"CODEX_THREAD_ID": "human-operative"}, clear=False
        ):
            with self.assertRaisesRegex(RecoveryError, "active verified Operative"):
                await _run(args, self.paths)
        with Store(self.paths.database, readonly=True) as store:
            self.assertEqual(
                store.row("SELECT value FROM meta WHERE key = 'repair_target'")[
                    "value"
                ],
                "0",
            )
        self.assertNotIn(
            "offline_operations", read_journal(self.paths.operative_journal)
        )

    async def test_verified_store_repair_is_backed_up_transactional_and_audited(
        self,
    ) -> None:
        self._seed_bound_repair()
        supplied = {
            "operation_key": "dispatch-row-repair",
            "reason": "restore the retained prior dispatch value",
            "statements": [
                {
                    "sql": "UPDATE meta SET value = ? WHERE key = ?",
                    "parameters": ["1", "dispatch_enabled"],
                }
            ],
            "observations": [
                {
                    "sql": "SELECT key, value FROM meta WHERE key = ?",
                    "parameters": ["dispatch_enabled"],
                }
            ],
        }
        request = self.root / "verified-store-repair.json"
        request.write_text(json.dumps(supplied), encoding="utf-8")
        args = build_parser().parse_args(["store-repair", "--input", str(request)])
        runtime = self._RecoveredRuntime()

        async def verified(
            _config: object, thread_id: str
        ) -> tuple[OfflineRecoveryTest._RecoveredRuntime, dict[str, object]]:
            return runtime, {
                "coverage": "observed",
                "method": "exact App Server thread/read",
                "native_thread_id": thread_id,
            }

        with (
            patch.dict(os.environ, {"CODEX_THREAD_ID": "human-operative"}, clear=False),
            patch("fulcrum.recovery._verified_runtime", side_effect=verified),
        ):
            result = await _run(args, self.paths)

        self.assertTrue(result["ok"])
        self.assertTrue(Path(result["quarantine"]["path"]).is_dir())
        with Store(self.paths.database, readonly=True) as store:
            self.assertEqual(
                store.row("SELECT value FROM meta WHERE key = 'dispatch_enabled'")[
                    "value"
                ],
                "1",
            )
        operation = read_journal(self.paths.operative_journal)["offline_operations"][0]
        self.assertEqual(operation["kind"], "direct_store_repair")
        self.assertEqual(operation["before"]["queries"][0]["rows"][0]["value"], "0")
        self.assertEqual(operation["after"]["queries"][0]["rows"][0]["value"], "1")
        with Store(self.paths.database) as store:
            store.mirror_operative_journal(read_journal(self.paths.operative_journal))
            mirrored = store.row(
                "SELECT kind, state FROM operative_operations WHERE correlation_id = ?",
                (operation["correlation_id"],),
            )
        self.assertEqual(
            (mirrored["kind"], mirrored["state"]), ("direct_store_repair", "complete")
        )

    async def test_store_repair_crash_boundaries_never_reissue_sent_mutation(
        self,
    ) -> None:
        journal = self._seed_bound_repair()
        for phase, expected_value, expected_state in (
            ("after_intent", "1", "complete"),
            ("during_transaction", "0", "failed"),
            ("after_commit", "1", "uncertain"),
        ):
            key = f"crash-{phase}"
            with Store(self.paths.database) as store:
                store.execute("INSERT INTO meta(key, value) VALUES (?, '0')", (key,))
            supplied = {
                "operation_key": key,
                "reason": f"exercise {phase} crash boundary",
                "statements": [
                    {
                        "sql": "UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = ?",
                        "parameters": [key],
                    }
                ],
                "observations": [
                    {
                        "sql": "SELECT value FROM meta WHERE key = ?",
                        "parameters": [key],
                    }
                ],
            }

            def fail_at(observed: str) -> None:
                if observed == phase:
                    raise RuntimeError(f"simulated crash {phase}")

            with patch("fulcrum.recovery._store_repair_boundary", side_effect=fail_at):
                with self.assertRaisesRegex(RuntimeError, f"simulated crash {phase}"):
                    _store_repair(
                        self.paths,
                        supplied,
                        "/tmp/repair.json",
                        journal=journal,
                    )
            journal = read_journal(self.paths.operative_journal)
            assert journal is not None
            retried = _store_repair(
                self.paths,
                supplied,
                "/tmp/repair.json",
                journal=journal,
            )
            with Store(self.paths.database, readonly=True) as store:
                value = store.row("SELECT value FROM meta WHERE key = ?", (key,))[
                    "value"
                ]
            self.assertEqual(value, expected_value)
            self.assertEqual(retried["operation"]["state"], expected_state)
            self.assertEqual(
                retried["operation"].get("observed_result", {}).get("reissued"),
                False if phase != "after_intent" else None,
            )

    async def test_store_repair_reentry_rejects_every_request_mismatch(self) -> None:
        journal = self._seed_bound_repair()
        mutations = {
            "statement": lambda value: value["statements"][0].update(
                {"sql": "UPDATE meta SET value = value + 2 WHERE key = ?"}
            ),
            "statement-parameters": lambda value: value["statements"][0].update(
                {"parameters": ["different-key"]}
            ),
            "observation": lambda value: value["observations"][0].update(
                {"sql": "SELECT value, key FROM meta WHERE key = ?"}
            ),
            "observation-parameters": lambda value: value["observations"][0].update(
                {"parameters": ["different-key"]}
            ),
            "reason": lambda value: value.update({"reason": "different reason"}),
        }
        for suffix, mutate in mutations.items():
            key = f"mismatch-{suffix}"
            with Store(self.paths.database) as store:
                store.execute("INSERT INTO meta(key, value) VALUES (?, '0')", (key,))
            supplied = {
                "operation_key": key,
                "reason": f"retain exact request for {suffix}",
                "statements": [
                    {
                        "sql": "UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = ?",
                        "parameters": [key],
                    }
                ],
                "observations": [
                    {
                        "sql": "SELECT value FROM meta WHERE key = ?",
                        "parameters": [key],
                    }
                ],
            }

            def fail_after_intent(phase: str) -> None:
                if phase == "after_intent":
                    raise RuntimeError("simulated crash after intent")

            with patch(
                "fulcrum.recovery._store_repair_boundary",
                side_effect=fail_after_intent,
            ):
                with self.assertRaisesRegex(RuntimeError, "after intent"):
                    _store_repair(
                        self.paths,
                        supplied,
                        "/tmp/repair.json",
                        journal=journal,
                    )
            snapshot = read_journal(self.paths.operative_journal)
            assert snapshot is not None
            changed = json.loads(json.dumps(supplied))
            mutate(changed)
            with self.assertRaisesRegex(RecoveryError, "different store repair"):
                _store_repair(
                    self.paths,
                    changed,
                    "/tmp/repair.json",
                    journal=snapshot,
                )
            self.assertEqual(read_journal(self.paths.operative_journal), snapshot)
            with Store(self.paths.database, readonly=True) as store:
                self.assertEqual(
                    store.row("SELECT value FROM meta WHERE key = ?", (key,))["value"],
                    "0",
                )
            journal = snapshot

        evidence_key = "mismatch-evidence"
        with Store(self.paths.database) as store:
            store.execute(
                "INSERT INTO meta(key, value) VALUES (?, '0')", (evidence_key,)
            )
        supplied = {
            "operation_key": evidence_key,
            "reason": "retain exact evidence",
            "statements": [
                {
                    "sql": "UPDATE meta SET value = value + 1 WHERE key = ?",
                    "parameters": [evidence_key],
                }
            ],
            "observations": [
                {
                    "sql": "SELECT value FROM meta WHERE key = ?",
                    "parameters": [evidence_key],
                }
            ],
        }

        def fail_after_intent(phase: str) -> None:
            if phase == "after_intent":
                raise RuntimeError("simulated crash after intent")

        with patch(
            "fulcrum.recovery._store_repair_boundary", side_effect=fail_after_intent
        ):
            with self.assertRaisesRegex(RuntimeError, "after intent"):
                _store_repair(
                    self.paths,
                    supplied,
                    "/tmp/original-evidence.json",
                    journal=journal,
                )
        snapshot = read_journal(self.paths.operative_journal)
        assert snapshot is not None
        with self.assertRaisesRegex(RecoveryError, "different store repair"):
            _store_repair(
                self.paths,
                supplied,
                "/tmp/changed-evidence.json",
                journal=snapshot,
            )
        self.assertEqual(read_journal(self.paths.operative_journal), snapshot)
        with Store(self.paths.database, readonly=True) as store:
            self.assertEqual(
                store.row("SELECT value FROM meta WHERE key = ?", (evidence_key,))[
                    "value"
                ],
                "0",
            )

        sent_key = "mismatch-sent-observation"
        with Store(self.paths.database) as store:
            store.execute("INSERT INTO meta(key, value) VALUES (?, '0')", (sent_key,))
        supplied = {
            "operation_key": sent_key,
            "reason": "retain exact sent observation",
            "statements": [
                {
                    "sql": "UPDATE meta SET value = value + 1 WHERE key = ?",
                    "parameters": [sent_key],
                }
            ],
            "observations": [
                {
                    "sql": "SELECT value FROM meta WHERE key = ?",
                    "parameters": [sent_key],
                }
            ],
        }

        def fail_after_commit(phase: str) -> None:
            if phase == "after_commit":
                raise RuntimeError("simulated crash after commit")

        journal = read_journal(self.paths.operative_journal)
        assert journal is not None
        with patch(
            "fulcrum.recovery._store_repair_boundary", side_effect=fail_after_commit
        ):
            with self.assertRaisesRegex(RuntimeError, "after commit"):
                _store_repair(
                    self.paths,
                    supplied,
                    "/tmp/sent-observation.json",
                    journal=journal,
                )
        snapshot = read_journal(self.paths.operative_journal)
        assert snapshot is not None
        changed = json.loads(json.dumps(supplied))
        changed["observations"][0]["parameters"] = ["different-key"]
        with self.assertRaisesRegex(RecoveryError, "different store repair"):
            _store_repair(
                self.paths,
                changed,
                "/tmp/sent-observation.json",
                journal=snapshot,
            )
        self.assertEqual(read_journal(self.paths.operative_journal), snapshot)
        sent_operation = next(
            item
            for item in snapshot["offline_operations"]
            if item["correlation_id"].endswith(f":{sent_key}")
        )
        self.assertEqual(sent_operation["state"], "sent")
        with Store(self.paths.database, readonly=True) as store:
            self.assertEqual(
                store.row("SELECT value FROM meta WHERE key = ?", (sent_key,))["value"],
                "1",
            )

    async def test_fallback_finish_requires_durable_finalizer_handoff(self) -> None:
        self._seed_bound_repair()
        runtime = self._RecoveredRuntime()

        async def verified(
            _config: object, thread_id: str
        ) -> tuple[OfflineRecoveryTest._RecoveredRuntime, dict[str, object]]:
            return runtime, {
                "coverage": "observed",
                "method": "exact App Server thread/read",
                "native_thread_id": thread_id,
            }

        class FakeController:
            def __init__(self, paths: RuntimePaths) -> None:
                self.paths = paths
                self.operative_journal = read_journal(paths.operative_journal)
                self.store = MagicMock()

            async def handle_request(
                self, _payload: dict[str, object]
            ) -> dict[str, object]:
                assert self.operative_journal is not None
                self.operative_journal = transition_journal(
                    self.paths.operative_journal,
                    self.operative_journal,
                    "closing",
                    now=utc_now(),
                    next_step="wait for exact terminal turn",
                )
                return {"ok": True, "state": "closing"}

        async def with_controller(
            paths: RuntimePaths,
            _config: object,
            _lock: object,
            _runtime: object,
            operation: object,
        ) -> dict[str, object]:
            return await operation(FakeController(paths))  # type: ignore[operator]

        finalizer = {
            "coverage": "observed",
            "kind": "detached_recovery_finalizer",
            "pid": 42,
        }
        with (
            patch.dict(os.environ, {"CODEX_THREAD_ID": "human-operative"}, clear=False),
            patch("fulcrum.recovery._verified_runtime", side_effect=verified),
            patch("fulcrum.recovery._with_controller", side_effect=with_controller),
            patch(
                "fulcrum.recovery._spawn_closeout_finalizer",
                return_value=finalizer,
            ) as spawn,
        ):
            result = await _controller_control(self.paths, "finish", {}, None)
        self.assertEqual(result["finalizer"], finalizer)
        spawn.assert_called_once()
        self.assertEqual(read_journal(self.paths.operative_journal)["state"], "closing")

        active = transition_journal(
            self.paths.operative_journal,
            read_journal(self.paths.operative_journal),
            "active",
            now=utc_now(),
            next_step="retry closeout",
        )
        write_journal(self.paths.operative_journal, active)
        with (
            patch.dict(os.environ, {"CODEX_THREAD_ID": "human-operative"}, clear=False),
            patch("fulcrum.recovery._verified_runtime", side_effect=verified),
            patch("fulcrum.recovery._with_controller", side_effect=with_controller),
            patch(
                "fulcrum.recovery._spawn_closeout_finalizer",
                side_effect=RecoveryError("handoff failed"),
            ),
        ):
            with self.assertRaisesRegex(RecoveryError, "not accepted"):
                await _controller_control(self.paths, "finish", {}, None)
        self.assertEqual(read_journal(self.paths.operative_journal)["state"], "active")

    def test_probe_does_not_create_missing_runtime_paths(self) -> None:
        untouched = RuntimePaths(
            brain_root=self.paths.brain_root,
            state_root=Path(self.temporary.name) / "missing-state",
            config_file=self.paths.config_file,
            control_root=Path(self.temporary.name) / "missing-control",
        )
        result = probe(untouched)
        self.assertTrue(result["ok"])
        self.assertEqual(result["database"]["state"], "missing")
        self.assertFalse(untouched.state_root.exists())
        self.assertFalse(untouched.control_root.exists())


if __name__ == "__main__":
    unittest.main()
