"""Tests for Beads conventions, idempotent intake, and brain synchronization."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fulcrum.beads import (
    BeadDraft,
    BeadsError,
    bead_label_facts,
    commit_and_push_markdown,
    commit_markdown,
    ensure_bead,
    record_push_obligations,
    run_beads,
)
from fulcrum.config import RuntimePaths
from fulcrum.records import ProgressRecord, load_record


def draft(**changes: object) -> BeadDraft:
    values: dict[str, object] = {
        "intake_key": "request-17",
        "title": "Reconcile review handoff",
        "project_id": "fulcrum",
        "problem": "The handoff can be interrupted after bead creation.",
        "outcome": "Retries recover the existing bead.",
        "bounded_scope": "Intake identity and dependencies only.",
        "context": "plans/fulcrum/review.md",
        "dependencies": ("brain-2",),
        "acceptance_criteria": "One bead exists after any retry.",
        "validation": "Exercise a retry after the create response is lost.",
        "activation": "queued",
    }
    values.update(changes)
    return BeadDraft(**values)  # pyre-ignore[6]


class BeadConventionTest(unittest.TestCase):
    def test_standalone_activation_defaults_to_future(self) -> None:
        facts = bead_label_facts(["project:fulcrum"])
        self.assertEqual(facts["activation"], "future")
        self.assertFalse(facts["inherited_activation"])

    def test_plan_bead_inherits_activation(self) -> None:
        facts = bead_label_facts(["project:tollgate", "plan:queue-repair"])
        self.assertEqual(facts["plan_id"], "queue-repair")
        self.assertIsNone(facts["activation"])
        self.assertTrue(facts["inherited_activation"])

    def test_invalid_or_ambiguous_labels_are_rejected(self) -> None:
        invalid = (
            [],
            ["project:a", "project:b"],
            ["project:a", "activation:later"],
            ["project:a", "plan:x", "activation:queued"],
        )
        for labels in invalid:
            with self.subTest(labels=labels), self.assertRaises(BeadsError):
                bead_label_facts(labels)

    def test_template_contains_every_required_section(self) -> None:
        body = draft().description()
        for section in (
            "Problem",
            "Outcome",
            "Bounded scope",
            "Context",
            "Dependencies",
            "Acceptance criteria",
            "Validation",
            "Authorized model overrides",
        ):
            self.assertIn(f"## {section}", body)

    def test_plan_draft_cannot_override_inherited_activation(self) -> None:
        with self.assertRaisesRegex(BeadsError, "inherit activation"):
            draft(plan_id="review", activation="queued")


class IntakeTest(unittest.TestCase):
    @patch(
        "fulcrum.beads.subprocess.run", side_effect=subprocess.TimeoutExpired("bd", 1)
    )
    def test_beads_commands_have_actionable_timeouts(self, _: object) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(BeadsError, "could not run 'bd'.*timed out"):
                run_beads(Path(temporary), ["list"], timeout=1)

    @patch("fulcrum.beads.run_beads")
    def test_retry_reuses_existing_bead_and_repairs_dependency(
        self, run: object
    ) -> None:
        mocked = run
        mocked.side_effect = [  # type: ignore[attr-defined]
            [
                {
                    "id": "brain-7",
                    "external_ref": "fulcrum-intake:request-17",
                    "labels": ["project:fulcrum", "activation:queued"],
                }
            ],
            {},
        ]
        issue_id = ensure_bead(Path("/brain"), draft())
        self.assertEqual(issue_id, "brain-7")
        commands = [call.args[1] for call in mocked.call_args_list]  # type: ignore[attr-defined]
        self.assertFalse(any(command[0] == "create" for command in commands))
        self.assertEqual(commands[1][:4], ["dep", "add", "brain-7", "brain-2"])

    @patch("fulcrum.beads.run_beads")
    def test_new_intake_uses_explicit_batch_commit_policy(self, run: object) -> None:
        mocked = run
        mocked.side_effect = [[], {"id": "brain-8"}, {}]  # type: ignore[attr-defined]
        self.assertEqual(ensure_bead(Path("/brain"), draft()), "brain-8")
        create = mocked.call_args_list[1].args[1]  # type: ignore[attr-defined]
        self.assertIn("--dolt-auto-commit", create)
        self.assertIn("fulcrum-intake:request-17", create)


class BrainGitTest(unittest.TestCase):
    def initialize_repository(self, root: Path) -> None:
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(
            ["git", "-C", str(root), "config", "user.name", "Test"], check=True
        )
        subprocess.run(
            ["git", "-C", str(root), "config", "user.email", "test@example.test"],
            check=True,
        )
        (root / "README.md").write_text("initial\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "initial"], check=True)

    def test_unrelated_staged_content_is_preserved_and_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.initialize_repository(root)
            (root / "unrelated.md").write_text("other\n", encoding="utf-8")
            (root / "plan.md").write_text("plan\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "unrelated.md"], check=True)

            with self.assertRaisesRegex(BeadsError, "already has staged content"):
                commit_markdown(root, [Path("plan.md")], "docs: add plan")

            staged = subprocess.run(
                ["git", "-C", str(root), "diff", "--cached", "--name-only"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            self.assertEqual(staged, ["unrelated.md"])

    def test_failed_push_keeps_local_commit_and_returns_obligation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.initialize_repository(root)
            (root / "NEWS.md").write_text("news\n", encoding="utf-8")

            commit_oid, attempt = commit_and_push_markdown(
                root, [Path("NEWS.md")], "docs: add news"
            )

            self.assertFalse(attempt["ok"])
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(root), "rev-parse", "HEAD"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                commit_oid,
            )
            self.assertTrue((root / "NEWS.md").is_file())


class PushObligationTest(unittest.TestCase):
    def test_failed_push_is_recorded_by_the_progress_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = RuntimePaths(root / "brain", root / "state", root / "config.json")
            progress: ProgressRecord = {
                "record_kind": "progress",
                "schema_version": 1,
                "writer_id": "task-weaver",
                "updated_at": "2026-09-11T20:00:00Z",
                "role_task_id": "task-weaver",
                "role": "weaver",
                "phase": "completed",
                "phase_started_at": "2026-09-11T19:00:00Z",
                "expected_next_actor": None,
                "expected_next_action": "Retry pending pushes during patrol",
                "handoff_needed": False,
                "handoff_sent": True,
                "delivery_error": None,
                "owned_resources": [],
            }
            target = record_push_obligations(
                paths,
                progress,
                [
                    {
                        "source": "git",
                        "command": ["git", "push"],
                        "ok": False,
                        "detail": "network unavailable",
                    },
                    {
                        "source": "beads",
                        "command": ["bd", "dolt", "push"],
                        "ok": True,
                        "detail": "push completed",
                    },
                ],
                updated_at="2026-09-11T20:01:00Z",
            )
            saved = load_record(target)
            self.assertEqual(saved["record_kind"], "progress")
            obligations = saved.get("push_obligations")
            self.assertEqual(len(obligations), 1)  # type: ignore[arg-type]
            self.assertEqual(obligations[0]["source"], "git")  # type: ignore[index]


if __name__ == "__main__":
    unittest.main()
