from __future__ import annotations

import json
import subprocess
import unittest
from unittest.mock import patch

from fulcrum.tollgate import Tollgate, TollgateError, TollgateUncertainError


class TollgateTests(unittest.TestCase):
    def test_repositories_accepts_tollgate_list_response(self) -> None:
        tollgate = Tollgate("/usr/bin/tg")
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='[{"state":{"id":"repo-1"}}]',
            stderr="",
        )

        with patch("fulcrum.tollgate.subprocess.run", return_value=completed):
            self.assertEqual(tollgate.repositories(), [{"state": {"id": "repo-1"}}])

    def test_object_operations_reject_list_response(self) -> None:
        tollgate = Tollgate("/usr/bin/tg")
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="[]", stderr=""
        )

        with patch("fulcrum.tollgate.subprocess.run", return_value=completed):
            with self.assertRaisesRegex(TollgateError, "non-object for status"):
                tollgate.status("repo-1")

    def test_malformed_output_after_mutation_is_uncertain_with_raw_evidence(
        self,
    ) -> None:
        tollgate = Tollgate("/usr/bin/tg")
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="promoted", stderr="warning"
        )
        with patch("fulcrum.tollgate.subprocess.run", return_value=completed):
            with self.assertRaises(TollgateUncertainError) as raised:
                tollgate.approve("repo-1", "candidate-1")
        self.assertEqual(raised.exception.stdout, "promoted")
        self.assertEqual(raised.exception.stderr, "warning")

    def test_structured_mutation_rejection_is_definitive(self) -> None:
        tollgate = Tollgate("/usr/bin/tg")
        stderr = json.dumps(
            {
                "error": {
                    "code": "unpromoted-source-ancestor",
                    "message": "rebase onto release",
                    "retryable": True,
                },
                "ok": False,
            }
        )
        completed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr=stderr
        )

        with patch("fulcrum.tollgate.subprocess.run", return_value=completed):
            with self.assertRaises(TollgateError) as raised:
                tollgate.submit_candidate("repo-1", "HEAD")

        self.assertNotIsInstance(raised.exception, TollgateUncertainError)
        self.assertEqual(raised.exception.stderr, stderr)

    def test_approve_is_nonblocking_and_returns_authorization_object(self) -> None:
        tollgate = Tollgate("/usr/bin/tg")
        authorization = {
            "item_id": "candidate-1",
            "already_authorized": False,
            "authorized_item_ids": ["candidate-1"],
        }
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(authorization), stderr=""
        )
        with patch("fulcrum.tollgate.subprocess.run", return_value=completed) as run:
            result = tollgate.approve("repo-1", "candidate-1")
        self.assertEqual(result, authorization)
        self.assertEqual(
            run.call_args.args[0],
            [
                "/usr/bin/tg",
                "--json",
                "--no-launch",
                "--repository",
                "repo-1",
                "approve",
                "candidate-1",
            ],
        )

    def test_nonblocking_mutation_rejects_multi_object_output_as_uncertain(
        self,
    ) -> None:
        tollgate = Tollgate("/usr/bin/tg")
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"ok":true}\n{"ok":true}', stderr=""
        )
        with patch("fulcrum.tollgate.subprocess.run", return_value=completed):
            with self.assertRaises(TollgateUncertainError) as raised:
                tollgate.approve("repo-1", "candidate-1")
        self.assertTrue(raised.exception.possible_effect)
        self.assertIn("invalid JSON", str(raised.exception))

    def test_malformed_read_output_is_deterministic(self) -> None:
        tollgate = Tollgate("/usr/bin/tg")
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="not-json", stderr=""
        )
        with patch("fulcrum.tollgate.subprocess.run", return_value=completed):
            with self.assertRaises(TollgateError) as raised:
                tollgate.status("repo-1")
        self.assertNotIsInstance(raised.exception, TollgateUncertainError)
