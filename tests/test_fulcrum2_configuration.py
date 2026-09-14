from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fulcrum.configuration import ConfigurationManager, _ensure_tollgate_repository
from fulcrum.contracts import FulcrumError
from fulcrum.ledger import Ledger
from fulcrum.tollgate import TollgateUncertainError


class Fulcrum2ConfigurationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name).resolve()
        cls.brain = cls.root / "brain"
        cls.instance = cls.root / "instance"
        cls.project = cls.root / "project"
        for path in (cls.brain, cls.instance, cls.project):
            path.mkdir()
        for path in (cls.brain, cls.project):
            subprocess.run(["git", "init", "--quiet"], cwd=path, check=True)
            subprocess.run(
                ["git", "config", "user.email", "fixture@example.com"],
                cwd=path,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Fixture"], cwd=path, check=True
            )
            subprocess.run(
                ["git", "config", "beads.role", "maintainer"], cwd=path, check=True
            )
        with socket.socket() as candidate:
            candidate.bind(("127.0.0.1", 0))
            requested_port = candidate.getsockname()[1]
        subprocess.run(
            [
                "bd",
                "init",
                "--server",
                "--server-host",
                "127.0.0.1",
                "--server-port",
                str(requested_port),
                "--database",
                "fulcrum",
                "--prefix",
                "fc",
                "--non-interactive",
                "--skip-agents",
                "--skip-hooks",
            ],
            cwd=cls.brain,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        observed = subprocess.run(
            ["bd", "-C", str(cls.brain), "--json", "dolt", "show"],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
        cls.port = int(json.loads(observed.stdout)["port"])
        cls.config = cls.brain / "fulcrum.yaml"
        cls.config.write_text(
            "# retain this human comment\n"
            f"brain:\n  root: {cls.brain}\n"
            "beads:\n"
            f"  executable: {shutil.which('bd')}\n"
            "  host: 127.0.0.1\n"
            f"  port: {cls.port}\n"
            "  database: fulcrum\n"
            "delivery:\n"
            "  kind: deterministic\n",
            encoding="utf-8",
        )
        (cls.instance / "config").symlink_to(cls.config)
        cls.executable = Path(os.sys.executable).with_name("fulcrum")

    @classmethod
    def tearDownClass(cls) -> None:
        subprocess.run(
            ["bd", "-C", str(cls.brain), "dolt", "stop"],
            capture_output=True,
            check=False,
            timeout=20,
        )
        cls.temporary.cleanup()

    def invoke(
        self, *arguments: str, payload: dict[str, object] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.executable), *arguments],
            input=json.dumps(payload) if payload is not None else None,
            capture_output=True,
            text=True,
            check=False,
            timeout=40,
        )

    def test_human_patch_preserves_comments_and_effective_defaults(self) -> None:
        shown = self.invoke(
            "config", "show", "--instance", str(self.instance), "--json"
        )
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(
            json.loads(shown.stdout)["result"]["config"]["policy"][
                "automatic_capacity"
            ],
            4,
        )

        changed = self.invoke(
            "config",
            "set",
            "--instance",
            str(self.instance),
            "--offline",
            "--actor",
            "human",
            "--input",
            "-",
            "--json",
            payload={
                "policy": {
                    "automatic_capacity": 6,
                    "rationale": "Fixture authority check.",
                }
            },
        )
        self.assertEqual(changed.returncode, 0, changed.stderr)
        result = json.loads(changed.stdout)
        self.assertIn(
            "policy.automatic_capacity", result["result"]["result"]["changed_fields"]
        )
        current = self.config.read_text(encoding="utf-8")
        self.assertIn("# retain this human comment", current)
        self.assertIn("automatic_capacity: 6", current)

    def test_non_authorized_roles_cannot_change_yaml_and_vizier_can(self) -> None:
        before = self.config.read_bytes()
        denied = self.invoke(
            "policy",
            "set",
            "--instance",
            str(self.instance),
            "--offline",
            "--actor",
            "task:marshal-task",
            "--thread-id",
            "marshal-task",
            "--input",
            "-",
            "--json",
            payload={"automatic_capacity": 5},
        )
        self.assertEqual(denied.returncode, 5)
        self.assertEqual(
            json.loads(denied.stdout)["error"]["code"], "CONFIG_AUTHORITY_DENIED"
        )
        self.assertEqual(self.config.read_bytes(), before)

        ledger = Ledger(self.brain)
        if ledger.show("fc-system") is None:
            ledger.create_record(
                record_id="fc-system",
                kind="control",
                title="Fulcrum control",
                description="Standing leadership identities.",
                owner="vizier-task",
                fc={
                    "kind": "control",
                    "owner": "vizier-task",
                    "vizier_thread": "vizier-task",
                    "marshal_thread": None,
                    "active_takeover": None,
                    "last_transition": None,
                },
            )
        accepted = self.invoke(
            "policy",
            "set",
            "--instance",
            str(self.instance),
            "--offline",
            "--actor",
            "task:vizier-task",
            "--thread-id",
            "vizier-task",
            "--input",
            "-",
            "--json",
            payload={"automatic_capacity": 7},
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        shown = self.invoke(
            "policy", "show", "--instance", str(self.instance), "--json"
        )
        self.assertEqual(
            json.loads(shown.stdout)["result"]["policy"]["automatic_capacity"], 7
        )

    def test_offline_human_can_repair_configuration_when_beads_is_unavailable(
        self,
    ) -> None:
        actual_bd = str(shutil.which("bd"))
        current = self.config.read_text(encoding="utf-8")
        self.config.write_text(
            current.replace(f"executable: {actual_bd}", "executable: /missing/bd"),
            encoding="utf-8",
        )
        self.assertIn("executable: /missing/bd", self.config.read_text())
        repaired = self.invoke(
            "config",
            "set",
            "--instance",
            str(self.instance),
            "--offline",
            "--actor",
            "human",
            "--input",
            "-",
            "--json",
            payload={"beads": {"executable": actual_bd}},
        )
        self.assertEqual(repaired.returncode, 6, repaired.stderr)
        result = json.loads(repaired.stdout)
        self.assertEqual(result["state"], "degraded")
        self.assertIsNone(result["operation_id"])
        self.assertFalse(result["result"]["receipt_persisted"])
        self.assertIn(f"executable: {actual_bd}", self.config.read_text())

    def test_concurrent_edit_and_malformed_yaml_fail_without_overwrite(self) -> None:
        manager = ConfigurationManager(
            self.config,
            before_replace=lambda path: path.write_bytes(
                path.read_bytes() + b"# external edit\n"
            ),
        )
        document, original, _, _ = manager.prepare_patch(
            {"policy": {"automatic_capacity": 8}}
        )
        with self.assertRaises(FulcrumError) as conflict:
            manager.replace(document, original)
        self.assertEqual(conflict.exception.code, "CONFIG_CONFLICT")
        self.assertTrue(self.config.read_bytes().endswith(b"# external edit\n"))

        malformed = self.root / "duplicate.yaml"
        malformed.write_text(
            f"brain:\n  root: {malformed.parent}\npolicy:\n  automatic_capacity: 4\n  automatic_capacity: 5\n",
            encoding="utf-8",
        )
        with self.assertRaises(FulcrumError) as invalid:
            ConfigurationManager(malformed).load()
        self.assertEqual(invalid.exception.code, "CONFIG_INVALID")

    def test_project_enrollment_sets_exact_actor_and_removal_checks_open_intake(
        self,
    ) -> None:
        added = self.invoke(
            "project",
            "add",
            "--instance",
            str(self.instance),
            "--offline",
            "--actor",
            "human",
            "--input",
            "-",
            "--json",
            payload={
                "id": "toy",
                "root": str(self.project),
                "codex_project_id": "native-toy",
                "delivery": {"id": "tollgate-toy"},
                "integration_branch": "main",
            },
        )
        self.assertEqual(added.returncode, 0, added.stderr + added.stdout)
        self.assertIn(
            ".beads/", (self.project / ".git" / "info" / "exclude").read_text()
        )

        created = subprocess.run(
            [
                "bd",
                "-C",
                str(self.project),
                "--json",
                "create",
                "--title",
                "Native intake",
                "--description",
                "Preserve me",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        issue = json.loads(created.stdout)
        self.assertEqual(issue["created_by"], "project:toy")
        issue_id = issue["id"]

        refused = self.invoke(
            "project",
            "remove",
            "toy",
            "--instance",
            str(self.instance),
            "--offline",
            "--actor",
            "human",
            "--json",
        )
        self.assertEqual(refused.returncode, 5)
        self.assertEqual(json.loads(refused.stdout)["error"]["code"], "PROJECT_IN_USE")
        subprocess.run(
            [
                "bd",
                "-C",
                str(self.project),
                "close",
                issue_id,
                "--reason",
                "fixture complete",
            ],
            capture_output=True,
            check=True,
            timeout=30,
        )
        removed = self.invoke(
            "project",
            "remove",
            "toy",
            "--instance",
            str(self.instance),
            "--offline",
            "--actor",
            "human",
            "--json",
        )
        self.assertEqual(removed.returncode, 0, removed.stderr)

    def test_tollgate_enrollment_verifies_exact_root_and_recovers_lost_creation(
        self,
    ) -> None:
        class FakeTollgate:
            def __init__(self) -> None:
                self.rows: list[dict[str, Any]] = []
                self.lose_creation = True

            def repositories(self) -> list[dict[str, Any]]:
                return list(self.rows)

            def add_repository(self, path: Path) -> dict[str, Any]:
                row = {"state": {"id": "repo-created", "path": str(path)}}
                self.rows.append(row)
                if self.lose_creation:
                    self.lose_creation = False
                    raise TollgateUncertainError(
                        "lost response", category="uncertain", possible_effect=True
                    )
                return row

        provider = FakeTollgate()
        effective = {"delivery": {"kind": "tollgate", "executable": "/fixture/tg"}}
        with patch("fulcrum.configuration.Tollgate", return_value=provider):
            delivery, evidence = _ensure_tollgate_repository(
                "exact", {"root": str(self.project)}, effective
            )
        self.assertEqual(delivery, {"id": "repo-created", "registration": "created"})
        self.assertEqual(evidence["ownership"], "created")

        with patch("fulcrum.configuration.Tollgate", return_value=provider):
            supplied, supplied_evidence = _ensure_tollgate_repository(
                "exact",
                {
                    "root": str(self.project),
                    "delivery": {"id": "repo-created"},
                },
                effective,
            )
        self.assertEqual(supplied["registration"], "supplied")
        self.assertEqual(supplied_evidence["state"], "verified")

        other = self.root / "other-provider-root"
        other.mkdir(exist_ok=True)
        provider.rows[0]["state"]["path"] = str(other)
        with patch("fulcrum.configuration.Tollgate", return_value=provider):
            with self.assertRaises(FulcrumError) as mismatch:
                _ensure_tollgate_repository(
                    "exact",
                    {
                        "root": str(self.project),
                        "delivery": {"id": "repo-created"},
                    },
                    effective,
                )
        self.assertEqual(mismatch.exception.code, "PROJECT_PROVIDER_MISMATCH")


if __name__ == "__main__":
    unittest.main()
