from __future__ import annotations

import json
import io
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fulcrum.cli import main
from fulcrum.store import Store, StoreError, format_usd


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "state.sqlite3")
        self.store.execute(
            "INSERT INTO projects(project_id, repo_path) VALUES ('fulcrum', '/tmp/fulcrum')"
        )
        self.store.execute(
            "INSERT INTO meta(key, value) VALUES ('global_limit', '10'), ('project_limits', ?) ",
            (json.dumps({"fulcrum": 10}),),
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_response_cost_formula_precision_subsets_and_reasoning(self) -> None:
        estimate = self.store.estimate_response_cost(
            requested_model="gpt-5.6-sol",
            effective_model="gpt-5.6-sol",
            processing_tier="standard",
            input_tokens=1_000,
            cached_input_tokens=600,
            cache_write_input_tokens=100,
            output_tokens=50,
            reasoning_output_tokens=40,
        )
        self.assertEqual(estimate["ordinary_input_tokens"], 300)
        self.assertEqual(estimate["amount"], "0.00294")
        self.assertEqual(format_usd(estimate["amount"]), "<$0.01")
        self.assertEqual(estimate["output_amount"], "0.001")

    def test_seeded_models_invalid_relationships_and_tier_rules(self) -> None:
        expected = {
            "gpt-5.6-sol": "0.0000476",
            "gpt-5.6-terra": "0.0000258",
            "gpt-5.6-luna": "0.00000258",
            "gpt-6-astra": "0.000119",
        }
        for model, amount in expected.items():
            estimate = self.store.estimate_response_cost(
                requested_model=model,
                effective_model=model,
                processing_tier="standard",
                input_tokens=10,
                cached_input_tokens=4,
                cache_write_input_tokens=2,
                output_tokens=1,
                reasoning_output_tokens=1,
            )
            self.assertEqual(estimate["amount"], amount, model)
        invalid = self.store.estimate_response_cost(
            requested_model="gpt-5.6-sol",
            effective_model="gpt-5.6-sol",
            processing_tier="standard",
            input_tokens=3,
            cached_input_tokens=2,
            cache_write_input_tokens=2,
            output_tokens=1,
            reasoning_output_tokens=2,
        )
        self.assertEqual(invalid["coverage"], "invalid")
        self.assertNotIn("amount", invalid)
        self.assertEqual(
            self.store.estimate_response_cost(
                requested_model="gpt-6-astra",
                effective_model="gpt-6-astra",
                processing_tier="flex",
                input_tokens=1,
                cached_input_tokens=0,
                cache_write_input_tokens=0,
                output_tokens=0,
            )["amount"],
            "0.000005",
        )

    def test_long_context_is_response_specific_and_sol_example_rounds_313(self) -> None:
        quantities = []
        remaining_ordinary = 116_994
        remaining_cached = 5_367_936
        for index in range(28):
            cached = min(191_712, remaining_cached)
            ordinary = remaining_ordinary if index == 27 else 4_179
            remaining_cached -= cached
            remaining_ordinary -= ordinary
            quantities.append((ordinary + cached, cached, ordinary))
        self.assertEqual(remaining_cached, 0)
        estimates = [
            self.store.estimate_response_cost(
                requested_model="gpt-5.6-sol",
                effective_model="gpt-5.6-sol",
                processing_tier="standard",
                input_tokens=total,
                cached_input_tokens=cached,
                cache_write_input_tokens=0,
                output_tokens=25_749 if index == 27 else 0,
            )
            for index, (total, cached, _ordinary) in enumerate(quantities)
        ]
        self.assertTrue(all(not item["applied_rules"] for item in estimates))
        total = sum((Decimal(item["amount"]) for item in estimates), Decimal("0"))
        self.assertEqual(format_usd(total), "$3.13")
        aggregate = self.store.estimate_response_cost(
            requested_model="gpt-5.6-sol",
            effective_model="gpt-5.6-sol",
            processing_tier="standard",
            input_tokens=5_484_930,
            cached_input_tokens=5_367_936,
            cache_write_input_tokens=0,
            output_tokens=25_749,
        )
        self.assertIn("long_context_input_over_272000", aggregate["applied_rules"])
        self.assertEqual(format_usd(aggregate["amount"]), "$6.00")

    def test_frozen_contribution_tool_coverage_and_workflow_idempotency(self) -> None:
        _, action_id = self._usage_action("priced", turn_id="priced-turn")
        event = self._usage("priced", "priced-turn", 10)
        event["effectiveModel"] = "gpt-5.6-luna"
        event["serviceTier"] = "standard"
        self.store.observe_turn_usage(event)
        before = self.store.action_cost_summary(action_id)
        self.store.execute(
            "UPDATE api_rate_cards SET output_per_million = '999' WHERE model = 'gpt-5.6-luna'"
        )
        self.store.observe_turn_usage(event)
        self.assertEqual(self.store.action_cost_summary(action_id), before)
        self.store.record_tool_cost(
            source_key="tool:known",
            tool_name="search",
            quantity=2,
            unit="call",
            unit_rate="0.01",
            source_url="https://example.test/public-rate",
            action_id=action_id,
        )
        self.store.record_tool_cost(
            source_key="tool:unknown",
            tool_name="future-tool",
            quantity=1,
            unit="call",
            unit_rate=None,
            source_url=None,
            action_id=action_id,
        )
        self.store.link_action_to_workflow(
            "workflow:test",
            action_id,
            causal_role="executor",
            origin_action_id=action_id,
        )
        report = self.store.finalize_workflow_cost("workflow:test")
        self.store.record_tool_cost(
            source_key="tool:too-late",
            tool_name="late-tool",
            quantity=1,
            unit="call",
            unit_rate="100",
            source_url="https://example.test/public-rate",
            action_id=action_id,
        )
        repeated = self.store.finalize_workflow_cost("workflow:test")
        self.assertEqual(report["frozen_amount"], repeated["frozen_amount"])
        self.assertEqual(report["groups"][0]["coverage"], "partial")
        self.assertEqual(report["groups"][0]["attributed"]["tool_count"], 2)

    def test_reroutes_unknown_tiers_and_missing_boundaries_are_explicit(self) -> None:
        task, action_id = self._usage_action("rerouted", turn_id="rerouted-turn")
        self.store.execute(
            "UPDATE tasks SET model = 'gpt-5.6-sol' WHERE id = ?", (task["id"],)
        )
        self.assertTrue(
            self.store.observe_model_reroute(
                {
                    "threadId": "rerouted",
                    "turnId": "rerouted-turn",
                    "fromModel": "gpt-5.6-sol",
                    "toModel": "gpt-5.6-luna",
                    "reason": "capacity",
                }
            )
        )
        self.assertFalse(
            self.store.observe_model_reroute(
                {
                    "threadId": "rerouted",
                    "turnId": "rerouted-turn",
                    "fromModel": "gpt-5.6-sol",
                    "toModel": "gpt-5.6-luna",
                    "reason": "capacity",
                }
            )
        )
        first = {
            "inputTokens": 10,
            "cachedInputTokens": 0,
            "cacheWriteInputTokens": 0,
            "outputTokens": 1,
            "reasoningOutputTokens": 1,
            "totalTokens": 11,
        }
        self.store.observe_turn_usage(
            {
                "threadId": "rerouted",
                "turnId": "rerouted-turn",
                "serviceTier": "standard",
                "responseId": "luna-response",
                "tokenUsage": {"last": first, "total": first},
            }
        )
        self.store.observe_model_reroute(
            {
                "threadId": "rerouted",
                "turnId": "rerouted-turn",
                "fromModel": "gpt-5.6-luna",
                "toModel": "gpt-6-astra",
                "reason": "capability",
            }
        )
        second = dict(first)
        total = {key: first[key] + second[key] for key in first}
        self.store.observe_turn_usage(
            {
                "threadId": "rerouted",
                "turnId": "rerouted-turn",
                "serviceTier": "standard",
                "responseId": "astra-response",
                "tokenUsage": {"last": second, "total": total},
            }
        )
        report = self.store.cost_report(action_id=action_id)["groups"][0]
        self.assertEqual(report["attributed"]["response_count"], 2)
        self.assertEqual(
            {item["model"] for item in report["rate_card_provenance"]},
            {"gpt-5.6-luna", "gpt-6-astra"},
        )
        self.assertEqual(
            self.store.row(
                "SELECT COUNT(*) AS count FROM model_reroutes WHERE contribution_id IS NOT NULL"
            )["count"],
            2,
        )
        self.store.observe_model_reroute(
            {
                "threadId": "rerouted",
                "turnId": "rerouted-turn",
                "fromModel": "gpt-6-astra",
                "toModel": "gpt-5.6-terra",
                "reason": "late notification",
            }
        )
        partial = self.store.cost_report(action_id=action_id)["groups"][0]
        self.assertEqual(partial["coverage"], "partial")
        self.assertIn(
            "without a following response boundary", " ".join(partial["exclusions"])
        )
        unknown = self.store.estimate_response_cost(
            requested_model="gpt-5.6-sol",
            effective_model="gpt-5.6-sol",
            processing_tier="unknown-tier",
            input_tokens=1,
            cached_input_tokens=0,
            cache_write_input_tokens=0,
            output_tokens=0,
        )
        self.assertEqual(unknown["coverage"], "partial")
        self.assertNotIn("amount", unknown)

        _, missing_action = self._usage_action("boundary-gap", turn_id="gap-turn")
        event = self._usage("boundary-gap", "gap-turn", 10)
        del event["tokenUsage"]["last"]
        self.store.observe_turn_usage(event)
        missing = self.store.cost_report(action_id=missing_action)["groups"][0]
        self.assertEqual(missing["coverage"], "partial")
        self.assertIn("priced response boundaries", " ".join(missing["exclusions"]))

    def test_consumed_reroute_does_not_leak_to_later_response(self) -> None:
        task, action_id = self._usage_action(
            "one-shot-reroute", turn_id="one-shot-turn"
        )
        self.store.execute(
            "UPDATE tasks SET model = 'gpt-5.6-sol' WHERE id = ?", (task["id"],)
        )
        reroute = {
            "threadId": "one-shot-reroute",
            "turnId": "one-shot-turn",
            "fromModel": "gpt-5.6-sol",
            "toModel": "gpt-5.6-luna",
            "reason": "capacity",
        }
        self.assertTrue(self.store.observe_model_reroute(reroute))
        self.assertFalse(self.store.observe_model_reroute(reroute))
        first = {
            "inputTokens": 10,
            "cachedInputTokens": 0,
            "cacheWriteInputTokens": 0,
            "outputTokens": 1,
            "reasoningOutputTokens": 1,
            "totalTokens": 11,
        }
        first_event = {
            "threadId": "one-shot-reroute",
            "turnId": "one-shot-turn",
            "serviceTier": "standard",
            "responseId": "r1",
            "tokenUsage": {"last": first, "total": first},
        }
        self.store.observe_turn_usage(first_event)
        self.store.observe_turn_usage(first_event)
        total = {key: value * 2 for key, value in first.items()}
        self.store.observe_turn_usage(
            {
                "threadId": "one-shot-reroute",
                "turnId": "one-shot-turn",
                "serviceTier": "standard",
                "responseId": "r2",
                "tokenUsage": {"last": first, "total": total},
            }
        )

        contributions = self.store.rows(
            """SELECT id, response_sequence, effective_model, coverage, assumptions
               FROM cost_contributions WHERE action_id = ?
                 AND contribution_kind = 'model_response' ORDER BY response_sequence""",
            (action_id,),
        )
        self.assertEqual(len(contributions), 2)
        self.assertEqual(
            [row["effective_model"] for row in contributions],
            ["gpt-5.6-luna", "gpt-5.6-sol"],
        )
        self.assertEqual(contributions[0]["coverage"], "complete")
        self.assertEqual(contributions[1]["coverage"], "partial")
        self.assertIn(
            "used configured model",
            " ".join(json.loads(contributions[1]["assumptions"])),
        )
        retained_reroute = self.store.row("SELECT * FROM model_reroutes")
        self.assertEqual(retained_reroute["contribution_id"], contributions[0]["id"])
        report = self.store.cost_report(action_id=action_id)["groups"][0]
        self.assertEqual(report["coverage"], "partial")
        self.assertEqual(
            {source["model"] for source in report["rate_card_provenance"]},
            {"gpt-5.6-luna", "gpt-5.6-sol"},
        )

    def test_identical_reroute_after_consumption_is_a_new_occurrence(self) -> None:
        task, action_id = self._usage_action(
            "repeated-reroute", turn_id="repeated-turn"
        )
        self.store.execute(
            "UPDATE tasks SET model = 'gpt-5.6-sol' WHERE id = ?", (task["id"],)
        )
        reroute = {
            "threadId": "repeated-reroute",
            "turnId": "repeated-turn",
            "fromModel": "gpt-5.6-sol",
            "toModel": "gpt-5.6-luna",
            "reason": "capacity",
        }
        response = {
            "inputTokens": 10,
            "cachedInputTokens": 0,
            "cacheWriteInputTokens": 0,
            "outputTokens": 1,
            "reasoningOutputTokens": 1,
            "totalTokens": 11,
        }

        first = {
            "threadId": "repeated-reroute",
            "turnId": "repeated-turn",
            "serviceTier": "standard",
            "responseId": "r1",
            "tokenUsage": {"last": response, "total": response},
        }
        self.assertTrue(self.store.observe_model_reroute(reroute))
        self.store.observe_turn_usage(first)
        self.assertTrue(self.store.observe_model_reroute(reroute))
        self.store.observe_turn_usage(first)
        self.assertFalse(self.store.observe_model_reroute(reroute))
        total = {key: value * 2 for key, value in response.items()}
        self.store.observe_turn_usage(
            {
                "threadId": "repeated-reroute",
                "turnId": "repeated-turn",
                "serviceTier": "standard",
                "responseId": "r2",
                "tokenUsage": {"last": response, "total": total},
            }
        )

        contributions = self.store.rows(
            """SELECT id, effective_model, coverage FROM cost_contributions
               WHERE action_id = ? AND contribution_kind = 'model_response'
               ORDER BY response_sequence""",
            (action_id,),
        )
        reroutes = self.store.rows(
            "SELECT contribution_id FROM model_reroutes ORDER BY id"
        )
        self.assertEqual(len(contributions), 2)
        self.assertEqual(
            [row["effective_model"] for row in contributions],
            ["gpt-5.6-luna", "gpt-5.6-luna"],
        )
        self.assertEqual([row["coverage"] for row in contributions], ["complete"] * 2)
        self.assertEqual(
            [row["contribution_id"] for row in reroutes],
            [contributions[0]["id"], contributions[1]["id"]],
        )
        report = self.store.cost_report(action_id=action_id)["groups"][0]
        self.assertEqual(report["coverage"], "complete")
        self.assertEqual(report["attributed"]["response_count"], 2)
        self.assertEqual(
            {source["model"] for source in report["rate_card_provenance"]},
            {"gpt-5.6-luna"},
        )

    def test_pragmas_and_names_are_role_specific(self) -> None:
        self.assertEqual(self.store.row("PRAGMA journal_mode")["journal_mode"], "wal")
        self.assertEqual(self.store.row("PRAGMA foreign_keys")["foreign_keys"], 1)
        sage = self.store.register_task(
            native_thread_id="sage-1",
            role="sage",
            description="Workflow postmortem",
            model="sol",
            reasoning_effort="high",
        )
        inquisitor = self.store.register_task(
            native_thread_id="inq-1",
            role="inquisitor",
            description="Architecture",
            model="sol",
            reasoning_effort="high",
        )
        self.assertEqual(sage["title"], "📖 [SAGE0001] Workflow postmortem")
        self.assertEqual(inquisitor["title"], "🛡️ [INQ0001] Architecture")
        self.store.execute(
            "UPDATE role_counters SET next_number = 10000 WHERE role = 'executor'"
        )
        _, title = self.store.allocate_name("executor", "Large fleet")
        self.assertEqual(title, "⚒️ [EXE10000] Large fleet")

    def test_registration_retry_and_conflict(self) -> None:
        first = self.store.register_task(
            native_thread_id="thread-1",
            role="weaver",
            description="Intake",
            model="sol",
            reasoning_effort="high",
            project_id="fulcrum",
        )
        second = self.store.register_task(
            native_thread_id="thread-1",
            role="weaver",
            description="Changed text does not reallocate",
            model="sol",
            reasoning_effort="high",
            project_id="fulcrum",
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(
            self.store.row(
                "SELECT next_number FROM role_counters WHERE role = 'weaver'"
            )["next_number"],
            2,
        )
        with self.assertRaises(StoreError):
            self.store.register_task(
                native_thread_id="thread-1",
                role="executor",
                description="Conflict",
                model="sol",
                reasoning_effort="high",
                project_id="fulcrum",
            )

    def test_lineage_names_share_the_weaver_number_and_allocate_stable_suffixes(
        self,
    ) -> None:
        weaver = self.store.register_task(
            native_thread_id="weaver-lineage",
            role="weaver",
            description="Lineage",
            model="sol",
            reasoning_effort="high",
            project_id="fulcrum",
        )
        self.assertEqual(weaver["lineage_number"], weaver["role_number"])

        allocated = []
        for position in range(2):
            allocation = self.store.allocate_lineage_name("overseer", 1, "Review")
            allocated.append(allocation)
            role_number, suffix, title = allocation
            self.store.register_task(
                native_thread_id=f"overseer-lineage-{position}",
                role="overseer",
                description="Review",
                model="sol",
                reasoning_effort="high",
                project_id="fulcrum",
                role_number=role_number,
                title=title,
                lineage_number=1,
                lineage_suffix=suffix,
            )
        allocated.append(self.store.allocate_lineage_name("overseer", 1, "Review"))
        self.assertEqual(
            [(suffix, title.split("]", 1)[0]) for _, suffix, title in allocated],
            [
                ("", "🔎 [OVR0001"),
                ("B", "🔎 [OVR0001B"),
                ("C", "🔎 [OVR0001C"),
            ],
        )

    def test_lineage_and_unlineaged_names_remain_unique_in_either_order(
        self,
    ) -> None:
        legacy = self.store.register_task(
            native_thread_id="legacy-executor",
            role="executor",
            description="Legacy",
            model="sol",
            reasoning_effort="high",
            project_id="fulcrum",
        )
        self.assertEqual(legacy["title"], "⚒️ [EXE0001] Legacy")
        role_number, suffix, title = self.store.allocate_lineage_name(
            "executor", 1, "Lineage"
        )
        self.assertEqual((suffix, title), ("B", "⚒️ [EXE0001B] Lineage"))
        self.store.register_task(
            native_thread_id="lineage-executor",
            role="executor",
            description="Lineage",
            model="sol",
            reasoning_effort="high",
            project_id="fulcrum",
            role_number=role_number,
            title=title,
            lineage_number=1,
            lineage_suffix=suffix,
        )
        self.store.execute(
            "UPDATE tasks SET state = 'archived', archived = 1 WHERE id = ?",
            (legacy["id"],),
        )
        _, next_suffix, next_title = self.store.allocate_lineage_name(
            "executor", 1, "More lineage work"
        )
        self.assertEqual(next_suffix, "C")
        self.assertIn("[EXE0001C]", next_title)

        overseer_number, overseer_suffix, overseer_title = (
            self.store.allocate_lineage_name("overseer", 1, "Lineage first")
        )
        self.store.register_task(
            native_thread_id="lineage-first-overseer",
            role="overseer",
            description="Lineage first",
            model="sol",
            reasoning_effort="high",
            project_id="fulcrum",
            role_number=overseer_number,
            title=overseer_title,
            lineage_number=1,
            lineage_suffix=overseer_suffix,
        )
        unlineaged = self.store.register_task(
            native_thread_id="later-unlineaged-overseer",
            role="overseer",
            description="Unlineaged later",
            model="sol",
            reasoning_effort="high",
            project_id="fulcrum",
        )
        self.assertEqual(unlineaged["title"], "🔎 [OVR0002] Unlineaged later")
        self.assertEqual(
            len(
                {
                    row["title"].split("]", 1)[0]
                    for row in self.store.rows(
                        "SELECT title FROM tasks WHERE role IN ('executor','overseer')"
                    )
                }
            ),
            4,
        )

    def test_lineage_allocation_ignores_agent_codes_in_descriptions(self) -> None:
        self.store.register_task(
            native_thread_id="unrelated-executor",
            role="executor",
            description="Discuss [EXE0003] and [EXE0003B] in docs",
            model="sol",
            reasoning_effort="high",
            project_id="fulcrum",
            role_number=2,
            title="⚒️ [EXE0002] Discuss [EXE0003] and [EXE0003B] in docs",
        )
        self.store.create_operation(
            "thread_start",
            "executor",
            {
                "title": "⚒️ [EXE0004] Compare [EXE0003C] in docs",
                "lineage_number": 4,
                "lineage_suffix": "",
            },
        )

        allocated = []
        for position in range(2):
            allocation = self.store.allocate_lineage_name(
                "executor", 3, "Actual lineage"
            )
            allocated.append(allocation)
            role_number, suffix, title = allocation
            self.store.register_task(
                native_thread_id=f"actual-lineage-{position}",
                role="executor",
                description="Actual lineage",
                model="sol",
                reasoning_effort="high",
                project_id="fulcrum",
                role_number=role_number,
                title=title,
                lineage_number=3,
                lineage_suffix=suffix,
            )
        allocated.append(
            self.store.allocate_lineage_name("executor", 3, "Actual lineage")
        )

        self.assertEqual(
            [(suffix, title.split("]", 1)[0]) for _, suffix, title in allocated],
            [
                ("", "⚒️ [EXE0003"),
                ("B", "⚒️ [EXE0003B"),
                ("C", "⚒️ [EXE0003C"),
            ],
        )

    def test_constraints_prevent_two_current_actions_and_pair_reservations(
        self,
    ) -> None:
        task = self.store.register_task(
            native_thread_id="thread-1",
            role="executor",
            description="Work",
            model="sol",
            reasoning_effort="high",
            project_id="fulcrum",
        )
        now = "2026-01-01T00:00:00Z"
        first = self.store.execute(
            "INSERT INTO actions(task_id, kind, payload, state, created_at, updated_at) VALUES (?, 'implement', '{}', 'active', ?, ?)",
            (task["id"], now, now),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.execute(
                "INSERT INTO actions(task_id, kind, payload, state, created_at, updated_at) VALUES (?, 'correct', '{}', 'pending', ?, ?)",
                (task["id"], now, now),
            )
        self.store.execute(
            "INSERT INTO reservations(action_id, pair_id, state, created_at) VALUES (?, 7, 'active', ?)",
            (first.lastrowid, now),
        )
        other = self.store.register_task(
            native_thread_id="thread-2",
            role="overseer",
            description="Review",
            model="sol",
            reasoning_effort="high",
            project_id="fulcrum",
        )
        action = self.store.execute(
            "INSERT INTO actions(task_id, kind, payload, state, created_at, updated_at) VALUES (?, 'review', '{}', 'active', ?, ?)",
            (other["id"], now, now),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.execute(
                "INSERT INTO reservations(action_id, pair_id, state, created_at) VALUES (?, 7, 'active', ?)",
                (action.lastrowid, now),
            )

    def test_status_has_controller_read_model(self) -> None:
        status = self.store.status()
        self.assertEqual(status["unfinished_work_count"], 0)
        self.assertEqual(status["slot_usage"], {"global": 0, "projects": {}})
        self.assertIn("operations", status)
        self.assertIn("obligations", status)

    def test_database_rejects_recovery_without_retry_or_hold(self) -> None:
        now = "2026-01-01T00:00:00Z"
        self.store.execute(
            "INSERT INTO beads VALUES ('b','k','fulcrum','t','scope','pending','sol','high','sol','high','default',NULL,NULL,'[]','complete',?,?)",
            (now, now),
        )
        run = self.store.execute(
            "INSERT INTO runs(project_id, authority, created_at, updated_at) VALUES ('fulcrum','test',?,?)",
            (now, now),
        )
        assignment = self.store.execute(
            "INSERT INTO assignments(run_id, bead_id, stage, scope_snapshot, created_at, updated_at) VALUES (?, 'b', 'queued', 'scope', ?, ?)",
            (run.lastrowid, now, now),
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "retry deadline"):
            self.store.execute(
                "UPDATE assignments SET stage = 'recovering' WHERE id = ?",
                (assignment.lastrowid,),
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "retry deadline"):
            self.store.execute(
                """INSERT INTO assignments(
                       run_id, bead_id, stage, scope_snapshot, created_at, updated_at
                   ) VALUES (?, 'b', 'recovering', 'scope', ?, ?)""",
                (run.lastrowid, now, now),
            )

    def test_failed_action_is_evidence_not_current_work(self) -> None:
        task = self.store.register_task(
            native_thread_id="retryable",
            role="executor",
            description="Retryable",
            model="sol",
            reasoning_effort="high",
            project_id="fulcrum",
        )
        now = "2026-01-01T00:00:00Z"
        failed = self.store.execute(
            """INSERT INTO actions(task_id, kind, payload, state, created_at, updated_at)
               VALUES (?, 'implement', '{}', 'failed', ?, ?)""",
            (task["id"], now, now),
        )
        pending = self.store.execute(
            """INSERT INTO actions(task_id, kind, payload, state, created_at, updated_at)
               VALUES (?, 'implement', '{}', 'pending', ?, ?)""",
            (task["id"], now, now),
        )

        current = self.store.current_action("retryable")
        self.assertEqual(current["id"], pending.lastrowid)
        self.assertNotEqual(current["id"], failed.lastrowid)

    def test_terminal_action_automatically_releases_reservation(self) -> None:
        task = self.store.register_task(
            native_thread_id="terminal",
            role="executor",
            description="Terminal",
            model="sol",
            reasoning_effort="high",
            project_id="fulcrum",
        )
        now = "2026-01-01T00:00:00Z"
        action = self.store.execute(
            "INSERT INTO actions(task_id, kind, payload, state, created_at, updated_at) VALUES (?, 'implement', '{}', 'active', ?, ?)",
            (task["id"], now, now),
        )
        self.store.execute(
            "INSERT INTO reservations(action_id, state, created_at) VALUES (?, 'active', ?)",
            (action.lastrowid, now),
        )
        self.store.execute(
            "UPDATE actions SET state = 'failed' WHERE id = ?", (action.lastrowid,)
        )
        self.assertIsNone(
            self.store.row(
                "SELECT * FROM reservations WHERE action_id = ?",
                (action.lastrowid,),
            )
        )
        transition = self.store.row(
            "SELECT * FROM state_transitions WHERE entity_type = 'action' AND entity_id = ?",
            (str(action.lastrowid),),
        )
        self.assertEqual(transition["from_state"], "active")
        self.assertEqual(transition["to_state"], "failed")

    def test_open_replaces_stale_invariant_trigger_definitions(self) -> None:
        self.store.execute("DROP TRIGGER handoff_must_match_terminal_source")
        self.store.execute("""CREATE TRIGGER handoff_must_match_terminal_source
               BEFORE INSERT ON handoffs
               WHEN NOT EXISTS (
                 SELECT 1 FROM actions WHERE id = NEW.source_action_id
                 AND outcome_payload = NEW.content_json
               )
               BEGIN
                 SELECT RAISE(ABORT, 'legacy trigger');
               END""")
        self.store.close()
        self.store = Store(Path(self.temporary.name) / "state.sqlite3")
        trigger = self.store.row(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
            ("handoff_must_match_terminal_source",),
        )
        self.assertNotIn("outcome_payload = NEW.content_json", trigger["sql"])

    def test_cost_schema_migration_preserves_usage_contributions_and_workflow(
        self,
    ) -> None:
        _, action_id = self._usage_action("cost-migration", turn_id="cost-turn")
        usage = self._usage("cost-migration", "cost-turn", 10)
        usage["effectiveModel"] = "gpt-5.6-luna"
        usage["serviceTier"] = "standard"
        self.store.observe_turn_usage(usage)
        self.store.link_action_to_workflow(
            "workflow:migration", action_id, causal_role="executor"
        )
        path = Path(self.temporary.name) / "state.sqlite3"
        self.store.close()
        connection = sqlite3.connect(path)
        connection.execute(
            "ALTER TABLE workflow_cost_actions DROP COLUMN exclusion_reason"
        )
        connection.execute("ALTER TABLE workflow_cost_actions DROP COLUMN include_cost")
        connection.execute("DROP TABLE api_tool_rate_cards")
        connection.execute("DROP TABLE model_reroutes")
        connection.commit()
        connection.close()

        self.store = Store(path)
        retained_usage = self.store.row("""SELECT total_tokens FROM action_turn_usage
               WHERE native_thread_id = 'cost-migration'""")
        retained_cost = self.store.row(
            "SELECT amount FROM cost_contributions WHERE action_id = ?", (action_id,)
        )
        retained_link = self.store.row(
            """SELECT include_cost, exclusion_reason FROM workflow_cost_actions
               WHERE workflow_id = 'workflow:migration' AND action_id = ?""",
            (action_id,),
        )
        self.assertEqual(retained_usage["total_tokens"], 14)
        self.assertIsNotNone(retained_cost["amount"])
        self.assertEqual(retained_link["include_cost"], 1)
        self.assertIsNone(retained_link["exclusion_reason"])
        self.assertEqual(
            self.store.row("""SELECT unit_rate FROM api_tool_rate_cards
                   WHERE tool_name = 'web_search'""")["unit_rate"],
            "0.01",
        )

    def test_lifetime_reroute_identity_migrates_without_data_loss(self) -> None:
        task, action_id = self._usage_action(
            "reroute-migration", turn_id="reroute-migration-turn"
        )
        self.store.execute(
            "UPDATE tasks SET model = 'gpt-5.6-sol' WHERE id = ?", (task["id"],)
        )
        reroute = {
            "threadId": "reroute-migration",
            "turnId": "reroute-migration-turn",
            "fromModel": "gpt-5.6-sol",
            "toModel": "gpt-5.6-luna",
            "reason": "capacity",
        }
        self.store.observe_model_reroute(reroute)
        usage = self._usage("reroute-migration", "reroute-migration-turn", 10)
        usage["serviceTier"] = "standard"
        self.store.observe_turn_usage(usage)
        contribution = self.store.row(
            "SELECT id FROM cost_contributions WHERE action_id = ?", (action_id,)
        )
        path = Path(self.temporary.name) / "state.sqlite3"
        self.store.close()
        connection = sqlite3.connect(path)
        connection.executescript("""PRAGMA foreign_keys = OFF;
            DROP INDEX one_pending_model_reroute;
            DROP INDEX reroutes_by_turn;
            ALTER TABLE model_reroutes RENAME TO replacement_model_reroutes;
            CREATE TABLE model_reroutes (
              id INTEGER PRIMARY KEY, native_thread_id TEXT NOT NULL,
              native_turn_id TEXT NOT NULL, from_model TEXT NOT NULL,
              to_model TEXT NOT NULL, reason TEXT NOT NULL,
              contribution_id INTEGER REFERENCES cost_contributions(id),
              observed_at TEXT NOT NULL,
              UNIQUE(native_thread_id, native_turn_id, from_model, to_model, reason)
            );
            INSERT INTO model_reroutes SELECT * FROM replacement_model_reroutes;
            DROP TABLE replacement_model_reroutes;
            PRAGMA foreign_keys = ON;""")
        connection.commit()
        connection.close()

        self.store = Store(path)
        retained = self.store.row("SELECT * FROM model_reroutes")
        self.assertEqual(retained["contribution_id"], contribution["id"])
        table_sql = self.store.row("""SELECT sql FROM sqlite_master
               WHERE type = 'table' AND name = 'model_reroutes'""")["sql"]
        index_sql = self.store.row("""SELECT sql FROM sqlite_master
               WHERE type = 'index' AND name = 'one_pending_model_reroute'""")["sql"]
        self.assertNotIn("UNIQUE", table_sql.upper())
        self.assertIn("WHERE contribution_id IS NULL", index_sql)
        self.assertTrue(self.store.observe_model_reroute(reroute))
        self.assertEqual(
            self.store.row("SELECT COUNT(*) AS count FROM model_reroutes")["count"],
            2,
        )

    def test_open_collapses_duplicate_unresolved_candidate_operations(self) -> None:
        now = "2026-01-01T00:00:00Z"
        self.store.execute("DROP INDEX one_current_tollgate_candidate_operation")
        for _ in range(3):
            self.store.execute(
                """INSERT INTO external_operations(
                       kind, target, input_json, state, correlation_id,
                       created_at, updated_at
                   ) VALUES ('tollgate_candidate_create', '7', '{}', 'uncertain',
                             'legacy-' || ?, ?, ?)""",
                (_, now, now),
            )
        self.store.close()
        self.store = Store(Path(self.temporary.name) / "state.sqlite3")
        unresolved = self.store.rows("""SELECT * FROM external_operations
               WHERE kind = 'tollgate_candidate_create'
                 AND state IN ('intent','sent','uncertain')""")
        self.assertEqual(len(unresolved), 1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.execute(
                """INSERT INTO external_operations(
                       kind, target, input_json, state, correlation_id,
                       created_at, updated_at
                   ) VALUES ('tollgate_candidate_create', '7', '{}', 'intent',
                             'new', ?, ?)""",
                (now, now),
            )

    def test_open_recovers_legacy_definitive_tollgate_failure(self) -> None:
        operation = self.store.create_operation(
            "tollgate_candidate_create",
            "7",
            {"worktree": "/tmp/worktree"},
            correlation_id="legacy-definitive-failure",
        )
        attempt = self.store.begin_operation_attempt(operation)
        response = '{"error":{"message":"repository is dirty","retryable":false}}'
        self.store.finish_operation_attempt(
            operation,
            attempt,
            state="uncertain",
            stderr=response,
            error=response,
        )

        self.store.close()
        self.store = Store(Path(self.temporary.name) / "state.sqlite3")

        recovered = self.store.row(
            "SELECT state, condition FROM external_operations WHERE id = ?",
            (operation,),
        )
        self.assertEqual(recovered["state"], "failed")
        self.assertIn("proves the operation failed", recovered["condition"])

    def test_existing_recovery_without_progress_is_migrated_to_a_hold(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            connection = sqlite3.connect(path)
            connection.execute("""CREATE TABLE assignments(
                     id INTEGER PRIMARY KEY, bead_id TEXT, stage TEXT,
                     condition TEXT, updated_at TEXT
                   )""")
            connection.execute(
                """INSERT INTO assignments(id, bead_id, stage, updated_at)
                   VALUES (1, 'b', 'recovering', '2026-01-01T00:00:00Z')"""
            )
            connection.commit()
            connection.close()
            with Store(path) as migrated:
                assignment = migrated.row("SELECT * FROM assignments WHERE id = 1")
                self.assertIsNotNone(assignment["operator_hold_id"])
                self.assertIsNone(assignment["completion_kind"])
                self.assertIsNone(assignment["completion_evidence"])
                hold = migrated.row(
                    "SELECT * FROM holds WHERE id = ?",
                    (assignment["operator_hold_id"],),
                )
                self.assertIn("legacy recovery", hold["reason"])

    def _usage_action(
        self, thread_id: str, role: str = "executor", turn_id: str = "turn-1"
    ) -> tuple[dict[str, object], int]:
        task = self.store.register_task(
            native_thread_id=thread_id,
            role=role,
            description=f"{role} usage",
            model="model-a",
            reasoning_effort="high",
            project_id="fulcrum" if role != "archon" else None,
        )
        now = "2026-01-01T00:00:00Z"
        action_id = int(
            self.store.execute(
                """INSERT INTO actions(
                       task_id, kind, payload, state, native_turn_id,
                       created_at, updated_at
                   ) VALUES (?, ?, '{}', 'active', ?, ?, ?)""",
                (task["id"], f"future-{role}", turn_id, now, now),
            ).lastrowid
        )
        self.store.bind_action_turn(action_id, thread_id, turn_id)
        return task, action_id

    def _usage(
        self,
        thread_id: str,
        turn_id: str,
        total: int,
        *,
        latest: int | None = None,
    ) -> dict[str, object]:
        latest = total if latest is None else latest
        return {
            "threadId": thread_id,
            "turnId": turn_id,
            "modelContextWindow": 128_000,
            "tokenUsage": {
                "last": {
                    "inputTokens": latest,
                    "cachedInputTokens": latest // 2,
                    "cacheWriteInputTokens": 0,
                    "outputTokens": 2,
                    "reasoningOutputTokens": 1,
                    "totalTokens": latest + 2,
                },
                "total": {
                    "inputTokens": total,
                    "cachedInputTokens": total // 2,
                    "cacheWriteInputTokens": 0,
                    "outputTokens": 4,
                    "reasoningOutputTokens": 2,
                    "totalTokens": total + 4,
                },
            },
        }

    def test_usage_generic_roles_unknown_and_explicit_zero(self) -> None:
        roles = ("archon", "weaver", "executor", "overseer", "sage", "inquisitor")
        action_ids = []
        for index, role in enumerate(roles):
            _, action_id = self._usage_action(f"{role}-thread", role, f"{role}-turn")
            action_ids.append(action_id)
            if index:
                zero = self._usage(f"{role}-thread", f"{role}-turn", 0)
                usage = zero["tokenUsage"]
                assert isinstance(usage, dict)
                usage["last"] = {key: 0 for key in usage["last"]}
                usage["total"] = {key: 0 for key in usage["total"]}
                self.store.observe_turn_usage(zero)

        unknown = self.store.action_usage_summary(action_ids[0])
        explicit_zero = self.store.action_usage_summary(action_ids[1])
        self.assertIsNone(unknown["direct_total_tokens"])
        self.assertEqual(unknown["coverage"], "unknown")
        self.assertEqual(explicit_zero["direct_total_tokens"], 0)
        self.assertEqual(
            {
                group["group"]
                for group in self.store.usage_report(group_by="role")["groups"]
            },
            set(roles),
        )

    def test_usage_coalesces_multiple_responses_duplicates_and_out_of_order(
        self,
    ) -> None:
        _, action_id = self._usage_action("stream", turn_id="response-turn")
        self.store.observe_turn_usage(
            self._usage("stream", "response-turn", 10, latest=10)
        )
        self.store.observe_turn_usage(
            self._usage("stream", "response-turn", 30, latest=20)
        )
        self.store.observe_turn_usage(
            self._usage("stream", "response-turn", 30, latest=20)
        )
        self.store.observe_turn_usage(
            self._usage("stream", "response-turn", 5, latest=5)
        )

        report = self.store.usage_report(action_id=action_id)
        turn = report["turns"][0]
        self.assertEqual(turn["total_tokens"], 34)
        self.assertEqual(turn["last_total_tokens"], 22)
        self.assertEqual(report["groups"][0]["direct"]["total_tokens"], 34)
        self.assertEqual(report["groups"][0]["contributing_turn_count"], 1)

    def test_usage_before_binding_and_two_actions_on_reused_thread(self) -> None:
        task = self.store.register_task(
            native_thread_id="reused",
            role="executor",
            description="reused",
            model="model-a",
            reasoning_effort="high",
            project_id="fulcrum",
        )
        self.store.observe_turn_usage(self._usage("reused", "early-turn", 11))
        now = "2026-01-01T00:00:00Z"
        first = int(
            self.store.execute(
                """INSERT INTO actions(task_id, kind, payload, state,
                       native_turn_id, created_at, updated_at)
                   VALUES (?, 'implement', '{}', 'active', 'early-turn', ?, ?)""",
                (task["id"], now, now),
            ).lastrowid
        )
        self.store.bind_action_turn(first, "reused", "early-turn")
        self.store.execute(
            "UPDATE actions SET state = 'processed' WHERE id = ?", (first,)
        )
        second = int(
            self.store.execute(
                """INSERT INTO actions(task_id, kind, payload, state,
                       native_turn_id, created_at, updated_at)
                   VALUES (?, 'correct', '{}', 'active', 'second-turn', ?, ?)""",
                (task["id"], now, now),
            ).lastrowid
        )
        self.store.bind_action_turn(second, "reused", "second-turn")
        self.store.observe_turn_usage(self._usage("reused", "second-turn", 3))

        self.assertEqual(
            self.store.action_usage_summary(first)["direct_total_tokens"], 15
        )
        self.assertEqual(
            self.store.action_usage_summary(second)["direct_total_tokens"], 7
        )

    def test_nested_late_helper_attribution_excludes_managed_threads(self) -> None:
        _, action_id = self._usage_action("parent", turn_id="parent-turn")
        self.store.observe_turn_usage(self._usage("helper", "helper-turn", 7))
        self.store.observe_helper_thread(
            parent_thread_id="helper",
            parent_turn_id="helper-turn",
            native_thread_id="nested",
        )
        self.store.observe_turn_usage(self._usage("nested", "nested-turn", 5))
        self.store.observe_helper_thread(
            parent_thread_id="parent",
            parent_turn_id="parent-turn",
            native_thread_id="helper",
        )
        self.store.observe_helper_thread(
            parent_thread_id="parent",
            parent_turn_id="parent-turn",
            native_thread_id="helper",
        )
        self.store.observe_turn_usage(self._usage("parent", "parent-turn", 10))
        self.store.finalize_native_turn_usage("helper", "helper-turn")
        self.store.finalize_native_turn_usage("nested", "nested-turn")
        unrelated = self.store.register_task(
            native_thread_id="unrelated",
            role="weaver",
            description="unrelated",
            model="model-a",
            reasoning_effort="high",
            project_id="fulcrum",
        )
        self.store.observe_helper_thread(
            parent_thread_id="parent",
            parent_turn_id="parent-turn",
            native_thread_id=str(unrelated["native_thread_id"]),
        )

        group = self.store.usage_report(action_id=action_id)["groups"][0]
        self.assertEqual(group["direct"]["total_tokens"], 14)
        self.assertEqual(group["attributed"]["total_tokens"], 34)
        self.assertEqual(group["helper_turn_count"], 2)
        helper_usage = self.store.row(
            "SELECT model, reasoning_effort FROM action_turn_usage WHERE native_thread_id = 'helper'"
        )
        self.assertEqual(
            (helper_usage["model"], helper_usage["reasoning_effort"]),
            ("model-a", "high"),
        )
        self.assertIsNone(
            self.store.row(
                "SELECT * FROM telemetry_helper_threads WHERE native_thread_id = 'unrelated'"
            )
        )

    def test_reused_helper_thread_keeps_turn_ownership_by_parent_action(self) -> None:
        now = "2026-01-01T00:00:00Z"
        self.store.execute(
            """INSERT INTO beads VALUES (
                 'reuse-bead','reuse-key','fulcrum','Reuse','Scope','pending',
                 'model-a','high','model-a','high','configured',NULL,NULL,
                 '[]','complete',?,?)""",
            (now, now),
        )
        run_id = int(
            self.store.execute(
                """INSERT INTO runs(project_id, authority, state, created_at, updated_at)
                   VALUES ('fulcrum', 'test', 'active', ?, ?)""",
                (now, now),
            ).lastrowid
        )
        task = self.store.register_task(
            native_thread_id="reuse-parent",
            role="executor",
            description="reuse parent",
            model="model-a",
            reasoning_effort="high",
            project_id="fulcrum",
            pair_id=run_id,
        )
        self.store.execute(
            "UPDATE runs SET executor_task_id = ? WHERE id = ?",
            (task["id"], run_id),
        )
        assignment_id = int(
            self.store.execute(
                """INSERT INTO assignments(
                       run_id, bead_id, executor_task_id, stage, scope_snapshot,
                       created_at, updated_at
                   ) VALUES (?, 'reuse-bead', ?, 'implementing', 'Scope', ?, ?)""",
                (run_id, task["id"], now, now),
            ).lastrowid
        )

        def parent_action(turn_id: str) -> int:
            action_id = int(
                self.store.execute(
                    """INSERT INTO actions(
                           task_id, assignment_id, kind, payload, state,
                           native_turn_id, created_at, updated_at
                       ) VALUES (?, ?, 'future-kind', '{}', 'active', ?, ?, ?)""",
                    (task["id"], assignment_id, turn_id, now, now),
                ).lastrowid
            )
            self.store.bind_action_turn(
                action_id, str(task["native_thread_id"]), turn_id
            )
            return action_id

        first_action = parent_action("parent-turn-1")
        self.store.observe_helper_thread(
            parent_thread_id="reuse-parent",
            parent_turn_id="parent-turn-1",
            native_thread_id="reused-helper",
        )
        self.assertTrue(
            self.store.observe_helper_turn_started("reused-helper", "helper-turn-1")
        )
        self.store.execute(
            "UPDATE actions SET state = 'processed' WHERE id = ?", (first_action,)
        )
        self.store.finalize_action_usage(first_action)

        second_action = parent_action("parent-turn-2")
        self.store.observe_helper_thread(
            parent_thread_id="reuse-parent",
            parent_turn_id="parent-turn-2",
            native_thread_id="reused-helper",
        )
        self.assertTrue(
            self.store.observe_helper_turn_started("reused-helper", "helper-turn-2")
        )
        # A late duplicate of the first collaboration item must not become the
        # current owner or rewrite the first helper turn.
        self.store.observe_helper_thread(
            parent_thread_id="reuse-parent",
            parent_turn_id="parent-turn-1",
            native_thread_id="reused-helper",
        )
        # Both first snapshots arrive only after the helper thread has been reused.
        self.store.observe_turn_usage(self._usage("reused-helper", "helper-turn-1", 10))
        self.store.observe_turn_usage(self._usage("reused-helper", "helper-turn-2", 20))

        helper_turns = self.store.rows("""SELECT native_turn_id, attributed_action_id
               FROM action_turn_usage WHERE native_thread_id = 'reused-helper'
               ORDER BY native_turn_id""")
        self.assertEqual(
            helper_turns,
            [
                {
                    "native_turn_id": "helper-turn-1",
                    "attributed_action_id": first_action,
                },
                {
                    "native_turn_id": "helper-turn-2",
                    "attributed_action_id": second_action,
                },
            ],
        )
        self.assertEqual(
            self.store.usage_report(action_id=first_action)["groups"][0]["attributed"][
                "total_tokens"
            ],
            14,
        )
        self.assertEqual(
            self.store.usage_report(action_id=second_action)["groups"][0]["attributed"][
                "total_tokens"
            ],
            24,
        )
        for report in (
            self.store.usage_report(task_id=int(task["id"]), group_by="task"),
            self.store.usage_report(assignment_id=assignment_id, group_by="assignment"),
            self.store.usage_report(run_id=run_id, group_by="run"),
            self.store.usage_report(role="executor", group_by="role"),
            self.store.usage_report(project_id="fulcrum", group_by="project"),
        ):
            self.assertEqual(report["groups"][0]["attributed"]["total_tokens"], 38)
            self.assertEqual(report["groups"][0]["helper_turn_count"], 2)
        self.assertEqual(
            self.store.row("""SELECT COUNT(*) AS count FROM telemetry_helper_threads
                   WHERE native_thread_id = 'reused-helper'""")["count"],
            2,
        )
        ownership = self.store.rows("""SELECT parent_turn_id, native_turn_id
               FROM telemetry_helper_threads
               WHERE native_thread_id = 'reused-helper' ORDER BY id""")
        self.assertEqual(
            ownership,
            [
                {
                    "parent_turn_id": "parent-turn-1",
                    "native_turn_id": "helper-turn-1",
                },
                {
                    "parent_turn_id": "parent-turn-2",
                    "native_turn_id": "helper-turn-2",
                },
            ],
        )

    def test_reused_helper_without_turn_lifecycle_remains_partial(self) -> None:
        task, first_action = self._usage_action("gap-parent", turn_id="parent-one")
        now = "2026-01-01T00:00:00Z"
        self.store.execute(
            "UPDATE actions SET state = 'processed' WHERE id = ?", (first_action,)
        )
        second_action = int(
            self.store.execute(
                """INSERT INTO actions(
                       task_id, kind, payload, state, native_turn_id,
                       created_at, updated_at
                   ) VALUES (?, 'future-executor', '{}', 'active', ?, ?, ?)""",
                (task["id"], "parent-two", now, now),
            ).lastrowid
        )
        self.store.bind_action_turn(second_action, "gap-parent", "parent-two")
        self.store.observe_helper_thread(
            parent_thread_id="gap-parent",
            parent_turn_id="parent-one",
            native_thread_id="gap-helper",
        )
        self.store.observe_helper_thread(
            parent_thread_id="gap-parent",
            parent_turn_id="parent-two",
            native_thread_id="gap-helper",
        )

        self.store.observe_turn_usage(self._usage("gap-helper", "unknown-turn", 9))
        self.store.observe_helper_thread(
            parent_thread_id="gap-helper",
            parent_turn_id=None,
            native_thread_id="gap-nested-helper",
        )

        turn = self.store.row(
            """SELECT attributed_action_id, is_helper, coverage, gap_reason
               FROM action_turn_usage WHERE native_thread_id = 'gap-helper'
                 AND native_turn_id = 'unknown-turn'"""
        )
        self.assertIsNone(turn["attributed_action_id"])
        self.assertEqual(turn["is_helper"], 1)
        self.assertEqual(turn["coverage"], "partial")
        self.assertIn("unambiguous collaboration ownership", turn["gap_reason"])
        self.assertIsNone(
            self.store.row("""SELECT attributed_action_id FROM telemetry_helper_threads
                   WHERE native_thread_id = 'gap-nested-helper'""")[
                "attributed_action_id"
            ]
        )
        self.assertIsNone(
            self.store.usage_report(action_id=first_action)["groups"][0]["attributed"][
                "total_tokens"
            ]
        )
        self.assertIsNone(
            self.store.usage_report(action_id=second_action)["groups"][0]["attributed"][
                "total_tokens"
            ]
        )

    def test_two_helper_calls_in_one_parent_turn_are_counted_exactly_once(self) -> None:
        _, action_id = self._usage_action("multi-helper-parent", turn_id="parent-turn")
        self.store.observe_turn_usage(
            self._usage("multi-helper-parent", "parent-turn", 1)
        )

        for item_id, turn_id, total in (
            ("collaboration-one", "helper-turn-one", 10),
            ("collaboration-two", "helper-turn-two", 20),
        ):
            self.store.observe_helper_thread(
                parent_thread_id="multi-helper-parent",
                parent_turn_id="parent-turn",
                native_thread_id="repeated-helper",
                collaboration_item_id=item_id,
            )
            self.assertTrue(
                self.store.observe_helper_turn_started("repeated-helper", turn_id)
            )
            self.store.observe_turn_usage(
                self._usage("repeated-helper", turn_id, total)
            )
            self.store.finalize_native_turn_usage("repeated-helper", turn_id)

        # Replaying one collaboration item cannot create another contribution.
        self.store.observe_helper_thread(
            parent_thread_id="multi-helper-parent",
            parent_turn_id="parent-turn",
            native_thread_id="repeated-helper",
            collaboration_item_id="collaboration-one",
        )
        self.store.finalize_action_usage(action_id)

        ownership = self.store.rows("""SELECT collaboration_item_id, native_turn_id
               FROM telemetry_helper_threads
               WHERE native_thread_id = 'repeated-helper' ORDER BY id""")
        self.assertEqual(
            ownership,
            [
                {
                    "collaboration_item_id": "collaboration-one",
                    "native_turn_id": "helper-turn-one",
                },
                {
                    "collaboration_item_id": "collaboration-two",
                    "native_turn_id": "helper-turn-two",
                },
            ],
        )
        group = self.store.usage_report(action_id=action_id)["groups"][0]
        self.assertEqual(group["direct"]["total_tokens"], 5)
        self.assertEqual(group["attributed"]["total_tokens"], 43)
        self.assertEqual(group["helper_turn_count"], 2)
        self.assertEqual(group["unobserved_helper_count"], 0)
        self.assertEqual(group["coverage"], "complete")

    def test_unassociated_known_helper_turn_prevents_complete_coverage(self) -> None:
        _, action_id = self._usage_action("missing-call-parent", turn_id="parent-turn")
        self.store.observe_turn_usage(
            self._usage("missing-call-parent", "parent-turn", 1)
        )
        self.store.observe_helper_thread(
            parent_thread_id="missing-call-parent",
            parent_turn_id="parent-turn",
            native_thread_id="known-helper",
            collaboration_item_id="observed-call",
        )
        self.store.observe_helper_turn_started("known-helper", "observed-helper-turn")
        self.store.observe_turn_usage(
            self._usage("known-helper", "observed-helper-turn", 10)
        )
        self.store.finalize_native_turn_usage("known-helper", "observed-helper-turn")

        # The helper thread starts another turn, but its collaboration item was
        # missed. It must not disappear merely because every retained item is bound.
        self.store.observe_helper_turn_started("known-helper", "unassociated-turn")
        self.store.observe_turn_usage(
            self._usage("known-helper", "unassociated-turn", 20)
        )
        self.store.finalize_native_turn_usage("known-helper", "unassociated-turn")
        self.store.finalize_action_usage(action_id)

        group = self.store.usage_report(action_id=action_id)["groups"][0]
        self.assertEqual(group["attributed"]["total_tokens"], 19)
        self.assertEqual(group["helper_turn_count"], 1)
        self.assertEqual(group["unobserved_helper_count"], 1)
        self.assertEqual(group["coverage"], "partial")

    def test_terminal_and_gap_coverage_and_filtered_rollups(self) -> None:
        _, action_id = self._usage_action("terminal", turn_id="terminal-turn")
        self.store.observe_turn_usage(self._usage("terminal", "terminal-turn", 20))
        self.store.mark_open_usage_gap("runtime disconnected")
        finalized = self.store.finalize_action_usage(action_id)
        self.assertEqual(finalized["coverage"], "partial")
        self.assertEqual(
            self.store.usage_report(project_id="fulcrum", group_by="project")["groups"][
                0
            ]["coverage"],
            "partial",
        )
        self.assertFalse(
            self.store.finalize_action_usage(action_id)["_newly_finalized"]
        )

    def test_usage_cli_reads_historical_filters_without_controller(self) -> None:
        _, action_id = self._usage_action("cli-thread", turn_id="cli-turn")
        self.store.observe_turn_usage(self._usage("cli-thread", "cli-turn", 6))
        paths = SimpleNamespace(
            database=self.store.path,
            socket=Path(self.temporary.name) / "missing.sock",
        )
        output = io.StringIO()
        with (
            patch("fulcrum.cli.resolve_paths", return_value=paths),
            redirect_stdout(output),
        ):
            result = main(["usage", "--action", str(action_id)])
        self.assertEqual(result, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["groups"][0]["direct"]["total_tokens"], 10)

    def test_usage_schema_and_rows_survive_database_reopen(self) -> None:
        _, action_id = self._usage_action("retained", turn_id="retained-turn")
        self.store.observe_turn_usage(self._usage("retained", "retained-turn", 9))
        path = self.store.path
        self.store.close()
        self.store = Store(path)

        self.assertEqual(
            self.store.action_usage_summary(action_id)["direct_total_tokens"], 13
        )
        self.assertIsNotNone(
            self.store.row(
                "SELECT sql FROM sqlite_master WHERE name = 'telemetry_helper_threads'"
            )
        )

    def test_single_row_helper_ownership_schema_migrates_without_data_loss(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute("""CREATE TABLE telemetry_helper_threads (
                     native_thread_id TEXT PRIMARY KEY,
                     parent_thread_id TEXT NOT NULL, parent_turn_id TEXT,
                     attributed_action_id INTEGER,
                     first_observed_at TEXT NOT NULL,
                     last_observed_at TEXT NOT NULL)""")
            connection.execute("""INSERT INTO telemetry_helper_threads VALUES (
                     'helper', 'parent', 'turn', NULL, 'first', 'last')""")
            connection.commit()
            connection.close()

            with Store(path) as migrated:
                columns = {
                    row["name"]
                    for row in migrated.rows(
                        "PRAGMA table_info(telemetry_helper_threads)"
                    )
                }
                ownership = migrated.row(
                    "SELECT * FROM telemetry_helper_threads WHERE native_thread_id = 'helper'"
                )
                self.assertIn("id", columns)
                self.assertIn("native_turn_id", columns)
                self.assertIn("collaboration_item_id", columns)
                self.assertEqual(ownership["parent_turn_id"], "turn")
                self.assertEqual(ownership["collaboration_item_id"], "")

    def test_turn_specific_helper_schema_migrates_without_data_loss(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "turn-specific.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute("""CREATE TABLE telemetry_helper_threads (
                     id INTEGER PRIMARY KEY, native_thread_id TEXT NOT NULL,
                     parent_thread_id TEXT NOT NULL,
                     parent_turn_id TEXT NOT NULL DEFAULT '', native_turn_id TEXT,
                     attributed_action_id INTEGER, first_observed_at TEXT NOT NULL,
                     last_observed_at TEXT NOT NULL,
                     UNIQUE(native_thread_id, parent_thread_id, parent_turn_id))""")
            connection.execute("""INSERT INTO telemetry_helper_threads(
                     native_thread_id, parent_thread_id, parent_turn_id,
                     native_turn_id, attributed_action_id,
                     first_observed_at, last_observed_at)
                     VALUES ('helper', 'parent', 'parent-turn', 'helper-turn',
                             NULL, 'first', 'last')""")
            connection.commit()
            connection.close()

            with Store(path) as migrated:
                ownership = migrated.row(
                    """SELECT collaboration_item_id, native_turn_id
                       FROM telemetry_helper_threads WHERE native_thread_id = 'helper'"""
                )
                self.assertEqual(ownership["collaboration_item_id"], "")
                self.assertEqual(ownership["native_turn_id"], "helper-turn")


if __name__ == "__main__":
    unittest.main()
