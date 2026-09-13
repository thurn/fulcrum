from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from copy import deepcopy
from contextlib import redirect_stderr
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from fulcrum.config import InstallationConfig, ProjectConfig, RuntimePaths
from fulcrum.cli import main as cli_main
from fulcrum.controller import Controller
from fulcrum.hook import handle_event
from fulcrum.lifecycle import accept_finish
from fulcrum.operative import (
    authority_gate,
    read_journal,
    transition_journal,
    write_journal,
)
from fulcrum.readiness import state_readiness
from fulcrum.setup import SetupError, run_setup
from fulcrum.store import Store, StoreError, utc_now


class OperativeRuntime:
    def __init__(self) -> None:
        self.ready = True
        self.name = "temporary"
        self.status = "active"
        self.turn_status = "inProgress"
        self.turn_visible = True
        self.archived = False
        self.archived_threads: list[str] = []
        self.fail_name_once = False
        self.fail_read_after_name_once = False
        self._read_must_fail = False
        self.set_name_calls: list[tuple[str, str]] = []
        self.assign_project_calls: list[tuple[str, str]] = []

    async def read_thread(
        self, thread_id: str, *, include_turns: bool = True
    ) -> dict[str, object]:
        if self._read_must_fail:
            self._read_must_fail = False
            raise RuntimeError("observation unavailable after accepted naming")
        if thread_id == "archon-thread":
            return {
                "id": thread_id,
                "name": "Archon",
                "projectId": None,
                "status": {"type": "idle"},
                "turns": [],
                "archived": False,
            }
        if thread_id.startswith("executor"):
            return {
                "id": thread_id,
                "name": "wrong executor name",
                "projectId": "wrong-project",
                "status": {"type": "idle"},
                "turns": [],
                "archived": False,
            }
        turns = (
            [{"id": "operative-turn", "status": self.turn_status, "items": []}]
            if include_turns and self.turn_visible
            else []
        )
        return {
            "id": thread_id,
            "name": self.name,
            "projectId": None,
            "status": {"type": self.status},
            "turns": turns,
            "archived": self.archived,
        }

    async def set_name(self, _thread_id: str, name: str) -> None:
        self.set_name_calls.append((_thread_id, name))
        if self.fail_name_once:
            self.fail_name_once = False
            raise RuntimeError("name unavailable")
        self.name = name
        if self.fail_read_after_name_once:
            self.fail_read_after_name_once = False
            self._read_must_fail = True

    async def assign_thread_project(
        self, thread_id: str, project_id: str
    ) -> dict[str, object]:
        self.assign_project_calls.append((thread_id, project_id))
        return {"thread": await self.read_thread(thread_id)}

    async def archive(self, thread_id: str) -> None:
        self.archived_threads.append(thread_id)
        self.archived = True


class OperativeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
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
                "test: seed fixture",
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
            codex_bin="/bin/codex",
            desktop_executable="/Applications/ChatGPT.app/ChatGPT",
            projects=[ProjectConfig("p", str(source))],
        )
        self.controller = Controller(self.paths, self.config)
        self.controller._initialize_configuration()
        self.runtime = OperativeRuntime()
        self.controller.runtime = self.runtime  # type: ignore[assignment]
        self.controller._observe_operative_services = (  # type: ignore[method-assign]
            self._observed_services
        )
        self._seed_ready_installation()

    @staticmethod
    def _observed_services() -> tuple[dict[str, object], dict[str, object]]:
        common = {"coverage": "observed", "complete": True, "truncated": False}
        return (
            {**common, "lock_owned": True, "ownership": "controller_lock"},
            {**common, "connected": True, "ownership": "launchd"},
        )

    def tearDown(self) -> None:
        self.controller.store.close()
        if self.controller.lock_handle is not None:
            self.controller.lock_handle.close()
            self.controller.lock_handle = None
        self.temporary.cleanup()

    def _seed_ready_installation(self) -> None:
        self.controller.store.execute(
            "INSERT INTO meta(key, value) VALUES ('global_limit', '2')"
        )
        self.controller.store.execute(
            "INSERT INTO meta(key, value) VALUES ('project_limits', ?)",
            (json.dumps({"p": 2}),),
        )
        now = utc_now()
        self.controller.store.execute(
            """INSERT INTO policies(
                 kind, scope, cadence_seconds, anchor_at, next_due_at, active
               ) VALUES ('sage', NULL, 86400, ?, ?, 1),
                        ('inquisitor', 'p', 86400, ?, ?, 1)""",
            (now, now, now, now),
        )
        archon = self.controller.store.register_task(
            native_thread_id="archon-thread",
            role="archon",
            description="",
            model="gpt-6-astra",
            reasoning_effort="high",
        )
        self.controller.store.execute(
            "UPDATE tasks SET runtime_status = 'idle' WHERE id = ?", (archon["id"],)
        )
        for worker in self.controller.critical_workers:
            self.controller.store.heartbeat(worker)
        self.controller.store.execute(
            "INSERT INTO meta(key, value) VALUES ('last_reconciliation', ?)", (now,)
        )
        self.controller.store.execute(
            "INSERT INTO meta(key, value) VALUES ('dispatch_enabled', '1')"
        )
        self.controller.starts_enabled = True

    async def _register(self, thread_id: str = "operative-thread") -> dict[str, object]:
        return await self.controller._register_operative(
            {
                "thread_id": thread_id,
                "description": "Repair the failed controller",
                "model": "gpt-6-astra",
                "effort": "high",
            }
        )

    async def _closeout_contract(self) -> dict[str, object]:
        dossier = await self.controller._build_operative_dossier()

        def items(name: str) -> list[dict[str, object]]:
            value = dossier[name].get("items", [])
            return value if isinstance(value, list) else []

        tasks = [item for item in items("managed_tasks") if item["role"] != "operative"]
        task_ids = {item["id"] for item in tasks}
        expected = {
            "agents": [str(item["id"]) for item in tasks],
            "actions": [
                str(item["id"])
                for item in items("actions")
                if item["task_id"] in task_ids
            ],
            "effects": [
                f"external:{item['id']}"
                for item in items("uncertain_external_operations")
            ]
            + [
                f"operative:{item['id']}"
                for item in items("operative_operations")
                if item["state"] in {"sent", "uncertain", "failed"}
            ],
            "worktrees": [str(item["path"]) for item in items("worktrees")],
            "candidates": [
                str(item.get("candidate_id") or item["assignment_id"])
                for item in items("candidates")
            ],
            "publication_delivery": [
                f"bead:{item['bead_id']}" for item in items("beads_publication")
            ]
            + [f"obligation:{item['id']}" for item in items("obligations")],
        }
        return {
            "emergency_outcome": "complete",
            "root_cause": "controller authority failure",
            "repairs": ["restored durable authority"],
            "commits": ["abc123"],
            "validation": ["all checks passed"],
            "limitations": [],
            "source": {"status": "observed"},
            "store": {"status": "observed"},
            "services": {"status": "observed"},
            "dispositions": {
                name: {
                    "status": "resolved" if identifiers else "not_applicable",
                    "ids": identifiers,
                }
                for name, identifiers in expected.items()
            },
        }

    async def test_registration_is_exclusive_idempotent_and_fences_before_return(
        self,
    ) -> None:
        result = await self._register()

        self.assertEqual(result["title"], "🗡️ [OPR0001] Repair the failed controller")
        self.assertEqual(result["state"], "active")
        self.assertFalse(result["dispatch_enabled"])
        journal = read_journal(self.paths.operative_journal)
        assert journal is not None
        self.assertEqual(journal["state"], "active")
        self.assertTrue(journal["prior_dispatch_enabled"])
        self.assertEqual(
            self.controller.store.row(
                "SELECT value FROM meta WHERE key = 'dispatch_enabled'"
            )["value"],
            "0",
        )
        action = self.controller.store.current_action("operative-thread")
        self.assertEqual(action["native_turn_id"], "operative-turn")
        self.assertEqual(action["kind"], "operative")

        retried = await self._register()
        self.assertTrue(retried["reused"])
        self.assertEqual(retried["takeover_id"], result["takeover_id"])
        with self.assertRaisesRegex(StoreError, "operative takeover active"):
            await self._register("different-human-thread")
        with self.assertRaisesRegex(StoreError, "operative takeover active"):
            await self.controller.handle_request(
                {"command": "enable_dispatch", "thread_id": "operative-thread"}
            )
        with self.assertRaisesRegex(StoreError, "operative takeover active"):
            await self.controller.handle_request(
                {
                    "command": "intake",
                    "thread_id": "operative-thread",
                    "task": {"project": "p", "title": "must stay fenced"},
                }
            )
        with self.assertRaisesRegex(StoreError, "not the bound Operative"):
            await self.controller.handle_request(
                {"command": "operative_status", "thread_id": "different-human-thread"}
            )
        status = await self.controller.handle_request(
            {"command": "status", "thread_id": "different-human-thread"}
        )
        self.assertEqual(status["operative_journal_state"], "active")

    async def test_setup_is_fenced_before_run_setup_for_active_and_acquiring_journal(
        self,
    ) -> None:
        await self._register()
        with patch("fulcrum.setup.collect_config") as collect_config:
            with self.assertRaisesRegex(SetupError, "operative takeover active"):
                run_setup(self.paths, input_path=None, non_interactive=True)
            collect_config.assert_not_called()
        with (
            patch("fulcrum.cli.resolve_paths", return_value=self.paths),
            patch("fulcrum.cli.run_setup") as run_setup_mock,
            redirect_stderr(StringIO()) as error,
        ):
            self.assertEqual(cli_main(["setup", "--non-interactive"]), 2)
            run_setup_mock.assert_not_called()
            self.assertIn("operative takeover active", error.getvalue())

        self.paths.operative_journal.write_text("{broken", encoding="utf-8")
        with (
            patch("fulcrum.cli.resolve_paths", return_value=self.paths),
            patch("fulcrum.cli.run_setup") as run_setup_mock,
            redirect_stderr(StringIO()) as error,
        ):
            self.assertEqual(cli_main(["setup", "--non-interactive"]), 2)
            run_setup_mock.assert_not_called()
            self.assertIn("operative takeover active", error.getvalue())

        acquiring = {
            "takeover_id": "journal-before-store",
            "state": "acquiring",
            "native_thread_id": "human-thread",
            "superseded_thread_ids": [],
            "scope": "repair setup",
            "prior_dispatch_enabled": True,
            "completed_effects": ["authority_fence_written"],
            "next_step": "mirror operative task and action",
            "caller_verification": {"coverage": "observed"},
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        write_journal(self.paths.operative_journal, acquiring)
        with (
            patch("fulcrum.cli.resolve_paths", return_value=self.paths),
            patch("fulcrum.cli.run_setup") as run_setup_mock,
            redirect_stderr(StringIO()) as error,
        ):
            self.assertEqual(cli_main(["setup", "--non-interactive"]), 2)
            run_setup_mock.assert_not_called()
            self.assertIn("operative takeover active", error.getvalue())

    async def test_setup_revalidates_after_takeover_wins_before_mutation(self) -> None:
        acquiring = {
            "takeover_id": "setup-race",
            "state": "acquiring",
            "native_thread_id": "human-thread",
            "superseded_thread_ids": [],
            "scope": "repair setup race",
            "prior_dispatch_enabled": True,
            "completed_effects": ["authority_fence_written"],
            "next_step": "mirror operative task and action",
            "caller_verification": {"coverage": "observed"},
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }

        def takeover_wins(_endpoint: str) -> None:
            write_journal(self.paths.operative_journal, acquiring)

        with (
            patch(
                "fulcrum.setup.collect_config",
                return_value=replace(self.config, codex_bin="/usr/bin/true"),
            ),
            patch("fulcrum.setup.verify_editable_source"),
            patch(
                "fulcrum.setup.verify_runtime_ownership_or_availability",
                side_effect=takeover_wins,
            ),
            patch("fulcrum.setup.shutil.which", return_value="/usr/bin/true"),
            patch("fulcrum.setup.save_installation") as save_installation,
            patch("fulcrum.setup._prepare_brain") as prepare_brain,
        ):
            with self.assertRaisesRegex(SetupError, "operative takeover active"):
                run_setup(self.paths, input_path=None, non_interactive=True)
        save_installation.assert_not_called()
        prepare_brain.assert_not_called()

    async def test_takeover_cannot_interleave_with_setup_authority_gate(self) -> None:
        with authority_gate(self.paths.authority_lock, blocking=True):
            with self.assertRaisesRegex(StoreError, "setup mutation active"):
                await self._register()
        self.assertFalse(self.paths.operative_journal.exists())

    async def test_journal_only_hook_and_readiness_are_protectively_fenced(
        self,
    ) -> None:
        await self._register()
        journal = read_journal(self.paths.operative_journal)
        assert journal is not None
        isolated = RuntimePaths(
            brain_root=self.paths.brain_root,
            state_root=Path(self.temporary.name) / "isolated-state",
            config_file=self.paths.config_file,
            control_root=Path(self.temporary.name) / "isolated-control",
        )
        write_journal(isolated.operative_journal, journal)
        self.assertFalse(isolated.database.exists())

        with patch("fulcrum.hook.resolve_paths", return_value=isolated):
            bound = handle_event(
                {
                    "hook_event_name": "SessionStart",
                    "source": "compact",
                    "session_id": "operative-thread",
                }
            )
            ordinary = handle_event(
                {
                    "hook_event_name": "SessionStart",
                    "source": "compact",
                    "session_id": "ordinary-thread",
                }
            )
        bound_prompt = bound["hookSpecificOutput"]["additionalContext"]
        self.assertIn("bound Operative", bound_prompt)
        self.assertIn("Repair the failed controller", bound_prompt)
        self.assertIn("two phase", bound_prompt)
        self.assertNotIn("controller handles dispatch and delivery", bound_prompt)
        self.assertIn(
            "Ordinary Fulcrum authority is fenced",
            ordinary["hookSpecificOutput"]["additionalContext"],
        )

        with Store(
            isolated.database, operative_journal=isolated.operative_journal
        ) as store:
            ready, reasons = state_readiness(store)
            self.assertFalse(ready)
            self.assertIn("operative takeover active", reasons)
            isolated.operative_journal.write_text("{broken", encoding="utf-8")
            ready, reasons = state_readiness(store)
            self.assertFalse(ready)
            self.assertIn("operative takeover active", reasons)
        with patch("fulcrum.hook.resolve_paths", return_value=isolated):
            unreadable = handle_event(
                {
                    "hook_event_name": "SessionStart",
                    "source": "compact",
                    "session_id": "ordinary-thread",
                }
            )
        self.assertIn(
            "authoritative journal is unreadable",
            unreadable["hookSpecificOutput"]["additionalContext"],
        )

    async def test_managed_agent_cannot_self_promote(self) -> None:
        self.controller.store.register_task(
            native_thread_id="managed-executor",
            role="executor",
            description="Managed work",
            model="gpt-5.6-sol",
            reasoning_effort="high",
            project_id="p",
        )
        with self.assertRaisesRegex(StoreError, "managed agents cannot"):
            await self.controller._register_operative(
                {
                    "thread_id": "managed-executor",
                    "description": "Promote myself",
                }
            )
        self.assertFalse(self.paths.operative_journal.exists())

    async def test_acquisition_crash_marker_resumes_without_duplicate_identity(
        self,
    ) -> None:
        self.runtime.fail_name_once = True
        with self.assertRaisesRegex(RuntimeError, "name unavailable"):
            await self._register()
        journal = read_journal(self.paths.operative_journal)
        assert journal is not None
        self.assertEqual(journal["state"], "acquiring")
        self.assertIn("sqlite_authority_mirrored", journal["completed_effects"])
        self.assertEqual(
            len(
                self.controller.store.rows(
                    "SELECT * FROM tasks WHERE role = 'operative'"
                )
            ),
            1,
        )

    async def test_naming_acceptance_with_lost_observation_is_not_reissued(
        self,
    ) -> None:
        self.runtime.fail_read_after_name_once = True
        with self.assertRaisesRegex(RuntimeError, "observation unavailable"):
            await self._register()
        operation = self.controller.store.row(
            "SELECT * FROM operative_operations WHERE kind = 'thread_name'"
        )
        self.assertEqual(operation["state"], "uncertain")
        self.assertEqual(len(self.runtime.set_name_calls), 1)

        resumed = await self._register()

        self.assertEqual(resumed["state"], "active")
        self.assertEqual(len(self.runtime.set_name_calls), 1)
        operation = self.controller.store.row(
            "SELECT * FROM operative_operations WHERE kind = 'thread_name'"
        )
        self.assertEqual(operation["state"], "complete")

    async def test_already_observed_canonical_name_is_never_reissued(self) -> None:
        self.runtime.name = "🗡️ [OPR0001] Repair the failed controller"

        result = await self._register()

        self.assertEqual(result["state"], "active")
        self.assertEqual(self.runtime.set_name_calls, [])
        operation = self.controller.store.row(
            "SELECT * FROM operative_operations WHERE kind = 'thread_name'"
        )
        self.assertEqual(operation["state"], "complete")
        self.assertFalse(json.loads(operation["result_json"])["sent"])

    async def test_crash_after_naming_observation_uses_completed_operation(
        self,
    ) -> None:
        original = self.controller._save_operative_journal
        crashed = False

        def crash_after_observation(**changes: object) -> dict[str, object]:
            nonlocal crashed
            effects = changes.get("completed_effects")
            if (
                not crashed
                and isinstance(effects, list)
                and "native_identity_bound" in effects
            ):
                crashed = True
                raise RuntimeError("crash after naming observation")
            return original(**changes)

        with patch.object(
            self.controller,
            "_save_operative_journal",
            side_effect=crash_after_observation,
        ):
            with self.assertRaisesRegex(RuntimeError, "crash after naming"):
                await self._register()
        self.assertEqual(len(self.runtime.set_name_calls), 1)
        operation = self.controller.store.row(
            "SELECT * FROM operative_operations WHERE kind = 'thread_name'"
        )
        self.assertEqual(operation["state"], "complete")

        resumed = await self._register()

        self.assertEqual(resumed["state"], "active")
        self.assertEqual(len(self.runtime.set_name_calls), 1)

        resumed = await self._register()

        self.assertEqual(resumed["title"], "🗡️ [OPR0001] Repair the failed controller")
        self.assertEqual(resumed["state"], "active")
        self.assertEqual(
            len(
                self.controller.store.rows(
                    "SELECT * FROM tasks WHERE role = 'operative'"
                )
            ),
            1,
        )

    async def test_late_outcome_is_quarantined_and_cannot_advance(self) -> None:
        task = self.controller.store.register_task(
            native_thread_id="late-executor",
            role="executor",
            description="Old work",
            model="gpt-5.6-sol",
            reasoning_effort="high",
            project_id="p",
        )
        action = self.controller.store.execute(
            """INSERT INTO actions(task_id, kind, payload, state, created_at, updated_at)
               VALUES (?, 'implement', '{}', 'active', ?, ?)""",
            (task["id"], utc_now(), utc_now()),
        )
        await self._register()

        with self.assertRaisesRegex(StoreError, "operative takeover active"):
            accept_finish(
                self.controller.store,
                native_thread_id="late-executor",
                outcome_kind="checkpointed",
                options={"evidence": "/tmp/late"},
            )
        retained = self.controller.store.row(
            "SELECT * FROM actions WHERE id = ?", (action.lastrowid,)
        )
        self.assertIsNone(retained["outcome_kind"])
        evidence = self.controller.store.row("""SELECT * FROM operative_evidence
               WHERE evidence_key LIKE 'late-finish:%'""")
        self.assertEqual(evidence["kind"], "late_outcome")

    async def test_operative_reconciliation_never_repairs_ordinary_runtime_state(
        self,
    ) -> None:
        self.controller.store.execute(
            "UPDATE projects SET codex_project_id = 'expected-project' WHERE project_id = 'p'"
        )
        self.controller.store.register_task(
            native_thread_id="executor-passive",
            role="executor",
            description="Superseded work",
            model="gpt-5.6-sol",
            reasoning_effort="high",
            project_id="p",
        )

        await self._register()
        await self.controller.reconcile()

        self.assertEqual(
            [thread for thread, _name in self.runtime.set_name_calls],
            ["operative-thread"],
        )
        self.assertEqual(self.runtime.assign_project_calls, [])
        observed = self.controller.store.row(
            "SELECT runtime_status FROM tasks WHERE native_thread_id = 'executor-passive'"
        )
        self.assertEqual(observed["runtime_status"], "idle")

    async def test_dossier_has_explicit_coverage_and_readiness_is_fenced(self) -> None:
        await self._register()
        action = self.controller.store.current_action("operative-thread")
        self.controller.store.observe_helper_thread(
            parent_thread_id="operative-thread",
            parent_turn_id="operative-turn",
            native_thread_id="helper-thread",
            collaboration_item_id="helper-call",
        )
        self.controller.store.observe_helper_turn_started(
            "helper-thread", "helper-turn"
        )
        now = utc_now()
        occurrence = self.controller.store.execute(
            """INSERT INTO occurrences(kind, authority, state, created_at, updated_at)
               VALUES ('sage', 'controller', 'collecting', ?, ?)""",
            (now, now),
        )
        archon = self.controller.store.row("SELECT id FROM tasks WHERE role = 'archon'")
        interview = self.controller.store.execute(
            """INSERT INTO interviews(
                 occurrence_id, subject_task_id, request, prior_archived, state,
                 deadline_at, created_at, updated_at
               ) VALUES (?, ?, 'What remains?', 0, 'active', ?, ?, ?)""",
            (occurrence.lastrowid, archon["id"], now, now, now),
        )
        takeover = str(read_journal(self.paths.operative_journal)["takeover_id"])
        for index in range(205):
            self.controller.store.record_operative_evidence(
                takeover,
                f"overflow-{index}",
                "test",
                coverage="observed",
                detail={"index": index},
            )
        dossier = await self.controller.handle_request(
            {"command": "operative_dossier", "thread_id": "operative-thread"}
        )
        for section in (
            "takeover",
            "managed_tasks",
            "native_turns",
            "helpers",
            "interviews",
            "actions",
            "reservations",
            "runs",
            "assignments",
            "holds",
            "occurrences",
            "updates",
            "obligations",
            "worktrees",
            "git",
            "candidates",
            "tollgate",
            "beads_publication",
            "uncertain_external_operations",
            "controller_service",
            "app_server",
            "store_integrity",
            "source_snapshot",
            "editable_environment",
            "readiness",
        ):
            self.assertIn(
                dossier[section]["coverage"],
                {"observed", "unavailable", "not_applicable"},
            )
            self.assertIn("complete", dossier[section])
            self.assertIn("truncated", dossier[section])
        native_turn = next(
            item
            for item in dossier["native_turns"]["items"]
            if item["native_turn_id"] == action["native_turn_id"]
        )
        helper = next(
            item
            for item in dossier["helpers"]["items"]
            if item["native_thread_id"] == "helper-thread"
        )
        retained_interview = next(
            item
            for item in dossier["interviews"]["items"]
            if item["id"] == interview.lastrowid
        )
        self.assertEqual(native_turn["runtime_state"], "active")
        self.assertEqual(helper["runtime_state"], "active")
        self.assertEqual(retained_interview["state"], "active")
        self.assertTrue(dossier["evidence"]["truncated"])
        overflow = Path(dossier["evidence"]["overflow_artifact"])
        self.assertTrue(overflow.is_file())
        overflow_payload = json.loads(overflow.read_text(encoding="utf-8"))
        self.assertEqual(
            len(overflow_payload["items"]), dossier["evidence"]["count"] - 200
        )
        first_bytes = overflow.read_bytes()
        self.controller.store.record_operative_evidence(
            takeover,
            "overflow-after-first-dossier",
            "test",
            coverage="observed",
            detail={"snapshot": "second"},
        )
        second_dossier = await self.controller.handle_request(
            {"command": "operative_dossier", "thread_id": "operative-thread"}
        )
        second_overflow = Path(second_dossier["evidence"]["overflow_artifact"])
        self.assertNotEqual(overflow, second_overflow)
        self.assertEqual(overflow.read_bytes(), first_bytes)
        self.assertEqual(
            len(self.controller._complete_section_items(dossier, "evidence") or []),
            dossier["evidence"]["count"],
        )
        self.assertEqual(
            len(
                self.controller._complete_section_items(second_dossier, "evidence")
                or []
            ),
            second_dossier["evidence"]["count"],
        )
        ready, reasons = state_readiness(self.controller.store)
        self.assertFalse(ready)
        self.assertIn("operative takeover active", reasons)

    async def test_exact_reconciliation_retry_reuses_completed_result(self) -> None:
        registered = await self._register()
        correlation = (
            f"operative-resolve:{registered['takeover_id']}:99:observed_success"
        )
        operation = self.controller.store.create_operative_operation(
            str(registered["takeover_id"]),
            "resolve_external_operation",
            "99",
            {"operation": None},
            correlation_id=correlation,
        )
        self.controller.store.mark_operative_operation_sent(int(operation["id"]))
        expected = {"resolved": [99], "reused": True}
        self.controller.store.finish_operative_operation(
            int(operation["id"]),
            state="complete",
            result=expected,
            after={"operation": None},
        )

        result = await self.controller.handle_request(
            {
                "command": "resolve_operation",
                "thread_id": "operative-thread",
                "decision": {
                    "operation_id": 99,
                    "resolution": "observed_success",
                },
            }
        )

        self.assertEqual(result, expected)

    async def test_sent_reconciliation_is_not_reapplied(self) -> None:
        registered = await self._register()
        operation = self.controller.store.create_operative_operation(
            str(registered["takeover_id"]),
            "resolve_external_operation",
            "98",
            {"operation": None},
            correlation_id=(
                f"operative-resolve:{registered['takeover_id']}:98:observed_failure"
            ),
        )
        self.controller.store.mark_operative_operation_sent(int(operation["id"]))

        with self.assertRaisesRegex(StoreError, "exact observation is required"):
            await self.controller.handle_request(
                {
                    "command": "resolve_operation",
                    "thread_id": "operative-thread",
                    "decision": {
                        "operation_id": 98,
                        "resolution": "observed_failure",
                    },
                }
            )

    async def test_closeout_contract_refuses_each_missing_required_field(
        self,
    ) -> None:
        await self._register()
        dossier = await self.controller._build_operative_dossier()
        contract = await self._closeout_contract()
        for field in (
            "emergency_outcome",
            "root_cause",
            "repairs",
            "commits",
            "validation",
            "limitations",
            "source",
            "store",
            "services",
            "dispositions",
        ):
            with self.subTest(field=field):
                incomplete = deepcopy(contract)
                incomplete.pop(field)
                self.assertTrue(
                    self.controller._validate_closeout_evidence(incomplete, dossier)
                )
        for category in (
            "agents",
            "actions",
            "effects",
            "worktrees",
            "candidates",
            "publication_delivery",
        ):
            with self.subTest(disposition=category):
                incomplete = deepcopy(contract)
                incomplete["dispositions"].pop(category)
                failures = self.controller._validate_closeout_evidence(
                    incomplete, dossier
                )
                self.assertTrue(any(category in failure for failure in failures))

        evidence = Path(self.temporary.name) / "incomplete-closeout.json"
        evidence.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(StoreError, "closeout refused"):
            await self.controller._request_operative_finish(
                {"thread_id": "operative-thread", "evidence": str(evidence)}
            )
        self.assertEqual(read_journal(self.paths.operative_journal)["state"], "active")

    async def test_closeout_refuses_each_unresolved_dossier_category(self) -> None:
        await self._register()
        baseline = await self.controller._build_operative_dossier()
        contract = await self._closeout_contract()
        cases = {
            "agents": ("managed_tasks", {"id": 999, "role": "executor"}),
            "actions": ("actions", {"id": 999, "task_id": 999}),
            "effects": ("uncertain_external_operations", {"id": 999}),
            "candidates": ("candidates", {"assignment_id": 999}),
            "publication": ("beads_publication", {"bead_id": "brain-x"}),
            "delivery": ("obligations", {"id": 999}),
        }
        for category, (section, item) in cases.items():
            with self.subTest(category=category):
                unresolved = deepcopy(baseline)
                unresolved[section]["items"].append(item)
                unresolved[section]["count"] += 1
                self.assertTrue(
                    self.controller._operative_closeout_failures(unresolved, contract)
                )
        for category, mutate in (
            ("source", lambda value: value["git"]["items"][0].update(dirty=True)),
            ("worktrees", lambda value: value["worktrees"].update(complete=False)),
            (
                "store",
                lambda value: value["store_integrity"].update(quick_check="failed"),
            ),
            (
                "controller_service",
                lambda value: value["controller_service"].update(complete=False),
            ),
            ("app_server", lambda value: value["app_server"].update(complete=False)),
            (
                "readiness",
                lambda value: value["readiness"].update(
                    ready_without_takeover_fence=False
                ),
            ),
        ):
            with self.subTest(category=category):
                unresolved = deepcopy(baseline)
                mutate(unresolved)
                self.assertTrue(
                    self.controller._operative_closeout_failures(unresolved, contract)
                )

    async def test_linked_worktree_state_is_observed_and_blocks_closeout(self) -> None:
        source = Path(self.config.projects[0].repo_path)
        linked = Path(self.temporary.name) / "linked-worktree"
        subprocess.run(
            ["git", "worktree", "add", "-q", "-b", "linked-test", str(linked)],
            cwd=source,
            check=True,
        )
        changed = linked / "linked-change.txt"
        changed.write_text("uncommitted\n", encoding="utf-8")
        await self._register()

        dossier = await self.controller._build_operative_dossier()
        worktrees = self.controller._complete_section_items(dossier, "worktrees")
        assert worktrees is not None
        linked_item = next(
            item
            for item in worktrees
            if Path(str(item.get("path"))).resolve() == linked.resolve()
        )
        self.assertEqual(linked_item["coverage"], "observed")
        self.assertTrue(linked_item["head"])
        self.assertEqual(linked_item["branch"], "linked-test")
        self.assertTrue(linked_item["dirty"])
        self.assertEqual(
            linked_item["changes"]["items"], [{"path": "linked-change.txt"}]
        )

        contract = await self._closeout_contract()
        failures = self.controller._operative_closeout_failures(dossier, contract)
        self.assertTrue(any("worktree remains dirty" in item for item in failures))
        evidence = Path(self.temporary.name) / "dirty-worktree-closeout.json"
        evidence.write_text(json.dumps(contract), encoding="utf-8")
        with self.assertRaisesRegex(StoreError, "worktree remains dirty"):
            await self.controller._request_operative_finish(
                {"thread_id": "operative-thread", "evidence": str(evidence)}
            )

        real_run = subprocess.run

        def fail_linked_observation(
            arguments: list[str], **options: object
        ) -> subprocess.CompletedProcess[str]:
            if Path(str(options.get("cwd"))).resolve() == linked.resolve():
                return subprocess.CompletedProcess(arguments, 1, "", "unavailable")
            return real_run(arguments, **options)  # type: ignore[arg-type]

        with patch(
            "fulcrum.controller.subprocess.run", side_effect=fail_linked_observation
        ):
            unavailable = await self.controller._build_operative_dossier()
            with self.assertRaisesRegex(StoreError, "worktree state is unavailable"):
                await self.controller._request_operative_finish(
                    {"thread_id": "operative-thread", "evidence": str(evidence)}
                )
        unavailable_worktrees = self.controller._complete_section_items(
            unavailable, "worktrees"
        )
        assert unavailable_worktrees is not None
        unavailable_linked = next(
            item
            for item in unavailable_worktrees
            if Path(str(item.get("path"))).resolve() == linked.resolve()
        )
        self.assertEqual(unavailable_linked["coverage"], "unavailable")
        failures = self.controller._operative_closeout_failures(unavailable, contract)
        self.assertTrue(
            any("worktree state is unavailable" in item for item in failures)
        )

    async def test_successful_closeout_waits_for_terminal_turn_then_restores_intent(
        self,
    ) -> None:
        await self._register()
        evidence = Path(self.temporary.name) / "closeout.md"
        evidence.write_text(
            json.dumps(await self._closeout_contract()), encoding="utf-8"
        )

        result = await self.controller._request_operative_finish(
            {"thread_id": "operative-thread", "evidence": str(evidence)}
        )
        self.assertEqual(result["state"], "closing")
        self.assertEqual(read_journal(self.paths.operative_journal)["state"], "closing")
        self.assertEqual(
            self.controller.store.row(
                "SELECT value FROM meta WHERE key = 'dispatch_enabled'"
            )["value"],
            "0",
        )

        self.runtime.status = "idle"
        self.runtime.turn_status = "completed"
        await self.controller._handle_runtime_event(
            "turn/completed",
            {
                "threadId": "operative-thread",
                "turn": {"id": "operative-turn", "status": "completed"},
            },
        )

        self.assertEqual(read_journal(self.paths.operative_journal)["state"], "closed")
        self.assertEqual(self.runtime.archived_threads, ["operative-thread"])
        self.assertEqual(
            self.controller.store.row(
                "SELECT value FROM meta WHERE key = 'dispatch_enabled'"
            )["value"],
            "1",
        )
        action = self.controller.store.row(
            "SELECT * FROM actions WHERE kind = 'operative'"
        )
        self.assertEqual(action["state"], "processed")

    async def test_closeout_recovery_uses_retained_evidence_not_caller_file(
        self,
    ) -> None:
        baseline = Path(self.temporary.name) / "pre-takeover.sqlite3"
        with sqlite3.connect(baseline) as destination:
            self.controller.store.connection.backup(destination)
        await self._register()
        evidence = Path(self.temporary.name) / "mutable-closeout.json"
        contract = await self._closeout_contract()
        evidence.write_text(json.dumps(contract), encoding="utf-8")

        await self.controller._request_operative_finish(
            {"thread_id": "operative-thread", "evidence": str(evidence)}
        )
        journal = read_journal(self.paths.operative_journal)
        assert journal is not None
        retained = Path(str(journal["evidence_path"]))
        self.assertNotEqual(retained, evidence)
        self.assertEqual(json.loads(retained.read_text(encoding="utf-8")), contract)

        evidence.write_text('{"emergency_outcome":"tampered"}', encoding="utf-8")
        evidence.unlink()
        self.controller.store.close()
        self.controller.lock_handle.close()
        self.controller.lock_handle = None
        for suffix in ("", "-wal", "-shm"):
            Path(str(self.paths.database) + suffix).unlink(missing_ok=True)
        os.replace(baseline, self.paths.database)

        self.controller = Controller(self.paths, self.config)
        self.controller.runtime = self.runtime  # type: ignore[assignment]
        self.controller._observe_operative_services = (  # type: ignore[method-assign]
            self._observed_services
        )
        self.runtime.status = "idle"
        self.runtime.turn_status = "completed"
        await self.controller.reconcile()

        self.assertEqual(read_journal(self.paths.operative_journal)["state"], "closed")
        recovered = self.controller.store.row(
            "SELECT detail_json FROM operative_evidence WHERE kind = 'closeout'"
        )
        self.assertEqual(json.loads(recovered["detail_json"])["contract"], contract)

    async def test_closeout_refuses_missing_or_mismatched_retained_artifact(
        self,
    ) -> None:
        await self._register()
        evidence = Path(self.temporary.name) / "closeout-artifact.json"
        evidence.write_text(
            json.dumps(await self._closeout_contract()), encoding="utf-8"
        )
        await self.controller._request_operative_finish(
            {"thread_id": "operative-thread", "evidence": str(evidence)}
        )
        journal = read_journal(self.paths.operative_journal)
        assert journal is not None
        retained = Path(str(journal["evidence_path"]))
        original = retained.read_text(encoding="utf-8")
        self.runtime.status = "idle"
        self.runtime.turn_status = "completed"
        task = self.controller.store.row("SELECT * FROM tasks WHERE role = 'operative'")
        assert task is not None
        facts = await self.controller._observe_task(task)

        retained.unlink()
        with self.assertRaisesRegex(StoreError, "evidence is unavailable"):
            await self.controller._maybe_finalize_operative_closeout(task, facts)
        retained.write_text(original, encoding="utf-8")
        tampered = json.loads(original)
        tampered["root_cause"] = "different accepted evidence"
        retained.write_text(json.dumps(tampered), encoding="utf-8")
        with self.assertRaisesRegex(StoreError, "evidence is mismatched"):
            await self.controller._maybe_finalize_operative_closeout(task, facts)
        self.assertEqual(read_journal(self.paths.operative_journal)["state"], "closing")

    async def test_closing_with_durable_complete_store_result_resumes_to_closed(
        self,
    ) -> None:
        # Model the exact crash point after archival/store finalization but before
        # the authoritative journal replacement.
        now = utc_now()
        task = self.controller.store.register_task(
            native_thread_id="operative-thread",
            role="operative",
            description="Repair the failed controller",
            model="gpt-6-astra",
            reasoning_effort="high",
            state="archived",
        )
        self.controller.store.execute(
            "UPDATE tasks SET archived = 1 WHERE id = ?", (task["id"],)
        )
        action = self.controller.store.execute(
            """INSERT INTO actions(
                 task_id, kind, payload, state, outcome_kind, outcome_payload,
                 created_at, updated_at
               ) VALUES (?, 'operative', '{}', 'processed', 'complete', '{}', ?, ?)""",
            (task["id"], now, now),
        )
        journal = {
            "takeover_id": "crash-close",
            "state": "closing",
            "task_id": task["id"],
            "action_id": action.lastrowid,
            "native_thread_id": "operative-thread",
            "superseded_thread_ids": [],
            "scope": "repair the failed controller",
            "prior_dispatch_enabled": True,
            "completed_effects": [
                "closeout_evidence_retained",
                "closeout_revalidated",
            ],
            "next_step": "finalize close",
            "caller_verification": {"coverage": "observed"},
            "operative_identity": {
                "role_number": task["role_number"],
                "title": task["title"],
                "model": task["model"],
                "reasoning_effort": task["reasoning_effort"],
            },
            "operative_action": {
                "payload": {},
                "native_turn_id": None,
            },
            "created_at": now,
            "updated_at": now,
        }
        write_journal(self.paths.operative_journal, journal)
        self.controller.operative_journal = journal
        self.controller.store.mirror_operative_journal(journal)
        operation = self.controller.store.create_operative_operation(
            "crash-close",
            "thread_archive",
            "operative-thread",
            {"terminal": True},
            correlation_id="operative-close-archive:crash-close",
        )
        self.controller.store.finish_operative_operation(
            int(operation["id"]),
            state="complete",
            result={"observed": True},
            after={"archived": True},
        )
        self.controller.store.close()
        self.controller.lock_handle.close()
        self.controller.lock_handle = None
        self.controller = Controller(self.paths, self.config)
        self.controller.runtime = self.runtime  # type: ignore[assignment]
        self.controller._observe_operative_services = (  # type: ignore[method-assign]
            self._observed_services
        )

        await self.controller.reconcile()

        recovered = read_journal(self.paths.operative_journal)
        assert recovered is not None
        self.assertEqual(recovered["state"], "closed")

    async def test_startup_reconstructs_missing_journal_from_unfinished_store(
        self,
    ) -> None:
        result = await self._register()
        self.paths.operative_journal.unlink()
        self.controller.store.close()
        self.controller.lock_handle.close()
        self.controller.lock_handle = None

        self.controller = Controller(self.paths, self.config)

        journal = read_journal(self.paths.operative_journal)
        assert journal is not None
        self.assertEqual(journal["takeover_id"], result["takeover_id"])
        self.assertEqual(journal["state"], "active")
        self.assertTrue(self.controller._operative_fenced())

    async def _restart_with_empty_store(self, state: str) -> dict[str, object]:
        registered = await self._register()
        journal = read_journal(self.paths.operative_journal)
        assert journal is not None
        if state != "active":
            journal = transition_journal(
                self.paths.operative_journal,
                journal,
                state,
                now=utc_now(),
                next_step=f"recover {state} takeover",
                **(
                    {"closing_at": utc_now(), "evidence_path": "/tmp/evidence"}
                    if state == "closing"
                    else {"aborted_at": utc_now()}
                ),
            )
        self.controller.store.close()
        self.controller.lock_handle.close()
        self.controller.lock_handle = None
        for path in (
            self.paths.database,
            self.paths.database.with_name(self.paths.database.name + "-wal"),
            self.paths.database.with_name(self.paths.database.name + "-shm"),
        ):
            path.unlink(missing_ok=True)
        self.controller = Controller(self.paths, self.config)
        self.runtime = OperativeRuntime()
        self.runtime.name = str(registered["title"])
        self.controller.runtime = self.runtime  # type: ignore[assignment]
        self.controller._observe_operative_services = (  # type: ignore[method-assign]
            self._observed_services
        )
        return registered

    async def test_active_journal_reconstructs_bound_authority_in_empty_store(
        self,
    ) -> None:
        registered = await self._restart_with_empty_store("active")

        task = self.controller.store.row(
            "SELECT * FROM tasks WHERE native_thread_id = 'operative-thread'"
        )
        action = self.controller.store.current_action("operative-thread")
        self.assertEqual(task["title"], registered["title"])
        self.assertEqual(action["kind"], "operative")
        self.assertTrue(self.controller._is_bound_operative("operative-thread"))
        retried = await self._register()
        self.assertTrue(retried["reused"])
        self.assertTrue(self.controller._operative_fenced())

    async def test_closing_journal_reconstructs_bound_authority_in_empty_store(
        self,
    ) -> None:
        await self._restart_with_empty_store("closing")

        status = self.controller._operative_status({"thread_id": "operative-thread"})
        retried = await self.controller._request_operative_finish(
            {"thread_id": "operative-thread", "evidence": "/unused"}
        )
        self.assertEqual(status["state"], "closing")
        self.assertEqual(retried["state"], "closing")
        self.assertTrue(retried["reused"])
        self.assertTrue(self.controller._operative_fenced())

    async def test_aborted_journal_reconstructs_and_allows_explicit_recovery(
        self,
    ) -> None:
        registered = await self._restart_with_empty_store("aborted")
        evidence = Path(self.temporary.name) / "recovery.md"
        evidence.write_text("Original thread is unavailable.\n", encoding="utf-8")

        recovered = await self.controller._recover_operative(
            {
                "thread_id": "successor-thread",
                "takeover_id": registered["takeover_id"],
                "evidence": str(evidence),
            }
        )

        self.assertEqual(recovered["takeover_id"], registered["takeover_id"])
        self.assertEqual(recovered["state"], "active")
        self.assertEqual(
            read_journal(self.paths.operative_journal)["native_thread_id"],
            "successor-thread",
        )

    async def test_abort_keeps_fence_and_explicit_successor_resumes_same_takeover(
        self,
    ) -> None:
        registered = await self._register()
        evidence = Path(self.temporary.name) / "abort.md"
        evidence.write_text(
            "Runtime is unreachable; human authorized stop.\n", encoding="utf-8"
        )

        aborted = self.controller._abort_operative(
            {
                "thread_id": "operative-thread",
                "reason": "mandatory platform safety stop",
                "evidence": str(evidence),
            }
        )

        self.assertFalse(aborted["ok"])
        self.assertEqual(read_journal(self.paths.operative_journal)["state"], "aborted")
        self.assertTrue(self.controller._operative_fenced())
        with self.assertRaisesRegex(StoreError, "exact takeover ID"):
            await self.controller._recover_operative(
                {
                    "thread_id": "successor-thread",
                    "takeover_id": "wrong",
                    "evidence": str(evidence),
                }
            )

        recovered = await self.controller._recover_operative(
            {
                "thread_id": "successor-thread",
                "takeover_id": registered["takeover_id"],
                "evidence": str(evidence),
                "model": "gpt-6-astra",
                "effort": "high",
            }
        )

        journal = read_journal(self.paths.operative_journal)
        assert journal is not None
        self.assertEqual(recovered["takeover_id"], registered["takeover_id"])
        self.assertEqual(
            recovered["title"], "🗡️ [OPR0002] Repair the failed controller"
        )
        self.assertEqual(journal["state"], "active")
        self.assertEqual(journal["native_thread_id"], "successor-thread")
        self.assertEqual(journal["superseded_thread_ids"], ["operative-thread"])
        old = self.controller.store.row(
            "SELECT state FROM tasks WHERE native_thread_id = 'operative-thread'"
        )
        self.assertEqual(old["state"], "retired")

    def test_startup_mirrors_unfinished_journal_into_store(self) -> None:
        self.controller.store.close()
        self.controller.lock_handle.close()
        self.controller.lock_handle = None
        now = utc_now()
        journal = {
            "takeover_id": "journal-only",
            "state": "acquiring",
            "task_id": 999,
            "action_id": 999,
            "native_thread_id": "human-thread",
            "superseded_thread_ids": [],
            "scope": "restore controller state",
            "prior_dispatch_enabled": True,
            "completed_effects": ["authority_fence_written"],
            "next_step": "mirror operative task and action",
            "caller_verification": {"coverage": "observed"},
            "created_at": now,
            "updated_at": now,
        }
        write_journal(self.paths.operative_journal, journal)

        self.controller = Controller(self.paths, self.config)

        mirrored = self.controller.store.unfinished_operative_takeover()
        self.assertEqual(mirrored["takeover_id"], "journal-only")
        self.assertEqual(mirrored["state"], "acquiring")
        self.assertIsNone(mirrored["task_id"])
        self.assertIsNone(mirrored["action_id"])
        self.assertTrue(self.controller._operative_fenced())

    def test_invalid_transition_does_not_replace_journal(self) -> None:
        now = utc_now()
        journal = {
            "takeover_id": "takeover",
            "state": "acquiring",
            "native_thread_id": "thread",
            "superseded_thread_ids": [],
            "scope": "scope",
            "prior_dispatch_enabled": True,
            "completed_effects": [],
            "next_step": "activate",
            "caller_verification": {"coverage": "observed"},
            "created_at": now,
            "updated_at": now,
        }
        write_journal(self.paths.operative_journal, journal)
        with self.assertRaisesRegex(StoreError, "invalid operative transition"):
            transition_journal(
                self.paths.operative_journal,
                journal,
                "closed",
                now=now,
                next_step="invalid",
            )
        self.assertEqual(
            read_journal(self.paths.operative_journal)["state"], "acquiring"
        )


if __name__ == "__main__":
    unittest.main()
