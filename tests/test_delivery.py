"""Disposable Git sources exercise review and completion boundaries."""

import copy
from pathlib import Path
import subprocess
import tempfile
import unittest

from fulcrum.delivery import record_review, authorize_replacement
from fulcrum.eligibility import plan_completion
from test_eligibility import assignment, bead, plan

NOW = "2026-09-11T20:00:00Z"


class DeliveryTests(unittest.TestCase):
    def test_disposable_sources_rejection_escalation_replacement_and_recovery(self):
        with tempfile.TemporaryDirectory(prefix="fulcrum-delivery-") as directory:
            root = Path(directory)

            def git(*args):
                return subprocess.run(
                    ["git", "-C", str(root), *args],
                    check=True,
                    text=True,
                    capture_output=True,
                ).stdout.strip()

            git("init", "-q")
            git("config", "user.name", "Fixture")
            git("config", "user.email", "fixture@example.test")
            sources = []
            for revision in range(4):
                (root / "change.txt").write_text(str(revision))
                git("add", "change.txt")
                git("commit", "-qm", f"fix: revision {revision}")
                sources.append(git("rev-parse", "HEAD"))
            owner = assignment()
            owner["mandate"] = None
            original = copy.deepcopy(owner)
            # Missing evidence leaves the count unchanged.
            self.assertEqual(
                record_review(
                    owner,
                    "c0",
                    sources[0],
                    NOW,
                    accepted=False,
                    notes="need logs",
                    evidence_complete=False,
                ),
                owner,
            )
            rejected = record_review(
                owner,
                "c0",
                sources[0],
                NOW,
                accepted=False,
                notes="Missing error retention",
                evidence_complete=True,
            )
            self.assertEqual(len(rejected["review_history"]), 1)
            # A different candidate ID for the same source does not inflate review count.
            self.assertEqual(
                record_review(
                    rejected,
                    "duplicate",
                    sources[0],
                    NOW,
                    accepted=False,
                    notes="same source",
                    evidence_complete=True,
                ),
                rejected,
            )
            accepted = record_review(
                rejected,
                "c1",
                sources[1],
                NOW,
                accepted=True,
                notes="Meets approved scope",
                evidence_complete=True,
                allowed_replacements=["bounded CI repair"],
            )
            self.assertEqual(accepted["mandate"]["candidate_id"], "c1")
            with self.assertRaises(ValueError):
                authorize_replacement(accepted, "c1", "c2", "new feature", NOW)
            replacement = authorize_replacement(
                accepted, "c1", "c2", "bounded CI repair", NOW
            )
            self.assertEqual(replacement["mandate"]["replaces_candidate_id"], "c1")
            self.assertEqual(len(replacement["review_history"]), 2)
            second = record_review(
                rejected,
                "c1",
                sources[1],
                NOW,
                accepted=False,
                notes="Still broken",
                evidence_complete=True,
            )
            third = record_review(
                second,
                "c2",
                sources[2],
                NOW,
                accepted=False,
                notes="Escalate contract ambiguity",
                evidence_complete=True,
            )
            self.assertEqual(third["review_history"][-1]["outcome"], "escalated")
            self.assertIsNone(third["mandate"])
            with self.assertRaises(ValueError):
                record_review(
                    third,
                    "c3",
                    sources[3],
                    NOW,
                    accepted=True,
                    notes="new approach",
                    evidence_complete=True,
                )
            resumed = record_review(
                third,
                "c3",
                sources[3],
                NOW,
                accepted=True,
                notes="New approach meets contract",
                evidence_complete=True,
                archon_decision="Narrow retry handling; preserve original scope",
            )
            self.assertEqual(len(resumed["review_history"]), 4)
            self.assertEqual(owner, original)
            # Observed Tollgate completion facts, not a locally invented certificate.
            for pushed, cleaned in ((False, False), (True, False), (True, True)):
                issue = bead(
                    status="closed",
                    evidence={
                        "certified_promotion": True,
                        "source_synchronized": pushed,
                        "cleanup_complete": cleaned,
                        "reference": "fixture-tollgate-observation",
                    },
                )
                self.assertEqual(
                    plan_completion(plan(), [issue])["complete"], pushed and cleaned
                )
            self.assertEqual(git("status", "--porcelain"), "")
            self.assertFalse((root / ".git/refs/heads/release").exists())
