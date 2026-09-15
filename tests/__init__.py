"""Fast tests: process execution is forbidden except the local launcher stub."""

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import sys

_launcher = ContextVar("launcher", default=None)


def _audit(event, args):
    if event == "subprocess.Popen":
        allowed = _launcher.get()
        if (
            allowed is not None
            and args[0] == allowed
            and args[1] == [allowed, "--json"]
        ):
            return
        raise AssertionError(f"Tests must mock external processes: {args[0]}")
    if event in {"os.system", "os.posix_spawn", "os.exec", "os.fork", "os.forkpty"}:
        raise AssertionError(f"Tests must mock external processes: {event}")


sys.addaudithook(_audit)


@contextmanager
def local_launcher_stub(script: Path):
    """Allow precisely one copied launcher invoking its temporary shell stub."""
    token = _launcher.set(str(script))
    try:
        yield
    finally:
        _launcher.reset(token)
