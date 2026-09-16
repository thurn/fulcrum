"""Checkout- and controller-independent recovery command entry point."""

from __future__ import annotations

import sys
from collections.abc import Sequence

from fulcrum.contracts import ActorContext, ParsedRequest
from fulcrum.install import inspect_service
from fulcrum.installation_service import _stop_one, load_installed_services
from fulcrum.instance import resolve_instance
from fulcrum.ipc import ControllerUnavailable, request_sync


def main(argv: Sequence[str] | None = None) -> int:
    """Expose only the normal recovery command family from an isolated install."""

    from fulcrum.cli import main as fulcrum_main

    supplied = list(sys.argv[1:] if argv is None else argv)
    action = next(
        (
            item
            for item in supplied
            if item in {"inspect", "takeover", "repair", "release"}
        ),
        None,
    )
    return fulcrum_main(["recover", *supplied])


def _option(arguments: Sequence[str], name: str) -> str | None:
    for index, value in enumerate(arguments):
        if value == name and index + 1 < len(arguments):
            return arguments[index + 1]
        if value.startswith(name + "="):
            return value.split("=", 1)[1]
    return None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
