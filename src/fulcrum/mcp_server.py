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
    "trace": (
        ("trace",),
        {
            "bead": "--bead",
            "operation": "--operation",
            "action": "--action",
            "task": "--task",
            "wait": "--wait-id",
        },
    ),
}

WAIT_TOOLS = {"wait_for_instructions", "wait_for_ci_results"}

CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "status": {"type": "string", "enum": ["passed", "failed", "not_run"]},
        "evidence": {"type": "string"},
    },
    "required": ["name", "status", "evidence"],
    "additionalProperties": False,
}

TOOL_INPUT_PROPERTIES: dict[str, dict[str, Any]] = {
    "register_standing": {
        "role": {
            "type": "string",
            "enum": ["steward", "marshal", "vizier"],
        },
        "action_id": {"type": "string"},
        "session_id": {"type": "string"},
    },
    "register_worker": {
        "workspace": {"type": "string"},
        "git_root": {"type": "string"},
        "source": {"type": "string"},
        "session_id": {"type": "string"},
        "branch": {"type": "string"},
    },
    "report_progress": {
        "kind": {
            "type": "string",
            "enum": ["source", "validation", "finding", "status"],
        },
        "summary": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
    },
    "submit_candidate": {
        "source": {"type": "string"},
    },
    "wait_for_ci_results": {"candidate_id": {"type": "string"}},
    "finish": {
        "outcome": {
            "type": "string",
            "enum": [
                "approved",
                "answered",
                "blocked",
                "completed",
                "findings",
                "planned",
                "ready",
                "ready_for_review",
                "repaired",
            ],
        },
        "summary": {"type": "string"},
        "source_oid": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
        "checks": {"type": "array", "items": CHECK_SCHEMA},
        "evidence": {"type": "array", "items": {"type": "string"}},
    },
}


def _schema(name: str) -> dict[str, Any]:
    required = ["request_id"] if name not in {"status", "trace"} else []
    properties: dict[str, Any] = {
        "request_id": {"type": "string", "format": "uuid"},
        "task_id": {"type": "string"},
        "host_id": {"type": "string"},
        "assignment_token": {"type": "string"},
        "input": {"type": "object", "additionalProperties": True},
    }
    if name in {
        "register_standing",
        "wait_for_instructions",
        "register_worker",
        "wait_for_ci_results",
        "marshal_check",
        "marshal_decide",
    }:
        properties["turn_id"] = {"type": "string"}
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
        "report_incident",
        "record_repair",
        "recovery_prepare",
        "decision_respond",
    }:
        properties["bead"] = {"type": "string"}
        required.append("bead")
    if name == "trace":
        for selector in ("bead", "operation", "action", "task", "wait"):
            properties[selector] = {"type": "string"}
    if name in {
        "register_worker",
        "report_progress",
        "submit_candidate",
        "wait_for_ci_results",
        "finish",
    }:
        required.append("assignment_token")
    properties.update(TOOL_INPUT_PROPERTIES.get(name, {}))
    required.extend(
        {
            "register_standing": [
                "role",
                "action_id",
                "task_id",
                "session_id",
            ],
            "register_worker": ["session_id", "turn_id"],
            "marshal_decide": ["turn_id"],
            "report_progress": ["kind", "summary", "evidence"],
            "submit_candidate": ["source"],
            "wait_for_ci_results": ["candidate_id"],
            "finish": ["outcome", "summary"],
        }.get(name, [])
    )
    schema = {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }
    if name == "trace":
        schema["oneOf"] = [
            {"required": [selector]}
            for selector in ("bead", "operation", "action", "task", "wait")
        ]
    return schema


def tool_descriptions() -> list[dict[str, Any]]:
    descriptions = {
        "register_standing": (
            "Bind this standing task using its lowercase role and the action_id "
            "from the Fulcrum-Action marker. Supply this task's CODEX_THREAD_ID as "
            "task_id and CODEX_SESSION_ID as session_id."
        ),
        "wait_for_instructions": "Steward only: wait for one exact recorded native action.",
        "wait_for_ci_results": "Warden only: wait for terminal evidence for the exact candidate.",
        "claim_action": "Claim one native invocation before executing it.",
        "report_action_result": "Record the actual result of one claimed native invocation.",
        "register_worker": (
            "Bind this native task to its reserved assignment before editing. "
            "Supply the exact workspace, Git root, and observed source when assigned."
        ),
        "report_progress": (
            "Record substantive assigned-work progress with a supported kind, summary, "
            "and nonempty evidence references."
        ),
        "submit_candidate": (
            "Warden only: submit one full exact source commit OID for configured local "
            "validation and provider CI."
        ),
        "finish": (
            "Seal the active role outcome. Executor uses ready_for_review and Warden "
            "uses approved; code outcomes also supply source_oid, exact check objects, "
            "and evidence references."
        ),
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
    def __init__(self, instance: Path, config: Path, executable: Path) -> None:
        self.instance = instance
        self.config = config
        self.executable = executable

    def invocation(
        self, name: str, supplied: Mapping[str, Any]
    ) -> tuple[list[str], str]:
        command, options = TOOLS[name]
        arguments = dict(supplied)
        request_id = arguments.pop("request_id", None)
        task_id = arguments.pop("task_id", None) or os.environ.get("CODEX_THREAD_ID")
        environment_task_id = os.environ.get("CODEX_THREAD_ID")
        supplied_task_id = supplied.get("task_id")
        if (
            environment_task_id
            and supplied_task_id
            and str(supplied_task_id) != environment_task_id
        ):
            raise ValueError("task_id does not match the current native task")
        host_id = arguments.pop("host_id", None)
        turn_id = arguments.pop("turn_id", None)
        payload = arguments.pop("input", {})
        if not isinstance(payload, Mapping):
            raise ValueError("input must be an object")
        payload = {**dict(payload), **arguments}
        if name == "register_standing":
            session_id = os.environ.get("CODEX_SESSION_ID") or task_id
            if session_id:
                payload.setdefault("session_id", session_id)
        assignment_token = payload.get("assignment_token")
        if host_id is not None:
            payload.setdefault("host_id", host_id)
        if turn_id is not None:
            payload.setdefault("turn_id", turn_id)
        argv = [
            str(self.executable),
            *command,
            "--instance",
            str(self.instance),
            "--config",
            str(self.config),
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
        if assignment_token:
            argv.extend(("--ownership-operation", str(assignment_token)))
        for field, flag in options.items():
            value = payload.pop(field, None)
            if value is None:
                if name == "trace":
                    continue
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
                "remaining_seconds": max(1, int(waiting.get("remaining_seconds") or 1))
                + 60,
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
                    "version": "fulcrum-desktop",
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
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    instance = Path(args.instance).expanduser()
    config = Path(args.config).expanduser()
    if not instance.is_absolute():
        parser.error("--instance must be absolute")
    if not config.is_absolute():
        parser.error("--config must be absolute")
    executable = Path.home() / "fulcrum" / ".venv" / "bin" / "fulcrum"
    if not executable.is_file():
        executable = Path(sys.executable).with_name("fulcrum")
    asyncio.run(McpServer(FreshCli(instance, config, executable)).serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
