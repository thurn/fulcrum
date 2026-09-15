from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fulcrum.configuration import ConfigurationManager, default_config
from fulcrum.contracts import (
    ActorContext,
    CommandResult,
    CommandState,
    FulcrumError,
    InstanceContext,
    ParsedRequest,
)
from fulcrum.ledger import Ledger
from fulcrum.source_refresh import (
    SourceRefreshService,
    build_installed_environment,
    install_recovery_link,
    maintenance_path,
    probe_installed_environment,
    switch_installed_pointer,
    write_maintenance,
)


class Fulcrum2SourceRefreshTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.brain = self.root / "brain"
        self.instance = self.root / "instance"
        self.brain.mkdir()
        self.instance.mkdir()
        subprocess.run(["git", "init", "--quiet"], cwd=self.brain, check=True)
        subprocess.run(
            ["git", "config", "user.email", "fixture@example.com"],
            cwd=self.brain,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Fixture"],
            cwd=self.brain,
            check=True,
        )
        subprocess.run(
            ["git", "config", "beads.role", "maintainer"],
            cwd=self.brain,
            check=True,
        )
        subprocess.run(
            [
                "bd",
                "init",
                "--prefix",
                "fc",
                "--non-interactive",
                "--skip-agents",
                "--skip-hooks",
            ],
            cwd=self.brain,
            capture_output=True,
            check=True,
            timeout=30,
        )
        self.config_path = self.brain / "fulcrum.yaml"
        config = default_config(self.brain)
        config["runtime"] = {
            "kind": "deterministic",
            "endpoint": "ws://127.0.0.1:1",
            "executable": None,
        }
        config["delivery"] = {"kind": "deterministic", "executable": None}
        config["source_watch_root"] = None
        stream = ConfigurationManager.yaml()
        with self.config_path.open("w", encoding="utf-8") as output:
            stream.dump(config, output)
        (self.instance / "config").symlink_to(self.config_path)
        self.context = InstanceContext(
            instance_root=self.instance,
            config_path=self.config_path,
            brain_root=self.brain,
            socket_path=self.instance / "controller.sock",
            lock_path=self.brain / ".fulcrum-controller.lock",
            explicit_selection=True,
        )
        self.ledger = Ledger(self.brain)
        self.ledger.create_record(
            record_id="fc-identity-sentinel",
            kind="work",
            title="retained identity",
            description="Must survive installed swaps.",
            owner="HUMAN",
            fc={"kind": "work", "owner": "HUMAN", "phase": "backlog"},
        )
        self.old_main = self._deployment("runtime", "old", recovery=False)
        self.old_recovery = self._deployment("recovery", "old", recovery=True)
        (self.instance / "runtime" / "current").symlink_to(self.old_main.name)
        (self.instance / "recovery" / "current").symlink_to(self.old_recovery.name)

    def tearDown(self) -> None:
        subprocess.run(
            ["bd", "-C", str(self.brain), "dolt", "stop"],
            capture_output=True,
            check=False,
            timeout=20,
        )
        self.temporary.cleanup()

    def _deployment(self, root: str, name: str, *, recovery: bool) -> Path:
        deployment = self.instance / root / f"deployment-{name}"
        binary = deployment / "bin"
        binary.mkdir(parents=True)
        (binary / "fulcrum").write_text("fixture\n", encoding="utf-8")
        (binary / "fulcrum-recover").write_text("fixture\n", encoding="utf-8")
        return deployment

    def request(self, request_id: str | None = None) -> ParsedRequest:
        return ParsedRequest(
            command=("service", "update"),
            arguments={"source": str(Path(__file__).parents[1])},
            input={},
            actor=ActorContext(kind="human"),
            instance=self.context,
            request_id=request_id or str(uuid.uuid4()),
            timeout=30,
            offline=True,
        )

    def _fake_build(
        self,
        _source: Path,
        deployment: Path,
        *,
        config_path: Path | None,
        recovery: bool,
    ) -> dict[str, object]:
        del config_path
        binary = deployment / "bin"
        binary.mkdir(parents=True, exist_ok=True)
        (binary / "fulcrum").write_text("new\n", encoding="utf-8")
        (binary / "fulcrum-recover").write_text("new\n", encoding="utf-8")
        return {
            "deployment": str(deployment),
            "isolated": True,
            "recovery": recovery,
        }

    def _run_with_fakes(
        self, request: ParsedRequest, *, start_effect: object
    ) -> CommandResult:
        with (
            patch(
                "fulcrum.source_refresh._wait_for_quiescence",
                return_value={"ready": True, "acknowledged": True},
            ),
            patch(
                "fulcrum.source_refresh.build_installed_environment",
                side_effect=self._fake_build,
            ),
            patch("fulcrum.source_refresh.load_installed_services", return_value={}),
            patch(
                "fulcrum.source_refresh.fulcrum2_service_definitions", return_value={}
            ),
            patch(
                "fulcrum.source_refresh.install_fulcrum2_service_definitions",
                return_value=({}, []),
            ),
            patch(
                "fulcrum.source_refresh.ServiceService.start",
                side_effect=start_effect,
            ),
        ):
            return SourceRefreshService().update(request)

    def test_crash_after_pointer_switch_resumes_without_identity_replacement(
        self,
    ) -> None:
        request = self.request()
        first = self._run_with_fakes(
            request,
            start_effect=FulcrumError(
                "SERVICE_START_FAILED", "injected post-activation crash", exit_code=4
            ),
        )
        self.assertEqual(first.state, CommandState.UNCERTAIN)
        selected = (self.instance / "runtime" / "current").resolve(strict=True)
        self.assertNotEqual(selected, self.old_main)
        self.assertTrue(maintenance_path(self.instance).is_file())

        started = CommandResult(
            ok=True,
            state=CommandState.COMPLETED,
            operation_id="fc-started",
        )
        second = self._run_with_fakes(request, start_effect=lambda _request: started)
        self.assertEqual(second.state, CommandState.COMPLETED)
        self.assertEqual(
            (self.instance / "runtime" / "current").resolve(strict=True), selected
        )
        self.assertFalse(maintenance_path(self.instance).exists())
        self.assertIsNotNone(self.ledger.show("fc-identity-sentinel"))

    def test_failed_candidate_never_switches_working_installation(self) -> None:
        request = self.request()
        with (
            patch(
                "fulcrum.source_refresh._wait_for_quiescence",
                return_value={"ready": True, "acknowledged": True},
            ),
            patch(
                "fulcrum.source_refresh.build_installed_environment",
                side_effect=RuntimeError("injected probe failure"),
            ),
        ):
            failed = SourceRefreshService().update(request)
        self.assertEqual(failed.state, CommandState.FAILED)
        self.assertEqual(
            (self.instance / "runtime" / "current").resolve(strict=True),
            self.old_main,
        )
        self.assertFalse(maintenance_path(self.instance).exists())

    def test_concurrent_refresh_is_coalesced_without_overwriting_owner(self) -> None:
        marker = maintenance_path(self.instance)
        write_maintenance(marker, {"operation_id": "fc-existing", "source": "/old"})
        with patch("fulcrum.source_refresh.build_installed_environment") as build:
            result = SourceRefreshService().update(self.request())
        self.assertEqual(result.state, CommandState.ACCEPTED)
        self.assertEqual(
            json.loads(marker.read_text(encoding="utf-8"))["operation_id"],
            "fc-existing",
        )
        build.assert_not_called()


