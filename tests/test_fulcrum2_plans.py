from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any

from fulcrum.application import Application
from fulcrum.contracts import ActorContext, FulcrumError, ParsedRequest
from fulcrum.instance import resolve_instance
from fulcrum.ledger import Ledger
from fulcrum.supervision import ControllerSupervisor


class Fulcrum2PlanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name).resolve()
        cls.brain = cls.root / "brain"
        cls.instance_root = cls.root / "instance"
        cls.project = cls.root / "project"
        for path in (cls.brain, cls.instance_root, cls.project):
            path.mkdir()
        for path in (cls.brain, cls.project):
            subprocess.run(["git", "init", "--quiet"], cwd=path, check=True)
            subprocess.run(
                ["git", "config", "user.email", "fixture@example.com"],
                cwd=path,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Fixture"],
                cwd=path,
                check=True,
            )
            subprocess.run(
                ["git", "config", "beads.role", "maintainer"],
                cwd=path,
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
            check=True,
            timeout=60,
        )
        config = cls.brain / "fulcrum.yaml"
        config.write_text(
            f"brain:\n  root: {cls.brain}\n"
            "projects:\n"
            "  toy:\n"
            f"    root: {cls.project}\n"
            "    enabled: true\n",
            encoding="utf-8",
        )
        (cls.instance_root / "config").symlink_to(config)
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
        ownership_operation: str | None = None,
    ) -> ParsedRequest:
        return ParsedRequest(
            command=command,
            arguments=arguments or {},
            input=payload or {},
            actor=actor or ActorContext(kind="human"),
            instance=self.instance,
            request_id=request_id or str(uuid.uuid4()),
            thread_id=thread_id,
            ownership_operation=ownership_operation,
            timeout=30,
            offline=True,
        )

    def create_root(self, name: str, *, size: str = "small") -> str:
        identifier = f"fc-plan-{name}"
        self.ledger.create_record(
            record_id=identifier,
            kind="work",
            title=f"Plan {name}",
            description=f"Complete {name}",
            acceptance=f"{name} is complete",
            owner="HUMAN",
            issue_type="epic",
            fc={
                "kind": "work",
                "project": "toy",
                "owner": "HUMAN",
                "role": None,
                "ownership_operation": f"fc-acquire-{name}",
                "phase": "backlog",
                "requested_role": "weaver",
                "origin": {"request_id": f"origin-{name}"},
                "workflow_root": identifier,
                "caused_by": None,
                "models": {},
                "plan": None,
                "completion_cost": None,
                "summary": None,
                "outcome": f"Complete {name}",
                "acceptance": [f"{name} is complete"],
                "context": [],
                "intake": None,
                "size": size,
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
                "last_transition": f"fc-acquire-{name}",
                "disposition": None,
            },
        )
        return identifier

    def draft(
        self,
        root: str,
        tasks: list[dict[str, Any]],
        *,
        text: str = "Implement the retained plan.",
        publication: dict[str, Any] | None = None,
        assembled_check: bool = False,
    ) -> dict[str, Any]:
        checks = [
            {
                "criterion": f"{item['key']} meets its acceptance",
                "task_keys": [item["key"]],
                "evidence_required": "Observed child disposition and evidence",
            }
            for item in tasks
        ]
        if assembled_check:
            checks.append(
                {
                    "criterion": "The assembled deliverables satisfy the root outcome",
                    "task_keys": [str(item["key"]) for item in tasks],
                    "evidence_required": "A deliberately justified assembled-system check",
                }
            )
        value = {
            "text": text,
            "tasks": tasks,
            "summary": f"Complete {root} through its approved obligations.",
            "publication": publication,
            "validation": {
                "summary": "Use the named child acceptance evidence.",
                "checks": checks,
            },
        }
        result = self.application.dispatch(
            self.request(("plan", "draft"), arguments={"bead": root}, payload=value)
        )
        self.assertEqual(result.result["result"]["draft"], value)
        return value

    def approve(self, root: str, *, waived: bool = False) -> tuple[str, dict[str, Any]]:
        waivers = (
            [
                {
                    "perspective": perspective,
                    "reason": "Fixture uses explicit bounded review waiver.",
                }
                for perspective in ("cold_reader", "requirements")
            ]
            if waived
            else []
        )
        result = self.application.dispatch(
            self.request(
                ("plan", "approve"),
                arguments={"bead": root},
                payload={
                    "reason": "The exact retained scope is proportionate and complete.",
                    "resolutions": {},
                    "waivers": waivers,
                },
            )
        )
        body = dict(result.result["result"])
        return str(body["approval_operation"]), body

    def publish(
        self,
        root: str,
        draft: dict[str, Any],
        approval: str,
        approval_body: dict[str, Any],
        *,
        activation: str = "active",
        request_id: str | None = None,
    ) -> Any:
        return self.application.dispatch(
            self.request(
                ("plan", "publish"),
                arguments={"bead": root},
                payload={
                    "approval_operation": approval,
                    "approved_by": approval_body["approved_by"],
                    "approval_evidence": approval_body["approval_evidence"],
                    "activation": activation,
                    "text": draft["text"],
                    "reviews": approval_body["approved_scope"]["reviews"],
                    "tasks": draft["tasks"],
                    "publication": draft["publication"],
                },
                request_id=request_id,
            )
        )

    @staticmethod
    def task(key: str, *, depends_on: list[str] | None = None) -> dict[str, Any]:
        return {
            "key": key,
            "title": f"Deliver {key}",
            "outcome": f"Produce the {key} outcome",
            "acceptance": [f"{key} is observed"],
            "requested_role": "executor",
            "depends_on": depends_on or [],
            "size": "small",
        }

    def close_child(self, identifier: str) -> None:
        result = self.application.dispatch(
            self.request(
                ("work", "close"),
                arguments={
                    "id": identifier,
                    "outcome": "answered",
                    "summary": "The approved child obligation is satisfied.",
                },
            )
        )
        self.assertTrue(result.ok)

    def test_small_plan_closes_without_a_validation_child_and_reopens_with_history(
        self,
    ) -> None:
        root = self.create_root("small")
        draft = self.draft(root, [self.task("implementation")])
        approval, approval_body = self.approve(root)
        published = self.publish(root, draft, approval, approval_body)
        children = published.result["result"]["children_by_key"]
        self.assertEqual(set(children), {"implementation"})
        child = str(children["implementation"])
        child_before_close = self.ledger.show(child)
        self.assertEqual(
            len(self.ledger.children(root)),
            1,
            child_before_close.native if child_before_close is not None else None,
        )
        self.close_child(child)
        supervisor = ControllerSupervisor(
            self.request(("reconcile",)), self.application
        )
        actions, closed_roots = asyncio.run(
            supervisor._complete_plan_roots(self.ledger.list_records(limit=0))
        )
        self.assertEqual(closed_roots, {root})
        self.assertTrue(
            next(row for row in actions if row["bead_id"] == root)["root_closed"]
        )
        closed = self.ledger.show(root)
        assert closed is not None and closed.fc
        historical_cost = {
            "state": "finalized",
            "summary_bead": "fc-historical-cost",
        }
        fc = dict(closed.fc)
        fc["completion_cost"] = historical_cost
        self.ledger.update_fc(root, fc)
        self.application.dispatch(
            self.request(
                ("work", "reopen"),
                arguments={"id": root, "reason": "New evidence needs another interval"},
            )
        )
        reopened = self.ledger.show(root)
        assert reopened is not None and reopened.fc
        self.assertEqual(reopened.fc["completion_cost"], historical_cost)
        self.assertNotEqual(
            reopened.fc["ownership_operation"], closed.fc["ownership_operation"]
        )

    def test_future_plan_requires_exact_authorization_and_remote_readiness(
        self,
    ) -> None:
        root = self.create_root("future")
        draft = self.draft(root, [self.task("later")])
        approval, approval_body = self.approve(root)
        published = self.publish(
            root, draft, approval, approval_body, activation="future"
        )
        child = str(published.result["result"]["children_by_key"]["later"])
        child_record = self.ledger.show(child)
        assert child_record is not None and child_record.fc
        self.assertEqual(child_record.fc["plan"]["activation"], "future")
        with self.assertRaises(FulcrumError) as missing:
            self.application.dispatch(
                self.request(
                    ("plan", "activate"),
                    arguments={"id": root, "authorization": "fc-not-authorized"},
                    actor=ActorContext(kind="controller"),
                )
            )
        self.assertEqual(missing.exception.code, "ACTIVATION_NOT_AUTHORIZED")
        authorized = self.application.dispatch(
            self.request(("plan", "activate"), arguments={"id": root})
        )
        authorization = authorized.result["result"]["activation_authorization"]
        still_future = self.ledger.show(root)
        assert still_future is not None and still_future.fc
        self.assertEqual(still_future.fc["plan"]["activation"], "future")
        dispatch_attempt = self.application.dispatch(
            self.request(
                ("dispatch",),
                arguments={"bead": root, "authorize": True},
            )
        )
        self.assertFalse(dispatch_attempt.ok)
        dispatch_receipt = self.ledger.show(str(dispatch_attempt.operation_id))
        assert dispatch_receipt is not None and dispatch_receipt.fc
        self.assertEqual(dispatch_receipt.fc["error"]["code"], "ACTIVATION_REQUIRED")
        activated = self.application.dispatch(
            self.request(
                ("plan", "activate"),
                arguments={
                    "id": root,
                    "authorization": authorization["operation_id"],
                },
                actor=ActorContext(kind="controller"),
            )
        )
        self.assertEqual(activated.result["result"]["activation"], "active")
        current_child = self.ledger.show(child)
        assert current_child is not None and current_child.fc
        self.assertIsNone(current_child.fc["waiting"])

        remote_root = self.create_root("remote")
        remote_publication = {
            "destination": "knowledge",
            "relative_path": "plans/remote.md",
            "require_remote_sync": True,
        }
        remote_draft = self.draft(
            remote_root,
            [self.task("remote-child")],
            publication=remote_publication,
        )
        remote_approval, remote_body = self.approve(remote_root)
        remote_published = self.publish(
            remote_root,
            remote_draft,
            remote_approval,
            remote_body,
            activation="future",
        )
        remote_child = self.ledger.show(
            str(remote_published.result["result"]["children_by_key"]["remote-child"])
        )
        assert remote_child is not None and remote_child.fc
        self.assertFalse(remote_child.fc["plan"]["publication_ready"])
        remote_authorized = self.application.dispatch(
            self.request(("plan", "activate"), arguments={"id": remote_root})
        )
        remote_auth = remote_authorized.result["result"]["activation_authorization"]
        with self.assertRaises(FulcrumError) as pending:
            self.application.dispatch(
                self.request(
                    ("plan", "activate"),
                    arguments={
                        "id": remote_root,
                        "authorization": remote_auth["operation_id"],
                    },
                    actor=ActorContext(kind="controller"),
                )
            )
        self.assertEqual(pending.exception.code, "ACTIVATION_NOT_AUTHORIZED")

    def test_substantial_plan_rejects_missing_and_stale_review_evidence(self) -> None:
        root = self.create_root("reviews", size="large")
        old = self.draft(
            root,
            [self.task("one"), self.task("two")],
            assembled_check=True,
        )
        self.assertEqual(len(old["validation"]["checks"]), 3)
        with self.assertRaises(FulcrumError) as missing:
            self.approve(root)
        self.assertEqual(missing.exception.code, "STALE_REVIEW")
        approval, body = self.approve(root, waived=True)
        changed = self.draft(
            root,
            [self.task("one"), self.task("two")],
            text="A substantively changed retained plan.",
        )
        with self.assertRaises(FulcrumError) as stale:
            self.publish(root, old, approval, body)
        self.assertEqual(stale.exception.code, "APPROVAL_CONFLICT")
        self.assertNotEqual(old["text"], changed["text"])

    def test_refinement_preserves_delivered_ids_and_blocks_active_scope_changes(
        self,
    ) -> None:
        root = self.create_root("refine", size="large")
        initial = self.draft(root, [self.task("kept"), self.task("removed")])
        approval, body = self.approve(root, waived=True)
        published = self.publish(root, initial, approval, body)
        mapping = dict(published.result["result"]["children_by_key"])
        kept = str(mapping["kept"])
        removed = str(mapping["removed"])
        self.close_child(kept)
        removed_record = self.ledger.show(removed)
        assert removed_record is not None and removed_record.fc
        removed_fc = dict(removed_record.fc)
        removed_fc["owner"] = "active-executor"
        removed_fc["role"] = "executor"
        removed_fc["phase"] = "working"
        removed_fc["ownership_operation"] = "fc-active-refinement"
        self.ledger.update_fc(
            removed, removed_fc, assignee="active-executor", status="in_progress"
        )
        refined_draft = self.draft(root, [self.task("kept"), self.task("added")])
        refined_approval, _ = self.approve(root, waived=True)
        refinement_payload = {
            "approval_operation": refined_approval,
            "tasks": refined_draft["tasks"],
            "publication": refined_draft["publication"],
            "dispositions": {
                "removed": {
                    "outcome": "cancelled",
                    "reason": "The approved replacement makes this unfinished work obsolete.",
                }
            },
            "cosmetic_changes": [],
        }
        with self.assertRaises(FulcrumError) as active:
            self.application.dispatch(
                self.request(
                    ("plan", "refine"),
                    arguments={"bead": root},
                    payload=refinement_payload,
                )
            )
        self.assertEqual(active.exception.code, "ACTIVE_SCOPE_CHANGE")
        stopped = self.ledger.show(removed)
        assert stopped is not None and stopped.fc
        stopped_fc = dict(stopped.fc)
        stopped_fc["owner"] = "HUMAN"
        stopped_fc["role"] = None
        stopped_fc["phase"] = "backlog"
        self.ledger.update_fc(removed, stopped_fc, assignee="HUMAN", status="open")
        refinement_request = str(uuid.uuid4())
        refined = self.application.dispatch(
            self.request(
                ("plan", "refine"),
                arguments={"bead": root},
                payload=refinement_payload,
                request_id=refinement_request,
            )
        )
        refined_mapping = dict(refined.result["result"]["children_by_key"])
        self.assertEqual(refined_mapping["kept"], kept)
        self.assertEqual(refined_mapping["removed"], removed)
        self.assertIn("added", refined_mapping)
        interrupted = self.ledger.show(str(refined.operation_id))
        assert interrupted is not None and interrupted.fc
        interrupted_fc = dict(interrupted.fc)
        interrupted_fc["state"] = "accepted"
        interrupted_fc["step"] = "children_created_before_graph_receipt_settled"
        interrupted_fc.pop("completed_at", None)
        self.ledger.update_fc(interrupted.id, interrupted_fc, status="open")
        added_before_recovery = self.ledger.show(str(refined_mapping["added"]))
        assert added_before_recovery is not None and added_before_recovery.fc
        added_fc = dict(added_before_recovery.fc)
        added_fc["plan"] = None
        self.ledger.update_fc(added_before_recovery.id, added_fc)
        replayed = self.application.dispatch(
            self.request(
                ("plan", "refine"),
                arguments={"bead": root},
                payload=refinement_payload,
                request_id=refinement_request,
            )
        )
        self.assertEqual(replayed.operation_id, refined.operation_id)
        self.assertEqual(replayed.result["state"], "completed")
        recovered_added = self.ledger.show(str(refined_mapping["added"]))
        assert recovered_added is not None and recovered_added.fc
        self.assertEqual(recovered_added.fc["plan"]["key"], "added")
        self.assertEqual(len(self.ledger.children(root)), 3)
        cancelled = self.ledger.show(removed)
        assert cancelled is not None and cancelled.fc
        self.assertEqual(cancelled.fc["disposition"]["outcome"], "cancelled")
        incomplete = self.application.dispatch(
            self.request(("plan", "complete"), arguments={"id": root})
        )
        self.assertFalse(incomplete.result["root_closed"])
        self.assertEqual(incomplete.result["unsatisfied"][0]["key"], "added")
        added = str(refined_mapping["added"])
        self.application.dispatch(
            self.request(
                ("work", "close"),
                arguments={
                    "id": added,
                    "outcome": "cancelled",
                    "summary": "A cancelled current obligation must block root success.",
                },
            )
        )
        cancelled_current = self.application.dispatch(
            self.request(("plan", "complete"), arguments={"id": root})
        )
        self.assertFalse(cancelled_current.result["root_closed"])
        self.assertEqual(
            cancelled_current.result["unsatisfied"][0]["outcome"], "cancelled"
        )
        self.application.dispatch(
            self.request(
                ("work", "reopen"),
                arguments={
                    "id": added,
                    "reason": "The current obligation must be delivered",
                },
            )
        )
        self.close_child(added)
        complete = self.application.dispatch(
            self.request(("plan", "complete"), arguments={"id": root})
        )
        self.assertTrue(complete.result["result"]["root_closed"])

    def test_installed_cli_plan_lifecycle_uses_canonical_receipts(self) -> None:
        root = self.create_root("installed")
        draft = {
            "text": "Deliver the installed CLI plan fixture.",
            "tasks": [self.task("cli-child")],
            "summary": "One CLI-created plan child completes the root.",
            "publication": None,
            "validation": {
                "summary": "The child disposition proves the fixture.",
                "checks": [
                    {
                        "criterion": "The CLI child closes successfully",
                        "task_keys": ["cli-child"],
                        "evidence_required": "Observed answered disposition",
                    }
                ],
            },
        }

        def invoke(
            *arguments: str, payload: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            executable = Path(os.sys.executable).with_name("fulcrum")
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
                timeout=120,
            )
            self.assertEqual(
                completed.returncode, 0, completed.stderr + completed.stdout
            )
            return dict(json.loads(completed.stdout))

        invoke("plan", "draft", "--bead", root, "--input", "-", payload=draft)
        approved = invoke(
            "plan",
            "approve",
            "--bead",
            root,
            "--input",
            "-",
            payload={
                "reason": "The small fixture has exact bounded scope.",
                "resolutions": {},
                "waivers": [],
            },
        )["result"]["result"]
        published = invoke(
            "plan",
            "publish",
            "--bead",
            root,
            "--input",
            "-",
            payload={
                "approval_operation": approved["approval_operation"],
                "approved_by": approved["approved_by"],
                "approval_evidence": approved["approval_evidence"],
                "activation": "active",
                "text": draft["text"],
                "reviews": approved["approved_scope"]["reviews"],
                "tasks": draft["tasks"],
                "publication": None,
            },
        )["result"]["result"]
        self.close_child(str(published["children_by_key"]["cli-child"]))
        completed = invoke("plan", "complete", root)
        self.assertTrue(completed["result"]["result"]["root_closed"])
        shown = invoke("plan", "show", root)
        self.assertTrue(shown["result"]["root_closed"])
        self.assertEqual(
            shown["result"]["children_by_key"], published["children_by_key"]
        )

    def test_weaver_finish_planned_ends_authoring_without_activating_root(self) -> None:
        root = self.create_root("weaver-future")
        thread = "weaver-plan-author"
        ownership = "fc-weaver-plan-acquisition"
        root_record = self.ledger.show(root)
        assert root_record is not None and root_record.fc
        root_fc = dict(root_record.fc)
        root_fc["owner"] = thread
        root_fc["role"] = "weaver"
        root_fc["phase"] = "working"
        root_fc["ownership_operation"] = ownership
        self.ledger.update_fc(root, root_fc, assignee=thread, status="in_progress")
        self.ledger.create_record(
            record_id="fc-weaver-plan-task",
            kind="task",
            title="Managed Weaver plan author",
            description=f"Weaver authoring {root}",
            owner=thread,
            external_ref=f"fulcrum:thread:{thread}",
            fc={
                "kind": "task",
                "owner": thread,
                "thread_id": thread,
                "role": "weaver",
                "purpose": "work",
                "work_bead": root,
                "associated_beads": [],
                "ownership_operation": ownership,
                "creation_operation": ownership,
                "deleted_at": None,
                "last_transition": ownership,
            },
        )
        draft = self.draft(root, [self.task("future-child")])
        approval, body = self.approve(root)
        publish_payload = {
            "approval_operation": approval,
            "approved_by": body["approved_by"],
            "approval_evidence": body["approval_evidence"],
            "activation": "future",
            "text": draft["text"],
            "reviews": body["approved_scope"]["reviews"],
            "tasks": draft["tasks"],
            "publication": draft["publication"],
        }
        published = self.application.dispatch(
            self.request(
                ("plan", "publish"),
                arguments={"bead": root},
                payload=publish_payload,
                actor=ActorContext(kind="task", task_id=thread),
                thread_id=thread,
                ownership_operation=ownership,
            )
        )
        child_id = str(published.result["result"]["children_by_key"]["future-child"])
        before_finish = self.ledger.show(root)
        child_before = self.ledger.show(child_id)
        assert before_finish is not None and before_finish.fc
        assert child_before is not None and child_before.fc
        self.assertEqual(before_finish.fc["owner"], thread)
        self.assertFalse(before_finish.fc["plan"]["authoring_ready"])
        self.assertFalse(child_before.fc["plan"]["authoring_ready"])
        finish_request = str(uuid.uuid4())
        finish_command = self.request(
            ("finish",),
            arguments={"bead": root, "outcome": "planned"},
            payload={
                "summary": "Published the approved future plan and ended authoring.",
                "plan_id": root,
            },
            actor=ActorContext(kind="task", task_id=thread),
            thread_id=thread,
            ownership_operation=ownership,
            request_id=finish_request,
        )
        finished = self.application.dispatch(finish_command)
        self.assertTrue(finished.result["result"]["accepted"])
        replayed_finish = self.application.dispatch(finish_command)
        self.assertEqual(replayed_finish.operation_id, finished.operation_id)
        after_finish = self.ledger.show(root)
        child_after = self.ledger.show(child_id)
        assert after_finish is not None and after_finish.fc
        assert child_after is not None and child_after.fc
        self.assertEqual(after_finish.status, "open")
        self.assertEqual(after_finish.fc["plan"]["activation"], "future")
        self.assertTrue(after_finish.fc["plan"]["authoring_ready"])
        self.assertEqual(
            {row["kind"] for row in after_finish.fc["waiting"]["reasons"]},
            {"future_activation"},
        )
        self.assertTrue(child_after.fc["plan"]["authoring_ready"])
        self.assertEqual(
            child_after.fc["next_action"],
            "Wait for explicit activation of the approved future plan.",
        )
        self.assertEqual(
            {row["kind"] for row in child_after.fc["waiting"]["reasons"]},
            {"future_activation"},
        )


if __name__ == "__main__":
    unittest.main()
