from __future__ import annotations

import json
import plistlib
import tempfile
import unittest
from pathlib import Path

from fulcrum.config import (
    InstallationConfig,
    ProjectConfig,
    load_installation,
    resolve_paths,
    save_installation,
)
from fulcrum.install import CONTROLLER_LABEL, install_hook_config, service_definitions
from fulcrum.setup import _dispatch_is_ready


class ConfigInstallTest(unittest.TestCase):
    def test_setup_waits_for_controller_dispatch_readiness(self) -> None:
        self.assertTrue(
            _dispatch_is_ready({"data": {"dispatch_enabled": {"value": "1"}}})
        )
        self.assertFalse(
            _dispatch_is_ready({"data": {"dispatch_enabled": {"value": "0"}}})
        )

    def test_round_trip_has_no_format_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.json"
            config = InstallationConfig(
                source_root="/source",
                brain_root="/brain",
                state_root="/state",
                codex_bin="/bin/codex",
                desktop_executable="/Applications/ChatGPT.app/ChatGPT",
                projects=[ProjectConfig("p", "/repo")],
            )
            save_installation(path, config)
            raw = json.loads(path.read_text())
            self.assertNotIn("version", raw)
            self.assertNotIn("schema_version", raw)
            self.assertEqual(load_installation(path, home=root), config)

    def test_paths_keep_control_outside_reset_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = resolve_paths(environ={}, user_home=home)
            self.assertEqual(paths.database.name, "fulcrum.sqlite3")
            self.assertEqual(paths.control_root.name, "control")
            self.assertNotEqual(paths.database.parent, paths.control_root)

    def test_hook_merge_removes_all_old_fulcrum_handlers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hooks.json"
            path.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "Stop": [
                                {
                                    "hooks": [
                                        {
                                            "statusMessage": "Fulcrum: old stop",
                                            "command": "old",
                                        },
                                        {
                                            "statusMessage": "Unrelated",
                                            "command": "keep",
                                        },
                                    ]
                                }
                            ]
                        }
                    }
                )
            )
            install_hook_config(path, Path("/linked/hook"))
            result = json.loads(path.read_text())
            self.assertEqual(result["hooks"]["Stop"][0]["hooks"][0]["command"], "keep")
            self.assertEqual(len(result["hooks"]["SessionStart"]), 1)
            self.assertNotIn("PreToolUse", result["hooks"])

    def test_services_are_separate_and_controller_does_not_spawn_server(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = InstallationConfig(
                source_root=str(root),
                brain_root=str(root / "brain"),
                state_root=str(root / "state"),
                codex_bin="/bin/codex",
                desktop_executable="/Applications/ChatGPT.app/ChatGPT",
            )
            paths = resolve_paths(
                state_override=root / "state",
                environ={"FULCRUM_CONFIG": str(root / "config.json")},
                user_home=root,
            )
            definitions = service_definitions(config, paths)
            controller = definitions[CONTROLLER_LABEL]["ProgramArguments"]
            self.assertIn("fulcrum.cli", controller)
            self.assertNotIn("app-server", controller)


if __name__ == "__main__":
    unittest.main()
