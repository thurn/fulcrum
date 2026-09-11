"""Tests for configuration precedence and safe derived paths."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fulcrum.config import ConfigurationError, resolve_paths, safe_child


class ConfigurationTest(unittest.TestCase):
    def test_precedence_is_cli_then_environment_then_user_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "record_kind": "installation",
                        "schema_version": 1,
                        "writer_id": "setup",
                        "updated_at": "2026-09-11T22:15:00Z",
                        "brain_root": str(root / "configured-brain"),
                        "state_root": str(root / "configured-state"),
                        "host_id": "local",
                        "configured_services": [],
                        "observations": {},
                    }
                )
            )
            environment = {
                "FULCRUM_CONFIG": str(config),
                "FULCRUM_BRAIN_ROOT": str(root / "environment-brain"),
            }
            paths = resolve_paths(
                brain_override=root / "cli-brain",
                environ=environment,
                user_home=root,
            )
            self.assertEqual(paths.brain_root, (root / "cli-brain").resolve())
            self.assertEqual(paths.state_root, (root / "configured-state").resolve())

    def test_defaults_use_supplied_home(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            paths = resolve_paths(environ={}, user_home=home)
            self.assertEqual(paths.brain_root, (home / "brain").resolve())
            self.assertEqual(
                paths.state_root,
                (home / "Library" / "Application Support" / "Fulcrum").resolve(),
            )

    def test_relative_configuration_path_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "path must be absolute"):
            resolve_paths(environ={"FULCRUM_CONFIG": "relative.json"})

    def test_safe_child_rejects_traversal_and_separators(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for value in ("..", "../escape", "nested/name"):
                with self.subTest(value=value):
                    with self.assertRaises(ConfigurationError):
                        safe_child(root, value)


if __name__ == "__main__":
    unittest.main()
