from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from fulcrum.contracts import CommandResult
from fulcrum.diagnostics import (
    DiagnosticLog,
    DiagnosticService,
    _loop_health,
    _runtime_component,
)
from fulcrum.supervision import HealthFile
from tests.support import MemoryLedger, record, request


class DiagnosticLogTest(unittest.TestCase):
    def test_health_retains_recovered_failure_episode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            clock = Mock()
            clock.now.return_value = datetime(2026, 9, 16, tzinfo=timezone.utc)
            health = HealthFile(root / "service-health.json", clock)
            health.failure("reconciliation", RuntimeError("stale approval"))
            health.failure("reconciliation", RuntimeError("stale approval"))
            health.success("reconciliation", {"work": 2})

            retained = health.values["reconciliation"]
            base = request()
            health_request = replace(
                base,
                instance=replace(base.instance, instance_root=root),
            )
            loops = _loop_health(
                health_request,
                {"timing": {"reconcile_seconds": 15}},
                clock.now(),
            )

        self.assertEqual(retained["state"], "healthy")
        self.assertIsNone(retained["failure_episode"])
        self.assertEqual(retained["last_failure_episode"]["count"], 2)
        self.assertEqual(retained["last_failure_episode"]["error"], "stale approval")
        self.assertIn("recovered_at", retained["last_failure_episode"])
        reconciliation = next(
            loop for loop in loops if loop["name"] == "reconciliation"
        )
        self.assertEqual(reconciliation["evidence"]["last_failure_episode"]["count"], 2)

    def test_runtime_component_uses_live_capabilities(self) -> None:
        capabilities = {
            "available": True,
            "endpoint": "ws://127.0.0.1:4500",
            "methods": ["thread/list", "turn/start"],
            "models": {"gpt-test": ["medium"]},
            "gaps": [],
        }
        with patch(
            "fulcrum.runtime_service.RuntimeService.capabilities",
            return_value=CommandResult.query(capabilities),
        ):
            component = _runtime_component(request(("doctor",)))

        self.assertEqual(component["state"], "healthy")
        self.assertEqual(component["affected_commands"], [])
        self.assertEqual(component["next_commands"], [])
        self.assertEqual(component["evidence"]["models"], ["gpt-test"])

    def test_redaction_stream_caps_rotation_and_pruning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            previous = os.environ.get("FULCRUM_TEST_API_TOKEN")
            os.environ["FULCRUM_TEST_API_TOKEN"] = "very-secret-value"
            try:
                log = DiagnosticLog(
                    root, retention_days=1, max_bytes=8192, capture_bytes=32
                )
                retained = log.append(
                    {
                        "event": "adapter",
                        "authorization": "Bearer abcdefghijklmnop",
                        "error": "value=very-secret-value",
                        "stdout": "x" * 100,
                    }
                )
            finally:
                if previous is None:
                    os.environ.pop("FULCRUM_TEST_API_TOKEN", None)
                else:
                    os.environ["FULCRUM_TEST_API_TOKEN"] = previous
            serialized = json.dumps(retained)
            self.assertNotIn("very-secret-value", serialized)
            self.assertNotIn("abcdefghijklmnop", serialized)
            self.assertEqual(retained["authorization"], "[REDACTED]")
            self.assertTrue(retained["stdout"]["truncated"])
            old = root / "events-20000101-00000.jsonl"
            old.write_text("{}\n", encoding="utf-8")
            timestamp = (datetime.now(timezone.utc) - timedelta(days=3)).timestamp()
            os.utime(old, (timestamp, timestamp))
            result = log.prune()
            self.assertIn(old.name, result["removed_files"])
            self.assertFalse(old.exists())

    def test_bead_filter_includes_correlated_multi_bead_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log = DiagnosticLog(Path(temporary))
            retained = log.append(
                {
                    "event": "reconciliation_completed",
                    "associated_beads": ["fc-one", "fc-two"],
                }
            )

            result = log.read(bead="fc-two", limit=0)

        self.assertEqual(result["items"], [retained])

    def test_trace_joins_operations_tasks_provider_and_background_spans(self) -> None:
        initial = "0" * 40
        repaired = "a" * 40
        work = record(
            delivery={"source_oid": repaired, "provider_handle": "provider-repaired"},
            failed_delivery_finishes=[{"source_oid": initial}],
            ownership_operation="fc-entry",
            last_transition="fc-recovery",
            dispatch={
                "role": "executor",
                "decision_operation": "fc-marshal",
                "authorized_at": "2026-09-16T00:00:01Z",
                "authorized_scope": {
                    "summary": "Repair behavior",
                    "acceptance": ["Behavior passes"],
                    "evidence": [],
                    "implementation_notes": [],
                    "finish_operation": "fc-scope",
                },
            },
            recovery_fence={
                "operation_id": "fc-takeover",
                "scope": "bead:fc-work",
                "state": "released",
                "owner_thread": "justiciar",
                "started_at": "2026-09-16T00:00:04Z",
                "released_at": "2026-09-16T00:00:05Z",
            },
            human_resolutions=[
                {
                    "reason_id": "human:fc-human",
                    "answer": "Continue Executor routing.",
                    "resume_role": "executor",
                    "operation_id": "fc-human-resolve",
                    "resolved_at": "2026-09-16T00:00:01Z",
                }
            ],
        )
        marshal_task = record(
            "fc-marshal-task",
            kind="task",
            role="marshal",
            thread_id="standing-marshal",
            creation_operation="fc-marshal",
            last_observed={"last_turn": {"id": "turn-marshal", "status": "completed"}},
        )
        marshal = record(
            "fc-marshal",
            kind="operation",
            owner="fc-marshal-task",
            state="completed",
            command=["marshal", "decide"],
            step="decision_applied",
            planned={"selected_ids": ["fc-work"]},
            external={"turn_id": "turn-marshal"},
            created_at="2026-09-16T00:00:00Z",
            completed_at="2026-09-16T00:00:01Z",
        )
        finish = record(
            "fc-entry",
            kind="operation",
            owner="executor",
            bead_id="fc-work",
            state="completed",
            command=["finish"],
            step="executor_finish_sealed",
            planned={
                "finish": {"source_oid": initial},
                "children": {
                    "local_check": {"operation_id": "fc-check", "state": "failed"}
                },
            },
            result={"source_oid": initial},
            created_at="2026-09-16T00:00:02Z",
            completed_at="2026-09-16T00:00:03Z",
        )
        check = record(
            "fc-check",
            kind="operation",
            owner="executor",
            state="failed",
            command=["validation", "check"],
            step="local_check_failed",
            result={
                "source_oid": initial,
                "provider_handle": "provider-initial",
            },
            created_at="2026-09-16T00:00:03Z",
            completed_at="2026-09-16T00:00:04Z",
        )
        recovery = record(
            "fc-recovery",
            kind="operation",
            owner="warden",
            bead_id="fc-work",
            state="completed",
            command=["finish"],
            step="warden_judgment_sealed",
            result={
                "source_oid": repaired,
                "delivery": {"provider_handle": "provider-repaired"},
            },
            created_at="2026-09-16T00:00:05Z",
            completed_at="2026-09-16T00:00:06Z",
        )
        publication = record(
            "fc-publication",
            kind="operation",
            owner="controller",
            state="completed",
            command=["ledger", "sync"],
            step="ledger_synchronized",
            planned={"changes": [{"id": "fc-work"}]},
            created_at="2026-09-16T00:00:06Z",
            completed_at="2026-09-16T00:00:07Z",
        )
        ledger = MemoryLedger(
            work, marshal_task, marshal, finish, check, recovery, publication
        )
        with tempfile.TemporaryDirectory() as temporary:
            log = DiagnosticLog(Path(temporary))
            log.append(
                {
                    "event": "reconciliation_completed",
                    "pass_id": "pass-1",
                    "associated_beads": ["fc-work", "fc-blocker"],
                    "duration_ms": 4,
                    "gaps": [
                        {
                            "component": "task_reconciliation",
                            "bead_id": "fc-blocker",
                            "code": "STALE_SOURCE",
                            "reason": "stale approval isolated",
                        }
                    ],
                }
            )
            log.append(
                {
                    "event": "dispatch_timeline",
                    "correlation_id": "fc-dispatch",
                    "associated_beads": ["fc-work"],
                    "bead_id": "fc-work",
                    "stages": {
                        "marshal_decision_received_at": "2026-09-16T00:00:01Z",
                        "dispatch_enqueued_at": "2026-09-16T00:00:02Z",
                        "worktree_preparation_completed_at": "2026-09-16T00:00:03Z",
                        "role_entry_completed_at": "2026-09-16T00:00:04Z",
                        "native_task_observed_at": "2026-09-16T00:00:04Z",
                    },
                }
            )
            log.append(
                {
                    "event": "publication_completed",
                    "publication_span_id": "publish-1",
                    "associated_beads": ["fc-work"],
                    "operation_id": "fc-publication",
                    "duration_ms": 1000,
                }
            )
            trace_request = request(
                ("trace",),
                arguments={"bead": "fc-work", "limit": 0},
            )
            with (
                patch("fulcrum.diagnostics._ledger", return_value=ledger),
                patch(
                    "fulcrum.diagnostics.DiagnosticLog.from_request",
                    return_value=log,
                ),
            ):
                result = DiagnosticService().trace(trace_request).result

        transitions = {item["transition"] for item in result["items"]}
        self.assertIn("task_observed", transitions)
        self.assertIn("reconciliation_completed", transitions)
        self.assertIn("dispatch_timeline", transitions)
        self.assertIn("publication_completed", transitions)
        self.assertIn("scope_authorized", transitions)
        self.assertIn("recovery_fence", transitions)
        self.assertIn("human_resolution", transitions)
        finish_item = next(item for item in result["items"] if item["id"] == "fc-entry")
        self.assertEqual(finish_item["child_operation_ids"], ["fc-check"])
        check_item = next(item for item in result["items"] if item["id"] == "fc-check")
        self.assertEqual(check_item["parent_operation_ids"], ["fc-entry"])
        handles = {
            handle for item in result["items"] for handle in item["provider_handles"]
        }
        self.assertEqual(handles, {"provider-initial", "provider-repaired"})
        self.assertEqual(result["source_oids"], [initial, repaired])
        spans = {item.get("span_id") for item in result["items"]}
        self.assertIn("pass-1", spans)
        self.assertIn("publish-1", spans)
        timeline = next(
            item
            for item in result["items"]
            if item["transition"] == "dispatch_timeline"
        )
        self.assertEqual(timeline["correlation_id"], "fc-dispatch")
        self.assertIn("native_task_observed_at", timeline["stages"])
        self.assertTrue(
            any(
                gap.get("component") == "task_reconciliation"
                and gap.get("bead_id") == "fc-blocker"
                for gap in result["gaps"]
            )
        )


if __name__ == "__main__":
    unittest.main()
