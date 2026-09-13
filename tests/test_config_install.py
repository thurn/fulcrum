from __future__ import annotations

import json
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fulcrum.config import (
    InstallationConfig,
    ProjectConfig,
    RuntimePaths,
    load_installation,
    resolve_paths,
    save_installation,
)
from fulcrum.install import (
    APP_SERVER_LABEL,
    CONTROLLER_LABEL,
    _package_contents,
    _service_observation_from_result,
    control_plane_source,
    install_control_plane,
    install_hook_config,
    install_services,
    service_definitions,
    service_executable_path,
    start_services,
)
from fulcrum.setup import _wait_for_archon_readiness


class ConfigInstallTest(unittest.TestCase):
    def test_setup_polls_initialization_until_archon_is_ready(self) -> None:
        paths = RuntimePaths(
            brain_root=Path("/brain"),
            state_root=Path("/state"),
            config_file=Path("/config"),
            control_root=Path("/control"),
        )
        responses = [
            {"data": {"ready": False, "condition": "materializing"}},
            {"data": {"ready": True, "archon": "thread-1"}},
        ]
        with (
            patch("fulcrum.setup.request_sync", side_effect=responses) as request,
            patch("fulcrum.setup.time.monotonic", side_effect=[0.0, 0.1, 0.2]),
            patch("fulcrum.setup.time.sleep"),
        ):
            result = _wait_for_archon_readiness(paths, timeout=1)
        self.assertEqual(result, responses[-1]["data"])
        self.assertEqual(request.call_count, 2)

    def test_round_trip_has_no_format_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.json"
            config = InstallationConfig(
                source_root="/source",
                brain_root="/brain",
                state_root="/state",
                codex_bin="/bin/codex",
                desktop_executable="/Applications/ChatGPT.app/ChatGPT",
                projects=[ProjectConfig("p", "/repo")],
            )
            save_installation(path, config)
            raw = json.loads(path.read_text())
            self.assertNotIn("version", raw)
            self.assertNotIn("schema_version", raw)
            self.assertEqual(load_installation(path, home=root), config)

    def test_paths_keep_control_outside_reset_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = resolve_paths(environ={}, user_home=home)
            self.assertEqual(paths.database.name, "fulcrum.sqlite3")
            self.assertEqual(paths.control_root.name, "control")
            self.assertNotEqual(paths.database.parent, paths.control_root)

    def test_hook_merge_removes_all_old_fulcrum_handlers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hooks.json"
            path.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "Stop": [
                                {
                                    "hooks": [
                                        {
                                            "statusMessage": "Fulcrum: old stop",
                                            "command": "old",
                                        },
                                        {
                                            "statusMessage": "Unrelated",
                                            "command": "keep",
                                        },
                                    ]
                                }
                            ]
                        }
                    }
                )
            )
            install_hook_config(path, Path("/linked/hook"))
            result = json.loads(path.read_text())
            self.assertEqual(result["hooks"]["Stop"][0]["hooks"][0]["command"], "keep")
            self.assertEqual(len(result["hooks"]["SessionStart"]), 1)
            self.assertNotIn("PreToolUse", result["hooks"])

    def test_services_are_separate_and_controller_does_not_spawn_server(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = InstallationConfig(
                source_root=str(root),
                brain_root=str(root / "brain"),
                state_root=str(root / "state"),
                codex_bin="/bin/codex",
                desktop_executable="/Applications/ChatGPT.app/ChatGPT",
            )
            paths = resolve_paths(
                state_override=root / "state",
                environ={"FULCRUM_CONFIG": str(root / "config.json")},
                user_home=root,
            )
            home = (root / "home").resolve()
            definitions = service_definitions(config, paths, user_home=home)
            controller = definitions[CONTROLLER_LABEL]["ProgramArguments"]
            self.assertTrue(any("fulcrum.cli" in item for item in controller))
            self.assertIn("-I", controller)
            self.assertNotIn("app-server", controller)
            self.assertEqual(
                definitions[CONTROLLER_LABEL]["WorkingDirectory"],
                str(paths.control_root),
            )
            expected_path = service_executable_path(user_home=home)
            self.assertEqual(
                definitions[CONTROLLER_LABEL]["EnvironmentVariables"]["PATH"],
                expected_path,
            )
            self.assertEqual(
                definitions[APP_SERVER_LABEL]["EnvironmentVariables"]["PATH"],
                expected_path,
            )
            self.assertTrue(expected_path.startswith(f"{home}/.local/bin:{home}/bin:"))
            self.assertIn("/usr/bin:/bin:/usr/sbin:/sbin", expected_path)

    def test_service_install_rerun_reports_only_repaired_plists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = InstallationConfig(
                source_root=str(root),
                brain_root=str(root / "brain"),
                state_root=str(root / "state"),
                codex_bin="/bin/codex",
                desktop_executable="/Applications/ChatGPT.app/ChatGPT",
            )
            paths = resolve_paths(
                state_override=root / "state",
                environ={"FULCRUM_CONFIG": str(root / "config.json")},
                user_home=root / "home",
            )
            agents = root / "LaunchAgents"
            installed, updated = install_services(
                config, paths, launch_agents=agents, user_home=root / "home"
            )
            self.assertEqual(updated, {APP_SERVER_LABEL, CONTROLLER_LABEL})
            with Path(installed[CONTROLLER_LABEL]).open("rb") as handle:
                controller = plistlib.load(handle)
            self.assertEqual(
                controller["EnvironmentVariables"]["PATH"],
                service_executable_path(user_home=root / "home"),
            )

            repeated, repeated_updates = install_services(
                config, paths, launch_agents=agents, user_home=root / "home"
            )
            self.assertEqual(repeated, installed)
            self.assertEqual(repeated_updates, frozenset())

            controller_file = Path(installed[CONTROLLER_LABEL])
            stale = controller.copy()
            stale["EnvironmentVariables"].pop("PATH")
            with controller_file.open("wb") as handle:
                plistlib.dump(stale, handle)
            _, repaired = install_services(
                config, paths, launch_agents=agents, user_home=root / "home"
            )
            self.assertEqual(repaired, {CONTROLLER_LABEL})

    def test_start_services_reloads_only_updated_loaded_definitions(self) -> None:
        definitions = {
            APP_SERVER_LABEL: "/tmp/app-server.plist",
            CONTROLLER_LABEL: "/tmp/controller.plist",
        }
        controller_booted_out = False
        controller_bootstrapped = False
        removal_polls = 0
        events: list[str] = []

        def completed(
            command: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess:
            nonlocal controller_booted_out, controller_bootstrapped, removal_polls
            operation = command[1]
            label = command[-1].rsplit("/", 1)[-1]
            if operation == "bootout":
                controller_booted_out = True
                events.append("bootout")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            if operation == "bootstrap":
                self.assertGreaterEqual(removal_polls, 3)
                controller_bootstrapped = True
                events.append("bootstrap")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            if (
                operation == "print"
                and label == CONTROLLER_LABEL
                and controller_booted_out
                and not controller_bootstrapped
            ):
                removal_polls += 1
                if removal_polls < 3:
                    events.append("still-loaded")
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout="\tstate = SIGTERMed\n\tpid = 123\n",
                        stderr="",
                    )
                events.append("unloaded")
                return subprocess.CompletedProcess(
                    command, 1, stdout="", stderr="absent"
                )
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="\tstate = running\n\tpid = 123\n",
                stderr="",
            )

        with (
            patch("fulcrum.install.os.getuid", return_value=501),
            patch("fulcrum.install.subprocess.run", side_effect=completed) as run,
        ):
            start_services(definitions, updated=frozenset({CONTROLLER_LABEL}))

        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(
            ["launchctl", "kickstart", f"gui/501/{APP_SERVER_LABEL}"], commands
        )
        self.assertIn(["launchctl", "bootout", f"gui/501/{CONTROLLER_LABEL}"], commands)
        self.assertIn(
            ["launchctl", "bootstrap", "gui/501", "/tmp/controller.plist"],
            commands,
        )
        self.assertNotIn(
            ["launchctl", "kickstart", f"gui/501/{CONTROLLER_LABEL}"], commands
        )
        self.assertEqual(
            events,
            ["bootout", "still-loaded", "still-loaded", "unloaded", "bootstrap"],
        )

    def test_start_services_repairs_a_loaded_job_after_a_partial_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            definition = root / "controller.plist"
            with definition.open("wb") as handle:
                plistlib.dump(
                    {"EnvironmentVariables": {"PATH": "/expected/bin:/usr/bin"}},
                    handle,
                )
            definitions = {
                APP_SERVER_LABEL: str(root / "missing-app-server.plist"),
                CONTROLLER_LABEL: str(definition),
            }
            repaired = False
            booted_out = False

            def completed(
                command: list[str], **_kwargs: object
            ) -> subprocess.CompletedProcess:
                nonlocal repaired, booted_out
                if command[1] == "bootout":
                    booted_out = True
                    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
                if command[1] == "bootstrap":
                    repaired = True
                if command[1] == "print" and booted_out and not repaired:
                    return subprocess.CompletedProcess(
                        command, 1, stdout="", stderr="absent"
                    )
                path = "/expected/bin:/usr/bin" if repaired else "/stale/bin:/usr/bin"
                output = (
                    "\tstate = running\n\tpid = 123\n"
                    f"\tenvironment = {{\n\t\tPATH => {path}\n\t}}\n"
                )
                return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

            with (
                patch("fulcrum.install.os.getuid", return_value=501),
                patch("fulcrum.install.subprocess.run", side_effect=completed) as run,
            ):
                start_services(definitions, updated=frozenset())

            commands = [call.args[0] for call in run.call_args_list]
            self.assertIn(
                ["launchctl", "bootout", f"gui/501/{CONTROLLER_LABEL}"], commands
            )
            self.assertIn(
                ["launchctl", "bootstrap", "gui/501", str(definition)], commands
            )

    def test_control_plane_is_an_atomic_snapshot_outside_managed_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            package = source / "src" / "fulcrum"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("VALUE = 1\n")
            paths = resolve_paths(
                state_override=root / "state",
                environ={"FULCRUM_CONFIG": str(root / "config.json")},
                user_home=root / "home",
            )
            config = InstallationConfig(
                source_root=str(source),
                brain_root=str(root / "brain"),
                state_root=str(paths.state_root),
                codex_bin="/bin/codex",
                desktop_executable="/Applications/ChatGPT.app/ChatGPT",
            )
            first, first_updated = install_control_plane(config, paths)
            first_deployment = first.parent.resolve()
            self.assertTrue(first_updated)
            self.assertEqual(first, control_plane_source(paths))
            self.assertEqual((first / "__init__.py").read_text(), "VALUE = 1\n")
            (package / "__init__.py").write_text("VALUE = 2\n")
            second, second_updated = install_control_plane(config, paths)
            second_deployment = second.parent.resolve()
            self.assertTrue(second_updated)
            self.assertEqual(second, first)
            self.assertEqual((second / "__init__.py").read_text(), "VALUE = 2\n")
            self.assertFalse(second.is_relative_to(source))
            self.assertNotEqual(first_deployment, second_deployment)
            self.assertFalse(first_deployment.exists())
            third, third_updated = install_control_plane(config, paths)
            self.assertEqual(third, second)
            self.assertFalse(third_updated)

    def test_control_plane_retries_if_source_changes_during_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            package = source / "src" / "fulcrum"
            package.mkdir(parents=True)
            module = package / "__init__.py"
            module.write_text("VALUE = 1\n")
            paths = resolve_paths(
                state_override=root / "state",
                environ={"FULCRUM_CONFIG": str(root / "config.json")},
                user_home=root / "home",
            )
            config = InstallationConfig(
                source_root=str(source),
                brain_root=str(root / "brain"),
                state_root=str(paths.state_root),
                codex_bin="/bin/codex",
                desktop_executable="/Applications/ChatGPT.app/ChatGPT",
            )
            source_reads = 0

            def changing_contents(target: Path) -> dict[Path, bytes]:
                nonlocal source_reads
                is_source = target.resolve() == package.resolve()
                if is_source:
                    source_reads += 1
                if is_source and source_reads == 2:
                    module.write_text("VALUE = 2\n")
                return _package_contents(target)

            with patch(
                "fulcrum.install._package_contents", side_effect=changing_contents
            ):
                deployed, updated = install_control_plane(config, paths)

            self.assertEqual(source_reads, 4)
            self.assertTrue(updated)
            self.assertEqual((deployed / "__init__.py").read_text(), "VALUE = 2\n")

    def test_transient_bootstrap_failure_is_observed_then_retried(self) -> None:
        definitions = {
            APP_SERVER_LABEL: "/tmp/app-server.plist",
            CONTROLLER_LABEL: "/tmp/controller.plist",
        }
        calls: dict[str, int] = {APP_SERVER_LABEL: 0, CONTROLLER_LABEL: 0}

        def completed(
            command: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess:
            if command[1] == "print":
                label = command[-1].rsplit("/", 1)[-1]
                if calls[label] >= 2:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout="\tstate = running\n\tpid = 123\n",
                        stderr="",
                    )
                return subprocess.CompletedProcess(
                    command, 1, stdout="", stderr="absent"
                )
            label = (
                APP_SERVER_LABEL
                if command[-1] == definitions[APP_SERVER_LABEL]
                else CONTROLLER_LABEL
            )
            calls[label] += 1
            code = 5 if calls[label] == 1 else 0
            return subprocess.CompletedProcess(
                command, code, stdout="", stderr="transient" if code else ""
            )

        with (
            patch("fulcrum.install.os.getuid", return_value=501),
            patch("fulcrum.install.subprocess.run", side_effect=completed),
        ):
            start_services(definitions, updated=frozenset())
        self.assertEqual(calls, {APP_SERVER_LABEL: 2, CONTROLLER_LABEL: 2})

    def test_launchctl_observation_ignores_inherited_environment(self) -> None:
        output = """gui/501/dev.fulcrum.controller = {
\tstate = running
\targuments = {
\t\t/control/python
\t\t-I
\t\t-c
\t\tlauncher
\t\tserve
\t}
\tworking directory = /control
\tinherited environment = {
\t\tPATH => /wrong
\t}
\tenvironment = {
\t\tPATH => /expected
\t}
\tpid = 321
}
"""
        result = subprocess.CompletedProcess(
            ["launchctl", "print"], 0, stdout=output, stderr=""
        )
        observed = _service_observation_from_result(CONTROLLER_LABEL, result)
        self.assertTrue(observed.running)
        self.assertEqual(observed.executable_path, "/expected")
        self.assertEqual(
            observed.program_arguments,
            ("/control/python", "-I", "-c", "launcher", "serve"),
        )
        self.assertEqual(observed.working_directory, "/control")

    def test_start_services_rejects_an_unmanaged_ready_listener(self) -> None:
        definitions = {
            APP_SERVER_LABEL: "/tmp/app-server.plist",
            CONTROLLER_LABEL: "/tmp/controller.plist",
        }
        absent = subprocess.CompletedProcess(
            ["launchctl", "print"], 1, stdout="", stderr="absent"
        )
        with (
            patch("fulcrum.install.subprocess.run", return_value=absent) as run,
            patch("fulcrum.install._endpoint_is_ready", return_value=True),
        ):
            with self.assertRaisesRegex(
                Exception, "refusing to accept an unmanaged listener"
            ):
                start_services(
                    definitions,
                    updated=frozenset(),
                    app_server_endpoint="ws://127.0.0.1:4500",
                )
        self.assertFalse(
            any(call.args[0][1] == "bootstrap" for call in run.call_args_list)
        )


if __name__ == "__main__":
    unittest.main()
