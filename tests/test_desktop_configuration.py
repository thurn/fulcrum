from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fulcrum.cli import _build_request, _execute, build_parser
from fulcrum.configuration import ConfigurationManager
from fulcrum.contracts import FulcrumError


class DesktopConfigurationTests(unittest.TestCase):
    def test_launch_with_invalid_workflow_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config"
            config.write_text(
                "runtime:\n  kind: codex\n  endpoint: ws://127.0.0.1:4500\n"
                "unrecognized_workflow_field: true\n"
            )
            with self.assertRaises(FulcrumError) as error:
                ConfigurationManager(config).load()
            self.assertEqual(error.exception.code, "UNKNOWN_FIELDS")
            value = _build_request(
                build_parser().parse_args(
                    [
                        "runtime",
                        "launch-desktop",
                        "--instance",
                        str(root),
                    ]
                )
            )
            with (
                patch("fulcrum.runtime_service.Path.is_file", return_value=False),
                patch(
                    "fulcrum.runtime_service.shutil.which", return_value="/usr/bin/open"
                ),
                patch(
                    "fulcrum.runtime_service.subprocess.Popen",
                    return_value=Mock(pid=123),
                ) as launch,
            ):
                result = _execute(value)
            self.assertTrue(result["ok"])
            self.assertEqual(result["result"]["endpoint"], "ws://127.0.0.1:4500")
            launch.assert_called_once()

    def test_invalid_runtime_and_yaml_are_still_rejected(self):
        for content in [
            "runtime: []\n",
            "runtime:\n  kind: other\n",
            "runtime:\n  endpoint: ''\n",
            "runtime:\n  endpoint: 123\n",
            "runtime:\n  endpoint: ws://localhost:4500\n  typo: true\n",
            "runtime: [\n",
            "runtime: {}\nruntime: {}\n",
        ]:
            with (
                self.subTest(content=content),
                tempfile.TemporaryDirectory() as directory,
            ):
                config = Path(directory) / "config"
                config.write_text(content)
                with self.assertRaises(FulcrumError):
                    ConfigurationManager(config).desktop_endpoint()
