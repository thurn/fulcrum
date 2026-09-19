import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from fulcrum.install import HUMAN_SKILLS
from tests import local_python

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "reconcile_skills"


class ReconcileSkillsScriptTests(unittest.TestCase):
    def run_script(self, home: Path) -> subprocess.CompletedProcess[str]:
        code = "import runpy,sys; sys.argv=[sys.argv[1]]; runpy.run_path(sys.argv[0], run_name='__main__')"
        command = [sys.executable, "-B", "-c", code, str(SCRIPT)]
        with local_python(command):
            return subprocess.run(
                command,
                cwd=ROOT,
                env={**os.environ, "HOME": str(home)},
                text=True,
                capture_output=True,
                timeout=10,
            )

    def test_repairs_rename_is_idempotent_and_preserves_unrelated_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            skills = home / ".codex" / "skills"
            skills.mkdir(parents=True)
            hooks = home / ".codex" / "hooks.json"
            hooks.write_text('{"hooks":{"UserPromptSubmit":[]}}\n', encoding="utf-8")
            hooks_before = hooks.read_bytes()
            unrelated = skills / "personal-skill"
            unrelated.mkdir()
            (unrelated / "notes.txt").write_text("mine", encoding="utf-8")
            stale = skills / "executor"
            stale.symlink_to(home / "missing", target_is_directory=True)
            obsolete = []
            for name in HUMAN_SKILLS:
                legacy = skills / f"fulcrum-{name}"
                legacy.symlink_to(ROOT / "skills" / name, target_is_directory=True)
                obsolete.append(legacy)

            first = self.run_script(home)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn(f"linked {stale}", first.stdout)
            for legacy in obsolete:
                self.assertIn(f"removed {legacy}", first.stdout)
                self.assertFalse(legacy.exists())
            self.assertTrue(unrelated.is_dir())
            self.assertEqual((unrelated / "notes.txt").read_text(), "mine")
            self.assertEqual(hooks.read_bytes(), hooks_before)
            for name in HUMAN_SKILLS:
                target = skills / name
                self.assertTrue(target.is_symlink(), name)
                self.assertEqual(target.readlink(), ROOT / "skills" / name)

            second = self.run_script(home)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertNotIn("linked ", second.stdout)
            self.assertEqual(second.stdout.count("unchanged "), len(HUMAN_SKILLS))

    def test_refuses_unsafe_real_directory_with_status_two(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            conflict = home / ".codex" / "skills" / "weaver"
            conflict.mkdir(parents=True)

            result = self.run_script(home)

            self.assertEqual(result.returncode, 2)
            self.assertIn(
                "refusing to replace real user skill directory", result.stderr
            )
            self.assertTrue(conflict.is_dir())


if __name__ == "__main__":
    unittest.main()
