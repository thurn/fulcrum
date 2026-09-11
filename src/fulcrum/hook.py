"""Placeholder hook entry point installed before hook behavior is introduced."""

from __future__ import annotations

from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Exit quietly until lifecycle hook behavior is implemented in Task 14."""

    del argv
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
