"""Tests for thin Beads brain setup and diagnostics."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fulcrum.brain import BrainError, brain_status, initialize_brain

GIT_REMOTE = "git@github.com:example/private-brain.git"
DOLT_REMOTE = "git+ssh://git@github.com/example/private-brain.git"


def completed(command: list[str], stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")


def response(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
    joined = " ".join(command)
    if "git -C" in joined:
        return completed(command, GIT_REMOTE + "\n")
    if " where " in f" {joined} ":
        return completed(
            command,
            json.dumps({"path": "/brain/.beads", "database_path": "/brain/db"}),
        )
    if "remote list" in joined:
        return completed(
            command,
            json.dumps([{"name": "origin", "url": DOLT_REMOTE}]),
        )
    if "dolt status" in joined:
        return completed(command, json.dumps({"running": True, "pid": 7}))
    if "dolt test" in joined:
        return completed(command, json.dumps({"connection_ok": True}))
    if command[:2] == ["bd", "init"]:
        return completed(command, "")
    raise AssertionError(f"unexpected command: {command}")


class BrainTest(unittest.TestCase):
    def make_brain(self, root: Path, *, initialized: bool = True) -> Path:
        brain = root / "Private_Brain"
        (brain / ".git").mkdir(parents=True)
        if initialized:
            beads = brain / ".beads"
            beads.mkdir()
            (beads / "metadata.json").write_text(
                json.dumps(
                    {
                        "backend": "dolt",
                        "dolt_mode": "server",
                        "dolt_server_host": "127.0.0.1",
                        "dolt_database": "private-brain",
                    }
                ),
                encoding="utf-8",
            )
        return brain

    def test_status_uses_only_scoped_beads_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            brain = self.make_brain(Path(temporary))
            with patch("fulcrum.brain.subprocess.run", side_effect=response) as run:
                status = brain_status(brain, GIT_REMOTE)

        self.assertEqual(status["host"], "127.0.0.1")
        self.assertEqual(status["server"]["pid"], 7)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len(commands), 5)
        for command in commands[1:]:
            self.assertEqual(command[:3], ["bd", "--directory", str(brain.resolve())])
        self.assertNotIn("start", [part for command in commands for part in command])
        self.assertNotIn("stop", [part for command in commands for part in command])

    def test_initialization_is_idempotent_when_store_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            brain = self.make_brain(Path(temporary))
            with patch("fulcrum.brain.subprocess.run", side_effect=response) as run:
                initialize_brain(brain, GIT_REMOTE)

        commands = [call.args[0] for call in run.call_args_list]
        self.assertFalse(any(command[:2] == ["bd", "init"] for command in commands))

    def test_initialization_of_missing_store_uses_server_mode_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            brain = self.make_brain(Path(temporary), initialized=False)

            def initializing_response(
                command: list[str], **kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                if command[:2] == ["bd", "init"]:
                    beads = brain / ".beads"
                    beads.mkdir()
                    (beads / "metadata.json").write_text(
                        json.dumps(
                            {
                                "backend": "dolt",
                                "dolt_mode": "server",
                                "dolt_server_host": "127.0.0.1",
                                "dolt_database": "private-brain",
                            }
                        ),
                        encoding="utf-8",
                    )
                return response(command)

            with patch(
                "fulcrum.brain.subprocess.run", side_effect=initializing_response
            ) as run:
                initialize_brain(brain, GIT_REMOTE)

        init_calls = [
            call for call in run.call_args_list if call.args[0][:2] == ["bd", "init"]
        ]
        self.assertEqual(len(init_calls), 1)
        command = init_calls[0].args[0]
        self.assertIn("--server", command)
        self.assertIn("127.0.0.1", command)
        self.assertIn("--init-if-missing", command)
        self.assertEqual(init_calls[0].kwargs["cwd"], brain.resolve())

    def test_wrong_git_remote_refuses_before_beads_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            brain = self.make_brain(Path(temporary))

            def wrong_remote(command: list[str], **_: object) -> object:
                return completed(command, "git@github.com:example/wrong.git\n")

            with patch("fulcrum.brain.subprocess.run", side_effect=wrong_remote) as run:
                with self.assertRaisesRegex(BrainError, "expected.*private remote"):
                    initialize_brain(brain, GIT_REMOTE)
        self.assertEqual(run.call_count, 1)

    def test_non_loopback_server_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            brain = self.make_brain(Path(temporary))
            metadata = brain / ".beads" / "metadata.json"
            value = json.loads(metadata.read_text(encoding="utf-8"))
            value["dolt_server_host"] = "0.0.0.0"
            metadata.write_text(json.dumps(value), encoding="utf-8")
            with patch("fulcrum.brain.subprocess.run", side_effect=response):
                with self.assertRaisesRegex(BrainError, "loopback"):
                    brain_status(brain, GIT_REMOTE)


if __name__ == "__main__":
    unittest.main()
