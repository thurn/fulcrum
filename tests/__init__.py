"""Fast tests: process execution is forbidden except the local launcher stub."""

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import sys

_python = ContextVar("python", default=None)
_launcher = ContextVar("launcher", default=None)


def _audit(event, args):
    if event == "subprocess.Popen":
        if _python.get() is not None and args[1] == _python.get():
            return
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


@contextmanager
def local_python(command):
    """Allow this exact local interpreter command for process-boundary tests."""
    if command[:3] != [sys.executable, "-B", "-c"]:
        raise AssertionError("local Python probe must use the test interpreter")
    token = _python.set(command)
    try:
        yield
    finally:
        _python.reset(token)