class Fulcrum2RecoveryEnvironmentTest(unittest.TestCase):
    def test_recovery_environment_ignores_checkout_and_inherited_pythonpath(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            deployment = root / "recovery" / "deployment-fixture"
            facts = build_installed_environment(
                Path(__file__).parents[1],
                deployment,
                config_path=None,
                recovery=True,
            )
            self.assertTrue(facts["isolated"])
            inspected = probe_installed_environment(
                deployment, config_path=None, recovery=True
            )
            self.assertTrue(inspected["isolated"])
            switch_installed_pointer(root / "recovery", deployment)
            launcher = install_recovery_link(root, production=False)
            environment = dict(os.environ)
            poisoned = root / "broken-main-import" / "fulcrum"
            poisoned.mkdir(parents=True)
            (poisoned / "__init__.py").write_text(
                "raise RuntimeError('inherited checkout import used')\n",
                encoding="utf-8",
            )
            environment["PYTHONPATH"] = str(poisoned.parent)
            completed = subprocess.run(
                [str(launcher), "--help"],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("inspect", completed.stdout)
            brain = root / "brain"
            instance = root / "instance"
            brain.mkdir()
            instance.mkdir()
            config = default_config(brain)
            config["runtime"] = {
                "kind": "deterministic",
                "endpoint": "ws://127.0.0.1:1",
                "executable": None,
            }
            config["delivery"] = {
                "kind": "deterministic",
                "executable": None,
            }
            config_path = brain / "fulcrum.yaml"
            yaml = ConfigurationManager.yaml()
            with config_path.open("w", encoding="utf-8") as output:
                yaml.dump(config, output)
            (instance / "config").symlink_to(config_path)
            inspected_scope = subprocess.run(
                [
                    str(launcher),
                    "inspect",
                    "--scope",
                    "instance",
                    "--instance",
                    str(instance),
                    "--json",
                ],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(
                inspected_scope.returncode,
                6,
                inspected_scope.stderr + inspected_scope.stdout,
            )
            result = json.loads(inspected_scope.stdout)
            self.assertEqual(result["state"], "degraded")
            self.assertFalse(result["result"]["durable_receipt"])
            repair_input = root / "repair.json"
            repair_input.write_text(
                json.dumps(
                    {
                        "actions": [
                            {
                                "action": "reinstall",
                                "target": str(instance / "recovery"),
                                "arguments": {
                                    "installation": "recovery",
                                    "source_root": str(Path(__file__).parents[1]),
                                },
                                "reason": "prove no-ledger isolated recovery reinstall",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            repaired = subprocess.run(
                [
                    str(launcher),
                    "repair",
                    "--scope",
                    "instance",
                    "--instance",
                    str(instance),
                    "--actor",
                    "human",
                    "--request-id",
                    str(uuid.uuid4()),
                    "--input",
                    str(repair_input),
                    "--json",
                ],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            self.assertEqual(repaired.returncode, 6, repaired.stderr + repaired.stdout)
            repaired_result = json.loads(repaired.stdout)
            self.assertEqual(repaired_result["state"], "degraded")
            effect = repaired_result["result"]["effects"][0]["effect"]
            self.assertEqual(effect["installation"], "recovery")
            self.assertFalse(effect["development_environment_used"])
            self.assertTrue(Path(effect["active"]).is_dir())


if __name__ == "__main__":
    unittest.main()
