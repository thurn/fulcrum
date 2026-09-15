from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
import subprocess
from contextlib import redirect_stderr
from unittest.mock import AsyncMock, MagicMock, patch

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

    def test_reset_initializes_leadership_like_setup_and_reports_started_turns(self):
        from fulcrum.reset import ResetService

        request = self.request()
        ledger = MagicMock()
        runtime = MagicMock()
        runtime.close = AsyncMock()
        for started in (True, False):
            with self.subTest(initial_requests_started=started):
                actions = [
                    {
                        "role": role,
                        "thread_id": f"{role}-thread",
                        "turn_started": started,
                        "initial_request": "sent" if started else "already_sent",
                    }
                    for role in ("vizier", "marshal")
                ]
                with (
                    patch(
                        "fulcrum.reset._bootstrap_control_record",
                        return_value={"created": started},
                    ),
                    patch.object(ResetService, "_runtime", return_value=runtime),
                    patch(
                        "fulcrum.leadership.ensure_leadership",
                        new_callable=AsyncMock,
                        return_value=actions,
                    ) as ensure,
                ):
                    result = ResetService()._bootstrap_control(
                        request, self.config, ledger
                    )
                ensure.assert_awaited_once_with(
                    request, ledger, runtime, self.config, send_initial_requests=True
                )
                self.assertEqual(result["turns_started"], 2 if started else 0)
                self.assertEqual(result["actions"], actions)
                runtime.close.assert_awaited_once()
                runtime.close.reset_mock()

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

    def test_final_remote_allows_recreated_system_ids_but_rejects_old_records(self):
        from fulcrum.reset import _publish_clean_terminal
        from fulcrum.contracts import FulcrumError

        ledger = MagicMock()
        ledger.list_records.return_value = [
            LedgerRecord.from_native({"id": identifier})
            for identifier in ("fc-system", "fc-leader", "fc-reset")
        ]
        inventory = {
            "remote_ledger": {"ordinary_remote": "remote"},
            "old_ledger_record_ids": ["fc-system", "fc-leader", "fc-old-work"],
        }
        with (
            patch(
                "fulcrum.reset._run",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ),
            patch("fulcrum.reset._remote_refs", return_value={"refs/dolt/data": "new"}),
            patch(
                "fulcrum.reset._fresh_remote_proof",
                return_value={"records": ["fc-system", "fc-leader", "fc-reset"]},
            ) as proof,
        ):
            _publish_clean_terminal(self.request(), ledger, inventory, "fc-reset")
            self.assertEqual(proof.call_args.args[2], ["fc-old-work"])
            proof.return_value = {"records": ["fc-system", "fc-reset", "unexpected"]}
            with self.assertRaisesRegex(FulcrumError, "differs from the clean ledger"):
                _publish_clean_terminal(self.request(), ledger, inventory, "fc-reset")

    def test_resume_after_cleanup_does_not_stop_clean_database(self):
        from fulcrum.reset import ResetService

        operation = OperationRecord.from_record(
            LedgerRecord.from_native(
                {
                    "id": "fc-reset",
                    "metadata": {
                        "fc": {
                            "kind": "operation",
                            "planned": {
                                "targets": [
                                    {"kind": "ledger_root", "state": "completed"},
                                    {"kind": "operational_path", "state": "completed"},
                                ]
                            },
                        }
                    },
                }
            )
        )
        with patch("fulcrum.installation_service._stop_one") as stop:
            result = ResetService()._remove_old_state(
                self.request(), MagicMock(), operation, []
            )
        self.assertIs(result, operation)
        stop.assert_not_called()
