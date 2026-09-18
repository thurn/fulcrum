from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fulcrum.contracts import ActorContext, InstanceContext, ParsedRequest
from fulcrum.desktop_reset import _inventory
from fulcrum.ledger import LedgerRecord


class _Ledger:
    def __init__(self, records: list[LedgerRecord]) -> None:
        self.records = records

    def list_records(self, *, limit: int) -> list[LedgerRecord]:
        assert limit == 0
        return self.records


def _request(tmp_path: Path) -> ParsedRequest:
    return ParsedRequest(
        command=("reset",),
        arguments={"hard": True, "yes": True},
        input={},
        actor=ActorContext(kind="human"),
        instance=InstanceContext(
            instance_root=tmp_path / "instance",
            config_path=tmp_path / "fulcrum.yaml",
            brain_root=tmp_path / "brain",
            socket_path=tmp_path / "controller.sock",
            lock_path=tmp_path / "lock",
            explicit_selection=True,
        ),
        request_id="00000000-0000-4000-8000-000000000001",
    )


class DesktopResetTests(unittest.TestCase):
    def test_inventory_ignores_stale_assignment_on_closed_work(self) -> None:
        record = LedgerRecord(
            id="fc-closed",
            title="closed work",
            status="closed",
            assignee="SYSTEM",
            labels=("fc:work",),
            metadata={
                "fc": {
                    "desktop": {
                        "assignment": {"state": "active", "task_id": "old-task"}
                    }
                }
            },
            native={},
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("fulcrum.desktop_reset._services", return_value={}),
            patch(
                "fulcrum.desktop_reset.inspect_service",
                return_value=SimpleNamespace(running=False),
            ),
        ):
            inventory = _inventory(
                _request(Path(directory)), _Ledger([record])  # type: ignore[arg-type]
            )

        self.assertEqual(inventory["blockers"], [])
