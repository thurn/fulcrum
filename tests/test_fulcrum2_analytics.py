from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

from fulcrum.analytics import AnalyticsService, seed_bundled_rates
from fulcrum.ledger import Ledger, random_record_id
from fulcrum.runtime import TaskFacts

OBSERVED_AT = "2026-09-14T18:00:00Z"


class Fulcrum2AnalyticsTest(unittest.TestCase):
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
            f"brain:\n  root: {cls.brain}\nprojects:\n  toy:\n    root: {cls.project}\n    enabled: true\n",
            encoding="utf-8",
        )
        (cls.instance / "config").symlink_to(config)
        cls.executable = Path(os.sys.executable).with_name("fulcrum")
        cls.ledger = Ledger(cls.brain)
        seed_bundled_rates(cls.ledger)

    @classmethod
    def tearDownClass(cls) -> None:
        subprocess.run(
            ["bd", "-C", str(cls.brain), "dolt", "stop"],
            capture_output=True,
            check=False,
            timeout=20,
        )
        cls.temporary.cleanup()

    def root_record(self, title: str) -> str:
        identifier = random_record_id()
        self.ledger.create_record(
            record_id=identifier,
            kind="work",
            title=title,
            description=title,
            owner="HUMAN",
            fc={
                "kind": "work",
                "owner": "HUMAN",
                "project": "toy",
                "workflow_root": identifier,
                "completion_cost": None,
                "disposition": None,
            },
        )
        return identifier

    def task_record(
        self,
        root: str,
        *,
        purpose: str = "work",
        role: str = "executor",
        operation_id: str = "fc-operation-fixture",
        associated_beads: list[str] | None = None,
    ) -> tuple[str, str]:
        identifier = random_record_id()
        thread_id = f"thread-{uuid.uuid4()}"
        self.ledger.create_record(
            record_id=identifier,
            kind="task",
            title=f"{role} task",
            description="Managed native task",
            owner=thread_id,
            fc={
                "kind": "task",
                "owner": thread_id,
                "thread_id": thread_id,
                "work_bead": root,
                "associated_beads": associated_beads or [root],
                "role": role,
                "purpose": purpose,
                "model": "gpt-5.6-luna",
                "effort": "medium",
                "last_turn": {"id": "turn-1", "operation_id": operation_id},
            },
        )
        return identifier, thread_id

    def facts(
        self,
        thread_id: str,
        *,
        turn_id: str = "turn-1",
        responses: list[dict[str, object]] | None = None,
    ) -> TaskFacts:
        return TaskFacts(
            id=thread_id,
            title="fixture",
            cwd=str(self.project),
            project_id=None,
            workspace_roots=(str(self.project),),
            archived=False,
            exists=True,
            loaded=True,
            runtime_status="idle",
            active_turn=None,
            last_turn={
                "id": turn_id,
                "status": "completed",
                "usage": {
                    "responses": (
                        responses if responses is not None else [priced_response()]
                    )
                },
            },
            pending_requests=(),
            observed_at=OBSERVED_AT,
        )

    def invoke(
        self, *arguments: str, payload: object | None = None
    ) -> dict[str, object]:
        result = subprocess.run(
            [str(self.executable), *arguments],
            input=json.dumps(payload) if payload is not None else None,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return json.loads(result.stdout)

    def test_cumulative_turns_are_replaced_and_cli_cost_is_exact(self) -> None:
        root = self.root_record("Exact cost")
        task_id, thread_id = self.task_record(root)
        service = AnalyticsService()
        first = service.observe_task(
            self.ledger, self.ledger.show(task_id), self.facts(thread_id)
        )
        self.assertIsNotNone(first)
        assert first is not None
        second = service.observe_task(
            self.ledger, self.ledger.show(task_id), self.facts(thread_id)
        )
        self.assertEqual(first.id, second.id if second else None)
        replacement = priced_response()
        replacement["outputTokens"] = 600_000
        service.observe_task(
            self.ledger,
            self.ledger.show(task_id),
            self.facts(thread_id, responses=[replacement]),
        )
        analytics = [
            item
            for item in self.ledger.list_records(kind="analytics", limit=0)
            if item.native.get("external_ref") == f"fulcrum:usage:{thread_id}:turn-1"
        ]
        self.assertEqual(len(analytics), 1)

        envelope = self.invoke(
            "cost",
            "--workflow",
            root,
            "--group-by",
            "role",
            "--instance",
            str(self.instance),
            "--json",
        )
        report = envelope["result"]
        self.assertEqual(report["total"], "0.889000")
        self.assertEqual(report["priced_subtotal"], "0.889000")
        self.assertEqual(report["components"]["direct"], "0.889000")
        self.assertEqual(report["included_native_turn_ids"], [f"{thread_id}:turn-1"])
        usage = self.invoke(
            "usage",
            "--bead",
            root,
            "--instance",
            str(self.instance),
            "--json",
        )["result"]
        self.assertEqual(usage["totals"]["input_tokens"], "1000000")

    def test_runtime_cumulative_event_replaces_one_response_boundary(self) -> None:
        root = self.root_record("Runtime usage events")
        task_id, thread_id = self.task_record(root)
        service = AnalyticsService()
        for output in (500_000, 600_000):
            response = priced_response()
            response["outputTokens"] = output
            event = {
                "threadId": thread_id,
                "turnId": "turn-1",
                "responseId": "response-1",
                "effectiveModel": "gpt-5.6-luna",
                "serviceTier": "standard",
                "tokenUsage": {"total": response, "last": response},
            }
            service.record_runtime_event(
                self.ledger,
                self.ledger.show(task_id),
                "turn/tokenUsage/updated",
                event,
            )
        observed = service.observe_task(
            self.ledger,
            self.ledger.show(task_id),
            self.facts(thread_id, responses=[]),
        )
        assert observed is not None and observed.fc
        self.assertEqual(observed.fc["usage"]["output_tokens"], 600_000)
        self.assertEqual(len(observed.fc["response_records"]), 1)
        self.assertEqual(
            observed.fc["response_records"][0]["priced_subtotal"], "0.889000"
        )
        retained_task = self.ledger.show(task_id)
        assert retained_task is not None and retained_task.fc
        retained_fc = dict(retained_task.fc)
        retained_fc["last_observed"] = self.facts(thread_id, responses=[]).to_dict()
        self.ledger.update_fc(task_id, retained_fc)
        reconciled = self.invoke(
            "usage",
            "reconcile",
            "--bead",
            root,
            "--instance",
            str(self.instance),
            "--offline",
            "--actor",
            "human",
            "--request-id",
            str(uuid.uuid4()),
            "--json",
        )
        self.assertEqual(reconciled["state"], "completed")
        self.assertEqual(
            reconciled["result"]["result"]["observed_analytics"], [observed.id]
        )

    def test_reroute_review_and_shared_marshal_attribution(self) -> None:
        service = AnalyticsService()
        first_root = self.root_record("First shared root")
        second_root = self.root_record("Second shared root")
        review_task, review_thread = self.task_record(
            first_root, purpose="plan_review", role="weaver"
        )
        service.observe_task(
            self.ledger, self.ledger.show(review_task), self.facts(review_thread)
        )

        operation_id = random_record_id()
        self.ledger.create_record(
            record_id=operation_id,
            kind="operation",
            title="Shared decision",
            description="Explicit roots",
            owner="marshal-thread",
            fc={
                "kind": "operation",
                "planned": {"selected_ids": [first_root, second_root, first_root]},
            },
        )
        marshal_task, marshal_thread = self.task_record(
            first_root,
            purpose="leadership",
            role="marshal",
            operation_id=operation_id,
            associated_beads=[first_root, second_root],
        )
        marshal = self.ledger.show(marshal_task)
        assert marshal is not None and marshal.fc
        marshal_fc = dict(marshal.fc)
        marshal_fc["pending_model_reroute"] = {
            "event_id": "reroute-1",
            "model": "gpt-5.6-luna",
            "service_tier": "standard",
            "method": "turn/model/rerouted",
            "turn_id": "turn-1",
        }
        self.ledger.update_fc(marshal.id, marshal_fc)
        astra_response = priced_response()
        astra_response["model"] = "gpt-6-astra"
        observed = service.observe_task(
            self.ledger,
            self.ledger.show(marshal_task),
            self.facts(marshal_thread, responses=[astra_response]),
        )
        assert observed is not None and observed.fc
        self.assertEqual(observed.fc["model"]["effective"], "gpt-5.6-luna")
        self.assertIsNone(
            (self.ledger.show(marshal_task).fc or {}).get("pending_model_reroute")
        )
        repeated = service.observe_task(
            self.ledger,
            self.ledger.show(marshal_task),
            self.facts(marshal_thread, responses=[astra_response]),
        )
        self.assertEqual((repeated.fc or {})["model"]["effective"], "gpt-5.6-luna")

        first_cost = self.invoke(
            "cost",
            "--workflow",
            first_root,
            "--instance",
            str(self.instance),
            "--json",
        )["result"]
        self.assertEqual(first_cost["components"]["review"], "0.769000")
        self.assertEqual(first_cost["components"]["coordination"], "0.384500")
        second_cost = self.invoke(
            "cost",
            "--workflow",
            second_root,
            "--instance",
            str(self.instance),
            "--json",
        )["result"]
        self.assertEqual(second_cost["total"], "0.384500")

    def test_response_blocks_unknown_prices_and_late_completion_correction(
        self,
    ) -> None:
        service = AnalyticsService()
        root = self.root_record("Late correction")
        task_id, thread_id = self.task_record(root)
        unknown = [
            {
                **priced_response(),
                "id": f"unknown-{index}",
                "model": "unsupported-fixture-model",
            }
            for index in range(130)
        ]
        observed = service.observe_task(
            self.ledger,
            self.ledger.show(task_id),
            self.facts(thread_id, responses=unknown),
        )
        assert observed is not None and observed.fc
        self.assertEqual(len(observed.fc["response_records"]), 64)
        self.assertEqual(len(observed.fc["response_blocks"]), 2)
        report = self.invoke(
            "cost",
            "--workflow",
            root,
            "--instance",
            str(self.instance),
            "--json",
        )["result"]
        self.assertEqual(report["coverage"], "partial")
        self.assertIsNone(report["total"])
        self.assertIsNone(report["priced_subtotal"])

        closed_fc = dict(self.ledger.show(root).fc or {})
        closed_fc["disposition"] = {
            "outcome": "answered",
            "completed_at": OBSERVED_AT,
        }
        closed = self.ledger.update_fc(root, closed_fc, status="closed")
        original = service.finalize_root(self.ledger, closed, "completion-1")
        original_summary = original["summary_bead"]
        duplicate = service.finalize_root(
            self.ledger, self.ledger.show(root), "completion-1"
        )
        self.assertEqual(duplicate["summary_bead"], original_summary)

        custom = {
            "model": "unsupported-fixture-model",
            "currency": "USD",
            "effective_at": "2026-09-14T00:00:00Z",
            "retrieved_at": OBSERVED_AT,
            "source_url": "https://example.invalid/rates",
            "input_per_million": "0.2",
            "cached_input_per_million": "0.02",
            "cache_write_input_per_million": "0.25",
            "output_per_million": "1.2",
            "tiers": {"standard": "1"},
            "long_context": None,
            "tools": {},
        }
        added = self.invoke(
            "rates",
            "add",
            "--instance",
            str(self.instance),
            "--offline",
            "--actor",
            "human",
            "--request-id",
            str(uuid.uuid4()),
            "--input",
            "-",
            "--json",
            payload=custom,
        )
        self.assertEqual(added["state"], "completed")
        rate_id = added["result"]["result"]["rate_card"]["id"]
        listed = self.invoke(
            "rates",
            "list",
            "--limit",
            "2",
            "--instance",
            str(self.instance),
            "--json",
        )["result"]
        self.assertEqual(len(listed["items"]), 2)
        shown = self.invoke(
            "rates",
            "show",
            rate_id,
            "--instance",
            str(self.instance),
            "--json",
        )["result"]
        self.assertEqual(shown["card"], custom)
        service.observe_task(
            self.ledger,
            self.ledger.show(task_id),
            self.facts(thread_id, responses=unknown),
        )
        correction = service.finalize_root(
            self.ledger,
            self.ledger.show(root),
            "reconcile-1",
            correction=True,
        )
        self.assertNotEqual(correction["summary_bead"], original_summary)
        self.assertEqual(correction["prior_summary"], original_summary)
        self.assertEqual(correction["estimated_api_cost_usd"], "99.970000", correction)
        self.assertIsNotNone(self.ledger.show(original_summary))

    def test_missing_telemetry_finalizes_partial_and_reopen_uses_unique_lifetime_turns(
        self,
    ) -> None:
        service = AnalyticsService()
        root = self.root_record("Missing and reopened")
        task_id, thread_id = self.task_record(root)
        task = self.ledger.show(task_id)
        assert task is not None and task.fc
        task_fc = dict(task.fc)
        task_fc["last_observed"] = {
            **self.facts(thread_id).to_dict(),
            "last_turn": {"id": "turn-1", "status": "completed"},
        }
        self.ledger.update_fc(task.id, task_fc)
        root_fc = dict(self.ledger.show(root).fc or {})
        root_fc["disposition"] = {"completed_at": OBSERVED_AT}
        closed = self.ledger.update_fc(root, root_fc, status="closed")
        missing = service.finalize_root(self.ledger, closed, "missing-completion")
        self.assertEqual(missing["coverage"], "partial")
        self.assertIsNone(missing["estimated_api_cost_usd"])
        self.assertIsNone(missing["priced_subtotal_usd"])
        self.assertIn("terminal_usage_observation_missing", missing["missing_reasons"])

        service.observe_task(
            self.ledger, self.ledger.show(task_id), self.facts(thread_id)
        )
        corrected = service.finalize_root(
            self.ledger,
            self.ledger.show(root),
            "late-observation",
            correction=True,
        )
        self.assertEqual(corrected["estimated_api_cost_usd"], "0.769000")
        reopened = self.ledger.show(root)
        assert reopened is not None and reopened.fc
        reopened_fc = dict(reopened.fc)
        reopened_fc["disposition"] = None
        self.ledger.update_fc(root, reopened_fc, status="open")
        reclosed_fc = dict(reopened_fc)
        reclosed_fc["disposition"] = {"completed_at": "2026-09-14T19:00:00Z"}
        reclosed = self.ledger.update_fc(root, reclosed_fc, status="closed")
        lifetime = service.finalize_root(self.ledger, reclosed, "completion-2")
        self.assertEqual(lifetime["estimated_api_cost_usd"], "0.769000")


def priced_response() -> dict[str, object]:
    return {
        "id": "response-1",
        "model": "gpt-5.6-luna",
        "serviceTier": "standard",
        "inputTokens": 1_000_000,
        "cachedInputTokens": 200_000,
        "cacheWriteInputTokens": 100_000,
        "outputTokens": 500_000,
    }


if __name__ == "__main__":
    unittest.main()
