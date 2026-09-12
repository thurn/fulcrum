"""Bounded interviews preserve original archival state through interruption."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from fulcrum.config import RuntimePaths
from fulcrum.interviews import begin_interview, advance_interview, interview_action
from fulcrum.state import atomic_write_record, read_record

NOW = datetime(2026, 9, 11, 20, tzinfo=timezone.utc)


class InterviewTests(unittest.TestCase):
    def test_restoration_and_multiple_subjects_survive_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = RuntimePaths(root / "brain", root / "state", root / "config.json")
            one = begin_interview(
                "sage", "postmortem", "subject1", "local", prior_archived=True, now=NOW
            )
            two = begin_interview(
                "sage", "postmortem", "subject2", "local", prior_archived=False, now=NOW
            )
            first = atomic_write_record(paths, one)
            self.assertNotEqual(first, atomic_write_record(paths, two))
            one = advance_interview(one, "reopening", NOW)
            atomic_write_record(paths, one)
            reloaded = read_record(paths, "interview", one["interview_id"])
            self.assertEqual(interview_action(reloaded, NOW), "inspect_archive")
            one = advance_interview(one, "reopened", NOW, reference="tool success")
            one = advance_interview(one, "reply", NOW, reference="debrief turn")
            self.assertEqual(interview_action(one, NOW), "restore_archive")
            with self.assertRaises(ValueError):
                advance_interview(one, "complete", NOW)
            one = advance_interview(
                one, "restored", NOW, reference="archived tool success"
            )
            one = advance_interview(one, "complete", NOW)
            self.assertEqual(one["outcome"], "responded")
            two = advance_interview(two, "reply", NOW, reference="other debrief")
            self.assertEqual(interview_action(two, NOW), "complete")
            with self.assertRaises(ValueError):
                advance_interview(two, "restored", NOW, reference="must not archive")

    def test_one_later_reminder_then_missing_response(self):
        record = begin_interview(
            "sage", "run", "subject", "local", prior_archived=False, now=NOW
        )
        self.assertEqual(interview_action(record, NOW), "wait")
        later = NOW + timedelta(hours=1)
        self.assertEqual(interview_action(record, later), "remind")
        record = advance_interview(record, "reminder_attempted", later)
        record = advance_interview(record, "reminder_failed", later)
        self.assertEqual(interview_action(record, later), "wait")
        with self.assertRaises(ValueError):
            advance_interview(record, "reminder_attempted", later)
        finished = advance_interview(record, "complete", NOW + timedelta(hours=3))
        self.assertEqual(finished["outcome"], "missing_response")
        # A skipped patrol does not postpone the finish deadline.
        untouched = begin_interview(
            "sage", "other", "subject", "local", prior_archived=False, now=NOW
        )
        self.assertEqual(
            interview_action(untouched, NOW + timedelta(days=1)), "complete"
        )

    def test_uncertain_reopen_keeps_cleanup_obligation_after_deadline(self):
        record = begin_interview(
            "sage", "run", "subject", "local", prior_archived=True, now=NOW
        )
        record = advance_interview(record, "reopening", NOW)
        later = NOW + timedelta(hours=3)
        self.assertEqual(interview_action(record, later), "inspect_archive")
        record = advance_interview(
            record, "restored", later, reference="inspection: still archived"
        )
        self.assertEqual(interview_action(record, later), "complete")

    def test_patrol_reports_retained_obligation_once_then_resolution(self):
        from fulcrum.watchman import patrol
        from test_watchman import registries, jobs

        roles, projects = registries()
        record = begin_interview(
            "sage", "run", "subject", "local", prior_archived=False, now=NOW
        )
        later = NOW + timedelta(hours=3)
        args = dict(
            role_registry=roles,
            project_registry=projects,
            progress_records=[],
            task_observations={},
            candidate_observations=[],
            executor_evidence=[],
            jobs_record=jobs(),
            tollgate_observation_available=True,
            now=later,
        )
        first = patrol(**args, interviews=[record], previous_conditions=[])
        self.assertTrue(
            any(
                item["condition"]["code"] == "interview_obligation"
                for item in first["notifications"]
            )
        )
        repeated = patrol(
            **args, interviews=[record], previous_conditions=first["current_conditions"]
        )
        self.assertFalse(repeated["notifications"])
        finished = advance_interview(record, "complete", later)
        result = patrol(
            **args,
            interviews=[finished],
            previous_conditions=first["current_conditions"],
        )
        self.assertTrue(
            any(
                item["change"] == "resolved"
                and item["condition"]["code"] == "interview_obligation"
                for item in result["notifications"]
            )
        )
