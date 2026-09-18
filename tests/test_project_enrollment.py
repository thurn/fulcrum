from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fulcrum.configuration import _enroll_project_beads


class ProjectEnrollmentTests(unittest.TestCase):
    def test_beads_initialization_uses_stealth_git_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git" / "info").mkdir(parents=True)
            calls: list[list[str]] = []

            def completed(command, **kwargs):
                calls.append(list(command))
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("fulcrum.configuration.subprocess.run", side_effect=completed):
                _enroll_project_beads(
                    "toy",
                    {"root": str(root)},
                    {
                        "beads": {
                            "host": "127.0.0.1",
                            "port": 3307,
                            "database": "fulcrum",
                        }
                    },
                    "bd",
                )

        init = calls[0]
        self.assertEqual(init[:2], ["bd", "init"])
        self.assertIn("--stealth", init)
        self.assertIn("--skip-agents", init)
        self.assertIn("--skip-hooks", init)
