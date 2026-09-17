from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fulcrum.application import Application
from fulcrum.cli import COMMANDS, _build_request, _execute, build_parser
from fulcrum.contracts import FulcrumError, ParsedRequest
from fulcrum.instance import WriterLock, resolve_instance
from fulcrum.ledger import operation_id
from tests.support import request


class CliTests(unittest.TestCase):
    def test_every_public_command_has_help_and_application_dispatch(self):
        parser = build_parser()
        self.assertEqual(
            {definition.path for definition in COMMANDS}, set(Application()._handlers)
        )
        for definition in COMMANDS:
            with redirect_stdout(StringIO()) as output, self.assertRaises(SystemExit):
                parser.parse_args([*definition.path, "--help"])
            self.assertIn("usage:", output.getvalue())

    def test_retired_direct_runtime_commands_are_absent(self):
        commands = {definition.path for definition in COMMANDS}
        for retired in {
            ("runtime", "status"),
            ("task", "start"),
            ("plan", "review", "start"),
            ("memory", "set"),
            ("fleet", "replace"),
            ("serve",),
            ("reconcile",),
        }:
            self.assertNotIn(retired, commands)

    def test_common_flags_and_payload_survive_request_wire_roundtrip(self):
        context = request().instance
        parser = build_parser()
        with patch("fulcrum.cli.resolve_instance", return_value=context):
            parsed = _build_request(
                parser.parse_args(
                    ["work", "show", "fc-work", "--timeout", "5", "--json"]
                )
            )
        self.assertEqual(parsed.timeout, 5)
        self.assertEqual(parsed.arguments["id"], "fc-work")
        original = request(input={"summary": "literal '$()'\nquotes ; | &"})
        self.assertEqual(ParsedRequest.from_wire(original.to_wire()), original)

    def test_detached_timeout_retains_operation_locator(self):
        pending = request(("work", "create"))
        with (
            patch("fulcrum.cli.subprocess.Popen") as spawn,
            self.assertRaises(FulcrumError) as raised,
        ):
            spawn.return_value.communicate.side_effect = subprocess.TimeoutExpired(
                "worker", 1
            )
            _execute(pending)
        self.assertEqual(raised.exception.code, "WAIT_TIMEOUT")
        self.assertEqual(
            raised.exception.operation_id, operation_id(pending.request_id)
        )

    def test_writer_lock_is_shared_by_instance_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            brain = root / "brain"
            brain.mkdir()
            config = brain / "fulcrum.yaml"
            config.write_text(f"brain:\n  root: {brain}\n")
            contexts = []
            for name in ["instance", "alias"]:
                instance = root / name
                instance.mkdir()
                (instance / "config").symlink_to(config)
                contexts.append(resolve_instance(instance=str(instance), config=None))
            self.assertEqual(contexts[0].socket_path.name, "broker.sock")
            self.assertEqual(contexts[0].lock_path, contexts[1].lock_path)
            with WriterLock(contexts[0].lock_path), self.assertRaises(FulcrumError):
                WriterLock(contexts[1].lock_path).acquire()
