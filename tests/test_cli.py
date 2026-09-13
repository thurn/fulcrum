from __future__ import annotations

import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fulcrum.beads import Beads
from fulcrum.cli import FINISH_REPORT_HINT, main
from fulcrum.config import InstallationConfig, RuntimePaths
from fulcrum.doctor import human_skill_link_checks
from fulcrum.install import (
    HUMAN_SKILLS,
    REMOVED_SKILLS,
    InstallationError,
    install_links,
    reconcile_skill_links,
)
from fulcrum.intake import report_task_from_payload
from fulcrum.store import StoreError


class CliTest(unittest.TestCase):
    def test_bead_skill_has_the_human_entry_contract_and_only_reports(self) -> None:
        skill = (
            Path(__file__).resolve().parents[1] / "skills" / "fulcrum-bead" / "SKILL.md"
        ).read_text()
        self.assertRegex(skill, r"(?m)^name: bead$")
        commands = "\n".join(re.findall(r"```sh\n(.*?)```", skill, re.DOTALL))
        self.assertIn("fulcrum report --input -", commands)
        self.assertNotIn("fulcrum intake", commands)
        self.assertNotIn("fulcrum finish", commands)
        self.assertNotRegex(commands, r"(?m)^\s*bd(?:\s|$)")
        self.assertIn("$weaver", skill)

    def test_report_help_and_bare_report_are_local_and_self_contained(self) -> None:
        for arguments in (["report", "--help"], ["report"]):
            stdout = io.StringIO()
            with (
                self.subTest(arguments=arguments),
                patch("fulcrum.cli.resolve_paths") as resolve,
                patch("fulcrum.cli.request_sync") as request,
                patch("sys.stdout", new=stdout),
            ):
                if "--help" in arguments:
                    with self.assertRaises(SystemExit) as stopped:
                        main(arguments)
                    self.assertEqual(stopped.exception.code, 0)
                else:
                    self.assertEqual(main(arguments), 0)
            resolve.assert_not_called()
            request.assert_not_called()
            guidance = stdout.getvalue()
            self.assertIn("pre-existing", guidance)
            self.assertIn("tooling failure", guidance)
            self.assertIn("workflow friction", guidance)
            self.assertIn("$bead", guidance)
            self.assertIn("$weaver", guidance)
            self.assertIn('"observed_evidence"', guidance)
            self.assertIn("fulcrum report --input - <<'JSON'", guidance)
            self.assertIn("separate report invocations", guidance)

    def test_report_snapshots_stdin_json_for_the_controller(self) -> None:
        root = Path("/tmp/report-cli-test")
        paths = RuntimePaths(root, root, root / "config.json", root / "control")
        report = {
            "report_key": "one",
            "title": "Small defect",
            "problem": "A problem",
            "observed_evidence": "An observation",
            "required_change": "A bounded fix",
            "acceptance_checks": ["A check passes"],
        }
        with (
            patch("fulcrum.cli.resolve_paths", return_value=paths),
            patch("fulcrum.cli._thread_id", return_value="ordinary-thread"),
            patch(
                "fulcrum.cli.request_sync",
                return_value={
                    "data": {"bead_id": "p-2", "publication_state": "complete"}
                },
            ) as request,
            patch("sys.stdin", new=io.StringIO(json.dumps(report))),
            patch("sys.stdout", new=io.StringIO()),
        ):
            self.assertEqual(main(["report", "--input", "-"]), 0)
        request.assert_called_once_with(
            paths.socket,
            {
                "command": "report",
                "report": report,
                "thread_id": "ordinary-thread",
            },
        )

    def test_finish_hint_is_emitted_only_for_accepted_and_reused_outcomes(self) -> None:
        root = Path("/tmp/finish-cli-test")
        paths = RuntimePaths(root, root, root / "config.json", root / "control")
        for reused in (False, True):
            stdout = io.StringIO()
            with (
                self.subTest(reused=reused),
                patch("fulcrum.cli.resolve_paths", return_value=paths),
                patch(
                    "fulcrum.cli._request",
                    return_value={"ok": True, "reused": reused},
                ),
                patch("sys.stdout", new=stdout),
            ):
                self.assertEqual(
                    main(["finish", "ready_for_review", "--evidence", "/tmp/e"]),
                    0,
                )
            self.assertEqual(stdout.getvalue().count(FINISH_REPORT_HINT), 1)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch("fulcrum.cli.resolve_paths", return_value=paths),
            patch("fulcrum.cli._request", side_effect=StoreError("rejected")),
            patch("sys.stdout", new=stdout),
            patch("sys.stderr", new=stderr),
        ):
            self.assertEqual(
                main(["finish", "ready_for_review", "--evidence", "/tmp/e"]), 2
            )
        self.assertNotIn(FINISH_REPORT_HINT, stdout.getvalue())
        self.assertNotIn(FINISH_REPORT_HINT, stderr.getvalue())

    def test_bead_skill_is_installed_without_overwriting_user_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            codex = root / "codex"
            for name in HUMAN_SKILLS:
                skill = source / "skills" / name
                skill.mkdir(parents=True)
                skill.joinpath("SKILL.md").write_text(f"---\nname: {name}\n---\n")
            hook = source / "hooks" / "fulcrum-hook"
            hook.parent.mkdir(parents=True)
            hook.write_text("#!/bin/sh\n")
            hook.chmod(0o700)
            cli = source / ".venv" / "bin" / "fulcrum"
            cli.parent.mkdir(parents=True)
            cli.write_text("#!/bin/sh\n")
            cli.chmod(0o700)
            config = InstallationConfig(
                source_root=str(source),
                brain_root=str(root / "brain"),
                state_root=str(root / "state"),
                codex_bin="/bin/codex",
                desktop_executable="/Applications/ChatGPT.app/ChatGPT",
                projects=[],
            )

            result = install_links(config, codex_root=codex)
            bead_link = codex / "skills" / "fulcrum-bead"
            self.assertTrue(bead_link.is_symlink())
            self.assertIn(str(bead_link), result["skills"])
            checks = human_skill_link_checks(source, codex_root=codex / "skills")
            self.assertTrue(all(item["ok"] for item in checks))
            self.assertIn("skill:fulcrum-bead", {item["name"] for item in checks})

            bead_link.unlink()
            bead_link.mkdir()
            bead_link.joinpath("notes.txt").write_text("keep me")
            with self.assertRaisesRegex(InstallationError, "non-symlink"):
                install_links(config, codex_root=codex)
            self.assertEqual(bead_link.joinpath("notes.txt").read_text(), "keep me")
            checks = human_skill_link_checks(source, codex_root=codex / "skills")
            bead_check = next(
                item for item in checks if item["name"] == "skill:fulcrum-bead"
            )
            self.assertFalse(bead_check["ok"])

    def test_skill_reconciliation_repairs_wrong_links_and_preserves_other_skills(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            skills_root = root / "codex-skills"
            smoke = root / "smoke"
            skills_root.mkdir()
            smoke.mkdir()
            for name in HUMAN_SKILLS:
                skill = source / "skills" / name
                skill.mkdir(parents=True)
                skill.joinpath("SKILL.md").write_text(f"---\nname: {name}\n---\n")
            for name in HUMAN_SKILLS[:-1]:
                (skills_root / name).symlink_to(smoke, target_is_directory=True)
            for name in REMOVED_SKILLS:
                (skills_root / name).symlink_to(smoke, target_is_directory=True)
            unrelated = skills_root / "unrelated"
            unrelated.mkdir()

            installed = reconcile_skill_links(source, skills_root=skills_root)

            for name in HUMAN_SKILLS:
                self.assertEqual(
                    (skills_root / name).resolve(strict=True),
                    (source / "skills" / name).resolve(strict=True),
                )
            for name in REMOVED_SKILLS:
                self.assertTrue((skills_root / name).is_symlink())
            self.assertEqual(
                set(installed),
                {str(skills_root / name) for name in HUMAN_SKILLS},
            )
            self.assertTrue(unrelated.is_dir())

    def test_skill_reconciliation_preflights_non_symlink_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            skills_root = root / "codex-skills"
            skills_root.mkdir()
            for name in HUMAN_SKILLS:
                skill = source / "skills" / name
                skill.mkdir(parents=True)
                skill.joinpath("SKILL.md").write_text(f"---\nname: {name}\n---\n")
            conflict = skills_root / HUMAN_SKILLS[-1]
            conflict.mkdir()
            conflict.joinpath("notes.txt").write_text("keep me")

            with self.assertRaisesRegex(InstallationError, "non-symlink"):
                reconcile_skill_links(source, skills_root=skills_root)

            self.assertEqual(conflict.joinpath("notes.txt").read_text(), "keep me")
            for name in HUMAN_SKILLS[:-1]:
                self.assertFalse((skills_root / name).exists())

    def test_report_publication_has_report_label_and_provenance_metadata(self) -> None:
        task = report_task_from_payload(
            {
                "report_key": "metadata-one",
                "title": "Fix metadata case",
                "problem": "Metadata is absent",
                "observed_evidence": "The issue lacks an origin label",
                "required_change": "Add the report origin metadata",
                "acceptance_checks": ["The created issue has the label"],
            },
            project="p",
            provenance={"source_action_id": 17, "source_bead_id": "p-1"},
        )
        beads = Beads(Path("/tmp/brain"), executable="bd")
        with patch.object(beads, "run", return_value={"id": "fc-2"}) as run:
            self.assertEqual(beads.create(task), "fc-2")

        arguments = run.call_args.args[0]
        labels = arguments[arguments.index("--labels") + 1].split(",")
        metadata = json.loads(arguments[arguments.index("--metadata") + 1])
        self.assertIn("origin:fulcrum-report", labels)
        self.assertEqual(metadata["fulcrum"]["report"]["kind"], "follow-up")
        self.assertEqual(
            metadata["fulcrum"]["report"]["provenance"],
            {"source_action_id": 17, "source_bead_id": "p-1"},
        )

    def test_sage_subcommands_send_distinct_controller_requests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = RuntimePaths(root, root, root / "config.json", root / "control")
            with (
                patch("fulcrum.cli.resolve_paths", return_value=paths),
                patch("fulcrum.cli._thread_id", return_value="sage-thread"),
                patch(
                    "fulcrum.cli.request_sync", return_value={"data": {"ok": True}}
                ) as request,
                patch("sys.stdout", new=io.StringIO()),
            ):
                self.assertEqual(
                    main(
                        [
                            "sage",
                            "register",
                            "--item",
                            "p-1",
                            "--description",
                            "Review failed delivery handoff",
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    main(["sage", "request", "--project", "p", "--scope", "focus"]),
                    0,
                )

            self.assertEqual(
                request.call_args_list[0].args[1],
                {
                    "command": "sage_register",
                    "item": "p-1",
                    "description": "Review failed delivery handoff",
                    "thread_id": "sage-thread",
                },
            )
            self.assertEqual(
                request.call_args_list[1].args[1],
                {
                    "command": "specialist",
                    "kind": "sage",
                    "scope": json.dumps({"global": False, "projects": ["p"]}),
                    "prompt": "focus",
                    "thread_id": "sage-thread",
                },
            )

    def test_context_uses_only_the_calling_thread_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = RuntimePaths(root, root, root / "config.json", root / "control")
            with (
                patch("fulcrum.cli.resolve_paths", return_value=paths),
                patch("fulcrum.cli._thread_id", return_value="managed-thread"),
                patch(
                    "fulcrum.cli.request_sync",
                    return_value={"data": {"context": "current action"}},
                ) as request,
                patch("sys.stdout", new=io.StringIO()),
            ):
                result = main(["context"])

            self.assertEqual(result, 0)
            request.assert_called_once_with(
                paths.socket,
                {"command": "context", "thread_id": "managed-thread"},
            )

    def test_finish_rejects_missing_input_before_controller_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = RuntimePaths(root, root, root / "config.json", root / "control")
            missing = paths.handoff_root / "state" / "action-1" / "decisions.json"
            stderr = io.StringIO()
            with (
                patch("fulcrum.cli.resolve_paths", return_value=paths),
                patch("fulcrum.cli._request", return_value={"ok": True}) as request,
                patch("sys.stderr", new=stderr),
            ):
                result = main(["finish", "decisions", "--input", str(missing)])

            self.assertEqual(result, 0)
            request.assert_called_once()
            sent = request.call_args.args[1]["options"]
            self.assertEqual(sent["input_path"], str(missing.resolve(strict=False)))
            self.assertTrue(sent["input_missing"])
            self.assertNotIn("input", sent)
            self.assertEqual(stderr.getvalue(), "")

    def test_finish_sends_complete_input_snapshot_to_controller(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = RuntimePaths(root, root, root / "config.json", root / "control")
            input_path = paths.handoff_root / "state" / "action-1" / "decisions.json"
            input_path.parent.mkdir(parents=True)
            decision = {
                "decisions": [],
                "handled_update_ids": [1],
            }
            input_path.write_text(json.dumps(decision), encoding="utf-8")

            def receive(
                _paths: RuntimePaths, request: dict[str, object]
            ) -> dict[str, object]:
                input_path.write_text("{", encoding="utf-8")
                options = request["options"]
                self.assertEqual(options["input"], decision)
                self.assertEqual(
                    options["input_path"], str(input_path.resolve(strict=False))
                )
                self.assertEqual(
                    set(options["input_identity"]),
                    {"device", "inode", "size", "mtime_ns", "ctime_ns"},
                )
                return {"ok": True}

            with (
                patch("fulcrum.cli.resolve_paths", return_value=paths),
                patch("fulcrum.cli._request", side_effect=receive) as request,
                patch("sys.stdout", new=io.StringIO()),
            ):
                result = main(["finish", "decisions", "--input", str(input_path)])

            self.assertEqual(result, 0)
            request.assert_called_once()

    def test_finish_rejects_relative_outside_and_symlink_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = RuntimePaths(root, root, root / "config.json", root / "control")
            outside = root / "outside.json"
            outside.write_text("{}", encoding="utf-8")
            target = paths.handoff_root / "state" / "action-1" / "decisions.json"
            target.parent.mkdir(parents=True)
            inside = target.with_name("inside.json")
            inside.write_text("{}", encoding="utf-8")
            target.symlink_to(inside)
            for supplied, message in (
                ("decisions.json", "absolute path"),
                (str(outside), "must be beneath"),
                (str(target), "must not be a symlink"),
            ):
                stderr = io.StringIO()
                with (
                    patch("fulcrum.cli.resolve_paths", return_value=paths),
                    patch("fulcrum.cli._request") as request,
                    patch("sys.stderr", new=stderr),
                ):
                    result = main(["finish", "decisions", "--input", supplied])
                self.assertEqual(result, 2)
                request.assert_not_called()
                self.assertIn(message, stderr.getvalue())

    def test_malformed_json_is_retained_without_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = RuntimePaths(root, root, root / "config.json", root / "control")
            target = paths.handoff_root / "state" / "action-1" / "decisions.json"
            target.parent.mkdir(parents=True)
            target.write_text("{", encoding="utf-8")
            with (
                patch("fulcrum.cli.resolve_paths", return_value=paths),
                patch("fulcrum.cli._request") as request,
                patch("sys.stderr", new=io.StringIO()),
            ):
                result = main(["finish", "decisions", "--input", str(target)])
            self.assertEqual(result, 2)
            request.assert_not_called()
            self.assertEqual(target.read_text(encoding="utf-8"), "{")


if __name__ == "__main__":
    unittest.main()
