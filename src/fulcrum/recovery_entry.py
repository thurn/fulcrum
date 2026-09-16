"""Source-pinned recovery command entry point, independent of the resident."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Expose the recovery command family from the current local master source."""

    if not os.environ.get("FULCRUM_SOURCE"):
        from fulcrum.bootstrap import main as launch

        return launch(
            "fulcrum.recovery_entry", list(sys.argv[1:] if argv is None else argv)
        )

    from fulcrum.cli import main as fulcrum_main

    supplied = list(sys.argv[1:] if argv is None else argv)
    return fulcrum_main(["recover", *supplied])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
