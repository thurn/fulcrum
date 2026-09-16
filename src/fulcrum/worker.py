"""Fresh, source-pinned application execution, independent of CLI lifetime."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import uuid
from typing import Any

from fulcrum.bootstrap import pin
from fulcrum.contracts import ActorContext, ParsedRequest, FulcrumError
from fulcrum.coordination import ProcessLock
from fulcrum.instance import resolve_instance


def main() -> int:
    source = os.environ.get("FULCRUM_SOURCE")
    descriptor = pin(Path(source)) if source else None
    active: Path | None = None
    try:
        from fulcrum.application import default_application

        if len(sys.argv) > 1:
            kind, instance, config = sys.argv[1:4]
            context = resolve_instance(instance=instance, config=config)
            request = ParsedRequest(
                command=("service", "update") if kind == "update" else ("reconcile",),
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
        if kind == "background":
            assert request.instance.brain_root is not None
            with (
                ProcessLock(
                    request.instance.brain_root / ".fulcrum-locks" / "maintenance",
                    shared=True,
                ),
                ProcessLock(
                    request.instance.brain_root / ".fulcrum-locks" / "reconcile",
                    blocking=False,
                ),
            ):
                asyncio.run(background(request))
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


async def background(request: ParsedRequest) -> None:
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
    await asyncio.to_thread(supervisor.publication.tick, request)


if __name__ == "__main__":
    raise SystemExit(main())
