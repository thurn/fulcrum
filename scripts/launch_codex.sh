#!/bin/sh
set -eu

expand_home() {
  case $1 in
    "~") printf '%s\n' "$HOME" ;;
    "~/"*) printf '%s/%s\n' "$HOME" "${1#\~/}" ;;
    *) printf '%s\n' "$1" ;;
  esac
}

if [ -n "${FULCRUM_CONTROL_ROOT:-}" ]; then
  control_root=$(expand_home "$FULCRUM_CONTROL_ROOT")
elif [ -n "${FULCRUM_CONFIG:-}" ]; then
  config_file=$(expand_home "$FULCRUM_CONFIG")
  control_root=$(dirname "$config_file")/control
else
  control_root=$HOME/Library/Application\ Support/Fulcrum/control
fi

launcher=$control_root/open-codex-with-fulcrum
if [ ! -x "$launcher" ]; then
  echo "fulcrum launch: installed launcher not found; run ./scripts/setup" >&2
  exit 1
fi

exec "$launcher" "$@"
