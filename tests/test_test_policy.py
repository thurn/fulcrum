"""Prevent accidental infrastructure dependencies in newly added tests."""

import os
import subprocess
import sys
import unittest


class TestPolicyTests(unittest.TestCase):
    def test_unexpected_subprocess_fails_before_execution(self):
        with self.assertRaisesRegex(AssertionError, "mock external processes"):
            subprocess.run([sys.executable, "-c", "raise SystemExit(0)"], check=True)

    def test_shell_cannot_bypass_process_guard(self):
        with self.assertRaisesRegex(AssertionError, "mock external processes"):
            os.system("exit 0")
