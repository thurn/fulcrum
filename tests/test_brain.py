from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from fulcrum.brain import BrainRepository


def git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


class BrainRepositoryTest(unittest.TestCase):
    def test_isolated_publication_preserves_remote_and_live_worktree(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = root / "brain.git"
            subprocess.run(
                ["git", "init", "--bare", str(remote)],
                capture_output=True,
                check=True,
            )
            first = root / "first"
            second = root / "second"
            subprocess.run(
                ["git", "clone", str(remote), str(first)],
                capture_output=True,
                check=True,
            )
            for checkout in (first,):
                git(checkout, "config", "user.email", "fulcrum@example.invalid")
                git(checkout, "config", "user.name", "Fulcrum Test")
            (first / "base.txt").write_text("base\n")
            git(first, "add", "base.txt")
            git(first, "commit", "-m", "base")
            branch = git(first, "symbolic-ref", "--short", "HEAD")
            git(first, "push", "origin", branch)
            subprocess.run(
                ["git", "clone", str(remote), str(second)],
                capture_output=True,
                check=True,
            )
            git(second, "config", "user.email", "fulcrum@example.invalid")
            git(second, "config", "user.name", "Fulcrum Test")

            (first / "remote.txt").write_text("remote\n")
            git(first, "add", "remote.txt")
            git(first, "commit", "-m", "remote")
            git(first, "push", "origin", branch)

            (second / "local.txt").write_text("local\n")
            publication = BrainRepository(second).publish(
                [second / "local.txt"], "publish local"
            )
            self.assertFalse(publication.merged_remote)
            self.assertEqual(publication.local_revision, publication.remote_revision)
            self.assertFalse((second / "remote.txt").exists())
            self.assertEqual((second / "local.txt").read_text(), "local\n")
            self.assertEqual(git(second, "status", "--porcelain=v1"), "?? local.txt")
            self.assertEqual(
                git(first, "fetch", "origin", branch),
                "",
            )
            remote_tip = git(first, "rev-parse", f"origin/{branch}")
            self.assertEqual(remote_tip, publication.remote_revision)
            self.assertEqual(git(first, "show", f"{remote_tip}:remote.txt"), "remote")
            self.assertEqual(git(first, "show", f"{remote_tip}:local.txt"), "local")


if __name__ == "__main__":
    unittest.main()
