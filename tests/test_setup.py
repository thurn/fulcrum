"""Human-authorized setup bootstrap and evidence recording."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fulcrum.config import RuntimePaths
from fulcrum.setup import SetupError, bootstrap_fleet, record_setup_evidence
from fulcrum.state import atomic_write_record, read_record

NOW = "2026-09-11T20:00:00Z"


class SetupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.paths = RuntimePaths(root / "brain", root / "state", root / "config.json")
        atomic_write_record(
            self.paths,
            {
                "record_kind": "installation",
                "schema_version": 1,
                "writer_id": "setup",
                "updated_at": NOW,
                "brain_root": str(self.paths.brain_root),
                "state_root": str(self.paths.state_root),
                "host_id": "local",
                "configured_services": ["beads", "codex", "hooks", "tollgate"],
                "observations": {"sage_cadence_anchor": "2026-09-11T08:00:00Z"},
            },
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def input(self, *, healthy: bool = True) -> dict[str, object]:
        projects = []
        for name in ("fulcrum", "tollgate", "battlement"):
            root = self.paths.state_root.parent / name
            (root / ".git").mkdir(parents=True, exist_ok=True)
            projects.append(
                {
                    "project_id": name,
                    "repo_path": str(root),
                    "host_id": "local",
                    "codex_project_id": f"codex-{name}",
                    "tollgate_repo_id": f"tg-{name}",
                    "observations": {
                        "git_root": str(root),
                        "codex_id": f"codex-{name}",
                        "codex_path": str(root),
                        "codex_host": "local",
                        "codex_is_git": True,
                        "tollgate_id": f"tg-{name}",
                        "tollgate_path": str(root),
                        "tollgate_healthy": healthy or name != "battlement",
                    },
                }
            )
        return {
            "observed_at": NOW,
            "archon": self.role("archon", "task-archon"),
            "watchman": self.role("night_watchman", "task-watchman"),
            "projects": projects,
        }

    @staticmethod
    def role(name: str, task_id: str) -> dict[str, object]:
        return {
            "task_id": task_id,
            "host_id": "local",
            "project_id": "fulcrum" if name == "archon" else "fleet",
            "title": f"Fulcrum {name}",
            "selected_model": "human-model",
            "selected_reasoning": "high",
            "human_created": True,
            "authorization_reference": "user invoked setup in this task",
        }

    def test_bootstrap_is_idempotent_and_initializes_patrol_state(self) -> None:
        first = bootstrap_fleet(self.paths, self.input())
        second = bootstrap_fleet(self.paths, self.input())
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        roles = read_record(self.paths, "role_run_registry")
        self.assertEqual(roles["current_archon_task_id"], "task-archon")
        self.assertEqual(len(roles["roles"]), 2)
        self.assertTrue(all(role["role_number"] is None for role in roles["roles"]))
        projects = read_record(self.paths, "project_registry")
        self.assertEqual(len(projects["projects"]), 3)
        self.assertTrue(all(project["enabled"] for project in projects["projects"]))
        progress = read_record(self.paths, "progress", "task-archon")
        self.assertEqual(progress["role"], "archon")
        watchman_progress = read_record(self.paths, "progress", "task-watchman")
        self.assertEqual(watchman_progress["role"], "night_watchman")
        jobs = read_record(self.paths, "holds_jobs")
        self.assertEqual(jobs["holds"], [])
        self.assertEqual(
            [job["job_id"] for job in jobs["recurring_jobs"]],
            [
                "sage:fleet",
                "inquisitor:battlement",
                "inquisitor:fulcrum",
                "inquisitor:tollgate",
            ],
        )
        self.assertEqual(
            {job["job_id"]: job["next_due"] for job in jobs["recurring_jobs"]},
            {
                "sage:fleet": "2026-09-11T08:00:00Z",
                "inquisitor:battlement": "2026-09-11T20:00:00Z",
                "inquisitor:fulcrum": "2026-09-11T20:00:00Z",
                "inquisitor:tollgate": "2026-09-11T20:00:00Z",
            },
        )

    def test_bootstrap_reconciles_partial_patrol_state_without_erasing_holds(
        self,
    ) -> None:
        bootstrap_fleet(self.paths, self.input())
        jobs = read_record(self.paths, "holds_jobs")
        jobs["holds"].append(
            {
                "hold_id": "incident:startup",
                "scope": "fleet",
                "reason": "startup investigation",
                "release_condition": "investigation complete",
                "permitted_exceptions": [],
            }
        )
        jobs["recurring_jobs"] = jobs["recurring_jobs"][:1]
        atomic_write_record(self.paths, jobs)
        (self.paths.state_root / "progress" / "task-watchman.json").unlink()

        bootstrap_fleet(self.paths, self.input())

        reconciled = read_record(self.paths, "holds_jobs")
        self.assertEqual(reconciled["holds"][0]["hold_id"], "incident:startup")
        self.assertEqual(len(reconciled["recurring_jobs"]), 4)
        self.assertEqual(
            len({job["job_id"] for job in reconciled["recurring_jobs"]}),
            4,
        )
        self.assertEqual(
            read_record(self.paths, "progress", "task-watchman")["role"],
            "night_watchman",
        )

    def test_bootstrap_reconciles_handover_with_historical_archon_and_stale_owner(
        self,
    ) -> None:
        bootstrap_fleet(self.paths, self.input())
        roles = read_record(self.paths, "role_run_registry")
        roles["current_archon_task_id"] = "task-current-archon"
        current_archon = dict(
            next(role for role in roles["roles"] if role["role"] == "archon")
        )
        current_archon["task_id"] = "task-current-archon"
        current_archon["title"] = "Fulcrum Current Archon"
        roles["roles"].append(current_archon)
        roles["writer_id"] = "task-current-archon"
        atomic_write_record(self.paths, roles, handover_from="task-archon")

        handover = self.input()
        handover["archon"] = self.role("archon", "task-current-archon")
        bootstrap_fleet(self.paths, handover)

        reconciled_roles = read_record(self.paths, "role_run_registry")
        archons = [
            role["task_id"]
            for role in reconciled_roles["roles"]
            if role["role"] == "archon"
        ]
        self.assertEqual(archons, ["task-archon", "task-current-archon"])
        self.assertEqual(
            reconciled_roles["current_archon_task_id"], "task-current-archon"
        )
        reconciled_jobs = read_record(self.paths, "holds_jobs")
        self.assertEqual(reconciled_jobs["writer_id"], "task-current-archon")
        self.assertEqual(len(reconciled_jobs["recurring_jobs"]), 4)

    def test_bootstrap_requires_explicit_sage_anchor(self) -> None:
        self.paths.config_file.write_text(
            self.paths.config_file.read_text().replace(
                '"sage_cadence_anchor": "2026-09-11T08:00:00Z"',
                '"other": "configured"',
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(SetupError, "sage_cadence_anchor is required"):
            bootstrap_fleet(self.paths, self.input())

    def test_partial_bootstrap_write_is_not_reported_as_success(self) -> None:
        writes = 0

        def fail_after_first(*args: object, **kwargs: object) -> object:
            nonlocal writes
            writes += 1
            if writes == 2:
                raise OSError("interrupted bootstrap")
            return atomic_write_record(*args, **kwargs)  # type: ignore[arg-type]

        with patch("fulcrum.setup.atomic_write_record", side_effect=fail_after_first):
            with self.assertRaisesRegex(SetupError, "state write failed"):
                bootstrap_fleet(self.paths, self.input())
        self.assertFalse(
            (self.paths.state_root / "registry" / "projects.json").exists()
        )

    def test_unhealthy_project_is_retained_disabled(self) -> None:
        result = bootstrap_fleet(self.paths, self.input(healthy=False))
        self.assertFalse(result["ok"])
        projects = read_record(self.paths, "project_registry")["projects"]
        battlement = next(
            item for item in projects if item["project_id"] == "battlement"
        )
        self.assertFalse(battlement["enabled"])
        self.assertIn("Tollgate", battlement["ineligibility_reason"])

    def test_conflicting_archon_and_nonhuman_role_are_rejected(self) -> None:
        bootstrap_fleet(self.paths, self.input())
        conflicting = self.input()
        conflicting["archon"] = self.role("archon", "task-other")
        with self.assertRaisesRegex(SetupError, "another current Archon"):
            bootstrap_fleet(self.paths, conflicting)
        nonhuman = self.input()
        watchman = nonhuman["watchman"]
        self.assertIsInstance(watchman, dict)
        watchman["human_created"] = False
        with self.assertRaisesRegex(SetupError, "human-created"):
            bootstrap_fleet(self.paths, nonhuman)

    def test_record_evidence_requires_current_archon_and_human_watchman(self) -> None:
        bootstrap_fleet(self.paths, self.input())
        with self.assertRaisesRegex(SetupError, "current Archon"):
            record_setup_evidence(
                self.paths,
                archon_task_id="task-other",
                watchman_schedule_id="automation-1",
                codex_projects_verified_at=NOW,
                hooks_verified_at=NOW,
                hooks_evidence="reviewed in the desktop hook browser",
            )
        result = record_setup_evidence(
            self.paths,
            archon_task_id="task-archon",
            watchman_schedule_id="automation-1",
            codex_projects_verified_at=NOW,
            hooks_verified_at=NOW,
            hooks_evidence="reviewed in the desktop hook browser",
        )
        self.assertTrue(result["ok"])
        observations = read_record(self.paths, "installation")["observations"]
        self.assertEqual(observations["watchman_schedule_id"], "automation-1")
        self.assertEqual(observations["hook_trust"], "desktop_verified")

    def test_schedule_evidence_does_not_invent_optional_hook_delivery(self) -> None:
        bootstrap_fleet(self.paths, self.input())
        record_setup_evidence(
            self.paths,
            archon_task_id="task-archon",
            watchman_schedule_id="automation-1",
            codex_projects_verified_at=NOW,
        )
        observations = read_record(self.paths, "installation")["observations"]
        self.assertEqual(observations["watchman_schedule"], "ready")
        self.assertNotIn("hook_trust", observations)
        with self.assertRaisesRegex(SetupError, "supplied together"):
            record_setup_evidence(
                self.paths,
                archon_task_id="task-archon",
                watchman_schedule_id="automation-1",
                codex_projects_verified_at=NOW,
                hooks_evidence="missing timestamp",
            )


if __name__ == "__main__":
    unittest.main()
