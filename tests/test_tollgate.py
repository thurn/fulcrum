from __future__ import annotations

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

    def test_malformed_read_output_is_deterministic(self) -> None:
        tollgate = Tollgate("/usr/bin/tg")
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="not-json", stderr=""
        )
        with patch("fulcrum.tollgate.subprocess.run", return_value=completed):
            with self.assertRaises(TollgateError) as raised:
                tollgate.status("repo-1")
        self.assertNotIsInstance(raised.exception, TollgateUncertainError)
