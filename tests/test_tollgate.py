from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from fulcrum.tollgate import Tollgate, TollgateError, TollgateUncertainError


class TollgateTests(unittest.TestCase):
    fixtures = Path(__file__).parent / "fixtures" / "tollgate"

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

    def test_approve_accepts_retained_json_lines_streams(self) -> None:
        tollgate = Tollgate("/usr/bin/tg")
        for operation_id in (13, 28, 38, 64):
            with self.subTest(operation_id=operation_id):
                stdout = (
                    self.fixtures / f"approve-operation-{operation_id}.jsonl"
                ).read_text(encoding="utf-8")
                documents = [json.loads(line) for line in stdout.splitlines()]
                candidate_id = documents[0]["item_id"]
                repository_id = documents[1]["item"]["repository_id"]
                completed = subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=stdout, stderr=""
                )

                with patch(
                    "fulcrum.tollgate.subprocess.run", return_value=completed
                ) as run:
                    result = tollgate.approve(repository_id, candidate_id)

                self.assertEqual(result["authorization"], documents[0])
                self.assertEqual(result["wait_statuses"], documents[1:])
                self.assertEqual(
                    result["wait_statuses"][-1]["item"]["state"], "promoted"
                )
                self.assertEqual(
                    run.call_args.args[0],
                    [
                        "/usr/bin/tg",
                        "--json",
                        "--no-launch",
                        "--repository",
                        repository_id,
                        "approve",
                        candidate_id,
                        "--wait",
                    ],
                )

    def test_approve_rejects_ambiguous_candidate_stream_with_raw_evidence(
        self,
    ) -> None:
        tollgate = Tollgate("/usr/bin/tg")
        stdout = "\n".join(
            [
                json.dumps(
                    {
                        "item_id": "candidate-1",
                        "already_authorized": False,
                        "authorized_item_ids": ["candidate-1"],
                    }
                ),
                json.dumps(
                    {
                        "item": {
                            "id": "candidate-2",
                            "repository_id": "repo-1",
                            "state": "promoted",
                        }
                    }
                ),
            ]
        )
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=stdout, stderr=""
        )

        with patch("fulcrum.tollgate.subprocess.run", return_value=completed):
            with self.assertRaises(TollgateUncertainError) as raised:
                tollgate.approve("repo-1", "candidate-1")

        self.assertEqual(raised.exception.stdout, stdout)
        self.assertIn("ambiguous JSON", str(raised.exception))

    def test_approve_accepts_idempotent_authorization_with_empty_candidate_set(
        self,
    ) -> None:
        tollgate = Tollgate("/usr/bin/tg")
        retained = (self.fixtures / "approve-operation-13.jsonl").read_text(
            encoding="utf-8"
        )
        documents = [json.loads(line) for line in retained.splitlines()]
        documents[0]["already_authorized"] = True
        documents[0]["authorized_item_ids"] = []
        stdout = "\n".join(json.dumps(document) for document in documents)
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=stdout, stderr=""
        )

        with patch("fulcrum.tollgate.subprocess.run", return_value=completed):
            result = tollgate.approve(
                documents[1]["item"]["repository_id"], documents[0]["item_id"]
            )

        self.assertTrue(result["authorization"]["already_authorized"])
        self.assertEqual(result["authorization"]["authorized_item_ids"], [])

    def test_malformed_read_output_is_deterministic(self) -> None:
        tollgate = Tollgate("/usr/bin/tg")
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="not-json", stderr=""
        )
        with patch("fulcrum.tollgate.subprocess.run", return_value=completed):
            with self.assertRaises(TollgateError) as raised:
                tollgate.status("repo-1")
        self.assertNotIsInstance(raised.exception, TollgateUncertainError)
