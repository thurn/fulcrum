"""Focused lifecycle hook behavior and hook-source installation checks."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fulcrum.config import RuntimePaths
from fulcrum.hook import HANDOFF_REMINDER, handle_event
from fulcrum.hook_config import install_hook_source
from fulcrum.state import atomic_write_record


class HookTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.paths = RuntimePaths(root / "brain", root / "state", root / "config.json")
        now = "2026-09-11T20:00:00Z"
        self.registry = {
            "record_kind": "role_run_registry",
            "schema_version": 1,
            "writer_id": "task-archon",
            "updated_at": now,
            "current_archon_task_id": "task-archon",
            "roles": [
                {
                    "role": "executor",
                    "project_id": "fulcrum",
                    "host_id": "local",
                    "task_id": "task-executor",
                    "identity_state": "resolved",
                    "role_number": 1,
                    "run_id": "run-executor",
                    "pair_id": "pair-1",
                    "title": "Executor",
                    "selected_model": "model",
                    "selected_reasoning": "high",
                    "model_authorization": {"source": "default", "reference": None},
                    "skill_revision": "abc123",
                }
            ],
        }
        atomic_write_record(self.paths, self.registry)
        self.progress = {
            "record_kind": "progress",
            "schema_version": 1,
            "writer_id": "task-executor",
            "updated_at": now,
            "role_task_id": "task-executor",
            "role": "executor",
            "phase": "implementing",
            "phase_started_at": now,
            "expected_next_actor": "task-overseer",
            "expected_next_action": "Review candidate",
            "handoff_needed": True,
            "handoff_sent": False,
            "delivery_error": None,
            "owned_resources": [],
        }
        atomic_write_record(self.paths, self.progress)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def event(self, name: str, **values: object) -> dict[str, object]:
        return {
            "hook_event_name": name,
            "session_id": "task-executor",
            "permission_mode": "default",
            **values,
        }

    def test_compaction_uses_exact_identity_and_bounded_context(self) -> None:
        result = handle_event(
            self.event("SessionStart", source="compact"), paths=self.paths
        )
        text = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Actual task: task-executor", text)
        self.assertLessEqual(len(text), 3600)
        unknown = handle_event(
            {**self.event("SessionStart", source="compact"), "session_id": "other"},
            paths=self.paths,
        )
        self.assertEqual(unknown, {"continue": True})

    def test_stop_requests_at_most_one_missing_handoff_correction(self) -> None:
        result = handle_event(
            self.event("Stop", stop_hook_active=False), paths=self.paths
        )
        self.assertEqual(result, {"decision": "block", "reason": HANDOFF_REMINDER})
        repeated = handle_event(
            self.event("Stop", stop_hook_active=True), paths=self.paths
        )
        self.assertEqual(repeated, {"continue": True})

    def test_stop_exclusions_are_nonintervening(self) -> None:
        events = [
            {**self.event("Stop"), "permission_mode": "plan"},
            {**self.event("Stop"), "session_id": "unrelated"},
        ]
        for event in events:
            self.assertEqual(handle_event(event, paths=self.paths), {"continue": True})

        for changes in (
            {"phase": "completed"},
            {"handoff_sent": True},
            {"handoff_needed": False},
        ):
            progress = dict(self.progress)
            progress.update(changes)
            atomic_write_record(self.paths, progress)
            self.assertEqual(
                handle_event(self.event("Stop"), paths=self.paths), {"continue": True}
            )

        atomic_write_record(self.paths, self.progress)
        atomic_write_record(
            self.paths,
            {
                "record_kind": "interview",
                "schema_version": 1,
                "writer_id": "task-sage",
                "updated_at": "2026-09-11T20:00:00Z",
                "sage_task_id": "task-sage",
                "run_id": "debrief-1",
                "subject_task_id": "task-executor",
                "prior_archived": False,
                "reminder_state": "not_due",
                "completion_state": "active",
            },
        )
        self.assertEqual(
            handle_event(self.event("Stop"), paths=self.paths), {"continue": True}
        )

        registry = dict(self.registry)
        registry["roles"] = [dict(self.registry["roles"][0], role="sage")]
        atomic_write_record(self.paths, registry)
        self.assertEqual(
            handle_event(self.event("Stop"), paths=self.paths), {"continue": True}
        )

    def test_missing_or_invalid_progress_never_intervenes(self) -> None:
        path = self.paths.state_root / "progress" / "task-executor.json"
        path.unlink()
        self.assertEqual(
            handle_event(self.event("Stop"), paths=self.paths), {"continue": True}
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not-json", encoding="utf-8")
        self.assertEqual(
            handle_event(self.event("Stop"), paths=self.paths), {"continue": True}
        )

    def test_hook_install_preserves_unrelated_handlers_and_is_idempotent(self) -> None:
        config = Path(self.temporary.name) / "hooks.json"
        config.write_text(
            json.dumps(
                {
                    "description": "mine",
                    "hooks": {
                        "Stop": [
                            {"hooks": [{"type": "command", "command": "/other/hook"}]}
                        ],
                        "PreToolUse": [{"matcher": "Bash", "hooks": []}],
                    },
                }
            ),
            encoding="utf-8",
        )
        command = Path(self.temporary.name) / "bin" / "fulcrum-hook"
        once = install_hook_source(config, command)
        twice = install_hook_source(config, command)
        self.assertEqual(once, twice)
        self.assertEqual(twice["description"], "mine")
        self.assertEqual(
            twice["hooks"]["Stop"][0]["hooks"][0]["command"], "/other/hook"
        )
        self.assertEqual(len(twice["hooks"]["Stop"]), 2)
        self.assertIn("PreToolUse", twice["hooks"])


if __name__ == "__main__":
    unittest.main()
