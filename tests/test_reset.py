from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
import subprocess
from contextlib import redirect_stderr
from unittest.mock import MagicMock, patch

from fulcrum.cli import _emit
from fulcrum.ledger import LedgerFailure, LedgerRecord, OperationRecord
from fulcrum.reset import _initialize_clean_normal_ledger, _operation_result
from fulcrum.configuration import default_config
from fulcrum.contracts import ActorContext, InstanceContext, ParsedRequest


class ResetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.brain = self.root / "brain"
        self.brain.mkdir()
        self.config = default_config(self.brain)

    def request(self):
        return ParsedRequest(
            command=("reset",),
            arguments={"hard": True, "yes": True},
            input={},
            actor=ActorContext(kind="human"),
            instance=InstanceContext(
                instance_root=self.root / "instance",
                config_path=self.brain / "fulcrum.yaml",
                brain_root=self.brain,
                socket_path=self.root / "controller.sock",
                lock_path=self.root / "controller.lock",
                explicit_selection=True,
            ),
            request_id="original-request",
        )

    def test_clean_database_directory_exists_before_server_start(self):
        ledger = MagicMock()
        ledger.run.side_effect = [
            LedgerFailure("missing database", category="missing", retryable=True),
            None,
        ]

        def start(*args, **kwargs):
            self.assertTrue((self.brain / ".beads" / "dolt").is_dir())

        with (
            patch("fulcrum.reset.Ledger", return_value=ledger),
            patch(
                "fulcrum.installation_service.load_installed_services",
                return_value={"dolt": object()},
            ),
            patch(
                "fulcrum.installation_service._start_one", side_effect=start
            ) as started,
            patch("fulcrum.reset._ensure_staging_anchor"),
            patch(
                "fulcrum.reset._run",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ),
        ):
            _initialize_clean_normal_ledger(
                self.request(), self.config, self.root / "reset"
            )
        started.assert_called_once()

    def test_failed_reset_exposes_cause_and_exact_retry_command(self):
        operation = OperationRecord.from_record(
            LedgerRecord.from_native(
                {
                    "id": "fc-reset",
                    "metadata": {
                        "fc": {
                            "kind": "operation",
                            "state": "failed",
                            "request_id": "original-request",
                            "error": {
                                "code": "RESET_INCOMPLETE",
                                "message": "reset incomplete",
                                "retryable": True,
                                "targets": [
                                    {
                                        "kind": "clean_ledger",
                                        "id": "/brain",
                                        "error": {"message": "server failed"},
                                    }
                                ],
                            },
                        }
                    },
                }
            )
        )
        result = _operation_result(operation).to_dict()
        output = io.StringIO()
        with redirect_stderr(output):
            _emit(result, json_output=False)
        self.assertIn("RESET_INCOMPLETE: reset incomplete", output.getvalue())
        self.assertIn("clean_ledger /brain: server failed", output.getvalue())
        self.assertIn("--request-id original-request", output.getvalue())
