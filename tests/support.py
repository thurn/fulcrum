"""Small record storage double; workflow decisions remain production code."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import uuid
import tempfile

_TEMP = tempfile.TemporaryDirectory(prefix="fulcrum-tests-")
BRAIN = Path(_TEMP.name) / "brain"
BRAIN.mkdir()

from fulcrum.contracts import ActorContext, FulcrumError, InstanceContext, ParsedRequest
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
            socket_path=Path("/unused/instance/broker.sock"),
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
        self._observed = {}

    def show(self, record_id):
        return deepcopy(self.rows.get(record_id))

    def list_records(self, *, kind=None, limit=0, assignee=None):
        return [
            deepcopy(row)
            for row in self.rows.values()
            if (kind is None or row.kind == kind)
            and (assignee is None or row.assignee == assignee)
        ]

    def create_record(
        self, *, record_id, kind, title, description, owner, fc, **native
    ):
        if record_id in self.rows:
            raise FulcrumError(
                "REQUEST_CONFLICT",
                f"existing record {record_id} does not match planned creation",
                exit_code=5,
            )
        value = LedgerRecord.from_native(
            {
                "id": record_id,
                "title": title,
                "description": description,
                "status": "open",
                "assignee": owner,
                "metadata": {"fc": deepcopy(fc)},
                **native,
            }
        )
        self.rows[record_id] = value
        self.writes.append(record_id)
        return deepcopy(value)

    def update_fc(self, record_id, fc, *, status=None, assignee=None, **native):
        current = self.rows[record_id]
        value = replace(
            current,
            native={**current.native, **native},
            assignee=assignee if assignee is not None else current.assignee,
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
        if arguments[:2] == ("dep", "add") and mutating:
            self.edges.setdefault(arguments[2], []).append(arguments[3])
            return CommandObservation(None, 0, "", "", False)
        if arguments[:2] == ("dep", "remove") and mutating:
            self.edges.setdefault(arguments[2], []).remove(arguments[3])
            return CommandObservation(None, 0, "", "", False)
        if arguments[0] != "close" or not mutating:
            raise AssertionError(f"unexpected ledger command: {arguments}")
        row = self.rows[arguments[1]]
        self.rows[row.id] = replace(row, status="closed")
        return CommandObservation(None, 0, "", "", False)


def observe_action_prompt(
    ledger,
    action,
    *,
    task_id,
    session_id,
    turn_id="turn-registration",
    instance="/unused/instance",
):
    """Record the trusted prompt callback required by managed registration."""

    from fulcrum.desktop_protocol import action_marker
    from fulcrum.hooks import HookService

    hook_request = request(("hook", "handle"))
    HookService(ledger).handle(
        replace(
            hook_request,
            actor=ActorContext.parse(f"task:{task_id}"),
            thread_id=task_id,
            instance=replace(
                hook_request.instance,
                instance_root=Path(instance).resolve(strict=False),
            ),
            input={
                "hook_event_name": "UserPromptSubmit",
                "event_id": str(uuid.uuid4()),
                "thread_id": task_id,
                "session_id": session_id,
                "turn_id": turn_id,
                "prompt": action_marker(
                    str(Path(instance).resolve(strict=False)),
                    str(action["record_id"]),
                    str(action["action_id"]),
                    action.get("assignment_token"),
                ),
            },
            request_id=str(uuid.uuid4()),
        )
    )


def seed_action(ledger, payload):
    """Install a native action fixture without exposing a production queue API."""

    record_id = str(payload.get("record_id") or "fc-system")
    current = ledger.show(record_id)
    if current is None:
        current = ledger.create_record(
            record_id=record_id,
            kind="control" if record_id == "fc-system" else "work",
            title=record_id,
            description="test fixture",
            owner="SYSTEM",
            fc={"kind": "control", "owner": "SYSTEM", "desktop": {}},
        )
    fc = dict(current.fc or {})
    desktop = dict(fc.get("desktop") or {})
    actions = dict(desktop.get("actions") or {})
    action_id = f"action-{uuid.uuid4()}"
    action = {
        "action_id": action_id,
        "record_id": record_id,
        "executor": payload["executor"],
        "tool": payload["tool"],
        "arguments": deepcopy(dict(payload["arguments"])),
        "expected_result": deepcopy(payload.get("expected_result") or {}),
        "reporting": deepcopy(payload.get("reporting") or {}),
        "assignment_token": payload.get("assignment_token"),
        "state": "pending",
        "attempts": [],
        "created_at": "2026-09-16T00:00:00Z",
        "purpose": payload.get("purpose"),
    }
    actions[action_id] = action
    desktop["actions"] = actions
    fc["desktop"] = desktop
    ledger.update_fc(record_id, fc)
    return deepcopy(action)
