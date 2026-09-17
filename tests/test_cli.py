from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fulcrum.application import Application
from fulcrum.cli import COMMANDS, _build_request, _execute, build_parser
from fulcrum.configuration import ConfigurationManager
from fulcrum.contracts import CommandResult, FulcrumError, ParsedRequest
from fulcrum.instance import WriterLock, resolve_instance
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

    def test_bootstrap_defaults_to_human_inside_a_codex_task(self):
        context = request().instance
        parser = build_parser()
        with (
            patch("fulcrum.cli.resolve_instance", return_value=context),
            patch.dict("os.environ", {"CODEX_THREAD_ID": "native-task"}),
        ):
            parsed = _build_request(parser.parse_args(["bootstrap"]))
        self.assertEqual(parsed.actor.kind, "human")
        self.assertEqual(parsed.thread_id, "native-task")

    def test_bootstrap_initializes_missing_authoritative_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            brain = root / "brain"
            config = brain / "fulcrum.yaml"
            instance = root / "instance"
            checkout = root / "checkout"
            payload = root / "bootstrap.json"
            payload.write_text(
                json.dumps(
                    {
                        "configuration": {
                            "policy": {"automatic_capacity": 2},
                        }
                    }
                )
            )
            parser = build_parser()
            with patch("fulcrum.install.master_source_root", return_value=checkout):
                parsed = _build_request(
                    parser.parse_args(
                        [
                            "bootstrap",
                            "--instance",
                            str(instance),
                            "--config",
                            str(config),
                            "--input",
                            str(payload),
                        ]
                    )
                )

            document, _ = ConfigurationManager(config).load()
            effective = ConfigurationManager(config).effective(document)
            self.assertEqual(parsed.instance.brain_root, brain.resolve())
            self.assertEqual(effective["policy"]["automatic_capacity"], 2)
            self.assertEqual(effective["source"]["repository"], str(checkout.resolve()))
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)

    def test_each_cli_invocation_dispatches_in_its_fresh_operation_process(self):
        pending = request(("work", "create"))
        with patch("fulcrum.cli.default_application") as application:
            application.return_value.dispatch.return_value = CommandResult.query(
                {"accepted": True}
            )
            result = _execute(pending)
        application.return_value.dispatch.assert_called_once_with(pending)
        self.assertEqual(result["result"], {"accepted": True})

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
