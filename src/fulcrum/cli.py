"""Local command-line entry point for the Python-owned Fulcrum runtime."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from fulcrum.config import RuntimePaths, load_installation, resolve_paths
from fulcrum.controller import run_controller
from fulcrum.doctor import doctor
from fulcrum.ipc import ControllerUnavailable, request_sync
from fulcrum.operative import read_journal
from fulcrum.setup import require_setup_unfenced, run_setup
from fulcrum.store import Store

FINISH_REPORT_HINT = (
    "If you encountered pre-existing issues or tooling errors this session, invoke "
    "fulcrum report --help for filing instructions."
)

REPORT_DESCRIPTION = """File exactly one small, understood, implementation-ready follow-up Bead.

Use this for an incidental discovery from the current session: a pre-existing
defect, tooling failure, workflow friction, or another concrete problem outside
the assigned work. Do not use it to expand or replace the assigned work. File
independent problems with separate report invocations. If the work needs
substantial planning, multiple dependent tasks, or material clarification, use
$weaver instead. The installed $bead skill is the guided convenience path."""

REPORT_EPILOG = """Accepted stdin JSON schema (no other fields are accepted):
{
  "report_key": "new stable UUID for this one report and its exact retries",
  "project": "required only when Fulcrum cannot infer one project",
  "title": "concise implementation title",
  "problem": "the bounded problem",
  "observed_evidence": "specific repository or command evidence",
  "required_change": "the bounded change required",
  "acceptance_checks": ["observable validation check"],
  "dependencies": ["relevant existing Bead ID"],
  "context": ["other concise implementation context"]
}

Complete example:
  fulcrum report --input - <<'JSON'
  {
    "report_key": "7d42dd03-c8e6-4dd2-a4d7-642bbc349a37",
    "project": "fulcrum",
    "title": "Handle missing formatter binary",
    "problem": "scripts/check crashes when black is unavailable",
    "observed_evidence": "Running scripts/check exited 127 at the black command",
    "required_change": "Detect the missing tool and print the setup command",
    "acceptance_checks": [
      "A missing black executable produces an actionable nonzero diagnostic",
      "The normal scripts/check path still passes"
    ],
    "dependencies": [],
    "context": ["Discovered while validating unrelated assigned work"]
  }
  JSON

