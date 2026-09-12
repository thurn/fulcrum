"""Fulcrum skill invocation policy checks."""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any, cast

import yaml

REPO_ROOT = Path(__file__).parents[1]


class SkillPolicyTest(unittest.TestCase):
    def test_all_fulcrum_skills_require_explicit_invocation(self) -> None:
        skill_roots = sorted((REPO_ROOT / "skills").glob("fulcrum-*/SKILL.md"))
        self.assertEqual(len(skill_roots), 8)

        for skill_file in skill_roots:
            config_file = skill_file.parent / "agents" / "openai.yaml"
            with self.subTest(skill=skill_file.parent.name):
                self.assertTrue(config_file.is_file())
                raw_config = yaml.safe_load(config_file.read_text(encoding="utf-8"))
                self.assertIsInstance(raw_config, dict)
                config = cast(dict[str, Any], raw_config)
                raw_policy = config.get("policy")
                self.assertIsInstance(raw_policy, dict)
                policy = cast(dict[str, Any], raw_policy)
                self.assertIs(policy.get("allow_implicit_invocation"), False)


if __name__ == "__main__":
    unittest.main()
