"""Local command-line entry point for the Python-owned Fulcrum runtime."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from fulcrum.config import load_installation, resolve_paths
from fulcrum.controller import run_controller
from fulcrum.doctor import doctor
from fulcrum.ipc import request_sync
from fulcrum.setup import run_setup
from fulcrum.store import Store


def _thread_id() -> str | None:
    for name in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_TASK_ID"):
        value = os.environ.get(name)
        if value:
            return value
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fulcrum",
        description="Coordinate work through one local Python controller.",
    )
    parser.add_argument("--brain-root")
    parser.add_argument("--state-root")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="run the foreground controller")
    setup = commands.add_parser(
        "setup", help="install or resume the complete local runtime"
    )
    setup.add_argument("--config")
    setup.add_argument("--non-interactive", action="store_true")
    doctor_parser = commands.add_parser(
        "doctor", help="inspect actual installation capabilities"
    )
    doctor_parser.add_argument("--json", action="store_true")
    status = commands.add_parser("status", help="read current controller state")
    status.add_argument("--json", action="store_true")
    status.add_argument("--events", type=int, default=20)
    status.add_argument("--queue", action="store_true")
    status.add_argument("--capabilities", action="store_true")
    status.add_argument("--run", type=int)
    commands.add_parser("archon", help="locate the current Archon task")
    weaver = commands.add_parser("weaver", help="register a human-created Weaver task")
    weaver_sub = weaver.add_subparsers(dest="weaver_command", required=True)
    register = weaver_sub.add_parser("register")
    register.add_argument("--project")
    register.add_argument("--description", default="Task intake")
    register.add_argument("--model", default="gpt-5.6-sol")
    register.add_argument("--effort", default="high")
    register.add_argument("--plan-mode", action="store_true")
    intake = commands.add_parser(
        "intake", help="file one task or a complete task graph"
    )
    intake.add_argument("--project")
    intake.add_argument("--title")
    intake.add_argument("--description")
    intake.add_argument("--input")
    intake.add_argument("--intake-key")
    intake.add_argument("--depends-on", action="append", default=[])
    intake.add_argument("--context", action="append", default=[])
    intake.add_argument(
        "--activation", choices=("pending", "future"), default="pending"
    )
    for role in ("executor", "overseer"):
        intake.add_argument(f"--{role}-model")
        intake.add_argument(f"--{role}-reasoning-effort")
    finish = commands.add_parser("finish", help="submit the bound action's result")
    finish_sub = finish.add_subparsers(dest="outcome", required=True)
    for name in ("ready_for_review", "checkpointed", "future_plan"):
        item = finish_sub.add_parser(name)
        item.add_argument("--evidence", required=True)
    repair = finish_sub.add_parser("permitted_repair_complete")
    repair.add_argument("--repair-category", required=True)
    repair.add_argument("--repair-rationale", required=True)
    repair.add_argument("--evidence", required=True)
    for name in ("blocked", "exception"):
        item = finish_sub.add_parser(name)
        item.add_argument("--reason", required=True)
    approved = finish_sub.add_parser("approved")
    approved.add_argument("--assessment", required=True)
    approved.add_argument("--allow-repair", action="append", default=[])
    for name in (
        "changes_requested",
        "incomplete",
        "decisions",
        "report",
        "evidence_needed",
        "interview_answer",
    ):
        item = finish_sub.add_parser(name)
        item.add_argument("--input", required=True)
    deferred = finish_sub.add_parser("deferred")
    deferred.add_argument("--reason", required=True)
    deferred.add_argument("--input", required=True)
    finish_sub.add_parser("intake_complete")
    for name in ("sage", "inquisitor"):
        specialist = commands.add_parser(name, help=f"request a one-off {name} run")
        specialist.add_argument("--project")
        specialist.add_argument("--scope")
    reboot = commands.add_parser("reboot", help="replace the managed fleet")
    reboot_modes = reboot.add_mutually_exclusive_group(required=True)
    reboot_modes.add_argument("--soft", action="store_true")
    reboot_modes.add_argument("--hard", action="store_true")
    reboot_modes.add_argument("--reset", action="store_true")
    return parser


def _request(paths: Any, payload: dict[str, Any]) -> dict[str, Any]:
    payload.setdefault("thread_id", _thread_id())
    response = request_sync(paths.socket, payload)
    data = response.get("data")
    return data if isinstance(data, dict) else {"result": data}


def _intake_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.input:
        value = (
            json.load(sys.stdin)
            if args.input == "-"
            else json.loads(Path(args.input).read_text(encoding="utf-8"))
        )
        if not isinstance(value, dict):
            raise ValueError("intake input must contain an object")
        return value
    if not args.title or not args.description:
        raise ValueError("single-task intake requires --title and --description")
    result: dict[str, Any] = {
        "project": args.project,
        "title": args.title,
        "description": args.description,
        "activation": args.activation,
        "depends_on": args.depends_on,
        "context": args.context,
    }
    for name in (
        "executor_model",
        "executor_reasoning_effort",
        "overseer_model",
        "overseer_reasoning_effort",
    ):
        value = getattr(args, name)
        if value:
            result[name] = value
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = resolve_paths(
        brain_override=args.brain_root, state_override=args.state_root
    )
    try:
        if args.command == "serve":
            config = load_installation(paths.config_file)
            asyncio.run(run_controller(paths, config))
            return 0
        if args.command == "setup":
            result = run_setup(
                paths,
                input_path=Path(args.config) if args.config else None,
                non_interactive=args.non_interactive,
            )
        elif args.command == "doctor":
            result = doctor(paths)
        elif args.command == "status" and not paths.socket.exists():
            with Store(paths.database, readonly=True) as store:
                result = store.status(event_limit=args.events)
            result["stale"] = True
            result = _status_view(result, args)
        elif args.command == "status":
            result = _request(
                paths,
                {
                    "command": "status",
                    "events": args.events,
                    "view": (
                        "queue"
                        if args.queue
                        else "capabilities" if args.capabilities else "full"
                    ),
                    "run": args.run,
                },
            )
        elif args.command == "archon":
            result = _request(paths, {"command": "archon"})
        elif args.command == "weaver":
            result = _request(
                paths,
                {
                    "command": "weaver_register",
                    "project": args.project,
                    "description": args.description,
                    "model": args.model,
                    "effort": args.effort,
                    "writable": not args.plan_mode,
                },
            )
        elif args.command == "intake":
            payload = _intake_payload(args)
            if "tasks" in payload:
                result = _request(
                    paths,
                    {
                        "command": "intake_graph",
                        "graph": payload,
                        "intake_key": args.intake_key,
                    },
                )
            else:
                result = _request(
                    paths,
                    {
                        "command": "intake",
                        "task": payload,
                        "intake_key": args.intake_key,
                    },
                )
        elif args.command == "finish":
            options = {
                key: value
                for key, value in vars(args).items()
                if key not in {"command", "outcome", "brain_root", "state_root"}
                and value is not None
            }
            result = _request(
                paths,
                {"command": "finish", "outcome": args.outcome, "options": options},
            )
        elif args.command in {"sage", "inquisitor"}:
            scope = {
                "global": args.project is None,
                "projects": [args.project] if args.project else [],
            }
            result = _request(
                paths,
                {
                    "command": "specialist",
                    "kind": args.command,
                    "scope": json.dumps(scope),
                    "prompt": args.scope,
                },
            )
        elif args.command == "reboot":
            mode = "soft" if args.soft else "hard" if args.hard else "reset"
            result = _request(paths, {"command": "reboot", "mode": mode})
        else:
            parser.error("unsupported command")
            return 2
        print(json.dumps(result, indent=2, sort_keys=True))
        if args.command == "reboot" and result.get("complete") is True:
            return 0
        return 0 if result.get("ready", result.get("ok", True)) else 2
    except Exception as error:
        print(f"fulcrum: {error}", file=sys.stderr)
        return 2


def _status_view(result: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if args.queue:
        return {
            "dispatch_enabled": result.get("dispatch_enabled"),
            "assignments": result.get("assignments", []),
            "pending_updates": result.get("pending_updates", []),
            "holds": result.get("holds", []),
        }
    if args.capabilities:
        return {
            "controller_state": result.get("controller_state"),
            "dispatch_enabled": result.get("dispatch_enabled"),
            "projects": result.get("projects", []),
            "slot_usage": result.get("slot_usage", {}),
            "policies": result.get("policies", []),
        }
    if args.run is not None:
        result["runs"] = [
            row for row in result.get("runs", []) if row.get("id") == args.run
        ]
        result["assignments"] = [
            row
            for row in result.get("assignments", [])
            if row.get("run_id") == args.run
        ]
    return result


if __name__ == "__main__":
    raise SystemExit(main())
