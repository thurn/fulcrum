"""Command-line entry point for Fulcrum."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from fulcrum.version import version_text


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level command parser."""

    parser = argparse.ArgumentParser(
        prog="fulcrum",
        description="Coordinate durable local agent workflows.",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("version", help="show package and source version")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Fulcrum CLI."""

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "version":
        print(version_text())
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
