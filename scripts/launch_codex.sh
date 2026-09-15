#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
fulcrum=$repo_root/.venv/bin/fulcrum

if [ ! -x "$fulcrum" ]; then
  echo "fulcrum launch: development environment not found; run ./scripts/setup" >&2
  exit 1
fi

exec "$fulcrum" runtime launch-desktop "$@"
