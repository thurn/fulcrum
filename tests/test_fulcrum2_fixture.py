from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fulcrum.contracts import (
    ActorContext,
    CommandResult,
    CommandState,
    FulcrumError,
    InstanceContext,
    ParsedRequest,
)
from fulcrum.fixture_service import FixtureService
from fulcrum.ledger import OperationRecord, operation_id


class _FixtureLedger:
    def __init__(self, request: ParsedRequest) -> None:
        assert request.request_id is not None
        self.record = OperationRecord(
            id=operation_id(request.request_id),
            title="fixture",
            status="open",
            assignee="HUMAN",
            labels=(),
            metadata={
                "fc": {
                    "kind": "operation",
                    "command": "fixture.create",
                    "request_id": request.request_id,
                    "input": {},
                    "state": "accepted",
                }
            },
            native={},
        )
        self.updated: dict[str, object] | None = None

    def create_operation(
        self, *_args: object, **_kwargs: object
    ) -> tuple[OperationRecord, bool]:
        return self.record, False

    def update_operation(
        self, operation: OperationRecord, **changes: object
    ) -> OperationRecord:
        self.updated = dict(changes)
        return operation


class FixtureFailureCleanupTest(unittest.TestCase):
    def test_failed_setup_retains_exact_cleanup_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "fixture"
            request_id = str(uuid.uuid4())
            request = ParsedRequest(
                command=("fixture", "create"),
                arguments={},
                input={
                    "root": str(root),
                    "model": "gpt-5.6-luna",
                    "effort": "low",
                    "capacity": 4,
                    "source_sync": True,
                    "runtime": {"kind": "codex", "endpoint": "ws://runtime"},
                    "delivery": {
                        "kind": "tollgate",
                        "executable": "/missing/tg",
                    },
                },
                actor=ActorContext(kind="human"),
                instance=InstanceContext(
                    instance_root=root / "instance",
                    config_path=root / "brain" / "fulcrum.yaml",
                    brain_root=root / "brain",
                    socket_path=root / "instance" / "controller.sock",
                    lock_path=root / "brain" / ".fulcrum-controller.lock",
                    explicit_selection=True,
                ),
                request_id=request_id,
                offline=True,
            )
            ledger = _FixtureLedger(request)

            def scaffold(selected: Path) -> dict[str, Path]:
                paths = {
                    "brain": selected / "brain",
                    "project": selected / "project",
                    "brain_remote": selected / "remotes" / "brain.git",
                    "source_remote": selected / "remotes" / "source.git",
                }
                for path in paths.values():
                    path.mkdir(parents=True, exist_ok=True)
                (selected / "providers").mkdir()
                return paths

            setup = CommandResult(
                ok=False,
                state=CommandState.FAILED,
                operation_id="fc-setup",
                result={"capabilities": {"delivery": {"available": False}}},
            )
            with (
                patch(
                    "fulcrum.fixture_service._scaffold_fixture", side_effect=scaffold
                ),
                patch("fulcrum.fixture_service.run_setup", return_value=setup),
                patch("fulcrum.fixture_service.Ledger", return_value=ledger),
            ):
                with self.assertRaises(FulcrumError) as raised:
                    FixtureService().create(request)

            self.assertEqual(raised.exception.code, "FIXTURE_CREATE_FAILED")
            assert raised.exception.details is not None
            cleanup = raised.exception.details["cleanup_command"]
            self.assertIn(operation_id(request_id), cleanup)
            assert ledger.updated is not None
            self.assertEqual(ledger.updated["state"], "uncertain")
            inventory = ledger.updated["result"]
            assert isinstance(inventory, dict)
            self.assertEqual(
                inventory["provider_ownership"],
                {
                    "runtime_project": "absent",
                    "delivery_repository": "absent",
                },
            )


if __name__ == "__main__":
    unittest.main()
