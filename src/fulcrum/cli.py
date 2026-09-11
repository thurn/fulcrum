"""Command-line entry point for Fulcrum."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from fulcrum.config import resolve_paths
from fulcrum.context import read_task_context
from fulcrum.records import load_record
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Fulcrum CLI."""

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "version":
        print(version_text())
        return 0
    if args.command in {"state", "context"}:
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
            else:
                result = read_task_context(paths, args.task)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        except Exception as error:
            print(f"fulcrum: {error}", file=sys.stderr)
            return 2
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
