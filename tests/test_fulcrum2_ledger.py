from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

from fulcrum.contracts import ActorContext, FulcrumError, ParsedRequest
from fulcrum.instance import resolve_instance
from fulcrum.ledger import (
    CAPTURE_BYTES,
    Ledger,
    LedgerFailure,
    OperationRecord,
    OperationService,
    random_record_id,
)


class LostCreateLedger(Ledger):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.lose_create_response = True

    def run(self, arguments: tuple[str, ...] | list[str], **kwargs: object):  # type: ignore[no-untyped-def]
        observation = super().run(arguments, **kwargs)
        if arguments and arguments[0] == "create" and self.lose_create_response:
            self.lose_create_response = False
            raise LedgerFailure(
                "injected lost create response",
                category="uncertain",
                retryable=True,
                uncertain=True,
            )
        return observation


class Fulcrum2LedgerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name).resolve()
        cls.brain = cls.root / "brain"
        cls.instance = cls.root / "instance"
        cls.brain.mkdir()
        cls.instance.mkdir()
        subprocess.run(["git", "init", "--quiet"], cwd=cls.brain, check=True)
        subprocess.run(
            ["git", "config", "beads.role", "maintainer"],
            cwd=cls.brain,
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
            text=True,
            capture_output=True,
            check=True,
            timeout=60,
        )
        cls.config = cls.brain / "fulcrum.yaml"
        cls.config.write_text(f"brain:\n  root: {cls.brain}\n", encoding="utf-8")
        (cls.instance / "config").symlink_to(cls.config)
        cls.context = resolve_instance(instance=str(cls.instance), config=None)
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
        request_id: str | None = None,
        arguments: dict[str, object] | None = None,
        supplied_input: dict[str, object] | None = None,
    ) -> ParsedRequest:
        return ParsedRequest(
            command=command,
            arguments=arguments or {},
            input=supplied_input or {},
            actor=ActorContext.parse("human"),
            instance=self.context,
            request_id=request_id,
        )

    def test_lost_create_is_adopted_and_exact_request_reuses_one_receipt(self) -> None:
        request_id = str(uuid.uuid4())
        request = self.request(
            ("config", "set"),
            request_id=request_id,
            supplied_input={"policy": {"automatic_capacity": 4}},
        )
        ledger = LostCreateLedger(self.brain)
        created, reused = ledger.create_operation(request)
        self.assertFalse(reused)
        self.assertEqual(created.id, "fc-" + uuid.UUID(request_id).hex)

        restarted = Ledger(self.brain)
        same, reused = restarted.create_operation(request)
        self.assertTrue(reused)
        self.assertEqual(same.id, created.id)

        changed = self.request(
            ("config", "set"),
            request_id=request_id,
            supplied_input={"policy": {"automatic_capacity": 3}},
        )
        with self.assertRaises(FulcrumError) as conflict:
            restarted.create_operation(changed)
        self.assertEqual(conflict.exception.code, "REQUEST_CONFLICT")
        self.assertEqual(conflict.exception.exit_code, 5)

    def test_fc_replacement_preserves_unrelated_metadata(self) -> None:
        request = self.request(
            ("policy", "set"),
            request_id=str(uuid.uuid4()),
            supplied_input={"automatic_capacity": 4},
        )
        operation, _ = self.ledger.create_operation(request)
        self.ledger.run(
            (
                "update",
                operation.id,
                "--metadata",
                json.dumps({"unrelated": {"keep": True}}),
            ),
            mutating=True,
        )
        updated = self.ledger.update_operation(
            operation,
            state="completed",
            step="verified",
            result={"changed": True},
            next_action="No further action is required.",
        )
        self.assertEqual(updated.metadata["unrelated"], {"keep": True})
        self.assertEqual(updated.operation["state"], "completed")
        self.assertEqual(updated.status, "closed")

    def test_record_class_queries_do_not_leak_operations_into_work(self) -> None:
        work_id = random_record_id()
        self.ledger.create_record(
            record_id=work_id,
            kind="work",
            title="Actual work",
            description="A bounded outcome",
            owner="HUMAN",
            issue_type="task",
            fc={"kind": "work", "owner": "HUMAN", "phase": "backlog"},
        )
        work = self.ledger.list_records(kind="work", limit=0)
        self.assertIn(work_id, {item.id for item in work})
        self.assertTrue(all(item.kind == "work" for item in work))
        operations = self.ledger.list_records(kind="operation", limit=0)
        self.assertTrue(all(item.id != work_id for item in operations))

    def test_valid_json_larger_than_diagnostic_capture_is_still_parsed(self) -> None:
        executable = self.root / "large-json-beads"
        executable.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            f"print(json.dumps([{{'id': 'fc-large', 'blob': 'x' * {CAPTURE_BYTES + 1}}}]))\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        observation = Ledger(self.brain, executable=str(executable)).run(("list",))
        self.assertEqual(observation.value[0]["id"], "fc-large")
        self.assertEqual(len(observation.value[0]["blob"]), CAPTURE_BYTES + 1)
        self.assertTrue(observation.truncated)

    def test_cancel_and_wait_report_actual_operation_state(self) -> None:
        target_request = self.request(
            ("project", "add"),
            request_id=str(uuid.uuid4()),
            supplied_input={"id": "toy", "root": str(self.root / "toy")},
        )
        target, _ = self.ledger.create_operation(target_request)
        service = OperationService(self.ledger)

        wait_request = self.request(
            ("operation", "wait"),
            arguments={"id": target.id},
        )
        object.__setattr__(wait_request, "timeout", 0.01)
        with self.assertRaises(FulcrumError) as timed_out:
            service.wait(wait_request)
        self.assertEqual(timed_out.exception.code, "WAIT_TIMEOUT")
        self.assertEqual(timed_out.exception.operation_id, target.id)

        cancel_request = self.request(
            ("operation", "cancel"),
            request_id=str(uuid.uuid4()),
            arguments={"id": target.id},
        )
        cancelled = service.cancel(cancel_request)
        self.assertTrue(cancelled.ok)
        observed = OperationRecord.from_record(self.ledger.show(target.id))  # type: ignore[arg-type]
        self.assertEqual(observed.operation["state"], "cancelled")

    def test_operation_is_inspectable_through_installed_cli(self) -> None:
        request = self.request(
            ("memory", "set"),
            request_id=str(uuid.uuid4()),
            supplied_input={
                "scope": "global",
                "title": "Preference",
                "text": "Keep it literal",
            },
        )
        operation, _ = self.ledger.create_operation(request)
        executable = Path(os.sys.executable).with_name("fulcrum")
        completed = subprocess.run(
            [
                str(executable),
                "operation",
                "show",
                operation.id,
                "--instance",
                str(self.instance),
                "--json",
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["result"]["id"], operation.id)
        self.assertEqual(result["result"]["input"]["input"]["text"], "Keep it literal")


if __name__ == "__main__":
    unittest.main()
