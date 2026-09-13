from __future__ import annotations

import json
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fulcrum.cli import _intake_payload, build_parser
from fulcrum.config import RuntimePaths
from fulcrum.hook import handle_event
from fulcrum.outcomes import ALLOWED, finish_examples, finish_syntax, validate_outcome
from fulcrum.prompts import (
    action_message,
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

    def test_archon_message_contains_the_decision_without_fetching_context(
        self,
    ) -> None:
        action = {
            "id": 42,
            "kind": "archon",
            "payload": {
                "batch_items": [
                    {
                        "update_id": 17,
                        "content": {
                            "kind": "proposal",
                            "bead_id": "p-1",
                            "project": "p",
                            "title": "Fix empty results",
                            "scope": "Show an empty state when search returns no matches; test both paths.",
                        },
                    }
                ],
                "fleet_snapshot": {
                    "capacity": {
                        "global_usage": 1,
                        "global_limit": 4,
                        "project_usage": {"p": 0},
                        "project_limits": {"p": 2},
                    },
                    "policies": [{"unchanged": "policy " * 10000}],
                },
            },
        }
        text = action_message(action=action)
        self.assertIn("Approve or defer p-1 (p)", text)
        self.assertIn("Update 17", text)
        self.assertIn(
            "Show an empty state when search returns no matches; test both paths.", text
        )
        self.assertIn("p 0/2", text)
        self.assertTrue(text.startswith("Action 42. Finish required: yes."))
        self.assertIn("fulcrum finish decisions", text)
        self.assertLess(len(text.split()), 150)
        self.assertNotIn("unchanged", text)
        self.assertNotIn("fulcrum instructions", text)
        self.assertNotIn("1 updates", text)
        for irrelevant in (
            "resolve_operation",
            "resolve_escalation",
            "request_specialist",
            "retire_archon",
            "set_models",
            "suspend_policy",
        ):
            self.assertNotIn(irrelevant, text)

    def test_archon_completion_message_gives_exact_minimal_acknowledgement(
        self,
    ) -> None:
        text = action_message(
            action={
                "id": 8,
                "kind": "archon",
                "payload": {
                    "batch_items": [
                        {
                            "update_id": 2,
                            "content": {
                                "kind": "assignment_completed",
                                "bead_id": "p-1",
                                "assignment_id": 1,
                                "run_id": 1,
                                "minor_fixes": [
                                    {
                                        "problem": "Tighten wording",
                                        "evidence": "README.md:20",
                                        "requested_change": "Use a shorter sentence",
                                    }
                                ],
                            },
                        }
                    ],
                    "fleet_snapshot": {
                        "capacity": {
                            "global_usage": 0,
                            "global_limit": 2,
                            "project_usage": {"p": 0},
                            "project_limits": {"p": 2},
                        }
                    },
                },
            }
        )
        self.assertTrue(
            text.startswith(
                "Action 8. Finish required: yes. Relevant outcome: decisions."
            )
        )
        self.assertIn('{"decisions": [], "handled_update_ids": [2]}', text)
        self.assertIn("Overseer nonblocking follow-up", text)
        self.assertIn("requires a new Weaver bead", text)
        for irrelevant in (
            "request_specialist",
            "resolve_operation",
            "resolve_escalation",
            '"decision": "approve"',
            "fulcrum finish deferred",
        ):
            self.assertNotIn(irrelevant, text)
        self.assertLess(len(text.split()), 145)

    def test_archon_completion_omits_absent_minor_followup(self) -> None:
        text = action_message(
            action={
                "id": 9,
                "kind": "archon",
                "payload": {
                    "batch_items": [
                        {
                            "update_id": 3,
                            "content": {
                                "kind": "assignment_completed",
                                "bead_id": "p-2",
                                "assignment_id": 2,
                                "run_id": 2,
                                "minor_fixes": [],
                            },
                        }
                    ]
                },
            }
        )
        self.assertNotIn("nonblocking follow-up", text)
        self.assertNotIn("Weaver bead", text)

    def test_archon_initial_policy_message_is_project_specific(self) -> None:
        text = action_message(
            action={
                "id": 1,
                "kind": "archon",
                "payload": {
                    "purpose": "initial_policies",
                    "projects": ["fulcrum", "other"],
                    "required": "Set initial policy.",
                },
            }
        )
        self.assertTrue(text.startswith("Action 1. Finish required: yes."))
        self.assertIn('"project_limits": {"fulcrum":', text)
        self.assertIn('"scope": "fulcrum"', text)
        self.assertIn('"scope": "other"', text)
        self.assertIn("offset project Inquisitor anchors twelve hours", text)
        self.assertNotIn("request_specialist", text)
        self.assertNotIn("resolve_operation", text)

    def test_archon_preserves_relevant_conflicts_holds_and_unknown_exceptions(
        self,
    ) -> None:
        action = {
            "kind": "archon",
            "payload": {
                "batch_items": [
                    {
                        "update_id": 2,
                        "content": {
                            "kind": "proposal",
                            "bead_id": "p-2",
                            "project": "p",
                            "title": "Repair",
                            "scope": "exact scope " * 1000,
                        },
                    },
                    {
                        "update_id": 3,
                        "content": {
                            "kind": "new_exception",
                            "reason": "unknown delivery outcome",
                            "decision_needed": "attach observed candidate",
                        },
                    },
                ],
                "fleet_snapshot": {
                    "unfinished_assignments": [
                        {
                            "id": 1,
                            "bead_id": "p-1",
                            "run_id": 1,
                            "project_id": "p",
                            "stage": "implementing",
                            "condition": "overlapping source",
                        }
                    ],
                    "holds": [
                        {
                            "id": 4,
                            "scope": "project",
                            "target": "p",
                            "reason": "benchmark",
                            "release_condition": "measurement complete",
                        }
                    ],
                },
            },
        }
        text = action_message(action=action)
        for required in (
            "exact scope " * 1000,
            "overlapping source",
            "measurement complete",
            "unknown delivery outcome",
            "attach observed candidate",
            "Update 3",
        ):
            self.assertIn(required, text)

    def test_compaction_is_a_reminder_not_a_replay_or_required_read(self) -> None:
        for kind in ALLOWED:
            action = {"kind": kind, "payload": {"large": "history " * 10000}}
            text = compaction_reminder(self.task, action)
            self.assertLess(len(text.split()), 55)
            self.assertNotIn("history", text)
            self.assertNotIn("fulcrum instructions", text)
            self.assertNotIn("--section", text)

    def test_correction_inlines_current_finding_and_permission_only(self) -> None:
        action = {
            "kind": "correct",
            "payload": {
                "handoffs": [
                    {"kind": "implementation_evidence", "content": "old test output"},
                    {"kind": "review_findings", "content": "previous resolved defect"},
                    {"kind": "review_findings", "content": "current defect"},
                ]
            },
        }
        text = action_message(action=action, assignment=self.assignment)
        for required in (
            "Full approved scope",
            "ordinary_merge_conflict",
            "c-1",
            "current defect",
        ):
            self.assertIn(required, text)
        for obsolete in (
            "previous resolved defect",
            "old test output",
            "--section",
            "You are Executor",
        ):
            self.assertNotIn(obsolete, text)

    def test_review_contains_latest_authored_evidence_once(self) -> None:
        action = {
            "kind": "review",
            "payload": {
                "handoffs": [
                    {
                        "kind": "implementation_evidence",
                        "content": {"evidence_content": "obsolete output"},
                    },
                    {
                        "kind": "implementation_evidence",
                        "content": {
                            "evidence": "/tmp/evidence.md",
                            "evidence_content": "commit abc: check passed",
                        },
                    },
                ]
            },
        }
        text = action_message(action=action, assignment=self.assignment)
        self.assertEqual(text.count("commit abc: check passed"), 1)
        self.assertNotIn("obsolete output", text)
        self.assertNotIn("--section", text)

    def test_specialist_followup_supplies_answers_without_replaying_initial_evidence(
        self,
    ) -> None:
        action = {
            "kind": "specialist",
            "payload": {
                "scope": {"projects": ["p"]},
                "prompt": "inspect scheduling",
                "continuation": "final report",
                "answers": [
                    {"subject": "executor", "answer": "the handoff lost my evidence"}
                ],
                "missing_evidence": ["overseer"],
                "retained_evidence": {"recent_events": ["old log " * 1000]},
            },
        }
        text = action_message(action=action)
        for required in (
            "inspect scheduling",
            "Projects: p",
            "the handoff lost my evidence",
            "overseer",
            "no further interview round",
        ):
            self.assertIn(required, text)
        self.assertNotIn("old log", text)
        self.assertNotIn("fulcrum instructions", text)

    def test_interview_contains_the_question_and_finish_shape(self) -> None:
        text = action_message(
            action={
                "kind": "interview",
                "payload": {"question": "Why did review need three turns?"},
            }
        )
        self.assertIn("Why did review need three turns?", text)
        self.assertIn("fulcrum finish interview_answer", text)
        self.assertIn('"answer"', text)

    def test_cli_has_no_instruction_fetch_interface(self) -> None:
        self.assertNotIn("instructions", build_parser().format_help())

    def test_cli_exposes_bootstrap_operation_resolution(self) -> None:
        args = build_parser().parse_args(
            [
                "resolve-operation",
                "--operation-id",
                "17",
                "--resolution",
                "confirmed_unsent",
                "--evidence",
                "exact runtime inventory is empty",
            ]
        )
        self.assertEqual(args.command, "resolve-operation")
        self.assertEqual(args.operation_id, 17)
        self.assertEqual(args.resolution, "confirmed_unsent")

    def test_creation_supplies_role_and_command_reference_without_fetches(self) -> None:
        text = role_instructions("archon", role="archon")
        self.assertIn("You are Archon", text)
        self.assertNotIn("fulcrum instructions", text)
        self.assertIn("independent compatible beads", text)
        self.assertIn("separate project-scoped runs", text)
        self.assertIn("normal human follow-up", text)
        self.assertNotIn("fulcrum finish decisions", text)
        self.assertNotIn('"resolve_operation"', text)
        self.assertLess(len(text.split()), 230)

    def test_executor_creation_instructions_are_concise_and_action_specific(
        self,
    ) -> None:
        text = role_instructions("implement", role="executor")
        for required in (
            "You are Executor",
            "# Required result",
            "# Ownership boundaries",
            "# Corrections only",
            "# Finish",
            "Make repository edits only in the assigned worktree",
            "Repository push or publication requirements",
            "evidence file may use a",
            "If Repair permission is unclear",
            "Workflow debrief only",
            "fulcrum finish ready_for_review",
            "fulcrum finish permitted_repair_complete",
            "fulcrum finish checkpointed",
            "fulcrum finish blocked",
        ):
            self.assertIn(required, text)
        for irrelevant in (
            "Planning Weaver",
            "An interview temporarily replaces",
            "Do not manage other conversations",
        ):
            self.assertNotIn(irrelevant, text)
        self.assertEqual(text.count("wait for helper agents"), 1)
        self.assertLess(len(text.split()), 380)

    def test_overseer_creation_instructions_are_concise_and_candidate_specific(
        self,
    ) -> None:
        text = role_instructions("review", role="overseer")
        for required in (
            "You are Overseer",
            "# Review",
            "# Boundaries",
            "source and tests are authoritative",
            "minor fixes",
            "may be promoted unchanged",
            "Wait only for helpers you started",
            "fulcrum finish approved --input",
            '"minor_fixes"',
            '"repair_permissions"',
        ):
            self.assertIn(required, text)
        for irrelevant in (
            "Planning Weaver",
            "An interview temporarily replaces",
            "Wait for native helpers",
            "No blocking findings remain",
        ):
            self.assertNotIn(irrelevant, text)
        self.assertLess(len(text.split()), 480)

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
                            {"input": example, "reason": "Waiting for capacity"},
                        )

    def test_finish_reference_lists_every_accepted_outcome_as_a_complete_command(
        self,
    ) -> None:
        for kind, outcomes in ALLOWED.items():
            syntax = finish_syntax(kind)
            self.assertNotIn(" | ", syntax)
            for outcome in outcomes:
                self.assertIn(f"fulcrum finish {outcome}", syntax)

    def test_approval_cli_uses_one_structured_input(self) -> None:
        args = build_parser().parse_args(
            ["finish", "approved", "--input", "/tmp/approval.json"]
        )
        self.assertEqual(args.input, "/tmp/approval.json")
        with self.assertRaises(SystemExit), patch("sys.stderr", new=io.StringIO()):
            build_parser().parse_args(["finish", "approved", "--assessment", "Correct"])

    def test_planning_registration_has_no_finish_obligation(self) -> None:
        text = weaver_instructions(plan_mode=True, project="p")
        self.assertIn("do not publish or call finish", text)
        self.assertNotIn("Finish exactly once", text)
        self.assertIn("cold reader", text)
        self.assertIn("requirements verifier", text)
        self.assertIn(
            "whole-codebase", role_instructions("specialist", role="inquisitor")
        )
        self.assertIn("Do not dump", role_instructions("specialist", role="sage"))
        self.assertIn(
            "Do not\ndump every source file",
            role_instructions("specialist", role="inquisitor"),
        )

    def test_writable_weaver_uses_stdin_json_for_shell_safe_intake(self) -> None:
        text = weaver_instructions(plan_mode=False, project="p")
        self.assertIn("fulcrum intake --input -", text)
        self.assertIn("single-quoted shell heredoc", text)
        self.assertNotIn('fulcrum intake --title "..."', text)
        self.assertNotIn("Subsequent messages contain the actual request", text)
        self.assertIn(
            'Interpret requests such as "fix this," "change this," or "please revise this file"',
            text,
        )
        self.assertIn(
            "Imperative wording does\nnot authorize you to edit source files", text
        )
        self.assertIn("run exactly one command", text)
        self.assertLess(
            text.index("fulcrum finish intake_complete"),
            text.index("fulcrum finish future_plan"),
        )
        self.assertLess(
            text.index("fulcrum finish future_plan"),
            text.index("fulcrum finish blocked"),
        )
        self.assertIn(
            "For future_plan only, --evidence is the absolute path to the saved plan",
            text,
        )
        self.assertIn("intake_complete and blocked do not use an evidence file", text)

        args = build_parser().parse_args(["intake", "--input", "-"])
        with patch("sys.stdin", io.StringIO('{"title":"Use `code` safely"}')):
            self.assertEqual(_intake_payload(args)["title"], "Use `code` safely")

    def test_correction_prompt_requires_one_release_based_commit(self) -> None:
        text = action_message(
            action={
                "kind": "correct",
                "payload": {"predecessor_candidate_id": "candidate-1"},
            },
            assignment={
                "bead_id": "p-1",
                "scope_snapshot": "Fix it",
                "worktree_path": "/tmp/worktree",
                "repair_permissions": "[]",
            },
        )
        self.assertIn("exactly one task commit", text)
        self.assertIn("rebase it onto `release`", text)

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
