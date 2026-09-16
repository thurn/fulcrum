from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from fulcrum.configuration import default_config
from fulcrum.continuity import _archive_eligible, _task_ownership_released
from fulcrum.contracts import ActorContext, CommandResult, CommandState, FulcrumError
from fulcrum.leadership import AdmissionService, capacity_snapshot
from fulcrum.ledger import CAPTURE_BYTES, Ledger, LedgerFailure
from fulcrum.roles import _role_workspace
from fulcrum.work import WorkService
from tests.support import MemoryLedger, record, request


class ReceiptTests(unittest.TestCase):
    def test_identical_requests_reuse_receipt_and_conflicts_do_not_write(self):
        ledger = MemoryLedger()
        original = request(input={"summary": "original"})
        first, reused = ledger.create_operation(original)
        self.assertFalse(reused)
        same, reused = ledger.create_operation(original)
        self.assertTrue(reused)
        self.assertEqual(first.id, same.id)
        for changed in [
            replace(original, input={"summary": "changed"}),
            replace(original, command=("work", "close")),
        ]:
            with (
                self.subTest(command=changed.command),
                self.assertRaises(FulcrumError) as error,
            ):
                ledger.create_operation(changed)
            self.assertEqual(error.exception.code, "REQUEST_CONFLICT")
        self.assertEqual(ledger.writes, [first.id])

    def test_uncertain_create_adopts_only_exact_observed_record(self):
        planned = record()
        ledger = Ledger(request().instance.brain_root, executable=sys.executable)
        lost = LedgerFailure(
            "lost response", category="uncertain", retryable=True, uncertain=True
        )
        for observed, expected in [
            (planned, None),
            (record(owner="wrong"), FulcrumError),
            (None, LedgerFailure),
        ]:
            with (
                self.subTest(observed=observed),
                patch.object(ledger, "run", side_effect=lost) as run,
                patch.object(ledger, "show", return_value=observed) as show,
            ):

                def create():
                    return ledger.create_record(
                        record_id=planned.id,
                        kind="work",
                        title="work",
                        description="work",
                        owner="HUMAN",
                        fc=planned.fc,
                    )

                if expected:
                    with self.assertRaises(expected):
                        create()
                else:
                    self.assertEqual(create(), planned)
                run.assert_called_once()
                show.assert_called_once_with(planned.id)

    def test_adapter_parses_large_json_but_bounds_diagnostics(self):
        ledger = Ledger(request().instance.brain_root, executable=sys.executable)
        payload = '{"blob":"' + "x" * (CAPTURE_BYTES + 1) + '"}'

        def completed(argv, **kwargs):
            self.assertEqual(argv[-2:], ["list", "--all"])
            self.assertEqual(kwargs["timeout"], 0.25)
            kwargs["stdout"].write(payload.encode())
            return subprocess.CompletedProcess(argv, 0)

        with patch("fulcrum.ledger.subprocess.run", side_effect=completed):
            observed = ledger.run(("list", "--all"), timeout=0.25)
        self.assertEqual(len(observed.value["blob"]), CAPTURE_BYTES + 1)
        self.assertTrue(observed.truncated)
        self.assertLessEqual(len(observed.stdout), CAPTURE_BYTES)

    def test_adapter_timeout_and_malformed_mutations_remain_uncertain(self):
        ledger = Ledger(request().instance.brain_root, executable=sys.executable)
        for mutating in [False, True]:
            for failure in ["timeout", "malformed"]:

                def respond(argv, **kwargs):
                    if failure == "timeout":
                        raise subprocess.TimeoutExpired(argv, 0.1)
                    kwargs["stdout"].write(b"{broken")
                    return subprocess.CompletedProcess(argv, 0)

                with (
                    self.subTest(mutating=mutating, failure=failure),
                    patch("fulcrum.ledger.subprocess.run", side_effect=respond),
                    self.assertRaises(LedgerFailure) as error,
                ):
                    ledger.run(("update",), mutating=mutating)
                self.assertEqual(error.exception.uncertain, mutating)

    def test_receipt_snapshot_merge_keeps_fresh_changes_with_two_verified_reads(self):
        import json
        from fulcrum.ledger import CommandObservation, OperationRecord

        ledger = Ledger(request().instance.brain_root, executable=sys.executable)
        original = record(
            "fc-op", kind="operation", state="running", attempts=0, step="before"
        )
        snapshot = OperationRecord.from_record(original)
        for changed, conflict in [({"attempts": 2}, False), ({"owner": "other"}, True)]:
            current = {**original.fc, **changed}
            commands = []

            def run(arguments, **kwargs):
                nonlocal current
                commands.append(arguments[0])
                if arguments[0] == "update":
                    current = json.loads(arguments[arguments.index("--metadata") + 1])[
                        "fc"
                    ]
                    return CommandObservation(None, 0, "", "", False)
                self.assertEqual(arguments[0], "show")
                native = {**original.native, "metadata": {"fc": current}}
                return CommandObservation([native], 0, "", "", False)

            with (
                self.subTest(changed=changed),
                patch.object(ledger, "run", side_effect=run),
            ):
                if conflict:
                    with self.assertRaises(FulcrumError):
                        ledger.update_operation(snapshot, step="after")
                    self.assertEqual(commands, ["show"])
                else:
                    result = ledger.update_operation(snapshot, step="after")
                    self.assertEqual(result.operation["attempts"], 2)
                    self.assertEqual(result.operation["step"], "after")
                    self.assertEqual(commands, ["show", "update", "show"])


