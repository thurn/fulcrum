from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fulcrum.install import fulcrum2_service_definitions, install_hook_config


class InstallTests(unittest.TestCase):
    def test_stock_services_are_only_dolt_and_broker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "bin" / "fulcrum"
            broker = executable.with_name("fulcrum-broker")
            executable.parent.mkdir()
            executable.touch()
            broker.touch()
            dolt = root / "bin" / "dolt"
            dolt.touch()
            with patch("fulcrum.install.shutil.which", return_value=str(dolt)):
                definitions = fulcrum2_service_definitions(
                    instance_root=root / "instance",
                    config_path=root / "brain" / "fulcrum.yaml",
                    brain_root=root / "brain",
                    config={
                        "beads": {
                            "host": "127.0.0.1",
                            "port": 3309,
                            "database": "fulcrum",
                        }
                    },
                    fulcrum_executable=executable,
                    production=False,
                )
            self.assertEqual(set(definitions), {"dolt", "broker"})
            self.assertIn(
                "fulcrum-broker", definitions["broker"]["ProgramArguments"][0]
            )

    def test_hook_install_preserves_unrelated_handlers_and_owns_six_events(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hooks.json"
            unrelated = {"type": "command", "command": "personal-hook"}
            path.write_text(
                json.dumps({"hooks": {"Stop": [{"hooks": [unrelated]}]}}),
                encoding="utf-8",
            )
            install_hook_config(path, "/tmp/fulcrum hook handle")
            hooks = json.loads(path.read_text(encoding="utf-8"))["hooks"]
            self.assertEqual(
                set(hooks),
                {
                    "SessionStart",
                    "UserPromptSubmit",
                    "PreToolUse",
                    "PostToolUse",
                    "Stop",
                    "Interrupt",
                },
            )
            self.assertIn(unrelated, hooks["Stop"][0]["hooks"])


if __name__ == "__main__":
    unittest.main()
