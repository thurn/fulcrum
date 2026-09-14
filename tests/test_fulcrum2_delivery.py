from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any

from fulcrum.application import Application
from fulcrum.contracts import ActorContext, ParsedRequest
from fulcrum.delivery import (
    DeliveryProviderError,
    SourceRef,
    TollgateDelivery,
    WorkRef,
    _delivery_facts,
)
from fulcrum.instance import resolve_instance
from fulcrum.ledger import Ledger
from fulcrum.runtime import (
    ReleaseFacts,
    ResourceFacts,
    RuntimeCapabilities,
    TaskFacts,
    TurnFacts,
)
from fulcrum.supervision import ControllerSupervisor
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


class HandoffRuntime:
    def __init__(self) -> None:
        self.facts: dict[str, TaskFacts] = {}
        self.creation_cwds: dict[str, str] = {}
        self.turns: dict[tuple[str, str], TurnFacts] = {}
        self.inputs: list[str] = []
        self.released: list[str] = []
        self.running_terminals: list[dict[str, Any]] = []

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            available=True,
            endpoint="fixture://runtime",
            methods=(),
            models={"gpt-5.6-sol": ("high",)},
        )

    async def resources(self) -> ResourceFacts:
        return ResourceFacts(
            loaded_count=len(self.facts),
            active_count=sum(
                item.active_turn is not None for item in self.facts.values()
            ),
            loaded_ids=tuple(self.facts),
            fd_soft_limit=1000,
            fd_usage=20,
            overloaded=False,
            observed_at="2026-09-14T20:00:00Z",
        )

    async def inspect_task(self, thread_id: str) -> TaskFacts:
        return self.facts[thread_id]

    async def find_tasks(self, creation_cwd: str) -> list[TaskFacts]:
        return [
            self.facts[thread]
            for thread, cwd in self.creation_cwds.items()
            if cwd == creation_cwd
        ]

    async def create_task(self, spec: Any) -> TaskFacts:
        thread_id = f"native-warden-{len(self.creation_cwds) + 1}"
        self.creation_cwds[thread_id] = spec.creation_cwd
        facts = TaskFacts(
            id=thread_id,
            title=spec.title,
            cwd=spec.creation_cwd,
            project_id=spec.project_id,
            workspace_roots=spec.workspace_roots,
            archived=False,
            exists=True,
            loaded=True,
            runtime_status="idle",
            active_turn=None,
            last_turn=None,
            pending_requests=(),
            observed_at="2026-09-14T20:00:00Z",
        )
        self.facts[thread_id] = facts
        return facts

    async def configure_task(self, thread_id: str, spec: Any) -> TaskFacts:
        current = self.facts[thread_id]
        configured = TaskFacts(
            **{
                **current.__dict__,
                "title": spec.title,
                "cwd": spec.cwd,
                "project_id": spec.project_id,
                "workspace_roots": spec.workspace_roots,
            }
        )
        self.facts[thread_id] = configured
        return configured

    async def find_turn(self, thread_id: str, operation_id: str) -> TurnFacts | None:
        return self.turns.get((thread_id, operation_id))

    async def start_turn(self, thread_id: str, turn: Any) -> TurnFacts:
        self.inputs.append(str(turn.text))
        facts = TurnFacts(
            id=f"turn-{len(self.turns) + 1}",
            thread_id=thread_id,
            state="inProgress",
            operation_id=turn.operation_id,
            completed=False,
            error=None,
            tools=(),
            usage=None,
            observed_at="2026-09-14T20:00:00Z",
        )
        self.turns[(thread_id, turn.operation_id)] = facts
        current = self.facts[thread_id]
        self.facts[thread_id] = TaskFacts(
            **{
                **current.__dict__,
                "runtime_status": "active",
                "active_turn": facts.id,
                "last_turn": facts.to_dict(),
            }
        )
        return facts

    async def terminals(
        self, thread_id: str, *, limit: int, cursor: str | None
    ) -> dict[str, Any]:
        return {
            "thread_id": thread_id,
            "items": list(self.running_terminals),
            "next_cursor": None,
        }

    async def release(self, thread_id: str) -> ReleaseFacts:
        self.released.append(thread_id)
        return ReleaseFacts(
            thread_id=thread_id,
            status="unsubscribed",
            active_terminals=(),
            observed_at="2026-09-14T20:00:00Z",
        )


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
            timeout=120,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        return json.loads(completed.stdout)

    def test_01_installed_cli_delivery_lifecycle(self) -> None:
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
        approved = self.invoke(
            "review",
            "approve",
            "--bead",
            bead_id,
            "--source",
            source_oid,
            "--summary",
            "Current source satisfies the fixture acceptance check",
            "--instance",
            str(self.instance),
            "--offline",
            "--actor",
            "human",
            "--json",
        )
        self.assertEqual(
            approved["result"]["result"]["approved_source"]["oid"], source_oid
        )
        (workspace / "warden-fix.txt").write_text("fixed\n", encoding="utf-8")
        git(workspace, "add", "warden-fix.txt")
        git(workspace, "commit", "-m", "warden fix")
        fixed_oid = git(workspace, "rev-parse", "HEAD")
        stale_promotion = subprocess.run(
            [
                str(self.executable),
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
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        self.assertNotEqual(stale_promotion.returncode, 0)
        stale_operation = json.loads(stale_promotion.stdout)
        self.assertEqual(stale_operation["result"]["error"]["code"], "STALE_SOURCE")
        self.invoke(
            "validation",
            "start",
            "--bead",
            bead_id,
            "--source",
            fixed_oid,
            "--instance",
            str(self.instance),
            "--offline",
            "--actor",
            "human",
            "--json",
        )
        self.invoke(
            "review",
            "approve",
            "--bead",
            bead_id,
            "--source",
            fixed_oid,
            "--summary",
            "Warden fix is current, validated, and accepted",
            "--instance",
            str(self.instance),
            "--offline",
            "--actor",
            "human",
            "--json",
        )
        source_oid = fixed_oid
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

    def test_02_executor_finish_waits_for_terminal_then_starts_one_warden(self) -> None:
        remote_release = subprocess.run(
            [
                "git",
                "-C",
                str(self.remote),
                "show-ref",
                "--verify",
                "--quiet",
                "refs/heads/release",
            ],
            check=False,
        )
        if remote_release.returncode == 0:
            git(self.project, "fetch", str(self.remote), "release")
            git(self.project, "reset", "--hard", "FETCH_HEAD")
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
                "title": "Controlled handoff",
                "outcome": "Transfer one clean commit to Warden",
                "project": "toy",
                "acceptance": ["Exactly one Warden receives current source"],
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
        workspace = Path(prepared["result"]["result"]["workspace"]["path"])
        (workspace / "handoff.txt").write_text("ready\n", encoding="utf-8")
        git(workspace, "add", "handoff.txt")
        git(workspace, "commit", "-m", "prepare handoff")
        source_oid = git(workspace, "rev-parse", "HEAD")
        ledger = Ledger(self.brain)
        work = ledger.show(bead_id)
        assert work is not None and work.fc
        acquisition = "fc-executor-acquisition"
        executor = "native-executor"
        work_fc = dict(work.fc)
        work_fc.update(
            {
                "owner": executor,
                "role": "executor",
                "phase": "working",
                "ownership_operation": acquisition,
                "next_action": "Implement and finish.",
            }
        )
        ledger.update_fc(bead_id, work_fc, assignee=executor, status="in_progress")
        ledger.create_record(
            record_id="fc-executor-task",
            kind="task",
            title="Managed Executor",
            description=f"Executor for {bead_id}",
            owner=executor,
            external_ref=f"fulcrum:thread:{executor}",
            fc={
                "kind": "task",
                "owner": executor,
                "thread_id": executor,
                "role": "executor",
                "work_bead": bead_id,
                "ownership_operation": acquisition,
                "creation_operation": acquisition,
                "creation_cwd": str(self.root / "executor-thread"),
                "model": "gpt-5.6-sol",
                "effort": "high",
                "associated_beads": [],
                "replaced_by": None,
                "deleted_at": None,
                "last_observed": None,
                "last_turn": None,
                "last_transition": acquisition,
            },
        )
        finish_request = str(uuid.uuid4())
        finish_arguments = (
            "finish",
            "--bead",
            bead_id,
            "--outcome",
            "ready_for_review",
            "--thread-id",
            executor,
            "--ownership-operation",
            acquisition,
            "--instance",
            str(self.instance),
            "--offline",
            "--actor",
            f"task:{executor}",
            "--request-id",
            finish_request,
            "--input",
            "-",
            "--json",
        )
        payload = {
            "summary": "Implemented and committed the accepted handoff fixture",
            "source_oid": source_oid,
            "checks": [
                {
                    "name": "fixture",
                    "status": "passed",
                    "evidence": "result file committed",
                }
            ],
            "evidence": [f"git:{source_oid}"],
        }
        finished = self.invoke(*finish_arguments, payload=payload)
        self.assertTrue(finished["result"]["result"]["accepted"])
        retried = self.invoke(*finish_arguments, payload=payload)
        self.assertEqual(retried["operation_id"], finished["operation_id"])
        stale = subprocess.run(
            [
                str(self.executable),
                "finish",
                "--bead",
                bead_id,
                "--outcome",
                "ready_for_review",
                "--thread-id",
                executor,
                "--ownership-operation",
                acquisition,
                "--instance",
                str(self.instance),
                "--offline",
                "--actor",
                f"task:{executor}",
                "--request-id",
                str(uuid.uuid4()),
                "--input",
                "-",
                "--json",
            ],
            input=json.dumps({**payload, "summary": "A conflicting second finish"}),
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        self.assertEqual(stale.returncode, 5)
        self.assertEqual(json.loads(stale.stdout)["error"]["code"], "FINISH_SEALED")

        runtime = HandoffRuntime()
        active_turn = {
            "id": "executor-turn",
            "status": "inProgress",
            "operationId": acquisition,
        }
        runtime.facts[executor] = TaskFacts(
            id=executor,
            title="Executor",
            cwd=str(workspace),
            project_id="native-toy",
            workspace_roots=(str(self.project),),
            archived=False,
            exists=True,
            loaded=True,
            runtime_status="active",
            active_turn="executor-turn",
            last_turn=active_turn,
            pending_requests=(),
            observed_at="2026-09-14T20:00:00Z",
        )
        for role in ("vizier", "marshal"):
            thread_id = f"native-{role}-leader"
            runtime.facts[thread_id] = TaskFacts(
                id=thread_id,
                title=role,
                cwd=str(self.brain),
                project_id=None,
                workspace_roots=(str(self.brain),),
                archived=False,
                exists=True,
                loaded=True,
                runtime_status="idle",
                active_turn=None,
                last_turn=None,
                pending_requests=(),
                observed_at="2026-09-14T20:00:00Z",
            )
            ledger.create_record(
                record_id=f"fc-{role}-delivery-leader",
                kind="task",
                title=f"Managed {role}",
                description=f"Standing {role}",
                owner=thread_id,
                external_ref=f"fulcrum:thread:{thread_id}",
                fc={
                    "kind": "task",
                    "owner": thread_id,
                    "thread_id": thread_id,
                    "role": role,
                    "purpose": "leadership",
                    "work_bead": None,
                    "creation_operation": f"fc-{role}-bootstrap",
                    "last_observed": runtime.facts[thread_id].to_dict(),
                    "deleted_at": None,
                    "replaced_by": None,
                },
            )
        ledger.create_record(
            record_id="fc-system",
            kind="control",
            title="Fulcrum control",
            description="Standing leaders",
            owner="native-marshal-leader",
            fc={
                "kind": "control",
                "owner": "native-marshal-leader",
                "vizier_thread": "native-vizier-leader",
                "marshal_thread": "native-marshal-leader",
                "active_takeover": None,
                "last_transition": None,
            },
        )
        request = ParsedRequest(
            command=("reconcile",),
            arguments={"bead": bead_id},
            input={},
            actor=ActorContext(kind="human"),
            instance=resolve_instance(instance=str(self.instance), config=None),
            request_id=str(uuid.uuid4()),
            timeout=30,
            offline=True,
        )
        supervisor = ControllerSupervisor(
            request, Application(), runtime=runtime  # type: ignore[arg-type]
        )
        asyncio.run(supervisor.run_once(bead_id=bead_id))
        waiting = ledger.show(bead_id)
        assert waiting is not None and waiting.fc
        self.assertEqual(waiting.fc["owner"], executor)
        self.assertNotIn(executor, runtime.released)

        runtime.facts[executor] = TaskFacts(
            **{
                **runtime.facts[executor].__dict__,
                "runtime_status": "idle",
                "active_turn": None,
                "last_turn": {"id": "executor-turn", "status": "completed"},
            }
        )
        runtime.running_terminals = [{"terminal_id": "terminal-1", "status": "running"}]
        blocked = asyncio.run(supervisor.run_once(bead_id=bead_id))
        still_waiting = ledger.show(bead_id)
        assert still_waiting is not None and still_waiting.fc
        self.assertEqual(still_waiting.fc["owner"], executor)
        self.assertEqual(runtime.released, [])
        blocked_handoff = [
            action
            for action in blocked.next_actions
            if action.get("kind") == "executor_warden_handoff"
        ]
        self.assertEqual(len(blocked_handoff), 1)
        self.assertFalse(blocked_handoff[0]["started"])
        runtime.running_terminals = []
        summary = asyncio.run(supervisor.run_once(bead_id=bead_id))
        transferred = ledger.show(bead_id)
        assert transferred is not None and transferred.fc
        self.assertEqual(transferred.fc["role"], "warden")
        self.assertEqual(transferred.fc["phase"], "reviewing")
        self.assertNotEqual(transferred.fc["owner"], executor)
        self.assertEqual(runtime.released, [executor])
        handoffs = [
            action
            for action in summary.next_actions
            if action.get("kind") == "executor_warden_handoff"
        ]
        self.assertEqual(len(handoffs), 1)
        self.assertTrue(handoffs[0]["started"])
        warden_tasks = [
            item
            for item in ledger.list_records(kind="task", limit=0)
            if item.fc and item.fc.get("role") == "warden"
        ]
        self.assertEqual(len(warden_tasks), 1)
        self.assertEqual(len(runtime.inputs), 1)
        self.assertIn("Fulcrum Warden", runtime.inputs[0])
        self.assertIn(source_oid, runtime.inputs[0])
        warden_start_operation = str(handoffs[0]["operation_id"])
        interrupted_start = ledger.show(warden_start_operation)
        assert interrupted_start is not None and interrupted_start.fc
        interrupted_fc = dict(interrupted_start.fc)
        interrupted_fc["state"] = "accepted"
        interrupted_fc["step"] = "ownership_bound_before_turn_observation"
        interrupted_fc.pop("completed_at", None)
        ledger.update_fc(warden_start_operation, interrupted_fc, status="open")
        warden_task = warden_tasks[0]
        warden_task_fc = dict(warden_task.fc or {})
        warden_task_fc["last_turn"] = None
        ledger.update_fc(warden_task.id, warden_task_fc)
        asyncio.run(supervisor.run_once(bead_id=bead_id))
        recovered_start = ledger.show(warden_start_operation)
        assert recovered_start is not None and recovered_start.fc
        self.assertEqual(recovered_start.fc["state"], "completed")
        self.assertEqual(len(runtime.turns), 1)
        self.assertEqual(
            len(
                [
                    item
                    for item in ledger.list_records(kind="task", limit=0)
                    if item.fc and item.fc.get("role") == "warden"
                ]
            ),
            1,
        )
        stale_progress = subprocess.run(
            [
                str(self.executable),
                "progress",
                "--bead",
                bead_id,
                "--kind",
                "source",
                "--summary",
                "Old Executor attempted a late source mutation",
                "--evidence",
                "git:stale",
                "--thread-id",
                executor,
                "--ownership-operation",
                acquisition,
                "--instance",
                str(self.instance),
                "--offline",
                "--actor",
                f"task:{executor}",
                "--json",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        self.assertEqual(stale_progress.returncode, 5)
        self.assertEqual(
            json.loads(stale_progress.stdout)["error"]["code"],
            "OWNERSHIP_CONFLICT",
        )
        warden = str(transferred.fc["owner"])
        warden_acquisition = str(transferred.fc["ownership_operation"])
        approved_finish = self.invoke(
            "finish",
            "--bead",
            bead_id,
            "--outcome",
            "approved",
            "--thread-id",
            warden,
            "--ownership-operation",
            warden_acquisition,
            "--instance",
            str(self.instance),
            "--offline",
            "--actor",
            f"task:{warden}",
            "--input",
            "-",
            "--json",
            payload={
                "summary": "Reviewed the current source and accepted it for delivery",
                "source_oid": source_oid,
                "checks": [
                    {
                        "name": "handoff fixture",
                        "status": "passed",
                        "evidence": "current committed source reviewed",
                    }
                ],
                "evidence": [f"git:{source_oid}"],
            },
        )
        self.assertEqual(approved_finish["result"]["step"], "warden_delivery_started")
        runtime.facts[warden] = TaskFacts(
            **{
                **runtime.facts[warden].__dict__,
                "runtime_status": "idle",
                "active_turn": None,
                "last_turn": {"id": "warden-turn", "status": "completed"},
            }
        )
        delivered = asyncio.run(supervisor.run_once(bead_id=bead_id))
        closed = ledger.show(bead_id)
        assert closed is not None and closed.fc
        self.assertEqual(closed.status, "closed")
        self.assertEqual(closed.fc["phase"], "done")
        self.assertEqual(closed.fc["disposition"]["outcome"], "delivered")
        self.assertFalse(workspace.exists())
        delivery_actions = [
            action
            for action in delivered.next_actions
            if action.get("kind") == "warden_delivery"
        ]
        self.assertEqual(len(delivery_actions), 1)
        self.assertEqual(delivery_actions[0]["state"], "closed")


if __name__ == "__main__":
    unittest.main()