class OwnershipTests(unittest.TestCase):
    def test_wrong_actor_and_stale_acquisition_fail_before_writes(self):
        ledger = MemoryLedger(record(owner="owner", ownership_operation="current"))
        for actor, acquisition in [("intruder", "current"), ("owner", "stale")]:
            attempt = request(
                actor=ActorContext(kind="task", task_id=actor),
                thread_id=actor,
                ownership_operation=acquisition,
                input={"summary": "update"},
            )
            with (
                self.subTest(actor=actor),
                patch("fulcrum.work._ledger", return_value=ledger),
                self.assertRaises(FulcrumError) as error,
            ):
                WorkService().update(attempt)
            self.assertEqual(error.exception.code, "OWNERSHIP_CONFLICT")
        self.assertEqual(ledger.writes, [])

    def test_cycle_rejected_before_dependency_or_receipt_mutation(self):
        ledger = MemoryLedger(record(), record("fc-other"))
        ledger.edges = {"fc-other": ["fc-work"]}
        with (
            patch("fulcrum.work._ledger", return_value=ledger),
            self.assertRaises(FulcrumError) as error,
        ):
            WorkService().dependencies(
                request(("work", "dependencies"), input={"add": ["fc-other"]})
            )
        self.assertEqual(error.exception.code, "INVALID_INPUT")
        self.assertEqual(ledger.writes, [])
        self.assertEqual(ledger.edges, {"fc-other": ["fc-work"]})

    def test_delivered_close_requires_promotion_sync_and_cleanup(self):
        settled = {
            "promotion": {"state": "observed"},
            "synchronization": {"state": "observed"},
            "cleanup": {"state": "observed"},
        }
        for missing in settled:
            delivery = deepcopy(settled)
            delivery.pop(missing)
            ledger = MemoryLedger(record(delivery=delivery))
            with (
                self.subTest(missing=missing),
                patch("fulcrum.work._ledger", return_value=ledger),
                self.assertRaises(FulcrumError) as error,
            ):
                WorkService().close(
                    request(
                        ("work", "close"),
                        arguments={
                            "id": "fc-work",
                            "outcome": "delivered",
                            "summary": "done",
                        },
                    )
                )
            self.assertEqual(error.exception.code, "DELIVERY_NOT_OBSERVED")
            self.assertEqual(ledger.writes, [])


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.work = record(
            project="toy",
            requested_role="executor",
            dispatch={
                "role": "executor",
                "decision_operation": "fc-authorized",
                "human_bypass": False,
            },
        )
        self.ledger = MemoryLedger(self.work)
        self.config = default_config(request().instance.brain_root)
        self.config["policy"].update(automatic_capacity=1, default_project_capacity=1)
        self.config["projects"] = {"toy": {}}
        self.service = AdmissionService()
        self.enter = Mock(return_value=CommandResult.query({"entered": True}))
        self.prepare = Mock(
            return_value=CommandResult(
                ok=True,
                state=CommandState.COMPLETED,
                operation_id="fc-workspace",
                result={"workspace": {"owned": True, "exists": True}},
            )
        )
        for target, value in [("_ledger", self.ledger), ("_config", self.config)]:
            p = patch(f"fulcrum.leadership.{target}", return_value=value)
            p.start()
            self.addCleanup(p.stop)
        p = patch("fulcrum.leadership.RoleService.enter", self.enter)
        p.start()
        self.addCleanup(p.stop)
        p = patch(
            "fulcrum.delivery_service.DeliveryService.worktree_prepare", self.prepare
        )
        p.start()
        self.addCleanup(p.stop)

    def dispatch(self):
        return self.service.dispatch(request(("dispatch",)))

    def test_capacity_queue_starts_after_observed_capacity_is_freed(self):
        self.ledger.rows["fc-active"] = record(
            "fc-active",
            kind="task",
            thread_id="native-active",
            project="toy",
            last_observed={"runtime_status": "active", "active_turn": "turn"},
        )
        queued = self.dispatch()
        self.assertTrue(queued.result["result"]["queued"])
        self.enter.assert_not_called()
        self.ledger.rows["fc-active"] = record(
            "fc-active",
            kind="task",
            thread_id="native-active",
            project="toy",
            last_observed={"runtime_status": "idle", "active_turn": None},
        )
        started = self.dispatch()
        self.assertTrue(started.result["result"]["started"])
        self.enter.assert_called_once()
        entry = self.enter.call_args.args[0]
        self.assertEqual(entry.input["bead"], "fc-work")
        self.assertEqual(entry.arguments["role"], "executor")
        self.prepare.assert_called_once()

    def test_workspace_failure_blocks_executor_before_role_entry(self):
        self.prepare.return_value = CommandResult(
            ok=False,
            state=CommandState.FAILED,
            operation_id="fc-workspace",
            result={"error": {"code": "WORKSPACE_FAILED"}},
        )

        result = self.dispatch()

        self.assertFalse(result.ok)
        self.assertEqual(result.result["step"], "workspace_prepare_failed")
        self.enter.assert_not_called()

    def test_paused_and_dependency_blocked_work_never_enters_runtime(self):
        for reason in ["paused", "dependency"]:
            with self.subTest(reason=reason):
                self.config["policy"]["paused_projects"] = (
                    ["toy"] if reason == "paused" else []
                )
                self.ledger.edges["fc-work"] = (
                    ["fc-missing"] if reason == "dependency" else []
                )
                self.assertTrue(self.dispatch().result["result"]["queued"])
        self.enter.assert_not_called()

    def test_future_plan_cannot_be_dispatched_even_with_human_bypass(self):
        self.ledger.rows["fc-work"] = record(
            project="toy", plan={"activation": "future"}
        )
        result = self.service.dispatch(
            request(("dispatch",), arguments={"bead": "fc-work", "human": True})
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.result["error"]["code"], "ACTIVATION_REQUIRED")
        self.enter.assert_not_called()

    def test_prepared_scope_rejects_direct_and_stale_weaver_authorization(self):
        for direct in (True, False):
            with self.subTest(direct=direct):
                self.ledger.rows["fc-work"] = record(
                    project="toy",
                    requested_role="weaver",
                    scope={"summary": "Implementation-ready scope."},
                    dispatch=(
                        None
                        if direct
                        else {
                            "role": "weaver",
                            "decision_operation": "fc-stale",
                            "human_bypass": False,
                        }
                    ),
                )
                arguments = {"bead": "fc-work", "authorize": direct}
                before = list(self.ledger.writes)
                with self.assertRaises(FulcrumError) as error:
                    self.service.dispatch(request(("dispatch",), arguments=arguments))
                self.assertEqual(error.exception.code, "REPEATED_AUTHORING")
                self.assertEqual(self.ledger.writes, before)
        self.enter.assert_not_called()

    def test_prepared_scope_allows_matching_material_clarification(self):
        self.ledger.rows["fc-work"] = record(
            project="toy",
            requested_role="weaver",
            scope={"summary": "Implementation-ready scope."},
            clarification={
                "question": "Which of the two named formats is authoritative?",
                "decision_operation": "fc-clarify",
            },
            dispatch={
                "role": "weaver",
                "decision_operation": "fc-clarify",
                "human_bypass": False,
            },
        )

        result = self.dispatch()

        self.assertTrue(result.result["result"]["started"])
        self.assertEqual(self.enter.call_args.args[0].arguments["role"], "weaver")
        self.prepare.assert_not_called()

    def test_unknown_tasks_and_reservations_count_without_double_counting(self):
        self.ledger.rows["fc-work"] = record(
            project="toy",
            dispatch={"reservation": {"operation_id": "fc-start", "state": "unknown"}},
        )
        self.ledger.rows["fc-task"] = record(
            "fc-task",
            kind="task",
            thread_id="native",
            work_bead="fc-work",
            project="toy",
        )
        self.ledger.rows["fc-duplicate"] = replace(
            self.ledger.rows["fc-task"], id="fc-duplicate"
        )
        snapshot = capacity_snapshot(self.ledger, self.config)
        self.assertEqual(snapshot["occupied"], 1)
        self.assertEqual(snapshot["unknown_managed_ids"], ["native"])
        self.assertEqual(snapshot["pending_start_reservations"], [])
        del self.ledger.rows["fc-task"], self.ledger.rows["fc-duplicate"]
        snapshot = capacity_snapshot(self.ledger, self.config)
        self.assertEqual(snapshot["occupied"], 1)
        self.assertEqual(snapshot["pending_start_reservations"], ["fc-start"])

    def test_locks_are_per_bead_and_shared_for_the_same_bead(self):
        self.assertIs(self.service._bead_lock("a"), self.service._bead_lock("a"))
        self.assertIsNot(self.service._bead_lock("a"), self.service._bead_lock("b"))


