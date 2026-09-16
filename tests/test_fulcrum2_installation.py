from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from tests import local_launcher_stub

from fulcrum.configuration import default_config
from fulcrum.contracts import (
    ActorContext,
    CommandResult,
    CommandState,
    InstanceContext,
    ParsedRequest,
)
from fulcrum.install import (
    HUMAN_SKILLS,
    InstalledService,
    InstallationError,
    ServiceObservation,
    fulcrum2_service_definitions,
    install_fulcrum2_service_definitions,
    reconcile_fulcrum2_skills,
)
from fulcrum.installation_service import (
    _service_child_failure,
    _start_one,
    _wait_for_controller,
    service_status_result,
)
from fulcrum.setup import (
    _missing_required,
    _model_capability,
    _prepare_configuration,
    _provider_id,
    _runtime_and_leadership,
    _stop_controller_for_setup,
    _stop_new_setup_services,
    _setup_operation_result,
    _setup_writer_lock,
)


class Fulcrum2InstallationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.brain = self.root / "brain"
        self.instance = self.root / "instance"
        self.brain.mkdir()
        self.instance.mkdir()
        self.config_path = self.brain / "fulcrum.yaml"
        self.config = default_config(self.brain)
        self.config["runtime"] = {
            "kind": "codex",
            "endpoint": "ws://127.0.0.1:48765",
            "executable": "/usr/bin/true",
        }
        self.config["delivery"] = {
            "kind": "tollgate",
            "executable": "/usr/bin/true",
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def request(self) -> ParsedRequest:
        return ParsedRequest(
            command=("service", "status"),
            arguments={},
            input={},
            actor=ActorContext(kind="human"),
            instance=InstanceContext(
                instance_root=self.instance,
                config_path=self.config_path,
                brain_root=self.brain,
                socket_path=self.instance / "controller.sock",
                lock_path=self.brain / ".fulcrum-controller.lock",
                explicit_selection=True,
            ),
            request_id=str(uuid.uuid4()),
        )

    @patch("fulcrum.install.shutil.which", new=lambda _name: "/usr/bin/true")
    def test_service_identity_is_stable_unique_and_definitions_are_rerunnable(
        self,
    ) -> None:
        self.config["source_watch_root"] = str(Path(__file__).parents[1])
        first = fulcrum2_service_definitions(
            instance_root=self.instance,
            config_path=self.config_path,
            brain_root=self.brain,
            config=self.config,
            controller_executable=Path("/usr/bin/true"),
            production=True,
        )
        second = fulcrum2_service_definitions(
            instance_root=self.instance,
            config_path=self.config_path,
            brain_root=self.brain,
            config=self.config,
            controller_executable=Path("/usr/bin/true"),
            production=True,
        )
        self.assertEqual(first, second)
        self.assertEqual(first["runtime"]["SoftResourceLimits"]["NumberOfFiles"], 4096)
        self.assertNotIn("updater", first)
        installed, changed = install_fulcrum2_service_definitions(first, self.instance)
        repeated, repeated_changed = install_fulcrum2_service_definitions(
            second, self.instance
        )
        self.assertEqual(set(installed), {"runtime", "dolt", "controller"})
        self.assertEqual(set(repeated), set(installed))
        self.assertEqual(set(changed), set(installed))
        self.assertEqual(repeated_changed, [])

    def test_skill_reconciliation_repairs_owned_links_and_refuses_user_directory(
        self,
    ) -> None:
        root = self.root / "skills"
        checkout = self.root / "fulcrum"
        checkout.mkdir()
        (checkout / "pyproject.toml").write_text("[project]\nname='fulcrum'\n")
        for name in HUMAN_SKILLS:
            skill = checkout / "skills" / name
            (skill / "agents").mkdir(parents=True)
            (skill / "SKILL.md").write_text(name)
            (skill / "agents/openai.yaml").write_text("interface: {}")
        legacy_skill = checkout / "skills" / "fulcrum-weaver"
        legacy_skill.mkdir()
        owned_legacy = root / "fulcrum-weaver"
        root.mkdir()
        owned_legacy.symlink_to(legacy_skill, target_is_directory=True)
        executable = checkout / ".venv" / "bin" / "fulcrum"
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o700)
        result = reconcile_fulcrum2_skills(
            self.instance,
            production=False,
            skills_root=root,
            source_root=checkout,
        )
        self.assertEqual(len(HUMAN_SKILLS), 9)
        self.assertEqual(len(result["installed"]), 9)
        weaver = root / "weaver"
        self.assertTrue(weaver.is_symlink())
        self.assertTrue(weaver.readlink().is_absolute())
        self.assertEqual(weaver.readlink(), checkout / "skills" / "weaver")
        self.assertIn(str(owned_legacy), result["removed"])
        self.assertFalse(owned_legacy.is_symlink())
        self.assertTrue(result["hook"]["installed"])
        hooks = (root.parent / "hooks.json").read_text(encoding="utf-8")
        self.assertIn("hook context --input - --instance", hooks)
        self.assertNotIn("fulcrum.hook", hooks)
        broken = root / HUMAN_SKILLS[0]
        broken.unlink()
        broken.symlink_to(self.root / "missing")
        repaired = reconcile_fulcrum2_skills(
            self.instance,
            production=False,
            skills_root=root,
            source_root=checkout,
        )
        self.assertIn(str(broken), repaired["installed"])
        broken_legacy = root / "fulcrum-weaver"
        broken_legacy.symlink_to(self.root / "missing-legacy", target_is_directory=True)
        cleaned = reconcile_fulcrum2_skills(
            self.instance,
            production=False,
            skills_root=root,
            source_root=checkout,
        )
        self.assertIn(str(broken_legacy), cleaned["removed"])
        self.assertFalse(broken_legacy.is_symlink())
        broken_legacy.mkdir()
        preserved = reconcile_fulcrum2_skills(
            self.instance,
            production=False,
            skills_root=root,
            source_root=checkout,
        )
        self.assertNotIn(str(broken_legacy), preserved["removed"])
        self.assertTrue(broken_legacy.is_dir())
        conflict = root / "weaver"
        conflict.unlink()
        conflict.mkdir()
        with self.assertRaisesRegex(InstallationError, "real user skill directory"):
            reconcile_fulcrum2_skills(
                self.instance,
                production=False,
                skills_root=root,
                source_root=checkout,
            )

    def test_existing_setup_without_patch_preserves_yaml_bytes(self) -> None:
        self.config_path.write_text(
            f"# retained\nbrain:\n  root: {self.brain}\n", encoding="utf-8"
        )
        before = self.config_path.read_bytes()
        effective, changed = _prepare_configuration(
            self.config_path, self.brain, {}, non_interactive=True
        )
        self.assertFalse(changed)
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertEqual(effective["brain"]["root"], str(self.brain))

    def test_new_setup_defaults_source_watch_to_installation_source(self) -> None:
        master = self.root / "canonical-master"
        master.mkdir()
        with patch("fulcrum.setup.master_source_root", return_value=master):
            effective, changed = _prepare_configuration(
                self.config_path,
                self.brain,
                {
                    "beads": {"executable": "/usr/bin/true"},
                    "runtime": {
                        "kind": "codex",
                        "endpoint": "ws://127.0.0.1:1",
                        "executable": "/usr/bin/true",
                    },
                    "delivery": {"kind": "tollgate", "executable": "/usr/bin/true"},
                },
                non_interactive=True,
            )
        self.assertTrue(changed)
        self.assertEqual(
            Path(str(effective["source"]["repository"])).resolve(),
            master.resolve(),
        )

    def test_missing_prerequisites_and_wrong_model_are_exact(self) -> None:
        missing = default_config(self.brain)
        missing["beads"]["executable"] = None
        missing["runtime"]["executable"] = None
        missing["delivery"]["executable"] = None
        self.assertEqual(
            _missing_required(missing),
            ["beads.executable", "runtime.executable", "delivery.executable"],
        )
        models = _model_capability(
            self.config,
            {
                "available": True,
                "models": {"gpt-5.6-sol": ["low"]},
            },
        )
        self.assertFalse(models["available"])
        self.assertEqual(len(models["invalid"]), 8)

    def test_setup_reads_current_tollgate_repository_identity(self) -> None:
        self.assertEqual(
            _provider_id({"state": {"id": "repo-current"}}), "repo-current"
        )

    def test_setup_waits_for_in_flight_reconciliation_before_stopping(self) -> None:
        from fulcrum.contracts import FulcrumError

        busy = FulcrumError(
            "OPERATION_BUSY",
            "operation is already executing",
            exit_code=3,
            retryable=True,
        )
        stopped = MagicMock(operation_id="fc-stop")
        with (
            patch(
                "fulcrum.setup.ServiceService.stop",
                side_effect=[busy, busy, stopped],
            ) as stop,
            patch("fulcrum.setup.time.sleep") as sleep,
        ):
            operation_id = _stop_controller_for_setup(self.request())
        self.assertEqual(operation_id, "fc-stop")
        self.assertEqual(stop.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_setup_waits_for_final_worker_before_taking_writer_lock(self) -> None:
        from fulcrum.contracts import FulcrumError

        class Gate:
            attempts = 0
            released = False

            def __init__(self, _path):
                pass

            def acquire(self):
                Gate.attempts += 1
                if Gate.attempts < 3:
                    raise FulcrumError(
                        "WRITER_BUSY",
                        "writer busy",
                        exit_code=4,
                        retryable=True,
                    )
                return self

            def release(self):
                Gate.released = True

        with (
            patch("fulcrum.setup.WriterLock", Gate),
            patch("fulcrum.setup.time.sleep") as sleep,
            _setup_writer_lock(self.brain / "maintenance", 1.0),
        ):
            self.assertEqual(Gate.attempts, 3)
        self.assertTrue(Gate.released)
        self.assertEqual(sleep.call_count, 2)

    def test_setup_creates_idle_leadership_without_model_turns(self) -> None:
        request = self.request()
        ledger = MagicMock()
        capability = MagicMock()
        capability.to_dict.return_value = {"available": True}
        runtime = MagicMock()
        runtime.connect = AsyncMock()
        runtime.capabilities = AsyncMock(return_value=capability)
        runtime.close = AsyncMock()
        with (
            patch("fulcrum.setup.AppServerRuntime", return_value=runtime),
            patch(
                "fulcrum.setup.ensure_leadership",
                new_callable=AsyncMock,
                return_value=[],
            ) as ensure,
        ):
            _runtime_and_leadership(request, ledger, self.config, {})
        ensure.assert_awaited_once_with(
            request,
            ledger,
            runtime,
            self.config,
            send_initial_requests=False,
        )

    def test_failed_setup_stops_only_services_started_by_that_attempt(self) -> None:
        runtime = MagicMock()
        dolt = MagicMock()
        with patch(
            "fulcrum.setup._stop_one",
            side_effect=lambda service: {"state": "stopped", "service": service},
        ) as stop:
            result = _stop_new_setup_services(
                {"runtime": runtime, "dolt": dolt},
                {"runtime": True, "dolt": False},
            )
        stop.assert_called_once_with(dolt)
        self.assertEqual(result["dolt"]["state"], "stopped")
        self.assertNotIn("runtime", result)

    def test_failed_setup_result_exposes_capability_error(self) -> None:
        operation = MagicMock(id="fc-operation")
        operation.operation = {
            "state": "failed",
            "request_id": "request",
            "error": {
                "code": "CAPABILITY_UNAVAILABLE",
                "message": "runtime is unavailable",
                "retryable": True,
            },
        }
        result = _setup_operation_result(operation)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "CAPABILITY_UNAVAILABLE")

    def test_desktop_launcher_uses_fulcrum_runtime_command(self) -> None:
        checkout = self.root / "checkout"
        script = checkout / "scripts" / "launch_codex.sh"
        executable = checkout / ".venv" / "bin" / "fulcrum"
        script.parent.mkdir(parents=True)
        executable.parent.mkdir(parents=True)
        script.write_bytes(
            (Path(__file__).parents[1] / "scripts" / "launch_codex.sh").read_bytes()
        )
        script.chmod(0o700)
        executable.write_text('#!/bin/sh\nprintf "<%s>\\n" "$@"\n', encoding="utf-8")
        executable.chmod(0o700)

        with local_launcher_stub(script):
            launched = subprocess.run(
                [str(script), "--json"], capture_output=True, text=True, check=False
            )

        self.assertEqual(launched.returncode, 0, launched.stderr)
        self.assertEqual(launched.stdout, "<runtime>\n<launch-desktop>\n<--json>\n")

    def test_occupied_port_is_reported_without_killing_or_starting(self) -> None:
        service = InstalledService("dolt", "dev.fulcrum.test.dolt", self.root / "x")
        observation = ServiceObservation(
            label=service.label,
            loaded=False,
            state=None,
            pid=None,
            executable_path=None,
            program_arguments=(),
            working_directory=None,
            detail="not loaded",
        )
        with (
            patch(
                "fulcrum.installation_service.inspect_service",
                return_value=observation,
            ),
            patch("fulcrum.installation_service._occupied", return_value=True),
            patch("fulcrum.installation_service.subprocess.run") as run,
        ):
            with self.assertRaisesRegex(Exception, "occupied"):
                _start_one(service, endpoint="tcp://127.0.0.1:48765")
            run.assert_not_called()

    @patch("fulcrum.install.shutil.which", new=lambda _name: "/usr/bin/true")
    def test_status_is_socket_independent_and_uses_owned_artifacts(self) -> None:
        definitions = fulcrum2_service_definitions(
            instance_root=self.instance,
            config_path=self.config_path,
            brain_root=self.brain,
            config=self.config,
            controller_executable=Path("/usr/bin/true"),
            production=False,
        )
        install_fulcrum2_service_definitions(definitions, self.instance)
        observation = ServiceObservation(
            label="test",
            loaded=True,
            state="running",
            pid=os.getpid(),
            executable_path=None,
            program_arguments=(),
            working_directory=None,
            detail="observed",
        )
        with (
            patch(
                "fulcrum.installation_service.inspect_service",
                return_value=observation,
            ),
            patch(
                "fulcrum.resident_client.exchange",
                return_value={
                    "pid": os.getpid(),
                    "process_commit": "controller-commit",
                    "connected": True,
                    "pending": {},
                },
            ),
            patch.dict(os.environ, {"FULCRUM_COMMIT": "client-commit"}, clear=False),
        ):
            result = service_status_result(self.request())
        self.assertFalse(result["socket"]["exists"])
        self.assertEqual(set(result["services"]), {"dolt", "controller"})
        self.assertTrue(all(row["running"] for row in result["services"].values()))
        self.assertTrue(result["responsive"])
        self.assertTrue(result["revisions"]["skew"])
        self.assertEqual(result["revisions"]["client"], "client-commit")
        self.assertEqual(result["revisions"]["controller"], "controller-commit")
        self.assertIn("revisions differ", result["gaps"][-1])

    def test_controller_readiness_requires_a_connectable_unix_socket(self) -> None:
        path = self.root / "ready.sock"
        service = InstalledService(
            "controller", "dev.fulcrum.test.controller", self.root / "x"
        )
        observation = ServiceObservation(
            label=service.label,
            loaded=True,
            state="running",
            pid=os.getpid(),
            executable_path=None,
            program_arguments=(),
            working_directory=None,
            detail="observed",
        )
        with (
            patch(
                "fulcrum.installation_service.inspect_service",
                return_value=observation,
            ),
            patch(
                "fulcrum.resident_client.exchange",
                return_value={"ok": True, "state": "completed", "result": {}},
            ) as probe,
        ):
            result = _wait_for_controller(
                self.request(),
                path,
                service,
                timeout=0.5,
                stability_seconds=0.05,
            )
        self.assertTrue(result["responsive"])
        self.assertEqual(result["socket"], str(path))
        self.assertEqual(result["probe_state"], "completed")
        probe.assert_awaited_once()
        self.assertEqual(probe.call_args.args[1]["action"], "health")

    def test_restart_child_failure_is_not_treated_as_success(self) -> None:
        completed = CommandResult(ok=True, state=CommandState.COMPLETED)
        failed = CommandResult(
            ok=False,
            state=CommandState.FAILED,
            result={
                "error": {
                    "code": "CONTROLLER_UNAVAILABLE",
                    "message": "controller exited during readiness",
                    "retryable": True,
                }
            },
        )

        self.assertIsNone(_service_child_failure(completed, "start"))
        self.assertEqual(
            _service_child_failure(failed, "start"),
            {
                "code": "CONTROLLER_UNAVAILABLE",
                "message": "controller exited during readiness",
                "retryable": True,
            },
        )


if __name__ == "__main__":
    unittest.main()
