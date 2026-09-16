"""Fresh, source-pinned application execution, independent of CLI lifetime."""

from __future__ import annotations

from fulcrum.timing import timed

import asyncio
import json
import os
from pathlib import Path
import sys
import time
import uuid
from typing import Any

from fulcrum.bootstrap import pin
from fulcrum.contracts import ActorContext, ParsedRequest, FulcrumError
from fulcrum.coordination import ProcessLock
from fulcrum.instance import resolve_instance


@timed("worker.main")
def main() -> int:
    if len(sys.argv) > 1 and os.environ.get("FULCRUM_WORKER_SELECTED") != "1":
        from fulcrum.bootstrap import main as launch

        return launch(
            "fulcrum.worker",
            sys.argv[1:],
            Path(sys.argv[2]),
            Path(sys.argv[3]),
        )
    source = os.environ.get("FULCRUM_SOURCE")
    descriptor = pin(Path(source)) if source else None
    active: Path | None = None
    try:
        from fulcrum.application import default_application

        if len(sys.argv) > 1:
            kind, instance, config = sys.argv[1:4]
            context = resolve_instance(instance=instance, config=config)
            request = ParsedRequest(
                command=(
                    ("service", "update")
                    if kind == "update"
                    else ("ledger", "sync") if kind == "publication" else ("reconcile",)
                ),
                arguments={},
                input={},
                actor=ActorContext(kind="controller"),
                instance=context,
                request_id=str(uuid.uuid4()),
                timeout=30,
            )
        else:
            kind = "command"
            request = ParsedRequest.from_wire(json.loads(sys.stdin.read()))
        root = request.instance.instance_root / "active-operations"
        root.mkdir(parents=True, exist_ok=True)
        active = root / f"{os.getpid()}.json"
        active.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "source": source,
                    "commit": os.environ.get("FULCRUM_COMMIT"),
                    "request_id": request.request_id,
                    "kind": kind,
                }
            )
        )
        if kind in {"reconcile", "publication"}:
            assert request.instance.brain_root is not None
            with (
                ProcessLock(
                    request.instance.brain_root / ".fulcrum-locks" / "maintenance",
                    shared=True,
                ),
                ProcessLock(
                    request.instance.brain_root
                    / ".fulcrum-locks"
                    / ("reconcile" if kind == "reconcile" else "publication"),
                    blocking=False,
                ),
            ):
                if kind == "reconcile":
                    asyncio.run(reconcile_background(request))
                else:
                    publication_background(request)
            return 0
        result = default_application().dispatch(request).to_dict()
        try:
            print(json.dumps(result), flush=True)
        except BrokenPipeError:
            pass
        return 0 if result.get("ok", True) else 1
    except FulcrumError as error:
        try:
            print(json.dumps(error.to_result().to_dict()), flush=True)
        except BrokenPipeError:
            pass
        return error.exit_code
    finally:
        if active is not None:
            active.unlink(missing_ok=True)
        if descriptor is not None:
            os.close(descriptor)


async def reconcile_background(request: ParsedRequest) -> None:
    from fulcrum.application import default_application
    from fulcrum.resident_client import ResidentTransport, exchange
    from fulcrum.runtime import AppServerRuntime
    from fulcrum.supervision import ControllerSupervisor
    from fulcrum.transport import RuntimeEvent

    socket = request.instance.instance_root / "resident.sock"
    runtime = AppServerRuntime("resident", transport=ResidentTransport(socket))
    supervisor = ControllerSupervisor(request, default_application(), runtime=runtime)
    events = await exchange(socket, {"action": "events"})
    for identifier, value in events["events"]:
        await supervisor._record_runtime_event(RuntimeEvent(**value))
        await exchange(socket, {"action": "ack", "ids": [identifier]})
    await supervisor.run_once(keep_runtime=True)


def publication_background(request: ParsedRequest) -> None:
    from fulcrum.diagnostics import DiagnosticLog
    from fulcrum.publication import LedgerPublicationService

    log = DiagnosticLog.from_request(request)
    started = time.monotonic()
    started_event = log.append(
        {
            "event": "publication_started",
            "trigger": request.command_name,
        }
    )
    try:
        result = LedgerPublicationService().tick(request)
    except BaseException as error:
        log.append(
            {
                "event": "publication_failed",
                "publication_span_id": started_event["event_id"],
                "trigger": request.command_name,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "error_category": type(error).__name__,
                "error": str(error),
            }
        )
        raise
    log.append(
        {
            "event": "publication_completed",
            "publication_span_id": started_event["event_id"],
            "trigger": request.command_name,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "operation_id": _publication_operation_id(result),
            "associated_beads": sorted(_publication_beads(result)),
            "outcome": result.get("action"),
            "result": result,
        }
    )


def _publication_beads(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"bead_id", "IssueID", "issue_id"} and isinstance(item, str):
                found.add(item)
            elif key == "associated_beads" and isinstance(item, list):
                found.update(str(candidate) for candidate in item)
            else:
                found.update(_publication_beads(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_publication_beads(item))
    return found


def _publication_operation_id(value: Any) -> str | None:
    if isinstance(value, dict):
        operation_id = value.get("operation_id")
        if isinstance(operation_id, str):
            return operation_id
        for item in value.values():
            found = _publication_operation_id(item)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _publication_operation_id(item)
            if found is not None:
                return found
    return None


if __name__ == "__main__":
    raise SystemExit(main())
