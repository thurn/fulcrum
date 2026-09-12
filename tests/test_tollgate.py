from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from fulcrum.tollgate import Tollgate, TollgateError


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