class RoleWorkspaceTests(unittest.TestCase):
    def test_executor_and_warden_require_a_distinct_verified_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            workspace = Path(directory) / "worktree"
            root.mkdir()
            workspace.mkdir()
            project = {"root": str(root)}
            prepared = record(
                worktree={
                    "path": str(workspace),
                    "exists": True,
                    "owned": True,
                    "dirty": False,
                }
            )
            for role in ("executor", "warden"):
                self.assertEqual(
                    _role_workspace(prepared, role, project), str(workspace.resolve())
                )
            for invalid in (
                record(),
                record(
                    worktree={
                        "path": str(root),
                        "exists": True,
                        "owned": True,
                        "dirty": False,
                    }
                ),
                record(
                    worktree={
                        "path": str(workspace),
                        "exists": True,
                        "owned": False,
                        "dirty": False,
                    }
                ),
            ):
                with self.subTest(worktree=(invalid.fc or {}).get("worktree")):
                    with self.assertRaises(FulcrumError):
                        _role_workspace(invalid, "executor", project)


class ArchiveLifecycleTests(unittest.TestCase):
    def test_terminal_task_is_immediately_eligible_after_ownership_transfer(self):
        work = record(owner="warden", role="warden", phase="reviewing")
        executor = record(
            "fc-executor",
            kind="task",
            owner="executor",
            thread_id="executor",
            work_bead="fc-work",
            role="executor",
        )
        ledger = MemoryLedger(work, executor)
        facts = Mock(archived=False, active_turn=None, runtime_status="idle")

        self.assertTrue(_archive_eligible(ledger, executor, facts))
        self.assertTrue(_task_ownership_released(ledger, executor))
