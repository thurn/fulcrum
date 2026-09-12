"""Command-line entry point for Fulcrum."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from fulcrum.brain import brain_status, initialize_brain
from fulcrum.config import resolve_paths
from fulcrum.context import read_task_context
from fulcrum.documents import discover_plans
from fulcrum.records import ProjectRegistryRecord, load_record
from fulcrum.resources import collect_resources
from fulcrum.state import atomic_write_record, read_record
from fulcrum.version import version_text


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level command parser."""

    parser = argparse.ArgumentParser(
        prog="fulcrum",
        description="Coordinate durable local agent workflows.",
    )
    parser.add_argument("--brain-root", help="override the configured brain root")
    parser.add_argument("--state-root", help="override the configured local state root")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("version", help="show package and source version")

    state_parser = subparsers.add_parser("state", help="read or write local records")
    state_commands = state_parser.add_subparsers(dest="state_command", required=True)
    read_parser = state_commands.add_parser("read", help="read one validated record")
    read_parser.add_argument("--kind", required=True, help="record kind")
    read_parser.add_argument("--id", help="record identifier for non-singletons")
    write_parser = state_commands.add_parser(
        "write", help="atomically write one record"
    )
    write_parser.add_argument("--input", required=True, help="UTF-8 JSON input file")

    context_parser = subparsers.add_parser("context", help="read concise task context")
    context_parser.add_argument("--task", required=True, help="exact Codex task ID")

    plans_parser = subparsers.add_parser("plans", help="discover Markdown plans")
    plans_commands = plans_parser.add_subparsers(dest="plans_command", required=True)
    list_plans = plans_commands.add_parser("list", help="list validated plan metadata")
    list_plans.add_argument("--project", help="limit results to one project ID")

    resources_parser = subparsers.add_parser(
        "resources", help="observe bounded local host and Tollgate resource facts"
    )
    resources_parser.add_argument(
        "--tollgate-repo", help="exact Tollgate repository ID to observe"
    )
    resources_parser.add_argument(
        "--owned-pid",
        action="append",
        default=[],
        type=int,
        help="owned process ID to include even when its command is not recognized",
    )

    brain_parser = subparsers.add_parser("brain", help="inspect or initialize Beads")
    brain_commands = brain_parser.add_subparsers(dest="brain_command", required=True)
    for name, help_text in (
        ("status", "verify server mode and connectivity"),
        ("init", "initialize a missing server-mode store, then verify it"),
    ):
        command = brain_commands.add_parser(name, help=help_text)
        command.add_argument(
            "--expected-remote",
            required=True,
            help="exact private Git remote expected for the brain",
        )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Fulcrum CLI."""

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "version":
        print(version_text())
        return 0
    if args.command == "resources":
        try:
            result = collect_resources(
                repository_id=args.tollgate_repo,
                owned_pids=args.owned_pid,
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        except Exception as error:
            print(f"fulcrum: {error}", file=sys.stderr)
            return 2
    if args.command in {"state", "context", "brain", "plans"}:
        try:
            paths = resolve_paths(
                brain_override=args.brain_root,
                state_override=args.state_root,
            )
            if args.command == "state" and args.state_command == "read":
                result = read_record(paths, args.kind, args.id)
            elif args.command == "state" and args.state_command == "write":
                input_record = load_record(Path(args.input))
                target = atomic_write_record(paths, input_record)
                result = {"ok": True, "path": str(target)}
            elif args.command == "context":
                result = read_task_context(paths, args.task)
            elif args.command == "plans":
                registry = read_record(paths, "project_registry")
                if registry["record_kind"] != "project_registry":
                    raise ValueError("expected project_registry record")
                project_registry = cast(ProjectRegistryRecord, registry)
                known_projects = {
                    project["project_id"] for project in project_registry["projects"]
                }
                result = discover_plans(paths.brain_root, known_projects)
                if args.project is not None:
                    result["plans"] = [
                        plan
                        for plan in result["plans"]
                        if plan["project"] == args.project
                    ]
            elif args.brain_command == "init":
                result = initialize_brain(paths.brain_root, args.expected_remote)
            else:
                result = brain_status(paths.brain_root, args.expected_remote)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        except Exception as error:
            print(f"fulcrum: {error}", file=sys.stderr)
            return 2
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
