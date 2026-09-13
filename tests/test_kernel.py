from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fulcrum.kernel import (
    LeaseRequest,
    acquire_lease,
    invariant_violations,
    release_lease,
    schedule_action_retry,
)
from fulcrum.readiness import progress_readiness
from fulcrum.store import Store


class KernelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.log = root / "workflow.jsonl"
        self.store = Store(root / "state.db", event_log=self.log)
        self.store.execute(
            "INSERT INTO projects(project_id, repo_path) VALUES ('p', '/tmp/p')"
        )
        self.store.execute(
            "INSERT INTO meta(key, value) VALUES ('global_limit', '2'), ('project_limits', ?)",
            (json.dumps({"p": 1}),),
        )
        self.first = self.store.register_task(
            native_thread_id="one",
            role="executor",
            description="One",
            model="sol",
            reasoning_effort="high",
            project_id="p",
        )
        self.second = self.store.register_task(
            native_thread_id="two",
            role="executor",
            description="Two",
            model="sol",
            reasoning_effort="high",
            project_id="p",
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def _request(
        self, task_id: int, *, pair_id: int, conflicts: tuple[str, ...] = ()
    ) -> LeaseRequest:
        return LeaseRequest(
            task_id=task_id,
            assignment_id=None,
            kind="implement",
            payload={},
            project_ids=("p",),
            pair_id=pair_id,
            conflict_keys=conflicts,
        )

    def test_capacity_is_rechecked_inside_each_lease_transaction(self) -> None:
        first = acquire_lease(self.store, self._request(self.first["id"], pair_id=1))
        second = acquire_lease(self.store, self._request(self.second["id"], pair_id=2))
        self.assertTrue(first.admitted)
        self.assertFalse(second.admitted)
        self.assertIn("project capacity is full for p", second.blockers)
        self.assertEqual(len(self.store.rows("SELECT * FROM reservations")), 1)

    def test_conflict_keys_block_even_when_capacity_remains(self) -> None:
        self.store.execute(
            "UPDATE meta SET value = ? WHERE key = 'project_limits'",
            (json.dumps({"p": 2}),),
        )
        acquire_lease(
            self.store,
            self._request(self.first["id"], pair_id=1, conflicts=("service",)),
        )
        second = acquire_lease(
            self.store,
            self._request(self.second["id"], pair_id=2, conflicts=("service",)),
        )
        self.assertFalse(second.admitted)
        self.assertIn("conflicting resources are leased: service", second.blockers)

    def test_retry_is_runnable_and_exhaustion_becomes_an_explicit_hold(self) -> None:
        decision = acquire_lease(self.store, self._request(self.first["id"], pair_id=1))
        assert decision.action is not None
        action_id = int(decision.action["id"])
        self.assertTrue(
            schedule_action_retry(
                self.store, action_id, "runtime not ready", delay_seconds=0
            )
        )
        pending = self.store.row("SELECT * FROM actions WHERE id = ?", (action_id,))
        self.assertEqual(pending["state"], "pending")
        self.assertIsNotNone(pending["next_attempt_at"])
        self.store.execute(
            "UPDATE actions SET attempt_count = 7 WHERE id = ?", (action_id,)
        )
        self.assertFalse(schedule_action_retry(self.store, action_id, "still down"))
        held = self.store.row("SELECT * FROM actions WHERE id = ?", (action_id,))
        self.assertEqual(held["state"], "failed")
        self.assertIsNotNone(held["operator_hold_id"])
        self.assertIsNone(
            self.store.row(
                "SELECT * FROM reservations WHERE action_id = ?", (action_id,)
            )
        )

    def test_operation_attempts_and_redacted_append_log_are_durable(self) -> None:
        operation = self.store.create_operation(
            "test", "target", {"token": "do-not-log", "value": "safe"}
        )
        attempt = self.store.begin_operation_attempt(operation)
        self.store.finish_operation_attempt(
            operation,
            attempt,
            state="complete",
            result={"ok": True},
            stdout="done",
            duration_ms=12,
        )
        row = self.store.row(
            "SELECT * FROM operation_attempts WHERE operation_id = ?", (operation,)
        )
        self.assertEqual(row["state"], "complete")
        self.assertEqual(row["duration_ms"], 12)
        records = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertTrue(any(item["kind"] == "operation_complete" for item in records))

    def test_health_detects_stale_reconciliation_and_dead_worker(self) -> None:
        old = (
            (datetime.now(timezone.utc) - timedelta(minutes=10))
            .isoformat()
            .replace("+00:00", "Z")
        )
        self.store.execute(
            "INSERT INTO meta(key, value) VALUES ('last_reconciliation', ?)", (old,)
        )
        self.store.heartbeat("fallback", state="degraded", error="boom")
        ready, reasons = progress_readiness(self.store, critical_workers={"fallback"})
        self.assertFalse(ready)
        self.assertTrue(any("stale" in reason for reason in reasons))
        self.assertIn("critical worker fallback is not running", reasons)

    def test_invariants_reject_a_lease_without_a_runnable_owner(self) -> None:
        decision = acquire_lease(self.store, self._request(self.first["id"], pair_id=1))
        assert decision.action is not None
        action_id = int(decision.action["id"])
        self.store.execute(
            "UPDATE actions SET state = 'failed' WHERE id = ?", (action_id,)
        )
        self.assertEqual(
            invariant_violations(self.store),
            [f"reservation for action {action_id} has no runnable owner"],
        )
        release_lease(self.store, action_id, reason="test cleanup")
        self.assertEqual(invariant_violations(self.store), [])


if __name__ == "__main__":
    unittest.main()
