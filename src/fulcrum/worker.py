"""Fresh, source-pinned application execution, independent of CLI lifetime."""

from __future__ import annotations

from fulcrum.timing import timed

import json
import os
from pathlib import Path
import sys

from fulcrum.bootstrap import pin
from fulcrum.contracts import ParsedRequest, FulcrumError


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


if __name__ == "__main__":
    raise SystemExit(main())
