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
                            "scope_summary": "Show an empty state when search returns no matches; test both paths.",
                            "scope_reference": "scope:17",
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
        self.assertIn("p-1 (p) — Fix empty results", text)
        self.assertIn("Update 17", text)
        self.assertIn(
            "Show an empty state when search returns no matches; test both paths.", text
        )
        self.assertIn("scope:17", text)
        self.assertIn("p 0/2", text)
        self.assertTrue(text.startswith("Action 42. Finish required: yes."))
        self.assertIn("fulcrum finish decisions", text)
        self.assertLess(len(text.split()), 150)
        self.assertNotIn("unchanged", text)
        self.assertNotIn("fulcrum instructions", text)
        self.assertNotIn("1 updates", text)
        self.assertNotIn("/absolute/decisions.json", text)
        self.assertNotIn("/absolute/reactivation.json", text)
        for irrelevant in (
            "resolve_operation",
            "resolve_escalation",
            "request_specialist",
            "retire_archon",
            "set_models",
            "suspend_policy",
        ):
            self.assertNotIn(irrelevant, text)

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

    def test_bound_messages_render_only_exact_structured_destinations(self) -> None:
        root = "/private/control/handoffs/" + "a" * 32 + "/action-42"
        archon_paths = {
            "decisions": root + "/decisions.json",
            "deferred": root + "/reactivation.json",
        }
        deferral = action_message(
            action={"id": 42, "kind": "archon", "payload": {}},
            handoff_paths=archon_paths,
            role="archon",
        )
        self.assertEqual(deferral.count(archon_paths["decisions"]), 2)
        self.assertEqual(deferral.count(archon_paths["deferred"]), 2)

        acknowledgement = action_message(
            action={
                "id": 42,
                "kind": "archon",
                "payload": {
                    "batch_items": [
                        {
                            "update_id": 1,
                            "content": {
                                "kind": "assignment_completed",
                                "bead_id": "p-1",
                                "assignment_id": 1,
                                "run_id": 1,
                            },
                        }
                    ]
                },
            },
            handoff_paths=archon_paths,
            role="archon",
        )
        self.assertIn(archon_paths["decisions"], acknowledgement)
        self.assertNotIn(archon_paths["deferred"], acknowledgement)
        self.assertIn("finish with decisions.", acknowledgement)
        self.assertNotIn("decisions or deferred", acknowledgement)

        judgment = action_message(
            action={
                "id": 42,
                "kind": "archon",
                "payload": {
                    "batch_items": [
                        {
                            "update_id": 1,
                            "content": {
                                "kind": "assignment_completed",
                                "bead_id": "p-1",
                                "assignment_id": 1,
                                "run_id": 1,
                                "required_decision": "Choose recovery",
                            },
                        }
                    ]
                },
            },
            handoff_paths=archon_paths,
            role="archon",
        )
        self.assertIn("finish with decisions or deferred", judgment)
        self.assertEqual(judgment.count(archon_paths["decisions"]), 2)
        self.assertEqual(judgment.count(archon_paths["deferred"]), 2)

        review_paths = {
            "approved": root + "/approval.json",
            "changes_requested": root + "/findings.json",
            "incomplete": root + "/missing.json",
        }
        review = action_message(
            action={"id": 42, "kind": "review", "payload": {}},
            assignment=self.assignment,
            handoff_paths=review_paths,
            role="overseer",
        )
        for path in review_paths.values():
            self.assertIn(path, review)
        self.assertNotIn("/absolute/approval.json", review)

        specialist_paths = {
            "report": root + "/report.json",
            "evidence_needed": root + "/requests.json",
        }
        specialist = action_message(
            action={
                "id": 42,
                "kind": "specialist",
                "payload": {"scope": {"projects": ["p"]}},
            },
            handoff_paths=specialist_paths,
            role="sage",
        )
        for path in specialist_paths.values():
            self.assertIn(path, specialist)

        answer_path = root + "/answer.json"
        interview = action_message(
            action={
                "id": 42,
                "kind": "interview",
                "payload": {"question": "Why?"},
            },
            handoff_paths={"interview_answer": answer_path},
            role="executor",
        )
        self.assertIn(answer_path, interview)

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
                            "scope_summary": "exact scope " * 1000,
                            "scope_reference": "scope:2",
                            "dependencies": ["p-0"],
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
                            "conflict_keys": ["shared-schema"],
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
            "overlapping source",
            "measurement complete",
            "unknown delivery outcome",
            "attach observed candidate",
            'dependencies: ["p-0"]',
            'Conflicts for assignment 1: ["shared-schema"]',
            "Update 3",
        ):
            self.assertIn(required, text)
        self.assertLess(len(text), 2500)
        self.assertNotIn("exact scope " * 30, text)

    def test_compaction_is_a_reminder_not_a_replay_or_required_read(self) -> None:
        for kind in ALLOWED:
            action = {"kind": kind, "payload": {"large": "history " * 10000}}
            text = compaction_reminder(self.task, action)
            self.assertLess(len(text.split()), 55)
            self.assertNotIn("history", text)
            self.assertNotIn("fulcrum instructions", text)
            self.assertNotIn("--section", text)
            self.assertIn("fulcrum context", text)

    def test_followup_assignment_message_omits_unchanged_scope(self) -> None:
        text = action_message(
            action={"kind": "implement", "payload": {}},
            assignment=self.assignment,
            include_scope=False,
        )

        self.assertIn("approved scope is unchanged", text)
        self.assertNotIn("Full approved scope", text)

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

        recovered = action_message(action=action, full_context=True)
        self.assertIn("Retained recent events", recovered)
        self.assertIn("old log", recovered)

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

    def test_weaver_registration_requires_a_nonempty_description(self) -> None:
        parser = build_parser()
        for arguments in (
            ["weaver", "register"],
            ["weaver", "register", "--description", "   "],
        ):
            with self.assertRaises(SystemExit), patch("sys.stderr", new=io.StringIO()):
                parser.parse_args(arguments)

        description = "Explain $(touch /tmp/not-run); quotes ' and `ticks`"
        args = parser.parse_args(
            [
                "weaver",
                "register",
                "--description",
                description,
                "--plan-mode",
            ]
        )
        self.assertEqual(args.description, description)
        self.assertTrue(args.plan_mode)

    def test_sage_parser_separates_registration_from_queued_requests(self) -> None:
        parser = build_parser()
        registered = parser.parse_args(
            [
                "sage",
                "register",
                "--item",
                "p-1",
                "--description",
                "Review failed delivery handoff",
            ]
        )
        self.assertEqual(registered.sage_command, "register")
        self.assertEqual(registered.item, "p-1")
        requested = parser.parse_args(
            ["sage", "request", "--project", "p", "--scope", "review retries"]
        )
        self.assertEqual(requested.sage_command, "request")
        self.assertEqual(requested.project, "p")
        for arguments in (
            ["sage", "--scope", "old interface"],
            ["sage", "register", "--item", "p-1", "--description", "too short"],
            [
                "sage",
                "register",
                "--item",
                "p-1",
                "--description",
                "Review $(touch unsafe) workflow",
            ],
            [
                "sage",
                "register",
                "--item",
                "p-1';touch-bad",
                "--description",
                "Review failed delivery handoff",
            ],
        ):
            with self.assertRaises(SystemExit), patch("sys.stderr", new=io.StringIO()):
                parser.parse_args(arguments)

    def test_direct_sage_prompt_requires_exact_pair_and_causal_evidence(self) -> None:
        text = role_instructions("specialist", role="sage")
        for required in (
            "retained causal workflow",
            "exactly one interview round",
            "supplied Executor and Overseer",
            "unknown or partial",
            "Do not dump complete databases",
            "one independent finding",
        ):
            self.assertIn(required, text)

    def test_role_creation_exposes_optional_context_recovery(self) -> None:
        for action_kind, role in (
            ("archon", "archon"),
            ("implement", "executor"),
            ("review", "overseer"),
            ("specialist", "sage"),
            ("specialist", "inquisitor"),
        ):
            self.assertIn("fulcrum context", role_instructions(action_kind, role=role))
        self.assertIn(
            "fulcrum context", weaver_instructions(plan_mode=False, project="p")
        )
        self.assertNotIn(
            "fulcrum context", weaver_instructions(plan_mode=True, project="p")
        )

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
        self.assertLess(len(text.split()), 280)

    def test_bound_messages_use_exact_paths_and_atomic_publish_wording(self) -> None:
        root = "/private/control/handoffs/" + "a" * 32 + "/action-42"
        cases = (
            (
                {"id": 42, "kind": "archon", "payload": {"batch_items": []}},
                None,
                {
                    "decisions": f"{root}/decisions.json",
                    "deferred": f"{root}/reactivation.json",
                },
                ("decisions", "deferred"),
            ),
            (
                {"id": 42, "kind": "review", "payload": {}},
                self.assignment,
                {
                    "approved": f"{root}/approval.json",
                    "changes_requested": f"{root}/findings.json",
                    "incomplete": f"{root}/missing.json",
                },
                ("approved", "changes_requested", "incomplete"),
            ),
            (
                {
                    "id": 42,
                    "kind": "specialist",
                    "payload": {"scope": {"projects": ["p"]}},
                },
                None,
                {
                    "report": f"{root}/report.json",
                    "evidence_needed": f"{root}/requests.json",
                },
                ("report", "evidence_needed"),
            ),
            (
                {"id": 42, "kind": "interview", "payload": {"question": "Why?"}},
                None,
                {"interview_answer": f"{root}/answer.json"},
                ("interview_answer",),
            ),
        )
        for action, assignment, paths, outcomes in cases:
            text = action_message(
                action=action,
                assignment=assignment,
                handoff_paths=paths,
                role="sage",
            )
            self.assertIn("sibling temporary file", text)
            self.assertIn("atomically rename", text)
            for outcome in outcomes:
                self.assertIn(paths[outcome], text)
                self.assertIn(f"fulcrum finish {outcome}", text)
            self.assertNotIn("/absolute/", text)

    def test_reminder_reuses_exact_bound_destination(self) -> None:
        path = "/private/control/handoffs/" + "b" * 32 + "/action-9/report.json"
        text = action_message(
            action={
                "id": 9,
                "kind": "specialist",
                "payload": {},
                "reminder_sent": 1,
            },
            handoff_paths={"report": path},
            role="inquisitor",
        )
        self.assertIn(path, text)
        self.assertIn("atomically rename", text)

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

    def test_writable_weaver_questions_require_a_later_filing_turn(self) -> None:
        text = weaver_instructions(plan_mode=False, project="p")

        for required in (
            "Any human prompt phrased as a question or containing a question",
            "This rule takes precedence over action wording",
            'neither "What causes this bug?" nor "What causes this\nbug, and please file a task to fix it" authorizes intake',
            "Do not run `fulcrum intake` or otherwise\nfile a task or Bead during that turn",
            "only after a subsequent human message explicitly instructs Weaver\nto file or create the task or Bead",
            "merely answers Weaver's clarifying\nquestion is not filing authorization",
            'a later message saying "File the\ntask now" authorizes intake',
            '"Would you file the task now?" remains\ninvestigative',
        ):
            self.assertIn(required, text)

        skill = (
            Path(__file__).parents[1] / "skills" / "fulcrum-weaver" / "SKILL.md"
        ).read_text(encoding="utf-8")
        for required in (
            "Any human prompt phrased as a question or containing a question",
            "even when the same prompt also requests action",
            'neither "What causes this bug?" nor "What causes this bug,\nand please file a task to fix it" authorizes `fulcrum intake`',
            "must not file a task or Bead during that turn",
            "only after a subsequent human message explicitly instructs Weaver to file or create\nthe task or Bead",
            "merely answering a clarifying question is not authorization",
            'a later message saying "File the task now" authorizes intake',
            '"Would\nyou file the task now?" remains investigative because it is a question',
        ):
            self.assertIn(required, skill)

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
