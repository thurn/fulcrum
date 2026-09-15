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
    if action in {"takeover", "repair", "release"} and "--offline" not in supplied:
        instance = _option(supplied, "--instance")
        config = _option(supplied, "--config")
        context = resolve_instance(
            instance=instance,
            config=config,
            allow_broken_config=action == "repair",
        )
        available = False
        if context.brain_root is not None:
            probe = ParsedRequest(
                command=("status",),
                arguments={"limit": 1},
                input={},
                actor=ActorContext(kind="human"),
                instance=context,
                request_id=None,
                timeout=2,
                offline=False,
            )
            try:
                response = request_sync(context.socket_path, probe.to_wire(), timeout=2)
                available = isinstance(response, dict)
            except ControllerUnavailable:
                pass
        if not available:
            controller = load_installed_services(context.instance_root).get(
                "controller"
            )
            if controller is not None and inspect_service(controller.label).running:
                _stop_one(controller)
            supplied.append("--offline")
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
