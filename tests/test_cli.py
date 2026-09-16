from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
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
from fulcrum.runtime_service import RuntimeService
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
                "fulcrum.cli.subprocess.Popen",
            ) as spawn,
            patch(
                "fulcrum.cli.subprocess.Popen.return_value.communicate",
                side_effect=__import__("subprocess").TimeoutExpired("worker", 1),
            ),
            patch(
                "fulcrum.cli.request_sync",
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

    def test_desktop_launch_remains_available_during_maintenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "fulcrum.yaml"
            document = default_config(root)
            document["runtime"]["endpoint"] = "ws://127.0.0.1:9876"
            with config.open("w") as stream:
                ConfigurationManager.yaml().dump(document, stream)
            value = request(("runtime", "launch-desktop"))
            value = replace(
                value,
                instance=replace(
                    value.instance,
                    instance_root=root,
                    config_path=config,
                    brain_root=root,
                    lock_path=root / "controller.lock",
                    socket_path=root / "controller.sock",
                ),
            )
            (root / "source-refresh-request.json").write_text(
                '{"operation_id": "pending-refresh"}'
            )
            with (
                WriterLock(value.instance.lock_path),
                patch("fulcrum.cli.request_sync") as ipc,
                patch("fulcrum.runtime_service._ledger") as ledger,
                patch("fulcrum.runtime_service.Path.is_file", return_value=True),
                patch(
                    "fulcrum.runtime_service.Path.resolve",
                    autospec=True,
                    side_effect=lambda path, **kwargs: path,
                ),
                patch(
                    "fulcrum.runtime_service.subprocess.Popen",
                    return_value=Mock(pid=123),
                ) as launch,
            ):
                result = _execute(value)
            self.assertTrue(result["ok"])
            self.assertIsNone(result["operation_id"])
            self.assertTrue(result["result"]["launched"])
            self.assertEqual(result["result"]["pid"], 123)
            self.assertEqual(result["result"]["attachment"]["state"], "unknown")
            self.assertFalse(result["result"]["separate_runtime_terminated"])
            ipc.assert_not_called()
            ledger.assert_not_called()
            launch.assert_called_once()
            self.assertEqual(
                launch.call_args.args[0],
                ["/Applications/Codex.app/Contents/MacOS/Codex"],
            )
            self.assertEqual(
                launch.call_args.kwargs["env"]["CODEX_APP_SERVER_WS_URL"],
                "ws://127.0.0.1:9876",
            )

    def test_desktop_launch_uses_explicit_absolute_home(self):
        for supplied, expected in [
            ("", "/Users/example/.codex"),
            ("/custom/codex", "/custom/codex"),
        ]:
            with (
                self.subTest(home=supplied),
                patch.dict("os.environ", {"CODEX_HOME": supplied}),
                patch(
                    "fulcrum.runtime_service.Path.home",
                    return_value=Path("/Users/example"),
                ),
                patch("fulcrum.runtime_service.ConfigurationManager") as manager,
                patch("fulcrum.runtime_service.Path.is_file", return_value=False),
                patch(
                    "fulcrum.runtime_service.shutil.which", return_value="/usr/bin/open"
                ),
                patch(
                    "fulcrum.runtime_service.subprocess.Popen",
                    return_value=Mock(pid=123),
                ) as launch,
            ):
                manager.return_value.load.return_value = ({}, b"")
                manager.return_value.effective.return_value = {
                    "runtime": {"endpoint": "ws://127.0.0.1:4500"}
                }
                result = RuntimeService().launch_desktop(request())
                self.assertEqual(launch.call_args.kwargs["env"]["CODEX_HOME"], expected)
                self.assertEqual(launch.call_args.kwargs["cwd"], Path("/Users/example"))
                self.assertEqual(result.result["codex_home"], expected)

    def test_desktop_launch_rejects_relative_home_before_starting(self):
        with (
            patch.dict("os.environ", {"CODEX_HOME": ".codex"}),
            patch("fulcrum.runtime_service.ConfigurationManager") as manager,
            patch("fulcrum.runtime_service.Path.is_file", return_value=False),
            patch("fulcrum.runtime_service.shutil.which", return_value="/usr/bin/open"),
            patch("fulcrum.runtime_service.subprocess.Popen") as launch,
        ):
            manager.return_value.load.return_value = ({}, b"")
            manager.return_value.effective.return_value = {
                "runtime": {"endpoint": "ws://127.0.0.1:4500"}
            }
            with self.assertRaises(FulcrumError) as error:
                RuntimeService().launch_desktop(request())
            self.assertEqual(error.exception.code, "INVALID_CODEX_HOME")
            launch.assert_not_called()

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
