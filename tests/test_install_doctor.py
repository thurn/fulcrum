"""Repository-linked installation and doctor diagnostics."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fulcrum.config import RuntimePaths
from fulcrum.doctor import doctor_runtime
from fulcrum.install import LINKED_SKILLS, InstallationError, install_runtime
from fulcrum.roles import initialize_progress
from fulcrum.state import atomic_write_record, read_record, selected_record_path

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
        shutil.copytree(REPO_ROOT / "hooks", self.source / "hooks")
        package = self.source / "src" / "fulcrum"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        git(self.source, "init", "-q")
        git(self.source, "config", "user.email", "test@example.test")
        git(self.source, "config", "user.name", "Test")
        git(self.source, "add", "skills", "hooks", "src")
        git(self.source, "commit", "-qm", "test source")
        self.revision = git(self.source, "rev-parse", "HEAD")
        git(self.source, "update-ref", "refs/heads/release", self.revision)
        git(self.source, "update-ref", "refs/remotes/origin/master", self.revision)
        self.paths = RuntimePaths(
            self.root / "brain", self.root / "state", self.root / "config.json"
        )
        self.codex_root = self.root / "codex"
        self.skills = self.codex_root / "skills"
        self.hooks = self.codex_root / "hooks.json"
        cli = self.source / ".venv" / "bin" / "fulcrum"
        cli.parent.mkdir(parents=True)
        cli.write_text("#!/bin/sh\n", encoding="utf-8")
        cli.chmod(0o700)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def install(self) -> dict[str, object]:
        with patch(
            "fulcrum.install.runtime_package_root",
            return_value=(self.source / "src" / "fulcrum").resolve(),
        ):
            return install_runtime(
                paths=self.paths,
                source_root=self.source,
                certified_revision=self.revision,
                skills_root=self.skills,
                hooks_config=self.hooks,
                host_id="local",
                expected_brain_remote="git@example.test:brain.git",
                sage_anchor="2026-09-12T08:00:00Z",
                now=NOW,
            )

    def prepare_valid_fleet_state(self) -> None:
        self.install()
        atomic_write_record(
            self.paths,
            {
                "record_kind": "role_run_registry",
                "schema_version": 1,
                "writer_id": "task-archon",
                "updated_at": NOW,
                "current_archon_task_id": "task-archon",
                "roles": [
                    self.role("archon", "task-archon"),
                    self.role("night_watchman", "task-watchman"),
                ],
            },
        )
        projects = []
        for name in ("fulcrum", "tollgate", "battlement"):
            repo = self.root / name
            (repo / ".git").mkdir(parents=True)
            projects.append(
                {
                    "project_id": name,
                    "repo_path": str(repo),
                    "host_id": "local",
                    "codex_project_id": f"codex-{name}",
                    "tollgate_repo_id": f"tg-{name}",
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
        for role_name, task_id in (
            ("archon", "task-archon"),
            ("night_watchman", "task-watchman"),
        ):
            atomic_write_record(
                self.paths,
                initialize_progress(self.role(role_name, task_id), NOW),
            )
        atomic_write_record(
            self.paths,
            {
                "record_kind": "holds_jobs",
                "schema_version": 1,
                "writer_id": "task-archon",
                "updated_at": NOW,
                "holds": [],
                "recurring_jobs": [
                    {
                        "job_id": "sage:fleet",
                        "cadence_anchor": NOW,
                        "next_due": NOW,
                        "active_run_id": None,
                        "role": "sage",
                        "scope": "fleet",
                    },
                    *[
                        {
                            "job_id": f"inquisitor:{name}",
                            "cadence_anchor": NOW,
                            "next_due": NOW,
                            "active_run_id": None,
                            "role": "inquisitor",
                            "scope": f"project:{name}",
                        }
                        for name in ("battlement", "fulcrum", "tollgate")
                    ],
                ],
            },
        )
        config = json.loads(self.paths.config_file.read_text())
        config["observations"].update(
            hook_trust="desktop_verified",
            runtime_observation="available",
            watchman_schedule="ready",
            codex_projects_verified_at=NOW,
        )
        config["first_watchman_patrol"] = {
            "watchman_task_id": "task-watchman",
            "observed_at": NOW,
            "outcome": "success",
            "evidence": "quiet patrol completed with no notifications",
        }
        self.paths.config_file.write_text(json.dumps(config), encoding="utf-8")

    def run_doctor(self) -> dict[str, object]:
        with (
            patch(
                "fulcrum.doctor.runtime_package_root",
                return_value=(self.source / "src" / "fulcrum").resolve(),
            ),
            patch(
                "fulcrum.doctor.schema_root",
                return_value=(self.source / "schemas").resolve(),
            ),
            patch("fulcrum.doctor._tool_version", return_value="version"),
            patch(
                "fulcrum.doctor._tollgate_project_status",
                side_effect=lambda repository_id: {
                    "path": str(self.root / repository_id.removeprefix("tg-")),
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
            return doctor_runtime(
                paths=self.paths,
                expected_brain_remote="git@example.test:brain.git",
                hooks_config=self.hooks,
                skills_root=self.skills,
            )

    def test_install_twice_links_live_source_without_touching_active_state(
        self,
    ) -> None:
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
        installed = json.loads(self.paths.config_file.read_text())
        installed["observations"]["skill_revision"] = "obsolete"
        installed["observations"]["package_version"] = "obsolete"
        installed["observations"]["source_revision"] = "obsolete"
        self.paths.config_file.write_text(json.dumps(installed), encoding="utf-8")
        second = self.install()
        self.assertEqual(first_assignment, assignment_path.read_bytes())
        self.assertEqual(first_jobs, jobs_path.read_bytes())
        installation = read_record(self.paths, "installation")
        self.assertEqual(
            installation["configured_services"],
            ["beads", "codex", "hooks", "tollgate"],
        )
        self.assertNotIn("skill_revision", installation["observations"])
        self.assertNotIn("package_version", installation["observations"])
        self.assertNotIn("source_revision", installation["observations"])
        self.assertEqual(len(json.loads(self.hooks.read_text())["hooks"]["Stop"]), 1)
        self.assertEqual(len(first["skill_links"]), len(LINKED_SKILLS))
        for name in LINKED_SKILLS:
            self.assertTrue((self.skills / name).is_symlink())
            self.assertEqual(
                (self.skills / name).resolve(),
                (self.source / "skills" / name).resolve(),
            )
        hook_link = self.codex_root / "hooks" / "fulcrum"
        self.assertTrue(hook_link.is_symlink())
        cli_link = self.codex_root / "bin" / "fulcrum"
        self.assertTrue(cli_link.is_symlink())
        self.assertEqual(
            cli_link.resolve(), (self.source / ".venv" / "bin" / "fulcrum").resolve()
        )
        configured_command = json.loads(self.hooks.read_text())["hooks"]["Stop"][0][
            "hooks"
        ][0]["command"]
        self.assertEqual(configured_command, str(hook_link / "fulcrum-hook"))
        skill_source = self.source / "skills" / "fulcrum-archon" / "SKILL.md"
        skill_source.write_text("changed live\n", encoding="utf-8")
        self.assertEqual(
            (self.skills / "fulcrum-archon" / "SKILL.md").read_text(),
            "changed live\n",
        )
        hook_source = self.source / "hooks" / "fulcrum-hook"
        hook_source.write_text("hook changed\n", encoding="utf-8")
        self.assertEqual((hook_link / "fulcrum-hook").read_text(), "hook changed\n")
        self.assertTrue(first["hook_retrust_required"])
        self.assertFalse(second["hook_retrust_required"])
        self.assertFalse(second["database_restarted"])

    def test_unsupported_schema_is_preserved(self) -> None:
        incompatible = b'{"record_kind":"installation","schema_version":44}\n'
        self.paths.config_file.write_bytes(incompatible)
        with self.assertRaises(InstallationError):
            self.install()
        self.assertEqual(self.paths.config_file.read_bytes(), incompatible)

    def test_existing_nonlink_asset_is_preserved_and_rejected(self) -> None:
        target = self.skills / "fulcrum-archon"
        target.mkdir(parents=True)
        marker = target / "keep.txt"
        marker.write_text("unrelated\n", encoding="utf-8")
        with self.assertRaisesRegex(InstallationError, "not the expected symlink"):
            self.install()
        self.assertEqual(marker.read_text(), "unrelated\n")

    def test_noneditable_package_import_is_rejected(self) -> None:
        with self.assertRaisesRegex(InstallationError, "imports from"):
            install_runtime(
                paths=self.paths,
                source_root=self.source,
                certified_revision=self.revision,
                skills_root=self.skills,
                hooks_config=self.hooks,
                host_id="local",
                expected_brain_remote="remote",
                sage_anchor=NOW,
                now=NOW,
            )

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
                host_id="local",
                expected_brain_remote="remote",
                sage_anchor=NOW,
                now=NOW,
            )

    def test_doctor_separates_required_optional_and_push_findings(self) -> None:
        self.install()
        roles = {
            "record_kind": "role_run_registry",
            "schema_version": 1,
            "writer_id": "task-archon",
            "updated_at": NOW,
            "current_archon_task_id": "task-archon",
            "roles": [
                self.role("archon", "task-archon"),
                self.role("night_watchman", "task-watchman"),
            ],
        }
        atomic_write_record(self.paths, roles)
        projects = []
        for name in ("fulcrum", "tollgate", "battlement"):
            repo = self.root / name
            (repo / ".git").mkdir(parents=True)
            projects.append(
                {
                    "project_id": name,
                    "repo_path": str(repo),
                    "host_id": "local",
                    "codex_project_id": f"codex-{name}",
                    "tollgate_repo_id": f"tg-{name}",
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
        for role_name, task_id in (
            ("archon", "task-archon"),
            ("night_watchman", "task-watchman"),
        ):
            atomic_write_record(
                self.paths,
                initialize_progress(self.role(role_name, task_id), NOW),
            )
        atomic_write_record(
            self.paths,
            {
                "record_kind": "holds_jobs",
                "schema_version": 1,
                "writer_id": "task-archon",
                "updated_at": NOW,
                "holds": [],
                "recurring_jobs": [
                    {
                        "job_id": "sage:fleet",
                        "cadence_anchor": NOW,
                        "next_due": NOW,
                        "active_run_id": None,
                        "role": "sage",
                        "scope": "fleet",
                    },
                    *[
                        {
                            "job_id": f"inquisitor:{name}",
                            "cadence_anchor": NOW,
                            "next_due": NOW,
                            "active_run_id": None,
                            "role": "inquisitor",
                            "scope": f"project:{name}",
                        }
                        for name in ("battlement", "fulcrum", "tollgate")
                    ],
                ],
            },
        )
        config = json.loads(self.paths.config_file.read_text())
        config["observations"].update(
            hook_trust="desktop_verified",
            runtime_observation="available",
            watchman_schedule="ready",
            codex_projects_verified_at=NOW,
        )
        config["first_watchman_patrol"] = {
            "watchman_task_id": "task-watchman",
            "observed_at": NOW,
            "outcome": "success",
            "evidence": "quiet patrol completed with no notifications",
        }
        self.paths.config_file.write_text(json.dumps(config), encoding="utf-8")
        with (
            patch(
                "fulcrum.doctor.runtime_package_root",
                return_value=(self.source / "src" / "fulcrum").resolve(),
            ),
            patch(
                "fulcrum.doctor.schema_root",
                return_value=(self.source / "schemas").resolve(),
            ),
            patch("fulcrum.doctor._tool_version", return_value="version"),
            patch(
                "fulcrum.doctor._tollgate_project_status",
                side_effect=lambda repository_id: {
                    "path": str(self.root / repository_id.removeprefix("tg-")),
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

    def test_doctor_fails_closed_for_missing_state_and_failed_first_patrol(
        self,
    ) -> None:
        self.install()
        atomic_write_record(
            self.paths,
            {
                "record_kind": "role_run_registry",
                "schema_version": 1,
                "writer_id": "task-archon",
                "updated_at": NOW,
                "current_archon_task_id": "task-archon",
                "roles": [
                    self.role("archon", "task-archon"),
                    self.role("night_watchman", "task-watchman"),
                ],
            },
        )
        config = json.loads(self.paths.config_file.read_text())
        config["first_watchman_patrol"] = {
            "watchman_task_id": "task-watchman",
            "observed_at": NOW,
            "outcome": "failure",
            "evidence": "patrol failed before completing its checks",
        }
        self.paths.config_file.write_text(json.dumps(config), encoding="utf-8")
        with (
            patch(
                "fulcrum.doctor.runtime_package_root",
                return_value=(self.source / "src" / "fulcrum").resolve(),
            ),
            patch(
                "fulcrum.doctor.schema_root",
                return_value=(self.source / "schemas").resolve(),
            ),
            patch("fulcrum.doctor._tool_version", return_value="version"),
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
        checks = {check["name"]: check for check in report["checks"]}
        self.assertFalse(report["ready"])
        for name in (
            "holds_jobs_state",
            "archon_progress",
            "watchman_progress",
            "watchman_first_patrol",
        ):
            self.assertEqual(checks[name]["status"], "fail")

    def test_doctor_names_invalid_holds_jobs_owner(self) -> None:
        self.prepare_valid_fleet_state()
        path = selected_record_path(self.paths, "holds_jobs", None)
        jobs = read_record(self.paths, "holds_jobs")
        jobs["writer_id"] = "task-other"
        path.write_text(json.dumps(jobs), encoding="utf-8")

        report = self.run_doctor()

        checks = {check["name"]: check for check in report["checks"]}
        self.assertFalse(report["ready"])
        self.assertEqual(checks["holds_jobs_state"]["status"], "fail")
        self.assertIn("current Archon", checks["holds_jobs_state"]["detail"])

    def test_doctor_names_wrong_archon_progress_owner(self) -> None:
        self.prepare_valid_fleet_state()
        path = selected_record_path(self.paths, "progress", "task-archon")
        progress = read_record(self.paths, "progress", "task-archon")
        progress["writer_id"] = "task-other"
        path.write_text(json.dumps(progress), encoding="utf-8")

        report = self.run_doctor()

        checks = {check["name"]: check for check in report["checks"]}
        self.assertFalse(report["ready"])
        self.assertEqual(checks["archon_progress"]["status"], "fail")
        self.assertIn("writer_id", checks["archon_progress"]["detail"])
        self.assertEqual(checks["watchman_progress"]["status"], "pass")

    def test_doctor_names_wrong_watchman_progress_owner(self) -> None:
        self.prepare_valid_fleet_state()
        path = selected_record_path(self.paths, "progress", "task-watchman")
        progress = read_record(self.paths, "progress", "task-watchman")
        progress["writer_id"] = "task-other"
        path.write_text(json.dumps(progress), encoding="utf-8")

        report = self.run_doctor()

        checks = {check["name"]: check for check in report["checks"]}
        self.assertFalse(report["ready"])
        self.assertEqual(checks["watchman_progress"]["status"], "fail")
        self.assertIn("writer_id", checks["watchman_progress"]["detail"])
        self.assertEqual(checks["archon_progress"]["status"], "pass")

    def test_doctor_names_incomplete_required_job_ledger(self) -> None:
        self.prepare_valid_fleet_state()
        path = selected_record_path(self.paths, "holds_jobs", None)
        jobs = read_record(self.paths, "holds_jobs")
        jobs["recurring_jobs"] = [
            job
            for job in jobs["recurring_jobs"]
            if job["job_id"] != "inquisitor:tollgate"
        ]
        path.write_text(json.dumps(jobs), encoding="utf-8")

        report = self.run_doctor()

        checks = {check["name"]: check for check in report["checks"]}
        self.assertFalse(report["ready"])
        self.assertEqual(checks["holds_jobs_state"]["status"], "fail")
        self.assertIn("inquisitor:tollgate", checks["holds_jobs_state"]["detail"])
        self.assertEqual(checks["archon_progress"]["status"], "pass")
        self.assertEqual(checks["watchman_progress"]["status"], "pass")

    def test_doctor_names_failed_patrol_without_other_state_failures(self) -> None:
        self.prepare_valid_fleet_state()
        config = json.loads(self.paths.config_file.read_text())
        config["first_watchman_patrol"]["outcome"] = "failure"
        self.paths.config_file.write_text(json.dumps(config), encoding="utf-8")

        report = self.run_doctor()

        checks = {check["name"]: check for check in report["checks"]}
        self.assertFalse(report["ready"])
        self.assertEqual(checks["watchman_first_patrol"]["status"], "fail")
        self.assertEqual(checks["holds_jobs_state"]["status"], "pass")
        self.assertEqual(checks["archon_progress"]["status"], "pass")
        self.assertEqual(checks["watchman_progress"]["status"], "pass")

    @staticmethod
    def role(role: str, task_id: str) -> dict[str, object]:
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
        }


if __name__ == "__main__":
    unittest.main()
