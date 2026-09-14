from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from fulcrum.delivery import (
    DeliveryProviderError,
    SourceRef,
    TollgateDelivery,
    WorkRef,
    _delivery_facts,
)
from fulcrum.tollgate import TollgateUncertainError


def git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


class FakeTollgate:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.candidates: dict[str, dict[str, Any]] = {}
        self.prepare_loss = False
        self.submit_loss = False
        self.promotion_loss = False
        self.cleanup_loss = False
        self.approve_arguments: list[tuple[str, str]] = []

    def create_worktree(self, repository_id: str, identity: str) -> dict[str, Any]:
        path = self.root / ".worktrees" / identity.replace("/", "-")
        path.parent.mkdir(exist_ok=True)
        git(self.root, "worktree", "add", "-b", identity, str(path), "release")
        if self.prepare_loss:
            self.prepare_loss = False
            raise TollgateUncertainError(
                "lost worktree response",
                category="uncertain",
                possible_effect=True,
            )
        return {"path": str(path), "repository_id": repository_id}

    def remove_worktree(self, repository_id: str, path: str) -> dict[str, Any]:
        branch = git(Path(path), "branch", "--show-current")
        git(self.root, "worktree", "remove", path)
        git(self.root, "branch", "-D", branch)
        if self.cleanup_loss:
            self.cleanup_loss = False
            raise TollgateUncertainError(
                "lost cleanup response",
                category="uncertain",
                possible_effect=True,
            )
        return {"removed": path, "repository_id": repository_id}

    def submit_candidate(
        self, repository_id: str, revision: str = "HEAD", *, cwd: Path | None = None
    ) -> dict[str, Any]:
        assert cwd is not None
        handle = f"candidate-{len(self.candidates) + 1}"
        row = {
            "item": {
                "id": handle,
                "repository_id": repository_id,
                "source_oid": {"format": "sha1", "bytes": revision},
                "metadata": {"worktree_path": str(cwd)},
                "state": "queued",
                "remote_state": "preflight-pending",
                "cleanup_state": "pending",
                "promotion_authorized": False,
            }
        }
        self.candidates[handle] = row
        if self.submit_loss:
            self.submit_loss = False
            raise TollgateUncertainError(
                "lost candidate response",
                category="uncertain",
                possible_effect=True,
            )
        return row

    def queue(self, repository_id: str) -> dict[str, Any]:
        return {"queue": list(self.candidates.values())}

    def history(self, repository_id: str) -> dict[str, Any]:
        return {"items": list(self.candidates.values())}

    def status(
        self, repository_id: str, candidate_id: str | None = None
    ) -> dict[str, Any]:
        if candidate_id is None:
            return {"queue": list(self.candidates.values())}
        return self.candidates[candidate_id]

    def approve(self, repository_id: str, candidate_id: str) -> dict[str, Any]:
        self.approve_arguments.append((repository_id, candidate_id))
        row = self.candidates[candidate_id]
        item = row["item"]
        item["promotion_authorized"] = True
        item["state"] = "promoted-local-push-pending"
        row["generation"] = {
            "prefix_oids": [item["source_oid"]],
            "tested_oid": item["source_oid"],
        }
        if self.promotion_loss:
            self.promotion_loss = False
            raise TollgateUncertainError(
                "lost approve response",
                category="uncertain",
                possible_effect=True,
            )
        return {"item_id": candidate_id, "already_authorized": False}

    def cancel(self, repository_id: str, candidate_id: str) -> dict[str, Any]:
        self.candidates[candidate_id]["item"]["state"] = "canceled"
        return {"item_id": candidate_id}


