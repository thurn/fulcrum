"""Disposable repository exercise for the documented architectural finding."""

import os
from pathlib import Path
import runpy
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fulcrum.beads import BeadsError
from fulcrum.findings import Finding, publish_finding


class InquisitorExercise(unittest.TestCase):
    def test_old_boundary_finding_preserves_scope_despite_recent_docs_commit(self):
        fixture = Path(__file__).parent / "fixtures/inquisitor/shop"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "shop"
            shutil.copytree(fixture, root)
            outside = Path(directory) / "other-project.txt"
            outside.write_text("not this review's project")

            def git(*args, date="2026-01-01T12:00:00Z"):
                return subprocess.run(
                    ["git", "-C", str(root), *args],
                    env=dict(os.environ, GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date),
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()

            git("init", "-q")
            git("config", "user.name", "Fixture")
            git("config", "user.email", "fixture@example.test")
            git("add", ".")
            git("commit", "-qm", "feat: original order interfaces")
            old = git("rev-parse", "HEAD")
            (root / "README.md").write_text(
                (root / "README.md").read_text() + "\nRecent punctuation fix.\n"
            )
            git("add", "README.md")
            git("commit", "-qm", "docs: punctuation", date="2026-09-11T12:00:00Z")
            self.assertEqual(git("diff", "--name-only", "HEAD~1", "HEAD"), "README.md")
            inventory = git("ls-files").splitlines()
            before = {name: (root / name).read_bytes() for name in inventory}
            quote = runpy.run_path(str(root / "checkout.py"))["quote"]
            render = runpy.run_path(str(root / "invoices.py"))["render"]
            for raw in (
                {"quantity": "2", "unit_cents": "125"},
                {"quantity": 1, "unit_cents": 0},
            ):
                self.assertEqual(
                    render(raw), f"Total: {quote(raw)['total_cents']} cents"
                )
            for raw, error in (
                ({"quantity": 0, "unit_cents": 1}, ValueError),
                ({"quantity": 1, "unit_cents": -1}, ValueError),
                ({"quantity": "bad", "unit_cents": 1}, ValueError),
                ({}, KeyError),
            ):
                for interface in (quote, render):
                    with self.assertRaises(error):
                        interface(raw)
            finding = Finding(
                "shop",
                "duplicated-order-normalization",
                "Centralize the order domain boundary",
                f"{old}: checkout.quote and invoices.render duplicate parsing, validation, and totals",
                "Fewer coordinated edits; no measured speedup",
                "One normalized order value preserving both public adapters",
                "Accepted inputs and output shapes unchanged; invalid inputs retain exceptions",
                "checkout.quote, invoices.render, service.preview",
                "Compare quote/invoice contracts for valid, zero-price, malformed, and missing inputs",
            )
            self.assertIn(old, finding.draft().description())
            self.assertIn("activation:future", finding.draft().labels)
            with patch(
                "fulcrum.findings.run_beads",
                return_value=[
                    {"id": "outside", "labels": ["project:other"], "status": "open"}
                ],
            ) as run:
                with self.assertRaises(BeadsError):
                    publish_finding(
                        Path(directory), finding, matching_issue_id="outside"
                    )
                self.assertEqual(run.call_count, 1)
            self.assertEqual(
                before, {name: (root / name).read_bytes() for name in inventory}
            )
            self.assertEqual(outside.read_text(), "not this review's project")
            self.assertEqual(git("status", "--porcelain"), "")
