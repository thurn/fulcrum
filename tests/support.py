"""Small record storage double; workflow decisions remain production code."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import uuid
import tempfile

_TEMP = tempfile.TemporaryDirectory(prefix="fulcrum-tests-")
BRAIN = Path(_TEMP.name) / "brain"
BRAIN.mkdir()

from fulcrum.contracts import ActorContext, InstanceContext, ParsedRequest
from fulcrum.ledger import CommandObservation, Ledger, LedgerRecord


def request(command=("work", "update"), **changes):
    value = ParsedRequest(
        command=command,
        arguments={"id": "fc-work", "bead": "fc-work"},
        input={},
        actor=ActorContext(kind="human"),
        instance=InstanceContext(
            instance_root=Path("/unused/instance"),
            config_path=Path("/unused/brain/fulcrum.yaml"),
            brain_root=BRAIN,
            socket_path=Path("/unused/instance/controller.sock"),
            lock_path=Path("/unused/brain/.lock"),
            explicit_selection=True,
        ),
        request_id=str(uuid.uuid4()),
        project="toy",
        timeout=1,
    )
    return replace(value, **changes)


def record(identifier="fc-work", *, kind="work", status="open", owner="HUMAN", **fc):
    return LedgerRecord.from_native(
        {
            "id": identifier,
            "title": identifier,
            "status": status,
            "assignee": owner,
            "metadata": {"fc": {"kind": kind, "owner": owner, **fc}},
        }
    )


class MemoryLedger(Ledger):
    """Only record storage and explicit dependency rows; no command emulator."""

    def __init__(self, *records):
        self.workspace = BRAIN
        self.rows = {row.id: deepcopy(row) for row in records}
        self.edges = {}
        self.writes = []

    def show(self, record_id):
        return deepcopy(self.rows.get(record_id))

    def list_records(self, *, kind=None, limit=0):
        return [
            deepcopy(row)
            for row in self.rows.values()
            if kind is None or row.kind == kind
        ]

    def create_record(self, *, record_id, kind, title, description, owner, fc):
        if record_id in self.rows:
            raise AssertionError(f"unexpected duplicate storage write: {record_id}")
        value = LedgerRecord.from_native(
            {
                "id": record_id,
                "title": title,
                "description": description,
                "status": "open",
                "assignee": owner,
                "metadata": {"fc": deepcopy(fc)},
            }
        )
        self.rows[record_id] = value
        self.writes.append(record_id)
        return deepcopy(value)

    def update_fc(self, record_id, fc, *, status=None):
        current = self.rows[record_id]
        value = replace(
            current,
            metadata={**current.metadata, "fc": deepcopy(fc)},
            status=status or current.status,
        )
        self.rows[record_id] = value
        self.writes.append(record_id)
        return deepcopy(value)

    def dependencies(self, record_id):
        return list(self.edges.get(record_id, []))

    def run(self, arguments, *, mutating=False):
        # Receipt finalization calls this one storage operation. Everything else
        # must be stubbed explicitly by its individual adapter test.
        if arguments[0] != "close" or not mutating:
            raise AssertionError(f"unexpected ledger command: {arguments}")
        row = self.rows[arguments[1]]
        self.rows[row.id] = replace(row, status="closed")
        return CommandObservation(None, 0, "", "", False)
