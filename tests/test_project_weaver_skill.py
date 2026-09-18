from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ProjectWeaverSkillTests(unittest.TestCase):
    def test_project_copy_matches_canonical_skill(self):
        project = ROOT / ".agents" / "skills" / "weaver"
        canonical = ROOT / "skills" / "weaver"

        self.assertEqual(
            (project / "SKILL.md").read_bytes(),
            (canonical / "SKILL.md").read_bytes(),
        )
        self.assertEqual(
            (project / "agents" / "openai.yaml").read_bytes(),
            (canonical / "agents" / "openai.yaml").read_bytes(),
        )

    def test_weaver_is_visible_to_fresh_project_tasks(self):
        manifest = (
            ROOT / ".agents" / "skills" / "weaver" / "agents" / "openai.yaml"
        ).read_text(encoding="utf-8")

        self.assertNotIn("allow_implicit_invocation: false", manifest)

    def test_weaver_finish_fields_have_explicit_list_shapes(self):
        skill = (ROOT / ".agents" / "skills" / "weaver" / "SKILL.md").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "`acceptance`, `evidence`, and `implementation_notes`\n"
            "as top-level lists of strings",
            skill,
        )


if __name__ == "__main__":
    unittest.main()
