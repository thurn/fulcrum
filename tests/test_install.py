from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fulcrum.install import (
    install_hook_config,
    reconcile_skills,
    service_definitions,
)


class InstallTests(unittest.TestCase):
    def test_owned_services_are_only_dolt_and_broker(self):
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
                definitions = service_definitions(
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
            owned = {event: groups[-1]["hooks"][0] for event, groups in hooks.items()}
            self.assertEqual(owned["SessionStart"]["additionalContextLimit"], 2000)
            for event in {
                "UserPromptSubmit",
                "PreToolUse",
                "PostToolUse",
                "Stop",
                "Interrupt",
            }:
                self.assertNotIn("additionalContextLimit", owned[event])

    def test_skill_storage_symlink_does_not_redirect_codex_hooks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex = root / ".codex"
            shared = root / ".llms" / "skills"
            shared.mkdir(parents=True)
            codex.mkdir()
            (codex / "skills").symlink_to(shared, target_is_directory=True)

            result = reconcile_skills(
                root / "instance",
                production=False,
                config_path=root / "brain" / "fulcrum.yaml",
                skills_root=codex / "skills",
                source_root=Path(__file__).parents[1],
            )

            self.assertTrue((codex / "hooks.json").is_file())
            self.assertFalse((shared.parent / "hooks.json").exists())
            self.assertEqual(
                result["hook"]["required_events"],
                [
                    "SessionStart",
                    "UserPromptSubmit",
                    "PreToolUse",
                    "PostToolUse",
                    "Stop",
                ],
            )
            self.assertEqual(result["hook"]["optional_events"], ["Interrupt"])
            self.assertEqual(
                result["hook"]["operational_state"], "confirmation_required"
            )


if __name__ == "__main__":
    unittest.main()
