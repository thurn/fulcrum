"""Small stdio MCP server exposing fresh Fulcrum command operations."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from fulcrum.broker import broker_request

PROTOCOL_VERSION = "2025-06-18"

TOOLS: dict[str, tuple[tuple[str, ...], dict[str, str]]] = {
    "register_standing": (("register", "standing"), {}),
    "wait_for_instructions": (("instruction", "wait"), {}),
    "claim_action": (
        ("action", "claim"),
        {"record_id": "--record-id", "action_id": "--action-id"},
    ),
    "report_action_result": (
        ("action", "result"),
        {"record_id": "--record-id", "action_id": "--action-id"},
    ),
    "register_worker": (("worker", "register"), {"bead": "--bead"}),
    "report_progress": (("progress",), {"bead": "--bead"}),
    "report": (("report",), {}),
    "submit_candidate": (("candidate", "submit"), {"bead": "--bead"}),
    "wait_for_ci_results": (("ci", "wait"), {"bead": "--bead"}),
    "finish": (("finish",), {"bead": "--bead"}),
    "marshal_check": (("marshal", "check"), {}),
    "marshal_decide": (("marshal", "apply"), {}),
    "report_incident": (("incident", "report"), {"bead": "--bead"}),
    "record_repair": (("repair", "record"), {"bead": "--bead"}),
    "recovery_prepare": (("recovery", "prepare"), {"bead": "--bead"}),
    "decision_respond": (("decision", "respond"), {"bead": "--bead"}),
    "pause": (("pause",), {}),
    "resume": (("resume",), {}),
    "status": (("status",), {}),
    "trace": (("trace",), {"bead": "--bead"}),
}

WAIT_TOOLS = {"wait_for_instructions", "wait_for_ci_results"}


def _schema(name: str) -> dict[str, Any]:
    required = ["request_id"] if name not in {"status", "trace"} else []
    properties: dict[str, Any] = {
        "request_id": {"type": "string", "format": "uuid"},
        "task_id": {"type": "string"},
        "host_id": {"type": "string"},
        "turn_id": {"type": "string"},
        "input": {"type": "object", "additionalProperties": True},
    }
    if name in {"claim_action", "report_action_result"}:
        properties.update(
            {"record_id": {"type": "string"}, "action_id": {"type": "string"}}
        )
        required.extend(("record_id", "action_id"))
    if name in {
        "register_worker",
        "report_progress",
        "submit_candidate",
        "wait_for_ci_results",
        "finish",
        "trace",
        "report_incident",
        "record_repair",
        "recovery_prepare",
        "decision_respond",
    }:
        properties["bead"] = {"type": "string"}
        required.append("bead")
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def tool_descriptions() -> list[dict[str, Any]]:
    descriptions = {
        "wait_for_instructions": "Steward only: wait for one exact recorded native action.",
        "wait_for_ci_results": "Warden only: wait for terminal evidence for the exact candidate.",
        "claim_action": "Claim one native invocation before executing it.",
        "report_action_result": "Record the actual result of one claimed native invocation.",
    }
    return [
        {
            "name": name,
            "description": descriptions.get(
                name, f"Run the durable Fulcrum {name.replace('_', ' ')} operation."
            ),
            "inputSchema": _schema(name),
        }
        for name in TOOLS
    ]


class FreshCli:
    def __init__(self, instance: Path, executable: Path) -> None:
        self.instance = instance
        self.executable = executable

    def invocation(
        self, name: str, supplied: Mapping[str, Any]
    ) -> tuple[list[str], str]:
        command, options = TOOLS[name]
        arguments = dict(supplied)
        request_id = arguments.pop("request_id", None)
        task_id = arguments.pop("task_id", None) or os.environ.get("CODEX_THREAD_ID")
        host_id = arguments.pop("host_id", None)
        turn_id = arguments.pop("turn_id", None)
        payload = arguments.pop("input", {})
        if not isinstance(payload, Mapping):
            raise ValueError("input must be an object")
        payload = {**dict(payload), **arguments}
        if host_id is not None:
            payload.setdefault("host_id", host_id)
        if turn_id is not None:
            payload.setdefault("turn_id", turn_id)
        argv = [
            str(self.executable),
            *command,
            "--instance",
            str(self.instance),
            "--json",
            "--input",
            "-",
        ]
        if request_id:
            argv.extend(("--request-id", str(request_id)))
        elif name not in {"status", "trace"}:
            argv.extend(("--request-id", str(uuid.uuid4())))
        if task_id:
            argv.extend(("--thread-id", str(task_id), "--actor", f"task:{task_id}"))
        for field, flag in options.items():
            value = payload.pop(field, None)
            if value is None:
                raise ValueError(f"{field} is required")
            argv.extend((flag, str(value)))
        return argv, json.dumps(payload, separators=(",", ":"))

    async def run(self, name: str, supplied: Mapping[str, Any]) -> Mapping[str, Any]:
        started = time.monotonic()
        argv, stdin = self.invocation(name, supplied)
        from fulcrum.broker import run_fresh

        first = await run_fresh(argv, stdin)
        payload = first.get("result")
        waiting = (
            payload.get("transport_wait") if isinstance(payload, Mapping) else None
        )
        if name not in WAIT_TOOLS or not isinstance(waiting, Mapping):
            _log(name, started, first)
            return first
        is_ci = waiting.get("kind") == "ci"
        result = await broker_request(
            self.instance / "broker.sock",
            {
                "type": "wait",
                "argv": argv,
                "stdin": stdin,
                "wait_id": waiting.get("wait_id"),
                "kind": waiting.get("kind"),
                "interval_seconds": 30 if is_ci else 15,
                "remaining_seconds": 1800 if is_ci else 3600,
            },
        )
        _log(name, started, result)
        return result


def _log(name: str, started: float, result: Mapping[str, Any]) -> None:
    print(
        json.dumps(
            {
                "event": "mcp_tool_completed",
                "component": "mcp",
                "tool": name,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "outcome": result.get("state"),
                "request_id": result.get("request_id"),
                "operation_id": result.get("operation_id"),
            },
            separators=(",", ":"),
        ),
        file=sys.stderr,
        flush=True,
    )


class McpServer:
    def __init__(self, cli: Any) -> None:
        self.cli = cli

    async def handle(self, message: Mapping[str, Any]) -> Mapping[str, Any] | None:
        identifier = message.get("id")
        method = message.get("method")
        if method == "notifications/initialized":
            return None
        if method == "initialize":
            result: Mapping[str, Any] = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "instructions": (
                    "Fulcrum tools enforce durable workflow authority. Execute only exact "
                    "returned actions: claim once, invoke once, report the actual result. "
                    "Never poll pending Steward or Warden waits or retry uncertain effects."
                ),
                "serverInfo": {
                    "name": "fulcrum",
                    "title": "Fulcrum",
                    "version": "stock-desktop",
                },
            }
        elif method == "tools/list":
            result = {"tools": tool_descriptions()}
        elif method == "tools/call":
            params = message.get("params")
            if not isinstance(params, Mapping):
                raise ValueError("tools/call params must be an object")
            name = str(params.get("name") or "")
            arguments = params.get("arguments")
            if name not in TOOLS or not isinstance(arguments, Mapping):
                raise ValueError("unknown tool or invalid arguments")
            value = await self.cli.run(name, arguments)
            result = {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(value, separators=(",", ":")),
                    }
                ],
                "structuredContent": value,
                "isError": not bool(value.get("ok")),
            }
        else:
            return {
                "jsonrpc": "2.0",
                "id": identifier,
                "error": {"code": -32601, "message": "method not found"},
            }
        return {"jsonrpc": "2.0", "id": identifier, "result": result}

    async def serve(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            raw = await loop.run_in_executor(None, sys.stdin.buffer.readline)
            if not raw:
                return
            try:
                value = json.loads(raw)
                if not isinstance(value, Mapping):
                    raise ValueError("JSON-RPC message must be an object")
                response = await self.handle(value)
            except BaseException as error:
                response = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32603, "message": str(error)},
                }
            if response is not None:
                sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
                sys.stdout.flush()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fulcrum-mcp")
    parser.add_argument("--instance", required=True)
    args = parser.parse_args(argv)
    instance = Path(args.instance).expanduser()
    if not instance.is_absolute():
        parser.error("--instance must be absolute")
    executable = Path.home() / "fulcrum" / ".venv" / "bin" / "fulcrum"
    if not executable.is_file():
        executable = Path(sys.executable).with_name("fulcrum")
    asyncio.run(McpServer(FreshCli(instance, executable)).serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