class Fulcrum2DeliveryAdapterTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name).resolve()
        self.project = root / "project"
        self.remote = root / "remote.git"
        self.project.mkdir()
        git(self.project, "init", "--initial-branch=release")
        git(self.project, "config", "user.email", "fixture@example.com")
        git(self.project, "config", "user.name", "Fixture")
        (self.project / "README.md").write_text("base\n", encoding="utf-8")
        git(self.project, "add", "README.md")
        git(self.project, "commit", "-m", "base")
        (self.project / ".git" / "info" / "exclude").write_text(
            ".worktrees/\n", encoding="utf-8"
        )
        subprocess.run(
            ["git", "init", "--bare", "--initial-branch=release", str(self.remote)],
            capture_output=True,
            text=True,
            check=True,
        )
        self.native = FakeTollgate(self.project)
        self.delivery = TollgateDelivery(self.native)  # type: ignore[arg-type]
        self.work = WorkRef(
            bead_id="fc-work",
            project_id="toy",
            project_root=str(self.project),
            repository_id="repo-toy",
            intended_path=str(root / "instance" / "worktrees" / "toy" / "fc-work"),
            branch="codex/fc-work",
            integration_branch="release",
            operation_id="fc-prepare",
            prepare_argv=("/usr/bin/true",),
            validate_argv=("/usr/bin/true",),
            source_remote=str(self.remote),
            require_source_sync=True,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    async def test_exact_source_lost_effects_remote_ancestry_and_dirty_cleanup(
        self,
    ) -> None:
        self.native.prepare_loss = True
        prepared = await self.delivery.prepare(self.work)
        self.assertTrue(prepared.owned)
        self.assertNotEqual(prepared.path, self.work.intended_path)
        assert prepared.path is not None
        actual = WorkRef(
            **{
                **self.work.__dict__,
                "actual_path": prepared.path,
                "base_oid": prepared.base_oid,
            }
        )
        workspace = Path(prepared.path)
        (workspace / "result.txt").write_text("candidate\n", encoding="utf-8")
        git(workspace, "add", "result.txt")
        git(workspace, "commit", "-m", "candidate")
        source_oid = git(workspace, "rev-parse", "HEAD")
        source = SourceRef(actual, source_oid)

        self.native.submit_loss = True
        submitted = await self.delivery.submit(source)
        self.assertEqual(submitted.source_oid, source_oid)
        self.assertEqual(len(self.native.candidates), 1)
        self.native.promotion_loss = True
        promoted = await self.delivery.promote(source, submitted.handle)
        self.assertEqual(promoted.promotion, "promoted")
        self.assertEqual(promoted.integration_oid, source_oid)
        self.assertEqual(
            self.native.approve_arguments, [("repo-toy", submitted.handle)]
        )

        synchronized = await self.delivery.synchronize(source, submitted.handle)
        self.assertEqual(synchronized.synchronization, "complete")
        self.assertTrue(synchronized.evidence["source_publication"]["pushed"])

        clone = Path(self.temporary.name) / "newer"
        subprocess.run(
            ["git", "clone", "--branch", "release", str(self.remote), str(clone)],
            capture_output=True,
            text=True,
            check=True,
        )
        git(clone, "config", "user.email", "fixture@example.com")
        git(clone, "config", "user.name", "Fixture")
        (clone / "newer.txt").write_text("newer\n", encoding="utf-8")
        git(clone, "add", "newer.txt")
        git(clone, "commit", "-m", "newer remote")
        git(clone, "push", "origin", "release")
        observed_newer = await self.delivery.synchronize(source, submitted.handle)
        self.assertFalse(observed_newer.evidence["source_publication"]["pushed"])

        (workspace / "dirty.txt").write_text("retain me\n", encoding="utf-8")
        with self.assertRaises(DeliveryProviderError) as dirty:
            await self.delivery.cleanup(actual)
        self.assertEqual(dirty.exception.category, "rejected")
        (workspace / "dirty.txt").unlink()
        self.native.cleanup_loss = True
        cleaned = await self.delivery.cleanup(actual)
        self.assertFalse(cleaned.exists)
        self.assertFalse(workspace.exists())
        self.assertTrue(cleaned.ownership_evidence["branch_absent"])
        branch = subprocess.run(
            [
                "git",
                "-C",
                str(self.project),
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/heads/{self.work.branch}",
            ],
            check=False,
        )
        self.assertEqual(branch.returncode, 1)

    async def test_retained_real_response_preserves_regenerated_integration_oid(
        self,
    ) -> None:
        documents = [
            json.loads(line)
            for line in (
                Path(__file__).parent
                / "fixtures"
                / "tollgate"
                / "approve-operation-28.jsonl"
            )
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        terminal = documents[-1]
        item = terminal["item"]
        response = {
            **terminal,
            "generation": {
                "prefix_oids": [
                    {
                        "format": "sha1",
                        "bytes": "3cd6135fec4e851489b2832c0acc144ceb4179aa",
                    }
                ],
                "tested_oid": {
                    "format": "sha1",
                    "bytes": "3cd6135fec4e851489b2832c0acc144ceb4179aa",
                },
            },
        }
        source = SourceRef(
            self.work,
            str(item["source_oid"]["bytes"]),
        )
        normalized = _delivery_facts(response, source, str(item["id"]))
        self.assertEqual(
            normalized.source_oid, "7a369b9b6dde8cab5694b9b52aa76b6f2f449488"
        )
        self.assertEqual(
            normalized.integration_oid,
            "3cd6135fec4e851489b2832c0acc144ceb4179aa",
        )
        self.assertNotEqual(normalized.source_oid, normalized.integration_oid)


class Fulcrum2DeliveryInstalledCliTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name).resolve()
        cls.brain = cls.root / "brain"
        cls.instance = cls.root / "instance"
        cls.project = cls.root / "project"
        cls.remote = cls.root / "remote.git"
        for path in (cls.brain, cls.instance, cls.project):
            path.mkdir()
        for path in (cls.brain, cls.project):
            git(path, "init", "--initial-branch=release")
            git(path, "config", "user.email", "fixture@example.com")
            git(path, "config", "user.name", "Fixture")
        (cls.project / "README.md").write_text("base\n", encoding="utf-8")
        git(cls.project, "add", "README.md")
        git(cls.project, "commit", "-m", "base")
        subprocess.run(
            ["git", "init", "--bare", "--initial-branch=release", str(cls.remote)],
            capture_output=True,
            text=True,
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
            cwd=cls.brain,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        cls.provider_state = cls.root / "provider.json"
        cls.provider_state.write_text('{"candidates": {}}', encoding="utf-8")
        cls.fake_tg = cls.root / "tg"
        provider = """#!/usr/bin/env python3
import json
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__PROJECT__)
STATE = Path(__STATE__)
args = sys.argv[1:]
repository = None
while args and args[0].startswith("--"):
    flag = args.pop(0)
    if flag == "--repository":
        repository = args.pop(0)
state = json.loads(STATE.read_text())

def save():
    STATE.write_text(json.dumps(state))

def run(*argv, cwd=None):
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()

if args[:2] == ["worktree", "create"]:
    identity = args[2]
    path = PROJECT / ".worktrees" / identity.replace("/", "-")
    path.parent.mkdir(exist_ok=True)
    run("git", "-C", str(PROJECT), "worktree", "add", "-b", identity, str(path), "release")
    print(json.dumps({"path": str(path), "repository_id": repository}))
elif args[:2] == ["worktree", "remove"]:
    path = Path(args[2])
    branch = run("git", "branch", "--show-current", cwd=path)
    run("git", "-C", str(PROJECT), "worktree", "remove", str(path))
    run("git", "-C", str(PROJECT), "branch", "-D", branch)
    print(json.dumps({"removed": str(path)}))
elif args[0] == "candidate":
    oid = args[1]
    handle = "candidate-1"
    row = {"item": {"id": handle, "repository_id": repository,
        "source_oid": {"format": "sha1", "bytes": oid},
        "metadata": {"worktree_path": str(Path.cwd())}, "state": "ready",
        "remote_state": "preflight-pending", "cleanup_state": "pending",
        "promotion_authorized": False}}
    state["candidates"][handle] = row
    save()
    print(json.dumps(row))
elif args[0] == "queue":
    print(json.dumps({"queue": list(state["candidates"].values())}))
elif args[0] == "history":
    print(json.dumps({"items": list(state["candidates"].values())}))
elif args[0] == "status":
    if len(args) == 1:
        print(json.dumps({"queue": list(state["candidates"].values())}))
    else:
        print(json.dumps(state["candidates"][args[1]]))
elif args[0] == "approve":
    handle = args[1]
    row = state["candidates"][handle]
    row["item"]["promotion_authorized"] = True
    row["item"]["state"] = "promoted-local-push-pending"
    row["generation"] = {"prefix_oids": [row["item"]["source_oid"]],
        "tested_oid": row["item"]["source_oid"]}
    save()
    print(json.dumps({"item_id": handle, "already_authorized": False,
        "authorized_item_ids": [handle]}))
else:
    print(json.dumps({"error": {"code": "unsupported", "message": repr(args)}}), file=sys.stderr)
    raise SystemExit(2)
"""
        cls.fake_tg.write_text(
            provider.replace("__PROJECT__", repr(str(cls.project))).replace(
                "__STATE__", repr(str(cls.provider_state))
            ),
            encoding="utf-8",
        )
        cls.fake_tg.chmod(0o755)
        cls.config = cls.brain / "fulcrum.yaml"
        cls.config.write_text(
            f"brain:\n  root: {cls.brain}\n"
            "beads:\n"
            f"  executable: {shutil.which('bd')}\n"
            "delivery:\n"
            "  kind: tollgate\n"
            f"  executable: {cls.fake_tg}\n"
            "projects:\n"
            "  toy:\n"
            f"    root: {cls.project}\n"
            "    codex_project_id: native-toy\n"
            "    delivery:\n"
            "      id: repo-toy\n"
            "      registration: supplied\n"
            "    integration_branch: release\n"
            "    prepare_argv: [/usr/bin/true]\n"
            "    validate_argv: [/usr/bin/true]\n"
            f"    source_remote: {cls.remote}\n"
            "    require_source_sync: true\n"
            "    enabled: true\n",
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
    ) -> dict[str, Any]:
        completed = subprocess.run(
            [str(self.executable), *arguments],
            input=json.dumps(payload) if payload is not None else None,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        return json.loads(completed.stdout)

    def test_installed_cli_delivery_lifecycle(self) -> None:
        created = self.invoke(
            "work",
            "create",
            "--instance",
            str(self.instance),
            "--offline",
            "--actor",
            "human",
            "--input",
            "-",
            "--json",
            payload={
                "title": "Deliver exact source",
                "outcome": "Publish an observed source",
                "project": "toy",
                "acceptance": ["Remote contains the integration commit"],
                "requested_role": "executor",
            },
        )
        bead_id = created["result"]["result"]["bead_id"]
        prepared = self.invoke(
            "worktree",
            "prepare",
            "--bead",
            bead_id,
            "--instance",
            str(self.instance),
            "--offline",
            "--actor",
            "human",
            "--json",
        )
        workspace_value = prepared["result"]["result"]["workspace"]["path"]
        workspace = Path(workspace_value)
        self.assertTrue(workspace.is_dir())
        inspected = self.invoke(
            "worktree",
            "inspect",
            "--bead",
            bead_id,
            "--instance",
            str(self.instance),
            "--json",
        )
        self.assertTrue(inspected["result"]["workspace"]["owned"])
        (workspace / "result.txt").write_text("delivered\n", encoding="utf-8")
        git(workspace, "add", "result.txt")
        git(workspace, "commit", "-m", "deliver exact source")
        source_oid = git(workspace, "rev-parse", "HEAD")

        validation = self.invoke(
            "validation",
            "start",
            "--bead",
            bead_id,
            "--source",
            source_oid,
            "--instance",
            str(self.instance),
            "--offline",
            "--actor",
            "human",
            "--json",
        )
        self.assertEqual(
            validation["result"]["result"]["validation"]["source_oid"], source_oid
        )
        shown = self.invoke(
            "validation",
            "show",
            "--bead",
            bead_id,
            "--instance",
            str(self.instance),
            "--json",
        )
        self.assertEqual(shown["result"]["state"], "passed")
        promoted = self.invoke(
            "promotion",
            "start",
            "--bead",
            bead_id,
            "--source",
            source_oid,
            "--instance",
            str(self.instance),
            "--offline",
            "--actor",
            "human",
            "--json",
        )
        facts = promoted["result"]["result"]["delivery"]
        self.assertEqual(facts["promotion"], "promoted")
        self.assertEqual(facts["integration_oid"], source_oid)
        shown_promotion = self.invoke(
            "promotion",
            "show",
            "--bead",
            bead_id,
            "--instance",
            str(self.instance),
            "--json",
        )
        self.assertEqual(shown_promotion["result"]["delivery"]["promotion"], "promoted")
        synchronized = self.invoke(
            "source",
            "sync",
            "--bead",
            bead_id,
            "--instance",
            str(self.instance),
            "--offline",
            "--actor",
            "human",
            "--json",
        )
        self.assertEqual(
            synchronized["result"]["result"]["delivery"]["synchronization"],
            "complete",
        )
        remote_oid = git(self.remote, "rev-parse", "refs/heads/release")
        self.assertEqual(remote_oid, source_oid)
        cleaned = self.invoke(
            "worktree",
            "cleanup",
            "--bead",
            bead_id,
            "--instance",
            str(self.instance),
            "--offline",
            "--actor",
            "human",
            "--json",
        )
        cleaned_workspace = cleaned["result"]["result"]["workspace"]
        self.assertFalse(cleaned_workspace["exists"])
        self.assertTrue(cleaned_workspace["ownership_evidence"]["branch_absent"])
        self.assertFalse(workspace.exists())
        self.invoke(
            "work",
            "close",
            bead_id,
            "--outcome",
            "delivered",
            "--summary",
            "Observed exact source delivery",
            "--instance",
            str(self.instance),
            "--offline",
            "--actor",
            "human",
            "--json",
        )


if __name__ == "__main__":
    unittest.main()
