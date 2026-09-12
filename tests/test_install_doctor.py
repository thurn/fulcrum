"""Repeatable installation, migration preservation, and doctor diagnostics."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fulcrum.config import RuntimePaths
from fulcrum.doctor import doctor_runtime
from fulcrum.install import InstallationError, install_runtime
from fulcrum.state import atomic_write_record, read_record

REPO_ROOT = Path(__file__).parents[1]
NOW = "2026-09-11T20:00:00Z"


def git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


class InstallDoctorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "retained-source"
        self.source.mkdir()
        shutil.copytree(REPO_ROOT / "skills", self.source / "skills")
        git(self.source, "init", "-q")
        git(self.source, "config", "user.email", "test@example.test")
        git(self.source, "config", "user.name", "Test")
        git(self.source, "add", "skills")
        git(self.source, "commit", "-qm", "test source")
        self.revision = git(self.source, "rev-parse", "HEAD")
        git(self.source, "update-ref", "refs/heads/release", self.revision)
        git(self.source, "update-ref", "refs/remotes/origin/master", self.revision)
        self.paths = RuntimePaths(
            self.root / "brain", self.root / "state", self.root / "config.json"
        )
        self.skills = self.root / "codex-skills"
        self.hooks = self.root / "hooks.json"
        self.command = self.root / "bin" / "fulcrum-hook"
        self.command.parent.mkdir()
        self.command.write_text("#!/bin/sh\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def install(self) -> dict[str, object]:
        return install_runtime(
            paths=self.paths,
            source_root=self.source,
            certified_revision=self.revision,
            skills_root=self.skills,
            hooks_config=self.hooks,
            hook_command=self.command,
            host_id="local",
            expected_brain_remote="git@example.test:brain.git",
            sage_anchor="2026-09-12T08:00:00Z",
            now=NOW,
        )

    def test_install_twice_updates_version_without_touching_active_state(self) -> None:
        self.paths.state_root.mkdir(parents=True)
        assignment_path = self.paths.state_root / "assignments" / "assignment-1.json"
        assignment_path.parent.mkdir()
        assignment_path.write_text('{"active":"opaque"}\n', encoding="utf-8")
        jobs_path = self.paths.state_root / "registry" / "holds-jobs.json"
        jobs_path.parent.mkdir()
        jobs_path.write_text('{"schedule":"opaque"}\n', encoding="utf-8")

        first = self.install()
        first_assignment = assignment_path.read_bytes()
        first_jobs = jobs_path.read_bytes()
        second = self.install()
        self.assertEqual(first["skill_revision"], second["skill_revision"])
        self.assertEqual(first_assignment, assignment_path.read_bytes())
        self.assertEqual(first_jobs, jobs_path.read_bytes())
        installation = read_record(self.paths, "installation")
        self.assertEqual(
            installation["configured_services"],
            ["beads", "codex", "hooks", "tollgate"],
        )
        self.assertEqual(installation["observations"]["package_version"], "0.2.0")
        self.assertEqual(len(json.loads(self.hooks.read_text())["hooks"]["Stop"]), 1)
        self.assertEqual(
            sorted(path.name for path in self.skills.glob("fulcrum-*/SKILL.md")),
            ["SKILL.md"] * 7,
        )
        for skill in self.skills.glob("fulcrum-*/SKILL.md"):
            for relative in re.findall(r"\]\((\.\./[^)]+)\)", skill.read_text()):
                self.assertTrue((skill.parent / relative).resolve().is_file())
        self.assertFalse(second["database_restarted"])

    def test_schema_zero_is_backed_up_and_unknown_schema_is_preserved(self) -> None:
        self.paths.config_file.write_text(
            json.dumps(
                {
                    "record_kind": "installation",
                    "schema_version": 0,
                    "brain_root": str(self.paths.brain_root),
                    "state_root": str(self.paths.state_root),
                    "host_id": "local",
                }
            ),
            encoding="utf-8",
        )
        migrated = self.install()
        backup = Path(str(migrated["migration_backup"]))
        self.assertTrue(backup.is_file())
        self.assertEqual(json.loads(backup.read_text())["schema_version"], 0)
        self.assertEqual(
            json.loads(self.paths.config_file.read_text())["schema_version"], 1
        )

        incompatible = b'{"record_kind":"installation","schema_version":44}\n'
        self.paths.config_file.write_bytes(incompatible)
        with self.assertRaises(InstallationError):
            self.install()
        self.assertEqual(self.paths.config_file.read_bytes(), incompatible)

    def test_disposable_source_is_rejected(self) -> None:
        disposable = self.root / ".worktrees" / "candidate"
        disposable.mkdir(parents=True)
        with self.assertRaises(InstallationError):
            install_runtime(
                paths=self.paths,
                source_root=disposable,
                certified_revision=self.revision,
                skills_root=self.skills,
                hooks_config=self.hooks,
                hook_command=self.command,
                host_id="local",
                expected_brain_remote="remote",
                sage_anchor=NOW,
                now=NOW,
            )

    def test_doctor_separates_required_optional_and_push_findings(self) -> None:
        result = self.install()
        skill_revision = str(result["skill_revision"])
        roles = {
            "record_kind": "role_run_registry",
            "schema_version": 1,
            "writer_id": "task-archon",
            "updated_at": NOW,
            "current_archon_task_id": "task-archon",
            "roles": [
                self.role("archon", "task-archon", skill_revision),
                self.role("night_watchman", "task-watchman", skill_revision),
            ],
        }
        atomic_write_record(self.paths, roles)
        projects = []
        for index in range(3):
            repo = self.root / f"project-{index}"
            (repo / ".git").mkdir(parents=True)
            projects.append(
                {
                    "project_id": f"project-{index}",
                    "repo_path": str(repo),
                    "host_id": "local",
                    "codex_project_id": f"codex-{index}",
                    "tollgate_repo_id": f"tg-{index}",
                    "enabled": True,
                }
            )
        atomic_write_record(
            self.paths,
            {
                "record_kind": "project_registry",
                "schema_version": 1,
                "writer_id": "task-archon",
                "updated_at": NOW,
                "projects": projects,
            },
        )
        config = json.loads(self.paths.config_file.read_text())
        config["observations"].update(
            hook_trust="desktop_verified",
            runtime_observation="available",
            watchman_schedule="ready",
            codex_projects_verified_at=NOW,
        )
        self.paths.config_file.write_text(json.dumps(config), encoding="utf-8")
        with (
            patch("fulcrum.doctor.source_revision", return_value=self.revision),
            patch("fulcrum.doctor._tool_version", return_value="version"),
            patch(
                "fulcrum.doctor._tollgate_project_status",
                side_effect=lambda repository_id: {
                    "path": str(
                        self.root / f"project-{repository_id.removeprefix('tg-')}"
                    ),
                    "execution_state": "active",
                    "block_reasons": [],
                    "remote_enabled": True,
                },
            ),
            patch(
                "fulcrum.doctor.brain_status",
                return_value={"database": "brain", "host": "127.0.0.1"},
            ),
        ):
            report = doctor_runtime(
                paths=self.paths,
                expected_brain_remote="git@example.test:brain.git",
                hooks_config=self.hooks,
                skills_root=self.skills,
            )
        self.assertTrue(report["ready"])
        self.assertEqual(report["required_failures"], [])
        self.assertEqual(report["optional_gaps"], [])
        self.assertEqual(report["push_failures"], [])

    @staticmethod
    def role(role: str, task_id: str, revision: str) -> dict[str, object]:
        return {
            "role": role,
            "project_id": "fleet",
            "host_id": "local",
            "task_id": task_id,
            "identity_state": "resolved",
            "role_number": 1,
            "run_id": f"run-{role}",
            "pair_id": None,
            "title": role,
            "selected_model": "model",
            "selected_reasoning": "high",
            "model_authorization": {"source": "human", "reference": "setup"},
            "skill_revision": revision,
        }


if __name__ == "__main__":
    unittest.main()
