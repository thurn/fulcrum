"""Tests for the initial command-line interface."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from fulcrum.cli import main
from fulcrum.version import source_revision, version_text


class CliTest(unittest.TestCase):
    def test_no_arguments_prints_help(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            result = main([])
        self.assertEqual(result, 0)
        self.assertIn("usage: fulcrum", output.getvalue())

    def test_version_prints_package_and_revision(self) -> None:
        output = io.StringIO()
        with patch("fulcrum.version.source_revision", return_value="abc123"):
            with redirect_stdout(output):
                result = main(["version"])
        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), "fulcrum 0.1.0 (revision abc123)\n")

    def test_revision_environment_override(self) -> None:
        with patch.dict("os.environ", {"FULCRUM_BUILD_REVISION": "build-7"}):
            self.assertEqual(source_revision(), "build-7")

    def test_version_without_revision_remains_valid(self) -> None:
        with patch("fulcrum.version.source_revision", return_value=None):
            self.assertEqual(version_text(), "fulcrum 0.1.0")

    def test_missing_git_falls_back_to_package_version(self) -> None:
        with patch("fulcrum.version.subprocess.run", side_effect=FileNotFoundError):
            self.assertIsNone(source_revision())

    def test_brain_status_emits_json(self) -> None:
        output = io.StringIO()
        with (
            patch("fulcrum.cli.brain_status", return_value={"host": "127.0.0.1"}),
            redirect_stdout(output),
        ):
            result = main(
                [
                    "--brain-root",
                    "/brain",
                    "brain",
                    "status",
                    "--expected-remote",
                    "git@example.test:owner/brain.git",
                ]
            )
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue()), {"host": "127.0.0.1"})

    def test_brain_failure_is_diagnostic_only_on_stderr(self) -> None:
        output = io.StringIO()
        diagnostic = io.StringIO()
        with (
            patch("fulcrum.cli.brain_status", side_effect=ValueError("wrong remote")),
            redirect_stdout(output),
            redirect_stderr(diagnostic),
        ):
            result = main(
                [
                    "brain",
                    "status",
                    "--expected-remote",
                    "git@example.test:owner/brain.git",
                ]
            )
        self.assertEqual(result, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("wrong remote", diagnostic.getvalue())


if __name__ == "__main__":
    unittest.main()
