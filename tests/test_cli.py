from contextlib import redirect_stdout, redirect_stderr
from io import BytesIO, StringIO, TextIOWrapper
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fulcrum.application import Application
from fulcrum.cli import COMMANDS, _build_request, _execute, build_parser, main
from fulcrum.configuration import ConfigurationManager, default_config
from fulcrum.contracts import CommandResult, FulcrumError, ParsedRequest
from fulcrum.instance import WriterLock, resolve_instance
from fulcrum.ipc import ControllerTimedOut, ControllerUnavailable
from fulcrum.ledger import operation_id
from tests.support import request


class CliTests(unittest.TestCase):
    def test_every_public_command_has_help_and_application_dispatch(self):
        parser = build_parser()
        application = Application()
        self.assertEqual(
            {definition.path for definition in COMMANDS} - {("serve",)},
            set(application._handlers),
        )
        for definition in COMMANDS:
            with (
                self.subTest(command=definition.path),
                redirect_stdout(StringIO()) as out,
                self.assertRaises(SystemExit) as exit,
            ):
                parser.parse_args([*definition.path, "--help"])
            self.assertEqual(exit.exception.code, 0)
            self.assertIn("usage:", out.getvalue())

    def test_retired_commands_and_provider_kinds_are_rejected(self):
        for command in ["fixture", "scenario", "smoke"]:
            with (
                self.subTest(command=command),
                redirect_stdout(StringIO()) as out,
                redirect_stderr(StringIO()),
            ):
                self.assertEqual(main([command, "--json"]), 2)
            self.assertEqual(
                json.loads(out.getvalue())["error"]["code"], "INVALID_COMMAND"
            )
        for section in ["runtime", "delivery"]:
            config = default_config(Path("/unused/brain"))
            config[section]["kind"] = "deterministic"
            with self.subTest(section=section), self.assertRaises(FulcrumError):
                ConfigurationManager(Path("/unused/config")).validate_document(config)

    def test_invalid_input_and_duplicate_fields_have_clean_envelopes(self):
        for payload in ["[1]", '{"description":"duplicate"}']:
            with (
                self.subTest(payload=payload),
                patch("sys.stdin", TextIOWrapper(BytesIO(payload.encode()))),
                redirect_stdout(StringIO()) as out,
                redirect_stderr(StringIO()),
            ):
                self.assertEqual(
                    main(
                        [
                            "enter",
                            "weaver",
                            "--description",
                            "direct",
                            "--input",
                            "-",
                            "--json",
                        ]
                    ),
                    2,
                )
            self.assertFalse(json.loads(out.getvalue())["ok"])
            self.assertIn("error", json.loads(out.getvalue()))

    def test_common_flags_and_payload_survive_request_wire_roundtrip(self):
        context = request().instance
        parser = build_parser()
        for argv in [
            ["--json", "--timeout", "5", "work", "show", "fc-work"],
            ["work", "--json", "show", "fc-work", "--timeout", "5"],
        ]:
            with (
                self.subTest(argv=argv),
                patch("fulcrum.cli.resolve_instance", return_value=context),
            ):
                parsed = _build_request(parser.parse_args(argv))
            self.assertEqual(parsed.timeout, 5)
            self.assertEqual(parsed.arguments["id"], "fc-work")
        original = request(input={"summary": "literal '$()'\nquotes ; | &"})
        self.assertEqual(ParsedRequest.from_wire(original.to_wire()), original)

    def test_controller_timeout_retains_operation_locator(self):
        pending = request(("plan", "review", "start"))
        with (
            patch(
                "fulcrum.cli.request_sync",
                side_effect=ControllerTimedOut("still running"),
            ),
            self.assertRaises(FulcrumError) as error,
        ):
            _execute(pending)
        self.assertEqual(error.exception.code, "WAIT_TIMEOUT")
        self.assertEqual(error.exception.operation_id, operation_id(pending.request_id))
        self.assertEqual(
            error.exception.next_command,
            (
                "fulcrum",
                "operation",
                "show",
                operation_id(pending.request_id),
                "--json",
            ),
        )

    def test_offline_and_served_reads_dispatch_the_same_request(self):
        app = Application()
        seen = []

        def status(value):
            seen.append(value.command)
            return CommandResult.query({"ready": True})

        app.replace_handler(("status",), status)
        value = request(("status",))
        with (
            patch("fulcrum.cli.default_application", return_value=app),
            patch.object(app, "_log"),
            patch(
                "fulcrum.cli.request_sync",
                side_effect=lambda *args, **kwargs: app.dispatch(
                    ParsedRequest.from_wire(args[1])
                ).to_dict(),
            ) as ipc,
        ):
            served = _execute(value)
            ipc.side_effect = ControllerUnavailable("offline")
            offline = _execute(value)
        self.assertEqual(served, offline)
        self.assertEqual(seen, [("status",), ("status",)])

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
            self.assertEqual(contexts[0].lock_path, contexts[1].lock_path)
            with WriterLock(contexts[0].lock_path), self.assertRaises(FulcrumError):
                WriterLock(contexts[1].lock_path).acquire()
