"""CLI checks for JSON stdout and stderr diagnostics."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from fulcrum.cli import main

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "records"


class StateCliTest(unittest.TestCase):
    def test_write_then_read_uses_json_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "config.json"
            input_path = FIXTURE_ROOT / "healthy-review-wait-progress.json"
            environment = {"FULCRUM_CONFIG": str(config_path)}
            output = io.StringIO()
            with (
                patch.dict("os.environ", environment, clear=True),
                redirect_stdout(output),
            ):
                code = main(
                    [
                        "--state-root",
                        str(root),
                        "state",
                        "write",
                        "--input",
                        str(input_path),
                    ]
                )
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(output.getvalue())["ok"])

            output = io.StringIO()
            with (
                patch.dict("os.environ", environment, clear=True),
                redirect_stdout(output),
            ):
                code = main(
                    [
                        "--state-root",
                        str(root),
                        "state",
                        "read",
                        "--kind",
                        "progress",
                        "--id",
                        "task-executor-3",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["record_kind"], "progress")

    def test_read_failure_is_diagnostic_only_on_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = io.StringIO()
            diagnostic = io.StringIO()
            with redirect_stdout(output), redirect_stderr(diagnostic):
                code = main(
                    [
                        "--state-root",
                        temporary,
                        "state",
                        "read",
                        "--kind",
                        "progress",
                        "--id",
                        "missing",
                    ]
                )
            self.assertEqual(code, 2)
            self.assertEqual(output.getvalue(), "")
            self.assertIn("fulcrum:", diagnostic.getvalue())


if __name__ == "__main__":
    unittest.main()
