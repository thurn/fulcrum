"""Operator controls for the stock Desktop broker and owned assets."""

from __future__ import annotations

import asyncio
import json
import os
import plistlib
import subprocess
import uuid
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from fulcrum.broker import broker_request
from fulcrum.configuration import ConfigurationManager
from fulcrum.contracts import CommandResult, FulcrumError, ParsedRequest
from fulcrum.desktop_protocol import DesktopProtocolService
from fulcrum.install import (
    InstalledService,
    InstallationError,
    inspect_service,
    reconcile_fulcrum2_skills,
)


def _services(root: Path) -> dict[str, InstalledService]:
    result: dict[str, InstalledService] = {}
    for path in sorted((root / "services").glob("*.plist")):
        try:
            value = plistlib.loads(path.read_bytes())
        except (OSError, plistlib.InvalidFileException):
            continue
        label = value.get("Label") if isinstance(value, Mapping) else None
        if isinstance(label, str) and label:
            result[path.stem] = InstalledService(path.stem, label, path)
    return result


def _launchctl(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", *arguments], capture_output=True, text=True, timeout=20
    )


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _start(service: InstalledService) -> dict[str, Any]:
    observed = inspect_service(service.label)
    if observed.running:
        return {"name": service.name, "state": "already_running", "pid": observed.pid}
    completed = _launchctl("bootstrap", _domain(), str(service.definition))
    if completed.returncode:
        raise FulcrumError(
            "SERVICE_START_FAILED",
            completed.stderr.strip() or completed.stdout.strip(),
            exit_code=4,
        )
    return {"name": service.name, "state": "started"}


def _stop(service: InstalledService) -> dict[str, Any]:
    observed = inspect_service(service.label)
    if not observed.loaded:
        return {"name": service.name, "state": "already_stopped"}
    completed = _launchctl("bootout", f"{_domain()}/{service.label}")
    if completed.returncode:
        raise FulcrumError(
            "SERVICE_STOP_FAILED",
            completed.stderr.strip() or completed.stdout.strip(),
            exit_code=4,
        )
    return {"name": service.name, "state": "stopped"}


async def _broker_health(path: Path) -> dict[str, Any]:
    try:
        result = await asyncio.wait_for(
            broker_request(path, {"type": "health"}), timeout=1
        )
        return {"available": True, **dict(result)}
    except Exception as error:
        return {"available": False, "state": "unavailable", "reason": str(error)}


def service_status_result(request: ParsedRequest) -> dict[str, Any]:
    installed = _services(request.instance.instance_root)
    rows: dict[str, Any] = {}
    for name, service in installed.items():
        observed = inspect_service(service.label)
        rows[name] = {
            "label": service.label,
            "definition": str(service.definition),
            "loaded": observed.loaded,
            "running": observed.running,
            "state": observed.state,
            "pid": observed.pid,
            "arguments": list(observed.program_arguments),
            "detail": observed.detail,
        }
    broker = asyncio.run(_broker_health(request.instance.instance_root / "broker.sock"))
    gaps: list[str] = []
    if "broker" not in installed:
        gaps.append("the owned broker service is not installed")
    if not broker["available"]:
        gaps.append("the broker socket did not answer its health probe")
    health_path = request.instance.instance_root / "diagnostic-health.json"
    try:
        diagnostic_health = json.loads(health_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        diagnostic_health = {"state": "healthy", "dropped_events": 0}
    return {
        "instance": str(request.instance.instance_root),
        "services": rows,
        "broker": broker,
        "diagnostic_health": diagnostic_health,
        "responsive": bool(broker["available"]),
        "gaps": gaps,
    }


class ServiceService:
    def status(self, request: ParsedRequest) -> CommandResult:
        return CommandResult.query(service_status_result(request))

    def start(self, request: ParsedRequest) -> CommandResult:
        services = _services(request.instance.instance_root)
        missing = sorted({"dolt", "broker"}.difference(services))
        if missing:
            raise FulcrumError(
                "SERVICE_NOT_INSTALLED",
                f"missing owned service definitions: {', '.join(missing)}",
                exit_code=4,
            )
        started = [_start(services[name]) for name in ("dolt", "broker")]
        return CommandResult.query(
            {"services": started, "shared_runtime_stopped": False}
        )

    def stop(self, request: ParsedRequest) -> CommandResult:
        services = _services(request.instance.instance_root)
        broker = services.get("broker")
        if broker is None:
            raise FulcrumError(
                "SERVICE_NOT_INSTALLED", "broker service is not installed", exit_code=4
            )
        health = asyncio.run(
            _broker_health(request.instance.instance_root / "broker.sock")
        )
        pending = health.get("pending")
        if pending and not request.arguments.get("interrupt"):
            raise FulcrumError(
                "SERVICE_BUSY",
                "pending responses must settle before broker handoff",
                exit_code=3,
                retryable=True,
                details={"pending": pending},
            )
        pause_request = replace(
            request,
            command=("pause",),
            input={"reason": "broker maintenance"},
            request_id=str(uuid.uuid5(uuid.UUID(str(request.request_id)), "pause")),
        )
        DesktopProtocolService().pause(pause_request)
        return CommandResult.query(
            {
                "broker": _stop(broker),
                "admission_paused": True,
                "dolt_stopped": False,
                "shared_runtime_stopped": False,
            }
        )

    def restart(self, request: ParsedRequest) -> CommandResult:
        self.stop(request)
        return self.start(request)

    def update(self, request: ParsedRequest) -> CommandResult:
        if not request.arguments.get("maintenance"):
            raise FulcrumError.invalid(
                "MAINTENANCE_REQUIRED",
                "connection-owner updates require --maintenance",
            )
        from fulcrum.activation import activate

        manager = ConfigurationManager(request.instance.config_path)
        document, _ = manager.load()
        source = manager.effective(document)["source"]
        repository = source.get("repository") or str(Path.home() / "fulcrum")
        result = activate(
            request.instance.instance_root,
            request.instance.config_path,
            {**dict(source), "repository": repository},
            maintenance=True,
            retry=bool(request.arguments.get("retry")),
        )
        return CommandResult.query(result)


class SkillsService:
    def reconcile(self, request: ParsedRequest) -> CommandResult:
        try:
            result = reconcile_fulcrum2_skills(
                request.instance.instance_root,
                production=not request.instance.explicit_selection,
            )
        except InstallationError as error:
            raise FulcrumError("ASSET_CONFLICT", str(error), exit_code=4) from error
        return CommandResult.query(result)
