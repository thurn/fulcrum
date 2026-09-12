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

    def test_weaver_is_ephemeral_and_direct_intake_is_queued(self) -> None:
        weaver = (REPO_ROOT / "skills/fulcrum-weaver/SKILL.md").read_text(
            encoding="utf-8"
        )
        identity = (REPO_ROOT / "skills/fulcrum-shared/identity.md").read_text(
            encoding="utf-8"
        )
        handoffs = (REPO_ROOT / "skills/fulcrum-shared/handoffs.md").read_text(
            encoding="utf-8"
        )
        weaver = " ".join(weaver.split())
        identity = " ".join(identity.split())
        handoffs = " ".join(handoffs.split())

        self.assertIn("short-lived, unregistered authoring actor", weaver)
        self.assertIn(
            "do not write a role registration or Weaver progress record", identity
        )
        self.assertIn(
            "do not create progress, assignment, or delivery-retry state", handoffs
        )

        self.assertIn(
            "standalone tasks and task lists default to `activation:queued`", weaver
        )
        self.assertIn("activation:future` only when the human explicitly asks", weaver)
        self.assertIn("at most one completion report", weaver)
        self.assertIn("Do not wait, poll, require acknowledgement, or retry", weaver)
        self.assertNotIn("future-by-default", weaver)
        self.assertNotIn("register a run", weaver)

    def test_registered_state_schema_keeps_retained_weaver_records_readable(
        self,
    ) -> None:
        schema = (REPO_ROOT / "schemas/records-v1.schema.json").read_text(
            encoding="utf-8"
        )
        self.assertIn('"sage", "weaver"', schema)


if __name__ == "__main__":
    unittest.main()
