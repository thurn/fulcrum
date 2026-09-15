from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any

from fulcrum.configuration import ConfigurationManager, default_config
from fulcrum.contracts import ActorContext, FulcrumError, ParsedRequest
from fulcrum.installation_service import ServiceService
from fulcrum.instance import resolve_instance
from fulcrum.ledger import Ledger, operation_id
from fulcrum.reset import ResetBoundaryCrash, ResetService, reset_workspace
from fulcrum.runtime import TaskFacts


def run(*argv: str, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return completed.stdout.strip()


class FakeRuntime:
    def __init__(self) -> None:
        self.exists = {"managed-old": True, "unrelated-native": True}
        self.deleted: list[str] = []

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def inspect_task(self, thread_id: str) -> TaskFacts:
        exists = self.exists.get(thread_id, False)
        return TaskFacts(
            id=thread_id,
            title=thread_id if exists else None,
            cwd=None,
            project_id=None,
            workspace_roots=(),
            archived=False,
            exists=exists,
            loaded=False,
            runtime_status="idle" if exists else "deleted",
            active_turn=None,
            last_turn=None,
            pending_requests=(),
            observed_at="2026-09-15T00:00:00Z",
        )

    async def terminals(
        self, thread_id: str, *, limit: int, cursor: str | None
    ) -> dict[str, Any]:
        return {"items": [], "next_cursor": None}

    async def delete(self, thread_id: str) -> TaskFacts:
        self.exists[thread_id] = False
        self.deleted.append(thread_id)
        return await self.inspect_task(thread_id)


class ResetFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.brain = self.root / "brain"
        self.instance = self.root / "instance"
        self.project = self.root / "project"
        self.worktree = self.root / "managed-worktree"
        self.remote = self.root / "brain-remote.git"
        for path in (self.brain, self.instance, self.project):
            path.mkdir()
        for path in (self.brain, self.project):
            run("git", "init", "--initial-branch=main", str(path))
            run("git", "-C", str(path), "config", "user.email", "fixture@example.com")
            run("git", "-C", str(path), "config", "user.name", "Fixture")
        (self.brain / "unrelated.md").write_text("preserve me\n", encoding="utf-8")
        run("git", "-C", str(self.brain), "add", "unrelated.md")
        run("git", "-C", str(self.brain), "commit", "-m", "docs: retain sentinel")
        run("git", "init", "--bare", "--initial-branch=main", str(self.remote))
        run("git", "-C", str(self.brain), "remote", "add", "origin", str(self.remote))
        run("git", "-C", str(self.brain), "push", "origin", "main")
        run(
            "git",
            "-C",
            str(self.brain),
            "push",
            "origin",
            "HEAD:refs/heads/unrelated-sentinel",
        )
        (self.project / "source.txt").write_text("source\n", encoding="utf-8")
        run("git", "-C", str(self.project), "add", "source.txt")
        run("git", "-C", str(self.project), "commit", "-m", "feat: fixture")
        run(
            "git",
            "-C",
            str(self.project),
            "worktree",
            "add",
            "-b",
            "codex/fc-old",
            str(self.worktree),
        )
        self.config = default_config(self.brain)
        self.config["runtime"] = {
            "kind": "deterministic",
            "endpoint": "ws://127.0.0.1:1",
            "executable": None,
        }
        self.config["delivery"] = {"kind": "deterministic", "executable": None}
        self.config["beads"]["executable"] = str(
            Path(shutil.which("bd") or "bd").resolve()
        )
        self.config["brain"]["remote"] = "origin"
        self.config["knowledge"]["root"] = str(self.brain)
        self.config["knowledge"]["remote"] = "origin"
        self.config["projects"] = {
            "toy": {
                "root": str(self.project),
                "codex_project_id": None,
                "delivery": {"id": "fixture-repository", "registration": "supplied"},
                "integration_branch": "main",
                "prepare_argv": [],
                "validate_argv": [],
                "source_remote": None,
                "require_source_sync": False,
                "models": {},
                "enabled": True,
            }
        }
        self.config_path = self.brain / "fulcrum.yaml"
        with self.config_path.open("w", encoding="utf-8") as stream:
            ConfigurationManager.yaml().dump(self.config, stream)
        (self.instance / "config").symlink_to(self.config_path)
        (self.brain / ".beads" / "dolt").mkdir(parents=True)
        run(
            str(self.config["beads"]["executable"]),
            "-C",
            str(self.brain),
            "init",
            "--remote",
            f"git+file://{self.remote}",
            "--prefix",
            "fc",
            "--non-interactive",
            "--skip-agents",
            "--skip-hooks",
        )
        self.ledger = Ledger(self.brain)
        self.ledger.create_record(
            record_id="fc-oldwork",
            kind="work",
            title="Old managed work",
            description="Reset fixture",
            owner="managed-old",
            fc={
                "kind": "work",
                "owner": "managed-old",
                "project": "toy",
                "worktree": {
                    "path": str(self.worktree),
                    "branch": "codex/fc-old",
                },
            },
        )
        self.ledger.create_record(
            record_id="fc-oldtask",
            kind="task",
            title="Managed task",
            description="Owned by old work",
            owner="managed-old",
            fc={
                "kind": "task",
                "owner": "managed-old",
                "thread_id": "managed-old",
                "role": "executor",
                "work_bead": "fc-oldwork",
            },
        )
        run("bd", "-C", str(self.brain), "dolt", "push", "--remote", "origin")
        self.old_remote_oid = run(
            "git", "--git-dir", str(self.remote), "rev-parse", "refs/dolt/data"
        )
        self.unrelated_oid = run(
            "git",
            "--git-dir",
            str(self.remote),
            "rev-parse",
            "refs/heads/unrelated-sentinel",
        )
        self.instance_context = resolve_instance(
            instance=str(self.instance), config=None
        )
        self.runtime = FakeRuntime()

    def request(self, request_id: str) -> ParsedRequest:
        return ParsedRequest(
            command=("reset",),
            arguments={"hard": True, "yes": True},
            input={},
            actor=ActorContext(kind="human"),
            instance=self.instance_context,
            request_id=request_id,
            timeout=60,
            offline=True,
        )

    def close(self) -> None:
        self.temporary.cleanup()


@unittest.skipUnless(shutil.which("bd"), "stock Beads is required")
class Fulcrum2ResetTest(unittest.TestCase):
    def test_crash_resume_replaces_only_remote_ledger_and_preserves_sentinels(
        self,
    ) -> None:
        fixture = ResetFixture()
        self.addCleanup(fixture.close)
        request_id = str(uuid.uuid4())
        config_bytes = fixture.config_path.read_bytes()
        for expected_boundary in (
            "authority_recorded",
            "native_cleanup_recorded",
            "old_state_removed",
            "clean_ledger_initialized",
            "remote_replaced",
            "clean_receipt_written",
        ):

            def boundary(
                name: str, _: object, expected: str = expected_boundary
            ) -> None:
                if name == expected:
                    raise ResetBoundaryCrash(name)

            with self.assertRaisesRegex(ResetBoundaryCrash, expected_boundary):
                ResetService(
                    boundary_observer=boundary,
                    runtime_factory=lambda _: fixture.runtime,
                ).hard_reset(fixture.request(request_id))
            self.assertTrue(reset_workspace(fixture.instance).is_dir())

        result = ResetService(runtime_factory=lambda _: fixture.runtime).hard_reset(
            fixture.request(request_id)
        )
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(result.state.value, "completed")
        self.assertFalse(reset_workspace(fixture.instance).exists())
        self.assertEqual(fixture.runtime.deleted, ["managed-old"])
        self.assertTrue(fixture.runtime.exists["unrelated-native"])
        self.assertFalse(fixture.worktree.exists())
        branch = subprocess.run(
            [
                "git",
                "-C",
                str(fixture.project),
                "show-ref",
                "--verify",
                "refs/heads/codex/fc-old",
            ],
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(branch.returncode, 0)
        self.assertEqual(
            (fixture.brain / "unrelated.md").read_text(encoding="utf-8"),
            "preserve me\n",
        )
        self.assertEqual(fixture.config_path.read_bytes(), config_bytes)
        self.assertEqual(
            run(
                "git",
                "--git-dir",
                str(fixture.remote),
                "rev-parse",
                "refs/heads/unrelated-sentinel",
            ),
            fixture.unrelated_oid,
        )
        new_remote_oid = run(
            "git", "--git-dir", str(fixture.remote), "rev-parse", "refs/dolt/data"
        )
        self.assertNotEqual(new_remote_oid, fixture.old_remote_oid)
        clean = Ledger(fixture.brain)
        self.assertIsNone(clean.show("fc-oldwork"))
        self.assertIsNone(clean.show("fc-oldtask"))
        self.assertIsNotNone(clean.show("fc-system"))
        receipt = clean.show(operation_id(request_id))
        self.assertIsNotNone(receipt)
        self.assertNotIn("inventory", json.dumps(receipt.fc))
        retried = ResetService(runtime_factory=lambda _: fixture.runtime).hard_reset(
            fixture.request(request_id)
        )
        self.assertEqual(retried.operation_id, operation_id(request_id))

    def test_remote_change_after_inventory_stops_before_overwrite(self) -> None:
        fixture = ResetFixture()
        self.addCleanup(fixture.close)
        request_id = str(uuid.uuid4())
        changed_oid: str | None = None

        def boundary(name: str, _: object) -> None:
            nonlocal changed_oid
            if name != "clean_ledger_initialized" or changed_oid is not None:
                return
            changed_oid = run(
                "git",
                "--git-dir",
                str(fixture.remote),
                "hash-object",
                "-w",
                "--stdin",
            )
            run(
                "git",
                "--git-dir",
                str(fixture.remote),
                "update-ref",
                "refs/dolt/data",
                changed_oid,
            )

        result = ResetService(
            boundary_observer=boundary,
            runtime_factory=lambda _: fixture.runtime,
        ).hard_reset(fixture.request(request_id))
        self.assertFalse(result.ok)
        self.assertEqual(result.state.value, "failed")
        self.assertTrue(reset_workspace(fixture.instance).is_dir())
        self.assertEqual(
            run(
                "git",
                "--git-dir",
                str(fixture.remote),
                "rev-parse",
                "refs/dolt/data",
            ),
            changed_oid,
        )
        self.assertTrue((fixture.brain / "unrelated.md").is_file())

    def test_beads_bootstrap_failure_is_read_only_and_fences_startup(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        brain = root / "brain"
        instance = root / "instance"
        brain.mkdir()
        instance.mkdir()
        run("git", "init", "--initial-branch=main", str(brain))
        run("git", "-C", str(brain), "config", "user.email", "fixture@example.com")
        run("git", "-C", str(brain), "config", "user.name", "Fixture")
        sentinel = brain / ".beads" / "old-ledger-sentinel"
        sentinel.parent.mkdir()
        sentinel.write_text("retain\n", encoding="utf-8")
        config = default_config(brain)
        config["beads"]["executable"] = str(root / "missing-bd")
        config["runtime"]["kind"] = "deterministic"
        config["delivery"]["kind"] = "deterministic"
        config_path = brain / "fulcrum.yaml"
        with config_path.open("w", encoding="utf-8") as stream:
            ConfigurationManager.yaml().dump(config, stream)
        (instance / "config").symlink_to(config_path)
        context = resolve_instance(instance=str(instance), config=None)
        request = ParsedRequest(
            command=("reset",),
            arguments={"hard": True, "yes": True},
            input={},
            actor=ActorContext(kind="human"),
            instance=context,
            request_id=str(uuid.uuid4()),
            timeout=10,
            offline=True,
        )
        result = ResetService().hard_reset(request)
        self.assertTrue(result.ok)
        self.assertEqual(result.state.value, "degraded")
        self.assertFalse(result.result["durable_receipt"])
        self.assertFalse(result.result["destructive_actions_started"])
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "retain\n")
        with self.assertRaises(FulcrumError) as blocked:
            ServiceService().start(request)
        self.assertEqual(blocked.exception.code, "RESET_INCOMPLETE")


if __name__ == "__main__":
    unittest.main()
