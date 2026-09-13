from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fulcrum.config import RuntimePaths
from fulcrum.hook import handle_event
from fulcrum.outcomes import ALLOWED, finish_examples, finish_syntax, validate_outcome
from fulcrum.prompts import (
    action_notice,
    build_context,
    compaction_reminder,
    role_instructions,
    weaver_instructions,
)
from fulcrum.store import Store


class PromptsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.task = {
            "title": "Overseer",
            "native_thread_id": "thread",
            "role": "overseer",
        }
        self.assignment = {
            "id": 1,
            "bead_id": "p-1",
            "scope_snapshot": "Full approved scope",
            "worktree_path": "/tmp/worktree",
            "mandate_candidate_id": "c-1",
            "mandate_scope": "Full approved scope",
            "repair_permissions": '["ordinary_merge_conflict"]',
        }

    def test_notices_stay_short_even_with_large_action_records(self) -> None:
        payload = {
            "batch_items": [{"content": "large evidence " * 10000}],
            "retained_evidence": {"history": "record " * 10000},
        }
        for kind in ALLOWED:
            action = {"kind": kind, "payload": payload}
            with self.subTest(kind=kind):
                self.assertLess(len(action_notice(action).split()), 60)
                self.assertLess(
                    len(compaction_reminder(self.task, action).split()), 100
                )
                self.assertNotIn("large evidence", action_notice(action))
                self.assertNotIn(
                    "large evidence", compaction_reminder(self.task, action)
                )

    def test_context_preserves_scope_and_current_repair_without_repeating_history(
        self,
    ) -> None:
        action = {
            "kind": "correct",
            "payload": {
                "handoffs": [
                    {"kind": "implementation_evidence", "content": "long test output"},
                    {"kind": "review_findings", "content": "previous resolved defect"},
                    {"kind": "review_findings", "content": "current defect"},
                ]
            },
        }
        context = build_context(
            task=self.task, action=action, assignment=self.assignment
        )
        self.assertIn("Full approved scope", context)
        self.assertIn("ordinary_merge_conflict", context)
        self.assertIn("c-1", context)
        self.assertIn("current defect", context)
        self.assertNotIn("previous resolved defect", context)
        self.assertNotIn("long test output", context)
        evidence = build_context(task=self.task, action=action, section="evidence")
        self.assertIn("previous resolved defect", evidence)
        self.assertEqual(evidence.count("long test output"), 1)
        self.assertNotIn("You are Overseer", context)
        self.assertNotIn("--allow-repair", context)

    def test_specialist_continuation_retains_scope_but_exposes_no_second_interview(
        self,
    ) -> None:
        action = {
            "kind": "specialist",
            "payload": {
                "scope": {"projects": ["p"]},
                "prompt": "inspect scheduling",
                "continuation": "final report",
                "answers": [{"answer": "large answer"}],
                "retained_evidence": {
                    "projects": [{"project_id": "p"}],
                    "coverage": {"truncated": True},
                    "recent_events": ["long log"],
                },
            },
        }
        task = {**self.task, "role": "sage"}
        context = build_context(task=task, action=action)
        self.assertIn("inspect scheduling", context)
        self.assertIn('"truncated": true', context)
        self.assertNotIn("large answer", context)
        self.assertIn(
            "large answer", build_context(task=task, action=action, section="evidence")
        )
        self.assertNotIn(
            "evidence_needed", build_context(task=task, action=action, section="finish")
        )
        task["role"] = "inquisitor"
        action["payload"].pop("continuation")
        self.assertNotIn(
            "evidence_needed", build_context(task=task, action=action, section="finish")
        )

    def test_every_file_example_is_accepted_without_an_outcome_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            for kind in ALLOWED:
                for outcome, example in finish_examples(kind).items():
                    with self.subTest(kind=kind, outcome=outcome):
                        path.write_text(json.dumps(example))
                        validate_outcome(
                            kind,
                            outcome,
                            {"input": str(path), "reason": "Waiting for capacity"},
                        )

    def test_finish_reference_lists_every_accepted_outcome_as_a_complete_command(
        self,
    ) -> None:
        for kind, outcomes in ALLOWED.items():
            syntax = finish_syntax(kind)
            self.assertNotIn(" | ", syntax)
            for outcome in outcomes:
                self.assertIn(f"fulcrum finish {outcome}", syntax)

    def test_planning_registration_has_no_finish_obligation(self) -> None:
        text = weaver_instructions(plan_mode=True, project="p")
        self.assertIn("do not publish or call finish", text)
        self.assertNotIn("Finish exactly once", text)
        self.assertIn("cold reader", text)
        self.assertIn("requirements verifier", text)
        self.assertIn(
            "whole-codebase", role_instructions("specialist", role="inquisitor")
        )

    def test_hook_is_quiet_for_unmanaged_and_actionless_threads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = RuntimePaths(
                brain_root=root / "brain",
                state_root=root / "state",
                config_file=root / "config.json",
                control_root=root / "control",
            )
            with Store(paths.database) as store:
                task = store.register_task(
                    native_thread_id="planning",
                    role="weaver",
                    description="Plan",
                    model="sol",
                    reasoning_effort="high",
                )
                with patch("fulcrum.hook.resolve_paths", return_value=paths):
                    for thread in ("unrelated", "planning"):
                        self.assertEqual(
                            handle_event(
                                {
                                    "hook_event_name": "SessionStart",
                                    "source": "compact",
                                    "session_id": thread,
                                }
                            ),
                            {"continue": True},
                        )
                    store.execute(
                        "INSERT INTO actions(task_id, kind, payload, state, created_at, updated_at) VALUES (?, 'weaver', ?, 'active', 'now', 'now')",
                        (task["id"], json.dumps({"large": "huge " * 10000})),
                    )
                    result = handle_event(
                        {
                            "hook_event_name": "SessionStart",
                            "source": "compact",
                            "session_id": "planning",
                        }
                    )
                    reminder = result["hookSpecificOutput"]["additionalContext"]
                    self.assertLess(len(reminder.split()), 100)
                    self.assertNotIn("huge", reminder)


if __name__ == "__main__":
    unittest.main()
