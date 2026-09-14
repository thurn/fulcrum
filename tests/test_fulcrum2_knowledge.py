from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fulcrum.application import Application
from fulcrum.brain import (
    IsolatedGitPublisher,
    KnowledgePublicationError,
    PublicationDestination,
)
from fulcrum.contracts import ActorContext, FulcrumError, ParsedRequest
from fulcrum.configuration import ConfigurationManager
from fulcrum.instance import resolve_instance
from fulcrum.knowledge import render_selected_memory, select_memory
from fulcrum.leadership import build_brief
from fulcrum.ledger import Ledger, LedgerFailure
from fulcrum.roles import _cook_role


def git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return result.stdout.strip()


def configure_git(root: Path) -> None:
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "user.name", "Fixture")


class IsolatedGitPublisherTest(unittest.TestCase):
    def prepare_repository(self, root: Path) -> tuple[Path, Path, str]:
        remote = root / "remote.git"
        subprocess.run(
            ["git", "init", "--bare", str(remote)],
            capture_output=True,
            check=True,
        )
        source = root / "source"
        writer = root / "writer"
        subprocess.run(
            ["git", "clone", str(remote), str(source)],
            capture_output=True,
            check=True,
        )
        configure_git(source)
        (source / "base.txt").write_text("base\n", encoding="utf-8")
        (source / "unrelated.txt").write_text("clean\n", encoding="utf-8")
        git(source, "add", "base.txt", "unrelated.txt")
        git(source, "commit", "-m", "chore: initialize fixture")
        branch = git(source, "symbolic-ref", "--short", "HEAD")
        git(source, "push", "origin", branch)
        subprocess.run(
            ["git", "clone", str(remote), str(writer)],
            capture_output=True,
            check=True,
        )
        configure_git(writer)
        return source, writer, branch

    def test_selected_publication_preserves_dirty_state_and_remote_history(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, writer, branch = self.prepare_repository(root)
            (source / "local-unsent.txt").write_text(
                "preserved local commit\n", encoding="utf-8"
            )
            git(source, "add", "local-unsent.txt")
            git(source, "commit", "-m", "docs: retain local unsent history")
            (source / "unrelated.txt").write_text(
                "dirty but retained\n", encoding="utf-8"
            )
            (source / "scratch.txt").write_text(
                "untracked and retained\n", encoding="utf-8"
            )
            before_status = git(source, "status", "--porcelain=v1")
            before_index = git(source, "diff", "--cached", "--name-only")

            (writer / "remote.txt").write_text("new remote history\n", encoding="utf-8")
            git(writer, "add", "remote.txt")
            git(writer, "commit", "-m", "docs: add remote history")
            git(writer, "push", "origin", branch)

            lost = {"commit": False, "push": False}

            def lose_commit(_: str) -> None:
                if not lost["commit"]:
                    lost["commit"] = True
                    raise ConnectionError("commit response lost")

            def lose_push(_: str) -> None:
                if not lost["push"]:
                    lost["push"] = True
                    raise ConnectionError("push response lost")

            publisher = IsolatedGitPublisher(
                after_commit=lose_commit,
                after_push=lose_push,
            )
            result = publisher.publish(
                {
                    "operation_id": "fc-publish-selected",
                    "documents": [
                        {
                            "relative_path": "plans/selected.md",
                            "content": "# Selected\n\nCanonical content.\n",
                        }
                    ],
                },
                PublicationDestination(
                    root=source,
                    worktree_root=root / "worktrees",
                    remote="origin",
                    branch=branch,
                    require_remote_sync=True,
                ),
            )
            self.assertEqual(result["local"]["state"], "observed")
            self.assertEqual(result["remote"]["state"], "observed")
            self.assertTrue(result["remote"]["contains_local"])
            self.assertTrue(result["merged_remote"])
            self.assertEqual(result["response_loss_reconciled"], ["commit", "push"])
            inspected = publisher.inspect(result)
            self.assertTrue(inspected["local_observed"])
            self.assertTrue(inspected["remote_contains_local"])
            self.assertEqual(git(source, "status", "--porcelain=v1"), before_status)
            self.assertEqual(
                git(source, "diff", "--cached", "--name-only"), before_index
            )
            git(writer, "fetch", "origin", branch)
            remote_tip = git(writer, "rev-parse", f"origin/{branch}")
            self.assertEqual(
                git(writer, "show", f"{remote_tip}:plans/selected.md"),
                "# Selected\n\nCanonical content.",
            )
            self.assertEqual(
                git(writer, "show", f"{remote_tip}:remote.txt"),
                "new remote history",
            )
            self.assertEqual(
                git(writer, "show", f"{remote_tip}:local-unsent.txt"),
                "preserved local commit",
            )
            git(writer, "merge", "--ff-only", f"origin/{branch}")
            (writer / "newer.txt").write_text("newer remote commit\n", encoding="utf-8")
            git(writer, "add", "newer.txt")
            git(writer, "commit", "-m", "docs: add newer remote history")
            git(writer, "push", "origin", branch)
            inspected_newer = publisher.inspect(result)
            self.assertTrue(inspected_newer["remote_contains_local"])
            self.assertNotEqual(
                inspected_newer["remote_commit"], result["local"]["commit"]
            )
            (writer / "plans" / "selected.md").write_text(
                "manual remote rewrite\n", encoding="utf-8"
            )
            git(writer, "add", "plans/selected.md")
            git(writer, "commit", "-m", "docs: manually rewrite selected plan")
            git(writer, "push", "origin", branch)
            conflicted_refinement = IsolatedGitPublisher().publish(
                {
                    "operation_id": "fc-publish-refinement-conflict",
                    "prior_publication_commit": result["local"]["commit"],
                    "documents": [
                        {
                            "relative_path": "plans/selected.md",
                            "content": "# Selected\n\nRefined canonical content.\n",
                        }
                    ],
                },
                PublicationDestination(
                    root=source,
                    worktree_root=root / "refinement-worktrees",
                    remote="origin",
                    branch=branch,
                    require_remote_sync=True,
                ),
            )
            self.assertEqual(
                conflicted_refinement["repair"]["kind"], "publication_conflict"
            )
            self.assertEqual(conflicted_refinement["remote"]["state"], "failed")

    def test_conflict_retains_local_content_and_symlink_escape_is_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, writer, branch = self.prepare_repository(root)
            (writer / "plans").mkdir()
            (writer / "plans" / "conflict.md").write_text(
                "remote content\n", encoding="utf-8"
            )
            git(writer, "add", "plans/conflict.md")
            git(writer, "commit", "-m", "docs: edit shared publication")
            git(writer, "push", "origin", branch)
            publisher = IsolatedGitPublisher()
            result = publisher.publish(
                {
                    "operation_id": "fc-publish-conflict",
                    "documents": [
                        {
                            "relative_path": "plans/conflict.md",
                            "content": "retained local content\n",
                        }
                    ],
                },
                PublicationDestination(
                    root=source,
                    worktree_root=root / "worktrees",
                    remote="origin",
                    branch=branch,
                    require_remote_sync=True,
                ),
            )
            self.assertEqual(result["local"]["state"], "observed")
            self.assertEqual(result["remote"]["state"], "failed")
            self.assertEqual(result["repair"]["kind"], "publication_conflict")
            self.assertEqual(
                (Path(result["worktree_path"]) / "plans" / "conflict.md").read_text(
                    encoding="utf-8"
                ),
                "retained local content\n",
            )
            self.assertEqual(
                git(writer, "show", f"origin/{branch}:plans/conflict.md"),
                "remote content",
            )

            outside = root / "outside"
            outside.mkdir()
            (source / "escape").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(KnowledgePublicationError):
                publisher.validate(
                    [
                        {
                            "relative_path": "escape/forbidden.md",
                            "content": "must not escape\n",
                        }
                    ],
                    PublicationDestination(
                        root=source,
                        worktree_root=root / "other-worktrees",
                        remote="origin",
                        branch=branch,
                        require_remote_sync=True,
                    ),
                )
            self.assertFalse((outside / "forbidden.md").exists())
            with self.assertRaises(KnowledgePublicationError):
                publisher.validate(
                    [
                        {
                            "relative_path": ".beads/dolt/forbidden.md",
                            "content": "must not stage ledger data\n",
                        }
                    ],
                    PublicationDestination(
                        root=source,
                        worktree_root=root / "ledger-worktrees",
                        remote="origin",
                        branch=branch,
                        require_remote_sync=True,
                    ),
                )

            (source / "plans").mkdir(exist_ok=True)
            manual = source / "plans" / "manual.md"
            manual.write_text("manual live edit\n", encoding="utf-8")
            with self.assertRaises(KnowledgePublicationError):
                publisher.publish(
                    {
                        "operation_id": "fc-publish-manual-conflict",
                        "documents": [
                            {
                                "relative_path": "plans/manual.md",
                                "content": "canonical Beads content\n",
                            }
                        ],
                    },
                    PublicationDestination(
                        root=source,
                        worktree_root=root / "manual-worktrees",
                        remote="origin",
                        branch=branch,
                        require_remote_sync=True,
                    ),
                )
            self.assertEqual(manual.read_text(encoding="utf-8"), "manual live edit\n")


class Fulcrum2KnowledgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name).resolve()
        cls.remote = cls.root / "knowledge.git"
        subprocess.run(
            ["git", "init", "--bare", str(cls.remote)],
            capture_output=True,
            check=True,
        )
        cls.brain = cls.root / "brain"
        subprocess.run(
            ["git", "clone", str(cls.remote), str(cls.brain)],
            capture_output=True,
            check=True,
        )
        configure_git(cls.brain)
        git(cls.brain, "config", "beads.role", "maintainer")
        cls.project = cls.root / "project"
        cls.project.mkdir()
        git(cls.project, "init", "--quiet")
        configure_git(cls.project)
        (cls.project / "README.md").write_text("toy\n", encoding="utf-8")
        git(cls.project, "add", "README.md")
        git(cls.project, "commit", "-m", "chore: initialize toy project")
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
            check=True,
            timeout=60,
        )
        cls.config = cls.brain / "fulcrum.yaml"
        cls.config.write_text(
            f"brain:\n  root: {cls.brain}\n"
            "knowledge:\n"
            f"  root: {cls.brain}\n"
            "  remote: origin\n"
            "  branch: null\n"
            "  require_remote_sync: true\n"
            "projects:\n"
            "  toy:\n"
            f"    root: {cls.project}\n"
            "    enabled: true\n",
            encoding="utf-8",
        )
        git(cls.brain, "add", ".gitignore", "fulcrum.yaml")
        git(cls.brain, "commit", "-m", "chore: initialize Fulcrum fixture")
        cls.branch = git(cls.brain, "symbolic-ref", "--short", "HEAD")
        git(cls.brain, "push", "origin", cls.branch)
        cls.instance_root = cls.root / "instance"
        cls.instance_root.mkdir()
        (cls.instance_root / "config").symlink_to(cls.config)
        cls.instance = resolve_instance(instance=str(cls.instance_root), config=None)
        cls.application = Application()
        cls.ledger = Ledger(cls.brain)

    @classmethod
    def tearDownClass(cls) -> None:
        subprocess.run(
            ["bd", "-C", str(cls.brain), "dolt", "stop"],
            capture_output=True,
            check=False,
            timeout=20,
        )
        cls.temporary.cleanup()

    def request(
        self,
        command: tuple[str, ...],
        *,
        arguments: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        actor: ActorContext | None = None,
        request_id: str | None = None,
        thread_id: str | None = None,
    ) -> ParsedRequest:
        return ParsedRequest(
            command=command,
            arguments=arguments or {},
            input=payload or {},
            actor=actor or ActorContext(kind="human"),
            instance=self.instance,
            request_id=request_id or str(uuid.uuid4()),
            thread_id=thread_id,
            timeout=60,
            offline=True,
        )

    def set_memory(
        self,
        title: str,
        text: str,
        *,
        scope: str = "project:toy",
        identifier: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "scope": scope,
            "title": title,
            "text": text,
            "references": ["evidence:fixture"],
        }
        if identifier:
            payload["id"] = identifier
        result = self.application.dispatch(
            self.request(("memory", "set"), payload=payload)
        )
        return dict(result.result["result"])

    def test_memory_replacement_retains_prior_text_and_context_is_bounded(self) -> None:
        created = self.set_memory(
            "Project preference",
            "Prefer explicit observable boundaries.",
        )
        memory_id = str(created["memory_id"])
        replaced = self.set_memory(
            "Project preference",
            "Prefer explicit observable boundaries after replacement.",
            identifier=memory_id,
        )
        self.assertEqual(
            replaced["previous"]["text"],
            "Prefer explicit observable boundaries.",
        )
        self.assertEqual(replaced["memory"]["origin"], created["memory"]["origin"])
        shown = self.application.dispatch(
            self.request(("memory", "show"), arguments={"id": memory_id})
        )
        self.assertEqual(
            shown.result["text"],
            "Prefer explicit observable boundaries after replacement.",
        )
        self.set_memory("Large lesson", "x" * 5000, scope="global")
        selection = select_memory(self.ledger, project_ids=("toy",), role="executor")
        self.assertLessEqual(selection["selected_text_characters"], 4000)
        self.assertTrue(selection["continuations"])
        rendered = render_selected_memory(selection)
        self.assertIn("Continuation: fulcrum memory show", rendered)

        work_id = "fc-memory-context"
        existing = self.ledger.show(work_id)
        if existing is None:
            self.ledger.create_record(
                record_id=work_id,
                kind="work",
                title="Memory context fixture",
                description="Use retained context",
                owner="replacement-thread",
                fc={
                    "kind": "work",
                    "project": "toy",
                    "owner": "replacement-thread",
                    "role": "executor",
                    "ownership_operation": "fc-memory-acquisition",
                    "outcome": "Use retained memory",
                    "acceptance": ["Memory is selected"],
                    "context": ["Original requirements remain complete."],
                    "waiting": None,
                    "next_action": "Use memory.",
                },
            )
        work = self.ledger.show(work_id)
        assert work is not None
        cooked = _cook_role(
            self.ledger,
            self.request(("context",), arguments={"bead": work_id}),
            work,
            "executor",
            "replacement-thread",
            "fc-memory-acquisition",
            start_operation="fc-memory-start",
        )
        self.assertIn("Original requirements remain complete.", cooked["instructions"])
        self.assertIn(
            "Prefer explicit observable boundaries after replacement.",
            cooked["instructions"],
        )

        manager = ConfigurationManager(self.config)
        document, _ = manager.load()
        with patch(
            "fulcrum.leadership.select_memory",
            side_effect=LedgerFailure(
                "memory unavailable", category="unavailable", retryable=True
            ),
        ):
            brief, _ = build_brief(
                self.ledger,
                manager.effective(document),
                requested_kind="auto",
                bead_id=None,
            )
        self.assertEqual(brief["memory_gap"], "memory unavailable")

        with self.assertRaises(FulcrumError) as unauthorized:
            self.application.dispatch(
                self.request(
                    ("memory", "set"),
                    payload={
                        "scope": "global",
                        "title": "Unauthorized",
                        "text": "Must become a proposal.",
                        "references": [],
                    },
                    actor=ActorContext(kind="task", task_id="ordinary-worker"),
                    thread_id="ordinary-worker",
                )
            )
        self.assertEqual(unauthorized.exception.code, "AUTHORITY_REQUIRED")

    def test_installed_cli_publishes_memory_and_authoritative_configuration(
        self,
    ) -> None:
        executable = Path(os.sys.executable).with_name("fulcrum")

        def invoke(
            *arguments: str, payload: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            completed = subprocess.run(
                [
                    str(executable),
                    *arguments,
                    "--instance",
                    str(self.instance_root),
                    "--offline",
                    "--actor",
                    "human",
                    "--json",
                ],
                input=json.dumps(payload) if payload is not None else None,
                capture_output=True,
                text=True,
                check=False,
                timeout=180,
            )
            self.assertEqual(
                completed.returncode, 0, completed.stderr + completed.stdout
            )
            return dict(json.loads(completed.stdout))

        memory = invoke(
            "memory",
            "set",
            "--input",
            "-",
            payload={
                "scope": "role:warden",
                "title": "Review lesson",
                "text": "Verify exact promoted source identities.",
                "references": ["test:installed-cli"],
            },
        )["result"]["result"]
        listed = invoke("memory", "list", "--scope", "role:warden", "--limit", "20")[
            "result"
        ]
        self.assertIn(memory["memory_id"], {item["id"] for item in listed["items"]})
        shown = invoke("memory", "show", str(memory["memory_id"]))["result"]
        self.assertEqual(shown["text"], "Verify exact promoted source identities.")
        published = invoke("knowledge", "publish", "--bead", str(memory["memory_id"]))[
            "result"
        ]["result"]
        facts = published["publication"]
        self.assertTrue(published["publication_ready"])
        self.assertEqual(facts["local"]["state"], "observed")
        self.assertTrue(facts["remote"]["contains_local"])
        selected_path = facts["selected_paths"][0]
        git(self.brain, "fetch", "origin", self.branch)
        exported = git(self.brain, "show", f"origin/{self.branch}:{selected_path}")
        self.assertIn("Verify exact promoted source identities.", exported)

        invoke(
            "config",
            "set",
            "--input",
            "-",
            payload={"policy": {"rationale": "Authorized current YAML fixture."}},
        )
        config_result = invoke("config", "sync")["result"]["result"]
        self.assertTrue(config_result["publication_ready"])
        operation = self.ledger.show(str(config_result["publication"]["operation_id"]))
        assert operation is not None and operation.fc
        retained = operation.fc["planned"]["publication_intent"]
        self.assertEqual(retained["documents"][0]["content"], self.config.read_text())
        git(self.brain, "fetch", "origin", self.branch)
        remote_config = git(self.brain, "show", f"origin/{self.branch}:fulcrum.yaml")
        self.assertIn("Authorized current YAML fixture.", remote_config)

    def test_plan_publish_exports_canonical_scope_and_settles_child_readiness(
        self,
    ) -> None:
        root_id = "fc-knowledge-plan"
        if self.ledger.show(root_id) is None:
            self.ledger.create_record(
                record_id=root_id,
                kind="work",
                title="Published knowledge plan",
                description="Export this approved plan",
                owner="HUMAN",
                issue_type="epic",
                acceptance="Canonical plan is published",
                fc={
                    "kind": "work",
                    "project": "toy",
                    "owner": "HUMAN",
                    "role": None,
                    "ownership_operation": "fc-knowledge-plan-acquisition",
                    "phase": "backlog",
                    "requested_role": "weaver",
                    "origin": {"request_id": "knowledge-plan-origin"},
                    "workflow_root": root_id,
                    "caused_by": None,
                    "models": {},
                    "plan": None,
                    "completion_cost": None,
                    "summary": None,
                    "outcome": "Export this approved plan",
                    "acceptance": ["Canonical plan is published"],
                    "context": [],
                    "intake": None,
                    "size": "small",
                    "overlap_tags": [],
                    "next_action": "Author a plan.",
                    "last_progress_at": None,
                    "last_progress": None,
                    "waiting": None,
                    "dispatch": None,
                    "worktree": None,
                    "delivery": None,
                    "handoff": None,
                    "interrupted_work": None,
                    "active_operation": None,
                    "last_transition": "fc-knowledge-plan-acquisition",
                    "disposition": None,
                },
            )
        task = {
            "key": "documented-child",
            "title": "Deliver documented child",
            "outcome": "Produce the documented result",
            "acceptance": ["The documented result is observed"],
            "requested_role": "executor",
            "depends_on": [],
            "size": "small",
        }
        draft = {
            "text": "Implement the canonical published plan.",
            "tasks": [task],
            "summary": "One approved child supplies the result.",
            "publication": {
                "destination": "knowledge",
                "relative_path": "plans/knowledge-plan.md",
                "require_remote_sync": True,
            },
            "validation": {
                "summary": "Observe the child evidence.",
                "checks": [
                    {
                        "criterion": "The child meets acceptance",
                        "task_keys": ["documented-child"],
                        "evidence_required": "Observed child disposition",
                    }
                ],
            },
        }
        self.application.dispatch(
            self.request(("plan", "draft"), arguments={"bead": root_id}, payload=draft)
        )
        approved = self.application.dispatch(
            self.request(
                ("plan", "approve"),
                arguments={"bead": root_id},
                payload={
                    "reason": "The exact small scope is sufficient.",
                    "resolutions": {},
                    "waivers": [],
                },
            )
        ).result["result"]
        published = self.application.dispatch(
            self.request(
                ("plan", "publish"),
                arguments={"bead": root_id},
                payload={
                    "approval_operation": approved["approval_operation"],
                    "approved_by": approved["approved_by"],
                    "approval_evidence": approved["approval_evidence"],
                    "activation": "active",
                    "text": draft["text"],
                    "reviews": approved["approved_scope"]["reviews"],
                    "tasks": draft["tasks"],
                    "publication": draft["publication"],
                },
            )
        ).result["result"]
        self.assertIsNotNone(published["publication_operation"])
        root = self.ledger.show(root_id)
        assert root is not None and root.fc
        plan = root.fc["plan"]
        self.assertTrue(plan["publication_ready"])
        self.assertEqual(plan["publication"]["local"]["state"], "observed")
        self.assertTrue(plan["publication"]["remote"]["contains_local"])
        child_id = str(published["children_by_key"]["documented-child"])
        child = self.ledger.show(child_id)
        assert child is not None and child.fc
        self.assertTrue(child.fc["plan"]["publication_ready"])
        self.assertIsNone(child.fc["waiting"])
        git(self.brain, "fetch", "origin", self.branch)
        exported = git(
            self.brain,
            "show",
            f"origin/{self.branch}:plans/knowledge-plan.md",
        )
        self.assertIn("Implement the canonical published plan.", exported)
        self.assertIn(child_id, exported)

        refined_task = {
            **task,
            "title": "Deliver refined documented child",
            "outcome": "Produce the refined documented result",
        }
        refined_draft = {
            **draft,
            "text": "Implement the refined canonical published plan.",
            "tasks": [refined_task],
            "summary": "The retained child ID now carries refined approved scope.",
        }
        self.application.dispatch(
            self.request(
                ("plan", "draft"),
                arguments={"bead": root_id},
                payload=refined_draft,
            )
        )
        refined_approval = self.application.dispatch(
            self.request(
                ("plan", "approve"),
                arguments={"bead": root_id},
                payload={
                    "reason": "The refined exact scope remains bounded.",
                    "resolutions": {},
                    "waivers": [],
                },
            )
        ).result["result"]
        refined = self.application.dispatch(
            self.request(
                ("plan", "refine"),
                arguments={"bead": root_id},
                payload={
                    "approval_operation": refined_approval["approval_operation"],
                    "tasks": refined_draft["tasks"],
                    "publication": refined_draft["publication"],
                    "dispositions": {},
                    "cosmetic_changes": [],
                },
            )
        ).result["result"]
        self.assertEqual(refined["children_by_key"]["documented-child"], child_id)
        refined_root = self.ledger.show(root_id)
        assert refined_root is not None and refined_root.fc
        refined_plan = refined_root.fc["plan"]
        self.assertTrue(refined_plan["publication_ready"])
        self.assertIsNone(refined_plan["previous_publication"])
        git(self.brain, "fetch", "origin", self.branch)
        refined_export = git(
            self.brain,
            "show",
            f"origin/{self.branch}:plans/knowledge-plan.md",
        )
        self.assertIn("Implement the refined canonical published plan.", refined_export)
        self.assertIn(child_id, refined_export)


if __name__ == "__main__":
    unittest.main()
