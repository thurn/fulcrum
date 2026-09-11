"""Tests for the initial command-line interface."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
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


if __name__ == "__main__":
    unittest.main()