Reuse the same report_key and exact payload only when retrying the same
invocation. A separately discovered problem needs a new report_key and a separate
invocation. Fulcrum publishes through its controller-owned durable Beads path and
returns the actual Bead ID and publication state."""


def _nonempty_description(value: str) -> str:
    description = value.strip()
    if not description:
        raise argparse.ArgumentTypeError("description must not be empty")
    return description


def _safe_sage_description(value: str) -> str:
    description = _nonempty_description(value)
    if not re.fullmatch(r"[A-Za-z0-9]+(?:[ -][A-Za-z0-9]+)*", description):
        raise argparse.ArgumentTypeError(
            "description may contain only letters, numbers, spaces, and hyphens"
        )
    if not 3 <= len(description.split()) <= 8:
        raise argparse.ArgumentTypeError("description must contain 3-8 words")
    return description


def _sage_item_id(value: str) -> str:
    item = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+", item):
        raise argparse.ArgumentTypeError(
            "item must be an exact Bead ID containing letters, numbers, and hyphens"
        )
    return item


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
    usage = commands.add_parser("usage", help="query durable action token usage")
    usage.add_argument("--action", type=int)
    usage.add_argument("--task", type=int)
    usage.add_argument("--assignment", type=int)
    usage.add_argument("--run", type=int)
    usage.add_argument(
        "--role",
        choices=(
            "archon",
            "operative",
            "weaver",
            "executor",
            "overseer",
            "sage",
            "inquisitor",
        ),
    )
    usage.add_argument("--project")
    usage.add_argument(
        "--group-by",
        choices=("action", "task", "assignment", "run", "role", "project"),
        default="action",
    )
    cost = commands.add_parser(
        "cost", help="query frozen API-equivalent workflow cost estimates"
    )
    cost.add_argument("--action", type=int)
    cost.add_argument("--task", type=int)
    cost.add_argument("--assignment", type=int)
    cost.add_argument("--run", type=int)
    cost.add_argument(
        "--role",
        choices=(
            "archon",
            "operative",
            "weaver",
            "executor",
            "overseer",
            "sage",
            "inquisitor",
        ),
    )
    cost.add_argument("--project")
    cost.add_argument("--workflow")
    cost.add_argument(
        "--group-by",
        choices=("action", "task", "assignment", "run", "role", "project", "workflow"),
        default="action",
    )
    commands.add_parser(
        "context", help="show the current managed action's authoritative context"
    )
    commands.add_parser("archon", help="locate the current Archon task")
    resolve_operation = commands.add_parser(
        "resolve-operation",
        help="resolve an exhausted external operation without an Archon turn",
    )
    resolve_operation.add_argument("--operation-id", type=int, required=True)
    resolve_operation.add_argument(
        "--resolution",
        choices=("observed_success", "observed_failure", "confirmed_unsent"),
        required=True,
    )
    resolve_operation.add_argument("--evidence", required=True)
    resolve_operation.add_argument("--native-id")
    resolve_operation.add_argument(
        "--result", help="path to a JSON object with kind-specific observed results"
    )
    weaver = commands.add_parser("weaver", help="register a human-created Weaver task")
    weaver_sub = weaver.add_subparsers(dest="weaver_command", required=True)
    register = weaver_sub.add_parser("register")
    register.add_argument("--project")
    register.add_argument("--description", required=True, type=_nonempty_description)
    register.add_argument("--model", default="gpt-5.6-sol")
    register.add_argument("--effort", default="high")
    register.add_argument("--plan-mode", action="store_true")
    operative = commands.add_parser(
        "operative", help="manage a human-authorized emergency takeover"
    )
    operative_sub = operative.add_subparsers(dest="operative_command", required=True)
    operative_register = operative_sub.add_parser("register")
    operative_register.add_argument("--input", required=True)
    operative_sub.add_parser("probe")
    operative_sub.add_parser("status")
    operative_sub.add_parser("dossier")
    for name in (
        "wind-down",
        "reconcile",
        "worktree",
        "reinstall",
        "quarantine",
        "store-repair",
    ):
        control = operative_sub.add_parser(name)
        control.add_argument("--input", required=True)
    operative_sub.add_parser("repair-check")
    operative_sub.add_parser("service-check")
    operative_finish = operative_sub.add_parser("finish")
    operative_finish.add_argument("--input", required=True)
    operative_abort = operative_sub.add_parser("abort")
    operative_abort.add_argument("--input", required=True)
    operative_recover = operative_sub.add_parser("recover")
    operative_recover.add_argument("--input", required=True)
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
    report = commands.add_parser(
        "report",
        help="file one small implementation-ready follow-up",
        description=REPORT_DESCRIPTION,
        epilog=REPORT_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    report.add_argument(
        "--input",
        choices=("-",),
        help="read the report JSON object from stdin",
    )
    report.set_defaults(_report_parser=report)
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
    approved.add_argument("--input", required=True)
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
    sage = commands.add_parser("sage", help="register or request a Sage investigation")
    sage_sub = sage.add_subparsers(dest="sage_command", required=True)
    sage_register = sage_sub.add_parser(
        "register", help="adopt this task for one retained work-item investigation"
    )
    sage_register.add_argument("--item", required=True, type=_sage_item_id)
    sage_register.add_argument(
        "--description", required=True, type=_safe_sage_description
    )
    sage_request = sage_sub.add_parser(
        "request", help="queue the existing one-off Sage specialist"
    )
    sage_request.add_argument("--project")
    sage_request.add_argument("--scope")
    inquisitor = commands.add_parser(
        "inquisitor", help="request a one-off inquisitor run"
    )
    inquisitor.add_argument("--project")
    inquisitor.add_argument("--scope")
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


def _file_identity(value: os.stat_result) -> dict[str, int]:
    return {
        "device": value.st_dev,
        "inode": value.st_ino,
        "size": value.st_size,
        "mtime_ns": value.st_mtime_ns,
        "ctime_ns": value.st_ctime_ns,
    }


def _finish_options(args: argparse.Namespace, paths: RuntimePaths) -> dict[str, Any]:
    """Snapshot structured finish input before contacting the controller."""

    options = {
        key: value
        for key, value in vars(args).items()
        if key not in {"command", "outcome", "brain_root", "state_root"}
        and value is not None
    }
    input_path = options.get("input")
    if input_path is None:
        return options
    supplied = Path(str(input_path))
    if not supplied.is_absolute():
        raise ValueError("structured finish --input must be an absolute path")
    path = supplied.resolve(strict=False)
    handoff_root = paths.handoff_root.resolve(strict=False)
    if not path.is_relative_to(handoff_root):
        raise ValueError(f"structured finish --input must be beneath {handoff_root}")
    try:
        path_status = supplied.lstat()
    except FileNotFoundError:
        options.pop("input", None)
        options["input_path"] = str(path)
        options["input_missing"] = True
        return options
    except OSError as error:
        raise ValueError(
            f"cannot inspect structured finish input {path}: {error}"
        ) from error
    if stat.S_ISLNK(path_status.st_mode):
        raise ValueError(f"structured finish --input must not be a symlink: {path}")
    if not stat.S_ISREG(path_status.st_mode):
        raise ValueError(f"structured finish --input must be a regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(supplied, flags)
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            raw = handle.read()
            after = os.fstat(handle.fileno())
        if _file_identity(before) != _file_identity(after):
            raise ValueError(
                f"structured finish input changed while being read: {path}"
            )
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read valid JSON from {path}: {error}") from error
    except UnicodeDecodeError as error:
        raise ValueError(f"cannot read UTF-8 JSON from {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    options["input"] = payload
    options["input_path"] = str(path)
    options["input_identity"] = _file_identity(after)
    return options


def _report_payload(args: argparse.Namespace) -> dict[str, Any]:
    value = json.load(sys.stdin)
    if not isinstance(value, dict):
        raise ValueError("report input must contain a JSON object")
    return value


def _operative_control_input(path_value: str) -> tuple[dict[str, Any], str]:
    supplied = Path(path_value)
    if not supplied.is_absolute():
        raise ValueError("operative --input must be an absolute path")
    status = supplied.lstat()
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
        raise ValueError("operative --input must be a regular non-symlink file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(supplied, flags)
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        raw = handle.read(1_000_001)
        after = os.fstat(handle.fileno())
    if _file_identity(before) != _file_identity(after):
        raise ValueError("operative --input changed while being read")
    if len(raw) > 1_000_000:
        raise ValueError("operative --input exceeds the retained artifact bound")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(
            f"operative --input must contain UTF-8 JSON: {error}"
        ) from error
    if not isinstance(value, dict):
        raise ValueError("operative --input must contain a JSON object")
    return value, str(supplied.resolve(strict=True))


def _recovery_request(
    paths: RuntimePaths, command: str, input_path: str | None
) -> dict[str, Any]:
    launcher = paths.recovery_launcher
    if not os.access(launcher, os.X_OK):
        raise RuntimeError(
            f"controller request failed and recovery launcher is unavailable: {launcher}"
        )
    arguments = [str(launcher), command]
    if input_path is not None:
        arguments.extend(["--input", input_path])
    completed = subprocess.run(
        arguments, capture_output=True, text=True, check=False, timeout=300
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic"
        raise RuntimeError(f"recovery launcher failed: {detail}")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("recovery launcher returned invalid JSON") from error
    if not isinstance(result, dict):
        raise RuntimeError("recovery launcher returned a non-object result")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "report" and args.input is None:
        print(args._report_parser.format_help(), end="")
        return 0
    paths = resolve_paths(
        brain_override=args.brain_root, state_override=args.state_root
    )
    try:
        if args.command == "serve":
            config = load_installation(paths.config_file)
            asyncio.run(run_controller(paths, config))
            return 0
        if args.command == "setup":
            require_setup_unfenced(paths)
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
            try:
                journal = read_journal(paths.operative_journal)
                result["operative_journal_state"] = (
                    journal.get("state") if journal is not None else None
                )
                if journal is None and result.get("operative_takeover") is not None:
                    result["operative_journal_error"] = (
                        "unfinished SQLite takeover has no authoritative journal"
                    )
            except Exception as error:
                result["operative_journal_state"] = None
                result["operative_journal_error"] = str(error)
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
        elif args.command == "usage":
            query = {
                "command": "usage",
                "action_id": args.action,
                "task_id": args.task,
                "assignment_id": args.assignment,
                "run_id": args.run,
                "role": args.role,
                "project_id": args.project,
                "group_by": args.group_by,
            }
            if paths.socket.exists():
                result = _request(paths, query)
            else:
                with Store(paths.database, readonly=True) as store:
                    result = store.usage_report(
                        action_id=args.action,
                        task_id=args.task,
                        assignment_id=args.assignment,
                        run_id=args.run,
                        role=args.role,
                        project_id=args.project,
                        group_by=args.group_by,
                    )
        elif args.command == "cost":
            query = {
                "command": "cost",
                "action_id": args.action,
                "task_id": args.task,
                "assignment_id": args.assignment,
                "run_id": args.run,
                "role": args.role,
                "project_id": args.project,
                "workflow_id": args.workflow,
                "group_by": args.group_by,
            }
            if paths.socket.exists():
                result = _request(paths, query)
            else:
                with Store(paths.database, readonly=True) as store:
                    result = store.cost_report(
                        action_id=args.action,
                        task_id=args.task,
                        assignment_id=args.assignment,
                        run_id=args.run,
                        role=args.role,
                        project_id=args.project,
                        workflow_id=args.workflow,
                        group_by=args.group_by,
                    )
        elif args.command == "context":
            result = _request(paths, {"command": "context"})
        elif args.command == "archon":
            result = _request(paths, {"command": "archon"})
        elif args.command == "resolve-operation":
            decision: dict[str, Any] = {
                "operation_id": args.operation_id,
                "resolution": args.resolution,
                "evidence": args.evidence,
            }
            if args.native_id:
                decision["native_id"] = args.native_id
            if args.result:
                observed_result = json.loads(
                    Path(args.result).read_text(encoding="utf-8")
                )
                if not isinstance(observed_result, dict):
                    raise ValueError("--result must contain a JSON object")
                decision["result"] = observed_result
            result = _request(
                paths, {"command": "resolve_operation", "decision": decision}
            )
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
        elif args.command == "operative":
            command = args.operative_command
            wire_command = command.replace("-", "_")
            payload: dict[str, Any] = {"command": f"operative_{wire_command}"}
            input_path: str | None = None
            if hasattr(args, "input"):
                supplied, input_path = _operative_control_input(args.input)
                payload["input"] = supplied
                payload["input_path"] = input_path
                if command in {"register", "finish", "abort", "recover"}:
                    payload.update(supplied)
            try:
                if command in {"probe", "quarantine", "store-repair"}:
                    raise ControllerUnavailable(
                        "direct recovery control requires the controller fence"
                    )
                result = _request(paths, payload)
            except ControllerUnavailable:
                result = _recovery_request(paths, command, input_path)
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
        elif args.command == "report":
            result = _request(
                paths,
                {"command": "report", "report": _report_payload(args)},
            )
        elif args.command == "finish":
            options = _finish_options(args, paths)
            result = _request(
                paths,
                {"command": "finish", "outcome": args.outcome, "options": options},
            )
        elif args.command == "sage" and args.sage_command == "register":
            result = _request(
                paths,
                {
                    "command": "sage_register",
                    "item": args.item,
                    "description": args.description,
                },
            )
        elif args.command == "inquisitor" or (
            args.command == "sage" and args.sage_command == "request"
        ):
            kind = args.command
            scope = {
                "global": args.project is None,
                "projects": [args.project] if args.project else [],
            }
            result = _request(
                paths,
                {
                    "command": "specialist",
                    "kind": kind,
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
        if args.command == "finish" and result.get("ok") is True:
            print(FINISH_REPORT_HINT)
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
            "operative_takeover": result.get("operative_takeover"),
            "operative_journal_state": result.get("operative_journal_state"),
            "resource_admission_condition": result.get("resource_admission_condition"),
            "assignments": result.get("assignments", []),
            "pending_updates": result.get("pending_updates", []),
            "holds": result.get("holds", []),
        }
    if args.capabilities:
        return {
            "controller_state": result.get("controller_state"),
            "dispatch_enabled": result.get("dispatch_enabled"),
            "operative_takeover": result.get("operative_takeover"),
            "operative_journal_state": result.get("operative_journal_state"),
            "app_server_resources": result.get("app_server_resources"),
            "resource_admission_condition": result.get("resource_admission_condition"),
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
