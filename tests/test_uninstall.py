from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from fulcrum.uninstall import (
    MCP_BEGIN,
    MCP_END,
    UninstallPaths,
    remove_fulcrum_hooks,
    remove_fulcrum_toml,
    uninstall,
)


class UninstallTests(unittest.TestCase):
    def test_owned_config_is_removed_without_touching_unrelated_content(self):
        source = Path("/Users/example/fulcrum")
        toml = (
            'model = "gpt-6-astra"\n\n'
            '[projects."/Users/example/fulcrum"]\ntrust_level = "trusted"\n\n'
            f'{MCP_BEGIN}\n[mcp_servers.fulcrum]\ncommand = "owned"\n{MCP_END}\n\n'
            '[mcp_servers.playwright]\nurl = "http://localhost"\n'
        )
        updated = remove_fulcrum_toml(toml, source)
        self.assertNotIn("fulcrum", updated.lower())
        self.assertIn('model = "gpt-6-astra"', updated)
        self.assertIn("[mcp_servers.playwright]", updated)

        personal = {"type": "command", "command": "personal-hook"}
        owned = {
            "type": "command",
            "command": "/Users/example/fulcrum/.venv/bin/fulcrum hook handle",
            "statusMessage": "Fulcrum: recording Stop",
        }
        hooks = {"hooks": {"Stop": [{"hooks": [personal, owned]}]}}
        retained = remove_fulcrum_hooks(
            hooks, source=source, instance=Path("/tmp/instance")
        )
        self.assertEqual(retained, {"hooks": {"Stop": [{"hooks": [personal]}]}})

    def test_preview_then_apply_removes_only_owned_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            source = home / "fulcrum"
            owned_skill = source / "skills" / "fulcrum-bootstrap"
            owned_skill.mkdir(parents=True)
            (source / ".venv").mkdir()
            codex = home / ".codex"
            skills = codex / "skills"
            skills.mkdir(parents=True)
            (skills / "fulcrum-bootstrap").symlink_to(owned_skill)
            foreign = skills / "fulcrum-marshal"
            foreign.mkdir()
            launcher = home / ".local" / "bin" / "fulcrum"
            launcher.parent.mkdir(parents=True)
            launcher.symlink_to(source / ".venv" / "bin" / "fulcrum")
            instance = home / "Library" / "Application Support" / "Fulcrum"
            instance.mkdir(parents=True)
            brain = home / "brain"
            brain.mkdir()
            config = brain / "fulcrum.yaml"
            config.write_text("brain: {}\n", encoding="utf-8")
            (codex / "config.toml").write_text(
                f'{MCP_BEGIN}\n[mcp_servers.fulcrum]\ncommand = "owned"\n{MCP_END}\n'
                '[mcp_servers.personal]\ncommand = "mine"\n',
                encoding="utf-8",
            )
            personal = {"type": "command", "command": "mine"}
            owned = {
                "type": "command",
                "command": f"{source}/.venv/bin/fulcrum hook handle",
                "statusMessage": "Fulcrum: recording Stop",
            }
            (codex / "hooks.json").write_text(
                json.dumps({"hooks": {"Stop": [{"hooks": [personal, owned]}]}}),
                encoding="utf-8",
            )
            paths = UninstallPaths(home, source, instance, config, brain, codex)

            preview = uninstall(
                paths, apply=False, remove_source=False, external_actions=False
            )
            self.assertEqual(preview.errors, [])
            payload = preview.as_dict()
            self.assertNotIn("codex_restart_may_be_required", payload)
            self.assertEqual(
                payload["native_cleanup_checklist"],
                [
                    "archive only Fulcrum role tasks",
                    "delete the Fulcrum Marshal heartbeat",
                ],
            )
            self.assertTrue(instance.exists())
            self.assertTrue(launcher.is_symlink())

            result = uninstall(
                paths, apply=True, remove_source=False, external_actions=False
            )
            self.assertEqual(result.errors, [])
            self.assertTrue(source.is_dir())
            self.assertFalse((source / ".venv").exists())
            self.assertFalse(instance.exists())
            self.assertFalse(brain.exists())
            self.assertFalse(launcher.exists())
            self.assertFalse((skills / "fulcrum-bootstrap").exists())
            self.assertTrue(foreign.is_dir())
            self.assertNotIn("fulcrum", (codex / "config.toml").read_text().lower())
            retained_hooks = json.loads((codex / "hooks.json").read_text())
            self.assertEqual(
                retained_hooks, {"hooks": {"Stop": [{"hooks": [personal]}]}}
            )


if __name__ == "__main__":
    unittest.main()
